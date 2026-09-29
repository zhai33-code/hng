# -*- coding: utf-8 -*-
"""角色域（创建、统计、战斗徽章、教程完成）。

从 realtest_server.dispatch 机械迁移，函数体保持原样；
统一签名 (sess, writer, cmd_id, req_type, proto)。
"""


import time

from protocol import (
    build_response,
    pb_int32,
    pb_int64,
    pb_message,
    pb_parse,
    pb_parse_multi,
    pb_string,
    )

from state import (
    STORE,
    log,
    )

from wardb import (
    _WEAPON_RIBBONS,
    _mod_usable_by_class,
    _rank_from_combat_xp,
    daily_bonus_template_for,
    )

from messages import (
    _msg_character,
    _msg_character_stats_entry,
    _msg_depot_new_item,
    _msg_tracer_effect,
    _msg_uniform_part,
    _msg_uniform_variant,
    _msg_vehicle,
    _msg_vehicle_ammo,
    _msg_vehicle_upgrade,
    _msg_vehicle_variant,
    _msg_weapon,
    _msg_weapon_ammo,
    _msg_weapon_modifier,
    _msg_weapon_variant,
    _push_kvcs,
    )

from player_service import (
    _char_unlock_bits_for,
    _charge_player,
    _push_player_refresh,
    _weapon_template_id,
    )



async def handle_requestreadcharacterstats(sess, writer, cmd_id, req_type, proto):
    # ★ 2026-09-06 对齐官服：RequestReadCharacterStats { f1 repeated uint64
    #   characterids, f2 bool ignoreSessionId } —— 空 proto 回空 body、
    #   按请求 id ∩ 本人角色过滤（交集天然防越权：请求别人的角色 id 拿不到
    #   数据）。原实现忽略请求参数全量回发，宽松行为掩盖了 foreign_sync_probe
    #   空 proto bug，导致官服采集勋带/经验为空。
    pid = await sess.ensure_player()
    req_ids = []
    try:
        req_ids = [int(v) for v in pb_parse_multi(proto).get(1, [])
                   if isinstance(v, int)]
    except Exception as e:
        log(f'      ReadCharacterStats parse error: {e}')

    char_ids = []
    if req_ids and pid is not None:
        try:
            own = {int(ch['id']) for ch in await STORE.list_characters(pid)}
            char_ids = [cid for cid in req_ids if cid in own]
        except Exception as e:
            log(f'[DB] list_characters failed: {e}')

    if not char_ids:
        resp = build_response(cmd_id, 'ResponseReadCharacterStats', b'')
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} ResponseReadCharacterStats '
            f'(empty: req={req_ids}, 0 entries)')
        return

    resp_proto = b''
    for cid in char_ids:
        ribbons = {}
        combat_xp = 0
        cstats = {}
        if pid is not None:
            try:
                weapon_templates = [w['template_id'] for w in await STORE.list_weapons(pid)
                                    if int(w['character_id']) == int(cid)]
                ribbon_xps = await STORE.get_ribbon_xp(pid, cid)
                for wt in weapon_templates:
                    for rid in _WEAPON_RIBBONS.get(int(wt), ()):
                        ribbons[rid] = max(ribbons.get(rid, 0),
                                           ribbon_xps.get(rid, 0))
                # 已获得勋带经验也下发（即使未持有对应武器，如从商店进度窗口
                # 购买莫辛纳甘等未购武器的勋带；缺失会让客户端进度窗口
                # getRibbonProgressionAttributes 返回 null → 购买后 tween 不执行卡住）
                for rid, total_xp in ribbon_xps.items():
                    ribbons[rid] = max(ribbons.get(rid, 0), total_xp)
                combat_xp = await STORE.get_combat_xp(pid, cid)
            except Exception as e:
                log(f'[DB] load ribbon attributes failed: {e}')
            # ★ 2026-09-01 统计界面修复：character_stats 全量回读（type-6 战斗
            #   上报落库的 WIRE 名 + w/v/gm 维度键）→ 击杀/死亡/游玩时间/
            #   武器击杀等全部字段（此前只发 XP，其余 0 → 统计页全空）。
            #   构建逻辑与 game_server（hng.exe 路径）共用 messages 的映射表。
            try:
                cstats = await STORE.get_stats(int(cid)) or {}
            except Exception as e:
                log(f'[DB] get_stats failed (char={cid}): {e}')
        log(f'      stats entry char={cid}: ribbons={sorted(ribbons.items())} '
            f'combatXp={combat_xp} statsKeys={len(cstats)}')
        resp_proto += pb_message(1,
                                 _msg_character_stats_entry(cid, sorted(ribbons.items()),
                                                            combat_xp, cstats))

    resp = build_response(cmd_id, 'ResponseReadCharacterStats', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} ResponseReadCharacterStats ({len(char_ids)} entries, '
        f'characterIds={char_ids}, total {len(resp_proto)}B)')





async def handle_settutorialcompletedrequest(sess, writer, cmd_id, req_type, proto):
    # 标记教程完成请求：持久化 tutorials_completed=1 并推送 player KVCS。
    # 客户端 SoldierShopMediator.onPlayerChanged 在 tutorialsCompleted==1 时
    # dispatch DATA_DOWNLOADED_GAME_CLIENT 继续教程流程；不推送则 UI 卡在
    # 新手模式（招募页无等级选择、关闭按钮跳阵营选择页）。
    # SetTutorialCompletedRequest { required int32 tutorial = 1; }
    # SetTutorialCompletedResponse { required int32 result = 1; }
    try:
        f = pb_parse(proto)
        tutorial_id = f.get(1, 0)
        log(f'      SetTutorialCompleted: tutorial={tutorial_id}')
    except Exception as e:
        log(f'      SetTutorialCompleted parse error: {e}')

    pid = await sess.ensure_player()
    if pid is not None:
        try:
            await STORE.set_tutorials_completed(pid, 1)
        except Exception as e:
            log(f'[DB] set_tutorials_completed failed: {e}')

    resp_proto = pb_int32(1, 1)  # result = 1 (ok)
    resp = build_response(cmd_id, 'SetTutorialCompletedResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetTutorialCompletedResponse (result=1 ok)')

    # 推送 player KVCS（f32 tutorialsCompleted=1）触发客户端 onPlayerChanged
    await _push_player_refresh(sess, writer)



