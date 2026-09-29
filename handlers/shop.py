# -*- coding: utf-8 -*-
"""商店域（捆绑包、勋带等级、券包、EOS 权益、金价查询）。

从 realtest_server.dispatch 机械迁移，函数体保持原样；
统一签名 (sess, writer, cmd_id, req_type, proto)。
"""


import os
import struct
import time
import xml.etree.ElementTree as ET

from protocol import (
    Chunk,
    build_response,
    pb_bool,
    pb_int64,
    pb_int32,
    pb_message,
    pb_parse,
    pb_string,
    )

from state import (
    GAME_DIR,
    STORE,
    log,
    )

from wardb import (
    _AMMO_MAXCOND,
    _CUR_NAME,
    _EXPERIENCE_LEVELS,
    _RIBBON_LEVEL_XP,
    _RIBBON_META_LEVEL,
    _RIBBON_UPGRADES,
    _VEHICLE_VARIANT_PARENT,
    _default_ammo_for,
    _parse_wardb_table,
    _rank_from_combat_xp,
    _ribbon_level_from_xp,
    default_uniform_variant,
    vehicle_full_condition,
    MEMBERSHIP_DAYS_BY_ID,
    BOOSTER_MINUTES_BY_ID,
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
    _msg_weapon_variant,
    _push_kvcs,
    )

from player_service import (
    _char_unlock_bits_for,
    _charge_player,
    _msg_player_for,
    _push_player_refresh,
    push_ribbon_unlock_notifications,
    )

from handlers.economy import (
    _BOOSTER_MINUTES,
    push_booster_rows,
    push_membership_rows,
    )

from handlers.vehicles_purchase import grant_default_vehicle_ammo
from handlers.weapons_purchase import grant_weapon_ammo


# ===== StoreBundle 发货数据（模块加载时从 WarDatabase 构建一次）=====
# bundleId -> (targetType, factionTemplateIds, identifier, itemsXml)
_STORE_BUNDLES = {}
for _f in _parse_wardb_table('StoreBundle'):
    _ttype = _f.get(3, b'')
    _factions = _f.get(4, b'')
    _ident = _f.get(2, b'')
    _items = _f.get(7, b'')
    _STORE_BUNDLES[int(_f.get(1, 0))] = (
        _ttype.decode('utf-8', 'replace') if isinstance(_ttype, bytes) else str(_ttype),
        _factions.decode('utf-8', 'replace') if isinstance(_factions, bytes) else str(_factions),
        _ident.decode('utf-8', 'replace') if isinstance(_ident, bytes) else str(_ident),
        _items.decode('utf-8', 'replace') if isinstance(_items, bytes) else str(_items),
        )

# WeaponVariantTemplate { id=1, name=2, weaponTemplateId=3, ... }
# → (武器名, 武器基础模板 id)：礼包 items 里的 WeaponVariantTemplate 是商店
# 武器商品（含皮肤），创建武器实例需要基础模板 + 变体模板两个字段
_WEAPON_VARIANT_META = {}
for _f in _parse_wardb_table('WeaponVariantTemplate'):
    _n = _f.get(2, b'')
    _WEAPON_VARIANT_META[int(_f.get(1, 0))] = (
        _n.decode('utf-8', 'replace') if isinstance(_n, bytes) else str(_n),
        int(_f.get(3, 0)))

# 变体图标可用性：带变体的武器客户端请求 images/Weapons/<武器模板>_<变体模板>.png
# （Thumbnails/ 同理），加载失败记入 _dontTryLoad → 图标空白整个会话不重试。
# 客户端素材库并不包含全部变体组合——Factory（出厂全新款）95/97 缺失（官方同步
# 清单里就没有），皮肤也有零星缺口。仅当变体图标存在时才挂变体发货；否则按裸
# 武器发货（客户端回落基础图标 <武器模板>.png，与普通武器购买一致）。
# 变体只影响外观（Factory/Used 是成色状态，其余是皮肤），属性与裸武器完全相同。
_VARIANT_ICON_OK = set()
for _vid, (_n, _wt) in _WEAPON_VARIANT_META.items():
    _root = os.path.join(GAME_DIR, 'images', 'Weapons', f'{_wt}_{_vid}.png')
    _thumb = os.path.join(GAME_DIR, 'images', 'Weapons', 'Thumbnails',
                          f'{_wt}_{_vid}.png')
    if os.path.isfile(_root) and os.path.isfile(_thumb):
        _VARIANT_ICON_OK.add(_vid)

# weaponAmmoMapping / WeaponModMapping：mappingId → 弹药/配件模板 id
# （礼包 items 里嵌套在 WeaponVariantTemplate 下的是 mapping id，不是模板 id）
_AMMO_TPL_BY_MAPPING = {}
for _f in _parse_wardb_table('weaponAmmoMapping'):
    _AMMO_TPL_BY_MAPPING[int(_f.get(1, 0))] = int(_f.get(3, 0))
_MOD_TPL_BY_MAPPING = {}
for _f in _parse_wardb_table('WeaponModMapping'):
    _MOD_TPL_BY_MAPPING[int(_f.get(1, 0))] = int(_f.get(2, 0))


def _first_faction(factions_str):
    """'1,2,3' → 1（礼包 factionTemplateIds 首个有效阵营）。"""
    for part in str(factions_str).split(','):
        part = part.strip()
        if part.isdigit() and int(part) > 0:
            return int(part)
    return 1


