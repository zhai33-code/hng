# -*- coding: utf-8 -*-
"""角色域（创建、统计、战斗徽章、教程完成）。

从 realtest_server.dispatch 机械迁移，函数体保持原样；
统一签名 (sess, writer, cmd_id, req_type, proto)。
"""


import struct

from protocol import (
    Chunk,
    build_response,
    pb_int32,
    pb_int64,
    pb_message,
    pb_parse,
    pb_string,
    )

from state import (
    STORE,
    log,
    )

from wardb import (
    _AMMO_MAXCOND,
    CHARACTER_PACKS,
    _EXPERIENCE_LEVELS,
    _PRICING_MAP,
    _default_ammo_for,
    _default_vehicle_ammo_for,
    _VEHICLE_VARIANT_PARENT,
    _WEAPON_RIBBONS,
    _rank_from_combat_xp,
    default_uniform_variant,
    )

from messages import (
    _msg_character,
    _msg_character_stats_entry,
    _msg_uniform_part,
    _msg_uniform_variant,
    _msg_vehicle,
    _msg_vehicle_ammo,
    _msg_weapon,
    _msg_weapon_ammo,
    _msg_weapon_modifier,
    _push_kvcs,
    )

from player_service import _char_unlock_bits_for, _charge_player, _push_player_refresh

from starting_gear import STARTING_GEAR


async def handle_requestreadcharacterstats(sess, writer, cmd_id, req_type, proto):
    pid = await sess.ensure_player()
    char_ids = [1]
    if pid is not None:
        try:
            char_ids = [ch['id'] for ch in await STORE.list_characters(pid)] or [1]
        except Exception as e:
            log(f'[DB] list_characters failed: {e}')

    resp_proto = b''
    for cid in char_ids:
        ribbons = {}
        combat_xp = 0
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
        log(f'      stats entry char={cid}: ribbons={sorted(ribbons.items())} '
            f'combatXp={combat_xp}')
        resp_proto += pb_message(1,
                                 _msg_character_stats_entry(cid, sorted(ribbons.items()),
                                                            combat_xp))

    resp = build_response(cmd_id, 'ResponseReadCharacterStats', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} ResponseReadCharacterStats ({len(char_ids)} entries, '
        f'characterIds={char_ids}, total {len(resp_proto)}B)')