async def handle_set_combatbadge_request(sess, writer, cmd_id, req_type, proto):
    # 设置角色战斗徽章槽位
    # set_combatbadge_request { int64 characterid=1; int64 combatbadgeid=2; int32 slotnumber=3; }
    # set_combatbadge_response { int32 msg = 1; }
    badge_char_id = 0
    badge_id = 0
    badge_slot = 0
    try:
        f = pb_parse(proto)
        badge_char_id = f.get(1, 0)
        badge_id = f.get(2, 0)
        badge_slot = f.get(3, 0)
        log(f'      set_combatbadge: charId={badge_char_id}, badgeId={badge_id}, '
            f'slot={badge_slot}')
    except Exception as e:
        log(f'      set_combatbadge parse error: {e}')

    pid = await sess.ensure_player()
    if pid is not None:
        try:
            await STORE.set_combat_badge(pid, badge_char_id, badge_slot, badge_id)
        except Exception as e:
            log(f'[DB] set_combat_badge failed: {e}')

    resp_proto = pb_int32(1, 1)  # msg = 1 (ok)
    resp = build_response(cmd_id, 'set_combatbadge_response', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} set_combatbadge_response (msg=1 ok)')

    # 重推角色（combatbadgeslot0/1 更新）
    if sess.soldier_view_cmd is not None and pid is not None:
        try:
            for ch in await STORE.list_characters(pid):
                if int(ch['id']) != int(badge_char_id):
                    continue
                bits = await _char_unlock_bits_for(sess, ch['template'], ch['faction'], ch['id'])
                await _push_kvcs(writer, sess.soldier_view_cmd, b'character',
                                 _msg_character(ch['name'], ch['faction'], ch['template'],
                                                _rank_from_combat_xp(ch.get('combat_xp', 0)),
                                                ch['id'], bits,
                                                ch.get('combat_badge_0', 0),
                                                ch.get('combat_badge_1', 0),
                                                0,
                                                ch.get('uniform_variant_id', 0),
                                                ch.get('headgear_id', 0),
                                                1 if ch.get('is_deleted') else 0,
                                                ch.get('previous_template', 0),
                                                ch.get('head_id', 0),
                                                0,
                                                1 if ch.get('is_on_leave') else 0))
                log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet (character '
                    f'{ch["name"]} re-pushed after badge set)')
        except Exception as e:
            log(f'[DB] reload character after badge set failed: {e}')


async def _repush_character_row(sess, writer, ch, head_swap_type=0):
    """按 DB 行重推 character KVCS（含 U7 字段 markedDeleted/previousTemplate/
    headId/isOnLeave）。ch 为 list_characters/get_character_any 返回的 dict。"""
    if sess.soldier_view_cmd is None:
        return
    bits = await _char_unlock_bits_for(sess, ch['template'], ch['faction'], ch['id'])
    await _push_kvcs(writer, sess.soldier_view_cmd, b'character',
                     _msg_character(ch['name'], ch['faction'], ch['template'],
                                    _rank_from_combat_xp(ch.get('combat_xp', 0)),
                                    ch['id'], bits,
                                    ch.get('combat_badge_0', 0),
                                    ch.get('combat_badge_1', 0),
                                    0,
                                    ch.get('uniform_variant_id', 0),
                                    ch.get('headgear_id', 0),
                                    1 if ch.get('is_deleted') else 0,
                                    ch.get('previous_template', 0),
                                    ch.get('head_id', 0),
                                    head_swap_type,
                                    1 if ch.get('is_on_leave') else 0))




async def handle_deletecharacterrequest(sess, writer, cmd_id, req_type, proto):
    # 角色删除（软删，character KVCS f14 markedDeleted=1；最后一个角色不可删）
    # DeleteCharacterRequest { int64 characterId = 1; }
    # DeleteCharacterResponse { int32 response = 1; }
    try:
        f = pb_parse(proto)
        dc_char_id = f.get(1, 0)
        log(f'      DeleteCharacter: charId={dc_char_id}')
    except Exception as e:
        log(f'      DeleteCharacter parse error: {e}')
        dc_char_id = 0

    pid = await sess.ensure_player()
    ok = False
    if pid is not None and dc_char_id:
        try:
            ok, active_left = await STORE.soft_delete_character(pid, int(dc_char_id))
        except Exception as e:
            log(f'[DB] soft_delete_character failed: {e}')
            ok = False

    resp_proto = pb_int32(1, 1 if ok else 0)
    resp = build_response(cmd_id, 'DeleteCharacterResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} DeleteCharacterResponse (response={1 if ok else 0})')

    # 重推软删后的 character 行（markedDeleted=1，客户端从列表移除）
    if ok and pid is not None:
        try:
            ch = await STORE.get_character_any(pid, int(dc_char_id))
            if ch is not None:
                await _repush_character_row(sess, writer, ch)
        except Exception as e:
            log(f'[DB] reload character after delete failed: {e}')




async def handle_changetierrequest(sess, writer, cmd_id, req_type, proto):
    # 管理员工具 tier 切换（AdminWindowMediator；请求无有效载荷）
    # ChangeTierRequest { int64 dummy = 1; }
    # ChangeTierResponse { int32 result = 1; }
    resp_proto = pb_int32(1, 1)  # result = 1 (ok)
    resp = build_response(cmd_id, 'ChangeTierResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} ChangeTierResponse (result=1 ok)')


async def handle_setcharacteractivestatusrequest(sess, writer, cmd_id, req_type, proto):
    # 角色休假状态切换（character KVCS f24 isOnLeave）
    # SetCharacterActiveStatusRequest { int64 characterId=1; bool isOnLeave=2; }
    # SetCharacterActiveStatusResponse { int32 response=1; int32 soldierMax=2; }
    try:
        f = pb_parse(proto)
        sa_char_id = f.get(1, 0)
        sa_on_leave = bool(f.get(2, 0))
        log(f'      SetCharacterActiveStatus: charId={sa_char_id}, '
            f'isOnLeave={sa_on_leave}')
    except Exception as e:
        log(f'      SetCharacterActiveStatus parse error: {e}')
        sa_char_id = 0
        sa_on_leave = False

    pid = await sess.ensure_player()
    updated = False
    soldier_max = 0
    if pid is not None and sa_char_id:
        try:
            updated = await STORE.set_character_on_leave(pid, int(sa_char_id),
                                                         sa_on_leave)
            soldier_max = await STORE.count_active_characters(pid)
        except Exception as e:
            log(f'[DB] set_character_on_leave failed: {e}')

    resp_proto = pb_int32(1, 1 if updated else 0)
    resp_proto += pb_int32(2, soldier_max)
    resp = build_response(cmd_id, 'SetCharacterActiveStatusResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetCharacterActiveStatusResponse '
        f'(response={1 if updated else 0}, soldierMax={soldier_max})')

    # 重推 character（isOnLeave 更新）
    if updated and pid is not None:
        try:
            for ch in await STORE.list_characters(pid):
                if int(ch['id']) == int(sa_char_id):
                    await _repush_character_row(sess, writer, ch)
                    break
        except Exception as e:
            log(f'[DB] reload character after active status failed: {e}')