async def _bundle_deliver_weapon(sess, writer, pid, char_id, wvt_id, nested, equip_slot1):
    """礼包武器发货：创建武器实例（含皮肤变体）+ 嵌套弹药/配件 + KVCS 推送。

    nested 为 XML 子节点列表（weaponAmmoMapping / WeaponModMapping）；
    未嵌套弹药映射时赠送默认弹药（与武器商店购买一致，首个装备槽 1）。
    equip_slot1=True 时武器装备到角色主武器槽（新角色首发武器）。"""
    meta = _WEAPON_VARIANT_META.get(int(wvt_id))
    if meta is None:
        log(f'      [bundle] weapon variant {wvt_id} not in WeaponVariantTemplate, skipped')
        return
    wname, wt = meta
    # 变体图标缺失（如 Factory 成色款）→ 按裸武器发货，不挂变体
    # （挂了客户端请求 <wt>_<wvt>.png 加载失败，图标空白且整个会话不重试）
    use_variant = int(wvt_id) in _VARIANT_ICON_OK
    if not use_variant:
        log(f'      [bundle] weapon variant {wvt_id} ({wname}) icon missing, '
            f'delivering plain weapon (base icon)')
    w = await STORE.create_weapon(pid, wname, wt, char_id,
                                  int(wvt_id) if use_variant else 0)
    wid = w['id']
    variant_id = 0
    if use_variant:
        vv = await STORE.create_weapon_variant(pid, wid, char_id, int(wvt_id))
        variant_id = vv['id']
        await STORE.set_weapon_variant(pid, wid, variant_id)
    w['container_id'] = 0
    w['container_slot'] = 0
    if equip_slot1:
        await STORE.update_weapon_container(pid, wid, char_id, 1)
        w['container_id'] = int(char_id)
        w['container_slot'] = 1

    # 弹药：嵌套 mapping id 优先，否则按基础模板默认弹药
    # （公共发放函数与购买武器/建号共用：首条装备槽 1，其余持有）
    ammo_rows = []
    nested_ammo = [int(c.get('id', 0) or 0) for c in nested
                   if c.tag.lower() == 'weaponammomapping']
    if nested_ammo:
        mappings = []
        for mid in nested_ammo:
            at = _AMMO_TPL_BY_MAPPING.get(mid)
            if at is None:
                log(f'      [bundle] ammo mapping {mid} unknown, skipped')
                continue
            mappings.append((mid, at))
        ammo_rows = await grant_weapon_ammo(pid, char_id, wid, mappings)
    else:
        ammo_rows = await grant_weapon_ammo(pid, char_id, wid,
                                            _default_ammo_for(wt))

    # 配件：嵌套 WeaponModMapping
    mod_rows = []
    for c in nested:
        if c.tag.lower() != 'weaponmodmapping':
            continue
        mdt = _MOD_TPL_BY_MAPPING.get(int(c.get('id', 0) or 0))
        if mdt is None:
            log(f'      [bundle] mod mapping {c.get("id")} unknown, skipped')
            continue
        m = await STORE.create_weapon_modifier(pid, mdt, char_id, wid)
        mod_rows.append(m)

    # KVCS 推送（须在 character 行之后；调用方保证顺序）
    if sess.soldier_view_cmd is not None:
        if use_variant:
            await _push_kvcs(writer, sess.soldier_view_cmd, b'WeaponVariant',
                             _msg_weapon_variant(variant_id, char_id, wid, int(wvt_id)))
        wproto = _msg_weapon(wid, w.get('name', wname), w.get('template_id', wt),
                             w.get('character_id', char_id), w['container_id'],
                             w['container_slot'], variant_id,
                             w.get('clip_count', 0), w.get('auto_repair', 0),
                             w.get('condition', 1.0))
        await _push_kvcs(writer, sess.soldier_view_cmd, b'weapon', wproto)
        for (_mid, a) in ammo_rows:
            await _push_kvcs(writer, sess.soldier_view_cmd, b'WeaponAmmo',
                             _msg_weapon_ammo(a['id'], a['ammo_template_id'],
                                              a['character_id'], a['weapon_id'],
                                              a['slot_index'], a['condition']))
        for m in mod_rows:
            await _push_kvcs(writer, sess.soldier_view_cmd, b'WeaponModifier',
                             _msg_weapon_modifier(m['id'], m['template_id'],
                                                  m['character_id'], m['weapon_id'],
                                                  m.get('auto_repair', 0),
                                                  m.get('condition', 1.0)))
        log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet (weapon id={wid}, '
            f'variantTemplate={wvt_id}, ammo={len(ammo_rows)}, mods={len(mod_rows)}, '
            f'char={char_id}) [bundle]')


async def _bundle_deliver_vehicle(sess, writer, pid, char_id, vvt_id, equip_slot1):
    """礼包载具发货：创建载具 + 默认载具弹药（按弹药仓顺延装备）+ KVCS 推送。"""
    v = await STORE.create_vehicle(pid, int(vvt_id), char_id)
    vid = v['id']
    cond = v.get('condition')
    if cond is None:
        cond = vehicle_full_condition(int(vvt_id))
    if equip_slot1:
        await STORE.set_vehicle_equipment(pid, vid, char_id, 1)
    ammo_rows = []
    parent_vv = _VEHICLE_VARIANT_PARENT.get(int(vvt_id))
    if parent_vv is not None:
        # 与买车/建号共用的发放函数：每仓按 storageSlots 发满装填；
        # KVCS 由本函数统一推（须先推 Vehicle 行再弹药行）
        ammo_rows = await grant_default_vehicle_ammo(pid, char_id, vid, parent_vv)
    if sess.soldier_view_cmd is not None:
        await _push_kvcs(writer, sess.soldier_view_cmd, b'Vehicle',
                         _msg_vehicle(vid, int(vvt_id), char_id,
                                      1 if equip_slot1 else 0, condition=cond))
        for (storage_id, slot_ammo, va) in ammo_rows:
            await _push_kvcs(writer, sess.soldier_view_cmd, b'VehicleAmmo',
                             _msg_vehicle_ammo(va['id'], va['ammo_template_id'],
                                               vid, storage_id, slot_ammo,
                                               char_id, va['condition']))
        log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet (Vehicle '
            f'id={vid}, variantTemplate={vvt_id}, ammo={len(ammo_rows)}, '
            f'char={char_id}) [bundle]')


