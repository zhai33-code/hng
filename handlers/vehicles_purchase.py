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
    _E_WEAPON_AMMO_MAPPING,
    _REQ_UNLOCK_BY_ITEM,
    _VEHICLE_UPGRADE_MAXCOND,
    _VEHICLE_UPGRADE_SLOT,
    _VEHICLE_VARIANT_PARENT,
    _default_vehicle_ammo_for,
    _vehicle_supports_ammo,
    vehicle_ammo_mapping_ids,
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
    _character_owned,
    _charge_player,
    _charge_player_multi,
    _fail_persisted_purchase,
    _msg_player_for,
    _unlock_need_for_char,
    )
from .weapons_purchase import _repush_character_unlocks


async def grant_default_vehicle_ammo(pid, char_id, vehicle_id, parent_vehicle_tid,
                                     sess=None, writer=None):
    """新车默认弹药发放（购买载具 / 新兵初始载具 / 商店礼包发货共用）。

    每个默认弹药仓仅创建一份弹药并装备到第 1 槽；单槽拥有完整载弹量。
    _default_vehicle_ammo_for 已按 (storage, ammo) 去重；同一仓多条默认
    映射只按首条发放，多个武器槽共享同一弹药仓（如 78 号机 6 挺机枪
    共用 storage 116）也只发一份。storageSlots 是可选装备槽数，
    勋带页的“已拥有”数量阈值不决定初始发货量。

    返回 [(storageId, slot, vehicle_ammo行), ...]；传 sess/writer 且
    soldier_view_cmd 有效时逐条 KVCS 推送（须在 Vehicle 行推送之后调用，
    客户端按 vehicle id 关联）。DB 失败抛异常，由调用方决定记录/中断。"""
    out = []
    filled = set()
    for (storage_id, ammo_template_id) in _default_vehicle_ammo_for(parent_vehicle_tid):
        if int(storage_id) in filled:
            continue
        filled.add(int(storage_id))
        slot = 1
        va = await STORE.create_vehicle_ammo(
            pid, ammo_template_id, vehicle_id, char_id,
            _AMMO_MAXCOND.get(int(ammo_template_id), 100.0))
        await STORE.set_vehicle_ammo_equipment(pid, va['id'], int(storage_id), slot, True)
        out.append((int(storage_id), slot, va))
        if sess is not None and writer is not None and sess.soldier_view_cmd is not None:
            await _push_kvcs(writer, sess.soldier_view_cmd, b'VehicleAmmo',
                             _msg_vehicle_ammo(va['id'], ammo_template_id,
                                               vehicle_id, int(storage_id), slot,
                                               char_id, va['condition']))
            log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet (VehicleAmmo '
                f'id={va["id"]}, template={ammo_template_id}, '
                f'storage={storage_id}, slot={slot})')
    return out


