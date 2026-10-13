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
import random
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
    # R 组（§10.2 #6/#7）：空中半径单位（真实表值 fighter_squadron/
    # paratroopers 的半径/速度比例）
    # 504 补给空运队（reserve 模板：504%100==4，交战机场例外）
    504: {'id': 504, 'name': 'Supply Flight', 'faction': 1,
          'resources': {1: 4}, 'vehicle_cats': set(), 'size': 1,
          'general_character_template_id': 0, 'level': 1,
          'attackradius': 550, 'transportradius': 1850, 'actionid': 14,
          'speed': 3.0},
    # 505 战斗机中队（actionid 15：attack 950 / transport 2850 / 5.2）
    505: {'id': 505, 'name': 'Fighter Squadron', 'faction': 1,
          'resources': {1: 6}, 'vehicle_cats': set(), 'size': 2,
          'general_character_template_id': 0, 'level': 1,
          'attackradius': 950, 'transportradius': 2850, 'actionid': 15,
          'speed': 5.2},
    # 506 伞兵（actionid 13：attack 1850 / transport 3600 / convert 507）
    506: {'id': 506, 'name': 'Paratroopers', 'faction': 1,
          'resources': {1: 16}, 'vehicle_cats': set(), 'size': 2,
          'general_character_template_id': 0, 'level': 1,
          'attackradius': 1850, 'transportradius': 3600, 'actionid': 13,
          'convert_to': 507, 'speed': 2.7},
    # 507 伞兵落地变体（半径 -1=无限 → 非 radius 单位，地面速度；
    # 真实表 paratroopers_on_ground f11 convertTo 指回原型 108）
    507: {'id': 507, 'name': 'paratroopers_on_ground', 'faction': 1,
          'resources': {1: 16}, 'vehicle_cats': set(), 'size': 2,
          'general_character_template_id': 0, 'level': 1,
          'attackradius': -1, 'transportradius': -1, 'convert_to': 506,
          'speed': 0.48},
    # 508/509：f11 互转用例对（真实伞兵口径 311/312 缩影：空中编制
    # 含运输机 cat10，落地变体只有伞兵 cat4 → 落地后 resupply 恒
    # "已满编"，须 f11 反向互转回空中原型才能重装运输机）
    508: {'id': 508, 'name': 'Paratroopers II', 'faction': 1,
          'resources': {4: 36, 10: 9}, 'vehicle_cats': {10}, 'size': 2,
          'general_character_template_id': 0, 'level': 1,
          'attackradius': 1850, 'transportradius': 3600, 'actionid': 13,
          'convert_to': 509, 'speed': 2.7},
    509: {'id': 509, 'name': 'paratroopers_on_ground', 'faction': 1,
          'resources': {4: 36}, 'vehicle_cats': set(), 'size': 2,
          'general_character_template_id': 0, 'level': 1,
          'attackradius': -1, 'transportradius': -1, 'convert_to': 508,
          'speed': 0.48},
}

wardb.accesspoints = lambda: [
    {'id': 101, 'battlefield': 10}, {'id': 102, 'battlefield': 11}]
wardb.supplylines = lambda: [{'id': 1, 'ap1': 101, 'ap2': 102}]
# 城市图（R 组半径判定的坐标/机场）：10 首都(0,0)、11 邻城+机场(100,0)、
# 12 远机场(800,0)、13 非机场城(800,100)、14 超半径城(2000,0)、
# 15 敌机场(850,0)、16 攻击半径内城(450,0)、17 伞兵换基地机场(1200,0)
wardb.battlefields = lambda: [
    {'id': 10, 'posx': 0, 'posy': 0, 'gamemap': 0},
    {'id': 11, 'posx': 100, 'posy': 0, 'gamemap': 204},
    {'id': 12, 'posx': 800, 'posy': 0, 'gamemap': 204},
    {'id': 13, 'posx': 800, 'posy': 100, 'gamemap': 0},
    {'id': 14, 'posx': 2000, 'posy': 0, 'gamemap': 0},
    {'id': 15, 'posx': 850, 'posy': 0, 'gamemap': 205},
    {'id': 16, 'posx': 450, 'posy': 0, 'gamemap': 0},
    {'id': 17, 'posx': 1200, 'posy': 0, 'gamemap': 204},
]
wardb.capitals = lambda: [{'battlefield_id': 10, 'default_faction': 1}]
# 部署官服语义（#4/#5）：11 为 10 的邻接己方城（首都 10 + 邻城 11 池）
# （拆包后 monkeypatch 须打定义子模块 lifecycle，__init__ re-export 不回流）
war_campaign.lifecycle.initial_city_owners = lambda: {10: 1, 11: 1}
wardb.commandnode_templates = lambda: {k: dict(v) for k, v in _TPLS.items()}
wardb.morale_template = lambda: None
# M3 生产种子库存禁用（ensure_war 不落 war_start_value，队列用例自控库存）
wardb.armyresource_production = lambda: []
war_campaign.common._ADJACENCY_CACHE = None
war_campaign.common._BF_POS_CACHE = None

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
    9003: {'type': 'reinforceVehicle', 'warfunds': 250, 'gold': 1.2222},
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
        # 活跃战况（#4 部署校验：主城/邻城交战判定）
        self.battles = []
        # 移动（R 组：空中半径单位飞行）
        self.movements = {}
        self.next_mv_id = 1

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

    async def set_city_owner(self, war_id, bf_id, faction):
        self.city_owners.setdefault(int(war_id), {})[int(bf_id)] = int(faction)

    async def get_war_cities(self, war_id):
        return dict(self.city_owners.get(int(war_id), {}))

    async def list_active_war_battles(self, war_id):
        return [dict(b) for b in self.battles
                if b['war_id'] == int(war_id)]

    # ---- 成局链（R2i：敌城空降到达 → _check_battle_formation）----
    async def create_war_battle(self, war_id, battlefield_id, factions,
                                battle_type=1):
        bid = max((int(b.get('id', 0)) for b in self.battles), default=0) + 1
        b = {'id': bid, 'war_id': int(war_id),
             'battlefield_id': int(battlefield_id),
             'battle_type': int(battle_type), 'state': 0,
             'winning_faction': 0, 'factions': list(factions or []),
             'started_at': _NOW}
        self.battles.append(b)
        return dict(b)

    async def create_mission_record(self, preset, attacker_faction=0,
                                    defender_faction=0, battle_id=0, **kw):
        # 与真实 store 契约一致：返回 mission id（int），record 落
        # mission_records（get_mission_by_battle 查询用）
        mid = 900000 + int(battle_id or 0)
        if not hasattr(self, 'mission_records'):
            self.mission_records = {}
        self.mission_records[mid] = {
            'id': mid, 'state': 0, 'preset': int(preset or 0),
            'attacker_faction': int(attacker_faction or 0),
            'defender_faction': int(defender_faction or 0),
            'battle_id': int(battle_id or 0)}
        return mid

    async def get_mission_by_battle(self, battle_id):
        for m in getattr(self, 'mission_records', {}).values():
            if int(m.get('battle_id') or 0) == int(battle_id):
                return dict(m)
        return None

    # ---- movement（R 组）----
    async def create_movement(self, at_id, war_id, path, started_at=None,
                              arrive_at=None, dest_ap=0):
        mid = self.next_mv_id
        self.next_mv_id += 1
        self.movements[mid] = {
            'id': mid, 'commandnode_id': int(at_id), 'war_id': int(war_id),
            'path': [dict(s) for s in path], 'started_at': started_at,
            'arrive_at': arrive_at, 'dest_ap': int(dest_ap or 0), 'state': 0}
        return copy.deepcopy(self.movements[mid])

    async def list_active_movements(self, war_id):
        return [copy.deepcopy(m) for m in self.movements.values()
                if m['war_id'] == int(war_id) and m['state'] == 0]

    async def get_active_movement(self, at_id):
        return next((copy.deepcopy(m) for m in self.movements.values()
                     if m['commandnode_id'] == int(at_id) and m['state'] == 0), None)

    async def finish_movement(self, mv_id, state):
        if int(mv_id) in self.movements:
            self.movements[int(mv_id)]['state'] = int(state)

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

    async def delete_command_node(self, node_id):
        a = self.ats.get(int(node_id))
        if not a:
            return False
        a['state'] = 3
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
            'parent_command_node_id': int(parent_command_node_id or 0),
            'has_gotten_soldier_free': False,
            'has_gotten_vehicle_free': False,
            'general_character_id': 0, 'morale': 100, 'max_morale': 100,
            'morale_updated_at': datetime.datetime.now(
                datetime.timezone.utc),
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

    async def add_currency(self, pid, credits=0, warfunds=0, gold=0):
        for key, amount in (('credits', credits), ('warfunds', warfunds),
                            ('gold', gold)):
            if amount:
                self.balances[(int(pid), key)] = self.balance(pid, key) + int(amount)
        return {key: self.balance(pid, key)
                for key in ('credits', 'warfunds', 'gold')}

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
            # 充足模式：合成模板编制涉及的全部 cat（1/4/5/7/10，
            # 10=运输机：f11 互转用例对 508/509 编制）
            seen = {k for k in self.stock
                    if k[0] == int(war_id)
                    and (faction is None or k[1] == int(faction))}
            facs = (int(faction),) if faction is not None else (1, 2, 3)
            for fac in facs:
                for cat in (1, 4, 5, 7, 10):
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
                                 items, metadata=None):
        metadata = metadata or {}
        per_category = metadata.get('per_category') or {}
        n = 0
        for cat, cnt in (items or {}).items():
            if int(cnt) <= 0:
                continue
            row = {
                'id': len(self.supply_queue) + 1,
                'war_id': int(war_id), 'faction': int(faction),
                'command_node_id': int(command_node_id),
                'category_id': int(cat), 'count': int(cnt),
                'queue_number': len(self.supply_queue) + 1}
            row.update(dict(per_category.get(int(cat)) or {}))
            self.supply_queue.append(row)
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

    async def reorder_war_supply(self, war_id, faction, command_node_id,
                                 direction):
        rows = [r for r in self.supply_queue
                if r['war_id'] == int(war_id)
                and r['faction'] == int(faction)]
        groups, by_node = [], {}
        for row in sorted(rows, key=lambda r: r['queue_number']):
            nid = row['command_node_id']
            if nid not in by_node:
                by_node[nid] = []
                groups.append((nid, by_node[nid]))
            by_node[nid].append(row)
        idx = next((i for i, group in enumerate(groups)
                    if group[0] == int(command_node_id)), -1)
        if idx < 0:
            return 111
        if direction == 0:
            if idx == 0:
                return 112
            groups[idx - 1], groups[idx] = groups[idx], groups[idx - 1]
        elif direction == 1:
            if idx == len(groups) - 1:
                return 112
            groups[idx], groups[idx + 1] = groups[idx + 1], groups[idx]
        elif direction == 2:
            groups.insert(0, groups.pop(idx))
        else:
            return 36
        for qn, row in enumerate(
                [row for _nid, group in groups for row in group], 1):
            row['queue_number'] = qn
        return 1


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
        'morale_updated_at': datetime.datetime.now(datetime.timezone.utc),
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
    """全部 KeyValueChangeSet set 帧中指定表的行 proto 字段列表。
    一帧可含多 action×多表块（_push_kvcs_batch 批量推送），遍历方式
    与客户端 updateTablesFromChangeset 同构：逐 action × 逐表 × 取首行。"""
    t = table.encode() if isinstance(table, str) else table
    out = []
    for c in _chunks(w.buf):
        if not c.subs or c.subs[0].data != b'KeyValueChangeSet':
            continue
        for act in c.subs[0].subs:
            if act.data != b'set':
                continue
            for tbl in act.subs:
                if tbl.data == t and tbl.subs:
                    out.append(pb_parse(tbl.subs[0].data))
    return out


