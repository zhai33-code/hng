# -*- coding: utf-8 -*-
"""武器域（皮肤、配件、弹药：创建/装备/修复/弹夹选择）。

从 realtest_server.dispatch 机械迁移，函数体保持原样；
统一签名 (sess, writer, cmd_id, req_type, proto)。
"""


from protocol import (
    build_response,
    pb_int32,
    pb_parse,
    )

from state import (
    STORE,
    log,
    )

from wardb import (
    _AMMO_MAPPINGS_BY_WEAPON,
    _AMMO_MAPPING_IDS,
    _AMMO_MAXCOND,
    _AMMO_REQ_LEVEL,
    _CUR_NAME,
    _E_WEAPON,
    _MOD_CATEGORY_BY_TEMPLATE,
    _MOD_CATEGORY_SLOT,
    _MOD_MAPPING_IDS,
    _MOD_REQ_LEVEL,
    _REQ_UNLOCK_BY_ITEM,
    _default_ammo_for,
    weapon_template_clipcount,
    )

from messages import (
    _msg_weapon,
    _msg_weapon_ammo,
    _msg_weapon_modifier,
    _msg_weapon_variant,
    _push_kvcs,
    )

from player_service import (
    _char_bits_for,
    _char_ribbon_level,
    _charge_player,
    _charge_player_multi,
    _msg_player_for,
    _push_player_refresh,
    _unlock_need_for_char,
    _weapon_template_id,
    )

from handlers.characters import _repush_character_row


async def _repush_character_unlocks(sess, writer, char_id):
    """购买武器/配件/弹药后重推 character KVCS（unlockBits 刷新）。

    客户端 EquippableItemRenderer 的 isUnlocked 走 hasCharacterUnlockedItem
    → CharacterSchema.unlockBits（登录时的旧位图）。购买成功后若不重推，
    本会话内已购物品的勾选框锁死并显示 "Ribbon required"，直到重新登录。"""
    pid = await sess.ensure_player()
    if pid is None or sess.soldier_view_cmd is None or not char_id:
        return
    try:
        for ch in await STORE.list_characters(pid):
            if int(ch['id']) != int(char_id):
                continue
            await _repush_character_row(sess, writer, ch)
            log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet (character '
                f'{ch["name"]} re-pushed after purchase, unlockBits refreshed)')
            break
    except Exception as e:
        log(f'[DB] reload character after purchase failed: {e}')