async def _bundle_deliver_character(sess, writer, pid, el, factions_str, identifier):
    """礼包新角色发货：创建角色（rank 属性 → 初始 combat_xp）+ KVCS 推送。

    返回新角色 id；名字取 identifier 冒号后的兵种名（如
    'US Soldier Pack: Medium Tanker' → 'Medium Tanker'），重名追加序号。"""
    template = int(el.get('id', 0) or 0)
    rank = int(el.get('rank', 0) or 0)
    faction = int(el.get('factiontemplateid', 0) or 0) or _first_faction(factions_str)

    base = identifier.split(':')[-1].strip() if ':' in identifier else ''
    if not (3 <= len(base) <= 20):
        base = 'Soldier'
    existing = set()
    try:
        existing = {ch['name'] for ch in await STORE.list_characters(pid)}
    except Exception as e:
        log(f'[DB] list_characters for bundle name failed: {e}')
    name, n = base, 2
    while name in existing and n < 100:
        suffix = f' {n}'
        name = base[:20 - len(suffix)] + suffix
        n += 1

    start_xp = _EXPERIENCE_LEVELS[rank] if 0 <= rank < len(_EXPERIENCE_LEVELS) else 0
    uv_template_id = default_uniform_variant(template, faction)
    row = await STORE.create_character(pid, name, faction, template, start_xp,
                                       uniform_variant_template_id=uv_template_id)
    char_id = row['id']
    uv_id = int(row.get('uniform_variant_id', 0) or 0)

    if sess.soldier_view_cmd is not None:
        # UniformVariant 必须先于 character 推送（客户端按 uniformVariantId
        # 查本地表，实例未入表 → Error #1009 且中断 KVCS 发布循环）
        if uv_id:
            await _push_kvcs(writer, sess.soldier_view_cmd, b'UniformVariant',
                             _msg_uniform_variant(uv_id, uv_template_id, char_id))
        bits = await _char_unlock_bits_for(sess, template, faction, char_id)
        await _push_kvcs(writer, sess.soldier_view_cmd, b'character',
                         _msg_character(name, faction, template,
                                        _rank_from_combat_xp(start_xp), char_id,
                                        bits, uniform_variant_id=uv_id))
        log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet (character '
            f'{name!r}, template={template}, faction={faction}, rank={rank}, '
            f'id={char_id}) [bundle]')
    return char_id


def _bundle_item_blocks(items_xml):
    """解析 items XML，返回 <items> 根列表。

    单根（常规礼包）→ [root]；多根（多阵营礼包，如 595230 每个
    factionTemplateId 一个 <items>，武器各阵营不同）→ 包一层后取全部
    <items> 子节点；无 <items> 标签的杂项 → 整体作为单根返回。"""
    try:
        root = ET.fromstring(items_xml)
    except ET.ParseError:
        root = ET.fromstring('<root>%s</root>' % items_xml)
    if root.tag.lower() == 'items':
        return [root]
    blocks = [b for b in root if b.tag.lower() == 'items']
    return blocks if blocks else [root]


