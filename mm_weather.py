"""战局天气/时段预设与编码（mm_weather）。

成局时随机抽天气/时段（简报与战斗服同源）、下雪开关与 f22 雪掩码编码。
从 matchmaking.py 拆出（2026-09-06 重构，纯移动行为不变）。
"""
import random

import mm_config


# ---- 战局天气/时段预设池（成局时随机抽取，简报与战斗服同源）----
# 枚举取自客户端 proto（decompiled proto-as3-classes/HnG_States）：
#   WeatherTimeOfDay 0=夜晚 1=黎明 2=白天 3=黄昏
#   WeatherCondition 0=少云 1=阴天 2=雾
# 三元组 = (weatherTimeOfDay, weatherCondition, timeofday 小时制)；
# timeofday 为 BattleFieldSettings f9（战斗内光照/太阳角度，24 小时浮点）。
# 同一组合重复列出 = 提高出现权重（白天·少云最常见，夜雾不设）。
WEATHER_PRESETS = [
    (2, 0, 12.0),   # 白天·少云
    (2, 0, 12.0),   # 白天·少云（权重加倍）
    (2, 1, 12.0),   # 白天·阴天
    (2, 2, 12.0),   # 白天·雾
    (1, 0, 6.0),    # 黎明·少云
    (1, 1, 6.0),    # 黎明·阴天
    (3, 0, 18.0),   # 黄昏·少云
    (3, 1, 18.0),   # 黄昏·阴天
    (0, 0, 0.0),    # 夜晚·少云
    (0, 1, 0.0),    # 夜晚·阴天
]


def roll_weather():
    """成局时随机抽一套天气/时段（存入 _MM_MISSION_DETAILS，全链路共用）。

    返回 dict 直接并入 mission details：weather_tod / weather_condition /
    timeofday（战斗服 BattleFieldSettings f9/f21/f22 与简报 MissionInfo
    f12/f13 均从这里取，保证简报图标与实际战斗一致；weather_condition
    恒为 0..2 原值，不带雪掩码）；snow 为独立抽签的下雪开关（概率
    mm_config.SNOW_CHANCE 默认 30%），由 game_server 战斗服握手处经
    snow_f22_condition() 把雪掩码 OR 进 f22 高位（snow-unified-patch.md
    §3：补丁版 player.dll 于 CreateInstance 拆开，全员同雪）。"""
    tod, cond, hour = random.choice(WEATHER_PRESETS)
    return {'weather_tod': tod, 'weather_condition': cond, 'timeofday': hour,
            'snow': random.random() < mm_config.SNOW_CHANCE}


# f22 高位雪掩码（snow-unified-patch.md §3 方案一）：0x20=小雪、0x30=雪+大雪，
# 与低 2 位天气列独立组合；补丁版 player.dll 在 ZGameMode_CreateInstance 拆开
# 分别生效（雪位 OR 进季节字节 byte+0x146，天气列照常查表）。原版 dll 收到
# 0x20-0x32 越表读不崩溃但天气错乱——启用前提：客户端与战斗服均已换补丁 dll。
SNOW_MASK = 0x30


def snow_f22_condition(det):
    """本局 BattleFieldSettings f22 = 天气列 |（雪局 OR 雪掩码 SNOW_MASK）。

    只用于 game_server 战斗服握手的 f22 编码；mission details 的
    weather_condition 保持 0..2 原值（简报 MissionInfo f13 同源，
    不能带掩码）。"""
    cond = int(det.get('weather_condition', 0))
    return (cond | SNOW_MASK) if det.get('snow') else cond
