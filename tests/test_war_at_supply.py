# -*- coding: utf-8 -*-
"""RTS AT 装备链（SetCommandNodeEquipmentRequest #1）+ 增援链
（resupply_request #2）+ 买壳/部署口径（§10.2 #4）回归，DB-free。

覆盖（docs/hng-rts-protocol.md §5.2/§8.2/§10.2）：
  A. 单元：_at_missing_resources / _unit_price / _template_matches_character /
     CommandNodeWarInstance f11/f12 真值、Definition f11 恒 False、armyresource 行
  B. 装备链：空 items → RC_FAIL(0) 原样（假成功 bug 回归）/ equip 成功 +
     Definition f9 推送 / 217 幂等 / 218 未装备即卸 / unequip / 219 阵营与
     模板不符 / 将军跨兵种 / 15 角色不存在 / 指挥点门控（unlocked-used≥size）
     / 189 非归属 / 批量 best-effort
  C. 增援链：240 币种 / 189 / 101 会战 / 116 移动 / 103 满编 / 首次免费置位
     / 二次付费 / 153 余额不足三不变 / 载具分侧免费 / 双侧付费 / 定价缺失免单
     / 响应 f2 字段位 / M3-2 库存不足 226 入队 + 部分即时交付（§10.2 #3/#5）
  F. M3-2 队列交付：_qn_window 窗口 / 队头 FIFO 交付 / 队头阻塞 /
     AT 歼灭出队不退款 / 阵营隔离
  G. AT 级队列行（§10.2 #3）：CommandNodeSupplyQueueItem/Ticket 行构建
     （REQUIRED 字段位/queuePlace 位次）/ supply_queue_table_rows 全量行 /
     push_supply_queue_rows resourceview 会话推送 / tick 交付 DELETE 行
  D. 买壳/部署冒烟：create 买空壳扣款（resources={}、generalCharacterId=0）
     / deploy 部署即补编（首次免费满编置位 / 付费扣款 / 缺库存 226 入队）/ 150 重复部署
  E. transport 失败码（族内 0=ok，失败禁回 0：未知 targetType / 不可达 → 7）
     + 升级口径（换模板不补编，resources 保持买壳/现有状态）

写法沿用 test_war_movement.py：monkeypatch wardb 表 + FakeStore + check()，
运行 `.venv/bin/python tests/test_war_at_supply.py`（pytest 收集不执行 main）。
"""
import asyncio
import copy
import datetime
import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol import (
    Chunk,
    pb_bool,
    pb_int32,
    pb_int64,
    pb_message,
    pb_parse,
    pb_string,
    )
import mm_config
import wardb
import war_campaign

RESULTS = []


def check(label, ok, detail=''):
    RESULTS.append((label, ok))
    print(f'[{"PASS" if ok else "FAIL"}] {label}{("  " + detail) if detail else ""}')


# ------------------------------------------------------- wardb 合成（加载前）

_NOW = datetime.datetime(2026, 9, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)

# 501 步兵 AT：size=2、无兵种限定、全步兵编制；502 装甲 AT：size=3、
# 限定兵种 601、cat5=士兵族（20）/cat7=载具族（8）
_TPLS = {
    501: {'id': 501, 'name': 'Infantry AT', 'faction': 1,
          'resources': {1: 12}, 'vehicle_cats': set(), 'size': 2,
          'general_character_template_id': 0, 'level': 1},
    502: {'id': 502, 'name': 'Armor AT', 'faction': 1,
          'resources': {5: 20, 7: 8}, 'vehicle_cats': {7}, 'size': 3,
          'general_character_template_id': 601, 'level': 1},
    # 503：501 的升级链（parent_template_id=501），size=4、全步兵 24
    503: {'id': 503, 'name': 'Infantry AT L2', 'faction': 1,
          'resources': {1: 24}, 'vehicle_cats': set(), 'size': 4,
          'general_character_template_id': 0, 'level': 2,
          'parent_template_id': 501},
}

wardb.accesspoints = lambda: []
wardb.supplylines = lambda: []
wardb.battlefields = lambda: []
wardb.capitals = lambda: [{'battlefield_id': 10, 'default_faction': 1}]
wardb.commandnode_templates = lambda: {k: dict(v) for k, v in _TPLS.items()}
wardb.morale_template = lambda: None
# M3 生产种子库存禁用（ensure_war 不落 war_start_value，队列用例自控库存）
wardb.armyresource_production = lambda: []
war_campaign._ADJACENCY_CACHE = None
war_campaign._BF_POS_CACHE = None

mm_config.WAR_AUTO_CREATE = True
mm_config.WAR_AT_REQUIRE_COMMAND_POINTS = True

# ---- 加载 war_at（绕开 handlers/__init__ 全量导入）----
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    'war_at_under_test', os.path.join(_ROOT, 'handlers', 'war_at.py'))
war_at = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(war_at)

import player_service  # noqa: E402  （在 war_at 之后 import 亦可，patch 均在调用前）

