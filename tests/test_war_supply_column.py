# -*- coding: utf-8 -*-
"""RTS 补给队（Reserve，官服 §10.2 #6）回归，DB-free。

覆盖：
  A. 单元：is_reserve（template%100==4）/ reserve_template_id /
     bf_in_faction_supply（归属+沿补给线连通首都；owners 空保守在网）/
     阵营首都映射
  B. spawn_supply_column：创建字段（template faction*100+4 / parent 链接 /
     载量）+ 事件行（Definition/Instance/Morale/armyresource + instance f3
     指向父 AT）+ 寻路移动；near_capital_bf 己方在网城生效 / 敌城·孤岛
     己方城拒绝回退首都；同城直接汇合；地面不可达拒单；无领地
     回阵营首都
  C. advance_movements 到达：汇合（资源按编制封顶并入 + 软删 + 删除行 +
     途经不占城不成战）；父 AT 挪窝 → 追击再汇合；父 AT 亡 → 就地解散
  D. _cleanup_supply_columns：孤儿滞留补给队解散 / 父在不动
  E. _apply_resupply / resupply_request 补给队分支：mode=1 网络外出队
     （不动库存不入队 / 扣款照常 / AT 编制不动）；在网即时（旧行为）；
     mode=0 全关；mode=2 一律出队（同城即合）；在途载量扣减缺编（重复
     补给 103 / 部分在途折半）；首次免费置位；f6 nearCapitalBattlefieldId
     透传（在网城生效 / 敌城回退首都）；孤岛己方城出队；连通非首都城即时

写法沿用 test_war_movement.py / test_war_at_supply.py：monkeypatch wardb
合成拓扑 + FakeStore + importlib 加载 war_at，运行
`.venv/bin/python tests/test_war_supply_column.py`（pytest 收集不执行 main）。

合成拓扑：城市 10(0,0) -sl101- 20(300,400) -sl102- 30(600,800)；城市
40(900,1200) 无补给线（不可达）。首都 {1: bf10, 2: bf30}。
模板：101/201 步兵 AT（编制 {1:12}）；104/204 补给队（speed 1.0）。
"""
import asyncio
import copy
import datetime
import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol import Chunk, pb_int32, pb_int64, pb_parse
import mm_config
import wardb
import war_campaign

RESULTS = []


def check(label, ok, detail=''):
    RESULTS.append((label, ok))
    print(f'[{"PASS" if ok else "FAIL"}] {label}{("  " + detail) if detail else ""}')


# ------------------------------------------------------- wardb 合成（加载前）

_NOW = datetime.datetime(2026, 9, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)

_TPLS = {
    101: {'id': 101, 'name': 'Infantry AT', 'faction': 1,
          'resources': {1: 12}, 'vehicle_cats': set(), 'size': 2,
          'general_character_template_id': 0, 'level': 1, 'speed': 1.0},
    201: {'id': 201, 'name': 'Infantry AT GE', 'faction': 2,
          'resources': {1: 12}, 'vehicle_cats': set(), 'size': 2,
          'general_character_template_id': 0, 'level': 1, 'speed': 1.0},
    104: {'id': 104, 'name': 'reserve', 'faction': 1,
          'resources': {}, 'vehicle_cats': set(), 'size': 0,
          'general_character_template_id': 0, 'level': 0, 'speed': 1.0},
    204: {'id': 204, 'name': 'reserve', 'faction': 2,
          'resources': {}, 'vehicle_cats': set(), 'size': 0,
          'general_character_template_id': 0, 'level': 0, 'speed': 1.0},
}

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
wardb.capitals = lambda: [{'battlefield_id': 10, 'default_faction': 1},
                          {'battlefield_id': 30, 'default_faction': 2}]
wardb.commandnode_templates = lambda: {k: dict(v) for k, v in _TPLS.items()}
wardb.morale_template = lambda: None
wardb.armyresource_production = lambda: []
war_campaign._ADJACENCY_CACHE = None
war_campaign._BF_POS_CACHE = None

mm_config.WAR_AUTO_CREATE = True

SCALE = (mm_config.WAR_TIMESCALE or 1.0) * (mm_config.WAR_AT_MOVE_SCALE or 1.0)
SEG_SEC = 500.0 / SCALE           # 10-20 与 20-30 距离均 = hypot(300,400)

# ---- 加载 war_at（绕开 handlers/__init__ 全量导入）----
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    'war_at_column_under_test', os.path.join(_ROOT, 'handlers', 'war_at.py'))
war_at = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(war_at)

war_at.commandnode_templates = wardb.commandnode_templates
war_at._PRICING_MAP = {
    9001: {'type': 'reinforceSoldier', 'warfunds': 100, 'credits': 1000,
           'gold': 10},
    9002: {'type': 'reinforceVehicle', 'warfunds': 250, 'credits': 2500,
           'gold': 25},
}
war_at._char_command_points = lambda sess, ch: 0
war_at._has_command_points = lambda sess, pid, faction: True


