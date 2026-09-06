# -*- coding: utf-8 -*-
"""RTS 战争状态机（Phase 3，docs/hng-rts-protocol.md）。

职责：
  - 战争生命周期：ensure_war（自动开战，读 WarDatabase capital 表分配初始
    归属）→ running → check_victory（占领首都数达 pointsNeededForVictory
    → end_war + 自动续战）。
  - war 行消息构建（29 REQUIRED 字段全填，docs/hng-rts-protocol.md §3.1）。
  - war catalogue 数据聚合（views.handle_query_war_catalogue_request 用）。

faction id 空间 = factiontemplateid 恒等映射 1/2/3（project_rules 硬约束）。
时间戳 STR 格式 'YYYYMMDDHHMMSSZ'（紧凑 Zulu，客户端统一用
TimeUtilities.convertZuluTimeToMilliseconds 按位截取解析——带 '-'/' '/':' 的
ISO 风格串会截出乱码 → Date.parse NaN → getMorale 士气恒 0）。
"""
import datetime
import json
import math
import random
import time

from protocol import (
    pb_bool,
    pb_int32,
    pb_int64,
    pb_message,
    pb_sfixed64,
    pb_string,
    pb_uint32,
    pb_float,
    )
import mm_config
import wardb
from state import log

# CommandNodeMoraleTemplate 官服实值（tests/test_wardb_rts.py §F 校验）
DEFAULT_MORALE = {
    'min': 0,
    'max': 100,
    'lost_per_battle': 20,
    'gained_per_win': 10,
    'gained_per_minute': 0.15,
    'surrender_limit': 25,
}

# armyresourcecategory 归类（force_pool._CAT_ORDER 同源）：
# 士兵族（infantryCount）/ 坦克族（tankCount）/ 地面载具族（vehicleCount）/
# 飞机族（planeCount）——war_catalogue 计数展示用（M1 近似值，可校准）。
# cat 5 = 坦克组乘员（人员，light_armor AT 编制 {5:20, 12:16} 实锤），归士兵族。
_SOLDIER_CATS = {1, 2, 4, 5, 6}
_TANK_CATS = {7, 8, 12, 17, 18}
_GROUND_VEHICLE_CATS = {9, 11, 15, 16}
_PLANE_CATS = {3, 10, 20, 21}


def morale_defaults():
    """战争 morale 参数：wardb.morale_template() 优先，缺表回退官服实值。"""
    mt = wardb.morale_template()
    if mt:
        return {k: mt.get(k, DEFAULT_MORALE[k]) for k in DEFAULT_MORALE}
    return dict(DEFAULT_MORALE)


def capital_battlefield_ids():
    """首都 battlefield id 集合（胜利点数判据）。"""
    return {c['battlefield_id'] for c in wardb.capitals()
            if c.get('battlefield_id')}


def effective_points_needed(stored=None):
    """胜利所需主城数（war f8 / war_catalogue f4 下发值与 check_victory 阈值）。

    基准 = 落库值（无值回退 MM_WAR_POINTS_VICTORY，官服 15=完整地图
    15 座主城），与 WarDatabase capital 表实际主城数取小：裁剪地图主城
    不足时按实际数——否则客户端按 capital 表数出总数 N，显示"占领 N 座
    城市中的 15 座"（N<15 永不可达，战争无法结束）。无 capital 表
    （空地图）回退基准值。"""
    base = int(stored or 0) or mm_config.WAR_POINTS_VICTORY
    caps = len(capital_battlefield_ids())
    return min(base, caps) if caps > 0 else base


def initial_city_owners():
    """开战初始城市归属 {battlefield_id: faction}。

    官服语义：仅 capital 表登记的城市有初始归属（default_faction），
    其余战场节点中立（0）。"""
    return {c['battlefield_id']: c['default_faction']
            for c in wardb.capitals()
            if c.get('battlefield_id') and c.get('default_faction')}


def _fmt_ts(dt):
    """DB TIMESTAMPTZ → 协议 STR 时间戳（紧凑 Zulu 'YYYYMMDDHHMMSSZ'，UTC）。

    客户端 TimeUtilities.convertZuluTimeToMilliseconds 按位 substr(0,4)/(4,2)/
    ... 截取组 'YYYY/MM/DD HH:mm:ss GMT-0000' 再 Date.parse；ISO 分隔符串
    会解析成 NaN（uint(NaN)=0 → 0 士气 bug）。"""
    if not dt:
        return ''
    if isinstance(dt, str):
        return dt
    if dt.tzinfo is None:            # naive（FakeStore/无时区来源）按 UTC
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc).strftime('%Y%m%d%H%M%SZ')


def _as_resources(raw):
    """command_nodes.resources（JSONB 或 str 或 None）→ dict。"""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def _war_config(morale=None):
    """新战争 config 快照（wars.config_json）。"""
    morale = morale or morale_defaults()
    return {
        'strategytimescale': mm_config.WAR_TIMESCALE,
        'daycycletimescale': mm_config.WAR_DAYCYCLE_TIMESCALE,
        'maxPlayerPerFaction': mm_config.WAR_MAX_PLAYERS_PER_FACTION,
        'morale': morale,
    }


async def _next_war_name(store):
    """递进战争名：'<base> #N'（N=历史战争轮次，wars 只增不删 count+1）。
    统计不可用（FakeStore/DB 异常）回退 base 无后缀。"""
    base = mm_config.WAR_NAME
    try:
        n = int(await store.war_count()) + 1
        return f'{base} #{n}'
    except Exception:
        return base


async def ensure_war(store):
    """取当前 running 战争；无且 MM_WAR_AUTO_CREATE=1 时自动开战。

    开战流程：create_war（递进名 + config 快照）→ init_war_cities（capital
    表初始归属）。返回 war dict 或 None（store 不可用 / 不自动开战）。"""
    if store is None or getattr(store, 'pool', None) is None:
        return None
    try:
        war = await store.get_active_war()
        if war is not None:
            return war
        if not mm_config.WAR_AUTO_CREATE:
            return None
        war_name = await _next_war_name(store)
        war = await store.create_war(
            war_name,
            points_needed_for_victory=effective_points_needed(),
            config=_war_config())
        await store.init_war_cities(war['id'], initial_city_owners())
        # 阵营种子库存 = ArmyResourceProductionTemplate warStartValue
        # （§3.12.1：步兵 85 万 / 重坦 225……幂等，已有行不覆盖）
        initial = {}
        for t in wardb.armyresource_production():
            v = int(t.get('war_start_value') or 0)
            if v > 0:
                initial.setdefault(int(t.get('faction') or 0), {})[
                    int(t.get('category_id') or 0)] = v
        if initial:
            await store.init_war_faction_resources(war['id'], initial)
        log(f'[war] 新战争开打 id={war["id"]} name={war_name!r} '
            f'points={war.get("points_needed_for_victory")} '
            f'cities={len(initial_city_owners())} '
            f'stock_rows={sum(len(c) for c in initial.values())}')
        return war
    except Exception as e:
        log(f'[war] ensure_war failed: {e}')
        return None


def build_war_proto(war):
    """构建 HnG_States.war 行 proto（29 REQUIRED 字段全填）。

    war：wars 表行（config_json 已由 store._war_row 转成 config dict）。"""
    morale = (war.get('config') or {}).get('morale') or morale_defaults()
    cfg = war.get('config') or {}
    m = b''
    m += pb_int64(1, war['id'])                                  # id
    m += pb_string(2, war.get('name') or '')                      # name
    m += pb_string(3, _fmt_ts(war.get('begintime')))              # begintime
    m += pb_string(4, _fmt_ts(war.get('endtime')))                # endtime（进行中=''）
    m += pb_string(5, '')                                         # web
    m += pb_string(6, '0')                                        # friendlyfire
    m += pb_int64(7, war.get('winning_faction_id') or 0)          # winningfactionid
    m += pb_int32(8, effective_points_needed(              # pointsNeededForVictory
        war.get('points_needed_for_victory')))
    m += pb_uint32(9, mm_config.WAR_AUTOMISSION_NORMAL)      # automissionnormal
    m += pb_uint32(10, mm_config.WAR_AUTOMISSION_SKIRMISH)   # automissionskirmish
    m += pb_uint32(11, mm_config.WAR_AUTOMISSION_SUPPLY)     # automissionsupply
    m += pb_float(12, float(cfg.get('strategytimescale',
                                    mm_config.WAR_TIMESCALE)))     # strategytimescale
    m += pb_float(13, float(cfg.get('daycycletimescale',
                                    mm_config.WAR_DAYCYCLE_TIMESCALE)))  # daycycletimescale
    m += pb_uint32(14, 0)                                         # disablestrategy
    m += pb_int32(15, int(cfg.get('maxPlayerPerFaction',
                                  mm_config.WAR_MAX_PLAYERS_PER_FACTION)))  # maxPlayerPerFaction
    m += pb_int64(16, war.get('sequel_war_id') or 0)              # sequelwarid
    m += pb_int64(17, war.get('mapid') or 1)                      # mapid
    m += pb_int32(18, 0)                                          # cleanupStatus
    # fow 网格参数（M3 §9.3）：非 0 即激活客户端 FogOfWarLayer，
    # 可见性判定在客户端本地；默认 0=全图可见
    m += pb_int32(19, mm_config.WAR_FOW_GRID_SIZE)                # fowGridSize
    m += pb_int32(20, mm_config.WAR_FOW_GRID_ORIGIN_X)            # fowGridOriginX
    m += pb_int32(21, mm_config.WAR_FOW_GRID_ORIGIN_Y)            # fowGridOriginY
    m += pb_int32(22, 0)                                          # queueReinforcements（M2）
    m += pb_int32(23, 0)                                          # firstResourceFree
    m += pb_uint32(24, int(morale['min']))                        # moraleMinValue
    m += pb_uint32(25, int(morale['max']))                        # moraleMaxValue
    m += pb_uint32(26, int(morale['lost_per_battle']))            # moraleLostPerBattle
    m += pb_uint32(27, int(morale['gained_per_win']))             # moraleGainedPerWin
    m += pb_float(28, float(morale['gained_per_minute']))          # moraleGainedPerMinute
    m += pb_uint32(29, int(morale['surrender_limit']))            # moraleSurrenderLimit
    return m


async def war_kvcs_proto(store):
    """当前战争的 war 行 proto；无战争（DB 不可用/未开战）返回 None，
    调用方回退静态 mock（views.handle_subscribe）。"""
    war = await ensure_war(store)
    if war is None:
        return None
    return build_war_proto(war)


# ---- war catalogue 聚合 ----

async def _list_wars(store):
    """目录用战争列表（新→旧，限量；走 store.list_wars 保持 DB-free 可测）。"""
    return await store.list_wars(mm_config.WAR_CATALOGUE_LIMIT)


def _count_resources(res, counts):
    """把单个 AT 的 resources 累进 counts dict。"""
    for cat, cnt in (res or {}).items():
        try:
            cat = int(cat)
            cnt = int(cnt)
        except (TypeError, ValueError):
            continue
        if cat in _SOLDIER_CATS:
            counts['infantry'] += cnt
        if cat in _TANK_CATS:
            counts['tank'] += cnt
        if cat in _GROUND_VEHICLE_CATS:
            counts['vehicle'] += cnt
        if cat in _PLANE_CATS:
            counts['plane'] += cnt


def _resource_totals(ats):
    counts = {'infantry': 0, 'tank': 0, 'vehicle': 0, 'plane': 0}
    for at in ats:
        _count_resources(_as_resources(at.get('resources')), counts)
    return counts


def _faction_stats(ats, city_owners, capitals):
    """三阵营统计 {faction: {...}}（warCatalogueFactions f4-19）。"""
    stats = {f: {'victory_points': 0, 'player_count': 0,
                 'controlled': 0, 'at_count': 0,
                 'infantry': 0, 'tank': 0, 'vehicle': 0, 'plane': 0}
             for f in (1, 2, 3)}
    for bf, owner in city_owners:
        if owner in stats:
            stats[owner]['controlled'] += 1
            if bf in capitals:
                stats[owner]['victory_points'] += 1
    players = {f: set() for f in (1, 2, 3)}
    for at in ats:
        f = int(at.get('faction') or 0)
        if f not in stats:
            continue
        stats[f]['at_count'] += 1
        if at.get('owner_player_id'):
            players[f].add(at['owner_player_id'])
        _count_resources(_as_resources(at.get('resources')), stats[f])
    for f in stats:
        stats[f]['player_count'] = len(players[f])
    return stats


def _msg_catalogue_faction(fid, s):
    """war_catalogue_factions 行 proto（messages._msg_war_catalogue_faction
    的动态版；factionTag 沿用官服缩写）。"""
    tag = {1: 'US', 2: 'GE', 3: 'SU'}[fid]
    m = b''
    m += pb_int64(1, fid)          # factionId
    m += pb_int64(2, fid)          # factionTemplateId（1/2/3 恒等）
    m += pb_string(3, tag)         # factionTag
    m += pb_int32(4, s['victory_points'])
    m += pb_int32(5, s['player_count'])
    m += pb_int32(6, 0)            # factionMinPlayerCount
    m += pb_int32(7, mm_config.WAR_MAX_PLAYERS_PER_FACTION)
    m += pb_int32(8, s['player_count'])   # factionPlayerOnlineCount（M1=有 AT 的玩家数）
    m += pb_int32(9, 0)            # factionBonus（M3 战争债券）
    m += pb_int32(10, s['infantry'])      # deployedCommandPointsInfantry
    m += pb_int32(11, s['tank'])          # deployedCommandPointsArmor
    m += pb_int32(12, s['plane'])         # deployedCommandPointsAir
    m += pb_int32(13, s['controlled'])    # factionControlledBattlefields
    m += pb_uint32(14, 0)         # battlesWon（M2 会战结算回写）
    m += pb_uint32(15, 0)         # battlesLost
    m += pb_uint32(16, 0)         # infantryLost
    m += pb_uint32(17, 0)         # vehiclesLost
    m += pb_uint32(18, 0)         # tanksLost
    m += pb_uint32(19, 0)         # planesLost
    return m