async def _repush_vehicle_row(sess, writer, pid, vehicle_id):
    """重推单条 Vehicle KVCS 及其挂靠行（皮肤/升级/弹药），仓库转运/指派/
    卸下升级件后客户端各表收敛用。

    SendItemToDepot / SetInventoryItemToCharacter / SendUpgradesToDepot 只回
    response 不推数据时，客户端列表等不到 characterId/vehicleId 变化 →
    UI"不走"→ 玩家连点重发（2026-08-27 La-7 重复扣费事故根因之一）。
    """
    if sess.soldier_view_cmd is None or pid is None or not vehicle_id:
        return
    try:
        v = await STORE.get_vehicle(pid, int(vehicle_id))
    except Exception as e:
        log(f'[DB] get_vehicle failed: {e}')
        return
    if v is None:
        return
    cmd = sess.soldier_view_cmd
    # 挂靠行（皮肤/升级/弹药）先推：Vehicle 行的 variant_id 引用 VehicleVariant
    # 实例，与登录回放推送顺序一致（先 VehicleVariant 后 Vehicle）
    try:
        for vv in await STORE.list_vehicle_variants(pid):
            if int(vv['vehicle_id']) != int(vehicle_id):
                continue
            await _push_kvcs(writer, cmd, b'VehicleVariant',
                             _msg_vehicle_variant(vv['id'], vv['character_id'],
                                                  vv['vehicle_id'],
                                                  vv['vehicle_variant_template_id']))
        for u in await STORE.list_vehicle_upgrades(pid):
            if int(u['vehicle_id']) != int(vehicle_id):
                continue
            await _push_kvcs(writer, cmd, b'VehicleUpgrade',
                             _msg_vehicle_upgrade(u['id'], u['upgrade_template_id'],
                                                  u['vehicle_id'], u['equipped_slot_id'],
                                                  u['character_id'],
                                                  u.get('condition', 100.0),
                                                  u.get('auto_repair', 0)))
        for a in await STORE.list_vehicle_ammo(pid):
            if int(a['vehicle_id']) != int(vehicle_id):
                continue
            await _push_kvcs(writer, cmd, b'VehicleAmmo',
                             _msg_vehicle_ammo(a['id'], a['ammo_template_id'],
                                               a['vehicle_id'],
                                               a['equipped_storage_id'],
                                               a['slot_index'], a['character_id'],
                                               a.get('condition', 100.0),
                                               a.get('auto_repair', 0)))
    except Exception as e:
        log(f'[DB] repush vehicle attached rows failed: {e}')
    vproto = _msg_vehicle(v['id'], v['vehicle_variant_template_id'],
                          v['character_id'], v['equipped_in_slot'],
                          v.get('variant_id', 0), v.get('auto_repair', 0),
                          v.get('condition', 100.0))
    await _push_kvcs(writer, cmd, b'Vehicle', vproto)
    log(f'S->C cmd={cmd:<4} KeyValueChangeSet '
        f'(Vehicle id={v["id"]}, charId={v["character_id"]}, '
        f'slot={v["equipped_in_slot"]} + attached rows)')


async def _repush_weapon_rows(sess, writer, pid, weapon_id):
    """重推单件武器的 Weapon KVCS 及其挂靠行（配件/皮肤/弹药），仓库转运/
    指派后客户端各表收敛用（对齐载具 _repush_vehicle_row；不推则武器列表
    UI 不走 → 连点重发，且本会话 unlockBits 不刷新）。"""
    if sess.soldier_view_cmd is None or pid is None or not weapon_id:
        return
    cmd = sess.soldier_view_cmd
    try:
        w = None
        for row in await STORE.list_weapons(pid):
            if int(row['id']) == int(weapon_id):
                w = row
                break
        if w is None:
            return
        # 皮肤实例先推（与登录回放顺序一致），再推 Weapon 行
        for vv in await STORE.list_weapon_variants(pid):
            if int(vv['weapon_id']) != int(weapon_id):
                continue
            await _push_kvcs(writer, cmd, b'WeaponVariant',
                             _msg_weapon_variant(vv['id'], vv['character_id'],
                                                 vv['weapon_id'],
                                                 vv['weapon_variant_template_id']))
        for m in await STORE.list_weapon_modifiers(pid):
            if int(m['weapon_id']) != int(weapon_id):
                continue
            await _push_kvcs(writer, cmd, b'WeaponModifier',
                             _msg_weapon_modifier(m['id'], m['template_id'],
                                                  m['character_id'], m['weapon_id'],
                                                  m.get('auto_repair', 0),
                                                  m.get('condition', 1.0)))
        for a in await STORE.list_weapon_ammo(pid):
            if int(a['weapon_id']) != int(weapon_id):
                continue
            await _push_kvcs(writer, cmd, b'WeaponAmmo',
                             _msg_weapon_ammo(a['id'], a['ammo_template_id'],
                                              a['character_id'], a['weapon_id'],
                                              a['slot_index'],
                                              a.get('condition', 100.0),
                                              a.get('auto_repair', 0)))
        wproto = _msg_weapon(w['id'], w['name'], w['template_id'],
                             w['character_id'], w['container_id'],
                             w['container_slot'], w.get('variant_id', 0),
                             w.get('clip_count', 0), w.get('auto_repair', 0),
                             w.get('condition', 1.0))
        await _push_kvcs(writer, cmd, b'weapon', wproto)
        log(f'S->C cmd={cmd:<4} KeyValueChangeSet (weapon id={w["id"]}, '
            f'charId={w["character_id"]}, slot={w["container_slot"]} '
            f'+ attached rows)')
    except Exception as e:
        log(f'[DB] repush weapon rows failed: {e}')


async def _repush_character_by_id(sess, writer, pid, char_id):
    """按 id 重推 character KVCS（unlockBits 刷新用：武器/弹药/配件归属变化后，
    不推则本会话内勾选框锁死 + "Ribbon required"，重登才恢复）。"""
    if sess.soldier_view_cmd is None or pid is None or not char_id:
        return
    try:
        for ch in await STORE.list_characters(pid):
            if int(ch['id']) == int(char_id):
                await _repush_character_row(sess, writer, ch)
                log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                    f'(character {ch["name"]} re-pushed after depot transfer)')
                break
    except Exception as e:
        log(f'[DB] repush character after depot transfer failed: {e}')


async def _repush_depot_new_item(sess, writer, pid):
    """重推 DepotNewItem 行（仓库红点即时点亮/消点用）。"""
    if sess.soldier_view_cmd is None or pid is None:
        return
    try:
        depot_new = await STORE.list_depot_new_items(pid)
    except Exception as e:
        log(f'[DB] list_depot_new_items failed: {e}')
        return
    new_item_str = '|'.join(
        f'{tab}_{",".join(str(i) for i in ids)}'
        for tab, ids in sorted(depot_new.items()))
    await _push_kvcs(writer, sess.soldier_view_cmd, b'DepotNewItem',
                     _msg_depot_new_item(int(pid), int(pid), new_item_str))
    log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
        f'(DepotNewItem: {len(depot_new)} tabs, '
        f'{sum(len(v) for v in depot_new.values())} unviewed)')