# ---- from-import 快照同步 + 测试替身 ----
war_at.commandnode_templates = wardb.commandnode_templates
war_at._PRICING_MAP = {
    9001: {'type': 'reinforceSoldier', 'warfunds': 100, 'credits': 1000,
           'gold': 10},
    9002: {'type': 'reinforceVehicle', 'warfunds': 250, 'credits': 2500,
           'gold': 25},
}
war_at.is_general_character = lambda t: int(t or 0) == 7

# 指挥点可控化：上限取角色 'cp' 字段（_command_points_used 保持真实现）


async def _fake_char_cp(sess, ch):
    return int(ch.get('cp') or 0)


async def _has_cp_true(sess, pid, faction):
    return True


war_at._char_command_points = _fake_char_cp
war_at._has_command_points = _has_cp_true

# create/deploy 扣款走 player_service 模块全局（与 war_at 快照是两套绑定）
player_service._PRICING_MAP = {8001: {'type': 'at', 'warfunds': 500}}


# ------------------------------------------------------------- 测试替身

class FakeStore:
    """war_at / player_service / war_campaign 最小存储接口（warfunds 默认 3000）。"""

    def __init__(self, balances=None):
        self.pool = object()
        self.wars = {}
        self.next_war_id = 1
        self.city_owners = {}
        self.ats = {}
        self.next_at_id = 1
        self.characters = {}
        self.balances = dict(balances or {})
        self.spend_log = []
        # M3-2 库存/队列：默认库存充足（10**9）→ 增援全额即时交付；
        # plentiful=False 时按 stock 实值判定（队列化用例）
        self.stock = {}
        self.plentiful = True
        self.adjust_log = []
        self.supply_queue = []
        self.dequeue_log = []

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

    async def get_war_cities(self, war_id):
        return dict(self.city_owners.get(int(war_id), {}))

    async def init_war_faction_resources(self, war_id, initial):
        """幂等种子（对齐 ON CONFLICT DO NOTHING：已有不覆盖）。"""
        for fac, cats in (initial or {}).items():
            for cat, cnt in cats.items():
                self.stock.setdefault((int(war_id), int(fac), int(cat)),
                                      int(cnt))

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

    async def create_command_node(self, war_id, template_id, owner_pid,
                                  faction, name, resources=None,
                                  abbreviation='', rank=1):
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
            'general_character_id': 0, 'morale': 100, 'max_morale': 100,
            'created_at': _NOW,
        }
        return copy.deepcopy(self.ats[nid])

    # ---- 玩家 ----
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

    # ---- M3 阵营资源库存/队列 ----
    async def list_war_faction_resources(self, war_id, faction=None):
        out = []
        keys = sorted(k for k in self.stock
                      if k[0] == int(war_id)
                      and (faction is None or k[1] == int(faction)))
        for wid, fac, cat in keys:
            out.append({'war_id': wid, 'faction': fac, 'category_id': cat,
                        'count': self.stock[(wid, fac, cat)]})
        if self.plentiful:
            # 充足模式：合成模板编制涉及的全部 cat（1/5/7）×10**9
            seen = {k for k in self.stock
                    if k[0] == int(war_id)
                    and (faction is None or k[1] == int(faction))}
            facs = (int(faction),) if faction is not None else (1, 2, 3)
            for fac in facs:
                for cat in (1, 5, 7):
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

    def stock_of(self, war_id, faction, cat):
        if (war_id, faction, cat) in self.stock:
            return self.stock[(war_id, faction, cat)]
        return 10 ** 9 if self.plentiful else 0

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
        self.dequeue_log.extend(ids)
        self.supply_queue = [r for r in self.supply_queue
                             if r['id'] not in ids]
        return len(ids)


def _setup(chars=None, balances=None):
    st = FakeStore(balances)
    st.characters[100] = list(chars or [])
    war_at.STORE = st
    player_service.STORE = st
    return st


def _mk_at(st, node_id, template=501, owner=100, faction=1, state=0,
           resources=None, sold_free=False, veh_free=False, gen=0,
           deployed=False, map_entity_id=0):
    st.ats[node_id] = {
        'id': node_id, 'war_id': 1, 'template_id': template,
        'owner_player_id': owner, 'faction': faction, 'name': 'Alpha',
        'abbreviation': 'ALT', 'rank': 1, 'state': state,
        'is_deployed': deployed, 'map_entity_id': map_entity_id,
        'map_entity_type': 1 if deployed else 0,
        'resources': dict(resources) if resources is not None else {},
        'has_gotten_soldier_free': sold_free,
        'has_gotten_vehicle_free': veh_free,
        'general_character_id': gen, 'morale': 100, 'max_morale': 100,
        'created_at': _NOW,
    }
    return st.ats[node_id]


def _mk_char(cid, template=601, faction=1, cp=0):
    return {'id': cid, 'template': template, 'faction': faction, 'cp': cp}


class FakeSess:
    warmap_view_cmd = 777
    commandnode_view_cmd = None
    playerview_cmd = None          # _push_player_refresh no-op

    async def ensure_player(self):
        return 100


class FakeWriter:
    def __init__(self):
        self.buf = bytearray()

    def write(self, data):
        self.buf.extend(data)

    async def drain(self):
        pass


# ------------------------------------------------------------- 帧解析

def _chunks(buf):
    out, off = [], 0
    while off < len(buf):
        c, off = Chunk.parse(bytes(buf), off)
        if c is None:
            break
        out.append(c)
    return out


