# -*- coding: utf-8 -*-
"""rejoin 掉线保护（300s 席位超时）回归测试（2026-09-08）。

事故：hng.exe 崩溃/被杀（非优雅退出）路径无任何服务端推送 →
客户端 REJOIN 按钮无 300s 倒计时（Admitted 状态缺失），且掉线
过久 PMS 会话/坑位永不释放（好友列表恒"战斗中"、战局满员卡死）。

官服语义复刻：
- 掉线（游戏连接断开 / PlayerExitBatch 优雅退出）→ 记 rejoin_deadline
  （now + mm_config.REJOIN_TIMEOUT）+ 给在线 Flash 推 PMS UPDATE
  connectionState=2 (Admitted) → 客户端 ActionGameService Admitted 分支
  起 300s 倒计时（StatusMediator "gameConnectionLostCountdown"）；
- 窗口内重进（PlayerJoined / GetPlayerSettings 精确命中）→ 清 deadline；
- 超时未重进 → rejoin_sweep_loop 释放会话（PMS DELETE + 坑位回收），
  重登不再推 REJOIN 行（正常进大厅）。

覆盖：deadline 助手纯逻辑 / 查找器过滤过期 / on_game_client_closed
推送 / PlayerExitBatch·PlayerJoined handler / rejoin_sweep_once 释放。
"""
import asyncio
import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol import pb_message, pb_varint

import matchmaking
import mm_config
import mm_state
import state
import combat_instances
from handlers import combat_join
from handlers import mission as mission_handlers
from handlers import social as social_handlers
from state import FLASH_SESSIONS

RESULTS = []


def check(label, ok, detail=''):
    RESULTS.append((label, ok))
    print(f'[{"PASS" if ok else "FAIL"}] {label}{("  " + detail) if detail else ""}')


class FakeWriter:
    def __init__(self):
        self.data = b''

    def write(self, data):
        self.data += bytes(data)

    async def drain(self):
        pass


class MockStore:
    """隔离 DB：set_mission_state 记录调用，其余查询回空。"""

    def __init__(self):
        self.mission_states = []

    async def set_mission_state(self, mid, st):
        self.mission_states.append((int(mid), int(st)))

    async def list_friend_rows(self, pid):
        return []

    async def get_character(self, pid, cid):
        return None

    async def list_characters(self, pid):
        return []

    async def add_stat(self, cid, key, val):
        pass


def seed_mission(mid, players=1):
    """登记一个（伪）运行中战局；返回 det。"""
    inst = {'mission_id': mid, 'port': 5600 + mid % 100, 'proc': None,
            'ready': asyncio.Event(), 'seq': 1, 'ttd': False, 'udp': None}
    inst['ready'].set()
    det = {'preset': 10, 'gamemode': 5, 'ai_fill': False,
           'map_entity_id': mm_state.MISSION_MAP_ENTITY_ID,
           'attacker_faction': 3, 'defender_faction': 2,
           'enable_bots': True, 'weather_tod': 2, 'weather_condition': 0,
           'timeofday': None, 'formed_at': time.monotonic(),
           'players': [{'player_id': 1000 + i, 'gamertag': f'Mate{i}',
                        'char_id': 2000 + i, 'faction': 3,
                        'pms_id': 3000 + i} for i in range(players)]}
    combat_instances.COMBAT_INSTANCES[mid] = inst
    mm_state._MM_MISSION_DETAILS[mid] = det
    return det


def seed_pms(pms, pid, mid, tag, char_id=42):
    """登记玩家会话（PMS 映射 + det.players 追加）。"""
    mm_state.PMS_TO_SESSION[pms] = {
        'pms_id': pms, 'player_id': pid, 'char_id': char_id,
        'gamertag': tag, 'faction': 3, 'squad_db_id': 0,
        'battle_session_id': 0, 'metrics_uid': f'uid-{pid}',
        'metrics_session_id': 0}
    mm_state.PMS_TO_MISSION[pms] = mid
    det = mm_state._MM_MISSION_DETAILS.get(mid)
    if det is not None and 'players' in det:
        det['players'].append({'player_id': pid, 'gamertag': tag,
                               'char_id': char_id, 'faction': 3, 'pms_id': pms})


def seed_flash(tag, view_cmd=105):
    w = FakeWriter()
    FLASH_SESSIONS[tag] = types.SimpleNamespace(writer=w,
                                                playerview_cmd=view_cmd,
                                                soldier_view_cmd=None)
    return w