# ------------------------------------------------------------- FakeStore

class FakeStore:
    """war_campaign 移动链 + war_at 增援链最小存储接口。"""

    def __init__(self, balances=None):
        self.pool = object()
        self.wars = {}
        self.next_war_id = 1
        self.city_owners = {}
        self.ats = {}
        self.next_at_id = 1
        self.movements = {}
        self.next_mv_id = 1
        self.battles = {}
        self.next_battle_id = 1
        self.characters = {}
        self.balances = dict(balances or {})
        self.spend_log = []
        self.stock = {}
        self.plentiful = True
        self.adjust_log = []
        self.supply_queue = []

    # ---- war ----
    async def get_active_war(self):
        for w in self.wars.values():
            if w['state'] == 1:
                return dict(w)
        return None

    async def create_war(self, name, points_needed_for_victory=15, mapid=1,
                         config=None):
        wid = self.next_war_id
        self.next_war_id += 1
        w = {'id': wid, 'name': name, 'begintime': _NOW, 'endtime': None,
             'winning_faction_id': 0,
             'points_needed_for_victory': points_needed_for_victory,
             'sequel_war_id': 0, 'mapid': mapid, 'state': 1,
             'config': config or {}}
        self.wars[wid] = w
        return dict(w)

    async def init_war_cities(self, war_id, owner_map):
        self.city_owners[war_id] = dict(owner_map)

    async def init_war_faction_resources(self, war_id, initial):
        for fac, cats in (initial or {}).items():
            for cat, cnt in cats.items():
                self.stock.setdefault((int(war_id), int(fac), int(cat)),
                                      int(cnt))

    async def get_war_cities(self, war_id):
        return dict(self.city_owners.get(int(war_id), {}))

    async def set_city_owner(self, war_id, battlefield_id, owner_faction):
        self.city_owners.setdefault(int(war_id), {})[int(battlefield_id)] = \
            int(owner_faction)

    async def create_mission_record(self, preset, **kw):
        return 500

    async def update_war_production(self, war_id, production_ms):
        return True

    async def list_wars(self, limit=20):
        return [dict(w) for w in
                sorted(self.wars.values(), key=lambda w: -w['id'])[:limit]]

    # ---- AT ----
    async def get_command_node(self, node_id):
        a = self.ats.get(int(node_id or 0))
        return copy.deepcopy(a) if a else None

    async def list_war_command_nodes(self, war_id):
        return [copy.deepcopy(a) for a in self.ats.values()
                if a['war_id'] == war_id]

    async def list_player_command_nodes(self, pid, war_id):
        return [copy.deepcopy(a) for a in self.ats.values()
                if a['war_id'] == war_id and a['owner_player_id'] == pid]

    async def update_command_node(self, node_id, **fields):
        self.ats[int(node_id)].update(fields)
        return True

    async def create_command_node(self, war_id, template_id, owner_pid,
                                  faction, name, resources=None,
                                  abbreviation='', rank=1,
                                  parent_command_node_id=0):
        nid = self.next_at_id
        self.next_at_id += 1
        self.ats[nid] = {
            'id': nid, 'war_id': war_id, 'template_id': template_id,
            'owner_player_id': owner_pid, 'faction': faction, 'name': name,
            'abbreviation': abbreviation, 'rank': rank, 'state': 0,
            'is_deployed': False, 'map_entity_id': 0, 'map_entity_type': 0,
            'resources': dict(resources or {}),
            'has_gotten_soldier_free': False,
            'has_gotten_vehicle_free': False,
            'parent_command_node_id': int(parent_command_node_id or 0),
            'general_character_id': 0, 'morale': 100, 'max_morale': 100,
            'created_at': _NOW,
        }
        return copy.deepcopy(self.ats[nid])

    # ---- movement ----
    async def create_movement(self, commandnode_id, war_id, path,
                              started_at=None, arrive_at=None, dest_ap=0):
        mv = {'id': self.next_mv_id, 'commandnode_id': int(commandnode_id),
              'war_id': int(war_id), 'path': [dict(s) for s in (path or [])],
              'started_at': started_at, 'arrive_at': arrive_at,
              'state': 0, 'dest_ap': int(dest_ap or 0)}
        self.next_mv_id += 1
        self.movements[mv['id']] = mv
        return copy.deepcopy(mv)

    async def get_active_movement(self, commandnode_id):
        for mv in sorted(self.movements.values(), key=lambda m: -m['id']):
            if mv['commandnode_id'] == int(commandnode_id) and mv['state'] == 0:
                return copy.deepcopy(mv)
        return None

    async def list_active_movements(self, war_id):
        return [copy.deepcopy(mv) for mv in
                sorted(self.movements.values(), key=lambda m: m['id'])
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
              'state': 0, 'started_at': _NOW, 'ended_at': None}
        self.next_battle_id += 1
        self.battles[wb['id']] = wb
        return copy.deepcopy(wb)

    async def list_active_war_battles(self, war_id):
        return [copy.deepcopy(wb) for wb in
                sorted(self.battles.values(), key=lambda b: b['id'])
                if wb['war_id'] == int(war_id) and wb['state'] == 0]

    # ---- 玩家 / 阵营库存 ----
    async def list_characters(self, pid):
        return [dict(c) for c in self.characters.get(pid, [])]

    async def get_player_faction(self, pid):
        return 1

    async def spend_currency(self, pid, key, amount, conn=None):
        bal = self.balances.get((int(pid), key), 3000)
        if bal < amount:
            return False, bal
        bal -= amount
        self.balances[(int(pid), key)] = bal
        self.spend_log.append((int(pid), key, amount))
        return True, bal

    def balance(self, pid, key='warfunds'):
        return self.balances.get((int(pid), key), 3000)

    async def list_war_faction_resources(self, war_id, faction=None):
        out = []
        keys = sorted(k for k in self.stock
                      if k[0] == int(war_id)
                      and (faction is None or k[1] == int(faction)))
        for wid, fac, cat in keys:
            out.append({'war_id': wid, 'faction': fac, 'category_id': cat,
                        'count': self.stock[(wid, fac, cat)]})
        if self.plentiful:
            seen = {k for k in self.stock
                    if k[0] == int(war_id)
                    and (faction is None or k[1] == int(faction))}
            facs = (int(faction),) if faction is not None else (1, 2, 3)
            for fac in facs:
                for cat in (1,):
                    if (int(war_id), fac, cat) not in seen:
                        out.append({'war_id': int(war_id), 'faction': fac,
                                    'category_id': cat, 'count': 10 ** 9})
        return out

    async def adjust_war_faction_resources(self, war_id, faction, deltas):
        for cat, d in deltas.items():
            key = (int(war_id), int(faction), int(cat))
            base = self.stock.get(key, 10 ** 9 if self.plentiful else 0)
            self.stock[key] = max(base + int(d), 0)
            self.adjust_log.append((key[0], key[1], key[2], int(d)))
        return await self.list_war_faction_resources(war_id, faction)

    async def enqueue_war_supply(self, war_id, faction, command_node_id,
                                 items):
        n = 0
        for cat, cnt in (items or {}).items():
            if int(cnt) <= 0:
                continue
            self.supply_queue.append({
                'id': len(self.supply_queue) + 1,
                'war_id': int(war_id), 'faction': int(faction),
                'command_node_id': int(command_node_id),
                'category_id': int(cat), 'count': int(cnt),
                'queue_number': len(self.supply_queue) + 1})
            n += 1
        return n

    async def list_war_supply_queue(self, war_id, faction=None,
                                    command_node_id=None):
        return [dict(r) for r in self.supply_queue
                if r['war_id'] == int(war_id)
                and (faction is None or r['faction'] == int(faction))
                and (command_node_id is None
                     or r['command_node_id'] == int(command_node_id))]

    async def dequeue_war_supply(self, row_ids):
        ids = {int(i) for i in (row_ids or [])}
        self.supply_queue = [r for r in self.supply_queue
                             if r['id'] not in ids]
        return len(ids)


