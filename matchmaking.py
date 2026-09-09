"""撮合核心（matchmaking）。

客户端匹配流程（HnG 协议，依据反编译源码）：
  1. 点击 To Battle → 发 JoinMatchMakingRequest → 服务器回 JoinMatchMakingResponse(ok)
  2. 服务器推 PlayerMatchMaking 表 'set'（新行）→ 客户端进入"匹配中"界面
     （MatchmakingModel.onMatchMakingChange ADD）
  3. 匹配成功（本模块模拟：延迟数秒）：推 PlayerMissionSession 'set'
     (connectionState=Admitted=2) → 客户端拉 GetMissionDetailsRequest(missionId)
     显示简报/部署界面；同时 PlayerMatchMaking 'delete'（客户端退出匹配中）
  4. CancelJoinMatchMakingRequest → CancelJoinMatchMakingResponse + 'delete' 匹配行

关键事实（来自反编译）：
- 表名 xmlTag：PlayerMatchMakingTable='PlayerMatchMaking'，
  PlayerMissionSessionTable='PlayerMissionSession'（表推送走 KeyValueChangeSet）
- 时间格式：客户端 TimeUtilities.convertZuluTimeToMilliseconds 解析 'YYYYMMDDHHmmssZ'
- delete 动作 id：8 字节小端 int64（客户端 Chunk.getAsInt64）
- PlayerMatchMaking DELETE 后客户端只做本地 FSM 重置（不发网络请求，无死循环）
- GetMissionDetailsResponse 的 repeated 空字段必须完全省略（hurlant 库 Buffer overflow 坑）

2026-09-06 拆分后本模块只保留撮合核心（成局分发 / AI 补位 / late join /
成局落地 / 小队路径 / rejoin / 任务资源池 / 消息构造器）；
战斗服生命周期 -> combat_instances，运行时 registry -> mm_state，
faction 行构建 -> mm_factions，天气 -> mm_weather，请求入口 -> mm_handler。
DB 访问统一走 state.STORE（不再本地遮蔽注入）。
"""
import asyncio
import random
import struct
import time
import uuid

from protocol import Chunk, pb_bool
from protocol import (
    pb_float, pb_int32, pb_int64, pb_message, pb_string,
)
from state import FLASH_SESSIONS, PUBLIC_HOST
import state
from wardb import (
    _capturepoint_ap_map, _load_accesspoint_templates,
    _load_capturepoint_templates, _load_objective_capturepoints,
    _load_preset, _missiontype_for_gamemap, _rank_from_combat_xp,
    assault_ap_decision, battlefield_entity_for_gamemap, battlefield_pos,
    commandnode_template, is_general_character, preset_scene,
    squad_template_for_character, squad_template_slot_count,
    vehicle_resource_category,
)
import mm_config
import mm_pool
import force_pool
import war_campaign      # #1 underdog 倍率（faction 行 f6/f7 下发）

from mm_state import (
    METRICS_DEVICE_ID, MISSION_HOSTING_CENTER_ID, MISSION_MAP_ENTITY_ID,
    POOL, PMS_TO_MISSION, PMS_TO_SESSION, _AI_FILL_TASKS, _MATCH_PMM_IDS,
    _MM_BATTLE_SESSION_ID, _MM_METRICS_SESSION_ID, _MM_MISSION_DETAILS,
    _MM_MISSION_ID, _MM_MISSION_SQUADS, _MM_PMM_ID, _MM_PMS_ID, _MM_QUEUES,
    _MM_SQUAD_ID, _MM_SQUAD_MEMBER_ROW, _SOLO_SQUADS,
    _WAR_ID, _auto_squad_numbered_name, _log, _next_match_id, _spawn,
)
import combat_hosts
import combat_instances
from combat_instances import (
    COMBAT_INSTANCES, PENDING_MATCHES, _ensure_combat_server,
    reserve_combat_instance,
)
from mm_factions import (
    ASSAULT_DEFENDER_LOGO_AP, FACTION_ROW_IDS, _msg_faction_rows,
    _rival_faction,
)
from mm_weather import roll_weather


AI_MISSIONPRESET = 10                   # mission_info f3（AI 模式=10；多人模式=29）
AI_PLAYER_COUNT = 2                     # mission_info f9（他服单人AI局实测=2）

# f4 突击队构成：本局 force_pool 抽取的 AT 名单（双侧镜像，名单见
# force_pool.init_mission 日志）。模板 id 是【阵营相关】库值
# （commandnodetemplate f2 兵种名 × f3 阵营：infantry US=103/GE=203/SU=303），
# 按参战阵营现查 WarDatabase。无池回退官方锚点组合（三兵种 ×2）。
ASSAULT_TEAM_NODE_NAMES = ('infantry', 'motorized_infantry', 'motorized_guard')


def _assault_team_nodes(faction, mission_id=None):
    """阵营突击队 commandNode 模板 id 序列（本局 AT 抽取名单跨阵营翻译）。

    war 会战局（M2）攻守名单独立 → 按 faction 取（init_mission_war 的
    names_by_faction）；staged 局回退镜像名单。"""
    names = (force_pool.assault_team_names(mission_id, faction)
             or force_pool.assault_team_names(mission_id)
             or list(ASSAULT_TEAM_NODE_NAMES) * 2)
    faction = int(faction) if int(faction) in (1, 2, 3) else 3
    return [commandnode_template(faction, n) for n in names]


def _mission_accesspoints(gamemap_id):
    """本图出兵点模板（WarDatabase/accesspointtemplate f2==gamemap 分组，升序
    = A/B 点攻防序）。"""
    return _load_accesspoint_templates(int(gamemap_id))


def _mission_capturepoints(gamemap_id):
    """本图据点模板（WarDatabase/CapturepointTemplate f2==gamemap 分组）。
    初始归属 0=中立（他服实测 f4=0）。"""
    return _load_capturepoint_templates(int(gamemap_id))


# ---------------------------------------------------------------- 每局资源池（单一事实源）
# 2026-08-26：资源池改由 force_pool 模块生成 —— 按模式随机抽取突击队(AT)
# 编制（WarDatabase/commandnodetemplatearmyresourcetemplaterelation 原值，
# 乘员:载具天然保持官方 1:1~1.25:1，伞兵:运输机=4:1），池 = Σ AT 编制，
# 双侧镜像同池。此前手写的 MISSION_RESOURCE_CATS / AI_TEAM_RESOURCES 静态
# 数值为自造值（非官方），已废弃删除。
# f5（匹配界面）/ f14（握手）/ f2（轮询）三处单一事实源，出生消耗由
# ReportMissionStatusRequest f2 statuslist（§8.1 ReportArmyResourceSpawnCount
# {f1 ap, f2 cat, f5 count, f6 faction}，实测每 10s 增量上报，AI/玩家出生
# 均含）按兵种扣减。渲染类别序与 spawnInAir 统一走 force_pool.categories()。
# 部署界面按资源条目判定各兵种可用生命数 —— 缺空军条目时飞行员即使有
# 飞机也不显示乘机槽（只能徒步部署）。

def _init_mission_resources(mission_id, gamemode_id, attacker, defender,
                            require_cats=()):
    """成局时建立资源池（按模式抽 AT，攻防双方镜像同编制）。

    require_cats：本局玩家已装备载具资源类别并集——官服口径"进局必有
    装备载具类别之一"（坦克兵/飞行员对齐飞行员匹配语义，缺类别即无法
    部署）；池抽取后查缺补齐（force_pool._ensure_required_cats）。"""
    force_pool.init_mission(mission_id, gamemode_id, attacker, defender,
                            require_cats=require_cats)


async def _char_vehicle_cats(pid, char_id):
    """玩家角色已装备载具 → 资源类别并集（成局覆盖保证用）。

    vehicles 表 equipped_in_slot>0 = 已装备到角色；变体模板经父模板归一
    后查 vehicletemplate f4 资源类别。DB 异常/无装备返回 ()。"""
    if state.STORE is None or not pid or not char_id:
        return ()
    try:
        rows = await state.STORE.list_vehicles(int(pid), int(char_id))
    except Exception as e:
        _log(f'      [DB] list_vehicles(pid={pid}) failed: {e}')
        return ()
    cats = set()
    for v in rows or ():
        if int(v.get('equipped_in_slot', 0) or 0) <= 0:
            continue
        cat = vehicle_resource_category(
            int(v.get('vehicle_variant_template_id', 0) or 0))
        if cat:
            cats.add(int(cat))
    return tuple(sorted(cats))


async def _init_war_mission_resources(mission_id, battle_id, attacker,
                                      defender):
    """war 会战资源池（M2-3）：兵力 = 双方实际参战 AT 编制之和。

    AT 名单 = war_battles.factions_json（会战登记的双方 AT 实例 id）；
    编制 = AT template_id → commandnodetemplate 兵种名 → 编制表原值。
    任一侧无有效 AT（DB 异常/实例已解散）→ 整局回退镜像随机抽取，
    保证会战可玩（宁要可打的局，不要空池死局）。"""
    comps = {int(attacker): [], int(defender): []}
    names = {int(attacker): [], int(defender): []}
    ats = {int(attacker): [], int(defender): []}   # AT 实例（战报 f11.f3 归属）
    try:
        wb = await state.STORE.get_war_battle(int(battle_id))
        roster = (wb or {}).get('factions') or []
        nodes = {}
        at_ids = [int(f.get('at_id', 0) or 0) for f in roster]
        if at_ids:
            nodes = {int(n['id']): n
                     for n in await state.STORE.get_command_nodes_by_ids(at_ids)}
        for f in roster:
            fac = int(f.get('faction', 0) or 0)
            node = nodes.get(int(f.get('at_id', 0) or 0))
            if fac not in comps or node is None:
                continue
            tid = int(node.get('template_id', 0) or 0)
            comp = force_pool.composition_for_template(tid)
            if comp is None:
                continue
            comps[fac].append(comp)
            names[fac].append(force_pool.template_name(tid) or f'at{tid}')
            ats[fac].append({
                'id': int(node.get('id', 0) or 0),
                'name': str(node.get('name') or ''),
                'owner_player_id': int(node.get('owner_player_id', 0) or 0),
                'template_id': tid,
            })
    except Exception as ex:
        _log(f'[war] battle {battle_id} AT roster load failed: {ex}')
    if not comps[int(attacker)] or not comps[int(defender)]:
        _log(f'[war] battle {battle_id} AT roster incomplete '
             f'(atk={len(comps[int(attacker)])}, '
             f'dfd={len(comps[int(defender)])}) -> fallback mirror draw')
        _init_mission_resources(mission_id, 10, attacker, defender)
        return
    force_pool.init_mission_war(mission_id, attacker, defender, comps, names,
                                 ats)


def mission_resource_pool(mission_id):
    """读本局资源池；无池返回 None（调用方回退初始表）。"""
    return force_pool.pool(mission_id)


def _pool_counts(pool, faction):
    """池中某阵营各类剩余；池/阵营缺失回退锚点值（官方 Encounter 池）。"""
    if pool and int(faction) in pool:
        return pool[int(faction)]
    return force_pool.counts_for(None, faction)


def consume_mission_resource(mission_id, faction, cat, n=1, ap=None):
    """玩家出生消耗资源（按兵种类别扣 n；ap 给定时攻方同步扣分线池）。

    返回扣后余量；池/类别缺失返回 None。"""
    return force_pool.consume(mission_id, faction, cat, n, ap=ap)


def release_mission_resources(mission_id):
    """局结束清理（与 _MM_MISSION_DETAILS 同生命周期）。"""
    force_pool.release(mission_id)


def _msg_battle_info(battle_id, pos_x, pos_y):
    """HnG_States.BattleInfo proto（PMS Armed 后必须推送，否则客户端不连 game server）。

    私服抓包 frame 0xa636 字段：
      field 1 battleId (int64) = 5752758341220508668
      field 2 posX (f32)      = 9984.054
      field 3 posY (f32)      = 4387.6885
    """
    m = b''
    m += pb_int64(1, battle_id)         # battleId
    m += pb_float(2, pos_x)             # posX
    m += pb_float(3, pos_y)             # posY
    return m

def _msg_solo_squad_session(row_id, owner_pid, template, faction,
                            mission_id=0, action_games_entered=0):
    """单人隐式小队 SquadSession proto（官方 solo 抓包 S2C#4/#12/#16 同构）。

    官方值（solo_kvcs_dump2.txt）：f18 squadId=0（非真小队，UI 占位），
    f10 name=''，f8 totalCustomizationPoints=1；匹配成功后 f17 missionId=实际
    mission id、f19 actionGamesEntered=1。
    ★ f17/f19 必须更新：abandon 后 PMS DELETE → StatusMediator.onLobbySignalUpdates
      读 squadSession.missionId（null 时 TypeError #1009 卡 processing；非零则
      LOCKED_IN_COMBAT 卡战斗中状态）——missionId=0 → isOwnedByPlayer → BASE_STATE。
    """
    m = b''
    m += pb_int64(1, row_id)                 # id（=squadSessionId）
    m += pb_int64(2, owner_pid)              # ownerPlayerId（自己）
    m += pb_int64(3, template)               # characterTemplateId
    m += pb_int64(4, faction)                # factionTemplateId
    m += pb_int64(5, 0)                      # supportSlotTemplateId1
    m += pb_int64(6, 0)                      # supportSlotTemplateId2
    m += pb_int64(7, 0)                      # supportSlotTemplateId3
    m += pb_int32(8, 1)                      # totalCustomizationPoints（官方=1）
    m += pb_int32(9, 0)                      # usedCustomizationPoints
    m += pb_string(10, '')                   # name（官方单人=空）
    m += pb_bool(12, False)                  # matchMakeWarMissionFlag
    m += pb_int64(17, mission_id)            # missionId（匹配成功=战局 id，其余 0）
    m += pb_int64(18, 0)                     # squadId（★0=隐式小队，不可定制）
    m += pb_int32(19, action_games_entered)  # actionGamesEntered
    m += pb_bool(20, False)                  # isLockedForMembers
    m += pb_bool(21, False)                  # isLockedForCustomization
    m += pb_bool(22, False)                  # forceReady
    return m


