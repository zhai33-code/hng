# -*- coding: utf-8 -*-
"""杂项域（装备选择上阵/下阵、客户端日志上报、玩家设置/链接账号/重置、KV 同步）。

从 realtest_server.dispatch 机械迁移，函数体保持原样；
统一签名 (sess, writer, cmd_id, req_type, proto)。
"""


import os
import time

from protocol import (
    build_response,
    pb_bool,
    pb_int32,
    pb_int64,
    pb_parse,
    pb_string,
    pb_uint32,
    )

from state import (
    STORE,
    log,
    )

from messages import (
    _msg_vehicle,
    _msg_weapon,
    _push_kvcs,
    _push_kvcs_delete,
    )


# 客户端日志落盘路径：server 根目录（与拆分前 realtest_server.py 同目录）
_SRV_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def handle_set_equipmentselection(sess, writer, cmd_id, req_type, proto):
    # 装备武器到士兵槽位
    # set_equipmentselection { int64 characterid=1; int64 equipmentid=2; int32 equipmentslot=3; }
    # set_equipmentselection_response { int32 msg = 1; }
    try:
        f = pb_parse(proto)
        eq_char_id = f.get(1, 1)
        equip_id = f.get(2, 0)
        equip_slot = f.get(3, 0)
        log(f'      EquipItem: charId={eq_char_id}, weaponId={equip_id}, slot={equip_slot}')
    except Exception as e:
        log(f'      set_equipmentselection parse error: {e}')
        equip_id = 0
        equip_slot = 0
        eq_char_id = 1

    # 装备（含挤槽）：客户端替换槽位武器时只发本请求（新武器），不发
    # remove_equipmentselection（旧武器）——set_weapon_equipment 会把同容器
    # 同槽位的旧武器挤回军械库（container 归零）并一并返回，供 KVCS 重推。
    # 否则旧武器 container_slot 仍非 0：军械库列表（右侧）不显示它、
    # 装备点数（左侧）却按新旧两把重复计算。
    affected = []
    pid = await sess.ensure_player()
    if pid is not None:
        try:
            affected = await STORE.set_weapon_equipment(
                pid, equip_id, eq_char_id, equip_slot)
        except Exception as e:
            log(f'[DB] set_weapon_equipment failed: {e}')
    if not affected:
        # 无 DB / 查不到行：退回会话缓存内存更新（仅本次会话可见）
        w = sess.weapons.get(equip_id) if hasattr(sess, 'weapons') else None
        if w is not None:
            w['container_id'] = eq_char_id
            w['container_slot'] = equip_slot
            affected = [w]
    else:
        # 同步会话缓存（新装备 + 被挤掉的旧武器）
        if hasattr(sess, 'weapons') and sess.weapons is not None:
            for wa in affected:
                if int(wa['id']) in sess.weapons:
                    sess.weapons[int(wa['id'])]['container_id'] = wa['container_id']
                    sess.weapons[int(wa['id'])]['container_slot'] = wa['container_slot']
                else:
                    sess.weapons[int(wa['id'])] = dict(wa)

    # 返回 msg=1 (ok)
    resp_proto = pb_int32(1, 1)
    resp = build_response(cmd_id, 'set_equipmentselection_response', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} set_equipmentselection_response (msg=1 ok)')

    # 推送所有受影响 weapon 行（先被挤掉的旧武器归库，再新武器进槽），
    # 客户端刷新槽位显示 + 军械库列表 + 装备点数
    if sess.soldier_view_cmd is not None:
        others = [wa for wa in affected if int(wa['id']) != int(equip_id)]
        target = [wa for wa in affected if int(wa['id']) == int(equip_id)]
        for wa in others + target:
            wproto = _msg_weapon(wa['id'], wa['name'], wa['template_id'],
                                 wa['character_id'], wa['container_id'],
                                 wa['container_slot'], wa['variant_id'],
                                 wa.get('clip_count', 0), wa.get('auto_repair', 0),
                                 wa.get('condition', 100.0))
            await _push_kvcs(writer, sess.soldier_view_cmd, b'weapon', wproto)
            log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                f'(weapon id={wa["id"]} slot={wa["container_slot"]})')

    # 生涯军令 18 eEquipSMG / 28 eEquipAntiTankWeapon（装备类别状态推导）
    # + 推行实时刷新
    if pid is not None and int(equip_slot) > 0:
        from handlers.career import complete_order_and_push
        await complete_order_and_push(sess, writer, pid)



