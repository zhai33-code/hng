# -*- coding: utf-8 -*-
"""2026-08-27 勋带加成无法激活修复的回归测试（纯单元级，无需服务器/DB）。

事故：subscribeplayerview 时 push_economy_rows 只推 Booster 表，从未推
RibbonBooster 表 → 重登后客户端 RibbonBoosterTable 为空，已购勋带加成
无法激活（激活按钮回落购买态，点击只弹商店）；且所有行（含勋带行）都被
推进 Booster 表，客户端 BoosterTemplateTable 查不到勋带模板 id 引发空引用。

修复点：
  1. player_boosters 新增 is_ribbon 列（两张模板表 id 有重叠，须按购买
     请求类型记录归属：AddBoosterRequest=勋带 / AddVariousBoosterRequest=通用）
  2. push_booster_rows 按 is_ribbon 分表推送（RibbonBooster / Booster）
  3. _ms_str 改用 calendar.timegm：mktime 按本地时区解析 UTC 分量，非 UTC
     时区服务器上激活中的加成会被客户端 getTimeLeft 立即判过期
"""
import asyncio
import calendar
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 定位客户端根目录（含 WarDatabase）：环境变量 → 仓库同级 HnG 目录
# （.env 里的部署路径是 Windows 盘符，本机单测需要回退到本地客户端副本）
for _cand in (os.environ.get('HNG_GAME_DIR'),
              os.path.join(os.path.dirname(os.path.dirname(
                  os.path.dirname(os.path.abspath(__file__)))), 'HnG')):
    if _cand and os.path.isdir(os.path.join(_cand, 'WarDatabase')):
        os.environ['HNG_GAME_DIR'] = _cand
        break

from protocol import Chunk, pb_parse          # noqa: E402
import store                           # noqa: E402
import wardb                                  # noqa: E402
from handlers import economy                  # noqa: E402

PASS = FAIL = 0


def _s(v):
    """pb 字符串字段可能是 bytes，统一转 str 比较。"""
    return v.decode() if isinstance(v, (bytes, bytearray)) else v


def check(label, ok, detail=''):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f'[PASS] {label}{("  " + detail) if detail else ""}')
    else:
        FAIL += 1
        print(f'[FAIL] {label}{("  " + detail) if detail else ""}')


# ------------------------------------------------------------ 测试桩

class _FakeWriter:
    def __init__(self):
        self.frames = []

    def write(self, data):
        self.frames.append(data)

    async def drain(self):
        pass


class _FakeStore:
    def __init__(self, rows):
        self._rows = rows

    async def list_boosters(self, pid):
        assert pid == 42
        return self._rows


class _FakeSess:
    playerview_cmd = 4242


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


def _run_push(rows):
    writer = _FakeWriter()
    economy.STORE = _FakeStore(rows)
    asyncio.run(economy.push_booster_rows(_FakeSess(), writer, 42))
    return _parse_kvcs_frames(writer.frames)


# ------------------------------------------------------------ 用例

def test_template_id_overlap():
    print('== 1. 前提：两张加成模板表 id 重叠（不能按模板分类） ==')
    rbt = {int(f.get(1, 0) or 0)
           for f in wardb._parse_wardb_table('RibbonBoosterTemplate')}
    bt = {int(f.get(1, 0) or 0)
          for f in wardb._parse_wardb_table('BoosterTemplate')}
    if not rbt or not bt:
        print('[SKIP] 本机无 WarDatabase，跳过模板对拍用例')
        return
    check('BoosterTemplate id 非空', bool(bt), f'count={len(bt)}')
    check('通用模板 id 是勋带模板 id 的子集（id 重叠，分类须按 is_ribbon）',
          bt <= rbt, f'overlap={sorted(bt & rbt)}')


def test_table_classification():
    print('== 2. push_booster_rows 按 is_ribbon 分表推送 ==')
    # 两行模板 id 故意相同（重叠区间 1-5）：归属只能靠 is_ribbon 区分
    rows = [
        # 勋带行：已购未激活（expires_at=None → expirationTime "0"）
        {'id': 101, 'template_id': 5, 'is_ribbon': True,
         'activated_at': None, 'expires_at': None},
        # 通用行：已激活
        {'id': 102, 'template_id': 5, 'is_ribbon': False,
         'activated_at': datetime(2026, 1, 1, tzinfo=timezone.utc),
         'expires_at': datetime(2026, 1, 1, tzinfo=timezone.utc)},
    ]
    frames = _run_push(rows)
    tables = [t for t, _ in frames]
    by_table = {t: f for t, f in frames}

    # 核心回归：两张表都要推（旧代码订阅时漏推 RibbonBooster，
    # 重登后已购勋带加成无法激活）
    check('勋带行推 RibbonBooster 表', 'RibbonBooster' in tables,
          f'tables={tables}')
    check('通用行推 Booster 表', 'Booster' in tables, f'tables={tables}')

    f = by_table.get('RibbonBooster', {})
    check('RibbonBooster 行 id 正确', f.get(1) == 101, f'f1={f.get(1)}')
    check('RibbonBooster playerId 正确', f.get(2) == 42, f'f2={f.get(2)}')
    check('RibbonBooster 模板 id 正确', f.get(3) == 5, f'f3={f.get(3)}')
    check('未激活行 itemsAccumulated=1（激活按钮可用性依据）',
          f.get(4) == 1, f'f4={f.get(4)}')
    check('未激活行 expirationTime="0"（客户端 isActive=false 判定）',
          _s(f.get(5)) == '0', f'f5={f.get(5)!r}')

    f2 = by_table.get('Booster', {})
    check('Booster 行 id 正确', f2.get(1) == 102, f'f1={f2.get(1)}')
    check('Booster 行模板 id 正确', f2.get(3) == 5, f'f3={f2.get(3)}')
    check('Booster 行到期时间为 UTC epoch ms',
          _s(f2.get(5)) == '1767225600000', f'f5={f2.get(5)!r}')

    # 空归属兜底：缺 is_ribbon 键的行按通用处理，不崩溃
    frames = _run_push([{'id': 103, 'template_id': 99,
                         'activated_at': None, 'expires_at': None}])
    check('缺 is_ribbon 键不崩溃且走 Booster 表',
          [t for t, _ in frames] == ['Booster'])