async def handle_createweaponvariantrequest(sess, writer, cmd_id, req_type, proto):
    # 武器商店购买武器请求（武器商店用这个，不是 BuyStoreBundleRequest）
    # CreateWeaponVariantRequest { int64 characterId=1; int64 weaponId=2; int64 weaponTemplateId=3;
    #   string weaponName=4; int64 weaponPricingId=5; int64 weaponVariantTemplateId=6;
    #   int64 weaponDiscountId=7; int64 variantPricingId=8; int64 variantDiscountId=9; int32 paymentCurrency=10; }
    # CreateWeaponVariantResponse { int32 response = 1; }
    char_id = 1
    weapon_id = 0
    weapon_template_id = 0
    weapon_name = 'Weapon'
    variant_template_id = 0
    weapon_pricing_id = 0
    variant_pricing_id = 0
    try:
        f = pb_parse(proto)
        char_id = f.get(1, 1)
        weapon_id = f.get(2, 0)
        weapon_template_id = f.get(3, 0)
        weapon_name = f.get(4, b'')
        if isinstance(weapon_name, bytes):
            weapon_name = weapon_name.decode('utf-8', 'replace')
        variant_template_id = f.get(6, 0)
        weapon_pricing_id = f.get(5, 0)
        variant_pricing_id = f.get(8, 0)
        currency = f.get(10, 0)
        currency_names = {1: 'Credits', 2: 'Warfunds', 3: 'Gold', 4: 'Tickets'}
        log(f'      CreateWeaponVariant: charId={char_id}, weaponId={weapon_id}, '
            f'templateId={weapon_template_id}, name={weapon_name!r}, '
            f'variantTemplateId={variant_template_id}, weaponPricingId={weapon_pricing_id}, '
            f'variantPricingId={variant_pricing_id}, '
            f'currency={currency_names.get(currency, currency)}')
    except Exception as e:
        log(f'      CreateWeaponVariant parse error: {e}')

    # 解锁校验：角色 bits 需已包含需求 uid。
    # - 买新武器判定用 eWeapon(2)（商店 hasCharacterUnlockedItem）
    # - 买武器皮肤变体判定用 eWeaponVariant(14)
    # 用 _REQ_UNLOCK_BY_ITEM 剔除 level=0 专属勋带关联条目（type=14 有 185 条，
    # 与载具 Yak-9 被拒同构，详见 wardb._REQ_UNLOCK_BY_ITEM 注释）
    req2 = _REQ_UNLOCK_BY_ITEM.get((weapon_template_id, _E_WEAPON), set()) if weapon_template_id else set()
    req14 = _REQ_UNLOCK_BY_ITEM.get((variant_template_id, 14), set()) if variant_template_id else set()
    need2 = await _unlock_need_for_char(sess, char_id, req2)
    need14 = await _unlock_need_for_char(sess, char_id, req14)
    if need2 or need14:
        char_uids = await _char_bits_for(sess, char_id)
        # OR 语义（与客户端 UnlockProgressionUtilities.hasCharacterUnlockedItem 一致）：
        # 同一物品多条解锁需求（通用 + 阵营/职业专属）任意一条命中即解锁；
        # 需求为空视为无门槛。原 AND（子集）判定会把"通用条目已解锁但阵营
        # 专属条目未解锁"的合法购买错误拒绝
        ok2 = (not need2) or bool(need2 & char_uids)
        ok14 = (not need14) or bool(need14 & char_uids)
        if not (ok2 and ok14):
            resp_proto = pb_int32(1, 0)  # fail（解锁需求未满足）
            resp = build_response(cmd_id, 'CreateWeaponVariantResponse', resp_proto)
            writer.write(resp)
            await writer.drain()
            log(f'S->C cmd={cmd_id:<4} CreateWeaponVariantResponse (rejected: weapon '
                f'{weapon_template_id} req2={sorted(need2)} variant {variant_template_id} '
                f'req14={sorted(need14)} not unlocked)')
            return

    # 扣款（武器本体 + 皮肤变体合并为一次原子扣款：余额不足时整体不扣，
    # 杜绝"武器已扣款、变体不足"的钱货两空）；余额不足返回错误响应，不创建武器
    cost_w, ok_w = await _charge_player_multi(
        sess, [weapon_pricing_id, variant_pricing_id], currency)
    if not ok_w:
        resp_proto = pb_int32(1, 56)  # not_enough_credits
        resp = build_response(cmd_id, 'CreateWeaponVariantResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} CreateWeaponVariantResponse (response=56 not_enough) '
            f'total={cost_w}')
        return

    # 皮肤购买判定：weaponTemplateId=0 且 weaponId 指向已有武器实例 + variantTemplateId>0
    # （ModifyWeaponMediator.weaponSkinPurchaseRequest 只买皮肤时填这两个字段）。
    # 只创建 WeaponVariant 实例，绝不创建新武器 —— 否则会推送 templateId=0 的假武器，
    # 客户端 WeaponDepotViewMediator 渲染时模板查不到 → Error #1009 崩溃卡住。
    is_skin_purchase = weapon_id > 0 and weapon_template_id == 0 and variant_template_id > 0
    if is_skin_purchase:
        # 1. 返回 CreateWeaponVariantResponse (response=1 ok)
        resp_proto = pb_int32(1, 1)
        resp = build_response(cmd_id, 'CreateWeaponVariantResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} CreateWeaponVariantResponse (response=1 ok, '
            f'skin weaponId={weapon_id}, variantTemplate={variant_template_id})')

        # 2. 创建 WeaponVariant 实例并推送（不自动装备；装备走 SetWeaponVariantRequest）
        spid = await sess.ensure_player()
        variant_id = None
        if spid is not None:
            try:
                vv = await STORE.create_weapon_variant(spid, weapon_id, char_id,
                                                       variant_template_id)
                variant_id = vv['id']
            except Exception as e:
                log(f'[DB] create_weapon_variant failed: {e}')
        if variant_id is None:
            variant_id = getattr(sess, '_fallback_weapon_variant_id', 799) + 1
            sess._fallback_weapon_variant_id = variant_id
        sess.weapon_variants = getattr(sess, 'weapon_variants', {})
        sess.weapon_variants[variant_id] = {
            'weapon_id': weapon_id, 'character_id': char_id,
            'weapon_variant_template_id': variant_template_id,
        }
        if sess.soldier_view_cmd is not None:
            await _push_kvcs(writer, sess.soldier_view_cmd, b'WeaponVariant',
                             _msg_weapon_variant(variant_id, char_id, weapon_id,
                                                 variant_template_id))
            log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                f'(WeaponVariant id={variant_id}, weapon={weapon_id})')

        # 3. 重推 weapon（WeaponSchema UPDATE）：客户端 ModifyWeaponMediator.onWeaponUpdate
        #    收到 UPDATE 才会重置 _startedBuyingItem 并重新 enable 皮肤列表；
        #    只推 WeaponVariant ADD（onWeaponVariantUpdates）不会恢复列表 → 一直卡 loading。
        if sess.soldier_view_cmd is not None:
            w2 = None
            if spid is not None:
                try:
                    w2 = await STORE.get_weapon(spid, int(weapon_id))
                except Exception as e:
                    log(f'[DB] get_weapon failed: {e}')
            if w2 is None:
                w2 = (sess.weapons.get(int(weapon_id))
                      if getattr(sess, 'weapons', None) else None)
            if w2 is not None:
                wproto = _msg_weapon(w2['id'], w2['name'], w2['template_id'],
                                     w2['character_id'], w2['container_id'],
                                     w2['container_slot'], w2['variant_id'],
                                     w2.get('clip_count', 0),
                                     w2.get('auto_repair', 0),
                                     w2.get('condition', 1.0))
                await _push_kvcs(writer, sess.soldier_view_cmd, b'weapon', wproto)
                log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                    f'(weapon id={weapon_id} re-pushed after skin purchase, '
                    f'variantId={w2["variant_id"]})')

        # 4. 推送 player 刷新货币显示
        if sess.playerview_cmd is not None:
            player_proto = await _msg_player_for(sess)
            await _push_kvcs(writer, sess.playerview_cmd, b'player', player_proto)
        return

    # 分配武器实例 ID 并写入数据库（失败时用内存计数器兜底，仅本次会话可见）
    pid = await sess.ensure_player()
    w_cond = None
    if pid is not None:
        try:
            w = await STORE.create_weapon(
                pid, weapon_name, weapon_template_id,
                char_id, variant_template_id)
            wid = w['id']
            w_cond = w.get('condition')
        except Exception as e:
            log(f'[DB] create_weapon failed: {e}')
            wid = None
    else:
        wid = None
    if wid is None:
        wid = getattr(sess, '_fallback_weapon_id', 99) + 1
        sess._fallback_weapon_id = wid
    if w_cond is None:
        # 兜底路径：武器 condition 量纲 0~1（weapontemplate f61 maxCondition=1.0），
        # 推默认 100.0 会算出负损坏比例 → 客户端钳成 1 块钱维修费
        w_cond = 1.0
    sess.weapons = getattr(sess, 'weapons', {})
    sess.weapons[wid] = {
        'name': weapon_name, 'template_id': weapon_template_id,
        'character_id': char_id, 'container_id': 0, 'container_slot': 0,
        'variant_template_id': variant_template_id, 'variant_id': 0,
        'clip_count': 0, 'condition': w_cond,
    }

    # 1. 返回 CreateWeaponVariantResponse (response=1 ok)，客户端显示购买成功弹窗并关闭商店
    resp_proto = pb_int32(1, 1)
    resp = build_response(cmd_id, 'CreateWeaponVariantResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} CreateWeaponVariantResponse (response=1 ok, weaponId={wid})')

    # 2. 若购买了皮肤变体：创建真实 WeaponVariant 实例行（SetWeaponVariantRequest
    #    装备校验查 weapon_variants 表，伪造 id 会导致 "not owned" 拒绝）并推送
    if variant_template_id and sess.soldier_view_cmd is not None:
        variant_id = None
        if pid is not None:
            try:
                vv = await STORE.create_weapon_variant(pid, wid, char_id,
                                                       variant_template_id)
                variant_id = vv['id']
            except Exception as e:
                log(f'[DB] create_weapon_variant failed: {e}')
        if variant_id is None:
            variant_id = wid + 5000  # 无 DB 时内存兜底
            sess._fallback_weapon_variant_id = max(
                getattr(sess, '_fallback_weapon_variant_id', 799), variant_id)
        sess.weapons[wid]['variant_id'] = variant_id
        sess.weapon_variants = getattr(sess, 'weapon_variants', {})
        sess.weapon_variants[variant_id] = {
            'weapon_id': wid, 'character_id': char_id,
            'weapon_variant_template_id': variant_template_id,
        }
        if pid is not None:
            try:
                await STORE.set_weapon_variant(pid, wid, variant_id)
            except Exception as e:
                log(f'[DB] set_weapon_variant failed: {e}')
        await _push_kvcs(writer, sess.soldier_view_cmd, b'WeaponVariant',
                         _msg_weapon_variant(variant_id, char_id, wid, variant_template_id))
        log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet (WeaponVariant id={variant_id})')

    # 3. 通过 SOLDIER_VIEW 推送 weapon 数据（武器才会出现在士兵装备列表）
    if sess.soldier_view_cmd is not None:
        w = sess.weapons[wid]
        wproto = _msg_weapon(wid, w['name'], w['template_id'], w['character_id'],
                             w['container_id'], w['container_slot'], w['variant_id'],
                             w.get('clip_count', 0),
                             w.get('auto_repair', 0),
                             w.get('condition', 1.0))
        await _push_kvcs(writer, sess.soldier_view_cmd, b'weapon', wproto)
        log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet (weapon id={wid}, '
            f'{len(wproto)}B, name={w["name"]!r})')

    # 3.5 购买武器自动赠送默认弹药（weaponAmmoMapping.defaultAmmo=true），
    #     否则客户端弹药槽显示"未拥有"，无法装备/购买其他弹药。
    #     第一个默认弹药直接装备到槽位 1（客户端 getWeaponEquipStatus:
    #     equippedAmmoSlotIndex==1 即已装备），其余默认弹药持有但不装备
    #     （武器只有一个弹药槽，避免多个默认弹药同时占 slot 1）。
    default_ammo = _default_ammo_for(weapon_template_id)
    for i, (mapping_id, ammo_template_id) in enumerate(default_ammo):
        slot_ammo = 1 if i == 0 else 0  # 第一个默认弹药装备，其余持有
        aid = None
        if pid is not None:
            try:
                a = await STORE.create_weapon_ammo(pid, ammo_template_id, char_id, wid,
                                                   slot_ammo,
                                                   _AMMO_MAXCOND.get(int(ammo_template_id), 100.0))
                aid = a['id']
            except Exception as e:
                log(f'[DB] create_weapon_ammo failed: {e}')
        if aid is None:
            aid = getattr(sess, '_fallback_ammo_id', 299) + 1
            sess._fallback_ammo_id = aid
        if sess.soldier_view_cmd is not None:
            aproto = _msg_weapon_ammo(aid, ammo_template_id, char_id, wid, slot_ammo,
                                      _AMMO_MAXCOND.get(int(ammo_template_id), 100.0))
            await _push_kvcs(writer, sess.soldier_view_cmd, b'WeaponAmmo', aproto)
            log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet (WeaponAmmo id={aid}, '
                f'template={ammo_template_id}, mapping={mapping_id}, '
                f'equippedSlot={slot_ammo})')

    # 4. 推送 player 刷新货币显示
    if sess.playerview_cmd is not None:
        player_proto = await _msg_player_for(sess)
        await _push_kvcs(writer, sess.playerview_cmd, b'player', player_proto)

    # 5. 重推 character KVCS：新武器的默认弹药 mapping uid 进入 unlockBits，
    #    否则本会话内弹药勾选框锁死 + "Ribbon required"（重登才恢复）
    await _repush_character_unlocks(sess, writer, char_id)