def _mk_at(st, node_id, template=101, owner=100, faction=1, state=0,
           resources=None, sold_free=False, veh_free=False,
           deployed=False, map_entity_id=0, parent=0):
    st.ats[node_id] = {
        'id': node_id, 'war_id': 1, 'template_id': template,
        'owner_player_id': owner, 'faction': faction, 'name': 'Alpha',
        'abbreviation': 'ALT', 'rank': 1, 'state': state,
        'is_deployed': deployed, 'map_entity_id': map_entity_id,
        'map_entity_type': 1 if deployed else 0,
        'resources': dict(resources) if resources is not None else {},
        'has_gotten_soldier_free': sold_free,
        'has_gotten_vehicle_free': veh_free,
        'parent_command_node_id': parent,
        'general_character_id': 0, 'morale': 100, 'max_morale': 100,
        'created_at': _NOW,
    }
    return st.ats[node_id]


async def _war(st, owners):
    """开战 + 覆写城市归属（确保测试自控网络内外判定）。"""
    war = await war_campaign.ensure_war(st)
    await st.init_war_cities(war['id'], owners)
    return war


async def _ewar(owners):
    """E 节公共开战：FakeStore + 覆写归属 + 挂 war_at.STORE。"""
    st = FakeStore()
    await _war(st, owners)
    war_at.STORE = st
    return st


def _cols(st, parent_id):
    return [a for a in st.ats.values()
            if a['parent_command_node_id'] == parent_id
            and war_campaign.is_reserve(a)]


class FakeSess:
    warmap_view_cmd = 777
    commandnode_view_cmd = None

    async def ensure_player(self):
        return 100


class FakeWriter:
    def __init__(self):
        self.buf = bytearray()

    def write(self, data):
        self.buf.extend(data)

    async def drain(self):
        pass