def _resp(w, title):
    """最后一帧指定标题响应的 proto 字段。"""
    want = title.encode()
    for c in reversed(_chunks(w.buf)):
        if c.subs and c.subs[0].data == want:
            return pb_parse(c.subs[0].subs[0].data)
    raise AssertionError(f'no response {title}')


def _rows(w, table):
    """全部 KeyValueChangeSet set 帧中指定表的行 proto 字段列表。"""
    t = table.encode() if isinstance(table, str) else table
    out = []
    for c in _chunks(w.buf):
        if not c.subs or c.subs[0].data != b'KeyValueChangeSet':
            continue
        act = c.subs[0].subs[0]
        if act.data == b'set' and act.subs and act.subs[0].data == t:
            out.append(pb_parse(act.subs[0].subs[0].data))
    return out


def _resupply_req(node, sp=9001, vp=9002, cur=2):
    return (pb_int64(1, node) + pb_int64(2, sp) + pb_int64(4, vp)
            + pb_int32(8, cur))


def _equip_req(items):
    out = b''
    for c, n, d in items:
        out += pb_message(1, pb_int64(1, c) + pb_int64(2, n) + pb_bool(3, d))
    return out


async def _equip(items):
    w = FakeWriter()
    await war_at.handle_set_command_node_equipment_request(
        FakeSess(), w, 900, b'SetCommandNodeEquipmentRequest',
        _equip_req(items))
    return w, _resp(w, 'SetCommandNodeEquipmentResponse')


async def _resupply(node, sp=9001, vp=9002, cur=2):
    w = FakeWriter()
    await war_at.handle_resupply_request(
        FakeSess(), w, 900, b'resupply_request', _resupply_req(node, sp, vp,
                                                               cur))
    return w, _resp(w, 'resupply_request_response')


async def _create(name='Alpha', abbr='ALT', tpl=501, pricing=8001, cur=2):
    w = FakeWriter()
    req = (pb_string(1, name) + pb_string(2, abbr) + pb_int64(3, tpl)
           + pb_int64(4, pricing) + pb_int32(6, cur))
    await war_at.handle_create_command_node_request(
        FakeSess(), w, 900, b'CreateCommandNodeRequest', req)
    return w, _resp(w, 'CreateCommandNodeResponse')


async def _deploy(node, cur=2, sp=0, vp=0):
    # f2/f3 计价 id（官服 Deploy All 口径：部署请求自带计价 id 补编）
    w = FakeWriter()
    req = (pb_int64(1, node) + pb_int64(2, sp) + pb_int64(3, vp)
           + pb_int32(7, cur))
    await war_at.handle_deploy_command_node_request(
        FakeSess(), w, 900, b'DeployCommandNodeRequest', req)
    return w, _resp(w, 'DeployCommandNodeResponse')


# ============================================================= A 单元

async def section_a():
    tpl501 = wardb.commandnode_templates()[501]
    tpl502 = wardb.commandnode_templates()[502]

    st = _setup()
    a1 = _mk_at(st, 301, template=502, resources={'1': 10, '5': 25})
    miss = war_at._at_missing_resources(a1, tpl502)
    check('A1 _at_missing_resources str键/负值截0', miss == {7: 8}, repr(miss))

    check('A2 _unit_price 有行/缺行/异币种',
          war_at._unit_price(9001, 'warfunds') == 100
          and war_at._unit_price(9999, 'warfunds') == 0
          and war_at._unit_price(9001, 'gold') == 10)

    check('A3 模板-兵种匹配（将军恒真/gct0恒真/相等/不符）',
          war_at._template_matches_character(tpl502, 7)
          and war_at._template_matches_character(tpl501, 999)
          and war_at._template_matches_character(tpl502, 601)
          and not war_at._template_matches_character(tpl502, 602))

    st = _setup()
    a4 = _mk_at(st, 302, template=501, sold_free=True)
    caps = war_campaign._faction_capital_bf()
    inst = pb_parse(war_campaign.msg_commandnode_instance_row(a4, 1, caps))
    defn = pb_parse(war_campaign.msg_commandnode_definition_row(a4))
    check('A4 instance f11/f12 真值 + definition f11 恒 False',
          inst.get(11) == 1 and inst.get(12) == 0 and defn.get(11) == 0,
          f'inst={inst} defn={defn}')

    a5 = _mk_at(st, 303, template=501, resources={1: 12, 5: 0})
    rows = [pb_parse(p) for p in
            war_campaign.msg_armyresource_rows(a5, 1)]
    check('A5 armyresource id=node*1000+cat，cnt0 不出行',
          len(rows) == 1 and rows[0].get(1) == 303001
          and rows[0].get(4) == 12, str(rows))


# ============================================================= B 装备链

