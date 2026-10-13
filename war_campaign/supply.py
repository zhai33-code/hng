# -*- coding: utf-8 -*-
"""补给系统：补给圈判定、补给车队的生成/合并/到达/清理、补给窗口与补给队列 tick。

原 war_campaign.py 阶段 3.5 拆分（docs/warserver-refactor-plan.md §3.5）：
符号逐字搬运，行为不变。"""

import time
import datetime
import heapq
import math
from protocol import (
    pb_int32,
    pb_int64,
    pb_string,
    )
import wardb
from state import log
from .common import (
    _as_resources,
    rts_scope,
    _PLANE_CATS,
    BATTLE_TYPE_ASSAULT,
    MAP_ENTITY_BATTLEFIELD,
    supplyline_adjacency,
    find_supplyline_path,
    )
from .lifecycle import (
    ensure_war,
    _faction_capital_bf,
    )
from .battle_msgs import (
    msg_transport_segment_row,
    msg_transport_destination_row,
    msg_air_transport_row,
    msg_missionresource_row,
    )
from .at_ops import (
    RC_OK,
    RC_COMMANDNODE_WAR_INSTANCE_NOT_FOUND,
    template_resources,
    RC_CANNOT_FIND_COMMANDNODE,
    is_air_family_at,
    is_reserve,
    nearest_owned_airfield,
    remount_on_ground_at_airfield,
    reserve_template_id,
    msg_air_commandnode_base_row,
    bf_distance,
    )
from .warmap_view import (
    msg_commandnode_definition_row,
    msg_commandnode_instance_row,
    msg_armyresource_rows,
    hq_stock_rows,
    msg_commandnode_morale_row,
    )
from .at_movement import (
    at_current_battlefield,
    movement_segments,
    at_speed,
    segment_seconds,
    )


def supply_state(at):
    """JSONB may arrive as text when asyncpg has no JSON codec."""
    return dict(_as_resources(at.get('supply_state')))


def is_plane_supply(at):
    return (is_reserve(at) and bool(_as_resources(at.get('resources')))
            and all(int(c) in _PLANE_CATS
                    for c in _as_resources(at.get('resources'))))


def fastest_supply_path(from_bf, to_bf, at, owners):
    """Shortest travel time, allowing hostile/neutral intermediate cities."""
    from_bf, to_bf = int(from_bf), int(to_bf)
    distances = {from_bf: 0.0}
    previous = {}
    queue = [(0.0, from_bf)]
    fac = int(at.get('faction') or 0)
    while queue:
        cost, bf = heapq.heappop(queue)
        if cost != distances[bf]:
            continue
        if bf == to_bf:
            path = []
            while bf != from_bf:
                prev, seg = previous[bf]
                path.append(seg)
                bf = prev
            return list(reversed(path))
        for sl, ap1, ap2, nb in supplyline_adjacency().get(bf, ()):
            seconds = segment_seconds(bf, nb, at_speed(at), fac, owners)
            new_cost = cost + seconds
            if new_cost < distances.get(nb, math.inf):
                distances[nb] = new_cost
                previous[nb] = (bf, {'sl_id': sl, 'from_ap': ap1,
                    'to_ap': ap2, 'from_bf': bf, 'to_bf': nb,
                    'seconds': seconds})
                heapq.heappush(queue, (new_cost, nb))
    return None


async def supply_parent(store, at):
    parent = await store.get_command_node(int(at.get('parent_command_node_id') or 0))
    if not parent or int(parent.get('state') or 0) >= 3:
        return None
    return parent


async def start_supply_movement(store, war, at, target_bf,
                                manual=False, override_move=False, now=None):
    """Independent supply order; planes use the parent's transport range."""
    parent = await supply_parent(store, at)
    cur = at_current_battlefield(at)
    if not parent or not cur or not target_bf:
        return None, RC_CANNOT_FIND_COMMANDNODE
    owners = dict(await store.get_war_cities(war['id']))
    target_bf = int(target_bf)
    if manual and int(owners.get(target_bf) or 0) != int(at['faction']):
        return None, RC_CANNOT_FIND_COMMANDNODE
    if is_plane_supply(at):
        from .at_ops import is_airfield
        tpl = wardb.commandnode_templates().get(int(parent['template_id'])) or {}
        if tpl.get('convert_to'):
            tpl = wardb.commandnode_templates().get(int(tpl['convert_to'])) or tpl
        distance = bf_distance(cur, target_bf)
        radius = float(tpl.get('transportradius') or 0) / 2
        if (not is_airfield(target_bf) or distance is None
                or distance > radius
                or int(owners.get(target_bf) or 0) != int(at['faction'])):
            return None, RC_CANNOT_FIND_COMMANDNODE
        path = [{'sl_id': 0, 'from_ap': 0, 'to_ap': 0,
                 'from_bf': cur, 'to_bf': target_bf, 'air': True,
                 'seconds': segment_seconds(cur, target_bf,
                                            tpl.get('speed') or 1, air=True)}]
    else:
        path = fastest_supply_path(cur, target_bf, at, owners)
        if path is None:
            return None, RC_CANNOT_FIND_COMMANDNODE
    if cur == target_bf:
        data = supply_state(at)
        data['manual'] = bool(manual)
        await store.update_command_node(at['id'], supply_state=data)
        return None, RC_OK
    active = await store.get_active_movement(at['id'])
    if active:
        return None, RC_CANNOT_FIND_COMMANDNODE
    if override_move:
        for seg in path:
            seg['seconds'] = 0
    now = now or datetime.datetime.now(datetime.timezone.utc)
    mv = await store.create_movement(at['id'], war['id'], path,
        started_at=now, arrive_at=now + datetime.timedelta(
            seconds=sum(s['seconds'] for s in path)))
    data = supply_state(at)
    data.setdefault('source', 'purchase')
    data.update(manual=bool(manual), blocked_bf=0, blocked_battle_id=0)
    data['visited_segment'] = -1
    fields = {'state': 1, 'supply_state': data}
    if not is_plane_supply(at):
        fields.update(map_entity_id=path[0]['sl_id'], map_entity_type=3)
    await store.update_command_node(at['id'], **fields)
    return mv, RC_OK