def _chunks(buf):
    out, off = [], 0
    while off < len(buf):
        c, off = Chunk.parse(bytes(buf), off)
        if c is None:
            break
        out.append(c)
    return out


def _resp(w, title):
    want = title.encode()
    for c in reversed(_chunks(w.buf)):
        if c.subs and c.subs[0].data == want:
            return pb_parse(c.subs[0].subs[0].data)
    raise AssertionError(f'no response {title}')


def _events_tags(events):
    return {(k, tag) for k, tag, _ in events}


async def _resupply(node, sp=9001, vp=9002, cur=2, near_capital=0):
    w = FakeWriter()
    req = (pb_int64(1, node) + pb_int64(2, sp) + pb_int64(4, vp)
           + pb_int32(8, cur))
    if near_capital:
        req += pb_int64(6, near_capital)
    await war_at.handle_resupply_request(
        FakeSess(), w, 900, b'resupply_request', req)
    return w, _resp(w, 'resupply_request_response')


# ============================================================= A 单元

def section_a():
    check('A1 is_reserve（104/204/304 真，101/201/0 假）',
          war_campaign.is_reserve({'template_id': 104})
          and war_campaign.is_reserve({'template_id': 204})
          and war_campaign.is_reserve({'template_id': 304})
          and not war_campaign.is_reserve({'template_id': 101})
          and not war_campaign.is_reserve({'template_id': 201})
          and not war_campaign.is_reserve({'template_id': 0})
          and not war_campaign.is_reserve({}))
    check('A2 reserve_template_id（faction*100+4）',
          war_campaign.reserve_template_id(1) == 104
          and war_campaign.reserve_template_id(2) == 204
          and war_campaign.reserve_template_id(3) == 304)
    # 拓扑 10-sl101-20-sl102-30、40 孤立；首都 fac1=10 / fac2=30
    check('A3 bf_in_faction_supply（归属+连通首都；owners 空保守在网）',
          war_campaign.bf_in_faction_supply({}, 1, 20)
          and war_campaign.bf_in_faction_supply({10: 1}, 1, 10)
          and not war_campaign.bf_in_faction_supply({10: 1}, 2, 10)
          and not war_campaign.bf_in_faction_supply({10: 1}, 1, 20)
          and war_campaign.bf_in_faction_supply({10: 1, 20: 1}, 1, 20)
          and not war_campaign.bf_in_faction_supply({10: 1, 20: 2}, 1, 20)
          and not war_campaign.bf_in_faction_supply({10: 1, 30: 2}, 1, 30))
    check('A3b 孤岛己方城不在网（断链孤城官方走补给队）',
          not war_campaign.bf_in_faction_supply({10: 1, 40: 1}, 1, 40))
    check('A3b2 多跳连通在网 / 首都即本城',
          war_campaign.bf_in_faction_supply({10: 1, 20: 1, 30: 1}, 1, 30)
          and war_campaign.bf_in_faction_supply({10: 1}, 1, 10))
    check('A4 阵营首都映射（补给队出生点）',
          war_campaign._faction_capital_bf().get(1) == 10
          and war_campaign._faction_capital_bf().get(2) == 30)


# ====================================================== B spawn_supply_column