def _dels(w, table):
    """全部 KeyValueChangeSet delete 帧中指定表的行 id 列表
    （delete 行内容为 8 字节 LE int64，非 proto 编码；多 action×多表
    块遍历同 _rows）。"""
    import struct as _struct
    t = table.encode() if isinstance(table, str) else table
    out = []
    for c in _chunks(w.buf):
        if not c.subs or c.subs[0].data != b'KeyValueChangeSet':
            continue
        for act in c.subs[0].subs:
            if act.data != b'delete':
                continue
            for tbl in act.subs:
                if tbl.data == t and tbl.subs:
                    out.append(_struct.unpack('<q', tbl.subs[0].data)[0])
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


async def _leave_supply_queue(node):
    w = FakeWriter()
    await war_at.handle_leave_supply_queue_request(
        FakeSess(), w, 900, b'LeaveSupplyQueueRequest', pb_int64(1, node))
    return w, _resp(w, 'LeaveSupplyQueueResponse')


async def _reorder_supply_queue(node, direction):
    w = FakeWriter()
    await war_at.handle_reorder_supply_queue_request(
        FakeSess(), w, 900, b'ReorderSupplyQueueRequest',
        pb_int64(1, node) + pb_int32(2, direction))
    return w, _resp(w, 'ReorderSupplyQueueResponse')


async def _create(name='Alpha', abbr='ALT', tpl=501, pricing=8001, cur=2):
    w = FakeWriter()
    req = (pb_string(1, name) + pb_string(2, abbr) + pb_int64(3, tpl)
           + pb_int64(4, pricing) + pb_int32(6, cur))
    await war_at.handle_create_command_node_request(
        FakeSess(), w, 900, b'CreateCommandNodeRequest', req)
    return w, _resp(w, 'CreateCommandNodeResponse')


async def _deploy(node, cur=2, sp=0, vp=0, cap=0):
    # f2/f3 计价 id（官服 Deploy All 口径：部署请求自带计价 id 补编）
    # f4 nearCapitalBattlefieldId（玩家所选部署主城）
    w = FakeWriter()
    req = (pb_int64(1, node) + pb_int64(2, sp) + pb_int64(3, vp)
           + pb_int64(4, cap) + pb_int32(7, cur))
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

    # C5 满编 → ok no-op（2026-09-14 修"部署成功后卡部署界面"：部署
    # 窗口自动补编链 issueSupplyRequest 在部署已补满编后必达（快照价
    # >0），回 103 会让 requestSuppliesResponse 进错误态永久卡窗——
    # 仅 ok 关窗；满编 ok 无扣款无行变更）
    st = _setup()
    _mk_at(st, 500, template=501, resources={'1': 12})
    w, f = await _resupply(500)
    check('C5 满编 → ok no-op（无扣款/资源不变）',
          f.get(2) == 1 and st.ats[500]['resources'] == {'1': 12}
          and not st.spend_log, str(f))

    # C6 已部署 AT 的增援不消耗“初始部署免费”机会
    st = _setup()
    _mk_at(st, 500, template=501)
    w, f = await _resupply(500)
    res = [r for r in _rows(w, 'armyresource') if r.get(1) == 500001]
    inst = [r for r in _rows(w, 'CommandNodeWarInstance')
            if r.get(1) == 500]
    check('C6 已部署增援付费（免费标志不置位）',
          f.get(2) == 1 and st.ats[500]['resources'] == {'1': 12}
          and st.ats[500]['has_gotten_soldier_free'] is False
          and st.spend_log == [(100, 'warfunds', 1200)]
          and st.balance(100) == 1800
          and res and res[-1].get(4) == 12
          and inst and inst[-1].get(11) in (None, 0), str(f))

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

    # C9 已部署载具增援同样付费
    st = _setup()
    _mk_at(st, 500, template=502, resources={'5': 20}, sold_free=True)
    w, f = await _resupply(500)
    check('C9 已部署载具增援付费',
          f.get(2) == 1
          and st.ats[500]['resources'] == {'5': 20, '7': 8}
          and st.ats[500]['has_gotten_vehicle_free'] is False
          and st.ats[500]['has_gotten_soldier_free'] is True
          and st.spend_log == [(100, 'warfunds', 2000)]
          and st.balance(100) == 1000, str(f))

    # C10 双侧付费：100×20 + 250×8 = 4000
    st = _setup(balances={(100, 'warfunds'): 10000})
    _mk_at(st, 500, template=502, sold_free=True, veh_free=True)
    w, f = await _resupply(500)
    check('C10 双侧付费 4000（10000→6000）',
          f.get(2) == 1 and st.balance(100) == 6000
          and st.ats[500]['resources'] == {'5': 20, '7': 8}
          and st.spend_log == [(100, 'warfunds', 4000)], str(f))

    # C11 定价缺失必须拒单，不允许零价刷资源
    st = _setup()
    _mk_at(st, 500, template=501, sold_free=True)
    w, f = await _resupply(500, sp=0)
    check('C11 定价缺失 → 193 且不交付',
          f.get(2) == war_campaign.RC_PRICE_NOT_FOUND
          and st.ats[500]['resources'] == {}
          and st.balance(100) == 3000 and not st.spend_log, str(f))

    # C12 响应字段位：resupply 走 f2（AS3 实锤），f1 无值
    check('C12 错误响应仍使用 f2 且无 f1',
          f.get(2) == war_campaign.RC_PRICE_NOT_FOUND
          and f.get(1) is None, str(f))

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
          and st.balance(100) == 8800
          and st.supply_queue[0].get('pricing_id') == 9001
          and st.supply_queue[0].get('payment_currency') == 2
          and st.supply_queue[0].get('paid_amount') == 1200
          and st.supply_queue[0].get('command_node_definition_id') == 500,
          str(f))

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

    # C15 库存读取失败：不可再按“库存无限”发兵，且不能先扣款
    st = _setup(balances={(100, 'warfunds'): 10000})
    _mk_at(st, 500, template=501, sold_free=True)

    async def _stock_boom(*_args, **_kwargs):
        raise RuntimeError('stock unavailable')

    st.list_war_faction_resources = _stock_boom
    _w, f = await _resupply(500)
    check('C15 库存读取失败 → 107/不扣款/不发兵',
          f.get(2) == war_campaign.RC_COMMANDNODE_WAR_INSTANCE_NOT_FOUND
          and st.balance(100) == 10000 and not st.spend_log
          and st.ats[500]['resources'] == {}, str(f))

    # C16 兼容存储的入队失败补偿：钱、库存、AT 必须全部恢复
    st = _setup(balances={(100, 'warfunds'): 10000})
    st.plentiful = False
    st.stock[(1, 1, 1)] = 5
    _mk_at(st, 500, template=501, sold_free=True)

    async def _queue_boom(*_args, **_kwargs):
        raise RuntimeError('queue unavailable')

    st.enqueue_war_supply = _queue_boom
    _w, f = await _resupply(500)
    check('C16 入队失败 → 补偿退款/还库存/还原AT',
          f.get(2) == war_campaign.RC_COMMANDNODE_WAR_INSTANCE_NOT_FOUND
          and st.balance(100) == 10000
          and st.stock[(1, 1, 1)] == 5
          and st.ats[500]['resources'] == {}, str(f))

    # C17 折扣必须校验目标/活动并按客户端“先乘后取整”计价
    old_discounts = war_at._STORE_DISCOUNT_MAP
    old_campaigns = war_at._STORE_CAMPAIGN_MAP
    try:
        war_at._STORE_DISCOUNT_MAP = {77: {
            'object_type': 'reinforceSoldier', 'object_id': 501,
            'currency': 2, 'discount_value': 25.0, 'campaign_id': 88}}
        war_at._STORE_CAMPAIGN_MAP = {88: {
            'active_from': '20200101000000',
            'active_to': '20990101000000'}}
        code, amount = war_at._validated_resupply_price(
            9001, 77, 'reinforceSoldier', 501, 2, 12)
        wrong, _ = war_at._validated_resupply_price(
            9001, 77, 'reinforceSoldier', 999, 2, 12)
        absent, _ = war_at._validated_resupply_price(
            9001, 78, 'reinforceSoldier', 501, 2, 12)
    finally:
        war_at._STORE_DISCOUNT_MAP = old_discounts
        war_at._STORE_CAMPAIGN_MAP = old_campaigns
    check('C17 折扣校验（25%=900/目标不符252/未知249）',
          code == 0 and amount == 900
          and wrong == war_campaign.RC_STORE_DISCOUNT_MISMATCH
          and absent == war_campaign.RC_STORE_DISCOUNT_NOT_FOUND)


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

    # F6 消耗行必须使用各资源类别自己的窗口，不可复用阵营总窗口
    st = _setup()
    await war_campaign.ensure_war(st)
    _mk_at(st, 500, template=501)
    _mk_at(st, 501, template=502)
    await st.enqueue_war_supply(1, 1, 500, {1: 12})
    await st.enqueue_war_supply(1, 1, 501, {5: 20})
    await st.enqueue_war_supply(1, 1, 500, {1: 4})
    windows = await war_campaign.supply_windows(st)
    check('F6 队列窗口按资源分类（总1..4/cat1=1..4/cat5=2..3）',
          windows[1]['overall'] == (1, 4)
          and windows[1]['categories'][1] == (1, 4)
          and windows[1]['categories'][5] == (2, 3), str(windows))


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

    # G6 死亡 AT 清队同样必须向客户端推 item/ticket DELETE
    st = _setup()
    st.plentiful = False
    st.stock[(1, 1, 1)] = 20
    _mk_at(st, 500, template=501, state=3)
    await st.enqueue_war_supply(1, 1, 500, {1: 12})
    sess_obj = SimpleNamespace(resource_view_cmd=555, writer=FakeWriter())
    state_mod.FLASH_SESSIONS['g6'] = sess_obj
    try:
        await war_campaign.supply_queue_tick(st)
    finally:
        state_mod.FLASH_SESSIONS.pop('g6', None)
    check('G6 死亡AT清队也推 DELETE×2',
          bytes(sess_obj.writer.buf).count(b'delete') == 2
          and not st.supply_queue)

    # G7 DB-free 回退：无战争 → ([], [])；push 无会话不炸
    st = _setup()
    st.wars.clear()
    items, tickets = await war_campaign.supply_queue_table_rows(st)
    check('G7 无战争回退（([], [])/push 静默）',
          items == [] and tickets == [])


# ======================================================== H 补给队列操作

