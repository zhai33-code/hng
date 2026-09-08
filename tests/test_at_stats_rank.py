# -*- coding: utf-8 -*-
"""AT 等级/XP/战绩回归（2026-09-03 三问题修复），DB-free。

覆盖：
  A. 单元：rank_from_xp / next_level_xp（AssaultTeamLevel 阈值 + 缺表回退）、
     template_rank（新 AT rank=0 根因）、_fmt_ts 紧凑 Zulu（0 士气根因：
     ISO 分隔符串 → 客户端 convertZuluTimeToMilliseconds NaN → uint(NaN)=0）
  B. GetAssaultTeamStatsRequest：多 id + 未知 id 跳过、record 七字段、
     rank0 nextLevelUpXp=350、满级 0、空请求、STORE=None 不崩
  C. CompleteAssaultTeamRankRequest（买等级）：全价/部分进度比例扣款、
     rank+1 且 xp 置阈值+5（客户端段位基线，裸阈值渲染成满条）、xp 超前
     不回落、满级 106、余额不足 153（warfunds）、非本人 189、
     无效币种 240、定价缺失免单、CommandNodeDefinition f7 行推送、
     响应帧先于行推送（isAwaitingRankUpdate 卡死回归）
  D. UpgradeCommandNodeRequest rank 门槛：rank0 < 目标 level → 106 不扣款
     （0 XP 直升回归）、rank 达标放行、跨链目标拒绝、
     motorized_guard→motorized_infantry 跨链特例放行、跨阵营拒绝
  E. settle_war_battle XP：battles×5+battlesWon×25 重算、rank 阈值晋升 +
     Definition 行推送（rankHasChanged）、败方 won 不增 morale 减

写法沿用 test_war_at_supply.py：monkeypatch wardb 表 + FakeStore + check()，
运行 `.venv/bin/python tests/test_at_stats_rank.py`（pytest 收集不执行 main）。
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
    pb_int32,
    pb_int64,
    pb_message,
    pb_parse,
    pb_parse_multi,
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

# 501 基础步兵（level 0）；503 一级升级链（parent 501, level 1）；
# 505 二级升级链（parent 503, level 2）；516/520 复刻真实链
# motorized_guard(lvl1, parent guard) → motorized_infantry(lvl2, parent
# infantry，跨链特例)；521 跨阵营同名目标
_TPLS = {
    501: {'id': 501, 'name': 'Infantry AT', 'faction': 1,
          'resources': {1: 12}, 'vehicle_cats': set(), 'size': 2,
          'general_character_template_id': 0, 'level': 0},
    503: {'id': 503, 'name': 'Infantry AT L2', 'faction': 1,
          'resources': {1: 24}, 'vehicle_cats': set(), 'size': 4,
          'general_character_template_id': 0, 'level': 1,
          'parent_template_id': 501},
    505: {'id': 505, 'name': 'Infantry AT L3', 'faction': 1,
          'resources': {1: 48}, 'vehicle_cats': set(), 'size': 6,
          'general_character_template_id': 0, 'level': 2,
          'parent_template_id': 503},
    516: {'id': 516, 'name': 'motorized_guard', 'faction': 1,
          'resources': {1: 24}, 'vehicle_cats': set(), 'size': 4,
          'general_character_template_id': 0, 'level': 1,
          'parent_template_id': 501},
    520: {'id': 520, 'name': 'motorized_infantry', 'faction': 1,
          'resources': {1: 48}, 'vehicle_cats': set(), 'size': 6,
          'general_character_template_id': 0, 'level': 2,
          'parent_template_id': 503},
    521: {'id': 521, 'name': 'motorized_infantry', 'faction': 2,
          'resources': {1: 48}, 'vehicle_cats': set(), 'size': 6,
          'general_character_template_id': 0, 'level': 2,
          'parent_template_id': 203},
}

wardb.accesspoints = lambda: []
wardb.supplylines = lambda: []
wardb.battlefields = lambda: []
wardb.capitals = lambda: [{'battlefield_id': 10, 'default_faction': 1}]
wardb.commandnode_templates = lambda: {k: dict(v) for k, v in _TPLS.items()}
wardb.morale_template = lambda: None
wardb.armyresource_production = lambda: []
# AssaultTeamLevel 档位（A 段再覆盖"缺表回退同值"分支）
wardb.at_levels = lambda: [(1, 350), (2, 3450), (3, 9200)]
war_campaign.common._ADJACENCY_CACHE = None
war_campaign.common._BF_POS_CACHE = None

mm_config.WAR_AUTO_CREATE = True

# ---- 加载 war_at（绕开 handlers/__init__ 全量导入）----
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    'war_at_under_test', os.path.join(_ROOT, 'handlers', 'war_at.py'))
war_at = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(war_at)

import player_service  # noqa: E402

# ---- from-import 快照同步 + 定价 ----
war_at.commandnode_templates = wardb.commandnode_templates
war_at._PRICING_MAP = {
    9001: {'type': 'atRank', 'warfunds': 100, 'credits': 1000, 'gold': 10},
}
player_service._PRICING_MAP = {
    8001: {'type': 'at', 'warfunds': 500},
    9001: {'type': 'atRank', 'warfunds': 100, 'credits': 1000, 'gold': 10},
}


# ------------------------------------------------------------- 测试替身

class FakeStore:
    """war_at / war_campaign 最小存储接口（warfunds 默认 3000）。"""

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
        self.war_battles = {}
        self.mission_records = {}
        self.funds_log = []
        self.faction_stock = {}
        self.ended = []

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
        pass

    async def list_active_movements(self, war_id):
        return []

    async def list_war_command_nodes(self, war_id):
        return [dict(a) for a in self.ats.values()
                if a['war_id'] == int(war_id)]

    async def get_war_cities(self, war_id):
        return sorted(self.city_owners.get(war_id, {}).items())

    async def count_war_city_owners(self, war_id, battlefield_ids=None):
        wanted = (None if battlefield_ids is None
                  else set(battlefield_ids))
        counts = {}
        for bf, owner in self.city_owners.get(war_id, {}).items():
            if owner > 0 and (wanted is None or bf in wanted):
                counts[owner] = counts.get(owner, 0) + 1
        return counts

    async def end_war(self, war_id, winning_faction_id, sequel_war_id=0):
        self.ended.append((war_id, winning_faction_id))
        return True

    # ---- AT ----
    async def get_command_node(self, node_id):
        a = self.ats.get(int(node_id or 0))
        return copy.deepcopy(a) if a else None

    async def update_command_node(self, node_id, **fields):
        self.ats[int(node_id)].update(fields)

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

    # ---- settle_war_battle 依赖 ----
    async def get_war_battle(self, bid):
        b = self.war_battles.get(int(bid))
        return copy.deepcopy(b) if b else None

    async def end_war_battle(self, bid, winner):
        if int(bid) in self.war_battles:
            self.war_battles[int(bid)]['state'] = 1
            self.war_battles[int(bid)]['winning_faction'] = int(winner)

    async def get_mission_record(self, mid):
        m = self.mission_records.get(int(mid))
        return dict(m) if m else None

    async def set_mission_state(self, mid, state):
        if int(mid) in self.mission_records:
            self.mission_records[int(mid)]['state'] = int(state)

    async def add_war_funds(self, pid, amount, reason):
        self.funds_log.append((int(pid), int(amount), reason))

    # ---- 缴获入账（settle_war_battle captured 参数）----
    async def adjust_war_faction_resources(self, war_id, faction, deltas):
        stock = self.faction_stock.setdefault((int(war_id), int(faction)), {})
        for cat, d in (deltas or {}).items():
            cat = int(cat)
            stock[cat] = max(0, stock.get(cat, 0) + int(d))
        return [dict(war_id=int(war_id), faction=int(faction),
                      category_id=c, count=n) for c, n in
                sorted(stock.items())]


def _setup(balances=None):
    st = FakeStore(balances)
    war_at.STORE = st
    player_service.STORE = st
    return st


def _mk_at(st, node_id, template=501, owner=100, faction=1, state=0,
           rank=0, xp=0, battles=0, battles_won=0, resources=None,
           deployed=False, morale=100, max_morale=100):
    st.ats[node_id] = {
        'id': node_id, 'war_id': 1, 'template_id': template,
        'owner_player_id': owner, 'faction': faction, 'name': 'Alpha',
        'abbreviation': 'ALT', 'rank': rank, 'state': state, 'xp': xp,
        'battles': battles, 'battles_won': battles_won,
        'is_deployed': deployed, 'map_entity_id': 0,
        'map_entity_type': 1 if deployed else 0,
        'resources': dict(resources) if resources is not None else {},
        'has_gotten_soldier_free': False, 'has_gotten_vehicle_free': False,
        'general_character_id': 0, 'morale': morale, 'max_morale': max_morale,
        'created_at': _NOW,
    }
    return st.ats[node_id]


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
    """最后一帧指定标题响应的 proto 字段（pb_parse 单层）。"""
    want = title.encode()
    for c in reversed(_chunks(w.buf)):
        if c.subs and c.subs[0].data == want:
            return pb_parse(c.subs[0].subs[0].data)
    raise AssertionError(f'no response {title}')


def _resp_multi(w, title):
    """同 _resp 但 pb_parse_multi（repeated 字段响应）。"""
    want = title.encode()
    for c in reversed(_chunks(w.buf)):
        if c.subs and c.subs[0].data == want:
            return pb_parse_multi(c.subs[0].subs[0].data)
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


def _titles(w):
    """全部帧首子块标题（发送顺序，帧序断言用）。"""
    return [c.subs[0].data for c in _chunks(w.buf) if c.subs]


async def _stats(nodes):
    w = FakeWriter()
    req = b''.join(pb_int64(1, n) for n in nodes)
    await war_at.handle_get_assault_team_stats_request(
        FakeSess(), w, 900, b'GetAssaultTeamStatsRequest', req)
    return w, _resp_multi(w, 'GetAssaultTeamStatsResponse')


async def _buy_rank(node, pricing=9001, currency=2):
    w = FakeWriter()
    req = (pb_int64(1, node) + pb_int64(2, pricing) + pb_int32(4, currency))
    await war_at.handle_complete_assault_team_rank_request(
        FakeSess(), w, 900, b'CompleteAssaultTeamRankRequest', req)
    return w, _resp(w, 'CompleteAssaultTeamRankResponse')


async def _upgrade(node, target, pricing=8001, currency=2):
    w = FakeWriter()
    req = (pb_int64(1, node) + pb_int64(2, target) + pb_int64(3, pricing)
           + pb_int32(5, currency))
    await war_at.handle_upgrade_command_node_request(
        FakeSess(), w, 900, b'UpgradeCommandNodeRequest', req)
    return w, _resp(w, 'UpgradeCommandNodeResponse')


# ============================================================= A 单元

async def section_a():
    check('A1 rank_from_xp 阈值表（0/349/350/3449/3450/9200/999999）',
          war_campaign.rank_from_xp(0) == 0
          and war_campaign.rank_from_xp(349) == 0
          and war_campaign.rank_from_xp(350) == 1
          and war_campaign.rank_from_xp(3449) == 1
          and war_campaign.rank_from_xp(3450) == 2
          and war_campaign.rank_from_xp(9200) == 3
          and war_campaign.rank_from_xp(999999) == 3)

    check('A2 next_level_xp（rank0/1/2/满级3）',
          war_campaign.next_level_xp(0) == 350
          and war_campaign.next_level_xp(1) == 3450
          and war_campaign.next_level_xp(2) == 9200
          and war_campaign.next_level_xp(3) == 0)

    # 缺 AssaultTeamLevel 表 → 回退同值三档
    saved = wardb.at_levels
    wardb.at_levels = lambda: []
    try:
        check('A3 rank_from_xp/next_level_xp 缺表回退档位',
              war_campaign.rank_from_xp(350) == 1
              and war_campaign.rank_from_xp(349) == 0
              and war_campaign.next_level_xp(0) == 350
              and war_campaign.next_level_xp(3) == 0)
    finally:
        wardb.at_levels = saved

    check('A4 template_rank：基础模板 level0 → 0（0 XP 可升级根因）',
          war_campaign.template_rank(_TPLS[501]) == 0
          and war_campaign.template_rank(_TPLS[503]) == 1)

    dt = datetime.datetime(2026, 9, 1, 12, 0, 0,
                           tzinfo=datetime.timezone.utc)
    naive = datetime.datetime(2026, 9, 1, 12, 0, 0)
    check('A5 _fmt_ts 紧凑 Zulu（UTC/naive/None/str 原样）',
          war_campaign._fmt_ts(dt) == '20260901120000Z'
          and war_campaign._fmt_ts(naive) == '20260901120000Z'
          and war_campaign._fmt_ts(None) == ''
          and war_campaign._fmt_ts('20260901120000Z')
          == '20260901120000Z')

    st = _setup()
    a6 = _mk_at(st, 306)
    row = pb_parse(war_campaign.msg_commandnode_morale_row(a6, 1))
    check('A6 Morale 行 f5 Zulu（ISO → NaN → 0 士气回归）',
          row.get(5) == b'20260901120000Z', str(row.get(5)))

    # A7：Definition f10 statUpdateTimeStamp（stats_updated_at 优先，
    # 无值回退 created_at；变化驱动客户端 statsHaveChanged 重拉 stats）
    a7 = _mk_at(st, 307)
    row = pb_parse(war_campaign.msg_commandnode_definition_row(a7))
    base = row.get(10)
    a7['stats_updated_at'] = _NOW + datetime.timedelta(seconds=5)
    row2 = pb_parse(war_campaign.msg_commandnode_definition_row(a7))
    check('A7 Definition f10 stats_updated_at 优先/回退',
          base == war_campaign._ts_ms(_NOW) and row2.get(10) == base + 5000,
          f'base={base} new={row2.get(10)}')


# ============================================================= B stats

async def section_b():
    # B1/B2：多 id + 未知 id 跳过 + 字段值
    st = _setup()
    _mk_at(st, 9, rank=1, xp=400, battles=5, battles_won=3)
    _mk_at(st, 10, rank=0, xp=0)
    w, f = await _stats([9, 10, 999])
    recs = [pb_parse(r) for r in (f.get(1) or [])]
    by_id = {int(r.get(7) or 0): r for r in recs}
    check('B1 未知 id 跳过（2/3 记录）', sorted(by_id) == [9, 10],
          str(sorted(by_id)))
    r9, r10 = by_id.get(9, {}), by_id.get(10, {})
    check('B2 记录字段（result/rank/xp/next/battles/won/id）',
          r9.get(1) == 1 and r9.get(2) == 1 and r9.get(3) == 400
          and r9.get(4) == 3450 and r9.get(5) == 5 and r9.get(6) == 3,
          str(r9))

    # B3：rank0 → next 350；满级 rank3 → 0
    st = _setup()
    _mk_at(st, 11, rank=0, xp=0)
    _mk_at(st, 12, rank=3, xp=99999)
    w, f = await _stats([11, 12])
    recs = [pb_parse(r) for r in (f.get(1) or [])]
    by_id = {int(r.get(7) or 0): r for r in recs}
    check('B3 rank0 next=350 / 满级 next=0',
          by_id.get(11, {}).get(4) == 350
          and by_id.get(12, {}).get(4) == 0,
          str({k: v.get(4) for k, v in by_id.items()}))

    # B4：空请求 → 0 记录不崩
    w = FakeWriter()
    await war_at.handle_get_assault_team_stats_request(
        FakeSess(), w, 900, b'GetAssaultTeamStatsRequest', b'')
    f = _resp_multi(w, 'GetAssaultTeamStatsResponse')
    check('B4 空请求 → 0 记录', not (f.get(1) or []))

    # B5：STORE=None → 0 记录不崩（DB 不可用降级）
    saved = war_at.STORE
    war_at.STORE = None
    try:
        w, f = await _stats([9])
        check('B5 STORE=None 不崩（0 记录）', not (f.get(1) or []))
    finally:
        war_at.STORE = saved


# ============================================================= C 买等级

async def section_c():
    # C1：rank0 xp0 全价买 → rank1 / xp=阈值+5（客户端段位基线，裸阈值
    # 会被 percentageToNextATLevel 渲染成满条）/ Definition f7 推送
    st = _setup()
    _mk_at(st, 9, rank=0, xp=0)
    w, f = await _buy_rank(9)
    a = st.ats[9]
    defs = _rows(w, 'CommandNodeDefinition')
    check('C1 全价买等级（code=1 rank1 xp=355 扣100 推f7行）',
          f.get(1) == 1 and a['rank'] == 1 and a['xp'] == 355
          and st.spend_log == [(100, 'warfunds', 100)]
          and defs and defs[0].get(7) == 1,
          f'code={f.get(1)} rank={a["rank"]} xp={a["xp"]} '
          f'spend={st.spend_log}')

    # C2：部分进度比例计价（xp=175 → scale 0.5 → 扣 50）
    st = _setup()
    _mk_at(st, 9, rank=0, xp=175)
    w, f = await _buy_rank(9)
    check('C2 部分进度比例扣款（175/350 → 50，xp=355）',
          f.get(1) == 1 and st.spend_log == [(100, 'warfunds', 50)]
          and st.ats[9]['xp'] == 355,
          f'code={f.get(1)} spend={st.spend_log}')

    # C2b：rank1 已有部分进度（下限阈值+5 口径）
    st = _setup()
    _mk_at(st, 9, rank=1, xp=400)      # 350+50，下限 355 → 剩 3050/3450
    w, f = await _buy_rank(9)
    cost = (100, 'warfunds',
            max(1, int(100 * (3450 - (400 - 355)) / 3450)))
    check('C2b rank1 进度计价（下限+5 偏移，xp=3455）',
          f.get(1) == 1 and st.spend_log == [cost]
          and st.ats[9]['rank'] == 2 and st.ats[9]['xp'] == 3455,
          f'spend={st.spend_log}')

    # C2c：XP 已超前（rank 落后）→ xp 不回落
    st = _setup()
    _mk_at(st, 9, rank=1, xp=9000)
    w, f = await _buy_rank(9)
    check('C2c xp 超前不回落（rank+1，xp=9000）',
          f.get(1) == 1 and st.ats[9]['rank'] == 2
          and st.ats[9]['xp'] == 9000, f'xp={st.ats[9]["xp"]}')

    # C3：满级 → 106、不扣款、无行推送
    st = _setup()
    _mk_at(st, 9, rank=3, xp=99999)
    w, f = await _buy_rank(9)
    check('C3 满级 106 不扣款',
          f.get(1) == 106 and not st.spend_log
          and not _rows(w, 'CommandNodeDefinition')
          and st.ats[9]['rank'] == 3, f'code={f.get(1)}')

    # C4：余额不足（warfunds=2 → 153）
    st = _setup(balances={(100, 'warfunds'): 10})
    _mk_at(st, 9, rank=0, xp=0)
    w, f = await _buy_rank(9)
    check('C4 余额不足 153（warfunds 码）rank 不变',
          f.get(1) == 153 and not st.spend_log and st.ats[9]['rank'] == 0
          and st.ats[9]['xp'] == 0, f'code={f.get(1)}')

    # C5：非本人 AT → 189
    st = _setup()
    _mk_at(st, 9, owner=999, rank=0, xp=0)
    w, f = await _buy_rank(9)
    check('C5 非本人 189 不扣款', f.get(1) == 189 and not st.spend_log,
          f'code={f.get(1)}')

    # C6：无效币种 → 240
    st = _setup()
    _mk_at(st, 9, rank=0, xp=0)
    w, f = await _buy_rank(9, currency=9)
    check('C6 无效币种 240', f.get(1) == 240 and not st.spend_log,
          f'code={f.get(1)}')

    # C7：定价缺失 → 免单放行（与 _charge_player 口径一致）
    st = _setup()
    _mk_at(st, 9, rank=0, xp=0)
    w, f = await _buy_rank(9, pricing=9999)
    check('C7 定价缺失免单 rank+1', f.get(1) == 1
          and st.ats[9]['rank'] == 1 and not st.spend_log,
          f'code={f.get(1)}')

    # C8：响应帧必须先于行推送（isAwaitingRankUpdate 卡死回归：行先到会被
    # 响应 code=1 重新置锁，经验条消失、买等级按钮永久禁用）
    st = _setup()
    _mk_at(st, 9, rank=0, xp=0)
    w, f = await _buy_rank(9)
    t = _titles(w)
    check('C8 响应帧先于 KVCS 行推送',
          f.get(1) == 1
          and t.index(b'CompleteAssaultTeamRankResponse')
          < t.index(b'KeyValueChangeSet'), str(t))


# ============================================================= D 升级门槛

async def section_d():
    # D1：rank0 → 503(level1) → 106 不扣款（0 XP 直升回归）
    st = _setup()
    _mk_at(st, 9, template=501, rank=0, xp=0)
    w, f = await _upgrade(9, 503)
    check('D1 rank0 < level1 → 106 不扣款',
          f.get(1) == 106 and not st.spend_log
          and st.ats[9]['template_id'] == 501, f'code={f.get(1)}')

    # D2：rank1 → 503 放行（扣款 + 换模板不补编）
    st = _setup()
    _mk_at(st, 9, template=501, rank=1, xp=400)
    w, f = await _upgrade(9, 503)
    check('D2 rank1 >= level1 放行（扣500 换模板）',
          f.get(1) == 1 and st.ats[9]['template_id'] == 503
          and st.spend_log == [(100, 'warfunds', 500)],
          f'code={f.get(1)} spend={st.spend_log}')

    # D3：跨链目标（501 → 505）拒绝
    st = _setup()
    _mk_at(st, 9, template=501, rank=2, xp=9000)
    w, f = await _upgrade(9, 505)
    check('D3 跨链目标 501→505 拒绝 106', f.get(1) == 106,
          f'code={f.get(1)}')

    # D4：rank2 → 505(level2, parent 503) 达标放行
    st = _setup()
    _mk_at(st, 9, template=503, rank=2, xp=9000)
    w, f = await _upgrade(9, 505)
    check('D4 rank2 >= level2 放行', f.get(1) == 1
          and st.ats[9]['template_id'] == 505, f'code={f.get(1)}')

    # D5：motorized_guard → motorized_infantry 跨链特例放行（客户端
    # AssaultTeamUpgradeOptions 同款特例；真实数据 316→320 复刻）
    st = _setup()
    _mk_at(st, 9, template=516, rank=2, xp=9000)
    w, f = await _upgrade(9, 520)
    check('D5 motorized_guard→motorized_infantry 特例放行',
          f.get(1) == 1 and st.ats[9]['template_id'] == 520
          and st.spend_log == [(100, 'warfunds', 500)],
          f'code={f.get(1)} tpl={st.ats[9]["template_id"]}')

    # D6：跨阵营同名目标拒绝（同阵营校验，对齐客户端 factionTemplateId）
    st = _setup()
    _mk_at(st, 9, template=516, rank=2, xp=9000)
    w, f = await _upgrade(9, 521)
    check('D6 跨阵营 motorized_infantry 拒绝 106',
          f.get(1) == 106 and not st.spend_log, f'code={f.get(1)}')


# ============================================================= E 结算 XP

def _mk_battle(st, bid, war_id=1, bf=10, ats=()):
    st.war_battles[bid] = {
        'id': bid, 'war_id': war_id, 'battlefield_id': bf,
        'factions': [{'at_id': a, 'faction': f} for a, f in ats],
        'state': 0, 'winning_faction': 0}


async def section_e():
    # E1：胜方首战 → battles=1 won=1 xp=30 rank=0 + warfunds 入账
    # （settle 只回写 state=2 在战中的 AT，_mk_at 需置 state=2）
    st = _setup()
    _mk_at(st, 9, state=2, rank=0, xp=0, battles=0, battles_won=0,
           resources={1: 100})
    _mk_battle(st, 1, ats=[(9, 1)])
    events = await war_campaign.settle_war_battle(st, 0, 1, 1)
    a = st.ats[9]
    tags = [t for kind, t, _ in events if kind == 'set']
    check('E1 胜方 XP 口径（battles×5+won×25=30 rank0）+ warfunds',
          a['battles'] == 1 and a['battles_won'] == 1 and a['xp'] == 30
          and a['rank'] == 0
          and b'CommandNodeWarInstance' in tags
          and (100, 100, 'war_battle 1') in st.funds_log
          and (100, 200, 'war_battle 1 win') in st.funds_log,
          f'b={a["battles"]} w={a["battles_won"]} xp={a["xp"]} '
          f'rank={a["rank"]} funds={st.funds_log}')

    # E2：累计 15 胜 → xp=450 晋升 rank1 + Definition 行推送（rankHasChanged）
    st = _setup()
    _mk_at(st, 9, state=2, rank=0, xp=210, battles=14, battles_won=14,
           resources={1: 100})
    _mk_battle(st, 1, ats=[(9, 1)])
    events = await war_campaign.settle_war_battle(st, 0, 1, 1)
    a = st.ats[9]
    defs = [p for kind, t, p in events
            if kind == 'set' and t == b'CommandNodeDefinition']
    check('E2 15 胜 xp=450 晋升 rank1 + Definition 推送',
          a['battles'] == 15 and a['battles_won'] == 15
          and a['xp'] == 450 and a['rank'] == 1 and len(defs) == 1,
          f'xp={a["xp"]} rank={a["rank"]} defs={len(defs)}')

    # E3：败方 won 不增 / xp=5 / morale -20
    st = _setup()
    _mk_at(st, 9, state=2, rank=0, xp=0, battles=0, battles_won=0,
           morale=100, resources={1: 100})
    _mk_battle(st, 1, ats=[(9, 1)])
    await war_campaign.settle_war_battle(st, 0, 1, 2)     # faction 2 胜
    a = st.ats[9]
    check('E3 败方 won 不增 xp=5 morale 100→80',
          a['battles'] == 1 and a['battles_won'] == 0 and a['xp'] == 5
          and a['morale'] == 80,
          f'b={a["battles"]} w={a["battles_won"]} xp={a["xp"]} '
          f'morale={a["morale"]}')

    # E6：买等级注入的 XP（阈值+5）不被公式重算冲掉
    st = _setup()
    _mk_at(st, 9, state=2, rank=2, xp=3455, battles=0, battles_won=0,
           resources={1: 100})
    _mk_battle(st, 1, ats=[(9, 1)])
    await war_campaign.settle_war_battle(st, 0, 1, 1)
    a = st.ats[9]
    check('E6 已购 XP 保护（3455+30=3485 而非公式 30）',
          a['xp'] == 3485 and a['rank'] == 2,
          f'xp={a["xp"]} rank={a["rank"]}')

    # E4：battle 终态 + 重复结算幂等
    st = _setup()
    _mk_at(st, 9, state=2, resources={1: 100})
    _mk_battle(st, 1, ats=[(9, 1)])
    await war_campaign.settle_war_battle(st, 0, 1, 1)
    b1 = st.ats[9]['battles']
    events = await war_campaign.settle_war_battle(st, 0, 1, 1)
    check('E4 会战终态幂等（二结无事件不重计）',
          st.war_battles[1]['state'] == 1 and events == []
          and st.ats[9]['battles'] == b1 == 1,
          f'events={events}')

    # E5：缴获入账——captured 加进胜方阵营库存 + HQ armyresource 行推送
    st = _setup()
    _mk_at(st, 9, state=2, resources={1: 100})       # faction 1 败方
    _mk_battle(st, 1, ats=[(9, 1)])
    events = await war_campaign.settle_war_battle(
        st, 0, 1, 2, captured={1: 40, 5: 7})
    stock = st.faction_stock.get((1, 2), {})
    import mm_factions as _mm
    hq = int(_mm.FACTION_SUPREME_COMMANDS[2])
    ids = set()                                       # HQ 行 f1 id 集合
    for kind, t, row in events:
        if kind != 'set' or t != b'armyresource':
            continue
        f = pb_parse(row)
        if int(f.get(2, 0)) == hq:                    # f2=commandNodeWarInstanceId
            ids.add(int(f.get(1)))
    check('E5 缴获入账胜方阵营库存 + HQ armyresource 行',
          stock.get(1) == 40 and stock.get(5) == 7
          and ((hq & ~0xFF) | 1) in ids
          and ((hq & ~0xFF) | 5) in ids,
          f'stock={stock} ids={ {hex(int(i)) for i in ids} } '
          f'hq={hex(hq)}')

    # E5b：无 captured（staged / automission）不触碰库存
    st = _setup()
    _mk_at(st, 9, state=2, resources={1: 100})
    _mk_battle(st, 1, ats=[(9, 1)])
    await war_campaign.settle_war_battle(st, 0, 1, 1)
    check('E5b 无 captured 不入账', st.faction_stock == {}, '')

    # E7：#1 underdog 加成——人数较少阵营 XP/战资结算倍率（faction 行
    #     f6/f7 同源公式 mult = 1 + cap×(1-n/n_max)）
    # E7a：fac1 两玩家 / fac2 一玩家 → fac2 share=0.5 →
    #     xp_mult=1.125（30×1.125→33）/ wf_mult=1.075（100→107 / 200→215）
    st = _setup()
    _mk_at(st, 9, owner=100, faction=1, state=2, resources={1: 100})
    _mk_at(st, 10, owner=101, faction=1, state=0)   # 待命：只抬 fac1 人数
    _mk_at(st, 19, owner=200, faction=2, state=2, resources={1: 100})
    _mk_battle(st, 1, ats=[(9, 1), (19, 2)])
    await war_campaign.settle_war_battle(st, 0, 1, 2)      # fac2 胜
    check('E7a underdog XP 33（30×1.125）/ warfunds 107+215',
          st.ats[19]['xp'] == 33
          and (200, 107, 'war_battle 1') in st.funds_log
          and (200, 215, 'war_battle 1 win') in st.funds_log,
          f'xp={st.ats[19]["xp"]} funds={st.funds_log}')
    check('E7a 强势阵营 mult=1.0（败方 xp=5 / 战资 100）',
          st.ats[9]['xp'] == 5
          and (100, 100, 'war_battle 1') in st.funds_log,
          f'xp={st.ats[9]["xp"]}')

    # E7b：软删 AT（state=3）不计人数 → 双方各 1 人 mult=1.0 → xp=30
    st = _setup()
    _mk_at(st, 9, owner=100, faction=1, state=2, resources={1: 100})
    _mk_at(st, 10, owner=101, faction=1, state=3)   # 软删不计
    _mk_at(st, 19, owner=200, faction=2, state=2, resources={1: 100})
    _mk_battle(st, 1, ats=[(9, 1), (19, 2)])
    await war_campaign.settle_war_battle(st, 0, 1, 2)
    check('E7b 软删 AT 不计人数（平衡 mult=1.0 → xp=30 / 200 战资）',
          st.ats[19]['xp'] == 30
          and (200, 200, 'war_battle 1 win') in st.funds_log,
          f'xp={st.ats[19]["xp"]} funds={st.funds_log}')

    # E8：is_auto 系统判定（automission）不提供经验——battles/won/xp/rank
    #     保持原值 + warfunds 不发放；编制损耗/士气/终态照常（war1.md）
    st = _setup()
    _mk_at(st, 9, owner=100, faction=1, state=2, rank=1, xp=400,
           battles=7, battles_won=3, morale=100, resources={1: 100})
    _mk_battle(st, 1, ats=[(9, 1)])
    await war_campaign.settle_war_battle(st, 0, 1, 1, is_auto=True)
    a = st.ats[9]
    check('E8 系统判定不发经验（battles/xp/rank 原值 + 无 warfunds）',
          a['battles'] == 7 and a['battles_won'] == 3 and a['xp'] == 400
          and a['rank'] == 1 and st.funds_log == []
          and a['morale'] == 100,        # 胜方 +moraleGainedPerWin 照常
          f'b={a["battles"]} xp={a["xp"]} funds={st.funds_log}')
    st = _setup()
    _mk_at(st, 9, owner=100, faction=1, state=2, morale=100,
           resources={1: 100}, battles=0, battles_won=0, xp=0)
    _mk_battle(st, 1, ats=[(9, 1)])
    await war_campaign.settle_war_battle(st, 0, 1, 2, is_auto=True)  # 败方
    a = st.ats[9]
    check('E8c is_auto 败方 morale 100→80 + 编制 100→40（60% 损耗）不涨经验',
          a['morale'] == 80 and a['resources'] == {1: 40}
          and a['battles'] == 0 and a['xp'] == 0 and st.funds_log == [],
          f'morale={a["morale"]} res={a["resources"]}')


# ------------------------------------------------------------- main

async def main():
    for sec in (section_a, section_b, section_c, section_d, section_e):
        print(f'\n===== {sec.__name__} =====')
        await sec()
    ok = sum(1 for _, v in RESULTS if v)
    print(f'\n{"=" * 50}\n{ok}/{len(RESULTS)} passed')
    return 0 if ok == len(RESULTS) else 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