def supply_movement_events(at, war_id, mv, caps, segment_index=0):
    segs = movement_segments(mv)
    events = [('set', b'CommandNodeWarInstance',
               msg_commandnode_instance_row(at, war_id, caps))]
    if segs and segs[0].get('air'):
        events.append(('set', b'air_transport',
                       msg_air_transport_row(at, war_id, mv, segs[0])))
        events.append(('set', b'air_commandnode_base',
                       msg_air_commandnode_base_row(at, war_id,
                           at.get('airbase_bf') or segs[0]['from_bf'],
                           target_bf=segs[-1]['to_bf'])))
    elif segs:
        events.extend([
            ('set', b'transport_commandnode_segment',
             msg_transport_segment_row(at, war_id, mv, segs[segment_index])),
            ('set', b'transport_commandnode_destination',
             msg_transport_destination_row(at, war_id, mv, segs))])
    return events


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
              ('del', b'CommandNodeMorale', col['id']),
              ('del', b'air_commandnode_base', col['id']),
              ('del', b'missionresource', col['id'])]
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
    fresh_col = await store.get_command_node(col['id'])
    if not fresh_col or int(fresh_col.get('state') or 0) >= 3:
        return []
    col = fresh_col
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
    active = await store.get_active_movement(col['id'])
    if hasattr(store, 'merge_supply_column'):
        result = await store.merge_supply_column(col['id'], parent['id'], full)
        if result is None:
            return []
        parent, merged = result
    else:
        # DB-free fixtures; the production store uses the locked merge above.
        await store.update_command_node(parent['id'], resources=res)
        await store.update_command_node(col['id'], state=3)
        if active:
            await store.finish_movement(active['id'], 2)
        parent = await store.get_command_node(parent['id'])
    events = [('set', b'CommandNodeWarInstance',
               msg_commandnode_instance_row(parent, war_id, caps))]
    if parent:
        for p in msg_armyresource_rows(parent, war_id):
            events.append(('set', b'armyresource', p))
    events.extend(_column_delete_events(col))
    if active:
        events.extend(_supply_stop_events(active))
    if parent and int(parent.get('state') or 0) == 2 and merged:
        battle_id = next((int(wb['id'])
            for wb in await store.list_active_war_battles(war_id)
            if any(int(f.get('at_id') or 0) == int(parent['id'])
                   for f in wb.get('factions') or [])), 0)
        async def sync_pool():
            from matchmaking import add_supply_to_live_mission
            if battle_id:
                await add_supply_to_live_mission(battle_id, parent, merged)
        callbacks = getattr(store, '_rts_after_commit', None)
        if callbacks is not None:
            callbacks.append(sync_pool)
        else:
            await sync_pool()
    log(f'[war] supply column {col["id"]} merged into AT {parent["id"] if parent else "?"} '
        f'resources={merged}')
    return events


async def merge_waiting_columns(store, war_id, at, caps):
    """本体到站取补给（官服「等本体飞过来合并就完成补给」）：父 AT 到达
    （或已在）某城时，把同城**原地等待**的子补给队（reserve + 非停驻
    机队 + 无在途移动）并入本体；合并后落地变体持满运输机 → 重新登机
    转回空中原型（remount_on_ground_at_airfield）。

    与 _arrive_supply_column 方向相反：地面补给队是队找 AT（机动追击
    汇合），空军系补给队是 AT 找队（机场原地等待）。返回 (at, events)；
    无可合并返回 (at, [])。"""
    if is_reserve(at):
        return at, []
    bf = int(at_current_battlefield(at) or 0)
    if not bf:
        return at, []
    try:
        nodes = await store.list_player_command_nodes(
            int(at.get('owner_player_id') or 0), int(war_id))
    except Exception as e:
        log(f'[war] waiting supply columns lookup failed: {e}')
        return at, []
    events = []
    for n in sorted(nodes, key=lambda x: int(x.get('id') or 0)):
        if (int(n.get('id') or 0) == int(at.get('id') or 0)
                or int(n.get('parent_command_node_id') or 0)
                != int(at.get('id') or 0)
                or not is_reserve(n)
                or (int(n.get('airbase_bf') or 0) > 0
                    and supply_state(n).get('source') != 'purchase')
                or int(n.get('state') or 0) >= 3
                or not n.get('is_deployed')
                or int(at_current_battlefield(n) or 0) != bf):
            continue
        if int(n.get('state') or 0) == 2 or await store.get_active_movement(n['id']):
            continue            # 在途地面队：_arrive_supply_column 处理
        events.extend(await _merge_column_events(store, war_id, n, at, caps))
        at = await store.get_command_node(at['id'])
    if events:
        at, re_ev = await remount_on_ground_at_airfield(store, war_id, at)
        events.extend(re_ev)
    return at, events