async def section_b():
    # B1 空 items → RC_FAIL(0) 原样回传（重映射假成功 bug 回归）
    _setup()
    w, f = await _equip([])
    check('B1 空 items → f1=0 原样（假成功 bug 回归）', f.get(1) == 0, str(f))

    # B2 equip 成功：gen 落库 + Definition f9 推送
    st = _setup(chars=[_mk_char(701, 601, 1, cp=3)])
    _mk_at(st, 400, template=501)
    w, f = await _equip([(701, 400, True)])
    defn = [r for r in _rows(w, 'CommandNodeDefinition') if r.get(1) == 400]
    check('B2 equip 成功（响应1/gen=701/Definition f9 推送）',
          f.get(1) == 1 and st.ats[400]['general_character_id'] == 701
          and defn and defn[-1].get(9) == 701, str(f))

    # B3 217 幂等：已装备同一角色
    st = _setup(chars=[_mk_char(701, 601, 1, cp=3)])
    _mk_at(st, 400, template=501, gen=701)
    w, f = await _equip([(701, 400, True)])
    check('B3 已装备同角色 → 217',
          f.get(1) == war_campaign.RC_COMMANDNODE_ALREADY_EQUIPPED, str(f))

    # B4 218：未装备即卸
    st = _setup(chars=[_mk_char(701, 601, 1, cp=3)])
    _mk_at(st, 400, template=501)
    w, f = await _equip([(701, 400, False)])
    check('B4 未装备即卸 → 218',
          f.get(1) == war_campaign.RC_COMMANDNODE_ALREADY_UNEQUIPPED, str(f))

    # B5 unequip 成功
    st = _setup(chars=[_mk_char(701, 601, 1, cp=3)])
    _mk_at(st, 400, template=501, gen=701)
    w, f = await _equip([(701, 400, False)])
    check('B5 unequip 成功（响应1/gen=0）',
          f.get(1) == 1 and st.ats[400]['general_character_id'] == 0, str(f))

    # B6 219：角色阵营与 AT 阵营不符
    st = _setup(chars=[_mk_char(701, 601, 2, cp=3)])
    _mk_at(st, 400, template=501)
    w, f = await _equip([(701, 400, True)])
    check('B6 阵营不符 → 219',
          f.get(1) == war_campaign.RC_EQUIPMENT_NOT_ALLOWED, str(f))

    # B7 219：模板限定兵种（502 gct=601）与角色兵种（602）不符
    st = _setup(chars=[_mk_char(701, 602, 1, cp=5)])
    _mk_at(st, 400, template=502)
    w, f = await _equip([(701, 400, True)])
    check('B7 模板兵种不符 → 219',
          f.get(1) == war_campaign.RC_EQUIPMENT_NOT_ALLOWED, str(f))

    # B8 将军角色（template=7）跨兵种装备 502 成功
    st = _setup(chars=[_mk_char(701, 7, 1, cp=3)])
    _mk_at(st, 400, template=502)
    w, f = await _equip([(701, 400, True)])
    check('B8 将军跨兵种装备成功',
          f.get(1) == 1 and st.ats[400]['general_character_id'] == 701,
          str(f))

    # B9 15：角色不存在
    st = _setup(chars=[])
    _mk_at(st, 400, template=501)
    w, f = await _equip([(888, 400, True)])
    check('B9 角色不存在 → 15',
          f.get(1) == war_campaign.RC_CHARACTER_NOT_FOUND, str(f))

    # B10 219：CP 不足（502 size=3 > unlocked 2）
    st = _setup(chars=[_mk_char(701, 601, 1, cp=2)])
    _mk_at(st, 400, template=502)
    w, f = await _equip([(701, 400, True)])
    check('B10 CP 不足（size3>cp2）→ 219',
          f.get(1) == war_campaign.RC_EQUIPMENT_NOT_ALLOWED, str(f))

    # B11 CP 恰好（size 3 == cp 3）
    st = _setup(chars=[_mk_char(701, 601, 1, cp=3)])
    _mk_at(st, 400, template=502)
    w, f = await _equip([(701, 400, True)])
    check('B11 CP 恰好（size3==cp3）成功',
          f.get(1) == 1 and st.ats[400]['general_character_id'] == 701,
          str(f))

    # B12 used 扣除：另一 AT（401，502 size3）已装备同角色
    st = _setup(chars=[_mk_char(701, 601, 1, cp=4)])
    _mk_at(st, 400, template=501)
    _mk_at(st, 401, template=502, gen=701)
    w, f = await _equip([(701, 400, True)])
    check('B12a 已占 size3、cp4 → 219（2 > 4-3）',
          f.get(1) == war_campaign.RC_EQUIPMENT_NOT_ALLOWED, str(f))

    st = _setup(chars=[_mk_char(701, 601, 1, cp=5)])
    _mk_at(st, 400, template=501)
    _mk_at(st, 401, template=502, gen=701)
    w, f = await _equip([(701, 400, True)])
    check('B12b cp5 余量恰够 → 成功',
          f.get(1) == 1 and st.ats[400]['general_character_id'] == 701,
          str(f))

    # B13 189：非归属 AT
    st = _setup(chars=[_mk_char(701, 601, 1, cp=3)])
    _mk_at(st, 400, template=501, owner=999)
    w, f = await _equip([(701, 400, True)])
    check('B13 非归属 → 189',
          f.get(1) == war_campaign.RC_INVALID_OWNER, str(f))

    # B14 批量 best-effort：item1 已应用仍推送，item2 失败回 218
    st = _setup(chars=[_mk_char(701, 601, 1, cp=5)])
    _mk_at(st, 400, template=501)
    _mk_at(st, 401, template=501)
    w, f = await _equip([(701, 400, True), (701, 401, False)])
    defn = [r for r in _rows(w, 'CommandNodeDefinition') if r.get(1) == 400]
    check('B14 批量 best-effort（前项已应用+推送，响应 218）',
          f.get(1) == war_campaign.RC_COMMANDNODE_ALREADY_UNEQUIPPED
          and st.ats[400]['general_character_id'] == 701
          and st.ats[401]['general_character_id'] == 0
          and bool(defn), str(f))


