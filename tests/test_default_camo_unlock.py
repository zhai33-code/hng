# -*- coding: utf-8 -*-
"""2026-09-01 默认迷彩解锁位修复的回归测试（纯单元级，无需服务器/DB）。

  _char_unlock_bits_for 的勋带等级推导只遍历已有 XP 行（character_ribbon_xp），
  新兵/没打过对应兵种勋带的角色没有 XP 行 → unlockMetaLevel==0 的默认迷彩
  uid（1156/1203-1216）进不了解锁位图 → 客户端 hasCharacterUnlockedItem
  纯按位图判定（无 level 0 特判），军服界面把默认迷彩显示成"需解锁"。

  修复：无 XP 行的勋带视为 0 级（官方语义：勋带人人从 0 级开始），把全部
  unlockMetaLevel==0 的解锁 uid（按阵营/职业过滤）授予进位图。

  覆盖点：
    1. 新兵（无 XP 行/无购物）默认迷彩 uid 全部补齐（11 个兵种×阵营组合）
    2. 勋带 XP 等级路径不受影响（PPSh-41 uid 1763 需 ribbon22 lv9：0 级不给/达标给）
    3. 跨阵营/跨职业的 level=0 迷彩位不误授
    4. 历史泄漏 uid（299/339/529/516/87/94/122/50，均 level>=1）不回归
    5. _LEVEL0_UIDS 数据边界：不含职业模板（itemType=1）与历史泄漏 uid
    6. 飞机初购 egg-chicken：uid 1401（Yak-9 Specialist level=0）在位图
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('HNG_GAME_DIR', r'd:\HnG-test')

import player_service                                # noqa: E402
import wardb                                         # noqa: E402
from starting_gear import STARTING_GEAR              # noqa: E402

PASS = FAIL = 0


def check(label, ok, detail=''):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f'[PASS] {label}{("  " + detail) if detail else ""}')
    else:
        FAIL += 1
        print(f'[FAIL] {label}{("  " + detail) if detail else ""}')


# ----------------------------------------------------------------桩：替换 STORE
class _StubStore:
    """空库桩：默认无勋带 XP 行/无购物；用 ribbon_xp 属性注入 XP 场景。"""

    def __init__(self, ribbon_xp=None):
        self.ribbon_xp = ribbon_xp or {}

    async def get_ribbon_xp(self, pid, char_id):
        return dict(self.ribbon_xp)

    async def list_characters(self, pid):
        return []

    async def list_weapons(self, pid, cid):
        return []

    async def list_weapon_modifiers(self, pid, cid):
        return []

    async def list_weapon_ammo(self, pid, cid):
        return []

    async def list_vehicles(self, pid, cid):
        return []


class _Sess:
    def __init__(self, pid=1):
        self._pid = pid

    async def ensure_player(self):
        return self._pid


def _bits(template, faction, ribbon_xp=None):
    """无 DB 计算解锁位图，返回已置位 uid 集合。"""
    player_service.STORE = _StubStore(ribbon_xp)
    bl = asyncio.run(player_service._char_unlock_bits_for(
        _Sess(), template, faction, 0))
    return {i for i, b in enumerate(bl) if b}


# (兵种, 阵营) -> 默认迷彩解锁 uid（progressionUnlock itemType=16 level=0
# unlockable=True 条目；苏军步兵 variant 28 无解锁条目，客户端天然解锁）
CAMO = {(1, 1): 1203, (1, 2): 1204, (5, 1): 1206, (5, 2): 1156, (5, 3): 1207,
        (4, 1): 1211, (4, 2): 1212, (4, 3): 1213, (6, 1): 1214, (6, 2): 1216,
        (6, 3): 1215}
# 历史四轮泄漏同根因 uid（默认序列授予物品类），均 unlockMetaLevel>=1
LEAKS = [299, 339, 529, 516, 87, 94, 122, 50]


def _starting_gear_uids(faction, template):
    """STARTING_GEAR 合法路径应产生的 uid（泄漏断言需排除）。"""
    gear = STARTING_GEAR.get((faction, template), {})
    uids = set()
    wt = gear.get('weapon')
    if wt:
        uids |= wardb._UNLOCK_BY_ITEM.get((wt, wardb._E_WEAPON), set())
    vv = gear.get('vehicle_variant')
    if vv:
        uids |= wardb._UNLOCK_BY_ITEM.get((vv, wardb._E_VEHICLES_VARIANT), set())
        uids |= wardb._UNLOCK_BY_ITEM.get((vv, wardb._E_VEHICLE), set())
        parent = wardb._VEHICLE_VARIANT_PARENT.get(vv)
        if parent:
            uids |= wardb._UNLOCK_BY_ITEM.get((parent, wardb._E_VEHICLE), set())
    return uids


def test_default_camo_bits():
    print('== 1. 新兵默认迷彩 uid 补齐 ==')
    for (tmpl, fac), uid in sorted(CAMO.items()):
        on = _bits(tmpl, fac)
        check(f'tmpl={tmpl} fac={fac} 含默认迷彩 uid {uid}', uid in on)


def test_ribbon_xp_path():
    print('== 2. 勋带 XP 等级路径不受影响 ==')
    # PPSh-41（weapon 5）苏军条目 uid 1763 = ribbon 22 level 9（见
    # test_purchase_fixes）。0 级（有 XP 行但等级不足）不得授予；
    # 1,000,000 XP 等级必 >= 9，应授予。
    lv0 = _bits(1, 3, ribbon_xp={22: 0})
    check('ribbon22 有 XP 行但 0 级：uid 1763 不授予', 1763 not in lv0)
    hi = _bits(1, 3, ribbon_xp={22: 1_000_000})
    check('ribbon22 达 9 级：uid 1763 授予', 1763 in hi)
    # 修复核心场景：无 XP 行的坦克兵仍拿默认迷彩（1207）
    check('无 XP 行苏军坦克兵含默认迷彩 1207', 1207 in _bits(5, 3))


def test_no_cross_grant():
    print('== 3. 跨阵营/跨职业 level=0 迷彩位不误授 ==')
    on = _bits(1, 3)  # 苏军步兵
    wrong = [u for (t, f), u in CAMO.items()
             if u in on and (f != 3 or t != 1)]
    check('苏军步兵不含他阵营/他职业默认迷彩位', not wrong, f'误授={wrong}')
    on2 = _bits(6, 2)  # 德军侦察兵
    wrong2 = [u for (t, f), u in CAMO.items()
              if u in on2 and (f != 2 or t != 6)]
    check('德军侦察兵不含他阵营/他职业默认迷彩位', not wrong2, f'误授={wrong2}')


def test_no_leak_regression():
    print('== 4. 历史泄漏 uid 不回归 ==')
    for tmpl, fac in ((1, 3), (5, 3), (6, 2)):
        on = _bits(tmpl, fac)
        gear = _starting_gear_uids(fac, tmpl)
        # 排除 STARTING_GEAR 合法路径（如苏军坦克兵起始武器 PPD-40 → uid 299）
        bad = [u for u in LEAKS if u in on and u not in gear]
        check(f'tmpl={tmpl} fac={fac} 无泄漏 uid（剔除起始装备路径）',
              not bad, f'泄漏={bad}' if bad else '')


def test_level0_data_boundary():
    print('== 5. _LEVEL0_UIDS 数据边界 ==')
    # level=0 条目不得含职业模板（itemType=1）——职业招募走 PlayerLevelUnlock
    itype1 = [int(f.get(1, 0)) for f in wardb._parse_wardb_table('progressionUnlock')
              if int(f.get(6, 0)) == 0 and int(f.get(7, 0)) == 1]
    check('level=0 条目不含 itemType=1（职业模板）', not itype1,
          f'违例={itype1}' if itype1 else '')
    check('_LEVEL0_UIDS 含默认迷彩 uid', all(u in wardb._LEVEL0_UIDS
                                           for u in CAMO.values()))
    check('_LEVEL0_UIDS 不含历史泄漏 uid',
          not [u for u in LEAKS if u in wardb._LEVEL0_UIDS])


def test_plane_first_purchase():
    print('== 6. 飞机初购 egg-chicken：level=0 专属勋带在位图 ==')
    # Yak-9 Specialist（ribbon 68 level=0，fac=3 苏军专属条目）：官方语义
    # 人人 0 级即满足，位图必含 1401，客户端才不把首购 Yak-9 显示成锁定
    check('苏军飞行员位图含 uid 1401', 1401 in _bits(2, 3))
    # uid 1401 是苏军条目，美军飞行员被阵营过滤（不误授）
    check('美军飞行员不含苏军专属 uid 1401', 1401 not in _bits(2, 1))
    # 美军侧同语义：uid 1046（variant 38 美军步兵 level=0 迷彩，非起始装备）
    check('美军步兵位图含 level=0 uid 1046', 1046 in _bits(1, 1))


def main():
    test_default_camo_bits()
    test_ribbon_xp_path()
    test_no_cross_grant()
    test_no_leak_regression()
    test_level0_data_boundary()
    test_plane_first_purchase()
    print(f'\n结果: PASS={PASS} FAIL={FAIL}')
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
