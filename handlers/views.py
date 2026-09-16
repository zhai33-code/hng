# -*- coding: utf-8 -*-
"""视图订阅域（player/soldier/shop/hostingcenter view、战争目录）。

从 realtest_server.dispatch 机械迁移，函数体保持原样；
统一签名 (sess, writer, cmd_id, req_type, proto)。
"""


import struct

from protocol import (
    Chunk,
    build_response,
    pb_int32,
    pb_int64,
    pb_message,
    pb_parse,
    pb_string,
    )

from state import FLASH_SESSIONS, PUBLIC_HOST, STORE, log

from wardb import (
    SHOP_TABLE_FILES,
    _load_wardb_kvcs,
    )

from messages import (
    _msg_war,
    _push_kvcs,
    msg_query_war_catalogue_response,
    )

from player_service import (
    _msg_player_for,
    _push_stored_soldier_data,
    )

from matchmaking import push_relogin_rejoin
from mm_factions import (
    _msg_faction_queue_rows,
    _msg_faction_resource_rows,
    _msg_faction_rows,
    _msg_supreme_command_rows,
    )

import login_queue
import war_campaign

from .career import (
    push_all_battle_pass_rows,
    push_career_order_row,
    push_season_templates,
    )
from .characters import push_daily_bonus_rows, push_tutorial_row
from .economy import (
    push_economy_rows,
    push_player_offer_metrics_row,
    )
from .social import (
    notify_friends_of,
    push_friend_rows,
    push_ignored_rows,
    push_new_mail_rows,
    )
from .squad import push_squad_rows


# HostingCenter ping 地址：客户端 HostingCenterTable 收到行后经 hngsync 测时 ping 该地址，
# 返回 rtt>=1ms 才进入 getSortedHostingCenterList，否则弹 "unable to ping game server"。
# daemon 的 CMD_PING 只收一个 "hostname" 参数：不带端口，固定 ping 到 20201 端口
# （带 ":port" 会被整体当主机名解析失败 → 超时；实测 50010/80/50080 都 Timeout）。
# 20201 由 _start_ping_responder 监听并应答。地址取 PUBLIC_HOST（外网开服=公网 IP）。
_HC_HOST = PUBLIC_HOST

# subscriberesourceview 的 ★CommandnodeTable fill 推送开关（登录卡死排查）
# 根因已修：CommandNodeWarInstance.mapEntityTypeId=0 不属于枚举 1/2/3，客户端
# AssaultTeamResources.updateAssaultTeamMapEntity 走 getBattlefieldForMapEntity(0,0)
# 返回 null 后取 .bftitle → NPE 中断 ServiceHandler 响应处理 → 登录卡在
# "设置最终细节"。改为 3(SUPPLYLINE) 后该分支 location="" 直接 return。
_RESOURCE_FILL_ENABLED = True


