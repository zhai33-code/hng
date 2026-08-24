# -*- coding: utf-8 -*-
"""战斗服协议域（combat）：hng.exe(player.dll ZServer) 战局内周期上报与结算。

消息 schema 来源：work/hng-server-re/warserver-battlefield-settings.md §6-§8
（IDA 反编译 player.dll spbc 消息描述符还原）；
请求载荷实测锚点见 server/realtest_game.log（2026-08-21 会话）。

响应规则（§9 ZServer WarDataComm 注册表，硬约束）：
- 响应名必须在 ZServer 注册的 14 种类型内（strcmp 精确匹配），cmdid 回显
- ActiveMissionRequestEventList → ActiveMissionRequestEventListResponse
- AddGeneralStats / CareerOrderActionGameRequest → SR{response=1}
  （SR handler @0x1800B64C0 默认返回 3 → 完成 pending wait，与 AddGeneralStats
   同为 statrequests 族通用 ack）
- AnnounceBattleFieldEarningsRequest → AnnounceBattleFieldEarningsResponse
- 通知类（PlayerJoined/ReportXxx/AnnounceBattleFieldWinner/PlayerExitBatch）→
  HandlerErrorResponse（注册内、能完成 pending wait；载荷为空不影响流程，
  此前兜底路径已实证不堵塞请求队列）

DB 落点（player_store，2026-08-21 U1 建表）：
- AnnounceBattleFieldEarnings → battle_reports/battle_report_players +
  characters.combat_xp 增量 + character_ribbon_xp + players 货币
- ReportMissionStatus f3 击杀 / PlayerExitBatch f2-3 grief → character_stats
- CareerOrderActionGame → career_orders.progress
"""

import matchmaking
from protocol import (
    build_response,
    pb_bool,
    pb_int32,
    pb_int64,
    pb_message,
    pb_string,
)
from state import FLASH_SESSIONS, STORE, log
from wardb import _RIBBON_LEVEL_XP, _rank_from_combat_xp
from warserver_messages import _accesspoint_layout, _msg_army_resource_list

# 与 matchmaking._handle_join 的 game_connect 同源（mission.py _GAME_CONNECT）
_GAME_CONNECT = '127.0.0.1:5678'


# 据点归属覆盖表：ReportMissionStatusRequest f4 上报的
# {capturepointTemplateId → ownerFactionId}，供 ActiveMissionRequestEventListResponse
# f3 与 BattleFieldSettings f12 布局同源覆盖（warserver_messages._accesspoint_layout
# 注释约定）。战局结束（earnings/shutdown）时清空。
_CAPTURE_OWNERS = {}

# 最近一局胜方（AnnounceBattleFieldWinner f2 / ReportDefeatedFaction f2 推导），
# 目前仅日志观测用；U3 战报模块可读取补全结算信息。
_LAST_WINNER = {'faction': 0, 'defeated': 0}


# ---------------------------------------------------------------- 解析小工具

def _split_fields(raw):
    """protobuf 顶层拆分 → [(field_num, wire_type, value)]。

    wire_type 0 → int；2/1/5 → bytes。保持顺序（repeated 全保留）。"""
    items = []
    i, n = 0, len(raw)
    while i < n:
        key = shift = 0
        while True:
            b = raw[i]; i += 1
            key |= (b & 0x7F) << shift; shift += 7
            if not (b & 0x80):
                break
        fn, wt = key >> 3, key & 7
        if wt == 0:
            val = vsh = 0
            while True:
                b = raw[i]; i += 1
                val |= (b & 0x7F) << vsh; vsh += 7
                if not (b & 0x80):
                    break
            items.append((fn, wt, val))
        elif wt == 2:
            ln = lsh = 0
            while True:
                b = raw[i]; i += 1
                ln |= (b & 0x7F) << lsh; lsh += 7
                if not (b & 0x80):
                    break
            items.append((fn, wt, raw[i:i + ln])); i += ln
        elif wt == 5:
            items.append((fn, wt, raw[i:i + 4])); i += 4
        elif wt == 1:
            items.append((fn, wt, raw[i:i + 8])); i += 8
        else:
            break
    return items


def _get(items, fn, default=0):
    """取首个命中字段值（varint→int；bytes 原样）。"""
    for f, _wt, v in items:
        if f == fn:
            return v
    return default


def _all(items, fn):
    """取指定字段全部取值（repeated）。"""
    return [v for f, _wt, v in items if f == fn]


def _as_str(v):
    return v.decode('utf-8', 'replace') if isinstance(v, (bytes, bytearray)) else \
        (str(v) if v is not None else '')


