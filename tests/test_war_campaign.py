# -*- coding: utf-8 -*-
"""RTS 战争状态机回归（war_campaign.py Phase 3，DB-free）。

覆盖：
  A. build_war_proto：29 REQUIRED 字段全填（pb_parse 解回逐字段断言，
     字段缺失=解析后取不到默认值）
  B. ensure_war：自动开战（MM_WAR_AUTO_CREATE）→ 初始城市归属落库；
     已有 running 战争直接复用；pool=None 回退 None
  C. check_victory：未达线 None；达线 end_war + sequel 自动开战 +
     update_war_sequel 回填；MM_WAR_SEQUEL_AUTO=0 只结束不续战
  D. war catalogue：聚合（城市/AT/玩家/资源计数、阵营统计、f15 三行）；
     factionId/factionTemplateId 均为 1/2/3 恒等
  E. _fmt_ts / _as_resources / _war_config 数值兜底

WarDatabase 依赖（capitals/morale_template）在 macOS 开发机缺表时
返回空/None → 相关断言自动降级为"回退官服缺省值"分支（同
test_mm_latejoin 环境豁免策略，不算失败）。
"""
import asyncio
import datetime
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol import pb_parse, pb_parse_multi
import mm_config
import wardb
import war_campaign

RESULTS = []


def f32(raw):
    """fixed32 字段（pb_parse 解回原始 4 字节）→ float。"""
    return struct.unpack('<f', raw)[0]


def check(label, ok, detail=''):
    RESULTS.append((label, ok))
    print(f'[{"PASS" if ok else "FAIL"}] {label}{("  " + detail) if detail else ""}')


# ------------------------------------------------------------- FakeStore

class FakeStore:
    """war_campaign 用到的最小存储接口。"""

    def __init__(self):
        self.pool = object()          # ensure_war 的可用性探针
        self.wars = {}                # id -> dict
        self.next_war_id = 1
        self.city_owners = {}        # war_id -> {bf_id: faction}
        self.ats = []                 # command_nodes 行

    async def get_active_war(self):
        for w in self.wars.values():
            if w['state'] == 1:
                return w
        return None

    async def list_wars(self, limit=20):
        return [dict(w) for w in
                sorted(self.wars.values(), key=lambda w: -w['id'])[:limit]]

    async def create_war(self, name, points_needed_for_victory=15, mapid=1,
                         config=None):
        wid = self.next_war_id
        self.next_war_id += 1
        w = {'id': wid, 'name': name, 'begintime': datetime.datetime(2026, 9, 2),
             'endtime': None, 'winning_faction_id': 0,
             'points_needed_for_victory': points_needed_for_victory,
             'sequel_war_id': 0, 'mapid': mapid, 'state': 1,
             'config': config or {}}
        self.wars[wid] = w
        return w

    async def init_war_cities(self, war_id, owner_map):
        self.city_owners[war_id] = dict(owner_map)

    async def init_war_faction_resources(self, war_id, initial):
        self.stock = getattr(self, 'stock', {})
        self.stock.setdefault(int(war_id), {})

    async def list_active_movements(self, war_id):
        return []

    async def list_active_war_battles(self, war_id):
        # warmapview 快照 battle 行段（2026-09-08 补）用；无会战数据
        return []

    async def get_war(self, war_id):
        return self.wars.get(int(war_id))

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

    async def list_war_command_nodes(self, war_id):
        return [dict(a) for a in self.ats if a['war_id'] == war_id]

    async def end_war(self, war_id, winning_faction_id, sequel_war_id=0):
        w = self.wars[war_id]
        w['state'] = 2
        w['winning_faction_id'] = winning_faction_id
        w['endtime'] = datetime.datetime(2026, 9, 2, 12)
        return True

    async def update_war_sequel(self, war_id, sequel_war_id):
        self.wars[war_id]['sequel_war_id'] = sequel_war_id