async def _msg_solo_squad_member_session(row_id, squad_row_id, pid, gamertag,
                                   char_id, char_name, rank, status,
                                   player_session_id, faction):
    """单人隐式小队 SquadMemberSession proto（官方 solo 抓包同构）。

    status：3=Ready（匹配中，官方 S2C#4）、4=InBattle（进战局，S2C#12）、
            2=InLobby（回大厅）。f13 照官方推 (1,2)。
    f8/f19：角色装备的迷彩/头盔模板 id（小队头像按此渲染外观；
    0 = 未装备 → 默认军服）。
    """
    uvt, upt = 0, 0
    try:
        uvt, upt = await state.STORE.get_character_appearance(int(pid), int(char_id))
    except Exception as e:
        _log(f'      [DB] solo squad appearance lookup failed: {e}')
    m = b''
    m += pb_int64(1, row_id)                 # id
    m += pb_int64(2, squad_row_id)           # squadSessionId
    m += pb_int64(3, 1)                      # squadPlayerSlotTemplateId（官方=1）
    m += pb_int64(4, pid)                    # playerId
    m += pb_string(5, gamertag)              # gamerTag
    m += pb_int64(6, 0)                      # playerFactionTemplateId（官方=0）
    m += pb_int64(7, char_id)                # characterId
    m += pb_int64(8, uvt)                    # uniformVariantTemplateId
    m += pb_int64(9, 0)                      # weaponVariantTemplateId
    m += pb_int32(10, rank)                  # characterRank
    m += pb_int64(11, 0)                     # shopMembershipItemId
    m += pb_int32(12, status)                # squadMemberSessionStatus
    m += pb_int64(13, 1)                     # verifiedSquadResourceTemplates
    m += pb_int64(13, 2)
    m += pb_string(14, char_name)            # characterName
    m += pb_int64(15, player_session_id)     # playerSessionId（官方=PMS f18）
    m += pb_int32(16, 0)                     # playerTier
    m += pb_int32(17, 0)                     # squadCustomizationPoints
    m += pb_bool(18, False)                  # wasInvited
    m += pb_int64(19, upt)                   # uniformPartTemplateId
    m += pb_int64(20, faction)               # characterFactionTemplateId
    return m


def _msg_auto_squad_session(sq_id, info, mission_id, entered=1):
    """散人凑成小队 SquadSession proto（字段结构与真实小队同构）。

    f18 squadId=0（局内动态凑队，非 DB 真小队，不可定制）；f17 missionId
    = 战局 id（StatusMediator 状态机按非零进 LOCKED_IN_COMBAT）。
    """
    members = info.get('members') or []
    leader_pid = int(members[0]['player_id']) if members else 0
    m = b''
    m += pb_int64(1, sq_id)                  # id（=squadSessionId）
    m += pb_int64(2, leader_pid)             # ownerPlayerId（slot 0 = 队长）
    m += pb_int64(3, int(info.get('template', 1) or 1))  # characterTemplateId
    m += pb_int64(4, int(info.get('faction', 0) or 0))  # factionTemplateId
    m += pb_int64(5, 0)                      # supportSlotTemplateId1
    m += pb_int64(6, 0)                      # supportSlotTemplateId2
    m += pb_int64(7, 0)                      # supportSlotTemplateId3
    m += pb_int32(8, 0)                      # totalCustomizationPoints
    m += pb_int32(9, 0)                      # usedCustomizationPoints
    m += pb_string(10, str(info.get('name') or ''))     # name
    m += pb_bool(12, False)                  # matchMakeWarMissionFlag
    m += pb_int64(17, int(mission_id or 0))  # missionId
    m += pb_int64(18, 0)                     # squadId（凑队非真小队）
    m += pb_int32(19, int(entered or 1))     # actionGamesEntered
    m += pb_bool(20, False)                  # isLockedForMembers
    m += pb_bool(21, False)                  # isLockedForCustomization
    m += pb_bool(22, False)                  # forceReady
    return m


async def _msg_auto_squad_member(mem, sq_id, faction, status=4):
    """散人凑成小队 SquadMemberSession proto（f3=slot+1 与 solo 行同规律）。"""
    uvt, upt = 0, 0
    try:
        uvt, upt = await state.STORE.get_character_appearance(
            int(mem['player_id']), int(mem['char_id']))
    except Exception:
        pass
    m = b''
    m += pb_int64(1, int(mem['row_id']))     # id（成员行 id）
    m += pb_int64(2, int(sq_id))             # squadSessionId
    # f3 槽模板 id = slot+1（solo 行官方 slot0→f3=1 同规律；客户端删行
    # handleClearingSlot 按 f3 反查 SquadPlayerSlotTemplate.slotNumber-1
    # 清槽，0 会 null 解引用）
    m += pb_int64(3, int(mem.get('slot', 0) or 0) + 1)  # squadPlayerSlotTemplateId
    m += pb_int64(4, int(mem['player_id']))  # playerId
    m += pb_string(5, str(mem['gamertag']))  # gamerTag
    m += pb_int64(6, 0)                      # playerFactionTemplateId
    m += pb_int64(7, int(mem['char_id']))    # characterId
    m += pb_int64(8, uvt)                    # uniformVariantTemplateId
    m += pb_int64(9, 0)                      # weaponVariantTemplateId
    m += pb_int32(10, int(mem.get('rank', 0) or 0))     # characterRank
    m += pb_int64(11, 0)                     # shopMembershipItemId
    m += pb_int32(12, int(status))           # squadMemberSessionStatus
    m += pb_int64(13, 1)                     # verifiedSquadResourceTemplates
    m += pb_int64(13, 2)
    m += pb_string(14, str(mem['gamertag'])) # characterName
    m += pb_int64(15, 0)                     # playerSessionId
    m += pb_int32(16, 0)                     # playerTier
    m += pb_int32(17, 0)                     # squadCustomizationPoints
    m += pb_bool(18, False)                  # wasInvited
    m += pb_int64(19, upt)                   # uniformPartTemplateId
    m += pb_int64(20, int(faction))          # characterFactionTemplateId
    return m


async def _push_auto_squad_rows(w, vc, mission_id, sq_id, info, entered=1):
    """凑队小队全量 KVCS 行推给单条连接（SquadSession + 全体成员行）。

    客户端 SquadModel.handleSquadMemberSessionAdd：新增含本人 playerId 的
    成员行（非邀请态）即切换当前小队 → 大厅小队面板从 solo 行切到合并队。
    先推 SquadSession 行再推成员行（成员行 ADD 触发 getFromId 切换）。
    """
    await _push_kvcs(w, vc, b'SquadSession',
                     _msg_auto_squad_session(sq_id, info, mission_id, entered))
    faction = int(info.get('faction', 0) or 0)
    for mem in info.get('members') or []:
        await _push_kvcs(w, vc, b'SquadMemberSession',
                         await _msg_auto_squad_member(mem, sq_id, faction))
    _log(f'S->C cmd={vc:<4} KeyValueChangeSet (auto squad id={sq_id} + '
         f'{len(info.get("members") or [])} members, mission={mission_id})')


async def _reset_auto_squad_rows(w, vc, auto_sq_id, auto_sq_rows):
    """战斗结束/放弃：删除凑队小队 KVCS 行（整队成员行 + SquadSession）。

    逐行删除（成员行先删——客户端 handleClearingSlot 按 _currentSquadMember
    -Session 清面板）；随后调用方重推 solo 行（ADD）恢复单人小队视图。
    """
    try:
        for rid in auto_sq_rows or []:
            await _push_kvcs_delete(w, vc, b'SquadMemberSession', int(rid))
        if auto_sq_id:
            await _push_kvcs_delete(w, vc, b'SquadSession', int(auto_sq_id))
        _log(f'S->C cmd={vc:<4} KeyValueChangeSet delete (auto squad '
             f'id={auto_sq_id} + {len(auto_sq_rows or [])} member rows)')
    except Exception as e:
        _log(f'      [WARN] auto squad reset push failed: {e}')

def _zulu_now():
    """客户端 TimeUtilities 解析的 UTC 时间字符串 YYYYMMDDHHmmssZ。"""
    return time.strftime('%Y%m%d%H%M%S', time.gmtime()) + 'Z'


def _zulu_activated(offset_sec=3600):
    """MissionInfo.activationTimeStamp 专用：取过去时间。

    客户端 MissionUtilities.checkIfBattleIsActivated 用
    serverTime >= activationTimeStamp 判定战局已激活；发当前时间会因
    客户端 serverTime 同步延迟判为未激活 → 大厅地图详情页显示
    "战斗准备完毕于xxx"（战争模式组队倒计时文案）。本服战局即时开打，
    activation 一律回溯 1 小时。"""
    return time.strftime('%Y%m%d%H%M%S',
                         time.gmtime(time.time() - offset_sec)) + 'Z'


# ---------------------------------------------------------------- 推送
async def _push_kvcs(writer, view_cmd, table_name, proto_data):
    """KeyValueChangeSet 'set' 动作（表行 ADD/UPDATE）。"""
    data_chunk = Chunk(proto_data)
    table_chunk = Chunk(table_name, [data_chunk])
    action_chunk = Chunk(b'set', [table_chunk])
    title_chunk = Chunk(b'KeyValueChangeSet', [action_chunk])
    main = Chunk(struct.pack('<i', view_cmd), [title_chunk])
    writer.write(main.write(b''))
    await writer.drain()


async def _push_kvcs_delete(writer, view_cmd, table_name, row_id):
    """KeyValueChangeSet 'delete' 动作（删除表行，row_id 为 8 字节小端 int64）。"""
    id_chunk = Chunk(struct.pack('<q', int(row_id)))
    table_chunk = Chunk(table_name, [id_chunk])
    action_chunk = Chunk(b'delete', [table_chunk])
    title_chunk = Chunk(b'KeyValueChangeSet', [action_chunk])
    main = Chunk(struct.pack('<i', view_cmd), [title_chunk])
    writer.write(main.write(b''))
    await writer.drain()


# ---------------------------------------------------------------- proto 构造
def _mm_live_stats():
    """实时战局统计（PMM f20-f23，排队 UI「战斗中/组局中/进行中」数字）：
    in_game = 活跃会话数（PMS_TO_SESSION 中仍属运行实例的行）；
    populating = 并发上限排队局 + 已拉起未握手完成的实例；
    running = 握手完成（ready）的实例数。"""
    in_game = sum(1 for pms_id in PMS_TO_SESSION
                  if PMS_TO_MISSION.get(pms_id) in COMBAT_INSTANCES)
    populating = len(PENDING_MATCHES) + sum(
        1 for i in COMBAT_INSTANCES.values() if not i['ready'].is_set())
    running = sum(1 for i in COMBAT_INSTANCES.values() if i['ready'].is_set())
    return in_game, populating, running


def _msg_player_matchmaking(pmm_id, char_id, faction_id, mission_id, zulu_time,
                            queue_local=1, queue_total=1):
    """HnG_States.PlayerMatchMaking proto（匹配中状态行）。
    仅 'set' 推送；repeated 空字段完全省略（hurlant Buffer overflow 坑）。
    f18/f19 队列人数 / f20-f23 战局统计 = 实时统计（原硬编码 1/0）。"""
    in_game, populating, running = _mm_live_stats()
    m = b''
    m += pb_int64(1, pmm_id)            # id
    m += pb_int64(2, 0)                 # squadSessionId
    m += pb_string(4, zulu_time)        # timeEntered
    m += pb_string(5, '')               # metricsUserId
    m += pb_int64(6, 0)                 # metricsSessionId
    m += pb_string(7, '')               # metricsDeviceId
    m += pb_int64(8, mission_id or 0)   # missionId（0 = 随机匹配）
    m += pb_bool(10, True)              # isMatchmaking
    m += pb_int64(11, 1)                # warId
    m += pb_int64(12, faction_id)       # factionId
    m += pb_int32(13, 0)                # missionTypeMask
    m += pb_bool(14, False)             # forceStart
    m += pb_int32(15, 0)                # tier
    m += pb_float(16, 0.0)              # skillLevel
    m += pb_bool(17, False)             # playerVsBots
    m += pb_int32(18, queue_local)      # playersInQueueLocal（池内同模式人数）
    m += pb_int32(19, queue_total)      # playersInQueueTotal（池内总人数）
    m += pb_int32(20, in_game)          # playersInGameLocal（战斗中人数）
    m += pb_int32(21, in_game)          # playersInGameTotal（单服 = 同上）
    m += pb_int32(22, populating)       # missionsMatchingPopulating（组局中）
    m += pb_int32(23, running)          # missionsMatchingRunning（进行中）
    m += pb_int64(25, 0)                # missionCategoryId
    return m