# itemType 字符串（SetInventoryItemToCharacterRequest f1，客户端
# buildSendItemToSoldierRequest 的取值）→ ItemType 枚举 int
_DEPOT_STR_TYPE = {
    'weapon': 1, 'vehicle': 2, 'uniformpart': 3, 'uniformvariant': 4,
    'weaponmodifier': 5, 'weaponammo': 6, 'weaponvariant': 7,
    'vehicleupgrade': 8, 'vehicleammo': 9, 'vehiclevariant': 10,
    # 客户端 ad-hoc 值（ItemInfoOverlayMediator.buildSendItemToSoldierRequest
    # case 42 → "tracereffect"；WeaponDepotViewMediator 同用 42）
    'tracereffect': 42,
}


async def _repush_item_row(sess, writer, pid, item_type, item_id):
    """按 ItemType（int 枚举或小写字符串）重推单件物品 KVCS 行（仓库转运/
    指派后客户端各表收敛用）。

    官方客户端所有 depot/装备列表监听对应表（WeaponModifier/WeaponAmmo/
    WeaponVariant/UniformPart/UniformVariant/VehicleUpgrade/VehicleAmmo/
    VehicleVariant/...）的 changeSignal，只回 response 不推行 → 列表不动
    → 玩家连点重发（2026-08-27 La-7 事故同根因；此前只覆盖 weapon/vehicle
    两类，其余 itemType 一律不推）。weapon/vehicle 走带挂靠行的整组重推。
    """
    if sess.soldier_view_cmd is None or pid is None or not item_id:
        return
    tid = item_type if isinstance(item_type, int) else \
        _DEPOT_STR_TYPE.get(str(item_type).strip().lower())
    if tid is None:
        return
    cmd = sess.soldier_view_cmd
    try:
        if tid == 1:
            await _repush_weapon_rows(sess, writer, pid, int(item_id))
            return
        if tid == 2:
            await _repush_vehicle_row(sess, writer, pid, int(item_id))
            return
        proto = None
        tname = b''
        if tid == 3:
            for up in await STORE.list_uniform_parts(pid):
                if int(up['id']) == int(item_id):
                    tname, proto = b'UniformPart', _msg_uniform_part(
                        up['id'], up['character_id'],
                        up['uniform_part_template_id'], up['uniform_part_type'])
                    break
        elif tid == 4:
            for uv in await STORE.list_uniform_variants(pid):
                if int(uv['id']) == int(item_id):
                    tname, proto = b'UniformVariant', _msg_uniform_variant(
                        uv['id'], uv['uniform_variant_template_id'],
                        uv['character_id'])
                    break
        elif tid == 5:
            for m in await STORE.list_weapon_modifiers(pid):
                if int(m['id']) == int(item_id):
                    tname, proto = b'WeaponModifier', _msg_weapon_modifier(
                        m['id'], m['template_id'], m['character_id'],
                        m['weapon_id'], m.get('auto_repair', 0),
                        m.get('condition', 1.0))
                    break
        elif tid == 6:
            for a in await STORE.list_weapon_ammo(pid):
                if int(a['id']) == int(item_id):
                    tname, proto = b'WeaponAmmo', _msg_weapon_ammo(
                        a['id'], a['ammo_template_id'], a['character_id'],
                        a['weapon_id'], a['slot_index'],
                        a.get('condition', 100.0), a.get('auto_repair', 0))
                    break
        elif tid == 7:
            for vv in await STORE.list_weapon_variants(pid):
                if int(vv['id']) == int(item_id):
                    tname, proto = b'WeaponVariant', _msg_weapon_variant(
                        vv['id'], vv['character_id'], vv['weapon_id'],
                        vv['weapon_variant_template_id'])
                    break
        elif tid == 8:
            for u in await STORE.list_vehicle_upgrades(pid):
                if int(u['id']) == int(item_id):
                    tname, proto = b'VehicleUpgrade', _msg_vehicle_upgrade(
                        u['id'], u['upgrade_template_id'], u['vehicle_id'],
                        u['equipped_slot_id'], u['character_id'],
                        u.get('condition', 100.0), u.get('auto_repair', 0))
                    break
        elif tid == 9:
            for a in await STORE.list_vehicle_ammo(pid):
                if int(a['id']) == int(item_id):
                    tname, proto = b'VehicleAmmo', _msg_vehicle_ammo(
                        a['id'], a['ammo_template_id'], a['vehicle_id'],
                        a['equipped_storage_id'], a['slot_index'],
                        a['character_id'], a.get('condition', 100.0),
                        a.get('auto_repair', 0))
                    break
        elif tid == 10:
            for vv in await STORE.list_vehicle_variants(pid):
                if int(vv['id']) == int(item_id):
                    tname, proto = b'VehicleVariant', _msg_vehicle_variant(
                        vv['id'], vv['character_id'], vv['vehicle_id'],
                        vv['vehicle_variant_template_id'])
                    break
        elif tid == 42:
            # 曳光弹实例行（指派/退仓后 TracerEffectTable 收敛：客户端按
            # characterId 分流仓库列表与角色名下）
            for t in await STORE.list_tracer_effects(pid):
                if int(t['id']) == int(item_id):
                    tname, proto = b'TracerEffect', _msg_tracer_effect(
                        t['id'], t['tracer_template_id'], t['character_id'],
                        t['weapon_id'], t['is_equipped'])
                    break
        if proto is not None:
            await _push_kvcs(writer, cmd, tname, proto)
            log(f'S->C cmd={cmd:<4} KeyValueChangeSet ({tname.decode()} '
                f'id={item_id} re-pushed after depot transfer)')
    except Exception as e:
        log(f'[DB] repush item row failed: {e}')


