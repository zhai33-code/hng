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
    _E_VEHICLE,
    _E_VEHICLES_VARIANT,
    _E_VEHICLE_UPGRADE_MAPPING,
    _REQ_UNLOCK_BY_ITEM,
    _VEHICLE_UPGRADE_MAXCOND,
    _VEHICLE_UPGRADE_SLOT,
    _VEHICLE_VARIANT_PARENT,
    _default_vehicle_ammo_for,
    _vehicle_supports_ammo,
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
    _push_player_refresh,
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
    if pid is not None:
        try:
            v = await STORE.create_vehicle(pid, vv_template_id, vv_char_id)
            vid = v['id']
        except Exception as e:
            log(f'[DB] create_vehicle failed: {e}')
            vid = None
    else:
        vid = None
    if vid is None:
        vid = getattr(sess, '_fallback_vehicle_id', 399) + 1
        sess._fallback_vehicle_id = vid

    # 1. 返回 CreateVehicleVariantResponse (response=1 ok)，客户端显示购买成功弹窗并关闭商店
    resp_proto = pb_int32(1, 1)
    resp = build_response(cmd_id, 'CreateVehicleVariantResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} CreateVehicleVariantResponse (response=1 ok, vehicleId={vid})')

    # 2. 通过 SOLDIER_VIEW 推送 Vehicle 数据（载具才会出现在士兵装备列表）
    if sess.soldier_view_cmd is not None:
        vproto = _msg_vehicle(vid, vv_template_id, vv_char_id, 0)
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

    # 装备校验：非卸下时皮肤实例必须属于该玩家且指向该载具
    valid = False
    pid = await sess.ensure_player()
    if pid is not None:
        try:
            if int(sv_variant_id) == 0:
                valid = True
            else:
                for vv in await STORE.list_vehicle_variants(pid):
                    if (int(vv['id']) == int(sv_variant_id)
                            and int(vv['vehicle_id']) == int(sv_vehicle_id)):
                        valid = True
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
    if pid is not None:
        try:
            await STORE.set_vehicle_variant(pid, int(sv_vehicle_id), int(sv_variant_id))
        except Exception as e:
            log(f'[DB] set_vehicle_variant failed: {e}')

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



async def handle_setvehicleequipmentrequest(sess, writer, cmd_id, req_type, proto):
    # 装备/卸下载具（VehicleDepotViewMediator.onVehicleItemClick equip/unEquip）
    # SetVehicleEquipmentRequest { int64 characterId=1; int64 vehicleId=2; int32 equipInSlot=3; }
    # SetVehicleEquipmentResponse { int32 msg = 1; }
    ve_char_id = 1
    ve_vehicle_id = 0
    ve_slot = 0
    try:
        f = pb_parse(proto)
        ve_char_id = f.get(1, 1)
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

    resp_proto = pb_int32(1, 1)  # msg = 1 (ok)
    resp = build_response(cmd_id, 'SetVehicleEquipmentResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetVehicleEquipmentResponse (msg=1 ok)')

    # 重推受影响的载具（目标 + 被挤掉槽位的），客户端 VehicleTable 刷新装备状态
    if sess.soldier_view_cmd is not None:
        for v in affected:
            vproto = _msg_vehicle(v['id'], v['vehicle_variant_template_id'],
                                  v['character_id'], v['equipped_in_slot'],
                                  v.get('variant_id', 0))
            await _push_kvcs(writer, sess.soldier_view_cmd, b'Vehicle', vproto)
            log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet (Vehicle id={v["id"]}, '
                f'charId={v["character_id"]}, slot={v["equipped_in_slot"]})')



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



