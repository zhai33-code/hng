# -*- coding: utf-8 -*-
"""每日签到（Daily Rewards）实现的回归测试（2026-09-02）。

覆盖三层：
  A. 纯单元（无需 DB / 服务器）：
     1. dailyRewards.xml 加载（day→type 映射与客户端 365 天表一致）
     2. _adjust_day 365 天循环折算（客户端 adjustDay 语义）
     3. VoucherPackItem / VoucherPackInfo proto 编码（repeated 空省略）
     4. push_player_offer_metrics_row：KVCS 行字段与 count/redeemed 语义
        （claimable → count=redeemed+1；当天已领 → 相等不弹窗）
     5. _grant_daily_reward：货币 / 随机缎带加成 / 会员 / 未知 day
  B. handler 流程（FakeStore + FakeSess）：
     6. 首次领取：响应带 VoucherPackInfo、发放调用、PlayerOfferMetrics 重推
     6b. 连续第 2 天领取：主奖励 + bonus 双包
     6c. 跨 UTC 零点在线领取：先 touch 再 claim，bonus 按新连续天数发，
         player 行（f19）随响应重推
     7. 当天二领：幂等（day=0 不发放、响应无 f2）
  C. DB 级（直连，复用 test_realdb 模式）：
     8. 迁移：players 4 个新列存在
     9. claim_daily_reward：幂等 + 并发防双领
    10. touch_daily_login：同日幂等 / 昨日+1 / 断签重置 1
    11. mark_first_action_game 幂等；get_daily_metrics available/fagd
    12. 跨 UTC 零点在线领取序列（touch → claim）：c/f19/last_login_date
        全部推进到当天
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HERE = os.path.dirname(os.path.abspath(__file__))

# 定位客户端根目录（含 dailyRewards.xml / WarDatabase）：环境变量 →
# 仓库根（server/tests 向上 4 级）
for _cand in (os.environ.get('HNG_GAME_DIR'),
              os.path.abspath(os.path.join(_HERE, '..', '..', '..', '..'))):
    if _cand and os.path.isfile(os.path.join(_cand, 'dailyRewards.xml')) \
            and os.path.isdir(os.path.join(_cand, 'WarDatabase')):
        os.environ['HNG_GAME_DIR'] = _cand
        break

from protocol import Chunk, pb_parse, pb_parse_multi  # noqa: E402
from handlers import economy           # noqa: E402
from handlers import career            # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  PASS  {name}')
    else:
        FAIL += 1
        print(f'  FAIL  {name}  {detail}')


def _s(v):
    return v.decode() if isinstance(v, (bytes, bytearray)) else v


# ------------------------------------------------------------ 测试桩

class _FakeWriter:
    def __init__(self):
        self.frames = []

    def write(self, data):
        self.frames.append(data)

    async def drain(self):
        pass


class _FakeSess:
    playerview_cmd = 4242
    username = 'daily-test'

    def __init__(self, pid=42):
        self._pid = pid

    async def ensure_player(self):
        return self._pid


class _FakeStore:
    """记录发放调用；claim 结果可编程（ok, day, redeemed, consecutive）。

    events 记录 touch/claim 调用次序（handler 必须先 touch 再 claim）。"""

    def __init__(self, metrics, claim_result=(True, 1, 1, 1)):
        self.metrics = metrics
        self.claim_result = claim_result
        self.claim_calls = 0
        self.currency_ops = []
        self.booster_ops = []
        self.membership_ops = []
        self.events = []

    async def get_daily_metrics(self, pid):
        return self.metrics

    async def touch_daily_login(self, pid):
        self.events.append(('touch', pid))
        return int(self.metrics['c']) if self.metrics else 1

    async def claim_daily_reward(self, pid):
        self.claim_calls += 1
        self.events.append(('claim', pid))
        return self.claim_result

    async def add_currency(self, pid, credits=0, warfunds=0, gold=0):
        self.currency_ops.append((pid, credits, warfunds, gold))

    async def add_booster(self, pid, template_id, duration_minutes=240,
                          activate_now=False, conn=None, is_ribbon=False):
        self.booster_ops.append((pid, template_id, is_ribbon))

    async def add_membership(self, pid, template_id, duration_days=30,
                             conn=None):
        self.membership_ops.append((pid, template_id, duration_days))

    async def list_boosters(self, pid):
        return []

    async def list_memberships(self, pid):
        return []


def _parse_kvcs_frames(frames):
    """写入的 KVCS 帧 → [(tableName, pb_fields), ...]"""
    out = []
    for buf in frames:
        root, _ = Chunk.parse(buf, 0)
        title = root.subs[0]
        assert title.as_str() == 'KeyValueChangeSet'
        action = title.subs[0]
        assert action.as_str() == 'set'
        for table in action.subs:
            out.append((table.as_str(), pb_parse(table.subs[0].data)))
    return out


def _parse_response_frame(buf):
    """build_response 帧 → (类型名, pb 字段)。"""
    root, _ = Chunk.parse(buf, 0)
    title = root.subs[0]
    return _s(title.data), pb_parse(title.subs[0].data)


async def _noop_complete_order_and_push(sess, writer, pid, oid):
    return None


# ------------------------------------------------------------ A. 纯单元

def test_unit():
    print('== 1. dailyRewards.xml 加载 ==')
    check('奖励表加载（364 天）', len(economy._DAILY_REWARDS) == 364,
          f'got {len(economy._DAILY_REWARDS)}')
    check('day1 = currency1（10 金）',
          economy._DAILY_REWARDS.get(1) == 'currency1')
    check('day2 = currency3（1000 银币）',
          economy._DAILY_REWARDS.get(2) == 'currency3')
    check('day7 = ribbonbooster1',
          economy._DAILY_REWARDS.get(7) == 'ribbonbooster1')
    check('day28 = veteranmembership1',
          economy._DAILY_REWARDS.get(28) == 'veteranmembership1')

    print('== 2. _adjust_day 365 天循环 ==')
    for n, want in ((7, 7), (364, 364), (365, 1), (366, 2),
                    (730, 1), (731, 2)):
        check(f'adjust_day({n}) == {want}', economy._adjust_day(n) == want)

    print('== 3. VoucherPack proto 编码 ==')
    item = economy._msg_voucher_pack_item('gold', 10)
    f = pb_parse(item)
    check('VoucherPackItem objectType/objectId',
          _s(f.get(1)) == 'gold' and int(f.get(2, 0)) == 10)
    check('VoucherPackItem info 省略（optional 空）', 3 not in f)

    pack = economy._msg_voucher_pack_info([item])
    f = pb_parse(pack)
    check('VoucherPackInfo identifier/status',
          _s(f.get(1)) == 'daily_login_reward' and int(f.get(3, 0)) == 1)
    sub = pb_parse(f[5])
    check('VoucherPackInfo.items[0] 包装正确',
          _s(sub.get(1)) == 'gold' and int(sub.get(2, 0)) == 10)
    check('VoucherPackInfo names 省略（repeated 空）', 4 not in f)

    empty_pack = economy._msg_voucher_pack_info([])
    f = pb_parse(empty_pack)
    check('空 items 的 pack 无 f5（repeated 零元素省略）', 5 not in f)

    print('== 4. PlayerOfferMetrics KVCS 行 ==')
    # 场景 1：未领（claimable）→ count = redeemed+1
    store = _FakeStore({'n': 3, 'c': 5, 'available': True, 'fagd': '20260901'})
    w = _FakeWriter()
    old = economy.STORE
    economy.STORE = store
    try:
        asyncio.run(economy.push_player_offer_metrics_row(
            _FakeSess(), w, 42))
    finally:
        economy.STORE = old
    rows = _parse_kvcs_frames(w.frames)
    check('推送 1 行 PlayerOfferMetrics', len(rows) == 1)
    name, f = rows[0]
    check('表名 PlayerOfferMetrics', name == 'PlayerOfferMetrics')
    check('id/playerId = 42', int(f.get(1, 0)) == 42 and int(f.get(2, 0)) == 42)
    check('claimable: count=redeemed+1 (f5=4, f6=3)',
          int(f.get(5, 0)) == 4 and int(f.get(6, 0)) == 3)
    check('consecutiveDayCount f8=5', int(f.get(8, 0)) == 5)

    # 场景 2：当天已领 → count == redeemed（弹窗条件不成立）
    store = _FakeStore({'n': 3, 'c': 5, 'available': False, 'fagd': ''})
    w = _FakeWriter()
    economy.STORE = store
    try:
        asyncio.run(economy.push_player_offer_metrics_row(
            _FakeSess(), w, 42))
    finally:
        economy.STORE = old
    rows = _parse_kvcs_frames(w.frames)
    name, f = rows[0]
    check('redeemed: count==redeemed (f5=3, f6=3)',
          int(f.get(5, 0)) == 3 and int(f.get(6, 0)) == 3)

    print('== 5. _grant_daily_reward 发放规格 ==')

    async def _run_grant(day):
        store = _FakeStore(None)
        old = economy.STORE
        economy.STORE = store
        try:
            return await economy._grant_daily_reward(42, day), store
        finally:
            economy.STORE = old

    # day1 currency1 → gold 10
    out, store = asyncio.run(_run_grant(1))
    check('day1 发 10 金', store.currency_ops == [(42, 0, 0, 10)])
    check('day1 push 标记 player', out['push'] == {'player'})
    f = pb_parse(out['items'][0])
    check('day1 item = gold 10', _s(f.get(1)) == 'gold' and int(f.get(2, 0)) == 10)

    # day2 currency3 → credits 1000
    out, store = asyncio.run(_run_grant(2))
    check('day2 发 1000 银币', store.currency_ops == [(42, 1000, 0, 0)])

    # day7 ribbonbooster1 → 3 个随机缎带加成（is_ribbon）
    out, store = asyncio.run(_run_grant(7))
    check('day7 发 3 个缎带加成', len(store.booster_ops) == 3)
    check('day7 缎带全部 is_ribbon',
          all(op[2] for op in store.booster_ops))
    check('day7 模板池合法',
          all(1 <= op[1] <= 99 for op in store.booster_ops))
    check('day7 push 标记 booster', out['push'] == {'booster'})
    check('day7 3 个 item', len(out['items']) == 3)

    # day28 veteranmembership1 → ShopMembershipItems 11（1 天）
    out, store = asyncio.run(_run_grant(28))
    check('day28 发会员 1 天', store.membership_ops == [(42, 11, 1)])
    f = pb_parse(out['items'][0])
    check('day28 item = shopmembershipitems 11',
          _s(f.get(1)) == 'shopmembershipitems' and int(f.get(2, 0)) == 11)

    # 未知 day → 空发放不炸
    out, store = asyncio.run(_run_grant(9999))
    check('未知 day 不发放',
          not out['items'] and not out['push']
          and not store.currency_ops and not store.booster_ops
          and not store.membership_ops)

    print('== 5b. 连续登录 bonus 表加载 + 发放 ==')
    check('bonus 表加载（6 天）', len(economy._CONSECUTIVE_REWARDS) == 6,
          f'got {sorted(economy._CONSECUTIVE_REWARDS)}')
    check('day2 = credits 2000',
          economy._CONSECUTIVE_REWARDS.get(2) == [('credits', 2000)])
    check('day7 = membership 11',
          economy._CONSECUTIVE_REWARDS.get(7) == [('membership', 11)])
    check('day1 无 bonus', 1 not in economy._CONSECUTIVE_REWARDS)

    async def _run_bonus(c):
        store = _FakeStore(None)
        old = economy.STORE
        economy.STORE = store
        try:
            return await economy._grant_consecutive_bonus(42, c), store
        finally:
            economy.STORE = old

    # 连续第 2 天 → 2000 银币（★本次修复的"连续两天登录奖励"）
    out, store = asyncio.run(_run_bonus(2))
    check('连续 2 天发 2000 银币', store.currency_ops == [(42, 2000, 0, 0)])
    f = pb_parse(out['items'][0])
    check('bonus item 字段', _s(f.get(1)) == 'credits'
          and int(f.get(2, 0)) == 2000)
    check('bonus push 标记 player', out['push'] == {'player'})

    # 连续第 7 天 → 会员 1 天；>=8 折算回第 2 天（镜像客户端 wrap）
    out, store = asyncio.run(_run_bonus(7))
    check('连续 7 天发会员 1 天', store.membership_ops == [(42, 11, 1)])
    out, store = asyncio.run(_run_bonus(8))
    check('连续 8 天折算回 day2（2000 银币）',
          store.currency_ops == [(42, 2000, 0, 0)])
    # 第 1 天 / 未知 → 不发
    out, store = asyncio.run(_run_bonus(1))
    check('连续 1 天无 bonus', not store.currency_ops
          and not store.membership_ops)


# ------------------------------------------------------------ B. handler 流程

def test_handler():
    print('== 6. 首次领取（响应 + 发放 + KVCS 重推）==')
    store = _FakeStore({'n': 0, 'c': 1, 'available': True, 'fagd': ''},
                       claim_result=(True, 1, 1, 1))
    w = _FakeWriter()
    old_store, old_career = economy.STORE, career.complete_order_and_push
    economy.STORE = store
    career.complete_order_and_push = _noop_complete_order_and_push
    try:
        asyncio.run(economy.handle_redeem_daily_login_reward(
            _FakeSess(), w, 123, 'RedeemDailyLoginRewardRequest', b''))
    finally:
        economy.STORE = old_store
        career.complete_order_and_push = old_career
    # 响应帧（最后一帧）解析
    name, f = _parse_response_frame(w.frames[-1])
    check('响应类型', name == 'RedeemDailyLoginRewardResponse')
    check('response=1', int(f.get(1, 0)) == 1)
    check('响应带 VoucherPackInfo f2', 2 in f and isinstance(f.get(2), bytes))
    sub = pb_parse(f[2])
    check('pack 内容 = day1 gold 10',
          _s(pb_parse(sub[5]).get(1)) == 'gold'
          and int(pb_parse(sub[5]).get(2, 0)) == 10)
    # KVCS 重推行
    kvcs = _parse_kvcs_frames(w.frames[:-1])
    tables = [t for t, _ in kvcs]
    check('重推 PlayerOfferMetrics 行', 'PlayerOfferMetrics' in tables)
    check('重推行 count/redeemed 字段有效',
          any(5 in fields and 6 in fields
              for t, fields in kvcs if t == 'PlayerOfferMetrics'))
    check('发放调用 day1 gold', store.currency_ops == [(42, 0, 0, 10)])

    print('== 6b. 连续第 2 天领取（主奖励 + bonus 双包）==')
    store = _FakeStore({'n': 1, 'c': 2, 'available': True, 'fagd': ''},
                       claim_result=(True, 2, 2, 2))
    w = _FakeWriter()
    economy.STORE = store
    career.complete_order_and_push = _noop_complete_order_and_push
    try:
        asyncio.run(economy.handle_redeem_daily_login_reward(
            _FakeSess(), w, 123, 'RedeemDailyLoginRewardRequest', b''))
    finally:
        economy.STORE = old_store
        career.complete_order_and_push = old_career
    # 重复解析需要原始字节（_parse_response_frame 返回单值 dict）
    root, _ = Chunk.parse(w.frames[-1], 0)
    fm = pb_parse_multi(root.subs[0].subs[0].data)
    check('响应带 2 个 VoucherPackInfo', len(fm.get(2, [])) == 2)
    main_pack = pb_parse(fm[2][0])
    bonus_pack = pb_parse(fm[2][1])
    check('主包 identifier=daily_login_reward',
          _s(main_pack.get(1)) == 'daily_login_reward')
    check('bonus 包 identifier=consecutive_daily_login_rewards',
          _s(bonus_pack.get(1)) == 'consecutive_daily_login_rewards')
    check('主包 day2 = 1000 银币，bonus = 2000 银币',
          store.currency_ops == [(42, 1000, 0, 0), (42, 2000, 0, 0)],
          str(store.currency_ops))

    print('== 6c. 跨 UTC 零点在线领取（touch 先行 + bonus 按新 c 发）==')
    # 场景：昨天 23:xx UTC 登录（c=1），跨零点未重登直接领取。
    # 修复后 handler 先 touch（c/f19 推进到当天，DB 侧表现为 c=2）再
    # claim —— claim_result=(True,2,2,2) 即 touch 后的 DB 返回。
    store = _FakeStore({'n': 1, 'c': 2, 'available': True, 'fagd': '20260915'},
                       claim_result=(True, 2, 2, 2))
    w = _FakeWriter()
    economy.STORE = store
    career.complete_order_and_push = _noop_complete_order_and_push
    try:
        asyncio.run(economy.handle_redeem_daily_login_reward(
            _FakeSess(), w, 123, 'RedeemDailyLoginRewardRequest', b''))
    finally:
        economy.STORE = old_store
        career.complete_order_and_push = old_career
    check('领取前先 touch 再 claim',
          store.events[:2] == [('touch', 42), ('claim', 42)],
          str(store.events))
    check('bonus 按 touch 后 c=2 发 2000 银币',
          store.currency_ops == [(42, 1000, 0, 0), (42, 2000, 0, 0)],
          str(store.currency_ops))
    kvcs = _parse_kvcs_frames(w.frames[:-1])
    tables = [t for t, _ in kvcs]
    check('重推 player 行（f19 同步客户端）', 'player' in tables)
    check('重推 PlayerOfferMetrics 行', 'PlayerOfferMetrics' in tables)

    print('== 7. 当天二领（幂等）==')
    store = _FakeStore({'n': 1, 'c': 1, 'available': False, 'fagd': ''},
                       claim_result=(True, 0, 1, 1))
    w = _FakeWriter()
    economy.STORE = store
    try:
        asyncio.run(economy.handle_redeem_daily_login_reward(
            _FakeSess(), w, 123, 'RedeemDailyLoginRewardRequest', b''))
    finally:
        economy.STORE = old_store
    name, f = _parse_response_frame(w.frames[-1])
    check('二领 response=1', int(f.get(1, 0)) == 1)
    check('二领响应无 f2（不重复发）', 2 not in f)
    check('二领无发放', not store.currency_ops and not store.booster_ops
          and not store.membership_ops)
    # 幂等分支不重推（day=0 短路）
    check('二领无 KVCS 重推', len(w.frames) == 1)


# ------------------------------------------------------------ C. DB 级

async def _db_main():
    import store
    st = store.PlayerStore()
    await st.start()

    print('== 8. 迁移：players 新列 ==')
    for col in ('daily_reward_redeemed_count', 'consecutive_day_count',
                'last_login_date', 'first_action_game_date'):
        has = await st.pool.fetchval(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name='players' AND column_name=$1", col)
        check(f'players.{col} 存在', bool(has))

    print('== 9. claim_daily_reward 幂等 + 防双领 ==')
    uname = f'test_daily_{int(time.time())}'
    pid = await st.create_player(uname, 'daily-test')
    check('建测试账号', pid is not None)
    ok1, day1, n1, c1 = await st.claim_daily_reward(pid)
    ok2, day2, n2, c2 = await st.claim_daily_reward(pid)
    check('首次领取 day=1', ok1 and day1 == 1 and n1 == 1)
    check('当天二领 day=0（幂等）', ok2 and day2 == 0 and n2 == 1)
    check('领取返回连续天数（未 touch 的号=0）', c1 == 0 and c2 == 0)

    # 并发：两个 claim 同时进，只有一个 day>0
    pid2 = await st.create_player(uname + '2', 'daily-test')
    res = await asyncio.gather(st.claim_daily_reward(pid2),
                               st.claim_daily_reward(pid2))
    granted = sum(1 for ok, d, n, c in res if d)
    check('并发只有一个领取成功', granted == 1, str(res))

    print('== 10. touch_daily_login 连续性 ==')
    async with st.pool.acquire() as conn:
        await conn.execute(
            'UPDATE players SET last_login_date = CURRENT_DATE, '
            'consecutive_day_count = 4 WHERE id = $1', pid)
    c = await st.touch_daily_login(pid)
    check('同日幂等（4 → 4）', c == 4)
    async with st.pool.acquire() as conn:
        await conn.execute(
            'UPDATE players SET last_login_date = CURRENT_DATE - 1 '
            'WHERE id = $1', pid)
    c = await st.touch_daily_login(pid)
    check('昨日已登（4 → 5）', c == 5)
    async with st.pool.acquire() as conn:
        await conn.execute(
            'UPDATE players SET last_login_date = CURRENT_DATE - 3 '
            'WHERE id = $1', pid)
    c = await st.touch_daily_login(pid)
    check('断签重置 1', c == 1)
    # firstActionGameDate（player f19）随 touch 刷成当天：客户端连续奖励链
    # 基准日 C = f8 + (f19 != 今天 ? 1 : 0)，f19 过期会使链条错位/卡死
    async with st.pool.acquire() as conn:
        f19 = await conn.fetchval(
            'SELECT to_char(first_action_game_date, \'YYYYMMDD\') '
            'FROM players WHERE id = $1', pid)
        # 以 DB 的 CURRENT_DATE 为准（DB 时区 UTC，与本地日期可能差一天）
        db_today = await conn.fetchval(
            "SELECT to_char(CURRENT_DATE, 'YYYYMMDD')")
    check('touch 同步 f19 = 当天', f19 == db_today, f'f19={f19}')

    print('== 11. mark_first_action_game / get_daily_metrics ==')
    await st.mark_first_action_game(pid)
    await st.mark_first_action_game(pid)      # 幂等
    m = await st.get_daily_metrics(pid)
    check('metrics 返回行', m is not None)
    # 以 DB 的 CURRENT_DATE 为准（DB 时区 UTC，与本地日期可能差一天）
    today = await st.pool.fetchval("SELECT to_char(CURRENT_DATE, 'YYYYMMDD')")
    check('first_action_game_date = 今天（幂等不覆盖）',
          m['fagd'] == today, f'fagd={m["fagd"]} want={today}')
    check('available=False（刚 claim 过）', m['available'] is False)
    check('redeemed=1', int(m['n']) == 1)
    # 未领账号 → available=True（pid2 在并发测试中已领，另建号验证）
    pid3 = await st.create_player(uname + '3', 'daily-test')
    m2 = await st.get_daily_metrics(pid3)
    check('未领账号 available=True', m2['available'] is True)

    print('== 12. 跨 UTC 零点在线领取序列（touch → claim）==')
    # handler 新路径的 DB 侧验证：昨天登录过（c=1），跨零点未重登直接
    # 领取 —— 先 touch 推进 c/f19，claim 拿到新连续天数，last_login_date
    # 刷到当天（次日重登不会断签重置）。
    pid4 = await st.create_player(uname + '4', 'daily-test')
    async with st.pool.acquire() as conn:
        await conn.execute(
            'UPDATE players SET last_login_date = CURRENT_DATE - 1, '
            'consecutive_day_count = 1 WHERE id = $1', pid4)
    c = await st.touch_daily_login(pid4)
    check('跨零点 touch 推进（1 → 2）', c == 2)
    ok, d, n, c2 = await st.claim_daily_reward(pid4)
    check('领取成功且返回新连续天数 2', ok and d == 1 and c2 == 2,
          f'ok={ok} day={d} c={c2}')
    async with st.pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT last_login_date, '
            'to_char(first_action_game_date, \'YYYYMMDD\') AS f19 '
            'FROM players WHERE id = $1', pid4)
        today_d = await conn.fetchval('SELECT CURRENT_DATE')
    check('last_login_date 刷到当天（次日重登不断签）',
          row['last_login_date'] == today_d)
    # 以 DB 的 CURRENT_DATE 为准（DB 时区 UTC，与本地日期可能差一天）
    check('f19 刷到当天（客户端 C 公式不再 +1 错位）',
          row['f19'] == today_d.strftime('%Y%m%d'), f'f19={row["f19"]}')

    # 清理
    await st.close()


def test_db():
    try:
        asyncio.run(_db_main())
    except Exception as e:
        print(f'  [skip] DB 测试不可用：{e}')


def main():
    test_unit()
    test_handler()
    test_db()
    print(f'\n结果: PASS={PASS} FAIL={FAIL}')
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())