async def _grant_starting_gear(pid, char_id, gear):
    """按 starting_gear.py 配置发放新角色初始装备（写 DB 并装备）。

    返回 (weapon_info, vehicle_info)，DB 任一步失败抛异常由调用方记录
    （已写入的行保留，不回滚也不中断建号；下次登录初始视图会重新下发）：
      weapon_info  = {'row': weapons行, 'ammo': [(mappingId, weapon_ammo行), ...],
                      'modifier': weapon_modifiers行或 None}
      vehicle_info = {'row': vehicles行,
                      'ammo': [(storageId, slot, vehicle_ammo行), ...]}
    战斗配装只下发已装备武器/载具（warserver_messages._msg_character_entry），
    故武器装备主武器槽 container_slot=1、载具装备 equipped_in_slot=1。"""
    weapon_info = None
    vehicle_info = None

    # 1. 初始武器 + 默认弹药（首个装备槽 1）+ 侦察瞄具
    wt = int(gear.get('weapon', 0) or 0)
    if wt:
        w = await STORE.create_weapon(pid, gear.get('weapon_name') or 'Weapon',
                                      wt, char_id)
        await STORE.update_weapon_container(pid, w['id'], char_id, 1)
        w['container_id'] = int(char_id)
        w['container_slot'] = 1
        weapon_info = {'row': w, 'ammo': [], 'modifier': None}
        for i, (mapping_id, ammo_template_id) in enumerate(_default_ammo_for(wt)):
            slot_ammo = 1 if i == 0 else 0  # 首个默认弹药装备，其余持有
            a = await STORE.create_weapon_ammo(
                pid, ammo_template_id, char_id, w['id'], slot_ammo,
                _AMMO_MAXCOND.get(int(ammo_template_id), 100.0))
            weapon_info['ammo'].append((mapping_id, a))
        sm = int(gear.get('sight_mod', 0) or 0)
        if sm:
            weapon_info['modifier'] = await STORE.create_weapon_modifier(
                pid, sm, char_id, w['id'])

    # 2. 赠送载具（坦克兵坦克 / 飞行员侦察机）+ 默认载具弹药（按弹药仓顺延装备）
    vvt = int(gear.get('vehicle_variant', 0) or 0)
    if vvt:
        v = await STORE.create_vehicle(pid, vvt, char_id)
        await STORE.set_vehicle_equipment(pid, v['id'], char_id, 1)
        v['equipped_in_slot'] = 1
        vehicle_info = {'row': v, 'ammo': []}
        parent_vv = _VEHICLE_VARIANT_PARENT.get(vvt)
        if parent_vv is not None:
            storage_slots = {}
            for (storage_id, ammo_template_id) in _default_vehicle_ammo_for(parent_vv):
                slot_ammo = storage_slots.get(storage_id, 1)
                storage_slots[storage_id] = slot_ammo + 1
                va = await STORE.create_vehicle_ammo(
                    pid, ammo_template_id, v['id'], char_id,
                    _AMMO_MAXCOND.get(int(ammo_template_id), 100.0))
                await STORE.set_vehicle_ammo_equipment(pid, va['id'], storage_id,
                                                       slot_ammo, True)
                vehicle_info['ammo'].append((storage_id, slot_ammo, va))
    return weapon_info, vehicle_info


