# -*- coding: utf-8 -*-
"""_start_match 成局链回归（DB-free 夹具直调两种分支）。

背景（2026-09-14 线上事故）：war 分支引入 war_activation/war_entity 后，
普通局 else 分支漏赋 war_entity → det 构造无条件引用 → 普通匹配每次成局
抛 NameError 全部失败。此前 DB-free 测试都止步于撮合池（try_form），
未覆盖 _start_match 的 det 构造——本测试补上该盲区：

- 普通局（battle_id=0）：成局不抛异常，det 激活时刻/实体口径为 None
  （渲染层回退 zulu/record 旧口径），成局后字段与旧行为一致；
- war 局（battle_id>0）：det 带真实 battle_id + battle 行实体口径，
  record state 0→1。

夹具：FakeStore（get_config/get_mission_by_battle/get_war_battle/
get_command_nodes_by_ids/set_mission_state）+ monkeypatch
combat_hosts.load_hosts（fallback 单机）与 _ensure_combat_server（no-op，
不 spawn）；玩家 ctx 无 writer → 进局推送自然跳过。
"""
import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import combat_hosts
import force_pool
import matchmaking
import state

RESULTS = []


def check(label, ok, detail=''):
    RESULTS.append((label, ok))
    print(f'[{"PASS" if ok else "FAIL"}] {label}{("  " + detail) if detail else ""}')


class FakeStore:
    """_start_match 两种分支的最小 STORE 面。"""

    def __init__(self, by_battle=None, battles=None, nodes=None):
        self.by_battle = by_battle or {}
        self.battles = battles or {}
        self.nodes = nodes or {}
        self.state_updates = []

    async def get_config(self, key):
        return None

    async def get_mission_by_battle(self, battle_id, states=(0, 1)):
        return self.by_battle.get(int(battle_id))

    async def get_war_battle(self, battle_id):
        return self.battles.get(int(battle_id))

    async def get_command_nodes_by_ids(self, ids):
        return [self.nodes[int(i)] for i in ids if int(i) in self.nodes]

    async def set_mission_state(self, mid, st):
        self.state_updates.append((int(mid), int(st)))


def _mk_entry(pid, faction):
    from mm_pool import QueueEntry
    return QueueEntry(player_id=pid, char_id=pid, faction=faction,
                      gamemode_id=5, presets=(10,), group_key=0)


async def _run_match(match, store):
    orig_store, orig_hosts, orig_spawn = (state.STORE,
                                          combat_hosts.load_hosts,
                                          matchmaking._ensure_combat_server)
    state.STORE = store

    async def fake_hosts():
        return [combat_hosts.fallback_host()]

    async def fake_spawn(mid, pms, combat_map):
        return None

    combat_hosts.load_hosts = fake_hosts
    matchmaking._ensure_combat_server = fake_spawn
    try:
        await matchmaking._start_match(match)
    finally:
        state.STORE = orig_store
        combat_hosts.load_hosts = orig_hosts
        matchmaking._ensure_combat_server = orig_spawn


def _cleanup(mid):
    matchmaking._MM_MISSION_DETAILS.pop(mid, None)
    inst = matchmaking.COMBAT_INSTANCES.pop(mid, None) \
        if hasattr(matchmaking, 'COMBAT_INSTANCES') else None
    from combat_instances import COMBAT_INSTANCES as CI
    CI.pop(mid, None)
    matchmaking.PMS_TO_MISSION.pop(mid if isinstance(mid, int) else 0, None)
    for pms in [k for k, v in matchmaking.PMS_TO_MISSION.items() if v == mid]:
        matchmaking.PMS_TO_MISSION.pop(pms, None)
        matchmaking.PMS_TO_SESSION.pop(pms, None)
    matchmaking._MM_MISSION_SQUADS.pop(mid, None)
    force_pool.release(mid)
    return inst


async def main():
    # ---- N1 普通局（battle_id=0）成局：回归 NameError（war_entity 未定义）----
    store = FakeStore()
    match = SimpleNamespace(battle_id=0, preset_id=10, ai_fill=False,
                            players=[_mk_entry(101, 1), _mk_entry(102, 2)])
    err = None
    try:
        await _run_match(match, store)
    except Exception as ex:
        err = ex
    mid = getattr(match, 'mission_id', 0)
    check('N1 普通局 _start_match 成局不抛异常（回归 war_entity NameError）',
          err is None and mid, f'err={err!r} mid={mid}')
    det = matchmaking._MM_MISSION_DETAILS.get(mid) if mid else None
    check('N1b det 已登记且 battle_id=0',
          det is not None and det.get('battle_id') == 0,
          f'det={None if det is None else "ok"}')
    check('N1c 普通局 activation/entity 为 None（渲染层回退旧口径）',
          det is not None and det.get('activation') is None
          and det.get('entity_id') is None
          and det.get('entity_type') is None,
          f'act={det.get("activation") if det else None} '
          f'ent={det.get("entity_id") if det else None}')
    check('N1d 双方玩家登记进 det.players',
          det is not None and
          {p['player_id'] for p in det['players']} == {101, 102},
          f'players={[(p["player_id"], p["faction"]) for p in det["players"]] if det else None}')
    if mid:
        _cleanup(mid)

    # ---- W1 war 局（battle_id>0）：det 带真实 battleId + battle 行口径 ----
    wrec = {'id': 9001, 'preset': 10, 'attacker_faction': 1,
            'defender_faction': 2, 'map_entity_id': 20}
    wb = {'id': 77, 'battle_type': 1, 'battlefield_id': 20,
          'started_at': None,
          'factions': []}
    store = FakeStore(by_battle={77: wrec}, battles={77: wb})
    match = SimpleNamespace(battle_id=77, preset_id=10, ai_fill=False,
                            players=[_mk_entry(201, 1), _mk_entry(202, 2)])
    err = None
    try:
        await _run_match(match, store)
    except Exception as ex:
        err = ex
    det = matchmaking._MM_MISSION_DETAILS.get(9001)
    check('W1 war 局成局不抛异常', err is None, f'err={err!r}')
    check('W1b det.battle_id=77 / map_entity=record 城市id',
          det is not None and det.get('battle_id') == 77
          and det.get('map_entity_id') == 20,
          f'det={None if det is None else {k: det.get(k) for k in ("battle_id", "map_entity_id")}}')
    check('W1c det 实体口径 = battle 行（城战 bf/1）',
          det is not None and det.get('entity_id') == 20
          and det.get('entity_type') == 1,
          f'ent={det.get("entity_id") if det else None}/'
          f'{det.get("entity_type") if det else None}')
    check('W1d record state 0→1（GetMissionList status）',
          (9001, 1) in store.state_updates,
          f'updates={store.state_updates}')
    _cleanup(9001)

    failed = [l for l, ok in RESULTS if not ok]
    print(f'==== start_match_formation 回归：'
          f'{len(RESULTS) - len(failed)}/{len(RESULTS)} PASS ====')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    asyncio.run(main())
