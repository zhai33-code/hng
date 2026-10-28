# -*- coding: utf-8 -*-
"""好友列表"小队邀请图标"生命周期回归（2026-09-06 玩家反馈）。

反馈：中午好友列表修复（7e741bc，missionId/missionFactionId/mapEntityId/
squadSessionId 真实化 + 恢复"跟随进入战斗"按钮）之后，跟进游戏（only_mission
跟随好友进战局）后小队邀请图标消失。

客户端反编译（SquadInviteListPlayerItemRenderer.set data）：
    inviteForSquad.visible = showSquadInvite
                           && friend.squadSessionId == 0   (friendinfo f12)
                           && friend.missionId == 0        (friendinfo f6)
即图标只由服务端 friendinfo 行的两个字段决定。本测试按客户端同款口径，
解析 B 收到的 friendinfo KVCS 字节，断言 A 的状态机各阶段字段取值：

 1. A 空闲在线              → f6=0   f12=0  → 图标可见
 2. A 战斗中（普通成局）     → f6≠0           → 图标隐藏（官服口径）
 3. 战局结束（settle 收尾）  → f6=0   重推    → 图标恢复 ★反馈主诉求
 4. A 放弃战局（abandon）    → f6=0   重推    → 图标恢复
 5. A 在真实小队（DB 行）    → f12≠0          → 图标隐藏（官服口径）
 6. A 退出真实小队           → f12=0  重推    → 图标恢复
 7. 跟随路径（only_mission _join_existing_mission 真函数）：
    登记会话后 f6≠0、f7（missionFactionId）≠0（跟随按钮依赖），结束恢复 f6=0
 8. 战局已释放但 PMS 残留（异常路径）→ f6=0（_find_player_live_pms 必须看
    COMBAT_INSTANCES 存活，防"幽灵战斗中"卡死图标）
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol import Chunk
import matchmaking
import mm_state
import combat_instances
from handlers import social

RESULTS = []


def check(label, ok, detail=''):
    RESULTS.append((label, ok))
    print(f'[{"PASS" if ok else "FAIL"}] {label}{("  " + detail) if detail else ""}')


class FakeWriter:
    def __init__(self):
        self.data = b''

    def write(self, data):
        self.data += data

    async def drain(self):
        pass


class FakeSess:
    """FLASH_SESSIONS 注册表里的 Flash 会话（push_friend_rows 只用这两个属性）。"""

    def __init__(self, cmd):
        self.playerview_cmd = cmd
        self.writer = FakeWriter()


A_PID, A_TAG = 2001, 'iconA'
B_PID, B_TAG = 2002, 'iconB'


class FakeStore:
    def __init__(self):
        self.squad_of = {}          # pid -> squad row dict or None

    async def list_friend_rows(self, pid):
        other = (A_PID, A_TAG) if pid == B_PID else (B_PID, B_TAG)
        return [{'id': other[0], 'gamertag': other[1], 'faction': 3,
                 'also_friend': True, 'initiated_by_me': True}]

    async def get_squad_for_player(self, pid):
        return self.squad_of.get(pid)

    async def get_active_war(self):
        return {'id': 7, 'name': 'IconWar'}


def _varint(buf, i):
    v = shift = 0
    while True:
        b = buf[i]
        i += 1
        v |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            return v, i


def _parse_fields(b):
    out = {}
    i = 0
    while i < len(b):
        key, i = _varint(b, i)
        fn, wt = key >> 3, key & 7
        if wt == 0:
            out[fn], i = _varint(b, i)
        elif wt == 2:
            ln, i = _varint(b, i)
            out[fn] = b[i:i + ln]
            i += ln
        elif wt == 1:
            i += 8
        elif wt == 5:
            i += 4
        else:
            break
    return out


def _collect_friendinfo(writer):
    """解析 writer 中全部 KVCS 帧，取 friendinfo 表 'set' 行 → [(f6,f12,f7)]。"""
    rows = []
    buf = writer.data
    while buf:
        chunk, nxt = Chunk.parse(buf, 0)
        if chunk is None:
            break
        buf = buf[nxt:]
        if not chunk.subs:
            continue
        title = chunk.subs[0]
        if title.as_str() != 'KeyValueChangeSet':
            continue
        for action in title.subs:
            if action.as_str() != 'set':
                continue
            for tbl in action.subs:
                if tbl.as_str() != 'friendinfo':
                    continue
                for row in tbl.subs:
                    f = _parse_fields(row.data)
                    rows.append({'f6': f.get(6, 0), 'f12': f.get(12, 0),
                                 'f7': f.get(7, 0), 'f3': f.get(3, 0),
                                 'f8': f.get(8, 0)})
    return rows


def _seed_live_mission(mid, *, faction=3):
    """登记一个存活战局 + A 的 PMS 会话（成局/late join 登记后的内存形态）。"""
    inst = {'mission_id': mid, 'port': 5601, 'proc': None,
            'ready': asyncio.Event(), 'seq': 1, 'ttd': False, 'udp': None}
    inst['ready'].set()
    combat_instances.COMBAT_INSTANCES[mid] = inst
    mm_state._MM_MISSION_DETAILS[mid] = {
        'preset': 10, 'gamemode': 5, 'attacker_faction': faction,
        'defender_faction': 2, 'players': [],
        'formed_at': 0, 'map_entity_id': 0}
    pms_id = 9001
    matchmaking.PMS_TO_SESSION[pms_id] = {
        'pms_id': pms_id, 'player_id': A_PID, 'char_id': 31,
        'gamertag': A_TAG, 'faction': faction, 'squad_db_id': 0,
        'mission_id': mid}
    matchmaking.PMS_TO_MISSION[pms_id] = mid
    return pms_id


def _settle(mid, pms_id):
    """模拟 _finish_mission_sessions + release_match 的内存清理 + 好友重推。"""
    matchmaking.PMS_TO_SESSION.pop(pms_id, None)
    matchmaking.PMS_TO_MISSION.pop(pms_id, None)
    combat_instances.COMBAT_INSTANCES.pop(mid, None)
    mm_state._MM_MISSION_DETAILS.pop(mid, None)
    mm_state._MM_QUEUES.pop(A_PID, None)


async def main():
    store = FakeStore()
    social.STORE = store
    sess_b = FakeSess(3)
    social.FLASH_SESSIONS.clear()
    social.FLASH_SESSIONS[A_TAG] = FakeSess(3)   # A 在线
    social.FLASH_SESSIONS[B_TAG] = sess_b

    # ---- 1. A 空闲在线 ----
    await social.push_friend_rows(sess_b, sess_b.writer, B_PID)
    rows = _collect_friendinfo(sess_b.writer)
    check('空闲：推送了 A 的 friendinfo 行', len(rows) == 1)
    check('空闲：missionId=0 且 squadSessionId=0（图标可见）',
          rows and rows[-1]['f6'] == 0 and rows[-1]['f12'] == 0,
          f"f6={rows[-1]['f6'] if rows else '?'} f12={rows[-1]['f12'] if rows else '?'}")
    check('warId(f8) 与 player 行 f10 同源（当前战争 id=7，非硬编码 1）',
          rows and rows[-1]['f8'] == 7,
          f"f8={rows[-1]['f8'] if rows else '?'}（≠7 会触发客户端"
          f'friend_status_wrong_server"不在同一服务器"提示并隐藏图标）')

    # ---- 2. A 战斗中 ----
    mid, pms_id = 910001, _seed_live_mission(910001)
    sess_b.writer.data = b''
    await social.push_friend_rows(sess_b, sess_b.writer, B_PID)
    rows = _collect_friendinfo(sess_b.writer)
    check('战斗中：missionId≠0（图标隐藏，官服口径）',
          rows and rows[-1]['f6'] == mid, f"f6={rows[-1]['f6'] if rows else '?'}")
    from matchmaking import FACTION_ROW_IDS
    check('战斗中：missionFactionId 可解析（跟随按钮前提）',
          rows and rows[-1]['f7'] == FACTION_ROW_IDS.get(3, 0) != 0,
          f"f7={rows[-1]['f7'] if rows else '?'} row3={FACTION_ROW_IDS.get(3)}")

    # ---- 3. 战局结束（settle）→ notify_friends_of(A) 重推 ----
    _settle(mid, pms_id)
    sess_b.writer.data = b''
    await social.notify_friends_of(A_PID)
    rows = _collect_friendinfo(sess_b.writer)
    check('settle 后重推 friendinfo', len(rows) == 1)
    check('settle 后：missionId=0（图标恢复 ★反馈主诉求）',
          rows and rows[-1]['f6'] == 0, f"f6={rows[-1]['f6'] if rows else '?'}")

    # ---- 4. 放弃路径（abandon 同款内存清理 + notify） ----
    mid2, pms2 = 910002, _seed_live_mission(910002)
    _settle(mid2, pms2)                       # abandon 收尾同款
    sess_b.writer.data = b''
    await social.notify_friends_of(A_PID)
    rows = _collect_friendinfo(sess_b.writer)
    check('abandon 后：missionId=0（图标恢复）',
          rows and rows[-1]['f6'] == 0, f"f6={rows[-1]['f6'] if rows else '?'}")

    # ---- 5/6. 真实小队 join/leave ----
    store.squad_of[A_PID] = {'id': 55, 'name': 'real', 'faction': 3,
                             'leader_character_id': 31, 'matchmaking_flag': False}
    sess_b.writer.data = b''
    await social.push_friend_rows(sess_b, sess_b.writer, B_PID)
    rows = _collect_friendinfo(sess_b.writer)
    check('真实小队中：squadSessionId≠0（图标隐藏，官服口径）',
          rows and rows[-1]['f12'] == 55, f"f12={rows[-1]['f12'] if rows else '?'}")
    store.squad_of[A_PID] = None
    sess_b.writer.data = b''
    await social.push_friend_rows(sess_b, sess_b.writer, B_PID)
    rows = _collect_friendinfo(sess_b.writer)
    check('退队后：squadSessionId=0（图标恢复）',
          rows and rows[-1]['f12'] == 0, f"f12={rows[-1]['f12'] if rows else '?'}")

    # ---- 7. 跟随路径（only_mission late join 登记后的内存形态） ----
    # _join_existing_mission 登记产物：PMS rec 带 faction/mission_id（与
    # _seed_live_mission 一致），此处直接按登记后形态断言推送结果
    mid3, pms3 = 910003, _seed_live_mission(910003)
    sess_b.writer.data = b''
    await social.push_friend_rows(sess_b, sess_b.writer, B_PID)
    rows = _collect_friendinfo(sess_b.writer)
    check('跟随进局后：missionId≠0 + missionFactionId≠0',
          rows and rows[-1]['f6'] == mid3 and rows[-1]['f7'] != 0,
          f"f6={rows[-1]['f6'] if rows else '?'} f7={rows[-1]['f7'] if rows else '?'}")
    # 局结束恢复
    _settle(mid3, pms3)
    sess_b.writer.data = b''
    await social.notify_friends_of(A_PID)
    rows = _collect_friendinfo(sess_b.writer)
    check('跟随的局结束：missionId=0（图标恢复）',
          rows and rows[-1]['f6'] == 0, f"f6={rows[-1]['f6'] if rows else '?'}")

    # ---- 8. 战局已释放但 PMS 残留（异常兜底口径） ----
    mid4, pms4 = 910004, _seed_live_mission(910004)
    combat_instances.COMBAT_INSTANCES.pop(mid4, None)   # 只释放实例，PMS 残留
    mm_state._MM_MISSION_DETAILS.pop(mid4, None)
    sess_b.writer.data = b''
    await social.push_friend_rows(sess_b, sess_b.writer, B_PID)
    rows = _collect_friendinfo(sess_b.writer)
    check('实例已释放+PMS残留：missionId=0（不得卡在战斗中）',
          rows and rows[-1]['f6'] == 0, f"f6={rows[-1]['f6'] if rows else '?'}")
    matchmaking.PMS_TO_SESSION.pop(pms4, None)
    matchmaking.PMS_TO_MISSION.pop(pms4, None)

    social.FLASH_SESSIONS.clear()

    failed = [l for l, ok in RESULTS if not ok]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    asyncio.run(main())
