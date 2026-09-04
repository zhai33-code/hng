# -*- coding: utf-8 -*-
"""匹配池配置（纯数据模块）。

gamemodeId 取值来源：客户端 MatchmakingDescription.as / GamemodesMediator.as（反编译实测）：
  1=First Encounter(训练, AI)  3=Capture&Hold Infantry  4=Capture&Hold Tanks/Battle
  5=Assault Infantry  6=Assault Battle  7=C&H Pilot(飞行员士兵 tile)  8=C&H Recon(侦察兵士兵 tile)
  10=War  11=Quick Play(按阵营)  12=Quick Play(按士兵)  13=自选图  14=Quick Play(全模式)

字段语义：
  min_players    成局所需最少玩家数（达到即撮合）
  max_players    单局玩家上限（超出部分留在池中等下一局）
  ai_fill_after  排队等待超时（秒）：超时后不足的人数由 AI 补位开局；
                 0 = 不启用超时补位（训练模式 min=1 立即成局；战争模式
                 纯真人，凑够 min_players 才开局）

环境变量覆盖（便于测试压低等待时间，不改变代码默认值）：
  MM_AI_FILL_AFTER       覆盖所有模式的 ai_fill_after（秒）
  MM_MAX_COMBAT_SERVERS  覆盖 MAX_CONCURRENT_COMBAT_SERVERS
  MM_MIN_PLAYERS         覆盖所有模式的 min_players
  MM_SIMULATE_COMBAT     =1 时用内嵌模拟器代替 hng.exe（macOS/Linux 等无
                          Windows 客户端环境跑集成测试用；实例登记/端口
                          占用/并发上限/释放逻辑全部照常真实运转）
  MM_SNOW_CHANCE         随机下雪概率 0.0-1.0（默认 0.3；0=关，见 SNOW_CHANCE）
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


def _float_env(name, default=None):
    v = os.environ.get(name)
    if v is None or v == '':
        return default
    try:
        return float(v)
    except ValueError:
        print(f'[mm_config] WARNING: {name}={v!r} not a float, '
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
# fallback_presets：组内无人勾选地图时的随机兜底池，★ 2026-09-01 完全对齐
# 官方运营版地图池（官方 1.10/1.14 更新日志 + wiki 交叉核对，preset f5 官方命名）：
#   1  First Encounter(训练)：Samrée Depot(9) / Colmar Hamlet(10)
#   3  C&H Infantry(=官方 Skirmish)：Sawmill / Hill 60 / Village Skirmish
#   4  C&H Tank(官方 1.14 模式)：仅 Lunéville(12)
#   5/6 staged Assault：官方 Assault Small 五图 Airfield(26)/Factory(27)/
#      Forward Airfield(28)/Mountain Town(29)/Town(31)
#   10 War：官方完整版 Assault 五图 Airfield(5)/Factory(6)/Forward Airfield(7)/
#      Mountain(8)/Town(1345335)
#   11 Quick Play：随机小图（同 staged Assault）
# 官方未上线内容一并移除：Russian Encounter/Skirmish/Factory（东线内部开发）、
# Vasyugan Fortification、City 三图、Gorge、Canal Bridge、Mountain Railroad。
# ★ Airfield(5/26) 2026-08-27 曾因误判缺场景被剔除，2026-09-01 复核
#   T02M04_AirField.fst 实际存在（_packed 与 combat-server\_packed 均有，
#   文件名大写 F 与表内 Airfield 仅大小写差异，Windows 加载不受影响）已加回。
# ★ 仍缺场景的 preset（客户端 _packed\Environments\Scenes\
#   Gamemaps\ 无对应 .fst，进图必失败）：
#   4 (T04M01_RussianFactory) /
#   400513682 (T03M01_CityRiver) / 595333531 (T03M03_CityBoulevard) /
#   775928046 (T03M02_CityTrainStation)
GAMEMODES = {
    1:  _mode('training',         1,  1,  0,  [9, 10]),               # 官方 First Encounter：Depot / Mill
    3:  _mode('capture_hold_inf', 2, 18, 30,  [373394539, 373394540,
                                               373394541]),          # 官方 Skirmish 三图
    4:  _mode('capture_hold_all', 2, 18, 30,  [12]),                  # 官方 C&H Tank：仅 Lunéville
    7:  _mode('capture_hold_pilot', 2, 18, 30, [35]),                 # 官方 C&H Pilot：Lunéville（飞行员士兵专用 tile）
    8:  _mode('capture_hold_recon', 2, 18, 30, [36]),                 # 官方 C&H Recon：Lunéville（侦察兵士兵专用 tile）
    5:  _mode('assault_infantry', 2, 18, 30,  [26, 27, 28, 29, 31]),   # 官方 staged Assault Small
    6:  _mode('assault_battle',   2, 18, 30,  [26, 27, 28, 29, 31]),   # 官方 staged Assault Small
    10: _mode('war',              2, 18, 0,   [5, 6, 7, 8, 1345335]),  # 战争模式纯真人：官方完整版 Assault 图
    11: _mode('quickplay',        2, 18, 30,  [27, 28, 29, 31]),      # quickplay 无选图 → 随机小图
}
DEFAULT_GAMEMODE = _mode('default', 2, 18, 30)    # 未知模式兜底


def gamemode_cfg(gamemode_id):
    """按 gamemodeId 取配置（未知模式回退 DEFAULT_GAMEMODE）。"""
    return GAMEMODES.get(int(gamemode_id), DEFAULT_GAMEMODE)


# 等待期中途加入窗口（秒）：战局成局后该窗口内，新排队玩家可直接加入
# 同模式、选图兼容、未满员的现有战局（仍在开局等待/倒计时阶段），而不是
# 另开新局。窗口从成局时刻起算（含专用服加载地图 + 客户端加载 + 开局倒计时
# 的并行时间）。0 = 禁用中途加入（每人/每组固定撮合开新局）。
# MM_JOIN_WINDOW 环境变量覆盖。
JOIN_WINDOW = _int_env('MM_JOIN_WINDOW', 90)
if JOIN_WINDOW < 0:
    print(f'[mm_config] WARNING: MM_JOIN_WINDOW={JOIN_WINDOW} < 0, '
          f'using 0 (disabled)')
    JOIN_WINDOW = 0

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

# 随机下雪概率（0.0-1.0）：成局时按此概率抽签，抽中则战斗服命令行加
# /R seasonal.forceSnowEvent=1 强制开启引擎季节雪事件（渲染层功能，不走
# 游戏协议；选项表见 docs/hng-combat-protocol.md §B.13 ZOption_Seasonal）。
# 0=永不下雪，1=每局都下。MM_SNOW_CHANCE 环境变量覆盖，非法值回退 0.3。
SNOW_CHANCE = _float_env('MM_SNOW_CHANCE', 0.3)
if SNOW_CHANCE is None or not 0.0 <= SNOW_CHANCE <= 1.0:
    print(f'[mm_config] WARNING: MM_SNOW_CHANCE={os.environ.get("MM_SNOW_CHANCE")!r} '
          f'not in [0,1], using 0.3')
    SNOW_CHANCE = 0.3

# ===== RTS 战争域（war_campaign.py / docs/hng-rts-protocol.md Phase 3）=====
# 数值默认取官服近似（CommandNodeMoraleTemplate 实值 / wiki 考据），
# 全部可被环境变量覆盖，留实测校准空间。
WAR_AUTO_CREATE = bool(_int_env('MM_WAR_AUTO_CREATE', 1))     # 无进行中战争时自动开战
WAR_POINTS_VICTORY = _int_env('MM_WAR_POINTS_VICTORY', 9)     # 胜利所需首都数（官服 12~15）
WAR_TIMESCALE = _float_env('MM_WAR_TIMESCALE', 1.0)            # 战略时间倍率（AT 移动速度乘数）
WAR_DAYCYCLE_TIMESCALE = _float_env('MM_WAR_DAYCYCLE_TIMESCALE', 1.0)
WAR_MAX_PLAYERS_PER_FACTION = _int_env('MM_WAR_MAX_PLAYERS_PER_FACTION', 999999)
WAR_NAME = os.environ.get('MM_WAR_NAME') or 'The Great War'    # 新战争名
WAR_SEQUEL_AUTO = bool(_int_env('MM_WAR_SEQUEL_AUTO', 1))      # 战争结束自动开续战
WAR_CATALOGUE_LIMIT = _int_env('MM_WAR_CATALOGUE_LIMIT', 20)   # 目录返回战争数上限
# AT 生命周期（Phase 5）：单玩家 AT 持有上限 / 解散返还比例（官服近似，可校准）
WAR_AT_MAX = _int_env('MM_WAR_AT_MAX', 10)
WAR_DISSOLVE_REFUND = _float_env('MM_WAR_DISSOLVE_REFUND', 0.5)
# 购买/部署 AT 需拥有指挥点角色（官服 gate = commandPointsUnlocked > 0：
# 将军默认 3 点；普通角色军衔 12+ 起每级 +1；客户端无 isGeneral 检查。
# 0=放开便于测试）
WAR_AT_REQUIRE_COMMAND_POINTS = bool(
    _int_env('MM_WAR_AT_REQUIRE_COMMAND_POINTS', 1))
# AT 移动（Phase 6）：段时长 = 城市坐标距离 / (模板speed × WAR_TIMESCALE ×
# WAR_AT_MOVE_SCALE) 秒。1.0=官服真实时间节奏（步兵跨半图 ~4 小时）；
# 2026-09-03 应玩家要求提速 10 倍（步兵邻城 ~19 秒、跨半图 ~25 分钟）。
WAR_AT_MOVE_SCALE = _float_env('MM_WAR_AT_MOVE_SCALE', 10.0)
# 地形减速（#2 官方玩法：中立区/海面行军速度大大降低）。跨海判据=段距离
# ≥ WAR_SEA_DISTANCE（2026-09-04 实测欧洲图 2661 条补给线：内陆最长 505、
# 551-860 共 5 条全为丹麦↔挪威跨斯卡格拉克海峡线，550 为干净分界）；
# 中立判据=段两端城市归属均非己方。系数为速度乘数（0.25=4 倍时长），
# 同时命中取更小值。仅作用于地面补给线段（air 段不减速）。
WAR_SEA_DISTANCE = _float_env('MM_WAR_SEA_DISTANCE', 550.0)
WAR_SEA_SPEED_FACTOR = _float_env('MM_WAR_SEA_SPEED_FACTOR', 0.25)
WAR_NEUTRAL_SPEED_FACTOR = _float_env('MM_WAR_NEUTRAL_SPEED_FACTOR', 0.5)
# underdog 加成（#1 官方玩法：人数较少阵营经验/战资加成；faction 行
# f6/f7 underdogBonus{Xp,Warfunds}Multiplier，客户端阵营选择窗
# "当前弱势阵营加成"组按 >1 显示）。倍率 = 1 + cap × (1 - n/n_max)，
# 人数口径=战争中各阵营 AT 持有玩家去重数。默认封顶 XP +25% / 战资 +15%
# （官服数值失传，可校准）。
WAR_UNDERDOG_XP_BONUS = _float_env('MM_WAR_UNDERDOG_XP_BONUS', 0.25)
WAR_UNDERDOG_WF_BONUS = _float_env('MM_WAR_UNDERDOG_WF_BONUS', 0.15)
# 战争推进 tick 间隔秒（移动换段/到达判定/会战成局桩；realtest_server 挂载）
WAR_TICK_SECONDS = _int_env('MM_WAR_TICK_SECONDS', 5)

# ---- M2 战斗接入（docs/hng-rts-protocol.md §8）----
# war 行 automission 三档（f9-f11，纯服务端参数，按秒取值）：war battle
# 成局后无人加入超过该时长 → 服务器按双方 AT 剩余资源自动结算（§8.4）。
WAR_AUTOMISSION_NORMAL = _int_env('MM_WAR_AUTOMISSION_NORMAL', 3600)
WAR_AUTOMISSION_SKIRMISH = _int_env('MM_WAR_AUTOMISSION_SKIRMISH', 1800)
WAR_AUTOMISSION_SUPPLY = _int_env('MM_WAR_AUTOMISSION_SUPPLY', 1800)
# automission 结算损耗系数（占各 AT 当前 resources 比例；胜方轻损/负方重损）
WAR_AUTOMISSION_WINNER_LOSS = _float_env('MM_WAR_AUTOMISSION_WINNER_LOSS', 0.25)
WAR_AUTOMISSION_LOSER_LOSS = _float_env('MM_WAR_AUTOMISSION_LOSER_LOSS', 0.6)
# FPS 结算士气变化（官服 moraleLostPerBattle=20 / moraleGainedPerWin=10，
# 默认取 build_war_proto 同源 morale config 的官方值）
WAR_MORALE_LOST_PER_BATTLE = _int_env('MM_WAR_MORALE_LOST', 20)
WAR_MORALE_GAINED_PER_WIN = _int_env('MM_WAR_MORALE_GAINED', 10)
# FPS 真人会战结算损耗系数（占参战 AT 当前 resources 比例；官服按局内
# 死亡逐兵扣减，逐兵数据不可得，退化为按胜负比例扣，可校准）
WAR_BATTLE_WINNER_LOSS = _float_env('MM_WAR_BATTLE_WINNER_LOSS', 0.15)
WAR_BATTLE_LOSER_LOSS = _float_env('MM_WAR_BATTLE_LOSER_LOSS', 0.6)
# 会战 warfunds（官服：AT 每参战一局发放 + 胜方加成；占点英雄收益已由
# PlayerBattleResult f3 结算路径入账）
WAR_WARFUNDS_PER_BATTLE = _int_env('MM_WAR_WARFUNDS_PER_BATTLE', 100)
WAR_WARFUNDS_WIN_BONUS = _int_env('MM_WAR_WARFUNDS_WIN_BONUS', 200)
# fow 战争迷雾网格参数（war f19-f21；发非 0 即激活客户端 FogOfWarLayer，
# 可见性判定在客户端本地。默认 0=全图可见；官服数值失传，启用后可校准）
WAR_FOW_GRID_SIZE = _int_env('MM_WAR_FOW_GRID_SIZE', 0)
WAR_FOW_GRID_ORIGIN_X = _int_env('MM_WAR_FOW_GRID_ORIGIN_X', 0)
WAR_FOW_GRID_ORIGIN_Y = _int_env('MM_WAR_FOW_GRID_ORIGIN_Y', 0)
# 空运（§9.4）：BFS 不可达且 AT 编制含 transport_plane（cat 10）→ 空运，
# 直达时长固定秒（官服耗时失传，可校准）
WAR_AIR_TRANSIT_SECONDS = _int_env('MM_WAR_AIR_TRANSIT_SECONDS', 600)

# ---- M3 战争经济（docs/hng-rts-protocol.md §9 / §3.12.1）----
# 阵营资源生产推进最小间隔秒（war_tick 内检查；产量节奏由模板 600s 决定，
# 此游标只限制 DB 写频率与行重推频率）
WAR_PRODUCTION_MIN_TICK = _int_env('MM_WAR_PRODUCTION_MIN_TICK', 60)
# 增援/部署受阵营库存约束时，库存可交付比例下限（0=有多少给多少，缺口入队）
WAR_SUPPLY_INSTANT_PARTIAL = bool(
    _int_env('MM_WAR_SUPPLY_INSTANT_PARTIAL', 1))
# 补给队（Reserve，官服机制 §10.2 #6）：0=关闭（一律即时/库存入队，旧
# 行为）；1=自动（AT 所在城市非己方领地 → 购买的兵员装上补给队从最近己方
# 城市沿补给线机动，到达与父 AT 汇合后入编）；2=一律生成补给队。
# 官服口径：客户端 AT_TEMPLATE_RESERVE(4) + parentCommandNodeInstanceId
# 汇合链（AssaultTeamModel.updateArmyResources OnTheWay 汇总）。
WAR_SUPPLY_COLUMN_MODE = _int_env('MM_WAR_SUPPLY_COLUMN_MODE', 1)