async def handle_setweaponvariantrequest(sess, writer, cmd_id, req_type, proto):
    # 武器皮肤装备/卸下（ModifyWeaponMediator.onWeaponVariantClick: equip/unEquip）
    # SetWeaponVariantRequest { int64 weaponId=1; int64 variantId=2; }
    # SetWeaponVariantResponse { int32 response = 2; }  ← 响应字段号是 2！
    # variantId 是 WeaponVariant 实例 id（0 = 卸下，恢复默认涂装）
    sw_weapon_id = 0
    sw_variant_id = 0
    try:
        f = pb_parse(proto)
        sw_weapon_id = f.get(1, 0)
        sw_variant_id = f.get(2, 0)
        log(f'      SetWeaponVariant: weaponId={sw_weapon_id}, variantId={sw_variant_id}')
    except Exception as e:
        log(f'      SetWeaponVariant parse error: {e}')

    # 装备校验：非卸下时变体实例必须属于该玩家且指向该武器
    valid = False
    pid = await sess.ensure_player()
    if pid is not None:
        try:
            if int(sw_variant_id) == 0:
                valid = True
            else:
                for vv in await STORE.list_weapon_variants(pid):
                    if (int(vv['id']) == int(sw_variant_id)
                            and int(vv['weapon_id']) == int(sw_weapon_id)):
                        valid = True
                        break
        except Exception as e:
            log(f'[DB] weapon variant ownership check failed: {e}')
    if not valid:
        resp_proto = pb_int32(2, 0)  # fail（皮肤不存在或不属于该武器）
        resp = build_response(cmd_id, 'SetWeaponVariantResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} SetWeaponVariantResponse (rejected: '
            f'variant={sw_variant_id} weapon={sw_weapon_id} not owned)')
        return

    # 持久化到 weapons.variant_id 列（0 = 卸下）
    if pid is not None:
        try:
            await STORE.set_weapon_variant(pid, int(sw_weapon_id), int(sw_variant_id))
        except Exception as e:
            log(f'[DB] set_weapon_variant failed: {e}')
    if getattr(sess, 'weapons', None) and int(sw_weapon_id) in sess.weapons:
        sess.weapons[int(sw_weapon_id)]['variant_id'] = int(sw_variant_id)

    # 响应字段号是 2（SetWeaponVariantResponse.response = 2），response=1 表示 ok
    resp_proto = pb_int32(2, 1)
    resp = build_response(cmd_id, 'SetWeaponVariantResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetWeaponVariantResponse (response=1 ok, '
        f'weaponVariantId={sw_variant_id})')

    # 重推 weapon（equippedWeaponVariantId 字段15 更新 → 客户端刷新皮肤显示）；
    # 优先用 DB 行（会话缓存的 container 值可能已过期，会把已卸下的武器
    # "自动装备"回旧槽位）
    w = None
    if pid is not None:
        try:
            w = await STORE.get_weapon(pid, int(sw_weapon_id))
        except Exception as e:
            log(f'[DB] get_weapon failed: {e}')
    if w is None:
        w = sess.weapons.get(int(sw_weapon_id)) if getattr(sess, 'weapons', None) else None
    if w is not None and sess.soldier_view_cmd is not None:
        wproto = _msg_weapon(int(sw_weapon_id), w['name'], w['template_id'],
                             w['character_id'], w['container_id'], w['container_slot'],
                             int(sw_variant_id), w.get('clip_count', 0),
                             w.get('auto_repair', 0),
                             w.get('condition', 100.0))
        await _push_kvcs(writer, sess.soldier_view_cmd, b'weapon', wproto)
        log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
            f'(weapon id={sw_weapon_id} re-pushed, variantId={sw_variant_id})')



async def handle_setweaponclipcountrequest(sess, writer, cmd_id, req_type, proto):
    # 武器弹夹数调整（ModifyWeaponMediator.ammoModified → WeaponUtilities.getClipDeltaForWeapon）
    # SetWeaponClipCountRequest { int64 equipmentId=1; int32 clipCount=2; }
    # SetWeaponClipCountResponse { enum response=1; }
    # clipCount 三态（T = weapontemplate f55 clipCount 最大弹夹数）：
    #   -(T-1) = 最少弹药（1 弹夹）/ 0 = 默认（T 弹夹）/ +(T-1) = 最大（2T-1 弹夹）
    # 出生弹夹数（WeaponSetting f7）由 wardb.spawn_clips_for 推导。
    swc_weapon_id = 0
    swc_clip_count = 0
    try:
        f = pb_parse(proto)
        swc_weapon_id = f.get(1, 0)
        swc_clip_count = int(f.get(2, 0))
        # 客户端（Flash protobuf）把 int32 负数编码为 32 位补码 varint：
        # -9 -> 4294967287（0xFFFFFFF7）。pb_parse 按无符号解码，这里必须
        # 转回有符号，否则减备弹到最少（-(T-1)）会被当 42 亿越界拒掉
        if 0x80000000 <= swc_clip_count <= 0xFFFFFFFF:
            swc_clip_count -= 0x100000000
        log(f'      SetWeaponClipCount: weaponId={swc_weapon_id}, clipCount={swc_clip_count}')
    except Exception as e:
        log(f'      SetWeaponClipCount parse error: {e}')

    # 校验：武器归属 + clipCount 不超模板三态范围
    swc_w = None
    valid = False
    pid = await sess.ensure_player()
    if pid is not None:
        try:
            swc_w = await STORE.get_weapon(pid, int(swc_weapon_id))
        except Exception as e:
            log(f'[DB] get_weapon failed: {e}')
    if swc_w is not None and getattr(sess, 'weapons', None) \
            and int(swc_weapon_id) not in sess.weapons:
        # DB 命中但会话缓存缺失 → 补进缓存（后续 re-push 用）
        sess.weapons[int(swc_weapon_id)] = dict(swc_w)
    elif swc_w is not None and getattr(sess, 'weapons', None):
        # 缓存已有条目也须用 DB 行刷新 container 字段——缓存里的旧 container
        # 值（卸下/换槽后未同步）会让 re-push 把已卸下的武器"自动装备"回旧槽位
        sess.weapons[int(swc_weapon_id)].update(
            {k: swc_w[k] for k in
             ('container_id', 'container_slot', 'character_id', 'variant_id')})
    if swc_w is not None:
        t_max = weapon_template_clipcount(swc_w['template_id'])
        limit = max(t_max - 1, 0)
        valid = -limit <= swc_clip_count <= limit
        if not valid:
            log(f'      SetWeaponClipCount rejected: clipCount={swc_clip_count} '
                f'out of range [-{limit}, {limit}] (T={t_max})')
    if not valid:
        resp_proto = pb_int32(1, 0)  # fail
        resp = build_response(cmd_id, 'SetWeaponClipCountResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} SetWeaponClipCountResponse '
            f'(rejected: weapon={swc_weapon_id} clipCount={swc_clip_count})')
        return

    # 持久化 + 会话缓存
    if pid is not None:
        try:
            await STORE.set_weapon_clip_count(pid, int(swc_weapon_id), swc_clip_count)
        except Exception as e:
            log(f'[DB] set_weapon_clip_count failed: {e}')
    if getattr(sess, 'weapons', None) and int(swc_weapon_id) in sess.weapons:
        sess.weapons[int(swc_weapon_id)]['clip_count'] = swc_clip_count

    # 响应（response=1 ok；字段号是 1）
    resp_proto = pb_int32(1, 1)
    resp = build_response(cmd_id, 'SetWeaponClipCountResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetWeaponClipCountResponse (response=1 ok, '
        f'weapon={swc_weapon_id} clipCount={swc_clip_count})')

    # 重推 weapon（f14 clipCount 更新 → 客户端弹药袋 UI 刷新）
    w = sess.weapons.get(int(swc_weapon_id)) if getattr(sess, 'weapons', None) else None
    if w is None:
        w = swc_w
    if w is not None and sess.soldier_view_cmd is not None:
        wproto = _msg_weapon(int(swc_weapon_id), w['name'], w['template_id'],
                             w['character_id'], w['container_id'], w['container_slot'],
                             w['variant_id'], swc_clip_count,
                             w.get('auto_repair', 0),
                             w.get('condition', 100.0))
        await _push_kvcs(writer, sess.soldier_view_cmd, b'weapon', wproto)
        log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
            f'(weapon id={swc_weapon_id} re-pushed, clipCount={swc_clip_count})')