async def handle_subscribe(sess, writer, cmd_id, req_type, proto):
    if req_type.lower() == 'subscribeplayerview':
        # 发送 KeyValueChangeSet（war 表 + player 表数据），必须在 subscriberesponse 之前发送
        # war 表数据设置 warModel.currentWar，使 onCurrentWarDataReceived 能找到 currentWarInformation
        # player 表数据设置 PlayerModel.currentPlayer
        # 两者在 subscriberesponse 触发 viewSubscribtionSynced 时处理

        # 1. war 表数据（DB 战争状态机优先；DB 不可用回退静态 mock）
        war_proto = await war_campaign.war_kvcs_proto(STORE)
        if war_proto is None:
            war_proto = _msg_war()
        war_data_chunk = Chunk(war_proto)
        war_table_chunk = Chunk(b'war', [war_data_chunk])
        war_action_chunk = Chunk(b'set', [war_table_chunk])
        war_title_chunk = Chunk(b'KeyValueChangeSet', [war_action_chunk])
        war_cs_main = Chunk(struct.pack('<i', cmd_id), [war_title_chunk])
        war_cs_bytes = war_cs_main.write(b'')
        writer.write(war_cs_bytes)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} KeyValueChangeSet (war data, {len(war_proto)}B)')

        # 2. player 表数据
        # ★ 每日签到：推 player 行前先记连续登录天数（同日幂等 / 昨日+1 /
        #   断签重置 1）并同步 firstActionGameDate（f19 = 连续链基准日，
        #   见 player_store.touch_daily_login）——f19 在这里随行下发，先
        #   touch 才能保证客户端拿到的基准日与 consecutive_day_count 同日。
        if STORE is not None:
            try:
                tpid = await sess.ensure_player()
                if tpid is not None:
                    await STORE.touch_daily_login(int(tpid))
            except Exception as e:
                log(f'      [DB] touch_daily_login failed: {e}')
        player_proto = await _msg_player_for(sess)
        data_chunk = Chunk(player_proto)
        table_chunk = Chunk(b'player', [data_chunk])
        action_chunk = Chunk(b'set', [table_chunk])
        title_chunk = Chunk(b'KeyValueChangeSet', [action_chunk])
        cs_main = Chunk(struct.pack('<i', cmd_id), [title_chunk])
        cs_bytes = cs_main.write(b'')
        writer.write(cs_bytes)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} KeyValueChangeSet (player data, {len(player_proto)}B)')

        # 3. faction 表 3 行（US/GE/SU）：客户端 FactionTable(xmlTag='faction')
        #    只有收到 KVCS 才有数据。原先只在 JoinMM ok / 小队入队时推——排队前
        #    打开战斗浏览器/战争图，getFromId(factionId) 返回 null → #1009；
        #    且行 id 必须==factiontemplateid（见 matchmaking.FACTION_ROW_IDS），
        #    登录即推保证 currentPlayer.factionid 的"我方"判定处处可用。
        # f6/f7 带当前 underdog 倍率（#1 人数较少阵营经验/战资加成）
        _ud = (await war_campaign.current_underdog_mults(STORE)
               if STORE is not None else None)
        for faction_row in _msg_faction_rows(_ud):
            await _push_kvcs(writer, cmd_id, b'faction', faction_row)
        log(f'S->C cmd={cmd_id:<4} KeyValueChangeSet (faction ×3 rows) ★FactionTable fill')

    # 跟踪 SOLDIER_VIEW 的 cmd_id，用于后续推送 character 数据
    if req_type.lower() == 'subscribesoldierview':
        sess.soldier_view_cmd = cmd_id
        log(f'      tracked SOLDIER_VIEW cmd_id={cmd_id}')
        # 推送数据库中已持久化的角色/武器（重启后客户端直接进入角色界面而非创建流程）
        await _push_stored_soldier_data(sess, writer)

    if req_type.lower() == 'subscribehostingcenterinfoview':
        # 推送 HostingCenterInfo 表：客户端 HostingCenterTable 收到后经 hngsync
        # (XMLSocket 127.0.0.1:50010) ping pingServiceConnect，命中后进入 To Battle 匹配列表；
        # 空表会弹 "unable to ping game server"
        hc = pb_int64(1, 1) + pb_string(2, _HC_HOST) + pb_string(3, 'Local Hosting Center')
        await _push_kvcs(writer, cmd_id, b'HostingCenterInfo', hc)
        log(f'S->C cmd={cmd_id:<4} KeyValueChangeSet (HostingCenterInfo id=1, ping={_HC_HOST})')

    if req_type.lower() == 'subscribeshopview':
        # 商店视图订阅：从 WarDatabase 回放商店表数据
        # 必须在 subscriberesponse 之前发送 KeyValueChangeSet
        sess.shop_view_cmd = cmd_id
        sent_count = 0
        total_bytes = 0
        for table_name in SHOP_TABLE_FILES:
            title_chunk = _load_wardb_kvcs(table_name)
            if title_chunk is None:
                log(f'      [!] shop table not found: {table_name}')
                continue
            # 用 cmd_id 包装 title chunk 后发送
            main_chunk = Chunk(struct.pack('<i', cmd_id), [title_chunk])
            data_bytes = main_chunk.write(b'')
            writer.write(data_bytes)
            total_bytes += len(data_bytes)
            sent_count += 1
        # StoreCampaign 表已作为 Wardb 静态表文件推送（SHOP_TABLE_FILES），
        # 不再需要运行时推送。客户端 CampaignAndInfoModel 依赖其填充轮播，
        # 空表会导致 HnGGUICampaignCarousel RangeError 崩溃(10005)。
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} KeyValueChangeSet (shop data, {sent_count} tables, {total_bytes}B)')

    if req_type.lower() == 'subscriberesourceview' and _RESOURCE_FILL_ENABLED:
        # 最高司令部指挥节点（CommandNodeDefinition + CommandNodeWarInstance 各 3 行）：
        # 阵营资源总览窗口查不到 faction.supremecommand 节点时会无限重订
        # resourceview（2026-09-01 每秒上千次 unsub/sub 死循环），必须随订阅推送。
        # 必须在下方 subscriberesponse 之前发送——客户端收到 response 即触发
        # resourceViewSubscribedSignal → setupProductionOverview 重查。
        # ⚠ 2026-09-02 登录卡死排查：此推送启用后客户端停在"设置最终细节"，
        # cmd=22 QueryShopWarBondItemsRequest 不再发出。待逐表定位后恢复。
        # （已修：CommandNodeWarInstance.mapEntityTypeId=0 NPE，见上
        # _RESOURCE_FILL_ENABLED 注释）
        sess.resource_view_cmd = cmd_id    # 生产 tick 周期重推行用（war_campaign）
        defs, instances = _msg_supreme_command_rows()
        for row in defs:
            await _push_kvcs(writer, cmd_id, b'CommandNodeDefinition', row)
        for row in instances:
            await _push_kvcs(writer, cmd_id, b'CommandNodeWarInstance', row)
        # 资源总览窗口的产量/消耗行（三阵营 × 全部分类）+ 增援队列窗口
        # （§3.12：消耗行 f8/f9 与 FactionResourceQueue 行同源）
        windows = await war_campaign.supply_windows(STORE)
        prods, cons = _msg_faction_resource_rows(windows)
        for row in prods:
            await _push_kvcs(writer, cmd_id, b'FactionResourceProduction', row)
        for row in cons:
            await _push_kvcs(writer, cmd_id, b'FactionResourceConsumption', row)
        for row in _msg_faction_queue_rows(windows):
            await _push_kvcs(writer, cmd_id, b'FactionResourceQueue', row)
        # AT 级补给队列行（§10.2 #3）：CommandNodeSupplyQueueItem 驱动
        # AssaultTeamModel.queueItems/isQueuedForResources，Ticket 驱动
        # 增援队列窗口"排队第 N 位"显示（onTicketUpdated 用
        # queueNumber - supplyQueueHead；窗口打开时会先发本订阅）
        q_items, q_tickets = await war_campaign.supply_queue_table_rows(STORE)
        for row in q_items:
            await _push_kvcs(writer, cmd_id, b'CommandNodeSupplyQueueItem',
                             row)
        for row in q_tickets:
            await _push_kvcs(writer, cmd_id,
                             b'CommandNodeSupplyQueueItemTicket', row)
        # 阵营库存行（armyresource 挂最高司令部，§3.12.1 库存列数据源；
        # 无战争/DB 不可用时静默为空）
        for row in await war_campaign.hq_stock_rows(STORE):
            await _push_kvcs(writer, cmd_id, b'armyresource', row)
        log(f'S->C cmd={cmd_id:<4} KeyValueChangeSet (supreme command ×3 factions, '
            f'resource production/consumption ×{len(prods)}) ★CommandnodeTable fill')

    if req_type.lower() in ('subscribewarmapview', 'subscribewarmaplightview'):
        # 战争地图全量快照（进入战争图时客户端订阅，WarMapViewMediator.setupWar
        # 图层初始化顺序推 battlefieldstatus → supplylinestatus →
        # accesspointstatus → CommandNodeDefinition/WarInstance →
        # CommandNodeMorale → armyresource；battle/transport 段 M1 无数据）。
        # 必须在 subscriberesponse 之前发送（客户端收 response 即触发
        # viewSubscribedSignal 开始按表取数渲染）。
        # player_id：reserve AT（补给队/运输机队）的 armyresource 行只推
        # 属主（他人收到会在开战争图时触发客户端 #1009 卡死，见
        # warmap_view.build_warmap_snapshot docstring）。
        sess.warmap_view_cmd = cmd_id
        try:
            pid = await sess.ensure_player()
        except Exception:
            pid = None
        snapshot = await war_campaign.build_warmap_snapshot(
            STORE, player_id=pid)
        for table_tag, row in snapshot:
            await _push_kvcs(writer, cmd_id, table_tag, row)
        n_tables = len({tag for tag, _ in snapshot})
        log(f'S->C cmd={cmd_id:<4} KeyValueChangeSet (warmap snapshot: '
            f'{len(snapshot)} rows / {n_tables} tables) ★warmapview fill')

    # 客户端 ServiceHandler.PLAYER_MISSION_VIEW 请求名带 Request 后缀
    # （"SubscribePlayerMissionViewRequest"，其余 subscribe* 均不带），两种都收
    if req_type.lower() in ('subscribeplayermissionview',
                            'subscribeplayermissionviewrequest'):
        # 掉线/崩溃重登 rejoin：玩家仍在存活战局 → 重推该局
        # PlayerMissionSession 行（同 pms id，state=3）+ BattleInfo，
        # 客户端 StatusMediator 据此回 REJOIN 态显示"恢复战斗"按钮
        # （点击重拉 hng.exe 回原局）；无存活战局时不推（正常登录）。
        # ★ 必须在本订阅推、不能在 subscribeplayerview 推（2026-09-08
        # remote-log 实锤）：客户端 ActionGameService.onPlayerMissionSessionChanges
        # 对 loggedIn==false 期间到达的行整段丢弃（playerMissionSessionId
        # 不设置），而 loggedIn 要到 login2_postlogin_result 成功才置 true。
        # 登录请求顺序 = subscribeplayerview → login2_postlogin →
        # subscribesoldierview → subscribeplayermissionview，故 PMS 行只有
        # 在本订阅（postlogin 之后）推才会被采纳；且若先在 playerview 推过
        # 同 id 行（表已收录、被丢弃），此处再推会命中 UPDATE 分支同样不
        # 设置 playerMissionSessionId——所以 playerview 处绝不能再推。
        try:
            pid = await sess.ensure_player()
            if pid is not None:
                await push_relogin_rejoin(writer, cmd_id, int(pid))
        except Exception as e:
            log(f'      [views] relogin rejoin push failed: {e}')

    # 跟踪 PLAYER_VIEW 的 cmd_id，用于购买后推送 player 更新
    if req_type.lower() == 'subscribeplayerview':
        sess.playerview_cmd = cmd_id
        # 注册到 Flash 连接注册表（重登录时新连接覆盖旧连接；断开时注销）
        # 供 combat.py 跨连接定位：hng.exe 游戏连接上报 PlayerExitBatch 时，
        # PMS Admitted KVCS 需推给该玩家的 Flash 连接
        if sess.username:
            FLASH_SESSIONS[sess.username] = sess
            # 登录排队：PENDING 名额转由 FLASH_SESSIONS 计数（总在线数不变）
            login_queue.on_logged_in(sess)
            log(f'      tracked PLAYER_VIEW cmd_id={cmd_id} (flash session registered)')
        else:
            log(f'      tracked PLAYER_VIEW cmd_id={cmd_id}')
        # 生涯军令进度行 + 通行证状态（赛季模板延期 + battle_pass 行）：
        # 客户端模板表来自本地 WarDatabase，进度行必须由服务器推，
        # 否则生涯军令界面无进度、通行证界面显示全部赛季过期。
        try:
            pid = await sess.ensure_player()
        except Exception as e:
            log(f'      [views] ensure_player failed: {e}')
            pid = None
        if pid is not None:
            await push_career_order_row(sess, writer, int(pid))
            await push_season_templates(sess, writer)
            # 全部有进度的赛季行都推（客户端 BattlePassTable 只有推过的
            # 行，只推默认赛季的话其他赛季查看时回落 startingScore=0）
            await push_all_battle_pass_rows(sess, writer, int(pid))
            # Tutorial 行（已触发教程集）：客户端 TutorialTable 只认 KVCS 行，
            # 不推则战报关闭后 wasTriggered(CAREER_ORDER_TUTORIAL_ID=2) 恒
            # false → 每次都切士兵界面重播生涯军令引导
            await push_tutorial_row(sess, writer, int(pid))
            # 好友列表 + 屏蔽名单（friendinfo / ignoredplayerwithname 表）：
            # 好友界面数据源，不推则列表恒空。
            await push_friend_rows(sess, writer, int(pid))
            await push_ignored_rows(sess, writer, int(pid))
            # 收件箱行（MessageRecipient 表）：邮箱 UI / 未读角标唯一数据源，
            # 不推则客户端永不发 GetMailMessagesRequest（邮箱恒空）
            await push_new_mail_rows(sess, writer)
            # 上线广播：在线好友的好友列表里把我的 onlinestatus 刷成 1，
            # 否则对方要重登才能看到我在线
            await notify_friends_of(int(pid))
            # 真实小队行（SquadSession + SquadMemberSession）：重登录成员的
            # 小队界面数据源，不推则重登后小队 UI 为空（整队匹配的前提）
            if STORE is not None:
                try:
                    squad = await STORE.get_squad_for_player(int(pid))
                except Exception as e:
                    log(f'      [DB] get_squad_for_player failed: {e}')
                    squad = None
                if squad is not None:
                    await push_squad_rows(sess, writer, squad['id'])
            # 经济道具行（PlayerMembershipItem / Booster / Ticket / TicketTimed）：
            # 会员/加成/修理票 UI 数据源，不推则购买记录重启后丢失。
            await push_economy_rows(sess, writer, int(pid))
            # 每日首战 +50% XP 行（DailyBonus 表）：士兵卡片徽章唯一数据源，
            # 不推则客户端走模板兜底分支 used 恒 false，首胜后徽章不消失。
            await push_daily_bonus_rows(sess, writer, int(pid))
            # 每日签到（PlayerOfferMetrics 行）：客户端签到窗口/主界面 tile 的
            # 唯一数据源，不推则窗口永不弹出。连续登录天数已在上面 player 行
            # 推送前 touch（views.handle_subscribe 第一分支）。
            await push_player_offer_metrics_row(sess, writer, int(pid))
            # 进展 tab 等级补发：totalScore 已跨过 PlayerLevel 阈值但奖励
            # 未发（历史数据 / 服务器停机期间结算）时登录即入账；升级则
            # 重推 player 行（上面第 99 行推的是发奖前的旧货币/等级）。
            try:
                from .progression import check_player_level
                leveled = await check_player_level(sess, writer, int(pid),
                                                   notify=False)
                if leveled:
                    player_proto = await _msg_player_for(sess)
                    await _push_kvcs(writer, cmd_id, b'player', player_proto)
                    log(f'S->C cmd={cmd_id:<4} KeyValueChangeSet '
                        f'(player refreshed, progression level-up ★进展奖励补发)')
            except Exception as e:
                log(f'      [views] progression level check failed: {e}')

    resp = build_response(cmd_id, 'subscriberesponse')
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} subscriberesponse')