async def _arrive_supply_column(store, war_id, col, mv, dest_bf, caps,
                                now=None):
    """补给队到达处置（advance_movements 到达分支，替 AT 的落位/占城/
    会战逻辑）：父 AT 在同城 → 汇合并删队；父 AT 挪窝 → 落位后重新寻路
    追击；父 AT 没了 → 就地解散（资源损失，官服口径）。"""
    dels = _supply_stop_events(mv)
    parent = await store.get_command_node(
        int(col.get('parent_command_node_id') or 0))
    # 落位（城市本体）
    await store.update_command_node(
        col['id'], state=0, map_entity_id=int(dest_bf or 0),
        map_entity_type=MAP_ENTITY_BATTLEFIELD,
        **({'airbase_bf': int(dest_bf)} if is_plane_supply(col) else {}))
    col = await store.get_command_node(col['id'])
    # 推序（批内 dels-first 派发，_push_kvcs_batch 按 delete-先/set-后
    # 分组，同 tick.py 到达推序）：del segment/destination 信号先派发清
    # moving，instance/新 segment 行后派发——若旧 del 晚于新 segment set
    # 派发会错误清掉 moving。批内全部行应用后才派发：del 时 instance
    # type 已=1，不误触客户端补给线重钉分支；队已删时 del segment 的
    # getFromId 为 null，客户端直接安全跳过。
    if not parent or int(parent.get('state') or 0) >= 3:
        await store.update_command_node(col['id'], state=3)
        events = _column_delete_events(col) + dels
        log(f'[war] supply column {col["id"]} dissolved '
            f'(parent AT gone)')
        return events
    parent_bf = at_current_battlefield(parent)
    if (not parent_bf or supply_state(col).get('manual')
            or is_plane_supply(col)) and parent_bf != dest_bf:
        events = dels + [('set', b'CommandNodeWarInstance',
                          msg_commandnode_instance_row(col, war_id, caps))]
        if is_plane_supply(col):
            events.append(('set', b'air_commandnode_base',
                           msg_air_commandnode_base_row(col, war_id, dest_bf)))
        return events
    if parent_bf and int(parent_bf) != int(dest_bf or 0):
        # 父 AT 已机动 → 追击（拓扑静态，寻路失败则原地待命，由
        # _cleanup_supply_columns 兜底清理）
        war = {'id': war_id}
        mv2, code = await start_supply_movement(store, war, col, parent_bf, now=now)
        if mv2 is not None:
            events = list(dels)
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
        return dels + [('set', b'CommandNodeWarInstance',
                        msg_commandnode_instance_row(col, war_id, caps))]
    # 汇合（父 AT 在同城 / 父 AT 位置未知（待命回首都）落位失败兜底同此）
    return (await _merge_column_events(store, war_id, col, parent,
                                       caps)) + dels


# 补给队出发城只认带⭐主城（capital 表登记的胜利点城市：三方首都 +
# 中立⭐城，占领后即己方候选）；普通 Town/Factory/据点不作出生点。
# 2026-09-15 玩家口径修正：此前按 gamemap Town/Factory 判定，刚占领的
# 普通 Town（318969767）被选作出发城，实报"补给队从敌方主城出来"。
_STAR_CITY_IDS = None


def _star_city_ids():
    """带⭐主城 battlefield id 集（capital 表全部登记城，模块级缓存）。"""
    global _STAR_CITY_IDS
    if _STAR_CITY_IDS is None:
        _STAR_CITY_IDS = set()
        for c in wardb.capitals():
            try:
                if c.get('battlefield_id'):
                    _STAR_CITY_IDS.add(int(c['battlefield_id']))
            except (TypeError, ValueError):
                continue
    return _STAR_CITY_IDS


def _nearest_supply_city(owners, fac, parent_bf):
    """Closest owned star capital connected to our supply network, by travel time."""

    try:
        fac, parent_bf = int(fac or 0), int(parent_bf or 0)
    except (TypeError, ValueError):
        return 0
    if not parent_bf or not owners:
        return 0
    candidates = []
    reserve = {'faction': fac, 'template_id': reserve_template_id(fac)}
    for bf in sorted(_star_city_ids()):
        if int(owners.get(bf) or 0) != fac or not bf_in_faction_supply(owners, fac, bf):
            continue
        path = fastest_supply_path(parent_bf, bf, reserve, owners)
        if path is not None:
            candidates.append((sum(s['seconds'] for s in path), bf))
    return min(candidates)[1] if candidates else 0


def _near_capital_supply_city(owners, fac, capital_bf, parent_bf, parent_at):
    """Select an owned adjacent city by travel time; fall back to A."""
    candidates = []
    reserve = dict(parent_at, template_id=reserve_template_id(fac))
    for edge in supplyline_adjacency().get(capital_bf, ()):
        bf = int(edge[3])
        if int(owners.get(bf) or 0) != int(fac):
            continue
        path = fastest_supply_path(bf, parent_bf, reserve, owners)
        if path is not None:
            candidates.append((sum(s['seconds'] for s in path), bf))
    return min(candidates)[1] if candidates else capital_bf


