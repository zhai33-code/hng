# -*- coding: utf-8 -*-
"""载具购买域（vehicles_purchase）：从 handlers/vehicles.py 拆出（纯移动）。

购买载具变体/升级件/弹药三个 Create 请求；装备/修理等配置族仍在
handlers/vehicles.py。"""

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
    _E_VEHICLE,
    _E_VEHICLES_VARIANT,
    _E_VEHICLE_UPGRADE_MAPPING,
    _REQ_UNLOCK_BY_ITEM,
    _VEHICLE_UPGRADE_MAXCOND,
    _VEHICLE_UPGRADE_SLOT,
    _VEHICLE_VARIANT_PARENT,
    _default_vehicle_ammo_for,
    _vehicle_supports_ammo,
    vehicle_full_condition,
    )

from messages import (
    _msg_vehicle,
    _msg_vehicle_ammo,
    _msg_vehicle_upgrade,
    _msg_vehicle_variant,
    _push_kvcs,
    )

from player_service import (
    _char_bits_for,
    _charge_player,
    _charge_player_multi,
    _msg_player_for,
    _unlock_need_for_char,
    )




async def handle_createvehiclevariantrequest(sess, writer, cmd_id, req_type, proto):
    # 载具商店购买载具请求（VehicleShopMediator / VehicleDepotViewMediator）
    # CreateVehicleVariantRequest { int64 characterId=1; int64 vehicleVariantTemplateId=2;
    #   int64 vehicleVariantPricingId=3; int64 vehicleId=4; int64 extraVehicleVariantTemplateId=5;
    #   int64 extraVehicleVariantPricingId=6; int64 discountId=7; int32 paymentCurrency=8; }
    # CreateVehicleVariantResponse { int32 response = 1; }
    vv_char_id = 1
    vv_vehicle_id = 0
    vv_template_id = 0
    vv_pricing_id = 0
    vv_extra_template_id = 0
    vv_extra_pricing_id = 0
    try:
        f = pb_parse(proto)
        vv_char_id = f.get(1, 1)
        vv_template_id = f.get(2, 0)
        vv_pricing_id = f.get(3, 0)
        vv_vehicle_id = f.get(4, 0)
        vv_extra_template_id = f.get(5, 0)
        vv_extra_pricing_id = f.get(6, 0)
        currency = f.get(8, 0)
        currency_names = {1: 'Credits', 2: 'Warfunds', 3: 'Gold', 4: 'Tickets'}
        log(f'      CreateVehicleVariant: charId={vv_char_id}, '
            f'vehicleId={vv_vehicle_id}, vehicleVariantTemplateId={vv_template_id}, '
            f'vehicleVariantPricingId={vv_pricing_id}, '
            f'extraTemplateId={vv_extra_template_id}, extraPricingId={vv_extra_pricing_id}, '
            f'currency={currency_names.get(currency, currency)}')
    except Exception as e:
        log(f'      CreateVehicleVariant parse error: {e}')

    # 皮肤购买判定：vehicleId 指向已有载具 + vehicleVariantTemplateId=0 + extraTemplate>0
    # （VehicleModificationMediator.onPurchaseClickHandler 给已有载具买皮肤时填这三个字段）。
    # 只创建 VehicleVariant 实例，绝不创建新载具（否则会推送 templateId=0 的假载具）。
    is_skin_purchase = vv_vehicle_id > 0 and vv_template_id == 0 and vv_extra_template_id > 0

    # 解锁校验：买新载具判定用 eVehicle(4) + eVehiclesVariant(15)；
    # 给已有载具买皮肤判定用皮肤模板的 eVehiclesVariant(15)。需求为空视为无门槛。
    # 用 _REQ_UNLOCK_BY_ITEM：level=0 条目是专属勋带关联（如 Yak-9 Specialist），
    # 不是购买门槛，全量映射会误拒（2026-08-26 Yak-9 variant 215 被拒事故）。
    req4 = _REQ_UNLOCK_BY_ITEM.get((vv_template_id, _E_VEHICLE), set())
    req15 = _REQ_UNLOCK_BY_ITEM.get(
        ((vv_extra_template_id if is_skin_purchase else vv_template_id),
         _E_VEHICLES_VARIANT), set())
    need4 = await _unlock_need_for_char(sess, vv_char_id, req4)
    need15 = await _unlock_need_for_char(sess, vv_char_id, req15)
    if need4 or need15:
        char_uids = await _char_bits_for(sess, vv_char_id)
        # OR 语义（与客户端 UnlockProgressionUtilities.hasCharacterUnlockedItem 一致）：
        # 同一物品多条解锁需求（通用 + 阵营/职业专属）任意一条命中即解锁；
        # 需求为空视为无门槛。原 AND（子集）判定会把"通用条目已解锁但阵营
        # 专属条目未解锁"的合法购买错误拒绝
        ok4 = (not need4) or bool(need4 & char_uids)
        ok15 = (not need15) or bool(need15 & char_uids)
        if not (ok4 and ok15):
            resp_proto = pb_int32(1, 0)  # fail（解锁需求未满足）
            resp = build_response(cmd_id, 'CreateVehicleVariantResponse', resp_proto)
            writer.write(resp)
            await writer.drain()
            log(f'S->C cmd={cmd_id:<4} CreateVehicleVariantResponse (rejected: '
                f'vehicle variant {vv_template_id} req4={sorted(need4)} '
                f'req15={sorted(need15)} not unlocked)')
            return

    # 扣款（载具本体 + 可选额外变体合并为一次原子扣款：余额不足时整体不扣，
    # 杜绝"载具已扣款、变体不足"的钱货两空）；余额不足返回错误响应，不创建载具
    cost_v, ok_v = await _charge_player_multi(
        sess, [vv_pricing_id, vv_extra_pricing_id], currency)
    if not ok_v:
        resp_proto = pb_int32(1, 56)  # not_enough_credits
        resp = build_response(cmd_id, 'CreateVehicleVariantResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} CreateVehicleVariantResponse (response=56 not_enough) '
            f'total={cost_v}')
        return

    if is_skin_purchase:
        # 给已有载具购买皮肤：只创建 VehicleVariant 实例，不创建新载具、不送弹药。
        # 1. 返回 CreateVehicleVariantResponse (response=1 ok)
        resp_proto = pb_int32(1, 1)
        resp = build_response(cmd_id, 'CreateVehicleVariantResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} CreateVehicleVariantResponse (response=1 ok, '
            f'skin vehicleId={vv_vehicle_id}, extraTemplate={vv_extra_template_id})')

        # 2. 创建 VehicleVariant 实例并推送（客户端 onVehicleVariantUpdates ADD
        #    会自动恢复购买按钮并标记已拥有；不自动装备，装备走 SetVehicleVariantRequest）
        spid = await sess.ensure_player()
        variant_id = None
        if spid is not None:
            try:
                vv = await STORE.create_vehicle_variant(spid, vv_vehicle_id, vv_char_id,
                                                        vv_extra_template_id)
                variant_id = vv['id']
            except Exception as e:
                log(f'[DB] create_vehicle_variant failed: {e}')
        if variant_id is None:
            variant_id = getattr(sess, '_fallback_vehicle_variant_id', 999) + 1
            sess._fallback_vehicle_variant_id = variant_id
        if sess.soldier_view_cmd is not None:
            await _push_kvcs(writer, sess.soldier_view_cmd, b'VehicleVariant',
                             _msg_vehicle_variant(variant_id, vv_char_id, vv_vehicle_id,
                                                  vv_extra_template_id))
            log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                f'(VehicleVariant id={variant_id}, vehicle={vv_vehicle_id})')

        # 3. 重推 vehicle（Vehicle UPDATE → 客户端 onVehicleUpdates 刷新皮肤显示）
        if sess.soldier_view_cmd is not None:
            v2 = None
            if spid is not None:
                try:
                    v2 = await STORE.get_vehicle(spid, int(vv_vehicle_id))
                except Exception as e:
                    log(f'[DB] get_vehicle failed: {e}')
            if v2 is not None:
                await _push_kvcs(writer, sess.soldier_view_cmd, b'Vehicle',
                                 _msg_vehicle(v2['id'], v2['vehicle_variant_template_id'],
                                              v2['character_id'], v2['equipped_in_slot'],
                                              v2['variant_id']))
                log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                    f'(Vehicle id={vv_vehicle_id} re-pushed after skin purchase)')

        # 4. 推送 player 刷新货币显示
        if sess.playerview_cmd is not None:
            player_proto = await _msg_player_for(sess)
            await _push_kvcs(writer, sess.playerview_cmd, b'player', player_proto)
        return

    # 分配载具实例 ID 并写入数据库（失败时用内存计数器兜底，仅本次会话可见）
    pid = await sess.ensure_player()
    v_cond = None
    if pid is not None:
        try:
            v = await STORE.create_vehicle(pid, vv_template_id, vv_char_id)
            vid = v['id']
            v_cond = v.get('condition')
        except Exception as e:
            log(f'[DB] create_vehicle failed: {e}')
            vid = None
    else:
        vid = None
    if vid is None:
        vid = getattr(sess, '_fallback_vehicle_id', 399) + 1
        sess._fallback_vehicle_id = vid
    if v_cond is None:
        # DB 不可用/兜底路径：按模板满耐久下发，避免 100.0 默认值显示异常
        v_cond = vehicle_full_condition(vv_template_id)

    # 1. 返回 CreateVehicleVariantResponse (response=1 ok)，客户端显示购买成功弹窗并关闭商店
    resp_proto = pb_int32(1, 1)
    resp = build_response(cmd_id, 'CreateVehicleVariantResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} CreateVehicleVariantResponse (response=1 ok, vehicleId={vid})')

    # 2. 通过 SOLDIER_VIEW 推送 Vehicle 数据（载具才会出现在士兵装备列表）
    if sess.soldier_view_cmd is not None:
        vproto = _msg_vehicle(vid, vv_template_id, vv_char_id, 0, condition=v_cond)
        await _push_kvcs(writer, sess.soldier_view_cmd, b'Vehicle', vproto)
        log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet (Vehicle id={vid}, '
            f'variantTemplate={vv_template_id}, {len(vproto)}B)')

    # 2.5 购买载具自动赠送默认弹药并装备到对应弹药仓（weaponAmmoMapping.defaultAmmo=true；
    #     客户端 VehicleUpgradeUtilities.updateVehicleAmmoObject 用
    #     equippedVehicleAmmoStorageId != 0 判定已装备，slotNumber 从 1 开始）。
    #     同一 storage 的多个默认弹药顺延装备到后续槽位（storageSlots=1 或 3）。
    parent_vv = _VEHICLE_VARIANT_PARENT.get(int(vv_template_id))
    vv_storage_slots = {}
    if parent_vv is not None:
        try:
            for v in await STORE.list_vehicles(pid):
                if v['id'] == vid:
                    vv_char_id = v['character_id']
                    break
        except Exception as e:
            log(f'[DB] reload vehicle char failed: {e}')
        for (storage_id, ammo_template_id) in _default_vehicle_ammo_for(parent_vv):
            slot_ammo = vv_storage_slots.get(storage_id, 1)
            vv_storage_slots[storage_id] = slot_ammo + 1
            va_id = None
            try:
                va = await STORE.create_vehicle_ammo(pid, ammo_template_id, vid,
                                                     vv_char_id,
                                                     _AMMO_MAXCOND.get(int(ammo_template_id), 100.0))
                va_id = va['id']
            except Exception as e:
                log(f'[DB] create_vehicle_ammo failed: {e}')
            if va_id is None:
                va_id = getattr(sess, '_fallback_vehicle_ammo_id', 599) + 1
                sess._fallback_vehicle_ammo_id = va_id
            # 装备到弹药仓槽位（equipped_storage_id + slot_index，互斥由 STORE 处理）
            try:
                await STORE.set_vehicle_ammo_equipment(pid, va_id, storage_id,
                                                       slot_ammo, True)
            except Exception as e:
                log(f'[DB] equip default vehicle ammo failed: {e}')
            if sess.soldier_view_cmd is not None:
                aproto = _msg_vehicle_ammo(va_id, ammo_template_id, vid, storage_id,
                                           slot_ammo, vv_char_id,
                                           _AMMO_MAXCOND.get(int(ammo_template_id), 100.0))
                await _push_kvcs(writer, sess.soldier_view_cmd, b'VehicleAmmo', aproto)
                log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet (VehicleAmmo '
                    f'id={va_id}, template={ammo_template_id}, storage={storage_id}, '
                    f'slot={slot_ammo})')

    # 3. 推送 player 刷新货币显示
    if sess.playerview_cmd is not None:
        player_proto = await _msg_player_for(sess)
        await _push_kvcs(writer, sess.playerview_cmd, b'player', player_proto)


