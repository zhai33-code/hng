# -*- coding: utf-8 -*-
"""RTS AT 移动系统回归（war_campaign.py Phase 6，DB-free 合成拓扑）。

覆盖：
  A. 拓扑/寻路：supplyline_adjacency 邻接、find_supplyline_path BFS
     （同地 []、不可达 None、路径连续性）
  B. segment_seconds：坐标距离公式 + 无坐标回退 SEGMENT_FALLBACK_SECONDS
     + 地形减速（#2：跨海段距离≥阈值、中立段两端城市均非己方）
  C. start_movement：movement 落库、AT 置在途（首段补给线）、同地 ok、
     不可达 fail、overrideMove 0 时长段、同城出兵点立即落位、dest_ap 持久化
  D. advance_movements：换段推送（segment+CommandNodeWarInstance）、
     到达落位（城市 type1 / 出兵点 type2）、会战成局（新建 + 加入既有）
  E. transport_commandnode_segment / destination 行字段
  F. war_tick 冒烟（advance + broadcast 接线）
  G. M3-3：fow 网格参数（war f19-21 ← mm_config）+ 空运
     （start_air_transport 消耗 cat10 / air 段 / 到达落位成局 / 快照行）

合成拓扑（monkeypatch wardb 三表 + 重置 war_campaign 缓存）：
  城市 10(0,0) --sl101-- 城市 20(300,400) --sl102-- 城市 30(600,800)
  出兵点 ap1→bf10 / ap2→bf20 / ap3→bf30；城市 40 无补给线（不可达）。
"""
import asyncio
import datetime
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol import pb_parse
import mm_config
import wardb
import war_campaign

RESULTS = []


def check(label, ok, detail=''):
    RESULTS.append((label, ok))
    print(f'[{"PASS" if ok else "FAIL"}] {label}{("  " + detail) if detail else ""}')


def _mr_id(row):
    """missionresource 行主键 f1（int64 varint）→ AT id。
    （曾误按 sfixed64/wt1 解，P3 conformance roundtrip 抓出后改正。）"""
    return int(row[1])


# ------------------------------------------------------------- 合成拓扑

# 取"昨天"确保 war_tick 冒烟用真实 now（必然晚于合成起点）能推进入途
NOW = datetime.datetime(2026, 9, 1, 12, 0, 0,
                        tzinfo=datetime.timezone.utc)

_SYN_APS = [
    {'id': 1, 'mapid': 1, 'template': 0, 'battlefield': 10},
    {'id': 2, 'mapid': 1, 'template': 0, 'battlefield': 20},
    {'id': 3, 'mapid': 1, 'template': 0, 'battlefield': 30},
    {'id': 4, 'mapid': 1, 'template': 0, 'battlefield': 40},
]
_SYN_SLS = [
    {'id': 101, 'mapid': 1, 'ap1': 1, 'ap2': 2, 'template_id': 0},
    {'id': 102, 'mapid': 1, 'ap1': 2, 'ap2': 3, 'template_id': 0},
]
_SYN_BFS = [
    {'id': 10, 'mapid': 1, 'title': 'A', 'sector': '', 'posx': 0.0,
     'posy': 0.0, 'gamemap': 0},
    {'id': 20, 'mapid': 1, 'title': 'B', 'sector': '', 'posx': 300.0,
     'posy': 400.0, 'gamemap': 0},
    {'id': 30, 'mapid': 1, 'title': 'C', 'sector': '', 'posx': 600.0,
     'posy': 800.0, 'gamemap': 0},
    {'id': 40, 'mapid': 1, 'title': 'D', 'sector': '', 'posx': 900.0,
     'posy': 1200.0, 'gamemap': 0},
]

wardb.accesspoints = lambda: list(_SYN_APS)
wardb.supplylines = lambda: list(_SYN_SLS)
wardb.battlefields = lambda: list(_SYN_BFS)
wardb.capitals = lambda: []
wardb.commandnode_templates = lambda: {}
wardb.morale_template = lambda: None
wardb.armyresource_production = lambda: []   # 禁用 M3 种子库存（合成环境）
war_campaign.common._ADJACENCY_CACHE = None
war_campaign.common._BF_POS_CACHE = None

SCALE = (mm_config.WAR_TIMESCALE or 1.0) * (mm_config.WAR_AT_MOVE_SCALE or 1.0)
SEG_SEC = 500.0 / SCALE           # 10-20 与 20-30 距离均 = hypot(300,400)


# ------------------------------------------------------------- FakeStore

class FakeStore:
    """war_campaign 移动链路最小存储接口。"""

    def __init__(self):
        self.pool = object()
        self.wars = {}
        self.next_war_id = 1
        self.city_owners = {}
        self.ats = {}
        self.movements = {}
        self.next_mv_id = 1
        self.battles = {}
        self.next_battle_id = 1
        self.mission_records = {}

    # ---- war ----
    async def get_active_war(self):
        for w in self.wars.values():
            if w['state'] == 1:
                return w
        return None

    async def create_war(self, name, points_needed_for_victory=15,
                         mapid=1, config=None):
        wid = self.next_war_id
        self.next_war_id += 1
        w = {'id': wid, 'name': name,
             'begintime': NOW, 'endtime': None, 'winning_faction_id': 0,
             'points_needed_for_victory': points_needed_for_victory,
             'sequel_war_id': 0, 'mapid': mapid, 'state': 1,
             'config': config or {}}
        self.wars[wid] = w
        return w

    async def init_war_cities(self, war_id, owner_map):
        self.city_owners[war_id] = dict(owner_map)

    async def init_war_faction_resources(self, war_id, initial):
        # M3 种子库存（armyresource_production 已 patch 为 [] → no-op）
        self.stock = getattr(self, 'stock', {})
        self.stock.setdefault(int(war_id), {})

    async def get_war_cities(self, war_id):
        return dict(self.city_owners.get(int(war_id), {}))

    async def set_city_owner(self, war_id, battlefield_id, owner_faction):
        self.city_owners.setdefault(int(war_id), {})[int(battlefield_id)] = \
            int(owner_faction)

    async def create_mission_record(self, preset, **kw):
        self.next_mission_id = getattr(self, 'next_mission_id', 500) + 1
        m = {'id': self.next_mission_id, 'state': 0, 'preset': preset}
        m.update(kw)
        self.mission_records[m['id']] = m
        return m['id']          # 与真实 store 契约一致（返回 mission id）

    async def update_war_production(self, war_id, production_ms):
        return True

    async def list_war_supply_queue(self, war_id, faction=None,
                                    command_node_id=None):
        return []

    async def dequeue_war_supply(self, row_ids):
        return True

    async def list_wars(self, limit=20):
        return [dict(w) for w in
                sorted(self.wars.values(), key=lambda w: -w['id'])[:limit]]

    # ---- AT ----
    async def get_command_node(self, node_id):
        a = self.ats.get(int(node_id or 0))
        return dict(a) if a else None

    async def list_war_command_nodes(self, war_id):
        return [dict(a) for a in self.ats.values()
                if a['war_id'] == war_id]

    async def update_command_node(self, node_id, **fields):
        a = self.ats.get(int(node_id))
        if a is None:
            return False
        a.update(fields)
        return True

    # ---- movement ----
    async def create_movement(self, commandnode_id, war_id, path,
                              started_at=None, arrive_at=None, dest_ap=0):
        mv = {'id': self.next_mv_id, 'commandnode_id': int(commandnode_id),
              'war_id': int(war_id), 'path': [dict(s) for s in (path or [])],
              'started_at': started_at, 'arrive_at': arrive_at,
              'state': 0, 'dest_ap': int(dest_ap or 0)}
        self.next_mv_id += 1
        self.movements[mv['id']] = mv
        return dict(mv)

    async def get_active_movement(self, commandnode_id):
        for mv in sorted(self.movements.values(), key=lambda m: -m['id']):
            if mv['commandnode_id'] == int(commandnode_id) and mv['state'] == 0:
                return dict(mv)
        return None

    async def get_last_movement(self, commandnode_id):
        mvs = [m for m in self.movements.values()
               if m['commandnode_id'] == int(commandnode_id)]
        return dict(max(mvs, key=lambda m: m['id'])) if mvs else None

    async def list_active_movements(self, war_id):
        return [dict(mv) for mv in sorted(self.movements.values(),
                                          key=lambda m: m['id'])
                if mv['war_id'] == int(war_id) and mv['state'] == 0]

    async def finish_movement(self, movement_id, state=1):
        mv = self.movements.get(int(movement_id))
        if mv is None or mv['state'] != 0:
            return False
        mv['state'] = int(state)
        return True

    async def update_movement_path(self, movement_id, path,
                                   arrive_at=None, dest_ap=0):
        mv = self.movements.get(int(movement_id))
        if mv is None or mv['state'] != 0:
            return None
        mv['path'] = [dict(s) for s in (path or [])]
        if arrive_at is not None:
            mv['arrive_at'] = arrive_at
        mv['dest_ap'] = int(dest_ap or 0)
        return dict(mv)

    # ---- war_battles ----
    async def create_war_battle(self, war_id, battlefield_id, factions,
                                battle_type=1, position=0.0):
        wb = {'id': self.next_battle_id, 'war_id': int(war_id),
              'battlefield_id': int(battlefield_id),
              'factions': [dict(f) for f in (factions or [])],
              'battle_type': int(battle_type or 1),
              'position': float(position or 0.0),
              'state': 0, 'started_at': NOW, 'ended_at': None}
        self.next_battle_id += 1
        self.battles[wb['id']] = wb
        return dict(wb)

    async def append_war_battle_faction(self, battle_id, at_id, faction):
        b = self.battles.get(int(battle_id))
        if b is None:
            return None
        if not any(int(f.get('at_id') or 0) == int(at_id)
                   for f in b['factions']):
            b['factions'].append({'at_id': int(at_id),
                                  'faction': int(faction or 0)})
        return [dict(f) for f in b['factions']]

    async def shift_movement_started(self, movement_id, seconds):
        mv = self.movements.get(int(movement_id))
        if mv is None or mv['state'] != 0:
            return False
        mv['started_at'] = (mv['started_at']
                            + datetime.timedelta(seconds=float(seconds or 0)))
        return True

    async def list_active_war_battles(self, war_id):
        return [dict(wb) for wb in sorted(self.battles.values(),
                                          key=lambda b: b['id'])
                if wb['war_id'] == int(war_id) and wb['state'] == 0]

    async def get_war_battle(self, battle_id):
        b = self.battles.get(int(battle_id))
        return dict(b) if b else None

    async def end_war_battle(self, battle_id, winning_faction=0):
        b = self.battles.get(int(battle_id))
        if b is not None:
            b['state'] = 1
            b['winning_faction'] = int(winning_faction)
            b['ended_at'] = NOW
        return True

    # ---- settle_war_battle 依赖（I 段）----
    async def count_war_city_owners(self, war_id, battlefield_ids=None):
        wanted = (None if battlefield_ids is None
                  else set(int(b) for b in battlefield_ids))
        counts = {}
        for bf, owner in self.city_owners.get(int(war_id), {}).items():
            if owner > 0 and (wanted is None or bf in wanted):
                counts[owner] = counts.get(owner, 0) + 1
        return counts

    async def get_mission_record(self, mission_id):
        return self.mission_records.get(int(mission_id))

    async def set_mission_state(self, mission_id, state):
        m = self.mission_records.get(int(mission_id))
        if m is not None:
            m['state'] = int(state)

    async def get_mission_by_battle(self, battle_id):
        for m in self.mission_records.values():
            if int(m.get('battle_id') or 0) == int(battle_id):
                return dict(m)
        return None

    async def get_command_nodes_by_ids(self, ids):
        return [dict(self.ats[int(i)]) for i in ids or []
                if int(i) in self.ats]

    async def add_war_funds(self, pid, amount, reason=''):
        self.funds_log = getattr(self, 'funds_log', [])
        self.funds_log.append((int(pid), int(amount), reason))

    async def sweep_ended_war_leftovers(self):
        return [], []


def _mk_at(store, war_id, at_id, faction, bf):
    at = {'id': at_id, 'war_id': war_id, 'owner_player_id': 100 + at_id,
          'faction': faction, 'template_id': 0, 'name': f'AT{at_id}',
          'abbreviation': 'AT', 'rank': 1, 'xp': 0,
          'resources': {}, 'morale': 100, 'max_morale': 100,
          'state': 0, 'map_entity_id': bf, 'map_entity_type': 1,
          'general_character_id': 0, 'is_deployed': True,
          # 官方士气模型：morale 是 morale_updated_at 时刻快照——置为当前
          # 真实时间，快照即现值（否则 created_at=NOW 过去时触发回复）
          'morale_updated_at': datetime.datetime.now(datetime.timezone.utc),
          'created_at': NOW, 'upgrade_level': 0}
    store.ats[at_id] = at
    return at


