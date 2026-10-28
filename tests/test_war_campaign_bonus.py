"""战争模式战役加成回归测试（2026-09-16，官方机制还原）。

官方链路（player.dll build_battle_report_spbc 0x1802C0350 实锤）：
- BattleFieldSettings f17 isCampaignMission 是战斗服战役加成总开关：
  为真才产出 xpCampaignBonus（ExperienceLevels f7-1=+50%）、勋带
  campaign（f8-1=+50%）、银币 campaignBonus（f10-1=+25%）与
  warfundsEarned 基础值；f16 daysSinceWarStarted / f24 supplyLineId
  为战役附属信息；f18 faction 行 f3/f4 带 underdog 倍率（战报 f7
  xpUnderdogBonus / 战资 f5 underdogBonus 数据源）。
- 主服入账 = 基础 + 全部加成位；S→C 战报如实回传各加成字段。

验证：
1) ResponseBattleFieldSettings：staged 局字节序列不变（f16=0/f17=False/
   faction 倍率 1.0）；war 局 f16/f17/f24/faction underdog 倍率正确上包
2) 结算兜底入账：war 局（det.battle_id>0）线上加成为 0 时按官方公式
   本地补齐（XP/银币/战资/勋带），线上已上报则信任线上值不重复加
3) S→C 战报回传 f6/f7/f8/f14-16 与 earnings f4/f5 实值
"""
import asyncio
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mm_state
import warserver_messages
from handlers import combat as combat_handlers
from handlers import battlereport as br
from protocol import pb_parse_multi

from test_settlement_parse import (
    _FakeStore,
    _MockSess,
    _MockWriter,
    _build_earnings_request,
    pb_msg,
    pb_varint,
)


def _fixed32(raw):
    return struct.unpack('<f', raw)[0]


def _repeated(parsed, field):
    """pb_parse_multi 的 repeated 字段 → list[bytes]（单值归一）。"""
    v = parsed.get(field)
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


# ---------------------------------------------------------------- 战场设置

def test_battlefield_settings_staged_unchanged():
    """staged 局：f16=0/f17=False/f24=0，faction 行倍率恒 1.0。"""
    m = warserver_messages._msg_response_battlefield_settings(10, 2, 3)
    f = pb_parse_multi(m)
    assert int(f.get(16, [0])[0]) == 0
    assert int(f.get(17, [0])[0]) == 0
    assert int(f.get(24, [0])[0]) == 0
    facs = _repeated(f, 18)
    assert len(facs) == 3, facs
    for fac in facs:
        ff = pb_parse_multi(fac)
        assert abs(_fixed32(ff[3][0]) - 1.0) < 1e-6, ff
        assert abs(_fixed32(ff[4][0]) - 1.0) < 1e-6, ff


def test_battlefield_settings_campaign_fields():
    """war 局：f17=True、f16 战争天数、f24 补给线 id、faction 行 underdog。"""
    m = warserver_messages._msg_response_battlefield_settings(
        10, 2, 3, is_campaign=True, days_since_war_started=3,
        supply_line_id=880123,
        underdog_mults={2: (1.25, 1.15)})
    f = pb_parse_multi(m)
    assert int(f[16][0]) == 3
    assert int(f[17][0]) == 1                       # isCampaignMission
    assert int(f[24][0]) == 880123                  # supplyLineId
    by_id = {}
    for fac in _repeated(f, 18):
        ff = pb_parse_multi(fac)
        by_id[int(ff[1][0])] = (_fixed32(ff[3][0]), _fixed32(ff[4][0]))
    assert set(by_id) == {1, 2, 3}, by_id
    # 劣势阵营 GE(2)：xp ×1.25 / 战资 ×1.15；其余 1.0
    assert abs(by_id[2][0] - 1.25) < 1e-6, by_id
    assert abs(by_id[2][1] - 1.15) < 1e-6, by_id
    assert by_id[1] == (1.0, 1.0) and by_id[3] == (1.0, 1.0), by_id


def test_battlefield_settings_campaign_defaults_zero():
    """war 局缺省附属信息：f16=0/f24=0 也允许（战役加成只认 f17）。"""
    m = warserver_messages._msg_response_battlefield_settings(
        10, 2, 3, is_campaign=True)
    f = pb_parse_multi(m)
    assert int(f[16][0]) == 0 and int(f[17][0]) == 1
    assert int(f.get(24, [0])[0]) == 0