async def handle_createweaponmodifierrequest(sess, writer, cmd_id, req_type, proto):
    # 武器改造（配件）购买请求
    # CreateWeaponModifierRequest { int64 modifierTemplateId=2; int64 characterId=3;
    #   int64 weaponId=4; int64 pricingId=5; int64 discountId=6; int32 paymentCurrency=7; }
    # CreateWeaponModifierResponse { int32 response = 2; }（★字段号 2，不同于 Variant/Ammo 的 1）
    mod_template_id = 0
    mod_char_id = 1
    mod_weapon_id = 0
    pricing_id = 0
    try:
        f = pb_parse(proto)
        mod_template_id = f.get(2, 0)
        mod_char_id = f.get(3, 1)
        mod_weapon_id = f.get(4, 0)
        pricing_id = f.get(5, 0)
        currency = f.get(7, 0)
        currency_names = {1: 'Credits', 2: 'Warfunds', 3: 'Gold', 4: 'Tickets'}
        log(f'      CreateWeaponModifier: templateId={mod_template_id}, charId={mod_char_id}, '
            f'weaponId={mod_weapon_id}, pricingId={pricing_id}, '
            f'currency={currency_names.get(currency, currency)}')
    except Exception as e:
        log(f'      CreateWeaponModifier parse error: {e}')

    # 等级校验：角色勋带等级需达到配件解锁需求（防越级购买镜子/扳机等）
    pid_v = await sess.ensure_player()
    mapping_id = 0
    req = None
    if pid_v is not None:
        wtemplate = await _weapon_template_id(sess, mod_weapon_id)
        mapping_id = _MOD_MAPPING_IDS.get((mod_template_id, wtemplate), 0)
        req = _MOD_REQ_LEVEL.get(mapping_id) if mapping_id else None
        if req is not None:
            ribbon_id, need_level = req
            have = await _char_ribbon_level(sess, mod_char_id, ribbon_id)
            if have < need_level:
                # 已拥有时允许重复购买（客户端会弹"已拥有"确认窗），不重复校验
                owned_already = False
                try:
                    for m in await STORE.list_weapon_modifiers(pid_v):
                        if (m['character_id'] == mod_char_id and m['weapon_id'] == mod_weapon_id
                                and m['template_id'] == mod_template_id):
                            owned_already = True
                            break
                except Exception:
                    pass
                if not owned_already:
                    resp_proto = pb_int32(2, 0)  # fail（勋带等级不足；response 字段号=2）
                    resp = build_response(cmd_id, 'CreateWeaponModifierResponse', resp_proto)
                    writer.write(resp)
                    await writer.drain()
                    log(f'S->C cmd={cmd_id:<4} CreateWeaponModifierResponse (rejected: '
                        f'ribbon {ribbon_id} level {have} < required {need_level})')
                    return

    # 扣款；余额不足返回错误响应，不创建配件
    cost, ok = await _charge_player(sess, pricing_id, currency)
    if not ok:
        resp_proto = pb_int32(2, 56)  # not_enough_credits（response 字段号=2）
        resp = build_response(cmd_id, 'CreateWeaponModifierResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} CreateWeaponModifierResponse (response=56 not_enough) cost={cost}')
        return

    # 持久化配件实例（失败时用内存计数器兜底，仅本次会话可见）
    pid = await sess.ensure_player()
    if pid is not None:
        try:
            mod = await STORE.create_weapon_modifier(pid, mod_template_id,
                                                     mod_char_id, mod_weapon_id)
            mid = mod['id']
            mod_condition = mod.get('condition', 1.0)
        except Exception as e:
            log(f'[DB] create_weapon_modifier failed: {e}')
            mid = None
    else:
        mid = None
    if mid is None:
        mid = getattr(sess, '_fallback_modifier_id', 199) + 1
        sess._fallback_modifier_id = mid
        mod_condition = 1.0  # 配件满耐久（WeaponModifierTemplate.maxCondition=1.0）

    # 1. 返回 CreateWeaponModifierResponse (response=1 ok)，客户端显示购买成功并关闭确认窗
    #    ★ response 字段号=2（客户端 Descriptor 定义 field 2；发 field 1 会被解析成
    #    默认值 -1 ≠ ok，配件页 ModifyWeaponMediator 会在底部弹 feedback 错误）
    resp_proto = pb_int32(2, 1)
    resp = build_response(cmd_id, 'CreateWeaponModifierResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} CreateWeaponModifierResponse (response=1 ok, modifierId={mid})')

    # 2. 通过 SOLDIER_VIEW 推送 WeaponModifier 数据（客户端 WeaponModifierTable
    #    识别已购买，modAlreadyPurchased 不再重复弹确认窗）
    if sess.soldier_view_cmd is not None:
        mod_proto = _msg_weapon_modifier(mid, mod_template_id, mod_char_id,
                                         mod_weapon_id, 0, mod_condition)
        await _push_kvcs(writer, sess.soldier_view_cmd, b'WeaponModifier', mod_proto)
        log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet (WeaponModifier id={mid}, '
            f'{len(mod_proto)}B, template={mod_template_id})')

    # 3. 推送 player 刷新货币显示
    if sess.playerview_cmd is not None:
        player_proto = await _msg_player_for(sess)
        await _push_kvcs(writer, sess.playerview_cmd, b'player', player_proto)

    # 4. 重推 character KVCS（配件 mapping uid 进入 unlockBits）
    await _repush_character_unlocks(sess, writer, mod_char_id)