async def handle_createcharacterrequest(sess, writer, cmd_id, req_type, proto):
    # 创建角色请求，返回 result=1 (ok) 并推送 character 数据到 SOLDIER_VIEW
    # CreateCharacterRequest { string characterName=1; int64 characterPackId=2; int64 pricingId=3; int64 discountId=4; int32 paymentCurrency=5; }
    # CreateCharacterResponse { int32 result=1; }
    try:
        f = pb_parse(proto)
        char_name = f.get(1, b'').decode('utf-8', 'replace') if isinstance(f.get(1, b''), bytes) else str(f.get(1, ''))
        char_pack_id = f.get(2, 0)
        if isinstance(char_pack_id, bytes):
            char_pack_id = int.from_bytes(char_pack_id, 'little')
        log(f'      CreateCharacter: name={char_name}, packId={char_pack_id}')
    except Exception as e:
        log(f'      CreateCharacter parse error: {e}')
        char_name = 'Soldier'
        char_pack_id = 0

    # 从 ShopCharacterPacks 解析阵营/模板/军衔（如 pack 35 = Soviet Infantryman: faction=3, template=1）
    pack_info = CHARACTER_PACKS.get(char_pack_id)
    if pack_info:
        faction, template, rank = pack_info
    else:
        faction, template, rank = 1, 1, 0
    log(f'      character faction={faction}, template={template}, rank={rank}')

    # 军衔单源：pack 的起始军衔 → 换算为初始 combat_xp（该军衔等级阈值）。
    # 此后军衔一律由 combat_xp 经 ExperienceLevels 阈值推导，不再单独存军衔。
    start_xp = _EXPERIENCE_LEVELS[rank] if 0 <= rank < len(_EXPERIENCE_LEVELS) else 0
    log(f'      character startCombatXp={start_xp}')

    # 写入数据库（失败时用内存 ID 兜底，仅本次会话可见）；
    # 默认军服实例随建号分配 —— (兵种,阵营) 基础迷彩（官方服角色生来带基础迷彩）
    char_id = 1
    new_uniform_variant_id = 0
    db_created = False
    uv_template_id = default_uniform_variant(template, faction)
    pid = await sess.ensure_player()
    if pid is not None:
        try:
            row = await STORE.create_character(
                pid, char_name, faction, template, start_xp,
                uniform_variant_template_id=uv_template_id)
            char_id = row['id']
            new_uniform_variant_id = int(row.get('uniform_variant_id', 0) or 0)
            db_created = True
        except Exception as e:
            log(f'[DB] create_character failed: {e}')
    log(f'      character id={char_id}')

    # 1. 返回 CreateCharacterResponse (result=1 ok)
    resp_proto = pb_int32(1, 1)  # result = 1 (ok)
    resp = build_response(cmd_id, 'CreateCharacterResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} CreateCharacterResponse (result=1 ok)')

    # 1.5 发放初始装备（starting_gear.py 独立配置：slot1 武器+默认弹药+侦察瞄具、
    #     坦克兵/飞行员赠送载具+默认载具弹药）。须在 character 推送前完成 ——
    #     unlock bits 计算包含已持有武器/载具的解锁需求（商店显示已拥有）。
    #     仅 DB 建号成功时发放（失败兜底 char_id=1 是假 id，发装备会错挂到别的角色）
    weapon_info = None
    vehicle_info = None
    gear = STARTING_GEAR.get((faction, template), {})
    if db_created and gear:
        try:
            weapon_info, vehicle_info = await _grant_starting_gear(pid, char_id, gear)
            granted = []
            if weapon_info is not None:
                granted.append('weapon=' + weapon_info['row']['name'])
            if vehicle_info is not None:
                granted.append('vehicleVariant=%s' % gear.get('vehicle_variant'))
            log('      starting gear granted: ' + ', '.join(granted))
        except Exception as e:
            log(f'[DB] grant starting gear failed: {e}')

    # 2. 推送默认军服实例（UniformVariant KVCS）——必须先于 character：
    #    客户端 changeset 串行队列处理 character 行时 getUniformVariantIdEquipped()
    #    按 uniformVariantId 查本地 UniformVariantTable，实例未入表则 getFromId
    #    返回 null → 取 uniformVariantTemplateId 抛 Error #1009，且未捕获异常会
    #    中断 publishPendingTableUpdates 发布循环，后续所有 KVCS 不被 publish
    if new_uniform_variant_id and sess.soldier_view_cmd is not None:
        await _push_kvcs(writer, sess.soldier_view_cmd, b'UniformVariant',
                         _msg_uniform_variant(new_uniform_variant_id,
                                              uv_template_id, char_id))
        log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
            f'(UniformVariant id={new_uniform_variant_id}, template={uv_template_id}, '
            f'char={char_id})')

    # 3. 通过 SOLDIER_VIEW 推送 character 数据 (KeyValueChangeSet)
    # character proto: id, name, playerid, charactertemplateid, factiontemplateid, ...
    if sess.soldier_view_cmd is not None:
        bits = await _char_unlock_bits_for(sess, template, faction, char_id)
        char_proto = _msg_character(char_name, faction, template, rank, char_id, bits,
                                    uniform_variant_id=new_uniform_variant_id)
        char_data_chunk = Chunk(char_proto)
        char_table_chunk = Chunk(b'character', [char_data_chunk])
        char_action_chunk = Chunk(b'set', [char_table_chunk])
        char_title_chunk = Chunk(b'KeyValueChangeSet', [char_action_chunk])
        char_cs_main = Chunk(struct.pack('<i', sess.soldier_view_cmd), [char_title_chunk])
        char_cs_bytes = char_cs_main.write(b'')
        writer.write(char_cs_bytes)
        await writer.drain()
        log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet (character: {char_name}, {len(char_proto)}B)')
    else:
        log(f'      WARNING: SOLDIER_VIEW not subscribed, cannot push character data')

    # 4. 推送初始武器 + 默认弹药 + 侦察瞄具（须在 character 之后：weapon 行
    #    引用 characterId，客户端 character 表先行入表）
    if weapon_info is not None and sess.soldier_view_cmd is not None:
        w = weapon_info['row']
        wproto = _msg_weapon(w['id'], w['name'], w['template_id'],
                             w['character_id'], w['container_id'],
                             w['container_slot'], w.get('variant_id', 0),
                             w.get('clip_count', 0), w.get('auto_repair', 0),
                             w.get('condition', 100.0))
        await _push_kvcs(writer, sess.soldier_view_cmd, b'weapon', wproto)
        log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet (weapon '
            f'id={w["id"]}, template={w["template_id"]}, slot={w["container_slot"]}, '
            f'{len(wproto)}B, name={w["name"]!r})')
        for mapping_id, a in weapon_info['ammo']:
            aproto = _msg_weapon_ammo(a['id'], a['ammo_template_id'],
                                      a['character_id'], a['weapon_id'],
                                      a['slot_index'], a['condition'])
            await _push_kvcs(writer, sess.soldier_view_cmd, b'WeaponAmmo', aproto)
            log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet (WeaponAmmo '
                f'id={a["id"]}, template={a["ammo_template_id"]}, '
                f'mapping={mapping_id}, equippedSlot={a["slot_index"]})')
        m = weapon_info['modifier']
        if m is not None:
            mproto = _msg_weapon_modifier(m['id'], m['template_id'],
                                          m['character_id'], m['weapon_id'],
                                          m.get('auto_repair', 0),
                                          m.get('condition', 1.0))
            await _push_kvcs(writer, sess.soldier_view_cmd, b'WeaponModifier', mproto)
            log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                f'(WeaponModifier id={m["id"]}, template={m["template_id"]}, '
                f'weapon={m["weapon_id"]})')

    # 5. 推送赠送载具 + 默认载具弹药
    if vehicle_info is not None and sess.soldier_view_cmd is not None:
        v = vehicle_info['row']
        vproto = _msg_vehicle(v['id'], v['vehicle_variant_template_id'],
                              v['character_id'], v['equipped_in_slot'],
                              v.get('variant_id', 0), condition=v.get('condition', 100.0))
        await _push_kvcs(writer, sess.soldier_view_cmd, b'Vehicle', vproto)
        log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet (Vehicle '
            f'id={v["id"]}, variantTemplate={v["vehicle_variant_template_id"]}, '
            f'slot={v["equipped_in_slot"]}, {len(vproto)}B)')
        for storage_id, slot_ammo, va in vehicle_info['ammo']:
            aproto = _msg_vehicle_ammo(va['id'], va['ammo_template_id'],
                                       va['vehicle_id'], storage_id, slot_ammo,
                                       va['character_id'], va['condition'])
            await _push_kvcs(writer, sess.soldier_view_cmd, b'VehicleAmmo', aproto)
            log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet (VehicleAmmo '
                f'id={va["id"]}, template={va["ammo_template_id"]}, '
                f'storage={storage_id}, slot={slot_ammo})')



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
    badge_char_id = 1
    badge_id = 0
    badge_slot = 0
    try:
        f = pb_parse(proto)
        badge_char_id = f.get(1, 1)
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