async def section_h():
    # H1 退出队列：删除该 AT 全部类别，并按尚未交付的 paid_amount 退款
    st = _setup(balances={(100, 'warfunds'): 5000})
    await war_campaign.ensure_war(st)
    _mk_at(st, 500, template=501)
    await st.enqueue_war_supply(
        1, 1, 500, {1: 7, 5: 2}, metadata={'per_category': {
            1: {'payment_currency': 2, 'paid_amount': 700},
            5: {'payment_currency': 2, 'paid_amount': 500}}})
    _w, f = await _leave_supply_queue(500)
    check('H1 退出补给队列 → 删除全部行并退款1200',
          f.get(2) == war_campaign.RC_OK
          and not st.supply_queue and st.balance(100) == 6200, str(f))

    # H2/H3 队列重排：同一 AT 的非连续类别仍作为一个整体，置顶后顺序稳定
    st = _setup()
    await war_campaign.ensure_war(st)
    for node in (500, 501, 502):
        _mk_at(st, node, template=501)
    await st.enqueue_war_supply(1, 1, 500, {1: 1})
    await st.enqueue_war_supply(1, 1, 501, {1: 1})
    await st.enqueue_war_supply(1, 1, 500, {5: 1})
    await st.enqueue_war_supply(1, 1, 502, {1: 1})
    _w, f = await _reorder_supply_queue(502, 2)
    ordered = [r['command_node_id'] for r in sorted(
        st.supply_queue, key=lambda row: row['queue_number'])]
    check('H2 置顶重排 → 目标AT到首位/同AT类别保持成组',
          f.get(2) == war_campaign.RC_OK
          and ordered == [502, 500, 500, 501], str(ordered))
    _w, f = await _reorder_supply_queue(502, 0)
    check('H3 队首继续上移 → 112 last_in_queue',
          f.get(2) == war_campaign.RC_COMMANDNODE_LAST_IN_QUEUE, str(f))


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

    # D1b 持有上限不计 reserve 行（2026-09-11 玩家报全员购买回
    # commandnode_limit_reached(114) 根因：补给列 spawn_supply_column 与
    # 空降运输机队 split_aircraft_back 同为玩家名下 reserve 模板行，
    # 计入 MM_WAR_AT_MAX 后活跃玩家名下行数很快 ≥10 → 购买全拒）
    st = _setup()
    for i in range(9):
        _mk_at(st, 600 + i, template=501)
    for i in range(10):
        _mk_at(st, 700 + i, template=504)   # 补给列/运输机队（reserve）
    w, f = await _create(name='Bravo', abbr='BRV')
    check('D1b 上限不计 reserve（9 真实 AT+10 reserve → 购买成功）',
          f.get(1) == 1 and len(st.ats) == 20, str(f))

    # D1c 上限仍对真实 AT 生效：MM_WAR_AT_MAX（默认 100）个真实 AT → 114
    st = _setup()
    for i in range(mm_config.WAR_AT_MAX):
        _mk_at(st, 600 + i, template=501)
    w, f = await _create(name='Charlie', abbr='CHL')
    check('D1c 真实 AT 顶满上限 → 114（limit 仍生效）',
          f.get(1) == war_campaign.RC_COMMANDNODE_LIMIT_REACHED, str(f))

    # D2 deploy：官服随机落位（#4/#5：主城10 + 邻接己方非交战城11 池）
    # + 部署即补编（§10.2 #4/#5：新 AT 首次免费满编置位，余额不动）
    st = _setup()
    _mk_at(st, 500, template=501)
    w, f = await _deploy(500)
    inst = [r for r in _rows(w, 'CommandNodeWarInstance')
            if r.get(1) == 500]
    check('D2 部署+首补免费（响应1/满编{1:12}/free置位/余额不动/bf∈{10,11}）',
          f.get(1) == 1 and st.ats[500]['is_deployed'] is True
          and st.ats[500]['map_entity_id'] in (10, 11)
          and st.ats[500]['map_entity_type'] == 1
          and st.ats[500]['resources'] == {'1': 12}
          and st.ats[500]['has_gotten_soldier_free'] is True
          and st.balance(100) == 3000 and not st.spend_log
          and inst and inst[-1].get(5) in (10, 11)
          and inst[-1].get(10) == 1,
          str(f))

    # D2b 随机落位池覆盖：固定种子多次部署，主城/邻城均出现
    random.seed(7)
    spots = set()
    for i in range(40):
        st = _setup()
        _mk_at(st, 500 + i, template=501)
        await _deploy(500 + i)
        spots.add(st.ats[500 + i]['map_entity_id'])
    check('D2b 落位随机池 {10,11} 全覆盖', spots == {10, 11}, str(spots))

    # D2c 主城交战 → 221（官服 deploy_location_not_found），AT 不部署
    st = _setup()
    st.battles.append({'war_id': 1, 'battlefield_id': 10})
    _mk_at(st, 500, template=501)
    w, f = await _deploy(500)
    check('D2c 主城交战 → 221 拒单',
          f.get(1) == war_campaign.RC_DEPLOY_LOCATION_NOT_FOUND
          and not st.ats[500]['is_deployed'], str(f))

    # D2d 周边无己方非交战城（11 中立）→ 落主城（2026-09-08 修正：
    # 官服开局仅首都 owned，周边全中立必须可部署——war1「就近原则」
    # 是出生点分布池而非部署门槛）
    # （ensure_war 在 deploy 时才建 war：owners 须经 initial_city_owners 注入）
    st = _setup()
    _orig_ico = war_campaign.lifecycle.initial_city_owners
    war_campaign.lifecycle.initial_city_owners = lambda: {10: 1, 11: 0}
    _mk_at(st, 500, template=501)
    w, f = await _deploy(500)
    war_campaign.lifecycle.initial_city_owners = _orig_ico
    check('D2d 周边中立 → 落主城部署成功',
          f.get(1) == 1 and st.ats[500]['is_deployed'] is True
          and st.ats[500]['map_entity_id'] == 10, str(f))

    # D2e 唯一邻城交战 → 候选池仅剩主城 → 落主城（同 D2d 修正）
    st = _setup()
    st.battles.append({'war_id': 1, 'battlefield_id': 11})
    _mk_at(st, 500, template=501)
    w, f = await _deploy(500)
    check('D2e 唯一邻城交战 → 落主城部署成功',
          f.get(1) == 1 and st.ats[500]['map_entity_id'] == 10, str(f))

    # D2f 部署推送行序（2026-09-08 修"主城两个一样图标"；2026-09-14 修
    # "部署成功后卡部署界面"）：WarInstance 行 DELETE(旧实例) →
    # ADD(isDeployed=1) → 【末尾】同值 set(→客户端 UPDATE)。ADD 走官服
    # 口径——客户端 ADD 分支 addAssaultTeamToMap 尾部有
    # checkLocationForStack 堆叠检查，UPDATE 分支 handleDeployChanges
    # 没有 → 若首推走 UPDATE 主城多 AT 图标不进 MultiMarker 叠放。
    # 末尾补推的同值 set 行到达时行已存在 → 客户端派发 UPDATE 事件：
    # 部署窗口 AssaultTeamReinforcementWindowMediator 收 ok 响应时无
    # 动作（仅 226 直接收窗），关窗只认 UPDATE → 没有这条就永久卡在
    # 禁用的部署界面；地图层 hasChanged* 按新旧行比较，同值无副作用。
    # 补推须在 armyresource 之后（窗口用 armyresource 重算补编价再决定
    # 关窗还是走自动补编链）。Morale/armyresource 照推、Definition
    # 不推（部署不改定义，且 DELETE 后 _warInstancesByDefinitionId 映射
    # 已清，客户端 definitionUpdated 会跳过）
    st = _setup()
    _mk_at(st, 500, template=501)
    w, f = await _deploy(500)
    events = []
    for c in _chunks(w.buf):
        if not c.subs or c.subs[0].data != b'KeyValueChangeSet':
            continue
        act = c.subs[0].subs[0]
        if act.data == b'delete' and act.subs:
            events.append(('del', act.subs[0].data))
        elif act.data == b'set' and act.subs:
            events.append(('set', act.subs[0].data))
    dels = _dels(w, 'CommandNodeWarInstance')
    inst = [r for r in _rows(w, 'CommandNodeWarInstance') if r.get(1) == 500]
    morale = _rows(w, 'CommandNodeMorale')
    defn = _rows(w, 'CommandNodeDefinition')
    seq = [e for e in events if e[1] == b'CommandNodeWarInstance']
    last_ar = max((i for i, e in enumerate(events)
                   if e[1] == b'armyresource'), default=-1)
    last_wi = max((i for i, e in enumerate(events)
                   if e[1] == b'CommandNodeWarInstance'), default=-1)
    check('D2f 部署推送 DELETE→ADD→末尾同值UPDATE 行序（解卡窗/Morale 推/Definition 不推）',
          f.get(1) == 1 and dels == [500]
          and inst and inst[-1].get(10) == 1
          and seq == [('del', b'CommandNodeWarInstance'),
                      ('set', b'CommandNodeWarInstance'),
                      ('set', b'CommandNodeWarInstance')]
          and len(inst) == 2 and inst[0] == inst[-1]     # 两条 set 同值
          and last_wi > last_ar >= 0                     # UPDATE 在 armyresource 后
          and morale and not defn,
          f'seq={seq} morale={len(morale)} defn={len(defn)} '
          f'last_wi={last_wi} last_ar={last_ar}')

    # D3 重复部署 → 150（先成功部署一次）
    st = _setup()
    _mk_at(st, 500, template=501)
    await _deploy(500)
    w, f = await _deploy(500)
    check('D3 重复部署 → 150',
          f.get(1) == war_campaign.RC_COMMANDNODE_ALREADY_DEPLOYED, str(f))

    # D6 占领敌方主城后部署（2026-09-08：玩家报"新占领的城市无法部署"
    # 排查场景）：wardb.capitals 含敌方主城 20 (default_faction=2 GE)，
    # initial_city_owners 给 20 出厂属 GE。先 _deploy 一次触发 ensure_war
    # 写入出厂归属（init_war_cities），再 set_city_owner(20,1) 模拟
    # 战斗结算占领，第二次 _deploy(cap=20) 须落位到 20（验证
    # owned_capital_bfs 动态归属判定生效，客户端 f4=新占领城可部署）
    _orig_caps = wardb.capitals
    _orig_ico = war_campaign.lifecycle.initial_city_owners
    _orig_fcb = war_campaign._faction_capital_bf
    wardb.capitals = lambda: [
        {'battlefield_id': 10, 'default_faction': 1, 'victory_points': 1},
        {'battlefield_id': 20, 'default_faction': 2, 'victory_points': 1}]
    war_campaign.lifecycle.initial_city_owners = lambda: {10: 1, 11: 1, 20: 2}
    war_campaign._faction_capital_bf = lambda: {1: 10, 2: 20}

    st = _setup()
    _mk_at(st, 500, template=501)
    await _deploy(500)              # 触发 ensure_war 写入出厂 city_owners
    # 战斗结算后 US 占领 20（同 _capture_city_events 调 set_city_owner）
    await st.set_city_owner(1, 20, 1)
    _mk_at(st, 501, template=501)
    w, f = await _deploy(501, cap=20)
    check('D6 占领敌方主城后部署（f4=20 → 落位 20/响应1）',
          f.get(1) == 1 and st.ats[501]['is_deployed'] is True
          and st.ats[501]['map_entity_id'] == 20, str(f))

    # D6b 占领敌方主城但不传 f4 → 回退集合内最小 id（10），部署仍成功
    # （客户端虽不应走此分支——验证服务端容错不拒单）
    st = _setup()
    _mk_at(st, 500, template=501)
    await _deploy(500)
    await st.set_city_owner(1, 20, 1)
    _mk_at(st, 501, template=501)
    w, f = await _deploy(501, cap=0)
    check('D6b 占领敌方主城无 f4 → 回退最小 id 仍部署成功',
          f.get(1) == 1 and st.ats[501]['is_deployed'] is True
          and st.ats[501]['map_entity_id'] == 10, str(f))

    # D6c 占领敌方主城但同时该城交战 → 221（捕获刚占领即被反攻场景）
    st = _setup()
    _mk_at(st, 500, template=501)
    await _deploy(500)
    await st.set_city_owner(1, 20, 1)
    st.battles.append({'war_id': 1, 'battlefield_id': 20})
    _mk_at(st, 501, template=501)
    w, f = await _deploy(501, cap=20)
    check('D6c 占领敌方主城但城战 → 221（占领后立刻被反攻拒单）',
          f.get(1) == war_campaign.RC_DEPLOY_LOCATION_NOT_FOUND
          and not st.ats[501]['is_deployed'], str(f))

    # D7 占领中立主城后部署（capital 表里 default_faction=0 的中立主城，
    # initial_city_owners 过滤掉开局不写归属，占领后 set_city_owner
    # 才写入）。D6 系列只覆盖敌方主城（default_faction=2）；
    # 真实场景里中立主城可能没在 city_owners 字典里 → 测试覆盖盲区
    # 新增 fixture：30 中立主城 default_faction=0
    _orig_caps_neutral = wardb.capitals
    wardb.capitals = lambda: [
        {'battlefield_id': 10, 'default_faction': 1, 'victory_points': 1},
        {'battlefield_id': 20, 'default_faction': 2, 'victory_points': 1},
        {'battlefield_id': 30, 'default_faction': 0, 'victory_points': 1}]
    st = _setup()
    _mk_at(st, 500, template=501)
    await _deploy(500)
    # 中立主城 30 不在 initial_city_owners（default_faction=0 被过滤）
    # → city_owners 字典里没有 30 → 占领前 owners.get(30)=None
    await st.set_city_owner(1, 30, 1)   # US 占领中立主城 30
    _mk_at(st, 501, template=501)
    w, f = await _deploy(501, cap=30)
    check('D7 占领中立主城后部署（f4=30 → 落位 30/响应1）',
          f.get(1) == 1 and st.ats[501]['is_deployed'] is True
          and st.ats[501]['map_entity_id'] == 30, str(f))
    wardb.capitals = _orig_caps_neutral

    wardb.capitals = _orig_caps
    war_campaign.lifecycle.initial_city_owners = _orig_ico
    war_campaign._faction_capital_bf = _orig_fcb

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
    check('D5 部署缺库存 → 226（入队(1,12)/扣1200/编制不动/AT行照推/bf∈{10,11}）',
          f.get(1) == war_campaign.RC_COMMANDNODE_QUEUED_FOR_RESOURCES
          and [(r['category_id'], r['count']) for r in st.supply_queue]
          == [(1, 12)]
          and st.spend_log == [(100, 'warfunds', 1200)]
          and st.ats[500]['resources'] == {}
          and st.ats[500]['is_deployed'] is True
          and inst and inst[-1].get(5) in (10, 11), str(f))