def _msg_catalogue(war, city_owners, ats, capitals):
    """单条 war_catalogue 行 proto（字段号见 docs/hng-rts-protocol.md §5.1）。"""
    stats = _faction_stats(ats, city_owners, capitals)
    total = _resource_totals(ats)
    m = b''
    m += pb_int64(1, war['id'])
    m += pb_string(2, war.get('name') or '')
    m += pb_string(3, _fmt_ts(war.get('begintime')))              # wartime
    m += pb_int32(4, effective_points_needed(
        war.get('points_needed_for_victory')))
    m += pb_int64(5, war.get('winning_faction_id') or 0)
    m += pb_int32(6, len(city_owners))                             # battlefieldCount
    m += pb_int32(7, len(ats))                                     # assaultTeamCount
    players = {at.get('owner_player_id') for at in ats
               if at.get('owner_player_id')}
    m += pb_int32(8, len(players))                                 # playerCount
    m += pb_int32(9, len(players))                                 # generalCount
    m += pb_uint32(10, total['infantry'])
    m += pb_uint32(11, total['tank'])
    m += pb_uint32(12, total['plane'])
    m += pb_uint32(13, total['vehicle'])
    m += pb_int64(14, war.get('sequel_war_id') or 0)
    for f in (1, 2, 3):
        m += pb_message(15, _msg_catalogue_faction(f, stats[f]))
    return m


async def build_war_catalogue_protos(store):
    """全量目录行 proto 列表（每场战争一条，新→旧）。

    返回 [bytes, ...]；DB 不可用返回 []（调用方回退静态 mock）。"""
    if store is None or getattr(store, 'pool', None) is None:
        return []
    try:
        wars = await _list_wars(store)
        capitals = capital_battlefield_ids()
        out = []
        for war in wars:
            city_owners = await store.get_war_cities(war['id'])
            ats = await store.list_war_command_nodes(war['id'])
            out.append(_msg_catalogue(war, city_owners, ats, capitals))
        return out
    except Exception as e:
        log(f'[war] build_war_catalogue failed: {e}')
        return []


async def build_war_catalogue_response_proto(store):
    """query_war_catalogue_response 完整 proto（f1 repeated + f2 bonusId）。

    无 DB 战争时返回 None（调用方回退静态 mock）。"""
    rows = await build_war_catalogue_protos(store)
    if not rows:
        return None
    m = b''
    for row in rows:
        m += pb_message(1, row)
    m += pb_int64(2, 0)   # bonusId（REQUIRED）
    return m


# ---- 胜利判定 ----

async def check_victory(store, war=None):
    """检查当前战争胜利条件：某阵营占领首都数 ≥ pointsNeededForVictory。

    达成 → end_war（winning_faction）+ MM_WAR_SEQUEL_AUTO 时自动开续战
    （复用 ensure_war）。返回胜方 faction id；未达成返回 None。"""
    if store is None or getattr(store, 'pool', None) is None:
        return None
    try:
        if war is None:
            war = await store.get_active_war()
        if war is None:
            return None
        counts = await store.count_war_city_owners(
            war['id'], capital_battlefield_ids())
        needed = effective_points_needed(war.get('points_needed_for_victory'))
        winner = 0
        for faction in (1, 2, 3):
            if counts.get(faction, 0) >= needed:
                winner = faction
                break
        if not winner:
            return None
        sequel = 0
        await store.end_war(war['id'], winner)
        if mm_config.WAR_SEQUEL_AUTO:
            nxt = await ensure_war(store)
            if nxt is not None:
                sequel = nxt['id']
                await store.update_war_sequel(war['id'], sequel)
        log(f'[war] 战争 {war["id"]} 结束：阵营 {winner} 占领 '
            f'{counts.get(winner, 0)} 首都 ≥ {needed}，sequel={sequel}')
        return winner
    except Exception as e:
        log(f'[war] check_victory failed: {e}')
        return None


# ---- warmapview 快照（Phase 4，docs/hng-rts-protocol.md §4.3 时序）----
# 推送顺序 = WarMapViewMediator.setupWar 图层初始化顺序：
#   battlefieldstatus → supplylinestatus → accesspointstatus →
#   CommandNodeDefinition + CommandNodeWarInstance → CommandNodeMorale →
#   battle → armyresource → transport_commandnode_segment/destination
# ⚠ xmlTag 大小写逐字符匹配（硬约束 §7.4）；f1 id 为 SFIXED64 的表用
#   pb_sfixed64（硬约束 §7.5）；mapEntityTypeId 必须 ∈ {1,2,3}（§7.2）。

# mapEntityTypeId 枚举（client MapEntityType）
MAP_ENTITY_BATTLEFIELD = 1
MAP_ENTITY_ACCESSPOINT = 2
MAP_ENTITY_SUPPLYLINE = 3


def _faction_capital_bf():
    """{faction: capital battlefield_id}（standby AT 落位用）。"""
    out = {}
    for c in wardb.capitals():
        if c.get('default_faction') and c.get('battlefield_id'):
            out.setdefault(c['default_faction'], c['battlefield_id'])
    return out


