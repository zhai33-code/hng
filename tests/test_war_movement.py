# -*- coding: utf-8 -*-
"""RTS AT 移动系统回归（war_campaign.py Phase 6，DB-free 合成拓扑）。

覆盖：
  A. 拓扑/寻路：supplyline_adjacency 邻接、find_supplyline_path BFS
     （同地 []、不可达 None、路径连续性）
  B. segment_seconds：坐标距离公式 + 无坐标回退 SEGMENT_FALLBACK_SECONDS
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
war_campaign._ADJACENCY_CACHE = None
war_campaign._BF_POS_CACHE = None

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
        return self.next_mission_id

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

    # ---- war_battles ----
    async def create_war_battle(self, war_id, battlefield_id, factions):
        wb = {'id': self.next_battle_id, 'war_id': int(war_id),
              'battlefield_id': int(battlefield_id),
              'factions': [dict(f) for f in (factions or [])],
              'state': 0, 'started_at': NOW, 'ended_at': None}
        self.next_battle_id += 1
        self.battles[wb['id']] = wb
        return dict(wb)

    async def list_active_war_battles(self, war_id):
        return [dict(wb) for wb in sorted(self.battles.values(),
                                          key=lambda b: b['id'])
                if wb['war_id'] == int(war_id) and wb['state'] == 0]


def _mk_at(store, war_id, at_id, faction, bf):
    at = {'id': at_id, 'war_id': war_id, 'owner_player_id': 100 + at_id,
          'faction': faction, 'template_id': 0, 'name': f'AT{at_id}',
          'abbreviation': 'AT', 'rank': 1, 'xp': 0,
          'resources': {}, 'morale': 100, 'max_morale': 100,
          'state': 0, 'map_entity_id': bf, 'map_entity_type': 1,
          'general_character_id': 0, 'is_deployed': True,
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
    check('到达落位城市 30 type=1',
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

    async def _fake_broadcast(events):
        broadcast_calls.append(events)

    _orig_bcast = war_campaign.broadcast_war_events
    war_campaign.broadcast_war_events = _fake_broadcast
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
        war_campaign.broadcast_war_events = _orig_bcast

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

    fails = [l for l, ok in RESULTS if not ok]
    print(f'\n{"="*50}\nwar_movement 回归：{len(RESULTS)-len(fails)}/{len(RESULTS)} PASS')
    if fails:
        for l in fails:
            print(f'  FAIL: {l}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