async def handle_setinventoryitemtocharacterrequest(sess, writer, cmd_id, req_type, proto):
    # 装备/仓库物品指派到角色（仓库物品栏拖拽）
    # SetInventoryItemToCharacterRequest { string itemType=1; int64 itemId=2; int64 characterId=3; }
    # SetInventoryItemToCharacterResponse { int32 msg = 1; }
    try:
        f = pb_parse(proto)
        si_item_type = f.get(1, b'')
        if isinstance(si_item_type, bytes):
            si_item_type = si_item_type.decode('utf-8', 'replace')
        si_item_id = f.get(2, 0)
        si_char_id = f.get(3, 0)
        log(f'      SetInventoryItemToCharacter: itemType={si_item_type!r}, '
            f'itemId={si_item_id}, charId={si_char_id}')
    except Exception as e:
        log(f'      SetInventoryItemToCharacter parse error: {e}')
        si_item_type = ''
        si_item_id = 0
        si_char_id = 0

    pid = await sess.ensure_player()
    # 指派前捕获原属角色（指派后归属已变无法反查；原角色 unlockBits 里
    # 弹药/配件/武器的 uid 要摘掉，不重推则原角色本会话解锁状态错乱）
    prev_char = None
    _si_tid = _DEPOT_STR_TYPE.get(str(si_item_type).strip().lower())
    if pid is not None and si_item_id and _si_tid:
        try:
            prev_char = await STORE.get_item_character(pid, _si_tid,
                                                       int(si_item_id))
        except Exception as e:
            log(f'[DB] get_item_character failed: {e}')
    updated = False
    if pid is not None and si_item_id:
        try:
            updated = await STORE.set_inventory_item_character(
                pid, si_item_type, int(si_item_id), int(si_char_id))
        except Exception as e:
            log(f'[DB] set_inventory_item_character failed: {e}')

    # 兵种专属配件剥离兜底（2026-09-04 98k 事故：侦察带镜武器入库再指派
    # 步兵，配件被无差别带走 → 步兵拿到侦察专属瞄具）。2026-09-13 入库
    # 解绑改造后正常流程指派时武器已无挂靠配件（store 层兜底清挂靠），
    # 此段仅防御历史脏数据/异常写入：仍挂在目标武器上的配件按目标兵种
    # 过滤，不兼容的剥离回仓库（weapon_id/character_id 清 0，单件版
    # SendUpgradesToDepot）。无映射/无需求（即买即用）配件不限制。
    stripped_mods = []
    if updated and pid is not None and si_char_id and _si_tid == 1:
        try:
            faction = template = 0
            for ch in await STORE.list_characters(pid):
                if int(ch['id']) == int(si_char_id):
                    faction, template = int(ch['faction']), int(ch['template'])
                    break
            wtemplate = await _weapon_template_id(sess, int(si_item_id))
            if faction and wtemplate:
                for m in await STORE.list_weapon_modifiers(pid):
                    if int(m['weapon_id']) != int(si_item_id):
                        continue
                    if not _mod_usable_by_class(m['template_id'], wtemplate,
                                                faction, template):
                        stripped_mods.append(m)
        except Exception as e:
            log(f'[DB] mod class-compat check failed: {e}')
        if stripped_mods:
            try:
                await STORE.detach_weapon_modifiers(
                    pid, [m['id'] for m in stripped_mods])
                log(f'      stripped {len(stripped_mods)} class-incompatible '
                    f'mod(s) from weapon {si_item_id} -> depot')
            except Exception as e:
                log(f'[DB] detach_weapon_modifiers failed: {e}')
                stripped_mods = []

    resp_proto = pb_int32(1, 1 if updated else 0)
    resp = build_response(cmd_id, 'SetInventoryItemToCharacterResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetInventoryItemToCharacterResponse '
        f'(msg={1 if updated else 0})')

    if updated:
        # 重推物品自身行（weapon/vehicle 带挂靠行整组，其余 itemType 单行）：
        # 客户端各表只认 KVCS UPDATE，不推则 depot/装备列表双不刷新（玩家
        # 连点重发；此前只覆盖 weapon/vehicle 两类，制服/配件/皮肤/弹药/
        # 载具件指派一律不推 → 官方对齐补全）
        await _repush_item_row(sess, writer, pid, si_item_type, si_item_id)
        # 剥离回仓库的配件行（weapon_id 已清 0，_repush_weapon_rows 按
        # weapon_id 过滤不会带上）需显式重推，否则客户端仍显示挂在武器上
        if stripped_mods and sess.soldier_view_cmd is not None:
            for m in stripped_mods:
                await _push_kvcs(writer, sess.soldier_view_cmd, b'WeaponModifier',
                                 _msg_weapon_modifier(m['id'], m['template_id'],
                                                      0, 0,
                                                      m.get('auto_repair', 0),
                                                      m.get('condition', 1.0)))
        # 重推 character KVCS：unlockBits 随指派增减（武器+随行弹药/配件
        # 指派到角色 → 增加对应 uid；原角色 → 摘掉 uid。不推则本会话内
        # 弹药勾选框锁死 + "Ribbon required"，重登才恢复）
        if prev_char:
            await _repush_character_by_id(sess, writer, pid, prev_char)
        if si_char_id:
            await _repush_character_by_id(sess, writer, pid, si_char_id)


# 余额不足 response 码按支付货币映射（paymentCurrency 枚举：1=credits 2=warfunds
# 3=gold；客户端 ResponseCodes not_enough_credits=56 / not_enough_gold=57 /
# not_enough_warfunds=153，handleSendItemToDepotRequest 按码弹对应货币提示）
_NOT_ENOUGH_RESP = {1: 56, 2: 153, 3: 57}