async def handle_createweaponammorequest(sess, writer, cmd_id, req_type, proto):
    # 弹药购买请求（ModifyWeaponMediator 购买弹药 / 购买"必需"弹药）
    # CreateWeaponAmmoRequest { int64 ammoTemplateId=1; int64 weaponId=2;
    #   int64 pricingId=3; int64 discountId=4; int32 paymentCurrency=5; }
    # CreateWeaponAmmoResponse { int32 response = 1; }
    ammo_template_id = 0
    ammo_weapon_id = 0
    pricing_id = 0
    try:
        f = pb_parse(proto)
        ammo_template_id = f.get(1, 0)
        ammo_weapon_id = f.get(2, 0)
        pricing_id = f.get(3, 0)
        currency = f.get(5, 0)
        currency_names = {1: 'Credits', 2: 'Warfunds', 3: 'Gold', 4: 'Tickets'}
        log(f'      CreateWeaponAmmo: ammoTemplateId={ammo_template_id}, weaponId={ammo_weapon_id}, '
            f'pricingId={pricing_id}, currency={currency_names.get(currency, currency)}')
    except Exception as e:
        log(f'      CreateWeaponAmmo parse error: {e}')

    # 弹药归属角色：从会话武器数据取，否则查数据库
    ammo_char_id = 1
    w = getattr(sess, 'weapons', {}).get(ammo_weapon_id)
    if w:
        ammo_char_id = w['character_id']
    else:
        try:
            pid_for_ammo = await sess.ensure_player()
            if pid_for_ammo is not None:
                wrows = await STORE.list_weapons(pid_for_ammo)
                for wrow in wrows:
                    if wrow['id'] == ammo_weapon_id:
                        ammo_char_id = wrow['character_id']
                        break
        except Exception as e:
            log(f'[DB] find weapon for ammo failed: {e}')

    # 等级校验：非默认弹药需角色勋带等级达标（防越级购买高级弹药如 L-41）
    pid_v = await sess.ensure_player()
    if pid_v is not None:
        wtemplate = await _weapon_template_id(sess, ammo_weapon_id)
        mapping_id = _AMMO_MAPPING_IDS.get((ammo_template_id, wtemplate), 0)
        is_default = False
        if mapping_id:
            for (mid, _at, dflt) in _AMMO_MAPPINGS_BY_WEAPON.get(wtemplate, ()):
                if mid == mapping_id:
                    is_default = bool(dflt)
                    break
        req = None if is_default else (_AMMO_REQ_LEVEL.get(mapping_id) if mapping_id else None)
        if req is not None:
            ribbon_id, need_level = req
            have = await _char_ribbon_level(sess, ammo_char_id, ribbon_id)
            if have < need_level:
                resp_proto = pb_int32(1, 0)  # fail（勋带等级不足）
                resp = build_response(cmd_id, 'CreateWeaponAmmoResponse', resp_proto)
                writer.write(resp)
                await writer.drain()
                log(f'S->C cmd={cmd_id:<4} CreateWeaponAmmoResponse (rejected: '
                    f'ribbon {ribbon_id} level {have} < required {need_level})')
                return

    # 扣款；余额不足返回错误响应，不创建弹药
    cost, ok = await _charge_player(sess, pricing_id, currency)
    if not ok:
        resp_proto = pb_int32(1, 56)  # not_enough_credits
        resp = build_response(cmd_id, 'CreateWeaponAmmoResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} CreateWeaponAmmoResponse (response=56 not_enough) cost={cost}')
        return

    pid = await sess.ensure_player()
    aid = None
    if pid is not None:
        try:
            a = await STORE.create_weapon_ammo(pid, ammo_template_id, ammo_char_id, ammo_weapon_id,
                                               0, _AMMO_MAXCOND.get(int(ammo_template_id), 100.0))
            aid = a['id']
        except Exception as e:
            log(f'[DB] create_weapon_ammo failed: {e}')
    if aid is None:
        aid = getattr(sess, '_fallback_ammo_id', 299) + 1
        sess._fallback_ammo_id = aid

    # 1. 返回 CreateWeaponAmmoResponse (response=1 ok)
    resp_proto = pb_int32(1, 1)
    resp = build_response(cmd_id, 'CreateWeaponAmmoResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} CreateWeaponAmmoResponse (response=1 ok, ammoId={aid})')

    # 2. 通过 SOLDIER_VIEW 推送 WeaponAmmo 数据
    if sess.soldier_view_cmd is not None:
        aproto = _msg_weapon_ammo(aid, ammo_template_id, ammo_char_id, ammo_weapon_id, 0,
                                  _AMMO_MAXCOND.get(int(ammo_template_id), 100.0))
        await _push_kvcs(writer, sess.soldier_view_cmd, b'WeaponAmmo', aproto)
        log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet (WeaponAmmo id={aid}, '
            f'template={ammo_template_id})')

    # 3. 推送 player 刷新货币显示
    if sess.playerview_cmd is not None:
        player_proto = await _msg_player_for(sess)
        await _push_kvcs(writer, sess.playerview_cmd, b'player', player_proto)

    # 4. 重推 character KVCS（已购弹药 mapping uid 进入 unlockBits）
    await _repush_character_unlocks(sess, writer, ammo_char_id)



async def handle_setweaponammorequest(sess, writer, cmd_id, req_type, proto):
    # 装备/卸下弹药请求
    # SetWeaponAmmoRequest { int64 weaponId=1; int64 weaponAmmoId=2; int32 slotIndex=3; bool doEquip=4; }
    # SetWeaponAmmoResponse { int32 response = 1; }
    ammo_id = 0
    do_equip = True
    slot_index = 1
    try:
        f = pb_parse(proto)
        ammo_id = f.get(2, 0)
        slot_index = f.get(3, 1)
        do_equip = bool(f.get(4, 1))
        log(f'      SetWeaponAmmo: ammoId={ammo_id}, slotIndex={slot_index}, doEquip={do_equip}')
    except Exception as e:
        log(f'      SetWeaponAmmo parse error: {e}')

    new_slot = slot_index if do_equip else 0
    pid = await sess.ensure_player()
    if pid is not None:
        try:
            # 装备时先清空同武器其他弹药槽（互斥：一枪只装一发弹药），
            # 否则客户端 isEquipped（equippedAmmoSlotIndex==1）会同时显示多发
            if do_equip:
                target = None
                for a in await STORE.list_weapon_ammo(pid):
                    if a['id'] == ammo_id:
                        target = a
                        break
                if target is not None:
                    await STORE.clear_weapon_ammo_slots(pid, target['weapon_id'], ammo_id)
            await STORE.update_weapon_ammo_slot(pid, ammo_id, new_slot)
        except Exception as e:
            log(f'[DB] update_weapon_ammo_slot failed: {e}')

    resp_proto = pb_int32(1, 1)  # response = 1 (ok)
    resp = build_response(cmd_id, 'SetWeaponAmmoResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetWeaponAmmoResponse (response=1 ok, slot={new_slot})')

    # 推送更新后的所有 WeaponAmmo（equippedAmmoSlotIndex 变化，客户端 isEquipped 判定依据）
    if sess.soldier_view_cmd is not None:
        try:
            pid_a = await sess.ensure_player()
            if pid_a is not None:
                for a in await STORE.list_weapon_ammo(pid_a):
                    if a['id'] == ammo_id:
                        aproto = _msg_weapon_ammo(a['id'], a['ammo_template_id'],
                                                  a['character_id'], a['weapon_id'], a['slot_index'],
                                                  a.get('condition', 100.0))
                        await _push_kvcs(writer, sess.soldier_view_cmd, b'WeaponAmmo', aproto)
                        log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                            f'(WeaponAmmo id={a["id"]} re-pushed, slot={a["slot_index"]})')
                    elif do_equip:
                        # 被清槽的其他弹药也重推，客户端才能及时取消 isEquipped
                        aproto = _msg_weapon_ammo(a['id'], a['ammo_template_id'],
                                                  a['character_id'], a['weapon_id'], a['slot_index'],
                                                  a.get('condition', 100.0))
                        await _push_kvcs(writer, sess.soldier_view_cmd, b'WeaponAmmo', aproto)
                        log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                            f'(WeaponAmmo id={a["id"]} slot cleared to 0)')
        except Exception as e:
            log(f'[DB] reload ammo failed: {e}')