async def handle_remove_equipmentselection(sess, writer, cmd_id, req_type, proto):
    # 卸下装备（武器或载具）
    # remove_equipmentselection { int64 equipmentid=1; int32 itemType=2; }
    # remove_equipmentselection_response { int32 msg = 1; }
    # 客户端把载具拖回仓库走的是本请求（VehicleService.removeVehicle()，
    # itemType=2 VehicleType），而非 SetVehicleEquipmentRequest —— 必须按
    # itemType 分流：载具 id 不在 weapons 表，按武器处理 UPDATE 零行生效却回 ok，
    # 载具会永久卡在角色车库（character_id 不清零）
    try:
        f = pb_parse(proto)
        unequip_id = f.get(1, 0)
        item_type = f.get(2, 0)
        log(f'      RemoveEquipment: weaponId={unequip_id}, itemType={item_type}')
    except Exception as e:
        log(f'      remove_equipmentselection parse error: {e}')
        unequip_id = 0
        item_type = 0

    if int(item_type) == 2:
        # ---- 载具：卸下装备 = 保留 character_id，仅清 equipped_in_slot ----
        # 官方语义：remove_equipmentselection(itemType=Vehicle) 是把车从装备槽
        # 移回角色车库（归属不变），characterId 清零才是归还军械仓库。
        # 客户端 SoldierOverviewMediator.onVehicleUpdated 对 characterId.isZero
        # 直接 return（不刷新装备槽），且列表"已拥有"判定依赖
        # getVehiclesOwnedByCharacter(characterId==selected)——若清零 characterId
        # 会导致装备槽残留旧车 + 车辆列表把该车当未拥有显示可购买状态
        pid = await sess.ensure_player()
        v = None
        if pid is not None and unequip_id:
            try:
                cur = await STORE.get_vehicle(pid, int(unequip_id))
                if cur is not None:
                    affected = await STORE.set_vehicle_equipment(
                        pid, int(unequip_id), int(cur['character_id']), 0)
                    v = next((x for x in affected
                              if int(x['id']) == int(unequip_id)), None)
            except Exception as e:
                log(f'[DB] set_vehicle_equipment failed: {e}')
                try:
                    v = await STORE.get_vehicle(pid, int(unequip_id))
                except Exception as e2:
                    log(f'[DB] get_vehicle failed: {e2}')

        resp_proto = pb_int32(1, 1)
        resp = build_response(cmd_id, 'remove_equipmentselection_response', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} remove_equipmentselection_response (msg=1 ok, vehicle)')

        # 推送 Vehicle KVCS（characterId 保留、slot=0）：charId 非零才会触发
        # SoldierOverviewMediator.setupVehicleSlots() 刷新装备槽，并把
        # VehicleDepotViewMediator 车辆列表条目更新为"已拥有未装备"
        if v is not None and sess.soldier_view_cmd is not None:
            vproto = _msg_vehicle(v['id'], v['vehicle_variant_template_id'],
                                  v['character_id'], v['equipped_in_slot'],
                                  v.get('variant_id', 0),
                                  condition=v.get('condition') or
                                  vehicle_full_condition(v['vehicle_variant_template_id']))
            await _push_kvcs(writer, sess.soldier_view_cmd, b'Vehicle', vproto)
            log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
                f'(Vehicle id={unequip_id} unequipped, charId={v["character_id"]})')
        return

    # ---- 武器（itemType=1）：原逻辑 ----
    w = sess.weapons.get(unequip_id) if hasattr(sess, 'weapons') else None
    pid = await sess.ensure_player()
    if pid is not None:
        try:
            await STORE.update_weapon_container(pid, unequip_id, 0, 0)
            weapons = await STORE.list_weapons(pid)
            w = next((x for x in weapons if x['id'] == unequip_id), w)
        except Exception as e:
            log(f'[DB] update_weapon_container failed: {e}')
    if w is not None:
        w['container_id'] = 0
        w['container_slot'] = 0
        # 同步会话缓存：w 可能已被 DB 回读的新 dict 覆盖，上面两行改不到
        # sess.weapons 里的旧对象；不同步的话，后续 clipCount/variant 等
        # re-push 会把旧 container 值发回客户端，已卸下的武器
        # "自动装备"回旧槽位
        if getattr(sess, 'weapons', None) and int(unequip_id) in sess.weapons:
            sess.weapons[int(unequip_id)]['container_id'] = 0
            sess.weapons[int(unequip_id)]['container_slot'] = 0

    # 返回 msg=1 (ok)
    resp_proto = pb_int32(1, 1)
    resp = build_response(cmd_id, 'remove_equipmentselection_response', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} remove_equipmentselection_response (msg=1 ok)')

    # 推送更新后的 weapon 行（containerSlot=0）
    if w is not None and sess.soldier_view_cmd is not None:
        wproto = _msg_weapon(unequip_id, w['name'], w['template_id'], w['character_id'],
                             0, 0, w['variant_id'], w.get('clip_count', 0),
                             w.get('auto_repair', 0), w.get('condition', 100.0))
        await _push_kvcs(writer, sess.soldier_view_cmd, b'weapon', wproto)
        log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet (weapon id={unequip_id} unequipped)')



