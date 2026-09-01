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


def _build_earnings_request():
    """合成 AnnounceBattleFieldEarningsRequest（结构同远端抓包）。"""
    # BattleReportEarnings：f1 valueEarned
    earn_warfund = pb_varint(1, 5678)
    # BattleReportCharacter：f1 charId, f2 startXp, f3 endXp,
    # f10/f11 嵌套 earnings（事故点），f13 ribbons{f1 id, f2 start, f3 end}
    char = (pb_varint(1, 4) + pb_varint(2, 1000) + pb_varint(3, 2500)
            + pb_msg(10, CRASH_EARNINGS_F10)      # 事故现场字节（嵌套消息）
            + pb_msg(11, earn_warfund)
            + pb_msg(13, pb_varint(1, 13) + pb_varint(2, 3) + pb_varint(3, 8)))
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
    return (pb_varint(1, 100003) + pb_varint(2, 730433917100003)
            + pb_varint(3, 10) + pb_msg(4, report) + pb_msg(5, pbr))


def test_earnings_value_crash_bytes():
    """崩溃现场字节：旧代码 int() 抛 ValueError，新代码解析出 2101。"""
    v = combat_handlers._earnings_value(CRASH_EARNINGS_F10)
    assert v == 2101, f'valueEarned 应为 2101，实际 {v}'


class _FakeStore:
    """记录入库调用的 STORE 替身。"""

    def __init__(self):
        self.saved = None
        self.currency = []
        self.xp = []

    async def save_battle_report(self, report, players):
        self.saved = (report, players)
        return 123

    async def add_currency(self, player_id, credits=0, warfunds=0):
        self.currency.append((player_id, credits, warfunds))
        return {'credits': credits, 'warfunds': warfunds}

    async def add_combat_xp(self, pid, cid, xp):
        self.xp.append((pid, cid, xp))
        return xp

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
    _, players = fake.saved
    ch = players[0]['characters'][0]
    # f13 PlayerBattleReportRibbon：f1 id=13, f2 start=3, f3 end=8
    assert ch['ribbons'] == [{'id': 13, 'start': 3, 'end': 8}], ch['ribbons']
    # f10 嵌套 valueEarned=2101 / f11=5678 → 老兵 +25%
    assert ch['creditEarned'] == 2101 and ch['creditVetBonus'] == 525
    assert ch['warfundEarned'] == 5678 and ch['warfundVetBonus'] == 1419
    # startXp=1000→endXp=2500：基础 1500 + 25%老兵加成 375 一并入账
    assert ch['xpVetBonus'] == 375, ch['xpVetBonus']
    assert fake.xp == [(1, 4, 1875)], fake.xp
    # PlayerBattleResult 货币 120cr/340wf → +25% 入账
    assert fake.currency == [(1, 150, 425)], fake.currency


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
    """战报响应：勋带 f2/f3 与 earnings f2 veteranBonus 编码正确。"""
    from handlers import battlereport as br
    m = br._msg_report_character({
        'characterId': 4, 'startXp': 1000, 'endXp': 2500,
        'creditEarned': 2101, 'creditVetBonus': 525,
        'warfundEarned': 5678, 'warfundVetBonus': 1419,
        'ribbons': [{'id': 13, 'start': 3, 'end': 8}],
    })
    assert pb_msg(13, pb_varint(1, 13) + pb_varint(2, 3)
                  + pb_varint(3, 8)) in m
    assert pb_msg(10, pb_varint(1, 2101) + pb_varint(2, 525)) in m
    assert pb_msg(11, pb_varint(1, 5678) + pb_varint(2, 1419)) in m
    # 旧格式（纯 id 列表）不崩，回退 0→1
    m2 = br._msg_report_character({'characterId': 4, 'ribbons': [13]})
    assert pb_msg(13, pb_varint(1, 13) + pb_varint(2, 0)
                  + pb_varint(3, 1)) in m2
    # veteranMember = True（f3 bool）
    mp = br._msg_report_player({'player_id': 1, 'gamertag': 'T',
                                'characters_json': '[]'})[0]
    assert pb_varint(3, 1) in mp


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


if __name__ == '__main__':
    test_earnings_value_crash_bytes()
    test_handle_earnings_no_crash_and_response_first()
    test_ribbon_start_end_and_vet_bonus_persisted()
    test_msid_synthesized_when_missing()
    test_battlereport_ribbon_and_vet_encoding()
    test_capturepoints_and_xpstats_parse_and_persist()
    test_capturepoints_fallback_from_status_overlay()
    test_battlereport_response_encodes_capturepoints_and_stats()
    print('test_settlement_parse: 8 项全部通过')