def _session_by_player(player_id):
    """playerId → 最新匹配会话（PMS_TO_SESSION 值序 = 进局序）。"""
    found = None
    for s in matchmaking.PMS_TO_SESSION.values():
        if int(s.get('player_id', 0) or 0) == int(player_id):
            found = s
    return found


def _session_by_msid(msid):
    """missionSessionId（battle_session_id 或 pms_id）→ 会话。"""
    try:
        msid = int(msid)
    except (TypeError, ValueError):
        return None
    for s in matchmaking.PMS_TO_SESSION.values():
        if msid in (int(s.get('pms_id', 0) or 0),
                    int(s.get('battle_session_id', 0) or 0)):
            return s
    return None


async def _ack_error(writer, cmd_id, note):
    """通知类消息统一回 HandlerErrorResponse（空载荷，完成 pending wait）。"""
    resp = build_response(cmd_id, 'HandlerErrorResponse', b'')
    writer.write(resp)
    await writer.drain()
    log(f'GAME S->C cmd={cmd_id:<4} HandlerErrorResponse ({note})')


# ---------------------------------------------------------------- 兵力/资源主通道

async def handle_active_mission_event_list(sess, writer, cmd_id, req_type, proto):
    """ActiveMissionRequestEventList（战斗服每 10s 轮询）→ Response。

    §6.1：f1 msg（required string，空=无错误）/ f2 armyResourceList（资源池）/
    f3 accesspointstatuslist（据点归属）/ f5 assaultTeams（突击队兵力），
    f4/f6/f7/f8 空 repeated 完全省略（spbc hurlant 坑）。
    f2 与 ResponseBattleFieldSettings f14 同源（warserver_messages 单源构建），
    f3 归属可被 ReportMissionStatusRequest f4 捕获上报覆盖（_CAPTURE_OWNERS）。
    ★ f3 增量同步（2026-08-21）：按连接记录已下发归属，只发变化条目——
      每 10s 重发全量会被战斗服 gamemode.cpp ChangeAccesspointOwner
      (sub_1803A35D0) 以 "Failed to change owner - already own accesspoint"
      拒绝（纯日志噪音、无状态副作用，但每周期刷 6 行）。战斗服初值已由
      BattleFieldSettings f12 下发（is_combat_server 连接跳过首发）；
      布局变化（换图）时重置并全量重发。
    """
    try:
        items = _split_fields(proto)
        mission_id = _get(items, 1)
    except Exception:
        mission_id = 0

    resp_proto = pb_string(1, '')                    # f1 msg
    res_msgs = _msg_army_resource_list()
    for r in res_msgs:                               # f2 armyResourceList
        resp_proto += pb_message(2, r)

    layout = _accesspoint_layout()
    sent = getattr(sess, 'ap_owners_sent', None)
    if sent is None or getattr(sess, 'ap_sync_layout', None) != layout:
        # 新连接 / 布局变化：重置增量状态。战斗服连接初值已知（BFSettings
        # f12 同源下发），直接标记已发；客户端连接首发全量。
        sent = sess.ap_owners_sent = {}
        sess.ap_sync_layout = layout
        if getattr(sess, 'is_combat_server', False):
            for tmpl, owner in layout:
                sent[int(tmpl)] = int(_CAPTURE_OWNERS.get(int(tmpl), int(owner)))

    ap_count = 0
    for tmpl, owner in layout:                       # f3 accesspointstatuslist
        team = int(_CAPTURE_OWNERS.get(int(tmpl), int(owner)))
        if sent.get(int(tmpl)) == team:
            continue                                 # 该连接已知此归属，跳过
        sent[int(tmpl)] = team
        resp_proto += pb_message(3, pb_int32(1, int(tmpl)) + pb_int32(2, team))
        ap_count += 1

    # f5 assaultTeams：每局一个突击队（commandNode 303 = 我方步兵突击队模板，
    # 与 matchmaking.AI_ASSAULT_TEAMS / 战局详情 f4 同源；mapEntityId 用
    # mission_info f5 同款 WarDatabase battlefield 真实行 id）。
    at_count = 0
    layout = _accesspoint_layout()
    home_ap = int(layout[0][0]) if layout else 0
    for s in matchmaking.PMS_TO_SESSION.values():
        at = b''
        at += pb_int64(1, int(s.get('battle_session_id', 0) or 0))  # assaultTeamId
        at += pb_int64(2, int(s.get('player_id', 0) or 0))          # ownerPlayerId
        at += pb_string(3, s.get('gamertag') or 'Player')           # ownerGamertag
        at += pb_int64(4, int(s.get('faction', 3) or 3))            # factionId(runtime)
        at += pb_int64(5, 303)                                      # commandNodeTemplateId
        at += pb_int64(6, matchmaking.MISSION_MAP_ENTITY_ID)        # mapEntityId
        at += pb_int64(7, 1)                                        # mapEntityTypeId(1=battlefield)
        at += pb_bool(8, False)                                     # inTransit
        at += pb_bool(14, True)                                     # inActiveMission
        at += pb_int64(15, home_ap)                                 # accesspointTemplateId
        resp_proto += pb_message(5, at)
        at_count += 1

    resp = build_response(cmd_id, 'ActiveMissionRequestEventListResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'GAME S->C cmd={cmd_id:<4} ActiveMissionRequestEventListResponse '
        f'(mission={mission_id}, resources={len(res_msgs)}, '
        f'accesspoints={ap_count}, assaultTeams={at_count}, {len(resp_proto)}B)')