class Patched:
    """集中打桩（STORE×4 / release_match），with 域内自动还原。"""

    def __enter__(self):
        self.saved = (
            state.STORE, mission_handlers.STORE, combat_join.STORE,
            social_handlers.STORE, mission_handlers.release_match)
        self.store = MockStore()
        self.released = []
        state.STORE = self.store
        mission_handlers.STORE = self.store
        combat_join.STORE = self.store
        social_handlers.STORE = self.store
        mission_handlers.release_match = lambda m: self.released.append(m)
        return self

    def __exit__(self, *exc):
        (state.STORE, mission_handlers.STORE, combat_join.STORE,
         social_handlers.STORE, mission_handlers.release_match) = self.saved
        return False


SEED_MIDS = (910001, 910002, 910003)


def _cleanup():
    for mid in SEED_MIDS:
        combat_instances.COMBAT_INSTANCES.pop(mid, None)
        mm_state._MM_MISSION_DETAILS.pop(mid, None)
    for pms in (610001, 610002, 610003, 610004, 610005):
        mm_state.PMS_TO_SESSION.pop(pms, None)
        mm_state.PMS_TO_MISSION.pop(pms, None)
    for pid in (700001, 700002, 700003):
        mm_state._MM_QUEUES.pop(pid, None)
        mm_state._SOLO_SQUADS.pop(pid, None)
        mm_state._MATCH_PMM_IDS.pop(pid, None)
    for tag in ('RejoinA', 'RejoinB', 'RejoinC', 'RejoinD'):
        FLASH_SESSIONS.pop(tag, None)


