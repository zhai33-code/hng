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
    pb_string,
    )

from state import FLASH_SESSIONS, PUBLIC_HOST, log

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

from .career import (
    push_battle_pass_row,
    push_career_order_row,
    push_season_templates,
    )
from .economy import push_economy_rows
from .social import (
    push_friend_rows,
    push_ignored_rows,
    )


# HostingCenter ping 地址：客户端 HostingCenterTable 收到行后经 hngsync 测时 ping 该地址，
# 返回 rtt>=1ms 才进入 getSortedHostingCenterList，否则弹 "unable to ping game server"。
# daemon 的 CMD_PING 只收一个 "hostname" 参数：不带端口，固定 ping 到 20201 端口
# （带 ":port" 会被整体当主机名解析失败 → 超时；实测 50010/80/50080 都 Timeout）。
# 20201 由 _start_ping_responder 监听并应答。地址取 PUBLIC_HOST（外网开服=公网 IP）。
_HC_HOST = PUBLIC_HOST


async def handle_subscribe(sess, writer, cmd_id, req_type, proto):
    if req_type.lower() == 'subscribeplayerview':
        # 发送 KeyValueChangeSet（war 表 + player 表数据），必须在 subscriberesponse 之前发送
        # war 表数据设置 warModel.currentWar，使 onCurrentWarDataReceived 能找到 currentWarInformation
        # player 表数据设置 PlayerModel.currentPlayer
        # 两者在 subscriberesponse 触发 viewSubscribtionSynced 时处理

        # 1. war 表数据
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

    # 跟踪 PLAYER_VIEW 的 cmd_id，用于购买后推送 player 更新
    if req_type.lower() == 'subscribeplayerview':
        sess.playerview_cmd = cmd_id
        # 注册到 Flash 连接注册表（重登录时新连接覆盖旧连接；断开时注销）
        # 供 combat.py 跨连接定位：hng.exe 游戏连接上报 PlayerExitBatch 时，
        # PMS Admitted KVCS 需推给该玩家的 Flash 连接
        if sess.username:
            FLASH_SESSIONS[sess.username] = sess
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
            await push_battle_pass_row(sess, writer, int(pid))
            # 好友列表 + 屏蔽名单（friendinfo / ignoredplayerwithname 表）：
            # 好友界面数据源，不推则列表恒空。
            await push_friend_rows(sess, writer, int(pid))
            await push_ignored_rows(sess, writer, int(pid))
            # 经济道具行（PlayerMembershipItem / Booster / Ticket / TicketTimed）：
            # 会员/加成/修理票 UI 数据源，不推则购买记录重启后丢失。
            await push_economy_rows(sess, writer, int(pid))

    resp = build_response(cmd_id, 'subscriberesponse')
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} subscriberesponse')



async def handle_unsubscribe(sess, writer, cmd_id, req_type, proto):
    resp = build_response(cmd_id, 'unsubscriberesponse')
    writer.write(resp)
    await writer.drain()



async def handle_query_war_catalogue_request(sess, writer, cmd_id, req_type, proto):
    # 战争目录查询，返回完整 war_catalogue（1个战争 + 3个阵营 US/GE/SU）
    resp_proto = msg_query_war_catalogue_response()
    resp = build_response(cmd_id, 'query_war_catalogue_response', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} query_war_catalogue_response (1 war, 3 factions, {len(resp_proto)}B)')



async def handle_join_war_request(sess, writer, cmd_id, req_type, proto):
    # 加入战争请求，返回 msg=1 (ResponseCodes.ok)
    # war 数据已通过 subscribeplayerview 的 KeyValueChangeSet 推送，currentWar 已设置
    # 客户端应直接走 onCurrentWarDataReceived → startPostLoginSignal
    resp_proto = pb_int32(1, 1)  # msg = 1 (ok)
    resp = build_response(cmd_id, 'join_war_response', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} join_war_response (msg=1 ok)')


HANDLERS = {
    'query_war_catalogue_request': handle_query_war_catalogue_request,
    'join_war_request': handle_join_war_request,
}