def _msg_player_mission_session(pms_id, player_id, gamertag, char_id, faction_id,
                                char_name, rank, mission_id, zulu_time, game_connect,
                                metrics_uid='', metrics_session_id=0,
                                metrics_device_id='', platform='PC', connection_state=2,
                                battle_session_id=None):
    """HnG_States.PlayerMissionSession proto（匹配成功会话行）。

    私服抓包 frame 57 字段（按重要性排序）：
      field 1  id                = 大数 PMS 实例 id
      field 2  playerId          = 大数 (与 PlayerSettings field 1 一致)
      field 3  playerGamerTag    = "DoubleBird"
      field 4  characterId       = 大数 (与 PlayerSettings field 15 一致)
      field 5  characterFactionId= 大数
      field 6  characterName     = "Artyom Mironov"
      field 7  characterRank     = 13
      field 8  missionId         = 大数 (game server GetMissionDetailsRequest field 1)
      field 9  missionSessionId? = 大数 (非零)
      field 13 timeAdmitted      = "20260817141437Z"
      field 14 connectionState   = 3 ★ (本地原 2=Admitted，私服 3=Armed)
      field 15 warId?            = 大数
      field 17 metricsUserId    = UUID (与 PlayerSettings field 22 一致)
      field 18 metricsSessionId = 大数
      field 19 metricsDeviceId  = "Flash;HnG;WIN 32,0,0,170;mozilla/5.0..."
      field 20 gameConnect      = "ip:port"
      field 21 platform         = "HnG"

    connectionState 语义（HnG_States.PlayerMissionConnectionState，反编译确认）：
      1=Queued  2=Admitted  3=Entered
    ★ matchmaking 推送必须用 2 (Admitted)：ActionGameService.handleSessionUpdates
      仅在 Admitted 分支调 findPlayerMissionSessionAndLaunchGame() 拉起 hng.exe；
      推 3 (Entered) 走 "Fully entered action game" 分支 → 永不启动游戏进程。
      （GMD 请求由 KVCS ADD 事件 dispatch getBattleDetailsSignal 触发，与 state 值无关）
    game_server.py 的 hng.exe 连接侧推送用 3（触发 game server 连接上的 cmd=37）。
    """
    m = b''
    m += pb_int64(1, pms_id)                  # id（客户端 /P 启动参数）
    m += pb_int64(2, player_id)               # playerId
    m += pb_string(3, gamertag)               # playerGamerTag
    m += pb_int64(4, char_id)                 # career id（他服实测=JoinMM f1/PlayerSettings f15）
    m += pb_int64(5, FACTION_ROW_IDS.get(int(faction_id), int(faction_id)))
    # characterFactionId：必须是 KVS 'faction' 表行 id（FACTION_ROW_IDS，行 id
    # ==factiontemplateid=1/2/3）：客户端 setAccessPoints/getFromId 按它查 faction
    # 行（行表未推/值不存在 → null 解引用 #1009），setPlayersInMatch/
    # setBasicInformation 又拿它与 factiontemplateid/defenderFactionId 比较
    # （"我方"高亮与攻防角色判定）——填 player_id 等无关值两处都会失效
    m += pb_string(6, char_name)              # characterName
    m += pb_int32(7, rank)                    # characterRank
    m += pb_int64(8, mission_id)              # missionId（客户端 /M 启动参数）
    if battle_session_id is None:              # 调用方未传则现发号（与 PlayerSettings f25 同源）
        battle_session_id = _next_match_id(_MM_BATTLE_SESSION_ID)
    m += pb_int64(9, battle_session_id)        # missionSessionId（非零大数，=PlayerSettings f25）
    m += pb_int64(11, 0)                      # missionAssaultTeamId
    m += pb_int32(12, 0)                      # missionAssaultTeamSlot
    m += pb_string(13, zulu_time)             # timeAdmitted
    m += pb_int32(14, connection_state)       # connectionState (2=Admitted, 3=Armed)
    m += pb_int64(15, _WAR_ID)                # warId（本服运行时战争 id，与 faction 表行 f5 同源）
    m += pb_int32(16, 0)                      # characterSpawned
    m += pb_string(17, metrics_uid)           # metricsUserId (与 PlayerSettings field 22 一致)
    m += pb_int64(18, metrics_session_id)      # metricsSessionId
    m += pb_string(19, metrics_device_id)      # metricsDeviceId
    m += pb_string(20, game_connect)          # gameConnect（游戏进程连接地址）
    m += pb_string(21, platform)              # platform (私服="HnG")
    return m


def _msg_mission_info(mission_id, player_id, zulu_time, defender_faction):
    """HnG_States.MissionInfo proto（AI 模式，他服私服 2026-08-18 抓包实测）。

    defender_faction = AI 对手阵营（_rival_faction(玩家阵营)）：f8 恒 2 的
    旧写法在 GE 玩家 AI 局会与实际 AI 方（SU）错位 → 简报攻防显示反。"""
    m = b''
    m += pb_int64(1, mission_id)               # missionId
    m += pb_int64(2, 0)                        # battleId
    m += pb_int64(3, AI_MISSIONPRESET)         # missionpreset（AI=10, 多人=29）
    m += pb_int64(5, MISSION_MAP_ENTITY_ID)    # mapEntityId（WarDatabase battlefield 表 'Benoit Village' 行 id）
    m += pb_int64(6, 1)                            # mapEntityTypeId（1=battlefield）
    m += pb_int32(7, 1)                            # isAutogenerated
    m += pb_int64(8, int(defender_faction))        # defenderFactionId（AI 局防守方）
    m += pb_int32(9, AI_PLAYER_COUNT)              # playerCount（他服单人AI局实测=2）
    m += pb_int64(10, MISSION_HOSTING_CENTER_ID)  # hostingCenterId（服务器侧基础设施 id，本服固定）
    m += pb_string(11, zulu_time)              # activationTimeStamp
    m += pb_int32(12, 1)                       # weatherTimeOfDay（他服单人AI局实测=1）
    m += pb_int32(13, 0)                       # weatherCondition
    return m


def _msg_mission_details_response(mission_id, player_id, gamertag, zulu_time,
                                  player_faction=3, gamemap_id=13):
    """GetMissionDetailsResponse proto（AI 模式，字段映射按客户端 SWC 描述符）。

    客户端 schema（proto_all/HnG_States）：
      f1 response(enum)=0 | f2 MissionInfo
      f3 MissionDetailsFaction ×2      {1: factionId, 2: players[{1: gamertag}]}
      f4 MissionDetailsAssaultTeam ×12 {1: factionId, 2: commandNodeTemplateId,
                                        3: accessPointTemplateId, 4: ownerPlayerId}
      f5 MissionDetailsArmyResource ×20{1: factionId, 2: armyResourceCategoryId,
                                        3: accessPointTemplateId, 4: count, 5: spawnInAir}
      f6 MissionDetailsAccessPoint ×2  {1: accessPointTemplateId, 2: factionTemplateId}
      f7 capturePointStatuses | f8 posX f32 | f9 posY f32
    ★ f3/f4/f5 的 factionId 是 KVS 'faction' 表的行 id（FACTION_ROW_IDS，
      行 id==factiontemplateid=1/2/3），客户端 FactionTable.getFromId(行id)
      解引用，行 id 不在已推送行表内即 TypeError #1009。
    出兵点/据点模板按 gamemap 查 WarDatabase（缺省 Mill=13）；突击队模板按
    参战阵营查 commandnodetemplate。
    """
    ai_faction = _rival_faction(player_faction)
    rfid = FACTION_ROW_IDS[player_faction]   # 我方 faction 行 id
    afid = FACTION_ROW_IDS[ai_faction]       # AI 方 faction 行 id
    aps = _mission_accesspoints(gamemap_id)   # 不截断（5 线图全参与）
    ours_ap, theirs_ap = aps[0], aps[1]
    # ★按 missiontype 分支（assault-map-spawn-investigation.md §9.2）：
    # Assault 出兵线随机激活（wardb.assault_ap_decision，按 mission_id 播种，
    # 与战斗侧 _accesspoint_layout/BattleFieldSettings f12 同源同值）：
    # 守方 = O 锚 1234567890（引擎虚拟 AP：战斗侧 f12 收编 + f14 挂资源
    # → 守方小队/坦克部署中心 O 区）、我方（进攻方）随机 2~3 条边线、
    # ★未激活线不开启。Encounter（Mill 等无连线图）维持原有交替方案不改动。
    assault = _missiontype_for_gamemap(gamemap_id) == 1
    owner_map = None
    d_ap = None
    atk_aps = ()
    if assault:
        d_ap, a_aps, _inactive = assault_ap_decision(gamemap_id, mission_id)
        theirs_ap = d_ap   # = O 锚 1234567890（f5 守方资源锚，同战斗 f14）
        ours_ap = min(a_aps) if a_aps else aps[0]
        atk_aps = set(int(t) for t in a_aps)
        owner_map = {int(d_ap): ai_faction}   # O 锚不在边线 aps → f6 不下发
        for t in a_aps:
            owner_map[int(t)] = player_faction
    m = b''
    m += pb_int32(1, 0)  # 他服私服实测 = 0（非 ResponseCodes.ok=1）
    m += pb_message(2, _msg_mission_info(mission_id, player_id, zulu_time,
                                         ai_faction))
    # f3 参战方：我方 {faction行id, 成员=[gamertag]}；AI 方无成员名单
    m += pb_message(3, pb_int64(1, rfid) +
                    pb_message(2, pb_string(1, gamertag)))
    m += pb_message(3, pb_int64(1, afid))
    # f4 突击队：本局 AT 抽取名单跨阵营翻译（无池回退官方锚点 ×2）；
    # ★守方锚 ASSAULT_DEFENDER_LOGO_AP（Mill A 点，全库无据点挂靠）——
    # 客户端 setupMissionAssaultTeamOnMap 次选路径把阵营 logo 画在
    # ★地图中心（O 点区域，官方简报守方 logo 位置），而非外围守方线
    # （BattlefieldUtilities.as L99-105；f4 只驱动大厅 logo 定位，
    # 战斗侧 home_ap 走 combat.py f5 锚 O 锚 1234567890，两者独立）
    for cmd_node in _assault_team_nodes(player_faction, mission_id):
        m += pb_message(4, pb_int64(1, rfid) + pb_int64(2, cmd_node) +
                        pb_int64(3, ours_ap) + pb_int64(4, 0))
    defender_logo_ap = (ASSAULT_DEFENDER_LOGO_AP
                        if assault else theirs_ap)
    for cmd_node in _assault_team_nodes(ai_faction, mission_id):
        m += pb_message(4, pb_int64(1, afid) + pb_int64(2, cmd_node) +
                        pb_int64(3, defender_logo_ap) + pb_int64(4, 0))
    # f5 资源：从本局资源池渲染（与战斗内 f2/f14 同源同值，消耗后递减）；
    # 每方每类一条挂该方出兵点（客户端跨点求和 → 显示即池剩余量）
    pool = mission_resource_pool(mission_id)
    ours_counts = _pool_counts(pool, player_faction)
    theirs_counts = _pool_counts(pool, ai_faction)
    for res_cat, res_air in force_pool.categories(counts_by_faction=pool):
        m += pb_message(5, pb_int64(1, rfid) + pb_int64(2, res_cat) +
                        pb_int64(3, ours_ap) +
                        pb_int64(4, ours_counts.get(res_cat, 0)) +
                        pb_bool(5, res_air))
    for res_cat, res_air in force_pool.categories(counts_by_faction=pool):
        m += pb_message(5, pb_int64(1, afid) + pb_int64(2, res_cat) +
                        pb_int64(3, theirs_ap) +
                        pb_int64(4, theirs_counts.get(res_cat, 0)) +
                        pb_bool(5, res_air))
    # f6 出兵点归属：Encounter 首个=我方、其余交替（Mill 实测 8=我方, 15=AI）；
    # Assault 展示层只发★攻方激活线（官方简报守方仅拥有中心 O 点——由 f7
    # 涂守方色 + f4 logo 画中心；守方 O 锚为虚拟点不在边线 aps → 不进 f6）
    # ——与战斗侧 _accesspoint_layout 同 mission_id 同源（战斗 f12 含守方
    # O 锚：引擎判负检查/部署必需）
    if owner_map is not None:
        for ap in aps:
            if int(ap) not in owner_map:
                continue        # 未激活线：不开启不下发
            m += pb_message(6, pb_int64(1, ap) +
                            pb_int64(2, owner_map[int(ap)]))
    else:
        for i, ap in enumerate(aps):
            m += pb_message(6, pb_int64(1, ap) +
                            pb_int64(2, ai_faction if i % 2 else player_faction))
    # f7 据点初始归属：Encounter 全 0 中立（他服私服实测 {3:120, 4:0}）；
    # Assault 只发★O 点（守方）+ 攻方激活线入口据点（挂攻方 AP，攻方色）
    # ——客户端 auto 路径 getAutoGeneratedActiveLines 按 owner!=0 据点名
    # 首字母判激活线（BattlefieldStatusView.as L152-171/L310-327，需
    # MissionInfo f7 isAutogenerated=1 已恒发）：守方线/未激活线据点不发
    # → 图上不画该线；O 点（objectiveId 恒显示）涂守方色=官方观感
    obj_cps = set(_load_objective_capturepoints(gamemap_id)) if assault else set()
    if assault:
        cp_ap = _capturepoint_ap_map(gamemap_id)
        for cp in _mission_capturepoints(gamemap_id):
            if cp in obj_cps:
                cp_owner = ai_faction
            elif cp_ap.get(cp) in atk_aps:
                cp_owner = player_faction
            else:
                continue        # 守方线/未激活线/线中段：展示层不下发
            m += pb_message(7, pb_int32(3, cp) + pb_int32(4, cp_owner))
    else:
        for cp in _mission_capturepoints(gamemap_id):
            m += pb_message(7, pb_int32(3, cp) + pb_int32(4, 0))
    pos = battlefield_pos(MISSION_MAP_ENTITY_ID)
    m += pb_float(8, pos[0])   # posX（mapEntityId 对应 battlefield 行坐标）
    m += pb_float(9, pos[1])   # posY
    return m


# ---------------------------------------------------------------- 处理器
def _multi_first(f, fn, default=0):
    """pb_parse_multi 结果取首个标量值（缺失/空 → default）。"""
    vals = f.get(fn)
    if not vals:
        return default
    v = vals[0]
    return v if not isinstance(v, (bytes, bytearray)) else default


def _split_factions(entries):
    """阵营分组 → (attacker_faction, defender_faction, 单侧真人)。

    玩家保留各自角色 faction；参战两阵营仍按人数前两选取（同人数按
    阵营 id 升序稳定），★攻防角色成局时掷硬币随机（2026-09-01：原
    "人数最多组恒进攻"使同人数对局固定低 id 阵营进攻，改随机对半开）。
    不足两个阵营时防守方由 AI 补位（ai_fill 局 / 同阵营匹配常见路径，
    ★玩家攻防角色同样掷硬币——玩家可能当守方打 AI（守方出兵线/
    判负条件由 assault_ap_decision 引擎侧对称支持））。
    """
    groups = {}
    for e in entries:
        groups.setdefault(int(e.faction), []).append(e)
    if len(groups) >= 2:
        ordered = sorted(groups, key=lambda fc: (-len(groups[fc]), fc))
        attacker, defender = ordered[0], ordered[1]
        if random.random() < 0.5:
            attacker, defender = defender, attacker
        return attacker, defender, False
    player_faction = next(iter(groups), 3)
    ai_faction = _rival_faction(player_faction)
    if random.random() < 0.5:
        return ai_faction, player_faction, True
    return player_faction, ai_faction, True


def _prune_match_factions(match, attacker, defender, gamemode_id):
    """非战争模式同局最多两阵营（防御性裁剪，就地修改 match.players）。

    返回被裁掉的 entries（空列表 = 无裁剪/战争模式不裁）；被裁玩家由
    调用方（_start_match）回池开下一局。
    """
    if int(gamemode_id) == 10:
        return []
    atk, dfd = int(attacker), int(defender)
    dropped = [e for e in match.players if int(e.faction) not in (atk, dfd)]
    if dropped:
        match.players = [e for e in match.players
                         if int(e.faction) in (atk, dfd)]
    return dropped


