# -*- coding: utf-8 -*-
"""载具域（皮肤、升级、弹药：创建/装备/修复）。

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
    _VEHICLE_UPGRADE_MAXCOND,
    _VEHICLE_VARIANT_PARENT,
    ammo_wear_params,
    vehicle_full_condition,
    vehicle_upgrade_wear_params,
    )

from messages import (
    _msg_vehicle,
    _msg_vehicle_ammo,
    _msg_vehicle_upgrade,
    _msg_vehicle_variant,
    _push_kvcs,
    )

from player_service import (
    _charge_player,
    _push_player_refresh,
    _refund_player_charge,
    )




async def handle_setvehiclevariantrequest(sess, writer, cmd_id, req_type, proto):
    # 载具皮肤装备/卸下（VehicleModificationMediator.onVehicleVariantClick equip/unEquip）
    # SetVehicleVariantRequest { int64 vehicleId=1; int64 variantId=2; }
    # SetVehicleVariantResponse { int32 response = 2; }  ← 响应字段号是 2！
    # variantId 是 VehicleVariant 实例 id（0 = 卸下，恢复默认涂装）
    sv_vehicle_id = 0
    sv_variant_id = 0
    try:
        f = pb_parse(proto)
        sv_vehicle_id = f.get(1, 0)
        sv_variant_id = f.get(2, 0)
        log(f'      SetVehicleVariant: vehicleId={sv_vehicle_id}, variantId={sv_variant_id}')
    except Exception as e:
        log(f'      SetVehicleVariant parse error: {e}')

    # 装备校验：非卸下时皮肤实例必须属于该玩家，且满足其一：
    #   a) 已绑定到该载具（购买路径：皮肤随载具/单买后绑定）
    #   b) vehicle_id=0 的仓库/角色行（通行证/军令奖励发放模型，
    #      client VehicleDepotViewMediator SkinMod 页签 + 指派到角色），
    #      且皮肤模板与该载具同 parent 载具模板（客户端 getVehicleVariantId
    #      对 vehicleless 行同样按 parent 匹配）
    valid = False
    depot_row = None
    pid = await sess.ensure_player()
    v = None
    if pid is not None:
        try:
            v = await STORE.get_vehicle(pid, int(sv_vehicle_id))
        except Exception as e:
            log(f'[DB] get_vehicle failed: {e}')
    if pid is not None:
        try:
            if int(sv_variant_id) == 0:
                valid = True
            else:
                for vv in await STORE.list_vehicle_variants(pid):
                    if int(vv['id']) != int(sv_variant_id):
                        continue
                    if int(vv['vehicle_id']) == int(sv_vehicle_id):
                        valid = True
                    elif int(vv['vehicle_id']) == 0 and v is not None:
                        # 仓库/角色行：校验皮肤模板与载具同 parent
                        skin_parent = _VEHICLE_VARIANT_PARENT.get(
                            int(vv['vehicle_variant_template_id']))
                        veh_parent = _VEHICLE_VARIANT_PARENT.get(
                            int(v['vehicle_variant_template_id']))
                        if skin_parent is not None and skin_parent == veh_parent:
                            valid = True
                            depot_row = vv
                    break
        except Exception as e:
            log(f'[DB] vehicle variant ownership check failed: {e}')
    if not valid:
        resp_proto = pb_int32(2, 0)  # fail（皮肤不存在或不属于该载具）；response 字段号是 2
        resp = build_response(cmd_id, 'SetVehicleVariantResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} SetVehicleVariantResponse (rejected: '
            f'variant={sv_variant_id} vehicle={sv_vehicle_id} not owned)')
        return

    # 持久化到 vehicles.variant_id 列（0 = 卸下）
    persisted = False
    if pid is not None:
        try:
            await STORE.set_vehicle_variant(pid, int(sv_vehicle_id), int(sv_variant_id))
            persisted = True
        except Exception as e:
            log(f'[DB] set_vehicle_variant failed: {e}')
    if not persisted:
        writer.write(build_response(
            cmd_id, 'SetVehicleVariantResponse', pb_int32(2, 0)))
        await writer.drain()
        return

    # 官方装备语义：仓库/角色皮肤行绑定到该载具（vehicleId + characterId
    # 跟随载具），客户端 isVehicleVariantOwned 要求 characterId==选中角色
    # （与 SetWeaponVariantRequest 的 bind_weapon_variant 同款）
    if depot_row is not None and pid is not None:
        try:
            bound = await STORE.bind_vehicle_variant(
                pid, int(sv_variant_id), int(sv_vehicle_id),
                int((v or {}).get('character_id') or 0))
            if bound is not None and sess.soldier_view_cmd is not None:
                await _push_kvcs(
                    writer, sess.soldier_view_cmd, b'VehicleVariant',
                    _msg_vehicle_variant(bound['id'], bound['character_id'],
                                         bound['vehicle_id'],
                                         bound['vehicle_variant_template_id']))
                log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                    f'(VehicleVariant id={bound["id"]} bound to vehicle '
                    f'{sv_vehicle_id}, char={bound["character_id"]})')
        except Exception as e:
            log(f'[DB] bind_vehicle_variant failed: {e}')

    # 响应字段号是 2（SetVehicleVariantResponse.response = 2），response=1 表示 ok
    resp_proto = pb_int32(2, 1)
    resp = build_response(cmd_id, 'SetVehicleVariantResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetVehicleVariantResponse (response=1 ok, '
        f'vehicleVariantId={sv_variant_id})')

    # 重推 vehicle（equippedVehicleVariantId 字段9 更新 → 客户端 onVehicleUpdates 刷新皮肤显示）
    v2 = None
    if pid is not None:
        try:
            v2 = await STORE.get_vehicle(pid, int(sv_vehicle_id))
        except Exception as e:
            log(f'[DB] get_vehicle failed: {e}')
    if v2 is not None and sess.soldier_view_cmd is not None:
        await _push_kvcs(writer, sess.soldier_view_cmd, b'Vehicle',
                         _msg_vehicle(v2['id'], v2['vehicle_variant_template_id'],
                                      v2['character_id'], v2['equipped_in_slot'],
                                      int(sv_variant_id)))
        log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
            f'(Vehicle id={sv_vehicle_id} re-pushed, variantId={sv_variant_id})')

    # 生涯军令 36 eChangeVehicleAppearance（variant_id!=0 状态推导）+ 推行刷新
    if pid is not None and int(sv_variant_id) != 0:
        from handlers.career import complete_order_and_push
        await complete_order_and_push(sess, writer, pid)



async def handle_setvehicleequipmentrequest(sess, writer, cmd_id, req_type, proto):
    # 装备/卸下载具（VehicleDepotViewMediator.onVehicleItemClick equip/unEquip）
    # SetVehicleEquipmentRequest { int64 characterId=1; int64 vehicleId=2; int32 equipInSlot=3; }
    # SetVehicleEquipmentResponse { int32 msg = 1; }
    ve_char_id = 0
    ve_vehicle_id = 0
    ve_slot = 0
    try:
        f = pb_parse(proto)
        ve_char_id = f.get(1, 0)
        ve_vehicle_id = f.get(2, 0)
        ve_slot = f.get(3, 0)
        log(f'      SetVehicleEquipment: charId={ve_char_id}, vehicleId={ve_vehicle_id}, '
            f'equipInSlot={ve_slot}')
    except Exception as e:
        log(f'      SetVehicleEquipment parse error: {e}')

    affected = []
    pid = await sess.ensure_player()
    if pid is not None:
        try:
            affected = await STORE.set_vehicle_equipment(pid, ve_vehicle_id,
                                                         ve_char_id, ve_slot)
        except Exception as e:
            log(f'[DB] set_vehicle_equipment failed: {e}')

    resp_proto = pb_int32(1, 1 if affected else 0)
    resp = build_response(cmd_id, 'SetVehicleEquipmentResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetVehicleEquipmentResponse '
        f'(msg={1 if affected else 0})')

    # 重推受影响的载具（目标 + 被挤掉槽位的），客户端 VehicleTable 刷新装备状态
    if sess.soldier_view_cmd is not None:
        for v in affected:
            vproto = _msg_vehicle(v['id'], v['vehicle_variant_template_id'],
                                  v['character_id'], v['equipped_in_slot'],
                                  v.get('variant_id', 0),
                                  condition=v['condition'])
            await _push_kvcs(writer, sess.soldier_view_cmd, b'Vehicle', vproto)
            log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet (Vehicle id={v["id"]}, '
                f'charId={v["character_id"]}, slot={v["equipped_in_slot"]})')

    # 生涯军令 24 eEquipVehicle（equipped_in_slot>0 状态推导）+ 推行实时刷新
    if pid is not None and int(ve_slot) > 0:
        from handlers.career import complete_order_and_push
        await complete_order_and_push(sess, writer, pid)





async def handle_setvehicleupgraderequest(sess, writer, cmd_id, req_type, proto):
    # 装备/卸下载具升级（VehicleModificationMediator.handleVehicleUpgradeAction）
    # SetVehicleUpgradeRequest { int64 vehicleId=1; int64 vehicleUpgradeId=2;
    #   int64 vehicleUpgradeSlotId=3; bool doEquip=4; }
    # SetVehicleUpgradeResponse { int32 response = 1; }
    su_vehicle_id = 0
    su_upgrade_id = 0
    su_slot_id = 0
    su_do_equip = True
    try:
        f = pb_parse(proto)
        su_vehicle_id = f.get(1, 0)
        su_upgrade_id = f.get(2, 0)
        su_slot_id = f.get(3, 0)
        su_do_equip = bool(f.get(4, 1))
        log(f'      SetVehicleUpgrade: vehicleId={su_vehicle_id}, upgradeId={su_upgrade_id}, '
            f'slotId={su_slot_id}, doEquip={su_do_equip}')
    except Exception as e:
        log(f'      SetVehicleUpgrade parse error: {e}')

    affected = []
    pid = await sess.ensure_player()
    if pid is not None:
        try:
            # vehicleId 一并传入：账户级库存件（vehicle_id=0，通行证奖励）装备时
            # 需绑定到目标载具，否则战斗配装按 vehicle_id 分组取不到 → 进战斗无外观
            affected = await STORE.set_vehicle_upgrade_equipment(
                pid, su_upgrade_id, su_slot_id, su_do_equip, su_vehicle_id)
        except Exception as e:
            log(f'[DB] set_vehicle_upgrade_equipment failed: {e}')

    resp_proto = pb_int32(1, 1 if affected else 0)
    resp = build_response(cmd_id, 'SetVehicleUpgradeResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetVehicleUpgradeResponse (response=1 ok, '
        f'slot={su_slot_id if su_do_equip else 0})')

    # 重推受影响的载具升级（目标 + 被挤掉槽位的），客户端刷新 isEquipped 状态
    if sess.soldier_view_cmd is not None:
        for u in affected:
            uproto = _msg_vehicle_upgrade(u['id'], u['upgrade_template_id'],
                                          u['vehicle_id'], u['equipped_slot_id'],
                                          u['character_id'], u.get('condition', 100.0))
            await _push_kvcs(writer, sess.soldier_view_cmd, b'VehicleUpgrade', uproto)
            log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                f'(VehicleUpgrade id={u["id"]}, slot={u["equipped_slot_id"]})')

    # 生涯军令 31 eEquipVehicleUpgrade（equipped_slot_id>0 状态推导）+ 推行刷新
    if pid is not None and su_do_equip:
        from handlers.career import complete_order_and_push
        await complete_order_and_push(sess, writer, pid)



async def handle_repairvehicleupgraderequest(sess, writer, cmd_id, req_type, proto):
    # 修复载具升级耐久（VehicleService.repairVehicleUpgrade）
    # RepairVehicleUpgradeRequest { int64 vehicleUpgradeId=1; int32 repairToCondition=2;
    #   int64 maxConditionRepairPricingId=3; int64 storeDiscountId=4; int32 paymentCurrency=5; }
    # RepairVehicleUpgradeResponse { int32 response = 1; }
    ru_upgrade_id = 0
    ru_pricing_id = 0
    ru_currency = 0
    try:
        f = pb_parse(proto)
        ru_upgrade_id = f.get(1, 0)
        ru_pricing_id = f.get(3, 0)
        ru_currency = f.get(5, 0)
        log(f'      RepairVehicleUpgrade: upgradeId={ru_upgrade_id}, pricingId={ru_pricing_id}, '
            f'currency={_CUR_NAME.get(int(ru_currency), ru_currency)}')
    except Exception as e:
        log(f'      RepairVehicleUpgrade parse error: {e}')

    # 先校验归属并以模板满耐久计算实际缺损比例。客户端显示价也是
    # (maxCondition - condition) / maxCondition，服务端不能按整条 Pricing 满额扣款。
    pid = await sess.ensure_player()
    repaired = None
    if pid is not None:
        try:
            repaired = next((u for u in await STORE.list_vehicle_upgrades(pid)
                             if int(u['id']) == int(ru_upgrade_id)), None)
        except Exception as e:
            log(f'[DB] load vehicle upgrade for repair failed: {e}')
    if repaired is None:
        resp = build_response(
            cmd_id, 'RepairVehicleUpgradeResponse', pb_int32(1, 0))
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} RepairVehicleUpgradeResponse '
            f'(response=0 upgrade_not_found)')
        return

    upgrade_template_id = int(repaired['upgrade_template_id'])
    params = vehicle_upgrade_wear_params(upgrade_template_id)
    maxcond = (params[0] if params and params[0] > 0
               else _VEHICLE_UPGRADE_MAXCOND.get(upgrade_template_id, 100.0))
    current = max(0.0, min(float(repaired.get('condition') or 0), maxcond))
    fraction = (maxcond - current) / maxcond if maxcond > 0 else 0.0

    # 扣款（维修费）；余额不足返回错误响应
    cost, ok = await _charge_player(
        sess, ru_pricing_id, ru_currency, fraction=fraction,
        expected_object_id=upgrade_template_id,
        expected_types='VehicleUpgradeMaxConditionRepair')
    if not ok:
        resp_proto = pb_int32(1, 56)  # not_enough_credits
        resp = build_response(cmd_id, 'RepairVehicleUpgradeResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} RepairVehicleUpgradeResponse (response=56 not_enough) cost={cost}')
        return

    # 修复到模板满耐久（不信任请求中的 repairToCondition）
    try:
        await STORE.update_vehicle_upgrade_condition(pid, ru_upgrade_id, maxcond)
        repaired['condition'] = maxcond
    except Exception as e:
        log(f'[DB] repair vehicle upgrade failed: {e}')
        await _refund_player_charge(sess, ru_currency, cost)
        writer.write(build_response(
            cmd_id, 'RepairVehicleUpgradeResponse', pb_int32(1, 0)))
        await writer.drain()
        return

    resp_proto = pb_int32(1, 1)  # response = 1 (ok)
    resp = build_response(cmd_id, 'RepairVehicleUpgradeResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} RepairVehicleUpgradeResponse (response=1 ok, condition={maxcond})')

    # 推送更新后的 VehicleUpgrade（condition 恢复满耐久）
    if sess.soldier_view_cmd is not None and repaired is not None:
        uproto = _msg_vehicle_upgrade(repaired['id'], repaired['upgrade_template_id'],
                                      repaired['vehicle_id'], repaired['equipped_slot_id'],
                                      repaired['character_id'], maxcond)
        await _push_kvcs(writer, sess.soldier_view_cmd, b'VehicleUpgrade', uproto)
        log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
            f'(VehicleUpgrade id={repaired["id"]} repaired, condition={maxcond})')





async def handle_setvehicleammorequest(sess, writer, cmd_id, req_type, proto):
    # 装备/卸下载具弹药（VehicleModificationMediator.handleVehicleAmmoAction）
    # SetVehicleAmmoRequest { int64 vehicleId=1; int64 vehicleAmmoId=2;
    #   int64 vehicleAmmoStorageId=3; int32 slotIndex=4; bool doEquip=5; }
    # SetVehicleAmmoResponse { int32 response = 1; }
    sa_ammo_id = 0
    sa_storage_id = 0
    sa_slot_index = 0
    sa_do_equip = True
    try:
        f = pb_parse(proto)
        sa_ammo_id = f.get(2, 0)
        sa_storage_id = f.get(3, 0)
        sa_slot_index = f.get(4, 0)
        sa_do_equip = bool(f.get(5, 1))
        log(f'      SetVehicleAmmo: vehicleId={f.get(1, 0)}, ammoId={sa_ammo_id}, '
            f'storageId={sa_storage_id}, slotIndex={sa_slot_index}, doEquip={sa_do_equip}')
    except Exception as e:
        log(f'      SetVehicleAmmo parse error: {e}')

    affected = []
    pid = await sess.ensure_player()
    if pid is not None:
        try:
            affected = await STORE.set_vehicle_ammo_equipment(pid, sa_ammo_id,
                                                              sa_storage_id,
                                                              sa_slot_index, sa_do_equip)
        except Exception as e:
            log(f'[DB] set_vehicle_ammo_equipment failed: {e}')

    resp_proto = pb_int32(1, 1)  # response = 1 (ok)
    resp = build_response(cmd_id, 'SetVehicleAmmoResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetVehicleAmmoResponse '
        f'(response={1 if affected else 0}, '
        f'storage={sa_storage_id if sa_do_equip else 0}, slot={sa_slot_index if sa_do_equip else 0})')

    # 重推受影响的载具弹药（目标 + 被挤掉槽位的），客户端刷新 isEquipped 状态
    if sess.soldier_view_cmd is not None:
        for a in affected:
            aproto = _msg_vehicle_ammo(a['id'], a['ammo_template_id'], a['vehicle_id'],
                                       a['equipped_storage_id'], a['slot_index'],
                                       a['character_id'], a.get('condition', 100.0))
            await _push_kvcs(writer, sess.soldier_view_cmd, b'VehicleAmmo', aproto)
            log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                f'(VehicleAmmo id={a["id"]}, storage={a["equipped_storage_id"]}, '
                f'slot={a["slot_index"]})')

    # 生涯军令 32 eEquipVehicleCustomAmmo（equipped_storage_id 状态推导）+ 推行刷新
    if pid is not None and sa_do_equip:
        from handlers.career import complete_order_and_push
        await complete_order_and_push(sess, writer, pid)



async def handle_repairvehicleammorequest(sess, writer, cmd_id, req_type, proto):
    # 修复载具弹药耐久（VehicleService.repairVehicleAmmo）
    # RepairVehicleAmmoRequest { int64 vehicleAmmoId=1; int32 repairToCondition=2;
    #   int64 maxConditionRepairPricingId=3; int64 storeDiscountId=4; int32 paymentCurrency=5; }
    # RepairVehicleAmmoResponse { int32 response = 1; }
    ra_ammo_id = 0
    ra_pricing_id = 0
    ra_currency = 0
    try:
        f = pb_parse(proto)
        ra_ammo_id = f.get(1, 0)
        ra_pricing_id = f.get(3, 0)
        ra_currency = f.get(5, 0)
        log(f'      RepairVehicleAmmo: ammoId={ra_ammo_id}, pricingId={ra_pricing_id}, '
            f'currency={_CUR_NAME.get(int(ra_currency), ra_currency)}')
    except Exception as e:
        log(f'      RepairVehicleAmmo parse error: {e}')

    pid = await sess.ensure_player()
    repaired = None
    if pid is not None:
        try:
            repaired = next((a for a in await STORE.list_vehicle_ammo(pid)
                             if int(a['id']) == int(ra_ammo_id)), None)
        except Exception as e:
            log(f'[DB] load vehicle ammo for repair failed: {e}')
    if repaired is None:
        resp = build_response(
            cmd_id, 'RepairVehicleAmmoResponse', pb_int32(1, 0))
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} RepairVehicleAmmoResponse '
            f'(response=0 ammo_not_found)')
        return

    ammo_template_id = int(repaired['ammo_template_id'])
    params = ammo_wear_params(ammo_template_id)
    maxcond = (params[0] if params and params[0] > 0
               else _AMMO_MAXCOND.get(ammo_template_id, 100.0))
    current = max(0.0, min(float(repaired.get('condition') or 0), maxcond))
    fraction = (maxcond - current) / maxcond if maxcond > 0 else 0.0

    # 扣款（维修费）；余额不足返回错误响应
    cost, ok = await _charge_player(
        sess, ra_pricing_id, ra_currency, fraction=fraction,
        expected_object_id=ammo_template_id,
        expected_types='AmmoMaxConditionRepair')
    if not ok:
        resp_proto = pb_int32(1, 56)  # not_enough_credits
        resp = build_response(cmd_id, 'RepairVehicleAmmoResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} RepairVehicleAmmoResponse (response=56 not_enough) cost={cost}')
        return

    # 修复到模板满耐久（不信任请求中的 repairToCondition）
    try:
        await STORE.update_vehicle_ammo_condition(pid, ra_ammo_id, maxcond)
        repaired['condition'] = maxcond
    except Exception as e:
        log(f'[DB] repair vehicle ammo failed: {e}')
        await _refund_player_charge(sess, ra_currency, cost)
        writer.write(build_response(
            cmd_id, 'RepairVehicleAmmoResponse', pb_int32(1, 0)))
        await writer.drain()
        return

    resp_proto = pb_int32(1, 1)  # response = 1 (ok)
    resp = build_response(cmd_id, 'RepairVehicleAmmoResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} RepairVehicleAmmoResponse (response=1 ok, condition={maxcond})')

    # 推送更新后的 VehicleAmmo（condition 恢复满耐久）
    if sess.soldier_view_cmd is not None and repaired is not None:
        aproto = _msg_vehicle_ammo(repaired['id'], repaired['ammo_template_id'],
                                   repaired['vehicle_id'], repaired['equipped_storage_id'],
                                   repaired['slot_index'], repaired['character_id'], maxcond)
        await _push_kvcs(writer, sess.soldier_view_cmd, b'VehicleAmmo', aproto)
        log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
            f'(VehicleAmmo id={repaired["id"]} repaired, condition={maxcond})')
    # 生涯军令 20 eRefillVehicleAmmo（客户端无上报点）+ 推行实时刷新
    from handlers.career import complete_order_and_push
    await complete_order_and_push(sess, writer, pid, 20)



async def handle_setvehicleammoautorepairrequest(sess, writer, cmd_id, req_type, proto):
    # 设置载具弹药自动维修（VehicleModificationMediator 弹药 autoRepair 开关）
    # SetVehicleAmmoAutoRepairRequest { int64 vehicleAmmoId=1; int32 autoRepair=2; }
    # SetVehicleAmmoAutoRepairResponse { int32 response = 1; }
    ar_ammo_id = 0
    ar_auto = 0
    try:
        f = pb_parse(proto)
        ar_ammo_id = f.get(1, 0)
        ar_auto = f.get(2, 0)
        log(f'      SetVehicleAmmoAutoRepair: ammoId={ar_ammo_id}, autoRepair={ar_auto}')
    except Exception as e:
        log(f'      SetVehicleAmmoAutoRepair parse error: {e}')

    pid = await sess.ensure_player()
    updated = False
    if pid is not None and ar_ammo_id:
        try:
            await STORE.set_equipment_auto_repair(pid, 9, int(ar_ammo_id),
                                                  int(ar_auto))
            updated = any(int(a['id']) == int(ar_ammo_id)
                          for a in await STORE.list_vehicle_ammo(pid))
        except Exception as e:
            log(f'[DB] set_equipment_auto_repair failed: {e}')

    resp_proto = pb_int32(1, 1 if updated else 0)
    resp = build_response(cmd_id, 'SetVehicleAmmoAutoRepairResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetVehicleAmmoAutoRepairResponse '
        f'(response={1 if updated else 0})')

    # 推送更新后的 VehicleAmmo（autoRepair f9 更新；三态原样回传）
    if pid is not None and sess.soldier_view_cmd is not None:
        try:
            for a in await STORE.list_vehicle_ammo(pid):
                if int(a['id']) == int(ar_ammo_id):
                    await _push_kvcs(writer, sess.soldier_view_cmd, b'VehicleAmmo',
                                     _msg_vehicle_ammo(a['id'], a['ammo_template_id'],
                                                       a['vehicle_id'],
                                                       a['equipped_storage_id'],
                                                       a['slot_index'], a['character_id'],
                                                       a.get('condition', 100.0),
                                                       int(ar_auto)))
                    log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                        f'(VehicleAmmo id={ar_ammo_id} autoRepair={ar_auto})')
                    break
        except Exception as e:
            log(f'[KVCS] vehicle ammo auto-repair push failed: {e}')


async def handle_repairvehiclerequest(sess, writer, cmd_id, req_type, proto):
    # 修复载具耐久（VehicleService.repairVehicle）
    # RepairVehicleRequest { int64 vehicleId=2; int32 repairToCondition=3;
    #   int64 maxConditionRepairPricingId=4; int32 overrideCondition=5;
    #   int64 storeDiscountId=6; int32 paymentCurrency=7; }
    # RepairVehicleResponse { int32 response = 2; }  ← 响应字段号是 2！
    try:
        f = pb_parse(proto)
        rv_vehicle_id = f.get(2, 0)
        rv_condition = f.get(3, 0)
        rv_pricing_id = f.get(4, 0)
        rv_currency = f.get(7, 0)
        log(f'      RepairVehicle: vehicleId={rv_vehicle_id}, '
            f'repairToCondition={rv_condition}, pricingId={rv_pricing_id}, '
            f'currency={_CUR_NAME.get(int(rv_currency), rv_currency)}')
    except Exception as e:
        log(f'      RepairVehicle parse error: {e}')
        rv_vehicle_id = 0
        rv_condition = 100
        rv_pricing_id = 0
        rv_currency = 0

    pid = await sess.ensure_player()
    repaired = None
    if pid is not None and rv_vehicle_id:
        try:
            repaired = await STORE.get_vehicle(pid, int(rv_vehicle_id))
        except Exception as e:
            log(f'[DB] load vehicle for repair failed: {e}')
    if repaired is None:
        resp = build_response(
            cmd_id, 'RepairVehicleResponse', pb_int32(2, 0))
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} RepairVehicleResponse '
            f'(response=0 vehicle_not_found)')
        return

    # 载具实例持有的是 variant template，由 wardb 映射到基础载具的满耐久。
    maxcond = vehicle_full_condition(repaired['vehicle_variant_template_id'])
    current = max(0.0, min(float(repaired.get('condition') or 0), maxcond))
    fraction = (maxcond - current) / maxcond if maxcond > 0 else 0.0

    # 与客户端显示价使用同一损耗比例扣款。
    cost, ok = await _charge_player(
        sess, rv_pricing_id, rv_currency, fraction=fraction,
        expected_object_id=_VEHICLE_VARIANT_PARENT.get(
            int(repaired['vehicle_variant_template_id']),
            int(repaired['vehicle_variant_template_id'])),
        expected_types='VehicleMaxConditionRepair')
    if not ok:
        resp_proto = pb_int32(2, 56)  # not_enough_credits（字段号 2）
        resp = build_response(cmd_id, 'RepairVehicleResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} RepairVehicleResponse (response=56 not_enough) '
            f'cost={cost}')
        return

    # 修复到模板满耐久，不信任客户端传入的目标值。
    try:
        await STORE.update_vehicle_condition(pid, int(rv_vehicle_id), maxcond)
        repaired['condition'] = maxcond
    except Exception as e:
        log(f'[DB] update_vehicle_condition failed: {e}')
        await _refund_player_charge(sess, rv_currency, cost)
        writer.write(build_response(
            cmd_id, 'RepairVehicleResponse', pb_int32(2, 0)))
        await writer.drain()
        return

    resp_proto = pb_int32(2, 1)
    resp = build_response(cmd_id, 'RepairVehicleResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} RepairVehicleResponse (response=1 ok, '
        f'condition={maxcond})')

    # 推送更新后的 Vehicle（condition 恢复）
    if sess.soldier_view_cmd is not None and repaired is not None:
        try:
            vproto = _msg_vehicle(repaired['id'], repaired['vehicle_variant_template_id'],
                                  repaired['character_id'],
                                  repaired.get('equipped_in_slot', 0),
                                  repaired.get('variant_id', 0),
                                  repaired.get('auto_repair', 0),
                                  maxcond)
            await _push_kvcs(writer, sess.soldier_view_cmd, b'Vehicle', vproto)
            log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                f'(Vehicle id={rv_vehicle_id} repaired, condition={maxcond})')
        except Exception as e:
            log(f'[KVCS] vehicle repair push failed: {e}')
    await _push_player_refresh(sess, writer)
    # 生涯军令 19 eRepairVehicle（客户端无上报点）+ 推行实时刷新
    from handlers.career import complete_order_and_push
    await complete_order_and_push(sess, writer, pid, 19)


async def handle_setvehicleautorepairrequest(sess, writer, cmd_id, req_type, proto):
    # 载具自动维修开关（VehicleService）
    # SetVehicleAutoRepairRequest { int64 vehicleId=2; int32 autoRepair=12; }  ← 字段号特殊！
    # SetVehicleAutoRepairResponse { int32 response = 2; }  ← 响应字段号是 2！
    try:
        f = pb_parse(proto)
        ar_vehicle_id = f.get(2, 0)
        ar_auto = f.get(12, 0)
        log(f'      SetVehicleAutoRepair: vehicleId={ar_vehicle_id}, autoRepair={ar_auto}')
    except Exception as e:
        log(f'      SetVehicleAutoRepair parse error: {e}')
        ar_vehicle_id = 0
        ar_auto = 0

    pid = await sess.ensure_player()
    updated = None
    if pid is not None and ar_vehicle_id:
        try:
            await STORE.set_equipment_auto_repair(pid, 2, int(ar_vehicle_id),
                                                  int(ar_auto))
            updated = await STORE.get_vehicle(pid, int(ar_vehicle_id))
        except Exception as e:
            log(f'[DB] set_equipment_auto_repair failed: {e}')

    resp_proto = pb_int32(2, 1 if updated is not None else 0)
    resp = build_response(cmd_id, 'SetVehicleAutoRepairResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetVehicleAutoRepairResponse '
        f'(response={1 if updated is not None else 0})')

    # 推送更新后的 Vehicle（autoRepair f8 更新；三态原样回传，勾选框不跳变）
    if sess.soldier_view_cmd is not None and updated is not None:
        try:
            vproto = _msg_vehicle(updated['id'], updated['vehicle_variant_template_id'],
                                  updated['character_id'],
                                  updated.get('equipped_in_slot', 0),
                                  updated.get('variant_id', 0),
                                  int(ar_auto),
                                  updated.get('condition', 100.0))
            await _push_kvcs(writer, sess.soldier_view_cmd, b'Vehicle', vproto)
            log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                f'(Vehicle id={ar_vehicle_id} autoRepair={ar_auto})')
        except Exception as e:
            log(f'[KVCS] vehicle auto-repair push failed: {e}')


async def handle_setvehicleupgradeautorepairrequest(sess, writer, cmd_id,
                                                    req_type, proto):
    # 载具升级自动维修开关（VehicleService）
    # SetVehicleUpgradeAutoRepairRequest { int64 vehicleUpgradeId=1;
    #   int32 autoRepair=2; }
    # SetVehicleUpgradeAutoRepairResponse { int32 response = 1; }
    try:
        f = pb_parse(proto)
        au_upgrade_id = f.get(1, 0)
        au_auto = f.get(2, 0)
        log(f'      SetVehicleUpgradeAutoRepair: upgradeId={au_upgrade_id}, '
            f'autoRepair={au_auto}')
    except Exception as e:
        log(f'      SetVehicleUpgradeAutoRepair parse error: {e}')
        au_upgrade_id = 0
        au_auto = 0

    pid = await sess.ensure_player()
    updated = False
    if pid is not None and au_upgrade_id:
        try:
            await STORE.set_equipment_auto_repair(pid, 8, int(au_upgrade_id),
                                                  int(au_auto))
            updated = any(int(u['id']) == int(au_upgrade_id)
                          for u in await STORE.list_vehicle_upgrades(pid))
        except Exception as e:
            log(f'[DB] set_equipment_auto_repair failed: {e}')

    resp_proto = pb_int32(1, 1 if updated else 0)
    resp = build_response(cmd_id, 'SetVehicleUpgradeAutoRepairResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetVehicleUpgradeAutoRepairResponse '
        f'(response={1 if updated else 0})')

    # 推送更新后的 VehicleUpgrade（autoRepair f8 更新；三态原样回传）
    if pid is not None and sess.soldier_view_cmd is not None:
        try:
            for u in await STORE.list_vehicle_upgrades(pid):
                if int(u['id']) == int(au_upgrade_id):
                    await _push_kvcs(writer, sess.soldier_view_cmd, b'VehicleUpgrade',
                                     _msg_vehicle_upgrade(u['id'], u['upgrade_template_id'],
                                                          u['vehicle_id'],
                                                          u['equipped_slot_id'],
                                                          u['character_id'],
                                                          u.get('condition', 100.0),
                                                          int(au_auto)))
                    log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                        f'(VehicleUpgrade id={au_upgrade_id} autoRepair={au_auto})')
                    break
        except Exception as e:
            log(f'[KVCS] vehicle upgrade auto-repair push failed: {e}')


HANDLERS = {
    'SetVehicleVariantRequest': handle_setvehiclevariantrequest,
    'SetVehicleEquipmentRequest': handle_setvehicleequipmentrequest,
    'SetVehicleUpgradeRequest': handle_setvehicleupgraderequest,
    'RepairVehicleRequest': handle_repairvehiclerequest,
    'RepairVehicleUpgradeRequest': handle_repairvehicleupgraderequest,
    'SetVehicleAmmoRequest': handle_setvehicleammorequest,
    'RepairVehicleAmmoRequest': handle_repairvehicleammorequest,
    'SetVehicleAmmoAutoRepairRequest': handle_setvehicleammoautorepairrequest,
    'SetVehicleAutoRepairRequest': handle_setvehicleautorepairrequest,
    'SetVehicleUpgradeAutoRepairRequest': handle_setvehicleupgradeautorepairrequest,
}