async def handle_repairweaponammorequest(sess, writer, cmd_id, req_type, proto):
    # 修复弹药耐久（ModifyWeaponMediator 点击弹药维修按钮）
    # RepairWeaponAmmoRequest { int64 weaponAmmoId=1; int32 repairToCondition=2;
    #   int64 maxConditionRepairPricingId=3; int64 storeDiscountId=4; int32 paymentCurrency=5; }
    # RepairWeaponAmmoResponse { int32 response = 1; }
    ammo_id = 0
    repair_pricing_id = 0
    try:
        f = pb_parse(proto)
        ammo_id = f.get(1, 0)
        repair_pricing_id = f.get(3, 0)
        currency = f.get(5, 0)
        log(f'      RepairWeaponAmmo: ammoId={ammo_id}, pricingId={repair_pricing_id}, '
            f'currency={_CUR_NAME.get(int(currency), currency)}')
    except Exception as e:
        log(f'      RepairWeaponAmmo parse error: {e}')

    # 扣款（维修费）；余额不足返回错误响应
    cost, ok = await _charge_player(sess, repair_pricing_id, currency)
    if not ok:
        resp_proto = pb_int32(1, 56)  # not_enough_credits
        resp = build_response(cmd_id, 'RepairWeaponAmmoResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} RepairWeaponAmmoResponse (response=56 not_enough) cost={cost}')
        return

    # 查弹药模板 → 修复到满耐久
    maxcond = 100.0
    ammo_template_id = 0
    pid = await sess.ensure_player()
    if pid is not None:
        try:
            for a in await STORE.list_weapon_ammo(pid):
                if a['id'] == ammo_id:
                    ammo_template_id = a['ammo_template_id']
                    maxcond = _AMMO_MAXCOND.get(int(ammo_template_id), 100.0)
                    await STORE.update_weapon_ammo_condition(pid, ammo_id, maxcond)
                    break
        except Exception as e:
            log(f'[DB] repair weapon ammo failed: {e}')

    resp_proto = pb_int32(1, 1)  # response = 1 (ok)
    resp = build_response(cmd_id, 'RepairWeaponAmmoResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} RepairWeaponAmmoResponse (response=1 ok, condition={maxcond})')

    # 推送更新后的 WeaponAmmo（condition 恢复满耐久）
    if sess.soldier_view_cmd is not None:
        try:
            pid_a = await sess.ensure_player()
            if pid_a is not None:
                for a in await STORE.list_weapon_ammo(pid_a):
                    if a['id'] == ammo_id:
                        aproto = _msg_weapon_ammo(a['id'], a['ammo_template_id'],
                                                  a['character_id'], a['weapon_id'], a['slot_index'],
                                                  a.get('condition', maxcond),
                                                  a.get('auto_repair', 0))
                        await _push_kvcs(writer, sess.soldier_view_cmd, b'WeaponAmmo', aproto)
                        log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                            f'(WeaponAmmo id={a["id"]} repaired, condition={maxcond})')
                        break
        except Exception as e:
            log(f'[DB] reload ammo after repair failed: {e}')


async def handle_renameweaponrequest(sess, writer, cmd_id, req_type, proto):
    # 武器改名（武器详情"重命名"）
    # RenameWeaponRequest { int64 weaponId=1; string newName=2; int64 pricingId=3;
    #   int64 discountId=4; int32 paymentCurrency=5; }
    # RenameWeaponResponse { int32 result = 1; }
    try:
        f = pb_parse(proto)
        rw_weapon_id = f.get(1, 0)
        rw_new_name = f.get(2, b'')
        if isinstance(rw_new_name, bytes):
            rw_new_name = rw_new_name.decode('utf-8', 'replace')
        rw_pricing_id = f.get(3, 0)
        rw_currency = f.get(5, 0)
        log(f'      RenameWeapon: weaponId={rw_weapon_id}, newName={rw_new_name!r}, '
            f'pricingId={rw_pricing_id}')
    except Exception as e:
        log(f'      RenameWeapon parse error: {e}')
        rw_weapon_id = 0
        rw_new_name = ''
        rw_pricing_id = 0
        rw_currency = 0

    # 扣款（改名费；无 pricing 时免费）
    cost, ok = await _charge_player(sess, rw_pricing_id, rw_currency)
    if not ok:
        resp_proto = pb_int32(1, 56)  # not_enough_credits
        resp = build_response(cmd_id, 'RenameWeaponResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} RenameWeaponResponse (result=56 not_enough) '
            f'cost={cost}')
        return

    pid = await sess.ensure_player()
    updated = False
    if pid is not None and rw_weapon_id:
        try:
            updated = await STORE.rename_weapon(pid, int(rw_weapon_id), rw_new_name)
        except Exception as e:
            log(f'[DB] rename_weapon failed: {e}')

    resp_proto = pb_int32(1, 1 if updated else 0)
    resp = build_response(cmd_id, 'RenameWeaponResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} RenameWeaponResponse (result={1 if updated else 0})')

    # 重推 weapon（name 更新）
    if updated and pid is not None and sess.soldier_view_cmd is not None:
        try:
            w = await STORE.get_weapon(pid, int(rw_weapon_id))
            if w is not None:
                await _push_kvcs(writer, sess.soldier_view_cmd, b'weapon',
                                 _msg_weapon(w['id'], w['name'], w['template_id'],
                                             w['character_id'], w['container_id'],
                                             w['container_slot'], w['variant_id'],
                                             w.get('clip_count', 0),
                                             w.get('auto_repair', 0),
                                             w.get('condition', 100.0)))
                log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                    f'(weapon id={rw_weapon_id} re-pushed after rename)')
        except Exception as e:
            log(f'[DB] reload weapon after rename failed: {e}')
    await _push_player_refresh(sess, writer)


async def handle_repairweaponrequest(sess, writer, cmd_id, req_type, proto):
    # 修复武器耐久
    # RepairWeaponRequest { int64 weaponId=2; int32 repairToCondition=3;
    #   int64 maxConditionRepairPricingId=4; int64 storeDiscountId=5; int32 paymentCurrency=6; }
    # RepairWeaponResponse { int32 response = 2; }  ← 响应字段号是 2！
    try:
        f = pb_parse(proto)
        rp_weapon_id = f.get(2, 0)
        rp_condition = f.get(3, 0)
        rp_pricing_id = f.get(4, 0)
        rp_currency = f.get(6, 0)
        log(f'      RepairWeapon: weaponId={rp_weapon_id}, '
            f'repairToCondition={rp_condition}, pricingId={rp_pricing_id}')
    except Exception as e:
        log(f'      RepairWeapon parse error: {e}')
        rp_weapon_id = 0
        rp_condition = 100
        rp_pricing_id = 0
        rp_currency = 0

    # 扣款（维修费）
    cost, ok = await _charge_player(sess, rp_pricing_id, rp_currency)
    if not ok:
        resp_proto = pb_int32(2, 56)  # not_enough_credits（字段号 2）
        resp = build_response(cmd_id, 'RepairWeaponResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} RepairWeaponResponse (response=56 not_enough) '
            f'cost={cost}')
        return

    pid = await sess.ensure_player()
    if pid is not None and rp_weapon_id:
        try:
            await STORE.update_weapon_condition(pid, int(rp_weapon_id),
                                                float(rp_condition or 100))
        except Exception as e:
            log(f'[DB] update_weapon_condition failed: {e}')

    resp_proto = pb_int32(2, 1)  # response = 1 (ok；字段号 2)
    resp = build_response(cmd_id, 'RepairWeaponResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} RepairWeaponResponse (response=1 ok, '
        f'condition={rp_condition})')

    # 重推 weapon（condition 恢复）
    if pid is not None and sess.soldier_view_cmd is not None:
        try:
            w = await STORE.get_weapon(pid, int(rp_weapon_id))
            if w is not None:
                await _push_kvcs(writer, sess.soldier_view_cmd, b'weapon',
                                 _msg_weapon(w['id'], w['name'], w['template_id'],
                                             w['character_id'], w['container_id'],
                                             w['container_slot'], w['variant_id'],
                                             w.get('clip_count', 0),
                                             w.get('auto_repair', 0),
                                             float(rp_condition or 100)))
                log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                    f'(weapon id={rp_weapon_id} repaired, condition={rp_condition})')
        except Exception as e:
            log(f'[DB] reload weapon after repair failed: {e}')
    await _push_player_refresh(sess, writer)