def _game_connect_for(inst):
    """实例 → 客户端连接地址：所在主机 public_addr:实际端口。

    多机部署下远端实例用该机 public_addr（客户端经公网/内网直连该机）；
    单机回退 host.public_addr 本就是 PUBLIC_HOST。本机 spawn 重试可能换
    端口，须取 inst['port'] 实时值。inst=None/缺 host（测试桩）回退
    PUBLIC_HOST:port（与旧单机行为一致）。"""
    host = (inst or {}).get('host') or {}
    addr = host.get('public_addr') or PUBLIC_HOST
    return f'{addr}:{int((inst or {}).get("port", 0) or 0)}'


def _arm_pending_drain(hosts):
    """整局排队后的重派兜底：满员场景 release_match 会触发 drain，但
    「全部主机都在 agent 冷却」没有自然触发点——冷却到期后无人重派，
    排队局会滞留到下一次战局结束。此处安排到期重派（幂等，队列空
    则 no-op）。"""
    if not any(combat_hosts.in_cooldown(h) for h in hosts):
        return

    async def _later():
        await asyncio.sleep(combat_hosts.AGENT_COOLDOWN_SECONDS + 1)
        combat_instances._drain_pending_matches()

    _spawn(_later(), 'pending_drain_retry')


def _dispatch_match(match):
    """成局分发：直接交 _start_match 选机（多机容量/端口池/agent 冷却
    综合判定须读库热配置，无法同步预检）；全满则其内部排队
    PENDING_MATCHES FIFO，坑位释放（release_match）后重派。"""
    _spawn(_start_match(match),
           f'start_match#{getattr(match, "mission_id", 0)}')


async def _try_form_all():
    """撮合并分发当前池内所有可成的局（入池/出池后调用）。"""
    while True:
        match = POOL.try_form()
        if match is None:
            break
        _dispatch_match(match)
    # war 会战撮合（M2）：battle 桶内攻守双方各≥1 真人即成局
    while True:
        wmatch = POOL.try_form_war()
        if wmatch is None:
            break
        _dispatch_match(wmatch)
    await _push_queue_counts()


async def _push_queue_counts():
    """向所有排队中玩家刷新 PlayerMatchMaking f18-f23（队列人数 + 战局统计，best-effort）。"""
    for pid, q in list(_MM_QUEUES.items()):
        if q.get('mission_id'):
            continue                     # 已成局，PMM 行已 delete
        ctx = q.get('ctx') or {}
        w, vc = ctx.get('writer'), ctx.get('view_cmd')
        if w is None or vc is None:
            continue
        try:
            await _push_kvcs(
                w, vc, b'PlayerMatchMaking',
                _msg_player_matchmaking(
                    q['pmm_id'], ctx['char_id'], ctx['faction'],
                    ctx.get('req_mission_id', 0), _zulu_now(),
                    POOL.waiting_count(q.get('gamemode', 0)),
                    POOL.waiting_count()))
        except Exception:
            pass                         # 连接已断：留给 cancel/abandon 清理


def _arm_ai_fill(pid, gamemode_id):
    """（重新）启动玩家的超时 AI 补位任务；ai_fill_after<=0 不启用。"""
    old = _AI_FILL_TASKS.pop(pid, None)
    if old is not None:
        old.cancel()
    cfg = mm_config.gamemode_cfg(gamemode_id)
    if cfg['ai_fill_after'] <= 0:
        return

    async def _fill():
        try:
            await asyncio.sleep(cfg['ai_fill_after'])
        except asyncio.CancelledError:
            return
        q = _MM_QUEUES.get(pid)
        if q is None or q.get('mission_id'):
            return                        # 已成局 / 已取消
        entry = POOL.get(pid)
        if entry is None or entry.gamemode_id != gamemode_id:
            return
        match = POOL.force_form(cfg, gamemode_id)
        if match is not None:
            _log(f'ai fill: gamemode={gamemode_id} timeout, forcing match '
                 f'(players={len(match.players)})')
            _dispatch_match(match)
            await _push_queue_counts()

    _AI_FILL_TASKS[pid] = _spawn(_fill(), f'ai_fill#{pid}')


def _cleanup_queued_player(pid):
    """出池玩家善后：取消 AI 补位任务、清 _MM_QUEUES。"""
    t = _AI_FILL_TASKS.pop(pid, None)
    if t is not None:
        t.cancel()
    _MM_QUEUES.pop(pid, None)


def drop_player(pid):
    """玩家断线清理：排队中 → 出池（防幽灵条目虚增人数 / 被撮合成局）。

    realtest_server.handle_tcp finally 调用（连接断开）。
    已成局（mission_id != 0）的玩家不处理：战局照常进行（等同中途退赛，
    由 combat/mission 域的 earnings/Abandon 流程收尾）。同步函数、幂等。
    """
    if pid is None:
        return
    q = _MM_QUEUES.get(pid)
    if q is None:
        return                          # 未在排队（含已成局/从未排队）
    if q.get('mission_id'):
        return                          # 已成局，交由战局生命周期管理
    gamemode = q.get('gamemode', 0)
    squad_db_id = int((q.get('ctx') or {}).get('squad_db_id') or 0)
    if squad_db_id:
        # 整队排队中掉线：整队出池回 InLobby（拆队不可部署）
        _spawn(_drop_squad_queue(squad_db_id, int(pid)),
               f'drop_squad:{pid}')
        return
    _cleanup_queued_player(pid)
    _MATCH_PMM_IDS.pop(pid, None)
    POOL.remove(pid)
    _log(f'mm: player {pid} dropped (disconnect, gamemode={gamemode})')
    _spawn(_try_form_all(), f'try_form_all(drop:{pid})')


# ---------------------------------------------------------------- 整队匹配
# 真实小队（CreateSquad 建立，DB squads 表）整队入池：
# 任何成员点匹配 → 全体在线 InLobby/Ready 成员作为原子单位入池
# （QueueEntry.group_key=squad_id，mm_pool 保证不拆队），成局同 mission 部署。
# 官方对应行为：队长激活小队（SquadSession f12 matchMakeWarMissionFlag）。

async def _char_is_general(player_id, character_id):
    """玩家某角色是否将军（小队成员行无 template 字段，按 character_id 反查）。"""
    try:
        for ch in await state.STORE.list_characters(int(player_id)):
            if int(ch['id']) == int(character_id):
                return is_general_character(ch.get('template'))
    except Exception as e:
        _log(f'      [DB] char template lookup failed: {e}')
    return False


async def _squad_eligible_members(pid):
    """(squad, 可参战成员列表)；非小队场景返回 (None, None)。

    玩家在 DB 真实小队即走小队路径（即使队里只有自己在线——推真实
    小队行而非 solo 隐式行，避免同一玩家两张 SquadSession 冲突）。
    可参战成员 = InLobby/Ready 且 Flash 会话在线（离线成员本局不部署）
    且当前角色非将军（官方设计将军不进 FPS 战斗）。
    自己当前 InBattle（如已在局中重复点匹配）→ 返回 (squad, []) 由
    调用方跳过排队。"""
    if state.STORE is None or pid is None:
        return None, None
    try:
        squad = await state.STORE.get_squad_for_player(int(pid))
        if squad is None:
            return None, None
        members = await state.STORE.list_squad_members(squad['id'])
    except Exception as e:
        _log(f'      [DB] squad lookup failed: {e}')
        return None, None
    eligible = []
    for m in members:
        if int(m['status']) not in (2, 3):
            continue
        s = FLASH_SESSIONS.get(m['gamertag'] or '')
        if s is None or s.playerview_cmd is None:
            continue
        # ★ 将军不参战：整队入池不经 _handle_join 单人校验，须在此过滤
        if await _char_is_general(int(m['player_id']), int(m['character_id'])):
            _log(f'      squad member {m["gamertag"]} skipped: '
                 f'char {m["character_id"]} is general (cannot enter combat)')
            continue
        eligible.append(m)
    if not any(int(m['player_id']) == int(pid) for m in eligible):
        return squad, []         # 自己不在可参战列表（InBattle/离线）
    return squad, eligible


async def _revive_own_membership(squad_db_id, pid):
    """status 卡 InBattle 复活（官服语义：重连即回小队 InLobby）。

    返回 True 表示已把自己成员行置回 InLobby。仅当玩家无存活战局
    （_find_player_active_mission 为空）时调用——战局收尾的
    reset_real_squad_to_lobby 未覆盖到的掉线者（战斗中崩溃 + 局已结束）
    status 残留 InBattle，_squad_eligible_members 永远过滤掉他，
    join 静默 no-op（2026-08-27 日志：9 次连点无任何入池/推送）。"""
    if state.STORE is None or not squad_db_id:
        return False
    try:
        members = await state.STORE.list_squad_members(int(squad_db_id))
    except Exception as e:
        _log(f'      [DB] _revive_own_membership lookup failed: {e}')
        return False
    for m in members:
        if int(m['player_id']) == int(pid) and int(m['status']) not in (2, 3):
            try:
                await state.STORE.update_squad_member(int(m['member_id']), status=2)
                return True
            except Exception as e:
                _log(f'      [DB] _revive_own_membership update failed: {e}')
                return False
    return False


async def _enqueue_squad(squad, members, gamemode_id, staged_presets,
                         req_mission_id):
    """全队入池：每名在线成员建排队上下文 + PMM 行；DB 置 Ready +
    小队 matchmaking_flag；整队 KVCS 行推给全体（跨会话）。"""
    if not members:
        return                      # 无可参战成员（如自己已 InBattle）
    squad_id = int(squad['id'])
    entries = []
    for m in members:
        mpid = int(m['player_id'])
        flash = FLASH_SESSIONS[m['gamertag']]
        w, vc = flash.writer, flash.playerview_cmd
        # 此前单独排队残留清理（行 id 即将更换）
        old_q = _MM_QUEUES.get(mpid)
        if old_q is not None and not old_q.get('mission_id'):
            _cleanup_queued_player(mpid)
            POOL.remove(mpid)
            if old_q.get('pmm_id') is not None:
                await _push_kvcs_delete(w, vc, b'PlayerMatchMaking',
                                        old_q['pmm_id'])
        # faction 行 f6/f7 带当前 underdog 倍率（#1 人数较少阵营加成）
        _ud = await war_campaign.current_underdog_mults(state.STORE)
        for row in _msg_faction_rows(_ud):
            await _push_kvcs(w, vc, b'faction', row)
        pmm_id = _next_match_id(_MM_PMM_ID)
        rank = _rank_from_combat_xp(int(m['combat_xp']))
        ctx = {'writer': w, 'view_cmd': vc, 'gamertag': m['gamertag'],
               'char_name': m['char_name'], 'rank': rank,
               'faction': int(m['faction']),
               'char_id': int(m['character_id']),
               'char_template': int(m['template']),
               'quickplay': False,
               'req_mission_id': req_mission_id,
               'metrics_uid': str(uuid.uuid4()),
               'metrics_session_id': _next_match_id(_MM_METRICS_SESSION_ID),
               'squad_db_id': squad_id}
        _MM_QUEUES[mpid] = {'pmm_id': pmm_id, 'mission_id': 0,
                            'gamemode': gamemode_id, 'ctx': ctx}
        _MATCH_PMM_IDS[mpid] = pmm_id
        await _push_kvcs(w, vc, b'PlayerMatchMaking',
                         _msg_player_matchmaking(
                             pmm_id, int(m['character_id']),
                             int(m['faction']), req_mission_id, _zulu_now(),
                             POOL.waiting_count(gamemode_id) + 1,
                             POOL.waiting_count() + 1))
        entries.append(mm_pool.QueueEntry(
            player_id=mpid, char_id=int(m['character_id']),
            faction=int(m['faction']), gamemode_id=gamemode_id,
            presets=staged_presets, group_key=squad_id,
            char_cat=character_resource_category(int(m['template'])),
            vehicle_cats=await _char_vehicle_cats(
                mpid, int(m['character_id']))))
    try:
        for m in members:
            await state.STORE.update_squad_member(m['id'], status=3)
        await state.STORE.set_squad_flag(squad_id, True)
    except Exception as e:
        _log(f'      [DB] squad enqueue status updates failed: {e}')
    from handlers.squad import push_squad_rows_to_all_members
    await push_squad_rows_to_all_members(squad_id)
    _log(f'squad enqueue: squad={squad_id}, players='
         f'{[e.player_id for e in entries]}, gamemode={gamemode_id}')
    for e in entries:
        POOL.enqueue(e)
    await _try_form_all()
    for e in entries:
        if _MM_QUEUES.get(e.player_id, {}).get('mission_id', 0) == 0:
            _arm_ai_fill(e.player_id, gamemode_id)


async def reset_real_squad_to_lobby(squad_db_id, exclude_pids=()):
    """整队回 InLobby（DB 状态 + 向在线成员推整队行 mission=0）。

    战局结束（combat._finish_mission_sessions）/ 放弃（mission abandon）/
    整队取消 / 整队掉线共用。排除 exclude_pids 中的玩家推送（连接已断）。"""
    if state.STORE is None or not squad_db_id:
        return
    try:
        await state.STORE.set_squad_flag(int(squad_db_id), False)
        members = await state.STORE.list_squad_members(int(squad_db_id))
        for m in members:
            await state.STORE.update_squad_member(m['id'], status=2)
    except Exception as e:
        _log(f'      [DB] reset_real_squad_to_lobby failed: {e}')
        return
    from handlers.squad import _online_member_sessions, push_squad_rows_to_writer
    for s in _online_member_sessions(members):
        if int(s.player_id or 0) in exclude_pids:
            continue
        try:
            await push_squad_rows_to_writer(
                s.writer, s.playerview_cmd, int(squad_db_id), 0,
                viewer_pid=s.player_id, viewer_name=s.username)
        except Exception as e:
            _log(f'      squad reset push to {s.username!r} failed: {e}')


