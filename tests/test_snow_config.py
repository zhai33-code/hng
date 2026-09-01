# -*- coding: utf-8 -*-
"""随机下雪功能回归测试（2026-09-01，DB-free）。

功能链：成局时 roll_weather() 按 mm_config.SNOW_CHANCE（环境变量
MM_SNOW_CHANCE 覆盖，默认 0.3=30%）独立抽签 snow 字段 → snow_r_args()
据此返回 /R seasonal.forceSnowEvent=1 参数对拼进战斗服命令行（引擎
季节雪事件，渲染层，不走协议；docs/hng-combat-protocol.md §B.13）。
"""
import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mm_config            # noqa: E402
import matchmaking          # noqa: E402


# ---------- 1. 配置解析 ----------
def test_snow_chance_default():
    """默认概率 30%。"""
    assert mm_config.SNOW_CHANCE == 0.3, mm_config.SNOW_CHANCE


def test_snow_chance_env():
    """MM_SNOW_CHANCE 覆盖；非法值（非数字/越界）回退 0.3。"""
    old = os.environ.pop('MM_SNOW_CHANCE', None)
    try:
        for val, want in (('0', 0.0), ('1', 1.0), ('0.55', 0.55),
                          ('abc', 0.3), ('2.5', 0.3), ('-0.1', 0.3)):
            os.environ['MM_SNOW_CHANCE'] = val
            importlib.reload(mm_config)
            assert mm_config.SNOW_CHANCE == want, \
                (val, mm_config.SNOW_CHANCE)
    finally:
        os.environ.pop('MM_SNOW_CHANCE', None)
        if old is not None:
            os.environ['MM_SNOW_CHANCE'] = old
        importlib.reload(mm_config)
    assert mm_config.SNOW_CHANCE == 0.3


# ---------- 2. roll_weather 抽签 ----------
def test_roll_weather_snow_boundaries():
    """概率边界：1.0 必下 / 0.0 必不下；天气三元组仍来自预设池。"""
    old = mm_config.SNOW_CHANCE
    try:
        mm_config.SNOW_CHANCE = 1.0
        w = matchmaking.roll_weather()
        assert w['snow'] is True, w
        assert (w['weather_tod'], w['weather_condition'],
                w['timeofday']) in matchmaking.WEATHER_PRESETS, w

        mm_config.SNOW_CHANCE = 0.0
        w = matchmaking.roll_weather()
        assert w['snow'] is False, w
    finally:
        mm_config.SNOW_CHANCE = old


# ---------- 3. spawn 参数拼接 ----------
def test_snow_r_args():
    """抽中→完整注册名参数对；未抽中/无本局详情→空（宁可不雪不错拼）。"""
    mid = 987654
    matchmaking._MM_MISSION_DETAILS[mid] = {'snow': True}
    try:
        assert matchmaking.snow_r_args(mid) == \
            ['/R', 'seasonal.forceSnowEvent=1'], matchmaking.snow_r_args(mid)

        matchmaking._MM_MISSION_DETAILS[mid] = {'snow': False}
        assert matchmaking.snow_r_args(mid) == []

        assert matchmaking.snow_r_args(mid + 1) == []   # 无 det = 不下雪
    finally:
        matchmaking._MM_MISSION_DETAILS.pop(mid, None)