# ============================================================= C 增援链

async def section_c():
    # C1 240：币种非法
    st = _setup()
    _mk_at(st, 500, template=501)
    w, f = await _resupply(500, cur=9)
    check('C1 非法币种 → 240',
          f.get(2) == war_campaign.RC_INVALID_CURRENCY, str(f))

    # C2 189：非归属
    st = _setup()
    _mk_at(st, 500, template=501, owner=999)
    w, f = await _resupply(500)
    check('C2 非归属 → 189',
          f.get(2) == war_campaign.RC_INVALID_OWNER, str(f))

    # C3 101：会战中
    st = _setup()
    _mk_at(st, 500, template=501, state=2)
    w, f = await _resupply(500)
    check('C3 会战中 → 101',
          f.get(2) == war_campaign.RC_NO_RESUPPLY_IN_BATTLE, str(f))

    # C4 116：移动中
    st = _setup()
    _mk_at(st, 500, template=501, state=1)
    w, f = await _resupply(500)
    check('C4 移动中 → 116',
          f.get(2) == war_campaign.RC_NO_RESUPPLY_IN_TRANSPORT, str(f))

    # C5 103：满编
    st = _setup()
    _mk_at(st, 500, template=501, resources={'1': 12})
    w, f = await _resupply(500)
    check('C5 满编 → 103',
          f.get(2) == war_campaign.RC_NO_RESUPPLY_ALREADY_FULL, str(f))

    # C6 首次免费：补满 + 置位 + 免单 + armyresource 推送
    st = _setup()
    _mk_at(st, 500, template=501)
    w, f = await _resupply(500)
    res = [r for r in _rows(w, 'armyresource') if r.get(1) == 500001]
    inst = [r for r in _rows(w, 'CommandNodeWarInstance')
            if r.get(1) == 500]
    check('C6 首次免费（补满/置位/免单/推送 f11=1）',
          f.get(2) == 1 and st.ats[500]['resources'] == {'1': 12}
          and st.ats[500]['has_gotten_soldier_free'] is True
          and not st.spend_log and st.balance(100) == 3000
          and res and res[-1].get(4) == 12
          and inst and inst[-1].get(11) == 1, str(f))

    # C7 二次付费：100 × 12 = 1200
    st = _setup()
    _mk_at(st, 500, template=501, sold_free=True)
    w, f = await _resupply(500)
    check('C7 二次付费 1200（3000→1800）',
          f.get(2) == 1 and st.balance(100) == 1800
          and st.ats[500]['resources'] == {'1': 12}
          and st.spend_log == [(100, 'warfunds', 1200)], str(f))

    # C8 153 不足：资源/免费标志/余额三不变
    st = _setup(balances={(100, 'warfunds'): 100})
    _mk_at(st, 500, template=501, sold_free=True)
    w, f = await _resupply(500)
    check('C8 余额不足 → 153（三不变）',
          f.get(2) == war_campaign.RC_NOT_ENOUGH_WARFUNDS
          and st.ats[500]['resources'] == {}
          and st.ats[500]['has_gotten_soldier_free'] is True
          and st.balance(100) == 100 and not st.spend_log, str(f))

    # C9 载具分侧免费：仅缺载具（cat7）→ 免单补满 + veh_free 置位
    st = _setup()
    _mk_at(st, 500, template=502, resources={'5': 20}, sold_free=True)
    w, f = await _resupply(500)
    check('C9 载具侧首次免费（补满/置位/免单）',
          f.get(2) == 1
          and st.ats[500]['resources'] == {'5': 20, '7': 8}
          and st.ats[500]['has_gotten_vehicle_free'] is True
          and st.ats[500]['has_gotten_soldier_free'] is True
          and not st.spend_log and st.balance(100) == 3000, str(f))

    # C10 双侧付费：100×20 + 250×8 = 4000
    st = _setup(balances={(100, 'warfunds'): 10000})
    _mk_at(st, 500, template=502, sold_free=True, veh_free=True)
    w, f = await _resupply(500)
    check('C10 双侧付费 4000（10000→6000）',
          f.get(2) == 1 and st.balance(100) == 6000
          and st.ats[500]['resources'] == {'5': 20, '7': 8}
          and st.spend_log == [(100, 'warfunds', 4000)], str(f))

    # C11 定价缺失免单（pricing 0 → 单价 0）
    st = _setup()
    _mk_at(st, 500, template=501, sold_free=True)
    w, f = await _resupply(500, sp=0)
    check('C11 定价缺失免单补满',
          f.get(2) == 1 and st.ats[500]['resources'] == {'1': 12}
          and st.balance(100) == 3000 and not st.spend_log, str(f))

    # C12 响应字段位：resupply 走 f2（AS3 实锤），f1 无值
    check('C12 响应 f2=1 且无 f1',
          f.get(2) == 1 and f.get(1) is None, str(f))

    # C13 M3-2 全额入队：库存 0 → 226 + 入队 + 扣款 + AT 编制不动
    st = _setup(balances={(100, 'warfunds'): 10000})
    st.plentiful = False
    _mk_at(st, 500, template=501, sold_free=True)
    w, f = await _resupply(500)
    check('C13 库存空 → 226（入队 cat1 x12/扣 1200/编制不动）',
          f.get(2) == war_campaign.RC_COMMANDNODE_QUEUED_FOR_RESOURCES
          and st.ats[500]['resources'] == {}
          and [(r['category_id'], r['count']) for r in st.supply_queue]
          == [(1, 12)]
          and st.spend_log == [(100, 'warfunds', 1200)]
          and st.balance(100) == 8800, str(f))

    # C14 M3-2 部分交付：库存 5 → 即时 5 + 缺口 7 入队 + 226 + 库存清 0
    st = _setup(balances={(100, 'warfunds'): 10000})
    st.plentiful = False
    st.stock[(1, 1, 1)] = 5
    _mk_at(st, 500, template=501, sold_free=True)
    w, f = await _resupply(500)
    check('C14 库存5 → 给5+入队7/226/库存0/编制{1:5}/扣全额1200',
          f.get(2) == war_campaign.RC_COMMANDNODE_QUEUED_FOR_RESOURCES
          and st.ats[500]['resources'] == {'1': 5}
          and [(r['category_id'], r['count']) for r in st.supply_queue]
          == [(1, 7)]
          and st.stock[(1, 1, 1)] == 0
          and st.spend_log == [(100, 'warfunds', 1200)]
          and bool([r for r in _rows(w, 'armyresource')
                    if r.get(1) == 500001 and r.get(4) == 5]), str(f))