async def handle_createvehiclevariantrequest(sess, writer, cmd_id, req_type, proto):
    # 载具商店购买载具请求（VehicleShopMediator / VehicleDepotViewMediator）
    # CreateVehicleVariantRequest { int64 characterId=1; int64 vehicleVariantTemplateId=2;
    #   int64 vehicleVariantPricingId=3; int64 vehicleId=4; int64 extraVehicleVariantTemplateId=5;
    #   int64 extraVehicleVariantPricingId=6; int64 discountId=7; int32 paymentCurrency=8; }
    # CreateVehicleVariantResponse { int32 response = 1; }
    vv_char_id = 0
    vv_vehicle_id = 0
    vv_template_id = 0
    vv_pricing_id = 0
    vv_extra_template_id = 0
    vv_extra_pricing_id = 0
    try:
        f = pb_parse(proto)
        vv_char_id = f.get(1, 0)
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

    if not await _character_owned(sess, vv_char_id):
        writer.write(build_response(
            cmd_id, 'CreateVehicleVariantResponse', pb_int32(1, 0)))
        await writer.drain()
        return
    if vv_vehicle_id:
        pid_v = await sess.ensure_player()
        owned_vehicle = (await STORE.get_vehicle(pid_v, int(vv_vehicle_id))
                         if pid_v is not None else None)
        if (owned_vehicle is None
                or int(owned_vehicle['character_id']) != int(vv_char_id)):
            writer.write(build_response(
                cmd_id, 'CreateVehicleVariantResponse', pb_int32(1, 0)))
            await writer.drain()
            return

    # 皮肤购买判定：vehicleId 指向已有载具 + vehicleVariantTemplateId=0 + extraTemplate>0
    # （VehicleModificationMediator.onPurchaseClickHandler 给已有载具买皮肤时填这三个字段）。
    # 只创建 VehicleVariant 实例，绝不创建新载具（否则会推送 templateId=0 的假载具）。
    is_skin_purchase = vv_vehicle_id > 0 and vv_template_id == 0 and vv_extra_template_id > 0
    if ((vv_template_id and int(vv_pricing_id or 0) <= 0)
            or (is_skin_purchase and int(vv_extra_pricing_id or 0) <= 0)):
        writer.write(build_response(
            cmd_id, 'CreateVehicleVariantResponse', pb_int32(1, 0)))
        await writer.drain()
        return

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
        sess, [vv_pricing_id, vv_extra_pricing_id], currency,
        # The shop sends a factory variant id, but its hull/airframe price
        # belongs to vehicletemplate (the variant's parent). Only the optional
        # paint/skin is priced as VehicleVariantTemplate.
        expected=[(_VEHICLE_VARIANT_PARENT.get(int(vv_template_id), 0),
                   'vehicletemplate'),
                  (vv_extra_template_id, 'VehicleVariantTemplate')])
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
        # 先落库，成功后再确认；失败时退款，禁止生成会话假 id。
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
            await _fail_persisted_purchase(
                sess, writer, cmd_id, 'CreateVehicleVariantResponse', 1,
                currency, cost_v)
            return
        resp = build_response(
            cmd_id, 'CreateVehicleVariantResponse', pb_int32(1, 1))
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} CreateVehicleVariantResponse (response=1 ok, '
            f'skin vehicleId={vv_vehicle_id}, extraTemplate={vv_extra_template_id})')
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

    # 分配载具实例 ID 并写入数据库；失败时退款并返回失败。
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
        await _fail_persisted_purchase(
            sess, writer, cmd_id, 'CreateVehicleVariantResponse', 1,
            currency, cost_v)
        return
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
    #     每仓只装备一份默认弹，其他槽位由玩家自行配置。
    parent_vv = _VEHICLE_VARIANT_PARENT.get(int(vv_template_id))
    if parent_vv is not None:
        try:
            for v in await STORE.list_vehicles(pid):
                if v['id'] == vid:
                    vv_char_id = v['character_id']
                    break
        except Exception as e:
            log(f'[DB] reload vehicle char failed: {e}')
        # 先刷新角色默认弹权限，再推新增弹药（客户端 ADD 会重算勋带状态）。
        await _repush_character_unlocks(sess, writer, vv_char_id)
        try:
            await grant_default_vehicle_ammo(pid, vv_char_id, vid, parent_vv,
                                             sess, writer)
        except Exception as e:
            log(f'[DB] grant default vehicle ammo failed: {e}')

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
    cost, ok = await _charge_player(
        sess, vu_pricing_id, vu_currency,
        expected_object_id=vu_template_id,
        expected_types='vehicleupgradetemplate')
    if not ok:
        resp_proto = pb_int32(1, 56)  # not_enough_credits
        resp = build_response(cmd_id, 'CreateVehicleUpgradeResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} CreateVehicleUpgradeResponse (response=56 not_enough) cost={cost}')
        return

    # 持久化载具升级实例；失败时退款并返回失败。
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
        await _fail_persisted_purchase(
            sess, writer, cmd_id, 'CreateVehicleUpgradeResponse', 1,
            vu_currency, cost)
        return

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
    parent_tpl = 0
    pid = await sess.ensure_player()
    if pid is not None:
        try:
            for v in await STORE.list_vehicles(pid):
                if v['id'] == va_vehicle_id:
                    va_char_id = v['character_id']
                    parent = _VEHICLE_VARIANT_PARENT.get(int(v['vehicle_variant_template_id']))
                    va_ok = parent is not None and _vehicle_supports_ammo(parent, va_template_id)
                    if va_ok:
                        parent_tpl = parent
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

    # 与勋带页相同：权限按 weaponAmmoMapping.id 判定，任一兼容映射满足
    # 即可购买。拥有几份、装备几槽不参与解锁；拒绝必须发生在扣款之前。
    unlocked = False
    char_uids = await _char_bits_for(sess, va_char_id)
    for mapping_id in vehicle_ammo_mapping_ids(parent_tpl, va_template_id):
        need = await _unlock_need_for_char(
            sess, va_char_id,
            _REQ_UNLOCK_BY_ITEM.get((mapping_id, _E_WEAPON_AMMO_MAPPING), set()))
        if not need or need & char_uids:
            unlocked = True
            break
    if not unlocked:
        writer.write(build_response(cmd_id, 'CreateVehicleAmmoResponse', pb_int32(1, 0)))
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} CreateVehicleAmmoResponse (rejected: '
            f'ammo={va_template_id} not unlocked for character={va_char_id})')
        return

    # 扣款；余额不足返回错误响应，不创建弹药
    cost, ok = await _charge_player(
        sess, va_pricing_id, va_currency,
        expected_object_id=va_template_id, expected_types='ammotemplate')
    if not ok:
        resp_proto = pb_int32(1, 56)  # not_enough_credits
        resp = build_response(cmd_id, 'CreateVehicleAmmoResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} CreateVehicleAmmoResponse (response=56 not_enough) cost={cost}')
        return

    # 一次购买只创建一份未装备弹药。勋带页在达到仓槽数前允许继续补购，
    # 不应为了隐藏购买按钮而一次赠送整仓。落库失败退款并返回失败。
    # ★ 2026-10-28 修正（第二次）：character_id 必须写**载具的角色**（va_char_id）。
    #   之前改成 0 是为了躲 Error #1009，但 #1009 的真因已查明是客户端
    #   VehicleModificationMediator.ammoPurchaseResponse 里 this._selectedUpgrade
    #   为 null（该载具没有任何已装备的升级件/弹药），与 character_id 无关。
    #   而 characterId=0 的弹药客户端按 INDEX_CHARACTER_ID 过滤时看不到，
    #   表现为「买了但被吞了」。所以写回 va_char_id；undefined-没有已装备项
    #   的情况由下方 _vehicle_has_equipped_item 的自动装备兜住。
    maxcond = _AMMO_MAXCOND.get(int(va_template_id), 100.0)
    try:
        a = await STORE.create_vehicle_ammo(pid, va_template_id, va_vehicle_id,
                                            va_char_id, maxcond)
    except Exception as e:
        log(f'[DB] create_vehicle_ammo failed: {e}')
        await _fail_persisted_purchase(
            sess, writer, cmd_id, 'CreateVehicleAmmoResponse', 1, va_currency, cost)
        return
    va_id = a['id']

    # ★ 2026-10-28 关键兼容修复（客户端 Error #1009 真因，反编译确认）：
    #   客户端 VehicleModificationMediator.ammoPurchaseResponse() 的实现是：
    #     if (response == ok) purchaseSignal.dispatch(VEHICLE_AMMO,
    #                                this._selectedUpgrade.upgradeTemplateId, 0);
    #   this._selectedUpgrade **没有 null 检查**（同一文件里 onUpgradeSelectionChange
    #   有检查，这里漏了）。而 _selectedUpgrade 只在「当前载具存在 isEquipped 的
    #   升级件/弹药」时才被赋值；弹药侧 isEquipped = equippedVehicleAmmoStorageId
    #   不等于 0。若该载具手上没有任何已装备的弹药，列表里就没有可选中项，
    #   _selectedUpgrade 保持 null → 买弹药返回 ok 时当场抛 Error #1009，
    #   改装窗口卡死、拖拽失效。
    #   规避：先查该载具是否已有「已装备」的弹药/升级件；没有的话把新买的这发
    #   自动装进它的默认弹药仓（KVCS 先于 Response 送达，客户端重建列表后就有
    #   isEquipped 项，_selectedUpgrade 不再是 null）。
    async def _vehicle_has_equipped_item(player_id, vehicle_id):
        try:
            for _a in await STORE.list_vehicle_ammo(player_id):
                if (int(_a['vehicle_id'] or 0) == int(vehicle_id)
                        and int(_a['equipped_storage_id'] or 0) != 0):
                    return True
            for _u in await STORE.list_vehicle_upgrades(player_id):
                if (int(_u['vehicle_id'] or 0) == int(vehicle_id)
                        and int(_u['equipped_slot_id'] or 0) != 0):
                    return True
        except Exception as _e:
            log(f'[DB] equipped-item probe failed: {_e}')
        return False

    va_auto_storage = 0
    va_auto_slot = 0
    try:
        if not await _vehicle_has_equipped_item(pid, va_vehicle_id):
            for (_sid, _tid) in _default_vehicle_ammo_for(parent_tpl):
                if int(_tid) == int(va_template_id):
                    va_auto_storage = int(_sid)
                    break
            if va_auto_storage:
                await STORE.set_vehicle_ammo_equipment(
                    pid, va_id, va_auto_storage, 1, True, va_vehicle_id)
                va_auto_slot = 1
                log(f'      [compat] 载具 {va_vehicle_id} 无已装备弹药，新弹药 '
                    f'id={va_id} 自动装到仓 {va_auto_storage} 槽 1（规避客户端 #1009）')
    except Exception as _e:
        log(f'[DB] auto-equip new ammo failed: {_e}')
        va_auto_storage = 0
        va_auto_slot = 0

    # ★ 2026-10-28 关键顺序修复（客户端 Error #1009 真因）：
    #   客户端 VehicleModificationMediator.ammoPurchaseResponse() 在收到
    #   CreateVehicleAmmoResponse 时会去 VehicleAmmoTable 里找这发新弹药；
    #   如果此时 KVCS 还没到达，查表得到 null → 访问属性抛 Error #1009，
    #   弹窗崩掉、界面卡死无法拖拽（实测堆栈：
    #   TypeError: Error #1009 at VehicleModificationMediator/ammoPurchaseResponse）。
    #   所以必须 **先推 VehicleAmmo 行，再发 Response**，让响应到达时表里已有数据。
    #   （va_char_id 仍用于解锁校验与角色权限推送。）

    # 1. 先通过 SOLDIER_VIEW 推送 VehicleAmmo 实例（同一 TCP 连接，顺序有保证）
    if sess.soldier_view_cmd is not None:
        aproto = _msg_vehicle_ammo(va_id, va_template_id, va_vehicle_id,
                                   va_auto_storage, va_auto_slot,
                                   va_char_id, maxcond)
        await _push_kvcs(writer, sess.soldier_view_cmd, b'VehicleAmmo', aproto)
        log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet (VehicleAmmo id={va_id}, '
            f'template={va_template_id}, vehicle={va_vehicle_id}, '
            f'storage={va_auto_storage}, slot={va_auto_slot}, condition={maxcond})')

    # 2. 再返回 CreateVehicleAmmoResponse (response=1 ok)，客户端显示购买成功
    resp_proto = pb_int32(1, 1)
    resp = build_response(cmd_id, 'CreateVehicleAmmoResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} CreateVehicleAmmoResponse (response=1 ok, '
        f'ammoId={va_id}, count=1)')

    # 3. 推角色权限（勋带页重算时看到更新后的权限）
    await _repush_character_unlocks(sess, writer, va_char_id)

    # 3. 推送 player 刷新货币显示
    if sess.playerview_cmd is not None:
        player_proto = await _msg_player_for(sess)
        await _push_kvcs(writer, sess.playerview_cmd, b'player', player_proto)


HANDLERS = {
    'CreateVehicleVariantRequest': handle_createvehiclevariantrequest,
    'CreateVehicleUpgradeRequest': handle_createvehicleupgraderequest,
    'CreateVehicleAmmoRequest': handle_createvehicleammorequest,
}