async def handle_repairweaponmodifierrequest(sess, writer, cmd_id, req_type, proto):
    # 修复武器配件耐久
    # RepairWeaponModifierRequest { int64 weaponModifierId=1; int32 repairToCondition=2;
    #   int64 maxConditionRepairPricingId=3; int64 storeDiscountId=4; int32 paymentCurrency=5; }
    # RepairWeaponModifierResponse { int32 response = 2; }  ← 响应字段号是 2！
    try:
        f = pb_parse(proto)
        rm_modifier_id = f.get(1, 0)
        rm_condition = f.get(2, 0)
        rm_pricing_id = f.get(3, 0)
        rm_currency = f.get(5, 0)
        log(f'      RepairWeaponModifier: modifierId={rm_modifier_id}, '
            f'repairToCondition={rm_condition}, pricingId={rm_pricing_id}')
    except Exception as e:
        log(f'      RepairWeaponModifier parse error: {e}')
        rm_modifier_id = 0
        rm_condition = 100
        rm_pricing_id = 0
        rm_currency = 0

    # 扣款（维修费）
    cost, ok = await _charge_player(sess, rm_pricing_id, rm_currency)
    if not ok:
        resp_proto = pb_int32(2, 56)  # not_enough_credits（字段号 2）
        resp = build_response(cmd_id, 'RepairWeaponModifierResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} RepairWeaponModifierResponse '
            f'(response=56 not_enough) cost={cost}')
        return

    pid = await sess.ensure_player()
    if pid is not None and rm_modifier_id:
        try:
            await STORE.update_weapon_modifier_condition(pid, int(rm_modifier_id),
                                                         float(rm_condition or 1.0))
        except Exception as e:
            log(f'[DB] update_weapon_modifier_condition failed: {e}')

    resp_proto = pb_int32(2, 1)  # response = 1 (ok；字段号 2)
    resp = build_response(cmd_id, 'RepairWeaponModifierResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} RepairWeaponModifierResponse (response=1 ok, '
        f'condition={rm_condition})')

    # 重推 WeaponModifier（condition 恢复）
    if pid is not None and sess.soldier_view_cmd is not None:
        try:
            for m in await STORE.list_weapon_modifiers(pid):
                if int(m['id']) == int(rm_modifier_id):
                    await _push_kvcs(writer, sess.soldier_view_cmd, b'WeaponModifier',
                                     _msg_weapon_modifier(m['id'], m['template_id'],
                                                          m['character_id'],
                                                          m['weapon_id'],
                                                          m.get('auto_repair', 0),
                                                          float(rm_condition or 1.0)))
                    log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                        f'(WeaponModifier id={rm_modifier_id} repaired, '
                        f'condition={rm_condition})')
                    break
        except Exception as e:
            log(f'[DB] reload modifier after repair failed: {e}')
    await _push_player_refresh(sess, writer)


async def handle_setweaponmodifierrequest(sess, writer, cmd_id, req_type, proto):
    # 配件装备/卸下（ModifyWeaponMediator.handleWeaponModifier）
    # SetWeaponModifierRequest { int64 weaponModifierId=1; int64 weaponId=2;
    #   bool doEquip=3; int64 selectedCharacterId=4; }
    # SetWeaponModifierResponse { int32 response = 2; }  ← 响应字段号是 2！
    try:
        f = pb_parse(proto)
        sm_modifier_id = f.get(1, 0)
        sm_weapon_id = f.get(2, 0)
        sm_do_equip = bool(f.get(3, 0))
        sm_char_id = f.get(4, 0)
        log(f'      SetWeaponModifier: modifierId={sm_modifier_id}, '
            f'weaponId={sm_weapon_id}, doEquip={sm_do_equip}, charId={sm_char_id}')
    except Exception as e:
        log(f'      SetWeaponModifier parse error: {e}')
        sm_modifier_id = 0
        sm_weapon_id = 0
        sm_do_equip = True
        sm_char_id = 0

    pid = await sess.ensure_player()
    replaced_ids = []  # 被同槽位互斥卸下的配件（重推 weaponId=0）
    if pid is not None and sm_modifier_id:
        try:
            # 同槽位（分类）互斥：装备前卸下同武器同槽位的其他配件。
            # 槽位取 WeaponModifierTemplate.f28（weaponModCategoryId）→
            # WeaponModCategory.f4（slotIndex：2=Barrel 3=Internal 4=Sight
            # 5=Trigger），客户端 ModifyWeaponMediator 每槽位只装一个，
            # 与弹药 clear_weapon_ammo_slots 互斥同理。
            if sm_do_equip and sm_weapon_id:
                all_mods = await STORE.list_weapon_modifiers(pid)
                target = next((m for m in all_mods
                               if int(m['id']) == int(sm_modifier_id)), None)
                if target is not None:
                    cat = _MOD_CATEGORY_BY_TEMPLATE.get(
                        int(target['template_id']), 0)
                    slot = _MOD_CATEGORY_SLOT.get(cat, cat)
                    if slot:
                        same_slot = [t for t, c
                                     in _MOD_CATEGORY_BY_TEMPLATE.items()
                                     if _MOD_CATEGORY_SLOT.get(c, c) == slot]
                        replaced_ids = [
                            int(m['id']) for m in all_mods
                            if int(m['weapon_id']) == int(sm_weapon_id)
                            and int(m['id']) != int(sm_modifier_id)
                            and int(m['template_id']) in same_slot]
                        if replaced_ids:
                            await STORE.clear_weapon_modifier_category(
                                pid, int(sm_weapon_id), same_slot,
                                int(sm_modifier_id))
                            log(f'      SetWeaponModifier: slot {slot} exclusive, '
                                f'unequipped {replaced_ids}')
            # 装备 → 挂到武器；卸下 → weapon_id 清 0
            await STORE.set_weapon_modifier_weapon(
                pid, int(sm_modifier_id),
                int(sm_weapon_id) if sm_do_equip else 0,
                int(sm_char_id) if sm_do_equip else None)
        except Exception as e:
            log(f'[DB] set_weapon_modifier_weapon failed: {e}')

    resp_proto = pb_int32(2, 1)  # response = 1 (ok；字段号 2)
    resp = build_response(cmd_id, 'SetWeaponModifierResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetWeaponModifierResponse '
        f'(response=1 ok, doEquip={sm_do_equip})')

    # 重推 WeaponModifier（weaponId 变化 → 客户端配件槽刷新；
    # 被互斥卸下的也重推 weaponId=0，客户端才能及时取消 isEquipped）
    if pid is not None and sess.soldier_view_cmd is not None:
        try:
            push_ids = {int(sm_modifier_id)} | set(replaced_ids)
            for m in await STORE.list_weapon_modifiers(pid):
                if int(m['id']) in push_ids:
                    await _push_kvcs(writer, sess.soldier_view_cmd, b'WeaponModifier',
                                     _msg_weapon_modifier(m['id'], m['template_id'],
                                                          m['character_id'],
                                                          m['weapon_id'],
                                                          m.get('auto_repair', 0),
                                                          m.get('condition', 1.0)))
                    log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                        f'(WeaponModifier id={m["id"]} re-pushed, '
                        f'weaponId={m["weapon_id"]})')
        except Exception as e:
            log(f'[DB] reload modifier after equip failed: {e}')


async def handle_setweaponautorepairrequest(sess, writer, cmd_id, req_type, proto):
    # 武器自动维修开关
    # SetWeaponAutoRepairRequest { int64 weaponId=2; int32 autoRepair=12; }  ← 字段号特殊！
    # SetWeaponAutoRepairResponse { int32 response = 2; }  ← 响应字段号是 2！
    try:
        f = pb_parse(proto)
        ar_weapon_id = f.get(2, 0)
        ar_auto = f.get(12, 0)
        log(f'      SetWeaponAutoRepair: weaponId={ar_weapon_id}, autoRepair={ar_auto}')
    except Exception as e:
        log(f'      SetWeaponAutoRepair parse error: {e}')
        ar_weapon_id = 0
        ar_auto = 0

    pid = await sess.ensure_player()
    if pid is not None and ar_weapon_id:
        try:
            await STORE.set_equipment_auto_repair(pid, 1, int(ar_weapon_id),
                                                  int(ar_auto))
        except Exception as e:
            log(f'[DB] set_equipment_auto_repair failed: {e}')

    resp_proto = pb_int32(2, 1)  # response = 1 (ok；字段号 2)
    resp = build_response(cmd_id, 'SetWeaponAutoRepairResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetWeaponAutoRepairResponse (response=1 ok)')

    # 重推 weapon（autoRepair f13 更新；三态原样回传，保持客户端勾选框不跳变）
    if pid is not None and sess.soldier_view_cmd is not None:
        try:
            w = await STORE.get_weapon(pid, int(ar_weapon_id))
            if w is not None:
                await _push_kvcs(writer, sess.soldier_view_cmd, b'weapon',
                                 _msg_weapon(w['id'], w['name'], w['template_id'],
                                             w['character_id'], w['container_id'],
                                             w['container_slot'], w['variant_id'],
                                             w.get('clip_count', 0),
                                             int(ar_auto),
                                             w.get('condition', 100.0)))
                log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                    f'(weapon id={ar_weapon_id} autoRepair={ar_auto})')
        except Exception as e:
            log(f'[DB] reload weapon after auto repair failed: {e}')