async def column_route(store, war, parent_at, near_capital_bf=0,
                       resources=None):
    """Read-only spawn/route validation before spending currency.

    Ground reserves spawn in an owned city adjacent to the nearest supplied
    star capital (or the capital itself if no usable neighbor exists).
    Air-family reserves spawn at the nearest owned airport to that capital.
    Personnel require a ground route; plane-only orders may wait at the airport.
    Legacy near_capital_bf remains an explicit supplied-city override.
    """
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
        anchor = (_nearest_supply_city(owners, fac, parent_bf)
                  or _faction_capital_bf().get(fac, 0))
        spawn_bf = (anchor if is_air_family_at(parent_at) else
            _near_capital_supply_city(owners, fac, anchor, parent_bf, parent_at))
    if not spawn_bf or not parent_bf:
        return 0, parent_bf, False
    if is_air_family_at(parent_at):
        # owners 空（存储异常）→ None 回退不查归属就近机场（宁部署不错杀）
        air_bf = nearest_owned_airfield(spawn_bf, owners or None, fac)
        if not air_bf:
            return 0, parent_bf, False
        needs_ground_route = any(
            int(cat) not in _PLANE_CATS and int(count) > 0
            for cat, count in (resources or {}).items())
        return air_bf, parent_bf, (not needs_ground_route or
            find_supplyline_path(air_bf, parent_bf) is not None)
    if spawn_bf == parent_bf:
        return spawn_bf, parent_bf, True
    return spawn_bf, parent_bf, \
        find_supplyline_path(spawn_bf, parent_bf) is not None


async def spawn_supply_column(store, war, parent_at, resources,
                              near_capital_bf=0, now=None):
    """Create separate reserves and start personnel/ground-vehicle movement.

    Plane reserves wait at their airport. Same-city reserves merge immediately.
    Route validation failures return an error without creating a reserve.
    """
    war_id = war['id']
    fac = int(parent_at.get('faction') or 0)
    caps = _faction_capital_bf()
    spawn_bf, parent_bf, ok = await column_route(
        store, war, parent_at, near_capital_bf=near_capital_bf,
        resources=resources)
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
    # 出生城市落位，作为增援寻路起点。
    await store.update_command_node(
        col['id'], is_deployed=True, map_entity_id=spawn_bf,
        map_entity_type=MAP_ENTITY_BATTLEFIELD,
        supply_state={'source': 'purchase', 'manual': False},
        airbase_bf=(spawn_bf if resources and
                    all(int(c) in _PLANE_CATS for c in resources) else 0))
    col = await store.get_command_node(col['id'])
    if spawn_bf == parent_bf:
        events.extend(await _merge_column_events(store, war_id, col,
                                                 parent_at, caps))
        if is_air_family_at(parent_at):
            # 落地变体在机场并入新买的运输机 → 重新登机转回空中原型
            _p, re_ev = await remount_on_ground_at_airfield(
                store, war_id,
                await store.get_command_node(parent_at['id']))
            events.extend(re_ev)
        return col, events, 0
    if is_plane_supply(col):
        # 飞机/运输机留机场；人员使用下方地面行军分支。
        events.append(('set', b'CommandNodeWarInstance',
                       msg_commandnode_instance_row(col, war_id, caps)))
        events.append(('set', b'air_commandnode_base',
                       msg_air_commandnode_base_row(col, war_id, spawn_bf)))
        log(f'[war] air supply column {col["id"]} waiting for AT '
            f'{parent_at["id"]} at airfield bf={spawn_bf} '
            f'resources={dict(resources or {})}')
        return col, events, 0
    mv, code = await start_supply_movement(store, war, col, parent_bf, now=now)
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


def _supply_stop_events(mv):
    return [('del', b'transport_commandnode_segment', mv['id']),
            ('del', b'transport_commandnode_destination', mv['id']),
            ('del', b'air_transport', mv['id'])]


async def _supply_city_blocker(store, war_id, col, bf):
    """A reserve cannot affect a battle or start one by itself."""
    for wb in await store.list_active_war_battles(war_id):
        if (int(wb.get('battle_type') or BATTLE_TYPE_ASSAULT) == BATTLE_TYPE_ASSAULT
                and int(wb.get('battlefield_id') or 0) == int(bf)):
            return True, int(wb['id'])
    nodes = await store.list_war_command_nodes(war_id)
    hostile = any(not is_reserve(n) and n.get('is_deployed')
        and int(n.get('state') or 0) < 3
        and int(n.get('faction') or 0) != int(col['faction'])
        and at_current_battlefield(n) == bf for n in nodes)
    return hostile, 0


async def _block_supply(store, war, col, bf, bid):
    data = supply_state(col)
    data.update(source='purchase', blocked_bf=bf, blocked_battle_id=bid,
                manual=False, visited_segment=-1)
    await store.update_command_node(col['id'], state=2,
        map_entity_id=bf, map_entity_type=MAP_ENTITY_BATTLEFIELD,
        supply_state=data, **({'airbase_bf': bf} if is_plane_supply(col) else {}))
    col = await store.get_command_node(col['id'])
    events = [('set', b'CommandNodeWarInstance', msg_commandnode_instance_row(
                col, war['id'], _faction_capital_bf()))]
    if is_plane_supply(col):
        events.append(('set', b'air_commandnode_base',
                       msg_air_commandnode_base_row(col, war['id'], bf)))
    if bid:
        rec = await store.get_mission_by_battle(bid)
        if rec:
            events.append(('set', b'missionresource',
                           msg_missionresource_row(col, rec['id'], war['id'])))
    return col, events