async def handle_senditemtodepotrequest(sess, writer, cmd_id, req_type, proto):
    # 物品送回仓库（物品栏 → 仓库；character_id 清 0）
    # SendItemToDepotRequest { int32 itemType=1; int64 objectId=2; int64 pricingId=3;
    #   int64 storeDiscountId=4; int32 paymentCurrency=5; string tabName=6; bool isFree=7; }
    # SendItemToDepotResponse { int32 response = 1; }
    try:
        f = pb_parse(proto)
        sd_item_type = f.get(1, 0)
        sd_object_id = f.get(2, 0)
        sd_pricing_id = f.get(3, 0)
        sd_currency = f.get(5, 0)
        sd_tab = f.get(6, b'')
        if isinstance(sd_tab, bytes):
            sd_tab = sd_tab.decode('utf-8', 'replace')
        sd_is_free = bool(f.get(7, 1))
        log(f'      SendItemToDepot: itemType={sd_item_type}, objectId={sd_object_id}, '
            f'pricingId={sd_pricing_id}, tab={sd_tab!r}, isFree={sd_is_free}')
    except Exception as e:
        log(f'      SendItemToDepot parse error: {e}')
        sd_item_type = 0
        sd_object_id = 0
        sd_pricing_id = 0
        sd_currency = 0
        sd_tab = ''
        sd_is_free = True

    pid = await sess.ensure_player()

    # 幂等预检（2026-08-27 La-7 事故：客户端收不到 Vehicle 重推导致连点 4 次，
    # 每次都被重复扣全价转运费）：物品已在仓库（character_id=0）时直接成功返回，
    # 不扣款不重写；物品不存在（None）时同样跳过扣款（避免钱货两空）
    cur_char = None
    if pid is not None and sd_object_id:
        try:
            cur_char = await STORE.get_item_character(pid, int(sd_item_type),
                                                      int(sd_object_id))
        except Exception as e:
            log(f'[DB] get_item_character failed: {e}')
    if cur_char == 0:
        resp_proto = pb_int32(1, 1)
        resp = build_response(cmd_id, 'SendItemToDepotResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} SendItemToDepotResponse '
            f'(already in depot, no charge)')
        # 仍重推行数据让客户端列表收敛（连点的根因就是收不到这条推送）
        await _repush_item_row(sess, writer, pid, int(sd_item_type),
                               sd_object_id)
        return

    # 扣款（非免费存放时收存放费 = 定价 50%，与客户端 ItemInfoOverlayMediator
    # setTransferPrice 的 finalGoldFractionPrice(0.5)/finalCreditFractionPrice(0.5)
    # 一致；此前扣全价，确认框显示半价、实际扣款翻倍）
    if not sd_is_free and sd_pricing_id and cur_char is not None:
        cost, ok = await _charge_player(sess, sd_pricing_id, sd_currency,
                                        fraction=0.5)
        if not ok:
            # 余额不足 response 码按支付货币映射（客户端 handleSendItemToDepotRequest
            # 区分 56/57/153 弹对应货币的不足提示，此前一律 56 金币不足时提示错误）
            resp_proto = pb_int32(1, _NOT_ENOUGH_RESP.get(int(sd_currency), 56))
            resp = build_response(cmd_id, 'SendItemToDepotResponse', resp_proto)
            writer.write(resp)
            await writer.drain()
            log(f'S->C cmd={cmd_id:<4} SendItemToDepotResponse '
                f'(response={_NOT_ENOUGH_RESP.get(int(sd_currency), 56)} '
                f'not_enough) cost={cost}')
            return

    # 入库前捕获武器/载具全部挂靠件（入库即解绑为独立仓库件，2026-09-13
    # 改造；解绑后 weapon_id/vehicle_id 清 0 无法反查归属，需先查后改）
    attached_mods = []    # 武器配件
    attached_wammo = []   # 武器弹药
    attached_wvar = []    # 武器皮肤
    attached_vupg = []    # 载具升级
    attached_ammo = []    # 载具弹药
    attached_vvar = []    # 载具皮肤
    if pid is not None and sd_object_id and cur_char:
        try:
            if int(sd_item_type) == 1:
                for m in await STORE.list_weapon_modifiers(pid):
                    if int(m['weapon_id']) == int(sd_object_id):
                        attached_mods.append(m)
                for a in await STORE.list_weapon_ammo(pid):
                    if int(a['weapon_id']) == int(sd_object_id):
                        attached_wammo.append(a)
                for vv in await STORE.list_weapon_variants(pid):
                    if int(vv['weapon_id']) == int(sd_object_id):
                        attached_wvar.append(vv)
            elif int(sd_item_type) == 2:
                for u in await STORE.list_vehicle_upgrades(pid):
                    if int(u['vehicle_id']) == int(sd_object_id):
                        attached_vupg.append(u)
                for a in await STORE.list_vehicle_ammo(pid):
                    if int(a['vehicle_id']) == int(sd_object_id):
                        attached_ammo.append(a)
                for vv in await STORE.list_vehicle_variants(pid):
                    if int(vv['vehicle_id']) == int(sd_object_id):
                        attached_vvar.append(vv)
        except Exception as e:
            log(f'[DB] list attached rows failed: {e}')

    updated = False
    if pid is not None and sd_object_id:
        try:
            updated = await STORE.send_item_to_depot(pid, int(sd_item_type),
                                                     int(sd_object_id))
            # 入仓标记"新"（客户端 DepotNewItemTable 红点；此前误标 viewed=TRUE
            # 导致红点永不出现）
            if updated and sd_tab:
                await STORE.mark_depot_item_new(pid, sd_tab, int(sd_object_id))
        except Exception as e:
            log(f'[DB] send_item_to_depot failed: {e}')

    resp_proto = pb_int32(1, 1 if updated else 0)
    resp = build_response(cmd_id, 'SendItemToDepotResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SendItemToDepotResponse '
        f'(response={1 if updated else 0})')

    # 重推行数据（characterId→0 → 客户端把物品从装备列表挪进仓库；不推 UI
    # 不刷新 → 玩家连点重发。全 itemType 通用：weapon/vehicle 带挂靠行整组，
    # 制服/配件/皮肤/弹药/载具件单行——此前只推 weapon/vehicle，其余类型
    # UI 不收敛）
    if updated:
        await _repush_item_row(sess, writer, pid, int(sd_item_type),
                               sd_object_id)
        # 解绑配件行显式重推（weapon_id 已清 0，_repush_weapon_rows 按
        # weapon_id 过滤不会带上 → 不推则客户端仍显示挂在枪上 isModded）
        if attached_mods and sess.soldier_view_cmd is not None:
            for m in attached_mods:
                await _push_kvcs(writer, sess.soldier_view_cmd, b'WeaponModifier',
                                 _msg_weapon_modifier(m['id'], m['template_id'],
                                                      0, 0,
                                                      m.get('auto_repair', 0),
                                                      m.get('condition', 1.0)))
            log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                f'(WeaponModifier x{len(attached_mods)} detached to depot '
                f'with weapon {sd_object_id})')
        # 解绑弹药行显式重推（weapon_id 已清 0，_repush_weapon_rows 按
        # weapon_id 过滤不会带上 → 不推则客户端弹药仍挂在武器名下）
        if attached_wammo and sess.soldier_view_cmd is not None:
            for a in attached_wammo:
                await _push_kvcs(writer, sess.soldier_view_cmd, b'WeaponAmmo',
                                 _msg_weapon_ammo(a['id'], a['ammo_template_id'],
                                                  0, 0, a['slot_index'],
                                                  a.get('condition', 100.0),
                                                  a.get('auto_repair', 0)))
            log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                f'(WeaponAmmo x{len(attached_wammo)} detached to depot '
                f'with weapon {sd_object_id})')
        # 解绑皮肤行显式重推（weapon_id 已清 0 → 独立仓库皮肤；
        # 武器行 variant_id 复位随 _repush_item_row 的武器行推送覆盖）
        if attached_wvar and sess.soldier_view_cmd is not None:
            for vv in attached_wvar:
                await _push_kvcs(writer, sess.soldier_view_cmd, b'WeaponVariant',
                                 _msg_weapon_variant(vv['id'], 0, 0,
                                                     vv['weapon_variant_template_id']))
            log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                f'(WeaponVariant x{len(attached_wvar)} detached to depot '
                f'with weapon {sd_object_id})')
        # 解绑升级行显式重推（vehicle_id 已清 0，_repush_vehicle_row 按
        # vehicle_id 过滤不会带上 → 不推则客户端仍显示 hasUpgradeEquipped）
        if attached_vupg and sess.soldier_view_cmd is not None:
            for u in attached_vupg:
                await _push_kvcs(writer, sess.soldier_view_cmd, b'VehicleUpgrade',
                                 _msg_vehicle_upgrade(u['id'],
                                                      u['upgrade_template_id'], 0,
                                                      u['equipped_slot_id'], 0,
                                                      u.get('condition', 100.0),
                                                      u.get('auto_repair', 0)))
            log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                f'(VehicleUpgrade x{len(attached_vupg)} detached to depot '
                f'with vehicle {sd_object_id})')
        # 解绑弹药行显式重推（vehicle_id 已清 0，_repush_vehicle_row 按
        # vehicle_id 过滤不会带上 → 不推则客户端弹药仍挂在载具名下）
        if attached_ammo and sess.soldier_view_cmd is not None:
            for a in attached_ammo:
                await _push_kvcs(writer, sess.soldier_view_cmd, b'VehicleAmmo',
                                 _msg_vehicle_ammo(a['id'], a['ammo_template_id'],
                                                   0, 0, a['slot_index'], 0,
                                                   a.get('condition', 100.0),
                                                   a.get('auto_repair', 0)))
            log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                f'(VehicleAmmo x{len(attached_ammo)} detached to depot '
                f'with vehicle {sd_object_id})')
        # 解绑皮肤行显式重推（vehicle_id 已清 0 → 独立仓库皮肤；
        # 载具行 variant_id 复位随 _repush_item_row 的载具行推送覆盖）
        if attached_vvar and sess.soldier_view_cmd is not None:
            for vv in attached_vvar:
                await _push_kvcs(writer, sess.soldier_view_cmd, b'VehicleVariant',
                                 _msg_vehicle_variant(vv['id'], 0, 0,
                                                      vv['vehicle_variant_template_id']))
            log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                f'(VehicleVariant x{len(attached_vvar)} detached to depot '
                f'with vehicle {sd_object_id})')
        # 重推原角色 KVCS：unlockBits 随归属变化（武器/弹药/配件/载具 uid
        # 移除；此前只覆盖 1/5/6，制服与载具件退仓后原角色本会话解锁状态
        # 不刷新）
        if cur_char:
            await _repush_character_by_id(sess, writer, pid, cur_char)
        if sd_tab:
            await _repush_depot_new_item(sess, writer, pid)
    await _push_player_refresh(sess, writer)
    # 生涯军令 45 eDepot（客户端无上报点，物品入仓即完成）+ 推行实时刷新
    if updated and pid is not None:
        from handlers.career import complete_order_and_push
        await complete_order_and_push(sess, writer, pid, 45)