async def _cancel_squad_queue(squad_db_id, exclude_pids=()):
    """整队取消匹配：全队出池、删 PMM 行、回 InLobby（任何成员取消全队退出）。"""
    if state.STORE is None:
        return
    try:
        members = await state.STORE.list_squad_members(int(squad_db_id))
    except Exception as e:
        _log(f'      [DB] _cancel_squad_queue lookup failed: {e}')
        return
    for m in members:
        mpid = int(m['player_id'])
        q = _MM_QUEUES.get(mpid)
        if q is None or q.get('mission_id'):
            continue                     # 不在排队 / 已成局不动
        _cleanup_queued_player(mpid)
        POOL.remove(mpid)
        s = FLASH_SESSIONS.get(m['gamertag'] or '')
        if (s is not None and s.playerview_cmd is not None
                and q.get('pmm_id') is not None):
            try:
                await _push_kvcs_delete(s.writer, s.playerview_cmd,
                                        b'PlayerMatchMaking', q['pmm_id'])
            except Exception as e:
                _log(f'      PMM delete push failed (player={mpid}): {e}')
    await reset_real_squad_to_lobby(squad_db_id, exclude_pids)
    _log(f'squad cancel: squad={squad_db_id}, exclude={list(exclude_pids)}')


async def _drop_squad_queue(squad_db_id, gone_pid):
    """整队排队中成员掉线：全队出池、回 InLobby（不推掉线者连接）。"""
    await _cancel_squad_queue(squad_db_id, (int(gone_pid),))
    await _try_form_all()


async def _weather_details():
    """成局天气：优先读 DB 动态配置（web 管理端 /config 保存，即时生效）；
    无行/读库异常回落内置默认（snow 仍走 mm_config.SNOW_CHANCE）。"""
    try:
        cfg = await state.STORE.get_config('weather')
        if cfg:
            return roll_weather(cfg)
    except Exception as ex:
        _log(f'[weather] dynamic config read failed, fallback to defaults: {ex}')
    return roll_weather()


async def _start_match(match):
    """成局落地：发号、登记会话、拉起 combat server、逐玩家推送进局序列。

    每场战局一个独立 combat server 实例（动态 UDP 端口），多人共享：
    - mission_id 全局唯一（/M、GetMissionDetails、release_match 键）
    - 每名玩家独立 pms_id / metrics ids（game_server 按 pms 查会话）
    - game_connect = 127.0.0.1:{port}（客户端 hng.exe 连此 UDP 打战斗）

    war 会战局（match.battle_id>0，M2）：mission_id = mission_records 行 id
    （成局时已落库），地图/攻防阵营从 record 读取（不随机、不掷硬币）。
    """
    battle_id = int(getattr(match, 'battle_id', 0) or 0)
    war_rec = None
    if battle_id:
        try:
            war_rec = await state.STORE.get_mission_by_battle(battle_id)
        except Exception as ex:
            _log(f'[war] get_mission_by_battle({battle_id}) failed: {ex}')
        if war_rec is None:
            # record 已清（automission 结算过）：全员回池无意义（battle 已
            # 结束）——直接退排队玩家回大厅（清 PMM 行）。
            _log(f'[war] battle {battle_id} has no live mission record, '
                 f'dropping {len(match.players)} queued players')
            for e in match.players:
                pid = int(e.player_id)
                _cleanup_queued_player(pid)
                _MATCH_PMM_IDS.pop(pid, None)
            await _push_queue_counts()
            return
    if battle_id:
        mission_id = int(war_rec['id'])
    else:
        mission_id = _next_match_id(_MM_MISSION_ID)
    match.mission_id = mission_id
    if battle_id:
        preset = int(war_rec['preset'] or 0) or AI_MISSIONPRESET
    else:
        preset = int(match.preset_id)
    gamemode_id = int(match.players[0].gamemode_id) if match.players else 0
    if battle_id:
        # war 局攻防固定（record 成局时按 AT 阵营写入），不随机掷硬币
        attacker = int(war_rec['attacker_faction'] or 0)
        defender = int(war_rec['defender_faction'] or 0)
        one_sided = False
        if not (attacker in (1, 2, 3) and defender in (1, 2, 3)
                and attacker != defender):
            _log(f'ERROR: war battle {battle_id} record factions invalid '
                 f'({attacker}/{defender}), aborting match')
            for e in match.players:
                _cleanup_queued_player(int(e.player_id))
            return
    else:
        attacker, defender, one_sided = _split_factions(match.players)
    # ★ AI 策略（2026-09-01 按官方实现复刻 killfeed 阵营默认名）：官方两套
    #   bot 系统按模式互斥（战斗服 player.dll 逆向实锤）：
    #   - 训练模式（gamemode 1）f20 enableBots=True：战斗服本地 bot
    #     （CreateSoldierEntity a6≠0 → SpawnBot，身份名=avatar 模板名；
    #     官方训练 preset 9/10/11 f7=0 本就无 SA bot）
    #   - 多人模式（3/4/5/6/11 及未知）f31 enableSaBots=True：SA bot
    #     （CreateSoldierEntity a6=0 → GetFactionDefaultCharName，killfeed
    #     显示 Soldat/Red Army Man/Recruit——官方多人局行为；数量取战斗服
    #     本地 missionpreset f7 aiSaBotCountPerFaction，多人兜底 preset 实测
    #     均 >0）；f20 同关，避免两套 bot 混编出两种名字
    #   - 战争模式（gamemode 10）纯真人：两开关全关（凑够真人才开局）
    enable_bots = (int(gamemode_id) == 1)
    enable_sa_bots = (int(gamemode_id) not in (1, 10))

    # ★ 防御（非战争模式同局最多两阵营）：combat server 只按攻防两阵营
    #   配出生侧，撮合若带出第三阵营（US/GE/SU 同池），第三阵营玩家进图
    #   无侧 → 判负+观战。超出两阵营的玩家回池开下一局（与端口耗尽
    #   回池同路径）。战争模式（gamemode 10）多方语义不裁剪。
    dropped = _prune_match_factions(match, attacker, defender, gamemode_id)
    if dropped:
        _log(f'match pruned to 2 factions: kept={len(match.players)} '
             f'(atk={attacker}, dfd={defender}), returned='
             f'{[(int(e.player_id), int(e.faction)) for e in dropped]}')
        for e in dropped:
            dpid = int(e.player_id)
            if _MM_QUEUES.get(dpid, {}).get('mission_id', 0) == 0:
                POOL.enqueue(e)
                _arm_ai_fill(dpid, e.gamemode_id)

    preset_row = _load_preset(preset)
    gamemap_id = int(preset_row.get(3, 0) or 0) or 13
    if battle_id:
        # war 局：简报落点 = 会战城市（record.map_entity_id，成局时写）
        map_entity_id = int(war_rec['map_entity_id'] or 0) \
            or battlefield_entity_for_gamemap(gamemap_id)
    else:
        map_entity_id = battlefield_entity_for_gamemap(gamemap_id)
    combat_map = preset_scene(preset)

    # ★ 选机 + 占坑（多机部署，2026-09-08）：热配置（server_config
    #   'combat_hosts'，管理后台保存即生效）按各机剩余容量/端口池/agent
    #   冷却选机，选机→登记间无 await，并发成局不会双占同机同端口。
    #   全满/全冷却 → 整局排队 PENDING_MATCHES（玩家 _MM_QUEUES 尚在，
    #   停留匹配界面），坑位释放后 _drain_pending_matches 重派——不再走
    #   旧「端口耗尽回池重撮」路径：整局排队保住已成的队伍，语义与容量
    #   排队（旧 _dispatch_match 预检）统一。
    hosts = await combat_hosts.load_hosts()
    host = combat_hosts.pick_host(hosts)
    if host is None:
        PENDING_MATCHES.append(match)
        _log(f'match queued (combat hosts busy: '
             f'{combat_hosts.describe_hosts(hosts)}, players='
             f'{len(match.players)}, pending={len(PENDING_MATCHES)})')
        _arm_pending_drain(hosts)
        return
    port = combat_hosts.alloc_port(host, COMBAT_INSTANCES)
    if port is None:
        # pick_host 已校验端口池，理论不可达（同机实例端口恰在选机→登记
        # 间隙被外部进程占用的竞态兜底）：按全满同路径排队
        PENDING_MATCHES.append(match)
        _log(f'ERROR: combat port pool exhausted on {host.get("name")}, '
             f'match queued (mission would be {mission_id}, '
             f'{len(match.players)} players, pending={len(PENDING_MATCHES)})')
        return
    inst = reserve_combat_instance(mission_id, host, port)

    # 每名玩家发号登记（发号规则与改造前单人路径一致：PMS f17/f18/f9 与
    # PlayerSettings f22/f23/f25 同源；solo squad 行 id 复用）
    # 整队成员复用 DB 小队 id（PMS f16 与小队行同源，战斗结束按它重置整队）
    sessions = []
    for e in match.players:
        pid = int(e.player_id)
        q = _MM_QUEUES.pop(pid, None)
        _cleanup_queued_player(pid)
        ctx = (q or {}).get('ctx') or {}
        pms_id = _next_match_id(_MM_PMS_ID)
        metrics_uid = ctx.get('metrics_uid') or str(uuid.uuid4())
        metrics_session_id = int(ctx.get('metrics_session_id', 0) or 0) or \
            _next_match_id(_MM_METRICS_SESSION_ID)
        battle_session_id = _next_match_id(_MM_BATTLE_SESSION_ID)
        squad_db_id = int(ctx.get('squad_db_id') or 0)
        squad_id = squad_db_id or _next_match_id(_MM_SQUAD_ID)
        gamertag = ctx.get('gamertag') or 'Player'
        sess_rec = {'pms_id': pms_id, 'player_id': pid,
                    'char_id': int(e.char_id), 'gamertag': gamertag,
                    'faction': int(e.faction),
                    'metrics_uid': metrics_uid,
                    'metrics_session_id': metrics_session_id,
                    'battle_session_id': battle_session_id,
                    'squad_id': squad_id,
                    'squad_db_id': squad_db_id,
                    'squad_slot': 0,
                    'mission_id': mission_id,
                    'quickplay': bool(ctx.get('quickplay', False))}
        PMS_TO_MISSION[pms_id] = mission_id
        PMS_TO_SESSION[pms_id] = sess_rec
        sessions.append({'entry': e, 'ctx': ctx, 'sess': sess_rec,
                         'pms_id': pms_id, 'squad_id': squad_id})

    # ★ 局内小队分组（覆盖上面每人一个 squad_id 的占位发号）：
    #   - 真实小队（ctx.squad_db_id 相同）共用 DB 小队 id，队名取库
    #     （大厅 SetSquadDetails 传入），队长取 DB leader_character_id
    #   - 单人匹配玩家按阵营+兵种自动凑队（每队上限 = 该兵种小队
    #     模板槽数，rank 最高者任队长、占 slot 0），队名用兵种对应官方小队模板名
    #     （Infantry/Fighter/Paratrooper/Armor/Recon Squad）+ 按本局
    #     (阵营, 兵种) 从 1 的编号（_auto_squad_numbered_name）
    # game_server SquadResponse 按登记表构建小队定义 → 同队成员进同一局内小队。
    def _register_mission_squad(sq_id, name, leader_char, fac, members,
                                db_id=0, template=1):
        members = sorted(members, key=lambda s: 0 if int(
            s['entry'].char_id) == int(leader_char) else 1)
        rows = []
        for slot, s in enumerate(members):
            s['sess']['squad_id'] = sq_id
            s['sess']['squad_slot'] = slot
            # 成员行 id：凑队小队 KVCS 行（SquadMemberSession.f1）——
            # 合并小队推送/战斗结束重置按 id 删行，须与 solo/真实小队
            # 成员行 id 不冲突（_MM_SQUAD_MEMBER_ROW 独立序列）
            rows.append({'player_id': int(s['entry'].player_id),
                         'char_id': int(s['entry'].char_id),
                         'gamertag': s['sess']['gamertag'],
                         'rank': int((s['ctx'] or {}).get('rank', 0) or 0),
                         'slot': slot,
                         'row_id': _next_match_id(_MM_SQUAD_MEMBER_ROW)})
        _MM_MISSION_SQUADS.setdefault(mission_id, {})[sq_id] = {
            'name': name, 'leader_char_id': int(leader_char),
            'faction': int(fac), 'squad_db_id': int(db_id),
            'template': int(template), 'members': rows}

    real_ids = sorted({s['sess']['squad_db_id'] for s in sessions
                       if s['sess']['squad_db_id']})
    for sid in real_ids:
        members = [s for s in sessions if s['sess']['squad_db_id'] == sid]
        squad_row = None
        try:
            squad_row = await state.STORE.get_squad(sid)
        except Exception as ex:
            _log(f'      [DB] get_squad for grouping failed ({sid}): {ex}')
        name = (squad_row['name'] if squad_row else None) or 'PlayerSquad'
        member_chars = {int(s['entry'].char_id) for s in members}
        leader_char = int(squad_row['leader_character_id']) \
            if squad_row else 0
        if leader_char not in member_chars:
            leader_char = int(members[0]['entry'].char_id)
        _register_mission_squad(sid, name, leader_char,
                                int(members[0]['entry'].faction), members,
                                db_id=sid)
    solo_by_faction = {}
    for s in sessions:
        if not s['sess']['squad_db_id']:
            solo_by_faction.setdefault(int(s['entry'].faction), []).append(s)
    for fac, grp in sorted(solo_by_faction.items()):
        # 按兵种再分组：同兵种才凑一队（队名=该兵种官方小队模板名，
        # 与大厅 UI 默认名一致；不同兵种大厅默认名不同，混编会再次对不上）
        by_branch = {}
        for s in grp:
            tmpl = squad_template_for_character(
                (s['ctx'] or {}).get('char_template'))
            by_branch.setdefault(int(tmpl), []).append(s)
        for sq_tmpl, sub in sorted(by_branch.items()):
            sub.sort(key=lambda s: (-int((s['ctx'] or {}).get('rank', 0) or 0),
                                    int(s['entry'].player_id)))
            # 凑队上限 = 该兵种小队模板槽数（超编成员战斗服找不到槽）
            _cap = squad_template_slot_count(sq_tmpl)
            for i in range(0, len(sub), _cap):
                chunk = sub[i:i + _cap]
                leader = chunk[0]
                _register_mission_squad(
                    _next_match_id(_MM_SQUAD_ID),
                    _auto_squad_numbered_name(mission_id, fac, sq_tmpl),
                    int(leader['entry'].char_id), fac, chunk,
                    template=sq_tmpl)
    if _MM_MISSION_SQUADS.get(mission_id):
        for sq_id, info in _MM_MISSION_SQUADS[mission_id].items():
            _log(f'mission={mission_id} squad={sq_id} name={info["name"]!r} '
                 f'leader_char={info["leader_char_id"]} members='
                 f'{[(m["gamertag"], m["slot"]) for m in info["members"]]}')

    # 真实小队整队置 InBattle（DB 先行；随后 KVCS 整队行读取该状态）
    real_squads = sorted({s['sess']['squad_db_id'] for s in sessions
                          if s['sess']['squad_db_id']})
    for sid in real_squads:
        try:
            for m in await state.STORE.list_squad_members(sid):
                await state.STORE.update_squad_member(m['id'], status=4)
        except Exception as ex:
            _log(f'      [DB] squad InBattle updates failed '
                 f'(squad={sid}): {ex}')

    _MM_MISSION_DETAILS[mission_id] = {
        'preset': preset, 'gamemode': gamemode_id,
        'ai_fill': bool(match.ai_fill), 'map_entity_id': map_entity_id,
        'attacker_faction': attacker, 'defender_faction': defender,
        'enable_bots': enable_bots, 'enable_sa_bots': enable_sa_bots,
        # war 会战局（M2）：battle_id>0 → MissionInfo f2=真实 battleId、
        # f7 isAutogenerated=0；结算钩子按 battle_id 回写战争状态
        'battle_id': battle_id,
        # 成局时刻（monotonic）：等待期中途加入窗口（mm_config.JOIN_WINDOW）
        # 从此起算；release_match 随详情一并清理
        'formed_at': time.monotonic(),
        # 本局天气/时段（成局时随机抽取一次，简报 MissionInfo f12/f13 与
        # 战斗服 BattleFieldSettings f9/f21/f22 同源取值；web 管理端
        # /config 保存的动态配置即时生效）
        **(await _weather_details()),
        # ★必须取 s['sess']（每人各自的登记行）：此处在 for e in
        #   match.players 循环之后，列表推导式里引用 e 会捕获循环残留
        #   变量 = 最后一名玩家 → 多人局所有条目 faction/char_id/player_id
        #   全被抹成最后一人的值（2026-09-02 双人局大厅玩家列表双方都
        #   显示在最后一人阵营下的根因；单人局 e 恰为本人故未暴露）
        'players': [{'player_id': int(s['sess']['player_id']),
                     'gamertag': s['sess']['gamertag'],
                     'char_id': int(s['sess']['char_id']),
                     'faction': int(s['sess']['faction']),
                     'pms_id': s['pms_id']} for s in sessions],
    }
    # 每局资源池：f5（匹配界面）/ f14（握手）/ f2（轮询）三处单一事实源，
    # 出生消耗（ReportMissionStatusRequest f2）扣减（与详情同生命周期）
    # war 会战局：池 = 双方实际参战 AT 编制之和（init_mission_war）
    if battle_id:
        await _init_war_mission_resources(mission_id, battle_id, attacker,
                                          defender)
    else:
        # 官服口径"进局必有装备载具之一"：池按成局玩家装备载具类别并集
        # 查缺补齐（坦克兵/飞行员缺类别即无法部署，攻方 120s 判负根因）
        require_cats = {int(c) for e in match.players
                        for c in getattr(e, 'vehicle_cats', ())}
        # ★角色自身兵种类别一并保底：部署界面按资源条目判定各兵种可用
        # 生命数，池缺角色类别 = 该兵种小队无法部署；伞兵尤其致命——引擎
        # 开局建伞兵小队失败直接整队判负（2026-09-09 VPS 实锤：伞兵进
        # capture_hold_inf 局池无 cat4，MissionStart 后 1s
        # ReportDefeatedFaction，连输 3 局；mode 计划有 para 的 assault
        # 局同玩家则正常）
        require_cats |= {int(e.char_cat) for e in match.players
                         if int(getattr(e, 'char_cat', 0) or 0)}
        _init_mission_resources(mission_id, gamemode_id, attacker, defender,
                                require_cats)
    _log(f'match formed: mission={mission_id}, preset={preset}, map='
         f'{combat_map}, players={len(match.players)}, ai_fill={match.ai_fill}, '
         f'bots={enable_bots}, sa_bots={enable_sa_bots}, '
         f'attacker={attacker}, defender={defender}, '
         f'host={host["name"]} udp/{port}, '
         f'weather tod={_MM_MISSION_DETAILS[mission_id]["weather_tod"]}/'
         f'cond={_MM_MISSION_DETAILS[mission_id]["weather_condition"]}'
         f'{" SNOW" if _MM_MISSION_DETAILS[mission_id].get("snow") else ""}')

    # 拉起本场专用服（/P 取第一名玩家 pms，仅 boot 参数）；实例已由
    # reserve_combat_instance 占坑，本函数补进程（本机 spawn / 远端 agent）
    first_pms = sessions[0]['pms_id'] if sessions else 0
    await _ensure_combat_server(mission_id, first_pms, combat_map)
    inst = COMBAT_INSTANCES.get(mission_id) or inst   # 本机重试换端口取实际值
    game_connect = _game_connect_for(inst)   # 客户端 hng.exe 连战斗服的地址
    if battle_id:
        # war 局：record state 0(组队中) → 1(运行中)（GetMissionList status）
        try:
            await state.STORE.set_mission_state(mission_id, 1)
        except Exception as ex:
            _log(f'[war] set_mission_state({mission_id}, 1) failed: {ex}')
    # 战局位置：mapEntityId 对应 battlefield 行坐标（简报战场名与地图标记一致；
    # battlefield_entity_for_gamemap 对无行图恒返回有效行 id，无需回退）
    pos = battlefield_pos(map_entity_id)

    # 逐玩家推送进局序列（单玩家失败不影响他人）
    for s in sessions:
        try:
            await _push_player_into_mission(s['entry'], s['ctx'], s['sess'],
                                            s['pms_id'], mission_id,
                                            game_connect, pos)
        except Exception as ex:
            _log(f'      match push failed (player={s["entry"].player_id}, '
                 f'mission={mission_id}): {ex}')
    await _push_queue_counts()