async def handle_unsubscribe(sess, writer, cmd_id, req_type, proto):
    resp = build_response(cmd_id, 'unsubscriberesponse')
    writer.write(resp)
    await writer.drain()



async def handle_query_war_catalogue_request(sess, writer, cmd_id, req_type, proto):
    # 战争目录查询：DB 聚合（war_campaign 状态机）优先，DB 不可用回退静态 mock
    # f1 includeWarId：WarIsOver 终局链查结束战争的目录 → 该 war 行置首
    # （客户端取 warcataloguedata[0]，按默认序会拿到新续战 → 结算界面无数据）
    inc_war = 0
    try:
        f = pb_parse(proto)
        inc_war = int(f.get(1, 0) or 0)
    except Exception as e:
        log(f'      [views] catalogue parse error: {e}')
    rows = await war_campaign.build_war_catalogue_protos(STORE, inc_war)
    if rows:
        resp_proto = b''.join(
            pb_message(1, row) for row in rows) + pb_int64(2, 0)
        n_wars = len(rows)
    else:
        resp_proto = msg_query_war_catalogue_response()
        n_wars = 1
    resp = build_response(cmd_id, 'query_war_catalogue_response', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} query_war_catalogue_response ({n_wars} war'
        f'{f", includeWarId={inc_war}" if inc_war else ""}, 3 factions, {len(resp_proto)}B)')