async def handle_renamecharacterrequest(sess, writer, cmd_id, req_type, proto):
    # 角色改名
    # RenameCharacterRequest { int64 characterId=1; string newName=2; int64 pricingId=3;
    #   int64 discountId=4; int32 paymentCurrency=5; }
    # RenameCharacterResponse { int32 result = 1; }
    try:
        f = pb_parse(proto)
        rc_char_id = f.get(1, 0)
        rc_new_name = f.get(2, b'')
        if isinstance(rc_new_name, bytes):
            rc_new_name = rc_new_name.decode('utf-8', 'replace')
        rc_pricing_id = f.get(3, 0)
        rc_currency = f.get(5, 0)
        log(f'      RenameCharacter: charId={rc_char_id}, newName={rc_new_name!r}, '
            f'pricingId={rc_pricing_id}')
    except Exception as e:
        log(f'      RenameCharacter parse error: {e}')
        rc_char_id = 0
        rc_new_name = ''
        rc_pricing_id = 0
        rc_currency = 0

    # 扣款（改名费；无 pricing 时免费）
    cost, ok = await _charge_player(sess, rc_pricing_id, rc_currency)
    if not ok:
        resp_proto = pb_int32(1, 56)  # not_enough_credits
        resp = build_response(cmd_id, 'RenameCharacterResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} RenameCharacterResponse (result=56 not_enough) '
            f'cost={cost}')
        return

    pid = await sess.ensure_player()
    updated = False
    if pid is not None and rc_char_id:
        try:
            updated = await STORE.rename_character(pid, int(rc_char_id), rc_new_name)
        except Exception as e:
            log(f'[DB] rename_character failed: {e}')

    resp_proto = pb_int32(1, 1 if updated else 0)
    resp = build_response(cmd_id, 'RenameCharacterResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} RenameCharacterResponse (result={1 if updated else 0})')

    # 重推 character（name 更新）+ 刷新货币
    if updated and pid is not None:
        try:
            for ch in await STORE.list_characters(pid):
                if int(ch['id']) == int(rc_char_id):
                    await _repush_character_row(sess, writer, ch)
                    break
        except Exception as e:
            log(f'[DB] reload character after rename failed: {e}')
    await _push_player_refresh(sess, writer)


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


