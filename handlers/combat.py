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

import struct
import time
import zlib

import matchmaking
from protocol import (
    build_response,
    pb_bool,
    pb_int32,
    pb_int64,
    pb_message,
    pb_string,
)
from messages import _msg_character, _push_kvcs
from player_service import _char_unlock_bits_for
from state import FLASH_SESSIONS, PUBLIC_HOST, STORE, log
from wardb import (
    _RIBBON_LEVEL_XP,
    _rank_from_combat_xp,
    ammo_wear_params,
    commandnode_template,
    vehicle_upgrade_wear_params,
    vehicle_wear_params_for_variant,
    weapon_modifier_wear_params,
    weapon_wear_params,
)
from warserver_messages import _accesspoint_layout, _msg_army_resource_list

# career 只依赖 protocol/state/messages/player_service/wardb，无回指 combat
from .career import _ACTIVE_SEASON as _BP_ACTIVE_SEASON

# 与 matchmaking 成局 game_connect 同源（PUBLIC_HOST；mission.py _GAME_CONNECT）
_GAME_CONNECT = f'{PUBLIC_HOST}:5678'


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


def _earnings_value(raw):
    """BattleReportEarnings 嵌套消息 → valueEarned（f1；f2-f7 加成实测恒 0）。

    BattleReportCharacter f10 creditEarnings / f11 warfundEarnings 是嵌套消息
    （BattleReportEarnings，0x1807b3e58），不是整数——2026-08-26 结算崩溃
    ValueError 即 int(bytes) 所致。"""
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        return 0
    try:
        return int(_get(_split_fields(raw), 1, 0) or 0)
    except Exception:
        return 0


def _report_ribbon_end(br_players):
    """(playerId, characterId) → {ribbonTemplateId: endValue}（f13 战后勋带 XP）。"""
    out = {}
    for p in br_players:
        pid = int(p.get('player_id', 0) or 0)
        for ch in p.get('characters') or []:
            ends = {}
            for rb in ch.get('ribbons') or []:
                if isinstance(rb, dict) and rb.get('id'):
                    ends[int(rb['id'])] = int(rb.get('end', 0) or 0)
            if ends:
                out[(pid, int(ch.get('characterId', 0) or 0))] = ends
    return out