# ====================================================== F M3-2 队列交付

async def section_f():
    # F1 _qn_window：空队 (0,0)/head=MIN、tail=MAX+1
    check('F1 _qn_window 空(0,0)/窗口(min,max+1)',
          war_campaign._qn_window([]) == (0, 0)
          and war_campaign._qn_window(
              [{'queue_number': 3}, {'queue_number': 5}]) == (3, 6))

    # F2 队头交付：库存足 → 库存扣减 + AT 补编 + 出队 + 余额不动
    st = _setup()
    st.plentiful = False
    st.stock[(1, 1, 1)] = 20
    _mk_at(st, 500, template=501, resources={'1': 2})
    await st.enqueue_war_supply(1, 1, 500, {1: 12})
    await war_campaign.supply_queue_tick(st)
    check('F2 队头交付（库存20→8/编制14/出队/不扣款）',
          st.stock[(1, 1, 1)] == 8
          and st.ats[500]['resources'] == {'1': 14}
          and not st.supply_queue and st.dequeue_log == [1]
          and not st.spend_log, str(st.stock))

    # F3 队头不足阻塞：不交付不出队
    st = _setup()
    st.plentiful = False
    st.stock[(1, 1, 1)] = 5
    _mk_at(st, 500, template=501)
    await st.enqueue_war_supply(1, 1, 500, {1: 12})
    await war_campaign.supply_queue_tick(st)
    check('F3 队头不足 → 阻塞（不交付/不出队）',
          st.stock[(1, 1, 1)] == 5
          and st.ats[500]['resources'] == {}
          and len(st.supply_queue) == 1, str(st.stock))

    # F4 AT 歼灭（state=3）→ 出队不交付不退款
    st = _setup()
    st.plentiful = False
    st.stock[(1, 1, 1)] = 20
    _mk_at(st, 500, template=501, state=3)
    await st.enqueue_war_supply(1, 1, 500, {1: 12})
    await war_campaign.supply_queue_tick(st)
    check('F4 AT 歼灭 → 出队不退款（库存不动/不出编）',
          st.stock[(1, 1, 1)] == 20
          and not st.supply_queue and st.dequeue_log == [1]
          and not st.spend_log, str(st.stock))

    # F5 阵营隔离：阵营 2 队列不消耗阵营 1 库存
    st = _setup()
    st.plentiful = False
    st.stock[(1, 2, 1)] = 20
    _mk_at(st, 500, template=501, faction=2)
    await st.enqueue_war_supply(1, 2, 500, {1: 12})
    await war_campaign.supply_queue_tick(st)
    check('F5 阵营隔离交付（fac2 库存扣/AT 补编）',
          st.stock[(1, 2, 1)] == 8
          and st.ats[500]['resources'] == {'1': 12}
          and not st.supply_queue, str(st.stock))


# ================================================= G AT 级队列行推送