async def _deliver_store_bundle(sess, writer, pid, bundle_id, req_char_id):
    """解析 StoreBundle.items XML 并逐项发货（入库 + KVCS 推送）。

    支持的元素：charactertemplate（含嵌套武器/载具，创建新角色）、
    WeaponVariantTemplate（含嵌套 weaponAmmoMapping/WeaponModMapping）、
    VehicleVariantTemplate、RibbonBoosterTemplate、ShopMembershipItems、
    gold、uniformparttemplate。返回 (加成数, 会员数) 供调用方决定是否
    推送对应 KVCS 表。"""
    info = _STORE_BUNDLES.get(int(bundle_id))
    if info is None:
        log(f'      [bundle] StoreBundle {bundle_id} not found in WarDatabase, '
            f'nothing delivered')
        return 0, 0
    target_type, factions, identifier, items_xml = info
    try:
        blocks = _bundle_item_blocks(items_xml)
    except Exception as e:
        log(f'      [bundle] StoreBundle {bundle_id} items XML parse failed: {e}')
        return 0, 0
    root = blocks[0]
    if len(blocks) > 1:
        # 多阵营礼包：按请求角色阵营选取对应 <items factionTemplateId=N>，
        # 角色未知/不匹配时回落首个块（避免三份全发——武器按阵营互斥）
        try:
            fac = None
            for ch in await STORE.list_characters(pid):
                if int(ch['id']) == int(req_char_id or 0):
                    fac = str(int(ch['faction']))
                    break
            if fac:
                for b in blocks:
                    if (b.get('factionTemplateId') or '').strip() == fac:
                        root = b
                        break
        except Exception as e:
            log(f'      [bundle] faction block select failed: {e}')
    log(f'      [bundle] delivering {identifier!r} (targetType={target_type}, '
        f'{len(list(root))} top-level items)')

    n_boosters = 0
    n_memberships = 0
    for el in list(root):
        tag = el.tag.lower()
        try:
            if tag == 'charactertemplate':
                # 新角色：嵌套武器/载具发给新角色；首件武器/载具装备槽 1
                new_char = await _bundle_deliver_character(
                    sess, writer, pid, el, factions, identifier)
                first_weapon = True
                first_vehicle = True
                for sub in list(el):
                    stag = sub.tag.lower()
                    if stag == 'weaponvarianttemplate':
                        await _bundle_deliver_weapon(
                            sess, writer, pid, new_char, sub.get('id'),
                            list(sub), equip_slot1=first_weapon)
                        first_weapon = False
                    elif stag == 'vehiclevarianttemplate':
                        await _bundle_deliver_vehicle(
                            sess, writer, pid, new_char, sub.get('id'),
                            equip_slot1=first_vehicle)
                        first_vehicle = False
                    else:
                        log(f'      [bundle] unknown nested element '
                            f'<{sub.tag}> under charactertemplate, skipped')
            elif tag == 'weaponvarianttemplate':
                await _bundle_deliver_weapon(sess, writer, pid, req_char_id,
                                             el.get('id'), list(el),
                                             equip_slot1=False)
            elif tag == 'vehiclevarianttemplate':
                await _bundle_deliver_vehicle(sess, writer, pid, req_char_id,
                                              el.get('id'), equip_slot1=False)
            elif tag == 'ribbonboostertemplate':
                await STORE.add_booster(pid, int(el.get('id', 0) or 0),
                                        duration_minutes=_BOOSTER_MINUTES,
                                        activate_now=False, is_ribbon=True)
                n_boosters += 1
            elif tag == 'shopmembershipitems':
                mid = int(el.get('id', 0) or 0)
                days = MEMBERSHIP_DAYS_BY_ID.get(mid, 30)
                await STORE.add_membership(pid, mid, duration_days=days)
                n_memberships += 1
            elif tag == 'gold':
                amount = int(el.get('amount', 0) or 0)
                if amount:
                    await STORE.add_currency(pid, gold=amount)
                    log(f'      [bundle] granted {amount} gold')
            elif tag == 'uniformparttemplate':
                tid = int(el.get('id', 0) or 0)
                part = await STORE.create_uniform_part(pid, req_char_id, tid, 1)
                if sess.soldier_view_cmd is not None:
                    await _push_kvcs(writer, sess.soldier_view_cmd, b'UniformPart',
                                     _msg_uniform_part(part['id'], req_char_id,
                                                       tid, 1))
                    log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                        f'(UniformPart id={part["id"]}, template={tid}, '
                        f'char={req_char_id}) [bundle]')
            else:
                log(f'      [bundle] unknown element <{el.tag}>, skipped')
        except Exception as e:
            # 单项失败不影响其余物品发货（钱已扣，尽量多发货并留日志排查）
            log(f'[DB] bundle item <{el.tag} id={el.get("id")}> delivery '
                f'failed: {e}')
    log(f'      [bundle] {identifier!r} delivered: +{n_boosters} boosters, '
        f'+{n_memberships} memberships')
    return n_boosters, n_memberships


async def handle_buystorebundlerequest(sess, writer, cmd_id, req_type, proto):
    # 商店捆绑包购买请求
    # BuyStoreBundleRequest { int64 storeBundleId=1; int64 pricingId=2;
    #   int64 storeDiscountId=3; int64 characterId=4; int32 paymentCurrency=5; }
    # BuyStoreBundleResponse { int32 result=1; }
    # paymentCurrency: 1=Credits, 2=Warfunds, 3=Gold, 4=Tickets
    # ResponseCodes: ok=1, not_enough_gold=57, not_enough_credits=56, not_enough_warfunds=153
    bundle_id = 0
    pricing_id = 0
    char_id = 0
    currency = 0
    try:
        f = pb_parse(proto)
        bundle_id = f.get(1, 0)
        pricing_id = f.get(2, 0)
        discount_id = f.get(3, 0)
        char_id = f.get(4, 0)
        currency = f.get(5, 0)
        currency_names = {1: 'Credits', 2: 'Warfunds', 3: 'Gold', 4: 'Tickets'}
        log(f'      BuyStoreBundle: bundleId={bundle_id}, pricingId={pricing_id}, '
            f'discountId={discount_id}, charId={char_id}, currency={currency_names.get(currency, currency)}')
    except Exception as e:
        log(f'      BuyStoreBundle parse error: {e}')

    # 扣款；余额不足返回错误响应
    cost, ok = await _charge_player(sess, pricing_id, currency)
    if not ok:
        resp_proto = pb_int32(1, 56)  # not_enough_credits
        resp = build_response(cmd_id, 'BuyStoreBundleResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} BuyStoreBundleResponse (result=56 not_enough) cost={cost}')
        return

    # ★ 发货：解析 WarDatabase StoreBundle.items XML，逐项入库并推送
    #   （此前只扣款返回 ok，礼包物品从未入库——钱扣了东西不到账的根因）
    n_boosters, n_memberships = 0, 0
    pid = await sess.ensure_player()
    if pid is not None and bundle_id:
        try:
            n_boosters, n_memberships = await _deliver_store_bundle(
                sess, writer, pid, bundle_id, char_id)
        except Exception as e:
            log(f'[DB] deliver store bundle {bundle_id} failed: {e}')
    elif pid is None:
        log('      [bundle] no player id, delivery skipped')

    # 返回 result=1 (ok)，客户端 dispatch PurchaseCompletedSignal 显示购买成功弹窗
    resp_proto = pb_int32(1, 1)  # result = 1 (ok)
    resp = build_response(cmd_id, 'BuyStoreBundleResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} BuyStoreBundleResponse (result=1 ok, delivered '
        f'boosters={n_boosters}, memberships={n_memberships})')

    # 加成/会员全量刷新（幂等）：有购买才推，避免无谓 KVCS
    if pid is not None:
        if n_boosters:
            try:
                await push_booster_rows(sess, writer, int(pid))
            except Exception as e:
                log(f'[DB] push booster rows after bundle failed: {e}')
        if n_memberships:
            try:
                await push_membership_rows(sess, writer, int(pid))
            except Exception as e:
                log(f'[DB] push membership rows after bundle failed: {e}')

    # 通过 PLAYER_VIEW 推送更新后的 player 数据，刷新货币显示
    if sess.playerview_cmd is not None:
        player_proto = await _msg_player_for(sess)
        data_chunk = Chunk(player_proto)
        table_chunk = Chunk(b'player', [data_chunk])
        action_chunk = Chunk(b'set', [table_chunk])
        title_chunk = Chunk(b'KeyValueChangeSet', [action_chunk])
        cs_main = Chunk(struct.pack('<i', sess.playerview_cmd), [title_chunk])
        cs_bytes = cs_main.write(b'')
        writer.write(cs_bytes)
        await writer.drain()
        log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet (player refresh, {len(player_proto)}B)')

    # 发货后重推角色行（unlockBits 刷新，新购物品立即解锁可用）
    await _repush_characters_after_delivery(sess, writer, int(pid) if pid is not None else None)



