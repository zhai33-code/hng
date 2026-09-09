# -*- coding: utf-8 -*-
"""伞兵资源保底回归测试（2026-09-09 VPS 实锤：伞兵进 capture_hold_inf 局判负）。

链路：JoinMatchMaking 入池带 char_cat（wardb.character_resource_category，
兵种模板 → 士兵资源类别，伞兵=4）→ 成局 _start_match 把角色类别并入
require_cats（force_pool.init_mission 查缺补齐——capture_hold_inf 计划
{infantry:5, recon:1} 天然无 cat4，补齐链追加 paratroopers AT）→ 等待期
late join 过滤（_find_joinable_mission char_cat 过滤，池缺类别不放行）。

实锤现象：池无 cat4 → 引擎开局建伞兵小队失败 → MissionStart 后 1s
ReportDefeatedFaction 整队判负（连输 3 局；同玩家进 assault 局——计划含
para:1——则正常）。

用例：
 A. character_resource_category 映射（真实 WarDatabase 锚点）
 B. 复现实锤 + 修复后建池（capture_hold_inf = gamemode 3）
 C. late join 过滤：池缺 char_cat 不放行 / 有则放行 / 步兵不受影响
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HERE = os.path.dirname(os.path.abspath(__file__))

# WarDatabase 目录自动探测（工作区 HnG/ 官方客户端拷贝）
for _cand in (os.environ.get('HNG_GAME_DIR'),
              os.path.abspath(os.path.join(_HERE, '..', '..', 'HnG'))):
    if _cand and os.path.isdir(os.path.join(_cand, 'WarDatabase')):
        os.environ['HNG_GAME_DIR'] = _cand
        break

import mm_config                                        # noqa: E402
import mm_state                                         # noqa: E402
import matchmaking                                      # noqa: E402
import combat_instances                                 # noqa: E402
import force_pool                                       # noqa: E402
import mm_pool                                          # noqa: E402
from wardb import character_resource_category           # noqa: E402


RESULTS = []


def check(label, ok, detail=''):
    RESULTS.append((label, ok))
    print(f'[{"PASS" if ok else "FAIL"}] {label}{("  " + detail) if detail else ""}')


def seed_mission(mid, *, gamemode=3, preset=373394539, attacker=1, defender=2,
                 age=5.0):
    """登记一个（伪）运行中战局（结构同 test_mm_latejoin.seed_mission）。"""
    inst = {'mission_id': mid, 'port': 5600 + mid % 100, 'proc': None,
            'ready': asyncio.Event(), 'seq': 1, 'ttd': False, 'udp': None}
    inst['ready'].set()
    det = {'preset': preset, 'gamemode': gamemode, 'ai_fill': False,
           'map_entity_id': mm_state.MISSION_MAP_ENTITY_ID,
           'attacker_faction': attacker, 'defender_faction': defender,
           'enable_bots': True,
           'weather_tod': 2, 'weather_condition': 0, 'timeofday': None,
           'players': [{'player_id': 1000, 'gamertag': 'P0',
                        'char_id': 2000, 'faction': attacker,
                        'pms_id': 3000}]}
    det['formed_at'] = time.monotonic() - age
    combat_instances.COMBAT_INSTANCES[mid] = inst
    mm_state._MM_MISSION_DETAILS[mid] = det
    return inst, det


def main():
    old_window = mm_config.JOIN_WINDOW
    mm_config.JOIN_WINDOW = 90
    pools = []
    try:
        # ---- A. 兵种模板 → 士兵资源类别映射 ----
        # 真实模板 id（WarDatabase charactertemplate）：1 rifleman /
        # 2 fighterpilot / 4 paratrooper / 5 tankcrew / 6 recon
        check('A1: rifleman(1) → cat1',
              character_resource_category(1) == 1)
        check('A2: fighterpilot(2) → cat2',
              character_resource_category(2) == 2)
        check('A3: paratrooper(4) → cat4（伞兵）',
              character_resource_category(4) == 4)
        check('A4: tankcrew(5) → cat5',
              character_resource_category(5) == 5)
        check('A5: recon(6) → cat6',
              character_resource_category(6) == 6)
        check('A6: 未知模板 → 0',
              character_resource_category(99999) == 0)

        # ---- B. 复现实锤 + 修复后建池（capture_hold_inf = 模式 3）----
        force_pool.init_mission(900400, 3, 1, 2)
        pools.append(900400)
        p0 = force_pool.pool(900400)
        check('B1: 复现——无 require 时 capture_hold_inf 池无 cat4（实锤条件）',
              all(p.get(4, 0) == 0 for p in p0.values()), f'pool={p0}')
        force_pool.init_mission(900401, 3, 1, 2, require_cats={4})
        pools.append(900401)
        p1 = force_pool.pool(900401)
        check('B2: require {4} → 双方阵营 cat4 > 0（伞兵资源保底）',
              all(p.get(4, 0) > 0 for p in p1.values()), f'pool={p1}')
        check('B3: 保底不破坏基础步兵池（cat1 > 0）',
              all(p.get(1, 0) > 0 for p in p1.values()))

        # ---- C. late join 过滤 ----
        seed_mission(900402)                    # 池 = 无 require 口径
        force_pool.init_mission(900402, 3, 1, 2)
        pools.append(900402)
        check('C1: 伞兵 late join 池缺 cat4 → 不放行（防进局判负）',
              matchmaking._find_joinable_mission(
                  3, (373394539,), 1, char_cat=4) is None)
        seed_mission(900403)                    # 池 = require {4} 口径
        force_pool.init_mission(900403, 3, 1, 2, require_cats={4})
        pools.append(900403)
        r = matchmaking._find_joinable_mission(3, (373394539,), 1, char_cat=4)
        check('C2: 伞兵 late join 池有 cat4 → 放行（跳过缺池局命中 900403）',
              r is not None and r[0] == 900403)
        r = matchmaking._find_joinable_mission(3, (373394539,), 1)
        check('C3: 无 char_cat（步兵/旧调用方）→ 不受过滤影响',
              r is not None and r[0] == 900402)
        check('C4: QueueEntry.char_cat 字段存在（入池口径）',
              'char_cat' in mm_pool.QueueEntry.__dataclass_fields__)
    finally:
        mm_config.JOIN_WINDOW = old_window
        for mid in pools:
            force_pool.release(mid)
            combat_instances.COMBAT_INSTANCES.pop(mid, None)
            mm_state._MM_MISSION_DETAILS.pop(mid, None)

    failed = [l for l, ok in RESULTS if not ok]
    print(f'\nPARA_RESOURCE_REQUIRE: '
          f'{"ALL PASS" if not failed else f"{len(failed)} FAILED: {failed}"} '
          f'({len(RESULTS) - len(failed)}/{len(RESULTS)} passed)')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