async def main():
    # ---- A. 拓扑 / 寻路 ----
    adj = war_campaign.supplyline_adjacency()
    check('邻接表 10-20-30 三城两向',
          sorted(n for n in adj) == [10, 20, 30]
          and {e[3] for e in adj[10]} == {20}
          and {e[3] for e in adj[20]} == {10, 30}
          and {e[3] for e in adj[30]} == {20},
          f'nodes={sorted(adj)}')
    check('邻接边携带补给线 id 与出兵点',
          {(e[0], e[1], e[2]) for e in adj[10]} == {(101, 1, 2)}
          and {(e[0], e[1], e[2]) for e in adj[20]} == {(101, 2, 1), (102, 2, 3)})
    check('寻路 同城返回 []',
          war_campaign.find_supplyline_path(10, 10) == [])
    check('寻路 不可达返回 None',
          war_campaign.find_supplyline_path(10, 40) is None
          and war_campaign.find_supplyline_path(10, 999) is None)
    path = war_campaign.find_supplyline_path(10, 30)
    check('寻路 10→30 两段',
          path is not None and len(path) == 2
          and path[0]['sl_id'] == 101 and path[1]['sl_id'] == 102)
    check('寻路 路径连续性',
          path[0]['from_bf'] == 10 and path[0]['to_bf'] == 20
          and path[1]['from_bf'] == 20 and path[1]['to_bf'] == 30
          and path[0]['to_ap'] == path[1]['from_ap'])
    rev = war_campaign.find_supplyline_path(30, 10)
    check('寻路 反向 30→10 sl 序反转',
          rev is not None and [s['sl_id'] for s in rev] == [102, 101])

    # ---- B. segment_seconds ----
    s = war_campaign.segment_seconds(10, 20, 1.0)
    check('segment_seconds 距离/速度公式',
          abs(s - SEG_SEC) < 1e-6, f'{s} vs {SEG_SEC}')
    check('segment_seconds 速度加倍时长减半',
          abs(war_campaign.segment_seconds(10, 20, 2.0) - SEG_SEC / 2) < 1e-6)
    check('segment_seconds 无坐标回退',
          war_campaign.segment_seconds(10, 999, 1.0)
          == war_campaign.SEGMENT_FALLBACK_SECONDS)
    check('segment_seconds 下限 1.0',
          war_campaign.segment_seconds(10, 20, 1e9) >= 1.0)

    # ---- B2. 地形减速（#2：中立区/海面大大降低行军速度）----
    SEA = float(mm_config.WAR_SEA_SPEED_FACTOR or 0.25)
    NEU = float(mm_config.WAR_NEUTRAL_SPEED_FACTOR or 0.5)
    # 10-20 距离 500 < 550 海阈值；10-30 距离 1000 ≥ 550（跨海）
    check('地形减速 中立段(两端均非己方) ×1/0.5',
          abs(war_campaign.segment_seconds(
                  10, 20, 1.0, 1, {10: 2, 20: 2}) - SEG_SEC / NEU) < 1e-6)
    check('地形减速 一端己方不减速',
          abs(war_campaign.segment_seconds(
                  10, 20, 1.0, 1, {10: 1, 20: 2}) - SEG_SEC) < 1e-6)
    check('地形减速 己方域不减速',
          abs(war_campaign.segment_seconds(
                  10, 20, 1.0, 1, {10: 1, 20: 1}) - SEG_SEC) < 1e-6)
    check('地形减速 缺归属(owner=0 中立)计入减速',
          abs(war_campaign.segment_seconds(
                  10, 20, 1.0, 1, {10: 2}) - SEG_SEC / NEU) < 1e-6)
    check('地形减速 owners 空(异常态)不减速',
          abs(war_campaign.segment_seconds(
                  10, 20, 1.0, 1, {}) - SEG_SEC) < 1e-6)
    check('地形减速 跨海段(距离≥550) ×1/0.25',
          abs(war_campaign.segment_seconds(10, 30, 1.0)
              - (1000.0 / SCALE) / SEA) < 1e-6)
    check('地形减速 海面+中立取更小系数',
          abs(war_campaign.segment_seconds(
                  10, 30, 1.0, 1, {10: 2, 30: 2})
              - (1000.0 / SCALE) / min(SEA, NEU)) < 1e-6)

    # ---- B3. 空中段不受地形减速（#air：飞机/伞兵半径直达 + 运输机返航）----
    # 10-30 直线 1000 ≥ 550 海阈值，air=True 必须免减速（否则空运慢 4 倍）
    check('air 段免海面减速（距离≥550 不×0.25）',
          abs(war_campaign.segment_seconds(10, 30, 1.0, air=True)
              - 1000.0 / SCALE) < 1e-6)
    check('air 段免中立减速（owners 全非己方不×0.5）',
          abs(war_campaign.segment_seconds(
                  10, 30, 1.0, 1, {10: 2, 30: 2}, air=True)
              - 1000.0 / SCALE) < 1e-6)

    # ---- C. start_movement ----
    store = FakeStore()
    war = await war_campaign.ensure_war(store)
    check('ensure_war 合成开战', war is not None and war['id'] == 1)
    a = _mk_at(store, war['id'], 11, 1, 10)
    mv, code = await war_campaign.start_movement(store, a, 30, now=NOW)
    check('start_movement 成功 RC_OK', code == war_campaign.RC_OK and mv)
    a11 = store.ats[11]
    check('AT 置在途 state=1 + 首段补给线',
          a11['state'] == 1 and a11['map_entity_id'] == 101
          and a11['map_entity_type'] == war_campaign.MAP_ENTITY_SUPPLYLINE)
    check('movement 段时长与到达时间',
          len(mv['path']) == 2
          and abs(mv['path'][0]['seconds'] - SEG_SEC) < 1e-6
          and abs((mv['arrive_at'] - NOW).total_seconds() - 2 * SEG_SEC) < 1e-6,
          f"arrive={mv['arrive_at']}")

    # 同城目标 → (None, RC_OK)
    b = _mk_at(store, war['id'], 12, 2, 20)
    mv3, code3 = await war_campaign.start_movement(store, b, 20, now=NOW)
    check('同地目标 (None, RC_OK)', mv3 is None
          and code3 == war_campaign.RC_OK)

    # 不可达 → 7（transport 族 0=ok，失败禁回 0）
    mv4, code4 = await war_campaign.start_movement(store, b, 40, now=NOW)
    check('不可达目标 7', mv4 is None
          and code4 == war_campaign.RC_CANNOT_FIND_COMMANDNODE)

    # override_move → 全 0 时长
    mv5, code5 = await war_campaign.start_movement(
        store, b, 30, override_move=True, now=NOW)
    check('overrideMove 段时长全 0',
          code5 == war_campaign.RC_OK and mv5
          and all(s['seconds'] == 0 for s in mv5['path'])
          and mv5['arrive_at'] == NOW)

    # 同城出兵点 → 立即落位
    c = _mk_at(store, war['id'], 13, 1, 10)
    mv6, code6 = await war_campaign.start_movement(
        store, c, 10, override_ap=1, now=NOW)
    check('同城出兵点立即落位 type=2',
          code6 == war_campaign.RC_OK and mv6
          and store.ats[13]['map_entity_id'] == 1
          and store.ats[13]['map_entity_type']
          == war_campaign.MAP_ENTITY_ACCESSPOINT
          and store.movements[mv6['id']]['state'] == 1)

    # dest_ap 持久化（跨城目标出兵点）
    mv7, code7 = await war_campaign.start_movement(
        store, c, 20, override_ap=2, now=NOW)
    check('dest_ap 持久化', code7 == war_campaign.RC_OK and mv7
          and mv7['dest_ap'] == 2
          and store.movements[mv7['id']]['dest_ap'] == 2)

    # 中立域集成：三城归属均非 fac1 → 段时长 ×1/0.5
    await store.set_city_owner(war['id'], 10, 2)
    await store.set_city_owner(war['id'], 20, 2)
    d = _mk_at(store, war['id'], 14, 1, 10)
    mv8, code8 = await war_campaign.start_movement(store, d, 30, now=NOW)
    check('start_movement 中立域段减速',
          code8 == war_campaign.RC_OK and mv8
          and all(abs(s['seconds'] - SEG_SEC / NEU) < 1e-6
                  for s in mv8['path']),
          f"secs={[round(s['seconds'], 1) for s in mv8['path']]}")

    # ---- C2. divert_movement（在途改道：走完当前段，下一节点转向）----
    store_c = FakeStore()
    war_c = await war_campaign.ensure_war(store_c)
    at_c = _mk_at(store_c, war_c['id'], 41, 1, 10)
    mv_c, code_c = await war_campaign.start_movement(
        store_c, at_c, 30, now=NOW)
    t_half1 = NOW + datetime.timedelta(seconds=SEG_SEC * 0.5)

    # 改道去当前段终点（20）→ 剩余行程=当前段，无新尾
    mv_d1, code_d1 = await war_campaign.divert_movement(
        store_c, at_c, mv_c, 20, now=t_half1)
    check('C2 改道到当前段终点：单段收尾',
          code_d1 == war_campaign.RC_OK and mv_d1
          and len(mv_d1['path']) == 1
          and mv_d1['path'][0]['to_bf'] == 20
          and abs((mv_d1['arrive_at'] - NOW).total_seconds() - SEG_SEC)
          < 1e-6)
    check('C2 改道 movement id 不变（同 id 覆盖客户端行）',
          mv_d1['id'] == mv_c['id']
          and store_c.movements[mv_c['id']]['path'][0]['to_bf'] == 20)

    # 改道折返回出发城市：pivot=20 起接反向段
    at_c1b = _mk_at(store_c, war_c['id'], 46, 1, 10)
    mv_c2, _ = await war_campaign.start_movement(
        store_c, at_c1b, 30, now=NOW)
    mv_d2, code_d2 = await war_campaign.divert_movement(
        store_c, at_c1b, mv_c2, 10, now=t_half1)
    check('C2 改道折返：当前段 + 反向段',
          code_d2 == war_campaign.RC_OK and mv_d2
          and [s['to_bf'] for s in mv_d2['path']] == [20, 10]
          and mv_d2['path'][1]['sl_id'] == 101
          and abs((mv_d2['arrive_at'] - NOW).total_seconds()
                  - 2 * SEG_SEC) < 1e-6)
    check('C2 改道保留当前段时间轴（started_at/首段不动）',
          mv_d2['started_at'] == NOW
          and abs(mv_d2['path'][0]['seconds'] - SEG_SEC) < 1e-6)

    # 改道延长行程：单段在途 → 拼接新尾段
    at_c2 = _mk_at(store_c, war_c['id'], 42, 1, 10)
    mv_c3, _ = await war_campaign.start_movement(
        store_c, at_c2, 20, now=NOW)
    mv_d3, code_d3 = await war_campaign.divert_movement(
        store_c, at_c2, mv_c3, 30, now=t_half1)
    check('C2 改道延长：首段 + 新尾段时长正确',
          code_d3 == war_campaign.RC_OK and mv_d3
          and [(s['from_bf'], s['to_bf']) for s in mv_d3['path']]
          == [(10, 20), (20, 30)]
          and abs(mv_d3['path'][1]['seconds'] - SEG_SEC) < 1e-6
          and abs((mv_d3['arrive_at'] - NOW).total_seconds()
                  - 2 * SEG_SEC) < 1e-6)

    # 末段封口（now 超过最后段 end）：pivot=原终点接新路径
    at_c3 = _mk_at(store_c, war_c['id'], 43, 1, 10)
    mv_c4, _ = await war_campaign.start_movement(
        store_c, at_c3, 20, now=NOW)
    mv_d4, code_d4 = await war_campaign.divert_movement(
        store_c, at_c3, mv_c4, 30,
        now=NOW + datetime.timedelta(seconds=SEG_SEC * 2))
    check('C2 末段封口改道：从原终点续接',
          code_d4 == war_campaign.RC_OK and mv_d4
          and [s['to_bf'] for s in mv_d4['path']] == [20, 30])

    # 不可达目标（40 孤岛）→ 7
    mv_d5, code_d5 = await war_campaign.divert_movement(
        store_c, at_c3, mv_d4, 40, now=t_half1)
    check('C2 改道不可达 7',
          mv_d5 is None
          and code_d5 == war_campaign.RC_CANNOT_FIND_COMMANDNODE)

    # 空运/半径单位单段 air 在途 → 33 不可改
    at_c4 = _mk_at(store_c, war_c['id'], 44, 1, 10)
    store_c.ats[44]['state'] = 1
    mv_air = await store_c.create_movement(
        44, war_c['id'],
        [{'from_bf': 10, 'to_bf': 30, 'sl_id': 0, 'from_ap': 0,
          'to_ap': 0, 'seconds': 600, 'air': True}],
        started_at=NOW, arrive_at=NOW + datetime.timedelta(seconds=600))
    mv_d6, code_d6 = await war_campaign.divert_movement(
        store_c, at_c4, mv_air, 20, now=t_half1)
    check('C2 air 在途不可改道 33',
          mv_d6 is None
          and code_d6 == war_campaign.RC_ALREADY_IN_TRANSPORT)

    # dest_ap：改道带出兵点 → 更新；不带 → 清除
    at_c5 = _mk_at(store_c, war_c['id'], 45, 1, 10)
    mv_c6, _ = await war_campaign.start_movement(
        store_c, at_c5, 30, override_ap=3, now=NOW)
    check('C2 前置：dest_ap 已存', mv_c6['dest_ap'] == 3)
    mv_d7, code_d7 = await war_campaign.divert_movement(
        store_c, at_c5, mv_c6, 20, now=t_half1)
    check('C2 改道不带出兵点 dest_ap 清除',
          code_d7 == war_campaign.RC_OK and mv_d7
          and mv_d7['dest_ap'] == 0,
          f"dest_ap={mv_d7 and mv_d7['dest_ap']}")
    mv_d8, code_d8 = await war_campaign.divert_movement(
        store_c, at_c5, mv_d7, 20, override_ap=2, now=t_half1)
    check('C2 改道指定出兵点 dest_ap=2',
          code_d8 == war_campaign.RC_OK and mv_d8
          and mv_d8['dest_ap'] == 2
          and len(mv_d8['path']) == 1)

    # ---- C3. 改道后 advance_movements 全程推进 ----
    store_d = FakeStore()
    war_d = await war_campaign.ensure_war(store_d)
    at_d = _mk_at(store_d, war_d['id'], 51, 1, 10)
    mv_e, code_e = await war_campaign.start_movement(
        store_d, at_d, 20, now=NOW)
    mvd_e, code_ed = await war_campaign.divert_movement(
        store_d, at_d, mv_e, 30, now=t_half1)
    check('C3 改道成功', code_ed == war_campaign.RC_OK and mvd_e
          and [s['to_bf'] for s in mvd_e['path']] == [20, 30])
    ev_h = await war_campaign.advance_movements(
        store_d, war_d, now=NOW + datetime.timedelta(seconds=SEG_SEC * 0.75))
    check('C3 改道后首段仍在途（无换段）',
          ev_h == [] and store_d.ats[51]['map_entity_id'] == 101)
    ev_m = await war_campaign.advance_movements(
        store_d, war_d, now=NOW + datetime.timedelta(seconds=SEG_SEC * 1.5))
    check('C3 改道后换段正常（sl102）',
          store_d.ats[51]['map_entity_id'] == 102
          and any(k == 'set' and tag == b'transport_commandnode_segment'
                  for k, tag, _ in ev_m))
    await war_campaign.advance_movements(
        store_d, war_d,
        now=NOW + datetime.timedelta(seconds=2 * SEG_SEC + 5))
    a51 = store_d.ats[51]
    check('C3 改道后到达新目的地 30',
          a51['state'] == 0
          and a51['map_entity_id'] == 30
          and a51['map_entity_type'] == war_campaign.MAP_ENTITY_BATTLEFIELD
          and store_d.movements[mvd_e['id']]['state'] == 1)


    # ---- D. advance_movements ----
    store2 = FakeStore()
    war2 = await war_campaign.ensure_war(store2)
    check('第二 store 独立开战', war2 is not None)
    a2 = _mk_at(store2, war2['id'], 21, 1, 10)
    _e2 = _mk_at(store2, war2['id'], 22, 2, 30)      # 敌对 AT 驻 30
    mv, code = await war_campaign.start_movement(store2, a2, 30, now=NOW)
    check('D 链 start RC_OK', code == war_campaign.RC_OK)

    t_half1 = NOW + datetime.timedelta(seconds=SEG_SEC / 2)
    ev1 = await war_campaign.advance_movements(store2, war2, now=t_half1)
    check('首段中段：无换段事件',
          ev1 == [] and store2.ats[21]['map_entity_id'] == 101)

    t_mid2 = NOW + datetime.timedelta(seconds=SEG_SEC * 1.5)
    ev2 = await war_campaign.advance_movements(store2, war2, now=t_mid2)
    tags2 = {(k, tag) for k, tag, _ in ev2}
    check('换段：AT 移到 sl102 + segment/instance 推送',
          store2.ats[21]['map_entity_id'] == 102
          and ('set', b'transport_commandnode_segment') in tags2
          and ('set', b'CommandNodeWarInstance') in tags2)
    seg_row2 = pb_parse(next(p for k, tag, p in ev2
                            if tag == b'transport_commandnode_segment'))
    check('换段推送的是新段 sl102',
          int(seg_row2[3]) == 102 and int(seg_row2[2]) == 21)

    t_arr = NOW + datetime.timedelta(seconds=2 * SEG_SEC + 5)
    ev3 = await war_campaign.advance_movements(store2, war2, now=t_arr)
    tags3 = {(k, tag) for k, tag, _ in ev3}
    check('到达：del segment/destination + set instance + set battle',
          ('del', b'transport_commandnode_segment') in tags3
          and ('del', b'transport_commandnode_destination') in tags3
          and ('set', b'CommandNodeWarInstance') in tags3
          and ('set', b'battle') in tags3,
          f'{sorted(tags3)}')
    check('到达落位城市 30 type=1（官服：图标叠城，方向差异在详情兵线）',
          store2.ats[21]['map_entity_id'] == 30
          and store2.ats[21]['map_entity_type']
          == war_campaign.MAP_ENTITY_BATTLEFIELD)
    check('移动行 closed（state=1）',
          store2.movements[mv['id']]['state'] == 1
          and await store2.list_active_movements(war2['id']) == [])
    check('会战成局：双方 state=2',
          store2.ats[21]['state'] == 2 and store2.ats[22]['state'] == 2)
    battles = list(store2.battles.values())
    check('会战登记：1 场、城市 30、两阵营',
          len(battles) == 1 and battles[0]['battlefield_id'] == 30
          and {f['faction'] for f in battles[0]['factions']} == {1, 2})
    battle_row = pb_parse(next(p for k, tag, p in ev3 if tag == b'battle'))
    check('battle 行 f2 warid / f3 mapEntityId / f9 factioncount',
          int(battle_row[2]) == war2['id'] and int(battle_row[3]) == 30
          and int(battle_row[9]) == 2)
    # missionresource 行（isInBattle 唯一数据源）：双方各一行 ADD，
    # 行 id=f1=AT id，f5 commandNodeWarInstanceId=AT id，
    # f2 missionid=成局 record id
    mr_rows3 = [pb_parse(p) for k, tag, p in ev3
                if tag == b'missionresource']
    mr_mid3 = next(m['id'] for m in store2.mission_records.values()
                   if int(m.get('battle_id') or 0) == battles[0]['id'])
    check('成局推 missionresource：双方各一行（id=AT id，f5=AT id，'
          'f2=missionid）',
          len(mr_rows3) == 2
          and {_mr_id(r) for r in mr_rows3} == {21, 22}
          and {int(r[5]) for r in mr_rows3} == {21, 22}
          and {int(r[2]) for r in mr_rows3} == {mr_mid3},
          f'{[(_mr_id(r), int(r[5]), int(r[2])) for r in mr_rows3]}')

    # 加入既有会战：不重复建战
    f3at = _mk_at(store2, war2['id'], 23, 1, 20)
    _mvj, codej = await war_campaign.start_movement(store2, f3at, 30, now=NOW)
    evj = await war_campaign.advance_movements(
        store2, war2, now=NOW + datetime.timedelta(seconds=2 * SEG_SEC + 10))
    check('后到 AT 加入既有会战不新建',
          codej == war_campaign.RC_OK
          and len(store2.battles) == 1
          and store2.ats[23]['state'] == 2
          and any(tag == b'battle' for _, tag, _ in evj))
    mr_rowsj = [pb_parse(p) for k, tag, p in evj
                if tag == b'missionresource']
    check('增援推 missionresource：新行 ADD（AT 23，同战同 missionid）',
          len(mr_rowsj) == 1
          and _mr_id(mr_rowsj[0]) == 23
          and int(mr_rowsj[0][2]) == mr_mid3,
          f'{[(_mr_id(r), int(r[2])) for r in mr_rowsj]}')

    # D-snap 快照 missionresource 行：进行中会战参战 AT 各一行（重登录
    #      后 isInBattle/撤退入口数据源；2026-09-17 补）；中途撤退 AT
    #      （state!=2，factions 残留）不发
    snap_mr = {_mr_id(pb_parse(p)): pb_parse(p)
               for tag, p in await war_campaign.build_warmap_snapshot(
                   store2, war2)
               if tag == b'missionresource'}
    check('快照 进行中会战参战 AT missionresource 行（f5=AT id、'
          'f2=missionid）',
          set(snap_mr) == {21, 22, 23}
          and all(int(r[5]) == aid and int(r[2]) == mr_mid3
                  for aid, r in snap_mr.items()),
          f'{sorted(snap_mr)}')
    store2.ats[23]['state'] = 0          # 模拟中途撤退（factions 残留）
    snap_mr2 = {_mr_id(pb_parse(p))
                for tag, p in await war_campaign.build_warmap_snapshot(
                    store2, war2)
                if tag == b'missionresource'}
    check('快照 离战 AT（state!=2）不发 missionresource 行',
          snap_mr2 == {21, 22}, f'{sorted(snap_mr2)}')
    store2.ats[23]['state'] = 2          # 还原（后续段只读 store2）

    # 出兵点到达落位 type=2
    store3 = FakeStore()
    war3 = await war_campaign.ensure_war(store3)
    g = _mk_at(store3, war3['id'], 31, 1, 10)
    mvg, codeg = await war_campaign.start_movement(
        store3, g, 20, override_ap=2, now=NOW)
    await war_campaign.advance_movements(
        store3, war3, now=NOW + datetime.timedelta(seconds=SEG_SEC + 5))
    check('dest_ap 到达落位出兵点 type=2',
          codeg == war_campaign.RC_OK
          and store3.ats[31]['map_entity_id'] == 2
          and store3.ats[31]['map_entity_type']
          == war_campaign.MAP_ENTITY_ACCESSPOINT)

    # AT 解散（state>=3）→ 移动取消
    store4 = FakeStore()
    war4 = await war_campaign.ensure_war(store4)
    h = _mk_at(store4, war4['id'], 41, 1, 10)
    mvh, _ = await war_campaign.start_movement(store4, h, 30, now=NOW)
    store4.ats[41]['state'] = 3
    await war_campaign.advance_movements(
        store4, war4, now=NOW + datetime.timedelta(seconds=1))
    check('AT 解散移动取消（state=2）',
          store4.movements[mvh['id']]['state'] == 2)

    # ---- E. transport 行字段 ----
    segs = war_campaign.movement_segments(mv)
    seg = segs[0]
    seg_row = war_campaign.msg_transport_segment_row(
        store2.ats[21], war2['id'], mv, seg)
    r = pb_parse(seg_row)
    check('segment 行 f1-f8',
          int(r[1]) == mv['id'] and int(r[2]) == 21 and int(r[3]) == 101
          and int(r[4]) == 1 and int(r[5]) == 2
          and r[6].decode().isdigit() and r[7].decode().isdigit()
          and int(r[8]) == war2['id'])
    dest_row = war_campaign.msg_transport_destination_row(
        store2.ats[21], war2['id'], mv, segs)
    d = pb_parse(dest_row)
    check('destination 行 f1-f6',
          int(d[1]) == mv['id'] and int(d[2]) == 21 and int(d[3]) == 30
          and int(d[4]) == 0 and d[5].decode() == '20,30'
          and int(d[6]) == war2['id'])
    check('movement_segments 毫秒时间轴连续',
          segs[0]['end_ms'] == segs[1]['begin_ms']
          and segs[0]['begin_ms'] == int(NOW.timestamp() * 1000))
    check('current_segment_index 定位',
          war_campaign.current_segment_index(segs, segs[0]['begin_ms']) == 0
          and war_campaign.current_segment_index(
              segs, segs[1]['begin_ms'] + 1) == 1
          and war_campaign.current_segment_index(
              segs, segs[-1]['end_ms'] + 999) == 1)

    # ---- F. war_tick 冒烟 ----
    broadcast_calls = []

    async def _fake_broadcast(events, store=None):
        broadcast_calls.append(events)

    # 拆包后 monkeypatch 须打定义子模块 tick（__init__ re-export 不回流）
    _orig_bcast = war_campaign.tick.broadcast_war_events
    war_campaign.tick.broadcast_war_events = _fake_broadcast
    try:
        store5 = FakeStore()
        await war_campaign.ensure_war(store5)
        k = _mk_at(store5, 1, 51, 1, 10)
        await war_campaign.start_movement(store5, k, 20, now=NOW)
        await war_campaign.war_tick(store5)
        # war_tick 用真实 now（远晚于 NOW）→ 立即到达 + 广播
        check('war_tick 推进并广播事件',
              broadcast_calls and any(
                  k2 == 'set' and tag == b'CommandNodeWarInstance'
                  for k2, tag, _ in broadcast_calls[0])
              and store5.ats[51]['map_entity_id'] == 20,
              f'{len(broadcast_calls)} calls')
    finally:
        war_campaign.tick.broadcast_war_events = _orig_bcast

    # ---- G. M3-3：fow 参数 + 空运 ----
    # G1 fow 网格参数：war f19-21 ← mm_config（非 0 激活客户端 FogOfWarLayer）
    _fow_bak = (mm_config.WAR_FOW_GRID_SIZE, mm_config.WAR_FOW_GRID_ORIGIN_X,
                mm_config.WAR_FOW_GRID_ORIGIN_Y)
    try:
        mm_config.WAR_FOW_GRID_SIZE = 512
        mm_config.WAR_FOW_GRID_ORIGIN_X = 2048
        mm_config.WAR_FOW_GRID_ORIGIN_Y = 1024
        war_row = pb_parse(war_campaign.build_war_proto(war))
        check('war f19-21 ← mm_config fow 参数',
              int(war_row[19]) == 512 and int(war_row[20]) == 2048
              and int(war_row[21]) == 1024,
              f'{int(war_row[19])},{int(war_row[20])},{int(war_row[21])}')
    finally:
        (mm_config.WAR_FOW_GRID_SIZE, mm_config.WAR_FOW_GRID_ORIGIN_X,
         mm_config.WAR_FOW_GRID_ORIGIN_Y) = _fow_bak
    war_row0 = pb_parse(war_campaign.build_war_proto(war))
    check('war f19-21 还原后回默认',
          int(war_row0[19]) == _fow_bak[0] and int(war_row0[20]) == _fow_bak[1]
          and int(war_row0[21]) == _fow_bak[2])

    store6 = FakeStore()
    war6 = await war_campaign.ensure_war(store6)
    plane = _mk_at(store6, war6['id'], 61, 1, 10)
    plane['resources'] = {'10': 2}                 # 2 架运输机
    enemy = _mk_at(store6, war6['id'], 62, 2, 40)  # 敌 AT 驻孤城 40（空运目标）

    # G2 无运输机 → 拒绝（7）
    noplane = _mk_at(store6, war6['id'], 63, 1, 20)
    amv, acode = await war_campaign.start_air_transport(store6, noplane, 40,
                                                        now=NOW)
    check('空运 无运输机 → 7', amv is None
          and acode == war_campaign.RC_CANNOT_FIND_COMMANDNODE)

    # G4 同城/无效目标 → 7
    amv4, acode4 = await war_campaign.start_air_transport(store6, plane, 10,
                                                          now=NOW)
    amv0, acode0 = await war_campaign.start_air_transport(store6, plane, 0,
                                                          now=NOW)
    check('空运 同城/无效目标 → 7', amv4 is None and acode4 == 7
          and amv0 is None and acode0 == 7)

    # G3 携运输机 → 单段 air 移动（消耗 1 架 + state=1 + map_entity 不变）
    amv3, acode3 = await war_campaign.start_air_transport(store6, plane, 40,
                                                          now=NOW)
    seg0 = amv3['path'][0] if amv3 else {}
    air_sec = max(1, int(mm_config.WAR_AIR_TRANSIT_SECONDS or 600))
    check('空运 成功 RC_OK + 单段 air',
          acode3 == war_campaign.RC_OK and amv3
          and len(amv3['path']) == 1 and seg0.get('air') is True
          and seg0['from_bf'] == 10 and seg0['to_bf'] == 40
          and seg0['sl_id'] == 0,
          f"path={amv3['path'] if amv3 else None}")
    check('空运 段时长 = WAR_AIR_TRANSIT_SECONDS',
          abs(float(seg0.get('seconds') or 0) - air_sec) < 1e-6
          and abs((amv3['arrive_at'] - NOW).total_seconds() - air_sec) < 1e-6)
    p61 = store6.ats[61]
    check('空运 消耗 1 架运输机 + state=1 + 出发城市不变',
          p61['resources'] == {'10': 1} and p61['state'] == 1
          and p61['map_entity_id'] == 10
          and p61['map_entity_type']
          == war_campaign.MAP_ENTITY_BATTLEFIELD)

    # G5 air_transport 行字段（§3.14）
    aseg = war_campaign.movement_segments(amv3)[0]
    arow = pb_parse(war_campaign.msg_air_transport_row(
        p61, war6['id'], amv3, aseg))
    check('air_transport 行 f1-f9',
          int(arow[1]) == amv3['id'] and int(arow[2]) == 61
          and int(arow[3]) == 10 and int(arow[4]) == 1
          and int(arow[5]) == 40 and int(arow[6]) == 1
          and int(arow[9]) == war6['id'],
          f'{dict(sorted(arow.items()))}')

    # G7 在途未到 → 无事件（不换段）
    ev_air_mid = await war_campaign.advance_movements(
        store6, war6, now=NOW + datetime.timedelta(seconds=air_sec / 2))
    check('空运在途 无换段事件', ev_air_mid == [])

    # G6 到达 → 落位孤城 40 + del air_transport + 会战成局（敌 AT 驻 40）
    ev_air = await war_campaign.advance_movements(
        store6, war6,
        now=NOW + datetime.timedelta(seconds=air_sec + 5))
    tags_air = {(k, tag) for k, tag, _ in ev_air}
    check('空运到达 del segment/air_transport + instance + battle',
          ('del', b'transport_commandnode_segment') in tags_air
          and ('del', b'air_transport') in tags_air
          and ('set', b'CommandNodeWarInstance') in tags_air
          and ('set', b'battle') in tags_air,
          f'{sorted(tags_air)}')
    check('空运落位城市 40 type=1 + 会战双方 state=2',
          p61['map_entity_id'] == 40 and p61['state'] == 2
          and store6.ats[62]['state'] == 2
          and store6.movements[amv3['id']]['state'] == 1)
    # G6b 空中到达推序：del air_transport 必须在 set instance 之后
    #     派发——onAirDestinationUpdates→updateAssaultTeamStatus 重算
    #     status=location，反序则按旧城市名冻结到重登（2026-09-16
    #     伞兵城市名卡上一城修复）
    i_segA = next(i for i, (k, t, _) in enumerate(ev_air)
                  if (k, t) == ('del', b'transport_commandnode_segment'))
    i_insA = next(i for i, (k, t, _) in enumerate(ev_air)
                  if i > i_segA
                  and (k, t) == ('set', b'CommandNodeWarInstance'))
    i_airA = next(i for i, (k, t, _) in enumerate(ev_air)
                  if i > i_insA and (k, t) == ('del', b'air_transport'))
    check('G6b 空运到达推序 del segment < set instance < del air_transport',
          i_segA < i_insA < i_airA,
          f'seg={i_segA} ins={i_insA} air={i_airA}')

    # G8 快照：无在途空运时无 air_transport 行
    store7 = FakeStore()
    war7 = await war_campaign.ensure_war(store7)
    snap = await war_campaign.build_warmap_snapshot(store7, war7)
    check('快照 无在途空运 → 无 air_transport 行',
          all(tag != b'air_transport' for tag, _ in snap))
    flier = _mk_at(store7, war7['id'], 71, 1, 20)
    flier['resources'] = {'10': 1}
    _famv, facode = await war_campaign.start_air_transport(store7, flier, 10,
                                                           now=NOW)
    snap2 = await war_campaign.build_warmap_snapshot(store7, war7)
    frow = pb_parse(next(p for tag, p in snap2 if tag == b'air_transport'))
    check('快照 在途空运 → air_transport 行（f2 instance/f5 to）',
          facode == war_campaign.RC_OK
          and int(frow[2]) == 71 and int(frow[5]) == 10)

    # ---- H. 占城（途经 + 到达 + 敌驻军拦截 + force）----
    store8 = FakeStore()
    war8 = await war_campaign.ensure_war(store8)
    mover = _mk_at(store8, war8['id'], 81, 1, 10)      # 无敌对 AT：10→30
    mvh2, codeh2 = await war_campaign.start_movement(
        store8, mover, 30, now=NOW)
    check('H0 占城链 start RC_OK', codeh2 == war_campaign.RC_OK)

    # H1 换段占城：途经城市 20 → faction 1 + 三类状态行
    evh1 = await war_campaign.advance_movements(
        store8, war8, now=NOW + datetime.timedelta(seconds=SEG_SEC * 1.5))
    tagsh1 = {(k, tag) for k, tag, _ in evh1}
    bf_rows1 = [pb_parse(p) for k, tag, p in evh1
                if tag == b'battlefieldstatus']
    ap_rows1 = [pb_parse(p) for k, tag, p in evh1
                if tag == b'accesspointstatus']
    check('H1 换段占城 途经城市 20 → faction 1',
          store8.city_owners[war8['id']].get(20) == 1
          and ('set', b'battlefieldstatus') in tagsh1
          and any(int(r[3]) == 20 and int(r[4]) == 1 for r in bf_rows1),
          f"owners={store8.city_owners[war8['id']]}")
    check('H1 出兵点状态行随城易主',
          any(int(r[3]) == 2 and int(r[4]) == 1 for r in ap_rows1))

    # H2 到达占城：终点 30 → faction 1 + 补给线 20-30 同阵营成行
    evh2 = await war_campaign.advance_movements(
        store8, war8, now=NOW + datetime.timedelta(seconds=2 * SEG_SEC + 5))
    bf_rows2 = [pb_parse(p) for k, tag, p in evh2
                if tag == b'battlefieldstatus']
    sl_rows2 = [pb_parse(p) for k, tag, p in evh2
                if tag == b'supplylinestatus']
    check('H2 到达占城 终点 30 → faction 1',
          store8.city_owners[war8['id']].get(30) == 1
          and any(int(r[3]) == 30 and int(r[4]) == 1 for r in bf_rows2))
    check('H2 补给线 20-30 同阵营 → supplylinestatus set',
          any(int(r[4]) == 102 and int(r[3]) == 1 for r in sl_rows2))

    # H3 归属未变 → 无事件（幂等）
    evh3 = await war_campaign._capture_city_events(
        store8, war8['id'], 1, 30)
    check('H3 归属未变无事件', evh3 == [])

    # H4 敌驻军 → 不占领（交由会战）
    store9 = FakeStore()
    war9 = await war_campaign.ensure_war(store9)
    atk = _mk_at(store9, war9['id'], 91, 1, 10)
    _def = _mk_at(store9, war9['id'], 92, 2, 20)       # 敌 AT 驻 20
    await war_campaign.start_movement(store9, atk, 20, now=NOW)
    await war_campaign.advance_movements(
        store9, war9, now=NOW + datetime.timedelta(seconds=SEG_SEC + 5))
    check('H4 敌驻军不占城（会战成局归属待结算）',
          store9.city_owners[war9['id']].get(20) != 1
          and store9.ats[91]['state'] == 2
          and len(store9.battles) == 1)

    # H5 force 占城（settle 复用）：跳过敌驻军检查
    evh5 = await war_campaign._capture_city_events(
        store9, war9['id'], 1, 20, force=True)
    check('H5 force 占城跳过敌驻军检查',
          store9.city_owners[war9['id']].get(20) == 1
          and any(tag == b'battlefieldstatus'
                  for _, tag, _ in evh5))

    # H6 敌方补给队驻城不算守军（2026-09-08 修复）：补给队（is_reserve，
    # template_id%100==4）非战斗部队 → 不挡占领、不拉进会战（与
    # _check_field_encounters 野外口径一致）。此前 bug：敌补给队驻城
    # 挡占领还成战 → 玩家报"敌城无守军却无法直接占领"
    storeR = FakeStore()
    warR = await war_campaign.ensure_war(storeR)
    atkR = _mk_at(storeR, warR['id'], 96, 1, 10)
    resv = _mk_at(storeR, warR['id'], 95, 2, 20)      # GE 补给队驻 20
    resv['template_id'] = 204                          # reserve 模板
    await war_campaign.start_movement(storeR, atkR, 20, now=NOW)
    await war_campaign.advance_movements(
        storeR, warR, now=NOW + datetime.timedelta(seconds=SEG_SEC + 5))
    check('H6 敌方补给队驻城不挡直接占领（不成战）',
          storeR.city_owners[warR['id']].get(20) == 1
          and storeR.ats[96]['state'] == 0
          and len(storeR.battles) == 0,
          f"owners={storeR.city_owners[warR['id']]} "
          f"battles={len(storeR.battles)}")

    # H7 途经敌驻军城市（无会战）→ 中止行军落位途经城 + 成局拦截
    #    （2026-09-15 穿城 bug 修复：目的地在更远处、途经敌情城市此前
    #    换段分支无 _check_battle_formation 直接穿城而过）
    storeP = FakeStore()
    warP = await war_campaign.ensure_war(storeP)
    atkP = _mk_at(storeP, warP['id'], 301, 1, 10)
    _defP = _mk_at(storeP, warP['id'], 302, 2, 20)      # 敌 AT 驻途经城 20
    mvP, codeP = await war_campaign.start_movement(
        storeP, atkP, 30, now=NOW)                      # 目的地 30，途经 20
    evP = await war_campaign.advance_movements(
        storeP, warP, now=NOW + datetime.timedelta(seconds=SEG_SEC + 5))
    tagsP = {(k, tag) for k, tag, _ in evP}
    check('H7 途经敌驻军城市 中止行军 + 成局（不穿城）',
          codeP == war_campaign.RC_OK
          and storeP.movements[mvP['id']]['state'] == 1
          and storeP.ats[301]['map_entity_id'] == 20
          and storeP.ats[301]['map_entity_type'] == 1
          and storeP.ats[301]['state'] == 2
          and storeP.ats[302]['state'] == 2
          and len(storeP.battles) == 1
          and ('del', b'transport_commandnode_segment') in tagsP
          and ('del', b'transport_commandnode_destination') in tagsP
          and ('set', b'CommandNodeWarInstance') in tagsP
          and ('set', b'battle') in tagsP,
          f'mv={storeP.movements.get(mvP["id"])} at={storeP.ats[301]}')
    evP2 = await war_campaign.advance_movements(
        storeP, warP, now=NOW + datetime.timedelta(seconds=2 * SEG_SEC + 10))
    check('H7b 中止后不再推进（无 segment 事件、AT 钉在 20）',
          all(tag != b'transport_commandnode_segment'
              for _, tag, _ in evP2)
          and storeP.ats[301]['map_entity_id'] == 20)
    # 相对顺序断言（每 tick 还会推在途 AT 的 instance 行，须按序找）
    i_seg = next(i for i, (k, t, _) in enumerate(evP)
                 if (k, t) == ('del', b'transport_commandnode_segment'))
    i_ins = next(i for i, (k, t, _) in enumerate(evP)
                 if i > i_seg
                 and (k, t) == ('set', b'CommandNodeWarInstance'))
    i_dst = next(i for i, (k, t, _) in enumerate(evP)
                 if i > i_ins
                 and (k, t) == ('del', b'transport_commandnode_destination'))
    check('H7c 中止推序 del segment < set instance < del destination'
          '（客户端 status 重算时机约束，2026-09-15 城市名修复）',
          i_seg < i_ins < i_dst,
          f'seg={i_seg} ins={i_ins} dst={i_dst}')

    # H10 普通到达（无交战）推序同上：del destination 必须在 set
    #     instance 之后派发，否则列表 status 按在途旧 location 冻结
    storeU = FakeStore()
    warU = await war_campaign.ensure_war(storeU)
    atkU = _mk_at(storeU, warU['id'], 601, 1, 10)
    await war_campaign.start_movement(storeU, atkU, 20, now=NOW)
    evU = await war_campaign.advance_movements(
        storeU, warU, now=NOW + datetime.timedelta(seconds=SEG_SEC + 5))
    i_segU = next(i for i, (k, t, _) in enumerate(evU)
                  if (k, t) == ('del', b'transport_commandnode_segment'))
    i_insU = next(i for i, (k, t, _) in enumerate(evU)
                  if i > i_segU
                  and (k, t) == ('set', b'CommandNodeWarInstance'))
    i_dstU = next(i for i, (k, t, _) in enumerate(evU)
                  if i > i_insU
                  and (k, t) == ('del', b'transport_commandnode_destination'))
    check('H10 普通到达推序 del segment < set instance < del destination',
          storeU.ats[601]['map_entity_id'] == 20
          and i_segU < i_insU < i_dstU,
          f'seg={i_segU} ins={i_insU} dst={i_dstU}')

    # H8 途经交战城市且我方阵营参战 → 中止行军 + 增援入战
    storeQ = FakeStore()
    warQ = await war_campaign.ensure_war(storeQ)
    _usQ = _mk_at(storeQ, warQ['id'], 401, 1, 20)       # US 驻 20
    geQ = _mk_at(storeQ, warQ['id'], 402, 2, 10)        # GE 攻 20
    await war_campaign.start_movement(storeQ, geQ, 20, now=NOW)
    await war_campaign.advance_movements(
        storeQ, warQ, now=NOW + datetime.timedelta(seconds=SEG_SEC + 5))
    check('H8 前置：20 城会战成局', len(storeQ.battles) == 1
          and storeQ.ats[401]['state'] == 2
          and storeQ.ats[402]['state'] == 2)
    us2Q = _mk_at(storeQ, warQ['id'], 403, 1, 10)       # US 第二队 10→30
    t8 = NOW + datetime.timedelta(seconds=SEG_SEC + 10)
    mvQ, _ = await war_campaign.start_movement(storeQ, us2Q, 30, now=t8)
    evQ = await war_campaign.advance_movements(
        storeQ, warQ,
        now=t8 + datetime.timedelta(seconds=SEG_SEC + 5))   # 换段途经 20
    tagsQ = {(k, tag) for k, tag, _ in evQ}
    wbQ = list(storeQ.battles.values())[0]
    check('H8 途经我方参战会战城 中止行军 + 增援入战',
          storeQ.movements[mvQ['id']]['state'] == 1
          and storeQ.ats[403]['map_entity_id'] == 20
          and storeQ.ats[403]['state'] == 2
          and any(f.get('at_id') == 403 for f in wbQ['factions'])
          and len(storeQ.battles) == 1
          and ('set', b'battle') in tagsQ,
          f"factions={[f.get('at_id') for f in wbQ['factions']]} "
          f"at={storeQ.ats[403]}")

    # H9 途经第三方会战城市（我方不参战）→ 正常通过（两方制契约）
    storeT = FakeStore()
    warT = await war_campaign.ensure_war(storeT)
    _geT = _mk_at(storeT, warT['id'], 501, 2, 20)       # GE 驻 20
    suT = _mk_at(storeT, warT['id'], 502, 3, 10)        # SU 攻 20
    await war_campaign.start_movement(storeT, suT, 20, now=NOW)
    await war_campaign.advance_movements(
        storeT, warT, now=NOW + datetime.timedelta(seconds=SEG_SEC + 5))
    check('H9 前置：20 城德苏会战成局', len(storeT.battles) == 1)
    usT = _mk_at(storeT, warT['id'], 503, 1, 10)        # US 10→30 途经
    t9 = NOW + datetime.timedelta(seconds=SEG_SEC + 10)
    mvT, _ = await war_campaign.start_movement(storeT, usT, 30, now=t9)
    await war_campaign.advance_movements(
        storeT, warT,
        now=t9 + datetime.timedelta(seconds=SEG_SEC + 5))   # 换段途经 20
    wbT = list(storeT.battles.values())[0]
    check('H9 第三方会战城正常通过（不停、不入 factions）',
          storeT.movements[mvT['id']]['state'] == 0
          and storeT.ats[503]['state'] == 1
          and storeT.ats[503]['map_entity_id'] == 102
          and {f['faction'] for f in wbT['factions']} == {2, 3},
          f"at={storeT.ats[503]} "
          f"factions={[f.get('at_id') for f in wbT['factions']]}")

    # ---- I. 会战生命周期（2026-09-08 审计 #1-#4：A 第三阵营不入战 /
    #      B 结算释放幸存 AT / C factioncount+excludedFactionId / D 再成局）----
    # I1 第三阵营 AT 到达交战城：不入战正常驻扎（两方制契约，防结算卡死）
    storeA = FakeStore()
    warA = await war_campaign.ensure_war(storeA)
    us = _mk_at(storeA, warA['id'], 201, 1, 20)          # US 驻 20
    ge = _mk_at(storeA, warA['id'], 202, 2, 10)          # GE 从 10 → 20
    await war_campaign.start_movement(storeA, ge, 20, now=NOW)
    await war_campaign.advance_movements(
        storeA, warA, now=NOW + datetime.timedelta(seconds=SEG_SEC + 5))
    check('I1 前置：美 vs 德 成局双方 state=2',
          storeA.ats[201]['state'] == 2 and storeA.ats[202]['state'] == 2
          and len(storeA.battles) == 1)
    su = _mk_at(storeA, warA['id'], 203, 3, 10)          # SU 第三方到场
    await war_campaign.start_movement(storeA, su, 20, now=NOW)
    await war_campaign.advance_movements(
        storeA, warA, now=NOW + datetime.timedelta(seconds=2 * SEG_SEC + 20))
    wbA = list(storeA.battles.values())[0]
    check('I1 第三阵营 AT 不入战（state=0 驻扎 + 不进 factions）',
          storeA.ats[203]['state'] == 0
          and {f['faction'] for f in wbA['factions']} == {1, 2}
          and len(storeA.battles) == 1,
          f'state={storeA.ats[203]["state"]}')

    # I1b 同阵营增援照旧入战
    us2 = _mk_at(storeA, warA['id'], 204, 1, 10)
    await war_campaign.start_movement(storeA, us2, 20, now=NOW)
    await war_campaign.advance_movements(
        storeA, warA, now=NOW + datetime.timedelta(seconds=3 * SEG_SEC + 25))
    check('I1b 参战阵营增援入战 state=2',
          storeA.ats[204]['state'] == 2 and len(storeA.battles) == 1)
    check('I1b2 增援 AT 追加进 factions 名单（结算可释放）',
          any(f.get('at_id') == 204 for f in wbA['factions']),
          f"factions={[f.get('at_id') for f in wbA['factions']]}")

    # I2 结算释放幸存 AT（B 修复）：败方编制扣空歼灭 → 胜方 state=2→0
    #    驻留原地、无再成局（城里无敌对 AT，D 不触发）——隔离验证 B；
    #    （storeA 的 GE 未歼灭 + SU 驻留，D 再成局在 I3 验证）
    storeB = FakeStore()
    warB = await war_campaign.ensure_war(storeB)
    b_us = _mk_at(storeB, warB['id'], 221, 1, 20)        # US 驻 20
    b_ge = _mk_at(storeB, warB['id'], 222, 2, 10)        # GE 空*40 编制
    await war_campaign.start_movement(storeB, b_ge, 20, now=NOW)
    await war_campaign.advance_movements(
        storeB, warB, now=NOW + datetime.timedelta(seconds=SEG_SEC + 5))
    wbB = list(storeB.battles.values())[0]
    storeB.ats[221]['resources'] = {1: 100}
    evB = await war_campaign.settle_war_battle(storeB, 0, wbB['id'], 1)
    check('I2 结算释放幸存 AT（胜方 state=0 驻留原地，败方歼灭）',
          storeB.ats[221]['state'] == 0
          and storeB.ats[221]['is_deployed'] is True
          and storeB.ats[221]['map_entity_id'] == 20
          and storeB.ats[222]['state'] == 3
          and len(storeB.battles) == 1,
          f'us={storeB.ats[221]["state"]} ge={storeB.ats[222]["state"]}')
    check('I2b 会战行终态 + del battle 事件',
          storeB.battles[wbB['id']]['state'] == 1
          and ('del', b'battle', wbB['id']) in evB)
    check('I2b2 结算删 missionresource：全体参战 AT 离战行删（胜/歼同删）',
          ('del', b'missionresource', 221) in evB
          and ('del', b'missionresource', 222) in evB)

    # I2c excludedFactionId：两方局（factions 1,2）→ f9=2 / f10=3
    rowB = war_campaign.msg_battle_row(wbB)
    check('I2c battle 行 f9 factioncount=2 / f10 excluded=3',
          int(pb_parse(rowB)[9]) == 2
          and int(pb_parse(rowB)[10]) == 3)

    # I3 再成局（连环三方局，D 修复）：storeA 结算（GE 未歼灭 + SU 第三方
    #     驻留 20）→ 胜方 US 释放后与城中全部敌对 AT（GE+SU）立即开新战，
    #     factions={1,2,3} 三方如实登记（factioncount=3 / excluded=0），
    #     无僵持；GE 若未被释放（B 缺失）则 D 找不到胜方 state=0 AT，
    #     新战不成局——I3 同时覆盖 B+D
    for _aid in (201, 202, 204):
        storeA.ats[_aid]['resources'] = {1: 100}
    evA = await war_campaign.settle_war_battle(storeA, 0, wbA['id'], 1)
    new_battles = [b for b in storeA.battles.values()
                   if b['id'] != wbA['id']]
    check('I3 再成局：胜方释放后与城中敌对 AT 立即开新战（三方登记）',
          len(new_battles) == 1
          and new_battles[0]['battlefield_id'] == 20
          and {f['faction'] for f in new_battles[0]['factions']} == {1, 2, 3}
          and storeA.ats[201]['state'] == 2
          and storeA.ats[202]['state'] == 2
          and storeA.ats[203]['state'] == 2,
          f'battles={[(b["id"], [f["faction"] for f in b["factions"]]) for b in storeA.battles.values()]}')
    rowA3 = war_campaign.msg_battle_row(new_battles[0])
    check('I3b 三方局 battle 行 f9 factioncount=3 / f10 excluded=0',
          int(pb_parse(rowA3)[9]) == 3
          and int(pb_parse(rowA3)[10]) == 0)

    # I4 撤退释放（retreat state=0，handlers/war_at 审计 #3）：
    #    会战中 AT 手动撤退后不再被 RC_COMMANDNODE_IN_BATTLE 拦截
    #    （handlers 层完整回归见 test_war_at_supply R 系列；此处校验
    #    settle 侧独立路径——士气投降分支带 state=0 的既有行为不回归）
    #    （合成 capitals=[] → 投降分支无首都可回，临时 patch 一座）
    _orig_caps = wardb.capitals
    wardb.capitals = lambda: [
        {'battlefield_id': 10, 'default_faction': 1},
        {'battlefield_id': 40, 'default_faction': 2},
    ]
    storeC = FakeStore()
    warC = await war_campaign.ensure_war(storeC)
    c1 = _mk_at(storeC, warC['id'], 211, 1, 10)
    c2 = _mk_at(storeC, warC['id'], 212, 2, 20)
    await war_campaign.start_movement(storeC, c1, 20, now=NOW)
    await war_campaign.advance_movements(
        storeC, warC, now=NOW + datetime.timedelta(seconds=SEG_SEC + 5))
    wbC = list(storeC.battles.values())[0]
    # 士气 5（≤ surrender 25）→ 投降分支：state=0 + 回首都 + is_deployed=0
    # （士气快照模型：settle 走 current_morale 物化，快照须配新时间戳，
    #   否则按 created_at 回填 regen 直接回满，永远进不了投降分支）
    storeC.ats[211]['morale'] = 5
    storeC.ats[211]['morale_updated_at'] = datetime.datetime.now(
        datetime.timezone.utc)
    storeC.ats[211]['resources'] = {1: 100}
    storeC.ats[212]['resources'] = {1: 100}
    await war_campaign.settle_war_battle(storeC, 0, wbC['id'], 2)
    check('I4 投降分支回首都（state=0/is_deployed=0）不回归',
          storeC.ats[211]['state'] == 0
          and storeC.ats[211]['is_deployed'] is False
          and storeC.ats[211]['map_entity_id'] == 10,
          f'state={storeC.ats[211]["state"]} '
          f'deployed={storeC.ats[211]["is_deployed"]}')
    wardb.capitals = _orig_caps

    # I5 败方撤退（③b 进攻路线优先）：攻方战败 → 撤回进攻路线上
    #    最近的仍己方据点（路径尾段逐段回退）；路线城市已丢 → 继续
    #    沿路线回退；全丢 → 回退最近友方城（BFS 兜底）
    _orig_caps_i5 = wardb.capitals
    wardb.capitals = lambda: [
        {'battlefield_id': 10, 'default_faction': 1},
        {'battlefield_id': 20, 'default_faction': 1},
        {'battlefield_id': 30, 'default_faction': 2},
    ]

    async def _i5_run():
        sti = FakeStore()
        wari = await war_campaign.ensure_war(sti)
        _mk_at(sti, wari['id'], 231, 1, 10)
        _mk_at(sti, wari['id'], 232, 2, 30)
        at_i5 = sti.ats[231]
        await war_campaign.start_movement(sti, at_i5, 30, now=NOW)
        await war_campaign.advance_movements(
            sti, wari,
            now=NOW + datetime.timedelta(seconds=2 * SEG_SEC + 5))
        wb_i5 = list(sti.battles.values())[0]
        sti.ats[231]['resources'] = {1: 100}
        sti.ats[232]['resources'] = {1: 100}
        return sti, wari, wb_i5

    sti, wari, wb_i5 = await _i5_run()
    await war_campaign.settle_war_battle(sti, 0, wb_i5['id'], 2)
    mv231 = next((m for m in sti.movements.values()
                  if m['commandnode_id'] == 231 and m['state'] == 0), None)
    check('I5a 败方撤退 → 向进攻路线上最近据点 20 撤退行军（非出发城）',
          mv231 is not None
          and mv231['path'][-1]['to_bf'] == 20
          and sti.ats[231]['state'] == 1
          and sti.ats[231]['is_deployed'] is True,
          f'{mv231 and mv231["path"][-1]["to_bf"]}/{sti.ats[231]["state"]}')
    for _seg in mv231['path']:
        _seg['seconds'] = 0
    await war_campaign.advance_movements(sti, wari)
    check('I5a2 撤退行军到达 → 据点 20 落位（state=0 保持部署）',
          sti.ats[231]['map_entity_id'] == 20
          and sti.ats[231]['state'] == 0
          and sti.ats[231]['is_deployed'] is True,
          f'{sti.ats[231]["map_entity_id"]}/{sti.ats[231]["state"]}')

    sti, wari, wb_i5 = await _i5_run()
    sti.city_owners[wari['id']][20] = 2      # 路线上最近据点 20 已被敌占
    await war_campaign.settle_war_battle(sti, 0, wb_i5['id'], 2)
    mv231 = next((m for m in sti.movements.values()
                  if m['commandnode_id'] == 231 and m['state'] == 0), None)
    check('I5b 最近据点已丢 → 沿进攻路线继续回退到出发城 10',
          mv231 is not None
          and mv231['path'][-1]['to_bf'] == 10,
          f'{mv231 and mv231["path"][-1]["to_bf"]}')
    for _seg in mv231['path']:
        _seg['seconds'] = 0
    await war_campaign.advance_movements(sti, wari)
    check('I5b2 撤退行军到达 → 出发城 10 落位',
          sti.ats[231]['map_entity_id'] == 10,
          f'{sti.ats[231]["map_entity_id"]}')

    sti, wari, wb_i5 = await _i5_run()
    sti.city_owners[wari['id']][10] = 2      # 进攻路线 10/20 全部失守
    sti.city_owners[wari['id']][20] = 2
    await war_campaign.settle_war_battle(sti, 0, wb_i5['id'], 2)
    mv231 = next((m for m in sti.movements.values()
                  if m['commandnode_id'] == 231 and m['state'] == 0), None)
    check('I5c 路线全丢 → BFS 无友方城，回退首都 10 兜底',
          mv231 is not None
          and mv231['path'][-1]['to_bf'] == 10,
          f'{mv231 and mv231["path"][-1]["to_bf"]}')
    wardb.capitals = _orig_caps_i5

    # ---- J. 野外遭遇成局（war-gameplay-fix-plan §2：encounter/skirmish、
    #      battle 行补给线契约、行军冻结与结算恢复）----
    # 合成模板：901 纯步兵（cat1 士兵族）/ 902 坦克族（cat7）
    _orig_tpls = wardb.commandnode_templates
    wardb.commandnode_templates = lambda: {
        901: {'id': 901, 'resources': {1: 100}, 'speed': 1.0},
        902: {'id': 902, 'resources': {7: 10}, 'speed': 1.0},
    }
    try:
        # J1 相向纯步兵同段 → encounter（type 5）
        storeJ = FakeStore()
        warJ = await war_campaign.ensure_war(storeJ)
        j1 = _mk_at(storeJ, warJ['id'], 301, 1, 10)
        j2 = _mk_at(storeJ, warJ['id'], 302, 2, 20)
        j1['template_id'] = 901
        j2['template_id'] = 901
        mvJ1, _ = await war_campaign.start_movement(storeJ, j1, 20, now=NOW)
        mvJ2, _ = await war_campaign.start_movement(storeJ, j2, 10, now=NOW)
        evJ = await war_campaign.advance_movements(
            storeJ, warJ,
            now=NOW + datetime.timedelta(seconds=SEG_SEC / 2))
        wbJ = next((b for b in storeJ.battles.values()), None)
        check('J1 相向步兵同段成局 encounter（type 5、双方 state=2）',
              wbJ is not None
              and wbJ['battle_type'] == war_campaign.BATTLE_TYPE_ENCOUNTER
              and wbJ['battlefield_id'] == 101
              and storeJ.ats[301]['state'] == 2
              and storeJ.ats[302]['state'] == 2,
              f"type={wbJ and wbJ['battle_type']}")
        rowJ = pb_parse(war_campaign.msg_battle_row(wbJ))
        prep = war_campaign.battle_prep_seconds(
            war_campaign.BATTLE_TYPE_ENCOUNTER)
        import struct as _struct
        posJ = _struct.unpack('<f', rowJ[11])[0]
        check('J1b battle 行补给线契约（f3=sl/f4=type3/f9=2/f10=3/'
              'f11 position/f12 activation=started+prep）',
              int(rowJ[3]) == 101
              and int(rowJ[4]) == war_campaign.MAP_ENTITY_SUPPLYLINE
              and int(rowJ[9]) == 2 and int(rowJ[10]) == 3
              and 0.0 < posJ < 1.0
              and rowJ[12].decode() == war_campaign._fmt_ts(
                  NOW + datetime.timedelta(seconds=prep)),
              f'pos={posJ} act={rowJ[12]}')
        check('J1c position 相向中点 0.5',
              abs(wbJ['position'] - 0.5) < 1e-6, f"{wbJ['position']}")
        mrecJ = [m for m in storeJ.mission_records.values()
                 if m.get('battle_id') == wbJ['id']]
        check('J1d mission record 落库（map_entity=遭遇段起点城）',
              len(mrecJ) == 1 and mrecJ[0]['map_entity_id'] == 10,
              f'{[(m["id"], m["map_entity_id"]) for m in mrecJ]}')
        mr_rowsJ = [pb_parse(p) for k, tag, p in evJ
                    if tag == b'missionresource']
        check('J1e 野外成局推 missionresource：双方各一行 ADD'
              '（id=AT id，f2=missionid）',
              len(mr_rowsJ) == 2
              and {_mr_id(r) for r in mr_rowsJ} == {301, 302}
              and {int(r[2]) for r in mr_rowsJ}
              == {mrecJ[0]['id'] if mrecJ else -1},
              f'{[(_mr_id(r), int(r[2])) for r in mr_rowsJ]}')

        # J2 冻结：会战中不推进不到达
        await war_campaign.advance_movements(
            storeJ, warJ,
            now=NOW + datetime.timedelta(seconds=5 * SEG_SEC))
        check('J2 会战冻结（movement 仍 active、AT 停留 sl101、无新战）',
              storeJ.movements[mvJ1['id']]['state'] == 0
              and storeJ.movements[mvJ2['id']]['state'] == 0
              and storeJ.ats[301]['map_entity_id'] == 101
              and len(storeJ.battles) == 1)

        # J3 结算：无城市易主 + 幸存双方释放 + 冻结行军顺延恢复
        storeJ.ats[301]['resources'] = {1: 100}
        storeJ.ats[302]['resources'] = {1: 100}
        midJ = mrecJ[0]['id'] if mrecJ else 0
        evJ3 = await war_campaign.settle_war_battle(
            storeJ, midJ, wbJ['id'], 1)
        check('J3 野外结算：幸存双方 state=0 + 无城市易主',
              storeJ.ats[301]['state'] == 0
              and storeJ.ats[302]['state'] == 0
              and not storeJ.city_owners.get(warJ['id']),
              f'owners={storeJ.city_owners.get(warJ["id"])}')
        check('J3c 野外结算删 missionresource：双方离战行删',
              ('del', b'missionresource', 301) in evJ3
              and ('del', b'missionresource', 302) in evJ3)
        check('J3b 冻结行军顺延恢复（started_at 后移，仍 active）',
              storeJ.movements[mvJ1['id']]['state'] == 0
              and storeJ.movements[mvJ1['id']]['started_at'] > NOW
              and storeJ.movements[mvJ2['id']]['started_at'] > NOW,
              f"start={storeJ.movements[mvJ1['id']]['started_at']}")

        # J4 含坦克族 → skirmish（type 3）
        storeK = FakeStore()
        warK = await war_campaign.ensure_war(storeK)
        k1 = _mk_at(storeK, warK['id'], 311, 1, 10)
        k2 = _mk_at(storeK, warK['id'], 312, 2, 20)
        k1['template_id'] = 901
        k2['template_id'] = 902                  # 坦克族
        await war_campaign.start_movement(storeK, k1, 20, now=NOW)
        await war_campaign.start_movement(storeK, k2, 10, now=NOW)
        await war_campaign.advance_movements(
            storeK, warK,
            now=NOW + datetime.timedelta(seconds=SEG_SEC / 2))
        wbK = next((b for b in storeK.battles.values()), None)
        check('J4 任一方含装甲族 → skirmish（type 3）',
              wbK is not None
              and wbK['battle_type'] == war_campaign.BATTLE_TYPE_SKIRMISH,
              f"type={wbK and wbK['battle_type']}")

        # J5 同段同阵营不成局
        storeL = FakeStore()
        warL = await war_campaign.ensure_war(storeL)
        l1 = _mk_at(storeL, warL['id'], 321, 1, 10)
        l2 = _mk_at(storeL, warL['id'], 322, 1, 20)
        await war_campaign.start_movement(storeL, l1, 20, now=NOW)
        await war_campaign.start_movement(storeL, l2, 10, now=NOW)
        await war_campaign.advance_movements(
            storeL, warL,
            now=NOW + datetime.timedelta(seconds=SEG_SEC / 2))
        check('J5 同段同阵营不成局', not storeL.battles)

        # J6 野外会战增援：交战阵营迟到 AT 入战（对齐城市会战口径
        # append_war_battle_faction + state=2），第三阵营照常行军不入战
        storeM = FakeStore()
        warM = await war_campaign.ensure_war(storeM)
        m1 = _mk_at(storeM, warM['id'], 331, 1, 10)
        m2 = _mk_at(storeM, warM['id'], 332, 2, 20)
        m1['template_id'] = 901
        m2['template_id'] = 901
        await war_campaign.start_movement(storeM, m1, 20, now=NOW)
        await war_campaign.start_movement(storeM, m2, 10, now=NOW)
        await war_campaign.advance_movements(
            storeM, warM, now=NOW + datetime.timedelta(seconds=SEG_SEC / 2))
        wbM = next((b for b in storeM.battles.values()), None)
        m3 = _mk_at(storeM, warM['id'], 333, 1, 10)   # 攻方阵营增援
        m4 = _mk_at(storeM, warM['id'], 334, 3, 10)   # 第三阵营路过
        m3['template_id'] = 901
        m4['template_id'] = 901
        _ = await war_campaign.start_movement(
            storeM, m3, 20, now=NOW + datetime.timedelta(seconds=1))
        _ = await war_campaign.start_movement(
            storeM, m4, 20, now=NOW + datetime.timedelta(seconds=1))
        evM6 = await war_campaign.advance_movements(
            storeM, warM, now=NOW + datetime.timedelta(seconds=SEG_SEC))
        check('J6 野外增援：交战阵营入战（state=2 + factions 追加）',
              wbM is not None
              and storeM.ats[333]['state'] == 2
              and any(f.get('at_id') == 333
                      for f in storeM.battles[wbM['id']]['factions'])
              and len(storeM.battles) == 1,
              f"m3={storeM.ats[333]['state']} "
              f"n={len(storeM.battles)}")
        mr_rowsM = [pb_parse(p) for k, tag, p in evM6
                    if tag == b'missionresource']
        mrecM = next((m for m in storeM.mission_records.values()
                      if m.get('battle_id') == wbM['id']), None)
        check('J6c 野外增援推 missionresource：新行 ADD（AT 333，'
              '同战同 missionid）',
              len(mr_rowsM) == 1
              and _mr_id(mr_rowsM[0]) == 333
              and mrecM is not None
              and int(mr_rowsM[0][2]) == mrecM['id'],
              f'{[(_mr_id(r), int(r[2])) for r in mr_rowsM]}')
        check('J6b 第三阵营照常行军不入战',
              storeM.ats[334]['state'] == 1
              and not any(f.get('at_id') == 334
                          for f in storeM.battles[wbM['id']]['factions']),
              f"m4={storeM.ats[334]['state']}")

        # J7 增援并入已开局战斗服资源池（matchmaking.
        # add_reinforcement_to_live_mission → force_pool）
        import matchmaking as _mm
        import force_pool as _fp
        import state as _state_mod
        mrecM = [m for m in storeM.mission_records.values()
                 if m.get('battle_id') == wbM['id']]
        midM = mrecM[0]['id'] if mrecM else 0
        _mm._MM_MISSION_DETAILS[midM] = {'preset': 5}
        _mm.COMBAT_INSTANCES[midM] = {'stub': 1}
        _prev_store = _state_mod.STORE
        _state_mod.STORE = storeM
        _prev_comp = _fp.composition_for_template
        _prev_tn = _fp.template_name
        _fp.composition_for_template = lambda tid: {1: 100}
        _fp.template_name = lambda tid: 'infantry'
        try:
            _fp.init_mission_war(
                midM, 1, 2, {1: [{1: 50}], 2: [{1: 60}]},
                {1: ['infantry'], 2: ['infantry']},
                {1: [{'id': 331, 'name': 'a', 'owner_player_id': 9,
                      'template_id': 901}],
                 2: [{'id': 332, 'name': 'b', 'owner_player_id': 9,
                      'template_id': 901}]})
            ok = await _mm.add_reinforcement_to_live_mission(
                wbM['id'], storeM.ats[333])
            p = _fp.pool(midM)
            check('J7 增援并入 live 池（攻方 cat1 50+100=150，守方不动）',
                  ok and p and p[1][1] == 150 and p[2][1] == 60,
                  f'ok={ok} p={p}')
            names = _fp.assault_team_names(midM, 1)
            check('J7b 名单同步（攻方 names 计 2）',
                  names and names.count('infantry') == 2, f'{names}')
            _mm._MM_MISSION_DETAILS.pop(midM, None)
            ok2 = await _mm.add_reinforcement_to_live_mission(
                wbM['id'], storeM.ats[333])
            check('J7c 局未开不并入（排队局建池时按 factions 全量读取）',
                  not ok2 and _fp.pool(midM)[1][1] == 150,
                  f'ok2={ok2}')

            # J8 详情兵线按进攻方向（assign_lines）：ap_tmpl 命中激活线
            # 模板的 AT 分到对应线（war accesspoint.template = 对局图
            # accesspointtemplate.id），无方向 AT 蛇形补位
            _fp._MISSIONS[9001] = {
                'mode': 'war', 'names': ['infA', 'infB', 'infC'],
                'names_by_faction': {1: ['infA', 'infB', 'infC']},
                'war_ats': {1: [
                    {'id': 1, 'name': 'a', 'owner_player_id': 9,
                     'template_id': 901, 'ap_tmpl': 15},
                    {'id': 2, 'name': 'b', 'owner_player_id': 9,
                     'template_id': 901, 'ap_tmpl': 8},
                    {'id': 3, 'name': 'c', 'owner_player_id': 9,
                     'template_id': 901, 'ap_tmpl': 0},
                ]},
                'attacker': 1, 'defender': 2,
                'pool': {1: {'1': 330}, 2: {'1': 330}},
                'lines': None, 'line_pool': None, 'ap_line': {},
            }
            _orig_atcomp = _fp._AT_COMP
            _fp._AT_COMP = {'infA': {1: 100}, 'infB': {1: 200},
                            'infC': {1: 30}}
            try:
                _fp.assign_lines(9001, 2, [8, 15])
                check('J8 兵线按进攻方向：ap_tmpl 15→线1 / 8→线0 / '
                      '无方向蛇形补位',
                      _fp._MISSIONS[9001]['lines'][0] == ['infB', 'infC']
                      and _fp._MISSIONS[9001]['lines'][1] == ['infA']
                      and _fp._MISSIONS[9001]['ap_line'] == {8: 0, 15: 1},
                      f'{_fp._MISSIONS[9001]["lines"]}')
            finally:
                _fp._AT_COMP = _orig_atcomp
                _fp._MISSIONS.pop(9001, None)

            # J8b 分线池口径 = AT 实际存量（war_ats 携带 comp，2026-09-15
            #     修复：不再用模板满编 _AT_COMP，分线合计 = 攻方主池）；
            #     同名 AT 各归各线（at_lane_aps 匹配即消耗，不再全撞首线）
            _fp._MISSIONS[9005] = {
                'mode': 'war', 'names': ['inf', 'inf'],
                'names_by_faction': {1: ['inf', 'inf']},
                'war_ats': {1: [
                    {'id': 11, 'ap_tmpl': 8, 'comp': {1: 33}},
                    {'id': 12, 'ap_tmpl': 15, 'comp': {1: 55}},
                ]},
                'attacker': 1, 'defender': 2,
                'pool': {1: {1: 88}, 2: {1: 60}},
                'lines': None, 'line_pool': None, 'ap_line': {},
            }
            try:
                _fp.assign_lines(9005, 2, [8, 15])
                check('J8b 分线池=AT 实际存量（线0=33/线1=55，合计=主池88）',
                      _fp._MISSIONS[9005]['line_pool'] == [{1: 33}, {1: 55}],
                      f'{_fp._MISSIONS[9005]["line_pool"]}')
                check('J8b 同名 AT 各归各线（at_lane_aps 匹配即消耗）',
                      _fp.at_lane_aps(9005, 1) == [8, 15],
                      f'{_fp.at_lane_aps(9005, 1)}')
            finally:
                _fp._MISSIONS.pop(9005, None)

            # J9 war 激活线按进攻方向（用户口径：不随机凑 2-3 条，哪条
            # 线有进攻兵力就开哪条）——strict=True 只开命中本图的来向线
            _j9 = wardb.assault_ap_decision(204, 9002, prefer=(1164235,),
                                            strict=True)
            check('J9 war 激活线=来向线（只开 1164235，其余全关）',
                  _j9[1] == [1164235]
                  and sorted(_j9[2]) == [2717391, 2881042, 3386581]
                  and 1164235 not in _j9[2]
                  and wardb.assault_ap_decision(204, 9003,
                                                strict=False)[1] != [1164235],
                  f'{_j9}')

            # J10 守方红线进简报（官方规则：边线连向的相邻城市为守方占领
            # → 该线归属守方，可能多条；_init_war_mission_resources 计算
            # dfn_lanes，简报 f6 逐线发归属）
            from protocol import (pb_split_top as _pst,
                                  pb_field_payload as _pfp,
                                  pb_parse_loose as _ppl)
            _fp._MISSIONS[9004] = {
                'mode': 'war', 'attacker': 2, 'defender': 3,
                'prefer_aps': [1164235], 'dfn_lanes': [2881042, 2717391],
                'names_by_faction': {2: ['infantry'], 3: ['infantry']},
                'names': ['infantry'], 'lines': [['infantry']],
                'ap_line': {1164235: 0},
                'pool': {2: {1: 10}, 3: {1: 10}},
                'war_ats': {2: [{'ap_tmpl': 1164235}],
                            3: [{'ap_tmpl': 0}]}}
            _mm._MM_MISSION_DETAILS[9004] = {'preset': 5}
            _f610 = {}
            _f410 = []
            try:
                for _fn, _blk in _pst(_mm._msg_mission_details_response(
                        9004, 1, 't', 'z', player_faction=2, gamemap_id=204)):
                    if _fn == 4:
                        _ef4 = _ppl(_pfp(_blk))
                        _f410.append((int(_ef4.get(1)),
                                      int(_ef4.get(3) or 0)))
                    elif _fn != 6:
                        continue
                    else:
                        _ef = _ppl(_pfp(_blk))
                        _f610[int(_ef.get(1))] = int(_ef.get(2))
            finally:
                _fp._MISSIONS.pop(9004, None)
                _mm._MM_MISSION_DETAILS.pop(9004, None)
            check('J10 war 守方红线进 f6（2881042/2717391=守方 3，来向'
                  '1164235=攻方 2，空闲 3386581 不发）',
                  _f610.get(2881042) == 3 and _f610.get(2717391) == 3
                  and _f610.get(1164235) == 2
                  and _f610.get(3386581) is None,
                  f'{_f610}')
            _d_ap10 = wardb.assault_ap_decision(204, 9004,
                                                prefer=[1164235],
                                                strict=True)[0]
            check('J10 war f4 逐 AT 挂线（攻 AT=来向 1164235，守 AT=O 锚'
                  f'{_d_ap10}）',
                  (2, 1164235) in _f410
                  and all(ap == _d_ap10 for f, ap in _f410 if f == 3),
                  f'{_f410}')

            # J12 边线归属 = 相邻城市归属（Tatabánya 真实拓扑：105→
            # Komárno=攻2，101/102/104→守方占城=3，103→Székesfh=中立0；
            # 2026-09-15 实锤：来向线被夺回后须过滤，陈旧来向作废。
            # 本文件头部把 wardb 两表 monkeypatch 成合成拓扑，此处临时
            # 还原真实表再算）
            import war_campaign.common as _wc
            _own12 = {703985954: 3, 421999695: 2, 176056349: 3,
                      781602986: 3, 436884145: 3, 591550748: 0}
            _saved12 = (wardb.accesspoints, wardb.supplylines,
                        _wc._ADJACENCY_CACHE)
            try:
                wardb.accesspoints = lambda: [
                    {'id': int(r.get(1, 0) or 0), 'mapid': 0,
                     'template': int(r.get(3, 0) or 0),
                     'battlefield': int(r.get(4, 0) or 0)}
                    for r in wardb._parse_wardb_table('accesspoint')]
                wardb.supplylines = lambda: [
                    {'id': int(r.get(1, 0) or 0), 'mapid': 0,
                     'ap1': int(r.get(3, 0) or 0),
                     'ap2': int(r.get(4, 0) or 0), 'template_id': 0}
                    for r in wardb._parse_wardb_table('supplyline')]
                _wc._ADJACENCY_CACHE = None
                _lo12 = _mm._lane_ownership(
                    703985954, _own12,
                    {a['id']: a['template'] for a in wardb.accesspoints()})
            finally:
                (wardb.accesspoints, wardb.supplylines,
                 _wc._ADJACENCY_CACHE) = _saved12
            check('J12 Tatabánya 边线归属（105=攻2，101/102/104=守3，'
                  '103=中立0）',
                  _lo12.get(105) == 2 and _lo12.get(101) == 3
                  and _lo12.get(102) == 3 and _lo12.get(104) == 3
                  and _lo12.get(103) == 0, f'{_lo12}')

            # J11 war 守方被包围（红线 0 条）→ 一条守方线都不开，不兜底
            _fp._MISSIONS[9005] = {
                'mode': 'war', 'attacker': 2, 'defender': 3,
                'prefer_aps': [1164235], 'dfn_lanes': [],
                'names_by_faction': {2: ['infantry'], 3: ['infantry']},
                'names': ['infantry'], 'pool': {2: {1: 10}, 3: {1: 10}},
                'war_ats': {2: [{'ap_tmpl': 1164235}],
                            3: [{'ap_tmpl': 0}]}}
            _mm._MM_MISSION_DETAILS[9005] = {'preset': 5}
            _f611 = {}
            try:
                for _fn, _blk in _pst(_mm._msg_mission_details_response(
                        9005, 1, 't', 'z', player_faction=2, gamemap_id=204)):
                    if _fn != 6:
                        continue
                    _ef = _ppl(_pfp(_blk))
                    _f611[int(_ef.get(1))] = int(_ef.get(2))
            finally:
                _fp._MISSIONS.pop(9005, None)
                _mm._MM_MISSION_DETAILS.pop(9005, None)
            check('J11 war 守方被包围（红线 0）→ f6 无守方线（仅来向'
                  '1164235=攻方 2）',
                  _f611 == {1164235: 2}, f'{_f611}')
        finally:
            _fp.composition_for_template = _prev_comp
            _fp.template_name = _prev_tn
            _state_mod.STORE = _prev_store
            _mm._MM_MISSION_DETAILS.pop(midM, None)
            _mm.COMBAT_INSTANCES.pop(midM, None)
            _fp.release(midM)
    finally:
        wardb.commandnode_templates = _orig_tpls

    # ---- K. automission 阈值/tie 判定（war-gameplay-fix-plan §3/§4/§7/§8）----
    import combat_instances
    _orig_preset_loader = wardb._load_preset
    _orig_cp_loader = wardb._load_capturepoint_templates
    _orig_tpls_k = wardb.commandnode_templates
    _orig_avg_k = wardb.army_resource_avg_sizes
    # P4-3 加权权重（armyresourcecategory.avgSize 官方值：步兵 1 / 运输机
    # 55），K11 用；真实表 diff 佐证见 docs/community-asset-diff-2026-09.md
    wardb.army_resource_avg_sizes = lambda: {1: 1.0, 10: 55.0}
    # preset 5 → gamemap 55（三点图 3 据点）/ preset 6 → gamemap 66（单点 1）
    wardb._load_preset = lambda pid: {3: 55 if int(pid or 0) == 5 else 66}
    wardb._load_capturepoint_templates = (
        lambda gm: [1, 2, 3] if int(gm) == 55 else [120])
    wardb.commandnode_templates = lambda: {
        901: {'id': 901, 'resources': {1: 100}, 'speed': 1.0},
        108: {'id': 108, 'name': 'paratroopers', 'actionid': 13,
              'resources': {4: 300}, 'convert_to': 114, 'speed': 1.0},
        114: {'id': 114, 'name': 'paratroopers_on_ground', 'actionid': 13,
              'resources': {4: 300}, 'convert_to': 108, 'speed': 1.0},
    }
    RN = datetime.datetime.now(datetime.timezone.utc)

    async def _mk_auto_battle(btype, atk_res, def_res, preset=5,
                              started=None, atk_tpl=0, def_tpl=0):
        """直造一场 automission 待判会战（攻 f1 守 f2、双方 state=2）。"""
        st = FakeStore()
        w = await war_campaign.ensure_war(st)
        a = _mk_at(st, w['id'], 401, 1, 20)
        d = _mk_at(st, w['id'], 402, 2, 20)
        a['resources'] = dict(atk_res)
        d['resources'] = dict(def_res)
        a['template_id'] = atk_tpl
        d['template_id'] = def_tpl
        a['state'] = 2
        d['state'] = 2
        wb = await st.create_war_battle(
            w['id'], 20 if btype == war_campaign.BATTLE_TYPE_ASSAULT else 101,
            [{'at_id': 401, 'faction': 1}, {'at_id': 402, 'faction': 2}],
            battle_type=btype)
        if started is not None:
            st.battles[wb['id']]['started_at'] = started
            wb['started_at'] = started
        mid = await st.create_mission_record(
            preset, attacker_faction=1, defender_faction=2,
            battle_id=wb['id'])
        return st, w, wb, int(mid)

    try:
        # K1 野外单点图（preset 6 → 1 据点 → 门槛 144）：攻 100 < 144 ≤
        #    守 200 → 攻方败（损 25% 编制 + 20 士气 + 释放；胜方无损无经验）
        st1, _w1, wb1, _m1 = await _mk_auto_battle(
            war_campaign.BATTLE_TYPE_ENCOUNTER,
            {1: 100}, {1: 200}, preset=6)
        await war_campaign.automission_tick(st1)
        b1 = st1.battles[wb1['id']]
        check('K1 野外单点 144 门槛：攻方不足败北（胜方=守方）',
              b1['state'] == 1 and b1['winning_faction'] == 2,
              f"state={b1['state']} win={b1['winning_faction']}")
        check('K1b 败方损 25% 编制 + 20 士气 + 释放 state=0',
              st1.ats[401]['resources'] == {1: 75}
              and st1.ats[401]['morale'] == 80
              and st1.ats[401]['state'] == 0,
              f"res={st1.ats[401]['resources']} "
              f'morale={st1.ats[401]["morale"]}')
        check('K1c 胜方零损耗 + 系统判定无经验无战币',
              st1.ats[402]['resources'] == {1: 200}
              and st1.ats[402]['morale'] == 100
              and 'battles' not in st1.ats[401]
              and not getattr(st1, 'funds_log', []),
              f"res={st1.ats[402]['resources']} "
              f"funds={getattr(st1, 'funds_log', None)}")

        # K2 野外三点图（preset 5 → 3 据点 → 门槛 216）：攻 200 < 216 ≤
        #    守 250 → 攻方败（单点门槛下 200 本可胜）
        st2, _w2, wb2, _m2 = await _mk_auto_battle(
            war_campaign.BATTLE_TYPE_SKIRMISH,
            {1: 200}, {1: 250}, preset=5)
        await war_campaign.automission_tick(st2)
        check('K2 野外三点 216 门槛：攻 200 不足败北',
              st2.battles[wb2['id']]['winning_faction'] == 2
              and st2.ats[401]['resources'] == {1: 150},
              f"win={st2.battles[wb2['id']]['winning_faction']} "
              f"res={st2.ats[401]['resources']}")

        # K2b 野外单点图守方不足：攻 200 ≥ 144 > 守 100 → 攻方胜
        st2b, _w2b, wb2b, _m2b = await _mk_auto_battle(
            war_campaign.BATTLE_TYPE_ENCOUNTER,
            {1: 200}, {1: 100}, preset=6)
        await war_campaign.automission_tick(st2b)
        check('K2b 野外守方不足 → 攻方胜（守方损 25%）',
              st2b.battles[wb2b['id']]['winning_faction'] == 1
              and st2b.ats[402]['resources'] == {1: 75}
              and st2b.ats[401]['resources'] == {1: 200},
              f"win={st2b.battles[wb2b['id']]['winning_faction']}")

        # K3 城攻（assault 非伞兵）：攻 100 < 216 → 攻方败城不易主
        st3, _w3, wb3, _m3 = await _mk_auto_battle(
            war_campaign.BATTLE_TYPE_ASSAULT, {1: 100}, {1: 250})
        await war_campaign.automission_tick(st3)
        check('K3 城攻攻方 <216 败北、城不易主',
              st3.battles[wb3['id']]['winning_faction'] == 2
              and st3.city_owners.get(_w3['id'], {}).get(20) != 1,
              f"win={st3.battles[wb3['id']]['winning_faction']} "
              f"owner={st3.city_owners.get(_w3['id'], {}).get(20)}")
        # K3b 城攻击方 ≥216 → 攻方胜城易主（守方 {1:1} 被 25% 损耗归零歼灭）
        st3b, _w3b, wb3b, _m3b = await _mk_auto_battle(
            war_campaign.BATTLE_TYPE_ASSAULT, {1: 300}, {1: 1})
        await war_campaign.automission_tick(st3b)
        check('K3b 城攻击方 ≥216 攻方胜 + 城易主',
              st3b.battles[wb3b['id']]['winning_faction'] == 1
              and st3b.city_owners.get(_w3b['id'], {}).get(20) == 1,
              f"win={st3b.battles[wb3b['id']]['winning_faction']} "
              f"owner={st3b.city_owners.get(_w3b['id'], {}).get(20)}")

        # K3c 城攻双方 ≥216 → tie（war1.md「双方都满足则等待6小时后比较
        #    兵力」，与 §7 伞兵分支同构；修复前攻方直接胜、守方满编判负）：
        #    6h 窗口内不结算城不易主，超时后兵力少者败（300 vs 250 → 攻胜
        #    占城）
        st3c, _w3c, wb3c, _m3c = await _mk_auto_battle(
            war_campaign.BATTLE_TYPE_ASSAULT, {1: 300}, {1: 250},
            started=RN - datetime.timedelta(hours=1))
        await war_campaign.automission_tick(st3c)
        k3c_wait = (st3c.battles[wb3c['id']]['state'] == 0
                    and st3c.city_owners.get(_w3c['id'], {}).get(20) != 1)
        st3c.battles[wb3c['id']]['started_at'] = (
            RN - datetime.timedelta(hours=7))
        await war_campaign.automission_tick(st3c)
        check('K3c 城攻双方达标 → tie，6h 后兵力少者败（攻 300 胜占城）',
              k3c_wait
              and st3c.battles[wb3c['id']]['winning_faction'] == 1
              and st3c.city_owners.get(_w3c['id'], {}).get(20) == 1
              and st3c.ats[402]['resources'] == {1: 187},
              f"wait={k3c_wait} "
              f"win={st3c.battles[wb3c['id']]['winning_faction']} "
              f"owner={st3c.city_owners.get(_w3c['id'], {}).get(20)} "
              f"res={st3c.ats[402]['resources']}")

        # K4 伞兵攻城：攻方伞兵（tpl 108）cat4 100 < 288 → 攻方败 +
        #    落地变步兵（convert_to 114 *_on_ground）
        st4, _w4, wb4, _m4 = await _mk_auto_battle(
            war_campaign.BATTLE_TYPE_ASSAULT, {4: 100}, {1: 250},
            atk_tpl=108)
        await war_campaign.automission_tick(st4)
        check('K4 伞兵 <288 攻方败北 + 落地变步兵（tpl 108→114）',
              st4.battles[wb4['id']]['winning_faction'] == 2
              and st4.ats[401]['template_id'] == 114
              and st4.ats[401]['resources'] == {4: 75},
              f"win={st4.battles[wb4['id']]['winning_faction']} "
              f"tpl={st4.ats[401]['template_id']}")
        # K5 伞兵 ≥288 且守方 <216 → 伞兵胜占城（★官方口径：胜方运输机
        #    也飞回出发机场——落地变体驻城 tpl 108→114；2026-09-11 敌区
        #    交战时序修复前旧期望为保持 108）
        st5, _w5, wb5, _m5 = await _mk_auto_battle(
            war_campaign.BATTLE_TYPE_ASSAULT, {4: 300}, {1: 100},
            atk_tpl=108)
        await war_campaign.automission_tick(st5)
        check('K5 伞兵 ≥288 守方 <216 → 伞兵胜占城 + 落地（tpl 108→114）',
              st5.battles[wb5['id']]['winning_faction'] == 1
              and st5.city_owners.get(_w5['id'], {}).get(20) == 1
              and st5.ats[401]['template_id'] == 114,
              f"win={st5.battles[wb5['id']]['winning_faction']} "
              f"tpl={st5.ats[401]['template_id']}")

        # K6 tie：双方满足（三点 216）→ 6h 窗口内不结算；超时后少者败
        st6, _w6, wb6, _m6 = await _mk_auto_battle(
            war_campaign.BATTLE_TYPE_SKIRMISH,
            {1: 250}, {1: 250}, preset=5, started=RN - datetime.timedelta(
                hours=1))
        await war_campaign.automission_tick(st6)
        k6_wait = st6.battles[wb6['id']]['state'] == 0
        st6.battles[wb6['id']]['started_at'] = (
            RN - datetime.timedelta(hours=7))
        st6.ats[402]['resources'] = {1: 200}     # 少者败（250 vs 200）
        await war_campaign.automission_tick(st6)
        check('K6 tie：6h 窗口内不结算，超时后兵力少者败',
              k6_wait
              and st6.battles[wb6['id']]['winning_faction'] == 1
              and st6.ats[402]['resources'] == {1: 150},
              f"wait={k6_wait} "
              f"win={st6.battles[wb6['id']]['winning_faction']}")

        # K7 未到准备期超时（野外 300s / 城攻 330s）不结算
        st7, _w7, wb7, _m7 = await _mk_auto_battle(
            war_campaign.BATTLE_TYPE_ENCOUNTER, {1: 100}, {1: 200},
            preset=6, started=RN - datetime.timedelta(seconds=60))
        await war_campaign.automission_tick(st7)
        k7a = st7.battles[wb7['id']]['state'] == 0
        st7b, _w7b, wb7b, _m7b = await _mk_auto_battle(
            war_campaign.BATTLE_TYPE_ASSAULT, {1: 100}, {1: 250},
            started=RN - datetime.timedelta(seconds=60))
        await war_campaign.automission_tick(st7b)
        check('K7 准备期内（60s < 300/330s）不结算',
              k7a and st7b.battles[wb7b['id']]['state'] == 0,
              f'field={k7a} assault={st7b.battles[wb7b["id"]]["state"]}')

        # K8 真人局战斗会话存活 → 不触碰
        st8, _w8, wb8, m8 = await _mk_auto_battle(
            war_campaign.BATTLE_TYPE_ASSAULT, {1: 100}, {1: 250})
        st8.mission_records[m8]['state'] = 1
        combat_instances.COMBAT_INSTANCES[m8] = object()
        try:
            await war_campaign.automission_tick(st8)
        finally:
            combat_instances.COMBAT_INSTANCES.pop(m8, None)
        check('K8 真人局会话存活不结算',
              st8.battles[wb8['id']]['state'] == 0
              and st8.ats[401]['state'] == 2,
              f"state={st8.battles[wb8['id']]['state']}")

        # K9 会话消亡的中途退场局 → 旧口径资源量比较（长超时兜底）
        st9, _w9, wb9, m9 = await _mk_auto_battle(
            war_campaign.BATTLE_TYPE_ASSAULT, {1: 100}, {1: 200})
        st9.mission_records[m9]['state'] = 1    # 不在 COMBAT_INSTANCES
        await war_campaign.automission_tick(st9)
        check('K9 会话消亡局旧口径兜底（资源多者胜、60% 败方损耗）',
              st9.battles[wb9['id']]['winning_faction'] == 2
              and st9.ats[401]['resources'] == {1: 40},
              f"win={st9.battles[wb9['id']]['winning_faction']} "
              f"res={st9.ats[401]['resources']}")

        # K11 旧口径兜底按 avgSize 加权（P4-3）：攻运输机 {10:2}=2×55=110
        #     vs 守步兵 {1:100}=100×1=100 → 攻方单位数更少仍胜（旧总量
        #     口径 2 < 100 会误判守方胜）；守方 60% 损耗归 40
        st11, _w11, wb11, m11 = await _mk_auto_battle(
            war_campaign.BATTLE_TYPE_ASSAULT, {10: 2}, {1: 100})
        st11.mission_records[m11]['state'] = 1
        await war_campaign.automission_tick(st11)
        check('K11 加权强度：运输机 2×55=110 > 步兵 100×1=100 攻方胜',
              st11.battles[wb11['id']]['winning_faction'] == 1
              and st11.ats[402]['resources'] == {1: 40},
              f"win={st11.battles[wb11['id']]['winning_faction']} "
              f"res={st11.ats[402]['resources']}")

        # K10 tie 持平收局（缺口 1，war-para-battle-briefing-investigation
        #     .md）：双方兵力持平 → winner=0 + 伞兵照常落地（tpl 108→114）
        #     + settle ③b 攻方（非城主）撤退、守方（城主）留守、不再成局
        #     （修复前 tie 双方原地释放 → D 修复立即再成局 → battle churn =
        #     城市信息图反复被自动关掉的根因）
        st10, w10, wb10, _m10 = await _mk_auto_battle(
            war_campaign.BATTLE_TYPE_ASSAULT, {4: 300}, {1: 300},
            atk_tpl=108, started=RN - datetime.timedelta(hours=7))
        st10.city_owners[w10['id']] = {10: 1, 20: 2}
        await war_campaign.automission_tick(st10)
        r10 = await st10.get_last_movement(401)
        check('K10 tie 持平：伞兵落地（tpl 114）+ 攻方撤退守方留守',
              st10.ats[401]['template_id'] == 114
              and st10.ats[401]['state'] == 1
              and r10 and r10['path'][-1]['to_bf'] == 10
              and st10.ats[402]['state'] == 0
              and st10.ats[402]['map_entity_id'] == 20,
              f"tpl={st10.ats[401]['template_id']} "
              f"atk={st10.ats[401]['state']} def={st10.ats[402]['state']}")
        check('K10b tie：battle 收局 winner=0 且不再成局（无 churn）',
              st10.battles[wb10['id']]['state'] == 1
              and st10.battles[wb10['id']]['winning_faction'] == 0
              and len(st10.battles) == 1,
              f"win={st10.battles[wb10['id']]['winning_faction']} "
              f'battles={len(st10.battles)}')
    finally:
        wardb._load_preset = _orig_preset_loader
        wardb._load_capturepoint_templates = _orig_cp_loader
        wardb.commandnode_templates = _orig_tpls_k
        wardb.army_resource_avg_sizes = _orig_avg_k

    # ---- L. war_pending 简报兵力（大厅战局详情 f4/f5 = battle roster
    #      真实 AT，非 FALLBACK 静态锚点；2026-09-11 玩家报"成局战局
    #      兵力值展示不对"修复回归）----
    import mm_handler
    import state as _st_l
    from protocol import pb_field_payload, pb_split_top
    stL = FakeStore()
    warL2 = await war_campaign.ensure_war(stL)
    aL1 = _mk_at(stL, warL2['id'], 441, 1, 10)
    aL2 = _mk_at(stL, warL2['id'], 442, 1, 10)
    dL1 = _mk_at(stL, warL2['id'], 443, 2, 10)
    aL1['template_id'] = 901
    aL2['template_id'] = 901
    dL1['template_id'] = 901
    aL1['resources'] = {1: 90}
    aL2['resources'] = {1: 60, 6: 12}
    dL1['resources'] = {1: 120}
    aL1['owner_player_id'] = 7001
    dL1['owner_player_id'] = 7002
    stL.ats[442]['state'] = 3        # 被歼：不计入简报
    wbL = await stL.create_war_battle(
        warL2['id'], 10,
        [{'at_id': 441, 'faction': 1}, {'at_id': 442, 'faction': 1},
         {'at_id': 443, 'faction': 2}],
        battle_type=war_campaign.BATTLE_TYPE_ASSAULT)
    _prev_store_l = _st_l.STORE
    _st_l.STORE = stL
    try:
        roster = await mm_handler._war_pending_roster(int(wbL['id']))
        check('L1 roster：编制合计（state>=3 跳过）',
              roster is not None
              and roster[1] == {1: {1: 90}, 2: {1: 120}}
              and [a['id'] for a in roster[0][1]] == [441]
              and [a['id'] for a in roster[0][2]] == [443],
              f'{roster and roster[1]}')
        # L4 activation：war_pending det f11 = battle 行 started_at+准备期
        #    （单一事实源；修复"刚成局显示已启动 1 小时"——旧口径
        #    _zulu_activated 固定回溯 1h）；普通 det 回退 zulu 参数
        from war_campaign.common import battle_activation_ts as _bat
        act = roster[2]
        check('L4 activation = battle 行激活时刻（非 1 小时回溯）',
              act == _bat(stL.battles[int(wbL['id'])])
              and act.endswith('Z') and len(act) == 15,
              f'act={act}')
        det = {'battle_id': int(wbL['id']), 'preset': 10,
               'map_entity_id': 10,
               'attacker_faction': 1, 'defender_faction': 2,
               'war_pending': True, 'players': [],
               'war_at_rows': roster[0], 'war_counts': roster[1],
               'activation': act}
        raw = mm_handler._msg_mission_details_response_multi(
            990100, det, 'zulu')
        f11 = [pb_parse(pb_field_payload(blk))[11]
               for fn, blk in pb_split_top(raw)
               if fn == 2 and 11 in pb_parse(pb_field_payload(blk))]
        check('L4b MissionInfo f11 = det activation；无 activation 回退 zulu',
              f11 == [act.encode()]
              and pb_parse(pb_field_payload(
                  next(blk for fn, blk in pb_split_top(
                      mm_handler._msg_mission_details_response_multi(
                          990101, {**det, 'activation': None}, 'zulu'))
                  if fn == 2)))[11] == b'zulu',
              f'f11={f11}')
        det = {'battle_id': int(wbL['id']), 'preset': 10,
               'map_entity_id': 10,
               'attacker_faction': 1, 'defender_faction': 2,
               'war_pending': True, 'players': [],
               'war_at_rows': roster[0], 'war_counts': roster[1]}
        raw = mm_handler._msg_mission_details_response_multi(
            990100, det, 'zulu')
        f4_rows = [pb_parse(pb_field_payload(blk))
                   for fn, blk in pb_split_top(raw) if fn == 4]
        f5_rows = [pb_parse(pb_field_payload(blk))
                   for fn, blk in pb_split_top(raw) if fn == 5]
        atk_inf = sum(int(r[4]) for r in f5_rows
                      if int(r[1]) == 1 and int(r[2]) == 1)
        dfd_inf = sum(int(r[4]) for r in f5_rows
                      if int(r[1]) == 2 and int(r[2]) == 1)
        check('L2 f5 兵力 = roster 真实编制（攻 90/守 120，非 1800 锚点）',
              atk_inf == 90 and dfd_inf == 120,
              f'atk={atk_inf} dfd={dfd_inf}')
        atk_f4 = [(int(r[2]), int(r[4])) for r in f4_rows if int(r[1]) == 1]
        dfd_f4 = [(int(r[2]), int(r[4])) for r in f4_rows if int(r[1]) == 2]
        check('L3 f4 AT 行 = 真实模板 id + 属主（被歼 AT 不发）',
              atk_f4 == [(901, 7001)] and dfd_f4 == [(901, 7002)],
              f'atk={atk_f4} dfd={dfd_f4}')
    finally:
        _st_l.STORE = _prev_store_l

    # ---- M. war-para-battle-briefing-investigation.md §6 修复回归
    #      （缺口 1 tie 撤退 / 缺口 2 第三阵营再成局 / 缺口 3 成局回滚）----
    # M1 tie（winner=0）城战结算：攻方（非城主）撤退回出发城、守方
    #     （城主）留守原地、无再成局（修复前 tie 双方原地释放 → D 修复
    #     立即再成局 → formed/settled 无限循环 = 简报反复被关的根因）
    stM1 = FakeStore()
    warM1 = await war_campaign.ensure_war(stM1)
    stM1.city_owners[warM1['id']] = {10: 1, 20: 2}
    m1a = _mk_at(stM1, warM1['id'], 511, 1, 10)         # US 从 10 攻 20
    m1d = _mk_at(stM1, warM1['id'], 512, 2, 20)         # GE 城主驻 20
    await war_campaign.start_movement(stM1, m1a, 20, now=NOW)
    await war_campaign.advance_movements(
        stM1, warM1, now=NOW + datetime.timedelta(seconds=SEG_SEC + 5))
    wbM1 = next(iter(stM1.battles.values()))
    stM1.ats[511]['resources'] = {1: 100}
    stM1.ats[512]['resources'] = {1: 100}
    await war_campaign.settle_war_battle(stM1, 0, wbM1['id'], 0)
    rmv1 = await stM1.get_last_movement(511)
    check('M1 tie：攻方撤退行军（state=1 回城 10）、守方城主留守',
          stM1.ats[511]['state'] == 1
          and rmv1 and rmv1['path'][-1]['to_bf'] == 10
          and stM1.ats[512]['state'] == 0
          and stM1.ats[512]['map_entity_id'] == 20,
          f"atk={stM1.ats[511]['state']} def={stM1.ats[512]['state']}")
    check('M1b tie：battle 收局 winning_faction=0 且不再成局（无 churn）',
          stM1.battles[wbM1['id']]['state'] == 1
          and stM1.battles[wbM1['id']]['winning_faction'] == 0
          and len(stM1.battles) == 1,
          f"win={stM1.battles[wbM1['id']]['winning_faction']} "
          f'battles={len(stM1.battles)}')

    # M2 战斗期间到场的第三阵营 AT：结算后 D 修复候选扩为全城非城主
    #     state=0 AT（缺口 2）→ 以 SU 为基准再成局；修复前只扫 wb.factions
    #     → SU 永不再成局（城上挂 AT 图标但永远没有会战）
    stM2 = FakeStore()
    warM2 = await war_campaign.ensure_war(stM2)
    stM2.city_owners[warM2['id']] = {10: 2, 20: 1}
    m2us = _mk_at(stM2, warM2['id'], 521, 1, 20)        # US 城主驻 20
    m2ge = _mk_at(stM2, warM2['id'], 522, 2, 10)        # GE 从 10 攻 20
    await war_campaign.start_movement(stM2, m2ge, 20, now=NOW)
    await war_campaign.advance_movements(
        stM2, warM2, now=NOW + datetime.timedelta(seconds=SEG_SEC + 5))
    m2su = _mk_at(stM2, warM2['id'], 523, 3, 30)        # SU 第三方到场
    await war_campaign.start_movement(stM2, m2su, 20, now=NOW)
    await war_campaign.advance_movements(
        stM2, warM2,
        now=NOW + datetime.timedelta(seconds=2 * SEG_SEC + 10))
    wbM2 = next(iter(stM2.battles.values()))
    check('M2 前置：GE vs US 成局 + SU 第三方到场不入战（两方制）',
          {f['faction'] for f in wbM2['factions']} == {1, 2}
          and stM2.ats[523]['state'] == 0
          and stM2.ats[523]['map_entity_id'] == 20,
          f"su={stM2.ats[523]['state']} "
          f"factions={[f['faction'] for f in wbM2['factions']]}")
    stM2.ats[521]['resources'] = {1: 100}
    stM2.ats[522]['resources'] = {1: 100}
    await war_campaign.settle_war_battle(stM2, 0, wbM2['id'], 1)
    newM2 = [b for b in stM2.battles.values() if b['id'] != wbM2['id']]
    check('M2 第三阵营再成局（D 候选扩全城）：SU 与城主 US 立即开新战',
          len(newM2) == 1
          and {f['faction'] for f in newM2[0]['factions']} == {1, 3}
          and stM2.ats[521]['state'] == 2
          and stM2.ats[523]['state'] == 2,
          f'battles={[(b["id"], [f["faction"] for f in b["factions"]]) for b in stM2.battles.values()]}')
    rmv2 = await stM2.get_last_movement(522)
    check('M2b 败方 GE 撤退回出发城 10（winner>0 原行为不回归）',
          stM2.ats[522]['state'] == 1
          and rmv2 and rmv2['path'][-1]['to_bf'] == 10
          and stM2.battles[wbM2['id']]['winning_faction'] == 1,
          f"ge={stM2.ats[522]['state']}")

    # M3 城战成局 mission record 落库失败 → 整体回滚（缺口 3）：battle
    #     关闭 + 双方回待命 + 不推 battle 行（修复前"有 battle 无 mission"
    #     半态 → 战图标记可点但 GetMissionDetails 恒 43 → 简报秒关）
    stM3 = FakeStore()
    warM3 = await war_campaign.ensure_war(stM3)
    m3us = _mk_at(stM3, warM3['id'], 541, 1, 20)
    m3ge = _mk_at(stM3, warM3['id'], 542, 2, 10)

    async def _raise_mission(*_a, **_kw):
        raise RuntimeError('db down')
    stM3.create_mission_record = _raise_mission
    await war_campaign.start_movement(stM3, m3ge, 20, now=NOW)
    evM3 = await war_campaign.advance_movements(
        stM3, warM3, now=NOW + datetime.timedelta(seconds=SEG_SEC + 5))
    check('M3 城战成局失败回滚：battle 关闭 + 双方回待命、无 mission',
          len(stM3.battles) == 1
          and next(iter(stM3.battles.values()))['state'] == 1
          and next(iter(stM3.battles.values()))['winning_faction'] == 0
          and stM3.ats[541]['state'] == 0
          and stM3.ats[542]['state'] == 0
          and not stM3.mission_records,
          f"us={stM3.ats[541]['state']} ge={stM3.ats[542]['state']}")
    check('M3b 不推 battle 行（客户端无交战标记 → 简报不秒关）',
          all(e[1] != b'battle' for e in evM3),
          f'{[(e[0], e[1]) for e in evM3]}')

    # M4 野外遭遇成局失败回滚（缺口 3 同源）：battle 关闭 + 双方恢复行军
    #     （state=1）+ movement 仍 active + 不推 battle 行
    _orig_tpls_m4 = wardb.commandnode_templates
    wardb.commandnode_templates = lambda: {
        901: {'id': 901, 'resources': {1: 100}, 'speed': 1.0},
    }
    try:
        stM4 = FakeStore()
        warM4 = await war_campaign.ensure_war(stM4)
        m4a = _mk_at(stM4, warM4['id'], 551, 1, 10)
        m4b = _mk_at(stM4, warM4['id'], 552, 2, 20)
        m4a['template_id'] = 901
        m4b['template_id'] = 901
        mvM4a, _ = await war_campaign.start_movement(
            stM4, m4a, 20, now=NOW)
        mvM4b, _ = await war_campaign.start_movement(
            stM4, m4b, 10, now=NOW)
        stM4.create_mission_record = _raise_mission
        evM4 = await war_campaign.advance_movements(
            stM4, warM4, now=NOW + datetime.timedelta(seconds=SEG_SEC / 2))
        check('M4 野外成局失败回滚：battle 关闭 + 双方恢复行军（state=1）',
              len(stM4.battles) == 1
              and next(iter(stM4.battles.values()))['state'] == 1
              and stM4.ats[551]['state'] == 1
              and stM4.ats[552]['state'] == 1
              and stM4.movements[mvM4a['id']]['state'] == 0
              and stM4.movements[mvM4b['id']]['state'] == 0
              and not stM4.mission_records,
              f"a={stM4.ats[551]['state']} b={stM4.ats[552]['state']}")
        check('M4b 不推 battle 行',
              all(e[1] != b'battle' for e in evM4),
              f'{[(e[0], e[1]) for e in evM4]}')
    finally:
        wardb.commandnode_templates = _orig_tpls_m4

    # ---- N. reserve AT armyresource 行属主过滤（2026-09-15 19:31 卡死）----
    # 客户端链：非属主收到他人 reserve（补给队/空降拆分运输机队）资源行 →
    # updateArmyResources 写 _reservesThatRequireParentUpdate（parent 永不在
    # 本人列表）→ 开战争图 handleReservesMissingParentInfo 把 null parent
    # 直传 addReserveResourcesToParentAssaultTeam → param2.templateId #1009。
    # 修复：快照与广播两层均只把 reserve 资源行发给属主。
    check('N0 _armyresource_row_at_id set/del 行归属解析',
          war_campaign.tick._armyresource_row_at_id(
              war_campaign.msg_armyresource_rows(
                  {'id': 238, 'resources': {'10': 3}}, 1)[0]) == 238
          and war_campaign.tick._armyresource_row_at_id(238 * 1000 + 10) == 238
          and war_campaign.tick._armyresource_row_at_id(b'\x10\x01') is None)

    stN = FakeStore()
    warN = await war_campaign.ensure_war(stN)
    own = _mk_at(stN, warN['id'], 91, 1, 10)      # owner_player_id=191
    own['template_id'] = 104                      # reserve（4=补给队/机队族）
    own['resources'] = {'10': 2}
    inf = _mk_at(stN, warN['id'], 92, 1, 10)
    inf['template_id'] = 103                      # 非 reserve 步兵
    inf['resources'] = {'1': 90}

    def _res_ids(snap):
        return {int(pb_parse(p)[1]) for t, p in snap if t == b'armyresource'}

    snap_own = await war_campaign.build_warmap_snapshot(
        stN, warN, player_id=191)
    snap_foreign = await war_campaign.build_warmap_snapshot(
        stN, warN, player_id=999)
    snap_anon = await war_campaign.build_warmap_snapshot(stN, warN)
    check('N1 快照 reserve 资源行仅属主可见（匿名/他人全隐藏）',
          _res_ids(snap_own) == {91 * 1000 + 10, 92 * 1000 + 1}
          and _res_ids(snap_foreign) == {92 * 1000 + 1}
          and _res_ids(snap_anon) == {92 * 1000 + 1})

    class _FakeSessN:
        def __init__(self, pid):
            self.player_id = pid
            self.warmap_view_cmd = 777
            self.writer = object()

        async def ensure_player(self):
            return self.player_id

    import state as state_mod
    import messages as messages_mod
    _fs_orig, _pb_orig = state_mod.FLASH_SESSIONS, messages_mod._push_kvcs_batch
    captured = []

    async def _fake_batch(w, cmd, evs, preserve_order=False, allow_zip=True):
        captured.append(list(evs))

    messages_mod._push_kvcs_batch = _fake_batch
    state_mod.FLASH_SESSIONS = {1: _FakeSessN(191), 2: _FakeSessN(999),
                                3: _FakeSessN(None)}
    try:
        evN = [('set', b'CommandNodeDefinition', b''),
               ('set', b'armyresource',
                war_campaign.msg_armyresource_rows(own, warN['id'])[0]),
               ('set', b'armyresource',
                war_campaign.msg_armyresource_rows(inf, warN['id'])[0]),
               ('del', b'armyresource', 91 * 1000 + 10)]
        await war_campaign.tick.broadcast_war_events(evN, stN)

        def _ar_at_ids(c):
            return [war_campaign.tick._armyresource_row_at_id(e[2])
                    for e in c if e[1] == b'armyresource']

        check('N2 广播 reserve set+del 行仅属主会话收到，非 armyresource 行全推',
              len(captured) == 3
              and _ar_at_ids(captured[0]) == [91, 92, 91]
              and _ar_at_ids(captured[1]) == [92]
              and _ar_at_ids(captured[2]) == [92]
              and all(any(e[1] == b'CommandNodeDefinition' for e in c)
                      for c in captured))
    finally:
        messages_mod._push_kvcs_batch = _pb_orig
        state_mod.FLASH_SESSIONS = _fs_orig

    fails = [l for l, ok in RESULTS if not ok]
    print(f'\n{"="*50}\nwar_movement 回归：{len(RESULTS)-len(fails)}/{len(RESULTS)} PASS')
    if fails:
        for l in fails:
            print(f'  FAIL: {l}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