async def section_b():
    # B1 网络外生成：父 AT 驻 30（敌领地）→ 最近己方 10 出生机动
    st = FakeStore()
    war = await _war(st, {10: 1, 30: 2})
    parent = _mk_at(st, 11, deployed=True, map_entity_id=30,
                    resources={'1': 2})
    col, events, ecode = await war_campaign.spawn_supply_column(
        st, war, parent, {1: 10}, now=_NOW)
    check('B1 生成 RC=0 + 模板 104 + parent 链接 + 载量',
          ecode == 0 and col and col['template_id'] == 104
          and col['faction'] == 1
          and col['owner_player_id'] == parent['owner_player_id']
          and col['parent_command_node_id'] == 11
          and st.ats[col['id']]['resources'] == {'1': 10})
    check('B1 出生落位 10 + 置在途（首段 sl101）',
          col['is_deployed'] is True
          and st.ats[col['id']]['state'] == 1
          and st.ats[col['id']]['map_entity_id'] == 101)
    mv = await st.get_active_movement(col['id'])
    check('B1 寻路 10→30 两段（sl101,sl102）',
          mv is not None and [s['sl_id'] for s in mv['path']] == [101, 102]
          and mv['path'][0]['from_bf'] == 10
          and mv['path'][-1]['to_bf'] == 30)
    tags = _events_tags(events)
    check('B1 事件行（definition/instance/morale/armyresource/transport）',
          ('set', b'CommandNodeDefinition') in tags
          and ('set', b'CommandNodeWarInstance') in tags
          and ('set', b'CommandNodeMorale') in tags
          and ('set', b'armyresource') in tags
          and ('set', b'transport_commandnode_segment') in tags
          and ('set', b'transport_commandnode_destination') in tags,
          f'{sorted(tags)}')
    inst = pb_parse([p for k, tag, p in events
                     if tag == b'CommandNodeWarInstance'][-1])
    res = pb_parse(next(p for k, tag, p in events if tag == b'armyresource'))
    check('B1 instance f3=父 AT（官服 OnTheWay 链）/ armyresource 行',
          inst.get(3) == 11 and int(inst.get(5)) == 101
          and res.get(1) == col['id'] * 1000 + 1 and res.get(4) == 10,
          f'f3={inst.get(3)}')
    check('B1 普通 AT instance f3=0（无 parent）',
          pb_parse(war_campaign.msg_commandnode_instance_row(
              parent, war['id'], war_campaign._faction_capital_bf()))
          .get(3) == 0)

    # B2 near_capital_bf：己方在网城生效；敌城/孤岛己方城拒绝回退首都
    st2 = FakeStore()
    war2 = await _war(st2, {10: 1, 20: 1, 30: 2})
    p2 = _mk_at(st2, 21, deployed=True, map_entity_id=30)
    col2, _e2, rc2 = await war_campaign.spawn_supply_column(
        st2, war2, p2, {1: 5}, near_capital_bf=20, now=_NOW)  # 20 在网
    mv2 = await st2.get_active_movement(col2['id'])
    check('B2 near_capital 己方在网城生效（出生 20）',
          rc2 == 0 and mv2 and mv2['path'][0]['from_bf'] == 20)
    col3, _e3, rc3 = await war_campaign.spawn_supply_column(
        st2, war2, p2, {1: 5}, near_capital_bf=30, now=_NOW)  # 30 敌领地
    mv3 = await st2.get_active_movement(col3['id'])
    check('B2 near_capital 敌领地拒绝（回退阵营首都 10）',
          rc3 == 0 and mv3 and mv3['path'][0]['from_bf'] == 10)
    col3b, _e3b, rc3b = await war_campaign.spawn_supply_column(
        st2, war2, p2, {1: 5}, near_capital_bf=40, now=_NOW)  # 40 孤岛己方
    mv3b = await st2.get_active_movement(col3b['id'])
    check('B2 near_capital 孤岛己方城拒绝（不在网 → 回退首都 10）',
          rc3b == 0 and mv3b and mv3b['path'][0]['from_bf'] == 10)

    # B3 同城（出生点=父 AT 位置）→ 直接汇合不出队
    st3 = FakeStore()
    war3 = await _war(st3, {10: 1, 20: 2})
    p3 = _mk_at(st3, 31, deployed=True, map_entity_id=10,
                resources={'1': 2})
    col4, ev4, rc4 = await war_campaign.spawn_supply_column(
        st3, war3, p3, {1: 10}, now=_NOW)
    check('B3 同城直接汇合（col 软删/父补编 12/无移动）',
          rc4 == 0 and st3.ats[col4['id']]['state'] == 3
          and st3.ats[31]['resources'] == {'1': 12}
          and await st3.get_active_movement(col4['id']) is None
          and ('del', b'CommandNodeWarInstance') in _events_tags(ev4))

    # B4 AT 驻孤岛城（网外）→ 首都出生地面不可达 → 拒单（不落库）
    st4 = FakeStore()
    war4 = await _war(st4, {10: 1, 40: 1})
    p4 = _mk_at(st4, 41, deployed=True, map_entity_id=40,
                resources={'1': 0})
    col5, _e5, rc5 = await war_campaign.spawn_supply_column(
        st4, war4, p4, {1: 12}, now=_NOW)
    check('B4 不可达拒单（col 未创建/父不变）',
          col5 is None
          and rc5 == war_campaign.RC_CANNOT_FIND_COMMANDNODE
          and st4.ats[41]['resources'] == {'1': 0})

    # B5 无己方领地（owners 空）→ 阵营首都出生
    st5 = FakeStore()
    war5 = await _war(st5, {})
    p5 = _mk_at(st5, 51, deployed=True, map_entity_id=20)
    col6, _e6, rc6 = await war_campaign.spawn_supply_column(
        st5, war5, p5, {1: 5}, now=_NOW)
    mv6 = await st5.get_active_movement(col6['id'])
    check('B5 无领地回阵营首都（faction1 → bf10 出生）',
          rc6 == 0 and mv6 and mv6['path'][0]['from_bf'] == 10)


# ================================================== C advance_movements 到达