async def _push_player_into_mission(e, ctx, sess_rec, pms_id, mission_id,
                                    game_connect, pos):
    """单玩家进局 KVCS 序列（成局分发与等待期中途加入共用）。

    PMS Admitted(f14=2，触发客户端拉起 hng.exe) → solo squad InBattle
    → PMM delete（退出匹配中界面）→ BattleInfo set → 5s 后 PMS Armed
    (f14=3)。单玩家失败由调用方捕获（不影响其他玩家）。
    """
    w, vc = ctx.get('writer'), ctx.get('view_cmd')
    if w is None or vc is None:
        _log(f'      [WARN] mission={mission_id}: player={e.player_id} '
             f'has no writer, skip push')
        return
    pid = int(e.player_id)
    char_id = int(e.char_id)
    faction = int(e.faction)
    gamertag = sess_rec['gamertag']
    char_name = ctx.get('char_name') or gamertag
    rank = int(ctx.get('rank', 0) or 0)
    solo = _SOLO_SQUADS.get(pid) or {}
    squad_db_id = int(ctx.get('squad_db_id') or 0)
    # PMS Admitted（f14=2 触发客户端拉起 hng.exe）
    await _push_kvcs(w, vc, b'PlayerMissionSession',
                     _msg_player_mission_session(
                         pms_id, pid, gamertag, char_id, faction,
                         char_name, rank, mission_id, _zulu_now(),
                         game_connect,
                         metrics_uid=sess_rec['metrics_uid'],
                         metrics_session_id=sess_rec['metrics_session_id'],
                         metrics_device_id=METRICS_DEVICE_ID,
                         platform='HnG', connection_state=2,
                         battle_session_id=sess_rec['battle_session_id']))
    _log(f'S->C cmd={vc:<4} KeyValueChangeSet (PlayerMissionSession '
         f'id={pms_id}, player={pid}, mission={mission_id}, '
         f'state=2 Admitted ★launch trigger)')
    # 整队 UPDATE（InBattle）：真实小队推整队行（f17=本局 missionId、
    # 成员 InBattle=4，每名成员各自连接收到同一份）；单人推 solo 行
    if squad_db_id:
        from handlers.squad import push_squad_rows_to_writer
        await push_squad_rows_to_writer(w, vc, squad_db_id, mission_id,
                                        viewer_pid=pid)
    elif solo:
        solo['mission_id'] = mission_id
        solo['entered'] = int(solo.get('entered', 0)) + 1
        # ★ 散人被编进 2 人以上凑队：推合并小队全量行（每人的连接都收到
        #   全队成员行 → 大厅小队面板显示同队队友，2026-09-02"同阵营队友
        #   没进我的队"根因：此前只推 solo 行，合并队仅存在于战斗内
        #   SquadResponse）；并删 solo 行（客户端按"含本人 playerId 的
        #   新成员行"自动切换当前小队）。单人凑队保持 solo 行推送。
        auto_sq_id = int(sess_rec.get('squad_id', 0) or 0)
        auto_info = ((_MM_MISSION_SQUADS.get(int(mission_id)) or {})
                     .get(auto_sq_id))
        if (auto_info is not None
                and not int(auto_info.get('squad_db_id', 0) or 0)
                and len(auto_info.get('members') or []) >= 2):
            await _push_auto_squad_rows(w, vc, mission_id, auto_sq_id,
                                        auto_info, solo['entered'])
            # 记录全队行 id：战斗结束/放弃时按 id 删行（mm_sess 会话行）
            sess_rec['auto_sq_id'] = auto_sq_id
            sess_rec['auto_sq_rows'] = [int(m['row_id'])
                                        for m in auto_info['members']]
            await _push_kvcs_delete(w, vc, b'SquadMemberSession',
                                    solo['member_row'])
            await _push_kvcs_delete(w, vc, b'SquadSession',
                                    solo['squad_row'])
            _log(f'S->C cmd={vc:<4} KeyValueChangeSet delete (solo squad '
                 f'id={solo["squad_row"]} → merged into {auto_sq_id})')
        else:
            await _push_kvcs(w, vc, b'SquadSession',
                             _msg_solo_squad_session(
                                 solo['squad_row'], pid,
                                 solo['template'], solo['faction'],
                                 mission_id, solo['entered']))
            await _push_kvcs(w, vc, b'SquadMemberSession',
                             await _msg_solo_squad_member_session(
                                 solo['member_row'], solo['squad_row'], pid,
                                 gamertag, char_id, char_name, rank, 4,
                                 solo.get('metrics_session_id', 0),
                                 solo['faction']))
    # PMM delete（退出匹配中界面）
    pmm_id = _MATCH_PMM_IDS.pop(pid, None)
    if pmm_id is not None:
        await _push_kvcs_delete(w, vc, b'PlayerMatchMaking', pmm_id)
        _log(f'S->C cmd={vc:<4} KeyValueChangeSet '
             f'delete (PlayerMatchMaking id={pmm_id})')
    # BattleInfo 'set'（PMS Armed 后必须推，否则客户端不连 game server）
    await _push_kvcs(w, vc, b'BattleInfo',
                     _msg_battle_info(mission_id, pos[0], pos[1]))
    _log(f'S->C cmd={vc:<4} KeyValueChangeSet (BattleInfo id={mission_id})')
    # ★ 好友列表实时刷新：好友的"战斗中/跟随进入"状态来自 friendinfo 行
    #   missionId/missionFactionId（social.push_friend_rows 读 PMS_TO_SESSION，
    #   此刻已登记）。不通知则在线好友要等重登才看到我进战斗、按钮不出现。
    #   成局分发与等待期中途加入共用本函数，一处覆盖两条路径。
    try:
        from handlers.social import notify_friends_of
        await notify_friends_of(pid)
    except Exception as ex:
        _log(f'      [social] notify_friends_of({pid}) failed: {ex}')
    # ★ private_login.pcapng 复核：Admitted 数秒后再推 f14=3 (Armed)，
    #   KVCS 全走 Flash 连接（game 连接保持静默）。
    await asyncio.sleep(5)
    await _push_kvcs(w, vc, b'PlayerMissionSession',
                     _msg_player_mission_session(
                         pms_id, pid, gamertag, char_id, faction,
                         char_name, rank, mission_id, _zulu_now(),
                         game_connect,
                         metrics_uid=sess_rec['metrics_uid'],
                         metrics_session_id=sess_rec['metrics_session_id'],
                         metrics_device_id=METRICS_DEVICE_ID,
                         platform='HnG', connection_state=3,
                         battle_session_id=sess_rec['battle_session_id']))
    _log(f'S->C cmd={vc:<4} KeyValueChangeSet (PlayerMissionSession '
         f'id={pms_id}, mission={mission_id}, state=3 Armed ★battle arm)')