async def _ground_supply_encircled(store, war_id, col, bf):
    owners = dict(await store.get_war_cities(war_id))
    neighbors = {e[3] for e in supplyline_adjacency().get(bf, ())}
    return not any(int(owners.get(n) or 0) == int(col['faction']) for n in neighbors)


async def _block_field_supply(store, war, col, mv, seg, index, wb, now_ms):
    owners = dict(await store.get_war_cities(war['id']))
    if not any(int(owners.get(bf) or 0) == int(col['faction'])
               for bf in (seg['from_bf'], seg['to_bf'])):
        return await _destroy_supply(store, col)
    parent = await supply_parent(store, col)
    if parent and any(int(e.get('at_id') or 0) == parent['id']
                      for e in wb.get('factions') or []):
        return await _merge_column_events(store, war['id'], col, parent,
                                          _faction_capital_bf())
    data = supply_state(col)
    data.update(source='purchase', blocked_field_id=int(wb['id']),
        field_movement_id=mv['id'], field_segment=index,
        field_frozen_at_ms=max(seg['begin_ms'], min(now_ms, seg['end_ms'] - 1)),
        field_from_bf=seg['from_bf'], field_to_bf=seg['to_bf'],
        visited_segment=index - 1, manual=False)
    await store.update_command_node(col['id'], state=2,
        map_entity_id=seg['sl_id'], map_entity_type=3, supply_state=data)
    col = await store.get_command_node(col['id'])
    events = _supply_stop_events(mv) + [('set', b'CommandNodeWarInstance',
        msg_commandnode_instance_row(col, war['id'], _faction_capital_bf()))]
    rec = await store.get_mission_by_battle(wb['id'])
    if rec:
        events.append(('set', b'missionresource',
                       msg_missionresource_row(col, rec['id'], war['id'])))
    return events


async def _check_field_supply(store, war, col, now=None):
    from .common import _ts_ms
    data = supply_state(col)
    wb = await store.get_war_battle(int(data['blocked_field_id']))
    mv = await store.get_active_movement(col['id'])
    if wb and int(wb.get('state') or 0) == 0:
        owners = dict(await store.get_war_cities(war['id']))
        if not any(int(owners.get(data[key]) or 0) == int(col['faction'])
                   for key in ('field_from_bf', 'field_to_bf')):
            return await _destroy_supply(store, col)
        parent = await supply_parent(store, col)
        if parent and any(int(e.get('at_id') or 0) == parent['id']
                          for e in wb.get('factions') or []):
            return await _merge_column_events(store, war['id'], col, parent,
                                              _faction_capital_bf())
        return []
    # A finished/missing battle releases the reserve before any loss check.
    now_ms = _ts_ms(now or datetime.datetime.now(datetime.timezone.utc))
    if mv:
        await store.shift_movement_started(mv['id'],
            max(0, now_ms - data['field_frozen_at_ms']) / 1000)
        mv = await store.get_active_movement(col['id'])
    data['blocked_field_id'] = 0
    await store.update_command_node(col['id'], state=1 if mv else 0, supply_state=data)
    events = [('del', b'missionresource', col['id'])]
    if mv:
        events.extend(supply_movement_events(await store.get_command_node(col['id']),
            war['id'], mv, _faction_capital_bf(), int(data['field_segment'])))
    return events


async def advance_supply_movement(store, war, col, mv, now=None):
    """Process every crossed node, even when one tick skips several segments."""
    from .common import _ts_ms
    now = now or datetime.datetime.now(datetime.timezone.utc)
    segs = movement_segments(mv)
    if not segs:
        await store.finish_movement(mv['id'], 1)
        return []
    now_ms = _ts_ms(now)
    caps = _faction_capital_bf()
    data = supply_state(col)
    # Record the last visited node so an already passed battle cannot catch
    # the column from behind on a later tick or after reconnecting.
    visited = int(data.get('visited_segment', -1))
    field_battles = {int(wb['battlefield_id']): wb
        for wb in await store.list_active_war_battles(war['id'])
        if int(wb.get('battle_type') or BATTLE_TYPE_ASSAULT) != BATTLE_TYPE_ASSAULT}
    for index, seg in enumerate(segs):
        if index <= visited or now_ms < seg['begin_ms']:
            continue
        wb = field_battles.get(int(seg.get('sl_id') or 0))
        if wb and _ts_ms(wb.get('started_at')) <= seg['end_ms']:
            return await _block_field_supply(store, war, col, mv, seg, index, wb, now_ms)
        if now_ms < seg['end_ms']:
            continue
        bf = int(seg['to_bf'])
        parent = await supply_parent(store, col)
        blocked, bid = await _supply_city_blocker(store, war['id'], col, bf)
        if (blocked and bid and not is_plane_supply(col)
                and await _ground_supply_encircled(store, war['id'], col, bf)):
            return await _destroy_supply(store, col)
        if parent and at_current_battlefield(parent) == bf:
            await store.finish_movement(mv['id'], 1)
            return (await _merge_column_events(store, war['id'], col, parent, caps)
                    + _supply_stop_events(mv))
        if blocked:
            await store.finish_movement(mv['id'], 2)
            col, events = await _block_supply(store, war, col, bf, bid)
            events = _supply_stop_events(mv) + events
            # The cleanup pass checks encirclement immediately as well.
            events.extend(await _check_blocked_supply(store, war, col, now))
            return events
        data['visited_segment'] = index
    if now_ms >= segs[-1]['end_ms']:
        await store.finish_movement(mv['id'], 1)
        return await _arrive_supply_column(store, war['id'], col, mv,
                                           segs[-1]['to_bf'], caps, now=now)
    idx = next(i for i, seg in enumerate(segs) if now_ms < seg['end_ms'])
    fields = {'supply_state': data}
    if not segs[0].get('air'):
        fields.update(map_entity_id=segs[idx]['sl_id'], map_entity_type=3)
    await store.update_command_node(col['id'], **fields)
    col = await store.get_command_node(col['id'])
    events = [('set', b'CommandNodeWarInstance',
               msg_commandnode_instance_row(col, war['id'], caps))]
    if not segs[0].get('air'):
        events.append(('set', b'transport_commandnode_segment',
            msg_transport_segment_row(col, war['id'], mv, segs[idx])))
    return events


