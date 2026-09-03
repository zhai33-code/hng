# -*- coding: utf-8 -*-
"""RTS 战争状态机（Phase 3，docs/hng-rts-protocol.md）。

职责：
  - 战争生命周期：ensure_war（自动开战，读 WarDatabase capital 表分配初始
    归属）→ running → check_victory（占领首都数达 pointsNeededForVictory
    → end_war + 自动续战）。
  - war 行消息构建（29 REQUIRED 字段全填，docs/hng-rts-protocol.md §3.1）。
  - war catalogue 数据聚合（views.handle_query_war_catalogue_request 用）。

faction id 空间 = factiontemplateid 恒等映射 1/2/3（project_rules 硬约束）。
时间戳 STR 格式 'YYYY-MM-DD HH:MM:SS'（协议 §3.1 begintime/endtime）。
"""
import datetime
import json
import math
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


def initial_city_owners():
    """开战初始城市归属 {battlefield_id: faction}。

    官服语义：仅 capital 表登记的城市有初始归属（default_faction），
    其余战场节点中立（0）。"""
    return {c['battlefield_id']: c['default_faction']
            for c in wardb.capitals()
            if c.get('battlefield_id') and c.get('default_faction')}


def _fmt_ts(dt):
    """DB TIMESTAMPTZ → 协议 STR 时间戳（UTC，无时区后缀）。"""
    if not dt:
        return ''
    if isinstance(dt, str):
        return dt
    if dt.tzinfo is None:            # naive（FakeStore/无时区来源）按 UTC
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


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


async def ensure_war(store):
    """取当前 running 战争；无且 MM_WAR_AUTO_CREATE=1 时自动开战。

    开战流程：create_war（config 快照）→ init_war_cities（capital 表初始
    归属）。返回 war dict 或 None（store 不可用 / 不自动开战）。"""
    if store is None or getattr(store, 'pool', None) is None:
        return None
    try:
        war = await store.get_active_war()
        if war is not None:
            return war
        if not mm_config.WAR_AUTO_CREATE:
            return None
        war = await store.create_war(
            mm_config.WAR_NAME,
            points_needed_for_victory=mm_config.WAR_POINTS_VICTORY,
            config=_war_config())
        await store.init_war_cities(war['id'], initial_city_owners())
        log(f'[war] 新战争开打 id={war["id"]} name={mm_config.WAR_NAME!r} '
            f'points={mm_config.WAR_POINTS_VICTORY} '
            f'cities={len(initial_city_owners())}')
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
    m += pb_int32(8, war.get('points_needed_for_victory')
                  or mm_config.WAR_POINTS_VICTORY)                # pointsNeededForVictory
    m += pb_uint32(9, 0)                                          # automissionnormal（M3）
    m += pb_uint32(10, 0)                                         # automissionskirmish（M3）
    m += pb_uint32(11, 0)                                         # automissionsupply（M3）
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
    m += pb_int32(19, 0)                                          # fowGridSize（M1 全图可见）
    m += pb_int32(20, 0)                                          # fowGridOriginX
    m += pb_int32(21, 0)                                          # fowGridOriginY
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
    m += pb_int32(4, war.get('points_needed_for_victory')
                  or mm_config.WAR_POINTS_VICTORY)
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
        needed = war.get('points_needed_for_victory') or mm_config.WAR_POINTS_VICTORY
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
    m += pb_int32(7, at.get('rank') or 1)            # rank
    m += pb_int64(9, at.get('general_character_id') or 0)  # generalCharacterId
    m += pb_int64(10, _ts_ms(at.get('created_at')))  # statUpdateTimeStamp
    m += pb_bool(11, False)                          # suppliedOnCreation
    return m


def msg_commandnode_instance_row(at, war_id, faction_capitals):
    """CommandNodeWarInstance 行（地图上的 AT 实例）。"""
    ent, ent_type = _at_map_entity(at, faction_capitals)
    m = b''
    m += pb_int64(1, at['id'])                       # id
    m += pb_int64(2, at['id'])                       # commandNodeDefinitionId（M1 同 id）
    m += pb_int64(3, 0)                              # parentCommandNodeInstanceId
    m += pb_int64(4, at.get('faction') or 0)         # factionId
    m += pb_int64(5, ent)                            # mapEntityId
    m += pb_int64(6, ent_type)                      # mapEntityTypeId（1/2/3 硬约束）
    m += pb_int64(7, war_id)                         # warId
    m += pb_uint32(8, at.get('morale') or 0)         # morale
    m += pb_int64(9, _ts_ms(at.get('created_at')))   # lastReset
    m += pb_uint32(10, 1 if at.get('is_deployed') else 0)  # isDeployed
    m += pb_bool(11, False)                          # hasGottenSoldierResourcesForFree
    m += pb_bool(12, False)                          # hasGottenVehicleResourcesForFree
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
    """新 AT 初始 rank（commandnodetemplate f10 level，缺省 1）。"""
    return int(tpl.get('level') or 1)