# ================================================= E transport/升级口径

async def _transport(node, target=30, ttype='battlefield', override_ap=0,
                     override_move=False):
    w = FakeWriter()
    req = (pb_int64(1, node) + pb_int64(2, target) + pb_string(3, ttype))
    if override_ap:
        req += pb_int64(4, override_ap)
    if override_move:
        req += pb_bool(5, True)
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

    # E4 f11 互转（落地变体 → 空中原型）在己方机场 → 空中重整
    # （2026-09-10 修复主诉"伞兵部队没办法补给伞兵运输机"：空降到达
    # 转 *_on_ground 后编制只含 cat4 → resupply 恒 103 已满编；官服
    # 恢复通道 = f11 反向 convert_to 互转 108⇄114/311⇄312）
    st = _setup()
    await war_campaign.ensure_war(st)
    st.city_owners[1] = {10: 1, 11: 1}
    _mk_at(st, 610, template=509, deployed=True, map_entity_id=11,
           resources={4: 36, 10: 5}, veh_free=True)
    w, f = await _upgrade(610, tgt=508)
    ab_rows = _rows(w, 'air_commandnode_base')
    check('E4 f11 己方机场互转 → 空中原型 508 + airbase_bf=11 + airbase 行',
          f.get(1) == 1
          and st.ats[610]['template_id'] == 508
          and st.ats[610]['is_deployed']
          and st.ats[610].get('airbase_bf') == 11
          and war_campaign.is_radius_unit(st.ats[610])
          and st.ats[610]['resources'] == {4: 36, 10: 5}
          and len(ab_rows) == 1 and ab_rows[0].get(3) == 11
          and st.spend_log == [(100, 'warfunds', 500)], str(f))

    # E5 转回空中原型后 resupply → 运输机 cat10 缺口可补（修复核心：
    # 509 编制无 cat10 恒 103；508 编制 {4,10} → 缺 4 架付费补齐；
    # 即时交付回写 resources 为 str 键）
    w, f = await _resupply(610, sp=9001, vp=9002)
    check('E5 f11 转回后 resupply → 运输机补至 9 架（付费 4×250）',
          f.get(2) == 1
          and st.ats[610]['resources'] == {'4': 36, '10': 9}
          and st.spend_log[-1] == (100, 'warfunds', 1000), str(f))

    # E5b 金币计价（2026-09-10 玩家实报"补给不收钱"实为点中金币价
    # 按钮 paymentCurrency=3=Gold）：单价 1.2222/架按客户端 PriceObject
    # finalFractionCost 口径 floor(单价×数量) 先乘后取整——旧实现逐架
    # int()=1×9=9 少收且与客户端显示总价 10 不一致
    st = _setup()
    await war_campaign.ensure_war(st)
    st.city_owners[1] = {10: 1, 11: 1}
    _mk_at(st, 615, template=509, deployed=True, map_entity_id=11,
           resources={4: 36}, veh_free=True)
    st.ats[615]['airbase_bf'] = 11
    st.balances[(100, 'gold')] = 100
    w, f = await _resupply(615, sp=9001, vp=9003, cur=3)
    check('E5b 金币补机 9 架 → floor(1.2222×9)=10 金币（先乘后取整）',
          f.get(2) == 1
          and st.ats[615]['resources'] == {'4': 36, '10': 9}
          and st.spend_log == [(100, 'gold', 10)]
          and st.balance(100, 'gold') == 90, str(f))

    # E6 f11 敌后拒单：敌占城无法登机（不扣款/模板不变）
    st = _setup()
    await war_campaign.ensure_war(st)
    st.city_owners[1] = {10: 1, 15: 2}
    _mk_at(st, 611, template=509, deployed=True, map_entity_id=15)
    w, f = await _upgrade(611, tgt=508)
    check('E6 f11 敌后互转 → 拒单 106（模板不动/不扣款）',
          f.get(1) == war_campaign.RC_COMMANDNODE_TEMPLATE_NOT_FOUND
          and st.ats[611]['template_id'] == 509
          and not st.spend_log, str(f))

    # E7 f11 己方普通城（非机场）→ 自动空运回最近己方机场
    # （镜像撤退方案2；区别于 E4 已在机场 → 就地重整、E6 敌后 → 拒单）
    st = _setup()
    await war_campaign.ensure_war(st)
    st.city_owners[1] = {10: 1, 11: 1, 12: 1, 13: 1}
    _mk_at(st, 612, template=509, deployed=True, map_entity_id=13)
    w, f = await _upgrade(612, tgt=508)
    air_rows = _rows(w, 'air_transport')
    ab_rows = _rows(w, 'air_commandnode_base')
    check('E7 f11 普通城互转 → 自动空运回最近机场 12（在途+air_transport+airbase 12）',
          f.get(1) == 1
          and st.ats[612]['template_id'] == 508
          and st.ats[612]['state'] == 1
          and st.ats[612].get('airbase_bf') == 12
          and len(air_rows) == 1
          and air_rows[0].get(3) == 13 and air_rows[0].get(5) == 12
          and len(ab_rows) == 1 and ab_rows[0].get(3) == 12, str(f))

    # E8 满编落地变体在己方机场点"补给"（客户端无 f11 按钮、只带
    # vehiclePricing 的实际入口）→ 空中重整：转回空中原型而非 103
    # （2026-09-10 玩家实报"点了补给还是拒绝"：AT 24 满编 {4:36,10:9}
    # 停机场点补给被 103 拒绝）
    st = _setup()
    await war_campaign.ensure_war(st)
    st.city_owners[1] = {10: 1, 11: 1}
    _mk_at(st, 613, template=509, deployed=True, map_entity_id=11,
           resources={4: 36, 10: 9}, veh_free=True)
    w, f = await _resupply(613, sp=9001, vp=9002)
    ab_rows = _rows(w, 'air_commandnode_base')
    check('E8 满编落地变体机场补给 → 空中重整（508+airbase_bf=11+airbase 行/不扣款）',
          f.get(2) == 1
          and st.ats[613]['template_id'] == 508
          and st.ats[613].get('airbase_bf') == 11
          and war_campaign.is_radius_unit(st.ats[613])
          and st.ats[613]['resources'] == {4: 36, 10: 9}
          and not st.spend_log
          and len(ab_rows) == 1 and ab_rows[0].get(3) == 11, str(f))

    # E9 缺机落地变体在己方机场点"补给" → 补齐 cat10（按 vehiclePricing
    # 计价 4×250）+ 补满后自动转回空中原型
    st = _setup()
    await war_campaign.ensure_war(st)
    st.city_owners[1] = {10: 1, 11: 1}
    _mk_at(st, 614, template=509, deployed=True, map_entity_id=11,
           resources={4: 36, 10: 5}, veh_free=True)
    w, f = await _resupply(614, sp=9001, vp=9002)
    ab_rows = _rows(w, 'air_commandnode_base')
    check('E9 缺机落地变体机场补给 → 补 4 架（vehiclePricing 1000）+ 转回 508',
          f.get(2) == 1
          and st.ats[614]['template_id'] == 508
          and st.ats[614]['resources'] == {'4': 36, '10': 9}
          and st.ats[614].get('airbase_bf') == 11
          and st.spend_log == [(100, 'warfunds', 1000)]
          and len(ab_rows) == 1 and ab_rows[0].get(3) == 11, str(f))

    # E10 落地变体在普通城（非机场）补给 → 满编 ok no-op（C5 同口径；
    # 须先回己方机场才能登机；缺口不算/不扣款/模板不动）
    st = _setup()
    await war_campaign.ensure_war(st)
    st.city_owners[1] = {10: 1, 11: 1, 12: 1, 13: 1}
    _mk_at(st, 615, template=509, deployed=True, map_entity_id=13,
           resources={4: 36, 10: 5}, veh_free=True)
    w, f = await _resupply(615, sp=9001, vp=9002)
    check('E10 落地变体普通城补给 → ok no-op（模板不动/不扣款/资源不动）',
          f.get(2) == 1
          and st.ats[615]['template_id'] == 509
          and st.ats[615]['resources'] == {4: 36, 10: 5}
          and not st.spend_log, str(f))

    # E11 合并免费登机（官服 "re-use old transport planes"）：落地
    # 伞兵机场补给 → 先免费并入停驻同机场的旧机队（R3c 拆分产物），
    # 补满转回空中原型，不扣款、不消耗首免、机队取尽软删
    st = _setup()
    await war_campaign.ensure_war(st)
    st.city_owners[1] = {10: 1, 11: 1}
    _mk_at(st, 616, template=509, deployed=True, map_entity_id=11,
           resources={4: 36}, veh_free=True)
    _mk_at(st, 617, template=504, deployed=True, map_entity_id=11,
           resources={10: 9})
    st.ats[617]['airbase_bf'] = 11
    w, f = await _resupply(616, sp=9001, vp=9002)
    check('E11 机场补给 → 免费合并旧机队 9 架 + 转回 508（不扣款/机队软删）',
          f.get(2) == 1
          and st.ats[616]['template_id'] == 508
          and st.ats[616]['resources'] == {'4': 36, '10': 9}
          and st.ats[616].get('airbase_bf') == 11
          and not st.spend_log
          and not st.adjust_log
          and st.ats[617]['state'] == 3, str(f))

    # E12 部分合并：旧机队只 5 架 → 免费 5 + 计价补 4（vehiclePricing
    # 4×250）+ 库存交付，转回 508；机队取尽软删
    st = _setup()
    await war_campaign.ensure_war(st)
    st.city_owners[1] = {10: 1, 11: 1}
    _mk_at(st, 618, template=509, deployed=True, map_entity_id=11,
           resources={4: 36}, veh_free=True)
    _mk_at(st, 619, template=504, deployed=True, map_entity_id=11,
           resources={10: 5})
    st.ats[619]['airbase_bf'] = 11
    w, f = await _resupply(618, sp=9001, vp=9002)
    check('E12 部分合并 → 旧机 5 免费 + 4 架计价 1000 → 转 508 + 机队软删',
          f.get(2) == 1
          and st.ats[618]['template_id'] == 508
          and st.ats[618]['resources'] == {'4': 36, '10': 9}
          and st.spend_log == [(100, 'warfunds', 1000)]
          and st.adjust_log == [(1, 1, 10, -4)]
          and st.ats[619]['state'] == 3
          and len(st.ats[618]['resources']) == 2,
          f'f={f} tpl={st.ats[618]["template_id"]} res={st.ats[618]["resources"]} '
          f'spend={st.spend_log} adjust={st.adjust_log} p619={st.ats[619]["state"]}')


