# -*- coding: utf-8 -*-
"""abandon 时凑队行重置回归测试（2026-09-08 "解散后小队卡住"复现）。

场景：玩家（匹配凑队 ≥2 人）在局内 abandon →
release_player_mission_sessions 应：
  1. 删除客户端上的凑队行（SquadMemberSession 全体 + SquadSession）
  2. 重推 solo 行（SquadSession mission=0 + SquadMemberSession InLobby）
  3. 推 PMS DELETE 回大厅

旧 bug：int() argument ... not 'dict' 异常被 except 吞掉 → 凑队行不删、
solo 行不推 → 客户端带着 missionId≠0 的凑队行卡 LOCKED_IN_COMBAT；
随后 RemoveSquad 走凑队分支删行后无人重推 solo 行 → 右侧按钮
"小队未准备好"卡死。
"""
import asyncio
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol import Chunk
import mm_state
import handlers.mission as mission_mod
from handlers.mission import release_player_mission_sessions
from state import FLASH_SESSIONS

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


def _collect_kvcs(w):
    """解析 writer 中全部 KVCS 变更：(action, table, row_id) 列表。

    action='set' 时 row_id 取 proto f1（varint）；'delete' 取定长 s64。
    """
    out = []
    off = 0
    while off < len(w.data):
        chunk, off2 = Chunk.parse(w.data, off)
        if chunk is None:
            break
        off = off2
        title = chunk.subs[0] if chunk.subs else None
        if title is None or title.as_str() != 'KeyValueChangeSet':
            continue
        action = title.subs[0] if title.subs else None
        if action is None:
            continue
        kind = action.as_str()
        table = action.subs[0] if action.subs else None
        if table is None or not table.subs:
            continue
        for row in table.subs:
            if kind == 'delete':
                out.append((kind, table.as_str(),
                            struct.unpack('<q', row.data)[0]))
            else:
                # body = tag varint + value varint（f1 均为 wire type 0）
                v = shift = 0
                body = row.data
                i = 0
                while True:
                    b = body[i]; i += 1
                    v |= (b & 0x7F) << shift; shift += 7
                    if not (b & 0x80):
                        break
                v = shift = 0
                while i < len(body):
                    b = body[i]; i += 1
                    v |= (b & 0x7F) << shift; shift += 7
                    if not (b & 0x80):
                        break
                out.append((kind, table.as_str(), v))
    return out


SQ_ID = 8072128442240809119
MISSION_ID = 223287
PMS_ID = 2356587968538935717


def _seed():
    mm_state._MM_MISSION_SQUADS.clear()
    mm_state._SOLO_SQUADS.clear()
    mm_state.PMS_TO_SESSION.clear()
    mm_state.PMS_TO_MISSION.clear()
    mm_state._MM_QUEUES.clear()
    mm_state._MM_MISSION_DETAILS.clear()
    # 凑队：DoubleBird(pid=2, row 101) + BotSU1(pid=11, row 102)
    mm_state._MM_MISSION_SQUADS[MISSION_ID] = {
        SQ_ID: {
            'name': 'Infantry Squad 1', 'faction': 1, 'template': 1,
            'squad_db_id': 0,
            'members': [
                {'player_id': 2, 'char_id': 25, 'gamertag': 'DoubleBird',
                 'rank': 5, 'slot': 0, 'row_id': 101},
                {'player_id': 11, 'char_id': 31, 'gamertag': 'BotSU1',
                 'rank': 0, 'slot': 1, 'row_id': 102},
            ],
        },
    }
    # solo 注册表（合并进凑队前保留的条目）
    mm_state._SOLO_SQUADS[2] = {
        'squad_row': 7001, 'member_row': 7002, 'template': 1, 'faction': 1,
        'mission_id': MISSION_ID, 'entered': 1, 'gamertag': 'DoubleBird',
        'char_id': 25, 'char_name': 'DB', 'rank': 5,
        'metrics_session_id': 42,
    }
    # mm 会话登记：auto_sq_id / auto_sq_rows（战斗结束/放弃删凑队行用）
    mm_state.PMS_TO_SESSION[PMS_ID] = {
        'pms_id': PMS_ID, 'player_id': 2, 'char_id': 25,
        'gamertag': 'DoubleBird', 'faction': 1,
        'metrics_uid': 'mu', 'metrics_session_id': 42,
        'battle_session_id': 77, 'squad_id': SQ_ID, 'squad_db_id': 0,
        'squad_slot': 0, 'mission_id': MISSION_ID,
        'auto_sq_id': SQ_ID, 'auto_sq_rows': [101, 102],
    }
    mm_state.PMS_TO_MISSION[PMS_ID] = MISSION_ID
    mm_state._MM_QUEUES[2] = {'pmm_id': 900, 'mission_id': MISSION_ID,
                              'gamemode': 1, 'ctx': {}}


async def main():
    _seed()
    w = FakeWriter()
    old_store = mission_mod.STORE
    mission_mod.STORE = None
    try:
        # ★ 核心：abandon 不再抛 int(dict)（旧 bug 下该方法内部 except 吞错，
        #   凑队行不删 + solo 行不推 → 客户端小队卡死）
        await release_player_mission_sessions(2, w, 55, source='test')
        check('release 无异常完成', True)
    except Exception as e:
        check('release 无异常完成', False, f'raised: {e!r}')
    finally:
        mission_mod.STORE = old_store

    kv = _collect_kvcs(w)
    dels = [(t, r) for (a, t, r) in kv if a == 'delete']
    sets = [(t, r) for (a, t, r) in kv if a == 'set']
    check('凑队成员行删除(101,102)',
          ('SquadMemberSession', 101) in dels
          and ('SquadMemberSession', 102) in dels,
          f'dels={dels}')
    check('凑队 SquadSession 删除',
          ('SquadSession', SQ_ID) in dels, f'dels={dels}')
    check('solo SquadSession 重推(mission=0)',
          ('SquadSession', 7001) in sets, f'sets={sets}')
    check('solo SquadMemberSession 重推(InLobby)',
          ('SquadMemberSession', 7002) in sets, f'sets={sets}')
    check('PMS DELETE 推送',
          ('PlayerMissionSession', PMS_ID) in dels, f'dels={dels}')
    check('solo 注册表 mission_id 归零',
          mm_state._SOLO_SQUADS.get(2, {}).get('mission_id') == 0,
          f'got {mm_state._SOLO_SQUADS.get(2, {}).get("mission_id")}')
    check('PMS_TO_SESSION 已清理',
          PMS_ID not in mm_state.PMS_TO_SESSION)

    ok = all(r[1] for r in RESULTS)
    print()
    print('RESULT:', 'PASS' if ok else 'FAIL',
          f'({sum(1 for r in RESULTS if r[1])}/{len(RESULTS)} checks passed)')
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    asyncio.run(main())