async def handle_changecharactertemplaterequest(sess, writer, cmd_id, req_type, proto):
    # 兵种切换（转职）：新模板从 pricing.objectId 推导（请求本身不带模板 id）
    # ChangeCharacterTemplateRequest { int64 characterId=1; int64 pricingId=2;
    #   int32 paymentCurrency=3; int64 discountId=4; }
    # ChangeCharacterTemplateResponse { int32 result = 1; }
    try:
        f = pb_parse(proto)
        ct_char_id = f.get(1, 0)
        ct_pricing_id = f.get(2, 0)
        ct_currency = f.get(3, 0)
        log(f'      ChangeCharacterTemplate: charId={ct_char_id}, '
            f'pricingId={ct_pricing_id}')
    except Exception as e:
        log(f'      ChangeCharacterTemplate parse error: {e}')
        ct_char_id = 0
        ct_pricing_id = 0
        ct_currency = 0

    # pricing → 新兵种模板 id
    price = _PRICING_MAP.get(int(ct_pricing_id)) if ct_pricing_id else None
    new_template = int(price['object_id']) if price else 0
    if not new_template:
        resp_proto = pb_int32(1, 0)  # fail：定价缺失无法确定新模板
        resp = build_response(cmd_id, 'ChangeCharacterTemplateResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} ChangeCharacterTemplateResponse '
            f'(rejected: pricing {ct_pricing_id} has no objectId)')
        return

    # 扣款（转职费）
    cost, ok = await _charge_player(sess, ct_pricing_id, ct_currency)
    if not ok:
        resp_proto = pb_int32(1, 56)  # not_enough_credits
        resp = build_response(cmd_id, 'ChangeCharacterTemplateResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} ChangeCharacterTemplateResponse '
            f'(result=56 not_enough) cost={cost}')
        return

    pid = await sess.ensure_player()
    old_template = None
    if pid is not None and ct_char_id:
        try:
            old_template = await STORE.change_character_template(
                pid, int(ct_char_id), new_template)
        except Exception as e:
            log(f'[DB] change_character_template failed: {e}')

    resp_proto = pb_int32(1, 1 if old_template is not None else 0)
    resp = build_response(cmd_id, 'ChangeCharacterTemplateResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} ChangeCharacterTemplateResponse '
        f'(result={1 if old_template is not None else 0}, '
        f'template {old_template} -> {new_template})')

    # 重推 character（template + previousCharacterTemplateId 更新）
    if old_template is not None and pid is not None:
        try:
            for ch in await STORE.list_characters(pid):
                if int(ch['id']) == int(ct_char_id):
                    await _repush_character_row(sess, writer, ch)
                    break
        except Exception as e:
            log(f'[DB] reload character after template change failed: {e}')
    await _push_player_refresh(sess, writer)


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
    updated = False
    if pid is not None and si_item_id:
        try:
            updated = await STORE.set_inventory_item_character(
                pid, si_item_type, int(si_item_id), int(si_char_id))
        except Exception as e:
            log(f'[DB] set_inventory_item_character failed: {e}')

    resp_proto = pb_int32(1, 1 if updated else 0)
    resp = build_response(cmd_id, 'SetInventoryItemToCharacterResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetInventoryItemToCharacterResponse '
        f'(msg={1 if updated else 0})')


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

    # 扣款（非免费存放时收存放费）
    if not sd_is_free and sd_pricing_id:
        cost, ok = await _charge_player(sess, sd_pricing_id, sd_currency)
        if not ok:
            resp_proto = pb_int32(1, 56)  # not_enough_credits
            resp = build_response(cmd_id, 'SendItemToDepotResponse', resp_proto)
            writer.write(resp)
            await writer.drain()
            log(f'S->C cmd={cmd_id:<4} SendItemToDepotResponse '
                f'(response=56 not_enough) cost={cost}')
            return

    pid = await sess.ensure_player()
    updated = False
    if pid is not None and sd_object_id:
        try:
            updated = await STORE.send_item_to_depot(pid, int(sd_item_type),
                                                     int(sd_object_id))
            if updated and sd_tab:
                await STORE.mark_depot_item_viewed(pid, sd_tab, int(sd_object_id))
        except Exception as e:
            log(f'[DB] send_item_to_depot failed: {e}')

    resp_proto = pb_int32(1, 1 if updated else 0)
    resp = build_response(cmd_id, 'SendItemToDepotResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SendItemToDepotResponse '
        f'(response={1 if updated else 0})')
    await _push_player_refresh(sess, writer)