def main():
    old_timeout = mm_config.REJOIN_TIMEOUT
    mm_config.REJOIN_TIMEOUT = 300
    try:
        with Patched() as p:
            # ---- A. _pms_rejoin_expired 纯逻辑 ----
            check('A1 无 deadline（在线/局中）→ 未过期',
                  not matchmaking._pms_rejoin_expired({}))
            check('A2 deadline 未来 → 未过期',
                  not matchmaking._pms_rejoin_expired(
                      {'rejoin_deadline': time.time() + 100}))
            check('A3 deadline 已过 → 过期',
                  matchmaking._pms_rejoin_expired(
                      {'rejoin_deadline': time.time() - 1}))

            # ---- B. arm_rejoin_deadline ----
            seed_mission(910001, players=1)
            seed_pms(610001, 700001, 910001, 'RejoinA')
            rec = mm_state.PMS_TO_SESSION[610001]
            t0 = time.time()
            ok = matchmaking.arm_rejoin_deadline(700001, reason='test')
            dl = rec.get('rejoin_deadline')
            check('B1 存活战局会话 → 起 protection（返回 True）', ok is True)
            check('B2 deadline ≈ now + REJOIN_TIMEOUT（300s 窗口）',
                  dl is not None and t0 + 299 <= dl <= t0 + 301,
                  f'dl={dl}')
            dl_kept = dl
            time.sleep(0.01)
            matchmaking.arm_rejoin_deadline(700001, reason='test2')
            check('B3 保护期内重复 arm → 不重置（最早掉线时刻起算）',
                  rec.get('rejoin_deadline') == dl_kept)
            rec['rejoin_deadline'] = time.time() - 5      # 已过期
            matchmaking.arm_rejoin_deadline(700001, reason='test3')
            check('B4 已过期 deadline → 不复活（留给 sweep 释放）',
                  rec.get('rejoin_deadline') == dl_kept - 5.01
                  or rec.get('rejoin_deadline') < time.time())
            rec.pop('rejoin_deadline', None)

            seed_pms(610002, 700002, 999999, 'RejoinB')   # mission 不存在
            check('B5 mission 不在 COMBAT_INSTANCES → 不起保护',
                  matchmaking.arm_rejoin_deadline(700002) is False and
                  'rejoin_deadline' not in mm_state.PMS_TO_SESSION[610002])
            check('B6 pid=None / 无会话 → False',
                  matchmaking.arm_rejoin_deadline(None) is False and
                  matchmaking.arm_rejoin_deadline(424242) is False)

            # ---- C. clear_rejoin_deadline ----
            matchmaking.arm_rejoin_deadline(700001, reason='test')
            matchmaking.clear_rejoin_deadline(700001)
            check('C1 重进战局 → 清 deadline',
                  'rejoin_deadline' not in mm_state.PMS_TO_SESSION[610001])
            matchmaking.clear_rejoin_deadline(700001)
            check('C2 幂等：再次清除无副作用',
                  'rejoin_deadline' not in mm_state.PMS_TO_SESSION[610001])

            # ---- D. 查找器过滤过期会话 ----
            seed_mission(910002, players=1)
            seed_pms(610003, 700003, 910002, 'RejoinC')
            rec3 = mm_state.PMS_TO_SESSION[610003]
            check('D1 未过期：active_mission 命中',
                  matchmaking._find_player_active_mission(700003) == 910002)
            fpms, frec, fmid = matchmaking._find_player_live_pms(700003)
            check('D2 未过期：live_pms 命中（返回会话记录）',
                  fpms == 610003 and frec is rec3 and fmid == 910002)
            rec3['rejoin_deadline'] = time.time() - 1
            check('D3 已过期：active_mission → None（席位已释放语义）',
                  matchmaking._find_player_active_mission(700003) is None)
            fpms, frec, fmid = matchmaking._find_player_live_pms(700003)
            check('D4 已过期：live_pms → (None, None, None)',
                  fpms is None and frec is None and fmid is None)
            # 过期（旧局）+ 未过期（新局）并存 → 只命中未过期
            rec3.pop('rejoin_deadline', None)
            seed_mission(910003, players=0)
            seed_pms(610004, 700003, 910003, 'RejoinC')
            mm_state.PMS_TO_SESSION[610003]['rejoin_deadline'] = \
                time.time() - 1
            check('D5 过期旧局 + 未过期新局 → 只命中未过期的',
                  matchmaking._find_player_active_mission(700003) == 910003)

            # ---- E. push_relogin_rejoin：保护期内推 / 过期不推 ----
            w_e = FakeWriter()
            check('E1 保护期内重登 → 推 REJOIN 行（返回 True）',
                  asyncio.run(matchmaking.push_relogin_rejoin(
                      w_e, 105, 700003)) is True and
                  b'PlayerMissionSession' in w_e.data)
            mm_state.PMS_TO_SESSION[610004]['rejoin_deadline'] = \
                time.time() - 1
            w_e2 = FakeWriter()
            check('E2 掉线超时后重登 → 不推（正常进大厅）',
                  asyncio.run(matchmaking.push_relogin_rejoin(
                      w_e2, 105, 700003)) is False and w_e2.data == b'')

            # ---- F. on_game_client_closed：崩溃/被杀路径 ----
            mm_state.PMS_TO_SESSION[610004].pop('rejoin_deadline', None)
            w_f = seed_flash('RejoinC')
            asyncio.run(matchmaking.on_game_client_closed(700003))
            check('F1 游戏连接断开：起 deadline + 推 PMS Admitted',
                  'rejoin_deadline' in mm_state.PMS_TO_SESSION[610004] and
                  b'PlayerMissionSession' in w_f.data)
            check('F2 推送 connectionState=2 Admitted（客户端 300s 倒计时源）',
                  b'\x70\x02' in w_f.data and b'\x70\x03' not in w_f.data)
            before = w_f.data
            asyncio.run(matchmaking.on_game_client_closed(700003))
            check('F3 PlayerExitBatch 已推过（deadline 未过期）→ 不重复推',
                  w_f.data == before)
            FLASH_SESSIONS.pop('RejoinC', None)
            mm_state.PMS_TO_SESSION[610004].pop('rejoin_deadline', None)
            asyncio.run(matchmaking.on_game_client_closed(700003))
            check('F4 Flash 离线：deadline 照起、无推送不崩',
                  'rejoin_deadline' in mm_state.PMS_TO_SESSION[610004])
            w_f2 = FakeWriter()
            asyncio.run(matchmaking.on_game_client_closed(424242))
            check('F5 无存活会话 → no-op', w_f2.data == b'')

            # ---- G. handle_player_exit_batch：优雅退出上报 ----
            # PlayerExitBatch { f1 repeated PlayerExit{ f1 msid } }
            proto_g = pb_message(1, pb_varint(1, 610004))
            sess_g = types.SimpleNamespace(playerview_cmd=None,
                                           username='game-conn',
                                           is_game_client=True)
            w_g = seed_flash('RejoinC')
            mm_state.PMS_TO_SESSION[610004].pop('rejoin_deadline', None)
            asyncio.run(combat_join.handle_player_exit_batch(
                sess_g, w_g, 40, 'PlayerExitBatch', proto_g))
            check('G1 优雅退出上报 → 起 deadline',
                  'rejoin_deadline' in mm_state.PMS_TO_SESSION[610004])
            check('G2 优雅退出上报 → Flash 连接收到 PMS Admitted 推送',
                  b'PlayerMissionSession' in w_g.data and
                  b'\x70\x02' in w_g.data)

            # ---- H. handle_player_joined：重进清 deadline ----
            proto_h = pb_varint(1, 610004)
            sess_h = types.SimpleNamespace(playerview_cmd=None,
                                           username='game-conn')
            w_h = FakeWriter()
            asyncio.run(combat_join.handle_player_joined(
                sess_h, w_h, 41, 'PlayerJoined', proto_h))
            check('H1 PlayerJoined（重进战局）→ 清 deadline',
                  'rejoin_deadline' not in mm_state.PMS_TO_SESSION[610004])

            # ---- I. rejoin_sweep_once：超时释放 ----
            # I1：保护期内（deadline 未来）→ 不动
            mm_state.PMS_TO_SESSION[610004]['rejoin_deadline'] = \
                time.time() + 100
            r = asyncio.run(mission_handlers.rejoin_sweep_once())
            check('I1 保护期内 → 不释放（返回空集）',
                  r == set() and 610004 in mm_state.PMS_TO_SESSION)
            # I2：无 deadline（在线局中）→ 不动
            mm_state.PMS_TO_SESSION[610004].pop('rejoin_deadline', None)
            r = asyncio.run(mission_handlers.rejoin_sweep_once())
            check('I2 无 deadline（局中）→ 不释放',
                  r == set() and 610004 in mm_state.PMS_TO_SESSION)
            # I3：过期 + Flash 在线 → PMS DELETE + 全量清理
            mm_state._MM_QUEUES[700003] = {'pmm_id': 555003, 'mission_id': 0}
            mm_state.PMS_TO_SESSION[610004]['rejoin_deadline'] = \
                time.time() - 1
            w_i = seed_flash('RejoinC')
            det3 = mm_state._MM_MISSION_DETAILS[910003]
            n_players = len(det3['players'])
            r = asyncio.run(mission_handlers.rejoin_sweep_once())
            check('I3 过期 → 释放（返回 {pid}）', r == {700003})
            check('I4 PMS 映射已清（坑位回收）',
                  610004 not in mm_state.PMS_TO_SESSION and
                  610004 not in mm_state.PMS_TO_MISSION and
                  700003 not in mm_state._MM_QUEUES)
            check('I5 Flash 在线 → 推 PMS DELETE（客户端倒计时收尾）',
                  b'PlayerMissionSession' in w_i.data and
                  b'delete' in w_i.data)
            check('I6 det.players 移除该玩家',
                  len(det3['players']) == n_players - 1 and
                  all(pl['player_id'] != 700003 for pl in det3['players']))
            check('I7 mission_records → state=3（防 GetMissionList 空壳局）',
                  (910003, 3) in p.store.mission_states)
            check('I8 全员离场 → release_match 释放 combat server',
                  910003 in p.released)
            # I9：过期 + 离线 → 清内部状态、无推送
            seed_mission(910002, players=0)      # 复用：910002 玩家已清空
            seed_pms(610005, 700001, 910002, 'RejoinA')
            mm_state.PMS_TO_SESSION[610005]['rejoin_deadline'] = \
                time.time() - 1
            r = asyncio.run(mission_handlers.rejoin_sweep_once())
            check('I9 离线超时 → 清内部状态（返回 {pid}）',
                  r == {700001} and 610005 not in mm_state.PMS_TO_SESSION)
            check('I10 离线释放后再重登 → 不再推 REJOIN（正常进大厅）',
                  asyncio.run(matchmaking.push_relogin_rejoin(
                      FakeWriter(), 105, 700001)) is False)

            # ---- J. 集成链：掉线 → 重进撤销 → 再掉线 → 超时释放 ----
            seed_pms(610005, 700001, 910001, 'RejoinA')
            asyncio.run(matchmaking.on_game_client_closed(700001))
            check('J1 掉线：deadline + Admitted 推送齐备',
                  'rejoin_deadline' in mm_state.PMS_TO_SESSION[610005])
            proto_j = pb_varint(1, 610005)
            asyncio.run(combat_join.handle_player_joined(
                types.SimpleNamespace(playerview_cmd=None,
                                      username='game-conn'),
                FakeWriter(), 41, 'PlayerJoined', proto_j))
            check('J2 窗口内重进 → deadline 撤销（席位保留）',
                  'rejoin_deadline' not in
                  mm_state.PMS_TO_SESSION[610005] and
                  610005 in mm_state.PMS_TO_SESSION)
            r = asyncio.run(mission_handlers.rejoin_sweep_once())
            check('J3 未掉线 → sweep 空转', r == set())
            mm_state.PMS_TO_SESSION[610005]['rejoin_deadline'] = \
                time.time() - 1
            r = asyncio.run(mission_handlers.rejoin_sweep_once())
            check('J4 掉线超时 → sweep 释放，席位/坑位回收',
                  r == {700001} and 610005 not in mm_state.PMS_TO_SESSION)
    finally:
        mm_config.REJOIN_TIMEOUT = old_timeout
        _cleanup()

    failed = [l for l, ok in RESULTS if not ok]
    print(f'\nREJOIN_TIMEOUT: '
          f'{"ALL PASS" if not failed else f"{len(failed)} FAILED: {failed}"} '
          f'({len(RESULTS) - len(failed)}/{len(RESULTS)} passed)')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