# ---------------------------------------------------------------- 统计上报

async def handle_add_general_stats(sess, writer, cmd_id, req_type, proto):
    """AddGeneralStats（statrequests 族）→ SR{response=1}。

    实测载荷（21:01:06, 1228B）：f1 ip="0.0.0.0" f2 username="@server"
    f3 action="MissionStart" f4 repeated extradata（长文本 metrics）。
    username 可解析到库内账号时按 general_<action> 计数写 character_stats，
    否则仅记日志。"""
    try:
        items = _split_fields(proto)
        ip = _as_str(_get(items, 1))
        username = _as_str(_get(items, 2))
        action = _as_str(_get(items, 3))
        extra = _all(items, 4)
    except Exception as e:
        log(f'      [combat] AddGeneralStats parse error: {e}')
        ip = username = action = ''
        extra = []

    log(f'      [combat] AddGeneralStats action={action!r} user={username!r} '
        f'ip={ip!r} extradata={len(extra)} entries '
        f'({len(extra[0]) if extra else 0}B first)')

    # username → 玩家 → 最新角色（"@server" 等服务端自报不落库）
    if STORE is not None and username and not username.startswith('@'):
        try:
            pid = await STORE.get_player(username)
            if pid is not None:
                chars = await STORE.list_characters(int(pid))
                if chars:
                    await STORE.add_stat(int(chars[-1]['id']),
                                         f'general_{action or "event"}', 1)
        except Exception as e:
            log(f'      [DB] add_stat(general) failed: {e}')

    resp = build_response(cmd_id, 'SR', pb_int32(1, 1))
    writer.write(resp)
    await writer.drain()
    log(f'GAME S->C cmd={cmd_id:<4} SR (response=1, action={action!r})')


async def handle_career_order_action_game(sess, writer, cmd_id, req_type, proto):
    """CareerOrderActionGameRequest → SR{response=1} + career_orders 进度累加。

    实测载荷（02:51:17, 12B）：f1 playerMissionSessionId（=battle_session_id
    大数）f2 careerOrderId（实测 22）。ZServer 未注册 CareerOrderResponse，
    回 SR（注册内通用 ack，完成 pending wait）。"""
    try:
        items = _split_fields(proto)
        msid = _get(items, 1)
        order_id = _get(items, 2)
    except Exception as e:
        log(f'      [combat] CareerOrderActionGame parse error: {e}')
        msid = order_id = 0

    mm_sess = _session_by_msid(msid)
    pid = int(mm_sess['player_id']) if mm_sess else 0
    log(f'      [combat] CareerOrderActionGame order={order_id} '
        f'msid={msid} player={pid}')

    if pid and STORE is not None:
        try:
            progress, status = await STORE.upsert_career_order_progress(
                pid, int(order_id or 0))
            log(f'      [DB] career order {order_id}: progress={progress} '
                f'status={status} (player={pid})')
        except Exception as e:
            log(f'      [DB] upsert_career_order_progress failed: {e}')

    resp = build_response(cmd_id, 'SR', pb_int32(1, 1))
    writer.write(resp)
    await writer.drain()
    log(f'GAME S->C cmd={cmd_id:<4} SR (response=1, careerOrder={order_id})')


# ---------------------------------------------------------------- 战局中上报

