# -*- coding: utf-8 -*-
"""角色购买/解锁域（characters_purchase）：从 handlers/characters.py 拆出
（纯移动）。

建号（含初始装备发放 _grant_starting_gear）/改名/转职/换头四个
扣款校验请求；统计/教程/depot 转移等仍在 handlers/characters.py。"""

import struct

from protocol import (
    Chunk,
    build_response,
    pb_int32,
    pb_parse,
    )

from state import (
    STORE,
    log,
    )

from wardb import (
    CHARACTER_PACKS,
    _EXPERIENCE_LEVELS,
    _PRICING_MAP,
    _default_ammo_for,
    _VEHICLE_VARIANT_PARENT,
    default_uniform_variant,
    )

from messages import (
    _msg_character,
    _msg_uniform_part,
    _msg_uniform_variant,
    _msg_vehicle,
    _msg_vehicle_ammo,
    _msg_weapon,
    _msg_weapon_ammo,
    _msg_weapon_modifier,
    _push_kvcs,
    )

from player_service import (
    RC_NO_NAME,
    _char_unlock_bits_for,
    _charge_player,
    _official_name_error,
    _push_player_refresh,
    _refund_player_charge,
    )

from starting_gear import STARTING_GEAR


from .characters import _repush_character_row
from .vehicles_purchase import grant_default_vehicle_ammo
from .weapons_purchase import grant_weapon_ammo


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
        # 与购买武器/礼包发货共用的发放函数：首条装备槽 1，其余持有
        weapon_info['ammo'] = await grant_weapon_ammo(
            pid, char_id, w['id'], _default_ammo_for(wt))
        sm = int(gear.get('sight_mod', 0) or 0)
        if sm:
            weapon_info['modifier'] = await STORE.create_weapon_modifier(
                pid, sm, char_id, w['id'])

    # 2. 赠送载具（坦克兵坦克 / 飞行员侦察机）+ 每仓一份默认载具弹药
    vvt = int(gear.get('vehicle_variant', 0) or 0)
    if vvt:
        v = await STORE.create_vehicle(pid, vvt, char_id)
        await STORE.set_vehicle_equipment(pid, v['id'], char_id, 1)
        v['equipped_in_slot'] = 1
        vehicle_info = {'row': v, 'ammo': []}
        parent_vv = _VEHICLE_VARIANT_PARENT.get(vvt)
        if parent_vv is not None:
            # 与买车/礼包发货共用的发放函数：每仓一份，装备第 1 槽
            vehicle_info['ammo'] = await grant_default_vehicle_ammo(
                pid, char_id, v['id'], parent_vv)
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
        char_name = ''
        char_pack_id = 0
        char_pricing_id = 0
        char_currency = 0

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
    if not pack_info:
        resp = build_response(
            cmd_id, 'CreateCharacterResponse', pb_int32(1, 0))
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} CreateCharacterResponse '
            f'(result=0 unknown_character_pack id={char_pack_id})')
        return
    faction, template, rank = pack_info
    log(f'      character faction={faction}, template={template}, rank={rank}')

    # 军衔单源：pack 的起始军衔 → 换算为初始 combat_xp（该军衔等级阈值）。
    # 此后军衔一律由 combat_xp 经 ExperienceLevels 阈值推导，不再单独存军衔。
    start_xp = _EXPERIENCE_LEVELS[rank] if 0 <= rank < len(_EXPERIENCE_LEVELS) else 0
    log(f'      character startCombatXp={start_xp}')

    # 写入数据库；失败时退款并返回失败，禁止使用会话假角色 id。
    # 默认军服实例随建号分配 —— (兵种,阵营) 基础迷彩（官方服角色生来带基础迷彩）
    char_id = None
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
        cost, ok = await _charge_player(
            sess, char_pricing_id, char_currency,
            expected_object_id=char_pack_id,
            expected_types='ShopCharacterPacks')
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
    if not db_created:
        resp = build_response(
            cmd_id, 'CreateCharacterResponse', pb_int32(1, 0))
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} CreateCharacterResponse '
            f'(result=0 persistence_failed)')
        return
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
    #     仅 DB 建号成功时发放。
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
    cost, ok = await _charge_player(
        sess, rc_pricing_id, rc_currency, expected_types='character_rename')
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
    if not updated:
        await _refund_player_charge(sess, rc_currency, cost)

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
    cost, ok = await _charge_player(
        sess, ct_pricing_id, ct_currency, expected_object_id=new_template,
        expected_types='CharacterTemplateSwitchItem')
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
    if old_template is None:
        await _refund_player_charge(sess, ct_currency, cost)

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

    # 先确认角色归属，避免扣款或创建部件后才发现目标无效。
    pid = await sess.ensure_player()
    try:
        character = (await STORE.get_character(pid, int(sh_char_id))
                     if pid is not None and sh_char_id else None)
    except Exception as e:
        log(f'[DB] lookup character before head swap failed: {e}')
        character = None
    if character is None:
        writer.write(build_response(cmd_id, 'SwapHeadResponse', pb_int32(1, 0)))
        await writer.drain()
        return

    # AppearanceWindowMediator.onOKClick 免费保存时不带 pricingId。
    # 与 character.eHeadSwapType=0 的下发契约一致；只有付费请求才走扣款。
    head_swap_type = 1 if sh_pricing_id else 0  # 0=eFree 1=eNeedPayment
    cost = 0
    if sh_head_template and sh_pricing_id:
        cost, ok = await _charge_player(
            sess, sh_pricing_id, sh_currency,
            expected_object_id=sh_head_template,
            expected_types=('UniformPartTemplate', 'SwapToDefaultHead'))
        if not ok:
            resp_proto = pb_int32(1, 56)  # not_enough_credits
            resp = build_response(cmd_id, 'SwapHeadResponse', resp_proto)
            writer.write(resp)
            await writer.drain()
            log(f'S->C cmd={cmd_id:<4} SwapHeadResponse '
                f'(response=56 not_enough) cost={cost}')
            return

    head_instance_id = 0
    if pid is not None and sh_char_id:
        try:
            if not sh_head_template:
                # 换回默认头：清 head_id
                updated = await STORE.swap_character_head(pid, int(sh_char_id), 0)
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
                updated = await STORE.swap_character_head(pid, int(sh_char_id), int(sh_head_template))
            if not updated:
                raise RuntimeError('character head was not persisted')
        except Exception as e:
            log(f'[DB] swap head failed: {e}')
            await _refund_player_charge(sess, sh_currency, cost)
            writer.write(build_response(
                cmd_id, 'SwapHeadResponse', pb_int32(1, 0)))
            await writer.drain()
            return

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
    'CreateCharacterRequest': handle_createcharacterrequest,
    'RenameCharacterRequest': handle_renamecharacterrequest,
    'ChangeCharacterTemplateRequest': handle_changecharactertemplaterequest,
    'SwapHeadRequest': handle_swapheadrequest,
}
