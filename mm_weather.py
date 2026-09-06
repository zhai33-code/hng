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

# ---- 维度无关权重模型（web 管理端 /config 天气面板可调）----
# 配置存 server_config 表 'weather' 行（JSONB），成局时读库生效：
#   tod_weights  时段权重 [夜, 黎, 白, 黄]
#   cond_weights 天气权重 [少云, 阴, 雾]
#   excluded     排除组合 [[tod, cond], ...]（先抽时段，再在该时段的
#                可用天气内按权重抽；被排除/权重 0 的天气不参与）
#   snow_chance  下雪概率 0.0-1.0
# 无 DB 行 / 字段非法时逐项回落内置默认；snow_chance 缺失沿用
# mm_config.SNOW_CHANCE（MM_SNOW_CHANCE 环境变量照旧生效）。
# 默认值提取自 WEATHER_PRESETS 的边际分布（等效旧行为），故
# roll_weather() 无参调用与原版随机池结果集一致。
DEFAULT_TOD_WEIGHTS = [2, 2, 4, 2]            # 夜/黎/白/黄
DEFAULT_COND_WEIGHTS = [5, 4, 1]              # 少云/阴/雾
DEFAULT_EXCLUDED = frozenset({(0, 2), (1, 2), (3, 2)})   # 夜雾/黎明雾/黄昏雾
TOD_HOURS = (0.0, 6.0, 12.0, 18.0)            # hour 恒由时段决定（原池行为）


def _sanitize_weights(value, size, fallback):
    """权重列表清洗：非列表/长度不符/含负数或非数值 → 整体回落默认。"""
    if not isinstance(value, (list, tuple)) or len(value) != size:
        return fallback
    try:
        weights = [float(w) for w in value]
    except (TypeError, ValueError):
        return fallback
    return weights if all(w >= 0 for w in weights) else fallback


def _sanitize_excluded(value):
    """排除组合清洗：非法条目剔除；缺省/结构非法 → None（沿用默认）。"""
    if not isinstance(value, (list, tuple)):
        return None
    pairs = set()
    for item in value:
        try:
            tod, cond = item
            if isinstance(tod, bool) or isinstance(cond, bool):
                raise TypeError
            tod, cond = int(tod), int(cond)
        except (TypeError, ValueError):
            continue
        if 0 <= tod < 4 and 0 <= cond < 3:
            pairs.add((tod, cond))
    return pairs


def _sanitize_snow_chance(value):
    """下雪概率清洗：0..1 数值；非法/缺省 → None（沿用 mm_config）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    chance = float(value)
    return chance if 0.0 <= chance <= 1.0 else None


def roll_weather(cfg=None):
    """成局时随机抽一套天气/时段（存入 _MM_MISSION_DETAILS，全链路共用）。

    返回 dict 直接并入 mission details：weather_tod / weather_condition /
    timeofday（战斗服 BattleFieldSettings f9/f21/f22 与简报 MissionInfo
    f12/f13 均从这里取，保证简报图标与实际战斗一致；weather_condition
    恒为 0..2 原值，不带雪掩码）；snow 为独立抽签的下雪开关（默认概率
    mm_config.SNOW_CHANCE），由 game_server 战斗服握手处经
    snow_f22_condition() 把雪掩码 OR 进 f22 高位（snow-unified-patch.md
    §3：补丁版 player.dll 于 CreateInstance 拆开，全员同雪）。

    cfg：server_config 表 'weather' 行（web 管理端保存的维度无关配置，
    见 DEFAULT_TOD_WEIGHTS 处注释）；None → 内置默认模型。

    抽签顺序：可用天气（被排除或权重 0 的天气剔除）全空的时段不参与
    → random.choices 抽时段 → 在该时段可用天气集合内按 cond_weights
    归一抽天气 → hour 恒由时段决定（TOD_HOURS，原池行为）。
    顺序抽样使时段/天气的边际分布与权重严格一致（不受排除组合影响）。"""
    cfg = cfg or {}
    tod_w = _sanitize_weights(cfg.get('tod_weights'), 4, DEFAULT_TOD_WEIGHTS)
    cond_w = _sanitize_weights(cfg.get('cond_weights'), 3, DEFAULT_COND_WEIGHTS)
    excluded = _sanitize_excluded(cfg.get('excluded'))
    if excluded is None:
        excluded = DEFAULT_EXCLUDED
    # 某时段可用天气 = 未被排除且权重 > 0 的天气；三种全不可用 → 该时段不出现
    avail = [tuple(c for c in range(3)
                   if (t, c) not in excluded and cond_w[c] > 0)
             for t in range(4)]
    tod_pool = [t for t in range(4) if avail[t] and tod_w[t] > 0]
    if not tod_pool:
        return roll_weather()   # 全时段被排除/权重全零的退化配置 → 默认模型
    tod = random.choices(tod_pool, weights=[tod_w[t] for t in tod_pool])[0]
    cond = random.choices(avail[tod],
                          weights=[cond_w[c] for c in avail[tod]])[0]
    snow_chance = _sanitize_snow_chance(cfg.get('snow_chance'))
    if snow_chance is None:
        snow_chance = mm_config.SNOW_CHANCE
    return {'weather_tod': tod, 'weather_condition': cond,
            'timeofday': TOD_HOURS[tod],
            'snow': random.random() < snow_chance}


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