# ============================================== R 空中半径单位（§10.2 #6/#7）

def section_r_unit():
    """R0 半径规则纯单元（城市图：10(0,0) 11(100,0)机 12(800,0)机
    13(800,100) 14(2000,0) 15(850,0)敌机 16(450,0) 17(1200,0)机）。"""
    at505 = {'template_id': 505, 'faction': 1}      # 战斗机 950/2850

    ok, _ = war_campaign.check_radius_move(at505, 10, 16, {16: 0}, set())
    check('R0a 攻击半径内未开战非机场城 → 拒（战斗机仅限机场间/已开战）',
          not ok)
    ok, atk = war_campaign.check_radius_move(at505, 10, 16, {16: 0}, {16})
    check('R0a2 攻击半径内已开战城（16 ∈ battling）→ (True,True)', ok and atk)

    ok, atk = war_campaign.check_radius_move(at505, 10, 12, {12: 1}, set())
    check('R0b 运输半径内己方非交战机场（800×2∈(950,2850]）→ (True,False)',
          ok and not atk)

    ok, _ = war_campaign.check_radius_move(at505, 10, 13, {13: 1}, set())
    check('R0c 运输半径内非机场城 → 拒', not ok)

    ok, _ = war_campaign.check_radius_move(at505, 10, 15, {15: 2}, set())
    check('R0d 运输半径内敌方机场 → 拒', not ok)

    ok, _ = war_campaign.check_radius_move(at505, 10, 12, {12: 1}, {12})
    check('R0e 交战机场 → 拒', not ok)

    at504 = {'template_id': 504, 'faction': 1}      # 补给空运队（reserve）
    ok, atk = war_campaign.check_radius_move(at504, 10, 12, {12: 1}, {12})
    check('R0e2 reserve 补给队交战机场例外 → (True,False)', ok and not atk)

    ok, _ = war_campaign.check_radius_move(at505, 10, 14, {14: 1}, set())
    check('R0f 超运输半径（2000×2>2850）→ 拒', not ok)

    ok, atk = war_campaign.check_radius_move(at505, 999, 16, {}, set())
    check('R0g 当前位无坐标回退全允许 (True,True)', ok and atk)
    ok, _ = war_campaign.check_radius_move(at505, 10, 999, {}, set())
    check('R0g2 目标城不在战图 → 拒（对齐地面不可达）', not ok)

    check('R0h 单位族判定（飞机/伞兵/落地变体非radius/地面AT）',
          war_campaign.is_radius_unit({'template_id': 505})
          and war_campaign.is_aircraft({'template_id': 505})
          and not war_campaign.is_paradrop_unit({'template_id': 505})
          and war_campaign.is_paradrop_unit({'template_id': 506})
          and not war_campaign.is_aircraft({'template_id': 506})
          and not war_campaign.is_radius_unit({'template_id': 507})
          and not war_campaign.is_radius_unit({'template_id': 501}))

    check('R0i 机场判定（204/205）/最近机场',
          war_campaign.is_airfield(11) and war_campaign.is_airfield(15)
          and not war_campaign.is_airfield(10)
          and war_campaign.nearest_airfield(10) == 11)

    row = pb_parse(war_campaign.msg_air_commandnode_base_row(
        {'id': 500}, 1, 12))
    check('R0j airbase 行 f1-f6（f5 String battlefield）',
          row.get(1) == 500 and row.get(2) == 500 and row.get(3) == 12
          and row.get(4) == 12 and row.get(5) == b'battlefield'
          and row.get(6) == 1, str(row))

    at506 = {'template_id': 506, 'faction': 1}      # 伞兵 1850/3600
    ok, atk = war_campaign.check_radius_move(at506, 11, 12, {12: 1}, set())
    check('R0k 伞兵攻击半径内己方非交战机场 → 换基地 (True,False)',
          ok and not atk)
    ok, atk = war_campaign.check_radius_move(at506, 11, 12, {12: 1}, {12})
    check('R0k2 伞兵攻击半径内己方交战机场 → 防御空降 (True,True)',
          ok and atk)
    ok, atk = war_campaign.check_radius_move(at506, 11, 15, {15: 2}, set())
    check('R0k3 伞兵攻击半径内敌方机场 → 空降 (True,True)', ok and atk)
    ok, atk = war_campaign.check_radius_move(at506, 11, 13, {13: 1}, set())
    check('R0k4 伞兵攻击半径内己方非机场城 → 仍空降 (True,True)',
          ok and atk)