async def section_c():
    # C1 到达汇合：父 AT 驻 30（载量并入封顶）+ 途经不占城不成战
    st = FakeStore()
    war = await _war(st, {10: 1, 30: 2})
    parent = _mk_at(st, 11, deployed=True, map_entity_id=30,
                    resources={'1': 2})
    _enemy = _mk_at(st, 12, template=201, owner=200, faction=2,
                    deployed=True, map_entity_id=30)     # 敌 AT 驻 30
    col, _e, _rc = await war_campaign.spawn_supply_column(
        st, war, parent, {1: 10}, now=_NOW)
    ev = await war_campaign.advance_movements(
        st, war, now=_NOW + datetime.timedelta(seconds=2 * SEG_SEC + 5))
    tags = _events_tags(ev)
    check('C1 汇合资源封顶并入（2+10 → 12）+ col 软删',
          st.ats[11]['resources'] == {'1': 12}
          and st.ats[col['id']]['state'] == 3
          and await st.get_active_movement(col['id']) is None)
    check('C1 删除行（instance/definition/morale/armyresource/transport）',
          ('del', b'CommandNodeWarInstance') in tags
          and ('del', b'CommandNodeDefinition') in tags
          and ('del', b'CommandNodeMorale') in tags
          and ('del', b'armyresource') in tags
          and ('del', b'transport_commandnode_segment') in tags,
          f'{sorted(tags)}')
    del_ids = [rid for k, tag, rid in ev
               if k == 'del' and tag == b'armyresource']
    parent_res = [pb_parse(p) for k, tag, p in ev
                  if k == 'set' and tag == b'armyresource']
    check('C1 删除 armyresource id=col*1000+cat / 父行推送 cnt=12',
          del_ids == [col['id'] * 1000 + 1]
          and any(r.get(1) == 11001 and r.get(4) == 12
                  for r in parent_res), f'{del_ids}')
    check('C1 不占城不成战（30 仍 faction2 / 敌我 state 不变）',
          st.city_owners[war['id']].get(30) == 2
          and st.ats[12]['state'] == 0 and st.ats[11]['state'] == 0
          and not st.battles)

    # C2 父 AT 挪窝 → 到达后追击再汇合
    st2 = FakeStore()
    war2 = await _war(st2, {10: 1, 30: 2})
    p2 = _mk_at(st2, 21, deployed=True, map_entity_id=30,
                resources={'1': 2})
    col2, _e2, _rc2 = await war_campaign.spawn_supply_column(
        st2, war2, p2, {1: 10}, now=_NOW)
    # 父 AT 撤回 10（override_move 立即到达）
    await war_campaign.start_movement(st2, p2, 10, override_move=True,
                                      now=_NOW)
    await war_campaign.advance_movements(
        st2, war2, now=_NOW + datetime.timedelta(seconds=1))
    check('C2 前置：父 AT 已落位 10', st2.ats[21]['map_entity_id'] == 10
          and st2.ats[21]['state'] == 0)
    ev2 = await war_campaign.advance_movements(
        st2, war2,
        now=_NOW + datetime.timedelta(seconds=2 * SEG_SEC + 5))
    mv2 = await st2.get_active_movement(col2['id'])
    check('C2 追击（col 到 30 发现父在 10 → 新移动 30→10）',
          st2.ats[col2['id']]['state'] == 1
          and mv2 is not None
          and mv2['path'][0]['from_bf'] == 30
          and mv2['path'][-1]['to_bf'] == 10
          and any(tag == b'transport_commandnode_segment'
                  for _, tag, _ in ev2))
    ev3 = await war_campaign.advance_movements(
        st2, war2,
        now=_NOW + datetime.timedelta(seconds=4 * SEG_SEC + 10))
    check('C2 追上汇合（父满编/col 软删/移动关闭）',
          st2.ats[21]['resources'] == {'1': 12}
          and st2.ats[col2['id']]['state'] == 3
          and not await st2.get_active_movement(col2['id'])
          and ('del', b'armyresource') in _events_tags(ev3))

    # C3 父 AT 亡 → 到达就地解散（资源损失）
    st3 = FakeStore()
    war3 = await _war(st3, {10: 1, 30: 2})
    p3 = _mk_at(st3, 31, deployed=True, map_entity_id=30,
                resources={'1': 2})
    col3, _e3, _rc3 = await war_campaign.spawn_supply_column(
        st3, war3, p3, {1: 10}, now=_NOW)
    st3.ats[31]['state'] = 3                      # 父 AT 歼灭
    ev4 = await war_campaign.advance_movements(
        st3, war3,
        now=_NOW + datetime.timedelta(seconds=2 * SEG_SEC + 5))
    check('C3 父亡就地解散（col 软删/父资源不动/无战局）',
          st3.ats[col3['id']]['state'] == 3
          and st3.ats[31]['resources'] == {'1': 2}
          and not st3.battles
          and ('del', b'CommandNodeWarInstance') in _events_tags(ev4))

    # C4 途经不占城：补给队过 20（中立）不改变归属
    st4 = FakeStore()
    war4 = await _war(st4, {10: 1, 30: 2})
    p4 = _mk_at(st4, 41, deployed=True, map_entity_id=30)
    col4, _e4, _rc4 = await war_campaign.spawn_supply_column(
        st4, war4, p4, {1: 5}, now=_NOW)
    ev5 = await war_campaign.advance_movements(
        st4, war4,
        now=_NOW + datetime.timedelta(seconds=SEG_SEC * 1.5))
    check('C4 途经城市 20 不占领（归属保持中立）',
          20 not in st4.city_owners[war4['id']]
          and st4.ats[col4['id']]['map_entity_id'] == 102
          and any(tag == b'transport_commandnode_segment'
                  for _, tag, _ in ev5))


