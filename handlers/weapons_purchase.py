# -*- coding: utf-8 -*-
"""武器购买域（weapons_purchase）：从 handlers/weapons.py 拆出（纯移动）。

购买武器变体/改造配件/弹药三个 Create 请求 + 购买后 character
unlockBits 重推；装备/修理/改名等配置族仍在 handlers/weapons.py。"""

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
    _E_WEAPON,
    _MOD_CATEGORY_BY_TEMPLATE,
    _MOD_CATEGORY_SLOT,
    _MOD_MAPPING_IDS,
    _MOD_REQ_LEVEL,
    _REQ_UNLOCK_BY_ITEM,
    _default_ammo_for,
    _mod_usable_by_class,
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
    _character_owned,
    _char_ribbon_level,
    _charge_player,
    _charge_player_multi,
    _refund_player_charge,
    _msg_player_for,
    _unlock_need_for_char,
    _weapon_owner_id,
    _weapon_template_id,
    )

from handlers.characters import _repush_character_row
from starting_gear import STARTING_GEAR


async def grant_weapon_ammo(pid, char_id, weapon_id, ammo_mappings,
                            sess=None, writer=None):
    """武器弹药发放（购买武器默认弹/新兵初始武器/礼包武器弹药共用）。

    ammo_mappings = [(mappingId, ammoTemplateId), ...]：默认弹直接传
    _default_ammo_for(武器模板) 输出（weaponAmmoMapping.defaultAmmo=true），
    礼包嵌套弹药传 XML mapping id 解析结果。首条装备到弹药槽 1（客户端
    getWeaponEquipStatus: equippedAmmoSlotIndex==1 即已装备，否则弹药槽
    显示"未拥有"无法装备/购买其他弹药），其余持有不装备（武器仅一个
    弹药槽，避免多个默认弹药同时占 slot 1）。

    返回 [(mappingId, weapon_ammo行), ...]；传 sess/writer 且
    soldier_view_cmd 有效时逐条 KVCS 推送（须在 weapon 行之后调用，
    客户端按 weapon id 关联）。DB 失败抛异常，由调用方决定记录/中断。"""
    out = []
    for i, (mapping_id, at) in enumerate(ammo_mappings):
        slot = 1 if i == 0 else 0  # 首条装备，其余持有
        a = await STORE.create_weapon_ammo(pid, at, char_id, weapon_id, slot,
                                           _AMMO_MAXCOND.get(int(at), 100.0))
        out.append((mapping_id, a))
        if sess is not None and writer is not None and sess.soldier_view_cmd is not None:
            await _push_kvcs(writer, sess.soldier_view_cmd, b'WeaponAmmo',
                             _msg_weapon_ammo(a['id'], at, char_id, weapon_id, slot,
                                              a.get('condition', 100.0)))
            log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet (WeaponAmmo '
                f'id={a["id"]}, template={at}, mapping={mapping_id}, '
                f'equippedSlot={slot})')
    return out


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
    char_id = 0
    weapon_id = 0
    weapon_template_id = 0
    weapon_name = 'Weapon'
    variant_template_id = 0
    weapon_pricing_id = 0
    variant_pricing_id = 0
    try:
        f = pb_parse(proto)
        char_id = f.get(1, 0)
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

    if not await _character_owned(sess, char_id):
        writer.write(build_response(
            cmd_id, 'CreateWeaponVariantResponse', pb_int32(1, 0)))
        await writer.drain()
        return
    if weapon_id and int(await _weapon_owner_id(sess, weapon_id)) != int(char_id):
        writer.write(build_response(
            cmd_id, 'CreateWeaponVariantResponse', pb_int32(1, 0)))
        await writer.drain()
        return

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

    is_skin_purchase = weapon_id > 0 and weapon_template_id == 0 and variant_template_id > 0
    if ((weapon_template_id and int(weapon_pricing_id or 0) <= 0)
            or (is_skin_purchase and int(variant_pricing_id or 0) <= 0)):
        writer.write(build_response(
            cmd_id, 'CreateWeaponVariantResponse', pb_int32(1, 0)))
        await writer.drain()
        return

    # 扣款（武器本体 + 皮肤变体合并为一次原子扣款：余额不足时整体不扣，
    # 杜绝"武器已扣款、变体不足"的钱货两空）；余额不足返回错误响应，不创建武器
    cost_w, ok_w = await _charge_player_multi(
        sess, [weapon_pricing_id, variant_pricing_id], currency,
        expected=[(weapon_template_id, 'weapontemplate'),
                  (variant_template_id, 'WeaponVariantTemplate')])
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
    if is_skin_purchase:
        # 先持久化再返回成功；失败时退款，禁止生成仅本会话可见的假 id。
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
            await _refund_player_charge(sess, currency, cost_w)
            resp = build_response(
                cmd_id, 'CreateWeaponVariantResponse', pb_int32(1, 0))
            writer.write(resp)
            await writer.drain()
            log(f'S->C cmd={cmd_id:<4} CreateWeaponVariantResponse '
                f'(response=0 persistence_failed)')
            return
        resp = build_response(
            cmd_id, 'CreateWeaponVariantResponse', pb_int32(1, 1))
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} CreateWeaponVariantResponse (response=1 ok, '
            f'skin weaponId={weapon_id}, variantTemplate={variant_template_id})')
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

    # 分配武器实例 ID 并写入数据库；失败时退款并返回失败。
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
        await _refund_player_charge(sess, currency, cost_w)
        resp = build_response(
            cmd_id, 'CreateWeaponVariantResponse', pb_int32(1, 0))
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} CreateWeaponVariantResponse '
            f'(response=0 persistence_failed)')
        return
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

    # 若购买了皮肤变体：创建真实 WeaponVariant 实例行（SetWeaponVariantRequest
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
            try:
                await STORE.delete_weapon(pid, wid)
            except Exception as e:
                log(f'[DB] cleanup weapon {wid} after variant failure failed: {e}')
            sess.weapons.pop(wid, None)
            await _refund_player_charge(sess, currency, cost_w)
            resp = build_response(
                cmd_id, 'CreateWeaponVariantResponse', pb_int32(1, 0))
            writer.write(resp)
            await writer.drain()
            log(f'S->C cmd={cmd_id:<4} CreateWeaponVariantResponse '
                f'(response=0 variant_persistence_failed)')
            return
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

    # 所有必需记录已落库后才向客户端确认成功。
    resp = build_response(
        cmd_id, 'CreateWeaponVariantResponse', pb_int32(1, 1))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} CreateWeaponVariantResponse '
        f'(response=1 ok, weaponId={wid})')

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
    #     公共发放函数 grant_weapon_ammo 与建号/礼包发货共用：
    #     首条装备到槽 1，其余持有（细节见其 docstring）。
    try:
        await grant_weapon_ammo(pid, char_id, wid,
                                _default_ammo_for(weapon_template_id),
                                sess, writer)
    except Exception as e:
        log(f'[DB] grant default weapon ammo failed: {e}')

    # 4. 推送 player 刷新货币显示
    if sess.playerview_cmd is not None:
        player_proto = await _msg_player_for(sess)
        await _push_kvcs(writer, sess.playerview_cmd, b'player', player_proto)

    # 5. 重推 character KVCS：新武器的默认弹药 mapping uid 进入 unlockBits，
    #    否则本会话内弹药勾选框锁死 + "Ribbon required"（重登才恢复）
    await _repush_character_unlocks(sess, writer, char_id)


