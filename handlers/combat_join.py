# -*- coding: utf-8 -*-
"""战斗服加入/离开链路（combat_join）：从 handlers/combat.py 拆出
（2026-09-06 重构，纯移动行为不变）。

PlayerJoined / PlayerExitBatch 上报与 PMS Admitted=2 重推——
hng.exe 游戏连接上报离开后，按玩家名反查 Flash 连接推
PlayerMissionSession KVCS UPDATE（connectionState=2 Admitted），
触发客户端 abandonActionGameSignal → AbandonMissionRequest
（handlers/mission.py 回 response=1 后推 PMS DELETE 收尾回大厅）。

响应规则（combat.py §9 同源）：通知类 → HandlerErrorResponse
（注册内、能完成 pending wait；载荷为空不影响流程）。
"""

import matchmaking
import mm_state
from state import FLASH_SESSIONS, PUBLIC_HOST, STORE, log
from wardb import _rank_from_combat_xp

from .combat import (
    _ack_error, _as_str, _all, _get, _session_by_msid, _split_fields,
)

# 与 matchmaking 成局 game_connect 同源（PUBLIC_HOST；mission.py _GAME_CONNECT）
_GAME_CONNECT = f'{PUBLIC_HOST}:5678'


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
    mission_id = int(mm_state.PMS_TO_MISSION.get(pms_id, 0) or 0)
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
            metrics_device_id=mm_state.METRICS_DEVICE_ID,
            platform='HnG', connection_state=2,
            battle_session_id=int(mm_sess.get('battle_session_id', 0) or 0))
        await matchmaking._push_kvcs(writer, sess.playerview_cmd,
                                     b'PlayerMissionSession', msg)
        log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet '
            f'(PlayerMissionSession id={pms_id}, mission={mission_id}, '
            f'state=2 Admitted ★leave-battle abandon trigger)')
    except Exception as e:
        log(f'      [combat] PMS Admitted push failed: {e}')


# ---------------------------------------------------------------- 注册表

HANDLERS = {
    'PlayerJoined': handle_player_joined,
    'PlayerExitBatch': handle_player_exit_batch,
}