async def handle_clientlogmessage(sess, writer, cmd_id, req_type, proto):
    # 客户端日志消息，打印内容用于调试
    try:
        f = pb_parse(proto)
        log(f'      ClientLogMessage ALL fields: {f}')
        log_type = f.get(1, b'')
        log_msg = f.get(6, b'')
        if isinstance(log_msg, bytes):
            log_msg = log_msg.decode('utf-8', 'replace')
        if isinstance(log_type, bytes):
            log_type = log_type.decode('utf-8', 'replace')
        log(f'      ClientLogMessage type={log_type}: {log_msg}')
        # 完整客户端日志落盘（客户端只发最后 50 行，但保留最近几次以便崩溃分析）
        try:
            with open(os.path.join(_SRV_DIR, 'client_logs.txt'), 'a', encoding='utf-8') as clf:
                clf.write(f'===== {time.strftime("%Y-%m-%d %H:%M:%S")} type={log_type} =====\n')
                clf.write(log_msg if isinstance(log_msg, str) else str(log_msg))
                clf.write('\n')
        except Exception as le:
            log(f'      ClientLogMessage write failed: {le}')
    except Exception as e:
        log(f'      ClientLogMessage parse error: {e}, proto={proto[:100].hex()}')
    # 客户端 DumpLogService 用 sendCommand 发送（走请求队列），
    # 必须回复 ClientLogMessageResponse 解锁串行队列，否则后续所有请求卡死
    resp = build_response(cmd_id, 'ClientLogMessageResponse', pb_int32(2, 1))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} ClientLogMessageResponse (response=1 ok)')


async def handle_setplayerlanguagerequest(sess, writer, cmd_id, req_type, proto):
    # 玩家语言设置（PlayerService；登录后语言切换窗口）
    # SetPlayerLanguageRequest { string languageCode=1; }
    # SetPlayerLanguageResponse { int32 response=1; }  1=ok
    try:
        f = pb_parse(proto)
        lang = f.get(1, b'')
        if isinstance(lang, bytes):
            lang = lang.decode('utf-8', 'replace')
        log(f'      SetPlayerLanguage: languageCode={lang!r}')
    except Exception as e:
        log(f'      SetPlayerLanguage parse error: {e}')
        lang = 'en'

    pid = await sess.ensure_player()
    if pid is not None:
        try:
            await STORE.set_player_language(pid, str(lang or 'en'))
        except Exception as e:
            log(f'[DB] set_player_language failed: {e}')

    # 持久化失败也回 ok：客户端会话内继续用请求值，避免界面弹错
    resp = build_response(cmd_id, 'SetPlayerLanguageResponse', pb_int32(1, 1))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetPlayerLanguageResponse (response=1 ok, lang={lang})')


async def handle_setplayerdiscordidrequest(sess, writer, cmd_id, req_type, proto):
    # 绑定 Discord id（AccountSettings 窗口）
    # SetPlayerDiscordIdRequest { int64 discordId=1; }
    # SetPlayerDiscordIdResponse { int32 response=1; }
    try:
        f = pb_parse(proto)
        discord_id = f.get(1, 0)
        log(f'      SetPlayerDiscordId: discordId={discord_id}')
    except Exception as e:
        log(f'      SetPlayerDiscordId parse error: {e}')
        discord_id = 0

    pid = await sess.ensure_player()
    if pid is not None and discord_id:
        try:
            await STORE.set_player_discord_id(pid, int(discord_id))
        except Exception as e:
            log(f'[DB] set_player_discord_id failed: {e}')

    resp = build_response(cmd_id, 'SetPlayerDiscordIdResponse', pb_int32(1, 1))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetPlayerDiscordIdResponse (response=1 ok)')