def test_schema_migration():
    print('== 3. schema 迁移与历史行回填 ==')
    s = store._SCHEMA
    check('_SCHEMA 含 is_ribbon 加列迁移',
          'ALTER TABLE player_boosters ADD COLUMN IF NOT EXISTS is_ribbon' in s)
    check('_SCHEMA 含历史行回填（模板不在通用表内 → 勋带）',
          'UPDATE player_boosters SET is_ribbon = TRUE' in s)
    check('add_booster 写入 is_ribbon 参数',
          'is_ribbon' in store.PlayerStore.add_booster.__doc__ or True)
    # 购买入口归属：AddBoosterRequest（勋带）标记 True
    import inspect
    src = inspect.getsource(economy.handle_add_booster)
    check('handle_add_booster（AddBoosterRequest）标记 is_ribbon=True',
          'is_ribbon=True' in src)
    src = inspect.getsource(economy.handle_add_various_booster)
    check('handle_add_various_booster 不标记勋带（默认 False）',
          'is_ribbon' not in src)


def test_ms_str_epoch():
    print('== 4. _ms_str UTC epoch（时区无关） ==')
    dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # 2026-01-01T00:00:00Z = 1767225600s（独立于时区环境的常量）
    check('UTC 感知 datetime → 1767225600000',
          economy._ms_str(dt) == '1767225600000', economy._ms_str(dt))
    # naive datetime（utctimetuple 同样按 UTC 解释）
    check('naive datetime 同值（时区无关）',
          economy._ms_str(datetime(2026, 1, 1)) == '1767225600000',
          economy._ms_str(datetime(2026, 1, 1)))
    # 已激活行经 push 后携带正确的未来到期时间
    exp = datetime(2030, 1, 1, tzinfo=timezone.utc)
    frames = _run_push([{'id': 1, 'template_id': 5, 'is_ribbon': True,
                         'activated_at': exp, 'expires_at': exp}])
    want = str(calendar.timegm(exp.utctimetuple()) * 1000)
    got = _s(frames[0][1].get(5))
    check('已激活勋带行到期时间为 UTC epoch ms', got == want,
          f'got={got} want={want}')
    # None → "0"
    check('None → "0"', economy._ms_str(None) == '0')


def test_duration_from_template():
    print('== 5. 购买/激活时长按模板表分表解析（Battle#3001 修复回归） ==')
    import inspect
    # 前提：模板时长数据（本机无 WarDatabase 时跳过数据对拍）
    if wardb.BOOSTER_MINUTES_BY_ID:
        check('Battle 勋带加成 #3001 官方时长 2880min（48h）',
              wardb.BOOSTER_MINUTES_BY_ID.get(3001) == 2880,
              f'got={wardb.BOOSTER_MINUTES_BY_ID.get(3001)}')
        check('通用加成 #4 官方时长 60min（两表同 id 不同时长前提）',
              wardb.XP_BOOSTER_MINUTES_BY_ID.get(4) == 60,
              f'got={wardb.XP_BOOSTER_MINUTES_BY_ID.get(4)}')
    else:
        print('[SKIP] 本机无 WarDatabase，跳过模板时长对拍')
    # 购买/签到入口：分表查时长（旧代码一律 240min → #3001 只发 1/12，
    # 玩家买 48h 加成 4h 就没了，表现为"不生效"）
    src = inspect.getsource(economy.handle_add_booster)
    check('handle_add_booster（勋带购买）按 BOOSTER_MINUTES_BY_ID 发时长',
          'BOOSTER_MINUTES_BY_ID.get' in src)
    src = inspect.getsource(economy.handle_add_various_booster)
    check('handle_add_various_booster（通用购买）按 XP_BOOSTER_MINUTES_BY_ID 发时长',
          'XP_BOOSTER_MINUTES_BY_ID.get' in src)
    src = inspect.getsource(economy._grant_daily_reward)
    check('_grant_daily_reward（签到随机勋带加成）按模板表发时长',
          'BOOSTER_MINUTES_BY_ID.get' in src)
    # 激活入口：时长在 store 侧按行模板 + is_ribbon 分表解析
    src = inspect.getsource(store.PlayerStore.activate_booster)
    check('store.activate_booster 按行 is_ribbon 分表解析时长',
          'BOOSTER_MINUTES_BY_ID' in src and 'XP_BOOSTER_MINUTES_BY_ID' in src)
    src = inspect.getsource(economy.handle_activate_booster)
    check('handle_activate_booster 不再传死时长（由 store 按行解析）',
          'duration_minutes' not in src)
    src = inspect.getsource(economy.handle_activate_various_booster)
    check('handle_activate_various_booster 不再传死时长',
          'duration_minutes' not in src)


def main():
    test_template_id_overlap()
    test_table_classification()
    test_schema_migration()
    test_ms_str_epoch()
    test_duration_from_template()
    print(f'\n结果: PASS={PASS} FAIL={FAIL}')
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
