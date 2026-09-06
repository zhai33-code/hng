# -*- coding: utf-8 -*-
"""随机下雪功能回归测试（2026-09-01，DB-free；2026-09-05 改 f22 编码）。

功能链：成局时 roll_weather() 按 mm_config.SNOW_CHANCE（环境变量
MM_SNOW_CHANCE 覆盖，默认 1=常驻雪；web 管理端 /config 保存的
server_config 'weather' 行优先级更高）独立抽签 snow 字段 → game_server
战斗服握手套 snow_f22_condition() 把雪掩码（SNOW_MASK=0x30）OR 进
BattleFieldSettings f22 高位（低 2 位天气列原样；补丁版 player.dll 于
ZGameMode_CreateInstance 拆开，snow-unified-patch.md §3 方案一）。
mission details 的 weather_condition 恒 0..2（简报 MissionInfo f13
同源，不带掩码）。
"""
import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mm_config            # noqa: E402
import mm_weather          # noqa: E402


# ---------- 1. 配置解析 ----------
def test_snow_chance_default():
    """默认概率 100%（雪补丁验证期常驻雪；web /config 保存后由 DB 行接管）。"""
    assert mm_config.SNOW_CHANCE == 1, mm_config.SNOW_CHANCE


def test_snow_chance_env():
    """MM_SNOW_CHANCE 覆盖；非数字回退默认 1，越界回退 0.3。"""
    old = os.environ.pop('MM_SNOW_CHANCE', None)
    try:
        for val, want in (('0', 0.0), ('1', 1.0), ('0.55', 0.55),
                          ('abc', 1), ('2.5', 0.3), ('-0.1', 0.3)):
            os.environ['MM_SNOW_CHANCE'] = val
            importlib.reload(mm_config)
            assert mm_config.SNOW_CHANCE == want, \
                (val, mm_config.SNOW_CHANCE)
    finally:
        os.environ.pop('MM_SNOW_CHANCE', None)
        if old is not None:
            os.environ['MM_SNOW_CHANCE'] = old
        importlib.reload(mm_config)
    assert mm_config.SNOW_CHANCE == 1


# ---------- 2. roll_weather 抽签 ----------
def test_roll_weather_snow_boundaries():
    """概率边界：1.0 必下 / 0.0 必不下；details 天气三元组保持预设池原值。"""
    old = mm_config.SNOW_CHANCE
    try:
        mm_config.SNOW_CHANCE = 1.0
        w = mm_weather.roll_weather()
        assert w['snow'] is True, w
        assert (w['weather_tod'], w['weather_condition'],
                w['timeofday']) in mm_weather.WEATHER_PRESETS, w
        # 雪局编码后：低 2 位天气列不变 + 高位雪掩码
        f22 = mm_weather.snow_f22_condition(w)
        assert f22 & 0x30 == mm_weather.SNOW_MASK, hex(f22)
        assert f22 & 3 == w['weather_condition'], hex(f22)

        mm_config.SNOW_CHANCE = 0.0
        w = mm_weather.roll_weather()
        assert w['snow'] is False, w
        assert w['weather_condition'] in (0, 1, 2), w
        assert mm_weather.snow_f22_condition(w) == w['weather_condition']
    finally:
        mm_config.SNOW_CHANCE = old


# ---------- 3. f22 编码 ----------
def test_snow_f22_condition():
    """雪局 f22 = 天气列|0x30；非雪局 = 原值；无天气详情回退 0。"""
    for det, want in (({'weather_condition': 1, 'snow': True}, 0x31),
                      ({'weather_condition': 2, 'snow': True}, 0x32),
                      ({'weather_condition': 0, 'snow': True}, 0x30),
                      ({'weather_condition': 2, 'snow': False}, 2),
                      ({'snow': False}, 0),
                      ({}, 0)):
        assert mm_weather.snow_f22_condition(det) == want, (det, want)
