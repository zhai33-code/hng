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
    _AMMO_MAXCOND,
    _CUR_NAME,
    _MOD_CATEGORY_BY_TEMPLATE,
    _MOD_CATEGORY_SLOT,
    _mod_usable_by_class,
    ammo_wear_params,
    weapon_modifier_wear_params,
    weapon_wear_params,
    weapon_template_clipcount,
    )

from messages import (
    _msg_weapon,
    _msg_weapon_ammo,
    _msg_weapon_modifier,
    _msg_weapon_variant,
    _msg_tracer_effect,
    _push_kvcs,
    )

from player_service import (
    _charge_player,
    _push_player_refresh,
    _refund_player_charge,
    _weapon_owner_id,
    _weapon_template_id,
    )





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

    # 装备校验：非卸下时变体实例必须属于该玩家，且满足其一：
    #   a) 已绑定到该武器（旧购买路径：皮肤随武器发放/单买后绑定）
    #   b) weapon_id=0 的仓库行（官方通行证/奖励发放模型，target='depot'）
    #      且变体模板兼容该武器（客户端 ModifyWeaponMediator 同样按
    #      weaponId==0||weaponId==当前武器 列出可选皮肤）
    valid = False
    depot_row = None
    pid = await sess.ensure_player()
    w = None
    if pid is not None:
        try:
            w = await STORE.get_weapon(pid, int(sw_weapon_id))
        except Exception as e:
            log(f'[DB] get_weapon failed: {e}')
    if w is None:
        w = sess.weapons.get(int(sw_weapon_id)) if getattr(sess, 'weapons', None) else None
    if pid is not None:
        try:
            if int(sw_variant_id) == 0:
                valid = True
            else:
                for vv in await STORE.list_weapon_variants(pid):
                    if int(vv['id']) != int(sw_variant_id):
                        continue
                    if int(vv['weapon_id']) == int(sw_weapon_id):
                        valid = True
                    elif int(vv['weapon_id']) == 0 and w is not None:
                        # 仓库行：校验变体模板与武器模板兼容
                        from handlers.shop import _WEAPON_VARIANT_META
                        meta = _WEAPON_VARIANT_META.get(
                            int(vv['weapon_variant_template_id']))
                        if meta is not None \
                                and int(meta[1]) == int(w['template_id']):
                            valid = True
                            depot_row = vv
                    break
        except Exception as e:
            log(f'[DB] weapon variant ownership check failed: {e}')
    if not valid:
        resp_proto = pb_int32(2, 0)  # fail（皮肤不存在或不适用于该武器）
        resp = build_response(cmd_id, 'SetWeaponVariantResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} SetWeaponVariantResponse (rejected: '
            f'variant={sw_variant_id} weapon={sw_weapon_id} not owned)')
        return

    # 官方装备语义：仓库皮肤行绑定到该武器（weaponId + characterId 跟随
    # 武器），客户端 isWeaponVariantOwned 要求 characterId==选中角色
    if depot_row is not None and pid is not None:
        try:
            bound = await STORE.bind_weapon_variant(
                pid, int(sw_variant_id), int(sw_weapon_id),
                int((w or {}).get('character_id') or 0))
            if bound is not None and sess.soldier_view_cmd is not None:
                await _push_kvcs(
                    writer, sess.soldier_view_cmd, b'WeaponVariant',
                    _msg_weapon_variant(bound['id'], bound['character_id'],
                                        bound['weapon_id'],
                                        bound['weapon_variant_template_id']))
                log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                    f'(WeaponVariant id={bound["id"]} bound to weapon '
                    f'{sw_weapon_id}, char={bound["character_id"]})')
        except Exception as e:
            log(f'[DB] bind_weapon_variant failed: {e}')

    # 持久化到 weapons.variant_id 列（0 = 卸下）
    persisted = False
    if pid is not None:
        try:
            await STORE.set_weapon_variant(pid, int(sw_weapon_id), int(sw_variant_id))
            persisted = True
        except Exception as e:
            log(f'[DB] set_weapon_variant failed: {e}')
    if not persisted:
        writer.write(build_response(
            cmd_id, 'SetWeaponVariantResponse', pb_int32(2, 0)))
        await writer.drain()
        return
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
    persisted = False
    if pid is not None:
        try:
            await STORE.set_weapon_clip_count(pid, int(swc_weapon_id), swc_clip_count)
            persisted = True
        except Exception as e:
            log(f'[DB] set_weapon_clip_count failed: {e}')
    if not persisted:
        writer.write(build_response(
            cmd_id, 'SetWeaponClipCountResponse', pb_int32(1, 0)))
        await writer.drain()
        return
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
    updated = False
    target_weapon_id = 0      # ★ 用于精准推送：只重推同一把武器上被清槽的弹药
    if pid is not None:
        try:
            # 装备时先清空同武器其他弹药槽（互斥：一枪只装一发弹药），
            # 否则客户端 isEquipped（equippedAmmoSlotIndex==1）会同时显示多发
            target = None
            for a in await STORE.list_weapon_ammo(pid):
                if a['id'] == ammo_id:
                    target = a
                    break
            if do_equip:
                if target is not None:
                    target_weapon_id = int(target['weapon_id'] or 0)
                    await STORE.clear_weapon_ammo_slots(pid, target['weapon_id'], ammo_id)
            if target is not None:
                await STORE.update_weapon_ammo_slot(pid, ammo_id, new_slot)
                updated = True
        except Exception as e:
            log(f'[DB] update_weapon_ammo_slot failed: {e}')

    resp_proto = pb_int32(1, 1 if updated else 0)
    resp = build_response(cmd_id, 'SetWeaponAmmoResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetWeaponAmmoResponse '
        f'(response={1 if updated else 0}, slot={new_slot})')

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
                    elif do_equip and int(a['weapon_id'] or 0) == target_weapon_id:
                        # ★ 2026-10-28 修复：只重推**同一把武器**上被清槽的弹药。
                        #   原来对 list_weapon_ammo 返回的【全部】弹药都推
                        #   "slot cleared to 0"，于是一次装弹会把其他枪的弹药
                        #   在客户端全部标成「未装备」（数据库其实没变），
                        #   界面上表现为别的武器弹药全消失、看起来像共享错乱。
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
    currency = 0
    try:
        f = pb_parse(proto)
        ammo_id = f.get(1, 0)
        repair_pricing_id = f.get(3, 0)
        currency = f.get(5, 0)
        log(f'      RepairWeaponAmmo: ammoId={ammo_id}, pricingId={repair_pricing_id}, '
            f'currency={_CUR_NAME.get(int(currency), currency)}')
    except Exception as e:
        log(f'      RepairWeaponAmmo parse error: {e}')

    # 客户端显示价不是整条 Pricing 的满额价格，而是按实际缺损比例计算：
    #   (maxCondition - condition) / maxCondition
    # 服务端必须用同一比例扣款，否则界面显示 146，实际却可能扣 9481。
    pid = await sess.ensure_player()
    ammo = None
    if pid is not None:
        try:
            ammo = next((a for a in await STORE.list_weapon_ammo(pid)
                         if int(a['id']) == int(ammo_id)), None)
        except Exception as e:
            log(f'[DB] load weapon ammo for repair failed: {e}')
    if ammo is None:
        resp = build_response(
            cmd_id, 'RepairWeaponAmmoResponse', pb_int32(1, 0))
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} RepairWeaponAmmoResponse '
            f'(response=0 ammo_not_found)')
        return

    ammo_template_id = int(ammo['ammo_template_id'])
    params = ammo_wear_params(ammo_template_id)
    maxcond = (params[0] if params and params[0] > 0
               else _AMMO_MAXCOND.get(ammo_template_id, 100.0))
    current = max(0.0, min(float(ammo.get('condition') or 0), maxcond))
    fraction = (maxcond - current) / maxcond if maxcond > 0 else 0.0

    # 扣款（维修费）；余额不足返回错误响应
    cost, ok = await _charge_player(
        sess, repair_pricing_id, currency, fraction=fraction,
        expected_object_id=ammo_template_id,
        expected_types='AmmoMaxConditionRepair')
    if not ok:
        resp_proto = pb_int32(1, 56)  # not_enough_credits
        resp = build_response(cmd_id, 'RepairWeaponAmmoResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} RepairWeaponAmmoResponse (response=56 not_enough) cost={cost}')
        return

    # 修复到模板满耐久（不信任请求中的 repairToCondition）
    try:
        await STORE.update_weapon_ammo_condition(pid, ammo_id, maxcond)
    except Exception as e:
        log(f'[DB] repair weapon ammo failed: {e}')
        await _refund_player_charge(sess, currency, cost)
        writer.write(build_response(
            cmd_id, 'RepairWeaponAmmoResponse', pb_int32(1, 0)))
        await writer.drain()
        return

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
    # 生涯军令 7 eRefillWeaponAmmo（客户端无上报点）+ 推行实时刷新
    from handlers.career import complete_order_and_push
    await complete_order_and_push(sess, writer, pid, 7)


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
    cost, ok = await _charge_player(
        sess, rw_pricing_id, rw_currency, expected_types='weapon_rename')
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
    if not updated:
        await _refund_player_charge(sess, rw_currency, cost)

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

    pid = await sess.ensure_player()
    weapon = None
    if pid is not None:
        try:
            weapon = await STORE.get_weapon(pid, int(rp_weapon_id))
        except Exception as e:
            log(f'[DB] load weapon for repair failed: {e}')
    if weapon is None:
        resp = build_response(
            cmd_id, 'RepairWeaponResponse', pb_int32(2, 0))
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} RepairWeaponResponse '
            f'(response=0 weapon_not_found)')
        return

    params = weapon_wear_params(weapon['template_id'])
    maxcond = params[0] if params and params[0] > 0 else 1.0
    current = max(0.0, min(float(weapon.get('condition') or 0), maxcond))
    fraction = (maxcond - current) / maxcond if maxcond > 0 else 0.0

    # 与客户端 PriceObject.calculateFractionPrice 使用同一损耗比例扣款。
    cost, ok = await _charge_player(
        sess, rp_pricing_id, rp_currency, fraction=fraction,
        expected_object_id=weapon['template_id'],
        expected_types=('WeaponMaxConditionRepair', 'WeaponBrokenRepair'))
    if not ok:
        resp_proto = pb_int32(2, 56)  # not_enough_credits（字段号 2）
        resp = build_response(cmd_id, 'RepairWeaponResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} RepairWeaponResponse (response=56 not_enough) '
            f'cost={cost}')
        return

    try:
        await STORE.update_weapon_condition(pid, int(rp_weapon_id), maxcond)
    except Exception as e:
        log(f'[DB] update_weapon_condition failed: {e}')
        await _refund_player_charge(sess, rp_currency, cost)
        writer.write(build_response(
            cmd_id, 'RepairWeaponResponse', pb_int32(2, 0)))
        await writer.drain()
        return

    resp_proto = pb_int32(2, 1)  # response = 1 (ok；字段号 2)
    resp = build_response(cmd_id, 'RepairWeaponResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} RepairWeaponResponse (response=1 ok, '
        f'condition={maxcond})')

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
                                             maxcond))
                log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                    f'(weapon id={rp_weapon_id} repaired, condition={maxcond})')
        except Exception as e:
            log(f'[DB] reload weapon after repair failed: {e}')
    await _push_player_refresh(sess, writer)
    # 生涯军令 6 eRepairWeapon（客户端无上报点）+ 推行实时刷新
    from handlers.career import complete_order_and_push
    await complete_order_and_push(sess, writer, pid, 6)


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

    pid = await sess.ensure_player()
    modifier = None
    if pid is not None:
        try:
            modifier = next((m for m in await STORE.list_weapon_modifiers(pid)
                             if int(m['id']) == int(rm_modifier_id)), None)
        except Exception as e:
            log(f'[DB] load weapon modifier for repair failed: {e}')
    if modifier is None:
        resp = build_response(
            cmd_id, 'RepairWeaponModifierResponse', pb_int32(2, 0))
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} RepairWeaponModifierResponse '
            f'(response=0 modifier_not_found)')
        return

    params = weapon_modifier_wear_params(modifier['template_id'])
    maxcond = params[0] if params and params[0] > 0 else 1.0
    current = max(0.0, min(float(modifier.get('condition') or 0), maxcond))
    fraction = (maxcond - current) / maxcond if maxcond > 0 else 0.0

    # 与客户端显示价使用相同的损耗比例。
    cost, ok = await _charge_player(
        sess, rm_pricing_id, rm_currency, fraction=fraction,
        expected_object_id=modifier['template_id'],
        expected_types=('WeaponModifierMaxConditionRepair',
                        'WeaponModifierBrokenRepair'))
    if not ok:
        resp_proto = pb_int32(2, 56)  # not_enough_credits（字段号 2）
        resp = build_response(cmd_id, 'RepairWeaponModifierResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} RepairWeaponModifierResponse '
            f'(response=56 not_enough) cost={cost}')
        return

    try:
        await STORE.update_weapon_modifier_condition(
            pid, int(rm_modifier_id), maxcond)
    except Exception as e:
        log(f'[DB] update_weapon_modifier_condition failed: {e}')
        await _refund_player_charge(sess, rm_currency, cost)
        writer.write(build_response(
            cmd_id, 'RepairWeaponModifierResponse', pb_int32(2, 0)))
        await writer.drain()
        return

    resp_proto = pb_int32(2, 1)  # response = 1 (ok；字段号 2)
    resp = build_response(cmd_id, 'RepairWeaponModifierResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} RepairWeaponModifierResponse (response=1 ok, '
        f'condition={maxcond})')

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
                                                          maxcond))
                    log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                        f'(WeaponModifier id={rm_modifier_id} repaired, '
                        f'condition={maxcond})')
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
    equip_denied = False  # 兵种校验拒绝（不挂接，response=0）
    persisted = False
    if pid is not None and sm_modifier_id:
        try:
            # 兵种/归属校验（2026-09-13 配件入库解绑改造：配件不再随武器
            # 容器转移、可单独发回任意士兵，兵种门槛收敛到挂接时点——
            # WeaponModMapping 的 level>0 解锁需求无一条适用于该角色
            # （_mod_usable_by_class，与客户端 getUnlocksRequiredForItem
            # 同口径）则拒绝挂接。如 K98 的 ZF-39 仅侦察可用，步兵名下
            # 的 ZF-39 挂 K98 → response=0。无映射/无需求（即买即用）
            # 配件不限制。
            # 2026-09-13 步兵买侦察镜事故收尾：兵种以「武器归属角色」为
            # 准（配件挂谁的枪就按谁校验），且请求 charId 必须就是武器
            # owner——客户端 UI 卡死/伪造请求会拿侦察 charId 把步兵名下
            # 的 ZF-39 挂到步兵武器上（按请求 charId 校验会误放行）；
            # 武器不存在同样拒绝（防悬空 weapon_id 引用）
            if sm_do_equip and sm_weapon_id:
                wowner = await _weapon_owner_id(sess, int(sm_weapon_id))
                wtemplate = await _weapon_template_id(sess, int(sm_weapon_id))
                if not wtemplate:
                    equip_denied = True
                    log(f'      SetWeaponModifier: weapon {sm_weapon_id} '
                        f'not found, equip denied')
                elif int(wowner) > 0 and int(wowner) != int(sm_char_id):
                    equip_denied = True
                    log(f'      SetWeaponModifier: owner check denied '
                        f'modifier {sm_modifier_id} on weapon {sm_weapon_id} '
                        f'(owner {wowner} != char {sm_char_id})')
                if not equip_denied:
                    owner_id = int(wowner) or int(sm_char_id)
                    faction = template = 0
                    for ch in await STORE.list_characters(pid):
                        if int(ch['id']) == owner_id:
                            faction, template = int(ch['faction']), int(ch['template'])
                            break
                    if faction and wtemplate:
                        target = next(
                            (m for m in await STORE.list_weapon_modifiers(pid)
                             if int(m['id']) == int(sm_modifier_id)), None)
                        if target is not None and not _mod_usable_by_class(
                                target['template_id'], wtemplate, faction, template):
                            equip_denied = True
                            log(f'      SetWeaponModifier: class check denied '
                                f'modifier {sm_modifier_id} on weapon '
                                f'{sm_weapon_id} for char {owner_id}')
            if not equip_denied:
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
                persisted = True
        except Exception as e:
            log(f'[DB] set_weapon_modifier_weapon failed: {e}')

    resp_proto = pb_int32(2, 1 if persisted and not equip_denied else 0)
    resp = build_response(cmd_id, 'SetWeaponModifierResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetWeaponModifierResponse '
        f'(response={1 if persisted and not equip_denied else 0}, '
        f'doEquip={sm_do_equip})')

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

    # 生涯军令 30 eEquipWeaponUpgrade（配件挂接到武器，状态推导）+ 推行刷新
    # （兵种校验拒绝的挂接不算完成）
    if pid is not None and sm_do_equip and not equip_denied:
        from handlers.career import complete_order_and_push
        await complete_order_and_push(sess, writer, pid)


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
    updated = None
    if pid is not None and ar_weapon_id:
        try:
            await STORE.set_equipment_auto_repair(pid, 1, int(ar_weapon_id),
                                                  int(ar_auto))
            updated = await STORE.get_weapon(pid, int(ar_weapon_id))
        except Exception as e:
            log(f'[DB] set_equipment_auto_repair failed: {e}')

    resp_proto = pb_int32(2, 1 if updated is not None else 0)
    resp = build_response(cmd_id, 'SetWeaponAutoRepairResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetWeaponAutoRepairResponse '
        f'(response={1 if updated is not None else 0})')

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
    updated = False
    if pid is not None and ma_modifier_id:
        try:
            await STORE.set_equipment_auto_repair(pid, 5, int(ma_modifier_id),
                                                  int(ma_auto))
            updated = any(int(m['id']) == int(ma_modifier_id)
                          for m in await STORE.list_weapon_modifiers(pid))
        except Exception as e:
            log(f'[DB] set_equipment_auto_repair failed: {e}')

    resp_proto = pb_int32(1, 1 if updated else 0)
    resp = build_response(cmd_id, 'SetWeaponModiferAutoRepairResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetWeaponModiferAutoRepairResponse '
        f'(response={1 if updated else 0})')

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
    updated = False
    if pid is not None and aa_ammo_id:
        try:
            await STORE.set_equipment_auto_repair(pid, 6, int(aa_ammo_id),
                                                  int(aa_auto))
            updated = any(int(a['id']) == int(aa_ammo_id)
                          for a in await STORE.list_weapon_ammo(pid))
        except Exception as e:
            log(f'[DB] set_equipment_auto_repair failed: {e}')

    resp_proto = pb_int32(1, 1 if updated else 0)
    resp = build_response(cmd_id, 'SetWeaponAmmoAutoRepairResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetWeaponAmmoAutoRepairResponse '
        f'(response={1 if updated else 0})')

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
        te_tracer_template_id = f.get(2, 0)
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
        te_tracer_template_id = 0
        te_weapon_id = 0
        te_pricing_id = 0
        te_currency = 0
        te_tracer_id = 0

    # 扣款（非免费更换时）
    cost = 0
    if not te_is_free and te_pricing_id:
        cost, ok = await _charge_player(
            sess, te_pricing_id, te_currency,
            expected_object_id=te_tracer_template_id,
            expected_types='TracerEffectTemplate')
        if not ok:
            resp_proto = pb_int32(1, 56)  # not_enough_credits
            resp = build_response(cmd_id, 'SetTracerEffectResponse', resp_proto)
            writer.write(resp)
            await writer.drain()
            log(f'S->C cmd={cmd_id:<4} SetTracerEffectResponse '
                f'(response=56 not_enough) cost={cost}')
            return

    pid = await sess.ensure_player()
    persisted = False
    if pid is not None and te_weapon_id:
        try:
            # character_id 跟随武器（行模型：characterId=0 为仓库在库，
            # 装备后绑定 weaponId 并置 isEquipped）
            tw = await STORE.get_weapon(pid, int(te_weapon_id))
            affected = await STORE.set_weapon_tracer_effect(
                pid, int(te_weapon_id), int(te_tracer_id),
                character_id=int((tw or {}).get('character_id') or 0))
            if affected is None:
                log(f'[DB] set_weapon_tracer_effect: tracer row '
                    f'{te_tracer_id} not owned, ignored')
            else:
                persisted = True
            if affected is not None and sess.soldier_view_cmd is not None:
                for t in affected:
                    await _push_kvcs(
                        writer, sess.soldier_view_cmd, b'TracerEffect',
                        _msg_tracer_effect(t['id'], t['tracer_template_id'],
                                           t['character_id'], t['weapon_id'],
                                           t['is_equipped']))
                log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                    f'(TracerEffect rows re-pushed: {len(affected)}, '
                    f'weapon={te_weapon_id}, tracerId={te_tracer_id})')
        except Exception as e:
            log(f'[DB] set_weapon_tracer_effect failed: {e}')

    if not persisted:
        await _refund_player_charge(sess, te_currency, cost)
        writer.write(build_response(
            cmd_id, 'SetTracerEffectResponse', pb_int32(1, 0)))
        await writer.drain()
        return

    resp_proto = pb_int32(1, 1)  # response = 1 (ok)
    resp = build_response(cmd_id, 'SetTracerEffectResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetTracerEffectResponse (response=1 ok, '
        f'tracerEffectId={te_tracer_id})')
    await _push_player_refresh(sess, writer)


HANDLERS = {
    'SetWeaponVariantRequest': handle_setweaponvariantrequest,
    'SetWeaponClipCountRequest': handle_setweaponclipcountrequest,
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