async def handle_queryshopwarbonditemsrequest(sess, writer, cmd_id, req_type, proto):
    # 查询战争债券物品（PlayerProfileMediator / WarBondsWindowMediator）
    # QueryShopWarBondItemsResponse { repeated ShopWarBondItem warBonds = 2; }
    # ShopWarBondItem { int64 id=1; int64 templateId=2; uint32 serialNumber=3;
    #   int64 playerId=4; string aquiredDate=5; string lastInterestDate=6;
    #   bool isExpired=7; }
    # 数据源 player_warbonds（AddWarBondRequest 购买入库）；
    # 注意：repeated 字段零条目时必须完全省略字段（b''），
    # 发送 pb_message(2, b'') 空条目会导致客户端解析越界（Buffer overflow）卡死队列
    resp_proto = b''
    pid = await sess.ensure_player()
    rows = []
    if pid is not None and STORE is not None:
        try:
            rows = await STORE.list_warbonds(int(pid))
        except Exception as e:
            log(f'      [DB] list_warbonds failed: {e}')
            rows = []
    for i, r in enumerate(rows, 1):
        m = pb_int64(1, r['template_id'])      # id（模板 id 当行 id）
        m += pb_int64(2, r['template_id'])     # templateId
        m += pb_int32(3, i)                    # serialNumber（第几张）
        m += pb_int64(4, int(pid))             # playerId
        m += pb_string(5, '')                  # aquiredDate
        m += pb_string(6, '')                  # lastInterestDate
        m += pb_bool(7, False)                 # isExpired
        resp_proto += pb_message(2, m)
    resp = build_response(cmd_id, 'QueryShopWarBondItemsResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} QueryShopWarBondItemsResponse '
        f'({len(rows)} war bonds)')



async def _deliver_voucher_items(sess, writer, pid, char_id, items):
    """兑换码物品发货（入库 + KVCS 推送），复用商店礼包的发放基建。

    items 为 items_json 解析结果：货币 {"type","amount"}、道具 {"type","id"}。
    返回 (勋带加成数, 会员数, 是否发了货币) 供调用方决定推送。"""
    n_boosters = 0
    n_memberships = 0
    currency = False
    cur = {'credits': 0, 'warfunds': 0, 'gold': 0}
    for it in items or []:
        t = str(it.get('type', '')).lower()
        try:
            if t in ('credits', 'warfunds', 'gold'):
                amt = int(it.get('amount', 0) or 0)
                if amt:
                    cur[t] += amt
            elif t == 'weaponvarianttemplate':
                await _bundle_deliver_weapon(sess, writer, pid, char_id,
                                             int(it.get('id', 0) or 0),
                                             [], equip_slot1=False)
            elif t == 'vehiclevarianttemplate':
                await _bundle_deliver_vehicle(sess, writer, pid, char_id,
                                              int(it.get('id', 0) or 0),
                                              equip_slot1=False)
            elif t == 'uniformparttemplate':
                tid = int(it.get('id', 0) or 0)
                part = await STORE.create_uniform_part(pid, char_id, tid, 1)
                if sess.soldier_view_cmd is not None:
                    await _push_kvcs(writer, sess.soldier_view_cmd,
                                     b'UniformPart',
                                     _msg_uniform_part(part['id'], char_id,
                                                       tid, 1))
            elif t == 'ribbonboostertemplate':
                bid = int(it.get('id', 0) or 0)
                await STORE.add_booster(pid, bid,
                                        duration_minutes=BOOSTER_MINUTES_BY_ID.get(
                                            bid, _BOOSTER_MINUTES),
                                        activate_now=False, is_ribbon=True)
                n_boosters += 1
            elif t == 'shopmembershipitems':
                mid = int(it.get('id', 0) or 0)
                days = MEMBERSHIP_DAYS_BY_ID.get(mid, 30)
                await STORE.add_membership(pid, mid, duration_days=days)
                n_memberships += 1
            else:
                log(f'      [voucher] unsupported item type {t!r}, skipped')
        except Exception as e:
            # 单项失败不阻断整包（voucher_claim 已先标记 granted，宁缺毋错）
            log(f'      [voucher] deliver item {t} failed: {e}')
    if any(cur.values()):
        await STORE.add_currency(pid, credits=cur['credits'],
                                 warfunds=cur['warfunds'], gold=cur['gold'])
        currency = True
        log(f'      [voucher] granted currency {cur}')
    return n_boosters, n_memberships, currency


