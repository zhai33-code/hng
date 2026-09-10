# -*- coding: utf-8 -*-
"""战争地图快照聚合：battlefield/supplyline/accesspoint 状态行、commandnode 定义/实例行、armyresource/hq 资源行、士气行。

原 war_campaign.py 阶段 3.5 拆分（docs/warserver-refactor-plan.md §3.5）：
符号逐字搬运，行为不变。"""

from protocol import (
    pb_bool,
    pb_int32,
    pb_int64,
    pb_sfixed64,
    pb_string,
    pb_uint32,
    pb_float,
    )
import wardb
from state import log
from .common import (
    _fmt_ts,
    _as_resources,
    MAP_ENTITY_BATTLEFIELD,
    MAP_ENTITY_ACCESSPOINT,
    MAP_ENTITY_SUPPLYLINE,
    _ts_ms,
    )
from .lifecycle import (
    ensure_war,
    _faction_capital_bf,
    )
from .battle_msgs import (
    msg_air_transport_row,
    )
from .at_ops import (
    is_radius_unit,
    is_reserve,
    msg_air_commandnode_base_row,
    )


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
    # f8 supplyResourceCategoryId：reserve 单位（补给队/运输机队）运载的
    # 资源类别 → 战地图图标（客户端 ReserveTeamGraphics 按类别选形状：
    # cat10=运输机剪影、cat1=步兵、cat20=战斗机…；非 reserve 行不写，
    # 官服同款字段）。缺省（多类/无资源）= 0 通用底板。
    if is_reserve(at):
        cats = sorted(int(c) for c in _as_resources(at.get('resources')))
        m += pb_int64(8, cats[0] if len(cats) == 1 else 0)
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
    随机数（mm_factions.FACTION_SUPREME_COMMANDS），*1000 惯例会溢出 int64；
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
        import mm_factions          # 延迟 import（防循环依赖）
        rows = []
        for fid, cats in stock.items():
            hq = mm_factions.FACTION_SUPREME_COMMANDS.get(fid)
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
    from .at_movement import at_current_battlefield, movement_segments
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
        # isAirborneUnit 判定 + 半径规则；基地 = 持久化出发机场 airbase_bf，
        # 未记录（legacy 行）回退当前驻地）
        for at in ats:
            if at.get('is_deployed') and is_radius_unit(at):
                base = int(at.get('airbase_bf') or 0) \
                    or at_current_battlefield(at)
                rows.append((b'air_commandnode_base',
                             msg_air_commandnode_base_row(
                                 at, war_id, base)))
        # 活跃会战 battle 行（2026-09-08 补：成局后新登录/重订阅的客户端
        # 才能看到战斗标记；此前快照缺 battle 段 → 该城 BattleTable 无行
        # → 点击走"城市详情"分支而非"战局详情"分支，且战斗标记不显示）
        from .battle_msgs import msg_battle_row
        for wb in await store.list_active_war_battles(war_id):
            rows.append((b'battle', msg_battle_row(wb)))
        # 在途空运行（订阅时已起飞的 AT；§3.14 air_transport）
        for mv in await store.list_active_movements(war_id):
            segs = movement_segments(mv)
            if segs and segs[0].get('air'):
                mvat = await store.get_command_node(mv['commandnode_id'])
                if mvat:
                    rows.append((b'air_transport',
                                 msg_air_transport_row(mvat, war_id, mv,                                                       segs[0])))
        return rows
    except Exception as e:
        log(f'[war] build_warmap_snapshot failed: {e}')
        return []


# ---- AT 管理业务逻辑（Phase 5，docs/hng-rts-protocol.md §5.2）----
