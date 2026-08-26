# -*- coding: utf-8 -*-
"""战斗通行证（session pass）购买/解锁修复回归测试（2026-08-27）。

覆盖四项修复：
1. 结算分数增长：AnnounceBattleFieldEarnings 把玩家本局 battle score
   计入当前激活赛季 battle_pass_progress.xp
2. 买层级设值：PurchaseBattlePassTierRequest 把 score 提升到模板
   requiredScore（GREATEST 只升不降，旧实现是增量累加会越买越超）
3. 赛季模板 KVCS 字段：battle_pass_season_template 运行时 schema 为
   6=startingTier 7=startingLevel 8=startingScore（旧实现误写 isActive
   布尔到 f6 并漏掉必填 f7/f8，KVCS set 整行替换会破坏客户端本地表）
4. 按请求赛季推行：purchase/activate/reward 处理后推的 battle_pass 行
   带请求的 season（旧实现硬编码 season 4，其他赛季操作客户端无反馈）

纯函数级断言无需 DB；DB 级部分注入 state.STORE（必须在 import handlers
之前设置——各 handler 是 from state import STORE 导入时快照），
复用 test_transactions.py 的直连模式，结束清理测试账号。
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol import Chunk, pb_parse  # noqa: E402


PASS = FAIL = 0


def check(name, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  PASS  {name}')
    else:
        FAIL += 1
        print(f'  FAIL  {name}  {detail}')


# ---------- protobuf 构造 / mock ----------
def _varint(value):
    if value < 0:
        value += 1 << 64
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        out.append(b | 0x80 if value else b)
        if not value:
            break
    return bytes(out)


def pb_varint(field, value):
    """varint 字段：tag wire type 0。"""
    return bytes([field << 3 | 0]) + _varint(value)


def pb_msg(field, payload):
    """嵌套消息字段：tag wire type 2 + 长度前缀。

    注意：test_settlement_parse 的 pb_msg 复用 varint tag（wire type 0），
    其请求实际未被服务器解析（断言只查不崩溃，未暴露）。此处修正。"""
    return bytes([field << 3 | 2]) + _varint(len(payload)) + payload


class _MockWriter:
    """收集响应/KVCS 字节的 writer 替身。"""

    def __init__(self):
        self.sent = b''

    def write(self, data):
        self.sent += bytes(data)

    async def drain(self):
        return


class _NullStore:
    """乱字节回归专用的空 STORE：吞掉一切 DB 调用，绝不写真实库。

    2026-08-27 事故：乱字节测试带真实 STORE 跑，msid=0 命中真实战报行
    把玩家数据擦了（后已在 player_store.save_battle_report 加空玩家保护）。"""

    def __getattr__(self, name):
        async def _noop(*_a, **_kw):
            return None
        return _noop


class _MockSess:
    """career/combat handler 所需的最小 session 替身。"""

    playerview_cmd = 77
    combat_mission = 100003
    game_mission = 100003
    username = None

    def __init__(self, pid=None):
        self._pid = pid

    async def ensure_player(self):
        return self._pid


def _kvcs_rows(sent, table_name):
    """从 writer 捕获的字节解出指定表的 KVCS 行 → [pb_parse dict]。

    结构：main(cmd_id) > 'KeyValueChangeSet' > action > tableName > data
    """
    rows = []
    pos = 0
    while pos < len(sent):
        root, nxt = Chunk.parse(sent, pos)
        if root is None:
            break
        pos = nxt
        for title in root.subs:
            if title.data != b'KeyValueChangeSet':
                continue
            for action in title.subs:          # b'set' / b'delete'
                for table in action.subs:
                    if table.data == table_name:
                        for row in table.subs:
                            rows.append(pb_parse(row.data))
    return rows


# ---------- 纯函数级（无需 DB） ----------
def test_season_starting_fields():
    """本地 WarDatabase 四赛季起始值：startingTier=1/Level=10/Score=0。"""
    from handlers import career
    seasons = career._bp_seasons()
    check('赛季表加载 4 行', len(seasons) == 4, f'len={len(seasons)}')
    for s in seasons:
        check(f"season {s['id']} 起始值 (1,10,0)",
              (s['s_tier'], s['s_lvl'], s['s_score']) == (1, 10, 0),
              f"got {(s['s_tier'], s['s_lvl'], s['s_score'])}")


def test_push_season_templates_schema():
    """KVCS 赛季模板行字段 6/7/8 = startingTier/Level/Score（非 isActive）。"""
    from handlers import career
    writer = _MockWriter()
    sess = _MockSess(pid=1)
    asyncio.run(career.push_season_templates(sess, writer))
    rows = _kvcs_rows(writer.sent, b'battle_pass_season_template')
    check('推送 4 行赛季模板', len(rows) == 4, f'rows={len(rows)}')
    for f in rows:
        got = (f.get(6), f.get(7), f.get(8))
        check(f"season {f.get(1)} KVCS f6/f7/f8 = (1,10,0)",
              got == (1, 10, 0), f'got {got}')


def test_tier_level_for_score_by_season():
    """tier/level 推导按赛季参数化（各赛季模板集隔离）。"""
    from handlers import career
    check('score=220000 season4 → (12,10)',
          career._tier_level_for_score(220000, 4) == (12, 10))
    check('默认参数 = season4',
          career._tier_level_for_score(220000) == (12, 10))
    check('score=0 → (1,10)（tier1 十级 req 全 0）',
          career._tier_level_for_score(0, 4) == (1, 10))
    ids3 = {t['id'] for t in career._bp_templates() if t['season'] == 3}
    ids4 = {t['id'] for t in career._bp_templates() if t['season'] == 4}
    check('赛季 3/4 模板 id 不重叠',
          bool(ids3 & ids4) is False and len(ids3) == 120 and len(ids4) == 120)


# ---------- DB 级（注入 state.STORE） ----------
def _earnings_request(pid, char_id, score):
    """合成 AnnounceBattleFieldEarningsRequest（结构同 test_settlement_parse，
    但嵌套消息 tag 用正确的 wire type 2）。"""
    char = pb_varint(1, char_id) + pb_varint(2, 1000) + pb_varint(3, 2500)
    player = (pb_varint(1, pid) + pb_varint(4, score) + pb_varint(6, 3)
              + pb_varint(7, 1) + pb_msg(9, b'TestGuy') + pb_varint(10, 0)
              + pb_msg(11, char))
    faction = pb_varint(1, 2) + pb_msg(2, player)
    report = (pb_varint(1, 730433917100003) + pb_varint(2, 2)
              + pb_varint(3, 3) + pb_varint(4, 900) + pb_varint(5, 1787750000)
              + pb_msg(6, faction))
    pbr = pb_varint(1, pid) + pb_varint(2, 120) + pb_varint(3, 340)
    return (pb_varint(1, 100003) + pb_varint(2, 730433917100003)
            + pb_varint(3, 10) + pb_msg(4, report) + pb_msg(5, pbr))


async def db_tests():
    import player_store
    import state

    st = player_store.PlayerStore()
    await st.start()
    uname = f'test_bpfix_{int(time.time())}'
    pid = await st.create_player(uname, 'bp-fix')
    check('create_player 建号', pid is not None)
    if pid is None:
        await st.close()
        return
    # 结算的 add_combat_xp 要求 charId 属于该玩家（否则 UPDATE 无行抛
    # int(None) 中断 try 块，顺带跳过 battle pass 加分）
    char_row = await st.create_character(pid, 'BpSoldier', 1, 1)
    check('create_character 建角色', char_row is not None)
    if char_row is None:
        await st.close()
        return
    char_id = int(char_row['id'])

    # from state import STORE 是导入时快照：state.STORE 在 import 时就是
    # 一个未 start 的 PlayerStore()（pool=None），各模块持有的都是它。
    # 必须无条件把相关模块的 STORE 全部替换为已连接的 st。
    state.STORE = st
    from handlers import combat as combat_handlers
    from handlers import career as career_handlers
    import player_service  # noqa: F401
    import matchmaking     # noqa: F401
    import sys as _sys
    for _name in ('player_service', 'matchmaking',
                  'handlers.career', 'handlers.combat'):
        _mod = _sys.modules.get(_name)
        if _mod is not None:
            _mod.STORE = st
    combat_handlers.STORE = st
    career_handlers.STORE = st

    season = career_handlers._ACTIVE_SEASON

    try:
        # ---- 1. 结算分数增长（battle score 计入激活赛季）
        print('== 1. 结算分数增长 ==')
        writer = _MockWriter()
        await combat_handlers.handle_announce_battlefield_earnings(
            _MockSess(), writer, 200,
            'AnnounceBattleFieldEarningsRequest',
            _earnings_request(pid, char_id, 500))
        bp = await st.get_battle_pass(pid, season)
        check('结算后 battle pass 行创建且 score=500',
              bp is not None and bp['score'] == 500,
              f'bp={bp}')
        await combat_handlers.handle_announce_battlefield_earnings(
            _MockSess(), _MockWriter(), 200,
            'AnnounceBattleFieldEarningsRequest',
            _earnings_request(pid, char_id, 300))
        bp = await st.get_battle_pass(pid, season)
        check('两局累计 score=800', bp['score'] == 800, f"score={bp['score']}")

        # ---- 2. 买层级设值（只升不降）
        print('== 2. 买层级设值 ==')
        await career_handlers.handle_purchase_battle_pass_tier(
            _MockSess(pid), _MockWriter(), 210,
            'PurchaseBattlePassTierRequest', pb_varint(1, 371))
        bp = await st.get_battle_pass(pid, season)
        check('买 req=1000 层级：score 800 → 1000（设值）',
              bp['score'] == 1000, f"score={bp['score']}")
        await career_handlers.handle_purchase_battle_pass_tier(
            _MockSess(pid), _MockWriter(), 211,
            'PurchaseBattlePassTierRequest', pb_varint(1, 371))
        bp = await st.get_battle_pass(pid, season)
        check('重复买同层级不重复累加（旧 bug：会变 2000）',
              bp['score'] == 1000, f"score={bp['score']}")

        # ---- 3. 购买通行证（非默认赛季）→ 按请求赛季推 battle_pass 行
        print('== 3. 按请求赛季推行 ==')
        other_season = 3
        await st.set_battle_pass_score(pid, other_season, 1500)
        writer = _MockWriter()
        await career_handlers.handle_purchase_battle_pass_season(
            _MockSess(pid), writer, 212,
            'PurchaseBattlePassSeasonRequest', pb_varint(1, other_season))
        rows = _kvcs_rows(writer.sent, b'battle_pass')
        check('购买 season3 后推 1 行 battle_pass（旧 bug：硬编码查 season4 无行）',
              len(rows) == 1, f'rows={len(rows)}')
        if rows:
            f = rows[0]
            check('行 season 字段 = 请求的 season 3',
                  f.get(5) == other_season, f'f5={f.get(5)}')
            check('行 isPurchased = true', f.get(7) == 1)
            check('行 score = 1500', f.get(6) == 1500, f'f6={f.get(6)}')

        # ---- 4. 领奖：按请求赛季领取 + 奖励入账
        print('== 4. 领奖入账 ==')
        cur_before = await st.get_currency(pid)
        writer = _MockWriter()
        await career_handlers.handle_receive_battle_pass_reward(
            _MockSess(pid), writer, 213,
            'ReceiveBattlePassRewardRequest', pb_varint(1, other_season))
        resp = writer.sent
        check('领奖响应含 rewardedBattlePassTemplates',
              b'ReceiveBattlePassRewardResponse' in resp)
        bp = await st.get_battle_pass(pid, other_season)
        # season3 score=1500 → tier1 全部 10 级（req=0）+ tier2 lvl1 (1000)
        # + lvl2 (2000 不可达) → 11 个模板可领
        check('claimed 落库 11 个模板', len(bp['claimed']) == 11,
              f'claimed={len(bp["claimed"])}')
        cur_after = await st.get_currency(pid)
        # tier1 奖励含 credits 10000（模板 241）与 warfunds 10000（242）
        check('奖励货币入账（credits/warfunds 增加）',
              cur_after['credits'] > cur_before['credits']
              and cur_after['warfunds'] > cur_before['warfunds'],
              f'before={cur_before} after={cur_after}')
        # 幂等：再领一次不再入账
        cur_mid = cur_after
        await career_handlers.handle_receive_battle_pass_reward(
            _MockSess(pid), _MockWriter(), 214,
            'ReceiveBattlePassRewardRequest', pb_varint(1, other_season))
        cur_again = await st.get_currency(pid)
        check('重复领奖幂等（不重复入账）',
              cur_again == cur_mid,
              f'mid={cur_mid} again={cur_again}')

        # ---- 4.5 空玩家结算不擦已有战报（2026-08-27 msid=0 事故保护）
        print('== 4.5 空玩家不擦战报 ==')
        hdr = {'mission_session_id': -999001, 'mission_id': 100003,
               'mission_preset_id': 10, 'winner_faction_id': 3,
               'defender_faction_id': 2, 'mission_time_seconds': 900,
               'battle_start_time': 1787750000}
        rows = [{'player_id': pid, 'gamertag': 'KeepMe', 'squad_name': '',
                 'score': 500, 'captures': 1, 'kills': 2, 'deaths': 1,
                 'is_bot': False, 'characters': []}]
        rid_keep = await st.save_battle_report(hdr, rows)
        rid_empty = await st.save_battle_report(
            {**hdr, 'winner_faction_id': 0, 'mission_time_seconds': 0}, [])
        got = await st.get_battle_report(rid_keep)
        check('空玩家结算后原玩家行仍在（id 回传相同）',
              rid_empty == rid_keep and got is not None
              and len(got[1]) == 1 and got[1][0]['gamertag'] == 'KeepMe'
              and got[0]['winner_faction_id'] == 3
              and got[0]['mission_time_seconds'] == 900,
              f'rid_keep={rid_keep} rid_empty={rid_empty}')

        # ---- 5. 结算响应先行不受影响（回归保护；空 STORE，不写真实库）
        print('== 5. 结算响应回归 ==')
        real_store = combat_handlers.STORE
        combat_handlers.STORE = _NullStore()
        try:
            writer = _MockWriter()
            await combat_handlers.handle_announce_battlefield_earnings(
                _MockSess(), writer, 200,
                'AnnounceBattleFieldEarningsRequest', b'\xff\xff garbage')
        finally:
            combat_handlers.STORE = real_store
        check('乱字节输入仍回结算响应',
              b'AnnounceBattleFieldEarningsResponse' in writer.sent)
    finally:
        async with st.pool.acquire() as c:
            await c.execute('DELETE FROM players WHERE id = $1', pid)
            # 测试合成结算固定 msid=730433917100003：清掉测试战报行，
            # 不在真实库里留 TestGuy 污染（2026-08-27 id=125 残留教训）
            await c.execute(
                'DELETE FROM battle_reports WHERE mission_session_id = $1',
                730433917100003)
            await c.execute(
                'DELETE FROM battle_reports WHERE mission_session_id = $1',
                -999001)
        print('  已清理测试账号')
        await st.close()


def main():
    print('== 纯函数级（无 DB） ==')
    test_season_starting_fields()
    test_push_season_templates_schema()
    test_tier_level_for_score_by_season()
    print('== DB 级（注入 state.STORE） ==')
    asyncio.run(db_tests())
    print(f'\n结果: PASS={PASS} FAIL={FAIL}')
    sys.exit(1 if FAIL else 0)


if __name__ == '__main__':
    main()