async def section_g():
    import state as state_mod
    from types import SimpleNamespace

    # G1 item 行 REQUIRED 字段位（f1 id/f2 playerId/f3 AT/f4 cat/f5 war/
    # f6 faction/f10 queueNumber/f17 resourcesNeeded；f8/f9/f11-f14 空值）
    st = _setup()
    _mk_at(st, 500, template=501)
    await st.enqueue_war_supply(1, 1, 500, {1: 12})
    q = st.supply_queue[0]
    item = pb_parse(war_campaign.msg_supply_queue_item_row(q, st.ats[500], 1))
    check('G1 item 行字段位（id/player/AT/cat/war/fac/qn/need）',
          item.get(1) == 1 and item.get(2) == 100 and item.get(3) == 500
          and item.get(4) == 1 and item.get(5) == 1 and item.get(6) == 1
          and item.get(10) == 1 and item.get(17) == 12
          and item.get(13) == b'' and item.get(8) == 0, str(item))

    # G2 ticket 行字段位（f1/f2/f3/f4/f7/f8/f9；queuePlace=传入位次）
    tick = pb_parse(war_campaign.msg_supply_queue_ticket_row(
        q, st.ats[500], 3))
    check('G2 ticket 行字段位（id/player/war/fac/ts/qn/place=3）',
          tick.get(1) == 1 and tick.get(2) == 100 and tick.get(3) == 1
          and tick.get(4) == 1 and tick.get(8) == 1 and tick.get(9) == 3
          and tick.get(5) is None and tick.get(6) is None, str(tick))

    # G3 全量行：跨阵营 FIFO 序 + queuePlace 按阵营独立计数（1 起）
    st = _setup()
    war = await war_campaign.ensure_war(st)
    _mk_at(st, 500, template=501)
    _mk_at(st, 501, template=502, faction=2)
    await st.enqueue_war_supply(1, 1, 500, {1: 12})     # 队列行 id=1
    await st.enqueue_war_supply(1, 2, 501, {5: 20})     # id=2（fac2）
    await st.enqueue_war_supply(1, 1, 500, {7: 8})      # id=3（fac1 第 2 行）
    items, tickets = await war_campaign.supply_queue_table_rows(st, war)
    pi = [pb_parse(r) for r in items]
    pt = [pb_parse(r) for r in tickets]
    check('G3 全量行（3 item+3 ticket/qn 全局单调/place 按阵营 1 起）',
          [r.get(6) for r in pi] == [1, 2, 1]
          and [r.get(9) for r in pt] == [1, 1, 2]
          and [r.get(10) for r in pi] == [1, 2, 3]
          and [r.get(8) for r in pt] == [1, 2, 3]
          and all(r.get(2) == 100 for r in pi), str(pt))

    # G4 push_supply_queue_rows → resourceview 订阅会话（item+ticket set 行）
    st = _setup()
    war = await war_campaign.ensure_war(st)
    _mk_at(st, 500, template=501)
    await st.enqueue_war_supply(1, 1, 500, {1: 12})
    sess_obj = SimpleNamespace(resource_view_cmd=555, writer=FakeWriter())
    state_mod.FLASH_SESSIONS['g4'] = sess_obj
    try:
        await war_campaign.push_supply_queue_rows(st, war)
    finally:
        state_mod.FLASH_SESSIONS.pop('g4', None)
    buf = bytes(sess_obj.writer.buf)
    check('G4 入队重推（buf 含 item+ticket 行）',
          b'CommandNodeSupplyQueueItem' in buf
          and b'CommandNodeSupplyQueueItemTicket' in buf
          and b'\x08\x01' in buf, f'{len(buf)}B')

    # G5 tick 交付 → DELETE 行推送（item+ticket 同 id；客户端关窗语义）
    st = _setup()
    st.plentiful = False
    st.stock[(1, 1, 1)] = 20
    _mk_at(st, 500, template=501)
    await st.enqueue_war_supply(1, 1, 500, {1: 12})
    sess_obj = SimpleNamespace(resource_view_cmd=555, writer=FakeWriter())
    state_mod.FLASH_SESSIONS['g5'] = sess_obj
    try:
        await war_campaign.supply_queue_tick(st)
    finally:
        state_mod.FLASH_SESSIONS.pop('g5', None)
    buf = bytes(sess_obj.writer.buf)
    check('G5 交付 DELETE（buf 含 delete 动作 ×2 表/队列已清）',
          buf.count(b'delete') == 2
          and not st.supply_queue
          and st.ats[500]['resources'] == {'1': 12}, f'{len(buf)}B')

    # G6 DB-free 回退：无战争 → ([], [])；push 无会话不炸
    st = _setup()
    st.wars.clear()
    items, tickets = await war_campaign.supply_queue_table_rows(st)
    check('G6 无战争回退（([], [])/push 静默）',
          items == [] and tickets == [])


# ======================================================== D 买壳/部署冒烟