async def section_r():
    section_r_unit()

    # ---- R1 部署落机场（#7：飞机/伞兵只出生在机场）----
    # R1a 候选池 {10,11} ∩ 机场 = {11} → 落 11 + airbase 行推送
    st = _setup()
    _mk_at(st, 500, template=505)
    w, f = await _deploy(500)
    ab = _rows(w, 'air_commandnode_base')
    check('R1a 飞机部署落机场（11）+ airbase 行（f3=11）+ airbase_bf=11',
          f.get(1) == 1 and st.ats[500]['map_entity_id'] == 11
          and st.ats[500]['airbase_bf'] == 11
          and ab and ab[-1].get(3) == 11 and ab[-1].get(2) == 500, str(f))

    # R1b 候选池无机场 → 回退距主城最近的**己方占领**机场（2026-09-08
    # 修复：机场须先占领才能部署；18 为更近的敌方机场应被跳过，
    # 12 为己方机场 → 落 12；无己方机场 → 拒单 221）
    orig_bf, orig_af = wardb.battlefields, war_campaign.at_ops._AIRFIELD_CACHE
    wardb.battlefields = lambda: [
        {'id': 10, 'posx': 0, 'posy': 0, 'gamemap': 0},
        {'id': 11, 'posx': 100, 'posy': 0, 'gamemap': 0},
        {'id': 12, 'posx': 800, 'posy': 0, 'gamemap': 204},
        {'id': 14, 'posx': 2000, 'posy': 0, 'gamemap': 0},
        {'id': 18, 'posx': 400, 'posy': 0, 'gamemap': 205}]
    war_campaign.common._BF_POS_CACHE = None
    war_campaign.at_ops._AIRFIELD_CACHE = None
    try:
        # R1b 无己方机场（12/18 均未占领）→ 拒单 221
        st = _setup()
        _mk_at(st, 500, template=505)
        w, f = await _deploy(500)
        check('R1b 候选池无机场 + 无己方机场 → 拒单 221',
              f.get(1) == 221 and not st.ats[500]['is_deployed'], str(f))

        # R1b2 12 己方占领、18 更近但敌方 → 回退落最近的己方机场 12
        st = _setup()
        war = await war_campaign.ensure_war(st)
        st.city_owners[war['id']][12] = 1
        st.city_owners[war['id']][18] = 2
        _mk_at(st, 500, template=505)
        w, f = await _deploy(500)
        check('R1b2 回退最近己方机场（12，跳过更近敌方 18）',
              f.get(1) == 1 and st.ats[500]['map_entity_id'] == 12, str(f))
    finally:
        wardb.battlefields = orig_bf
        war_campaign.common._BF_POS_CACHE = None
        war_campaign.at_ops._AIRFIELD_CACHE = orig_af

    # R1c 伞兵同规则（airborne 统一语义）
    st = _setup()
    _mk_at(st, 500, template=506)
    w, f = await _deploy(500)
    check('R1c 伞兵部署同落机场（11）',
          f.get(1) == 1 and st.ats[500]['map_entity_id'] == 11
          and _rows(w, 'air_commandnode_base'), str(f))

    # ---- R2 半径移动（transport_commandnode 半径分支）----
    # R2a 换基地：11→12（700×2=1400∈(950,2850]，己方非交战机场）→ 21
    # （起飞即推目的地 base 行：FakeSess 注册进 FLASH_SESSIONS 收广播）
    st = _setup()
    war = await war_campaign.ensure_war(st)
    st.city_owners[1][12] = 1
    _mk_at(st, 500, template=505, deployed=True, map_entity_id=11)
    from state import FLASH_SESSIONS
    w = FakeWriter()
    sess = FakeSess()
    sess.writer = w
    FLASH_SESSIONS['t-r2a'] = sess
    try:
        req = (pb_int64(1, 500) + pb_int64(2, 12)
               + pb_string(3, 'battlefield'))
        await war_at.handle_transport_commandnode(
            sess, w, 900, b'transport_commandnode', req)
    finally:
        FLASH_SESSIONS.pop('t-r2a', None)
    f = _resp(w, 'transport_commandnode_response')
    seg0 = st.movements[1]['path'][0]
    ab_start = [r for r in _rows(w, 'air_commandnode_base')
                if r.get(3) == 12 and r.get(4) == 12]
    check('R2a 换基地 → 21 + air 单段(radius_attack=False) + state=1 不耗cat10'
          ' + 起飞即推目的地 base 行(f3=f4=12)',
          f.get(3) == war_campaign.RC_RELOCATING_TO_NEW_AIRBASE
          and st.ats[500]['state'] == 1
          and seg0['air'] is True and seg0['radius_attack'] is False
          and seg0['to_bf'] == 12
          and st.ats[500].get('resources', {}).get('10') is None
          and ab_start, str(f))

    # R2a2 攻击飞行（伞兵空降 atk=True）：base 行即时推 f3=出发机场
    # （11，airbase_bf 保持）、f4=目标城（12）——目的地文字随指令更新
    # （独立 st2，勿覆盖 R2a 的 st——R2b 复用其 movements[1]）
    st2 = _setup()
    war2 = await war_campaign.ensure_war(st2)
    st2.city_owners[1][12] = 2         # 敌占目标城（攻击半径 1850≥1400）
    _mk_at(st2, 501, template=506, deployed=True, map_entity_id=11)
    st2.ats[501]['airbase_bf'] = 11
    w = FakeWriter()
    sess = FakeSess()
    sess.writer = w
    FLASH_SESSIONS['t-r2a2'] = sess
    try:
        req = (pb_int64(1, 501) + pb_int64(2, 12)
               + pb_string(3, 'battlefield'))
        await war_at.handle_transport_commandnode(
            sess, w, 901, b'transport_commandnode', req)
    finally:
        FLASH_SESSIONS.pop('t-r2a2', None)
    war_at.STORE = st                  # _setup(st2) 重绑了全局 STORE，恢复
    f = _resp(w, 'transport_commandnode_response')
    ab_atk = [r for r in _rows(w, 'air_commandnode_base')
              if r.get(3) == 11 and r.get(4) == 12]
    seg0 = st2.movements[1]['path'][0]
    check('R2a2 攻击飞行 → atk 单段 + 起飞即推 base 行(f3=11 出发机场/'
          'f4=12 目标城)',
          seg0['radius_attack'] is True and seg0['to_bf'] == 12
          and st2.ats[501]['state'] == 1 and ab_atk,
          f"resp={_resp(w, 'transport_commandnode_response')} "
          f"seg0={seg0}")

    # R2b 到达换基地 → 落位 12 + airbase 行 set（f3=12）
    mv = st.movements[1]
    events = await war_campaign.advance_movements(
        st, war, now=mv['arrive_at'] + datetime.timedelta(seconds=1))
    ab_ev = [e for e in events if e[1] == b'air_commandnode_base']
    check('R2b 到达换基地 → AT 落位 12 + airbase 行 set + airbase_bf=12',
          st.ats[500]['map_entity_id'] == 12
          and st.ats[500]['state'] == 0
          and st.ats[500]['airbase_bf'] == 12
          and len(ab_ev) == 1 and ab_ev[0][0] == 'set'
          and pb_parse(ab_ev[0][2]).get(3) == 12)

    # R2c 战斗机未开战非机场城 → 拒单 7（用户纠正 #1：战斗机仅能在
    # 友方机场间移动，飞入非机场据点须已开战）
    w, f2c = await _transport(500, target=16)
    check('R2c 战斗机未开战非机场城 → 7',
          f2c.get(3) == war_campaign.RC_CANNOT_FIND_COMMANDNODE
          and len(st.movements) == 1, str(f2c))

    # R2c2 战斗机已开战城 → 23(join battle)；作战到达 airbase 行不变 +
    # airbase_bf 保持 12
    st.battles.append({'war_id': 1, 'battlefield_id': 16, 'battle_type': 1,
                       'id': 1, 'factions': [{'at_id': 999, 'faction': 2}]})
    w, f = await _transport(500, target=16)
    mv = st.movements[2]
    events = await war_campaign.advance_movements(
        st, war, now=mv['arrive_at'] + datetime.timedelta(seconds=1))
    check('R2c2 战斗机已开战城 → 23；到达 airbase 行不变 + airbase_bf=12',
          f.get(3) == war_campaign.RC_TRANSPORTING_TO_JOIN_BATTLE
          and st.ats[500]['map_entity_id'] == 16
          and st.ats[500]['airbase_bf'] == 12
          and not [e for e in events
                   if e[1] == b'air_commandnode_base'], str(f))

    # R2d-f 拒单：运输半径内非机场 / 超运输半径 / override_ap 出兵点
    st = _setup()
    war = await war_campaign.ensure_war(st)
    for nid, tgt, oap, label in (
            (531, 13, 0, '运输半径内非机场城'),
            (532, 14, 0, '超运输半径城'),
            (533, 16, 102, 'airborne 落出兵点'),
            (534, 30, 0, '无坐标目标')):
        _mk_at(st, nid, template=505, deployed=True, map_entity_id=10)
    w1, f1 = await _transport(531, target=13)
    w2, f2 = await _transport(532, target=14)
    w3, f3 = await _transport(533, target=16, override_ap=102)
    w4, f4 = await _transport(534, target=30)
    check('R2d 半径拒单族 → 7（非机场/超半径/出兵点/无坐标）',
          f1.get(3) == 7 and f2.get(3) == 7 and f3.get(3) == 7
          and f4.get(3) == 7
          and not st.movements
          and all(st.ats[n]['state'] == 0
                  for n in (531, 532, 533, 534)),
          f'{f1.get(3)}/{f2.get(3)}/{f3.get(3)}/{f4.get(3)}')

    # R2g 敌方机场（owners 15=2）→ 拒
    st = _setup()
    war = await war_campaign.ensure_war(st)
    st.city_owners[1][15] = 2
    _mk_at(st, 540, template=505, deployed=True, map_entity_id=10)
    w, f = await _transport(540, target=15)
    check('R2g 运输半径内敌方机场 → 7', f.get(3) == 7, str(f))

    # R2h 伞兵攻击半径内拖到己方**非交战**机场 → 换基地（用户纠正 #2：
    # 不空降自家机场、机队随队整体转场不拆分返航）：11→12（700×2=
    # 1400≤1850 攻击半径）→ 21；到达保持 506 airborne + airbase=12，
    # 无拆分机队 AT、无落地变体
    st = _setup()
    war = await war_campaign.ensure_war(st)
    st.city_owners[1][12] = 1
    _mk_at(st, 541, template=506, deployed=True, map_entity_id=11,
           resources={4: 36, 10: 9})
    st.ats[541]['airbase_bf'] = 11
    w, f = await _transport(541, target=12)
    mv = next(m for m in st.movements.values()
              if m['commandnode_id'] == 541)
    seg0 = war_campaign.movement_segments(mv)[0]
    check('R2h 伞兵→己方机场（攻击半径内）换基地 → 21 + 非 radius_attack',
          f.get(3) == war_campaign.RC_RELOCATING_TO_NEW_AIRBASE
          and st.ats[541]['state'] == 1
          and seg0['air'] is True and seg0['radius_attack'] is False,
          str(f))
    events = await war_campaign.advance_movements(
        st, war, now=mv['arrive_at'] + datetime.timedelta(seconds=1))
    split_ats = [a for a in st.ats.values()
                 if a.get('parent_command_node_id') == 541]
    check('R2h2 到达 → 整队转场 12（506 airborne + airbase=12，无拆分）',
          st.ats[541]['state'] == 0
          and st.ats[541]['template_id'] == 506
          and st.ats[541]['map_entity_id'] == 12
          and st.ats[541]['airbase_bf'] == 12
          and st.ats[541]['resources'].get(10) == 9
          and not split_ats,
          f'{st.ats[541]} split={split_ats}')

    # ---- R2i 敌城空降到达（★2026-09-11 敌区交战时序修复）----
    # 目标有敌驻军 → 不拆分：保持空中原型编制（cat4+cat10 成对入战
    # ——战斗内伞兵从运输机跳伞出生）state=2 成局，运输机在会战结算
    # 时才返航（玩家报"敌城空降运输机秒走"；结算时序回归见 K4/K5）
    st = _setup()
    war = await war_campaign.ensure_war(st)
    st.city_owners[1][16] = 2
    _mk_at(st, 550, template=1, owner=101, faction=2,
           deployed=True, map_entity_id=16, resources={1: 100})
    _mk_at(st, 551, template=506, deployed=True, map_entity_id=11,
           resources={4: 36, 10: 9})
    st.ats[551]['airbase_bf'] = 11
    w, f = await _transport(551, target=16)
    mv = st.movements[1]
    events = await war_campaign.advance_movements(
        st, war, now=mv['arrive_at'] + datetime.timedelta(seconds=1))
    split_ats = [a for a in st.ats.values()
                 if a.get('parent_command_node_id') == 551]
    check('R2i 敌城空降到达 → 空中原型入战不拆分（tpl 506 + state=2 '
          '+ 成局 + 无运输机队 AT）',
          f.get(3) == war_campaign.RC_TRANSPORTING_TO_ATTACK_SUPPLY
          and st.ats[551]['template_id'] == 506
          and st.ats[551]['state'] == 2
          and st.ats[551].get('airbase_bf') == 11
          and st.ats[551]['resources'].get(10) == 9
          and any(b.get('battlefield_id') == 16 and b.get('state', 0) == 0
                  for b in st.battles)
          and not split_ats,
          f"tpl={st.ats[551]['template_id']} "
          f"state={st.ats[551]['state']} split={split_ats}")

    # ---- R3 伞兵落地变体（#6：空降作战到达 → *_on_ground）----
    st = _setup()
    war = await war_campaign.ensure_war(st)
    _mk_at(st, 510, template=506, deployed=True, map_entity_id=11)
    st.ats[510]['airbase_bf'] = 11                   # 部署落机场持久化
    w, f = await _transport(510, target=16)      # 350×2=700≤1850 攻击半径
    mv = st.movements[1]
    events = await war_campaign.advance_movements(
        st, war, now=mv['arrive_at'] + datetime.timedelta(seconds=1))
    ev_set_def = [e for e in events if e[1] == b'CommandNodeDefinition']
    ev_del_ab = [e for e in events
                 if e[0] == 'del' and e[1] == b'air_commandnode_base']
    check('R3 伞兵空降到达 → 落地变体 507 + airbase 删行 + Definition 重推',
          f.get(3) == war_campaign.RC_OK
          and st.ats[510]['template_id'] == 507
          and st.ats[510]['map_entity_id'] == 16
          and st.ats[510]['airbase_bf'] == 0
          and not war_campaign.is_radius_unit(st.ats[510])
          and ev_del_ab and ev_del_ab[0][2] == 510
          and ev_set_def
          and pb_parse(ev_set_def[0][2]).get(6) == 507)

    # R3c 空降拆分（官服 Spaatz："returns the transport planes to the
    # origin"）：508 空降到达 → 伞兵转 509 只留 {4:36}，运输机拆成
    # 玩家名下独立运输机队（reserve 模板 504 + {10:9} + parent 指向
    # 伞兵 AT → 客户端运输机剪影图标挂父名），从空投点 16 **飞回**
    # 原机场 11（air_transport 在途行），不回阵营库存（adjust_log 空）
    st = _setup()
    war = await war_campaign.ensure_war(st)
    _mk_at(st, 512, template=508, deployed=True, map_entity_id=11,
           resources={4: 36, 10: 9})
    st.ats[512]['airbase_bf'] = 11
    w, f = await _transport(512, target=16)    # 350×2=700≤1850 攻击半径
    mv = st.movements[1]
    events = await war_campaign.advance_movements(
        st, war, now=mv['arrive_at'] + datetime.timedelta(seconds=1))
    planes = [a for a in st.ats.values()
              if a['id'] != 512 and war_campaign.is_reserve(a)]
    at_ev = [e for e in events if e[1] == b'air_transport']
    ab_ev = [e for e in events if e[1] == b'air_commandnode_base']
    at_set = [e for e in at_ev if e[0] == 'set']
    check('R3c 空降拆分 → 509{4:36} + 运输机队 504{10:9} 16→11 在途 + 库存不动',
          f.get(3) == war_campaign.RC_OK
          and st.ats[512]['template_id'] == 509
          and st.ats[512]['resources'] == {'4': 36}
          and st.ats[512]['airbase_bf'] == 0
          and len(planes) == 1
          and planes[0]['template_id'] == 504
          and planes[0]['resources'] == {'10': 9}
          and planes[0]['owner_player_id'] == 100
          and planes[0]['state'] == 1               # 在途飞回
          and int(planes[0].get('parent_command_node_id') or 0) == 512
          and planes[0].get('airbase_bf') == 11
          and not st.adjust_log
          and len(at_set) == 1
          and pb_parse(at_set[0][2]).get(2) == planes[0]['id']
          and pb_parse(at_set[0][2]).get(3) == 16    # from 空投点
          and pb_parse(at_set[0][2]).get(5) == 11    # to 原机场
          and any(e[0] == 'del' and e[2] == 512 for e in ab_ev),
          f'{f.get(3)} {st.ats[512]} planes={planes} at_ev={at_ev}')

    # R3c2 运输机队返航到达 → 落位原机场待命（不走补给队汇合/追击）
    pmv = next(m for m in st.movements.values()
               if m['commandnode_id'] == planes[0]['id'])
    events2 = await war_campaign.advance_movements(
        st, war, now=pmv['arrive_at'] + datetime.timedelta(seconds=1))
    del_at = [e for e in events2 if e[1] == b'air_transport']
    check('R3c2 返航到达 → 落位机场 11 待命（state0 + del air_transport）',
          planes[0]['state'] == 0
          and planes[0]['map_entity_id'] == 11
          and planes[0]['map_entity_type'] == 1
          and planes[0]['resources'] == {'10': 9}    # 不被汇合吞并
          and len(del_at) == 1 and del_at[0][0] == 'del'
          and del_at[0][2] == pmv['id'],
          f'planes={planes[0]} del_at={del_at}')

    # R3d 独立运输机队/补给队（reserve 模板）**不能手动移动**（官服
    # 口径：无飞行员——补给队自动机动或在出生机场等本体来合并；停驻
    # 机队只能自动返航或被 re-use 合并）：504{10:9} 停 11（airbase_bf=11）
    st = _setup()
    war = await war_campaign.ensure_war(st)
    st.city_owners[1] = {10: 1, 11: 1, 12: 1, 13: 1}
    _mk_at(st, 529, template=508, deployed=True, map_entity_id=11,
           resources={4: 36})
    _mk_at(st, 530, template=504, deployed=True, map_entity_id=11,
           resources={10: 9})
    st.ats[530]['airbase_bf'] = 11
    st.ats[530]['parent_command_node_id'] = 529
    w, f = await _transport(530, target=12)       # 12 己方机场
    check('R3d1 停驻运输机队手动移动 → 拒 152（不动不耗机）',
          f.get(3) == war_campaign.RC_COMMANDNODE_NOT_DEPLOYABLE
          and st.ats[530]['state'] == 0
          and st.ats[530]['map_entity_id'] == 11
          and st.ats[530]['resources'] == {10: 9}, str(f))
    w2, f2 = await _transport(530, target=13)
    check('R3d2 任意目标一律拒（非机场城 13 同 152）',
          f2.get(3) == war_campaign.RC_COMMANDNODE_NOT_DEPLOYABLE
          and st.ats[530]['state'] == 0, str(f2))

    # R3d3 补给队（同 reserve 模板但 airbase_bf=0）同样不能手动移动
    _mk_at(st, 532, template=504, deployed=True, map_entity_id=10)
    w3, f3 = await _transport(532, target=12)
    check('R3d3 无对应主力的地面补给队 → 拒 7',
          f3.get(3) == war_campaign.RC_CANNOT_FIND_COMMANDNODE
          and st.ats[532]['state'] == 0, str(f3))

    # R3d4 补给队非 parked planes 判定（is_parked_planes_at = reserve +
    # airbase_bf>0）
    check('R3d4 补给队非 parked planes 判定',
          not war_campaign.is_parked_planes_at(st.ats[532])
          and war_campaign.is_parked_planes_at(st.ats[530]))

    # R3b 伞兵换基地（11→17：1100×2=2200∈(1850,3600]）→ 保持 airborne
    st.city_owners[1][17] = 1
    _mk_at(st, 511, template=506, deployed=True, map_entity_id=11)
    w, f = await _transport(511, target=17)
    mv = next(m for m in st.movements.values()
              if m['commandnode_id'] == 511)
    events = await war_campaign.advance_movements(
        st, war, now=mv['arrive_at'] + datetime.timedelta(seconds=1))
    ab_ev = [e for e in events if e[1] == b'air_commandnode_base']
    check('R3b 伞兵换基地 → 21 保持 airborne（模板不变 + airbase set 17）',
          f.get(3) == war_campaign.RC_RELOCATING_TO_NEW_AIRBASE
          and st.ats[511]['template_id'] == 506
          and st.ats[511]['map_entity_id'] == 17
          and st.ats[511]['airbase_bf'] == 17
          and len(ab_ev) == 1 and ab_ev[0][0] == 'set'
          and pb_parse(ab_ev[0][2]).get(3) == 17, str(f))

    # ---- R4 airbase 行生命周期（撤退/快照/解散）----
    # R4a 撤退待命 → airbase 删行推送
    st = _setup()
    await war_campaign.ensure_war(st)
    _mk_at(st, 520, template=505, deployed=True, map_entity_id=11)
    w = FakeWriter()
    await war_at.handle_retreat_command_node_request(
        FakeSess(), w, 900, b'RetreatCommandNodeRequest', pb_int64(1, 520))
    fr = _resp(w, 'RetreatCommandNodeResponse')
    check('R4a 撤退 → airbase 删行',
          fr.get(1) == 1 and 520 in _dels(w, 'air_commandnode_base')
          and not st.ats[520]['is_deployed'], str(fr))

    # R4b 快照：已部署 radius 单位含 airbase 行，待命不含
    _mk_at(st, 521, template=506, deployed=True, map_entity_id=12)
    rows = await war_campaign.build_warmap_snapshot(st)
    ab_rows = [r for r in rows if r[0] == b'air_commandnode_base']
    check('R4b 快照 airbase 行（已部署 521 f3=12；待命 520 不含）',
          len(ab_rows) == 1
          and pb_parse(ab_rows[0][1]).get(2) == 521
          and pb_parse(ab_rows[0][1]).get(3) == 12)

    # R4c 解散推送含 airbase 删行
    w = FakeWriter()
    await war_at._push_at_delete(FakeSess(), w, st.ats[521])
    check('R4c 解散 → airbase 删行',
          521 in _dels(w, 'air_commandnode_base'))

    # R4d 落地变体撤退 → 转回伞兵原型（官服 convertTo 双向字段
    # paratroopers_on_ground.f11=108 指回 paratroopers；war2.md
    # 「伞兵机飞回」——待命=仓库，重新部署须恢复空降能力）
    _mk_at(st, 522, template=507, deployed=True, map_entity_id=13)
    w = FakeWriter()
    await war_at.handle_retreat_command_node_request(
        FakeSess(), w, 900, b'RetreatCommandNodeRequest', pb_int64(1, 522))
    fr4 = _resp(w, 'RetreatCommandNodeResponse')
    check('R4d 落地变体撤退 → template 转回 506 + Definition 行推送',
          fr4.get(1) == 1 and st.ats[522]['template_id'] == 506
          and not st.ats[522]['is_deployed']
          and war_campaign.is_radius_unit(st.ats[522])
          and _rows(w, 'CommandNodeDefinition'), str(fr4))

    # R4e 升级链：伞兵→落地变体仍拒（客户端 getUpgradeOption AS3 L37
    # 显式排除 PARATROOPERS_ON_GROUND；原 convert_to 分支曾放行伞兵→
    # on_ground 步行变体 = 伞兵永远步行 bug 根因）；落地变体→空中原型
    # 放行（2026-09-10 恢复官服 f11 互转语义：落地编制无 cat10 →
    # resupply 恒"已满编"，f11 反向链是重装运输机的唯一通道）
    check('R4e 升级链：伞兵→落地拒 / f11 落地→原型放行 / parent 链放行',
          not war_campaign.upgrade_allowed(_TPLS[506], _TPLS[507])
          and war_campaign.upgrade_allowed(_TPLS[507], _TPLS[506])
          and war_campaign.upgrade_allowed(_TPLS[501], _TPLS[503])
          and not war_campaign.upgrade_allowed(_TPLS[503], _TPLS[501])
          and not war_campaign.upgrade_allowed(_TPLS[501], _TPLS[501]))

    # R4f 落地变体在己方机场撤退 → 空中重整（方案1）：转回空中原型 506
    # （恢复运输机编制）+ 留驻机场保持部署 + airbase_bf 指向机场 11
    # （区别于 R4d 非机场 → 撤回待命）
    _mk_at(st, 523, template=507, deployed=True, map_entity_id=11)
    w = FakeWriter()
    await war_at.handle_retreat_command_node_request(
        FakeSess(), w, 900, b'RetreatCommandNodeRequest', pb_int64(1, 523))
    fr5 = _resp(w, 'RetreatCommandNodeResponse')
    check('R4f 落地变体己方机场撤退 → 空中重整（506+部署+airbase_bf=11）',
          fr5.get(1) == 1
          and st.ats[523]['template_id'] == 506
          and st.ats[523]['is_deployed']
          and st.ats[523]['map_entity_id'] == 11
          and st.ats[523].get('airbase_bf') == 11
          and war_campaign.is_radius_unit(st.ats[523]), str(fr5))

    # R4g 落地变体在普通城（己方、非机场）撤退 → 自动空运回最近己方机场
    # （区别于 R4d 敌后/无机场 → 撤回待命；也区别于 R4f 已在机场 → 就地重整）
    st = _setup()
    await war_campaign.ensure_war(st)
    st.city_owners[1] = {10: 1, 11: 1, 12: 1, 13: 1}
    _mk_at(st, 524, template=507, deployed=True, map_entity_id=13)
    w = FakeWriter()
    await war_at.handle_retreat_command_node_request(
        FakeSess(), w, 900, b'RetreatCommandNodeRequest', pb_int64(1, 524))
    fr6 = _resp(w, 'RetreatCommandNodeResponse')
    air_rows = _rows(w, 'air_transport')
    ab_rows = _rows(w, 'air_commandnode_base')
    check('R4g 落地变体普通城撤退 → 自动空运回最近机场 12（506+在途+air_transport）',
          fr6.get(1) == 1
          and st.ats[524]['template_id'] == 506
          and st.ats[524]['is_deployed']
          and st.ats[524]['state'] == 1
          and st.ats[524].get('airbase_bf') == 12
          and len(air_rows) == 1
          and air_rows[0].get(3) == 13
          and air_rows[0].get(5) == 12
          and len(ab_rows) == 1
          and ab_rows[0].get(3) == 12, str(fr6))

    # ---- R5 落地伞兵走回机场自动重整（tick 到达分支，f11 补给 /
    # 撤退重整之外的第三入口）----
    # R5a 走回己方机场 + 停驻同机场的同主机队 → 免费并机 + 转回空中
    # 原型（508 恢复 cat10）+ airbase_bf 指向机场 + 机队取尽软删
    st = _setup()
    war = await war_campaign.ensure_war(st)
    _mk_at(st, 541, template=509, deployed=True, map_entity_id=13,
           state=1, resources={4: 36})
    _mk_at(st, 542, template=504, deployed=True, map_entity_id=11,
           resources={10: 9})
    st.ats[542]['airbase_bf'] = 11
    mv = await st.create_movement(
        541, 1, [{'sl_id': 1, 'from_bf': 13, 'to_bf': 11, 'seconds': 100}],
        started_at=_NOW)
    events = await war_campaign.advance_movements(
        st, war, now=_NOW + datetime.timedelta(seconds=101))
    del_def_542 = [e for e in events
                   if e[0] == 'del' and e[1] == b'CommandNodeDefinition'
                   and e[2] == 542]
    set_def = [e for e in events
               if e[0] == 'set' and e[1] == b'CommandNodeDefinition']
    set_ab = [e for e in events
              if e[0] == 'set' and e[1] == b'air_commandnode_base']
    check('R5a 走回机场重整 → 508{4:36,10:9}+airbase_bf=11+机队软删+行推送',
          st.ats[541]['template_id'] == 508
          and st.ats[541]['resources'] == {'4': 36, '10': 9}
          and st.ats[541]['airbase_bf'] == 11
          and st.ats[541]['is_deployed']
          and war_campaign.is_radius_unit(st.ats[541])
          and st.ats[542]['state'] == 3
          and del_def_542
          and set_def and pb_parse(set_def[-1][2]).get(6) == 508
          and len(set_ab) == 1
          and pb_parse(set_ab[0][2]).get(3) == 11
          and pb_parse(set_ab[0][2]).get(2) == 541,
          f'{st.ats[541]} ev={len(events)}')

    # R5b 走回机场但机队停**别的**机场（12）→ 无机可并不重整（保持落地）
    st = _setup()
    war = await war_campaign.ensure_war(st)
    _mk_at(st, 543, template=509, deployed=True, map_entity_id=13,
           state=1, resources={4: 36})
    _mk_at(st, 544, template=504, deployed=True, map_entity_id=12,
           resources={10: 9})
    st.ats[544]['airbase_bf'] = 12
    mv = await st.create_movement(
        543, 1, [{'sl_id': 1, 'from_bf': 13, 'to_bf': 11, 'seconds': 100}],
        started_at=_NOW)
    await war_campaign.advance_movements(
        st, war, now=_NOW + datetime.timedelta(seconds=101))
    check('R5b 机队停他场 → 不重整（保持 509 落地 + 机队保留）',
          st.ats[543]['template_id'] == 509
          and st.ats[543]['resources'] == {4: 36}
          and st.ats[543].get('airbase_bf', 0) == 0
          and st.ats[544]['state'] == 0
          and st.ats[544]['resources'] == {10: 9},
          str(st.ats[543]))

    # R5c 走到**敌方**机场 → 不重整（归属判定先行，城随后由到达占城易主）
    st = _setup()
    war = await war_campaign.ensure_war(st)
    st.city_owners[1][11] = 2
    _mk_at(st, 545, template=509, deployed=True, map_entity_id=13,
           state=1, resources={4: 36})
    _mk_at(st, 546, template=504, deployed=True, map_entity_id=11,
           resources={10: 9})
    st.ats[546]['airbase_bf'] = 11
    mv = await st.create_movement(
        545, 1, [{'sl_id': 1, 'from_bf': 13, 'to_bf': 11, 'seconds': 100}],
        started_at=_NOW)
    await war_campaign.advance_movements(
        st, war, now=_NOW + datetime.timedelta(seconds=101))
    check('R5c 敌方机场 → 不重整（保持 509；机队不动）',
          st.ats[545]['template_id'] == 509
          and st.ats[545]['resources'] == {4: 36}
          and st.ats[546]['state'] == 0
          and st.ats[546]['resources'] == {10: 9},
          str(st.ats[545]))

    # R5d 部分取回：小机队 {10:2} 取尽软删 + 大机队 {10:9} 取 7 留 2
    # （满编口径 = 空中原型 508 cat10=9；多机队按 id 升序取）
    st = _setup()
    war = await war_campaign.ensure_war(st)
    _mk_at(st, 547, template=509, deployed=True, map_entity_id=13,
           state=1, resources={4: 36})
    _mk_at(st, 548, template=504, deployed=True, map_entity_id=11,
           resources={10: 2})
    _mk_at(st, 549, template=504, deployed=True, map_entity_id=11,
           resources={10: 9})
    st.ats[548]['airbase_bf'] = 11
    st.ats[549]['airbase_bf'] = 11
    mv = await st.create_movement(
        547, 1, [{'sl_id': 1, 'from_bf': 13, 'to_bf': 11, 'seconds': 100}],
        started_at=_NOW)
    await war_campaign.advance_movements(
        st, war, now=_NOW + datetime.timedelta(seconds=101))
    check('R5d 多机队取满即止 → 508{4:36,10:9}+548 软删+549 保留{10:2}',
          st.ats[547]['template_id'] == 508
          and st.ats[547]['resources'] == {'4': 36, '10': 9}
          and st.ats[548]['state'] == 3
          and st.ats[549]['state'] == 0
          and st.ats[549]['resources'] == {'10': 2},
          f'548={st.ats[548]["state"]} 549={st.ats[549]["resources"]}')

    # R5f 运输机找伞兵（反向自动重整）：机队自动返航飞抵**伞兵（落地
    # 变体）所在**机场 → 到达落位后免费并机 + 伞兵转回空中原型 +
    # 机队取尽软删（del 行齐全）。用 509（convert_to 508 空中含 cat10；
    # 506/507 是无 cat10 的简化对，语义上不该并机）。手动换防已按官服
    # 禁用（无飞行员），此处以 split_aircraft_back 同款 paradrop_return
    # 返航段构造在途机队
    st = _setup()
    war = await war_campaign.ensure_war(st)
    st.city_owners[1] = {10: 1, 11: 1, 12: 1, 13: 1}
    _mk_at(st, 560, template=509, deployed=True, map_entity_id=11,
           resources={4: 36})
    _mk_at(st, 561, template=504, deployed=True, map_entity_id=12,
           state=1, resources={10: 9})
    st.ats[561]['airbase_bf'] = 12
    st.ats[561]['parent_command_node_id'] = 560
    mv5f = await st.create_movement(
        561, 1, [{'sl_id': 0, 'from_bf': 12, 'to_bf': 11, 'seconds': 100,
                  'air': True, 'paradrop_return': True}], started_at=_NOW)
    await war_campaign.advance_movements(
        st, war, now=_NOW + datetime.timedelta(seconds=101))
    check('R5f 机队返航到达并入 → 伞兵 508{4:36,10:9}+airbase_bf=11+机队软删',
          st.ats[560]['template_id'] == 508
          and st.ats[560]['resources'] == {'4': 36, '10': 9}
          and st.ats[560]['airbase_bf'] == 11
          and st.ats[561]['state'] == 3,
          f'560={st.ats[560]["template_id"]}/{st.ats[560]["resources"]} '
          f'561={st.ats[561]["state"]}')

    # R5g 伞兵在**别的**机场（12），机队返航落 11 → 无候选 → 照常落位
    # 不合并（机队保留待命）
    st = _setup()
    war = await war_campaign.ensure_war(st)
    st.city_owners[1] = {10: 1, 11: 1, 12: 1, 13: 1}
    _mk_at(st, 562, template=507, deployed=True, map_entity_id=12,
           resources={4: 36})
    _mk_at(st, 563, template=504, deployed=True, map_entity_id=13,
           state=1, resources={10: 9})
    st.ats[563]['airbase_bf'] = 13
    st.ats[563]['parent_command_node_id'] = 562
    mv5g = await st.create_movement(
        563, 1, [{'sl_id': 0, 'from_bf': 13, 'to_bf': 11, 'seconds': 100,
                  'air': True, 'paradrop_return': True}], started_at=_NOW)
    await war_campaign.advance_movements(
        st, war, now=_NOW + datetime.timedelta(seconds=101))
    check('R5g 伞兵不在该机场 → 不合并（机队落位 11 保留 {10:9}）',
          st.ats[562]['template_id'] == 507
          and st.ats[562]['resources'] == {4: 36}
          and st.ats[563]['state'] == 0
          and st.ats[563]['map_entity_id'] == 11
          and st.ats[563]['airbase_bf'] == 11
          and st.ats[563]['resources'] == {10: 9},
          f'562={st.ats[562]["template_id"]} '
          f'563={st.ats[563]["state"]}/{st.ats[563]["resources"]}')


