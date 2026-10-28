# -*- coding: utf-8 -*-
"""战局天气动态配置测试（2026-09-07，DB-free 纯函数）。

功能链：web 管理端 PUT /api/config/weather → server_config 表 'weather'
行（JSONB：tod_weights/cond_weights/excluded/snow_chance）→ 游戏服成局
时 matchmaking._weather_details() 读库 → roll_weather(cfg) 按维度独立
模型抽签；无行/字段非法 → 内置默认（roll_weather()，snow 沿用
mm_config.SNOW_CHANCE）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mm_config          # noqa: E402
import mm_weather         # noqa: E402


def _cfg(**over):
    cfg = {'tod_weights': [1, 1, 1, 1], 'cond_weights': [1, 1, 1],
           'excluded': [], 'snow_chance': 0.0}
    cfg.update(over)
    return cfg


def _in_presets(w):
    """结果组合必须落在原版预设池内（hour 与时段的绑定关系不变）。"""
    return ((w['weather_tod'], w['weather_condition'], w['timeofday'])
            in mm_weather.WEATHER_PRESETS)


# ---------- 1. 强制组合（确定性抽签） ----------
def test_forced_combo():
    """单时段 100% + 单天气 100% + snow 边界。"""
    cfg = _cfg(tod_weights=[0, 0, 1, 0], cond_weights=[1, 0, 0],
               snow_chance=0.0)
    for _ in range(50):
        w = mm_weather.roll_weather(cfg)
        assert w['weather_tod'] == 2 and w['weather_condition'] == 0, w
        assert w['timeofday'] == 12.0, w
        assert w['snow'] is False, w

    cfg = _cfg(tod_weights=[1, 0, 0, 0], cond_weights=[0, 0, 1],
               snow_chance=1.0)
    for _ in range(50):
        w = mm_weather.roll_weather(cfg)
        assert w['weather_tod'] == 0 and w['weather_condition'] == 2, w
        assert w['timeofday'] == 0.0, w
        assert w['snow'] is True, w


def test_hour_follows_tod():
    """hour 恒由时段决定（夜 0 / 黎 6 / 白 12 / 黄 18），与权重无关。"""
    for _ in range(200):
        w = mm_weather.roll_weather(_cfg())
        assert w['timeofday'] == mm_weather.TOD_HOURS[w['weather_tod']], w


# ---------- 2. 权重语义 ----------
def test_zero_tod_weight_never_picked():
    cfg = _cfg(tod_weights=[0, 1, 0, 0])
    for _ in range(100):
        assert mm_weather.roll_weather(cfg)['weather_tod'] == 1


def test_zero_cond_weight_never_picked():
    cfg = _cfg(cond_weights=[0, 0, 1])
    for _ in range(100):
        assert mm_weather.roll_weather(cfg)['weather_condition'] == 2


# ---------- 3. 排除组合 ----------
def test_excluded_combos_never_picked():
    excluded = [[0, 2], [1, 2], [3, 2], [2, 0]]
    cfg = _cfg(excluded=excluded)
    for _ in range(300):
        w = mm_weather.roll_weather(cfg)
        assert [w['weather_tod'], w['weather_condition']] not in excluded, w


def test_tod_all_conds_excluded_disappears():
    """白天三天气全排除 → 白天不出现（其余时段正常）。"""
    cfg = _cfg(excluded=[[2, 0], [2, 1], [2, 2]])
    seen = set()
    for _ in range(300):
        w = mm_weather.roll_weather(cfg)
        assert w['weather_tod'] != 2, w
        seen.add(w['weather_tod'])
    assert seen == {0, 1, 3}, seen


def test_tod_avail_conds_all_zero_weight_disappears():
    """可用天气权重全为 0 的时段同样不出现（random.choices 防呆）。"""
    # 阴天权重 0，且白天排除 少云/雾 → 白天只剩权重为 0 的阴天 → 不可选
    cfg = _cfg(cond_weights=[1, 0, 1], excluded=[[2, 0], [2, 2]])
    for _ in range(100):
        assert mm_weather.roll_weather(cfg)['weather_tod'] != 2


# ---------- 4. 退化配置回落默认 ----------
def test_all_excluded_falls_back_to_defaults():
    """12 种组合全排除 → 回落内置默认模型（结果集 ⊆ 原预设池）。"""
    cfg = _cfg(excluded=[[t, c] for t in range(4) for c in range(3)])
    for _ in range(30):
        assert _in_presets(mm_weather.roll_weather(cfg))


def test_all_zero_weights_falls_back_to_defaults():
    for cfg in (_cfg(tod_weights=[0, 0, 0, 0]),
                _cfg(cond_weights=[0, 0, 0])):
        for _ in range(30):
            assert _in_presets(mm_weather.roll_weather(cfg))


# ---------- 5. 字段清洗（手工改库产生的脏数据） ----------
def test_invalid_weights_fall_back_fieldwise():
    """长度不符/负数/非数值 → 该维度回落默认（其余维度照常生效）。"""
    cfg = _cfg(tod_weights=[9, 9], cond_weights=[1, -1, 1],
               excluded=[[0, 1]])
    tods, conds = set(), set()
    for _ in range(200):
        w = mm_weather.roll_weather(cfg)
        # 合法的 excluded=[[0,1]] 继续生效：夜晚·阴天 不出现
        assert w['weather_condition'] != 1 or w['weather_tod'] != 0, w
        # 回落默认权重后四个时段/三种天气都可达（权重非 0）
        tods.add(w['weather_tod'])
        conds.add(w['weather_condition'])
    assert tods == {0, 1, 2, 3} and conds == {0, 1, 2}, (tods, conds)


def test_invalid_excluded_falls_back_to_default():
    """excluded 非列表 → 沿用默认排除（夜雾/黎明雾/黄昏雾不出现）。"""
    cfg = _cfg(excluded='garbage')
    for _ in range(200):
        w = mm_weather.roll_weather(cfg)
        assert (w['weather_tod'], w['weather_condition']) \
            not in mm_weather.DEFAULT_EXCLUDED, w


def test_excluded_invalid_entries_are_skipped():
    """非法条目剔除、合法条目保留。"""
    cfg = _cfg(excluded=[[9, 9], ['x', 0], [1, 1], None, [0, 0]])
    for _ in range(200):
        w = mm_weather.roll_weather(cfg)
        assert [w['weather_tod'], w['weather_condition']] \
            not in ([1, 1], [0, 0]), w


def test_invalid_snow_chance_uses_mm_config():
    old = mm_config.SNOW_CHANCE
    try:
        mm_config.SNOW_CHANCE = 1.0
        for bad in ('x', None, -0.5, 2, True):
            cfg = _cfg(snow_chance=bad)
            for _ in range(5):
                # 回落 mm_config.SNOW_CHANCE=1.0 → 必下雪
                assert mm_weather.roll_weather(cfg)['snow'] is True, bad
    finally:
        mm_config.SNOW_CHANCE = old


# ---------- 6. 无参调用（现状回归，不依赖 DB） ----------
def test_no_cfg_matches_legacy_presets():
    """无 cfg：结果组合必须都在原版预设池内（等价旧行为）。"""
    old = mm_config.SNOW_CHANCE
    try:
        mm_config.SNOW_CHANCE = 0.0
        for _ in range(300):
            assert _in_presets(mm_weather.roll_weather())
        mm_config.SNOW_CHANCE = 1.0
        for _ in range(10):
            w = mm_weather.roll_weather()
            assert w['snow'] is True and _in_presets(w)
    finally:
        mm_config.SNOW_CHANCE = old