async def handle_sendupgradestodepotrequest(sess, writer, cmd_id, req_type, proto):
    # 卸下容器上挂的升级件（客户端 ItemInfoOverlayMediator
    # showSendUpgradesToDepotButtonIfNeeded：物品已在仓库时，武器 isModded /
    # 载具 hasUpgradeEquipped 或非默认皮肤 → 显示"卸下升级件"按钮）
    # SendUpgradesToDepotRequest { int32 itemType=1; int64 objectId=2; int64 characterId=3; }
    #   itemType 是【容器类型】（1=weapon 2=vehicle），objectId 是【容器实例 id】
    # SendUpgradesToDepotResponse { int32 response = 1; }
    try:
        f = pb_parse(proto)
        su_item_type = f.get(1, 0)
        su_object_id = f.get(2, 0)
        su_char_id = f.get(3, 0)
        log(f'      SendUpgradesToDepot: itemType={su_item_type}, '
            f'objectId={su_object_id}, charId={su_char_id}')
    except Exception as e:
        log(f'      SendUpgradesToDepot parse error: {e}')
        su_item_type = 0
        su_object_id = 0
        su_char_id = 0

    pid = await sess.ensure_player()
    affected = 0
    # 卸下前捕获挂靠在该武器上的配件行（卸下后 weapon_id 已清 0，无法反查）
    loaded_mods = []
    if pid is not None and su_object_id and int(su_item_type) == 1:
        try:
            loaded_mods = [m for m in await STORE.list_weapon_modifiers(pid)
                           if int(m['weapon_id']) == int(su_object_id)]
        except Exception as e:
            log(f'[DB] list_weapon_modifiers failed: {e}')
    if pid is not None and su_object_id:
        try:
            affected = await STORE.send_upgrades_to_depot(pid, int(su_item_type),
                                                          int(su_object_id))
        except Exception as e:
            log(f'[DB] send_upgrades_to_depot failed: {e}')

    resp_proto = pb_int32(1, 1 if affected else 0)
    resp = build_response(cmd_id, 'SendUpgradesToDepotResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SendUpgradesToDepotResponse '
        f'(response={1 if affected else 0}, affected={affected})')

    # 重推受影响行：武器 → 配件行（weaponId→0）+ 原角色 unlockBits（配件
    # mapping uid 移除）；载具 → 升级/皮肤/Vehicle 行。只回 response 不推
    # 数据时客户端 isModded/hasUpgradeEquipped 不变 → 按钮状态不收敛
    if affected and sess.soldier_view_cmd is not None:
        if int(su_item_type) == 1:
            for m in loaded_mods:
                await _push_kvcs(writer, sess.soldier_view_cmd, b'WeaponModifier',
                                 _msg_weapon_modifier(m['id'], m['template_id'],
                                                      0, 0,
                                                      m.get('auto_repair', 0),
                                                      m.get('condition', 1.0)))
            if su_char_id:
                await _repush_character_by_id(sess, writer, pid, su_char_id)
        elif int(su_item_type) == 2:
            await _repush_vehicle_row(sess, writer, pid, su_object_id)


async def handle_setdepotitemviewedrequest(sess, writer, cmd_id, req_type, proto):
    # 仓库物品已读（去除仓库入口"新物品"提示）
    # SetDepotItemViewedRequest { string tabName=1; int64 itemId=2; }
    # SetDepotItemViewedResponse { int32 response = 1; }
    try:
        f = pb_parse(proto)
        dv_tab = f.get(1, b'')
        if isinstance(dv_tab, bytes):
            dv_tab = dv_tab.decode('utf-8', 'replace')
        dv_item_id = f.get(2, 0)
        log(f'      SetDepotItemViewed: tab={dv_tab!r}, itemId={dv_item_id}')
    except Exception as e:
        log(f'      SetDepotItemViewed parse error: {e}')
        dv_tab = ''
        dv_item_id = 0

    pid = await sess.ensure_player()
    if pid is not None and dv_item_id:
        try:
            await STORE.mark_depot_item_viewed(pid, dv_tab, int(dv_item_id))
        except Exception as e:
            log(f'[DB] mark_depot_item_viewed failed: {e}')

    resp_proto = pb_int32(1, 1)  # response = 1 (ok)
    resp = build_response(cmd_id, 'SetDepotItemViewedResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetDepotItemViewedResponse (response=1 ok)')

    # 重推 DepotNewItem 行消点（不推则本会话红点不消失，重登才恢复）
    if pid is not None and dv_item_id:
        await _repush_depot_new_item(sess, writer, pid)


