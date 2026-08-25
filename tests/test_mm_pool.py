"""匹配池纯逻辑单测（无需服务器/DB，macOS 可直接跑）。

覆盖（对齐 .trae/documents/matchmaking-pool-plan.md Tests 节）：
1. 交集成组（无交集者留池）
2. 空选图通配
3. min/max 截断（人数不足不成局；超 max 截断留池）
4. FIFO 锚点（后来者不能挤掉先排队者）
5. 重复入队覆盖（人数不虚增）
6. 选图多数票（并列取 preset id 小者；全空 → FALLBACK_PRESET）
7. remove 出池
8. force_form 超时 AI 补位（1 人也成局）
9. 模式隔离（不同 gamemode 不互撮）
10. 训练模式单人直开（min=1）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mm_config
import mm_pool


RESULTS = []


def check(label, ok, detail=''):
    RESULTS.append((label, ok))
    print(f'[{"PASS" if ok else "FAIL"}] {label}{("  " + detail) if detail else ""}')


def entry(pid, mode=5, presets=(), faction=3, char_id=None):
    return mm_pool.QueueEntry(player_id=pid, char_id=char_id or pid,
                              faction=faction, gamemode_id=mode,
                              presets=tuple(presets))


def main():
    # 测试专用模式 99（不受环境变量 MM_MIN_PLAYERS 影响真实模式）
    mm_config.GAMEMODES[99] = dict(name='test', min_players=2, max_players=3,
                                   ai_fill_after=30)

    # ---- 1. 交集成组 ----
    p = mm_pool.MatchPool()
    p.enqueue(entry(1, presets=[10]))          # A 只选 10
    p.enqueue(entry(2, presets=[26]))          # B 只选 26（与 A 无交集）
    p.enqueue(entry(3, presets=[10, 29]))      # C 选 10/29
    m = p.try_form()
    check('交集成组：A+C 成组', m is not None and
          {e.player_id for e in m.players} == {1, 3},
          f'players={[e.player_id for e in m.players] if m else None}')
    check('交集成组：选图取交集内多数票 preset=10',
          m is not None and m.preset_id == 10, f'preset={m.preset_id if m else None}')
    check('交集成组：B 留池', p.waiting_count() == 1 and
          p.get(2) is not None)

    # ---- 2. 空选图通配 ----
    p = mm_pool.MatchPool()
    p.enqueue(entry(1, mode=99, presets=[10]))
    p.enqueue(entry(2, mode=99, presets=()))   # 未选图 = 通配
    m = p.try_form()
    check('空选图通配：可与选图者成组', m is not None and
          {e.player_id for e in m.players} == {1, 2})
    check('空选图通配：选图回落组内非空投票', m is not None and
          m.preset_id == 10)

    # ---- 3. min/max 截断 ----
    p = mm_pool.MatchPool()
    p.enqueue(entry(1, mode=99))
    check('人数不足：1 人不成局', p.try_form() is None)
    for pid in (2, 3, 4):
        p.enqueue(entry(pid, mode=99))
    # 池内现有 4 人（1-4），max=3 → 成 3 人组，1 人留池
    m = p.try_form()
    check('max 截断：4 人成 3 人组', m is not None and len(m.players) == 3,
          f'group={[e.player_id for e in m.players] if m else None}')
    check('max 截断：锚点(最早)必在组内', m is not None and
          m.players[0].player_id == 1)
    check('max 截断：剩 1 人留池', p.waiting_count() == 1)

    # ---- 4. FIFO 锚点 ----
    p = mm_pool.MatchPool()
    p.enqueue(entry(1, mode=99, presets=[10]))   # 最早
    p.enqueue(entry(2, mode=99, presets=[26]))   # 与锚点不兼容
    p.enqueue(entry(3, mode=99, presets=[10]))   # 兼容锚点
    m = p.try_form()
    check('FIFO：锚点与兼容者成组', m is not None and
          {e.player_id for e in m.players} == {1, 3})
    check('FIFO：不兼容者留池', p.waiting_count() == 1 and p.get(2) is not None)

    # ---- 5. 重复入队覆盖 ----
    p = mm_pool.MatchPool()
    p.enqueue(entry(1, mode=99, presets=[10]))
    p.enqueue(entry(1, mode=99, presets=[29]))   # 同 player 二次入队
    check('重复入队：人数不虚增', p.waiting_count() == 1)
    check('重复入队：保留最新选图', p.get(1).presets == (29,))

    # ---- 6. 选图多数票 / 并列取小 / 全空兜底 ----
    p = mm_pool.MatchPool()
    p.enqueue(entry(1, mode=99, presets=[26, 10]))
    p.enqueue(entry(2, mode=99, presets=[10, 29]))
    p.enqueue(entry(3, mode=99, presets=[26]))
    # 票数：26→2, 10→2, 29→1 → 并列取 id 小者 10
    m = p.try_form()
    check('选图：并列取 preset id 小者', m is not None and m.preset_id == 10,
          f'preset={m.preset_id if m else None}')
    p = mm_pool.MatchPool()
    p.enqueue(entry(1, mode=99))
    p.enqueue(entry(2, mode=99))
    m = p.try_form()
    check('选图：全空 → FALLBACK_PRESET', m is not None and
          m.preset_id == mm_config.FALLBACK_PRESET)

    # ---- 6b. 模式专属兜底池随机（只选模式不勾图） ----
    mm_config.GAMEMODES[98] = dict(name='test_fb', min_players=2, max_players=3,
                                   ai_fill_after=30,
                                   fallback_presets=[201, 202, 203])
    try:
        p = mm_pool.MatchPool()
        p.enqueue(entry(1, mode=98))
        p.enqueue(entry(2, mode=98))
        m = p.try_form()
        check('模式兜底：无人勾图 → 从该模式 fallback_presets 随机',
              m is not None and m.preset_id in (201, 202, 203),
              f'preset={m.preset_id if m else None}')
        # 随机性冒烟：多局采样应命中不止一个值（1% 误报率可接受）
        picks = set()
        for _ in range(30):
            p2 = mm_pool.MatchPool()
            p2.enqueue(entry(1, mode=98))
            p2.enqueue(entry(2, mode=98))
            m2 = p2.try_form()
            if m2:
                picks.add(m2.preset_id)
        check('模式兜底：随机覆盖多个候选（非固定单值）', len(picks) >= 2,
              f'picks={sorted(picks)}')
        # force_form（AI 补位）同样走模式兜底
        p3 = mm_pool.MatchPool()
        p3.enqueue(entry(1, mode=98))
        m3 = p3.force_form(mm_config.gamemode_cfg(98), 98)
        check('模式兜底：force_form 无选图也用模式池',
              m3 is not None and m3.preset_id in (201, 202, 203),
              f'preset={m3.preset_id if m3 else None}')
    finally:
        mm_config.GAMEMODES.pop(98, None)

    # ---- 7. remove 出池 ----
    p = mm_pool.MatchPool()
    p.enqueue(entry(1, mode=99, presets=[10]))
    p.enqueue(entry(2, mode=99, presets=[10]))
    removed = p.remove(2)
    check('remove：返回被移除 entry', removed is not None and
          removed.player_id == 2)
    check('remove：人数减少', p.waiting_count() == 1)
    check('remove：剩余人数不足不成局', p.try_form() is None)
    check('remove：不在池中返回 None', p.remove(999) is None)

    # ---- 8. force_form 超时 AI 补位 ----
    p = mm_pool.MatchPool()
    cfg = mm_config.gamemode_cfg(99)
    p.enqueue(entry(1, mode=99, presets=[29]))
    check('force_form 前：单人不成局', p.try_form() is None)
    m = p.force_form(cfg, 99)
    check('force_form：单人强制成局', m is not None and len(m.players) == 1)
    check('force_form：标记 ai_fill', m is not None and m.ai_fill is True)
    check('force_form：选图用玩家所选', m is not None and m.preset_id == 29)
    check('force_form：池清空', p.waiting_count() == 0)
    check('force_form：空池返回 None', p.force_form(cfg, 99) is None)

    # ---- 9. 模式隔离 ----
    p = mm_pool.MatchPool()
    p.enqueue(entry(1, mode=3, presets=[10]))
    p.enqueue(entry(2, mode=5, presets=[10]))
    check('模式隔离：跨模式不撮合', p.try_form() is None)
    check('模式隔离：分池计数', p.waiting_count(3) == 1 and
          p.waiting_count(5) == 1 and p.waiting_count() == 2)

    # ---- 10. 训练模式单人直开 ----
    p = mm_pool.MatchPool()
    p.enqueue(entry(1, mode=1))                 # training: min=1
    m = p.try_form()
    check('训练模式：单人立即成局', m is not None and len(m.players) == 1)
    check('训练模式：非 ai_fill', m is not None and m.ai_fill is False)

    failed = [l for l, ok in RESULTS if not ok]
    print(f'\nMM_POOL: {"ALL PASS" if not failed else f"{len(failed)} FAILED: {failed}"} '
          f'({len(RESULTS) - len(failed)}/{len(RESULTS)} passed)')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