def _ts_ms(dt):
    """datetime → 毫秒时间戳（无值回 0）。"""
    if not dt:
        return 0
    if isinstance(dt, (int, float)):
        return int(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


def msg_battlefieldstatus_row(war_id, battlefield_id, owner):
    """battlefieldstatus 行（f1 id 为 S64）。"""
    m = b''
    m += pb_sfixed64(1, battlefield_id)          # id（=battlefield id，战内唯一）
    m += pb_int64(2, war_id)                     # warid
    m += pb_int64(3, battlefield_id)             # battlefieldid
    m += pb_int64(4, owner)                       # factionid（1/2/3）
    return m


def msg_supplylinestatus_rows(war_id, city_owners):
    """supplylinestatus 行列表：两端城市同属一阵营的补给线才推（中立不推）。

    city_owners：{battlefield_id: faction}。"""
    out = []
    for sl in wardb.supplylines():
        o = city_owners.get(_ap_battlefield(sl.get('ap1')))
        o2 = city_owners.get(_ap_battlefield(sl.get('ap2')))
        if o and o == o2:
            out.append(pb_sfixed64(1, sl['id'])      # id（=supplyline id）
                      + pb_int64(2, war_id)
                      + pb_int64(3, o)               # factionid
                      + pb_int64(4, sl['id']))       # supplylineid
    return out


def _ap_battlefield(ap_id):
    """accesspoint id → 所属 battlefield id（0=未知）。"""
    for ap in wardb.accesspoints():
        if ap['id'] == ap_id:
            return ap.get('battlefield') or 0
    return 0


def msg_accesspointstatus_rows(war_id, city_owners):
    """accesspointstatus 行列表：所属城市有归属的出兵点才推。"""
    out = []
    for ap in wardb.accesspoints():
        owner = city_owners.get(ap.get('battlefield'))
        if owner:
            out.append(pb_sfixed64(1, ap['id'])      # id（=accesspoint id）
                      + pb_int64(2, war_id)
                      + pb_int64(3, ap['id'])        # accesspointid
                      + pb_int64(4, owner))          # factionid
    return out


def _at_map_entity(at, faction_capitals):
    """AT 行的 (mapEntityId, mapEntityTypeId)。

    显式 map_entity_type ∈{1,2,3}（Phase 6 在途补给线/出兵点落位）优先；
    已部署/驻扎 → 所在 battlefield（type 1）；map_entity_id=0（首都待命）
    → 落位己方首都（type 1）；无 WarDatabase 首都数据 → (0, 3) 空位置
    （客户端 location='' 分支直接 return，登录卡死教训的避让路径）。"""
    ent_type = int(at.get('map_entity_type') or 0)
    ent = int(at.get('map_entity_id') or 0)
    if ent_type in (MAP_ENTITY_BATTLEFIELD, MAP_ENTITY_ACCESSPOINT,
                    MAP_ENTITY_SUPPLYLINE):
        return ent, ent_type
    if ent:
        return ent, MAP_ENTITY_BATTLEFIELD
    cap = faction_capitals.get(int(at.get('faction') or 0))
    if cap:
        return cap, MAP_ENTITY_BATTLEFIELD
    return 0, MAP_ENTITY_SUPPLYLINE


def msg_commandnode_definition_row(at):
    """CommandNodeDefinition 行（AT 定义；id 用 command_nodes.id）。"""
    m = b''
    m += pb_int64(1, at['id'])                       # id
    m += pb_string(2, at.get('name') or '')          # name
    m += pb_string(3, at.get('abbreviation') or '') # abbreviation
    m += pb_int64(4, at.get('owner_player_id') or 0)  # ownerPlayerId
    m += pb_int64(5, at.get('faction') or 0)        # factionTemplateId（1/2/3）
    m += pb_int64(6, at.get('template_id') or 0)    # commandNodeTemplateId
    m += pb_int32(7, int(at.get('rank') or 0))          # rank（0 起，客户端升级门槛）
    m += pb_int64(9, at.get('general_character_id') or 0)  # generalCharacterId
    # statUpdateTimeStamp：stats_updated_at 随 rank/xp/battles 变化刷新 →
    # 客户端 statsHaveChanged 重拉 GetAssaultTeamStats（无值回退 created_at）
    m += pb_int64(10, _ts_ms(at.get('stats_updated_at')
                             or at.get('created_at')))
    # suppliedOnCreation：官服买壳口径恒 False（购买即满编已废弃，见 §10.2 #4）
    m += pb_bool(11, False)                          # suppliedOnCreation
    return m


def msg_commandnode_instance_row(at, war_id, faction_capitals):
    """CommandNodeWarInstance 行（地图上的 AT 实例）。
    f3 parentCommandNodeInstanceId：补给队（Reserve）指向被补给的父 AT
    （客户端 AssaultTeamModel.updateArmyResources 按此把补给队载量汇总
    进父 AT 的 resourceOne/TwoOnTheWay，官服汇合链）。"""
    ent, ent_type = _at_map_entity(at, faction_capitals)
    m = b''
    m += pb_int64(1, at['id'])                       # id
    m += pb_int64(2, at['id'])                       # commandNodeDefinitionId（M1 同 id）
    m += pb_int64(3, at.get('parent_command_node_id') or 0)  # parentCommandNodeInstanceId
    m += pb_int64(4, at.get('faction') or 0)         # factionId
    m += pb_int64(5, ent)                            # mapEntityId
    m += pb_int64(6, ent_type)                      # mapEntityTypeId（1/2/3 硬约束）
    m += pb_int64(7, war_id)                         # warId
    m += pb_uint32(8, at.get('morale') or 0)         # morale
    m += pb_int64(9, _ts_ms(at.get('created_at')))   # lastReset
    m += pb_uint32(10, 1 if at.get('is_deployed') else 0)  # isDeployed
    m += pb_bool(11, bool(at.get('has_gotten_soldier_free')))  # hasGottenSoldierResourcesForFree
    m += pb_bool(12, bool(at.get('has_gotten_vehicle_free')))  # hasGottenVehicleResourcesForFree
    m += pb_uint32(13, at.get('max_morale') or 100)  # maxMorale
    m += pb_float(14, 1.0)                           # moraleGainModifier
    return m


def msg_armyresource_rows(at, war_id):
    """armyresource 行列表（AT 各资源类别一行，id = node_id*1000+cat）。"""
    out = []
    for cat, cnt in sorted(_as_resources(at.get('resources')).items()):
        try:
            cat, cnt = int(cat), int(cnt)
        except (TypeError, ValueError):
            continue
        if not cnt:
            continue
        m = b''
        m += pb_int64(1, at['id'] * 1000 + cat)      # id
        m += pb_int64(2, at['id'])                   # commandNodeWarInstanceId
        m += pb_int64(3, cat)                        # armyResourceCategoryId
        m += pb_uint32(4, cnt)                       # count
        m += pb_int64(5, war_id)                     # warid
        out.append(m)
    return out


def msg_hq_resource_rows(war_id, hq_id, stock):
    """阵营最高司令部 armyresource 行（阵营库存本体，§3.12.1：
    FactionResourceOverviewMediator 库存列数据源）。

    stock={category_id: count}。id = (hq_id & ~0xFF) | cat：HQ id 是 63 位
    随机数（matchmaking.FACTION_SUPREME_COMMANDS），*1000 惯例会溢出 int64；
    低 8 位塞 cat（1-21 < 255）保证 (hq,cat) 唯一且不越界。"""
    out = []
    base = int(hq_id) & ~0xFF
    for cat, cnt in sorted((stock or {}).items()):
        try:
            cat, cnt = int(cat), int(cnt)
        except (TypeError, ValueError):
            continue
        if cnt <= 0:
            continue
        m = b''
        m += pb_int64(1, base | cat)                 # id
        m += pb_int64(2, int(hq_id))                 # commandNodeWarInstanceId（=faction.supremecommand）
        m += pb_int64(3, cat)                        # armyResourceCategoryId
        m += pb_uint32(4, cnt)                       # count
        m += pb_int64(5, war_id)                     # warid
        out.append(m)
    return out


async def hq_stock_rows(store, war=None):
    """全阵营最高司令部 armyresource 行列表（库存本体）。

    production_tick 周期重推与 views subscriberesourceview 订阅首推共用；
    store 不可用 / 无战争 / 库存读失败回 []。"""
    try:
        war = war or await ensure_war(store)
        if war is None:
            return []
        stock = {}
        for r in await store.list_war_faction_resources(war['id']):
            stock.setdefault(int(r['faction']), {})[
                int(r['category_id'])] = int(r['count'])
        import matchmaking          # 延迟 import（防循环依赖）
        rows = []
        for fid, cats in stock.items():
            hq = matchmaking.FACTION_SUPREME_COMMANDS.get(fid)
            if hq:
                rows.extend(msg_hq_resource_rows(war['id'], hq, cats))
        return rows
    except Exception as e:
        log(f'[war] hq_stock_rows failed: {e}')
        return []


def msg_commandnode_morale_row(at, war_id):
    """CommandNodeMorale 行（f2 playerId=AT 所有者，M1 每 AT 一行）。"""
    m = b''
    m += pb_int64(1, at['id'])                      # id
    m += pb_int64(2, at.get('owner_player_id') or 0)  # playerId
    m += pb_int64(3, at['id'])                      # commandNodeWarInstanceId
    m += pb_int64(4, war_id)                         # warId
    m += pb_string(5, _fmt_ts(at.get('created_at')))  # moraleStartTimeStamp
    return m


async def build_warmap_snapshot(store, war=None):
    """warmapview 全量快照行 [(xmlTag, row_proto), ...]（推送序=§4.3）。

    DB 不可用/无战争返回 []（调用方仅回 subscriberesponse）。
    battle/transport 段 M1 无数据（会战成局 Phase 6）。"""
    if store is None or getattr(store, 'pool', None) is None:
        return []
    try:
        if war is None:
            war = await ensure_war(store)
        if war is None:
            return []
        war_id = war['id']
        city_owners = dict(await store.get_war_cities(war_id))
        ats = await store.list_war_command_nodes(war_id)
        faction_capitals = _faction_capital_bf()
        rows = []
        for bf in sorted(city_owners):
            owner = city_owners[bf]
            if owner:
                rows.append((b'battlefieldstatus',
                             msg_battlefieldstatus_row(war_id, bf, owner)))
        for p in msg_supplylinestatus_rows(war_id, city_owners):
            rows.append((b'supplylinestatus', p))
        for p in msg_accesspointstatus_rows(war_id, city_owners):
            rows.append((b'accesspointstatus', p))
        for at in ats:
            rows.append((b'CommandNodeDefinition',
                         msg_commandnode_definition_row(at)))
        for at in ats:
            rows.append((b'CommandNodeWarInstance',
                         msg_commandnode_instance_row(at, war_id,
                                                       faction_capitals)))
        for at in ats:
            rows.append((b'CommandNodeMorale',
                         msg_commandnode_morale_row(at, war_id)))
        for at in ats:
            for p in msg_armyresource_rows(at, war_id):
                rows.append((b'armyresource', p))
        # 已部署空中半径单位（§10.2 #6/#7）：airbase 行（客户端
        # isAirborneUnit 判定 + 半径规则；基地 = 当前驻地）
        for at in ats:
            if at.get('is_deployed') and is_radius_unit(at):
                rows.append((b'air_commandnode_base',
                             msg_air_commandnode_base_row(
                                 at, war_id,
                                 at_current_battlefield(at))))
        # 在途空运行（订阅时已起飞的 AT；§3.14 air_transport）
        for mv in await store.list_active_movements(war_id):
            segs = movement_segments(mv)
            if segs and segs[0].get('air'):
                mvat = await store.get_command_node(mv['commandnode_id'])
                if mvat:
                    rows.append((b'air_transport',
                                 msg_air_transport_row(mvat, war_id, mv,
                                                       segs[0])))
        return rows
    except Exception as e:
        log(f'[war] build_warmap_snapshot failed: {e}')
        return []


# ---- AT 管理业务逻辑（Phase 5，docs/hng-rts-protocol.md §5.2）----

# ResponseCodes（HnG_States/ResponseCodes.as，AT 生命周期相关子集）
RC_OK = 1
RC_CHARACTER_NOT_FOUND = 15
RC_NO_NAME = 53
RC_INVALID_NAME = 36
RC_WAR_NOT_FOUND = 77
RC_COMMANDNODE_TEMPLATE_NOT_FOUND = 106
RC_COMMANDNODE_WAR_INSTANCE_NOT_FOUND = 107
RC_COMMANDNODE_NOT_IN_QUEUE = 111
RC_COMMANDNODE_LIMIT_REACHED = 114
RC_COMMANDNODE_ALREADY_DEPLOYED = 150
RC_NOT_ENOUGH_WARFUNDS = 153
RC_INVALID_OWNER = 189
RC_INVALID_CURRENCY = 240
# SetCommandNodeEquipment（ResponseCodes.as 实锤）
RC_COMMANDNODE_ALREADY_EQUIPPED = 217
RC_COMMANDNODE_ALREADY_UNEQUIPPED = 218
RC_EQUIPMENT_NOT_ALLOWED = 219
RC_COMMANDNODE_QUEUED_FOR_RESOURCES = 226
# resupply_request（no_resupply_* 族）
RC_NO_RESUPPLY_IN_BATTLE = 101
RC_NO_RESUPPLY_INSUFFICIENT_RESOURCES = 102
RC_NO_RESUPPLY_ALREADY_FULL = 103
RC_NO_RESUPPLY_IN_TRANSPORT = 116

# AT 名/缩写长度上限（官服 UI 语义：min 3 校验在客户端，服务端同口径拦截）
AT_NAME_MAX_LENGTH = 20
AT_ABBR_MAX_LENGTH = 10


def at_name_error(name, abbr):
    """AT 名/缩写校验 → ResponseCodes 错误码（0=合法）。

    官方口径：trim 后空 → no_name(53)；长度 <3 或 >上限 → invalid_name(36)。"""
    for s, max_len in ((name, AT_NAME_MAX_LENGTH),
                       (abbr, AT_ABBR_MAX_LENGTH)):
        t = (s or '').strip()
        if not t:
            return RC_NO_NAME
        if len(t) < 3 or len(t) > max_len:
            return RC_INVALID_NAME
    return 0


async def validate_create_at(store, war, player_id, faction, name, abbr,
                             template_id, has_command_points=True):
    """购买 AT 前校验。返回 (error_code, template)；0=通过。

    校验链：指挥点角色（MM_WAR_AT_REQUIRE_COMMAND_POINTS，官服口径 =
    commandPointsUnlocked > 0，将军默认 3 点 / 普通角色军衔 12+ 起有，
    客户端无 isGeneral 检查）→ 名字 → 模板存在且阵营匹配 → 持有数上限
    （MM_WAR_AT_MAX）。"""
    if mm_config.WAR_AT_REQUIRE_COMMAND_POINTS and not has_command_points:
        return RC_CHARACTER_NOT_FOUND, None
    code = at_name_error(name, abbr)
    if code:
        return code, None
    tpl = wardb.commandnode_templates().get(int(template_id or 0))
    if not tpl:
        return RC_COMMANDNODE_TEMPLATE_NOT_FOUND, None
    tf = int(tpl.get('faction') or 0)
    if faction in (1, 2, 3) and tf not in (0, faction):
        return RC_COMMANDNODE_TEMPLATE_NOT_FOUND, None
    try:
        ats = await store.list_player_command_nodes(player_id, war['id'])
    except Exception:
        ats = []
    if len(ats) >= mm_config.WAR_AT_MAX:
        return RC_COMMANDNODE_LIMIT_REACHED, None
    return 0, tpl


def template_resources(tpl):
    """AT 模板编制资源 {cat: count}（int 键；command_nodes.resources JSON 用 str 键）。"""
    return {int(cat): int(cnt)
            for cat, cnt in (tpl.get('resources') or {}).items()}


def template_rank(tpl):
    """新 AT 初始 rank（commandnodetemplate f10 level：基础模板 guard/
    light_armor 为 0，升级链目标 1/2——客户端升级门槛 rank >= level）。"""
    return int(tpl.get('level') or 0)


def rank_from_xp(xp):
    """AT XP → 等级（AssaultTeamLevel 阈值：level N 需 xp>=阈值[N]，
    0 XP = rank 0；与客户端 ExperienceCalculation 区间算法同构）。
    缺表回退 350/3450/9200 三档。"""
    thresholds = wardb.at_levels() or [(1, 350), (2, 3450), (3, 9200)]
    rank = 0
    for level, need in thresholds:
        if int(xp) >= int(need):
            rank = int(level)
    return rank


# ---- underdog 加成（#1 官方玩法：人数较少阵营经验/战资加成）----

WAR_FACTIONS = (1, 2, 3)


def underdog_multipliers(counts):
    """各阵营 (xp_mult, wf_mult)：人数相对最大阵营线性插值。

    官方证据：faction 行 f6 underdogBonusXpMultiplier / f7
    underdogBonusWarfundsMultiplier（float，FactionOption.as 按 >1 显示
    "当前弱势阵营加成"）。mult = 1 + cap × (1 - n/n_max)；并列最多=1.0，
    无人阵营拿满加成（吸引玩家填线）。counts 缺阵营按 0 人计。"""
    ns = [int((counts or {}).get(f, 0) or 0) for f in WAR_FACTIONS]
    cmax = max(ns) if ns else 0
    out = {}
    for f, n in zip(WAR_FACTIONS, ns):
        if cmax <= 0 or n >= cmax:
            out[f] = (1.0, 1.0)
        else:
            share = 1.0 - n / float(cmax)
            out[f] = (
                round(1.0 + (mm_config.WAR_UNDERDOG_XP_BONUS or 0) * share, 4),
                round(1.0 + (mm_config.WAR_UNDERDOG_WF_BONUS or 0) * share, 4))
    return out


async def faction_player_counts(store, war_id):
    """战争中各阵营人数（AT 持有玩家 owner 去重；软删 AT 不计）。"""
    counts = {f: 0 for f in WAR_FACTIONS}
    seen = {}
    try:
        ats = await store.list_war_command_nodes(int(war_id))
    except Exception:
        return counts
    for at in ats:
        if int(at.get('state') or 0) >= 3:
            continue
        fac = int(at.get('faction') or 0)
        if fac not in counts:
            continue
        seen.setdefault(fac, set()).add(
            int(at.get('owner_player_id') or 0))
    for fac, players in seen.items():
        counts[fac] = len(players)
    return counts


async def current_underdog_mults(store):
    """当前战争 underdog 倍率 {faction: (xp, wf)}；无战争/异常回退全 1.0。"""
    try:
        war = await ensure_war(store)
        if war is None:
            return {f: (1.0, 1.0) for f in WAR_FACTIONS}
        return underdog_multipliers(
            await faction_player_counts(store, war['id']))
    except Exception as e:
        log(f'[war] underdog mults failed: {e}')
        return {f: (1.0, 1.0) for f in WAR_FACTIONS}


def next_level_xp(rank):
    """下一等级 XP 阈值（rank 已满级返回 0）。GetAssaultTeamStatsRecord f4。"""
    thresholds = wardb.at_levels() or [(1, 350), (2, 3450), (3, 9200)]
    for level, need in thresholds:
        if int(level) > int(rank):
            return int(need)
    return 0


def dissolve_refund(tpl):
    """解散返还 warfunds = 模板定价 × MM_WAR_DISSOLVE_REFUND（无定价回 0）。"""
    price = wardb.at_pricing().get(int(tpl['id'])) or {}
    return int((price.get('warfunds') or 0) * mm_config.WAR_DISSOLVE_REFUND)


def upgrade_allowed(cur_tpl, target_tpl):
    """升级链校验（对齐客户端 AssaultTeamUpgradeOptions.getUpgradeOption）：
    同阵营，且目标模板 parent_template_id == 当前模板 id（或当前模板
    convert_to == 目标 id，两方向任一命中即官服升级关系）。

    特例：motorized_guard → motorized_infantry 无 parent 链也合法
    （客户端 HnGGUIGameVariables 模板特例；wardb 三阵营同名，
    316→320 / 116→120 / 216→220）。"""
    if not cur_tpl or not target_tpl:
        return False
    cur_id, tgt = int(cur_tpl['id']), int(target_tpl['id'])
    if tgt == cur_id:
        return False
    if int(cur_tpl.get('faction') or 0) != int(target_tpl.get('faction') or 0):
        return False
    if int(target_tpl.get('parent_template_id') or 0) == cur_id:
        return True
    if int(cur_tpl.get('convert_to') or 0) == tgt:
        return True
    return (str(cur_tpl.get('name') or '') == 'motorized_guard'
            and str(target_tpl.get('name') or '') == 'motorized_infantry')


RC_DEPLOY_LOCATION_NOT_FOUND = 221    # 官服：主城交战/无可用落位城


def faction_capital_bfs(faction):
    """阵营全部首都 battlefield id 集合（部署 UI 玩家可选项，
    AssaultTeamReinforcementWindowMediator：CapitalTable 己方首都）。"""
    return {int(c['battlefield_id']) for c in wardb.capitals()
            if c.get('battlefield_id')
            and int(c.get('default_faction') or 0) == int(faction or 0)}


async def _battling_battlefields(store, war_id):
    """交战城市集合 {battlefield_id}（活跃 war battle 所在城市）。
    存储无该接口/异常时回退空集（视为全域非交战，维持可部署）。"""
    try:
        rows = await store.list_active_war_battles(war_id)
    except Exception:
        return set()
    out = set()
    for b in rows or ():
        try:
            bid = int(b.get('battlefield_id') or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if bid:
            out.add(bid)
    return out


async def resolve_deploy_battlefield(store, war, at, near_capital_bf=0):
    """官服部署落位（§10.2 #4/#5）：主城非交战，且周边至少一座
    非交战己方城市 → random.choice(主城 ∪ 周边非交战己方城)。

    near_capital_bf（请求 f4 玩家所选首都）为本阵营首都时优先生效，
    否则回退阵营默认主城。返回 (battlefield, 0)=成功；失败
    (0, 221)=主城交战/周边无己方非交战城（官服 deploy_location_not_
    found）、(0, 7)=无主城。"""
    fac = int(at.get('faction') or 0)
    capital = int(near_capital_bf or 0)
    if capital not in faction_capital_bfs(fac):
        capital = _faction_capital_bf().get(fac, 0)
    if not capital:
        return 0, RC_CANNOT_FIND_COMMANDNODE
    battling = await _battling_battlefields(store, war['id'])
    if capital in battling:
        return 0, RC_DEPLOY_LOCATION_NOT_FOUND
    try:
        owners = dict(await store.get_war_cities(war['id']))
    except Exception:
        owners = {}
    cands = [capital]
    for edge in supplyline_adjacency().get(capital, ()):
        nb = int(edge[3] or 0)
        if nb in battling or nb == capital:
            continue
        try:
            if int(owners.get(nb, 0) or 0) != fac:
                continue
        except (TypeError, ValueError):
            continue
        cands.append(nb)
    if len(cands) < 2:
        # 官服口径：周边须有一座非交战己方城市（含落位随机池来源）
        return 0, RC_DEPLOY_LOCATION_NOT_FOUND
    if is_radius_unit(at):
        # §10.2 #7：飞机/伞兵（airborne）只出生在机场——候选池 ∩ 机场；
        # 空池回退距主城最近的机场（官服"主城附近的机场"口径，不查归属，
        # fac1 首都邻接无机场的既成事实）
        af_cands = [c for c in cands if is_airfield(c)]
        if not af_cands:
            near = nearest_airfield(capital)
            if not near:
                return 0, RC_DEPLOY_LOCATION_NOT_FOUND
            af_cands = [near]
        return random.choice(af_cands), 0
    return random.choice(cands), 0


# ---- AT 移动（Phase 6，docs/hng-rts-protocol.md §5.3）----

# 0=fail 语义族的通用失败码（SetCommandNodeEquipmentResponse 实锤 0=fail）；
# transport 族响应 0=ok，禁用本码表达失败（用 7/33/109/152 等非 0 实码）
RC_FAIL = 0
RC_CANNOT_FIND_COMMANDNODE = 7
RC_ALREADY_IN_TRANSPORT = 33
RC_COMMANDNODE_IN_BATTLE = 109
RC_COMMANDNODE_NOT_DEPLOYABLE = 152
# 官服移动成功语义码（CommandnodeArrow.transportCommandNodeResponse 全按成功处理）
RC_RELOCATING_TO_NEW_AIRBASE = 21
RC_TRANSPORTING_TO_ATTACK_SUPPLY = 22
RC_TRANSPORTING_TO_JOIN_BATTLE = 23
RC_TRANSPORTING_TO_JOIN_ONGOING_BATTLE = 24

# 无坐标数据时单段回退时长（秒）
SEGMENT_FALLBACK_SECONDS = 300

_ADJACENCY_CACHE = None
_BF_POS_CACHE = None


def supplyline_adjacency():
    """battlefield 邻接表 {bf: [(supplyline_id, from_ap, to_ap, 对端 bf)]}。

    supplyline 两端 accesspoint 各归属一个 battlefield，构成城市图的边。"""
    global _ADJACENCY_CACHE
    if _ADJACENCY_CACHE is None:
        ap_by_id = {ap['id']: ap for ap in wardb.accesspoints()}
        adj = {}
        for sl in wardb.supplylines():
            a = ap_by_id.get(sl.get('ap1'))
            b = ap_by_id.get(sl.get('ap2'))
            if not a or not b:
                continue
            bf1, bf2 = a.get('battlefield'), b.get('battlefield')
            if not bf1 or not bf2 or bf1 == bf2:
                continue
            adj.setdefault(bf1, []).append(
                (sl['id'], sl['ap1'], sl['ap2'], bf2))
            adj.setdefault(bf2, []).append(
                (sl['id'], sl['ap2'], sl['ap1'], bf1))
        _ADJACENCY_CACHE = adj
    return _ADJACENCY_CACHE


def find_supplyline_path(from_bf, to_bf):
    """BFS 最短补给线路径 → [{sl_id, from_ap, to_ap, from_bf, to_bf}]。

    起终点相同返回 []；不可达返回 None。"""
    from_bf, to_bf = int(from_bf or 0), int(to_bf or 0)
    if from_bf == to_bf:
        return []
    adj = supplyline_adjacency()
    prev = {from_bf: None}          # bf → (来源 bf, 边)
    queue = [from_bf]
    while queue:
        cur = queue.pop(0)
        for edge in adj.get(cur, ()):
            nb = edge[3]
            if nb in prev:
                continue
            prev[nb] = (cur, edge)
            if nb == to_bf:
                path = []
                node = to_bf
                while prev[node] is not None:
                    p, e = prev[node]
                    path.append({'sl_id': e[0], 'from_ap': e[1],
                                 'to_ap': e[2], 'from_bf': p, 'to_bf': node})
                    node = p
                path.reverse()
                return path
            queue.append(nb)
    return None


def _bf_pos():
    global _BF_POS_CACHE
    if _BF_POS_CACHE is None:
        _BF_POS_CACHE = {b['id']: (b['posx'], b['posy'])
                         for b in wardb.battlefields()}
    return _BF_POS_CACHE


def at_speed(at):
    """AT 模板移动速度（commandnodetemplate f4 speed；缺省回退 1.0）。"""
    tpl = wardb.commandnode_templates().get(int(at.get('template_id') or 0)) or {}
    try:
        return float(tpl.get('speed') or 0) or 1.0
    except (TypeError, ValueError):
        return 1.0


def segment_seconds(from_bf, to_bf, speed, faction=0, owners=None):
    """单段移动时长（秒）= 城市坐标距离 / (speed × timescale × move_scale × 地形系数)。

    官服语义：步兵 AT 数小时跨越半张图；speed 0.45-5.6（实测表值）、
    城市间典型距离 1100-6400 坐标单位，默认参数下首都对首都 ~1-4 小时。
    地形减速（#2，仅地面补给线段，air 段不适用）：
    - 跨海段：距离 ≥ WAR_SEA_DISTANCE（实测欧洲图内陆最长 505、
      551+ 全为丹麦↔挪威海峡线）→ × WAR_SEA_SPEED_FACTOR；
    - 中立段：段两端城市归属均非己方（owners 提供）→ × WAR_NEUTRAL_SPEED_FACTOR；
    - 同时命中取更小系数（"速度大大降低"）。"""
    p = _bf_pos()
    from_bf, to_bf = int(from_bf or 0), int(to_bf or 0)
    a, b = p.get(from_bf), p.get(to_bf)
    if not a or not b:
        return SEGMENT_FALLBACK_SECONDS
    import math
    dist = math.hypot(a[0] - b[0], a[1] - b[1])
    factor = 1.0
    thr = float(mm_config.WAR_SEA_DISTANCE or 0)
    if thr > 0 and dist >= thr:
        factor = float(mm_config.WAR_SEA_SPEED_FACTOR or 1.0)
    fac = int(faction or 0)
    if fac and owners:
        o1 = int((owners.get(from_bf) or 0) or 0)
        o2 = int((owners.get(to_bf) or 0) or 0)
        if o1 != fac and o2 != fac:
            factor = min(factor,
                         float(mm_config.WAR_NEUTRAL_SPEED_FACTOR or 1.0))
    denom = (float(speed or 1.0) * (mm_config.WAR_TIMESCALE or 1.0)
             * (mm_config.WAR_AT_MOVE_SCALE or 1.0) * factor)
    if denom <= 0:
        denom = 1.0
    return max(1.0, dist / denom)


def at_current_battlefield(at):
    """AT 当前驻扎 battlefield id（type1=本身；type2=出兵点所属；
    type3=在途（新移动被 33 挡，仅回退用）→ 补给线起端 0）。"""
    ent, ent_type = _at_map_entity(at, _faction_capital_bf())
    if ent_type == MAP_ENTITY_BATTLEFIELD and ent:
        return ent
    if ent_type == MAP_ENTITY_ACCESSPOINT and ent:
        bf = _ap_battlefield(ent)
        if bf:
            return bf
    return 0


def movement_segments(mv):
    """movement 行 → 带绝对毫秒时间的段列表（started_at 逐段累加）。"""
    start_ms = _ts_ms(mv.get('started_at'))
    out = []
    t = start_ms
    for seg in mv.get('path') or []:
        try:
            dur = int(float(seg.get('seconds') or 0) * 1000)
        except (TypeError, ValueError):
            dur = 0
        s = dict(seg)
        s['begin_ms'] = t
        s['end_ms'] = t + dur
        out.append(s)
        t += dur
    return out


def current_segment_index(segs, now_ms):
    """按时间定位当前段（末段封口：now 超过最后 end 也返回最后一段）。"""
    if not segs:
        return -1
    for i, s in enumerate(segs):
        if now_ms < s['end_ms']:
            return i
    return len(segs) - 1


async def start_movement(store, at, target_bf, override_ap=0,
                         override_move=False, now=None):
    """发起 AT 移动。返回 (movement, error_code)。

    校验在调用方（handler）；本函数负责寻路、时长计算、movement 落库与
    AT 置在途（map_entity → 首段补给线）。同地目标返回 (None, RC_OK)。"""
    cur_bf = at_current_battlefield(at)
    target_bf = int(target_bf or 0)
    if not cur_bf or not target_bf:
        return None, RC_CANNOT_FIND_COMMANDNODE
    if cur_bf == target_bf and not override_ap:
        return None, RC_OK
    path = find_supplyline_path(cur_bf, target_bf)
    if path is None:
        return None, RC_CANNOT_FIND_COMMANDNODE
    speed = at_speed(at)
    try:
        owners = dict(await store.get_war_cities(at['war_id']))
    except Exception:
        owners = {}
    fac = int(at.get('faction') or 0)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    for seg in path:
        seg['seconds'] = (0 if override_move
                          else segment_seconds(seg['from_bf'], seg['to_bf'],
                                               speed, fac, owners))
    arrive = now + datetime.timedelta(
        seconds=sum(s['seconds'] for s in path))
    mv = await store.create_movement(at['id'], at['war_id'], path,
                                     started_at=now, arrive_at=arrive,
                                     dest_ap=int(override_ap or 0))
    if path:
        await store.update_command_node(
            at['id'], state=1, map_entity_id=path[0]['sl_id'],
            map_entity_type=MAP_ENTITY_SUPPLYLINE)
    else:
        await store.finish_movement(mv['id'], 1)
        if override_ap:
            # 同城出兵点挪位：无在途动画，立即落位到目标出兵点
            await store.update_command_node(
                at['id'], map_entity_id=int(override_ap),
                map_entity_type=MAP_ENTITY_ACCESSPOINT)
    return mv, RC_OK


async def start_air_transport(store, at, target_bf, now=None):
    """空运（M3 §9.4）：地面 BFS 不可达时，携运输机（resources cat 10 ≥1）
    的 AT 可空运直达目标城市：消耗 1 架运输机 + 单段 air 移动
    （时长 mm_config.WAR_AIR_TRANSIT_SECONDS）。

    返回 (movement, error_code)。movement 非 None 时段带 air=True：
    AT state=1 在途但 map_entity 保持出发城市（instance 行不变位，
    在途动画由 air_transport 行驱动）；到达走 advance_movements 通用
    落位/会战成局逻辑。"""
    cur_bf = at_current_battlefield(at)
    target_bf = int(target_bf or 0)
    if not cur_bf or not target_bf or cur_bf == target_bf:
        return None, RC_CANNOT_FIND_COMMANDNODE
    res = dict(_as_resources(at.get('resources')))
    try:
        planes = int(res.get('10') or res.get(10) or 0)
    except (TypeError, ValueError):
        planes = 0
    if planes < 1:
        return None, RC_CANNOT_FIND_COMMANDNODE
    res['10'] = planes - 1
    now = now or datetime.datetime.now(datetime.timezone.utc)
    seconds = max(1, int(mm_config.WAR_AIR_TRANSIT_SECONDS or 600))
    seg = {'from_bf': cur_bf, 'to_bf': target_bf, 'sl_id': 0,
           'from_ap': 0, 'to_ap': 0, 'seconds': seconds, 'air': True}
    mv = await store.create_movement(
        at['id'], at['war_id'], [seg], started_at=now,
        arrive_at=now + datetime.timedelta(seconds=seconds))
    await store.update_command_node(at['id'], state=1, resources=res)
    return mv, RC_OK


# ---- 空中半径单位（官服 §10.2 #6/#7，CommandnodeArrow.detectBattleField）----

# 机场判定（swf BattlefieldSchema.isAirfield 实锤：gamemap 204/205）
AIRFIELD_GAMEMAPS = (204, 205)
# transportradius 无限哨兵（表内 -1 → uint64 max = 2^63-1 量级）
RADIUS_INFINITE = 1 << 63
# 空中单位模板 actionid（p0 勘查：12/13 伞兵族、14/15 飞机族）
PARADROP_ACTIONIDS = (12, 13)
AIRCRAFT_ACTIONIDS = (14, 15)

_AIRFIELD_CACHE = None


def airfield_bfs():
    """机场 battlefield id 集合（gamemap ∈ 204/205；勘查 127 座）。"""
    global _AIRFIELD_CACHE
    if _AIRFIELD_CACHE is None:
        _AIRFIELD_CACHE = {
            int(b['id']) for b in wardb.battlefields()
            if int(b.get('gamemap') or 0) in AIRFIELD_GAMEMAPS}
    return _AIRFIELD_CACHE


def is_airfield(bf_id):
    return int(bf_id or 0) in airfield_bfs()


def _tpl_of(at):
    return wardb.commandnode_templates().get(
        int(at.get('template_id') or 0)) or {}


def is_radius_unit(at):
    """有限半径空中单位（伞兵 + 飞机）：1 < transportradius < 无限哨兵。

    客户端 CommandnodeArrow L440-570：有限半径 + air_commandnode_base
    行存在 → airborne 单位（不走补给线，按半径直达）。"""
    try:
        tr = int(_tpl_of(at).get('transportradius') or 0)
    except (TypeError, ValueError):
        return False
    return 1 < tr < RADIUS_INFINITE


def is_aircraft(at):
    """飞机族（actionid 14 fighter_recon / 15 战斗机·重型战斗机）。"""
    try:
        return int(_tpl_of(at).get('actionid') or 0) in AIRCRAFT_ACTIONIDS
    except (TypeError, ValueError):
        return False


def is_paradrop_unit(at):
    """伞兵族（actionid 12 pathfinders / 13 paratroopers）。"""
    try:
        return int(_tpl_of(at).get('actionid') or 0) in PARADROP_ACTIONIDS
    except (TypeError, ValueError):
        return False


def bf_distance(from_bf, to_bf):
    """城市坐标直线距离；任一端无坐标返回 None（回退全允许）。"""
    p = _bf_pos()
    a, b = p.get(int(from_bf or 0)), p.get(int(to_bf or 0))
    if not a or not b:
        return None
    return math.hypot(a[0] - b[0], a[1] - b[1])


def nearest_airfield(bf_id):
    """距指定城市最近的机场（官服"主城附近的机场"口径，不查归属——
    fac1 首都 6724566 邻接无机场，最近机场距离 114）。无坐标返回 0。"""
    src = _bf_pos().get(int(bf_id or 0))
    if not src:
        return 0
    best, best_d = 0, None
    for af in airfield_bfs():
        q = _bf_pos().get(af)
        if not q:
            continue
        d = (src[0] - q[0]) ** 2 + (src[1] - q[1]) ** 2
        if best_d is None or d < best_d:
            best, best_d = af, d
    return best


def check_radius_move(at, cur_bf, target_bf, owners, battling):
    """空中半径单位移动校验 → (allowed, is_attack)。

    官服规则（CommandnodeArrow.detectBattleField L440-570）：
      dist ≤ attackradius/2   → 任意战场（攻击/参战/空降作战）；
      attackradius/2 < dist ≤ transportradius/2 → 仅己方非交战机场
        （换基地；reserve 补给队例外允许交战机场）；
      超出 transportradius/2  → 无效；不可落出兵点（调用方拦）。
    目标城不在战图 → 拒（对齐地面链 BFS 不可达）；当前位坐标缺失
    （异常态）回退全允许 (True, True)。"""
    p = _bf_pos()
    if int(cur_bf or 0) not in p:
        return True, True
    if int(target_bf or 0) not in p:
        return False, False
    a, b = p[int(cur_bf)], p[int(target_bf)]
    dist = math.hypot(a[0] - b[0], a[1] - b[1])
    try:
        attack = int(_tpl_of(at).get('attackradius') or 0)
        transport = int(_tpl_of(at).get('transportradius') or 0)
    except (TypeError, ValueError):
        attack = transport = 0
    target_bf = int(target_bf or 0)
    if dist * 2 <= attack:
        return True, True
    if dist * 2 <= transport:
        fac = int(at.get('faction') or 0)
        try:
            owner = int((owners or {}).get(target_bf, 0) or 0)
        except (TypeError, ValueError):
            owner = 0
        ok = (is_airfield(target_bf) and owner == fac
              and (target_bf not in (battling or set())
                   or is_reserve(at)))
        return ok, False
    return False, False


async def start_radius_move(store, war, at, target_bf, override_move=False,
                            now=None):
    """空中半径单位单段 air 直达移动（不走补给线、不消耗 cat10 运输机）。

    返回 (movement, error_code, is_attack)；成功码 RC_OK、段带
    air=True + radius_attack（到达分支判定换基地/落地变体用）。"""
    cur_bf = at_current_battlefield(at)
    target_bf = int(target_bf or 0)
    if not cur_bf or not target_bf or cur_bf == target_bf:
        return None, RC_CANNOT_FIND_COMMANDNODE, False
    try:
        owners = dict(await store.get_war_cities(war['id']))
    except Exception:
        owners = {}
    battling = await _battling_battlefields(store, war['id'])
    allowed, is_attack = check_radius_move(
        at, cur_bf, target_bf, owners, battling)
    if not allowed:
        return None, RC_CANNOT_FIND_COMMANDNODE, False
    now = now or datetime.datetime.now(datetime.timezone.utc)
    seconds = (0 if override_move
               else segment_seconds(cur_bf, target_bf, at_speed(at)))
    seg = {'from_bf': cur_bf, 'to_bf': target_bf, 'sl_id': 0,
           'from_ap': 0, 'to_ap': 0, 'seconds': seconds, 'air': True,
           'radius_attack': is_attack}
    mv = await store.create_movement(
        at['id'], at['war_id'], [seg], started_at=now,
        arrive_at=now + datetime.timedelta(seconds=seconds))
    await store.update_command_node(at['id'], state=1)
    return mv, RC_OK, is_attack


def msg_air_commandnode_base_row(at, war_id, base_bf):
    """air_commandnode_base 行（proto 实锤 f1-f6）：f1 id / f2
    commandNodeWarInstanceId / f3 battlefieldid（基地机场）/ f4
    targetentityid / f5 targetentitytype（'battlefield'，String）/ f6 warid。

    行 id = AT id；客户端 AirCommandnodeBaseTable 按 f2 join →
    isAirborneUnit 判定（有行 = airborne，应用半径规则）。"""
    return (pb_int64(1, int(at['id'])) + pb_int64(2, int(at['id']))
            + pb_int64(3, int(base_bf or 0)) + pb_int64(4, int(base_bf or 0))
            + pb_string(5, 'battlefield') + pb_int64(6, int(war_id)))


# ---- 补给队（Reserve，官服 §10.2 #6）----

RESERVE_TEMPLATE_OFFSET = 4       # template_id = faction*100 + 4（104/204/304）


def is_reserve(at):
    """补给队（Reserve）判定：template_id % 100 == 4
    （客户端 locTemplateId = templateId - faction*100 → 4 = AT_TEMPLATE_RESERVE）。"""
    return int(at.get('template_id') or 0) % 100 == RESERVE_TEMPLATE_OFFSET


def reserve_template_id(faction):
    """阵营补给队模板 id（faction*100+4；wardb commandnodetemplate 104/
    204/304 'reserve'，speed=1.0）。"""
    return int(faction or 0) * 100 + RESERVE_TEMPLATE_OFFSET


def bf_in_faction_supply(city_owners, faction, bf_id):
    """AT 所在城市是否在己方补给网络内（可就地即时增援）。

    官服口径：城市归本方，且沿补给线只经本方城市连通到本方首都
    （补给自首都沿网络流动；孤悬敌后/链路被断的己方孤城不在网 →
    需补给队机动）。无归属数据（owners 空，如 DB 异常）保守视为在网
    （维持旧行为即时增援）。"""
    if not city_owners:
        return True
    try:
        fac = int(faction or 0)
        bf = int(bf_id or 0)
        if int(city_owners.get(bf, 0) or 0) != fac:
            return False
    except (TypeError, ValueError):
        return True
    cap = _faction_capital_bf().get(fac, 0)
    if not cap or cap == bf:
        return True
    adj = supplyline_adjacency()
    seen = {bf}
    queue = [bf]
    while queue:
        cur = queue.pop(0)
        for edge in adj.get(cur, ()):
            nb = edge[3]
            if nb == cap:
                return True
            if nb in seen:
                continue
            try:
                if int(city_owners.get(nb, 0) or 0) != fac:
                    continue
            except (TypeError, ValueError):
                continue
            seen.add(nb)
            queue.append(nb)
    return False


def _column_delete_events(col):
    """补给队删除推送事件（与 _push_at_delete 同表集合：客户端
    CommandnodeTable DELETE 分支按 schema 移除 + OnTheWay 扣减）。"""
    events = [('del', b'CommandNodeWarInstance', col['id']),
              ('del', b'CommandNodeDefinition', col['id']),
              ('del', b'CommandNodeMorale', col['id'])]
    for cat in _as_resources(col.get('resources')):
        try:
            events.append(('del', b'armyresource',
                           int(col['id']) * 1000 + int(cat)))
        except (TypeError, ValueError):
            continue
    return events


async def _merge_column_events(store, war_id, col, parent, caps):
    """补给队与父 AT 汇合：资源并入父 AT（按模板编制封顶，超编丢弃）+
    补给队软删（state=3）。返回追加推送事件。"""
    parent = await store.get_command_node(parent['id']) or parent
    res = {str(k): int(v) for k, v in
           _as_resources(parent.get('resources')).items()}
    tpl = wardb.commandnode_templates().get(
        int(parent.get('template_id') or 0)) or {}
    full = template_resources(tpl)
    merged = {}
    for cat, n in _as_resources(col.get('resources')).items():
        try:
            cat, n = int(cat), int(n)
        except (TypeError, ValueError):
            continue
        cur = int(res.get(str(cat), 0) or 0)
        cap = int(full.get(cat, 0) or 0)
        add = n if not cap else max(0, min(n, cap - cur))
        if add > 0:
            res[str(cat)] = cur + add
            merged[cat] = add
    await store.update_command_node(parent['id'], resources=res)
    await store.update_command_node(col['id'], state=3)
    parent = await store.get_command_node(parent['id'])
    events = [('set', b'CommandNodeWarInstance',
               msg_commandnode_instance_row(parent, war_id, caps))]
    if parent:
        for p in msg_armyresource_rows(parent, war_id):
            events.append(('set', b'armyresource', p))
    events.extend(_column_delete_events(col))
    log(f'[war] supply column {col["id"]} merged into AT {parent["id"] if parent else "?"} '
        f'resources={merged}')
    return events


async def _arrive_supply_column(store, war_id, col, mv, dest_bf, caps,
                                now=None):
    """补给队到达处置（advance_movements 到达分支，替 AT 的落位/占城/
    会战逻辑）：父 AT 在同城 → 汇合并删队；父 AT 挪窝 → 落位后重新寻路
    追击；父 AT 没了 → 就地解散（资源损失，官服口径）。"""
    events = [('del', b'transport_commandnode_segment', mv['id']),
              ('del', b'transport_commandnode_destination', mv['id'])]
    parent = await store.get_command_node(
        int(col.get('parent_command_node_id') or 0))
    # 落位（城市本体）
    await store.update_command_node(
        col['id'], state=0, map_entity_id=int(dest_bf or 0),
        map_entity_type=MAP_ENTITY_BATTLEFIELD)
    col = await store.get_command_node(col['id'])
    if not parent or int(parent.get('state') or 0) >= 3:
        await store.update_command_node(col['id'], state=3)
        events.extend(_column_delete_events(col))
        log(f'[war] supply column {col["id"]} dissolved '
            f'(parent AT gone)')
        return events
    parent_bf = at_current_battlefield(parent)
    if parent_bf and int(parent_bf) != int(dest_bf or 0):
        # 父 AT 已机动 → 追击（拓扑静态，寻路失败则原地待命，由
        # _cleanup_supply_columns 兜底清理）
        mv2, code = await start_movement(store, col, parent_bf, now=now)
        if mv2 is not None:
            col = await store.get_command_node(col['id'])
            events.append(('set', b'CommandNodeWarInstance',
                           msg_commandnode_instance_row(col, war_id, caps)))
            segs = movement_segments(mv2)
            if segs:
                events.append(('set', b'transport_commandnode_segment',
                               msg_transport_segment_row(col, war_id, mv2,
                                                         segs[0])))
                events.append(('set', b'transport_commandnode_destination',
                               msg_transport_destination_row(col, war_id,
                                                             mv2, segs)))
            log(f'[war] supply column {col["id"]} retargeting '
                f'-> bf={parent_bf}')
            return events
        events.append(('set', b'CommandNodeWarInstance',
                       msg_commandnode_instance_row(col, war_id, caps)))
        return events
    # 汇合（父 AT 在同城 / 父 AT 位置未知（待命回首都）落位失败兜底同此）
    return events + await _merge_column_events(store, war_id, col, parent,
                                               caps)


async def column_route(store, war, parent_at, near_capital_bf=0):
    """补给队路线预检（纯读，不落库不扣款，购买侧拒单用）：确定出生点
    （f6 己方在网城 → 阵营首都）并按全图补给线拓扑验证可达性。

    返回 (spawn_bf, parent_bf, ok)：ok=False 表示无出生点/无父位置/
    地面不可达（真·孤岛：整张补给线拓扑无路，官服地图连通理论不达）。"""
    fac = int(parent_at.get('faction') or 0)
    try:
        owners = dict(await store.get_war_cities(war['id']))
    except Exception as e:
        log(f'[war] supply column owners read failed: {e}')
        owners = {}
    parent_bf = at_current_battlefield(parent_at)
    spawn_bf = int(near_capital_bf or 0)
    if spawn_bf and not bf_in_faction_supply(owners, fac, spawn_bf):
        spawn_bf = 0                    # 非己方在网（敌城/孤城）→ 自选
    if not spawn_bf:
        spawn_bf = _faction_capital_bf().get(fac, 0)
    if not spawn_bf or not parent_bf:
        return 0, parent_bf, False
    if spawn_bf == parent_bf:
        return spawn_bf, parent_bf, True
    return spawn_bf, parent_bf, \
        find_supplyline_path(spawn_bf, parent_bf) is not None


async def spawn_supply_column(store, war, parent_at, resources,
                              near_capital_bf=0, now=None):
    """生成补给队并寻路：阵营首都出生（官服口径：补给自首都出发；
    near_capital_bf=f6 玩家指定出发城，须己方在网否则忽略），载
    resources（{cat: n}），沿补给线机动到父 AT 所在城市。

    返回 (column, events, error_code)：column=None 且 error_code≠0 为失败
    （RC_CANNOT_FIND_COMMANDNODE=无出生点或地面不可达（真·孤岛），购买侧
    column_route 预检已挡、未扣款，理论不达；软删的父 AT 不会到这）。
    同城（出生点=父 AT 位置）直接汇合不出队。"""
    war_id = war['id']
    fac = int(parent_at.get('faction') or 0)
    caps = _faction_capital_bf()
    spawn_bf, parent_bf, ok = await column_route(
        store, war, parent_at, near_capital_bf=near_capital_bf)
    if not ok:
        log(f'[war] supply column for AT {parent_at["id"]} unreachable '
            f'(spawn={spawn_bf} parent={parent_bf}), rejected')
        return None, [], RC_CANNOT_FIND_COMMANDNODE
    try:
        col = await store.create_command_node(
            war_id, reserve_template_id(fac),
            int(parent_at.get('owner_player_id') or 0), fac,
            parent_at.get('name') or '',
            resources={str(k): int(v) for k, v in (resources or {}).items()},
            abbreviation=parent_at.get('abbreviation') or '',
            parent_command_node_id=parent_at['id'])
    except Exception as e:
        log(f'[war] supply column create failed: {e}')
        return None, [], RC_COMMANDNODE_WAR_INSTANCE_NOT_FOUND
    if col is None:
        return None, [], RC_COMMANDNODE_WAR_INSTANCE_NOT_FOUND
    events = [
        ('set', b'CommandNodeDefinition',
         msg_commandnode_definition_row(col)),
        ('set', b'CommandNodeWarInstance',
         msg_commandnode_instance_row(col, war_id, caps)),
        ('set', b'CommandNodeMorale', msg_commandnode_morale_row(col, war_id)),
    ]
    for p in msg_armyresource_rows(col, war_id):
        events.append(('set', b'armyresource', p))
    # 出生城市落位（start_movement 以此为寻路起点）
    await store.update_command_node(
        col['id'], is_deployed=True, map_entity_id=spawn_bf,
        map_entity_type=MAP_ENTITY_BATTLEFIELD)
    col = await store.get_command_node(col['id'])
    if spawn_bf == parent_bf:
        events.extend(await _merge_column_events(store, war_id, col,
                                                 parent_at, caps))
        return col, events, 0
    mv, code = await start_movement(store, col, parent_bf, now=now)
    if mv is None and code != RC_OK:
        # column_route 预检已过仍失败（状态异常等）→ 软删拒单，不留滞留行
        log(f'[war] supply column {col["id"]} start_movement failed '
            f'({code}), dissolved')
        await store.update_command_node(col['id'], state=3)
        return None, [], code
    col = await store.get_command_node(col['id'])
    events.append(('set', b'CommandNodeWarInstance',
                   msg_commandnode_instance_row(col, war_id, caps)))
    if mv is not None:
        segs = movement_segments(mv)
        if segs:
            events.append(('set', b'transport_commandnode_segment',
                           msg_transport_segment_row(col, war_id, mv,
                                                     segs[0])))
        events.append(('set', b'transport_commandnode_destination',
                       msg_transport_destination_row(col, war_id, mv, segs)))
    log(f'[war] supply column {col["id"]} spawned for AT '
        f'{parent_at["id"]} at bf={spawn_bf} -> bf={parent_bf} '
        f'resources={dict(resources or {})}')
    return col, events, 0


async def _cleanup_supply_columns(store, war=None):
    """滞留补给队清理（war_tick 兜底）：父 AT 已亡（state>=3/缺失）的
    空闲补给队就地解散；返回推送事件。"""
    if store is None or getattr(store, 'pool', None) is None:
        return []
    try:
        if war is None:
            war = await ensure_war(store)
        if war is None:
            return []
        events = []
        for at in await store.list_war_command_nodes(war['id']):
            if not is_reserve(at) or int(at.get('state') or 0) >= 3:
                continue
            parent = await store.get_command_node(
                int(at.get('parent_command_node_id') or 0))
            if parent and int(parent.get('state') or 0) < 3:
                continue
            await store.update_command_node(at['id'], state=3)
            events.extend(_column_delete_events(at))
            log(f'[war] supply column {at["id"]} dissolved in cleanup')
        return events
    except Exception as e:
        log(f'[war] cleanup supply columns failed: {e}')
        return []


def msg_transport_segment_row(at, war_id, mv, seg):
    """transport_commandnode_segment 行（f6/f7 为毫秒数字符串，
    客户端 Number(begintime) 直接与 serverTimeAsNumber 比较）。"""
    m = pb_int64(1, mv['id'])                          # id（movement id）
    m += pb_int64(2, at['id'])                         # commandNodeWarInstanceId
    m += pb_int64(3, seg['sl_id'])                     # supplylineid
    m += pb_int64(4, seg['from_ap'])                  # fromAccesspoint
    m += pb_int64(5, seg['to_ap'])                     # toAccesspoint
    m += pb_string(6, str(int(seg['begin_ms'])))       # begintime（ms STR）
    m += pb_string(7, str(int(seg['end_ms'])))         # endtime（ms STR）
    m += pb_int64(8, war_id)                           # warid
    return m


def msg_transport_destination_row(at, war_id, mv, segs, now_ms=None):
    """transport_commandnode_destination 行（终落地 + 总时长估算）。"""
    if segs:
        dest_bf = segs[-1]['to_bf']
        end_ms = segs[-1]['end_ms']
        cached = ','.join(str(s['to_bf']) for s in segs)
    else:
        dest_bf, end_ms, cached = 0, 0, ''
    start_ms = _ts_ms(mv.get('started_at'))
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    total = max(0.0, (end_ms - start_ms) / 1000.0)
    m = pb_int64(1, mv['id'])                          # id
    m += pb_int64(2, at['id'])                         # commandNodeWarInstanceId
    m += pb_int64(3, dest_bf)                          # battlefieldid
    m += pb_int64(4, int(mv.get('dest_ap') or 0))      # accesspointid
    m += pb_string(5, cached)                          # cachedpath
    m += pb_int64(6, war_id)                            # warid
    m += pb_bool(7, False)                             # retreatFlag
    m += pb_float(8, total)                             # timeEstimate（秒）
    m += pb_float(9, 0.0)                              # beginPosition
    return m


def msg_air_transport_row(at, war_id, mv, seg):
    """air_transport 行（§3.14）：f3/f4 from + f5/f6 to（type1=battlefield）
    + f7/f8 起止毫秒。空运在途动画由本行驱动（instance 行保持出发城市）。"""
    m = pb_int64(1, mv['id'])                          # id（movement id）
    m += pb_int64(2, at['id'])                         # commandNodeWarInstanceId
    m += pb_int64(3, seg['from_bf'])                   # fromentityid
    m += pb_int32(4, MAP_ENTITY_BATTLEFIELD)           # fromentitytype
    m += pb_int64(5, seg['to_bf'])                     # toentityid
    m += pb_int32(6, MAP_ENTITY_BATTLEFIELD)           # toentitytype
    m += pb_int64(7, int(seg['begin_ms']))             # begintime（ms）
    m += pb_int64(8, int(seg['end_ms']))               # endtime（ms）
    m += pb_int64(9, war_id)                           # warid
    return m


def msg_battle_row(wb):
    """battle 行（会战；f1 id 为 S64，f11 position 仅补给线会战使用）。"""
    factions = wb.get('factions') or []
    n_factions = len({int(f.get('faction') or 0) for f in factions})
    m = pb_sfixed64(1, wb['id'])                       # id
    m += pb_int64(2, wb['war_id'])                     # warid
    m += pb_int64(3, wb['battlefield_id'])             # mapEntityId
    m += pb_int64(4, MAP_ENTITY_BATTLEFIELD)           # mapEntityTypeId
    m += pb_string(5, _fmt_ts(wb.get('started_at')))   # start
    m += pb_uint32(9, n_factions)                      # factioncount
    m += pb_int64(10, 0)                               # excludedFactionId
    m += pb_float(11, 0.0)                             # position
    m += pb_string(12, _fmt_ts(wb.get('started_at')))  # activationTimeStamp
    return m


async def advance_movements(store, war=None, now=None):
    """推进入途移动：换段（AT map_entity → 当前补给线）/ 到达落位 +
    会战成局桩。返回推送事件 [('set', tag, row) / ('del', tag, row_id)]。"""
    if store is None or getattr(store, 'pool', None) is None:
        return []
    try:
        if war is None:
            war = await ensure_war(store)
        if war is None:
            return []
        war_id = war['id']
        now_ms = _ts_ms(now) if now is not None else int(time.time() * 1000)
        caps = _faction_capital_bf()
        events = []
        for mv in await store.list_active_movements(war_id):
            at = await store.get_command_node(mv['commandnode_id'])
            if not at or int(at.get('state') or 0) >= 3:
                await store.finish_movement(mv['id'], 2)   # AT 已解散
                continue
            segs = movement_segments(mv)
            if not segs:
                await store.finish_movement(mv['id'], 1)
                continue
            is_air = bool(segs[0].get('air'))
            if now_ms >= segs[-1]['end_ms']:
                # ---- 到达：终段 to_bf（或 dest_ap 出兵点）落位 ----
                await store.finish_movement(mv['id'], 1)
                if is_reserve(at):
                    # 补给队到达：汇合/追击/解散（不占城、不成战）
                    dest_bf = segs[-1]['to_bf']
                    events.extend(await _arrive_supply_column(
                        store, war_id, at, mv, dest_bf, caps, now=now))
                    continue
                dest_ap = int(mv.get('dest_ap') or 0)
                if dest_ap:
                    ent, ent_type = dest_ap, MAP_ENTITY_ACCESSPOINT
                else:
                    ent, ent_type = segs[-1]['to_bf'], MAP_ENTITY_BATTLEFIELD
                await store.update_command_node(
                    at['id'], state=0, map_entity_id=ent,
                    map_entity_type=ent_type)
                at = await store.get_command_node(at['id'])
                events.append(('del', b'transport_commandnode_segment',
                               mv['id']))
                events.append(('del', b'air_transport' if is_air else
                               b'transport_commandnode_destination',
                               mv['id']))
                events.append(('set', b'CommandNodeWarInstance',
                               msg_commandnode_instance_row(at, war_id, caps)))
                # ---- 空中半径单位到达（§10.2 #6/#7）----
                if is_radius_unit(at):
                    if not segs[-1].get('radius_attack'):
                        # 换基地到达：airbase 行指向新机场
                        events.append((
                            'set', b'air_commandnode_base',
                            msg_air_commandnode_base_row(
                                at, war_id, segs[-1]['to_bf'])))
                    elif is_paradrop_unit(at):
                        # 空降作战到达：伞兵落地变体（convert_to，*_on_
                        # ground 半径无限、地面速度）+ 删 airbase 行
                        cvt = int(_tpl_of(at).get('convert_to') or 0)
                        if cvt and wardb.commandnode_templates().get(cvt):
                            await store.update_command_node(
                                at['id'], template_id=cvt)
                            at = await store.get_command_node(at['id'])
                            events.append((
                                'set', b'CommandNodeDefinition',
                                msg_commandnode_definition_row(at)))
                            events.append((
                                'set', b'CommandNodeWarInstance',
                                msg_commandnode_instance_row(
                                    at, war_id, caps)))
                            for p in msg_armyresource_rows(at, war_id):
                                events.append(('set', b'armyresource', p))
                        events.append(('del', b'air_commandnode_base',
                                       at['id']))
                    # 飞机作战到达：airbase 行保持原基地（作战后拖回机场
                    # = 换基地，届时更新）
                # ---- 会战成局桩：目标城市有敌对 AT 驻扎 ----
                dest_bf = (segs[-1]['to_bf'] if not dest_ap
                           else _ap_battlefield(dest_ap))
                # 到达占城：无敌驻军/无进行中会战 → 城市易主（敌驻军
                # → 会战成局，归属由结算决定）
                events.extend(await _capture_city_events(
                    store, war_id, at.get('faction'), dest_bf))
                events.extend(await _check_battle_formation(
                    store, war_id, at, dest_bf))
                continue
            if is_air:
                # 空运单段在途：无换段（instance 行保持出发城市）
                continue
            # ---- 换段：AT map_entity 同步当前段补给线 ----
            idx = current_segment_index(segs, now_ms)
            cur_sl = segs[idx]['sl_id']
            if int(at.get('map_entity_id') or 0) != cur_sl:
                await store.update_command_node(
                    at['id'], map_entity_id=cur_sl,
                    map_entity_type=MAP_ENTITY_SUPPLYLINE, state=1)
                at = await store.get_command_node(at['id'])
                events.append(('set', b'transport_commandnode_segment',
                               msg_transport_segment_row(at, war_id, mv,
                                                         segs[idx])))
                events.append(('set', b'CommandNodeWarInstance',
                               msg_commandnode_instance_row(at, war_id, caps)))
                if is_reserve(at):
                    continue        # 补给队途经不占城（补给线两端本方领地）
                # 换段占城：AT 已抵达新段起点城市（=上一段终点），无敌
                # 驻军/无进行中会战则易主（途经城市陆续占领）
                events.extend(await _capture_city_events(
                    store, war_id, at.get('faction'), segs[idx]['from_bf']))
        return events
    except Exception as e:
        log(f'[war] advance_movements failed: {e}')
        return []


async def _check_battle_formation(store, war_id, at, dest_bf):
    """到达敌情检查（M1 会战桩 → M2 成局）：目标城市驻有敌对 AT →
    war_battles 登记 + war mission record 落库（任务列表可见）。

    已有同城市进行中会战 → 本 AT 标记入战（state=2）不重复建战。
    返回追加推送事件。"""
    if not dest_bf:
        return []
    events = []
    others = await store.list_war_command_nodes(war_id)
    enemies = [o for o in others
               if o['id'] != at['id'] and o.get('is_deployed')
               and int(o.get('faction') or 0) != int(at.get('faction') or 0)
               and at_current_battlefield(o) == int(dest_bf)]
    if not enemies:
        return events
    for wb in await store.list_active_war_battles(war_id):
        if int(wb.get('battlefield_id') or 0) == int(dest_bf):
            await store.update_command_node(at['id'], state=2)
            events.append(('set', b'battle', msg_battle_row(wb)))
            return events
    factions = [{'at_id': at['id'], 'faction': at.get('faction') or 0}]
    factions += [{'at_id': e['id'], 'faction': e.get('faction') or 0}
                 for e in enemies]
    wb = await store.create_war_battle(war_id, dest_bf, factions)
    await store.update_command_node(at['id'], state=2)
    for e in enemies:
        await store.update_command_node(e['id'], state=2)
    if wb:
        events.append(('set', b'battle', msg_battle_row(wb)))
        # M2：会战 → war mission record（GetMissionList 展示，玩家加入成
        # 真人局；mission_id = mission_records serial id，battle_id 关联）。
        # 攻方=到达的 AT 阵营，守方=首个敌 AT 阵营（M1 桩既有简化）。
        try:
            preset_pool = (mm_config.gamemode_cfg(10).get('fallback_presets')
                           or [])
            mid = await store.create_mission_record(
                random.choice(preset_pool) if preset_pool else 0,
                attacker_faction=int(at.get('faction') or 0),
                defender_faction=int(enemies[0].get('faction') or 0),
                name=f'war battle {wb["id"]} battlefield {dest_bf}',
                map_entity_id=int(dest_bf),
                battle_id=int(wb['id']))
            log(f'[war] battle {wb["id"]} formed → mission record id={mid} '
                f'bf={dest_bf} atk={at.get("faction")} '
                f'def={enemies[0].get("faction")}')
        except Exception as e:
            log(f'[war] create war mission record failed: {e}')
    return events


async def _capture_city_events(store, war_id, faction, bf_id,
                               ignore_battle_id=0, force=False):
    """城市易主：war_cities UPDATE + battlefieldstatus / supplylinestatus /
    accesspointstatus 行事件（受影响补给线按新归属重推或删陈旧行）。

    force=False 时守方检查：目标城市有进行中会战（battle id !=
    ignore_battle_id）或敌对 AT 驻扎 → 放弃占领（归属由会战结算/对峙
    决定），返回 []。force=True（会战结算胜方占领）跳过检查。
    归属未变 → 返回 []（无事件）。"""
    bf_id = int(bf_id or 0)
    faction = int(faction or 0)
    if not bf_id or not faction:
        return []
    try:
        if not force:
            for wb in await store.list_active_war_battles(war_id):
                if (int(wb.get('battlefield_id') or 0) == bf_id
                        and int(wb['id']) != int(ignore_battle_id or 0)):
                    return []
            for o in await store.list_war_command_nodes(war_id):
                if (o.get('is_deployed')
                        and int(o.get('faction') or 0) != faction
                        and at_current_battlefield(o) == bf_id):
                    return []
        owners = dict(await store.get_war_cities(war_id))
        if owners.get(bf_id) == faction:
            return []
        await store.set_city_owner(war_id, bf_id, faction)
        events = [('set', b'battlefieldstatus',
                   msg_battlefieldstatus_row(war_id, bf_id, faction))]
        owners[bf_id] = faction
        for sl in wardb.supplylines():
            b1 = _ap_battlefield(sl.get('ap1'))
            b2 = _ap_battlefield(sl.get('ap2'))
            if bf_id not in (b1, b2):
                continue
            o = owners.get(b1)
            if o and o == owners.get(b2):
                events.append(('set', b'supplylinestatus',
                               pb_sfixed64(1, sl['id'])
                               + pb_int64(2, war_id)
                               + pb_int64(3, o)
                               + pb_int64(4, sl['id'])))
            else:
                events.append(('del', b'supplylinestatus', sl['id']))
        for ap in wardb.accesspoints():
            if ap.get('battlefield') == bf_id:
                events.append(('set', b'accesspointstatus',
                               pb_sfixed64(1, ap['id'])
                               + pb_int64(2, war_id)
                               + pb_int64(3, ap['id'])
                               + pb_int64(4, faction)))
        log(f'[war] battlefield {bf_id} owner -> faction {faction}')
        return events
    except Exception as e:
        log(f'[war] capture city bf={bf_id} failed: {e}')
        return []


async def broadcast_war_events(events):
    """战争事件广播：全部 warmapview 订阅会话（含敌我视角）。"""
    if not events:
        return
    from state import FLASH_SESSIONS
    from messages import _push_kvcs, _push_kvcs_delete
    for sess in list(FLASH_SESSIONS.values()):
        cmd = getattr(sess, 'warmap_view_cmd', None)
        w = getattr(sess, 'writer', None)
        if not cmd or not w:
            continue
        for kind, tag, payload in events:
            try:
                if kind == 'set':
                    await _push_kvcs(w, cmd, tag, payload)
                else:
                    await _push_kvcs_delete(w, cmd, tag, payload)
            except Exception:
                pass          # 单会话故障不影响其他订阅者


async def _sweep_ended_war_leftovers(store):
    """已结束战争残留兜底清理：关账孤儿会战（state=0）+ 释放会战中 AT
    （state=2 → 0）。战争被强制结束/胜利结算时未关账的会战，automission
    只看当前战争不会再结算，靠本函数每 tick 兜底（幂等）。"""
    if store is None or getattr(store, 'pool', None) is None:
        return
    try:
        closed, released = await store.sweep_ended_war_leftovers()
        if closed or released:
            log(f'[war] ended-war leftovers swept: '
                f'battles {closed} closed, ATs {released} released')
    except Exception as e:
        log(f'[war] sweep_ended_war_leftovers failed: {e}')


async def war_tick(store):
    """战争推进 tick（realtest_server 周期挂载）：移动推进 + automission
    超时结算 + 阵营资源生产 + 增援队列交付 + 补给队滞留清理 + 事件广播。"""
    events = await advance_movements(store)
    events.extend(await _cleanup_supply_columns(store))
    await _sweep_ended_war_leftovers(store)
    await automission_tick(store)
    await production_tick(store)
    await supply_queue_tick(store)
    if events:
        await broadcast_war_events(events)


# ---- M2-4 会战结算回写（docs/hng-rts-protocol.md §8.3-§8.4）----

def _scale_resources(res, ratio):
    """按比例扣减 AT 编制 {cat: count}（向下取整，保零）。"""
    out = {}
    for cat, cnt in (res or {}).items():
        try:
            cnt = int(cnt)
        except (TypeError, ValueError):
            continue
        if cnt <= 0:
            continue
        out[int(cat)] = int(cnt * (1.0 - ratio))
    return out


async def settle_war_battle(store, mission_id, battle_id, winner_faction,
                            loss_ratios=None, captured=None):
    """FPS 会战结算回写（BattleReport 之后、release_match 之前调用）。

    loss_ratios：可选 (胜方损耗比, 败方损耗比) 覆盖（automission 用
    WAR_AUTOMISSION_* 系数）；缺省用 WAR_BATTLE_*_LOSS。
    captured：可选 {category_id: count} 战场缴获（败方 AT 未投入的池剩余，
    combat.py 从 force_pool 快照），入账胜方阵营库存并推送 armyresource 行。

    流程（协议 §8.3-4）：
      ① AT 损耗：胜方轻损 / 败方重损（mm_config.WAR_BATTLE_*_LOSS 比例），
         编制扣空 = 歼灭（state=3 软删，将军损失投资）；
      ② 士气：败 -moraleLostPerBattle / 胜 +moraleGainedPerWin，低于
         surrenderLimit → 投降撤回首都（is_deployed=0，需重建再战）；
      ③ 城市易主：攻方（mission record attacker_faction）胜 → war_cities
         UPDATE + battlefieldstatus 行推送；
      ④ war_battles state=1 + winning_faction；mission record 置 2（任务
         列表移除）；battle 行删除（战争图交战标记消失）；
      ⑤ warfunds：参战 AT 主人每局 +WAR_WARFUNDS_PER_BATTLE，胜方再
         +WIN_BONUS（占点英雄收益已在 PlayerBattleResult f3 入账）；
      ⑤b 缴获入账：captured 加进胜方 war_faction_resources 库存 + 推送
         HQ armyresource 行（客户端资源视图实时上涨）；
      ⑥ check_victory（首都数判据）。

    返回推送事件列表（调用方 broadcast_war_events）。
    """
    if store is None or getattr(store, 'pool', None) is None:
        return []
    try:
        wb = await store.get_war_battle(int(battle_id))
        if wb is None or int(wb.get('state') or 0) != 0:
            return []
        war_id = int(wb['war_id'])
        winner = int(winner_faction or 0)
        win_loss, lose_loss = loss_ratios or (
            mm_config.WAR_BATTLE_WINNER_LOSS,
            mm_config.WAR_BATTLE_LOSER_LOSS)
        events = []
        caps = _faction_capital_bf()
        morale_cfg = morale_defaults()
        surrender = int(morale_cfg.get('surrender_limit', 25))
        # #1 underdog 加成：人数较少阵营 XP/战资结算倍率（faction 行
        # f6/f7 同源），按参战各阵营 AT 持有玩家数计算
        ud_mults = underdog_multipliers(
            await faction_player_counts(store, war_id))

        for entry in (wb.get('factions') or []):
            at = await store.get_command_node(int(entry.get('at_id') or 0))
            if not at or int(at.get('state') or 0) != 2:
                continue                      # 已解散/不在战中
            won = bool(winner) and int(at.get('faction') or 0) == winner
            ratio = win_loss if won else lose_loss
            new_res = _scale_resources(
                _as_resources(at.get('resources')), ratio)
            morale = int(at.get('morale') or 0)
            morale += (mm_config.WAR_MORALE_GAINED_PER_WIN if won
                       else -mm_config.WAR_MORALE_LOST_PER_BATTLE)
            morale = max(0, min(int(at.get('max_morale') or 100), morale))
            # ⑤ warfunds（先行：与状态回写解耦，玩家不存在不阻断结算）；
            #    underdog 阵营（#1）按 f7 倍率放大每局/胜方加成
            owner = int(at.get('owner_player_id') or 0)
            _, wf_mult = ud_mults.get(
                int(at.get('faction') or 0), (1.0, 1.0))
            if owner and mm_config.WAR_WARFUNDS_PER_BATTLE:
                try:
                    await store.add_war_funds(
                        owner,
                        int(mm_config.WAR_WARFUNDS_PER_BATTLE * wf_mult),
                        f'war_battle {battle_id}')
                    if won and mm_config.WAR_WARFUNDS_WIN_BONUS:
                        await store.add_war_funds(
                            owner,
                            int(mm_config.WAR_WARFUNDS_WIN_BONUS * wf_mult),
                            f'war_battle {battle_id} win')
                except ValueError:
                    pass                      # 玩家已删除，跳过

            if not any(v > 0 for v in new_res.values()):
                # ---- 歼灭：编制扣空（state=3 软删，地图行删除）----
                await store.update_command_node(
                    at['id'], state=3, resources={}, morale=morale)
                events.append(('del', b'CommandNodeWarInstance', at['id']))
                for cat in _as_resources(at.get('resources')):
                    events.append(('del', b'armyresource',
                                   at['id'] * 1000 + int(cat)))
                log(f'[war] battle {battle_id}: AT {at["id"]} '
                    f'{at.get("name")!r} (faction {at.get("faction")}) '
                    f'wiped out')
                continue
            # ---- 存活：编制/士气回写 + XP/等级 + 投降判定 ----
            # ②b AT XP（客户端 ExperienceCalculation.createATXpList 口径：
            #     battles×5 + battlesWon×25）→ rank 按阈值自动晋升；
            #     买等级注入的 XP（阈值+5）取 max 保护，不被公式重算冲掉；
            #     underdog 阵营（#1）增量按 f6 倍率放大
            battles = int(at.get('battles') or 0) + 1
            battles_won = int(at.get('battles_won') or 0) + (1 if won else 0)
            xp_mult, _ = ud_mults.get(
                int(at.get('faction') or 0), (1.0, 1.0))
            xp = max(battles * 5 + battles_won * 25,
                     int(at.get('xp') or 0)
                     + int((5 + (25 if won else 0)) * xp_mult))
            rank = max(int(at.get('rank') or 0), rank_from_xp(xp))
            fields = {'resources': new_res, 'morale': morale,
                      'battles': battles, 'battles_won': battles_won,
                      'xp': xp, 'rank': rank}
            rank_up = rank > int(at.get('rank') or 0)
            if rank_up:
                log(f'[war] battle {battle_id}: AT {at["id"]} '
                    f'{at.get("name")!r} rank up -> {rank} (xp={xp})')
            if morale <= surrender and at.get('is_deployed'):
                cap_bf = caps.get(int(at.get('faction') or 0))
                if cap_bf:
                    fields.update(state=0, is_deployed=False,
                                  map_entity_id=cap_bf,
                                  map_entity_type=MAP_ENTITY_BATTLEFIELD)
                    log(f'[war] battle {battle_id}: AT {at["id"]} morale '
                        f'{morale} <= surrender {surrender}, retreated to '
                        f'capital {cap_bf}')
            await store.update_command_node(at['id'], **fields)
            at = await store.get_command_node(at['id'])
            if rank_up:
                # rank 变化须重推 Definition 行（f7），客户端才会刷新升级选项
                events.append(('set', b'CommandNodeDefinition',
                               msg_commandnode_definition_row(at)))
            events.append(('set', b'CommandNodeWarInstance',
                           msg_commandnode_instance_row(at, war_id, caps)))
            for cat, cnt in sorted(new_res.items()):
                if cnt:
                    events.append(
                        ('set', b'armyresource',
                         msg_armyresource_rows(
                             {'id': at['id'], 'resources': {cat: cnt}},
                             war_id)[0]))
                else:
                    events.append(('del', b'armyresource',
                                   at['id'] * 1000 + cat))

        # ③ 城市易主：攻方胜 → 占领（守方胜城市不变）
        attacker = 0
        try:
            mrec = await store.get_mission_record(int(mission_id))
            attacker = int((mrec or {}).get('attacker_faction') or 0)
        except Exception:
            pass
        dest_bf = int(wb.get('battlefield_id') or 0)
        if winner and attacker and winner == attacker and dest_bf:
            # force：会战胜方占领（结算语义，跳过敌驻军检查）；
            # 复用助手补推 supplylinestatus/accesspointstatus 关联行
            city_events = await _capture_city_events(
                store, war_id, winner, dest_bf, force=True)
            if city_events:
                events.extend(city_events)
                log(f'[war] battle {battle_id}: attacker faction {winner} '
                    f'captured battlefield {dest_bf}')

        # ⑤b 缴获入账（war 局）：败方 AT 未投入的池剩余 → 胜方阵营库存，
        #     推送 HQ armyresource 行（在线客户端资源视图实时上涨）
        if winner and captured:
            try:
                await store.adjust_war_faction_resources(
                    war_id, winner, captured)
                import matchmaking      # 延迟 import（防循环依赖）
                hq = matchmaking.FACTION_SUPREME_COMMANDS.get(winner)
                if hq:
                    for row in msg_hq_resource_rows(war_id, hq, captured):
                        events.append(('set', b'armyresource', row))
                log(f'[war] battle {battle_id}: faction {winner} captured '
                    f'{dict(sorted((int(c), int(n)) for c, n in captured.items()))}')
            except Exception as e:
                log(f'[war] battle {battle_id}: capture credit failed: {e}')

        # ④ 会战终态 + 任务列表移除 + 战争图交战标记删除
        await store.end_war_battle(int(battle_id), winner)
        try:
            await store.set_mission_state(int(mission_id), 2)
        except Exception as e:
            log(f'[war] set_mission_state({mission_id}, 2) failed: {e}')
        events.append(('del', b'battle', int(battle_id)))
        log(f'[war] battle {battle_id} settled: winner={winner or "defender"}')

        # ⑥ 首都数判据（占领可能触发战争结束）
        victory = await check_victory(store)
        if victory:
            log(f'[war] war ended by battle {battle_id}: '
                f'faction {victory} victorious')
        return events
    except Exception as e:
        log(f'[war] settle_war_battle({battle_id}) failed: {e}')
        return []


async def automission_tick(store):
    """M2-5 automission（协议 §8.4）：war battle 无人加入超时自动结算。

    每 active battle（state=0）按 mission record 判定：
      - record 缺失 / state==0（撮合未满，无人加入）：started_at 超过
        WAR_AUTOMISSION_NORMAL 秒 → 结算；
      - state==1 且战斗会话已不存在（全员中途退出，release_match 不回写
        record）：同样按超时结算；
      - state==1 且会话存活（COMBAT_INSTANCES 有实例）：live，跳过。
    胜负：双方参战 AT 剩余资源总量多者胜（持平/双方皆空 = 攻方不胜，守方
    守住）。损耗按 WAR_AUTOMISSION_WINNER_LOSS / LOSER_LOSS。
    由 realtest_server war_tick 周期调用；返回无值（事件已广播）。
    """
    if store is None or getattr(store, 'pool', None) is None:
        return
    try:
        war = await ensure_war(store)
        if war is None:
            return
        import matchmaking            # 延迟 import（防未来循环依赖）
        now_s = time.time()
        timeout = max(60, mm_config.WAR_AUTOMISSION_NORMAL)
        for wb in await store.list_active_war_battles(war['id']):
            try:
                mrec = await store.get_mission_by_battle(int(wb['id']))
            except Exception:
                mrec = None
            r_state = int((mrec or {}).get('state', 0) or 0)
            if r_state == 1 and mrec is not None:
                if int(mrec['id']) in matchmaking.COMBAT_INSTANCES:
                    continue          # 成局且战斗会话存活
            started = _ts_ms(wb.get('started_at')) / 1000.0
            if started <= 0 or now_s - started < timeout:
                continue
            # ---- 超时：按 AT 剩余资源判胜负 ----
            power = {}
            for entry in (wb.get('factions') or []):
                at = await store.get_command_node(
                    int(entry.get('at_id') or 0))
                if not at or int(at.get('state') or 0) != 2:
                    continue
                fac = int(at.get('faction') or 0)
                power[fac] = power.get(fac, 0) + sum(
                    v for v in _as_resources(at.get('resources')).values()
                    if isinstance(v, (int, float)))
            winner = 0
            if power:
                best = max(power.values())
                tops = [f for f, p in power.items() if p == best]
                if best > 0 and len(tops) == 1:
                    winner = tops[0]
            log(f'[war] automission: battle {wb["id"]} idle '
                f'{int(now_s - started)}s, settling (winner={winner})')
            events = await settle_war_battle(
                store, int((mrec or {}).get('id') or 0), int(wb['id']),
                winner,
                loss_ratios=(mm_config.WAR_AUTOMISSION_WINNER_LOSS,
                             mm_config.WAR_AUTOMISSION_LOSER_LOSS))
            await broadcast_war_events(events)
    except Exception as e:
        log(f'[war] automission_tick failed: {e}')


# ---- M3-1 阵营资源生产（docs/hng-rts-protocol.md §9.1 / §3.12.1）----

_PROD_INTERVAL_MS = 600_000        # ArmyResourceProductionTemplate 全表恒 600s
_PROD_LAST_RUN = 0.0               # WAR_PRODUCTION_MIN_TICK 节流游标


async def production_tick(store):
    """阵营资源生产推进：wars.production_ms 游标按 600s/run 结算 runs，
    把模板产量累加进 war_faction_resources，并对 resourceview 订阅会话
    重推 FactionResourceProduction/Consumption 行 + 最高司令部 armyresource
    行（客户端不做本地生产模拟，不推则资源总览数值停滞）。

    游标语义：0 = 首跑只初始化游标（不补发开战前历史产量）；
    runs>0 时游标推进 cursor + runs*interval。war_tick 5s 周期调用，
    WAR_PRODUCTION_MIN_TICK 节流整函数。"""
    global _PROD_LAST_RUN
    if store is None or getattr(store, 'pool', None) is None:
        return
    now_s = time.time()
    if now_s - _PROD_LAST_RUN < max(5, mm_config.WAR_PRODUCTION_MIN_TICK):
        return
    _PROD_LAST_RUN = now_s
    try:
        war = await ensure_war(store)
        if war is None:
            return
        now_ms = int(now_s * 1000)
        cursor = int(war.get('production_ms') or 0)
        if cursor <= 0:
            await store.update_war_production(war['id'], now_ms)
            return
        runs = int((now_ms - cursor) // _PROD_INTERVAL_MS)
        if runs <= 0:
            return
        deltas = {}
        for t in wardb.armyresource_production():
            per = int(t.get('produce_per_run') or 0)
            if per <= 0:
                continue            # 死行 cat 13 等
            fac = int(t.get('faction') or 0)
            cat = int(t.get('category_id') or 0)
            d = deltas.setdefault(fac, {})
            d[cat] = d.get(cat, 0) + per * runs
        for fac, d in deltas.items():
            await store.adjust_war_faction_resources(war['id'], fac, d)
        await store.update_war_production(
            war['id'], cursor + runs * _PROD_INTERVAL_MS)
        log(f'[war] production: war={war["id"]} runs={runs} '
            f'factions={sorted(deltas)}')
        # ---- 行重推（resourceview 订阅会话，与订阅时同函数同 warId 源）----
        import matchmaking          # 延迟 import（防循环依赖）
        from state import FLASH_SESSIONS
        from messages import _push_kvcs
        windows = await supply_windows(store, war)
        prods, cons = matchmaking._msg_faction_resource_rows(windows)
        queue_rows = matchmaking._msg_faction_queue_rows(windows)
        hq_rows = await hq_stock_rows(store, war)
        for sess in list(FLASH_SESSIONS.values()):
            cmd = getattr(sess, 'resource_view_cmd', None)
            w = getattr(sess, 'writer', None)
            if not cmd or not w:
                continue
            try:
                for row in prods:
                    await _push_kvcs(w, cmd, b'FactionResourceProduction', row)
                for row in hq_rows:
                    await _push_kvcs(w, cmd, b'armyresource', row)
                for row in cons:
                    await _push_kvcs(w, cmd, b'FactionResourceConsumption', row)
                for row in queue_rows:
                    await _push_kvcs(w, cmd, b'FactionResourceQueue', row)
            except Exception:
                pass                # 单会话故障不影响其他订阅者
    except Exception as e:
        log(f'[war] production_tick failed: {e}')


# ---- M3-2 阵营资源增援队列（docs/hng-rts-protocol.md §3.12 / §10.2 #3/#5）----

def _qn_window(rows):
    """队列行 → 窗口 (head, tail)：head=MIN(queue_number)、tail=MAX+1、
    空队 (0,0)（客户端 AssaultTeamModel 对称语义）。"""
    if not rows:
        return (0, 0)
    qns = [int(r.get('queue_number') or 0) for r in rows]
    return (min(qns), max(qns) + 1)


async def supply_windows(store, war=None):
    """{faction: (head, tail)} 队列窗口（resourceview 订阅/重推共用）。
    DB 不可用/无战争时回 {}（全部按 (0,0)）。"""
    try:
        if store is None or getattr(store, 'pool', None) is None:
            return {}
        if war is None:
            war = await ensure_war(store)
        if war is None:
            return {}
        by_fac = {}
        for r in await store.list_war_supply_queue(war['id']):
            by_fac.setdefault(int(r.get('faction') or 0), []).append(r)
        return {fac: _qn_window(rs) for fac, rs in by_fac.items()}
    except Exception as e:
        log(f'[war] supply_windows failed: {e}')
        return {}


async def supply_queue_tick(store):
    """增援队列推进（每阵营 FIFO，队头阻塞）：war_supply_queue 队头行
    库存足够 → 库存转移（HQ → AT 补编）+ 出队；不足 → 整个阵营本 tick
    阻塞（不跳行，保官方队列语义）。

    - AT 歼灭/解散（state>=3 或缺失）→ 出队不退款（warfunds 入队时已扣，
      简化口径）。
    - 交付后重推 AT 行（warmap 广播）+ 库存/队列/production 行
      （resourceview 订阅会话）。"""
    if store is None or getattr(store, 'pool', None) is None:
        return
    try:
        war = await ensure_war(store)
        if war is None:
            return
        import matchmaking          # 延迟 import（防循环依赖）
        from state import FLASH_SESSIONS
        from messages import _push_kvcs
        delivered = []              # 交付过的 AT（重取后供推送）
        dequeued_ids = []           # 出队行 id（交付/清理），供 DELETE 行推送
        for fid in (1, 2, 3):
            q = await store.list_war_supply_queue(war['id'], fid)
            if not q:
                continue
            stock = {int(r['category_id']): int(r['count'])
                     for r in await store.list_war_faction_resources(
                         war['id'], fid)}
            for row in q:           # 队头起逐行判定
                need = int(row['count'])
                cat = int(row['category_id'])
                if need <= 0 or stock.get(cat, 0) < need:
                    break           # 队头不足：本阵营阻塞
                at = await store.get_command_node(
                    int(row['command_node_id'] or 0))
                if not at or int(at.get('state') or 0) >= 3:
                    await store.dequeue_war_supply([row['id']])
                    dequeued_ids.append(int(row['id']))
                    continue        # AT 没了：出队不退款，继续清理下一行
                await store.adjust_war_faction_resources(
                    war['id'], fid, {cat: -need})
                await store.update_command_node(
                    at['id'], resources=_at_add_resources(at, cat, need))
                await store.dequeue_war_supply([row['id']])
                dequeued_ids.append(int(row['id']))
                at = await store.get_command_node(at['id'])
                delivered.append(at)
                log(f'[war] supply queue: at={at["id"]} faction={fid} '
                    f'cat={cat} x{need} delivered from stock')
                break               # 队头已推进，本阵营下 tick 再看下一行
        if not delivered:
            return
        # ---- 推送：touched AT 重取后推 armyresource/Instance 行 ----
        events = []
        for at in delivered:
            caps = _faction_capital_bf()
            events.append(('set', b'CommandNodeWarInstance',
                           msg_commandnode_instance_row(at, war['id'], caps)))
            for p in msg_armyresource_rows(at, war['id']):
                events.append(('set', b'armyresource', p))
        await broadcast_war_events(events)
        windows = await supply_windows(store, war)
        queue_rows = matchmaking._msg_faction_queue_rows(windows)
        cons = matchmaking._msg_faction_resource_rows(windows)[1]
        hq_rows = await hq_stock_rows(store, war)
        from messages import _push_kvcs_delete
        for sess in list(FLASH_SESSIONS.values()):
            cmd = getattr(sess, 'resource_view_cmd', None)
            w = getattr(sess, 'writer', None)
            if not cmd or not w:
                continue
            try:
                for row in queue_rows:
                    await _push_kvcs(w, cmd, b'FactionResourceQueue', row)
                for row in hq_rows:
                    await _push_kvcs(w, cmd, b'armyresource', row)
                for row in cons:
                    await _push_kvcs(w, cmd, b'FactionResourceConsumption', row)
                # AT 级队列行 DELETE（交付/清理；客户端 onQueueUpdates
                # 移除 queueItems 并重算 isQueuedForResources；
                # ticket DELETE → 增援队列窗口关闭——官服语义）
                for rid in dequeued_ids:
                    await _push_kvcs_delete(
                        w, cmd, b'CommandNodeSupplyQueueItem', rid)
                    await _push_kvcs_delete(
                        w, cmd, b'CommandNodeSupplyQueueItemTicket', rid)
            except Exception:
                pass
    except Exception as e:
        log(f'[war] supply_queue_tick failed: {e}')


def msg_supply_queue_item_row(qrow, at, war_id=None):
    """CommandNodeSupplyQueueItem 行（AT 级补给队列，客户端 xmlTag 同名，
    Database._warTables 注册；AssaultTeamModel.onQueueUpdates 按 AT 归属
    过滤消费 → queueItems/isQueuedForResources；增援队列窗口按 factionId
    聚合显示排队位次，§10.2 #3）。

    REQUIRED f1-14 + 可选 f17 resourcesNeeded（客户端实锤 schema：
    id/playerId/commandNodeWarInstanceId/armyResourceCategoryId/warId/
    factionId/timestamp/pricingId/paymentCurrency/queueNumber/
    nearCapitalBattlefieldId/playerSessionId/deviceId/metricsUid）。
    f8 pricingId / f9 paymentCurrency 入队时未持久化、客户端展示不读 →
    填 0；f11-f14 官服遥测字段填空值。queueNumber>10000000 客户端显示
    "unassigned"（我们 plentiful 合成值不入 war_supply_queue，不受影响）。"""
    ts = int(qrow.get('created_at') or 0) or int(time.time() * 1000)
    m = b''
    m += pb_int64(1, int(qrow['id']))                       # id
    m += pb_int64(2, int(at.get('owner_player_id') or 0))   # playerId
    m += pb_int64(3, int(qrow['command_node_id']))          # commandNodeWarInstanceId
    m += pb_int64(4, int(qrow['category_id']))              # armyResourceCategoryId
    m += pb_int64(5, int(qrow.get('war_id') or war_id or 0))
    m += pb_int64(6, int(qrow['faction']))                  # factionId
    m += pb_int64(7, ts)                                    # timestamp
    m += pb_int64(8, 0)                                     # pricingId
    m += pb_int32(9, 0)                                     # paymentCurrency (enum)
    m += pb_int32(10, int(qrow['queue_number']))            # queueNumber
    m += pb_int64(11, 0)                                    # nearCapitalBattlefieldId
    m += pb_int64(12, 0)                                    # playerSessionId
    m += pb_string(13, '')                                  # deviceId
    m += pb_string(14, '')                                  # metricsUid
    m += pb_int32(17, int(qrow.get('count') or 0))          # resourcesNeeded
    return m


def msg_supply_queue_ticket_row(qrow, at, place):
    """CommandNodeSupplyQueueItemTicket 行（队列位次凭证；增援队列窗口
    onTicketUpdated/updateQueueData 用 queueNumber - supplyQueueHead 显示
    "排队第 N 位"，DELETE → 关窗；§10.2 #3 lnTicket）。

    REQUIRED f1 id / f2 playerId / f3 warId / f4 factionId / f7 timestamp /
    f8 queueNumber / f9 queuePlace（f5/f6 无 descriptor 跳过，实锤 schema）。
    ticket id 复用队列行 id（两表键空间独立，出队 DELETE 同 id 推两张表）。"""
    ts = int(qrow.get('created_at') or 0) or int(time.time() * 1000)
    m = b''
    m += pb_int64(1, int(qrow['id']))
    m += pb_int64(2, int(at.get('owner_player_id') or 0))
    m += pb_int64(3, int(qrow.get('war_id') or 0))
    m += pb_int64(4, int(qrow['faction']))
    m += pb_int64(7, ts)
    m += pb_int32(8, int(qrow['queue_number']))
    m += pb_int32(9, int(place))                            # queuePlace（1 起）
    return m


async def supply_queue_table_rows(store, war=None):
    """AT 级补给队列全量行 (item_rows, ticket_rows)：subscriberesourceview
    订阅快照 / 入队重推共用。queuePlace=阵营 FIFO 位次（1 起）。
    DB 不可用/无战争回 ([], [])。"""
    try:
        if store is None or getattr(store, 'pool', None) is None:
            return [], []
        if war is None:
            war = await ensure_war(store)
        if war is None:
            return [], []
        rows = await store.list_war_supply_queue(war['id'])
        items, tickets = [], []
        at_cache = {}
        places = {}                     # faction → 已序位次
        for q in rows:                  # list 按 faction, queue_number FIFO 序
            nid = int(q['command_node_id'])
            at = at_cache.get(nid)
            if at is None:
                at = await store.get_command_node(nid) or {}
                at_cache[nid] = at
            fac = int(q['faction'])
            places[fac] = places.get(fac, 0) + 1
            items.append(msg_supply_queue_item_row(q, at, war['id']))
            tickets.append(msg_supply_queue_ticket_row(q, at, places[fac]))
        return items, tickets
    except Exception as e:
        log(f'[war] supply_queue_table_rows failed: {e}')
        return [], []


async def push_supply_queue_rows(store, war=None):
    """AT 级补给队列行重推到 resourceview 订阅会话（item+ticket 全量 set）。
    入队后调用（226 触发的 RESOURCE_VIEW 订阅在响应之后才发生，新订阅者
    由 views.handle_subscribe 快照覆盖；此处覆盖已订阅会话）。"""
    from state import FLASH_SESSIONS
    from messages import _push_kvcs
    items, tickets = await supply_queue_table_rows(store, war)
    if not items:
        return
    for sess in list(FLASH_SESSIONS.values()):
        cmd = getattr(sess, 'resource_view_cmd', None)
        w = getattr(sess, 'writer', None)
        if not cmd or not w:
            continue
        try:
            for row in items:
                await _push_kvcs(w, cmd, b'CommandNodeSupplyQueueItem', row)
            for row in tickets:
                await _push_kvcs(w, cmd,
                                 b'CommandNodeSupplyQueueItemTicket', row)
        except Exception:
            pass                # 单会话故障不影响其他订阅者


def _at_add_resources(at, cat, need):
    """AT 现有 resources（str 键）+ 交付补编 → 新 resources dict（str 键，
    update_command_node JSON 序列化用）。"""
    res = {str(k): int(v) for k, v in _as_resources(at.get('resources')).items()}
    res[str(cat)] = int(res.get(str(cat), 0)) + int(need)
    return res