def _session_by_player(player_id, mission_id=None):
    """playerId → 最新匹配会话（PMS_TO_SESSION 值序 = 进局序）。

    mission_id 给定时只在该局会话里反查：bot 的 playerId 是小整数、
    可能与真实账号 id 撞号（实测 bot 3 ≠ 玩家 3），全局反查会把
    bot 的击杀记到真实账号名下；圈定本局后不在名单 = bot → None。"""
    found = None
    for s in matchmaking.PMS_TO_SESSION.values():
        if int(s.get('player_id', 0) or 0) != int(player_id):
            continue
        if mission_id and int(s.get('mission_id', 0) or 0) != int(mission_id):
            continue
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

    # ★ 按本局 mission 取 preset/阵营（与 BattleFieldSettings 握手同源）：
    #   不传参时两个 builder 默认 preset 10（Mill 模板 8/15）——换图后战斗服
    #   校验 "New Resource Invalid accesspoint for resource"（m_AccesspointId
    #   是 Mill 的 8/15 而本图无此点）。
    #   实测战斗服本请求 mission_id=0（proto 无 field1，与握手同款）：
    #   用握手时记在连接上的 sess.combat_mission 定位本局。
    # 解析本局 mission id：proto f1 → PMS_TO_MISSION 反查 → 连接记录
    # （战斗服 combat_mission / 玩家游戏进程 game_mission，实测两者轮询均 f1=0）
    mid = int(mission_id or 0)
    det = matchmaking._MM_MISSION_DETAILS.get(mid)
    if not det and mid:
        mid2 = int(matchmaking.PMS_TO_MISSION.get(mid, 0) or 0)
        if mid2:
            mid = mid2
            det = matchmaking._MM_MISSION_DETAILS.get(mid)
    if not det:
        cm = int(getattr(sess, 'combat_mission', 0) or 0) or \
            int(getattr(sess, 'game_mission', 0) or 0)
        if cm:
            mid = cm
            det = matchmaking._MM_MISSION_DETAILS.get(cm)
    if det and 'preset' in det:
        # ★ 阵营字段无默认值：_faction_pair 校验（损坏 → None + ERROR 日志），
        #   不静默用 2/GE、3/SU 默认（曾致错阵营布局）。校验不过走
        #   builder 默认路径（与"det 缺失"同款降级）。
        fp = matchmaking._faction_pair(det)
        if fp is None:
            mm_preset = None
            mm_defender = None
            mm_attacker = None
        else:
            mm_preset = int(det['preset'])
            mm_attacker, mm_defender = fp
    else:
        mm_preset = None      # 走 builder 默认（兼容旧路径）
        mm_defender = None
        mm_attacker = None

    def _layout_args():
        if mm_preset is None:
            return ()
        return (mm_preset, mm_defender, mm_attacker)

    resp_proto = pb_string(1, '')                    # f1 msg
    # f2 资源：从本局资源池渲染（与 f14 握手 / 匹配界面 f5 同源同值，
    # ReportMissionStatusRequest f2 出生上报扣减后此处轮询即递减）
    res_msgs = _msg_army_resource_list(*_layout_args(),
                                       counts_by_faction=matchmaking.
                                       mission_resource_pool(mid))
    for r in res_msgs:                               # f2 armyResourceList
        resp_proto += pb_message(2, r)

    layout = _accesspoint_layout(*_layout_args())
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

    # f5 assaultTeams：每局一个突击队（commandNode 模板按玩家阵营查
    # WarDatabase/commandnodetemplate：infantry US=103/GE=203/SU=303；
    # 与 matchmaking._assault_team_nodes / 战局详情 f4 同源；
    # mapEntityId 用 mission_info f5 同款 WarDatabase battlefield 真实行 id）。
    at_count = 0
    home_ap = int(layout[0][0]) if layout else 0
    for s in matchmaking.PMS_TO_SESSION.values():
        fac = int(s.get('faction', 3) or 3)
        at = b''
        at += pb_int64(1, int(s.get('battle_session_id', 0) or 0))  # assaultTeamId
        at += pb_int64(2, int(s.get('player_id', 0) or 0))          # ownerPlayerId
        at += pb_string(3, s.get('gamertag') or 'Player')           # ownerGamertag
        at += pb_int64(4, fac)                                      # factionId(runtime)
        at += pb_int64(5, commandnode_template(fac, 'infantry'))  # commandNodeTemplateId
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
    f5 小队资源 / f6 成就。f2 statuslist（ReportArmyResourceSpawnCount
    {f1 ap, f2 cat, f5 count, f6 faction}，每 10s 增量上报，AI/玩家出生均含）
    按兵种扣减每局资源池 → f5/f2/f14 同步递减；f4 写 _CAPTURE_OWNERS
    （覆盖 ActiveMissionResponse f3 归属）；f3 击杀按 playerId→会话角色
    写 character_stats('kills')。
    """
    try:
        items = _split_fields(proto)
    except Exception as e:
        log(f'      [combat] ReportMissionStatus parse error: {e}')
        items = []

    msid = _get(items, 1)

    # f2 资源消耗 → 扣每局资源池（mission：msid→会话→PMS 反查，回退连接记录）
    mm_sess = _session_by_msid(msid) if msid else None
    mid = int(matchmaking.PMS_TO_MISSION.get(
        int(mm_sess.get('pms_id', 0) or 0), 0) or 0) if mm_sess else 0
    if not mid:
        mid = int(getattr(sess, 'combat_mission', 0) or 0) or \
            int(getattr(sess, 'game_mission', 0) or 0)
    spawned_total = 0
    for raw in _all(items, 2):
        sub = _split_fields(raw)
        cat = int(_get(sub, 2, 0) or 0)
        cnt = int(_get(sub, 5, 0) or 0)
        fac = int(_get(sub, 6, 0) or 0)
        if not (mid and cat and cnt and fac):
            continue
        left = matchmaking.consume_mission_resource(mid, fac, cat, cnt)
        spawned_total += cnt
        if left is not None:
            log(f'      [combat] resource spawn: faction={fac} cat={cat} '
                f'count={cnt} -> pool left={left}')

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
            # ★ bot 撞号过滤：killer 不在本局（mid）会话名单 → bot →
            #   不给真实账号记击杀（f3 无 is_bot 字段，只能按 roster 判定）
            mm_sess = _session_by_player(killer_pid, mission_id=mid)
            if mm_sess:
                try:
                    await STORE.add_stat(int(mm_sess['char_id']), 'kills', n_kills)
                except Exception as e:
                    log(f'      [DB] add_stat(kills) failed: {e}')
            else:
                log(f'      [combat] kills x{n_kills} by pid={killer_pid} '
                    f'skipped (not in mission {mid} roster, likely bot)')

    n_res = len(_all(items, 2)) + len(_all(items, 5))
    if msid or kill_total or n_res:
        log(f'      [combat] ReportMissionStatus msid={msid} kills={kill_total} '
            f'spawned={spawned_total} resources={n_res} '
            f'captures_tracked={len(_CAPTURE_OWNERS)}')

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
    """ReportCharacterSpawned（实测 2B：f2 varint 1/2/3）→ HandlerErrorResponse。

    仅通知类；资源池扣减不走此消息（用 ReportMissionStatusRequest f2
    statuslist，含阵营/兵种/数量，见 handle_report_mission_status），避免双扣。"""
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


async def handle_set_player_settings(sess, writer, cmd_id, req_type, proto):
    """SetPlayerSettings（游戏进程对局内上报，远端实测 19B）→ HandlerErrorResponse。

    实包锚点（2026-08-31 远端日志 ×38）：f1 playerMissionSessionId、
    f5 fixed32 float（观测恒 0.4）、f7/f8 varint 0/1。
    请求描述符未逆向（字段号与 PlayerSettings 响应 40 字段表对不上，
    f5 在响应里是 bool invertmouseturret 而此处是 float），语义确认前
    只结构化日志不落库——此前走 realtest 兜底被整条丢弃。"""
    try:
        items = _split_fields(proto)
        msid = _get(items, 1)
        raw5 = _get(items, 5)
        f5 = struct.unpack('<f', raw5)[0] if isinstance(raw5, (bytes, bytearray)) \
            else None
        f7 = _get(items, 7)
        f8 = _get(items, 8)
    except Exception as e:
        log(f'      [combat] SetPlayerSettings parse error: {e}')
        msid = f5 = f7 = f8 = None
    mm_sess = _session_by_msid(msid) if msid else None
    log(f'      [combat] SetPlayerSettings msid={msid} f5={f5!r} f7={f7} f8={f8}'
        f' -> {mm_sess["gamertag"] if mm_sess else "unknown session"}')
    await _ack_error(writer, cmd_id, 'SetPlayerSettings acknowledged')


async def handle_tutorial_request(sess, writer, cmd_id, req_type, proto):
    """TutorialRequest（战斗内教程步骤上报，远端实测 12B）→ character_tutorials。

    实包锚点（2026-08-31 远端日志 ×33）：f1 playerMissionSessionId、
    f2 tutorialTemplateId（观测恒 26）。按 msid→会话角色记
    character_tutorials(triggered+completed，幂等)——此前被兜底丢弃，
    教程进度每局重置。"""
    try:
        items = _split_fields(proto)
        msid = _get(items, 1)
        tutorial_id = int(_get(items, 2, 0) or 0)
    except Exception as e:
        log(f'      [combat] TutorialRequest parse error: {e}')
        msid = tutorial_id = 0
    mm_sess = _session_by_msid(msid) if msid else None
    log(f'      [combat] TutorialRequest msid={msid} tutorial={tutorial_id} -> '
        f'{mm_sess["gamertag"] if mm_sess else "unknown session"}')
    if mm_sess and tutorial_id and STORE is not None:
        try:
            await STORE.mark_character_tutorial(int(mm_sess['char_id']), tutorial_id)
        except Exception as e:
            log(f'      [DB] mark_character_tutorial failed: {e}')
    await _ack_error(writer, cmd_id, 'TutorialRequest acknowledged')


async def handle_report_accesspoint_faction_change(sess, writer, cmd_id,
                                                   req_type, proto):
    """ReportAccesspointFactionChange（据点易主即时上报，远端实测 5B）。

    实包锚点（2026-08-31 远端日志 ×4）：f2 capturepointTemplateId（观测
    202）、f3 ownerFactionId（观测 3）。与 ReportMissionStatusRequest f4
    捕获上报同源写 _CAPTURE_OWNERS（f4 是 10s 周期全量，本消息是事件级
    即时——覆盖后者两次轮询间隔内的快速易主）。"""
    try:
        items = _split_fields(proto)
        tmpl = int(_get(items, 2, 0) or 0)
        owner = int(_get(items, 3, 0) or 0)
    except Exception as e:
        log(f'      [combat] ReportAccesspointFactionChange parse error: {e}')
        tmpl = owner = 0
    if tmpl:
        _CAPTURE_OWNERS[tmpl] = owner
    log(f'      [combat] accesspoint faction change {tmpl} -> faction {owner}')
    await _ack_error(writer, cmd_id, 'ReportAccesspointFactionChange acknowledged')


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

def _est_shots(time_active):
    """按活跃秒数估算本局射击发数（0.75 发/秒，钳 100~2000）。"""
    return max(100, min(2000, int(time_active * 0.75)))


def _est_spawns(time_active):
    """按活跃秒数估算本局载具出生次数（每 300 秒一次，钳 1~6）。"""
    return max(1, min(6, int(round(time_active / 300.0))))


async def _apply_equipment_wear(char_times):
    """战后维护损耗：按 CharacterBattleResult f7 timeActive 估算，扣减参战
    角色已装备物品的 condition。

    官方磨损模型（客户端 WeaponUtilities/VehicleUtilities + WarDatabase 模板）：
      武器/配件 maxCondition=1.0，每发 wearPerShot；
      载具/载具升级 maxCondition 为点数（25~220），每次 spawn wearPerSpawn；
      弹药 condition 即剩余发数，wearPerShot 即每发消耗。
    战斗服统计只到模板粒度（statrequests.WeaponStats 无实例磨损），只能
    lobby 侧估算。模板磨损全 0（不可维护物品）与已扣到 0 的行跳过；
    仅对参战角色已装备（container_slot / equipped_in_slot / 挂靠已装备
    武器/载具）的物品生效。char_times: {(pid, charId): timeActive}。"""
    for (pid, char_id), t_active in sorted(char_times.items()):
        if not pid or not char_id or t_active <= 0:
            continue
        est_shots = _est_shots(t_active)
        est_spawns = _est_spawns(t_active)
        worn = 0
        try:
            # ---- 武器（装备中：container_id==角色 && container_slot>0）----
            # 主武器槽（slot 1）全额定损，副武器按 30% 估算
            weapons = await STORE.list_weapons(pid, char_id)
            equipped_w = {int(w['id']): (int(w.get('container_slot') or 0), w)
                          for w in weapons
                          if int(w.get('container_id') or 0) == int(char_id)
                          and int(w.get('container_slot') or 0) > 0}
            for wid, (slot, w) in equipped_w.items():
                params = weapon_wear_params(w['template_id'])
                cond = float(w.get('condition') or 0)
                if params and cond > 0 and (params[1] or params[2]):
                    shots = est_shots if slot == 1 else int(est_shots * 0.3)
                    new_cond = max(0.0, cond - params[1] * shots - params[2])
                    if new_cond < cond:
                        await STORE.update_weapon_condition(pid, wid, new_cond)
                        worn += 1

            # ---- 配件/弹药（挂在已装备武器上；弹药须已装备到弹药槽）----
            if equipped_w:
                for m in await STORE.list_weapon_modifiers(pid, char_id):
                    if int(m.get('weapon_id') or 0) not in equipped_w:
                        continue
                    params = weapon_modifier_wear_params(m['template_id'])
                    cond = float(m.get('condition') or 0)
                    if params and cond > 0 and (params[1] or params[2]):
                        slot = equipped_w[int(m['weapon_id'])][0]
                        shots = est_shots if slot == 1 else int(est_shots * 0.3)
                        new_cond = max(0.0, cond - params[1] * shots - params[2])
                        if new_cond < cond:
                            await STORE.update_weapon_modifier_condition(
                                pid, m['id'], new_cond)
                            worn += 1
                for a in await STORE.list_weapon_ammo(pid, char_id):
                    if int(a.get('weapon_id') or 0) not in equipped_w:
                        continue
                    if not int(a.get('slot_index') or 0):
                        continue
                    params = ammo_wear_params(a['ammo_template_id'])
                    cond = float(a.get('condition') or 0)
                    if params and cond > 0 and params[1]:
                        slot = equipped_w[int(a['weapon_id'])][0]
                        shots = est_shots if slot == 1 else int(est_shots * 0.3)
                        new_cond = max(0.0, cond - params[1] * shots)
                        if new_cond < cond:
                            await STORE.update_weapon_ammo_condition(
                                pid, a['id'], new_cond)
                            worn += 1

            # ---- 载具（装备中：character_id==角色 && equipped_in_slot>0）----
            vehicles = await STORE.list_vehicles(pid, char_id)
            equipped_v = {int(v['id']): v for v in vehicles
                          if int(v.get('character_id') or 0) == int(char_id)
                          and int(v.get('equipped_in_slot') or 0) > 0}
            for vid, v in equipped_v.items():
                params = vehicle_wear_params_for_variant(
                    v['vehicle_variant_template_id'])
                cond = float(v.get('condition') or 0)
                if params and cond > 0 and (params[1] or params[2]):
                    new_cond = max(0.0, cond - params[1] * est_spawns - params[2])
                    if new_cond < cond:
                        await STORE.update_vehicle_condition(pid, vid, new_cond)
                        worn += 1

            # ---- 载具升级/载具弹药（装在已装备载具上）----
            if equipped_v:
                for u in await STORE.list_vehicle_upgrades(pid):
                    if int(u.get('vehicle_id') or 0) not in equipped_v:
                        continue
                    if not int(u.get('equipped_slot_id') or 0):
                        continue
                    params = vehicle_upgrade_wear_params(
                        u['upgrade_template_id'])
                    cond = float(u.get('condition') or 0)
                    if params and cond > 0 and (params[1] or params[2]):
                        new_cond = max(0.0,
                                       cond - params[1] * est_spawns - params[2])
                        if new_cond < cond:
                            await STORE.update_vehicle_upgrade_condition(
                                pid, u['id'], new_cond)
                            worn += 1
                for a in await STORE.list_vehicle_ammo(pid):
                    if int(a.get('vehicle_id') or 0) not in equipped_v:
                        continue
                    if not int(a.get('equipped_storage_id') or 0):
                        continue
                    params = ammo_wear_params(a['ammo_template_id'])
                    cond = float(a.get('condition') or 0)
                    if params and cond > 0 and params[1]:
                        new_cond = max(0.0, cond - params[1] * est_shots)
                        if new_cond < cond:
                            await STORE.update_vehicle_ammo_condition(
                                pid, a['id'], new_cond)
                            worn += 1

            if worn:
                log(f'      [wear] player={pid} char={char_id} '
                    f'active={t_active}s shots~{est_shots} spawns~{est_spawns} '
                    f'-> {worn} item(s) worn')
        except Exception as e:
            log(f'      [wear] player={pid} char={char_id} failed: {e}')


async def handle_announce_battlefield_earnings(sess, writer, cmd_id, req_type, proto):
    """AnnounceBattleFieldEarningsRequest（结算）→ AnnounceBattleFieldEarningsResponse。

    §8.2：f1 missionId / f2 missionSessionId / f3 missionPresetId /
    f4 BattleReport / f5 repeated PlayerBattleResult。
    BattleReport { f1 missionSessionId, f2 defenderFactionId, f3 winnerFactionId,
      f4 missionTimeInSeconds, f5 battleStartTime, f6 repeated
      BattleReportFaction{f1 factionId, f2 repeated BattleReportPlayer}, f7 capturepoints }
    BattleReportPlayer { f1 playerId, f4 score, f5 captures, f6 kills, f7 deaths,
      f8 squadName, f9 gamertag, f10 isBot, f11 repeated BattleReportCharacter }
    BattleReportCharacter { f1 characterId, f2 startXp, f3 endXp,
      f10/f11 嵌套 BattleReportEarnings{f1 valueEarned, f2-f7 加成, f8 statValues,
      f9 anyFactionQuickplay}（warserver-battlefield-settings.md §8.2 实测：
      顶层 181b22a036… 的 f10 是 0x08… 嵌套消息，非整数），f13 ribbons }
    PlayerBattleResult { f1 playerId, f2 creditsEarned, f3 warfundsEarned,
      f4 skillScore, f5 playerScore, f6 repeated CharacterBattleResult{f1 characterId,
      f2 totalCharacterXp, f3 playerDamage, f4 vehicleDamage, f5 capturepointsCaptured,
      f6 capturepointsNeutralized, f7 timeActive, f9 repeated CharacterRibbon{f1
      ribbonTemplateId, f2 ribbonLevel}} }

    DB 落点：battle_reports + battle_report_players（按 missionSessionId 幂等）、
    characters.combat_xp += (endXp-startXp)、character_ribbon_xp（达成等级取阈值
    下限）、players.credits/warfunds += 结算收益、character_stats(kills/deaths/
    score/captures/damage)。响应恒 response=1 且必发（解析/DB 异常仅记日志，
    不回滚收尾——否则战斗服 pending wait 超时、大厅卡 rejoin、无结算）。

    战局结束收尾（_finish_mission_sessions）：对局内每名玩家的 Flash 连接推
    solo squad 重置 + PlayerMissionSession DELETE（清 rejoin）+
    Notification(NewBattleReport)（客户端自动 GetBattleReport → 弹出
    MissionBattleReportWindow 结算界面 + 对局记录入托盘）。
    """
    try:
        items = _split_fields(proto)
    except Exception as e:
        log(f'      [combat] AnnounceBattleFieldEarnings parse error: {e}')
        items = []

    mission_id = int(_get(items, 1, 0) or 0)
    mission_session_id = int(_get(items, 2, 0) or 0)
    mission_preset_id = int(_get(items, 3, 0) or 0)
    # 实测 earnings 顶层 f1 missionId 缺省（proto 首字段是 f3 preset）——
    # 用握手时记录在连接上的本局 mission 兜底，否则 release/收尾找不到局
    if not mission_id:
        mission_id = int(getattr(sess, 'combat_mission', 0) or 0) or \
            int(getattr(sess, 'game_mission', 0) or 0)

    # ---- BattleReport（f4）
    br_players = []          # save_battle_report 的 players 行
    report = {
        'mission_session_id': mission_session_id,
        'mission_id': mission_id,
        'mission_preset_id': mission_preset_id,
        'winner_faction_id': 0, 'defender_faction_id': 0,
        'mission_time_seconds': 0, 'battle_start_time': 0,
        'capturepoints': [],
    }
    try:
        br_raw = _get(items, 4, b'')
        if isinstance(br_raw, (bytes, bytearray)) and br_raw:
            br = _split_fields(br_raw)
            report['mission_session_id'] = int(_get(br, 1, mission_session_id) or 0)
            report['defender_faction_id'] = int(_get(br, 2, 0) or 0)
            report['winner_faction_id'] = int(_get(br, 3, 0) or 0)
            report['mission_time_seconds'] = int(_get(br, 4, 0) or 0)
            report['battle_start_time'] = int(_get(br, 5, 0) or 0)
            # f7 capturepoints：结算界面战场态势图（BattlefieldStatusView
            # setBattleReportCapturePointStatuses）逐点着色数据源
            for cp_raw in _all(br, 7):
                cp = _split_fields(cp_raw)
                tmpl = int(_get(cp, 1, 0) or 0)
                if tmpl:
                    report['capturepoints'].append(
                        {'id': tmpl, 'owner': int(_get(cp, 2, 0) or 0)})
            for fac_raw in _all(br, 6):                  # BattleReportFaction
                fac = _split_fields(fac_raw)
                faction_id = int(_get(fac, 1, 0) or 0)
                for p_raw in _all(fac, 2):               # BattleReportPlayer
                    p = _split_fields(p_raw)
                    chars = []
                    for c_raw in _all(p, 11):            # BattleReportCharacter
                        c = _split_fields(c_raw)
                        # f13 ribbons：PlayerBattleReportRibbon{f1 模板id,
                        # f2 startValue, f3 endValue}——战报界面勋带进度条
                        # 直接用这两个值画（客户端 start==end 会跳过不显示）
                        ribbons = []
                        for r_raw in _all(c, 13):
                            rf = _split_fields(r_raw)
                            ribbons.append({
                                'id': int(_get(rf, 1, 0) or 0),
                                'start': int(_get(rf, 2, 0) or 0),
                                'end': int(_get(rf, 3, 0) or 0),
                            })
                        credit = _earnings_value(_get(c, 10, b''))
                        warfund = _earnings_value(_get(c, 11, b''))
                        # f12 xpStatValues：士兵详情"战斗经验明细"列表
                        # （MissionBattleReportDetailedPlayerInformation
                        # setDetailedInfoListItems 按统计分类展示）
                        # ClientBattleStatRequirement{f1 statRequirementId,
                        # f2 statValue, f3 xpValue(fixed32 float)}
                        stats = []
                        for st_raw in _all(c, 12):
                            st = _split_fields(st_raw)
                            st_id = int(_get(st, 1, 0) or 0)
                            if not st_id:
                                continue
                            xp_raw = _get(st, 3, b'')
                            xp_val = struct.unpack('<f', xp_raw)[0] \
                                if isinstance(xp_raw, (bytes, bytearray)) \
                                and len(xp_raw) == 4 else 0.0
                            stats.append({
                                'id': st_id,
                                'value': int(_get(st, 2, 0) or 0),
                                'xp': round(float(xp_val), 2),
                            })
                        chars.append({
                            'characterId': int(_get(c, 1, 0) or 0),
                            'startXp': int(_get(c, 2, 0) or 0),
                            'endXp': int(_get(c, 3, 0) or 0),
                            'creditEarned': credit,
                            'creditVetBonus': credit * 25 // 100,
                            'warfundEarned': warfund,
                            'warfundVetBonus': warfund * 25 // 100,
                            'stats': stats,
                            'ribbons': ribbons,
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
    except Exception as e:
        log(f'      [combat] BattleReport parse error: {e}')

    # 战报无 f7 时（本地战斗服缺省字段）：用 ReportMissionStatusRequest f4
    # 持续上报的 _CAPTURE_OWNERS 终态兜底（须在 clear() 前快照）
    if not report['capturepoints'] and _CAPTURE_OWNERS:
        report['capturepoints'] = [
            {'id': int(t), 'owner': int(o)} for t, o in _CAPTURE_OWNERS.items()]
        log(f'      [combat] capturepoints from status overlay: '
            f'{len(report["capturepoints"])} points')

    # msid=0（本地战斗服不发 BattleReport f1）时合成唯一幂等键：
    # 同局重复结算键值稳定（幂等保留），不同局互不覆盖——否则所有
    # msid=0 战报挤同一行，后一局会把前一局的 players 全部擦掉。
    # 取负数区间，避开真实 msid（大正数）。
    # 键来源：battle_start_time（首选，局唯一）；缺省时用结算内容指纹
    # （时长/胜方/玩家集合），同局重复结算内容一致 → 键一致。
    if not report['mission_session_id']:
        if report['battle_start_time']:
            key = (f"{report['mission_id']}:{report['battle_start_time']}:"
                   f"{report['mission_time_seconds']}")
        else:
            key = (f"{report['mission_id']}:{report['mission_time_seconds']}:"
                   f"{report['winner_faction_id']}:"
                   f"{sorted(p['player_id'] for p in br_players)}:"
                   f"{sum(p['score'] for p in br_players)}")
        report['mission_session_id'] = -(zlib.crc32(key.encode()) or 1)
        log(f'      [combat] msid missing, synthesized '
            f'{report["mission_session_id"]} (mission={report["mission_id"]}, '
            f'start={report["battle_start_time"]})')

    # ---- PlayerBattleResult（f5）：货币 + 角色战斗明细
    currency_ops = []       # (player_id, credits, warfunds)
    char_results = []       # (player_id, char_dict)
    try:
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
    except Exception as e:
        log(f'      [combat] PlayerBattleResult parse error: {e}')

    log(f'      [combat] AnnounceBattleFieldEarnings mission={mission_id} '
        f'msid={mission_session_id} preset={mission_preset_id} '
        f'winner={report["winner_faction_id"]} '
        f'time={report["mission_time_seconds"]}s '
        f'players={len(br_players)} results={len(char_results)}')

    # ---- 响应先行：战斗服 pending wait 立即完成（后续 DB/推送异常不再
    #      拖垮结算链路——2026-08-26 崩溃事故即解析异常导致无响应）
    resp_proto = pb_int64(1, mission_session_id)     # f1 missionSessionId
    resp_proto += pb_int32(2, 1)                     # f2 response=1
    resp = build_response(cmd_id, 'AnnounceBattleFieldEarningsResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'GAME S->C cmd={cmd_id:<4} AnnounceBattleFieldEarningsResponse '
        f'(msid={mission_session_id}, response=1)')

    # ---- DB 持久化（全部 best-effort）
    # 战报全量入库（含 bot 行，人机局战报需显示 bot）；bot 无 DB 账号，
    # 下方收益/xp/统计循环仍按 is_bot 名单过滤
    # f5 PlayerBattleResult 里 bot 也带 playerId，且编号可能与真实账号
    # id 撞号（实测 bot 3 ≠ 玩家 3，错发货币到真实账号）——收益/统计
    # 循环统一按 BattleReport 的 is_bot 名单过滤；不在 BattleReport
    # 名单里的 pid 一并跳过（bot 无 DB 账号，只会刷 FK/NoneType 错误）
    bot_pids = {p['player_id'] for p in br_players
                if p.get('is_bot') and p['player_id']}
    known_pids = {p['player_id'] for p in br_players if p['player_id']}

    def _is_settleable(pid):
        return pid and pid not in bot_pids and \
            (not known_pids or pid in known_pids)
    report_id = 0
    if STORE is not None:
        try:
            report_id = await STORE.save_battle_report(report, br_players)
            log(f'      [DB] battle_report saved id={report_id} '
                f'(msid={report["mission_session_id"]}, '
                f'{len(br_players)} players)')
        except Exception as e:
            log(f'      [DB] save_battle_report failed: {e}')

        for pid, credits, warfunds in currency_ops:
            if not (credits or warfunds) or not _is_settleable(pid):
                continue
            try:
                # 老兵会员 +25%（PlayerSettings f26 恒 2=会员模板 → 人人加成）
                vet_c = credits * 25 // 100 if credits > 0 else 0
                vet_w = warfunds * 25 // 100 if warfunds > 0 else 0
                cur = await STORE.add_currency(pid, credits=credits + vet_c,
                                               warfunds=warfunds + vet_w)
                log(f'      [DB] earnings player={pid} +{credits}cr(+{vet_c}vet) '
                    f'+{warfunds}wf(+{vet_w}vet) '
                    f'-> {cur["credits"]}cr/{cur["warfunds"]}wf')
            except Exception as e:
                log(f'      [DB] add_currency({pid}) failed: {e}')

        for p in br_players:
            pid = p['player_id']
            if not pid or p.get('is_bot'):
                continue
            try:
                for ch in p['characters']:
                    xp_delta = max(0, ch['endXp'] - ch['startXp'])
                    if xp_delta:
                        await STORE.add_combat_xp(pid, ch['characterId'], xp_delta)
                        log(f'      [DB] char {ch["characterId"]} +{xp_delta} xp')
                # 战斗通行证分数：本局 battle score 计入当前激活赛季
                # （未购买也累计，与官服一致；奖励仅已购买行可领）
                if p['score']:
                    bp_score = await STORE.add_battle_pass_score(
                        pid, _BP_ACTIVE_SEASON, p['score'])
                    log(f'      [DB] battle pass +{p["score"]} -> {bp_score} '
                        f'(player={pid})')
                if p['kills'] or p['deaths'] or p['score'] or p['captures']:
                    target = p['characters'][0]['characterId'] if p['characters'] else 0
                    if not target:
                        mm_sess = _session_by_player(
                            pid, mission_id=report['mission_id'] or None)
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

        ribbon_ends = _report_ribbon_end(br_players)
        for pid, ch in char_results:
            if not _is_settleable(pid) or not ch['characterId']:
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
                # 勋带达成（templateId, level）→ XP 补到该等级阈值下限；
                # f13 报告里的 endValue（战后勋带 XP）优先，取两者较大值
                report_ends = ribbon_ends.get((pid, ch['characterId']), {})
                for ribbon_id, ribbon_lv in ch['ribbons']:
                    if ribbon_id and ribbon_lv:
                        floor_xp = _RIBBON_LEVEL_XP.get(int(ribbon_lv), 0)
                        floor_xp = max(floor_xp,
                                       report_ends.get(int(ribbon_id), 0))
                        if floor_xp:
                            await STORE.set_ribbon_xp_floor(
                                pid, ch['characterId'], int(ribbon_id), floor_xp)
                # f13 有 endValue 但 f9 无达成等级的勋带也要落库
                achieved = {int(r) for r, l in ch['ribbons'] if r}
                for rid, end_v in report_ends.items():
                    if end_v and rid not in achieved:
                        await STORE.set_ribbon_xp_floor(
                            pid, ch['characterId'], rid, end_v)
            except Exception as e:
                log(f'      [DB] char result stats (pid={pid}) failed: {e}')

        # ---- 战后维护损耗：参战角色已装备的武器/配件/弹药/载具/升级
        #      按 timeActive 估算扣减 condition（同局重复结算取最大活跃值，
        #      不叠加；bot 与未参战（timeActive=0）角色跳过）
        char_times = {}
        for pid, ch in char_results:
            if not _is_settleable(pid) or not ch['characterId']:
                continue
            key = (pid, ch['characterId'])
            char_times[key] = max(char_times.get(key, 0), ch['timeActive'])
        await _apply_equipment_wear(char_times)

    # 战局结束：清空据点归属覆盖（下局从布局重新开始）+ 会话收尾
    # （PMS DELETE 清 rejoin + 战报通知触发结算界面）+ 释放本场
    # combat server 实例（杀进程/释放端口，触发排队战局拉起）
    _CAPTURE_OWNERS.clear()
    if mission_id:
        try:
            await _finish_mission_sessions(mission_id, report_id)
        except Exception as e:
            log(f'      [combat] finish mission sessions failed: {e}')
        matchmaking.release_match(mission_id)


# ---------------------------------------------------------------- 战局结束收尾

# Notification 表行 id 自增（NewBattleReport 通知，KVCS 推送）
_NOTIF_SEQ = [int(time.time() * 1000) & 0x7FFFFFFF]

# 已推送 Notification 行登记（social.SetNotificationsRead 回读重推用）：
# notif_id → (player_id, type, type_id, timestamp, expires)。
# 客户端已读后只发 SetNotificationsReadRequest，本地不刷 NotificationTable；
# 服务器必须回推 readStatus 更新行，通知托盘角标（newNotificationCount）
# 才会清零。
_NOTIF_ROWS = {}


def _next_notif_id():
    _NOTIF_SEQ[0] += 1
    return _NOTIF_SEQ[0]


def _msg_battle_report_notification(notif_id, player_id, report_id):
    """Notification 行（type=5 NewBattleReport，typeId=战报 id）。

    客户端 NotificationModel.handleNotification：NewBattleReport + 未读 →
    GetBattleReportRequest{typeId} → MissionBattleReportWindow（大厅结算
    界面）+ 对局记录入通知托盘。readStatus=0 Unread 才会自动弹窗。"""
    expires = time.strftime('%Y%m%d%H%M%S',
                            time.gmtime(time.time() + 7 * 86400)) + 'Z'
    timestamp = matchmaking._zulu_now()
    m = pb_int64(1, int(notif_id))                    # id
    m += pb_int64(2, int(player_id))                  # playerId
    m += pb_int32(3, 5)                               # type = NewBattleReport
    m += pb_int64(4, int(report_id))                  # typeId = 战报 id
    m += pb_int32(5, 0)                               # readStatus = Unread
    m += pb_string(6, timestamp)                      # timestamp（zulu）
    m += pb_string(7, expires)                        # expires（7 天）
    _NOTIF_ROWS[int(notif_id)] = (int(player_id), 5, int(report_id),
                                  timestamp, expires)
    return m


async def _finish_mission_sessions(mission_id, report_id):
    """战局正常结束（earnings 已入库）对局内每名玩家收尾。

    与 abandon（mission.py，玩家主动放弃路径）同款收尾序列，区别在触发方是
    服务器（战斗结束客户端不会发 AbandonMissionRequest——StatusMediator 卡
    REJOIN、无结算界面的根因）：
    1. SquadSession mission=0 + SquadMemberSession status=2（solo squad 重置，
       PMS DELETE 前置——StatusMediator.onLobbySignalUpdates 读 squadSession，
       null/missionId≠0 会 TypeError #1009 或 LOCKED_IN_COMBAT）；
    2. PlayerMissionSession DELETE（playerMissionSessionId=null → REJOIN 消失，
       回 BASE_STATE）；
    3. Notification(NewBattleReport, typeId=report_id)（战报已入库才推：
       客户端 GetBattleReport 查不到会静默跳过弹窗）；
    4. 清理 PMS_TO_SESSION / PMS_TO_MISSION / _MM_QUEUES 内部状态。

    另一调用方：matchmaking._watch_proc_exit（战斗服未走 earnings 即退出，
    report_id=None → 跳过通知推送，只做 1/2/4——否则客户端 closing 流程收
    不到会话结束信号，轮询 GetMissionDetails 到超时才自行 Abandon）。
    末尾统一清 PMS 映射，重复调用空转，天然幂等。
    """
    my_pms = [pms for pms, mid in matchmaking.PMS_TO_MISSION.items()
              if int(mid or 0) == int(mission_id)]
    # 真实小队整队重置必须先于 PMS DELETE（StatusMediator 在 PMS DELETE 的
    # lobbySignal 里读 squadSession.missionId，非 0 → LOCKED_IN_COMBAT）：
    # 先预扫描收集本局涉及的小队，重置后再逐玩家推 PMS DELETE
    real_squads = sorted({int((matchmaking.PMS_TO_SESSION.get(p) or {})
                              .get('squad_db_id', 0) or 0)
                          for p in my_pms} - {0})
    for sid in real_squads:
        try:
            await matchmaking.reset_real_squad_to_lobby(sid)
        except Exception as e:
            log(f'      [combat] squad reset failed (squad={sid}): {e}')
    for pms_id in my_pms:
        mm_sess = matchmaking.PMS_TO_SESSION.get(pms_id)
        if mm_sess is None:
            matchmaking.PMS_TO_MISSION.pop(pms_id, None)
            continue
        pid = int(mm_sess.get('player_id', 0) or 0)
        gamertag = mm_sess.get('gamertag') or ''
        squad_db_id = int(mm_sess.get('squad_db_id', 0) or 0)
        flash = FLASH_SESSIONS.get(gamertag)
        if flash is None or flash.playerview_cmd is None:
            log(f'      [combat] battle end: no live Flash session for '
                f'{gamertag!r}; PMS delete/notification NOT pushed')
            continue
        solo = matchmaking._SOLO_SQUADS.get(pid)
        try:
            if solo is not None and not squad_db_id:
                solo['mission_id'] = 0
                await matchmaking._push_kvcs(
                    flash.writer, flash.playerview_cmd, b'SquadSession',
                    matchmaking._msg_solo_squad_session(
                        solo['squad_row'], pid, solo['template'],
                        solo['faction'], 0, solo.get('entered', 0)))
                await matchmaking._push_kvcs(
                    flash.writer, flash.playerview_cmd, b'SquadMemberSession',
                    await matchmaking._msg_solo_squad_member_session(
                        solo['member_row'], solo['squad_row'], pid,
                        solo['gamertag'], solo['char_id'], solo['char_name'],
                        solo['rank'], 2,
                        solo.get('metrics_session_id', 0), solo['faction']))
            await matchmaking._push_kvcs_delete(
                flash.writer, flash.playerview_cmd,
                b'PlayerMissionSession', pms_id)
            log(f'S->C cmd={flash.playerview_cmd:<4} KeyValueChangeSet '
                f'delete (PlayerMissionSession id={pms_id}) ★battle end')
            if report_id:
                await matchmaking._push_kvcs(
                    flash.writer, flash.playerview_cmd, b'Notification',
                    _msg_battle_report_notification(
                        _next_notif_id(), pid, report_id))
                log(f'S->C cmd={flash.playerview_cmd:<4} KeyValueChangeSet '
                    f'(Notification NewBattleReport typeId={report_id} '
                    f'player={pid} ★结算界面触发)')
        except Exception as e:
            log(f'      [combat] battle end push failed '
                f'(player={pid}): {e}')
        # 结算角色行重推：rank 用已入库的新 combat_xp 推导 + statUpdateTimeStamp
        # 取当前毫秒。客户端 CharacterModel.checkStatsWasUpdated 比较新时间戳与
        # 上次 ResponseReadCharacterStats 入库时的旧时间戳，不同即自动重发
        # RequestReadCharacterStats → 人物界面勋带/XP/等级无需重登即刷新
        # （客户端 activeCharacterStatRequests 会话级缓存不会主动失效，不推则
        # 只显示登录时的旧值）；rank 提升时 checkRankWasUpdated 另弹升级窗口。
        # 尾部字段必须传全：客户端 setObject 整体替换 schema，漏传 head_id 等
        # 会被抹回默认值（外观回退根因）。
        char_id = int(mm_sess.get('char_id', 0) or 0)
        if char_id and STORE is not None and flash.soldier_view_cmd is not None:
            try:
                for ch in await STORE.list_characters(pid):
                    if int(ch['id']) != char_id:
                        continue
                    bits = await _char_unlock_bits_for(
                        flash, ch['template'], ch['faction'], ch['id'])
                    new_ts = int(time.time() * 1000)
                    new_rank = _rank_from_combat_xp(ch.get('combat_xp', 0))
                    await _push_kvcs(
                        flash.writer, flash.soldier_view_cmd, b'character',
                        _msg_character(
                            ch['name'], ch['faction'], ch['template'],
                            new_rank, ch['id'], bits,
                            ch.get('combat_badge_0', 0),
                            ch.get('combat_badge_1', 0),
                            new_ts,
                            ch.get('uniform_variant_id', 0),
                            ch.get('headgear_id', 0),
                            1 if ch.get('is_deleted') else 0,
                            ch.get('previous_template', 0),
                            ch.get('head_id', 0),
                            0,
                            1 if ch.get('is_on_leave') else 0))
                    log(f'S->C cmd={flash.soldier_view_cmd:<4} KeyValueChangeSet '
                        f'(character {ch["name"]} id={ch["id"]} re-pushed, '
                        f'rank={new_rank}, statTs={new_ts} ★settlement refresh)')
                    break
            except Exception as e:
                log(f'      [combat] settlement character re-push failed '
                    f'(player={pid}): {e}')
    # 内部状态清理（无论 Flash 连接是否在线）
    for pms_id in my_pms:
        s = matchmaking.PMS_TO_SESSION.pop(pms_id, None)
        matchmaking.PMS_TO_MISSION.pop(pms_id, None)
        if s is not None:
            matchmaking._MM_QUEUES.pop(int(s.get('player_id', 0) or 0), None)


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
    'SetPlayerSettings': handle_set_player_settings,
    'TutorialRequest': handle_tutorial_request,
    'ReportAccesspointFactionChange': handle_report_accesspoint_faction_change,
}