async def handle_createvehicleupgraderequest(sess, writer, cmd_id, req_type, proto):
    # 载具升级购买（VehicleModificationMediator.onPurchaseClickHandler，弹药箱/补给箱/车载机枪等）
    # CreateVehicleUpgradeRequest { int64 vehicleUpgradeTemplateId=1; int64 vehicleId=2;
    #   int64 pricingId=3; int64 discountId=4; int32 paymentCurrency=5; }
    # CreateVehicleUpgradeResponse { int32 response = 1; }
    vu_template_id = 0
    vu_vehicle_id = 0
    vu_pricing_id = 0
    vu_currency = 0
    try:
        f = pb_parse(proto)
        vu_template_id = f.get(1, 0)
        vu_vehicle_id = f.get(2, 0)
        vu_pricing_id = f.get(3, 0)
        vu_currency = f.get(5, 0)
        log(f'      CreateVehicleUpgrade: upgradeTemplateId={vu_template_id}, '
            f'vehicleId={vu_vehicle_id}, pricingId={vu_pricing_id}, '
            f'currency={_CUR_NAME.get(int(vu_currency), vu_currency)}')
    except Exception as e:
        log(f'      CreateVehicleUpgrade parse error: {e}')

    # 校验载具归属 + 取角色/变体模板 → 查槽位（客户端按 parentVehicleTemplateId +
    # upgradeTemplateId 查 VehicleUpgradeSlot，服务端一致）
    vu_char_id = 0
    slot_id = 0
    pid = await sess.ensure_player()
    if pid is not None:
        try:
            for v in await STORE.list_vehicles(pid):
                if v['id'] == vu_vehicle_id:
                    vu_char_id = v['character_id']
                    parent = _VEHICLE_VARIANT_PARENT.get(int(v['vehicle_variant_template_id']))
                    slot_id = _VEHICLE_UPGRADE_SLOT.get((parent, vu_template_id), 0)
                    break
        except Exception as e:
            log(f'[DB] vehicle upgrade ownership check failed: {e}')
    if vu_char_id == 0 or slot_id == 0:
        resp_proto = pb_int32(1, 0)  # fail（载具不存在或该载具无此升级槽位）
        resp = build_response(cmd_id, 'CreateVehicleUpgradeResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} CreateVehicleUpgradeResponse (rejected: no slot for '
            f'vehicle={vu_vehicle_id} upgrade={vu_template_id})')
        return

    # 解锁校验：客户端用 canCharacterUseItem(slotId,...,eVehicleUpgradeMapping) 判定，
    # 服务端一致——角色 bits 需包含该槽位的需求 uid（按角色阵营/职业过滤后；需求为空视为无门槛）
    # 同样用 _REQ_UNLOCK_BY_ITEM 剔除 level=0 关联条目（type=8 有 185 条）
    req = _REQ_UNLOCK_BY_ITEM.get((slot_id, _E_VEHICLE_UPGRADE_MAPPING), set())
    need = await _unlock_need_for_char(sess, vu_char_id, req)
    if need:
        char_uids = await _char_bits_for(sess, vu_char_id)
        # OR 语义（与客户端 canCharacterUseItem 一致）：任一需求命中即可
        if not (need & char_uids):
            resp_proto = pb_int32(1, 0)  # fail（解锁需求未满足）
            resp = build_response(cmd_id, 'CreateVehicleUpgradeResponse', resp_proto)
            writer.write(resp)
            await writer.drain()
            log(f'S->C cmd={cmd_id:<4} CreateVehicleUpgradeResponse (rejected: '
                f'slot {slot_id} req {sorted(need)} not unlocked)')
            return

    # 扣款；余额不足返回错误响应，不创建升级
    cost, ok = await _charge_player(sess, vu_pricing_id, vu_currency)
    if not ok:
        resp_proto = pb_int32(1, 56)  # not_enough_credits
        resp = build_response(cmd_id, 'CreateVehicleUpgradeResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} CreateVehicleUpgradeResponse (response=56 not_enough) cost={cost}')
        return

    # 持久化载具升级实例（condition = 模板满耐久；失败时用内存计数器兜底）
    maxcond = _VEHICLE_UPGRADE_MAXCOND.get(int(vu_template_id), 100.0)
    pid2 = await sess.ensure_player()
    if pid2 is not None:
        try:
            u = await STORE.create_vehicle_upgrade(pid2, vu_template_id,
                                                   vu_vehicle_id, vu_char_id, maxcond)
            uid = u['id']
        except Exception as e:
            log(f'[DB] create_vehicle_upgrade failed: {e}')
            uid = None
    else:
        uid = None
    if uid is None:
        uid = getattr(sess, '_fallback_vehicle_upgrade_id', 499) + 1
        sess._fallback_vehicle_upgrade_id = uid

    # 1. 返回 CreateVehicleUpgradeResponse (response=1 ok)，客户端显示购买成功并关闭确认窗
    resp_proto = pb_int32(1, 1)
    resp = build_response(cmd_id, 'CreateVehicleUpgradeResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} CreateVehicleUpgradeResponse (response=1 ok, upgradeId={uid})')

    # 2. 通过 SOLDIER_VIEW 推送 VehicleUpgrade 数据（客户端 VehicleUpgradeTable
    #    识别已购买，未装备升级显示在升级列表，可再装备到槽位）
    if sess.soldier_view_cmd is not None:
        uproto = _msg_vehicle_upgrade(uid, vu_template_id, vu_vehicle_id, 0,
                                      vu_char_id, maxcond)
        await _push_kvcs(writer, sess.soldier_view_cmd, b'VehicleUpgrade', uproto)
        log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet (VehicleUpgrade id={uid}, '
            f'template={vu_template_id}, vehicle={vu_vehicle_id}, condition={maxcond})')

    # 3. 推送 player 刷新货币显示
    if sess.playerview_cmd is not None:
        player_proto = await _msg_player_for(sess)
        await _push_kvcs(writer, sess.playerview_cmd, b'player', player_proto)