async def main():
    has_wardb = bool(wardb.capitals())
    check('WarDatabase capital 表可用（macOS 缺表时降级）', True,
          'present' if has_wardb else 'absent -> 官服缺省回退分支')
    # f8/胜利阈值预期：落库值（缺省 MM_WAR_POINTS_VICTORY）与实际主城数
    # 取小（effective_points_needed 语义：stored 优先；私服配置
    # WAR_POINTS_VICTORY 可与官服 15 不同，期望式按同式计算）
    exp_pts = war_campaign.effective_points_needed(15)
    exp_pts_def = war_campaign.effective_points_needed()

    # ---- A. build_war_proto 29 字段 ----
    war = {'id': 42, 'name': 'Test War',
           'begintime': datetime.datetime(2026, 9, 2, 8, 30),
           'endtime': None, 'winning_faction_id': 0,
           'points_needed_for_victory': 15, 'sequel_war_id': 0,
           'mapid': 1, 'state': 1,
           'config': {'strategytimescale': 2.0, 'daycycletimescale': 1.5,
                      'maxPlayerPerFaction': 50,
                      'morale': dict(war_campaign.DEFAULT_MORALE)}}
    proto = war_campaign.build_war_proto(war)
    top = pb_parse(proto)
    check('war proto 29 个字段号全出现',
          sorted(top.keys()) == list(range(1, 30)),
          f'{sorted(top.keys())}')
    check('f1 id', int(top[1]) == 42)
    check('f2 name', top[2].decode() == 'Test War')
    check('f3 begintime STR（紧凑 Zulu，客户端 convertZuluTime 解析）',
          top[3].decode() == '20260902083000Z', top[3].decode())
    check('f4 endtime 进行中空串', top[4] == b'')
    check('f7 winningfactionid=0', int(top[7]) == 0)
    check('f8 pointsNeededForVictory（按实际主城数钳制）',
          int(top[8]) == exp_pts, f'{int(top[8])}')
    check('f12 strategytimescale 快照', abs(f32(top[12]) - 2.0) < 1e-5)
    check('f13 daycycletimescale 快照', abs(f32(top[13]) - 1.5) < 1e-5)
    check('f15 maxPlayerPerFaction 快照', int(top[15]) == 50)
    check('f17 mapid', int(top[17]) == 1)
    mt = war_campaign.morale_defaults()
    check('f24-29 morale 参数', int(top[24]) == mt['min']
          and int(top[25]) == mt['max']
          and int(top[26]) == mt['lost_per_battle']
          and int(top[27]) == mt['gained_per_win']
          and abs(f32(top[28]) - mt['gained_per_minute']) < 1e-5
          and int(top[29]) == mt['surrender_limit'])
    # 无 config 的战争行（全缺省）
    proto2 = war_campaign.build_war_proto(
        {'id': 1, 'name': '', 'begintime': None, 'endtime': None,
         'winning_faction_id': 0, 'points_needed_for_victory': 0,
         'sequel_war_id': 0, 'mapid': 0, 'state': 1, 'config': {}})
    top2 = pb_parse(proto2)
    check('缺省回退 f8=MM_WAR_POINTS_VICTORY（钳制后）',
          int(top2[8]) == exp_pts_def)
    check('缺省回退 f12=MM_WAR_TIMESCALE',
          abs(f32(top2[12]) - mm_config.WAR_TIMESCALE) < 1e-5)

    # ---- B. ensure_war ----
    store = FakeStore()
    w = await war_campaign.ensure_war(store)
    check('ensure_war 自动开战', w is not None and w['state'] == 1,
          f'id={w["id"] if w else None}')
    check('ensure_war config 快照含 morale',
          'morale' in w['config'] and 'strategytimescale' in w['config'])
    expected_owners = war_campaign.initial_city_owners()
    check('初始城市归属落库',
          store.city_owners[w['id']] == expected_owners,
          f'{len(expected_owners)} cities')
    w2 = await war_campaign.ensure_war(store)
    check('已有 running 战争直接复用', w2['id'] == w['id']
          and store.next_war_id == w['id'] + 1)
    check('store=None 返回 None',
          await war_campaign.ensure_war(None) is None)
    nopool = FakeStore()
    nopool.pool = None
    check('无 pool 的 store 返回 None',
          await war_campaign.ensure_war(nopool) is None)

    # ---- C. check_victory ----
    store = FakeStore()
    w = await war_campaign.ensure_war(store)
    capitals = war_campaign.capital_battlefield_ids()
    check('未达线返回 None', await war_campaign.check_victory(store) is None)
    # 造占领：本机 WarDatabase 首都数可能远小于 MM_WAR_POINTS_VICTORY，
    # 压低阈值到 min(2, 可用首都数) 保证可达线（无 WarDatabase 用普通城市）
    w['points_needed_for_victory'] = 2
    needed = 2
    own = dict(store.city_owners[w['id']])
    src = list(capitals) if capitals else list(range(1000, 1000 + needed))
    for bf in src[:needed]:
        own[bf] = 2
    store.city_owners[w['id']] = own
    winner = await war_campaign.check_victory(store)
    check('达线判定 + end_war', winner == 2
          and w['state'] == 2 and w['winning_faction_id'] == 2)
    check('sequel 自动开战 + 回填',
          w['sequel_war_id'] > 0
          and store.wars[w['sequel_war_id']]['state'] == 1)
    # SEQUEL_AUTO=0：只结束不续战
    old = mm_config.WAR_SEQUEL_AUTO
    try:
        mm_config.WAR_SEQUEL_AUTO = False
        store2 = FakeStore()
        w2 = await war_campaign.ensure_war(store2)
        w2['points_needed_for_victory'] = 2
        own2 = dict(store2.city_owners[w2['id']])
        src2 = list(capitals) if capitals else list(range(2000, 2100))
        for bf in src2[:2]:
            own2[bf] = 3
        store2.city_owners[w2['id']] = own2
        winner2 = await war_campaign.check_victory(store2)
        check('SEQUEL_AUTO=0 只结束不续战',
              winner2 == 3 and w2['sequel_war_id'] == 0
              and store2.next_war_id == w2['id'] + 1)
    finally:
        mm_config.WAR_SEQUEL_AUTO = old

    # C2. 胜利条件第二支（war1.md 2026-09-08 修复）：一方失光全部主城 →
    #     剩余两方主城数多者胜；平分平局无胜方；第一支不可达时才走第二支
    _orig_caps_fn = wardb.capitals
    wardb.capitals = lambda: [
        {'battlefield_id': 3001, 'default_faction': 1},
        {'battlefield_id': 3002, 'default_faction': 1},
        {'battlefield_id': 3003, 'default_faction': 2},
        {'battlefield_id': 3004, 'default_faction': 2},
        {'battlefield_id': 3005, 'default_faction': 3},
    ]
    try:
        # C2a US 失光全部主城，GE 3 > SU 2 → GE 胜（第一支不可达 99→钳 5）
        store3 = FakeStore()
        w3 = await war_campaign.ensure_war(store3)
        w3['points_needed_for_victory'] = 99
        store3.city_owners[w3['id']] = {
            3001: 2, 3002: 2, 3003: 3, 3004: 2, 3005: 3}
        winner3 = await war_campaign.check_victory(store3)
        check('C2a 失光主城方淘汰 → 剩两方多者胜（GE 3>SU 2）',
              winner3 == 2 and w3['state'] == 2
              and w3['winning_faction_id'] == 2,
              f'winner={winner3} state={w3["state"]}')

        # C2b 平分 → 平局无胜方（战争继续）
        store4 = FakeStore()
        w4 = await war_campaign.ensure_war(store4)
        w4['points_needed_for_victory'] = 99
        store4.city_owners[w4['id']] = {
            3001: 2, 3002: 2, 3003: 3, 3004: 3, 3005: 0}
        winner4 = await war_campaign.check_victory(store4)
        check('C2b 剩两方平分 → 平局无胜方',
              winner4 is None and w4['state'] == 1,
              f'winner={winner4} state={w4["state"]}')

        # C2c 无一方失光（三方各持有）→ 第二支不触发
        store5 = FakeStore()
        w5 = await war_campaign.ensure_war(store5)
        w5['points_needed_for_victory'] = 99
        store5.city_owners[w5['id']] = {
            3001: 1, 3002: 2, 3003: 2, 3004: 2, 3005: 3}
        winner5 = await war_campaign.check_victory(store5)
        check('C2c 三方各持主城 → 第二支不触发',
              winner5 is None and w5['state'] == 1,
              f'winner={winner5}')
    finally:
        wardb.capitals = _orig_caps_fn

    # ---- C3. 战争终态 war 行（广播数据端，2026-09-08）----
    # check_victory 结束战争后 get_war+build_war_proto 必须能构造出
    # f7=胜方 / f16=sequel 的 war 行（客户端 WarIsOver 链的触发源）
    store6 = FakeStore()
    w6 = await war_campaign.ensure_war(store6)
    await store6.end_war(w6['id'], 1, sequel_war_id=0)
    await store6.update_war_sequel(w6['id'], 0)
    ended = await store6.get_war(w6['id'])
    war_proto = war_campaign.build_war_proto(ended)
    wf = pb_parse(war_proto)
    check('C3 终态 war 行 f7 胜方 / f16 sequel 可构造',
          int(wf[7]) == 1 and int(wf[16]) == 0,
          f'f7={int(wf.get(7, 0))} f16={int(wf.get(16, 0))}')

    # ---- D. war catalogue ----
    store = FakeStore()
    w = await war_campaign.ensure_war(store)
    # 玩家 7（GE）2 支 AT + 玩家 9（US）1 支 AT
    store.ats = [
        {'id': 1, 'war_id': w['id'], 'owner_player_id': 7, 'faction': 2,
         'resources': {'1': 90, '12': 16}},
        {'id': 2, 'war_id': w['id'], 'owner_player_id': 7, 'faction': 2,
         'resources': {'11': 12}},
        {'id': 3, 'war_id': w['id'], 'owner_player_id': 9, 'faction': 1,
         'resources': {'2': 24, '20': 24}},
    ]
    rows = await war_campaign.build_war_catalogue_protos(store)
    check('catalogue 每场战争一行', len(rows) == 1)
    cat = pb_parse(rows[0])
    check('catalogue f1 id / f2 name', int(cat[1]) == w['id']
          and cat[2].decode() == w['name'])
    check('catalogue f7 assaultTeamCount', int(cat[7]) == 3)
    check('catalogue f8 playerCount', int(cat[8]) == 2)
    check('catalogue f10 infantry 合计', int(cat[10]) == 90 + 24,
          f'{int(cat[10])}')
    check('catalogue f11 tank 合计', int(cat[11]) == 16)
    check('catalogue f12 plane 合计', int(cat[12]) == 24)
    check('catalogue f13 vehicle 合计', int(cat[13]) == 12)
    facs = [pb_parse(p) for p in pb_parse_multi(rows[0])[15]]
    check('catalogue f15 三阵营行', len(facs) == 3)
    check('factionId 恒等 1/2/3',
          [int(f[1]) for f in facs] == [1, 2, 3]
          and [int(f[2]) for f in facs] == [1, 2, 3])
    fac_by_id = {int(f[1]): f for f in facs}
    ge_cities = sum(1 for _, o in store.city_owners[w['id']].items()
                    if o == 2)
    check('GE controlled 计数', int(fac_by_id[2][13]) == ge_cities)
    check('US deployedCommandPointsInfantry', int(fac_by_id[1][10]) == 24)
    check('GE playerCount=1', int(fac_by_id[2][5]) == 1)
    # 结束的战争也进目录
    await store.end_war(w['id'], 2)
    rows2 = await war_campaign.build_war_catalogue_protos(store)
    check('已结束战争保留在目录', len(rows2) == 1
          and int(pb_parse(rows2[0])[5]) == 2)
    check('catalogue f3 wartime=天数（非 Zulu 时间戳）',
          pb_parse(rows2[0])[3].decode() == '1',
          pb_parse(rows2[0])[3].decode())
    resp = await war_campaign.build_war_catalogue_response_proto(store)
    check('response proto 含 bonusId(f2)',
          int(pb_parse(resp)[2]) == 0)
    # includeWarId 置首（2026-09-08）：WarIsOver 终局链取
    # warcataloguedata[0]——多战争时指定 war 必须排首位
    w2 = await store.create_war('Second War', 15)
    await store.init_war_cities(w2['id'], {})
    rows3 = await war_campaign.build_war_catalogue_protos(store)
    check('默认序新战争在前', int(pb_parse(rows3[0])[1]) == w2['id'])
    rows4 = await war_campaign.build_war_catalogue_protos(
        store, include_war_id=w['id'])
    check('includeWarId 指定战争置首',
          int(pb_parse(rows4[0])[1]) == w['id']
          and int(pb_parse(rows4[1])[1]) == w2['id'])
    # 空库回退
    check('空 store 返回 None',
          await war_campaign.build_war_catalogue_response_proto(FakeStore()) is None)

    # ---- E. 纯函数 ----
    check('_fmt_ts 空/None', war_campaign._fmt_ts(None) == '')
    check('_fmt_ts str 透传', war_campaign._fmt_ts('x') == 'x')
    check('_as_resources json str',
          war_campaign._as_resources('{"1": 90}') == {'1': 90})
    check('_as_resources 坏 json 容错',
          war_campaign._as_resources('{bad') == {})
    cfg = war_campaign._war_config()
    check('_war_config 数值链',
          cfg['strategytimescale'] == mm_config.WAR_TIMESCALE
          and cfg['morale']['surrender_limit'] == 25)

    # ---- E2. _war_days（WarIsOver 结算文案"用X天占领欧洲"，2026-09-08）----
    # 此前误发 begintime Zulu 时间戳 → 结算页"X天"显示成具体日期
    check('_war_days 无 begintime → 1',
          war_campaign._war_days({}) == '1')
    check('_war_days 坏时间 → 1',
          war_campaign._war_days({'begintime': 'bad'}) == '1')
    check('_war_days 同日开打结束 → 1',
          war_campaign._war_days({
              'begintime': datetime.datetime(2026, 9, 2, 10),
              'endtime': datetime.datetime(2026, 9, 2, 12)}) == '1')
    check('_war_days 整三天 → 3',
          war_campaign._war_days({
              'begintime': datetime.datetime(2026, 9, 2, 10),
              'endtime': datetime.datetime(2026, 9, 5, 10)}) == '3')
    check('_war_days 三天+1分钟 → 4（ceil）',
          war_campaign._war_days({
              'begintime': datetime.datetime(2026, 9, 2, 10),
              'endtime': datetime.datetime(2026, 9, 5, 10, 1)}) == '4')
    check('_war_days 未来开始（elapsed<=0）→ 1',
          war_campaign._war_days({
              'begintime': datetime.datetime.now(
                  datetime.timezone.utc) + datetime.timedelta(days=1)
          }) == '1')
    check('_war_days ISO str 时间可用（1天+1分 → 2）',
          war_campaign._war_days({
              'begintime': '2026-09-02T10:00:00+00:00',
              'endtime': '2026-09-03T10:01:00+00:00'}) == '2')

    # ---- F. warmapview 快照 ----
    store = FakeStore()
    w = await war_campaign.ensure_war(store)
    created = datetime.datetime(2026, 9, 2, 10, 0, 0)
    store.ats = [
        {'id': 11, 'war_id': w['id'], 'owner_player_id': 7, 'faction': 2,
         'template_id': 203, 'name': '1. Infanterie', 'abbreviation': 'INF',
         'rank': 3, 'morale': 80, 'max_morale': 100, 'state': 0,
         'map_entity_id': 0, 'general_character_id': 555,
         'is_deployed': False, 'created_at': created,
         'resources': {'1': 90, '12': 16}},
        {'id': 12, 'war_id': w['id'], 'owner_player_id': 9, 'faction': 1,
         'template_id': 103, 'name': '101st Airborne', 'abbreviation': 'AB',
         'rank': 1, 'morale': 100, 'max_morale': 100, 'state': 1,
         'map_entity_id': 9001, 'general_character_id': 0,
         'is_deployed': True, 'created_at': created,
         'resources': {'4': 72}},
    ]
    snapshot = await war_campaign.build_warmap_snapshot(store)
    tags = [tag for tag, _ in snapshot]
    # 推送顺序：battlefieldstatus → … → CommandNodeDefinition →
    # CommandNodeWarInstance → CommandNodeMorale → armyresource（§4.3 时序）
    tag_order = [t for t in tags if t in
                 (b'battlefieldstatus', b'CommandNodeDefinition',
                  b'CommandNodeWarInstance', b'CommandNodeMorale',
                  b'armyresource')]
    check('快照表序符合 §4.3',
          tag_order == [b'battlefieldstatus'] * tag_order.count(b'battlefieldstatus')
          + [b'CommandNodeDefinition'] * 2 + [b'CommandNodeWarInstance'] * 2
          + [b'CommandNodeMorale'] * 2 + [b'armyresource'] * 3)
    bf_rows = [p for t, p in snapshot if t == b'battlefieldstatus']
    check('battlefieldstatus 行数=有归属城市数',
          len(bf_rows) == sum(1 for o in store.city_owners[w['id']].values()
                              if o))
    bf1 = pb_parse(bf_rows[0])
    check('battlefieldstatus f1 id 为 S64（wiretype 1）',
          bf_rows[0][0] == 0x09   # tag = field 1 << 3 | wire 1
          and struct.unpack('<q', bf1[1])[0] == int(bf1[3]))
    check('battlefieldstatus f2 warid / f4 factionid',
          int(bf1[2]) == w['id'] and int(bf1[4]) in (1, 2, 3))
    # CommandNodeWarInstance：mapEntityTypeId ∈ {1,2,3}（登录卡死硬约束）
    inst_rows = [pb_parse(p) for t, p in snapshot
                 if t == b'CommandNodeWarInstance']
    check('CommandNodeWarInstance mapEntityTypeId ∈ {1,2,3}',
          all(int(r[6]) in (1, 2, 3) for r in inst_rows))
    by_id = {int(r[1]): r for r in inst_rows}
    ent, ent_type = war_campaign._at_map_entity(
        store.ats[0], war_campaign._faction_capital_bf())
    check('待命 AT 落位（首都或空位置）',
          int(by_id[11][5]) == ent and int(by_id[11][6]) == ent_type)
    check('已部署 AT mapEntityId=9001 type=1',
          int(by_id[12][5]) == 9001 and int(by_id[12][6]) == 1)
    check('isDeployed / morale / warId',
          int(by_id[12][10]) == 1 and int(by_id[11][8]) == 80
          and int(by_id[11][7]) == w['id'])
    def_rows = {int(pb_parse(p)[1]): pb_parse(p)
                for t, p in snapshot if t == b'CommandNodeDefinition'}
    check('CommandNodeDefinition name/abbrev/owner',
          def_rows[11][2].decode() == '1. Infanterie'
          and def_rows[11][3].decode() == 'INF'
          and int(def_rows[11][4]) == 7
          and int(def_rows[11][5]) == 2)
    check('Definition f6 模板 id / f7 rank / f9 将军角色',
          int(def_rows[11][6]) == 203 and int(def_rows[11][7]) == 3
          and int(def_rows[11][9]) == 555)
    ar_rows = [pb_parse(p) for t, p in snapshot if t == b'armyresource']
    check('armyresource id 段 = node*1000+cat',
          {int(r[1]) for r in ar_rows} == {11001, 11012, 12004})
    check('armyresource f2 实例 / f4 count / f5 warid',
          int(ar_rows[0][2]) in (11, 12) and int(ar_rows[0][4]) > 0
          and int(ar_rows[0][5]) == w['id'])
    mor = {int(pb_parse(p)[3]): pb_parse(p) for t, p in snapshot
           if t == b'CommandNodeMorale'}
    check('CommandNodeMorale playerId/warId/STR 时间戳（紧凑 Zulu）',
          int(mor[11][2]) == 7 and int(mor[11][4]) == w['id']
          and mor[11][5].decode() == '20260902100000Z')
    # 退化路径：store=None / 无 pool
    check('快照 store=None 返回 []',
          await war_campaign.build_warmap_snapshot(None) == [])
    nopool = FakeStore()
    nopool.pool = None
    check('快照无 pool 返回 []',
          await war_campaign.build_warmap_snapshot(nopool) == [])

    # ---- G. underdog 倍率（#1 人数较少阵营经验/战资加成）----
    u = war_campaign.underdog_multipliers({1: 5, 2: 5, 3: 5})
    check('underdog 平衡全员 1.0', u == {1: (1.0, 1.0), 2: (1.0, 1.0),
                                         3: (1.0, 1.0)}, f'{u}')
    u = war_campaign.underdog_multipliers({1: 10, 2: 0, 3: 0})
    check('underdog 无人阵营拿满加成（1.25/1.15）',
          u[1] == (1.0, 1.0)
          and abs(u[2][0] - 1.25) < 1e-9 and abs(u[2][1] - 1.15) < 1e-9
          and abs(u[3][0] - 1.25) < 1e-9 and abs(u[3][1] - 1.15) < 1e-9,
          f'{u}')
    u = war_campaign.underdog_multipliers({1: 4, 2: 2})
    check('underdog 线性插值（share=0.5 → 1.125/1.075；缺阵营=0 人）',
          abs(u[2][0] - 1.125) < 1e-9 and abs(u[2][1] - 1.075) < 1e-9
          and abs(u[3][0] - 1.25) < 1e-9 and abs(u[3][1] - 1.15) < 1e-9,
          f'{u}')
    check('underdog 空计数全 1.0',
          war_campaign.underdog_multipliers({}) ==
          {1: (1.0, 1.0), 2: (1.0, 1.0), 3: (1.0, 1.0)})
    u = war_campaign.underdog_multipliers({1: 5, 2: 5, 3: 1})
    check('underdog 并列最多不加分（fac3 share=0.8 → 1.2/1.12）',
          u[1] == (1.0, 1.0) and u[2] == (1.0, 1.0)
          and abs(u[3][0] - 1.2) < 1e-9 and abs(u[3][1] - 1.12) < 1e-9,
          f'{u}')
    # 人数口径：AT 持有玩家 owner 去重、软删（state>=3）不计、
    # 阵营号外（9）与其他战争的 AT 不计
    gst = FakeStore()
    gst.ats = [
        {'war_id': 1, 'faction': 1, 'owner_player_id': 7, 'state': 0},
        {'war_id': 1, 'faction': 1, 'owner_player_id': 7, 'state': 2},
        {'war_id': 1, 'faction': 2, 'owner_player_id': 9, 'state': 1},
        {'war_id': 1, 'faction': 2, 'owner_player_id': 11, 'state': 3},
        {'war_id': 1, 'faction': 9, 'owner_player_id': 13, 'state': 0},
        {'war_id': 2, 'faction': 1, 'owner_player_id': 15, 'state': 0},
    ]
    cnt = await war_campaign.faction_player_counts(gst, 1)
    check('faction_player_counts owner 去重/软删剔除/异战争过滤',
          cnt == {1: 1, 2: 1, 3: 0}, f'{cnt}')
    # 无战争（pool=None → ensure_war None）回退全 1.0
    nopool2 = FakeStore()
    nopool2.pool = None
    m = await war_campaign.current_underdog_mults(nopool2)
    check('current_underdog_mults 无战争回退全 1.0',
          m == {1: (1.0, 1.0), 2: (1.0, 1.0), 3: (1.0, 1.0)}, f'{m}')

    fails = [l for l, ok in RESULTS if not ok]
    print(f'\n{"="*50}\nwar_campaign 回归：{len(RESULTS)-len(fails)}/{len(RESULTS)} PASS')
    if fails:
        for l in fails:
            print(f'  FAIL: {l}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