async def check_supply_field_encounters(store, war, now_ms):
    """Catch reserves when an ordinary AT formed a field battle this tick."""
    battles = {int(wb['battlefield_id']): wb
        for wb in await store.list_active_war_battles(war['id'])
        if int(wb.get('battle_type') or BATTLE_TYPE_ASSAULT) != BATTLE_TYPE_ASSAULT}
    events = []
    for mv in await store.list_active_movements(war['id']):
        col = await store.get_command_node(mv['commandnode_id'])
        if not col or not is_reserve(col) or int(col.get('state') or 0) != 1:
            continue
        segs = movement_segments(mv)
        for index, seg in enumerate(segs):
            wb = battles.get(int(seg.get('sl_id') or 0))
            if wb and seg['begin_ms'] <= now_ms < seg['end_ms']:
                events.extend(await _block_field_supply(
                    store, war, col, mv, seg, index, wb, now_ms))
                break
    return events


async def _destroy_supply(store, col):
    mv = await store.get_active_movement(col['id'])
    if mv:
        await store.finish_movement(mv['id'], 2)
    await store.update_command_node(col['id'], state=3)
    return _column_delete_events(col) + (_supply_stop_events(mv) if mv else [])


async def _plane_supply_has_airfield(store, war, col, parent):
    from .at_ops import airfield_bfs
    tpl = wardb.commandnode_templates().get(int(parent['template_id'])) or {}
    if tpl.get('convert_to'):
        tpl = wardb.commandnode_templates().get(int(tpl['convert_to'])) or tpl
    radius = float(tpl.get('transportradius') or 0) / 2
    owners = dict(await store.get_war_cities(war['id']))
    bf = at_current_battlefield(col)
    return any(int(owners.get(af) or 0) == int(col['faction'])
        and (distance := bf_distance(bf, af)) is not None and distance <= radius
        for af in airfield_bfs())


async def _check_blocked_supply(store, war, col, now=None):
    data = supply_state(col)
    bf = int(data.get('blocked_bf') or 0)
    if not bf:
        return []
    blocked, bid = await _supply_city_blocker(store, war['id'], col, bf)
    old_bid = int(data.get('blocked_battle_id') or 0)
    if old_bid and old_bid != bid and not bid:
        blocked = False  # Release the ended battle before testing encirclement.
    if blocked and not is_plane_supply(col) and bid:
        # No friendly retreat route means this blocked supply is surrounded.
        if await _ground_supply_encircled(store, war['id'], col, bf):
            return await _destroy_supply(store, col)
    parent = await supply_parent(store, col)
    if parent and at_current_battlefield(parent) == bf:
        return await _merge_column_events(store, war['id'], col, parent,
                                          _faction_capital_bf())
    if blocked:
        if bid != int(data.get('blocked_battle_id') or 0):
            data['blocked_battle_id'] = bid
            await store.update_command_node(col['id'], supply_state=data)
            if bid:
                rec = await store.get_mission_by_battle(bid)
                if rec:
                    return [('del', b'missionresource', col['id']),
                            ('set', b'missionresource',
                             msg_missionresource_row(col, rec['id'], war['id']))]
        return []
    data.update(blocked_bf=0, blocked_battle_id=0, manual=False, visited_segment=-1)
    await store.update_command_node(col['id'], state=0, supply_state=data)
    col = await store.get_command_node(col['id'])
    parent = await supply_parent(store, col)
    events = [('del', b'missionresource', col['id']),
        ('set', b'CommandNodeWarInstance', msg_commandnode_instance_row(
            col, war['id'], _faction_capital_bf()))]
    if parent and at_current_battlefield(parent) == bf:
        events.extend(await _merge_column_events(store, war['id'], col, parent,
                                                 _faction_capital_bf()))
    elif parent:
        mv, _ = await start_supply_movement(store, war, col,
            at_current_battlefield(parent), now=now)
        if mv:
            events.extend(supply_movement_events(
                await store.get_command_node(col['id']), war['id'], mv,
                _faction_capital_bf()))
    return events


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
            if (int(at.get('airbase_bf') or 0)
                    and supply_state(at).get('source') != 'purchase'):
                # 空降拆分的运输机队（停驻机场待命）：玩家独立财产，
                # 不随父 AT 解散而清（官服 war 结束按剩余资源退款）
                continue
            parent = await store.get_command_node(
                int(at.get('parent_command_node_id') or 0))
            if not parent or int(parent.get('state') or 0) >= 3:
                events.extend(await _destroy_supply(store, at))
                continue
            if (is_plane_supply(at) and not await
                    _plane_supply_has_airfield(store, war, at, parent)):
                events.extend(await _destroy_supply(store, at))
                continue
            if supply_state(at).get('blocked_field_id'):
                events.extend(await _check_field_supply(store, war, at))
                continue
            if supply_state(at).get('blocked_bf'):
                events.extend(await _check_blocked_supply(store, war, at))
                continue
            if await store.get_active_movement(at['id']):
                continue
            bf = at_current_battlefield(at)
            if bf and bf == at_current_battlefield(parent):
                events.extend(await _merge_column_events(store, war['id'], at,
                    parent, _faction_capital_bf()))
                continue
            blocked, bid = await _supply_city_blocker(store, war['id'], at, bf)
            if blocked:
                at, block_events = await _block_supply(store, war, at, bf, bid)
                events.extend(block_events)
                events.extend(await _check_blocked_supply(store, war, at))
            elif not is_plane_supply(at) and not supply_state(at).get('manual'):
                mv, _ = await start_supply_movement(store, war, at,
                    at_current_battlefield(parent))
                if mv:
                    events.extend(supply_movement_events(
                        await store.get_command_node(at['id']), war['id'], mv,
                        _faction_capital_bf()))
        return events
    except Exception as e:
        log(f'[war] cleanup supply columns failed: {e}')
        return []


