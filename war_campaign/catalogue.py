# -*- coding: utf-8 -*-
"""war catalogue 数据聚合（views.handle_query_war_catalogue_request 用）。

原 war_campaign.py 阶段 3.5 拆分（docs/warserver-refactor-plan.md §3.5）：
符号逐字搬运，行为不变。"""

from protocol import (
    pb_int32,
    pb_int64,
    pb_message,
    pb_string,
    pb_uint32,
    )
import mm_config
from state import log
from .common import (
    _SOLDIER_CATS,
    _TANK_CATS,
    _GROUND_VEHICLE_CATS,
    _PLANE_CATS,
    _war_days,
    _as_resources,
    )
from .lifecycle import (
    capital_battlefield_ids,
    effective_points_needed,
    )


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
    m += pb_string(3, _war_days(war))                             # wartime
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


async def build_war_catalogue_protos(store, include_war_id=0):
    """全量目录行 proto 列表（每场战争一条，新→旧）。

    include_war_id != 0（客户端 WarIsOver 终局链带的 f1）：把该战争行
    置首——客户端 LoginCheckPlayerWarStatusCommand 取
    warcataloguedata[0] 存 lastWarPlayerParticipatedInCatalogues，若按
    默认序返回（新续战在前）→ 结算界面 getWarInformation(结束id)=null
    早退，阵营统计全空。返回 [bytes, ...]；DB 不可用返回 []。"""
    if store is None or getattr(store, 'pool', None) is None:
        return []
    try:
        wars = await _list_wars(store)
        capitals = capital_battlefield_ids()
        first = None
        out = []
        for war in wars:
            city_owners = await store.get_war_cities(war['id'])
            ats = await store.list_war_command_nodes(war['id'])
            row = _msg_catalogue(war, city_owners, ats, capitals)
            if int(include_war_id or 0) and war['id'] == int(include_war_id):
                first = row
            else:
                out.append(row)
        if first is not None:
            out.insert(0, first)
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