async def handle_createweaponmodifierrequest(sess, writer, cmd_id, req_type, proto):
    # 武器改造（配件）购买请求
    # CreateWeaponModifierRequest { int64 modifierTemplateId=2; int64 characterId=3;
    #   int64 weaponId=4; int64 pricingId=5; int64 discountId=6; int32 paymentCurrency=7; }
    # CreateWeaponModifierResponse { int32 response = 2; }（★字段号 2，不同于 Variant/Ammo 的 1）
    mod_template_id = 0
    mod_char_id = 0
    mod_weapon_id = 0
    pricing_id = 0
    try:
        f = pb_parse(proto)
        mod_template_id = f.get(2, 0)
        mod_char_id = f.get(3, 0)
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
        # 归属校验（2026-09-13 步兵买侦察镜事故：客户端 UI 卡死/伪造请求拿
        # 侦察 charId 配步兵武器实例的 weaponId 发购买请求——创建即挂接，
        # 绕过 SetWeaponModifier 挂接兵种校验，ZF-39 直落步兵枪上）：
        # 配件只能买给 charId 自己名下的武器实例，weaponId=0 / 别人的枪
        # / 不存在的武器一律拒绝
        wowner = await _weapon_owner_id(sess, mod_weapon_id)
        if int(wowner) != int(mod_char_id):
            resp_proto = pb_int32(2, 0)  # fail（response 字段号=2）
            resp = build_response(cmd_id, 'CreateWeaponModifierResponse', resp_proto)
            writer.write(resp)
            await writer.drain()
            log(f'S->C cmd={cmd_id:<4} CreateWeaponModifierResponse (rejected: '
                f'weapon {mod_weapon_id} owner {wowner} != char {mod_char_id})')
            return
        wtemplate = await _weapon_template_id(sess, mod_weapon_id)
        # 起始瞄具豁免：侦察兵随枪赠送瞄具（STARTING_GEAR.sight_mod，如德军
        # K98 的 ZF-39=107）——unlockBits 授予认 STARTING_GEAR（客户端显示
        # 可购），镜子放仓库转走实例后重新购买同样放行；否则"显示可买却
        # 被 uid 37（K98 Specialist 7 级）勋带门槛拒绝"两套判定自相矛盾
        try:
            ch = await STORE.get_character(pid_v, mod_char_id)
        except Exception:
            ch = None
        # 兵种校验：与 SetWeaponModifier 挂接时点同口径（_mod_usable_by_class），
        # 创建即挂接不能比挂接校验宽松——如 K98 的 ZF-39 仅侦察可用，
        # 步兵给（哪怕自己的）K98 买 ZF-39 也要拒绝
        if ch is not None and wtemplate and not _mod_usable_by_class(
                int(mod_template_id), int(wtemplate), int(ch['faction']),
                int(ch['template'])):
            resp_proto = pb_int32(2, 0)
            resp = build_response(cmd_id, 'CreateWeaponModifierResponse', resp_proto)
            writer.write(resp)
            await writer.drain()
            log(f'S->C cmd={cmd_id:<4} CreateWeaponModifierResponse (rejected: '
                f'mod {mod_template_id} not usable by char {mod_char_id} '
                f'class faction={ch["faction"]} template={ch["template"]})')
            return
        gear = (STARTING_GEAR.get((int(ch['faction']), int(ch['template'])), {})
                if ch else {})
        starting_sight = (int(gear.get('weapon') or 0) == int(wtemplate or 0)
                          and int(gear.get('sight_mod') or 0)
                          == int(mod_template_id or 0))
        if starting_sight:
            log(f'      starting-gear sight {mod_template_id} on weapon tpl '
                f'{wtemplate}: ribbon check waived')
        mapping_id = _MOD_MAPPING_IDS.get((mod_template_id, wtemplate), 0)
        if not mapping_id:
            writer.write(build_response(
                cmd_id, 'CreateWeaponModifierResponse', pb_int32(2, 0)))
            await writer.drain()
            log(f'S->C cmd={cmd_id:<4} CreateWeaponModifierResponse '
                f'(rejected: no modifier mapping)')
            return
        req = _MOD_REQ_LEVEL.get(mapping_id) if mapping_id else None
        if req is not None and not starting_sight:
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
    cost, ok = await _charge_player(
        sess, pricing_id, currency, expected_object_id=mod_template_id,
        expected_types='WeaponModifierTemplate')
    if not ok:
        resp_proto = pb_int32(2, 56)  # not_enough_credits（response 字段号=2）
        resp = build_response(cmd_id, 'CreateWeaponModifierResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} CreateWeaponModifierResponse (response=56 not_enough) cost={cost}')
        return

    # 持久化配件实例；失败时退款并返回失败。
    pid = await sess.ensure_player()
    mid_db = False     # 配件是否真实落库（互斥卸下只对落库路径生效）
    replaced_ids = []  # 被同槽位互斥卸下的旧配件（重推 weaponId=0）
    if pid is not None:
        try:
            mod = await STORE.create_weapon_modifier(pid, mod_template_id,
                                                     mod_char_id, mod_weapon_id)
            mid = mod['id']
            mod_condition = mod.get('condition', 1.0)
            mid_db = True
        except Exception as e:
            log(f'[DB] create_weapon_modifier failed: {e}')
            mid = None
    else:
        mid = None
    if mid is None:
        await _refund_player_charge(sess, currency, cost)
        resp = build_response(
            cmd_id, 'CreateWeaponModifierResponse', pb_int32(2, 0))
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} CreateWeaponModifierResponse '
            f'(response=0 persistence_failed)')
        return

    # 同槽位（分类）互斥（2026-09-13 买新镜子老镜子不卸下事故）：购买是
    # "创建即挂接"（create_weapon_modifier 直接写 weapon_id，不经过
    # SetWeaponModifier），创建后必须卸下同武器同槽位的旧配件——如已装
    # ZF-41 再买 ZF-39，老镜子要取消装备（镜子只能装备一个）；与手动
    # 装备路径（weapons.py handle_setweaponmodifierrequest）同口径，否则
    # 数据库出现同武器同槽位双配件，客户端按槽位互斥渲染与数据不一致。
    # 槽位取 WeaponModifierTemplate.f28（weaponModCategoryId）→
    # WeaponModCategory.f4（slotIndex：4=Sight 等）
    if mid_db and mod_weapon_id:
        try:
            cat = _MOD_CATEGORY_BY_TEMPLATE.get(int(mod_template_id), 0)
            slot = _MOD_CATEGORY_SLOT.get(cat, cat)
            if slot:
                same_slot = [t for t, c in _MOD_CATEGORY_BY_TEMPLATE.items()
                             if _MOD_CATEGORY_SLOT.get(c, c) == slot]
                replaced_ids = [int(m['id']) for m in
                                await STORE.list_weapon_modifiers(pid)
                                if int(m['weapon_id']) == int(mod_weapon_id)
                                and int(m['id']) != int(mid)
                                and int(m['template_id']) in same_slot]
                if replaced_ids:
                    await STORE.clear_weapon_modifier_category(
                        pid, int(mod_weapon_id), same_slot, int(mid))
                    log(f'      CreateWeaponModifier: slot {slot} exclusive, '
                        f'unequipped old mods {replaced_ids}')
        except Exception as e:
            log(f'[DB] purchase slot-exclusive unequip failed: {e}')

    # 1. 返回 CreateWeaponModifierResponse (response=1 ok)，客户端显示购买成功并关闭确认窗
    #    ★ response 字段号=2（客户端 Descriptor 定义 field 2；发 field 1 会被解析成
    #    默认值 -1 ≠ ok，配件页 ModifyWeaponMediator 会在底部弹 feedback 错误）
    resp_proto = pb_int32(2, 1)
    resp = build_response(cmd_id, 'CreateWeaponModifierResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} CreateWeaponModifierResponse (response=1 ok, modifierId={mid})')

    # 2. 通过 SOLDIER_VIEW 推送 WeaponModifier 数据（客户端 WeaponModifierTable
    #    识别已购买，modAlreadyPurchased 不再重复弹确认窗）；被互斥卸下的
    #    旧配件一并重推 weaponId=0，客户端才能及时取消 isEquipped
    if sess.soldier_view_cmd is not None:
        mod_proto = _msg_weapon_modifier(mid, mod_template_id, mod_char_id,
                                         mod_weapon_id, 0, mod_condition)
        await _push_kvcs(writer, sess.soldier_view_cmd, b'WeaponModifier', mod_proto)
        log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet (WeaponModifier id={mid}, '
            f'{len(mod_proto)}B, template={mod_template_id})')
        if replaced_ids and pid is not None:
            try:
                for m in await STORE.list_weapon_modifiers(pid):
                    if int(m['id']) in set(replaced_ids):
                        await _push_kvcs(
                            writer, sess.soldier_view_cmd, b'WeaponModifier',
                            _msg_weapon_modifier(m['id'], m['template_id'],
                                                 m['character_id'],
                                                 m['weapon_id'],
                                                 m.get('auto_repair', 0),
                                                 m.get('condition', 1.0)))
                        log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                            f'(WeaponModifier id={m["id"]} re-pushed, '
                            f'weaponId={m["weapon_id"]})')
            except Exception as e:
                log(f'[DB] reload modifier after purchase failed: {e}')

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
    ammo_char_id = 0
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
        if (not wtemplate or not mapping_id
                or int(await _weapon_owner_id(sess, ammo_weapon_id))
                != int(ammo_char_id)):
            writer.write(build_response(
                cmd_id, 'CreateWeaponAmmoResponse', pb_int32(1, 0)))
            await writer.drain()
            return
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
    cost, ok = await _charge_player(
        sess, pricing_id, currency, expected_object_id=ammo_template_id,
        expected_types='ammotemplate')
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
        await _refund_player_charge(sess, currency, cost)
        resp = build_response(
            cmd_id, 'CreateWeaponAmmoResponse', pb_int32(1, 0))
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} CreateWeaponAmmoResponse '
            f'(response=0 persistence_failed)')
        return

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


HANDLERS = {
    'CreateWeaponVariantRequest': handle_createweaponvariantrequest,
    'CreateWeaponModifierRequest': handle_createweaponmodifierrequest,
    'CreateWeaponAmmoRequest': handle_createweaponammorequest,
}