async def handle_report_mission_status(sess, writer, cmd_id, req_type, proto):
    """ReportMissionStatusRequest（战局中每 10s）→ HandlerErrorResponse。

    §8.1：f1 missionSessionId / f2 资源消耗 / f3 击杀记录 / f4 据点归属 /
    f5 小队资源 / f6 成就。f4 写 _CAPTURE_OWNERS（覆盖 ActiveMissionResponse
    f3 归属）；f3 击杀按 playerId→会话角色 写 character_stats('kills')。
    """
    try:
        items = _split_fields(proto)
    except Exception as e:
        log(f'      [combat] ReportMissionStatus parse error: {e}')
        items = []

    msid = _get(items, 1)

    # f4 据点归属：ReportCapturepointStatus { f1 capturepointTemplateId, f2 ownerFactionId }
    for raw in _all(items, 4):
        sub = _split_fields(raw)
        tmpl = int(_get(sub, 1, 0) or 0)
        owner = int(_get(sub, 2, 0) or 0)
        if tmpl:
            _CAPTURE_OWNERS[tmpl] = owner
            log(f'      [combat] capturepoint {tmpl} -> faction {owner}')

    # f3 击杀：PlayerKillRecords { f1 playerId, f2 squadId, f3 accesspointTemplateId,
    #   f4 characterTemplateId, f5 repeated PlayerKillTuple{f1 resCat, f2 killerResCat, f3 killCount} }
    kill_total = 0
    for raw in _all(items, 3):
        sub = _split_fields(raw)
        killer_pid = int(_get(sub, 1, 0) or 0)
        n_kills = 0
        for tup_raw in _all(sub, 5):
            tup = _split_fields(tup_raw)
            n_kills += int(_get(tup, 3, 0) or 0)
        kill_total += n_kills
        if n_kills and STORE is not None:
            mm_sess = _session_by_player(killer_pid)
            if mm_sess:
                try:
                    await STORE.add_stat(int(mm_sess['char_id']), 'kills', n_kills)
                except Exception as e:
                    log(f'      [DB] add_stat(kills) failed: {e}')

    n_res = len(_all(items, 2)) + len(_all(items, 5))
    if msid or kill_total or n_res:
        log(f'      [combat] ReportMissionStatus msid={msid} kills={kill_total} '
            f'resources={n_res} captures_tracked={len(_CAPTURE_OWNERS)}')

    await _ack_error(writer, cmd_id, 'ReportMissionStatus acknowledged')


async def handle_player_joined(sess, writer, cmd_id, req_type, proto):
    """PlayerJoined { f1 playerMissionSessionId } → HandlerErrorResponse。"""
    try:
        items = _split_fields(proto)
        msid = _get(items, 1)
    except Exception:
        msid = 0
    mm_sess = _session_by_msid(msid)
    log(f'      [combat] PlayerJoined msid={msid} -> '
        f'{mm_sess["gamertag"] if mm_sess else "unknown session"}')
    await _ack_error(writer, cmd_id, 'PlayerJoined acknowledged')


async def handle_player_exit_batch(sess, writer, cmd_id, req_type, proto):
    """PlayerExitBatch { f1 repeated PlayerExit } → HandlerErrorResponse。

    PlayerExit { f1 playerMissionSessionId, f2 yellowGriefPoints,
    f3 redGriefPoints, f4 exitType, f5 missionState, f6 battleEyeKickReason }。
    grief/exitType 落 character_stats（按 msid→会话角色）。"""
    exits = []
    try:
        items = _split_fields(proto)
        for raw in _all(items, 1):
            sub = _split_fields(raw)
            exits.append({
                'msid': int(_get(sub, 1, 0) or 0),
                'yellow': int(_get(sub, 2, 0) or 0),
                'red': int(_get(sub, 3, 0) or 0),
                'exit_type': int(_get(sub, 4, 0) or 0),
                'mission_state': int(_get(sub, 5, 0) or 0),
                'reason': _as_str(_get(sub, 6, b'')),
            })
    except Exception as e:
        log(f'      [combat] PlayerExitBatch parse error: {e}')

    for ex in exits:
        log(f'      [combat] PlayerExit msid={ex["msid"]} exitType={ex["exit_type"]} '
            f'missionState={ex["mission_state"]} grief=Y{ex["yellow"]}/R{ex["red"]} '
            f'reason={ex["reason"]!r}')
        mm_sess = _session_by_msid(ex['msid'])
        if mm_sess and STORE is not None and (ex['yellow'] or ex['red']):
            try:
                cid = int(mm_sess['char_id'])
                if ex['yellow']:
                    await STORE.add_stat(cid, 'yellow_grief_points', ex['yellow'])
                if ex['red']:
                    await STORE.add_stat(cid, 'red_grief_points', ex['red'])
            except Exception as e:
                log(f'      [DB] add_stat(grief) failed: {e}')
        # ★ 离开战局链路（StatusMediator.onCancelButtonConfirmedClicked）：
        #   客户端关 hng.exe 后 shouldAbandonGameOnSessionUpdate=true，等待
        #   PlayerMissionSession KVCS UPDATE connectionState=2 (Admitted) 才发
        #   AbandonMissionRequest；不推则客户端 UI 永卡"处理中"。
        #   真实客户端是双连接：PlayerExitBatch 由 hng.exe 游戏连接上报
        #   （playerview_cmd=None），必须按玩家名查 FLASH_SESSIONS 注册表
        #   找到 Flash 连接再推（ZServer/游戏连接禁推 KVCS）；
        #   单连接模拟（验证脚本）时请求连接本身就是 Flash 连接。
        if mm_sess:
            flash = None
            if (sess.playerview_cmd is not None
                    and not getattr(sess, 'is_combat_server', False)
                    and mm_sess.get('gamertag') == (sess.username or '')):
                flash = sess
            else:
                cand = FLASH_SESSIONS.get(mm_sess.get('gamertag') or '')
                if cand is not None and cand.playerview_cmd is not None:
                    flash = cand
            if flash is not None:
                await _push_pms_admitted(flash, flash.writer, mm_sess)
            else:
                log(f'      [combat] leave-battle: no live Flash session for '
                    f'{mm_sess.get("gamertag")!r}; PMS Admitted NOT pushed')

    await _ack_error(writer, cmd_id, f'PlayerExitBatch x{len(exits)} acknowledged')


