"""玩家身份 id 相关回归测试（2026-08-27 playerId/factionid 硬编码修复）。

事故：login2_result 的 currentplayer.id 恒发 1，而 DB player_id 从 2 开始，
客户端战报界面 getCurrentPlayerInformation() 按 currentPlayer.id 匹配本局
玩家行 → 匹配不到 → 结算窗口不弹。同族问题：
1) msg_player f11 factionid 恒发 1(US)，GE/SU 玩家界面按美国阵营展示
2) messages._msg_* 士兵视图各表 playerId 恒发 1，CharacterTable 按
   currentPlayer.id 过滤 → DB id≥2 玩家看不到自己的角色/装备
3) 结算收益循环不过滤 bot playerId，bot 编号撞真实账号 id 时错发货币

验证：
- msg_player/msg_login2_result 透传真实 pid/faction
- CURRENT_PID contextvar 并发隔离（每连接协程独立 context）
- _msg_* 系列 playerId 跟随 CURRENT_PID
- 结算：bot pid 与名单外 pid 不入账/不记统计
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol import RC, msg_login2_result, msg_player
from state import CURRENT_PID
from messages import _cur_pid, _msg_character
from handlers import combat as combat_handlers


# ---------- protobuf 构造/解析 helpers ----------
def pb_varint(field, value):
    if value < 0:
        value += 1 << 64
    out = bytearray([field << 3 | 0])
    while True:
        b = value & 0x7F
        value >>= 7
        out.append(b | 0x80 if value else b)
        if not value:
            break
    return bytes(out)


def pb_msg(field, payload):
    out = bytearray([field << 3 | 2])
    ln = len(payload)
    while True:
        b = ln & 0x7F
        ln >>= 7
        out.append(b | 0x80 if ln else b)
        if not ln:
            break
    return bytes(out) + payload


def _read_varint(b, i):
    v = 0; shift = 0
    while True:
        c = b[i]; i += 1
        v |= (c & 0x7F) << shift; shift += 7
        if not c & 0x80:
            return v, i


def read_fields(b):
    fields = {}
    i = 0
    while i < len(b):
        tag, i = _read_varint(b, i)     # 字段号>15 时 tag 是多字节 varint
        fn, wt = tag >> 3, tag & 7
        if wt == 2:
            ln = 0; shift = 0
            while True:
                c = b[i]; i += 1
                ln |= (c & 0x7F) << shift; shift += 7
                if not c & 0x80:
                    break
            fields[fn] = b[i:i + ln]; i += ln
        elif wt == 0:
            v = 0; shift = 0
            while True:
                c = b[i]; i += 1
                v |= (c & 0x7F) << shift; shift += 7
                if not c & 0x80:
                    break
            fields[fn] = v
        elif wt == 1:                     # fixed64（mousesensitivity 等）
            fields[fn] = b[i:i + 8]; i += 8
        elif wt == 5:                     # fixed32
            fields[fn] = b[i:i + 4]; i += 4
    return fields


# ---------- 1. msg_player：pid / faction 透传 ----------
def test_msg_player_pid_and_faction():
    p = read_fields(msg_player('Tester', pid=42, faction=3))
    assert p[1] == 42, f'f1 id 应为 42，实际 {p[1]}'
    assert p[11] == 3, f'f11 factionid 应为 3(SU)，实际 {p[11]}'


def test_msg_player_faction_fallback():
    """faction=None（无角色/DB 不可用）回退 1，保持老行为。"""
    p = read_fields(msg_player('Tester', pid=42))
    assert p[11] == 1
    # pid 缺省仍回退 1
    p2 = read_fields(msg_player('Tester'))
    assert p2[1] == 1


# ---------- 2. msg_login2_result：currentplayer 携带 pid/faction ----------
def test_msg_login2_result_success_carries_identity():
    m = read_fields(msg_login2_result('Tester', RC['login_success'],
                                      pid=42, faction=2))
    assert 6 in m, '登录成功必须携带 f6 currentplayer'
    p = read_fields(m[6])
    assert p[1] == 42 and p[11] == 2, \
        f'currentplayer.id/factionid 应为 42/2，实际 {p[1]}/{p[11]}'


def test_msg_login2_result_failure_has_no_player():
    m = read_fields(msg_login2_result('Tester', RC['wrong_password'],
                                      pid=42, faction=2))
    assert 6 not in m, '登录失败不得携带 currentplayer'


# ---------- 3. CURRENT_PID contextvar：并发连接隔离 ----------
def test_cur_pid_contextvar_isolation():
    """两个"连接"协程交错执行，各自读到自己的 pid（asyncio task 各持
    context 拷贝，set 不串号）。"""

    async def conn(pid):
        CURRENT_PID.set(pid)
        for _ in range(50):
            await asyncio.sleep(0)      # 强制交错
            assert _cur_pid() == pid, f'串号! 期望 {pid}，读到 {_cur_pid()}'
        return pid

    async def main():
        return await asyncio.gather(conn(2), conn(303), conn(7))

    got = asyncio.run(main())
    assert sorted(got) == [2, 7, 303]


def test_cur_pid_default():
    """未设置（未登录连接/主上下文）回退默认 1。"""
    token = CURRENT_PID.set(1)          # 隔离测试现场，结束还原
    try:
        assert _cur_pid() == 1
    finally:
        CURRENT_PID.reset(token)


# ---------- 4. _msg_*：playerId 跟随 CURRENT_PID ----------
def test_msg_character_uses_cur_pid():
    """CharacterTable 按 playerId.Equals(currentPlayer.id) 过滤自己拥有的
    行——恒发 1 会让 DB id≥2 玩家看不到角色。"""
    token = CURRENT_PID.set(42)
    try:
        ch = read_fields(_msg_character('Soldier', faction=3, char_id=9))
    finally:
        CURRENT_PID.reset(token)
    assert ch[3] == 42, f'character f3 playerid 应为 42，实际 {ch[3]}'


# ---------- 5. 结算：bot / 名单外 pid 过滤 ----------
class _MockWriter:
    def __init__(self):
        self.sent = b''

    def write(self, data):
        self.sent += bytes(data)

    async def drain(self):
        return


class _MockSess:
    combat_mission = 100003
    game_mission = 100003


class _FakeStore:
    def __init__(self):
        self.saved = None
        self.currency = []
        self.stats = []

    async def save_battle_report(self, report, players):
        self.saved = (report, players)
        return 1

    async def add_currency(self, player_id, credits=0, warfunds=0):
        self.currency.append((player_id, credits, warfunds))
        return {'credits': credits, 'warfunds': warfunds}

    async def add_combat_xp(self, pid, cid, xp):
        return xp

    async def add_battle_pass_score(self, pid, season, score):
        return score

    async def add_stat(self, cid, key, val):
        self.stats.append((cid, key, val))
        return None

    async def set_ribbon_xp_floor(self, pid, cid, rid, xp):
        return None


def _build_bot_collision_request():
    """模拟 2026-08-27 事故现场：bot playerId=3 撞真实账号 id=3。

    BattleReport 名单：真人 pid=1（is_bot=0）、bot pid=3（is_bot=1）；
    f5 PlayerBattleResult：pid=1（真人）、pid=3（bot 撞号）、pid=7
    （不在名单）。"""
    # 真人（带角色 4 与勋带）
    char_real = (pb_varint(1, 4) + pb_varint(2, 1000) + pb_varint(3, 2500))
    player_real = (pb_varint(1, 1) + pb_varint(4, 500) + pb_varint(6, 3)
                   + pb_varint(7, 1) + pb_msg(9, b'TestGuy')
                   + pb_varint(10, 0) + pb_msg(11, char_real))
    # bot（is_bot f10=1，角色 10001）
    char_bot = pb_varint(1, 10001)
    player_bot = (pb_varint(1, 3) + pb_varint(4, 900) + pb_varint(6, 9)
                  + pb_msg(9, b'[Bot] Ivan') + pb_varint(10, 1)
                  + pb_msg(11, char_bot))
    faction = pb_varint(1, 2) + pb_msg(2, player_real) + pb_msg(2, player_bot)
    report = (pb_varint(1, 730433917100003) + pb_varint(2, 2)
              + pb_varint(3, 3) + pb_varint(4, 900) + pb_varint(5, 1787750000)
              + pb_msg(6, faction))
    # f5：真人 120cr/340wf；bot 撞号 999cr；名单外 pid=7 500cr
    pbr_real = pb_varint(1, 1) + pb_varint(2, 120) + pb_varint(3, 340)
    pbr_bot = pb_varint(1, 3) + pb_varint(2, 999) + pb_varint(3, 999)
    pbr_ghost = pb_varint(1, 7) + pb_varint(2, 500) + pb_varint(3, 500)
    return (pb_varint(1, 100003) + pb_varint(2, 730433917100003)
            + pb_varint(3, 10) + pb_msg(4, report) + pb_msg(5, pbr_real)
            + pb_msg(5, pbr_bot) + pb_msg(5, pbr_ghost))


def test_settlement_bot_and_ghost_pids_filtered():
    """bot 撞号 pid=3 与名单外 pid=7 不入账；真人 pid=1 正常（含 +25%
    老兵加成 120→150 / 340→425）。"""
    fake = _FakeStore()
    old = combat_handlers.STORE
    combat_handlers.STORE = fake
    try:
        asyncio.run(combat_handlers.handle_announce_battlefield_earnings(
            _MockSess(), _MockWriter(), 200,
            'AnnounceBattleFieldEarningsRequest',
            _build_bot_collision_request()))
    finally:
        combat_handlers.STORE = old

    assert fake.currency == [(1, 150, 425)], \
        f'货币只应发给真人 pid=1（+25%），实际 {fake.currency}'
    # 战报入库含 bot 行（人机局战报需显示 bot）
    assert [p['player_id'] for p in fake.saved[1]] == [1, 3], \
        f'入库玩家行应含真人 pid=1 与 bot pid=3，实际 {fake.saved[1]}'
    assert fake.saved[1][1]['is_bot'] is True, \
        f'bot 行应带 is_bot=True，实际 {fake.saved[1][1]}'
    # bot 角色 10001 不得产生任何统计写入
    assert all(cid != 10001 for cid, _, _ in fake.stats), \
        f'bot 角色 10001 不应写统计，实际 {fake.stats}'


# ---------- 6. faction 行 id 空间对齐（2026-09-01 跨阵营"同阵营"显示事故） ----------
# 事故：FACTION_ROW_IDS 用随机 63 位行 id，而客户端 currentPlayer.factionid
# （player 行 f11=1/2/3）、MissionDetailsMediator 传的 factionTemplateId、
# factiontemplateid 比较全是模板 id 空间 → 大厅"我方/敌方"判定永不相等：
# friendCount 永不命中、enemyCount 把跨阵营双方全算敌方（=同阵营观感）、
# 简报我方不高亮、攻防角色恒"进攻"。官服语义（客户端三处调用点反编译实证）
# 唯一解：faction 表行 id == factiontemplateid。
import matchmaking  # noqa: E402
import mm_handler  # noqa: E402
import mm_factions  # noqa: E402
from protocol import (  # noqa: E402
    pb_field_payload,
    pb_parse_loose,
    pb_split_top,
    )


def _split_factions(raw, fn):
    """顶层 repeated 子消息 → [pb_parse_loose(子块)]（保序）。"""
    return [pb_parse_loose(pb_field_payload(blk))
            for _fn, blk in pb_split_top(raw) if _fn == fn]


def test_faction_row_ids_identity_mapping():
    """faction 行 id 必须 == factiontemplateid（官服语义，见
    mm_factions.FACTION_ROW_IDS 注释的反编译证据链）。"""
    assert mm_factions.FACTION_ROW_IDS == {1: 1, 2: 2, 3: 3}, \
        f'FACTION_ROW_IDS 应为恒等映射，实际 {mm_factions.FACTION_ROW_IDS}'


def test_faction_rows_id_equals_template():
    """KVS faction 行 f1(id) 与 f2(factiontemplateid) 同值——客户端
    getFromId(行id).factiontemplateid.Equals(currentPlayer.factionid)
    链路两端才在同一空间。"""
    for row in mm_factions._msg_faction_rows():
        f = pb_parse_loose(row)
        assert int(f[1]) == int(f[2]), \
            f'faction 行 id({f[1]}) != factiontemplateid({f[2]})'


def test_faction_rows_underdog_multipliers():
    """faction 行 f6/f7 underdog 倍率（#1 人数较少阵营加成）：无参默认
    1.0，传 mults 时按阵营下发（客户端 FactionOption.as 按 >1 显示
    "当前弱势阵营加成"组）。"""
    for row in mm_factions._msg_faction_rows():
        f = pb_parse_loose(row)
        assert abs(f[6] - 1.0) < 1e-6 and abs(f[7] - 1.0) < 1e-6, \
            f'默认应全 1.0：f6={f[6]} f7={f[7]}'
    rows = {int(f[2]): f for f in (
        pb_parse_loose(r)
        for r in mm_factions._msg_faction_rows({1: (1.0, 1.0),
                                                2: (1.25, 1.15)}))}
    assert abs(rows[2][6] - 1.25) < 1e-6 and abs(rows[2][7] - 1.15) < 1e-6, \
        f'fac2 应带倍率：f6={rows[2][6]} f7={rows[2][7]}'
    assert abs(rows[1][6] - 1.0) < 1e-6 and abs(rows[1][7] - 1.0) < 1e-6, \
        f'fac1 多数阵营应 1.0：f6={rows[1][6]} f7={rows[1][7]}'
    assert abs(rows[3][6] - 1.0) < 1e-6 and abs(rows[3][7] - 1.0) < 1e-6, \
        f'fac3 缺省应 1.0：f6={rows[3][6]} f7={rows[3][7]}'


def test_multi_gmd_f3_uses_template_ids_and_groups_by_faction():
    """德军 A + 苏军 B 成局：GMD f3 每条的 factionId 必须落在模板 id 空间
    （客户端 updateMissionDetailsFactionCounts 拿它 Equals
    currentPlayer.factionid=1/2/3），且按阵营分组正确。"""
    old_pos = mm_handler.battlefield_pos
    mm_handler.battlefield_pos = lambda eid: (0.5, 0.5)   # macOS 无 WarDatabase
    try:
        det = {'preset': 10, 'attacker_faction': 2, 'defender_faction': 3,
               'players': [{'player_id': 101, 'gamertag': 'A_german',
                            'faction': 2},
                           {'player_id': 202, 'gamertag': 'B_soviet',
                            'faction': 3}],
               'map_entity_id': 112741325}
        raw = mm_handler._msg_mission_details_response_multi(990002, det,
                                                              'zulu')
    finally:
        mm_handler.battlefield_pos = old_pos

    groups = {}
    for fac in _split_factions(raw, 3):
        fid = int(fac[1])
        assert fid in (1, 2, 3), \
            f'f3 factionId={fid} 不在模板 id 空间（客户端我方判定永不相等）'
        # players：子消息内 repeated f2 {1: gamertag}
        players = [pb_parse_loose(pb_field_payload(blk))
                   for _fn, blk in pb_split_top(_find_f3_block(raw, fid))
                   if _fn == 2]
        groups[fid] = [p[1] for p in players]
    assert groups == {2: [b'A_german'], 3: [b'B_soviet']}, \
        f'f3 分组/阵营 id 错误：{groups}'


def _find_f3_block(raw, fid):
    """取 f3 中 factionId==fid 的子消息原文（供拆其内层 repeated players）。"""
    for _fn, blk in pb_split_top(raw):
        if _fn != 3:
            continue
        inner = pb_parse_loose(pb_field_payload(blk))
        if int(inner[1]) == fid:
            return pb_field_payload(blk)
    raise AssertionError(f'f3 无 factionId={fid} 条目')


def test_pms_f5_character_faction_in_template_space():
    """PMS f5 characterFactionId：客户端 setBasicInformation 拿它与
    MissionInfo f8 defenderFactionId（模板 id）比较定攻防角色、
    setPlayersInMatch 拿它与 factiontemplateid 比较高亮我方——必须
    落在 1/2/3 模板空间（且是已推送 faction 行 id）。"""
    m = matchmaking._msg_player_mission_session(
        990003, 101, 'A_german', 55, 2, 'Hans', 5, 990002, 'zulu',
        '1.2.3.4:15110')
    f = read_fields(m)
    assert f[5] == 2, f'PMS f5 应为 2(GE 模板 id)，实际 {f[5]}'


# ---------- 7. 登录即推 faction 表（subscribeplayerview ★FactionTable fill） ----------
# 修复点：faction 行原先只在 JoinMM ok / 小队入队时推——排队前客户端
# FactionTable(xmlTag='faction') 为空，战斗浏览器/战争图 getFromId 返回
# null → Error #1009；且行 id 必须==factiontemplateid（§6），登录即推
# 保证 currentPlayer.factionid 的"我方"判定在所有界面处处可用。
from protocol import Chunk, pb_parse  # noqa: E402
from handlers import views as views_handlers  # noqa: E402


class _LoginWriter:
    """收集 handle_subscribe 全部写帧（war/player/faction KVCS + 响应）。"""

    def __init__(self):
        self.frames = []

    def write(self, data):
        self.frames.append(data)

    async def drain(self):
        pass


class _LoginSess:
    username = ''           # 空=不注册 FLASH_SESSIONS（不动全局状态）

    async def ensure_player(self):
        return None         # DB 不可用路径：默认值 + 跳过 career/economy 推送


def _login_kvcs_tables(frames):
    """全部写入帧 → [(table, fields)]（只收 KVCS set，忽略其他帧）。"""
    out = []
    for buf in frames:
        root, _ = Chunk.parse(buf, 0)
        title = root.subs[0]
        if title.as_str() != 'KeyValueChangeSet':
            continue                       # subscriberesponse 等非 KVCS 帧
        action = title.subs[0]
        assert action.as_str() == 'set', f'action={action.as_str()!r}'
        for table in action.subs:
            out.append((table.as_str(), pb_parse(table.subs[0].data)))
    return out


def test_subscribeplayerview_pushes_faction_rows():
    """登录订阅必须推 faction 表 3 行（id==factiontemplateid），且在
    subscriberesponse 之前——客户端 viewSubscribtionSynced 前数据须就位。"""
    writer = _LoginWriter()
    asyncio.run(views_handlers.handle_subscribe(
        _LoginSess(), writer, 778899, 'subscribeplayerview', b''))

    tables = _login_kvcs_tables(writer.frames)
    seq = [t for t, _ in tables]
    faction_rows = [f for t, f in tables if t == 'faction']

    assert len(faction_rows) == 3, \
        f'登录应推 faction ×3，实际 {len(faction_rows)}（seq={seq}）'
    ids = sorted(int(f.get(1, 0) or 0) for f in faction_rows)
    assert ids == [1, 2, 3], f'faction 行 id 必须==模板 id，实际 {ids}'
    for f in faction_rows:
        assert int(f.get(1, 0) or 0) == int(f.get(2, 0) or 0), \
            f'faction 行 id({f.get(1)}) != factiontemplateid({f.get(2)})'

    # 回归：war/player 基础推送仍在且先于 faction（不因新增推送破坏原链路）
    assert seq[:2] == ['war', 'player'], f'war/player 应最先推送，实际 {seq}'

    # faction 推送必须先于 subscriberesponse（最后一帧为响应帧）
    last_root, _ = Chunk.parse(writer.frames[-1], 0)
    assert last_root.subs[0].as_str() == 'subscriberesponse', \
        f'最后一帧应为 subscriberesponse，实际 {last_root.subs[0].as_str()!r}'


# ---------- 8. FactionResourceProduction 行取官服模板值（2026-09-02 勘误） ----------
# 修复点：nextProductionValue 原误用 armyresourcecategory f6（speed）×10，
# 官服语义 = ArmyResourceProductionTemplate f5（produceEveryRun）、间隔 f4=600s、
# 开战库存 f6（warStartValue），三阵营数值相同
# （docs/hng-rts-protocol.md §3.12.1，客户端只渲染不本地模拟）。
import calendar  # noqa: E402
import time as _time  # noqa: E402


def test_faction_resource_production_rows_use_official_template():
    """步兵（cat 1）三阵营：nextProductionValue=900 / warStartValue=850000 /
    nextRunTimeStamp≈now+600s / f6 模板 id 非 0；总行数=3 阵营×18 分类。"""
    if not mm_factions.armyresource_production():
        print('  [skip] WarDatabase ArmyResourceProductionTemplate 不可用')
        return
    prods, cons = mm_factions._msg_faction_resource_rows()
    assert len(prods) == 54 and len(cons) == 54, \
        f'应 3 阵营×18 分类=54 行，实际 prods={len(prods)} cons={len(cons)}'
    infantry = {}
    for row in prods:
        f = pb_parse_loose(row)
        if int(f[5]) == 1:                    # armyResourceCategoryId=1 infantryman
            infantry[int(f[2])] = f           # 按 factionId 索引
    assert set(infantry) == {1, 2, 3}, \
        f'步兵生产行缺阵营：{set(infantry)}'
    for fid, f in infantry.items():
        assert int(f[8]) == 900, \
            f'faction{fid} f8 应 900（官服步兵每跳产量），实际 {f[8]}'
        assert int(f[9]) == 850000, \
            f'faction{fid} f9 应 850000（官服开战库存），实际 {f[9]}'
        assert int(f[6]) > 0, \
            f'faction{fid} f6 模板 id 应非 0，实际 {f[6]}'
        ts = calendar.timegm(
            _time.strptime(f[7].decode(), '%Y%m%d%H%M%S'))
        delta = ts - _time.time()
        assert 540 <= delta <= 660, \
            f'faction{fid} nextRunTimeStamp 应≈now+600s，实际 Δ{delta:.0f}s'


def test_faction_resource_production_rows_heavy_armor_anchor():
    """重坦（cat 17）锚点：产量 2/跳、开战库存 225——与步兵梯度拉开，
    确认没把全部分类填成同值（旧 bug 的速度×10 恰好会命中这种假象）。"""
    if not mm_factions.armyresource_production():
        print('  [skip] WarDatabase ArmyResourceProductionTemplate 不可用')
        return
    prods, _cons = mm_factions._msg_faction_resource_rows()
    for row in prods:
        f = pb_parse_loose(row)
        if int(f[5]) == 17 and int(f[2]) == 1:    # US 重坦
            assert int(f[8]) == 2, f'US 重坦 f8 应 2，实际 {f[8]}'
            assert int(f[9]) == 225, f'US 重坦 f9 应 225，实际 {f[9]}'
            return
    raise AssertionError('未找到 US(faction=1) 重坦(cat=17) 生产行')