async def handle_sendupgradestodepotrequest(sess, writer, cmd_id, req_type, proto):
    # 升级件（配件/补给）送回仓库
    # SendUpgradesToDepotRequest { int32 itemType=1; int64 objectId=2; int64 characterId=3; }
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
    updated = False
    if pid is not None and su_object_id:
        try:
            updated = await STORE.send_upgrades_to_depot(pid, int(su_item_type),
                                                         int(su_object_id))
        except Exception as e:
            log(f'[DB] send_upgrades_to_depot failed: {e}')

    resp_proto = pb_int32(1, 1 if updated else 0)
    resp = build_response(cmd_id, 'SendUpgradesToDepotResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SendUpgradesToDepotResponse '
        f'(response={1 if updated else 0})')


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


async def handle_swapheadrequest(sess, writer, cmd_id, req_type, proto):
    # 换头（外观窗口：0=换回默认头；否则购买/应用头模板 → UniformPart 实例）
    # SwapHeadRequest { int64 characterId=1; int64 uniformPartTemplateId=2;
    #   int32 uniformPartType=3; int64 pricingId=4; int64 storeDiscountId=5;
    #   int32 paymentCurrency=6; }
    # SwapHeadResponse { int32 response = 1; }
    try:
        f = pb_parse(proto)
        sh_char_id = f.get(1, 0)
        sh_head_template = f.get(2, 0)
        sh_pricing_id = f.get(4, 0)
        sh_currency = f.get(6, 0)
        log(f'      SwapHead: charId={sh_char_id}, headTemplate={sh_head_template}, '
            f'pricingId={sh_pricing_id}')
    except Exception as e:
        log(f'      SwapHead parse error: {e}')
        sh_char_id = 0
        sh_head_template = 0
        sh_pricing_id = 0
        sh_currency = 0

    # 购买头（有定价且非默认头）先扣款
    head_swap_type = 1 if sh_pricing_id else 0  # 0=eFree 1=eNeedPayment
    if sh_pricing_id and sh_head_template:
        cost, ok = await _charge_player(sess, sh_pricing_id, sh_currency)
        if not ok:
            resp_proto = pb_int32(1, 56)  # not_enough_credits
            resp = build_response(cmd_id, 'SwapHeadResponse', resp_proto)
            writer.write(resp)
            await writer.drain()
            log(f'S->C cmd={cmd_id:<4} SwapHeadResponse '
                f'(response=56 not_enough) cost={cost}')
            return

    pid = await sess.ensure_player()
    head_instance_id = 0
    if pid is not None and sh_char_id:
        try:
            if not sh_head_template:
                # 换回默认头：清 head_id
                await STORE.swap_character_head(pid, int(sh_char_id), 0)
            else:
                # 已有同模板头实例则复用，否则创建 UniformPart(eHead=0) 实例
                head_instance_id = 0
                for part in await STORE.list_uniform_parts(pid):
                    if (int(part['character_id']) == int(sh_char_id)
                            and int(part['uniform_part_template_id']) == int(sh_head_template)
                            and int(part['uniform_part_type']) == 0):
                        head_instance_id = int(part['id'])
                        break
                if not head_instance_id:
                    part = await STORE.create_uniform_part(
                        pid, int(sh_char_id), int(sh_head_template), 0)
                    head_instance_id = int(part['id'])
                    if sess.soldier_view_cmd is not None:
                        await _push_kvcs(writer, sess.soldier_view_cmd, b'UniformPart',
                                         _msg_uniform_part(head_instance_id,
                                                           int(sh_char_id),
                                                           int(sh_head_template), 0))
                # ★characters.head_id 必须存 UniformPartTemplate 模板 id（如 89），
                # 不是 uniform_parts 行实例 id：客户端 getUniformPartIdsEquipped 直接
                # UniformPartTemplateTable.getFromId(character.headId)，实例 id 查不到
                # 返回 null → #1009 空引用崩溃。
                await STORE.swap_character_head(pid, int(sh_char_id), int(sh_head_template))
        except Exception as e:
            log(f'[DB] swap head failed: {e}')

    resp_proto = pb_int32(1, 1)  # response = 1 (ok)
    resp = build_response(cmd_id, 'SwapHeadResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SwapHeadResponse (response=1 ok, '
        f'headTemplate={sh_head_template}, headInstance={head_instance_id})')

    # 重推 character（headId f22 + eHeadSwapType f23）
    if pid is not None and sh_char_id:
        try:
            for ch in await STORE.list_characters(pid):
                if int(ch['id']) == int(sh_char_id):
                    await _repush_character_row(sess, writer, ch, head_swap_type)
                    break
        except Exception as e:
            log(f'[DB] reload character after head swap failed: {e}')
    await _push_player_refresh(sess, writer)


HANDLERS = {
    'RequestReadCharacterStats': handle_requestreadcharacterstats,
    'CreateCharacterRequest': handle_createcharacterrequest,
    'SetTutorialCompletedRequest': handle_settutorialcompletedrequest,
    'set_combatbadge_request': handle_set_combatbadge_request,
    'RenameCharacterRequest': handle_renamecharacterrequest,
    'DeleteCharacterRequest': handle_deletecharacterrequest,
    'ChangeCharacterTemplateRequest': handle_changecharactertemplaterequest,
    'ChangeTierRequest': handle_changetierrequest,
    'SetCharacterActiveStatusRequest': handle_setcharacteractivestatusrequest,
    'SetInventoryItemToCharacterRequest': handle_setinventoryitemtocharacterrequest,
    'SendItemToDepotRequest': handle_senditemtodepotrequest,
    'SendUpgradesToDepotRequest': handle_sendupgradestodepotrequest,
    'SetDepotItemViewedRequest': handle_setdepotitemviewedrequest,
    'MarkTutorialTriggeredRequest': handle_marktutorialtriggeredrequest,
    'SwapHeadRequest': handle_swapheadrequest,
}