async def _repush_characters_after_delivery(sess, writer, pid):
    """发货后重推角色行：unlockBits 含新购物品解锁需求 → 客户端
    EquipmentItemItemRenderer.isUnlocked 判定立即生效；否则已拥有武器
    以旧 unlockBits 评价 → 误报“该武器不适用于你所选的职业”直到重登。
    新建角色的初始 character 行在嵌套武器入库前推送，bits 同样过期，一并重推。
    （BuyStoreBundle 与兑换码 voucher claim 共用）"""
    if pid is None or sess.soldier_view_cmd is None:
        return
    try:
        new_ts = int(time.time() * 1000)
        for ch in await STORE.list_characters(pid):
            bits = await _char_unlock_bits_for(sess, ch['template'],
                                               ch['faction'], ch['id'])
            # 尾部字段传全：客户端 setObject 整体替换 schema，漏传
            # head_id/previous_template 等会被抹回默认值（外观回退）
            await _push_kvcs(writer, sess.soldier_view_cmd, b'character',
                             _msg_character(ch['name'], ch['faction'], ch['template'],
                                            _rank_from_combat_xp(ch.get('combat_xp', 0)),
                                            ch['id'], bits,
                                            ch.get('combat_badge_0', 0),
                                            ch.get('combat_badge_1', 0),
                                            new_ts,
                                            ch.get('uniform_variant_id', 0),
                                            ch.get('headgear_id', 0),
                                            1 if ch.get('is_deleted') else 0,
                                            ch.get('previous_template', 0),
                                            ch.get('head_id', 0),
                                            0,
                                            1 if ch.get('is_on_leave') else 0))
            log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet (character '
                f'{ch["name"]} re-pushed, unlockBits={sum(bits)} true, '
                f'statTs={new_ts}) [delivery]')
    except Exception as e:
        log(f'[DB] reload characters after delivery failed: {e}')


def _voucher_pack_info_msg(pack):
    """VoucherPackInfo {identifier=1, factionTemplateId=2, status=3,
    names=4 repeated {language=1, name=2}, items=5 repeated
    {objectType=1, objectId=2}}——客户端 BundleWindow 按 objectType 本地化
    渲染明细（货币 objectId=数量，道具 objectId=模板 id）。"""
    note = (pack.get('note') or '').strip()
    m = pb_string(1, note or pack['pack_name'])   # identifier
    m += pb_int64(2, 0)                            # factionTemplateId
    m += pb_int32(3, 1)                            # status = granted
    nm = pb_string(1, 'en') + pb_string(2, note or '补给包')
    m += pb_message(4, nm)
    for it in pack.get('items') or []:
        t = str(it.get('type', ''))
        oid = (int(it.get('amount', 0) or 0)
               if t in ('credits', 'warfunds', 'gold')
               else int(it.get('id', 0) or 0))
        im = pb_string(1, t) + pb_int64(2, oid)
        m += pb_message(5, im)
    return m