def _find_joinable_mission(gamemode_id, presets, faction, only_mission=None,
                           vehicle_cats=(), char_cat=0):
    """找一个等待期内可中途加入的战局，返回 (mission_id, inst) 或 None。

    条件（全部满足）：
    - 战局实例仍在运行（COMBAT_INSTANCES 且 _MM_MISSION_DETAILS 均在）
    - 同 gamemode；玩家选图兼容（未选图=通配，否则须包含该局 preset）
    - 玩家阵营属于该局攻防双方（第三方阵营无出生侧，无法进图）
    - 成局后未超过 mm_config.JOIN_WINDOW 秒（开局等待/倒计时阶段）
    - 该局人数未达该模式 max_players
    - 玩家侧池覆盖其装备载具类别之一（官服口径"进局必有装备载具之一"，
      否则坦克兵/飞行员无法部署）——仅 staged 局过滤：war 会战局兵力 =
      将军部署 AT（真实语义不因个人装备改池）、显式指定战局（only_mission
      尊重玩家选择）均不过滤
    - 玩家角色自身兵种类别本局池必须 >0（char_cat：伞兵=4/坦克兵=5 等，
      any-of 的载具过滤不覆盖无载具玩家；缺类别进局即判负/无法部署）
    only_mission 非空时只考虑该 mission_id（JoinMatchMakingRequest f2
    指定战局加入：战争地图点进行中战斗、好友列表"跟随进入"两条路径）。
    ★ 指定战局跳过 gamemode/preset/JOIN_WINDOW 三过滤（同 f4=war 的 M2
    路径口径）：跟随好友时 UI 勾选的模式/地图不必与好友那局一致、开打
    多久都应能加入，否则会被筛掉转入普通撮合 → 落进另一场局（跟随失效）。
    阵营/人数上限为硬约束，保留。
    ★ gamemode_id=None（快速匹配跨模式搜局）：不限模式，但只进具体
    staging 模式局（QUICKPLAY_JOIN_CANDIDATES）——训练(1)/战争(10)/
    会战(battle_id>0)/旧虚拟号局一律不进；preset/阵营/窗口/满员/载具
    过滤照常生效。
    """
    if mm_config.JOIN_WINDOW <= 0 and not only_mission:
        return None
    now = time.monotonic()
    if only_mission:
        inst = COMBAT_INSTANCES.get(int(only_mission))
        cands = [(int(only_mission), inst)] if inst is not None else []
    else:
        cands = list(COMBAT_INSTANCES.items())
    for mission_id, inst in cands:
        det = _MM_MISSION_DETAILS.get(mission_id)
        if det is None or 'preset' not in det:
            continue
        if not only_mission:
            if gamemode_id is None:
                # 快速匹配跨模式搜局：只进具体 staging 模式局（训练/战争/
                # 会战/旧虚拟号局不进——训练是 PVE、战争走战争路径、
                # 虚拟号局是本修复前的历史遗留）
                if det.get('battle_id'):
                    continue
                if int(det.get('gamemode', 0) or 0) \
                        not in mm_config.QUICKPLAY_JOIN_CANDIDATES:
                    continue
            elif int(det.get('gamemode', 0) or 0) != int(gamemode_id):
                continue
        if (not only_mission and presets
                and int(det.get('preset', 0) or 0) not in presets):
            continue
        # ★ 阵营边界（开局两阵营固定，第三方阵营禁入）：det 阵营字段
        #   缺失/非法时不放行——原 .get 默认 3 恰为 SU，字段未就绪会
        #   漏放第三阵营（线上美德局苏军 late join 进局判负+观战）。
        #   玩家 faction 必须严格命中该局攻防两阵营之一。
        atk = int(det.get('attacker_faction', 0) or 0)
        dfd = int(det.get('defender_faction', 0) or 0)
        if atk <= 0 or dfd <= 0:
            continue
        if faction not in (atk, dfd):
            continue
        # JOIN_WINDOW 仅约束模糊 late join（按模式找局）；显式指定战局
        # 随时可加入（好友跟随/战争图点战斗，官服口径同 M2 路径）
        if (not only_mission
                and now - float(det.get('formed_at', 0) or 0)
                > mm_config.JOIN_WINDOW):
            continue
        # 人数上限按该局自身 gamemode 查（指定战局时请求侧 UI 模式
        # 不必与该局一致）
        max_p = int(mm_config.gamemode_cfg(
            int(det.get('gamemode', 0) or gamemode_id)).get('max_players', 18))
        if len(det.get('players') or ()) >= max_p:
            continue
        # 载具/兵种覆盖过滤（staged 局；war 局/显式指定战局不过滤，见 docstring）
        if not only_mission and not det.get('battle_id'):
            fac_counts = (force_pool.pool(mission_id) or {}).get(int(faction))
            if vehicle_cats and (not fac_counts or not any(
                    int(c) in fac_counts and fac_counts[int(c)] > 0
                    for c in vehicle_cats)):
                continue
            # 角色自身兵种类别必须本局池有（any-of 的载具过滤不覆盖：
            # 伞兵/坦克兵等无载具玩家；缺类别进局即判负）
            if int(char_cat or 0) and (
                    not fac_counts
                    or int(fac_counts.get(int(char_cat), 0)) <= 0):
                continue
        return mission_id, inst
    return None


def _resolve_quickplay_gamemode():
    """快速匹配入池模式解析（官服语义"扔进任何战局"的池侧半边）：

    并入等待人数最多的具体 staging 模式池（与已排队玩家凑局最快，
    并列随机），全空则从 QUICKPLAY_POOL_MODES 随机。返回值一定是
    QUICKPLAY_POOL_MODES 之一——虚拟模式号（11/12/14）到此为止，
    绝不入池（入池即与所有具体模式池隔离，谁也汇不进谁）。
    """
    counts = POOL.waiting_counts()
    best = max((counts.get(m, 0) for m in mm_config.QUICKPLAY_POOL_MODES),
               default=0)
    if best > 0:
        return random.choice([m for m in mm_config.QUICKPLAY_POOL_MODES
                              if counts.get(m, 0) == best])
    return random.choice(mm_config.QUICKPLAY_POOL_MODES)


async def _join_existing_mission(pid, mission_id, e, ctx):
    """等待期中途加入现有战局：登记 PMS 会话 + 推进局 KVCS 序列。

    与 _start_match 单玩家路径同构：发 pms/metrics/battle_session/squad id、
    登记 PMS_TO_MISSION/PMS_TO_SESSION（game_server 按 pms 识别会话）、
    追加进 _MM_MISSION_DETAILS['players']（简报参战方名单/GMD 立即可见），
    客户端 hng.exe 直连该局 combat server UDP 端口。
    """
    q = _MM_QUEUES.pop(pid, None)
    _cleanup_queued_player(pid)
    ctx = (q or {}).get('ctx') or ctx
    det = _MM_MISSION_DETAILS[mission_id]
    inst = COMBAT_INSTANCES[mission_id]
    # ★ 去重：同玩家再次进入同一局（掉线 rejoin / 重复点匹配）先清旧痕
    #   迹——否则旧 PMS 会话、det['players'] 条目、小队 members 会重复
    #   append（幽灵行：earnings/结算会重复计入）。
    pid = int(pid)
    old_pms = [p for p, m in list(PMS_TO_MISSION.items())
               if int(m) == int(mission_id) and
               int((PMS_TO_SESSION.get(p) or {}).get('player_id', 0) or 0) == pid]
    old_squad_db = 0
    for p in old_pms:
        PMS_TO_MISSION.pop(p, None)
        old_s = PMS_TO_SESSION.pop(p, None)
        if old_s:
            # 旧会话的小队归属（真实小队 rejoin 归队用；散人为 0）
            old_squad_db = int(old_s.get('squad_db_id', 0) or 0) or old_squad_db
    det['players'] = [p for p in (det.get('players') or [])
                      if int(p.get('player_id', 0) or 0) != pid]
    for sq_id, info in list((_MM_MISSION_SQUADS.get(mission_id) or {}).items()):
        info['members'] = [m for m in (info.get('members') or [])
                           if int(m.get('player_id', 0) or 0) != pid]
        if not info['members']:
            _MM_MISSION_SQUADS[mission_id].pop(sq_id, None)
    pms_id = _next_match_id(_MM_PMS_ID)
    metrics_uid = ctx.get('metrics_uid') or str(uuid.uuid4())
    metrics_session_id = int(ctx.get('metrics_session_id', 0) or 0) or \
        _next_match_id(_MM_METRICS_SESSION_ID)
    sess_rec = {'pms_id': pms_id, 'player_id': pid,
                'char_id': int(e.char_id),
                'gamertag': ctx.get('gamertag') or 'Player',
                'faction': int(e.faction),
                'metrics_uid': metrics_uid,
                'metrics_session_id': metrics_session_id,
                'battle_session_id': _next_match_id(_MM_BATTLE_SESSION_ID),
                'squad_id': 0, 'squad_slot': 0,
                'squad_db_id': int(ctx.get('squad_db_id', 0) or 0),
                'mission_id': mission_id,
                'quickplay': bool(ctx.get('quickplay', False))}
    # ★ 局内小队归组：
    #   - 真实小队 rejoin 归队（优先）：掉线重连的 DB 小队成员放回原
    #     小队在本局的登记行（去重已移出 members），恢复 squad_id/slot
    #     与队友一致（PlayerSettings f16 / SquadResponse f1 同源）；
    #     原小队行不在本局（被裁/解散）才降级散人路径
    #   - 散人中途加入补进同阵营、同兵种、未满员的单人凑成队（不动
    #     真实小队）；没有可补的队则自建一队（本人任队长），队名=本人
    #     兵种官方小队模板名 + 本局 (阵营, 兵种) 编号（不占已有号）。
    join_rank = int(ctx.get('rank', 0) or 0)
    join_tmpl = squad_template_for_character(ctx.get('char_template'))
    new_member = {'player_id': pid, 'char_id': int(e.char_id),
                  'gamertag': sess_rec['gamertag'], 'rank': join_rank,
                  'slot': 0,
                  'row_id': _next_match_id(_MM_SQUAD_MEMBER_ROW)}
    picked = None
    squad_db_id = old_squad_db or int(ctx.get('squad_db_id', 0) or 0)
    if squad_db_id:
        for sq_id, info in (_MM_MISSION_SQUADS.get(mission_id) or {}).items():
            if int(info.get('squad_db_id', 0) or 0) != int(squad_db_id):
                continue
            picked = (sq_id, info)      # 原小队在本局的登记行
            break
    if picked is None:
        for sq_id, info in (_MM_MISSION_SQUADS.get(mission_id) or {}).items():
            if info.get('squad_db_id'):
                continue                  # 真实小队不吸收散人
            if int(info.get('faction', 0) or 0) != int(e.faction):
                continue
            if int(info.get('template', 1) or 1) != int(join_tmpl):
                continue                  # 兵种不同则大厅默认名对不上
            # 补队上限 = 小队模板槽数：战斗服开局已拉取全量槽定义，
            # 超过槽数的 late join 会报 slot not found（2026-09-02）
            if len(info.get('members') or []) >= squad_template_slot_count(
                    info.get('template', 1) or 1):
                continue
            picked = (sq_id, info)
            break
    if picked is not None:
        sq_id, info = picked
        # 取最小空闲槽位：成员掉线后列表收缩，len(members) 可能与在队
        # 成员的槽位冲突 → 战斗服按槽位踢人（"Slot already occupied
        # removing old player"），绝不能撞在队玩家的槽
        used = {int(m.get('slot', 0) or 0) for m in info['members']}
        slot = 0
        while slot in used:
            slot += 1
        new_member['slot'] = slot
        info['members'].append(new_member)
        sess_rec['squad_id'] = sq_id
        sess_rec['squad_slot'] = new_member['slot']
        sess_rec['squad_db_id'] = int(info.get('squad_db_id', 0) or 0)
    else:
        sq_id = _next_match_id(_MM_SQUAD_ID)
        _MM_MISSION_SQUADS.setdefault(mission_id, {})[sq_id] = {
            'name': _auto_squad_numbered_name(mission_id, e.faction, join_tmpl),
            'leader_char_id': int(e.char_id), 'faction': int(e.faction),
            'squad_db_id': 0, 'template': int(join_tmpl),
            'members': [new_member]}
        sess_rec['squad_id'] = sq_id
        sess_rec['squad_db_id'] = 0   # 自建散人队：清掉 ctx 带入的小队归属
    PMS_TO_MISSION[pms_id] = mission_id
    PMS_TO_SESSION[pms_id] = sess_rec
    det.setdefault('players', []).append(
        {'player_id': pid, 'gamertag': sess_rec['gamertag'],
         'char_id': int(e.char_id), 'faction': int(e.faction),
         'pms_id': pms_id})
    age = time.monotonic() - float(det.get('formed_at', 0) or 0)
    _log(f'late join: player {pid} -> mission={mission_id} '
         f'(players={len(det["players"])}, udp/{inst["port"]}, '
         f'age={age:.0f}s, gamemode={det.get("gamemode")}, '
         f'faction={sess_rec["faction"]} vs '
         f'atk={det.get("attacker_faction")}/'
         f'dfd={det.get("defender_faction")})')
    # 该局专用服可能仍在加载地图（握手未完成）：有界等待，超时放行——
    # 客户端 hng.exe 连不上 UDP 会自动重试（与 _start_match 同哲学）
    try:
        await asyncio.wait_for(inst['ready'].wait(), 30)
    except asyncio.TimeoutError:
        _log(f'late join: mission={mission_id} combat server not ready '
             f'after 30s - pushing Admitted anyway (client will retry UDP)')
    game_connect = _game_connect_for(inst)   # 该局所在主机对外地址:端口
    pos = battlefield_pos(int(det.get('map_entity_id') or 0)
                          or MISSION_MAP_ENTITY_ID)
    try:
        await _push_player_into_mission(e, ctx, sess_rec, pms_id, mission_id,
                                        game_connect, pos)
    except Exception as ex:
        _log(f'      late join push failed (player={pid}, mission='
             f'{mission_id}): {ex}')
    # ★ 中途加入进了 2 人以上凑队：把凑队全量行（SquadSession + 全体
    #   成员行，含新成员）推给已在队成员的 Flash 连接——他们的大厅小队
    #   面板才能看到新队友。已在队成员若成局时是单人（后来才被凑成队），
    #   还要删其 solo 行 + 登记 reset 信息（battle end 删凑队行用）。
    #   新成员自己的行由上面 _push_player_into_mission 全量推送；
    #   真实小队不在此处理（走 DB 成员行推送路径）。全量行按相同行 id
    #   重复推送幂等（客户端表 upsert），已被推送过的成员无副作用。
    try:
        _sq_id = int(sess_rec.get('squad_id', 0) or 0)
        _info = ((_MM_MISSION_SQUADS.get(int(mission_id)) or {}).get(_sq_id))
        if (_info is not None and not int(_info.get('squad_db_id', 0) or 0)
                and len(_info.get('members') or []) >= 2):
            _row_ids = [int(mm['row_id']) for mm in _info['members']]
            for m in _info['members']:
                _mpid = int(m.get('player_id', 0) or 0)
                if _mpid == int(pid):
                    continue          # 本人已在上面收到全量行
                flash = FLASH_SESSIONS.get(m.get('gamertag') or '')
                if flash is None or flash.playerview_cmd is None:
                    continue
                await _push_auto_squad_rows(flash.writer,
                                            flash.playerview_cmd, mission_id,
                                            _sq_id, _info, 1)
                _msolo = _SOLO_SQUADS.get(_mpid)
                if _msolo is not None:
                    await _push_kvcs_delete(flash.writer,
                                            flash.playerview_cmd,
                                            b'SquadMemberSession',
                                            _msolo['member_row'])
                    await _push_kvcs_delete(flash.writer,
                                            flash.playerview_cmd,
                                            b'SquadSession',
                                            _msolo['squad_row'])
                for _pms, _rec in PMS_TO_SESSION.items():
                    if (int((_rec or {}).get('player_id', 0) or 0) == _mpid
                            and int((_rec or {}).get('squad_id', 0) or 0)
                            == _sq_id):
                        _rec['auto_sq_id'] = _sq_id
                        _rec['auto_sq_rows'] = _row_ids
                        break
    except Exception as ex:
        _log(f'      [WARN] late join member row fanout failed '
             f'(mission={mission_id}): {ex}')
    await _push_queue_counts()


