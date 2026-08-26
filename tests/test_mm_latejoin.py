"""等待期中途加入（late join）单测：_find_joinable_mission 纯逻辑 +
_join_existing_mission 会话登记/推送序列（假 writer，无需服务器/DB 写）。

场景（对应 matchmaking.py 1c 步骤）：
 1. 窗口内同模式选图兼容 → 可加入
 2. 不同 gamemode / 选图无交集 / 第三方阵营 / 超窗 / 满员 / 实例已释放 → 不可
 3. 未选图（通配）→ 可加入
 4. formed_at 缺失（旧结构战局）→ 视为超窗不可加入
 5. JOIN_WINDOW=0 → 功能关闭
 6. only_mission（f2 指定战局）→ 只匹配该局
 7. _join_existing_mission：登记 PMS_TO_MISSION/PMS_TO_SESSION、追加
    details['players']、推出 PMS Admitted/Armed + PMM delete + BattleInfo
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import matchmaking
import mm_config
import mm_pool


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


def seed_mission(mid, *, gamemode=5, preset=10, attacker=3, defender=2,
                 players=1, age=0.0, formed=True):
    """登记一个（伪）运行中战局；返回 (inst, det)。"""
    inst = {'mission_id': mid, 'port': 5600 + mid % 100, 'proc': None,
            'ready': asyncio.Event(), 'seq': 1, 'ttd': False, 'udp': None}
    inst['ready'].set()
    det = {'preset': preset, 'gamemode': gamemode, 'ai_fill': False,
           'map_entity_id': matchmaking.MISSION_MAP_ENTITY_ID,
           'attacker_faction': attacker, 'defender_faction': defender,
           'enable_bots': True,
           'weather_tod': 2, 'weather_condition': 0, 'timeofday': None,
           'players': [{'player_id': 1000 + i, 'gamertag': f'P{i}',
                        'char_id': 2000 + i, 'faction': attacker,
                        'pms_id': 3000 + i} for i in range(players)]}
    if formed:
        det['formed_at'] = time.monotonic() - age
    matchmaking.COMBAT_INSTANCES[mid] = inst
    matchmaking._MM_MISSION_DETAILS[mid] = det
    return inst, det


def main():
    old_window = mm_config.JOIN_WINDOW
    old_gm5 = mm_config.GAMEMODES[5]
    mm_config.JOIN_WINDOW = 90
    try:
        # ---- 1. 窗口内同模式兼容 ----
        seed_mission(900001, gamemode=5, preset=10, players=1, age=5)
        r = matchmaking._find_joinable_mission(5, (10,), 3)
        check('窗口内同模式同图 → 可加入', r is not None and r[0] == 900001)
        check('返回的是运行实例', r is not None and r[1] is
              matchmaking.COMBAT_INSTANCES[900001])

        # ---- 2. 各不兼容情形（only_mission 隔离，防被其它可加入局命中） ----
        check('不同 gamemode → 不可加入',
              matchmaking._find_joinable_mission(3, (10,), 3) is None)
        check('选图无交集 → 不可加入',
              matchmaking._find_joinable_mission(5, (26, 27), 3) is None)
        check('第三方阵营 → 不可加入',
              matchmaking._find_joinable_mission(5, (10,), 1) is None)
        seed_mission(900002, gamemode=5, preset=10, players=1, age=200)
        check('超窗（age=200s > 90s）→ 不可加入',
              matchmaking._find_joinable_mission(5, (10,), 3,
                                                  only_mission=900002) is None)
        seed_mission(900003, gamemode=5, preset=10, players=1, age=5)
        mm_config.GAMEMODES[5] = dict(old_gm5, max_players=2)
        seed_mission(900004, gamemode=5, preset=10, players=2, age=5)
        check('满员（players=2 >= max=2）→ 不可加入',
              matchmaking._find_joinable_mission(5, (10,), 3,
                                                  only_mission=900004) is None)
        check('实例已释放（不在 COMBAT_INSTANCES）→ 不可加入',
              matchmaking._find_joinable_mission(
                  5, (10,), 3, only_mission=999999) is None)
        seed_mission(900005, gamemode=5, preset=10, players=1, formed=False)
        check('formed_at 缺失（旧结构）→ 不可加入',
              matchmaking._find_joinable_mission(5, (10,), 3,
                                                  only_mission=900005) is None)

        # ---- 3. 通配 / 防守方阵营 / 多实例择 earliest ----
        # 池内现有 900001(可加入) 900002(超窗) 900003(可加入) 900004(满员)
        #   900005(无 formed_at) → 全局搜索应跳过后四者命中 900001
        r = matchmaking._find_joinable_mission(5, (10,), 3)
        check('多实例：跳过超窗/满员/无 formed_at，命中最早可加入局',
              r is not None and r[0] == 900001)
        r = matchmaking._find_joinable_mission(5, (), 3)
        check('未选图通配 → 可加入', r is not None and r[0] == 900001)
        r = matchmaking._find_joinable_mission(5, (), 2)
        check('防守方阵营（defender=2）→ 可加入',
              r is not None and r[0] == 900001)

        # ---- 4. JOIN_WINDOW=0 → 关闭 ----
        mm_config.JOIN_WINDOW = 0
        check('JOIN_WINDOW=0 → 功能关闭',
              matchmaking._find_joinable_mission(5, (10,), 3) is None)
        mm_config.JOIN_WINDOW = 90

        # ---- 5. only_mission 指定战局 ----
        r = matchmaking._find_joinable_mission(5, (10,), 3, only_mission=900003)
        check('only_mission 命中指定局', r is not None and r[0] == 900003)
        r = matchmaking._find_joinable_mission(5, (10,), 3, only_mission=900004)
        check('only_mission 指向满员局 → 不可加入', r is None)
        r = matchmaking._find_joinable_mission(5, (26,), 3, only_mission=900001)
        check('only_mission 但选图不含该局 preset → 不可加入', r is None)

        # ---- 6. _join_existing_mission 全流程（假 writer） ----
        w = FakeWriter()
        pid, char_id = 777, 888
        ctx = {'writer': w, 'view_cmd': 100, 'gamertag': 'LateJoiner',
               'char_name': 'LateJoiner', 'rank': 3, 'metrics_uid': 'uid-x',
               'metrics_session_id': 424242}
        matchmaking._MM_QUEUES[pid] = {'pmm_id': 555001, 'mission_id': 0,
                                       'gamemode': 5, 'ctx': ctx}
        matchmaking._MATCH_PMM_IDS[pid] = 555001
        entry = mm_pool.QueueEntry(player_id=pid, char_id=char_id, faction=3,
                                   gamemode_id=5, presets=(10,))
        det = matchmaking._MM_MISSION_DETAILS[900001]
        n_before = len(det['players'])
        pms_before = set(matchmaking.PMS_TO_SESSION)
        asyncio.run(matchmaking._join_existing_mission(pid, 900001, entry,
                                                        ctx))
        new_pms = set(matchmaking.PMS_TO_SESSION) - pms_before
        check('中途加入：登记了新 pms 会话', len(new_pms) == 1)
        if new_pms:
            pms_id = new_pms.pop()
            check('中途加入：pms 映射到目标战局',
                  matchmaking.PMS_TO_MISSION.get(pms_id) == 900001)
            rec = matchmaking.PMS_TO_SESSION[pms_id]
            check('中途加入：会话字段正确',
                  rec['player_id'] == pid and rec['char_id'] == char_id
                  and rec['faction'] == 3
                  and rec['metrics_session_id'] == 424242)
        check('中途加入：details.players 追加 1 人',
              len(det['players']) == n_before + 1 and
              det['players'][-1]['player_id'] == pid)
        check('中途加入：排队上下文已清理',
              pid not in matchmaking._MM_QUEUES and
              pid not in matchmaking._MATCH_PMM_IDS)
        # KVCS 序列：PMS(Admitted+Armed) + PMM delete + BattleInfo
        data = w.data
        check('推送序列：PlayerMissionSession ×2（Admitted+Armed）',
              data.count(b'PlayerMissionSession') == 2)
        check('推送序列：PMM delete 已推',
              b'delete' in data and b'PlayerMatchMaking' in data)
        check('推送序列：BattleInfo 已推', b'BattleInfo' in data)

        # ---- 清理（模块级全局，防污染其它测试） ----
        for mid in (900001, 900002, 900003, 900004, 900005):
            matchmaking.COMBAT_INSTANCES.pop(mid, None)
            matchmaking._MM_MISSION_DETAILS.pop(mid, None)
        for pms_id in list(matchmaking.PMS_TO_SESSION):
            if matchmaking.PMS_TO_MISSION.get(pms_id) in (
                    900001, 900002, 900003, 900004, 900005):
                matchmaking.PMS_TO_MISSION.pop(pms_id, None)
                matchmaking.PMS_TO_SESSION.pop(pms_id, None)
        matchmaking._MM_QUEUES.pop(pid, None)
        matchmaking._MATCH_PMM_IDS.pop(pid, None)
        matchmaking._SOLO_SQUADS.pop(pid, None)
    finally:
        mm_config.JOIN_WINDOW = old_window
        mm_config.GAMEMODES[5] = old_gm5

    failed = [l for l, ok in RESULTS if not ok]
    print(f'\nMM_LATEJOIN: {"ALL PASS" if not failed else f"{len(failed)} FAILED: {failed}"} '
          f'({len(RESULTS) - len(failed)}/{len(RESULTS)} passed)')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())