async def handle_setvehicleupgraderequest(sess, writer, cmd_id, req_type, proto):
    # 装备/卸下载具升级（VehicleModificationMediator.handleVehicleUpgradeAction）
    # SetVehicleUpgradeRequest { int64 vehicleId=1; int64 vehicleUpgradeId=2;
    #   int64 vehicleUpgradeSlotId=3; bool doEquip=4; }
    # SetVehicleUpgradeResponse { int32 response = 1; }
    su_upgrade_id = 0
    su_slot_id = 0
    su_do_equip = True
    try:
        f = pb_parse(proto)
        su_upgrade_id = f.get(2, 0)
        su_slot_id = f.get(3, 0)
        su_do_equip = bool(f.get(4, 1))
        log(f'      SetVehicleUpgrade: vehicleId={f.get(1, 0)}, upgradeId={su_upgrade_id}, '
            f'slotId={su_slot_id}, doEquip={su_do_equip}')
    except Exception as e:
        log(f'      SetVehicleUpgrade parse error: {e}')

    affected = []
    pid = await sess.ensure_player()
    if pid is not None:
        try:
            affected = await STORE.set_vehicle_upgrade_equipment(pid, su_upgrade_id,
                                                                 su_slot_id, su_do_equip)
        except Exception as e:
            log(f'[DB] set_vehicle_upgrade_equipment failed: {e}')

    resp_proto = pb_int32(1, 1)  # response = 1 (ok)
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

    # 扣款（维修费）；余额不足返回错误响应
    cost, ok = await _charge_player(sess, ru_pricing_id, ru_currency)
    if not ok:
        resp_proto = pb_int32(1, 56)  # not_enough_credits
        resp = build_response(cmd_id, 'RepairVehicleUpgradeResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} RepairVehicleUpgradeResponse (response=56 not_enough) cost={cost}')
        return

    # 查升级模板 → 修复到满耐久
    maxcond = 100.0
    repaired = None
    pid = await sess.ensure_player()
    if pid is not None:
        try:
            for u in await STORE.list_vehicle_upgrades(pid):
                if u['id'] == ru_upgrade_id:
                    maxcond = _VEHICLE_UPGRADE_MAXCOND.get(int(u['upgrade_template_id']), 100.0)
                    await STORE.update_vehicle_upgrade_condition(pid, ru_upgrade_id, maxcond)
                    repaired = u
                    break
        except Exception as e:
            log(f'[DB] repair vehicle upgrade failed: {e}')

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
    log(f'S->C cmd={cmd_id:<4} SetVehicleAmmoResponse (response=1 ok, '
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

    # 扣款（维修费）；余额不足返回错误响应
    cost, ok = await _charge_player(sess, ra_pricing_id, ra_currency)
    if not ok:
        resp_proto = pb_int32(1, 56)  # not_enough_credits
        resp = build_response(cmd_id, 'RepairVehicleAmmoResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} RepairVehicleAmmoResponse (response=56 not_enough) cost={cost}')
        return

    # 查弹药模板 → 修复到满耐久
    maxcond = 100.0
    repaired = None
    pid = await sess.ensure_player()
    if pid is not None:
        try:
            for a in await STORE.list_vehicle_ammo(pid):
                if a['id'] == ra_ammo_id:
                    maxcond = _AMMO_MAXCOND.get(int(a['ammo_template_id']), 100.0)
                    await STORE.update_vehicle_ammo_condition(pid, ra_ammo_id, maxcond)
                    repaired = a
                    break
        except Exception as e:
            log(f'[DB] repair vehicle ammo failed: {e}')

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
    if pid is not None and ar_ammo_id:
        try:
            await STORE.set_equipment_auto_repair(pid, 9, int(ar_ammo_id),
                                                  int(ar_auto))
        except Exception as e:
            log(f'[DB] set_equipment_auto_repair failed: {e}')

    resp_proto = pb_int32(1, 1)  # response = 1 (ok)
    resp = build_response(cmd_id, 'SetVehicleAmmoAutoRepairResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetVehicleAmmoAutoRepairResponse (response=1 ok)')

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

    # 扣款（维修费）；余额不足返回错误响应
    cost, ok = await _charge_player(sess, rv_pricing_id, rv_currency)
    if not ok:
        resp_proto = pb_int32(2, 56)  # not_enough_credits（字段号 2）
        resp = build_response(cmd_id, 'RepairVehicleResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} RepairVehicleResponse (response=56 not_enough) '
            f'cost={cost}')
        return

    pid = await sess.ensure_player()
    repaired = None
    if pid is not None and rv_vehicle_id:
        try:
            v = await STORE.get_vehicle(pid, int(rv_vehicle_id))
            await STORE.update_vehicle_condition(pid, int(rv_vehicle_id),
                                                 float(rv_condition or 100))
            if v is not None:
                v['condition'] = float(rv_condition or 100)
                repaired = v
        except Exception as e:
            log(f'[DB] update_vehicle_condition failed: {e}')

    resp_proto = pb_int32(2, 1)  # response = 1 (ok；字段号 2)
    resp = build_response(cmd_id, 'RepairVehicleResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} RepairVehicleResponse (response=1 ok, '
        f'condition={rv_condition})')

    # 推送更新后的 Vehicle（condition 恢复）
    if sess.soldier_view_cmd is not None and repaired is not None:
        try:
            vproto = _msg_vehicle(repaired['id'], repaired['vehicle_variant_template_id'],
                                  repaired['character_id'],
                                  repaired.get('equipped_in_slot', 0),
                                  repaired.get('variant_id', 0),
                                  repaired.get('auto_repair', 0),
                                  float(rv_condition or 100))
            await _push_kvcs(writer, sess.soldier_view_cmd, b'Vehicle', vproto)
            log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                f'(Vehicle id={rv_vehicle_id} repaired, condition={rv_condition})')
        except Exception as e:
            log(f'[KVCS] vehicle repair push failed: {e}')
    await _push_player_refresh(sess, writer)


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

    resp_proto = pb_int32(2, 1)  # response = 1 (ok；字段号 2)
    resp = build_response(cmd_id, 'SetVehicleAutoRepairResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetVehicleAutoRepairResponse (response=1 ok)')

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
    if pid is not None and au_upgrade_id:
        try:
            await STORE.set_equipment_auto_repair(pid, 8, int(au_upgrade_id),
                                                  int(au_auto))
        except Exception as e:
            log(f'[DB] set_equipment_auto_repair failed: {e}')

    resp_proto = pb_int32(1, 1)  # response = 1 (ok)
    resp = build_response(cmd_id, 'SetVehicleUpgradeAutoRepairResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetVehicleUpgradeAutoRepairResponse (response=1 ok)')

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
    'CreateVehicleVariantRequest': handle_createvehiclevariantrequest,
    'SetVehicleVariantRequest': handle_setvehiclevariantrequest,
    'SetVehicleEquipmentRequest': handle_setvehicleequipmentrequest,
    'CreateVehicleUpgradeRequest': handle_createvehicleupgraderequest,
    'SetVehicleUpgradeRequest': handle_setvehicleupgraderequest,
    'RepairVehicleRequest': handle_repairvehiclerequest,
    'RepairVehicleUpgradeRequest': handle_repairvehicleupgraderequest,
    'CreateVehicleAmmoRequest': handle_createvehicleammorequest,
    'SetVehicleAmmoRequest': handle_setvehicleammorequest,
    'RepairVehicleAmmoRequest': handle_repairvehicleammorequest,
    'SetVehicleAmmoAutoRepairRequest': handle_setvehicleammoautorepairrequest,
    'SetVehicleAutoRepairRequest': handle_setvehicleautorepairrequest,
    'SetVehicleUpgradeAutoRepairRequest': handle_setvehicleupgradeautorepairrequest,
}
