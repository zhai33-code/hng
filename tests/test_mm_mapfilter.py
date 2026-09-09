# -*- coding: utf-8 -*-
"""地图筛选（gamemode 13）模式解析 + C&H 编号校正单测。

背景（2026-09-10 修复"用地图筛选匹配人机模式不能秒匹配"）：
  客户端高级筛选视图 "To Battle" 按钮（GamemodesView.as:458）恒发
  gamemodeId=13 + 勾选 preset 列表；服务端此前把 13 当未知模式落
  DEFAULT_GAMEMODE（min=2 / ai_fill=30）→ 勾训练图匹人机也要空等
  30s 超时 AI 补位。修复 = matchmaking._resolve_mapfilter_gamemode 按
  勾选 preset 的 missionCategoryId（missionpreset f12，与客户端
  updateMaps cat==GamemodeId 过滤同源）解析为具体模式。

同批修正 mm_config.GAMEMODES 的 C&H 编号（对齐客户端
GamemodeDescription 静态编号 / missionCategoryId）：
  旧（错）：3=C&H步兵(Skirmish 三图) 4=全兵种([12] Lunéville)
  新（对）：2=C&H步兵 3=C&H坦克([12]) 4=C&H全兵种([22,23,24,25])

本文件覆盖：
  A. GAMEMODES 编号 / 参数 / quickplay 候选集
  B. _resolve_mapfilter_gamemode 解析规则（单分类/混合/未知/收窄）
  C. mm_handler 集成冒烟（import 链 + 解析函数已接线）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mm_config
import mm_pool
import mm_state
import matchmaking

RESULTS = []


def check(label, ok, detail=''):
    RESULTS.append((label, ok))
    print(f'[{"PASS" if ok else "FAIL"}] {label}{("  " + detail) if detail else ""}')


def _entry(pid, mode):
    return mm_pool.QueueEntry(player_id=pid, char_id=pid, faction=3,
                              gamemode_id=mode, presets=())


def main():
    # ---- A. GAMEMODES 编号校正 ----
    check('A1 gamemode 2 = C&H Infantry（Skirmish 三图）',
          mm_config.GAMEMODES[2]['name'] == 'capture_hold_inf'
          and mm_config.GAMEMODES[2]['fallback_presets']
          == [373394539, 373394540, 373394541])
    check('A2 gamemode 3 = C&H Tanks（Lunéville 12）',
          mm_config.GAMEMODES[3]['name'] == 'capture_hold_tank'
          and mm_config.GAMEMODES[3]['fallback_presets'] == [12])
    check('A3 gamemode 4 = C&H 全兵种（22-25）',
          mm_config.GAMEMODES[4]['name'] == 'capture_hold_all'
          and mm_config.GAMEMODES[4]['fallback_presets'] == [22, 23, 24, 25])
    check('A4 C&H 模式参数沿用（min=2 max=18 ai_fill=30）',
          all(mm_config.GAMEMODES[m]['min_players'] == 2
              and mm_config.GAMEMODES[m]['max_players'] == 18
              and mm_config.GAMEMODES[m]['ai_fill_after'] == 30
              for m in (2, 3, 4)))
    check('A5 训练模式 1 语义不变（min=1 ai_fill=0 秒成局）',
          mm_config.GAMEMODES[1]['min_players'] == 1
          and mm_config.GAMEMODES[1]['ai_fill_after'] == 0)
    check('A6 MAPFILTER_GAMEMODE=13 且 13 不入 GAMEMODES',
          mm_config.MAPFILTER_GAMEMODE == 13
          and 13 not in mm_config.GAMEMODES)
    check('A7 quickplay 候选集补 2',
          2 in mm_config.QUICKPLAY_JOIN_CANDIDATES
          and 2 in mm_config.QUICKPLAY_POOL_MODES
          and 13 not in mm_config.QUICKPLAY_POOL_MODES)

    # A8 官方实测 preset → missionCategoryId（wardb dump）与模式号对齐：
    #    cat 1:9/10  2:373394539  3:12  4:22  5:26  6:5  7:35  8:36
    _cat_expect = {9: 1, 10: 1, 373394539: 2, 12: 3, 22: 4, 26: 5,
                   5: 6, 35: 7, 36: 8}
    bad = [p for p, c in _cat_expect.items()
           if int(matchmaking._load_preset(p).get(12, 0) or 0) != c]
    check('A8 preset missionCategoryId 与模式号一一对应', not bad,
          f'bad={bad}')

    # ---- B. _resolve_mapfilter_gamemode ----
    r = matchmaking._resolve_mapfilter_gamemode((10,))
    check('B1 [10] → 模式 1（用户场景：训练图秒成局）',
          r == (1, (10,)), f'got={r}')

    r = matchmaking._resolve_mapfilter_gamemode((9, 10, 32, 33))
    check('B2 全部训练勾选 → 模式 1（32/33 同图别名）',
          r == (1, (9, 10, 32, 33)), f'got={r}')

    r = matchmaking._resolve_mapfilter_gamemode((12,))
    check('B3 [12] → 模式 3（C&H Tanks）', r == (3, (12,)), f'got={r}')

    r = matchmaking._resolve_mapfilter_gamemode((22, 25))
    check('B4 [22,25] → 模式 4（C&H 全兵种）', r == (4, (22, 25)),
          f'got={r}')

    r = matchmaking._resolve_mapfilter_gamemode((26, 27, 31))
    check('B5 [26,27,31] → 模式 5（Assault Small）', r == (5, (26, 27, 31)),
          f'got={r}')

    r = matchmaking._resolve_mapfilter_gamemode((5, 6, 7, 8, 1345335))
    check('B6 war 图组 → 模式 6（cat 6 Assault）',
          r == (6, (5, 6, 7, 8, 1345335)), f'got={r}')

    r = matchmaking._resolve_mapfilter_gamemode((10, 26, 27))
    check('B7 混合训练+Assault → 排除训练落模式 5（选图收窄去 10）',
          r == (5, (26, 27)), f'got={r}')

    r = matchmaking._resolve_mapfilter_gamemode((10, 22, 25))
    check('B8 混合训练+C&H 全兵种 → 模式 4（收窄去训练 preset）',
          r == (4, (22, 25)), f'got={r}')

    # B9/B10 混合多 PvP 分类 → 并入等待人数最多的池
    pool = mm_state.POOL
    seeded = [_entry(910001, 5), _entry(910002, 5), _entry(910003, 6)]
    try:
        for e in seeded:
            pool.enqueue(e)
        r = matchmaking._resolve_mapfilter_gamemode((26, 27, 5, 6))
        check('B9 混合 {5,6} 并入等待最多的模式 5（2 vs 1）',
              r[0] == 5, f'got={r}')
        pool.enqueue(_entry(910004, 6))
        pool.enqueue(_entry(910005, 6))
        r = matchmaking._resolve_mapfilter_gamemode((26, 27, 5, 6))
        check('B10 模式 6 等待反超（1 vs 3）→ 解析到 6',
              r[0] == 6, f'got={r}')
    finally:
        for e in seeded:
            pool.remove(e.player_id)
        pool.remove(910004)
        pool.remove(910005)

    r = matchmaking._resolve_mapfilter_gamemode((999999, 888888))
    check('B11 preset 全未知（fallback 行无 f12）→ None 维持 13',
          r == (None, (999999, 888888)), f'got={r}')

    r = matchmaking._resolve_mapfilter_gamemode(())
    check('B12 空选图 → None（无勾选无从解析）', r == (None, ()),
          f'got={r}')

    r = matchmaking._resolve_mapfilter_gamemode((10, 999999))
    check('B13 未知 preset 混入训练 → 仍解析模式 1（未知项剔除）',
          r == (1, (10,)), f'got={r}')

    # ---- C. mm_handler 集成冒烟 ----
    import mm_handler
    check('C1 mm_handler import 成功且接线解析函数',
          hasattr(mm_handler, '_resolve_mapfilter_gamemode'))

    n_fail = sum(1 for _, ok in RESULTS if not ok)
    print(f'\n=== {len(RESULTS) - n_fail}/{len(RESULTS)} passed ===')
    return 1 if n_fail else 0


if __name__ == '__main__':
    sys.exit(main())
