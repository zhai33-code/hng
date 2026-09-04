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
    _E_WEAPON_MOD_MAPPING,
    _EXPERIENCE_LEVELS,
    _MOD_MAPPING_IDS,
    _PRICING_MAP,
    _REQ_UNLOCK_BY_ITEM,
    _UNLOCK_UID_META,
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
    RC_INVALID_NAME,
    RC_NO_NAME,
    _char_unlock_bits_for,
    _charge_player,
    _official_name_error,
    _push_player_refresh,
    )

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
        char_pricing_id = f.get(3, 0)
        if isinstance(char_pricing_id, bytes):
            char_pricing_id = int.from_bytes(char_pricing_id, 'little')
        char_currency = f.get(5, 1)
        if isinstance(char_currency, bytes):
            char_currency = int.from_bytes(char_currency, 'little')
        log(f'      CreateCharacter: name={char_name}, packId={char_pack_id}, '
            f'pricingId={char_pricing_id}, currency={char_currency}')
    except Exception as e:
        log(f'      CreateCharacter parse error: {e}')
        char_name = 'Soldier'
        char_pack_id = 0
        char_pricing_id = 0
        char_currency = 1

    # 官方规则校验：空名/非法名拒绝建号（no_name=53 / invalid_name=36）
    name_err = _official_name_error(char_name)
    if name_err:
        resp_proto = pb_int32(1, name_err)
        resp = build_response(cmd_id, 'CreateCharacterResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} CreateCharacterResponse '
            f'(result={name_err} '
            f'{"no_name" if name_err == RC_NO_NAME else "invalid_name"}) '
            f'name={char_name!r}')
        return
    char_name = char_name.strip()

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

    # 收费判定（镜像客户端 SoldierShopMediator.isSoldierFree）：该阵营首个
    # 该兵种且包军衔<1（fake soldier 占位槽 / 新手首兵）免费；其余（商店购买
    # 额外/高军衔兵）按 Pricing 定价扣款；pricingId<=0 / 定价缺失 / 0 价同样
    # 免费（_charge_player 口径）。余额不足：返回货币对应错误码不建号
    # （ResponseCodes: n_credits=56 / n_gold=57 / n_warfunds=153）。
    first_free = False
    if rank < 1 and pid is not None:
        try:
            owned = await STORE.list_characters(pid)
            first_free = not any(c['faction'] == faction and c['template'] == template
                                 for c in owned)
        except Exception as e:
            log(f'[DB] list_characters for charge check failed: {e}')
    paid_cost = 0
    paid_key = None
    if not first_free:
        cost, ok = await _charge_player(sess, char_pricing_id, char_currency)
        if not ok:
            rc = {1: 56, 3: 57, 2: 153}.get(int(char_currency or 1), 56)
            resp = build_response(cmd_id, 'CreateCharacterResponse', pb_int32(1, rc))
            writer.write(resp)
            await writer.drain()
            log(f'S->C cmd={cmd_id:<4} CreateCharacterResponse (result={rc} '
                f'{"n_credits" if rc == 56 else "n_gold" if rc == 57 else "n_warfunds"}) '
                f'cost={cost} name={char_name!r}')
            return
        if cost > 0:
            paid_cost = cost
            paid_key = {1: 'credits', 2: 'warfunds', 3: 'gold'}.get(
                int(char_currency or 1))

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
            # 建号失败但已扣款 → 退款，杜绝钱号两空
            if paid_cost and paid_key:
                try:
                    await STORE.add_currency(pid, **{paid_key: paid_cost})
                    log(f'      [MONEY] refund +{paid_cost} {paid_key} '
                        f'(create failed)')
                except Exception as re_:
                    log(f'[DB] refund after create failure failed: {re_}')
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
                             w.get('condition', 1.0))
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

    # 官方规则校验（trim 后 3..20；no_name=53 / invalid_name=36）——
    # 校验先于扣款，杜绝白扣改名费后改名失败
    rc_new_name = rc_new_name.strip()
    name_err = _official_name_error(rc_new_name)
    if name_err:
        resp_proto = pb_int32(1, name_err)
        resp = build_response(cmd_id, 'RenameCharacterResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} RenameCharacterResponse '
            f'(result={name_err} '
            f'{"no_name" if name_err == RC_NO_NAME else "invalid_name"}) '
            f'name={rc_new_name!r}')
        return

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


def _mod_usable_by_class(mod_template_id, weapon_template_id, faction, char_template):
    """配件是否可供该阵营/兵种使用：WeaponModMapping 的 level>0 解锁需求
    uid 中存在适用于该角色的一条（faction 相符或 0，且 characterType 相符
    或 0）。无映射/无需求（即买即用）恒可用——兵种门槛只来自 uid 元数据
    （如 K98 的 ZF-39 仅 uid(0,6) 侦察、ZF-41 有 (0,6)+(0,1) 双兵种）。"""
    mid = _MOD_MAPPING_IDS.get((int(mod_template_id), int(weapon_template_id)))
    reqs = _REQ_UNLOCK_BY_ITEM.get((mid, _E_WEAPON_MOD_MAPPING)) if mid else None
    if not reqs:
        return True
    for uid in reqs:
        ft, ct = _UNLOCK_UID_META.get(int(uid), (0, 0))
        if (not ft or ft == int(faction)) and (not ct or ct == int(char_template)):
            return True
    return False


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

    # 兵种专属配件剥离（2026-09-04 98k 事故：侦察带镜武器入库再指派步兵，
    # set_inventory_item_character 无差别带走全部挂靠配件 → 步兵拿到侦察
    # 专属瞄具）。入库不拆挂靠（客户端 isModded + "卸下升级件"按钮依赖
    # weapon_id 关联，同兵种往返也应保留配件），在出库指派时按目标兵种
    # 过滤：不兼容配件剥离回仓库（weapon_id/character_id 清 0，单件版
    # SendUpgradesToDepot）。无映射/无需求（即买即用）配件不限制，随武器走。
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