async def _push_pms_admitted(sess, writer, mm_sess):
    """推 PlayerMissionSession KVCS UPDATE connectionState=2 (Admitted)。

    ActionGameService.onPlayerMissionSessionChanges：UPDATE 且 state 回到
    Admitted → 触发 abandonActionGameSignal → 客户端发 AbandonMissionRequest
    （mission.py 回 response=1 后再推 PMS DELETE 收尾回大厅）。"""
    pms_id = int(mm_sess.get('pms_id', 0) or 0)
    pid = int(mm_sess.get('player_id', 0) or 0)
    char_id = int(mm_sess.get('char_id', 0) or 0)
    mission_id = int(matchmaking.PMS_TO_MISSION.get(pms_id, 0) or 0)
    gamertag = mm_sess.get('gamertag') or 'Player'
    faction = int(mm_sess.get('faction', 3) or 3)
    char_name, rank = gamertag, 0
    if STORE is not None and pid and char_id:
        try:
            ch = await STORE.get_character(pid, char_id)
            if ch:
                char_name = ch['name']
                rank = _rank_from_combat_xp(int(ch.get('combat_xp', 0) or 0))
        except Exception as e:
            log(f'      [DB] get_character for PMS push failed: {e}')
    try:
        msg = matchmaking._msg_player_mission_session(
            pms_id, pid, gamertag, char_id, faction, char_name, rank,
            mission_id, matchmaking._zulu_now(), _GAME_CONNECT,
            metrics_uid=mm_sess.get('metrics_uid', ''),
            metrics_session_id=int(mm_sess.get('metrics_session_id', 0) or 0),
            metrics_device_id=matchmaking.METRICS_DEVICE_ID,
            platform='HnG', connection_state=2,
            battle_session_id=int(mm_sess.get('battle_session_id', 0) or 0))
        await matchmaking._push_kvcs(writer, sess.playerview_cmd,
                                     b'PlayerMissionSession', msg)
        log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet '
            f'(PlayerMissionSession id={pms_id}, mission={mission_id}, '
            f'state=2 Admitted ★leave-battle abandon trigger)')
    except Exception as e:
        log(f'      [combat] PMS Admitted push failed: {e}')


async def handle_report_character_spawned(sess, writer, cmd_id, req_type, proto):
    """ReportCharacterSpawned（实测 2B：f2 varint 1/2/3）→ HandlerErrorResponse。"""
    try:
        items = _split_fields(proto)
        note = ' '.join(f'f{f}={_as_str(v) if isinstance(v, bytes) else v}'
                        for f, _wt, v in items)
    except Exception:
        note = 'unparsed'
    log(f'      [combat] ReportCharacterSpawned {note}')
    await _ack_error(writer, cmd_id, 'ReportCharacterSpawned acknowledged')


async def handle_report_mission_locked(sess, writer, cmd_id, req_type, proto):
    """ReportMissionLocked（实测 2B：f2 varint 1）→ HandlerErrorResponse。"""
    try:
        items = _split_fields(proto)
        locked = _get(items, 2)
    except Exception:
        locked = -1
    log(f'      [combat] ReportMissionLocked state={locked}')
    await _ack_error(writer, cmd_id, 'ReportMissionLocked acknowledged')