def _qn_window(rows):
    """队列行 → 窗口 (head, tail)：head=MIN(queue_number)、tail=MAX+1、
    空队 (0,0)（客户端 AssaultTeamModel 对称语义）。"""
    if not rows:
        return (0, 0)
    qns = [int(r.get('queue_number') or 0) for r in rows]
    return (min(qns), max(qns) + 1)


async def supply_windows(store, war=None):
    """Faction-wide and per-resource queue windows.

    Return ``{faction: {'overall': (head, tail), 'categories': {cat:
    (head, tail)}}}``.  FactionResourceQueue consumes ``overall`` while each
    FactionResourceConsumption row consumes its category window.
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
        out = {}
        for fac, rows in by_fac.items():
            cats = {}
            for row in rows:
                cats.setdefault(int(row.get('category_id') or 0), []).append(row)
            out[fac] = {
                'overall': _qn_window(rows),
                'categories': {cat: _qn_window(rs)
                               for cat, rs in cats.items()},
            }
        return out
    except Exception as e:
        log(f'[war] supply_windows failed: {e}')
        return {}


async def _deliver_supply_queue(store, war, fid):
    """Apply at most one valid FIFO order inside the caller's transaction."""
    delivered, removed, events = [], [], []
    for row in await store.list_war_supply_queue(war['id'], fid):
        need, cat = int(row['count']), int(row['category_id'])
        at = await store.get_command_node(int(row['command_node_id'] or 0))
        # Invalid orders must not block on stock that they will never consume.
        if need <= 0 or not at or int(at.get('state') or 0) >= 3:
            await store.dequeue_war_supply([row['id']])
            removed.append(int(row['id']))
            continue
        stock = {int(r['category_id']): int(r['count'])
                 for r in await store.list_war_faction_resources(war['id'], fid)}
        if stock.get(cat, 0) < need:
            break
        if bool(row.get('deliver_as_column')):
            col, events, code = await spawn_supply_column(
                store, war, at, {cat: need}, near_capital_bf=int(
                    row.get('near_capital_battlefield_id') or 0))
            if col is None and code:
                break
        else:
            await store.update_command_node(at['id'],
                resources=_at_add_resources(at, cat, need))
        await store.adjust_war_faction_resources(war['id'], fid, {cat: -need})
        await store.dequeue_war_supply([row['id']])
        removed.append(int(row['id']))
        if not row.get('deliver_as_column'):
            delivered.append(await store.get_command_node(at['id']))
        break
    return delivered, removed, events


