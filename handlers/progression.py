"""玩家等级（进展 tab）奖励发放与解锁通知。

进展 tab 的每级奖励/模式解锁由客户端本地 WarDatabase 表（PlayerLevel /
PlayerLevelUnlock）渲染，等级 = totalScore(player f36) 经 PlayerLevel 阈值
推导；但"升级获得奖励"弹窗与"新模式解锁"弹窗都由服务器 Notification KVCS
行驱动（NotificationType 18/9），且货币奖励必须服务器入账——本模块补齐：

- check_player_level：等级检测 + PlayerLevelUnlock 表 (granted, new_level]
  各级 credits/gold/warfunds 发放（players.level_granted 防重发）
- 推送更新后的 player 行（货币/totalScore/level 一起刷新）
- Notification(PlayerLevelUnlocked=18, typeId=新等级) 未读行 → 客户端
  UnlockPlayerLevelWindow"升级获得"弹窗（奖励数额客户端从本地表读）
- Notification(MissionTypeUnlocked=9, typeId=任务类型 id) 未读行 →
  UnlockMissionTypeWindow"新模式解锁"弹窗（typeId=1 默认 Assault 客户端
  自动跳过；跨多级补发只弹最终等级、模式弹窗跳过，防刷屏）

调用点：combat._finish_mission_sessions（战局结束）notify=True；
views.subscribeplayerview（登录补发）notify=False（只入账不弹窗）。
"""
import time

from protocol import pb_int32, pb_int64, pb_string
from state import STORE, log
from wardb import _LEVEL_UNLOCKS, player_level_from_score

# HnG_States.NotificationType
_NT_PLAYER_LEVEL_UNLOCKED = 18
_NT_MISSION_TYPE_UNLOCKED = 9


async def _total_score(pid):
    """全角色 PlayerScore 汇总（与 player_service._msg_player_for 同源：
    type-6 上报的 PlayerScore 优先，legacy 结算键 score 回落）。"""
    total = 0
    for ch in await STORE.list_characters(pid):
        st = await STORE.get_stats(ch['id']) or {}
        total += int(st.get('PlayerScore', st.get('score', 0)) or 0)
    return total


def _msg_notification(notif_id, player_id, ntype, type_id):
    """Notification 行（与 combat._msg_battle_report_notification 同构，
    readStatus=0 Unread 才会触发客户端弹窗）。"""
    from matchmaking import _zulu_now
    timestamp = _zulu_now()
    expires = time.strftime('%Y%m%d%H%M%S',
                            time.gmtime(time.time() + 7 * 86400)) + 'Z'
    m = pb_int64(1, int(notif_id))              # id
    m += pb_int64(2, int(player_id))            # playerId
    m += pb_int32(3, int(ntype))                # type
    m += pb_int64(4, int(type_id))              # typeId
    m += pb_int32(5, 0)                         # readStatus = Unread
    m += pb_string(6, timestamp)                # timestamp（zulu）
    m += pb_string(7, expires)                  # expires（7 天）
    return m, timestamp, expires


async def check_player_level(sess, writer, pid, notify=True):
    """等级检测 + 发奖 + 通知推送；未升级时静默返回 None，升级返回新等级。

    notify=False（登录补发路径）：只入账货币并推进 level_granted，
    不推 Notification（登录流程随后自会下发全量 player 行，调用方按
    返回值决定是否重推）。"""
    if STORE is None or pid is None:
        return None
    if not hasattr(STORE, 'get_level_granted'):
        return None
    try:
        score = await _total_score(pid)
    except Exception as e:
        log(f'[prog] totalScore sum failed (player={pid}): {e}')
        return None
    lvl = player_level_from_score(score)
    try:
        granted = await STORE.get_level_granted(pid)
    except Exception as e:
        log(f'[prog] get_level_granted failed (player={pid}): {e}')
        return None
    if lvl <= granted:
        return None

    credits = gold = warfunds = 0
    modes = []
    for lv in range(granted + 1, lvl + 1):
        ent = _LEVEL_UNLOCKS.get(lv)
        if not ent:
            continue
        credits += ent['credits']
        gold += ent['gold']
        warfunds += ent['warfunds']
        modes.extend(ent['modes'])

    if credits or gold or warfunds:
        try:
            cur = await STORE.add_currency(pid, credits=credits, gold=gold,
                                           warfunds=warfunds)
            log(f'[prog] player={pid} level {granted}->{lvl} rewards: '
                f'+{credits}cr +{gold}g +{warfunds}wf -> '
                f'{cur["credits"]}cr/{cur["gold"]}g/{cur["warfunds"]}wf')
        except Exception as e:
            log(f'[prog] add_currency failed (player={pid}): {e}')
    try:
        await STORE.set_level_granted(pid, lvl)
    except Exception as e:
        log(f'[prog] set_level_granted failed (player={pid}): {e}')
        return None

    if not notify:
        return lvl
    if getattr(sess, 'playerview_cmd', None) is None:
        return lvl

    # 函数级导入避免环：combat → progression（顶部）→ combat（此处）
    from .combat import _NOTIF_ROWS, _next_notif_id
    from messages import _push_kvcs
    from player_service import _msg_player_for

    multi = lvl - granted > 1
    try:
        # 升级弹窗（跨多级补发只弹最终等级）
        nid = _next_notif_id()
        m, ts, exp = _msg_notification(nid, pid,
                                       _NT_PLAYER_LEVEL_UNLOCKED, lvl)
        _NOTIF_ROWS[nid] = (pid, _NT_PLAYER_LEVEL_UNLOCKED, lvl, ts, exp)
        await _push_kvcs(writer, sess.playerview_cmd, b'Notification', m)
        log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet '
            f'(Notification PlayerLevelUnlocked typeId={lvl} player={pid} '
            f'★升级奖励弹窗)')
        # 模式解锁弹窗（仅单级升迁时推，补发路径跳过防刷屏）
        if not multi:
            for mt in modes:
                nid = _next_notif_id()
                m, ts, exp = _msg_notification(nid, pid,
                                               _NT_MISSION_TYPE_UNLOCKED, mt)
                _NOTIF_ROWS[nid] = (pid, _NT_MISSION_TYPE_UNLOCKED, mt,
                                    ts, exp)
                await _push_kvcs(writer, sess.playerview_cmd,
                                 b'Notification', m)
                log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet '
                    f'(Notification MissionTypeUnlocked typeId={mt} '
                    f'player={pid} ★新模式解锁弹窗)')
        # 货币/等级刷新：重推全量 player 行
        player_proto = await _msg_player_for(sess)
        await _push_kvcs(writer, sess.playerview_cmd, b'player', player_proto)
        log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet '
            f'(player refreshed, level {granted}->{lvl} ★进展奖励入账)')
    except Exception as e:
        log(f'[prog] level-up push failed (player={pid}): {e}')