async def handle_join_war_request(sess, writer, cmd_id, req_type, proto):
    """join_war_request {f1 warid, f2 factionid, f3 playedFirstBlood}
    → join_war_response {f1 msg}。

    官服口径——每场战争玩家显式选阵营（FactionSelectionWindow → 本请求）：
    factionid∈1/2/3 落库 player_war_factions(player, war, faction)，
    新战争（sequel 新 war_id）无行 → 登录 player f11=0 → 客户端重弹选择窗。
    成功后必须重推 player 行（f11 带已选 faction）：客户端
    join_war_response 的 onJoinWarRequestResponse 只处理错误码，成功态
    靠 PlayerModel 收到新 player KVCS（factionTemplateId!=0）驱动
    JoinWarFactionSuccessSignal 关窗继续。

    错误码（ResponseCodes）：77 war_not_found / 79 战争进行中不可换阵营 /
    48 moving_to_same_war（无 factionid 且已有选择：客户端 resetWarData）。"""
    factionid = 0
    try:
        f = pb_parse(proto)
        factionid = int(f.get(2) or 0)
    except Exception as e:
        log(f'      [views] join_war parse error: {e}')

    war = await war_campaign.ensure_war(STORE)
    pid = await sess.ensure_player() if STORE is not None else None
    code = 1                                     # ResponseCodes.ok
    stored = False
    if war is None:
        code = 77                                # war_not_found
    elif pid is None:
        code = 15                                # 玩家不存在
    elif factionid not in (1, 2, 3):
        # 无/非法 factionid：视为"只加入战争不选阵营"（重定向流程）。
        # 已有选择回 48：客户端 _isJoiningFaction=false 分支 resetWarData
        try:
            if await STORE.get_player_war_faction(pid, war['id']) is not None:
                code = 48                        # moving_to_same_war
        except Exception as e:
            log(f'      [views] get_player_war_faction failed: {e}')
    else:
        try:
            cur = await STORE.get_player_war_faction(pid, war['id'])
        except Exception as e:
            log(f'      [views] get_player_war_faction failed: {e}')
            cur = None
        if cur is not None and int(cur) != factionid:
            code = 79                            # cannot switch while running
            log(f'      [views] join_war blocked: player {pid} '
                f'faction {cur} -> {factionid} (war {war["id"]} running)')
        elif cur is None:
            try:
                await STORE.set_player_war_faction(pid, war['id'], factionid)
                stored = True
                log(f'[war] player {pid} joined war {war["id"]} '
                    f'faction={factionid} ★faction selection stored')
            except Exception as e:
                code = 0
                log(f'      [views] set_player_war_faction failed: {e}')
        # cur == factionid：幂等重选，直接 ok

    resp_proto = pb_int32(1, code)
    resp = build_response(cmd_id, 'join_war_response', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} join_war_response (msg={code})')

    # 落库成功 → 重推 player 行（f11=已选 faction）驱动客户端关窗继续；
    # factionid=0 无既有选择（WarIsOver 界面"进入下一场战争"按钮 → 新战争
    # 无 faction 行）同样重推：f11=0 → PlayerModel 检测 war id 变化走
    # unSubscribeViewsSignal 退订重订阅 + 弹阵营选择窗（此前不重推 →
    # 客户端停在旧战争上下文）
    if stored or (code == 1 and factionid not in (1, 2, 3)):
        push_cmd = sess.playerview_cmd or cmd_id
        try:
            player_proto = await _msg_player_for(sess)
            await _push_kvcs(writer, push_cmd, b'player', player_proto)
            log(f'S->C cmd={push_cmd:<4} KeyValueChangeSet '
                f'(player refreshed'
                f'{", faction selected" if stored else ", no faction (new war)"}'
                f' ★join_war)')
        except Exception as e:
            log(f'      [views] player refresh after join_war failed: {e}')


HANDLERS = {
    'query_war_catalogue_request': handle_query_war_catalogue_request,
    'join_war_request': handle_join_war_request,
}