async def section_d():
    # D1 create 买壳：空编 + 扣款 500 + Definition 推送
    st = _setup()
    w, f = await _create()
    at = st.ats.get(1)
    defn = [r for r in _rows(w, 'CommandNodeDefinition') if r.get(1) == 1]
    check('D1 买壳（响应1/resources={}/gen=0/扣500/推送）',
          f.get(1) == 1 and at is not None and at['resources'] == {}
          and at['general_character_id'] == 0
          and st.balance(100) == 2500
          and st.spend_log == [(100, 'warfunds', 500)]
          and defn and defn[-1].get(2) == b'Alpha', str(f))

    # D2 deploy：落位己方首都（capitals 合成 {1: bf10}）+ 部署即补编
    # （§10.2 #4/#5：新 AT 首次免费满编置位，余额不动）
    st = _setup()
    _mk_at(st, 500, template=501)
    w, f = await _deploy(500)
    inst = [r for r in _rows(w, 'CommandNodeWarInstance')
            if r.get(1) == 500]
    check('D2 部署+首补免费（响应1/满编{1:12}/free置位/余额不动/bf=10）',
          f.get(1) == 1 and st.ats[500]['is_deployed'] is True
          and st.ats[500]['map_entity_id'] == 10
          and st.ats[500]['resources'] == {'1': 12}
          and st.ats[500]['has_gotten_soldier_free'] is True
          and st.balance(100) == 3000 and not st.spend_log
          and inst and inst[-1].get(5) == 10 and inst[-1].get(10) == 1,
          str(f))

    # D3 重复部署 → 150
    w, f = await _deploy(500)
    check('D3 重复部署 → 150',
          f.get(1) == war_campaign.RC_COMMANDNODE_ALREADY_DEPLOYED, str(f))

    # D4 部署付费补编（free 已置位 → 按计价扣款；Deploy All 空编缺口修复）
    st = _setup()
    _mk_at(st, 500, template=501, sold_free=True, veh_free=True)
    w, f = await _deploy(500, sp=9001, vp=9002)
    check('D4 部署付费补编（响应1/扣1200/满编{1:12}）',
          f.get(1) == 1 and st.ats[500]['resources'] == {'1': 12}
          and st.spend_log == [(100, 'warfunds', 1200)]
          and st.balance(100) == 1800, str(f))

    # D5 部署库存不足 → 226 入队（官服"入队成功"语义；扣款照常、部署不回滚）
    st = _setup()
    st.plentiful = False
    _mk_at(st, 500, template=501, sold_free=True, veh_free=True)
    w, f = await _deploy(500, sp=9001, vp=9002)
    inst = [r for r in _rows(w, 'CommandNodeWarInstance')
            if r.get(1) == 500]
    check('D5 部署缺库存 → 226（入队(1,12)/扣1200/编制不动/AT行照推/bf=10）',
          f.get(1) == war_campaign.RC_COMMANDNODE_QUEUED_FOR_RESOURCES
          and [(r['category_id'], r['count']) for r in st.supply_queue]
          == [(1, 12)]
          and st.spend_log == [(100, 'warfunds', 1200)]
          and st.ats[500]['resources'] == {}
          and st.ats[500]['is_deployed'] is True
          and inst and inst[-1].get(5) == 10, str(f))


# ================================================= E transport/升级口径

async def _transport(node, target=30, ttype='battlefield'):
    w = FakeWriter()
    req = pb_int64(1, node) + pb_int64(2, target) + pb_string(3, ttype)
    await war_at.handle_transport_commandnode(
        FakeSess(), w, 900, b'transport_commandnode', req)
    return w, _resp(w, 'transport_commandnode_response')


async def _upgrade(node, tgt=503, pricing=8001, cur=2):
    w = FakeWriter()
    req = (pb_int64(1, node) + pb_int64(2, tgt) + pb_int64(3, pricing)
           + pb_int32(5, cur))
    await war_at.handle_upgrade_command_node_request(
        FakeSess(), w, 900, b'UpgradeCommandNodeRequest', req)
    return w, _resp(w, 'UpgradeCommandNodeResponse')


async def section_e():
    # E1 未知 targetType → 7（transport 族 0=ok，失败禁回 0）
    st = _setup()
    _mk_at(st, 600, template=501, deployed=True, map_entity_id=10)
    w, f = await _transport(600, ttype='moon')
    check('E1 未知 targetType → 7（非 0 假成功）',
          f.get(3) == war_campaign.RC_CANNOT_FIND_COMMANDNODE, str(f))

    # E2 目标不可达（空补给线图）→ 7，且不置在途
    st = _setup()
    _mk_at(st, 600, template=501, deployed=True, map_entity_id=10)
    w, f = await _transport(600, target=30)
    check('E2 不可达目标 → 7（非 0 假成功）',
          f.get(3) == war_campaign.RC_CANNOT_FIND_COMMANDNODE
          and st.ats[600]['state'] == 0, str(f))

    # E3 升级不补编：模板换 503，resources 保持 {'1': 6}（买壳口径）
    # （503 level=2 → 2026-09-03 升级 rank 门槛后需 AT rank>=2）
    st = _setup()
    _mk_at(st, 600, template=501, resources={'1': 6})
    st.ats[600]['rank'] = 2
    w, f = await _upgrade(600)
    check('E3 升级换模板不补编（resources 不动/扣款照常）',
          f.get(1) == 1 and st.ats[600]['template_id'] == 503
          and st.ats[600]['resources'] == {'1': 6}
          and st.spend_log == [(100, 'warfunds', 500)], str(f))


async def main():
    await section_a()
    await section_b()
    await section_c()
    await section_f()
    await section_g()
    await section_d()
    await section_e()
    fails = [label for label, ok in RESULTS if not ok]
    print(f'\n==== {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS ====')
    for label in fails:
        print(f'  FAIL: {label}')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