# ====================================== T 交战撤退规则（war1.md §撤退）
# 扩图：18 会战城（补给线邻 19/20，包围/中立用）；10-11 既有友方线


def _retreat_graph():
    wardb.accesspoints = lambda: [
        {'id': 101, 'battlefield': 10}, {'id': 102, 'battlefield': 11},
        {'id': 103, 'battlefield': 18}, {'id': 104, 'battlefield': 19},
        {'id': 105, 'battlefield': 18}, {'id': 106, 'battlefield': 20}]
    wardb.supplylines = lambda: [
        {'id': 1, 'ap1': 101, 'ap2': 102},
        {'id': 2, 'ap1': 103, 'ap2': 104},
        {'id': 3, 'ap1': 105, 'ap2': 106}]
    war_campaign.common._ADJACENCY_CACHE = None


async def _retreat(node_id):
    w = FakeWriter()
    await war_at.handle_retreat_command_node_request(
        FakeSess(), w, 900, b'RetreatCommandNodeRequest', pb_int64(1, node_id))
    return w, _resp(w, 'RetreatCommandNodeResponse')


async def section_t():
    _retreat_graph()

    # T1 交战强撤：损 25% 编制 + 扣 20 士气 + 向友方邻城撤退行军（动画）
    st = _setup()
    await war_campaign.ensure_war(st)
    _mk_at(st, 530, resources={1: 12}, deployed=True, map_entity_id=11,
           state=2)
    w, fr = await _retreat(530)
    mv530 = next((m for m in st.movements.values()
                  if m['commandnode_id'] == 530 and m['state'] == 0), None)
    check('T1 交战强撤 → 25% 损耗 + -20 士气 + 向友方邻城 10 撤退行军',
          fr.get(1) == 1
          and st.ats[530]['state'] == 1
          and st.ats[530]['is_deployed']
          and st.ats[530]['resources'] == {1: 9}
          and st.ats[530]['morale'] == 80
          and mv530 is not None
          and mv530['path'][-1]['to_bf'] == 10, str(fr))

    # T1b 撤退行军到达 → 落位友方城（保持部署，不逼重新部署）
    mv530['path'][0]['seconds'] = 0
    await war_campaign.advance_movements(st)
    check('T1b 撤退行军到达 → 友方邻城 10 落位（保持部署）',
          st.ats[530]['state'] == 0
          and st.ats[530]['is_deployed']
          and st.ats[530]['map_entity_id'] == 10, str(fr))

    # T2 包围：邻城 19/20 全驻敌军 AT → 拒绝撤退，AT 原地不动
    st = _setup()
    await war_campaign.ensure_war(st)
    _mk_at(st, 531, resources={1: 12}, deployed=True, map_entity_id=18,
           state=2)
    _mk_at(st, 5311, owner=200, faction=2, deployed=True, map_entity_id=19)
    _mk_at(st, 5312, owner=200, faction=2, deployed=True, map_entity_id=20)
    w, fr = await _retreat(531)
    check('T2 包围（邻城全驻敌军）→ 拒绝撤退 107 / 编制士气不动 / 仍 state=2',
          fr.get(1) == war_campaign.RC_COMMANDNODE_WAR_INSTANCE_NOT_FOUND
          and st.ats[531]['state'] == 2
          and st.ats[531]['resources'] == {1: 12}
          and st.ats[531]['morale'] == 100
          and st.ats[531]['map_entity_id'] == 18, str(fr))

    # T2b 敌空城/中立邻城不算包围 → 无友方路线回退撤回待命（惩罚照扣）
    st = _setup()
    await war_campaign.ensure_war(st)
    _mk_at(st, 532, resources={1: 12}, deployed=True, map_entity_id=18,
           state=2)
    _mk_at(st, 5321, owner=200, faction=2, deployed=True, map_entity_id=19)
    # 20 中立空城 → 可撤但无友方路线
    w, fr = await _retreat(532)
    check('T2b 非包围无友方路线 → 撤回待命（仍扣 25% 编制 / -20 士气）',
          fr.get(1) == 1
          and not st.ats[532]['is_deployed']
          and st.ats[532]['map_entity_id'] == 0
          and st.ats[532]['state'] == 0
          and st.ats[532]['resources'] == {1: 9}
          and st.ats[532]['morale'] == 80, str(fr))

    # T3 编制扣空 → 撤退途中溃散全歼（state=3 + 删行推送）
    st = _setup()
    await war_campaign.ensure_war(st)
    _mk_at(st, 533, resources={1: 1}, deployed=True, map_entity_id=11,
           state=2)
    w, fr = await _retreat(533)
    check('T3 撤退溃散 → state=3 + 全表删行推送',
          fr.get(1) == 1
          and st.ats[533]['state'] == 3
          and st.ats[533]['resources'] == {}
          and st.ats[533]['morale'] == 80
          and 533 in _dels(w, 'CommandNodeWarInstance')
          and 533 in _dels(w, 'CommandNodeDefinition'), str(fr))

    # T4 非交战撤退：无惩罚（编制/士气不动）+ 撤回待命（M1 回归）
    st = _setup()
    await war_campaign.ensure_war(st)
    _mk_at(st, 534, resources={1: 12}, deployed=True, map_entity_id=11)
    w, fr = await _retreat(534)
    check('T4 非交战撤退 → 待命且无惩罚',
          fr.get(1) == 1
          and not st.ats[534]['is_deployed']
          and st.ats[534]['resources'] == {1: 12}
          and st.ats[534]['morale'] == 100, str(fr))

    # T5 移动中撤退 → 在途行军中止（movement 终态 + 段/终点行删除推送）
    st = _setup()
    await war_campaign.ensure_war(st)
    _mk_at(st, 535, resources={1: 12}, deployed=True, map_entity_id=11,
           state=1)
    mv = await st.create_movement(
        535, 1,
        [{'sl_id': 1, 'from_ap': 102, 'to_ap': 101, 'seconds': 100}],
        started_at=_NOW)
    w, fr = await _retreat(535)
    check('T5 移动中撤退 → movement 中止 + segment/destination 删行 + 待命',
          fr.get(1) == 1
          and st.movements[mv['id']]['state'] == 2
          and mv['id'] in _dels(w, 'transport_commandnode_segment')
          and mv['id'] in _dels(w, 'transport_commandnode_destination')
          and not st.ats[535]['is_deployed'], str(fr))


async def main():
    # 本套件回归即时/库存入队链（M3-2）：钉 mode=0 关闭补给队
    # （全局默认已改 2=官服一律出队，出队分支由 test_war_supply_column 覆盖）
    mm_config.WAR_SUPPLY_COLUMN_MODE = 0
    await section_a()
    await section_b()
    await section_c()
    await section_f()
    await section_g()
    await section_h()
    await section_d()
    await section_e()
    await section_r()
    await section_t()
    fails = [label for label, ok in RESULTS if not ok]
    print(f'\n==== {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS ====')
    for label in fails:
        print(f'  FAIL: {label}')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