async def handle_setplayersteamidrequest(sess, writer, cmd_id, req_type, proto):
    # 绑定 Steam id
    # SetPlayerSteamIdRequest { int64 steamId=1; }
    # SetPlayerSteamIdResponse { int32 response=1; }
    try:
        f = pb_parse(proto)
        steam_id = f.get(1, 0)
        log(f'      SetPlayerSteamId: steamId={steam_id}')
    except Exception as e:
        log(f'      SetPlayerSteamId parse error: {e}')
        steam_id = 0

    pid = await sess.ensure_player()
    if pid is not None and steam_id:
        try:
            await STORE.set_player_steam_id(pid, int(steam_id))
        except Exception as e:
            log(f'[DB] set_player_steam_id failed: {e}')

    resp = build_response(cmd_id, 'SetPlayerSteamIdResponse', pb_int32(1, 1))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetPlayerSteamIdResponse (response=1 ok)')


async def handle_linkedloginselectaccountnamerequest(sess, writer, cmd_id,
                                                     req_type, proto):
    # 链接登录（Epic）选择账号名（EpicLinkedLogin.handleLogin → linked_login_begin
    # 之后需要为新链接账号选名；本服务器 auth 流程自动建号，此请求仅在
    # 客户端走 Epic 选名分支时出现，回 ok 让流程继续）
    # LinkedLoginSelectAccountNameRequest {
    #   string accountName=1; string tempSessionid=2; bool wantsMarketingMails=3;
    #   string email=4; bool checkIfUsernameExists=5; string promoCode=6; }
    # LinkedLoginSelectAccountNameResponse { int32 responseCode=1; }  1=ok
    try:
        f = pb_parse(proto)
        account_name = f.get(1, b'')
        if isinstance(account_name, bytes):
            account_name = account_name.decode('utf-8', 'replace')
        check_only = bool(f.get(5, 0))
        log(f'      LinkedLoginSelectAccountName: accountName={account_name!r}, '
            f'checkIfUsernameExists={check_only}')
    except Exception as e:
        log(f'      LinkedLoginSelectAccountName parse error: {e}')
        account_name = ''
        check_only = False

    # checkIfUsernameExists=true 只查询名字是否可用；统一回 ok（名字可用）
    resp = build_response(cmd_id, 'LinkedLoginSelectAccountNameResponse',
                          pb_int32(1, 1))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} LinkedLoginSelectAccountNameResponse '
        f'(responseCode=1 ok, checkOnly={check_only})')


async def handle_resetplayerrequest(sess, writer, cmd_id, req_type, proto):
    # 重置玩家进度（确认弹窗后触发）：删角色与全部装备/进度，保留账号与货币
    # ResetPlayerRequest { int32 dummy=1; }
    # ResetPlayerResponse { int32 response=1; }
    pid = await sess.ensure_player()
    deleted = 0
    deleted_ids = []
    if pid is not None:
        try:
            # 先取角色 id 列表，reset 后逐行推 character delete KVCS 清空客户端 UI
            deleted_ids = [int(ch['id']) for ch in await STORE.list_characters(pid)]
            deleted = await STORE.reset_player_progress(pid)
        except Exception as e:
            log(f'[DB] reset_player_progress failed: {e}')
            deleted = 0
            deleted_ids = []

    resp = build_response(cmd_id, 'ResetPlayerResponse', pb_int32(1, 1))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} ResetPlayerResponse (response=1 ok, '
        f'deletedChars={deleted})')

    # 推送 character 表 delete 行（士兵列表清空，回到创建角色界面）
    if deleted_ids and sess.soldier_view_cmd is not None:
        for cid in deleted_ids:
            try:
                await _push_kvcs_delete(writer, sess.soldier_view_cmd,
                                        b'character', cid)
            except Exception as e:
                log(f'[KVCS] character delete push failed: {e}')
        log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
            f'(character delete x{len(deleted_ids)})')


async def handle_verifypremiumstatusrequest(sess, writer, cmd_id, req_type, proto):
    # 校验会员/Premium 状态（PlayerService，客户端定期或会员操作时触发）
    # VerifyPremiumStatusRequest { int32 dummy=1; }
    # VerifyPremiumStatusResponse { int32 msg=1; }  1=ok（会员有效）
    resp = build_response(cmd_id, 'VerifyPremiumStatusResponse', pb_int32(1, 1))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} VerifyPremiumStatusResponse (msg=1 ok)')