# ================================================ D _cleanup_supply_columns

async def section_d():
    st = FakeStore()
    war = await _war(st, {10: 1, 30: 2})
    parent = _mk_at(st, 11, deployed=True, map_entity_id=30)
    _mk_at(st, 12, template=104, owner=100, faction=1,
           resources={'1': 4}, parent=11)               # 孤儿（父在→保留）
    _mk_at(st, 13, template=104, owner=100, faction=1,
           resources={'1': 4}, parent=11, state=3)      # 已删（不动）
    st.ats[11]['state'] = 3                             # 父亡
    _mk_at(st, 14, template=104, owner=100, faction=1,
           resources={'1': 4}, parent=99)               # 孤儿（父缺）
    _mk_at(st, 15, template=101, owner=100, faction=1,
           parent=99)                                   # 非 reserve（不动）
    ev = await war_campaign._cleanup_supply_columns(st)
    check('D 孤儿解散（12/14 软删）+ 父在/已删/非队不动',
          st.ats[12]['state'] == 3 and st.ats[14]['state'] == 3
          and st.ats[13]['state'] == 3 and st.ats[15]['state'] == 0
          and [rid for k, tag, rid in ev
               if tag == b'CommandNodeWarInstance'] == [12, 14])


# ================================================= E resupply 补给队分支