async def handle_queryvoucherpacksrequest(sess, writer, cmd_id, req_type, proto):
    """QueryVoucherPacksRequest {characterId=1} → 兑换码统一弹窗流程。

    characterId=0（登录自动检查 / 输码后回查）：有 pending 包 → 264
    （客户端弹士兵选择轮播；valid ids 留空 = 全部士兵可选）；
    characterId>0（玩家已选定士兵）：voucher_claim 标记 granted 后逐包发货，
    回 ok + infos[]，客户端 BundleWindow 显示"你收到了补给"明细。
    客户端 ServiceHandler 的请求队列是严格串行的：
    一个请求没收到响应，后续所有请求（包括购买）都会堵在队列里（左上角 loading 转圈）。
    """
    char_id = 0
    try:
        f = pb_parse(proto)
        char_id = int(f.get(1, 0) or 0)
    except Exception as e:
        log(f'      [voucher] QueryVoucherPacks parse error: {e}')

    pid = await sess.ensure_player()
    packs = []
    need_char = False
    if pid is not None and STORE is not None:
        try:
            if char_id:
                # 校验角色归属（失败 → ok 空列表，宁缺毋错；pending 保留待重选）
                ch = await STORE.get_character(int(pid), char_id)
                if ch is not None:
                    packs = await STORE.voucher_claim(int(pid), char_id)
                    n_boosters = n_memberships = 0
                    has_currency = False
                    for pack in packs:
                        b, m, c = await _deliver_voucher_items(
                            sess, writer, int(pid), char_id, pack['items'])
                        n_boosters += b
                        n_memberships += m
                        has_currency = has_currency or c
                    # 加成/会员/货币全量刷新（幂等，照搬 BuyStoreBundle 推送模式）
                    if n_boosters:
                        try:
                            await push_booster_rows(sess, writer, int(pid))
                        except Exception as e:
                            log(f'[DB] push booster rows after voucher failed: {e}')
                    if n_memberships:
                        try:
                            await push_membership_rows(sess, writer, int(pid))
                        except Exception as e:
                            log(f'[DB] push membership rows after voucher failed: {e}')
                    if has_currency:
                        try:
                            await _push_player_refresh(sess, writer)
                        except Exception as e:
                            log(f'[DB] player refresh after voucher failed: {e}')
                    # 发货后重推角色行（与 BuyStoreBundle 一致）：unlockBits
                    # 刷新，新武器/皮肤当场解锁可用，无需重登
                    await _repush_characters_after_delivery(sess, writer, int(pid))
            else:
                packs = await STORE.voucher_pending_packs(int(pid))
                need_char = bool(packs)
        except Exception as e:
            log(f'      [DB] voucher query/claim failed: {e}')
            packs = []

    resp_proto = pb_int32(1, 264 if need_char else 1)
    if not need_char:
        for pack in packs:
            resp_proto += pb_message(2, _voucher_pack_info_msg(pack))
    resp_proto += pb_string(3, '')   # validFactionTemplateIds（空 = 不限）
    resp_proto += pb_string(4, '')   # validCharacterTemplateIds（空 = 不限）
    resp = build_response(cmd_id, 'QueryVoucherPacksResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    if need_char:
        log(f'S->C cmd={cmd_id:<4} QueryVoucherPacksResponse '
            f'(264 character_id_needed, {len(packs)} pending packs)')
    else:
        log(f'S->C cmd={cmd_id:<4} QueryVoucherPacksResponse '
            f'(ok, {len(packs)} packs granted, char={char_id})')



async def handle_epicqueryentitlementsrequest(sess, writer, cmd_id, req_type, proto):
    # Epic 渠道周期性查询用户拥有的道具权益（DLC / 兑换券）
    # 触发点：VoucherRedemptionModel.checkForEpicEntitlements —— 只要 Global.isEpic=true 就会
    # 在登录后、UI 转场后周期性发送，客户端串行队列，必须回复解锁。
    # 客户端 epicQueryEntitlementsResponse 只读 response 字段就 checkFlow(true) 继续流程，
    # 不消费任何 entitlement 数据，故返回 response=ok + epicResponseCode=0 即可。
    # EpicQueryEntitlementsResponse { ResponseCodes response=1; int32 epicResponseCode=2; }
    resp_proto = b''
    resp_proto += pb_int32(1, 1)       # response = 1 (ResponseCodes.ok)
    resp_proto += pb_int32(2, 0)       # epicResponseCode = 0
    resp = build_response(cmd_id, 'EpicQueryEntitlementsResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} EpicQueryEntitlementsResponse (response=1 ok, no entitlements)')



async def handle_queryactivesurveyrequest(sess, writer, cmd_id, req_type, proto):
    # 查询活跃问卷调查，返回空 URL（无问卷，客户端跳过弹窗继续流程）
    # QueryActiveSurveyResponse { string queryURL=1; string imageURL=2; }
    resp_proto = b''
    resp_proto += pb_string(1, '')      # queryURL (空 = 无问卷)
    resp_proto += pb_string(2, '')      # imageURL (空)
    resp = build_response(cmd_id, 'QueryActiveSurveyResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} QueryActiveSurveyResponse (no survey)')



async def handle_getgoldpricesrequest(sess, writer, cmd_id, req_type, proto):
    # 查询金币价格表，返回空列表（无充值渠道；私服玩家货币由服务器发放）
    # GetGoldPricesResponse { repeated Price prices=1; string currencyCode=2; int32 response=3; }
    # 注意：repeated 字段零条目时必须完全省略（b''），否则客户端解析越界
    resp_proto = b''
    resp_proto += pb_string(2, 'EUR')   # currencyCode
    resp_proto += pb_int32(3, 1)        # response = 1 (ok)
    resp = build_response(cmd_id, 'GetGoldPricesResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} GetGoldPricesResponse (0 gold prices)')



async def handle_addribbonlevelrequest(sess, writer, cmd_id, req_type, proto):
    # 购买勋带等级（ProgressionOverviewWindowMediator.levelPriceClicked）
    # AddRibbonLevelRequest { int64 ribbonLevelUpgradeTemplateId=1; int64 characterId=2;
    #   int64 pricingId=3; int64 discountId=4; int32 paymentCurrency=5; }
    # AddRibbonLevelResponse { int32 result = 1; }
    upgrade_template_id = 0
    pricing_id = 0
    try:
        f = pb_parse(proto)
        upgrade_template_id = f.get(1, 0)
        char_id = f.get(2, 0)
        pricing_id = f.get(3, 0)
        currency = f.get(5, 0)
        log(f'      AddRibbonLevel: ribbonLevelUpgradeTemplateId={upgrade_template_id}, '
            f'charId={char_id}, pricingId={pricing_id}, '
            f'currency={_CUR_NAME.get(int(currency), currency)}')
    except Exception as e:
        log(f'      AddRibbonLevel parse error: {e}')

    # 扣款（购买勋带等级费用）；余额不足返回错误响应
    cost, ok = await _charge_player(sess, pricing_id, currency)
    if not ok:
        resp_proto = pb_int32(1, 56)  # not_enough_credits
        resp = build_response(cmd_id, 'AddRibbonLevelResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} AddRibbonLevelResponse (result=56 not_enough) cost={cost}')
        return

    # 解锁该勋带等级对应的全部解锁需求（unlockMetaName==ribbonTemplateId,
    # unlockMetaLevel <= ribbonLevelId），持久化后客户端 isUnlocked=true
    unlocked_uids = set()
    info = _RIBBON_UPGRADES.get(int(upgrade_template_id))
    if info is not None:
        ribbon_template_id, ribbon_level_id = info
        for (uid, level) in _RIBBON_META_LEVEL.get(str(ribbon_template_id), set()):
            if level <= ribbon_level_id:
                unlocked_uids.add(uid)
    pid = await sess.ensure_player()
    if pid is not None:
        if unlocked_uids:
            try:
                await STORE.add_extra_unlock_uids(pid, char_id, unlocked_uids)
                log(f'      AddRibbonLevel: unlocked {len(unlocked_uids)} uid(s) (ribbon={info})')
            except Exception as e:
                log(f'[DB] add_extra_unlock_uids failed: {e}')
        if info is not None:
            # 持久化勋带经验到角色（人物级；勋带单源 = total_xp，等级由阈值推导。
            # ResponseReadCharacterStats attributes 按角色显示 XP/等级/进度）
            _old_xps = {}
            try:
                min_xp = _RIBBON_LEVEL_XP.get(ribbon_level_id, 0)
                # 写入前快照勋带 XP（floor 前后等级差 = 新解锁区间）
                _old_xps = await STORE.get_ribbon_xp(pid, char_id)
                await STORE.set_ribbon_xp_floor(pid, char_id,
                                                ribbon_template_id, min_xp)
                log(f'      AddRibbonLevel: char {char_id} ribbon {ribbon_template_id} '
                    f'totalXp -> max(prev, {min_xp}) (level {ribbon_level_id})')
            except Exception as e:
                log(f'[DB] set_ribbon_xp_floor failed: {e}')

    resp_proto = pb_int32(1, 1)  # result = 1 (ok)
    resp = build_response(cmd_id, 'AddRibbonLevelResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} AddRibbonLevelResponse (result=1 ok, +{len(unlocked_uids)} uids)')

    # 推送 player 刷新货币显示（购买扣款后）
    await _push_player_refresh(sess, writer)

    # 解锁通知（UnlockNotification 行）：购买勋带等级的提升区间内每个新
    # 解锁 uid 一行——客户端弹解锁窗 + 士兵选择面包屑角标；已读回执后
    # misc.py 推 delete 行清表。重复购买低等级（等级未涨）不推
    if info is not None and pid is not None:
        _old = int(_old_xps.get(int(ribbon_template_id), 0) or 0)
        _n = await push_ribbon_unlock_notifications(
            writer, sess.playerview_cmd, pid, char_id, ribbon_template_id,
            _ribbon_level_from_xp(_old), int(ribbon_level_id))
        if _n:
            log(f'      AddRibbonLevel: {_n} unlock notification(s) pushed '
                f'(ribbon={ribbon_template_id} lv={ribbon_level_id})')

    # 重新推送目标角色（unlockBits 更新 + statUpdateTimeStamp 变化 → 客户端
    # CharacterModel.checkStatsWasUpdated 检测到 stats 过期 → 重新请求
    # RequestReadCharacterStats → attributes 刷新，进度条/按钮/图标立即更新）
    if sess.soldier_view_cmd is not None:
        try:
            pid_c = await sess.ensure_player()
            if pid_c is not None:
                new_ts = int(time.time() * 1000)
                for ch in await STORE.list_characters(pid_c):
                    if int(ch['id']) != int(char_id):
                        continue
                    bits = await _char_unlock_bits_for(sess, ch['template'], ch['faction'], ch['id'])
                    # 尾部字段传全：客户端 setObject 整体替换 schema，漏传
                    # head_id/previous_template 等会被抹回默认值（外观回退）
                    await _push_kvcs(writer, sess.soldier_view_cmd, b'character',
                                     _msg_character(ch['name'], ch['faction'], ch['template'],
                                                    _rank_from_combat_xp(ch.get('combat_xp', 0)),
                                                    ch['id'], bits,
                                                    ch.get('combat_badge_0', 0),
                                                    ch.get('combat_badge_1', 0),
                                                    new_ts,
                                                    ch.get('uniform_variant_id', 0),
                                                    ch.get('headgear_id', 0),
                                                    1 if ch.get('is_deleted') else 0,
                                                    ch.get('previous_template', 0),
                                                    ch.get('head_id', 0),
                                                    0,
                                                    1 if ch.get('is_on_leave') else 0))
                    log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet (character '
                        f'{ch["name"]} re-pushed, unlockBits={sum(bits)} true, '
                        f'statTs={new_ts})')
        except Exception as e:
            log(f'[DB] reload character after ribbon level failed: {e}')


HANDLERS = {
    'BuyStoreBundleRequest': handle_buystorebundlerequest,
    'QueryShopWarBondItemsRequest': handle_queryshopwarbonditemsrequest,
    'QueryVoucherPacksRequest': handle_queryvoucherpacksrequest,
    'EpicQueryEntitlementsRequest': handle_epicqueryentitlementsrequest,
    'QueryActiveSurveyRequest': handle_queryactivesurveyrequest,
    'GetGoldPricesRequest': handle_getgoldpricesrequest,
    'AddRibbonLevelRequest': handle_addribbonlevelrequest,
}