async def supply_queue_tick(store):
    """增援队列推进（每阵营 FIFO，队头阻塞）：war_supply_queue 队头行
    库存足够 → 库存转移（HQ → AT 补编）+ 出队；不足 → 整个阵营本 tick
    阻塞（不跳行，保官方队列语义）。

    - AT 歼灭/解散（state>=3 或缺失）→ 出队不退款（warfunds 入队时已扣，
      简化口径）。
    - 交付后重推 AT 行（warmap 广播）+ 库存/队列/production 行
      （resourceview 订阅会话）。"""
    from .tick import broadcast_war_events
    if store is None or getattr(store, 'pool', None) is None:
        return
    try:
        war = await ensure_war(store)
        if war is None:
            return
        import mm_factions           # 延迟 import（防循环依赖）
        from state import FLASH_SESSIONS
        from messages import _push_kvcs
        delivered = []              # 交付过的 AT（重取后供推送）
        dequeued_ids = []           # 出队行 id（交付/清理），供 DELETE 行推送
        for fid in (1, 2, 3):
            async with rts_scope(store, 'supply', war['id'], fid) as bound:
                rows, removed, delivery_events = await _deliver_supply_queue(
                    bound, war, fid)
            delivered.extend(rows)
            dequeued_ids.extend(removed)
            # A failed push must never leave a paid, delivered order pending.
            try:
                if delivery_events:
                    await broadcast_war_events(delivery_events, store)
            except Exception as e:
                log(f'[war] supply delivery push failed after commit: {e}')
        if not delivered and not dequeued_ids:
            return
        # ---- 推送：touched AT 重取后推 armyresource/Instance 行 ----
        events = []
        for at in delivered:
            caps = _faction_capital_bf()
            events.append(('set', b'CommandNodeWarInstance',
                           msg_commandnode_instance_row(at, war['id'], caps)))
            for p in msg_armyresource_rows(at, war['id']):
                events.append(('set', b'armyresource', p))
        if events:
            await broadcast_war_events(events, store)
        windows = await supply_windows(store, war)
        queue_rows = mm_factions._msg_faction_queue_rows(windows)
        cons = mm_factions._msg_faction_resource_rows(
            windows, int(war.get('production_ms') or 0))[1]
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
    f8 pricingId / f9 paymentCurrency / f11 nearCapitalBattlefieldId 与
    f15 discountId 均来自购买时持久化快照；f12-f14 官服遥测字段留空。
    queueNumber>10000000 客户端显示 "unassigned"。"""
    created = qrow.get('created_at')
    if hasattr(created, 'timestamp'):
        ts = int(created.timestamp() * 1000)
    else:
        ts = int(created or 0) or int(time.time() * 1000)
    m = b''
    m += pb_int64(1, int(qrow['id']))                       # id
    m += pb_int64(2, int(at.get('owner_player_id') or 0))   # playerId
    m += pb_int64(3, int(qrow['command_node_id']))          # commandNodeWarInstanceId
    m += pb_int64(4, int(qrow['category_id']))              # armyResourceCategoryId
    m += pb_int64(5, int(qrow.get('war_id') or war_id or 0))
    m += pb_int64(6, int(qrow['faction']))                  # factionId
    m += pb_int64(7, ts)                                    # timestamp
    m += pb_int64(8, int(qrow.get('pricing_id') or 0))       # pricingId
    m += pb_int32(9, int(qrow.get('payment_currency') or 2)) # paymentCurrency
    m += pb_int32(10, int(qrow['queue_number']))            # queueNumber
    m += pb_int64(11, int(qrow.get(
        'near_capital_battlefield_id') or 0))
    m += pb_int64(12, 0)                                    # playerSessionId
    m += pb_string(13, '')                                  # deviceId
    m += pb_string(14, '')                                  # metricsUid
    if int(qrow.get('discount_id') or 0):
        m += pb_int64(15, int(qrow['discount_id']))
    m += pb_int64(16, int(qrow.get(
        'command_node_definition_id') or qrow['command_node_id']))
    m += pb_int32(17, int(qrow.get('count') or 0))          # resourcesNeeded
    return m


def msg_supply_queue_ticket_row(qrow, at, place):
    """CommandNodeSupplyQueueItemTicket 行（队列位次凭证；增援队列窗口
    onTicketUpdated/updateQueueData 用 queueNumber - supplyQueueHead 显示
    "排队第 N 位"，DELETE → 关窗；§10.2 #3 lnTicket）。

    REQUIRED f1 id / f2 playerId / f3 warId / f4 factionId / f7 timestamp /
    f8 queueNumber / f9 queuePlace（f5/f6 无 descriptor 跳过，实锤 schema）。
    ticket id 复用队列行 id（两表键空间独立，出队 DELETE 同 id 推两张表）。"""
    created = qrow.get('created_at')
    if hasattr(created, 'timestamp'):
        ts = int(created.timestamp() * 1000)
    else:
        ts = int(created or 0) or int(time.time() * 1000)
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
    import mm_factions
    from state import FLASH_SESSIONS
    from messages import _push_kvcs
    if war is None:
        war = await ensure_war(store)
    items, tickets = await supply_queue_table_rows(store, war)
    windows = await supply_windows(store, war)
    queue_rows = mm_factions._msg_faction_queue_rows(windows)
    consumption_rows = mm_factions._msg_faction_resource_rows(
        windows, int((war or {}).get('production_ms') or 0))[1]
    for sess in list(FLASH_SESSIONS.values()):
        cmd = getattr(sess, 'resource_view_cmd', None)
        w = getattr(sess, 'writer', None)
        if not cmd or not w:
            continue
        try:
            for row in queue_rows:
                await _push_kvcs(w, cmd, b'FactionResourceQueue', row)
            for row in consumption_rows:
                await _push_kvcs(w, cmd, b'FactionResourceConsumption', row)
            for row in items:
                await _push_kvcs(w, cmd, b'CommandNodeSupplyQueueItem', row)
            for row in tickets:
                await _push_kvcs(w, cmd,
                                 b'CommandNodeSupplyQueueItemTicket', row)
        except Exception:
            pass                # 单会话故障不影响其他订阅者


async def push_supply_queue_deletes(row_ids):
    """Remove queue item/ticket rows from every resource-view subscriber."""
    from state import FLASH_SESSIONS
    from messages import _push_kvcs_delete
    ids = [int(x) for x in row_ids or ()]
    for sess in list(FLASH_SESSIONS.values()):
        cmd = getattr(sess, 'resource_view_cmd', None)
        writer = getattr(sess, 'writer', None)
        if not cmd or not writer:
            continue
        try:
            for rid in ids:
                await _push_kvcs_delete(
                    writer, cmd, b'CommandNodeSupplyQueueItem', rid)
                await _push_kvcs_delete(
                    writer, cmd, b'CommandNodeSupplyQueueItemTicket', rid)
        except Exception:
            pass


def _at_add_resources(at, cat, need):
    """AT 现有 resources（str 键）+ 交付补编 → 新 resources dict（str 键，
    update_command_node JSON 序列化用）。"""
    res = {str(k): int(v) for k, v in _as_resources(at.get('resources')).items()}
    res[str(cat)] = int(res.get(str(cat), 0)) + int(need)
    return res