def _pms_rejoin_expired(rec):
    """会话掉线保护是否已过期（rejoin_deadline 已过 = 席位已释放语义）。

    rejoin_deadline 只在 hng.exe 游戏连接断开（on_game_client_closed）或
    PlayerExitBatch 上报时设置；未设置 = 玩家在线/局中，恒未过期。
    过期会话由 handlers/mission.rejoin_sweep_loop 异步清理，查找器先行
    过滤（重登 rejoin / 1c-0 回原局 / 好友跟随都不得再命中过期会话）。
    """
    dl = (rec or {}).get('rejoin_deadline')
    return bool(dl) and time.time() >= float(dl)


def arm_rejoin_deadline(pid, reason='game-conn-closed'):
    """给玩家当前存活会话记 rejoin 掉线保护截止（now + mm_config.REJOIN_TIMEOUT）。

    - 已有 deadline 的记录不动（保护期内不重置；已过期的留给
      rejoin_sweep_loop 释放，绝不"复活"——PlayerExitBatch 在玩家另开
      新局后上报时不得把同 pid 的过期旧会话重新拉活）
    - 只给 mission 仍在 COMBAT_INSTANCES 存活的记录起保护（战局收尾
      中的会话交给战局生命周期）
    返回是否有记录被起保护。
    """
    if pid is None:
        return False
    pid = int(pid)
    armed = False
    for pms_id, rec in PMS_TO_SESSION.items():
        if int((rec or {}).get('player_id', 0) or 0) != pid:
            continue
        if rec.get('rejoin_deadline'):
            continue              # 已有 deadline（含已过期）：不覆盖
        mid = int(PMS_TO_MISSION.get(pms_id, 0) or 0)
        if not mid or mid not in COMBAT_INSTANCES:
            continue              # 战局已不存在（收尾中）
        rec['rejoin_deadline'] = time.time() + mm_config.REJOIN_TIMEOUT
        armed = True
    if armed:
        _log(f'rejoin: player {pid} drop protection armed '
             f'({mm_config.REJOIN_TIMEOUT}s, {reason})')
    return armed


def clear_rejoin_deadline(pid):
    """玩家重进战局（hng.exe 携同会话重连）：撤销掉线保护。幂等。"""
    if pid is None:
        return
    pid = int(pid)
    for rec in PMS_TO_SESSION.values():
        if int((rec or {}).get('player_id', 0) or 0) == pid:
            rec.pop('rejoin_deadline', None)


async def on_game_client_closed(pid):
    """hng.exe 游戏连接断开（崩溃/退出/被杀）：官服掉线语义复刻。

    1. 记 rejoin_deadline（now + REJOIN_TIMEOUT），超时由
       handlers.mission.rejoin_sweep_loop 释放会话（PMS 清理 + 坑位回收）；
    2. 给在线 Flash 会话推 PMS UPDATE connectionState=2 (Admitted) +
       新 timeAdmitted → 客户端 ActionGameService Admitted 分支起
       300s 倒计时（StatusMediator REJOIN + "gameConnectionLostCountdown"，
       到 0 转 GAME_TIMED_OUT）——此前只靠 PlayerExitBatch（优雅退出才
       上报），崩溃/被杀路径无任何推送 → REJOIN 按钮无倒计时、席位
       永不释放（2026-09-08 排查根因）。
    """
    pms_id, rec, mission_id = _find_player_live_pms(pid)
    if pms_id is None or rec is None:
        return                      # 无存活战局（含已过期被过滤）
    dl = rec.get('rejoin_deadline')
    if dl and time.time() < float(dl):
        return          # 优雅退出已由 PlayerExitBatch 推过（防双触发重复推）
    arm_rejoin_deadline(pid)
    from handlers.combat_join import _push_pms_admitted   # 延迟 import 防环
    flash = FLASH_SESSIONS.get(rec.get('gamertag') or '')
    if flash is not None and flash.playerview_cmd is not None:
        await _push_pms_admitted(flash, flash.writer, rec)
    else:
        _log(f'rejoin: player {pid} game-conn closed, no live Flash '
             f'session; countdown push skipped (deadline armed)')


def _find_player_active_mission(pid):
    """玩家当前存活的原始战局（掉线 rejoin 用）。

    遍历 PMS 会话找 player_id 命中且 mission 仍在 COMBAT_INSTANCES
    存活的记录，返回 mission_id（多条取最大=最新成局）；无 → None。
    drop_player 不清已成局会话（战局照常进行），掉线重连据此找回原局。
    rejoin_deadline 已过期（掉线保护超时）的会话视为已释放，不再命中。
    """
    if pid is None:
        return None
    pid = int(pid)
    best = 0
    for pms_id, rec in list(PMS_TO_SESSION.items()):
        if int((rec or {}).get('player_id', 0) or 0) != pid:
            continue
        if _pms_rejoin_expired(rec):
            continue
        mid = int(PMS_TO_MISSION.get(pms_id, 0) or 0)
        if mid and mid in COMBAT_INSTANCES and mid > best:
            best = mid
    return best or None


def _find_player_live_pms(pid):
    """玩家当前存活战局的 (pms_id, sess_rec, mission_id)（重登 rejoin 用）。

    与 _find_player_active_mission 同口径（mission 必须仍在
    COMBAT_INSTANCES 存活，多条取最新成局），多返回会话记录本身；
    无 → (None, None, None)。rejoin_deadline 已过期的会话不再命中
    （重登不再推 REJOIN 行，正常登录进大厅）。
    """
    if pid is None:
        return None, None, None
    pid = int(pid)
    best_mid = 0
    best = (None, None)
    for pms_id, rec in list(PMS_TO_SESSION.items()):
        if int((rec or {}).get('player_id', 0) or 0) != pid:
            continue
        if _pms_rejoin_expired(rec):
            continue
        mid = int(PMS_TO_MISSION.get(pms_id, 0) or 0)
        if mid and mid in COMBAT_INSTANCES and mid > best_mid:
            best_mid = mid
            best = (pms_id, rec)
    if best_mid:
        return best[0], best[1], best_mid
    return None, None, None


async def push_relogin_rejoin(writer, view_cmd, pid):
    """掉线/崩溃重登 rejoin 恢复：登录订阅时重推存活的 PlayerMissionSession 行。

    根因（2026-09-06 排查）：客户端整端崩溃重开后 ActionGameStatusModel 是
    全新的，playerMissionSessionId=null；登录同步不带回流局 PMS 行 →
    StatusMediator.onRegister 判 BASE_STATE，主按钮是普通"进入战斗"，
    没有"恢复战斗"（REJOIN）——用户只能再点一次匹配，靠 1c-0 rejoin
    路径静默回局。
    ★ 二次根因（2026-09-08 remote-log + SWF 反编译实锤）：推了也没用的
    时机问题——最初实现在 subscribeplayerview 响应里推，但客户端登录
    顺序是 subscribeplayerview → login2_postlogin（成功才置
    settingsModel.loggedIn=true）→ subscribesoldierview →
    SubscribePlayerMissionViewRequest，且 ActionGameService.
    onPlayerMissionSessionChanges 对 loggedIn==false 期间到达的 PMS 行
    整段丢弃（playerMissionSessionId 不设置），故行必须在
    subscribeplayermissionview 订阅时推（views.handle_subscribe 对应
    分支），此时 loggedIn 已为 true，ADD 分支生效。

    官方行为：登录时服务器把仍在进行的 PMS 行重新下发。此处复刻：
    - 同一 pms 行 id / battle_session_id / metrics id（hng.exe 重拉后
      GetPlayerSettings 按 pms 精确命中原会话，game_server 无需兜底）；
    - ★ connectionState=2 (Admitted)（2026-09-08 三修，战局详情弹窗）：
      曾经用 state=3（Entered）——按钮有了但 StatusCallout 永不弹出。
      反编译实锤：REJOIN 弹窗唯一自动显示路径 = ACTION_GAME_SERVICE_
      MISSION_END_TIMER 每秒 tick → onLosingPlayerMissionSessionCountdown
      Updates → handleCallOutStatus()，而该定时器只在 handleSession
      Updates 的 Admitted 分支启动，Entered 分支什么都不做 → 弹窗死路。
      曾经担心 state=2 会在登录瞬间自动拉起 hng.exe 是误判：自动拉起
      条件 _isGameLaunchedOnce==false && gameClientDataDownloaded，后者
      在登录开始被 ClearAndConnectCommand 重置 false、全部 view 订阅完
      +资产同步后才置 true，PMS 行在 missionview 订阅响应中到达必然
      早于该时刻 → 不拉起，走倒计时分支弹出战局详情（与优雅退出后
      300s 倒计时弹窗同一代码路径，已实测）；
    - timeAdmitted 刷新为当前（该字段同时是 Admitted 分支 300s 会话
      丢失倒时基，推旧值会秒判 GAME_TIMED_OUT）；
    - 附带 BattleInfo 'set'（同局同 id 幂等）：PMS 行在而 BattleInfo
      缺失时客户端不连 game server（见 _push_player_into_mission）；
    - ★ 小队行重推（2026-09-08 四修）：临时凑的小队只存内存
      （_MM_MISSION_SQUADS / _SOLO_SQUADS），重登无 DB 可查 → 小队面板
      不恢复。凑队推全量行、单人推 solo 行（status=4）、DB 真小队按
      本局 mission_id 重推（补 SquadSession.f17）。
    战局结束/放弃仍走 _finish_mission_sessions / mission abandon 照常
    PMS DELETE（REJOIN 消失回 BASE_STATE）。
    ★ 掉线保护（2026-09-08）：rejoin_deadline 已过期的会话不再命中
    （_find_player_live_pms 过滤）→ 不推 REJOIN 行，正常登录进大厅；
    在保护期内重登则照常推（重进战局即清 deadline，保护期到点由
    rejoin_sweep_loop 推 PMS DELETE 收尾）。无存活战局 → 不推
    （正常登录）。
    """
    pms_id, rec, mission_id = _find_player_live_pms(pid)
    if pms_id is None or rec is None:
        return False
    inst = COMBAT_INSTANCES.get(mission_id) or {}
    det = _MM_MISSION_DETAILS.get(mission_id) or {}
    gamertag = rec.get('gamertag') or 'Player'
    char_name = gamertag
    rank = 0
    if state.STORE is not None:
        try:
            for ch in await state.STORE.list_characters(int(rec['player_id'])):
                if int(ch['id']) == int(rec.get('char_id', 0) or 0):
                    char_name = ch['name']
                    rank = _rank_from_combat_xp(
                        int(ch.get('combat_xp', 0) or 0))
                    break
        except Exception as e:
            _log(f'      [DB] relogin rejoin char lookup failed: {e}')
    game_connect = _game_connect_for(inst)   # 该局所在主机对外地址:端口
    pos = battlefield_pos(int(det.get('map_entity_id') or 0)
                          or MISSION_MAP_ENTITY_ID)
    await _push_kvcs(writer, view_cmd, b'PlayerMissionSession',
                     _msg_player_mission_session(
                         pms_id, int(rec['player_id']), gamertag,
                         int(rec.get('char_id', 0) or 0),
                         int(rec.get('faction', 3) or 3),
                         char_name, rank, mission_id, _zulu_now(),
                         game_connect,
                         metrics_uid=rec.get('metrics_uid') or str(uuid.uuid4()),
                         metrics_session_id=int(
                             rec.get('metrics_session_id', 0) or 0),
                         metrics_device_id=METRICS_DEVICE_ID,
                         platform='HnG', connection_state=2,
                         battle_session_id=int(
                             rec.get('battle_session_id', 0) or 0)))
    await _push_kvcs(writer, view_cmd, b'BattleInfo',
                     _msg_battle_info(mission_id, pos[0], pos[1]))
    _log(f'S->C cmd={view_cmd:<4} KeyValueChangeSet (PlayerMissionSession '
         f'id={pms_id}, player={rec.get("player_id")}, '
         f'mission={mission_id}, state=2 Admitted ★relogin rejoin)')
    # ★ 四修（2026-09-08）：小队行重推——临时凑的小队（_MM_MISSION_SQUADS
    # 内存登记，非 DB）重登后无人推 → 客户端 SquadSession/SquadMemberSession
    # 表全空，小队面板不恢复（大厅看不到同队队友）。按入局时同一套逻辑推：
    #   - DB 真小队：views 登录已在 playerview 推过（missionId=0），此处按
    #     本局 mission_id 重推同 id 行（命中 UPDATE 分支补 f17）；
    #   - 凑队（≥2 人）：全量行（SquadSession + 全体成员行，客户端按"含
    #     本人 playerId 的成员行"自动切换当前小队）；
    #   - 单人：solo 行（status=4 InBattle——玩家仍在局内 rejoin）。
    try:
        pid_ = int(rec['player_id'])
        sq_id = int(rec.get('squad_id', 0) or 0)
        sq_db = int(rec.get('squad_db_id', 0) or 0)
        if sq_db:
            from handlers.squad import push_squad_rows_to_writer
            await push_squad_rows_to_writer(writer, view_cmd, sq_db,
                                            mission_id, viewer_pid=pid_)
        elif sq_id:
            info = (_MM_MISSION_SQUADS.get(int(mission_id)) or {}).get(sq_id)
            if info is not None and len(info.get('members') or []) >= 2:
                solo = _SOLO_SQUADS.get(pid_) or {}
                await _push_auto_squad_rows(
                    writer, view_cmd, mission_id, sq_id, info,
                    int((solo or {}).get('entered', 1) or 1))
            else:
                solo = _SOLO_SQUADS.get(pid_)
                if solo:
                    await _push_kvcs(writer, view_cmd, b'SquadSession',
                                     _msg_solo_squad_session(
                                         solo['squad_row'], pid_,
                                         solo['template'],
                                         solo['faction'], mission_id,
                                         int(solo.get('entered', 1) or 1)))
                    await _push_kvcs(
                        writer, view_cmd, b'SquadMemberSession',
                        await _msg_solo_squad_member_session(
                            solo['member_row'], solo['squad_row'], pid_,
                            gamertag, int(rec.get('char_id', 0) or 0),
                            char_name, rank, 4,
                            int(solo.get('metrics_session_id', 0) or 0),
                            solo['faction']))
    except Exception as e:
        _log(f'      [WARN] relogin squad re-push failed: {e}')
    return True
