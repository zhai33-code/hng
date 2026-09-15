# -*- coding: utf-8 -*-
"""补给系统：补给圈判定、补给车队的生成/合并/到达/清理、补给窗口与补给队列 tick。

原 war_campaign.py 阶段 3.5 拆分（docs/warserver-refactor-plan.md §3.5）：
符号逐字搬运，行为不变。"""

import time
from protocol import (
    pb_int32,
    pb_int64,
    pb_string,
    )
import wardb
from state import log
from .common import (
    _as_resources,
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
    )
from .at_ops import (
    RC_OK,
    RC_COMMANDNODE_WAR_INSTANCE_NOT_FOUND,
    template_resources,
    RC_CANNOT_FIND_COMMANDNODE,
    is_reserve,
    reserve_template_id,
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
    start_movement,
    )


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
    dels = [('del', b'transport_commandnode_segment', mv['id']),
            ('del', b'transport_commandnode_destination', mv['id'])]
    parent = await store.get_command_node(
        int(col.get('parent_command_node_id') or 0))
    # 落位（城市本体）
    await store.update_command_node(
        col['id'], state=0, map_entity_id=int(dest_bf or 0),
        map_entity_type=MAP_ENTITY_BATTLEFIELD)
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
    if parent_bf and int(parent_bf) != int(dest_bf or 0):
        # 父 AT 已机动 → 追击（拓扑静态，寻路失败则原地待命，由
        # _cleanup_supply_columns 兜底清理）
        mv2, code = await start_movement(store, col, parent_bf, now=now)
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
    """父 AT 位置出发 BFS 最近己方在网带⭐**主城**（补给队默认出生点）。

    沿补给线拓扑逐跳外扩（途经敌城/中立城/据点只过不停），首个"归属
    本方、在己方补给网内且为带⭐主城（capital 表登记城）"的城市即最近
    出发城；跳过父城本身（在网 AT 的补给保持 OnTheWay 出队语义，不因
    同城瞬并）。无归属数据（owners 空，DB 异常）或全图无候选 → 0
    （调用方回退阵营首都）。"""
    try:
        fac, parent_bf = int(fac or 0), int(parent_bf or 0)
    except (TypeError, ValueError):
        return 0
    if not parent_bf or not owners:
        return 0
    cities = _star_city_ids()
    adj = supplyline_adjacency()
    seen = {parent_bf}
    queue = [parent_bf]
    while queue:
        cur = queue.pop(0)
        for edge in adj.get(cur, ()):
            nb = edge[3]
            if nb in seen:
                continue
            seen.add(nb)
            try:
                owned = int(owners.get(nb, 0) or 0) == fac
            except (TypeError, ValueError):
                owned = False
            if (owned and nb in cities
                    and bf_in_faction_supply(owners, fac, nb)):
                return nb
            queue.append(nb)
    return 0


async def column_route(store, war, parent_at, near_capital_bf=0):
    """补给队路线预检（纯读，不落库不扣款，购买侧拒单用）：确定出生点
    （f6 己方在网城 → 最近己方在网带⭐主城 → 阵营首都兜底）并按全图
    补给线拓扑验证可达性。

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
        spawn_bf = (_nearest_supply_city(owners, fac, parent_bf)
                    or _faction_capital_bf().get(fac, 0))
    if not spawn_bf or not parent_bf:
        return 0, parent_bf, False
    if spawn_bf == parent_bf:
        return spawn_bf, parent_bf, True
    return spawn_bf, parent_bf, \
        find_supplyline_path(spawn_bf, parent_bf) is not None


async def spawn_supply_column(store, war, parent_at, resources,
                              near_capital_bf=0, now=None):
    """生成补给队并寻路：最近己方在网带⭐主城出生（官服口径：就近出发
    沿补给线机动；near_capital_bf=f6 玩家指定出发城优先，须己方在网否则
    忽略；全图无⭐主城候选回退阵营首都），载 resources（{cat: n}），沿
    补给线机动到父 AT 所在城市。

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
            if int(at.get('airbase_bf') or 0):
                # 空降拆分的运输机队（停驻机场待命）：玩家独立财产，
                # 不随父 AT 解散而清（官服 war 结束按剩余资源退款）
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
        queue_rows = mm_factions._msg_faction_queue_rows(windows)
        cons = mm_factions._msg_faction_resource_rows(windows)[1]
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