async def handle_report_squad_state(sess, writer, cmd_id, req_type, proto):
    """ReportSquadState（实测 12B：f1 squadId 大数, f2 state=2）→ HandlerErrorResponse。"""
    try:
        items = _split_fields(proto)
        squad_id = _get(items, 1)
        state = _get(items, 2)
    except Exception:
        squad_id = state = 0
    log(f'      [combat] ReportSquadState squad={squad_id} state={state}')
    await _ack_error(writer, cmd_id, 'ReportSquadState acknowledged')


async def handle_report_defeated_faction(sess, writer, cmd_id, req_type, proto):
    """ReportDefeatedFaction（实测 2B：f2 varint 3 = 战败方）→ HandlerErrorResponse。"""
    try:
        items = _split_fields(proto)
        defeated = _get(items, 2)
    except Exception:
        defeated = 0
    _LAST_WINNER['defeated'] = int(defeated)
    log(f'      [combat] ReportDefeatedFaction faction={defeated}')
    await _ack_error(writer, cmd_id, 'ReportDefeatedFaction acknowledged')


async def handle_report_eac_kick(sess, writer, cmd_id, req_type, proto):
    """ReportEacKick（实测 89B：f1 reason f2 gamertag f3 ip...）→ HandlerErrorResponse。"""
    try:
        items = _split_fields(proto)
        parts = [f'f{f}={_as_str(v) if isinstance(v, bytes) else v}'
                 for f, _wt, v in items[:6]]
    except Exception:
        parts = ['unparsed']
    log(f'      [combat] ReportEacKick {", ".join(parts)}')
    await _ack_error(writer, cmd_id, 'ReportEacKick acknowledged')


async def handle_report_server_shutdown(sess, writer, cmd_id, req_type, proto):
    """ReportServerShutdown（实测 0B）→ HandlerErrorResponse；战斗服退出，
    清空本局据点归属覆盖表（下局从 BattleFieldSettings 布局重新开始）。"""
    _CAPTURE_OWNERS.clear()
    log('      [combat] ReportServerShutdown (capture owners cleared)')
    await _ack_error(writer, cmd_id, 'ReportServerShutdown acknowledged')


async def handle_announce_battlefield_winner(sess, writer, cmd_id, req_type, proto):
    """AnnounceBattleFieldWinner（实测 2B：f2 varint 3 = 胜方）→ HandlerErrorResponse。"""
    try:
        items = _split_fields(proto)
        winner = _get(items, 2)
    except Exception:
        winner = 0
    _LAST_WINNER['faction'] = int(winner)
    log(f'      [combat] AnnounceBattleFieldWinner faction={winner}')
    await _ack_error(writer, cmd_id, 'AnnounceBattleFieldWinner acknowledged')


# ---------------------------------------------------------------- 战局结算