# ---------------------------------------------------------------- 结算兜底

def _war_det(mission_id=100003, battle_id=77):
    """登记 war 会战局 det（结算兜底识别 is_campaign 的唯一依据）。"""
    mm_state._MM_MISSION_DETAILS[int(mission_id)] = {
        'preset': 10, 'battle_id': int(battle_id),
        'attacker_faction': 3, 'defender_faction': 2,
    }


def _mock_underdog(mults):
    """替换 war_campaign.current_underdog_mults 为固定倍率（免 DB）。"""
    async def _fake(store):
        return dict(mults)
    old = combat_handlers.war_campaign.current_underdog_mults
    combat_handlers.war_campaign.current_underdog_mults = _fake
    return old


def _run_war_settlement(fake, ud_mults, mission_id=100003):
    """war 局结算驱动：det + underdog mock + FakeStore，返回清理函数结果。"""
    _war_det(mission_id)
    old_ud = _mock_underdog(ud_mults)
    old_store = combat_handlers.STORE
    combat_handlers.STORE = fake
    try:
        asyncio.run(combat_handlers.handle_announce_battlefield_earnings(
            _MockSess(), _MockWriter(), 200,
            'AnnounceBattleFieldEarningsRequest',
            _build_earnings_request(mission_id)))
    finally:
        combat_handlers.STORE = old_store
        combat_handlers.war_campaign.current_underdog_mults = old_ud
        mm_state._MM_MISSION_DETAILS.pop(int(mission_id), None)


def test_campaign_settlement_xp_bonus_fallback():
    """war 局 XP：战役 +50% / underdog +25% 兜底入账（基础 1500）。

    入账 = 基础 1500 + vet 375 + campaign 750 + underdog 375 = 3000；
    战报入库字段（结算界面加成行数据源）同值。"""
    fake = _FakeStore()
    _run_war_settlement(fake, {2: (1.25, 1.15)})
    assert fake.saved, '战报未入库'
    ch = fake.saved[1][0]['characters'][0]
    assert ch['xpCampaignBonus'] == 750, ch['xpCampaignBonus']
    assert ch['xpUnderdogBonus'] == 375, ch['xpUnderdogBonus']
    assert fake.xp == [(1, 4, 3000)], fake.xp


def test_campaign_settlement_currency_bonus_pr_based():
    """war 局货币：战役银币/战资 underdog 按 pr 基础值口径入账。

    pr.f2=120cr → campaign 120×0.25=30、vet 120×25%=30 → 180；
    pr.f3=340wf → underdog 340×0.15（浮点截断 50，与 host int 截断口径
    一致）、vet 85 → 475。"""
    fake = _FakeStore()
    _run_war_settlement(fake, {2: (1.25, 1.15)})
    assert fake.currency == [(1, 180, 475)], fake.currency


def test_campaign_settlement_ribbon_bonus_fallback():
    """war 局勋带：floor = end + vet + booster + campaign + underdog +
    squadVet（线上 wire 值优先，缺的按官方公式兜底）。

    测试包勋带 earned=5、wire xpVet=25/xpSquadVet=5：
    campaign=5×0.5=2、underdog=5×0.25=1 → floor=8+25+0+2+1+5=41。"""
    floors = []

    class _FloorStore(_FakeStore):
        async def set_ribbon_xp_floor(self, pid, cid, rid, xp):
            floors.append((pid, cid, rid, xp))

    fake = _FloorStore()
    _run_war_settlement(fake, {2: (1.25, 1.15)})
    ch = fake.saved[1][0]['characters'][0]
    rb = ch['ribbons'][0]
    assert rb['xpVet'] == 25, rb           # wire 值优先不覆盖
    assert rb['xpCampaign'] == 2, rb
    assert rb['xpUnderdog'] == 1, rb
    assert (1, 4, 13, 41) in floors, floors