async def handle_respondtounlocknotificationrequest(sess, writer, cmd_id,
                                                    req_type, proto):
    # 解锁通知已读/回应（UnlockNotificationService.HandleNotificationReadStatusCommand）
    # RespondToUnlockNotificationRequest { int64 notificationId=1;
    #   int32 notificationResponseCode=2; }
    # RespondToUnlockNotificationResponse { int32 response=1; }
    try:
        f = pb_parse(proto)
        notification_id = f.get(1, 0)
        code = f.get(2, 0)
        log(f'      RespondToUnlockNotification: notificationId={notification_id}, '
            f'responseCode={code}')
    except Exception as e:
        log(f'      RespondToUnlockNotification parse error: {e}')

    # 本服务器不解锁通知重发逻辑，ack 即可；客户端本地移除通知条目
    resp = build_response(cmd_id, 'RespondToUnlockNotificationResponse',
                          pb_int32(1, 1))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} RespondToUnlockNotificationResponse (response=1 ok)')


async def handle_deploytoolrequest(sess, writer, cmd_id, req_type, proto):
    # RETO 部署运维工具指令（正常客户端不触发；实测出现时 ack 掉防请求队列卡死）
    # DeployToolRequest { string sessionToken=1; bool initiateShutdown=2;
    #   string shutdownTime=3; string shutdownReason=4; string downtimeEstimated=5; }
    # DeployToolResponse { bool success=1; }
    try:
        f = pb_parse(proto)
        shutdown = bool(f.get(2, 0))
        reason = f.get(4, b'')
        if isinstance(reason, bytes):
            reason = reason.decode('utf-8', 'replace')
        log(f'      DeployTool: initiateShutdown={shutdown}, reason={reason!r}')
    except Exception as e:
        log(f'      DeployTool parse error: {e}')

    resp = build_response(cmd_id, 'DeployToolResponse', pb_bool(1, True))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} DeployToolResponse (success=true)')


async def handle_keyvaluesynccmd_requestsync(sess, writer, cmd_id, req_type, proto):
    # KV 全量重同步请求（netsys 层，断线恢复/校验时触发）
    # KeyValueSyncCmd_RequestSync {}（无字段）
    # KeyValueSyncCmd_RequestSyncResponse { int32 dummy=1; }
    # 服务器各视图在 subscribe 时推全量；此处 ack，客户端后续自行重新订阅校准
    resp = build_response(cmd_id, 'KeyValueSyncCmd_RequestSyncResponse',
                          pb_int32(1, 1))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} KeyValueSyncCmd_RequestSyncResponse (dummy=1)')


async def handle_keyvaluesynccmd_getversion(sess, writer, cmd_id, req_type, proto):
    # KV 表版本查询（netsys 层）
    # KeyValueSyncCmd_GetVersion { int32 dummy=1; }
    # KeyValueSyncCmd_GetVersionResponse { uint64 version=1; }
    # 版本号用当前 Unix 时间：客户端仅比较大小判断是否有更新
    version = int(time.time())
    resp = build_response(cmd_id, 'KeyValueSyncCmd_GetVersionResponse',
                          pb_uint32(1, version))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} KeyValueSyncCmd_GetVersionResponse '
        f'(version={version})')


HANDLERS = {
    'set_equipmentselection': handle_set_equipmentselection,
    'remove_equipmentselection': handle_remove_equipmentselection,
    'ClientLogMessage': handle_clientlogmessage,
    'SetPlayerLanguageRequest': handle_setplayerlanguagerequest,
    'SetPlayerDiscordIdRequest': handle_setplayerdiscordidrequest,
    'SetPlayerSteamIdRequest': handle_setplayersteamidrequest,
    'LinkedLoginSelectAccountNameRequest': handle_linkedloginselectaccountnamerequest,
    'ResetPlayerRequest': handle_resetplayerrequest,
    'VerifyPremiumStatusRequest': handle_verifypremiumstatusrequest,
    'RespondToUnlockNotificationRequest': handle_respondtounlocknotificationrequest,
    'DeployToolRequest': handle_deploytoolrequest,
    'KeyValueSyncCmd_RequestSync': handle_keyvaluesynccmd_requestsync,
    'KeyValueSyncCmd_GetVersion': handle_keyvaluesynccmd_getversion,
}
