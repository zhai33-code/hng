"""战局结算解析回归测试（2026-08-26 远端崩溃事故）。

事故：AnnounceBattleFieldEarningsRequest 的 BattleReportCharacter f10
（creditEarnings）是嵌套 BattleReportEarnings 消息而非整数，旧代码
int(_get(c, 10, 0)) 抛 ValueError → HandlerErrorResponse → 无结算。

验证：
1) _earnings_value 对崩溃现场原始字节解析出 valueEarned=2101（不抛异常）
2) 完整 handle_announce_battlefield_earnings（含嵌套 f10/f11）：
   - 不向 dispatch 冒泡任何异常
   - 无论解析结果如何都发出 AnnounceBattleFieldEarningsResponse（响应先行）
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from handlers import combat as combat_handlers


# 2026-08-26 22:03:44 崩溃现场：f10 的原始字节（旧代码 int() 此值抛 ValueError）
CRASH_EARNINGS_F10 = (b'\x08\xb5\x10B\t\x08\xb7\x04\x10\xe4\x05\x18\xe1\r'
                      b'B\x08\x08\xa3\x0f\x10K\x18\xd4\x02H\xd2\x01')


# ---------- protobuf 构造 helpers ----------
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
    """length-delimited（wire type 2）子消息编码。"""
    out = bytearray([field << 3 | 2])
    ln = len(payload)
    while True:
        b = ln & 0x7F
        ln >>= 7
        out.append(b | 0x80 if ln else b)
        if not ln:
            break
    return bytes(out) + payload


class _MockWriter:
    """收集响应字节的 writer 替身（write/drain 均 async）。"""

    def __init__(self):
        self.sent = b''

    def write(self, data):
        self.sent += bytes(data)

    async def drain(self):
        return


class _MockSess:
    combat_mission = 100003
    game_mission = 100003


def _build_earnings_request(mission_id=100003):
    """合成 AnnounceBattleFieldEarningsRequest（结构同远端抓包）。"""
    # BattleReportEarnings：f1 valueEarned
    earn_warfund = pb_varint(1, 5678)
    # BattleReportCharacter：f1 charId, f2 startXp, f3 endXp,
    # f10/f11 嵌套 earnings（事故点），f13 ribbons{f1 id, f2 start, f3 end,
    # f4-f8 加成, f9 ribbonStatValues{f1 id, f2 value, f3 xp}}
    ribbon = (pb_varint(1, 13) + pb_varint(2, 3) + pb_varint(3, 8)
              + pb_varint(4, 25) + pb_varint(5, 0) + pb_varint(6, 0)
              + pb_varint(7, 0) + pb_varint(8, 5)
              + pb_msg(9, pb_varint(1, 5001) + pb_varint(2, 7)
                       + pb_varint(3, 12))
              + pb_msg(9, pb_varint(1, 5002) + pb_varint(2, 2)
                       + pb_varint(3, 4)))
    char = (pb_varint(1, 4) + pb_varint(2, 1000) + pb_varint(3, 2500)
            + pb_msg(10, CRASH_EARNINGS_F10)      # 事故现场字节（嵌套消息）
            + pb_msg(11, earn_warfund)
            + pb_msg(13, ribbon))
    # BattleReportPlayer：f1 playerId, f4 score, f6 kills, f7 deaths,
    # f9 gamertag, f10 isBot, f11 repeated character
    player = (pb_varint(1, 1) + pb_varint(4, 500) + pb_varint(6, 3)
              + pb_varint(7, 1) + pb_msg(9, b'TestGuy') + pb_varint(10, 0)
              + pb_msg(11, char))
    # BattleReportFaction：f1 factionId, f2 repeated player
    faction = pb_varint(1, 2) + pb_msg(2, player)
    # BattleReport：f1 msid, f2 defender, f3 winner, f4 time, f5 start,
    # f6 repeated faction
    report = (pb_varint(1, 730433917100003) + pb_varint(2, 2)
              + pb_varint(3, 3) + pb_varint(4, 900) + pb_varint(5, 1787750000)
              + pb_msg(6, faction))
    # PlayerBattleResult：f1 playerId, f2 credits, f3 warfunds, f6 character
    char_result = (pb_varint(1, 4) + pb_varint(2, 2000) + pb_varint(3, 5000)
                   + pb_varint(7, 60))
    pbr = (pb_varint(1, 1) + pb_varint(2, 120) + pb_varint(3, 340)
           + pb_msg(6, char_result))
    # 顶层：f1 missionId, f2 msid, f3 preset, f4 report, f5 repeated pbr
    return (pb_varint(1, mission_id) + pb_varint(2, 730433917100003)
            + pb_varint(3, 10) + pb_msg(4, report) + pb_msg(5, pbr))


def test_earnings_value_crash_bytes():
    """崩溃现场字节：旧代码 int() 抛 ValueError，新代码解析出 2101。"""
    v = combat_handlers._earnings_value(CRASH_EARNINGS_F10)
    assert v == 2101, f'valueEarned 应为 2101，实际 {v}'


class _FakeStore:
    """记录入库调用的 STORE 替身。"""

    def __init__(self, grant_daily=False):
        self.saved = None
        self.currency = []
        self.xp = []
        self.daily_calls = []
        self.grant_daily = grant_daily

    async def save_battle_report(self, report, players):
        self.saved = (report, players)
        return 123

    async def get_active_membership(self, player_id):
        return {'template_id': 11} if int(player_id) == 1 else None

    async def add_currency(self, player_id, credits=0, warfunds=0):
        self.currency.append((player_id, credits, warfunds))
        return {'credits': credits, 'warfunds': warfunds}

    async def add_combat_xp(self, pid, cid, xp):
        self.xp.append((pid, cid, xp))
        return xp

    async def consume_daily_bonus(self, pid, cid):
        self.daily_calls.append((pid, cid))
        return self.grant_daily

    async def add_battle_pass_score(self, pid, season, score):
        return score

    async def add_stat(self, cid, key, val):
        return None

    async def set_ribbon_xp_floor(self, pid, cid, rid, xp):
        return None


def test_handle_earnings_no_crash_and_response_first():
    """完整 handler：嵌套 f10/f11 不再抛异常，且必发结算响应。"""
    writer = _MockWriter()
    proto = _build_earnings_request()
    # 任何异常冒泡（如旧版 ValueError）都会让本测试失败
    asyncio.run(combat_handlers.handle_announce_battlefield_earnings(
        _MockSess(), writer, 200, 'AnnounceBattleFieldEarningsRequest', proto))
    assert writer.sent, '未发出任何响应（战斗服 pending wait 会超时卡死）'
    assert b'AnnounceBattleFieldEarningsResponse' in writer.sent, \
        f'响应类型错误: {writer.sent[:80]!r}'
    # 恶劣输入（截断/乱字节）也不允许抛异常
    writer2 = _MockWriter()
    asyncio.run(combat_handlers.handle_announce_battlefield_earnings(
        _MockSess(), writer2, 201, 'AnnounceBattleFieldEarningsRequest',
        b'\xff\xff\xff\xff garbage'))
    assert b'AnnounceBattleFieldEarningsResponse' in writer2.sent, \
        '乱字节输入也必须回响应（响应先行原则）'


def test_ribbon_start_end_and_vet_bonus_persisted():
    """f13 勋带 start/end 完整入库；货币 +25% 老兵加成。"""
    fake = _FakeStore()
    old = combat_handlers.STORE
    combat_handlers.STORE = fake
    try:
        writer = _MockWriter()
        asyncio.run(combat_handlers.handle_announce_battlefield_earnings(
            _MockSess(), writer, 200, 'AnnounceBattleFieldEarningsRequest',
            _build_earnings_request()))
    finally:
        combat_handlers.STORE = old
    assert fake.saved, '战报未入库'
    rep, players = fake.saved
    assert rep['was_autogenerated'] is True
    ch = players[0]['characters'][0]
    # f13 PlayerBattleReportRibbon 全量：f1 id=13, f2 start=3, f3 end=8,
    # f4/f8 加成 + f9 明细（f5-f7 为 0 也原样保留）
    assert ch['ribbons'] == [{
        'id': 13, 'start': 3, 'end': 8, 'xpVet': 25, 'xpBooster': 0,
        'xpCampaign': 0, 'xpUnderdog': 0, 'xpSquadVet': 5,
        'stats': [{'id': 5001, 'value': 7, 'xp': 12.0},
                  {'id': 5002, 'value': 2, 'xp': 4.0}],
    }], ch['ribbons']
    # f10 嵌套 valueEarned=2101 / f11=5678 → 老兵 +25%
    assert ch['creditEarned'] == 2101 and ch['creditVetBonus'] == 525
    assert ch['warfundEarned'] == 5678 and ch['warfundVetBonus'] == 1419
    # startXp=1000→endXp=2500：基础 1500 + 25%老兵加成 375 一并入账
    assert ch['xpVetBonus'] == 375, ch['xpVetBonus']
    assert fake.xp == [(1, 4, 1875)], fake.xp
    # PlayerBattleResult 货币 120cr/340wf → +25% 入账
    assert fake.currency == [(1, 150, 425)], fake.currency
    # 默认（资格已用/不可用）：无每日加成，行为不变
    assert fake.daily_calls == [(1, 4)], fake.daily_calls
    assert 'xpDailyBonus' not in ch


def test_daily_first_battle_bonus():
    """每日首战 +50% XP：资格可用 → 基础 1500 + 老兵 375 + 首战 750。"""
    fake = _FakeStore(grant_daily=True)
    old = combat_handlers.STORE
    combat_handlers.STORE = fake
    try:
        writer = _MockWriter()
        asyncio.run(combat_handlers.handle_announce_battlefield_earnings(
            _MockSess(), writer, 200, 'AnnounceBattleFieldEarningsRequest',
            _build_earnings_request()))
    finally:
        combat_handlers.STORE = old
    assert fake.saved, '战报未入库'
    _, players = fake.saved
    ch = players[0]['characters'][0]
    # 资格消费恰一次，+50% 随战报入库（结算界面 f9 xpDailyBonus）
    assert fake.daily_calls == [(1, 4)], fake.daily_calls
    assert ch['xpDailyBonus'] == 750, ch['xpDailyBonus']
    # 入账 = 基础 1500 + 老兵 375 + 首战 750
    assert fake.xp == [(1, 4, 2625)], fake.xp


def test_daily_bonus_not_consumed_without_xp_gain():
    """无 XP 收益（endXp<=startXp）不消费首战资格。"""
    earn = pb_varint(1, 5678)
    char = pb_varint(1, 4) + pb_varint(2, 1000) + pb_varint(3, 1000) \
        + pb_msg(11, earn)
    player = (pb_varint(1, 1) + pb_varint(4, 500) + pb_msg(9, b'G')
              + pb_varint(10, 0) + pb_msg(11, char))
    faction = pb_varint(1, 2) + pb_msg(2, player)
    report = (pb_varint(1, 730433917100003) + pb_varint(2, 2)
              + pb_varint(3, 3) + pb_varint(4, 900) + pb_varint(5, 1787750000)
              + pb_msg(6, faction))
    proto = (pb_varint(1, 100003) + pb_varint(2, 730433917100003)
             + pb_varint(3, 10) + pb_msg(4, report))

    fake = _FakeStore(grant_daily=True)
    old = combat_handlers.STORE
    combat_handlers.STORE = fake
    try:
        asyncio.run(combat_handlers.handle_announce_battlefield_earnings(
            _MockSess(), _MockWriter(), 200,
            'AnnounceBattleFieldEarningsRequest', proto))
    finally:
        combat_handlers.STORE = old
    assert fake.daily_calls == [], fake.daily_calls
    assert fake.xp == [], fake.xp


def test_msid_synthesized_when_missing():
    """BattleReport f1 msid 缺省（本地战斗服）→ 合成负数幂等键。

    同局重发同键（幂等），不同 battle_start_time 不同键（互不覆盖）。"""

    def _req_no_msid(start_time):
        earn = pb_varint(1, 5678)
        char = (pb_varint(1, 4) + pb_varint(2, 1000) + pb_varint(3, 2500)
                + pb_msg(11, earn))
        player = (pb_varint(1, 1) + pb_varint(4, 500) + pb_msg(9, b'G')
                  + pb_varint(10, 0) + pb_msg(11, char))
        faction = pb_varint(1, 2) + pb_msg(2, player)
        report = (pb_varint(2, 2) + pb_varint(3, 3) + pb_varint(4, 900)
                  + pb_varint(5, start_time) + pb_msg(6, faction))
        return (pb_varint(1, 100003) + pb_varint(3, 10)
                + pb_msg(4, report))

    def _run(proto):
        fake = _FakeStore()
        old = combat_handlers.STORE
        combat_handlers.STORE = fake
        try:
            asyncio.run(combat_handlers.handle_announce_battlefield_earnings(
                _MockSess(), _MockWriter(), 200,
                'AnnounceBattleFieldEarningsRequest', proto))
        finally:
            combat_handlers.STORE = old
        assert fake.saved, '战报未入库'
        return fake.saved[0]['mission_session_id']

    msid_a = _run(_req_no_msid(1787750001))
    msid_a2 = _run(_req_no_msid(1787750001))     # 同局重发
    msid_b = _run(_req_no_msid(1787750999))      # 另一局
    assert msid_a < 0, f'合成 msid 应为负数，实际 {msid_a}'
    assert msid_a == msid_a2, '同局重发应同键（幂等）'
    assert msid_a != msid_b, '不同局应不同键（互不覆盖）'


def test_battlereport_ribbon_and_vet_encoding():
    """战报响应：勋带 f2/f3/f4-f8/f9 与 earnings f2 veteranBonus 编码正确。"""
    from handlers import battlereport as br
    m = br._msg_report_character({
        'characterId': 4, 'startXp': 1000, 'endXp': 2500,
        'creditEarned': 2101, 'creditVetBonus': 525,
        'warfundEarned': 5678, 'warfundVetBonus': 1419,
        'xpDailyBonus': 750,
        'ribbons': [{'id': 13, 'start': 3, 'end': 8, 'xpVet': 25,
                      'xpBooster': 0, 'xpCampaign': 0, 'xpUnderdog': 0,
                      'xpSquadVet': 5,
                      'stats': [{'id': 5001, 'value': 7, 'xp': 12.0}]}],
    })
    # f9 xpDailyBonus（每日首战 +50%）编码
    assert pb_varint(9, 750) in m
    # f13 勋带：f1/f2/f3 + f4 xpVet + f8 squadVet + f9 明细
    # （S→C 侧 f3 xpValue 是 fixed32 float）
    assert pb_msg(13, pb_varint(1, 13) + pb_varint(2, 3)
                  + pb_varint(3, 8) + pb_varint(4, 25)
                  + pb_varint(5, 0) + pb_varint(6, 0) + pb_varint(7, 0)
                  + pb_varint(8, 5)
                  + pb_msg(9, pb_varint(1, 5001) + pb_varint(2, 7)
                           + _pb_fixed32(3, 12.0))) in m
    assert pb_msg(10, pb_varint(1, 2101) + pb_varint(2, 525)) in m
    assert pb_msg(11, pb_varint(1, 5678) + pb_varint(2, 1419)) in m
    # 旧格式（纯 id 列表）不崩，回退 0→1，无 f4-f9
    m2 = br._msg_report_character({'characterId': 4, 'ribbons': [13]})
    assert pb_msg(13, pb_varint(1, 13) + pb_varint(2, 0)
                  + pb_varint(3, 1)) in m2
    # 空角色列表没有会员证据，应为 false；有实际会员加成时才为 true。
    mp = br._msg_report_player({'player_id': 1, 'gamertag': 'T',
                                'characters_json': '[]'})[0]
    assert pb_varint(3, 0) in mp
    mp_vet = br._msg_report_player({
        'player_id': 1, 'gamertag': 'T',
        'characters_json': '[{"xpVetBonus":25}]'})[0]
    assert pb_varint(3, 1) in mp_vet


def _pb_fixed32(field, value):
    """wire type 5（fixed32 float）编码，供 f12 xpValue 测试。"""
    import struct
    return bytes([field << 3 | 5]) + struct.pack('<f', value)


def test_capturepoints_and_xpstats_parse_and_persist():
    """f7 据点归属 + 角色 f12 xpStatValues 解析入库（结算界面缺项补全）。"""
    # BattleReportCharacter f12：BattleStatRequirement（spbc 描述符
    # 0x1807B42D8 实锤：C→S 侧 f3 xpValue 是 varint int32，非 fixed32 float）
    stat = (pb_varint(1, 5001) + pb_varint(2, 7)
            + pb_varint(3, 12))
    earn = pb_varint(1, 5678)
    char = (pb_varint(1, 4) + pb_varint(2, 1000) + pb_varint(3, 2500)
            + pb_msg(11, earn) + pb_msg(12, stat))
    player = (pb_varint(1, 1) + pb_varint(4, 500) + pb_msg(9, b'TestGuy')
              + pb_varint(10, 0) + pb_msg(11, char))
    faction = pb_varint(1, 2) + pb_msg(2, player)
    # BattleReport f7：repeated CapturepointStatus{f1 tmpl, f2 owner}
    report = (pb_varint(1, 730433917100003) + pb_varint(2, 2)
              + pb_varint(3, 3) + pb_varint(4, 900) + pb_varint(5, 1787750000)
              + pb_msg(6, faction)
              + pb_msg(7, pb_varint(1, 101) + pb_varint(2, 3))
              + pb_msg(7, pb_varint(1, 102) + pb_varint(2, 0)))
    proto = (pb_varint(1, 100003) + pb_varint(2, 730433917100003)
             + pb_varint(3, 10) + pb_msg(4, report))

    fake = _FakeStore()
    old = combat_handlers.STORE
    combat_handlers.STORE = fake
    try:
        asyncio.run(combat_handlers.handle_announce_battlefield_earnings(
            _MockSess(), _MockWriter(), 200,
            'AnnounceBattleFieldEarningsRequest', proto))
    finally:
        combat_handlers.STORE = old
    assert fake.saved, '战报未入库'
    rep, players = fake.saved
    # f7 据点归属透传入库
    assert rep['capturepoints'] == [{'id': 101, 'owner': 3},
                                    {'id': 102, 'owner': 0}], rep['capturepoints']
    # 角色 f12 统计项：id/value/xp（varint int → float 落库）
    ch = players[0]['characters'][0]
    assert ch['stats'] == [{'id': 5001, 'value': 7, 'xp': 12.0}], ch['stats']
    # XP 老兵加成（endXp-startXp=1500 → +25%）：随战报入库 + 结算入账
    assert ch['xpVetBonus'] == 375, ch['xpVetBonus']


def test_capturepoints_fallback_from_status_overlay():
    """BattleReport 无 f7 → ReportMissionStatus 持续上报的 _CAPTURE_OWNERS 兜底。"""
    earn = pb_varint(1, 5678)
    char = pb_varint(1, 4) + pb_varint(2, 1000) + pb_varint(3, 2500) \
        + pb_msg(11, earn)
    player = (pb_varint(1, 1) + pb_varint(4, 500) + pb_msg(9, b'G')
              + pb_varint(10, 0) + pb_msg(11, char))
    faction = pb_varint(1, 2) + pb_msg(2, player)
    report = (pb_varint(1, 730433917100003) + pb_varint(2, 2)
              + pb_varint(3, 3) + pb_varint(4, 900) + pb_varint(5, 1787750000)
              + pb_msg(6, faction))            # 无 f7
    proto = (pb_varint(1, 100003) + pb_varint(2, 730433917100003)
             + pb_varint(3, 10) + pb_msg(4, report))

    combat_handlers._CAPTURE_OWNERS.clear()
    combat_handlers._CAPTURE_OWNERS.update({201: 1, 202: 2})
    fake = _FakeStore()
    old = combat_handlers.STORE
    combat_handlers.STORE = fake
    try:
        asyncio.run(combat_handlers.handle_announce_battlefield_earnings(
            _MockSess(), _MockWriter(), 200,
            'AnnounceBattleFieldEarningsRequest', proto))
    finally:
        combat_handlers.STORE = old
    rep, _ = fake.saved
    assert rep['capturepoints'] == [{'id': 201, 'owner': 1},
                                    {'id': 202, 'owner': 2}], rep['capturepoints']
    # 结算后清空（下局从布局重新开始）
    assert not combat_handlers._CAPTURE_OWNERS


def test_battlereport_response_encodes_capturepoints_and_stats():
    """GetBattleReportResponse：f12 capturepoints + 角色 f12 xpStatValues。"""
    from handlers import battlereport as br
    rep = {'id': 123, 'mission_preset_id': 10, 'defender_faction_id': 2,
           'winner_faction_id': 3, 'mission_time_seconds': 900,
           'battle_start_time': 1787750000,
           'capturepoints_json': '[{"id": 101, "owner": 3}, '
                                 '{"id": 102, "owner": 0}]'}
    players = [{'player_id': 1, 'gamertag': 'T', 'score': 500, 'captures': 0,
                'kills': 3, 'deaths': 1, 'squad_name': '',
                'characters_json': '[{"characterId": 4, "factionId": 2, '
                '"stats": [{"id": 5001, "value": 7, "xp": 12.5}]}]'}]
    m = br._msg_client_battle_report(rep, players)
    # f12 capturepoints：{f1 tmpl, f2 owner}
    assert pb_msg(12, pb_varint(1, 101) + pb_varint(2, 3)) in m
    assert pb_msg(12, pb_varint(1, 102) + pb_varint(2, 0)) in m
    # 角色 f12 xpStatValues：{f1 id, f2 value, f3 fixed32 xp}
    assert pb_msg(12, pb_varint(1, 5001) + pb_varint(2, 7)
                  + _pb_fixed32(3, 12.5)) in m
    # 旧战报（无 capturepoints_json / stats）不崩、不误发
    rep_old = dict(rep)
    rep_old['capturepoints_json'] = None
    m_old = br._msg_client_battle_report(rep_old, players)
    assert pb_msg(12, pb_varint(1, 101)) not in m_old


def test_battlereport_was_autogenerated_matches_battle_type():
    """ClientBattleReport f10：普通匹配=true，war 会战=false。"""
    from protocol import pb_parse_multi
    from handlers import battlereport as br
    base = {'id': 123, 'mission_preset_id': 10, 'defender_faction_id': 2,
            'winner_faction_id': 3, 'mission_time_seconds': 900,
            'battle_start_time': 1787750000, 'capturepoints_json': '[]'}

    staged = br._msg_client_battle_report(
        dict(base, was_autogenerated=True), [])
    war = br._msg_client_battle_report(
        dict(base, was_autogenerated=False), [])
    legacy = br._msg_client_battle_report(base, [])

    assert pb_parse_multi(staged)[10] == [1]
    assert pb_parse_multi(war)[10] == [0]
    # 旧行没有类型字段时优先按普通匹配展示。
    assert pb_parse_multi(legacy)[10] == [1]


def test_war_settlement_persists_non_autogenerated_flag():
    """结算时从 live mission 的 battle_id 快照 war 类型，不能随清理丢失。"""
    import mm_state
    fake = _FakeStore()
    old_store = combat_handlers.STORE
    old_det = mm_state._MM_MISSION_DETAILS.get(100003)
    combat_handlers.STORE = fake
    mm_state._MM_MISSION_DETAILS[100003] = {
        'battle_id': 77, 'defender_faction': 2, 'map_entity_id': 123,
    }
    try:
        asyncio.run(combat_handlers.handle_announce_battlefield_earnings(
            _MockSess(), _MockWriter(), 200,
            'AnnounceBattleFieldEarningsRequest', _build_earnings_request()))
    finally:
        combat_handlers.STORE = old_store
        if old_det is not None:
            mm_state._MM_MISSION_DETAILS[100003] = old_det
        else:
            mm_state._MM_MISSION_DETAILS.pop(100003, None)
    assert fake.saved[0]['was_autogenerated'] is False


def test_factions_extra_snapshotted_from_force_pool():
    """结算时从 force_pool 快照 f11.f3/f4/f5 数据（AT/战损/缴获）。"""
    import force_pool
    fake = _FakeStore()
    old = combat_handlers.STORE
    combat_handlers.STORE = fake
    cat1 = None
    initial_cat1 = 0
    try:
        force_pool.init_mission(100003, 0, 2, 3)   # attacker=2, defender=3
        pool2 = force_pool.pool(100003)[2]
        cat1 = next(iter(pool2))
        initial_cat1 = pool2[cat1]
        force_pool.consume(100003, 2, cat1, 5)     # 攻方消耗 5
        force_pool.consume(100003, 3, cat1, 2)     # 守方消耗 2
        asyncio.run(combat_handlers.handle_announce_battlefield_earnings(
            _MockSess(), _MockWriter(), 200,
            'AnnounceBattleFieldEarningsRequest', _build_earnings_request()))
    finally:
        combat_handlers.STORE = old
        force_pool.release(100003)
    rep, _ = fake.saved
    fx = rep['factions_extra']
    assert set(fx) == {2, 3}, fx
    # f3 AT 名单：每方 >=1 条，tmpl 为阵营相关 commandnode 模板 id
    for fac in (2, 3):
        assert fx[fac]['ats'], fx
        assert all(a['tmpl'] and a['name'] for a in fx[fac]['ats'])
    # f4 战损：攻方消耗 5，守方消耗 2
    assert fx[2]['destroyed'].get(cat1) == 5, fx[2]['destroyed']
    assert fx[3]['destroyed'].get(cat1) == 2, fx[3]['destroyed']
    # f5 缴获：winner=3 → 缴获败方（2）的剩余 cat1（初始-5）
    assert fx[3]['captured'].get(cat1) == initial_cat1 - 5, \
        fx[3]['captured']
    assert 'captured' not in fx[2], '败方无缴获'


def test_battlereport_response_encodes_faction_extras():
    """GetBattleReportResponse f11.f3/f4/f5：参战 AT / 损失 / 缴获编码。"""
    import json as _json
    from protocol import pb_parse_multi
    from handlers import battlereport as br
    rep = {'id': 123, 'mission_preset_id': 10, 'defender_faction_id': 2,
           'winner_faction_id': 3, 'mission_time_seconds': 900,
           'battle_start_time': 1787750000, 'capturepoints_json': '[]',
           'factions_json': _json.dumps({
               '2': {'ats': [{'name': 'infantry', 'tmpl': 203}],
                     'destroyed': {'1': 10}},
               '3': {'ats': [{'name': 'guard', 'tmpl': 303}],
                     'destroyed': {'1': 7}, 'captured': {'1': 100, '5': 4}},
           })}
    players = [{'player_id': 1, 'gamertag': 'T', 'score': 500, 'captures': 0,
                'kills': 3, 'deaths': 1, 'squad_name': '',
                'characters_json': '[{"characterId": 4, "factionId": 3}]'}]
    m = br._msg_client_battle_report(rep, players)
    fac_by_id = {}
    for fac_raw in pb_parse_multi(m).get(11, []):
        fac_by_id[int(pb_parse_multi(fac_raw).get(1, [0])[0])] = fac_raw
    assert set(fac_by_id) == {2, 3}, fac_by_id   # 无玩家行的 faction 2 也发
    for fac, exp_ats, exp_des, exp_cap in (
            (2, [('infantry', 203)], {1: 10}, {}),
            (3, [('guard', 303)], {1: 7}, {1: 100, 5: 4})):
        ff = pb_parse_multi(fac_by_id[fac])
        # f3 AT：{f1 name, f2 tmpl, f3 owner='', f4 ownerPlayerId=0}
        ats = [(pb_parse_multi(a).get(1, [b''])[0].decode(),
                int(pb_parse_multi(a).get(2, [0])[0]))
               for a in ff.get(3, [])]
        assert ats == exp_ats, (fac, ats)
        for a_raw in ff.get(3, []):
            a = pb_parse_multi(a_raw)
            assert a.get(4, [0])[0] == 0 and a.get(3, [b''])[0] == b''
        # f4 损失 / f5 缴获：{f1 cat, f2 n}
        des = {int(pb_parse_multi(d).get(1, [0])[0]):
               int(pb_parse_multi(d).get(2, [0])[0]) for d in ff.get(4, [])}
        cap = {int(pb_parse_multi(c).get(1, [0])[0]):
               int(pb_parse_multi(c).get(2, [0])[0]) for c in ff.get(5, [])}
        assert des == exp_des, (fac, des)
        assert cap == exp_cap, (fac, cap)
    # 旧战报（无 factions_json）不崩、不误发 f3/f4/f5
    rep_old = dict(rep)
    rep_old['factions_json'] = None
    m_old = br._msg_client_battle_report(rep_old, players)
    for fac_raw in pb_parse_multi(m_old).get(11, []):
        ff = pb_parse_multi(fac_raw)
        assert not ff.get(3) and not ff.get(4) and not ff.get(5), ff


def test_factions_extra_war_at_ownership():
    """war 局 AT 实例归属：factions_extra.ats 带真实 owner + gamertag。

    owner 在本局玩家表（BattleReport f9 gamertag）→ tag 直取；
    owner 不在局（部署 AT 的将军未参战）→ tag=''（_FakeStore 无 pool，
    DB 查询分支被吞，线上走 players 表反查）。"""
    import force_pool
    fake = _FakeStore()
    old = combat_handlers.STORE
    combat_handlers.STORE = fake
    try:
        comp = force_pool.composition_for_template(203) or {1: 90}
        force_pool.init_mission_war(
            100005, 2, 3,
            {2: [comp], 3: [comp]},
            {2: ['infantry'], 3: ['guard']},
            {2: [{'id': 11, 'name': '第7装甲营', 'owner_player_id': 1,
                  'template_id': 203}],
             3: [{'id': 12, 'name': 'Guard Alpha', 'owner_player_id': 999,
                  'template_id': 303}]})
        asyncio.run(combat_handlers.handle_announce_battlefield_earnings(
            _MockSess(), _MockWriter(), 200,
            'AnnounceBattleFieldEarningsRequest',
            _build_earnings_request(100005)))
    finally:
        combat_handlers.STORE = old
        force_pool.release(100005)
    rep, _ = fake.saved
    fx = rep['factions_extra']
    at2 = fx[2]['ats'][0]
    assert at2['name'] == '第7装甲营', at2          # AT 实例名（非兵种名）
    assert at2['owner'] == 1 and at2['tag'] == 'TestGuy', at2
    at3 = fx[3]['ats'][0]
    assert at3['owner'] == 999 and at3['tag'] == '', at3


def test_battlereport_response_encodes_at_owner():
    """GetBattleReportResponse f11.f3：AT 归属 ownerGamerTag/ownerPlayerId
    编码（客户端显示属主与加好友按钮的数据源）。"""
    import json as _json
    from protocol import pb_parse_multi
    from handlers import battlereport as br
    rep = {'id': 321, 'mission_preset_id': 10, 'defender_faction_id': 2,
           'winner_faction_id': 2, 'mission_time_seconds': 900,
           'battle_start_time': 1787750000, 'capturepoints_json': '[]',
           'factions_json': _json.dumps({
               '2': {'ats': [{'name': '第7装甲营', 'tmpl': 203,
                              'owner': 1, 'tag': 'TestGuy'}],
                     'destroyed': {'1': 10}}})}
    players = [{'player_id': 1, 'gamertag': 'TestGuy', 'score': 500,
                'captures': 0, 'kills': 3, 'deaths': 1, 'squad_name': '',
                'characters_json': '[{"characterId": 4, "factionId": 2}]'}]
    m = br._msg_client_battle_report(rep, players)
    fac_raw = pb_parse_multi(m).get(11, [])[0]
    at_raw = pb_parse_multi(fac_raw).get(3, [])[0]
    a = pb_parse_multi(at_raw)
    assert a.get(1, [b''])[0].decode() == '第7装甲营'
    assert int(a.get(2, [0])[0]) == 203
    assert a.get(3, [b''])[0].decode() == 'TestGuy'   # ownerGamerTag
    assert int(a.get(4, [0])[0]) == 1                  # ownerPlayerId
    # 角色 f14/f15/f16（proto2 required）显式补 0
    pm = pb_parse_multi(pb_parse_multi(fac_raw).get(2, [])[0])
    ch = pb_parse_multi(pm.get(9, [])[0])
    for f in (14, 15, 16):
        assert int(ch.get(f, [0])[0]) == 0, f


def test_battlereport_map_entity_resolution():
    """战报 f2/f3 mapEntityId/TypeId：war 局按 battle 行口径解析（城战=
    城市 bf/1、野外=补给线 id/3），不再恒回退 Benoit Village——客户端
    结算界面"赢得了 %c 之战"城市名与地图 zoom 位置的数据源。"""
    import asyncio
    from handlers import battlereport as br

    class _EntStore:
        def __init__(self, rec, wb):
            self._rec, self._wb = rec, wb

        async def get_mission_record(self, mid):
            return self._rec

        async def get_war_battle(self, bid):
            return self._wb

    rep = {'id': 1, 'mission_id': 555}
    old = br.STORE
    try:
        # 城战（battle_type=1）：entity = 城市 battlefield id / 1
        br.STORE = _EntStore(
            {'id': 555, 'battle_id': 77, 'map_entity_id': 111},
            {'id': 77, 'battlefield_id': 222, 'battle_type': 1})
        assert asyncio.run(br._report_map_entity(rep)) == (222, 1)
        # 野外遭遇（battle_type=2）：entity = 补给线 id / 3
        br.STORE = _EntStore(
            {'id': 555, 'battle_id': 77, 'map_entity_id': 111},
            {'id': 77, 'battlefield_id': 333, 'battle_type': 2})
        assert asyncio.run(br._report_map_entity(rep)) == (333, 3)
        # battle 行缺失：回退 record.map_entity_id / 1
        br.STORE = _EntStore(
            {'id': 555, 'battle_id': 77, 'map_entity_id': 111}, None)
        assert asyncio.run(br._report_map_entity(rep)) == (111, 1)
        # 正常匹配局（无 record）：用结算时快照 rep['map_entity_id'] / 1
        br.STORE = _EntStore(None, None)
        snap = dict(rep, map_entity_id=444)
        assert asyncio.run(br._report_map_entity(snap)) == (444, 1)
        # 无 record 且无快照：保持未知，不冒充任意城市。
        ent_id, ent_type = asyncio.run(br._report_map_entity(rep))
        assert (ent_id, ent_type) == (0, 0)
    finally:
        br.STORE = old
    # builder 直通：f2/f3 用解析值而非写死常量
    m = br._msg_client_battle_report(
        {'id': 9, 'mission_preset_id': 10, 'defender_faction_id': 2,
         'winner_faction_id': 3, 'mission_time_seconds': 1,
         'battle_start_time': 1}, [], 222, 1)
    assert pb_varint(2, 1) in m and pb_varint(3, 222) in m


if __name__ == '__main__':
    test_earnings_value_crash_bytes()
    test_handle_earnings_no_crash_and_response_first()
    test_ribbon_start_end_and_vet_bonus_persisted()
    test_msid_synthesized_when_missing()
    test_battlereport_ribbon_and_vet_encoding()
    test_capturepoints_and_xpstats_parse_and_persist()
    test_capturepoints_fallback_from_status_overlay()
    test_battlereport_response_encodes_capturepoints_and_stats()
    test_battlereport_was_autogenerated_matches_battle_type()
    test_war_settlement_persists_non_autogenerated_flag()
    test_factions_extra_snapshotted_from_force_pool()
    test_battlereport_response_encodes_faction_extras()
    test_factions_extra_war_at_ownership()
    test_battlereport_response_encodes_at_owner()
    test_battlereport_map_entity_resolution()
    print('test_settlement_parse: 15 项全部通过')
