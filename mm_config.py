# -*- coding: utf-8 -*-
"""匹配池配置（纯数据模块）。

gamemodeId 取值来源：客户端 GamemodeDescription.as 静态构造器 + GamemodesMediator.as
706-714 行把静态描述写回 view model（反编译实测，2026-09-10 校正 C&H 编号）：
  1=First Encounter(训练, AI)  2=Capture&Hold Infantry  3=Capture&Hold Tanks
  4=Capture&Hold Battle(全兵种)  5=Assault Infantry  6=Assault Battle
  7=C&H Pilot(飞行员士兵 tile)  8=C&H Recon(侦察兵士兵 tile)
  10=War  11=Quick Play(按阵营)  12=Quick Play(按士兵)  13=自选图  14=Quick Play(全模式)
（GamemodesView.as 视图初始值 3=C&H步兵/4=C&H坦克是 mediator 覆写前的占位默认，
真实 dispatch 以 GamemodeDescription 静态编号为准；编号与 missionpreset f12
missionCategoryId 一一相等——客户端 updateMaps 按 cat==GamemodeId 过滤地图。）

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
  MM_SNOW_CHANCE         随机下雪概率 0.0-1.0（默认 1=常驻雪；0=关，见 SNOW_CHANCE）
  MM_COMBAT_CORES        战斗服 CPU 核数覆盖（0=自动探测；容器/云切片
                          探测失真时钉死）
  MM_PACKTHREADS         server.packthreads 显式值（-1=按并发推导 W-1；
                          引擎默认 0=串行是多人局 SendPump 饱和根因）
  MM_AFFINITY_MASKS      战斗服亲核掩码表（逗号分隔十六进制；空=按
                          MAX_CONCURRENT_COMBAT_SERVERS 推导连续窗口）
  MM_COMBAT_PRIORITY     战斗服进程优先级（normal/above/high，默认
                          high；主模拟循环单线程需抢赢同核其它进程）
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
#   1  First Encounter(训练)：Samrée Depot(9) / Colmar Hamlet(10)（32/33 为同图别名 preset）
#   2  C&H Infantry(=官方 Skirmish)：Sawmill(373394539) / Hill 60(373394540) /
#      Village Skirmish(373394541)（旧 preset 1/19/20/21 = 同图/俄线图，不入池）
#   3  C&H Tanks：仅 Lunéville(12)
#   4  C&H Battle(全兵种)：22/23/24 与 2 号三图同 gamemap、25=RussianSkirmish
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
    1:  _mode('training',         1,  4,  0,  [9, 10]),               # 官方 First Encounter：Depot / Mill（PVE，≤4 人同局：小队整队进 + JOIN_WINDOW 内散人 late join 汇入，AI 补对面）
    2:  _mode('capture_hold_inf', 2, 18, 30,  [373394539, 373394540,
                                               373394541]),          # 官方 Skirmish 三图（C&H Infantry）
    3:  _mode('capture_hold_tank', 2, 18, 30, [12]),                  # 官方 C&H Tanks：仅 Lunéville
    4:  _mode('capture_hold_all', 2, 18, 30,  [22, 23, 24, 25]),      # 官方 C&H Battle（全兵种）四图
    7:  _mode('capture_hold_pilot', 2, 18, 30, [35]),                 # 官方 C&H Pilot：Lunéville（飞行员士兵专用 tile）
    8:  _mode('capture_hold_recon', 2, 18, 30, [36]),                 # 官方 C&H Recon：Lunéville（侦察兵士兵专用 tile）
    5:  _mode('assault_infantry', 2, 18, 30,  [26, 27, 28, 29, 31]),   # 官方 staged Assault Small
    6:  _mode('assault_battle',   2, 18, 30,  [26, 27, 28, 29, 31]),   # 官方 staged Assault Small
    10: _mode('war',              2, 18, 0,   [5, 6, 7, 8, 1345335]),  # 战争模式纯真人：官方完整版 Assault 图
    11: _mode('quickplay',        2, 18, 30,  [27, 28, 29, 31]),      # quickplay 无选图 → 随机小图
}
DEFAULT_GAMEMODE = _mode('default', 2, 18, 30)    # 未知模式兜底

# ★ 快速匹配（客户端 Quick Play：11 按阵营 / 12 按士兵 / 14 全模式，
#   GamemodesMediator.as 实测）：虚拟模式号，绝不能以虚拟号入池——
#   按模式分池会把它与所有具体模式池隔离（每个 quickplay 玩家孤立成
#   局，谁也汇不进谁；12/14 还会走 DEFAULT 兜底，成局落在训练 preset
#   10 Mill 图上）。服务端在入池前解析为具体模式（官服语义"扔进任何
#   战局"）：先跨模式搜可中途加入的存活局（QUICKPLAY_JOIN_CANDIDATES），
#   找不到再并入等待人数最多的具体模式池（QUICKPLAY_POOL_MODES），
#   全空则从通用模式随机。
QUICKPLAY_GAMEMODES = (11, 12, 14)
# 跨模式搜局候选：全部具体 staging 模式（含 C&H 步兵 2 与士兵 tile 7/8；
# 阵营/载具/满员过滤兜底）。训练(1)、战争(10)、会战(battle_id) 局不进。
QUICKPLAY_JOIN_CANDIDATES = (2, 3, 4, 5, 6, 7, 8)
# 入池解析候选：通用 staging 模式（7/8 为飞行员/侦察兵 tile 模式，
# 随机落进去对步兵玩家不合适，只允许被动并入等待池不主动随机）。
QUICKPLAY_POOL_MODES = (2, 3, 4, 5, 6)

# ★ 地图筛选（客户端高级筛选视图 GamemodesView.as:458 "To Battle" 按钮恒
#   dispatch 13；GamemodesMediator.gamemodeClicked 13 分支把勾选地图装进
#   selectedMissionPresets）：同 quickplay 属虚拟模式号，入池前必须按
#   勾选 preset 的 missionCategoryId（missionpreset f12）解析为具体模式
#   （matchmaking._resolve_mapfilter_gamemode）。13 不入 GAMEMODES——
#   兜底走 DEFAULT_GAMEMODE（解析失败的极端场景）。
MAPFILTER_GAMEMODE = 13

# ★ 官方兵种准入矩阵（客户端 GamemodesMediator.as:572-626 的
#   UNAVAILABLE_REASON_WRONG_CHARACTER 表镜像）：各模式允许进入的士兵
#   资源类别（wardb.character_resource_category：1=步兵 2=飞行员
#   4=伞兵 5=坦克兵 6=侦察兵）。服务器在直排/quickplay/mapfilter/
#   指定战局/late join/小队入池六个入口统一执法；类别 0（未知模板）
#   不拦。省略的模式 = 不限兵种（6 Assault 全兵种、10 war、11/12/13/14
#   虚拟号）。
MODE_CHAR_CATS = {
    1: (1,),          # 训练：仅步兵
    2: (1,),          # C&H Infantry：仅步兵
    3: (5,),          # C&H Tanks 坦克掌控：仅坦克兵
    4: (1, 5, 6),     # C&H Battle：步兵/坦克兵/侦察兵
    5: (1, 4, 6),     # Assault Small：步兵/伞兵/侦察兵
    7: (2,),          # C&H Pilot：仅飞行员
    8: (6,),          # C&H Recon 侦查掌控：仅侦察兵
}


def mode_char_cats(gamemode_id):
    """按 gamemodeId 取允许的士兵类别（None=不限）。"""
    return MODE_CHAR_CATS.get(int(gamemode_id or 0))


def gamemode_cfg(gamemode_id):
    """按 gamemodeId 取配置（未知模式回退 DEFAULT_GAMEMODE）。"""
    return GAMEMODES.get(int(gamemode_id), DEFAULT_GAMEMODE)


# 等待期中途加入窗口（秒）：战局成局后该窗口内，新排队玩家可直接加入
# 同模式、选图兼容、未满员的现有战局（仍在开局等待/倒计时阶段），而不是
# 另开新局。窗口从成局时刻起算（含专用服加载地图 + 客户端加载 + 开局倒计时
# 的并行时间）。0 = 禁用中途加入（每人/每组固定撮合开新局）。
# MM_JOIN_WINDOW 环境变量覆盖。
JOIN_WINDOW = _int_env('MM_JOIN_WINDOW', 900)
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

# ---- 战斗服 CPU 资源分配（2026-09-09，以 B=MAX_CONCURRENT_COMBAT_SERVERS
# 为主控推导每实例亲核掩码窗口与 server.packthreads）----
# "人数越多客户端帧数越低"的服务端半边根因：专用服每 tick 快照全部实体 ×
# 每客户端打包下发（O(实例数×客户端数)），而 server.packthreads 引擎默认
# 0=串行（groups_true.txt 实锤），多人局 SendPump 饱和 → 全员 tick 落后 →
# 客户端 tickrate 自适应被拖慢（docs/hng-combat-protocol.md §6.4/§6.8.4）；
# 另主模拟循环单线程需独占一核，多实例无隔离会互相挤核丢 60Hz。
# 推导（combat_agent.plan_combat_resources，warserver 本机 spawn 与远端
# agent 两侧同构；agent 机读自己环境的同名变量）：
#   W_i = 核数 // B（前 核数%B 个窗口各 +1 核），P_i = W_i - 1
#   掩码 = 窗口核段位掩码，实例按 seq 轮转取槽
# env 覆盖：MM_COMBAT_CORES（核数）、MM_PACKTHREADS（统一打包线程数，
# -1=推导）、MM_AFFINITY_MASKS（显式掩码表，P 取 popcount(mask)-1）。
COMBAT_CORES = _int_env('MM_COMBAT_CORES', 0)      # 0 = 自动探测
COMBAT_PACKTHREADS = _int_env('MM_PACKTHREADS', -1)   # -1 = 推导 W-1
COMBAT_AFFINITY_MASKS = os.environ.get('MM_AFFINITY_MASKS', '')

# 玩家全未选图时的兜底 preset（WarDatabase 实测可用：
# 10 = Training - Encounter - Mill）
FALLBACK_PRESET = 10

# 模拟 combat server（macOS 等无 hng.exe 环境的集成测试）：不 spawn 进程，
# 由 warserver 内嵌模拟——bind UDP 端口 + 直接完成 BattleFieldSettings 握手
SIMULATE_COMBAT = bool(_int_env('MM_SIMULATE_COMBAT'))

# 随机下雪概率（0.0-1.0）：成局时按此概率抽签，抽中则战斗服握手
# BattleFieldSettings f22 高位 OR 雪掩码 0x30（低 2 位天气列原样；补丁版
# player.dll 于 ZGameMode_CreateInstance 拆开 → 全员同雪，
# snow-unified-patch.md §3 方案一）。
# 0=永不下雪，1=每局都下。默认 1=常驻雪（雪补丁验证期口径）；
# MM_SNOW_CHANCE 环境变量覆盖，非数字回退默认、越界回退 0.3。
# 注意：web 管理端 /config 保存过天气配置后，server_config 'weather'
# 行的 snow_chance 优先于此（matchmaking._weather_details 读库）。
SNOW_CHANCE = _float_env('MM_SNOW_CHANCE', 1)
if SNOW_CHANCE is None or not 0.0 <= SNOW_CHANCE <= 1.0:
    print(f'[mm_config] WARNING: MM_SNOW_CHANCE={os.environ.get("MM_SNOW_CHANCE")!r} '
          f'not in [0,1], using 0.3')
    SNOW_CHANCE = 0.3

# 是否向客户端下发 EOS openid token（PlayerSettings f999/f1000）。
# 发了 → 客户端 EOS_Connect_Login 真实走通 → EAC 客户端会话激活 → 进战斗时
#   EAC 按官方 Epic 云端清单校验 player.dll 哈希（原版过、任何补丁版弹
#   "未知的文件版本" 并退出 10011，2026-09-05 实证）。白名单在官方产品
#   e47c7a3e… 云端后台，私服无权登记补丁哈希。
# 不发 → EOS 登录流不启动（2026-08-20 前的常态）→ EAC 不激活、不做文件
#   校验，客户端 dll 可自由打补丁；副作用仅为 requesterType=1（EAC 路径）
#   的 GetPlayerSettings 不再出现（服务端 EAC 本就禁用，无消费方）。
# MM_EOS_TOKEN=1 临时开启（如需回归官方 dll + 真 EAC 环境）。
EOS_TOKEN_ENABLED = bool(_int_env('MM_EOS_TOKEN', 0))

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
WAR_AT_MAX = _int_env('MM_WAR_AT_MAX', 100)
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
# 撤退惩罚（war1.md §撤退：交战状态强撤无经验无战争币，损 25% 编制 +
# 扣 20 士气；士气扣减与战斗失败同源 WAR_MORALE_LOST）
WAR_RETREAT_LOSS = _float_env('MM_WAR_RETREAT_LOSS', 0.25)
# 包围判定开关：1=交战撤退时检查四周补给线邻城（全部驻有敌军 AT =
# 被包围无法撤退，战败时全歼）；0=关闭（恒可撤，M1 旧行为）
WAR_ENCIRCLE_CHECK = _int_env('MM_WAR_ENCIRCLE_CHECK', 1)
# 会战准备期秒数（battle 行 activationTimeStamp = 成局 + 准备期；
# war1.md：野外遭遇 5 分钟 / 城攻 5 分 30 秒；automission 超时以此起算）
WAR_BATTLE_PREP_FIELD = _int_env('MM_WAR_BATTLE_PREP_FIELD', 300)
WAR_BATTLE_PREP_ASSAULT = _int_env('MM_WAR_BATTLE_PREP_ASSAULT', 330)
# war 野外会战地图池（war1.md §野外遭遇战：准备期兵力未固定不生图，
# 准备期结束按最终构成定图——双方纯步兵=encounter 池 / 任一方含侦察·
# 装甲=skirmish 池；绝不用城内 assault 图池，2026-09-16 实测 Berlin-a
# 夺控战错发 assault 图）。池内均为有场景的官方图（Russian 东线系
# 未上线缺场景不入池）：
#   encounter = First Encounter 两图（Samrée Depot 9 / Colmar Mill 10，
#               与 GAMEMODES[1] 同源；32/33 为同图别名不入池）
#   skirmish  = 官方 Skirmish 三图（Forest/King of the Hill/Village，
#               与 GAMEMODES[2] 同源；单点 13v13 ~ 三点 18v18）
WAR_ENCOUNTER_PRESETS = [9, 10]
WAR_SKIRMISH_PRESETS = [373394539, 373394540, 373394541]
# war 会战单方成局开关：0=官服口径（try_form_war 要求攻守双方各≥1 真人
# 才开局）；1=桶内≥1 人即成局（单方排队也立即拉起 FPS 局，对面留空，
# 后续真人可 late join 补入）——单人测试用，线上保持 0。
WAR_SOLO_FORM = bool(_int_env('MM_WAR_SOLO_FORM', 0))
# war 会战跳过准备期开关（单人测试）：1=入池即成局，不等
# started_at+准备期（跳过 battle_ready_in 集结倒计时）；0=到点才成局
# （war_tick 周期撮合，见 matchmaking.war_battle_formable），线上保持 0。
WAR_SKIP_PREP = bool(_int_env('MM_WAR_SKIP_PREP', 0))
# rejoin 掉线保护窗口（秒）：hng.exe 游戏连接断开（崩溃/退出）后，
# 玩家须在此窗口内重进战局，否则会话释放（PMS 清理 + 坑位回收）。
# 客户端同源倒计时：ActionGameStatusModel.
# DEFAULT_SECONDS_BEFORE_LOSING_PLAYER_MISSION_SESSION = 300（Admitted
# 分支 timeAdmitted+300s−serverTime，到 0 转 GAME_TIMED_OUT）
REJOIN_TIMEOUT = _int_env('MM_REJOIN_TIMEOUT', 300)
# ---- automission 阈值判定（war-gameplay-fix-plan §3/§4/§7/§8；war1.md
# 社区口径 144/216/288/5:00/5:30/6h，客户端无证据，全部可配）----
# 据点数 ≥ 此值判「三点图」（用 216 门槛），否则「单点图」（144）
WAR_AUTO_TP_CP = _int_env('MM_WAR_AUTO_TP_CP', 2)
WAR_AUTO_SP_MIN = _int_env('MM_WAR_AUTO_SP_MIN', 144)    # 单点图步兵门槛
WAR_AUTO_TP_MIN = _int_env('MM_WAR_AUTO_TP_MIN', 216)    # 三点图/城攻步兵门槛
WAR_AUTO_LOSS = _float_env('MM_WAR_AUTO_LOSS', 0.25)     # 阈值判定败方编制损耗
WAR_AUTO_ATTACK_TIMEOUT = _int_env('MM_WAR_AUTO_ATTACK_TIMEOUT', 330)  # 城攻判定
WAR_AUTO_PARADROP_TIMEOUT = _int_env('MM_WAR_AUTO_PARADROP_TIMEOUT', 330)  # 伞兵攻城
WAR_AUTO_PARA_MIN = _int_env('MM_WAR_AUTO_PARA_MIN', 288)   # 伞兵攻城判定
WAR_AUTO_TIE_HOURS = _float_env('MM_WAR_AUTO_TIE_HOURS', 6.0)  # tie 超时（小时）
# 开战判定宽限（秒）：准备期结束（activationTimeStamp）只是战斗开始
# （客户端切正向计时），系统判定再等此窗口（war1.md 野外口径"战斗开始
# 后4分30秒判定"=270s；城攻/伞兵同口径顺延，避免准备期一到即结算）
WAR_AUTO_BATTLE_GRACE = _int_env('MM_WAR_AUTO_BATTLE_GRACE', 270)
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
# WarDatabase 坐标缺失时的单段移动回退；仅用于损坏/不完整数据集。
WAR_SEGMENT_FALLBACK_SECONDS = _int_env(
    'MM_WAR_SEGMENT_FALLBACK_SECONDS', 300)
# AT 会战经验，官服公式为参战基础值 + 胜利加成。
WAR_AT_XP_PER_BATTLE = _int_env('MM_WAR_AT_XP_PER_BATTLE', 5)
WAR_AT_XP_WIN_BONUS = _int_env('MM_WAR_AT_XP_WIN_BONUS', 25)
# 专用服启动倒计时覆盖值；0 表示不覆盖客户端/引擎默认值。
COMBAT_START_COUNTDOWN_OVERRIDE = _int_env(
    'MM_COMBAT_START_COUNTDOWN_OVERRIDE', 10)
# 非战役地图通常没有 battlefield 行。0 会让客户端不解析成无关城市；
# 仅兼容特殊客户端时才显式设置一个真实 battlefield entity id。
MISSION_MAP_ENTITY_FALLBACK = _int_env(
    'MM_MISSION_MAP_ENTITY_FALLBACK', 0)

# ---- M3 战争经济（docs/hng-rts-protocol.md §9 / §3.12.1）----
# 阵营资源生产推进最小间隔秒（war_tick 内检查；产量节奏由模板 600s 决定，
# 此游标只限制 DB 写频率与行重推频率）
WAR_PRODUCTION_MIN_TICK = _int_env('MM_WAR_PRODUCTION_MIN_TICK', 60)
# war f22/f23 and the server-side rules must come from the same snapshot.
WAR_QUEUE_REINFORCEMENTS = bool(
    _int_env('MM_WAR_QUEUE_REINFORCEMENTS', 1))
WAR_FIRST_RESOURCE_FREE = bool(
    _int_env('MM_WAR_FIRST_RESOURCE_FREE', 1))
# 增援/部署受阵营库存约束时，库存可交付比例下限（0=有多少给多少，缺口入队）
WAR_SUPPLY_INSTANT_PARTIAL = bool(
    _int_env('MM_WAR_SUPPLY_INSTANT_PARTIAL', 1))
# 补给队（Reserve，官服机制 §10.2 #6）：0=关闭（一律即时/库存入队，旧
# 行为）；1=自动（AT 所在城市非己方补给网才出队）；2=一律生成补给队。
# 官服口径（默认 2）：客户端补编窗口缺口公式 templateCount - onTheWay -
# current（AssaultTeamReinforcementWindowMediator）——购买的增援一律
# OnTheWay 在途（AT_TEMPLATE_RESERVE(4) + parentCommandNodeInstanceId
# 汇合链），与 AT 是否在补给网无关；同城（首都）出生即直接汇合。
WAR_SUPPLY_COLUMN_MODE = _int_env('MM_WAR_SUPPLY_COLUMN_MODE', 2)