def dissolve_refund(tpl):
    """解散返还 warfunds = 模板定价 × MM_WAR_DISSOLVE_REFUND（无定价回 0）。"""
    price = wardb.at_pricing().get(int(tpl['id'])) or {}
    return int((price.get('warfunds') or 0) * mm_config.WAR_DISSOLVE_REFUND)


def upgrade_allowed(cur_tpl, target_tpl):
    """升级链校验：目标模板 parent_template_id == 当前模板 id
    （或当前模板 convert_to == 目标 id，两方向任一命中即官服升级关系）。"""
    if not cur_tpl or not target_tpl:
        return False
    cur_id, tgt = int(cur_tpl['id']), int(target_tpl['id'])
    if tgt == cur_id:
        return False
    if int(target_tpl.get('parent_template_id') or 0) == cur_id:
        return True
    return int(cur_tpl.get('convert_to') or 0) == tgt


def deploy_battlefield(at, faction, near_capital_bf=0):
    """部署落位 battlefield id：请求指定 nearCapital 优先，否则己方首都。"""
    if near_capital_bf:
        return int(near_capital_bf)
    return _faction_capital_bf().get(int(faction or 0), 0)


# ---- AT 移动（Phase 6，docs/hng-rts-protocol.md §5.3）----

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


def segment_seconds(from_bf, to_bf, speed):
    """单段移动时长（秒）= 城市坐标距离 / (speed × timescale × move_scale)。

    官服语义：步兵 AT 数小时跨越半张图；speed 0.45-5.6（实测表值）、
    城市间典型距离 1100-6400 坐标单位，默认参数下首都对首都 ~1-4 小时。"""
    p = _bf_pos()
    a, b = p.get(int(from_bf or 0)), p.get(int(to_bf or 0))
    if not a or not b:
        return SEGMENT_FALLBACK_SECONDS
    import math
    dist = math.hypot(a[0] - b[0], a[1] - b[1])
    denom = (float(speed or 1.0) * (mm_config.WAR_TIMESCALE or 1.0)
             * (mm_config.WAR_AT_MOVE_SCALE or 1.0))
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
        return None, RC_FAIL
    if cur_bf == target_bf and not override_ap:
        return None, RC_OK
    path = find_supplyline_path(cur_bf, target_bf)
    if path is None:
        return None, RC_FAIL
    speed = at_speed(at)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    for seg in path:
        seg['seconds'] = (0 if override_move
                          else segment_seconds(seg['from_bf'], seg['to_bf'],
                                               speed))
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
            if now_ms >= segs[-1]['end_ms']:
                # ---- 到达：终段 to_bf（或 dest_ap 出兵点）落位 ----
                await store.finish_movement(mv['id'], 1)
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
                events.append(('del', b'transport_commandnode_destination',
                               mv['id']))
                events.append(('set', b'CommandNodeWarInstance',
                               msg_commandnode_instance_row(at, war_id, caps)))
                # ---- 会战成局桩：目标城市有敌对 AT 驻扎 ----
                dest_bf = (segs[-1]['to_bf'] if not dest_ap
                           else _ap_battlefield(dest_ap))
                events.extend(await _check_battle_formation(
                    store, war_id, at, dest_bf))
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
        return events
    except Exception as e:
        log(f'[war] advance_movements failed: {e}')
        return []


async def _check_battle_formation(store, war_id, at, dest_bf):
    """到达敌情检查（M1 会战桩）：目标城市驻有敌对 AT → war_battles 登记。

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
    return events


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


async def war_tick(store):
    """战争推进 tick（realtest_server 周期挂载）：移动推进 + 事件广播。"""
    events = await advance_movements(store)
    if events:
        await broadcast_war_events(events)