async def handle_setweaponmodiferautorepairrequest(sess, writer, cmd_id, req_type, proto):
    # 配件自动维修开关（注意请求名拼写 Modifer 少 i，与客户端一致）
    # SetWeaponModiferAutoRepairRequest { int64 weaponModifierId=1; int32 autoRepair=2; }
    # SetWeaponModiferAutoRepairResponse { int32 response = 1; }
    try:
        f = pb_parse(proto)
        ma_modifier_id = f.get(1, 0)
        ma_auto = f.get(2, 0)
        log(f'      SetWeaponModiferAutoRepair: modifierId={ma_modifier_id}, '
            f'autoRepair={ma_auto}')
    except Exception as e:
        log(f'      SetWeaponModiferAutoRepair parse error: {e}')
        ma_modifier_id = 0
        ma_auto = 0

    pid = await sess.ensure_player()
    if pid is not None and ma_modifier_id:
        try:
            await STORE.set_equipment_auto_repair(pid, 5, int(ma_modifier_id),
                                                  int(ma_auto))
        except Exception as e:
            log(f'[DB] set_equipment_auto_repair failed: {e}')

    resp_proto = pb_int32(1, 1)  # response = 1 (ok)
    resp = build_response(cmd_id, 'SetWeaponModiferAutoRepairResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetWeaponModiferAutoRepairResponse (response=1 ok)')

    # 重推 WeaponModifier（autoRepair f7 更新；三态原样回传）
    if pid is not None and sess.soldier_view_cmd is not None:
        try:
            for m in await STORE.list_weapon_modifiers(pid):
                if int(m['id']) == int(ma_modifier_id):
                    await _push_kvcs(writer, sess.soldier_view_cmd, b'WeaponModifier',
                                     _msg_weapon_modifier(m['id'], m['template_id'],
                                                          m['character_id'],
                                                          m['weapon_id'],
                                                          int(ma_auto),
                                                          m.get('condition', 1.0)))
                    log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                        f'(WeaponModifier id={ma_modifier_id} autoRepair={ma_auto})')
                    break
        except Exception as e:
            log(f'[DB] reload modifier after auto repair failed: {e}')


async def handle_setweaponammaautorepairrequest(sess, writer, cmd_id, req_type, proto):
    # 弹药自动维修开关
    # SetWeaponAmmoAutoRepairRequest { int64 weaponAmmoId=1; int32 autoRepair=2; }
    # SetWeaponAmmoAutoRepairResponse { int32 response = 1; }
    try:
        f = pb_parse(proto)
        aa_ammo_id = f.get(1, 0)
        aa_auto = f.get(2, 0)
        log(f'      SetWeaponAmmoAutoRepair: ammoId={aa_ammo_id}, autoRepair={aa_auto}')
    except Exception as e:
        log(f'      SetWeaponAmmoAutoRepair parse error: {e}')
        aa_ammo_id = 0
        aa_auto = 0

    pid = await sess.ensure_player()
    if pid is not None and aa_ammo_id:
        try:
            await STORE.set_equipment_auto_repair(pid, 6, int(aa_ammo_id),
                                                  int(aa_auto))
        except Exception as e:
            log(f'[DB] set_equipment_auto_repair failed: {e}')

    resp_proto = pb_int32(1, 1)  # response = 1 (ok)
    resp = build_response(cmd_id, 'SetWeaponAmmoAutoRepairResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetWeaponAmmoAutoRepairResponse (response=1 ok)')

    # 重推 WeaponAmmo（autoRepair f8 更新；三态原样回传）
    if pid is not None and sess.soldier_view_cmd is not None:
        try:
            for a in await STORE.list_weapon_ammo(pid):
                if int(a['id']) == int(aa_ammo_id):
                    await _push_kvcs(writer, sess.soldier_view_cmd, b'WeaponAmmo',
                                     _msg_weapon_ammo(a['id'], a['ammo_template_id'],
                                                      a['character_id'], a['weapon_id'],
                                                      a['slot_index'],
                                                      a.get('condition', 100.0),
                                                      int(aa_auto)))
                    log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                        f'(WeaponAmmo id={aa_ammo_id} autoRepair={aa_auto})')
                    break
        except Exception as e:
            log(f'[DB] reload ammo after auto repair failed: {e}')


async def handle_settracereffectrequest(sess, writer, cmd_id, req_type, proto):
    # 武器曳光弹效果设置（ModifyWeaponMediator.setWeaponTracer）
    # SetTracerEffectRequest { bool isFree=1; int64 tracerEffectTemplateId=2;
    #   int64 characterId=3; int64 weaponId=4; int64 pricingId=5; int64 storeDiscountId=6;
    #   int32 paymentCurrency=7; int64 tracerEffectId=8; }
    # SetTracerEffectResponse { int32 response = 1; }
    try:
        f = pb_parse(proto)
        te_is_free = bool(f.get(1, 0))
        te_weapon_id = f.get(4, 0)
        te_pricing_id = f.get(5, 0)
        te_currency = f.get(7, 0)
        te_tracer_id = f.get(8, 0)
        log(f'      SetTracerEffect: weaponId={te_weapon_id}, '
            f'tracerEffectId={te_tracer_id}, pricingId={te_pricing_id}, '
            f'isFree={te_is_free}')
    except Exception as e:
        log(f'      SetTracerEffect parse error: {e}')
        te_is_free = True
        te_weapon_id = 0
        te_pricing_id = 0
        te_currency = 0
        te_tracer_id = 0

    # 扣款（非免费更换时）
    if not te_is_free and te_pricing_id:
        cost, ok = await _charge_player(sess, te_pricing_id, te_currency)
        if not ok:
            resp_proto = pb_int32(1, 56)  # not_enough_credits
            resp = build_response(cmd_id, 'SetTracerEffectResponse', resp_proto)
            writer.write(resp)
            await writer.drain()
            log(f'S->C cmd={cmd_id:<4} SetTracerEffectResponse '
                f'(response=56 not_enough) cost={cost}')
            return

    pid = await sess.ensure_player()
    if pid is not None and te_weapon_id:
        try:
            await STORE.set_weapon_tracer_effect(pid, int(te_weapon_id),
                                                 int(te_tracer_id))
        except Exception as e:
            log(f'[DB] set_weapon_tracer_effect failed: {e}')

    resp_proto = pb_int32(1, 1)  # response = 1 (ok)
    resp = build_response(cmd_id, 'SetTracerEffectResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetTracerEffectResponse (response=1 ok, '
        f'tracerEffectId={te_tracer_id})')
    await _push_player_refresh(sess, writer)


HANDLERS = {
    'CreateWeaponVariantRequest': handle_createweaponvariantrequest,
    'SetWeaponVariantRequest': handle_setweaponvariantrequest,
    'SetWeaponClipCountRequest': handle_setweaponclipcountrequest,
    'CreateWeaponModifierRequest': handle_createweaponmodifierrequest,
    'CreateWeaponAmmoRequest': handle_createweaponammorequest,
    'SetWeaponAmmoRequest': handle_setweaponammorequest,
    'RepairWeaponAmmoRequest': handle_repairweaponammorequest,
    'RenameWeaponRequest': handle_renameweaponrequest,
    'RepairWeaponRequest': handle_repairweaponrequest,
    'RepairWeaponModifierRequest': handle_repairweaponmodifierrequest,
    'SetWeaponModifierRequest': handle_setweaponmodifierrequest,
    'SetWeaponAutoRepairRequest': handle_setweaponautorepairrequest,
    'SetWeaponModiferAutoRepairRequest': handle_setweaponmodiferautorepairrequest,
    'SetWeaponAmmoAutoRepairRequest': handle_setweaponammaautorepairrequest,
    'SetTracerEffectRequest': handle_settracereffectrequest,
}
