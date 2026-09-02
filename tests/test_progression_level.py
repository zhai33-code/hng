# -*- coding: utf-8 -*-
"""玩家等级（进展 tab）奖励发放回归测试（2026-09-02）。

覆盖：
  A. wardb 静态表：
     1. PlayerLevel 阈值表加载 + player_level_from_score 边界
     2. _LEVEL_UNLOCKS：2 级起有奖励、跨级汇总数据源就绪
  B. progression.check_player_level（FakeStore）：
     3. 未升级（level <= granted）静默返回 None、不入账
     4. 升级 notify=False（登录补发）：货币入账 + level_granted 推进 + 返回新等级
     5. 跨多级补发：各级奖励累加一次性入账
     6. notify=True 但无 playerview_cmd：仍入账，推送安全跳过
     7. totalScore 汇总：PlayerScore 优先、legacy score 回落
  C. 通知行编码：
     8. _msg_notification 字段（id/playerId/type/typeId/readStatus=0/时间戳）
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HERE = os.path.dirname(os.path.abspath(__file__))

for _cand in (os.environ.get('HNG_GAME_DIR'),
              os.path.abspath(os.path.join(_HERE, '..', '..', '..', '..'))):
    if _cand and os.path.isdir(os.path.join(_cand, 'WarDatabase')):
        os.environ['HNG_GAME_DIR'] = _cand
        break

from protocol import pb_parse                     # noqa: E402
from wardb import (_LEVEL_UNLOCKS, _PLAYER_LEVELS, # noqa: E402
                   player_level_from_score,
                   player_unlock_bits_for_level)
from handlers import progression                  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  PASS  {name}')
    else:
        FAIL += 1
        print(f'  FAIL  {name}  {detail}')


# ------------------------------------------------------------ 测试桩

class _FakeWriter:
    def __init__(self):
        self.frames = []

    def write(self, data):
        self.frames.append(data)

    async def drain(self):
        pass


class _FakeSess:
    playerview_cmd = None
    username = 'prog-test'

    def __init__(self, pid=42):
        self._pid = pid
        self.writer = _FakeWriter()

    async def ensure_player(self):
        return self._pid


class _FakeStore:
    def __init__(self, chars, granted=1, currency=None):
        # chars: [{id, stats-dict}]
        self.chars = chars
        self.granted = granted
        self.cur = currency or {'credits': 0, 'gold': 0, 'warfunds': 0}
        self.added = None

    async def list_characters(self, pid):
        return list(self.chars)

    async def get_stats(self, char_id):
        for c in self.chars:
            if c['id'] == char_id:
                return c.get('stats')
        return None

    async def get_level_granted(self, pid):
        return self.granted

    async def set_level_granted(self, pid, level):
        self.granted = int(level)

    async def add_currency(self, pid, credits=0, gold=0, warfunds=0):
        self.added = (credits, gold, warfunds)
        self.cur['credits'] += credits
        self.cur['gold'] += gold
        self.cur['warfunds'] += warfunds
        return dict(self.cur)


def _run(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError
    except RuntimeError:
        return asyncio.run(coro)
    return loop.run_until_complete(coro)


# ------------------------------------------------------------ A. wardb 静态表

def test_wardb_tables():
    print('[A] wardb 静态表')
    check('A1 PlayerLevel 表非空且含 1 级阈值 0',
          bool(_PLAYER_LEVELS) and _PLAYER_LEVELS[0] == (1, 0),
          f'{_PLAYER_LEVELS[:2]}')
    check('A2 score=0 → level 1',
          player_level_from_score(0) == 1)
    lv2_threshold = dict(_PLAYER_LEVELS).get(2)
    check('A3 score=lv2 阈值 → level 2（含边界）',
          lv2_threshold and player_level_from_score(lv2_threshold) == 2,
          f'threshold={lv2_threshold}')
    max_lv, max_th = _PLAYER_LEVELS[-1]
    check('A4 超过最高阈值 → 封顶',
          player_level_from_score(max_th + 10 ** 9) == max_lv,
          f'max={max_lv}')
    check('A5 2 级起有奖励条目',
          any(_LEVEL_UNLOCKS.get(lv) for lv in range(2, max_lv + 1)),
          f'unlock levels={sorted(_LEVEL_UNLOCKS)}')
    lv2 = _LEVEL_UNLOCKS.get(2) or {}
    check('A6 2 级奖励含 credits 与模式解锁',
          lv2.get('credits', 0) > 0 and lv2.get('modes'),
          f'lv2={lv2}')


# ------------------------------------------------------------ B. check_player_level

def test_check_player_level():
    print('[B] progression.check_player_level')
    old = progression.STORE
    try:
        # B3 未升级：level <= granted
        store = _FakeStore([{'id': 1, 'stats': {'PlayerScore': 0}}], granted=1)
        progression.STORE = store
        ret = _run(progression.check_player_level(
            _FakeSess(), _FakeWriter(), 42, notify=False))
        check('B3 未升级返回 None 且不入账',
              ret is None and store.added is None and store.granted == 1)

        # B4 单级升级 notify=False（登录补发）
        lv2_th = dict(_PLAYER_LEVELS)[2]
        store = _FakeStore([{'id': 1, 'stats': {'PlayerScore': lv2_th}}],
                           granted=1)
        progression.STORE = store
        ret = _run(progression.check_player_level(
            _FakeSess(), _FakeWriter(), 42, notify=False))
        exp = _LEVEL_UNLOCKS.get(2) or {}
        check('B4 升级返回新等级 2',
              ret == 2, f'ret={ret}')
        check('B4 货币按 2 级奖励入账',
              store.added == (exp.get('credits', 0), exp.get('gold', 0),
                              exp.get('warfunds', 0)),
              f'added={store.added} exp={exp}')
        check('B4 level_granted 推进到 2',
              store.granted == 2)

        # B5 跨多级补发：1 → 4，累加 2/3/4 级奖励
        lv4_th = dict(_PLAYER_LEVELS)[4]
        store = _FakeStore([{'id': 1, 'stats': {'PlayerScore': lv4_th}}],
                           granted=1)
        progression.STORE = store
        ret = _run(progression.check_player_level(
            _FakeSess(), _FakeWriter(), 42, notify=False))
        exp_c = sum((_LEVEL_UNLOCKS.get(lv) or {}).get('credits', 0)
                    for lv in range(2, 5))
        exp_g = sum((_LEVEL_UNLOCKS.get(lv) or {}).get('gold', 0)
                    for lv in range(2, 5))
        exp_w = sum((_LEVEL_UNLOCKS.get(lv) or {}).get('warfunds', 0)
                    for lv in range(2, 5))
        check('B5 跨级补发返回 4 且累加入账',
              ret == 4 and store.added == (exp_c, exp_g, exp_w),
              f'ret={ret} added={store.added} expect=({exp_c},{exp_g},{exp_w})')

        # B6 notify=True 但无 playerview_cmd：入账不受影响
        store = _FakeStore([{'id': 1, 'stats': {'PlayerScore': lv2_th}}],
                           granted=1)
        progression.STORE = store
        ret = _run(progression.check_player_level(
            _FakeSess(), _FakeWriter(), 42, notify=True))
        check('B6 无 playerview_cmd 仍入账并返回等级',
              ret == 2 and store.added is not None and store.granted == 2)

        # B7 totalScore 汇总：PlayerScore 优先 + legacy score 回落 + 多角色累加
        store = _FakeStore([
            {'id': 1, 'stats': {'PlayerScore': 100, 'score': 999}},
            {'id': 2, 'stats': {'score': 50}},
        ], granted=1)
        progression.STORE = store
        total = _run(progression._total_score(42))
        check('B7 PlayerScore 优先 / score 回落 / 多角色累加',
              total == 150, f'total={total}')
    finally:
        progression.STORE = old


# ------------------------------------------------------------ C. 通知行编码

def test_notification_row():
    print('[C] Notification 行编码')
    m, ts, exp = progression._msg_notification(7, 42,
                                               progression._NT_PLAYER_LEVEL_UNLOCKED,
                                               3)
    f = pb_parse(m)
    check('C1 字段 id/playerId/type/typeId',
          int(f.get(1, 0)) == 7 and int(f.get(2, 0)) == 42
          and int(f.get(3, 0)) == 18 and int(f.get(4, 0)) == 3,
          f'{f}')
    check('C2 readStatus=0（Unread 才弹窗）',
          int(f.get(5, -1)) == 0, f'readStatus={f.get(5)}')
    check('C3 timestamp/expires 非空',
          bool(f.get(6)) and bool(f.get(7)), f'{f.get(6)} {f.get(7)}')


# ------------------------------------------------------------ D. unlockBits 按等级

def test_unlock_bits():
    print('[D] player unlockBits 按等级置位')
    # PlayerLevelUnlock 行：id=1(Assault lv1) / id=2(captureHoldInfantry lv2)
    # / id=17(eWar lv10) / id=7,9,11,12,26(角色模板，恒关)
    b1 = player_unlock_bits_for_level(1)
    check('D1 1 级仅默认 Assault 模式位开',
          b1[1] and not b1[2] and not b1[17], f'on={[i for i, v in enumerate(b1) if v]}')
    b2 = player_unlock_bits_for_level(2)
    check('D2 2 级开 captureHoldInfantry（id=2）+ 货币位（id=3）',
          b2[1] and b2[2] and b2[3], f'on={[i for i, v in enumerate(b2) if v]}')
    b9 = player_unlock_bits_for_level(9)
    check('D3 9 级 War（id=17）仍锁',
          not b9[17])
    b10 = player_unlock_bits_for_level(10)
    check('D4 10 级解锁 War（id=17）',
          b10[17])
    b4 = player_unlock_bits_for_level(4)
    b5 = player_unlock_bits_for_level(5)
    b6 = player_unlock_bits_for_level(6)
    b7 = player_unlock_bits_for_level(7)
    b8 = player_unlock_bits_for_level(8)
    b12 = player_unlock_bits_for_level(12)
    check('D5 兵种位按等级开（坦克5/伞兵6/飞行员8/侦察9），将军(26)恒关',
          b5[11] and b6[7] and b8[12] and b9[9] and not b12[26]
          and not b4[11] and not b5[7] and not b7[12] and not b8[9],
          f'on12={[i for i, v in enumerate(b12) if v]}')
    check('D6 avail=0 行恒关（id=10 金币 / id=27-29 referral）',
          not b12[10] and not any(b12[i] for i in (27, 28, 29)))
    check('D7 位图长度 41（与旧 [True]*41 兼容）',
          len(b1) == 41, f'len={len(b1)}')


if __name__ == '__main__':
    test_wardb_tables()
    test_check_player_level()
    test_notification_row()
    test_unlock_bits()
    print(f'\n{"=" * 50}')
    print(f'PASS={PASS} FAIL={FAIL}')
    sys.exit(1 if FAIL else 0)