def test_campaign_settlement_trusts_wire_values():
    """线上已上报战役加成（host 实算）→ 信任线上值，本地不重复加。"""
    # 角色行 f6 xpCampaignBonus=999 线上直报
    char = (pb_varint(1, 4) + pb_varint(2, 1000) + pb_varint(3, 2500)
            + pb_varint(6, 999))
    player = (pb_varint(1, 1) + pb_varint(4, 500) + pb_msg(9, b'G')
              + pb_varint(10, 0) + pb_msg(11, char))
    faction = pb_varint(1, 2) + pb_msg(2, player)
    report = (pb_varint(1, 730433917100003) + pb_varint(2, 2)
              + pb_varint(3, 3) + pb_varint(4, 900) + pb_varint(5, 1787750000)
              + pb_msg(6, faction))
    proto = (pb_varint(1, 100003) + pb_varint(2, 730433917100003)
             + pb_varint(3, 10) + pb_msg(4, report))

    fake = _FakeStore()
    _war_det()
    old_ud = _mock_underdog({2: (1.25, 1.15)})
    old_store = combat_handlers.STORE
    combat_handlers.STORE = fake
    try:
        asyncio.run(combat_handlers.handle_announce_battlefield_earnings(
            _MockSess(), _MockWriter(), 200,
            'AnnounceBattleFieldEarningsRequest', proto))
    finally:
        combat_handlers.STORE = old_store
        combat_handlers.war_campaign.current_underdog_mults = old_ud
        mm_state._MM_MISSION_DETAILS.pop(100003, None)
    ch = fake.saved[1][0]['characters'][0]
    assert ch['xpCampaignBonus'] == 999, ch['xpCampaignBonus']  # 未兜底覆盖
    # 入账 = 基础 1500 + vet 375 + campaign 999 + underdog 375
    assert fake.xp == [(1, 4, 3249)], fake.xp


def test_staged_settlement_no_campaign_bonus():
    """staged 局（无 det battle_id）：不加任何战役加成（回归护栏）。"""
    fake = _FakeStore()
    old = combat_handlers.STORE
    combat_handlers.STORE = fake
    try:
        asyncio.run(combat_handlers.handle_announce_battlefield_earnings(
            _MockSess(), _MockWriter(), 200,
            'AnnounceBattleFieldEarningsRequest',
            _build_earnings_request()))
    finally:
        combat_handlers.STORE = old
    ch = fake.saved[1][0]['characters'][0]
    assert ch['xpCampaignBonus'] == 0 and ch['xpUnderdogBonus'] == 0
    # 基础 1500 + vet 375；货币 120+30vet / 340+85vet（无战役位）
    assert fake.xp == [(1, 4, 1875)], fake.xp
    assert fake.currency == [(1, 150, 425)], fake.currency


# ---------------------------------------------------------------- 战报回传

def test_battlereport_echoes_campaign_bonuses():
    """S→C 战报：f6-f8/f14-16 与 earnings f4/f5 回传实值（结算界面
    加成行显示数据源）。"""
    m = br._msg_report_character({
        'characterId': 4, 'startXp': 1000, 'endXp': 2500,
        'xpVetBonus': 375, 'xpCampaignBonus': 750, 'xpUnderdogBonus': 125,
        'xpSquadVetBonus': 5, 'xpWarBonus': 60,
        'creditEarned': 120, 'creditVetBonus': 30, 'creditCampaignBonus': 30,
        'warfundEarned': 340, 'warfundVetBonus': 85,
        'warfundUnderdogBonus': 51})
    f = pb_parse_multi(m)
    assert int(f[6][0]) == 750                # xpCampaignBonus
    assert int(f[7][0]) == 125                # xpUnderdogBonus
    assert int(f[8][0]) == 5                  # xpSquadVetBonus
    assert int(f[16][0]) == 60                # xpWarBonus
    cr = pb_parse_multi(f[10][0])
    assert int(cr[1][0]) == 120 and int(cr[2][0]) == 30
    assert int(cr[4][0]) == 30                # creditEarnings campaignBonus
    wf = pb_parse_multi(f[11][0])
    assert int(wf[1][0]) == 340 and int(wf[2][0]) == 85
    assert int(wf[5][0]) == 51                # warfundEarnings underdogBonus


if __name__ == '__main__':
    test_battlefield_settings_staged_unchanged()
    test_battlefield_settings_campaign_fields()
    test_battlefield_settings_campaign_defaults_zero()
    test_campaign_settlement_xp_bonus_fallback()
    test_campaign_settlement_currency_bonus_pr_based()
    test_campaign_settlement_ribbon_bonus_fallback()
    test_campaign_settlement_trusts_wire_values()
    test_staged_settlement_no_campaign_bonus()
    test_battlereport_echoes_campaign_bonuses()
    print('test_war_campaign_bonus: 9 项全部通过')
