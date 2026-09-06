"""匹配/战局运行时状态 registry（mm_state）。

集中持有撮合池/登记表/局内动态 id 发号器/日志注入与通用后台任务入口：
game_server、handlers、war_campaign 统一从这里 import，消除原先
"人人 import matchmaking 拿全局 dict" 的重依赖与双向 import 环。
从 matchmaking.py 拆出（2026-09-06 重构，纯移动行为不变）。
"""
import asyncio
import random

import mm_pool
from wardb import squad_template_name

# ---------------------------------------------------------------- 日志注入
# 由 realtest_server.py 启动时注入（避免重复建日志句柄）；未注入时静默 no-op
_LOG_FN = lambda msg: None


def configure_log(log_fn):
    """注入日志函数（realtest_server 启动时调用；拆分前为 matchmaking.configure）。"""
    global _LOG_FN
    _LOG_FN = log_fn


def _log(msg):
    _LOG_FN(f'[MM] {msg}')


def _spawn(coro, label=''):
    """后台任务统一入口：异常兜底记日志（裸 ensure_future 的异常只会在
    GC 时打 "exception was never retrieved"，且无人善后）。"""
    async def _wrapped():
        try:
            await coro
        except asyncio.CancelledError:
            pass
        except Exception as e:
            import traceback
            _log(f'[mm-task] {label or coro} CRASHED: {type(e).__name__}: {e}')
            _log(traceback.format_exc())
    try:
        return asyncio.ensure_future(_wrapped())
    except RuntimeError:
        coro.close()                 # 事件循环未运行（测试环境）：丢弃避免告警
        return None

# ---------------------------------------------------------------- 状态
# ===== 运行时 id / 库引用常量（他服回放值已清除：id 现生成，库值读 WarDatabase）=====
# 客户端设备描述串（PMS f19 metricsDeviceId）：非 id、无库表可读，本服固定值
METRICS_DEVICE_ID = 'Flash;HnG;WIN 32,0,0,170;mozilla/5.0 (windows; u; zh-cn) applewebkit/533'
# 本服战争 id：战争是服务器侧运行时实体（客户端无本地表可查，任意值行为一致），
# 每次服务器启动生成新 id = 开一场新战争；faction 表行 f5 war 与 PMS f15 warId 同源用它
_WAR_ID = random.getrandbits(63)
# mission_info f5 mapEntityId：必须是客户端 WarDatabase battlefield 表真实行 id
# （112741325 = 'Benoit Village'）。Mill 等遭遇战图在 battlefield 表无行时用它兜底，
# 客户端简报按它查战场名（battlefield_entity_for_gamemap 动态匹配的回退值）。
MISSION_MAP_ENTITY_ID = 112741325
# mission_info f10 hostingCenterId：服务器侧基础设施 id（WarDatabase 无对应表，
# 客户端仅存储不查表）→ 每次启动生成（原值为他服回放 id）
MISSION_HOSTING_CENTER_ID = random.getrandbits(63)

# 局内动态 id 发号：基值每次进程启动随机初始化，避免重启后 id 从头复用
# （客户端可能缓存旧局 id，重启后撞号会导致实体绑定错乱）。
_MM_MISSION_ID = [random.randint(100001, 999000)]    # missionId 自增（保持 6 位小整数风格）
_MM_PMM_ID = [random.randint(200001, 999000)]        # PlayerMatchMaking 行 id 自增
_MM_PMS_ID = [random.getrandbits(63)]  # PlayerMissionSession 行 id 自增
                                     # (=握手 id1 / hng.exe /P 参数, 快照实体按它绑定, 必须大整数)
_MM_SOLO_SMS_ID = [random.getrandbits(63)]           # 单人隐式小队 SquadMemberSession 行 id 自增
# 每局动态 id 自增（2026-08-20：PlayerSettings 回放字段动态化）：
#   metrics_session_id → PMS f18 / PlayerSettings f23（必须同值）
#   battle_session_id  → PMS f9  / PlayerSettings f25（同源）
#   squad_id           → PlayerSettings f16 / SquadResponse f1（ZServer squad 树键）
_MM_METRICS_SESSION_ID = [random.getrandbits(63)]    # metricsSessionId 自增
_MM_BATTLE_SESSION_ID = [random.getrandbits(63)]     # battle session id 自增
_MM_SQUAD_ID = [random.getrandbits(63)]              # squadId 自增
_MM_SQUAD_MEMBER_ROW = [random.getrandbits(63)]      # 凑队成员行 id 自增

