# -*- coding: utf-8 -*-
"""匹配池配置（纯数据模块）。

gamemodeId 取值来源：客户端 GamemodesView.as（反编译实测）：
  1=First Encounter(训练, AI)  3=Capture&Hold Infantry  4=Capture&Hold Tanks/Battle
  5=Assault Infantry  6=Assault Battle  10=War  11=Quick Play

字段语义：
  min_players    成局所需最少玩家数（达到即撮合）
  max_players    单局玩家上限（超出部分留在池中等下一局）
  ai_fill_after  排队等待超时（秒）：超时后不足的人数由 AI 补位开局；
                 0 = 立即成局（训练模式单人直开）

环境变量覆盖（便于测试压低等待时间，不改变代码默认值）：
  MM_AI_FILL_AFTER       覆盖所有模式的 ai_fill_after（秒）
  MM_MAX_COMBAT_SERVERS  覆盖 MAX_CONCURRENT_COMBAT_SERVERS
  MM_MIN_PLAYERS         覆盖所有模式的 min_players
  MM_SIMULATE_COMBAT     =1 时用内嵌模拟器代替 hng.exe（macOS/Linux 等无
                          Windows 客户端环境跑集成测试用；实例登记/端口
                          占用/并发上限/释放逻辑全部照常真实运转）
"""
import os


def _int_env(name, default=None):
    v = os.environ.get(name)
    if v is None or v == '':
        return default
    try:
        return int(v)
    except ValueError:
        print(f'[mm_config] WARNING: {name}={v!r} not an int, '
              f'using default {default}')
        return default


_ENV_AI_FILL_AFTER = _int_env('MM_AI_FILL_AFTER')
_ENV_MIN_PLAYERS = _int_env('MM_MIN_PLAYERS')


def _mode(name, min_players, max_players, ai_fill_after, fallback_presets=None):
    if _ENV_MIN_PLAYERS is not None and min_players > 1:
        min_players = max(1, _ENV_MIN_PLAYERS)
    if _ENV_AI_FILL_AFTER is not None and ai_fill_after > 0:
        ai_fill_after = _ENV_AI_FILL_AFTER
    return dict(name=name, min_players=min_players,
                max_players=max_players, ai_fill_after=ai_fill_after,
                fallback_presets=list(fallback_presets or []))


# 每模式匹配配置（gamemodeId → 参数）
# fallback_presets：组内无人勾选地图时的随机兜底池（模式专属地图，
# 数据源 missionpreset 表名称归类，preset → gamemap → scenefile 链见 wardb.py）
GAMEMODES = {
    1:  _mode('training',         1,  1,  0,  [9, 10, 11]),            # 训练：单人立即开局
    3:  _mode('capture_hold_inf', 2, 18, 30,  [19, 20, 21, 1, 373394539,
                                               373394540, 373394541]),
    4:  _mode('capture_hold_all', 2, 18, 30,  [12, 22, 23, 24, 25]),
    5:  _mode('assault_infantry', 2, 18, 30,  [8, 4, 1345335, 400513682,
                                               595333531, 775928046]),
    6:  _mode('assault_battle',   2, 18, 30,  [26, 27, 28, 29, 31]),
    10: _mode('war',              2, 18, 30,  [26, 27, 28, 29, 31]),   # 同 assault 小图（war 局通常由联盟推图，此处兜底）
    11: _mode('quickplay',        2, 18, 30,  [26, 27, 28, 29, 31]),   # quickplay 无选图 → 随机小图
}
DEFAULT_GAMEMODE = _mode('default', 2, 18, 30)    # 未知模式兜底


def gamemode_cfg(gamemode_id):
    """按 gamemodeId 取配置（未知模式回退 DEFAULT_GAMEMODE）。"""
    return GAMEMODES.get(int(gamemode_id), DEFAULT_GAMEMODE)


# combat server 并发上限：达到上限时成组的战局排队等待（不拉起进程），
# 有实例释放（release_match）后按成局先后顺序拉起。0 = 不限制。
MAX_CONCURRENT_COMBAT_SERVERS = _int_env('MM_MAX_COMBAT_SERVERS', 4)
if MAX_CONCURRENT_COMBAT_SERVERS < 0:
    print(f'[mm_config] WARNING: MM_MAX_COMBAT_SERVERS='
          f'{MAX_CONCURRENT_COMBAT_SERVERS} < 0, using 4')
    MAX_CONCURRENT_COMBAT_SERVERS = 4

# 战局 combat server UDP 端口池（端口数应 >= 并发上限）
# MM_COMBAT_PORT_BASE / MM_COMBAT_PORT_MAX 可覆盖（测试构造端口耗尽场景）
COMBAT_PORT_BASE = _int_env('MM_COMBAT_PORT_BASE', 5678)
COMBAT_PORT_MAX = _int_env('MM_COMBAT_PORT_MAX', 5699)
if COMBAT_PORT_BASE > COMBAT_PORT_MAX:
    # 非法区间：回退默认（留 P0 修复的回池逻辑兜底"不会发生"的耗尽场景）
    print(f'[mm_config] WARNING: port range {COMBAT_PORT_BASE}-{COMBAT_PORT_MAX} '
          f'invalid (base > max), using 5678-5699')
    COMBAT_PORT_BASE, COMBAT_PORT_MAX = 5678, 5699
_ports = COMBAT_PORT_MAX - COMBAT_PORT_BASE + 1
if (MAX_CONCURRENT_COMBAT_SERVERS and
        _ports < MAX_CONCURRENT_COMBAT_SERVERS):
    print(f'[mm_config] WARNING: port pool ({_ports} ports) < concurrent limit '
          f'({MAX_CONCURRENT_COMBAT_SERVERS}) - 并发上限实际由端口池决定')

# 玩家全未选图时的兜底 preset（WarDatabase 实测可用：
# 10 = Training - Encounter - Mill）
FALLBACK_PRESET = 10

# 模拟 combat server（macOS 等无 hng.exe 环境的集成测试）：不 spawn 进程，
# 由 warserver 内嵌模拟——bind UDP 端口 + 直接完成 BattleFieldSettings 握手
SIMULATE_COMBAT = bool(_int_env('MM_SIMULATE_COMBAT'))