async def handle_createvehicleammorequest(sess, writer, cmd_id, req_type, proto):
    # 载具弹药购买（VehicleModificationShopMediator 弹药分类 / ProgressionOverview 购买）
    # CreateVehicleAmmoRequest { int64 ammoTemplateId=1; int64 vehicleId=2;
    #   int64 pricingId=3; int64 discountId=4; int32 paymentCurrency=5; }
    # CreateVehicleAmmoResponse { int32 response = 1; }
    va_template_id = 0
    va_vehicle_id = 0
    va_pricing_id = 0
    va_currency = 0
    try:
        f = pb_parse(proto)
        va_template_id = f.get(1, 0)
        va_vehicle_id = f.get(2, 0)
        va_pricing_id = f.get(3, 0)
        va_currency = f.get(5, 0)
        log(f'      CreateVehicleAmmo: ammoTemplateId={va_template_id}, '
            f'vehicleId={va_vehicle_id}, pricingId={va_pricing_id}, '
            f'currency={_CUR_NAME.get(int(va_currency), va_currency)}')
    except Exception as e:
        log(f'      CreateVehicleAmmo parse error: {e}')

    # 校验载具归属 + 反查角色 + 校验该载具是否支持此弹药（驾驶员主武器经
    # weaponAmmoMapping 推导；客户端 canVehicleSupportAmmunition 同链路）
    va_char_id = 0
    va_ok = False
    pid = await sess.ensure_player()
    if pid is not None:
        try:
            for v in await STORE.list_vehicles(pid):
                if v['id'] == va_vehicle_id:
                    va_char_id = v['character_id']
                    parent = _VEHICLE_VARIANT_PARENT.get(int(v['vehicle_variant_template_id']))
                    va_ok = parent is not None and _vehicle_supports_ammo(parent, va_template_id)
                    break
        except Exception as e:
            log(f'[DB] vehicle ammo ownership check failed: {e}')
    if va_char_id == 0 or not va_ok:
        resp_proto = pb_int32(1, 0)  # fail（载具不存在或该载具不支持此弹药）
        resp = build_response(cmd_id, 'CreateVehicleAmmoResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} CreateVehicleAmmoResponse (rejected: vehicle={va_vehicle_id} '
            f'ammo={va_template_id} not supported)')
        return

    # 扣款；余额不足返回错误响应，不创建弹药
    cost, ok = await _charge_player(sess, va_pricing_id, va_currency)
    if not ok:
        resp_proto = pb_int32(1, 56)  # not_enough_credits
        resp = build_response(cmd_id, 'CreateVehicleAmmoResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} CreateVehicleAmmoResponse (response=56 not_enough) cost={cost}')
        return

    # 持久化载具弹药实例（condition = 模板满耐久；失败时用内存计数器兜底）
    maxcond = _AMMO_MAXCOND.get(int(va_template_id), 100.0)
    pid2 = await sess.ensure_player()
    if pid2 is not None:
        try:
            a = await STORE.create_vehicle_ammo(pid2, va_template_id, va_vehicle_id,
                                                va_char_id, maxcond)
            va_id = a['id']
        except Exception as e:
            log(f'[DB] create_vehicle_ammo failed: {e}')
            va_id = None
    else:
        va_id = None
    if va_id is None:
        va_id = getattr(sess, '_fallback_vehicle_ammo_id', 599) + 1
        sess._fallback_vehicle_ammo_id = va_id

    # 1. 返回 CreateVehicleAmmoResponse (response=1 ok)，客户端显示购买成功
    resp_proto = pb_int32(1, 1)
    resp = build_response(cmd_id, 'CreateVehicleAmmoResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} CreateVehicleAmmoResponse (response=1 ok, ammoId={va_id})')

    # 2. 通过 SOLDIER_VIEW 推送 VehicleAmmo 数据（客户端 VehicleAmmoTable 识别已购买，
    #    VehicleModificationMediator 显示可装填弹药）
    if sess.soldier_view_cmd is not None:
        aproto = _msg_vehicle_ammo(va_id, va_template_id, va_vehicle_id, 0, 0,
                                   va_char_id, maxcond)
        await _push_kvcs(writer, sess.soldier_view_cmd, b'VehicleAmmo', aproto)
        log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet (VehicleAmmo id={va_id}, '
            f'template={va_template_id}, vehicle={va_vehicle_id}, condition={maxcond})')

    # 3. 推送 player 刷新货币显示
    if sess.playerview_cmd is not None:
        player_proto = await _msg_player_for(sess)
        await _push_kvcs(writer, sess.playerview_cmd, b'player', player_proto)


HANDLERS = {
    'CreateVehicleVariantRequest': handle_createvehiclevariantrequest,
    'CreateVehicleUpgradeRequest': handle_createvehicleupgraderequest,
    'CreateVehicleAmmoRequest': handle_createvehicleammorequest,
}