async def handle_announce_battlefield_earnings(sess, writer, cmd_id, req_type, proto):
    """AnnounceBattleFieldEarningsRequest（结算）→ AnnounceBattleFieldEarningsResponse。

    §8.2：f1 missionId / f2 missionSessionId / f3 missionPresetId /
    f4 BattleReport / f5 repeated PlayerBattleResult。
    BattleReport { f1 missionSessionId, f2 defenderFactionId, f3 winnerFactionId,
      f4 missionTimeInSeconds, f5 battleStartTime, f6 repeated
      BattleReportFaction{f1 factionId, f2 repeated BattleReportPlayer}, f7 capturepoints }
    BattleReportPlayer { f1 playerId, f4 score, f5 captures, f6 kills, f7 deaths,
      f8 squadName, f9 gamertag, f10 isBot, f11 repeated BattleReportCharacter }
    BattleReportCharacter { f1 characterId, f2 startXp, f3 endXp, f10 creditEarnings,
      f11 warfundEarnings, f13 ribbons }
    PlayerBattleResult { f1 playerId, f2 creditsEarned, f3 warfundsEarned,
      f4 skillScore, f5 playerScore, f6 repeated CharacterBattleResult{f1 characterId,
      f2 totalCharacterXp, f3 playerDamage, f4 vehicleDamage, f5 capturepointsCaptured,
      f6 capturepointsNeutralized, f7 timeActive, f9 repeated CharacterRibbon{f1
      ribbonTemplateId, f2 ribbonLevel}} }

    DB 落点：battle_reports + battle_report_players（按 missionSessionId 幂等）、
    characters.combat_xp += (endXp-startXp)、character_ribbon_xp（达成等级取阈值
    下限）、players.credits/warfunds += 结算收益、character_stats(kills/deaths/
    score/captures/damage)。响应恒 response=1（DB 异常仅记日志不阻塞客户端）。
    """
    try:
        items = _split_fields(proto)
    except Exception as e:
        log(f'      [combat] AnnounceBattleFieldEarnings parse error: {e}')
        items = []

    mission_id = int(_get(items, 1, 0) or 0)
    mission_session_id = int(_get(items, 2, 0) or 0)
    mission_preset_id = int(_get(items, 3, 0) or 0)

    # ---- BattleReport（f4）
    br_players = []          # save_battle_report 的 players 行
    report = {
        'mission_session_id': mission_session_id,
        'mission_id': mission_id,
        'mission_preset_id': mission_preset_id,
        'winner_faction_id': 0, 'defender_faction_id': 0,
        'mission_time_seconds': 0, 'battle_start_time': 0,
    }
    br_raw = _get(items, 4, b'')
    if isinstance(br_raw, (bytes, bytearray)) and br_raw:
        br = _split_fields(br_raw)
        report['mission_session_id'] = int(_get(br, 1, mission_session_id) or 0)
        report['defender_faction_id'] = int(_get(br, 2, 0) or 0)
        report['winner_faction_id'] = int(_get(br, 3, 0) or 0)
        report['mission_time_seconds'] = int(_get(br, 4, 0) or 0)
        report['battle_start_time'] = int(_get(br, 5, 0) or 0)
        for fac_raw in _all(br, 6):                  # BattleReportFaction
            fac = _split_fields(fac_raw)
            faction_id = int(_get(fac, 1, 0) or 0)
            for p_raw in _all(fac, 2):               # BattleReportPlayer
                p = _split_fields(p_raw)
                chars = []
                for c_raw in _all(p, 11):            # BattleReportCharacter
                    c = _split_fields(c_raw)
                    chars.append({
                        'characterId': int(_get(c, 1, 0) or 0),
                        'startXp': int(_get(c, 2, 0) or 0),
                        'endXp': int(_get(c, 3, 0) or 0),
                        'creditEarned': int(_get(c, 10, 0) or 0),
                        'warfundEarned': int(_get(c, 11, 0) or 0),
                        'ribbons': [int(_get(_split_fields(r), 1, 0) or 0)
                                    for r in _all(c, 13)],
                        'factionId': faction_id,
                    })
                br_players.append({
                    'player_id': int(_get(p, 1, 0) or 0),
                    'score': int(_get(p, 4, 0) or 0),
                    'captures': int(_get(p, 5, 0) or 0),
                    'kills': int(_get(p, 6, 0) or 0),
                    'deaths': int(_get(p, 7, 0) or 0),
                    'squad_name': _as_str(_get(p, 8, b'')),
                    'gamertag': _as_str(_get(p, 9, b'')),
                    'is_bot': bool(_get(p, 10, 0)),
                    'characters': chars,
                })

    # ---- PlayerBattleResult（f5）：货币 + 角色战斗明细
    currency_ops = []       # (player_id, credits, warfunds)
    char_results = []       # (player_id, char_dict)
    for pr_raw in _all(items, 5):
        pr = _split_fields(pr_raw)
        pr_pid = int(_get(pr, 1, 0) or 0)
        credits = int(_get(pr, 2, 0) or 0)
        warfunds = int(_get(pr, 3, 0) or 0)
        if pr_pid:
            currency_ops.append((pr_pid, credits, warfunds))
        for c_raw in _all(pr, 6):                    # CharacterBattleResult
            c = _split_fields(c_raw)
            entry = {
                'characterId': int(_get(c, 1, 0) or 0),
                'totalCharacterXp': int(_get(c, 2, 0) or 0),
                'playerDamage': int(_get(c, 3, 0) or 0),
                'vehicleDamage': int(_get(c, 4, 0) or 0),
                'capturepointsCaptured': int(_get(c, 5, 0) or 0),
                'capturepointsNeutralized': int(_get(c, 6, 0) or 0),
                'timeActive': int(_get(c, 7, 0) or 0),
                'ribbons': [(int(_get(_split_fields(r), 1, 0) or 0),
                             int(_get(_split_fields(r), 2, 0) or 0))
                            for r in _all(c, 9)],
            }
            char_results.append((pr_pid, entry))

    log(f'      [combat] AnnounceBattleFieldEarnings mission={mission_id} '
        f'msid={mission_session_id} preset={mission_preset_id} '
        f'winner={report["winner_faction_id"]} '
        f'time={report["mission_time_seconds"]}s '
        f'players={len(br_players)} results={len(char_results)}')

    # ---- DB 持久化（全部 best-effort）
    if STORE is not None:
        try:
            report_id = await STORE.save_battle_report(report, br_players)
            log(f'      [DB] battle_report saved id={report_id} '
                f'(msid={report["mission_session_id"]}, '
                f'{len(br_players)} players)')
        except Exception as e:
            log(f'      [DB] save_battle_report failed: {e}')

        for pid, credits, warfunds in currency_ops:
            if not (credits or warfunds):
                continue
            try:
                cur = await STORE.add_currency(pid, credits=credits, warfunds=warfunds)
                log(f'      [DB] earnings player={pid} +{credits}cr +{warfunds}wf '
                    f'-> {cur["credits"]}cr/{cur["warfunds"]}wf')
            except Exception as e:
                log(f'      [DB] add_currency({pid}) failed: {e}')

        for p in br_players:
            pid = p['player_id']
            if not pid:
                continue
            try:
                for ch in p['characters']:
                    xp_delta = max(0, ch['endXp'] - ch['startXp'])
                    if xp_delta:
                        await STORE.add_combat_xp(pid, ch['characterId'], xp_delta)
                        log(f'      [DB] char {ch["characterId"]} +{xp_delta} xp')
                if p['kills'] or p['deaths'] or p['score'] or p['captures']:
                    target = p['characters'][0]['characterId'] if p['characters'] else 0
                    if not target:
                        mm_sess = _session_by_player(pid)
                        target = int(mm_sess['char_id']) if mm_sess else 0
                    if target:
                        if p['kills']:
                            await STORE.add_stat(target, 'kills', p['kills'])
                        if p['deaths']:
                            await STORE.add_stat(target, 'deaths', p['deaths'])
                        if p['score']:
                            await STORE.add_stat(target, 'score', p['score'])
                        if p['captures']:
                            await STORE.add_stat(target, 'captures', p['captures'])
            except Exception as e:
                log(f'      [DB] battle player stats (pid={pid}) failed: {e}')

        for pid, ch in char_results:
            if not pid or not ch['characterId']:
                continue
            try:
                if ch['playerDamage']:
                    await STORE.add_stat(ch['characterId'], 'player_damage',
                                         ch['playerDamage'])
                if ch['vehicleDamage']:
                    await STORE.add_stat(ch['characterId'], 'vehicle_damage',
                                         ch['vehicleDamage'])
                if ch['capturepointsCaptured']:
                    await STORE.add_stat(ch['characterId'], 'captures',
                                         ch['capturepointsCaptured'])
                if ch['timeActive']:
                    await STORE.add_stat(ch['characterId'], 'time_active',
                                         ch['timeActive'])
                # 勋带达成（templateId, level）→ XP 补到该等级阈值下限
                for ribbon_id, ribbon_lv in ch['ribbons']:
                    if ribbon_id and ribbon_lv:
                        floor_xp = _RIBBON_LEVEL_XP.get(int(ribbon_lv), 0)
                        if floor_xp:
                            await STORE.set_ribbon_xp_floor(
                                pid, ch['characterId'], int(ribbon_id), floor_xp)
            except Exception as e:
                log(f'      [DB] char result stats (pid={pid}) failed: {e}')

    # 战局结束：清空据点归属覆盖（下局从布局重新开始）
    _CAPTURE_OWNERS.clear()

    resp_proto = pb_int64(1, mission_session_id)     # f1 missionSessionId
    resp_proto += pb_int32(2, 1)                     # f2 response=1
    resp = build_response(cmd_id, 'AnnounceBattleFieldEarningsResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'GAME S->C cmd={cmd_id:<4} AnnounceBattleFieldEarningsResponse '
        f'(msid={mission_session_id}, response=1)')


# ---------------------------------------------------------------- 注册表

HANDLERS = {
    'ActiveMissionRequestEventList': handle_active_mission_event_list,
    'AddGeneralStats': handle_add_general_stats,
    'CareerOrderActionGameRequest': handle_career_order_action_game,
    'ReportMissionStatusRequest': handle_report_mission_status,
    'AnnounceBattleFieldEarningsRequest': handle_announce_battlefield_earnings,
    'PlayerJoined': handle_player_joined,
    'PlayerExitBatch': handle_player_exit_batch,
    'ReportCharacterSpawned': handle_report_character_spawned,
    'ReportMissionLocked': handle_report_mission_locked,
    'ReportSquadState': handle_report_squad_state,
    'ReportDefeatedFaction': handle_report_defeated_faction,
    'ReportEacKick': handle_report_eac_kick,
    'ReportServerShutdown': handle_report_server_shutdown,
    'AnnounceBattleFieldWinner': handle_announce_battlefield_winner,
}