# 教程已触发默认集：与 PlayerSettings f28 triggeredTutorialTemplateIds 同源
# （[1,2,3,4]+6..29，跳过 5）。客户端 TutorialTable.wasTriggered 只认
# KVCS Tutorial 行（id/playerId/triggeredTutorialTemplateIds 逗号串），
# 不推该行则 wasTriggered 恒 false：战报关闭时 MissionBattleReportMediator
# 每次 dispatch CAREER_ORDER_TUTORIAL_ID(=2) → 生涯军令引导反复弹出。
_DEFAULT_TRIGGERED_TUTORIALS = [1, 2, 3, 4] + list(range(6, 30))


async def push_tutorial_row(sess, writer, pid):
    """推玩家 Tutorial KVCS 行（id=playerId，单行聚合已触发教程集）。

    triggeredTutorialTemplateIds = 默认集 ∪ player_tutorials_triggered 持久化集。"""
    if sess.playerview_cmd is None:
        return
    triggered = set(_DEFAULT_TRIGGERED_TUTORIALS)
    if STORE is not None:
        try:
            triggered.update(await STORE.list_tutorials_triggered(int(pid)))
        except Exception as e:
            log(f'[DB] list_tutorials_triggered failed: {e}')
    m = pb_int64(1, int(pid))                      # id
    m += pb_int64(2, int(pid))                     # playerId
    m += pb_string(3, ','.join(str(t) for t in sorted(triggered)))
    await _push_kvcs(writer, sess.playerview_cmd, b'Tutorial', m)
    log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet '
        f'(Tutorial id={pid}, {len(triggered)} triggered templates)')


def _zulu(dt):
    """datetime → Zulu 串（YYYYMMDDHHmmssZ，客户端 TimeUtilities
    .convertZuluTimeToMilliseconds 按 GMT+0000 定位解析）。"""
    if dt is None:
        return ''
    return time.strftime('%Y%m%d%H%M%S', dt.utctimetuple()) + 'Z'


async def push_daily_bonus_row(sess, writer, pid, character_id):
    """推单角色 DailyBonus KVCS 行（结算消费首胜资格后重推）。

    客户端士兵卡片 "+50% XP" 徽章唯一数据源（DailyBonusModel
    .getDailyBonusObjectForCharacter）：行缺失时走模板兜底分支
    dailyBonusUsed 恒 false → 徽章永显示。行 lastReceived 在
    05:00 UTC 窗口内 → dailyBonusUsed=true 徽章隐藏。"""
    if sess is None or sess.playerview_cmd is None or STORE is None:
        return
    try:
        ch = await STORE.get_character(int(pid), int(character_id))
    except Exception as e:
        log(f'      [DB] get_character(daily bonus) failed: {e}')
        return
    if ch is None:
        return
    tmpl_id = daily_bonus_template_for(ch.get('template'))
    if not tmpl_id:
        return      # 无匹配模板（如将军）不推：客户端回落不显示，官方同款
    m = pb_int64(1, int(ch['id']))          # id（派生行：行 id = characterId）
    m += pb_int64(2, int(pid))              # playerId
    m += pb_int64(3, int(ch['id']))         # characterId
    m += pb_int64(4, tmpl_id)               # dailyBonusTemplateId
    m += pb_string(5, _zulu(ch.get('last_daily_battle_at')))   # lastReceived
    await _push_kvcs(writer, sess.playerview_cmd, b'DailyBonus', m)
    log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet '
        f'(DailyBonus char={ch["id"]} tmpl={tmpl_id} '
        f'lastReceived={_zulu(ch.get("last_daily_battle_at")) or "-"})')


async def push_daily_bonus_rows(sess, writer, pid):
    """subscribeplayerview 时推送全部角色 DailyBonus 行（登录 fill）。"""
    if sess.playerview_cmd is None or STORE is None:
        return
    try:
        chars = await STORE.list_characters(int(pid))
    except Exception as e:
        log(f'      [DB] list_characters(daily bonus) failed: {e}')
        return
    for ch in chars:
        if daily_bonus_template_for(ch.get('template')):
            await push_daily_bonus_row(sess, writer, pid, ch['id'])


async def handle_marktutorialtriggeredrequest(sess, writer, cmd_id, req_type, proto):
    # 教程已触发（幂等；完成态走 SetTutorialCompletedRequest）
    # MarkTutorialTriggeredRequest { int64 tutoriaTemplateId=1; int64 playerId=2; }
    # MarkTutorialTriggeredResponse { int32 response = 1; }
    try:
        f = pb_parse(proto)
        mt_tutorial_id = f.get(1, 0)
        log(f'      MarkTutorialTriggered: tutorialTemplateId={mt_tutorial_id}')
    except Exception as e:
        log(f'      MarkTutorialTriggered parse error: {e}')
        mt_tutorial_id = 0

    pid = await sess.ensure_player()
    if pid is not None and mt_tutorial_id:
        try:
            await STORE.mark_tutorial_triggered(pid, int(mt_tutorial_id))
        except Exception as e:
            log(f'[DB] mark_tutorial_triggered failed: {e}')
        # 重推 Tutorial 行（客户端 TutorialTable 只认 KVCS 行，
        # 不重推则 wasTriggered 恒 false → 战报关闭后重复弹生涯军令引导）
        await push_tutorial_row(sess, writer, pid)

    resp_proto = pb_int32(1, 1)  # response = 1 (ok)
    resp = build_response(cmd_id, 'MarkTutorialTriggeredResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} MarkTutorialTriggeredResponse (response=1 ok)')




HANDLERS = {
    'RequestReadCharacterStats': handle_requestreadcharacterstats,
    'SetTutorialCompletedRequest': handle_settutorialcompletedrequest,
    'set_combatbadge_request': handle_set_combatbadge_request,
    'DeleteCharacterRequest': handle_deletecharacterrequest,
    'ChangeTierRequest': handle_changetierrequest,
    'SetCharacterActiveStatusRequest': handle_setcharacteractivestatusrequest,
    'SetInventoryItemToCharacterRequest': handle_setinventoryitemtocharacterrequest,
    'SendItemToDepotRequest': handle_senditemtodepotrequest,
    'SendUpgradesToDepotRequest': handle_sendupgradestodepotrequest,
    'SetDepotItemViewedRequest': handle_setdepotitemviewedrequest,
    'MarkTutorialTriggeredRequest': handle_marktutorialtriggeredrequest,
}