async def section_e():
    # E1 mode=1 网络外：生成补给队（不动库存/不入队/扣款照常/编制不动）
    st = await _ewar({10: 1, 30: 2})                     # 20 中立 = 网络外
    _mk_at(st, 500, deployed=True, map_entity_id=20,
           resources={'1': 2}, sold_free=True)
    w, f = await _resupply(500)
    cols = _cols(st, 500)
    check('E1 网络外出队（响应 1/扣 1000=10×100/编制不动）',
          f.get(2) == 1 and len(cols) == 1
          and st.spend_log == [(100, 'warfunds', 1000)]
          and st.ats[500]['resources'] == {'1': 2}
          and cols[0]['resources'] == {'1': 10}, str(f))
    mv = await st.get_active_movement(cols[0]['id'])
    check('E1 队列从最近己方城 10 出发 + 库存/226 队列均不动',
          mv is not None and mv['path'][0]['from_bf'] == 10
          and not st.adjust_log and not st.supply_queue
          and st.ats[cols[0]['id']]['parent_command_node_id'] == 500)

    # E2 在途扣减：全额在途 → 103（客户端 getNumberOfInfantryMissing 口径）
    w, f = await _resupply(500)
    check('E2 全额在途 → 103（不再重复购买）',
          f.get(2) == war_campaign.RC_NO_RESUPPLY_ALREADY_FULL
          and len(_cols(st, 500)) == 1
          and len(st.spend_log) == 1, str(f))

    # E2b 部分在途：已载 7/缺 12 → 只买 5
    st2 = await _ewar({10: 1, 30: 2})
    _mk_at(st2, 501, deployed=True, map_entity_id=20, resources={},
           sold_free=True)
    _mk_at(st2, 900, template=104, resources={'1': 4}, parent=501,
           state=1)
    _mk_at(st2, 901, template=104, resources={'1': 3}, parent=501,
           state=1)
    w, f = await _resupply(501)
    new_cols = [c for c in _cols(st2, 501) if c['id'] not in (900, 901)]
    check('E2b 部分在途（4+3）→ 只补差 5',
          f.get(2) == 1 and len(new_cols) == 1
          and new_cols[0]['resources'] == {'1': 5}
          and st2.spend_log == [(100, 'warfunds', 500)], str(f))

    # E3 在网（己方城市）→ 旧行为即时交付（动库存）
    st3 = await _ewar({10: 1, 30: 2})
    _mk_at(st3, 500, deployed=True, map_entity_id=10,
           resources={'1': 2}, sold_free=True)
    w, f = await _resupply(500)
    check('E3 在网即时（满编 {1:12}/库存扣减/无补给队）',
          f.get(2) == 1 and st3.ats[500]['resources'] == {'1': 12}
          and not _cols(st3, 500)
          and st3.adjust_log == [(1, 1, 1, -10)], str(f))

    # E4 mode=0 → 一律旧行为（网络外也即时）
    _mode_bak = mm_config.WAR_SUPPLY_COLUMN_MODE
    try:
        mm_config.WAR_SUPPLY_COLUMN_MODE = 0
        st4 = await _ewar({10: 1, 30: 2})
        _mk_at(st4, 500, deployed=True, map_entity_id=20,
               resources={'1': 2}, sold_free=True)
        w, f = await _resupply(500)
        check('E4 mode=0 网络外也即时（无补给队）',
              f.get(2) == 1 and st4.ats[500]['resources'] == {'1': 12}
              and not _cols(st4, 500))

        # E5 mode=2 → 一律出队（同城出生 → 直接汇合满编）
        mm_config.WAR_SUPPLY_COLUMN_MODE = 2
        st5 = await _ewar({10: 1, 30: 2})
        _mk_at(st5, 500, deployed=True, map_entity_id=10,
               resources={'1': 2}, sold_free=True)
        w, f = await _resupply(500)
        cols5 = _cols(st5, 500)
        check('E5 mode=2 在网也出队（同城即合满编/col 软删/不动库存）',
              f.get(2) == 1 and st5.ats[500]['resources'] == {'1': 12}
              and len(cols5) == 1 and cols5[0]['state'] == 3
              and not st5.adjust_log and not st5.supply_queue)
    finally:
        mm_config.WAR_SUPPLY_COLUMN_MODE = _mode_bak

    # E6 首次免费：网络外首补免单 + 置位
    st6 = await _ewar({10: 1, 30: 2})
    _mk_at(st6, 500, deployed=True, map_entity_id=20, resources={})
    w, f = await _resupply(500)
    check('E6 首次免费（免单/置位/出队载 12）',
          f.get(2) == 1 and not st6.spend_log
          and st6.ats[500]['has_gotten_soldier_free'] is True
          and _cols(st6, 500)[0]['resources'] == {'1': 12})

    # E7 f6 nearCapitalBattlefieldId 透传（在网城生效/敌城回退首都）
    st7 = await _ewar({10: 1, 20: 1, 30: 2})
    _mk_at(st7, 500, deployed=True, map_entity_id=30,
           resources={'1': 2}, sold_free=True)
    w, f = await _resupply(500, near_capital=20)         # 20 在网 → 出生 20
    cols7 = [c for c in _cols(st7, 500) if c['state'] < 3]
    mv7 = await st7.get_active_movement(cols7[0]['id'])
    check('E7 f6=20 己方在网城 → 出生 20',
          f.get(2) == 1 and mv7 and mv7['path'][0]['from_bf'] == 20,
          str(f))
    _mk_at(st7, 501, deployed=True, map_entity_id=30,
           resources={'1': 2}, sold_free=True)
    w, f = await _resupply(501, near_capital=30)         # 30 敌领地 → 忽略
    cols8 = [c for c in _cols(st7, 501) if c['state'] < 3]
    mv8 = await st7.get_active_movement(cols8[-1]['id'])
    check('E7 f6=30 敌领地 → 忽略回退首都 10',
          f.get(2) == 1 and mv8 and mv8['path'][0]['from_bf'] == 10,
          str(f))

    # E8 会战/移动状态门（101/116）不因补给队分支变化
    st8 = await _ewar({10: 1, 30: 2})
    _mk_at(st8, 500, deployed=True, map_entity_id=20, state=2)
    w, f = await _resupply(500)
    check('E8 会战中 → 101（不出队）',
          f.get(2) == war_campaign.RC_NO_RESUPPLY_IN_BATTLE
          and not _cols(st8, 500), str(f))

    # E9 孤岛己方城（归属己方但断链）→ 网络外判定成立但地面不可达 →
    # 扣款前拒单（RC_CANNOT_FIND_COMMANDNODE，不出队不动库存）
    st9 = await _ewar({10: 1, 40: 1})
    _mk_at(st9, 500, deployed=True, map_entity_id=40,
           resources={'1': 2}, sold_free=True)
    w, f = await _resupply(500)
    check('E9 孤岛不可达 → 扣款前拒单（无 col/不扣钱/不走库存）',
          f.get(2) == war_campaign.RC_CANNOT_FIND_COMMANDNODE
          and not _cols(st9, 500)
          and st9.ats[500]['resources'] == {'1': 2}
          and not st9.spend_log
          and not st9.adjust_log and not st9.supply_queue, str(f))

    # E10 连通己方非首都城（用户 AT002 场景）→ 在网即时
    st10 = await _ewar({10: 1, 20: 1, 30: 2})
    _mk_at(st10, 500, deployed=True, map_entity_id=20,
           resources={'1': 2}, sold_free=True)
    w, f = await _resupply(500)
    check('E10 连通己方非首都城 → 在网即时（无补给队/动库存）',
          f.get(2) == 1 and st10.ats[500]['resources'] == {'1': 12}
          and not _cols(st10, 500)
          and st10.adjust_log == [(1, 1, 1, -10)], str(f))


async def main():
    section_a()
    await section_b()
    await section_c()
    await section_d()
    await section_e()
    fails = [label for label, ok in RESULTS if not ok]
    print(f'\n{"="*50}\nsupply_column 回归：'
          f'{len(RESULTS) - len(fails)}/{len(RESULTS)} PASS')
    for label in fails:
        print(f'  FAIL: {label}')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