# 局内小队分组登记：mission_id -> {squad_id: {
#   name, leader_char_id, faction, squad_db_id(真小队=DB id / 单人凑队=0),
#   members: [{player_id, char_id, gamertag, rank, slot}] }}
# _start_match 成局时写入；game_server SquadResponse 按此构建局内小队定义
# （队名/队长/每成员槽位）；release_match 清理。
_MM_MISSION_SQUADS = {}
# 单人匹配自动凑队人数上限 = 小队模板玩家槽数（wardb
# SquadPlayerSlotTemplate 表：Infantry/Paratrooper 4、Armor 3、
# Fighter/Recon 2；客户端 SquadModel MAX_CHARACTERS_PER_SQUAD=4）。
# 超过槽数的成员在战斗服里找不到槽（SquadResponse 按模板发全量槽），
# 故凑队/late join 补队都按 wardb.squad_template_slot_count 封顶。


def _next_match_id(seq):
    """局内动态 id 发号（[_MM_*] 序列共用：squad/metrics/battle session）。"""
    seq[0] += 1
    return seq[0]


def _auto_squad_numbered_name(mission_id, fac, tmpl):
    """散人自动凑队 / late-join 自建队的默认名编号：按本局 (阵营, 兵种模板)
    从 1 起数，如 "Infantry Squad 1"、"Infantry Squad 2"。

    引擎默认命名（AddSquad 空名分支）的计数器只按 squadTemplateId 全局
    累计、跨阵营连续（AI 队第二阵营从 6 起）；SquadResponse f4 名非空时
    引擎逐字使用，故编号须在登记时算好写入。真实小队（squad_db_id 非零，
    用 DB 队名）不参与编号、也不占号。
    """
    n = 1 + sum(
        1 for info in (_MM_MISSION_SQUADS.get(mission_id) or {}).values()
        if not info.get('squad_db_id')
        and int(info.get('faction', 0) or 0) == int(fac)
        and int(info.get('template', 1) or 1) == int(tmpl))
    return f'{squad_template_name(tmpl)} {n}'


_MM_MISSION_DETAILS = {}    # mission_id -> {preset, players, map_entity_id, ai_fill, gamemode}
                            #（旧 AI 局结构 {player_id, gamertag, faction} 兼容保留）
_MM_QUEUES = {}             # player_id -> {pmm_id, mission_id, gamemode, ctx}（取消匹配用）
_MATCH_PMM_IDS = {}         # player_id -> 排队中 PlayerMatchMaking 行 id（成局时 delete 用）
_SOLO_SQUADS = {}           # player_id -> {squad_row, member_row, mission_id, faction,
                            #              template, char_id, gamertag, char_name, rank}
                            # 单人隐式小队行（官方 solo_kvcs_dump2.txt：单人匹配也推
                            # SquadSession + SquadMemberSession，同一玩家会话复用行 id）
POOL = mm_pool.MatchPool()  # 匹配池（gamemode 分池 + 选图交集撮合，见 mm_pool.py）
_AI_FILL_TASKS = {}         # player_id -> 超时 AI 补位任务（人数不足时兜底开局）

PMS_TO_MISSION = {}         # pms_id -> mission_id（game_server 按启动参数 /M 反查）
PMS_TO_SESSION = {}         # pms_id -> {player_id, char_id, gamertag, faction,
                            #            metrics_uid, metrics_session_id,
                            #            battle_session_id, squad_id, quickplay}
                            # ★ game_server 用它把 PlayerSettings f12 characterlist
                            #   / f15 missionCharacterId 换成玩家账号库里的真实角色
                            #   （客户端 RequestReadCharacterStats 请求的就是这些
                            #    characters 表小 id，回放他服 6360... 大 id 完全对不上）。
                            #   metrics/squad/battle_session 每局动态生成，
                            #   PlayerSettings(回放补丁) 与 PMS/SquadResponse 同源取值。
