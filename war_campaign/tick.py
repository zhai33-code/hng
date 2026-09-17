# -*- coding: utf-8 -*-
"""战争 tick：移动推进、接战判定、城市占领事件、战争事件广播、战后清扫、战斗结算与资源缩放。

原 war_campaign.py 阶段 3.5 拆分（docs/warserver-refactor-plan.md §3.5）：
符号逐字搬运，行为不变。"""

import random
import time
import datetime
from protocol import (
    pb_int64,
    pb_sfixed64,
    )
import mm_config
import wardb
from state import log
from .common import (
    morale_defaults,
    current_morale,
    _as_resources,
    MAP_ENTITY_BATTLEFIELD,
    MAP_ENTITY_ACCESSPOINT,
    MAP_ENTITY_SUPPLYLINE,
    BATTLE_TYPE_ASSAULT,
    BATTLE_TYPE_SKIRMISH,
    BATTLE_TYPE_ENCOUNTER,
    _ts_ms,
    supplyline_adjacency,
    )
from .lifecycle import (
    ensure_war,
    check_victory,
    _faction_capital_bf,
    )
from .battle_msgs import (
    msg_transport_segment_row,
    msg_transport_destination_row,
    msg_battle_row,
    msg_missionresource_row,
    )
from .at_ops import (
    rank_from_xp,
    underdog_multipliers,
    faction_player_counts,
    _tpl_of,
    land_paradrop_split,
    is_radius_unit,
    is_paradrop_unit,
    is_aircraft,
    is_airfield,
    is_infantry_at,
    msg_air_commandnode_base_row,
    is_reserve,
    remount_on_ground_at_airfield,
    remount_parked_planes_on_arrival,
    )
from .warmap_view import (
    msg_battlefieldstatus_row,
    _ap_battlefield,
    msg_commandnode_definition_row,
    msg_commandnode_instance_row,
    msg_commandnode_morale_row,
    msg_armyresource_rows,
    msg_hq_resource_rows,
    )
from .at_movement import (
    at_current_battlefield,
    movement_segments,
    current_segment_index,
    moving_ats_by_supplyline,
    start_retreat_movement,
    )
from .supply import (
    _arrive_supply_column,
    _cleanup_supply_columns,
    merge_waiting_columns,
    supply_queue_tick,
    )


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
            if int(at.get('state') or 0) == 2:
                # 野外会战冻结（§2）：会战中不推进，结算时按会战时长顺延
                # started_at 恢复行军
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
                    if segs[-1].get('paradrop_return'):
                        # 运输机队返航到达（空降拆分产物自动返航，
                        # reserve 模板但非补给队）：落位机场待命（不汇合/
                        # 不追击/不占城/不成战），删在途行，airbase_bf
                        # 指向落位机场（旧机 re-use 合并以 airbase_bf
                        # 匹配）；落地伞兵回机场补给时免费
                        # re-use（_merge_parked_planes）
                        dest_bf = int(segs[-1]['to_bf'] or 0)
                        await store.update_command_node(
                            at['id'], state=0, map_entity_id=dest_bf,
                            map_entity_type=MAP_ENTITY_BATTLEFIELD,
                            airbase_bf=dest_bf)
                        at = await store.get_command_node(at['id'])
                        # 到达推序（空中：del segment 先清 moving
                        # （commandnodeStoppedMoving），set instance
                        # 更新 location 后 del air_transport 才触发
                        # onAirDestinationUpdates→updateAssaultTeamStatus
                        # 重算 status=新城市名——反序则 status 按旧城市
                        # 冻结到重登（2026-09-16 伞兵城市名卡上一城）；
                        # 重钉不依赖派发序：批内全部行先应用（setObject
                        # copyFromObject 原地改），onAirAssaultTeamMove
                        # Ended→setStationaryPosition 读到的新
                        # mapEntityId 已就位，不会钉回出发地。
                        # 注：broadcast_war_events 走 preserve_order
                        # 保序批次，此处事件顺序即派发顺序。
                        events.append(('del', b'transport_commandnode_segment',
                                       mv['id']))
                        events.append(('set', b'CommandNodeWarInstance',
                                       msg_commandnode_instance_row(
                                           at, war_id, caps)))
                        events.append(('del', b'air_transport', mv['id']))
                        log(f'[war] 运输机队返航到达 node={at["id"]} '
                            f'-> airfield {dest_bf}')
                        # ---- 运输机找伞兵（反向自动重整）：落位机场
                        # 停有同 owner 落地伞兵变体 → 免费并机 + 伞兵转
                        # 回空中原型（at 可能被替换为伞兵 AT）----
                        at, fleet_events = (
                            await remount_parked_planes_on_arrival(
                                store, war_id, at))
                        events.extend(fleet_events)
                        continue
                    # 补给队到达：汇合/追击/解散（不占城、不成战）
                    dest_bf = segs[-1]['to_bf']
                    events.extend(await _arrive_supply_column(
                        store, war_id, at, mv, dest_bf, caps, now=now))
                    continue
                dest_ap = int(mv.get('dest_ap') or 0)
                dest_bf = (segs[-1]['to_bf'] if not dest_ap
                           else _ap_battlefield(dest_ap))
                # 到达落位：官服口径——进攻 AT 一律站城市本体（图标
                # 叠城，多线进攻不拆站出兵点；进攻方向差异体现在对局
                # 详情兵线分配，见 force_pool.assign_lines）
                if dest_ap:
                    ent, ent_type = dest_ap, MAP_ENTITY_ACCESSPOINT
                else:
                    ent, ent_type = dest_bf, MAP_ENTITY_BATTLEFIELD
                await store.update_command_node(
                    at['id'], state=0, map_entity_id=ent,
                    map_entity_type=ent_type)
                at = await store.get_command_node(at['id'])
                # 到达推序（2026-09-11 重钉修复 + 2026-09-15 城市名修复 +
                # 2026-09-16 空中城市名修复）：
                # 1) del segment 先派发——清 schema.moving
                #    （commandnodeStoppedMoving），否则 instance 派发时
                #    updatedWarInstance 重钉条件 hasChangedMapEntityOrType
                #    && !moving 不满足，单位卡在动画终点（出兵点偏移位）。
                # 2) set instance——location 更新为新城市（
                #    updateAssaultTeamMapEntity）。
                # 3) del destination/air_transport 最后派发——
                #    onDestinationUpdates/onAirDestinationUpdates→
                #    updateAssaultTeamStatus 重算 status=location，此时
                #    读到的已是新城市名。若 del 先于 set instance，status
                #    按在途旧值冻结：非交战 AT 靠后续 armyresource/queue
                #    事件自愈，交战中 AT 无任何触发 → 部队列表城市名卡
                #    在上一城到重登（空中 air_transport 单表承载
                #    moving+destination 两职责，但 moving 已被 del
                #    segment 清掉、重钉由 onAirAssaultTeamMoveEnded 读
                #    批内已应用的新 mapEntityId 完成，del 放最后是
                #    安全的）。
                events.append(('del', b'transport_commandnode_segment',
                               mv['id']))
                if is_air:
                    events.append(('set', b'CommandNodeWarInstance',
                                   msg_commandnode_instance_row(
                                       at, war_id, caps)))
                    events.append(('del', b'air_transport', mv['id']))
                else:
                    events.append(('set', b'CommandNodeWarInstance',
                                   msg_commandnode_instance_row(
                                       at, war_id, caps)))
                    events.append(('del',
                                   b'transport_commandnode_destination',
                                   mv['id']))
                # ---- 落地伞兵走回己方机场：与停驻同机场的同主运输机队
                # 自动重整（免费并机 + 转回空中原型，可再次空降）----
                remounted = False
                if not is_air:
                    at, remount_events = await remount_on_ground_at_airfield(
                        store, war_id, at)
                    events.extend(remount_events)
                    remounted = bool(remount_events)
                # ---- 本体到站取补给（官服：空军系补给的飞行员/飞机/伞兵/
                #      运输机在出生机场原地等待，本体飞/走达后合并完成
                #      补给；落地变体持满运输机一并重新登机）----
                at, supply_wait_events = await merge_waiting_columns(
                    store, war_id, at, caps)
                events.extend(supply_wait_events)
                # ---- 空中半径单位到达（§10.2 #6/#7）----
                if is_radius_unit(at) and not remounted:
                    if not segs[-1].get('radius_attack'):
                        # 换基地到达：airbase 行 + airbase_bf 指向新机场
                        await store.update_command_node(
                            at['id'], airbase_bf=int(segs[-1]['to_bf']))
                        events.append((
                            'set', b'air_commandnode_base',
                            msg_air_commandnode_base_row(
                                at, war_id, segs[-1]['to_bf'])))
                    elif is_paradrop_unit(at):
                        # 空降作战到达（★2026-09-11 敌情分流修复：玩家报
                        # "敌城空降运输机秒走"）：目标有敌驻军/同城进行中
                        # 会战 → 不拆分，保持空中原型编制（cat4 伞兵 +
                        # cat10 运输机成对入战——战斗内伞兵从运输机跳伞
                        # 出生，结算时才拆分返航）；无敌情（友方/中立/
                        # 空城）→ 伞兵落地变体（convert_to，*_on_ground
                        # 半径无限、地面速度）+ 运输机拆成玩家名下独立
                        # planes-AT 停原机场（官服 Spaatz 实锤 "returns
                        # the transport planes to the origin of the
                        # transportation"；非回阵营库存——玩家私有，
                        # 落地伞兵回机场补给时优先免费合并旧机重登机
                        # "re-use old transport planes"）
                        if await _dest_hostile(store, war_id, at, dest_bf):
                            log(f'[war] paradrop onto hostile battlefield '
                                f'{dest_bf}: AT {at["id"]} 以空中原型入战'
                                f'（cat4+cat10，运输机结算时返航）')
                        elif int(_tpl_of(at).get('convert_to') or 0):
                            at, split_events = await land_paradrop_split(
                                store, war_id, at)
                            events.extend(split_events)
                        else:
                            await store.update_command_node(
                                at['id'], airbase_bf=0)
                            events.append(('del', b'air_commandnode_base',
                                           at['id']))
                    # 飞机作战到达：airbase 行/airbase_bf 保持原基地
                    # （作战后结算拖回机场，届时更新）
                # ---- 会战成局桩：目标城市有敌对 AT 驻扎 ----
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
                # ---- 换段敌情检查（途经城市 = 上一段终点）----
                # 敌驻军/我方参战会战 → 中止行军落位途经城，成局/增援
                # （修复：途经交战城市不停下直接穿城而过）；第三阵营
                # 会战（我方不参战）正常通过（两方制契约）
                pass_bf = int(segs[idx]['from_bf'] or 0)
                if await _passthrough_hostile(store, war_id, at, pass_bf):
                    await store.finish_movement(mv['id'], 1)
                    await store.update_command_node(
                        at['id'], state=0, map_entity_id=pass_bf,
                        map_entity_type=MAP_ENTITY_BATTLEFIELD)
                    at = await store.get_command_node(at['id'])
                    # 中止落位推序同到达分支：del segment → set
                    # instance → del destination（destination 的 del
                    # 须在 instance 之后派发，否则客户端 AT 列表 status
                    # 按在途旧 location="" 冻结，不显示途经城市名）
                    events.append(('del', b'transport_commandnode_segment',
                                   mv['id']))
                    events.append(('set', b'CommandNodeWarInstance',
                                   msg_commandnode_instance_row(
                                       at, war_id, caps)))
                    events.append(('del',
                                   b'transport_commandnode_destination',
                                   mv['id']))
                    events.extend(await _check_battle_formation(
                        store, war_id, at, pass_bf))
                    log(f'[war] AT {at["id"]} 途经敌情城市 {pass_bf} '
                        f'中止行军入战')
                    continue
                # 换段占城：AT 已抵达新段起点城市（=上一段终点），无敌
                # 驻军/无进行中会战则易主（途经城市陆续占领）
                events.extend(await _capture_city_events(
                    store, war_id, at.get('faction'), segs[idx]['from_bf']))
        # ---- 野外遭遇成局（§2）：同补给线段在途敌对 AT 相遇 ----
        events.extend(await _check_field_encounters(store, war_id, now_ms))
        return events
    except Exception as e:
        log(f'[war] advance_movements failed: {e}')
        return []


async def _reinforce_live_battle(wb, at):
    """增援 AT 并入已开战的会战资源池（官服口径：增援给进行中战斗
    补充可出生兵力）。matchmaking 属上层模块（import war_campaign，
    函数级引用防 import 环）。"""
    try:
        from matchmaking import add_reinforcement_to_live_mission
        await add_reinforcement_to_live_mission(int(wb['id']), at)
    except Exception as e:
        log(f'[war] reinforce live battle {wb.get("id")} failed: {e}')


async def _battle_mission(store, battle_id):
    """会战关联的活任务记录（missionresource 行 missionid 数据源）。

    get_mission_by_battle 只认 state 0/1（活任务）；孤儿会战（record
    已死待重建）返回 None，调用方跳过推 missionresource 行——行缺时
    客户端 isInBattle=false，重建后无行也不会误标，安全方向。"""
    try:
        return await store.get_mission_by_battle(int(battle_id))
    except Exception:
        return None


async def _dest_hostile(store, war_id, at, dest_bf):
    """空降目标是否有敌情（敌驻军 / 同城进行中会战）——判据与
    _check_battle_formation 同源。

    有敌情 → 伞兵保持空中原型编制（cat4 + cat10 成对）入战，运输机
    在会战结算时才拆分返航（官方敌区交战时序：战败"运输机会返回
    出发的机场，然后按步兵进攻失败的逻辑撤退"、战胜"运输机飞回
    出发的机场"、包围歼灭含运输机）；无敌情（友方/中立/空城）→
    落地即拆分返航（官方口径）。"""
    if not dest_bf:
        return False
    my_faction = int(at.get('faction') or 0)
    for wb in await store.list_active_war_battles(war_id):
        if (int(wb.get('battle_type') or BATTLE_TYPE_ASSAULT)
                == BATTLE_TYPE_ASSAULT
                and int(wb.get('battlefield_id') or 0) == int(dest_bf)):
            return True
    others = await store.list_war_command_nodes(war_id)
    return any(o['id'] != at['id'] and o.get('is_deployed')
               and not is_reserve(o)
               and int(o.get('state') or 0) < 3
               and int(o.get('faction') or 0) != my_faction
               and at_current_battlefield(o) == int(dest_bf)
               for o in others)


async def _city_owner_faction(store, war_id, bf_id):
    """城主阵营（war_cities.owner_faction），未知返回 0。"""
    try:
        owners = dict(await store.get_war_cities(int(war_id)))
        return int(owners.get(int(bf_id)) or 0)
    except Exception as e:
        log(f'[war] get_war_cities({war_id}) failed: {e}')
        return 0


def _assault_sides(owner_faction, factions_in_battle):
    """城战攻守口径（官方语义 2026-09-14 用户实测修正）：守方=城主
    阵营，攻方=进攻城的一方（非城主阵营）；城主未知（0/未初始化）退
    回既有简化：先到者（factions[0]，成局时到达 AT 在前）=攻方。
    返回 (attacker, defender)。"""
    facs = [int(f or 0) for f in factions_in_battle if int(f or 0) > 0]
    if not facs:
        return 0, 0
    first = facs[0]
    if int(owner_faction or 0) in facs:
        owner = int(owner_faction)
        return next(f for f in facs if f != owner), owner
    return first, next((f for f in facs if f != first), 0)


def _nearest_friendly_bf(from_bf, faction, city_owners):
    """补给线 BFS 最近友方城（owner==faction），不含出发城；无则 0。
    战败 AT 撤退落点（war1.md：撤退回最近友方城市）。"""
    from_bf = int(from_bf or 0)
    adj = supplyline_adjacency()
    seen = {from_bf}
    queue = [from_bf]
    while queue:
        cur = queue.pop(0)
        for edge in adj.get(cur, ()):
            nb = edge[3]
            if nb in seen:
                continue
            seen.add(nb)
            if int(city_owners.get(nb) or 0) == int(faction):
                return nb
            queue.append(nb)
    return 0


async def _passthrough_hostile(store, war_id, at, bf):
    """途经城市敌情判据（与 _check_battle_formation 同源）：
    - 同城进行中会战且我方阵营参战 → 停（增援入战）；
    - 无进行中会战且城内有敌驻军 → 停（成局拦截）；
    - 第三阵营会战（我方不参战）→ 不停，正常通过（两方制契约）。"""
    if not bf:
        return False
    my_faction = int(at.get('faction') or 0)
    for wb in await store.list_active_war_battles(war_id):
        if (int(wb.get('battle_type') or BATTLE_TYPE_ASSAULT)
                == BATTLE_TYPE_ASSAULT
                and int(wb.get('battlefield_id') or 0) == int(bf)):
            return my_faction in {int(f.get('faction') or 0)
                                  for f in (wb.get('factions') or [])}
    others = await store.list_war_command_nodes(war_id)
    return any(o['id'] != at['id'] and o.get('is_deployed')
               and not is_reserve(o)
               and int(o.get('state') or 0) < 3
               and int(o.get('faction') or 0) != my_faction
               and at_current_battlefield(o) == int(bf)
               for o in others)


async def _check_battle_formation(store, war_id, at, dest_bf):
    """到达敌情检查（M1 会战桩 → M2 成局）：目标城市驻有敌对 AT →
    war_battles 登记 + war mission record 落库（任务列表可见）。

    已有同城市进行中会战 → 参战阵营的增援 AT 标记入战（state=2）不重复
    建战；第三阵营（battle factions 之外的阵营）AT 到场视为无关方正常
    驻扎（官方 battle 行两方制/ factioncount 契约，excludedFactionId
    排除方不得参战），否则结算遍历 factions 名单永不释放 → 卡死。
    返回追加推送事件。"""
    if not dest_bf:
        return []
    events = []
    my_faction = int(at.get('faction') or 0)
    others = await store.list_war_command_nodes(war_id)
    # state>=3（歼灭/解散软删）不算驻军——否则被歼 AT 持续触发成局；
    # 补给队（is_reserve）非战斗部队不算守军（与 _check_field_encounters
    # 口径一致：敌方补给队驻城不挡占领、不拉进会战）
    enemies = [o for o in others
               if o['id'] != at['id'] and o.get('is_deployed')
               and not is_reserve(o)
               and int(o.get('state') or 0) < 3
               and int(o.get('faction') or 0) != my_faction
               and at_current_battlefield(o) == int(dest_bf)]
    if not enemies:
        return events
    battle_factions = None
    for wb in await store.list_active_war_battles(war_id):
        if (int(wb.get('battle_type') or BATTLE_TYPE_ASSAULT)
                != BATTLE_TYPE_ASSAULT):
            continue      # 野外会战 battlefield_id=补给线 id（与城市 id
                          # 不同空间，撞号不算同城局）
        if int(wb.get('battlefield_id') or 0) == int(dest_bf):
            battle_factions = {int(f.get('faction') or 0)
                               for f in (wb.get('factions') or [])}
            if my_faction in battle_factions:
                # 参战阵营增援：入战（state=2）不重复建战；须追加进
                # factions 名单（append_war_battle_faction）——否则结算
                # 遍历 factions 释放不到该 AT → 永久卡在会战中
                await store.append_war_battle_faction(
                    int(wb['id']), at['id'], my_faction)
                await store.update_command_node(at['id'], state=2)
                wb2 = await store.get_war_battle(int(wb['id']))
                if wb2:
                    wb['factions'] = wb2.get('factions') or []
                events.append(('set', b'battle', msg_battle_row(wb)))
                # 增援 AT 入战：missionresource 行（新行 ADD，客户端
                # isInBattle=true → 战图"撤退"入口出现）
                mrec = await _battle_mission(store, wb['id'])
                if mrec:
                    events.append(('set', b'missionresource',
                                   msg_missionresource_row(
                                       at, mrec['id'], war_id)))
                # 已开局的战斗服资源池同步并入增援编制
                await _reinforce_live_battle(wb, at)
            # else：第三阵营 AT 到场 → 不入战，正常驻扎（两方制契约）
            return events
    factions = [{'at_id': at['id'], 'faction': at.get('faction') or 0}]
    factions += [{'at_id': e['id'], 'faction': e.get('faction') or 0}
                 for e in enemies]
    wb = await store.create_war_battle(war_id, dest_bf, factions,
                                       battle_type=BATTLE_TYPE_ASSAULT)
    await store.update_command_node(at['id'], state=2)
    for e in enemies:
        await store.update_command_node(e['id'], state=2)
    if wb:
        # M2：会战 → war mission record（GetMissionList 展示，玩家加入成
        # 真人局；mission_id = mission_records serial id，battle_id 关联）。
        # 攻守口径：城主=守方、非城主=攻方（_assault_sides，2026-09-14
        # 用户实测修正：苏联驻守德军进攻曾反）；城主未知退回先到者=攻方。
        # 缺口 3（war-para-battle-briefing-investigation.md）：mission
        # record 创建失败不能只记日志——"有 battle 无 mission"半态下
        # 战图交战标记可点但 GetMissionDetails 恒 43 → 简报窗口秒关
        # （点不开）；回滚整个成局（关会战 + 释放 AT 回待命，battle
        # 行不推送），下次到达/D 修复重试。
        try:
            preset_pool = (mm_config.gamemode_cfg(10).get('fallback_presets')
                           or [])
            atk_f, def_f = _assault_sides(
                await _city_owner_faction(store, war_id, dest_bf),
                [at.get('faction') or 0]
                + [e.get('faction') or 0 for e in enemies])
            mid = await store.create_mission_record(
                _preset_for_battlefield(preset_pool, dest_bf),
                attacker_faction=atk_f,
                defender_faction=def_f,
                name=f'war battle {wb["id"]} battlefield {dest_bf}',
                map_entity_id=int(dest_bf),
                battle_id=int(wb['id']))
            log(f'[war] battle {wb["id"]} formed → mission record id={mid} '
                f'bf={dest_bf} atk={atk_f} def={def_f}')
            events.append(('set', b'battle', msg_battle_row(wb)))
            # 参战 AT 全部入战：missionresource 行（新行 ADD，客户端
            # isInBattle=true；missionid=本次成局的 record id）
            for mr_at in [at] + enemies:
                events.append(('set', b'missionresource',
                               msg_missionresource_row(mr_at, mid, war_id)))
        except Exception as ex:
            log(f'[war] create war mission record failed: {ex} '
                f'(battle {wb["id"]} rolled back)')
            try:
                await store.end_war_battle(int(wb['id']), 0)
                await store.update_command_node(at['id'], state=0)
                for e in enemies:
                    await store.update_command_node(e['id'], state=0)
            except Exception as ex2:
                log(f'[war] rollback battle {wb["id"]} failed: {ex2}')
    return events


def _preset_for_battlefield(preset_pool, bf_id):
    """按战场 gamemap 从池内选同图 preset（据点成局地图类型修复，
    2026-09-14 用户实测：机场城成局随机选到城镇图）。

    战场 gamemap 经出兵点模板映射（wardb.gamemap_for_battlefield，
    1438 战场零冲突实锤）；池内无同图 preset / 未知图 → 回退随机。"""
    try:
        gm = wardb.gamemap_for_battlefield(bf_id)
        if gm:
            matched = [p for p in (preset_pool or [])
                       if int((wardb._load_preset(int(p)) or {}).get(3, 0)
                              or 0) == gm]
            if matched:
                return random.choice(matched)
    except Exception:
        pass
    return random.choice(preset_pool) if preset_pool else 0


def _preset_for_field_battle(btype):
    """野外会战（encounter/skirmish）选图：按 battle_type 取对应图池
    （war1.md：双方纯步兵=encounter 池 / 含侦察·装甲=skirmish 池），
    绝不用城内 assault 图池（2026-09-16 实测 Berlin-a 夺控战错发
    assault 图——此前按邻近城市 gamemap 从 war assault 池选）。
    准备期为临时值：roster 中途变化（增援入场改变构成）由
    matchmaking._war_mission_preset 在准备期结束成局时定图修正。"""
    pool = (mm_config.WAR_ENCOUNTER_PRESETS
            if int(btype) == BATTLE_TYPE_ENCOUNTER
            else mm_config.WAR_SKIRMISH_PRESETS)
    return random.choice(pool) if pool else 0


async def _check_field_encounters(store, war_id, now_ms=None):
    """野外遭遇成局（war-gameplay-fix-plan §2）：同一补给线段上出现
    ≥2 个敌对阵营的在途 AT → 会战。

    - 双方均为纯步兵族（is_infantry_at）→ encounter（type 5）；
      任一方含侦察/装甲族 → skirmish（type 3）。
    - battle 行 battlefield_id=补给线 id + position=各 AT 段进度折算的
      补给线绝对位置（ap1→ap2 方向 0-1）均值。
    - 参战 AT 全部 state=2（行军冻结，advance_movements 跳过推进，
      结算时 shift_movement_started 按会战时长顺延恢复）。
    - 补给队（is_reserve）不参战；同段已有进行中野外会战不重复成局。
    - mission record：map_entity=遭遇段起点城市（简报落点回退用），
      攻方=首个 AT 阵营，守方=首个异阵营 AT。
    返回推送事件。"""
    events = []
    try:
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        ats = await store.list_war_command_nodes(war_id)
        groups = moving_ats_by_supplyline(ats)
        if not groups:
            return events
        active = await store.list_active_war_battles(war_id)
        # 同段已有进行中野外会战：交战阵营的迟到 AT 入战（对齐城市会战
        # 增援口径 append_war_battle_faction + state=2 + live 池并入），
        # 第三阵营照常行军（两方制契约）；不重复建战
        field_battles = {int(wb.get('battlefield_id') or 0): wb
                         for wb in active
                         if int(wb.get('battle_type')
                                or BATTLE_TYPE_ASSAULT) != BATTLE_TYPE_ASSAULT}
        sl_by_id = {int(s.get('id') or 0): s
                    for s in wardb.supplylines()}
        for sl_id, group in sorted(groups.items()):
            group = [a for a in group if not is_reserve(a)
                     and a.get('is_deployed')]
            facs = {int(a.get('faction') or 0) for a in group}
            wb0 = field_battles.get(sl_id)
            if wb0 is not None:
                bfacs = {int(f.get('faction') or 0)
                         for f in (wb0.get('factions') or [])}
                joined = [a for a in group
                          if int(a.get('faction') or 0) in bfacs]
                for a in joined:
                    await store.append_war_battle_faction(
                        int(wb0['id']), a['id'], a.get('faction'))
                    await store.update_command_node(a['id'], state=2)
                if joined:
                    wb2 = await store.get_war_battle(int(wb0['id']))
                    if wb2:
                        wb0['factions'] = wb2.get('factions') or []
                        events.append(
                            ('set', b'battle', msg_battle_row(wb0)))
                        # 增援 AT 入战：missionresource 新行 ADD（客户端
                        # isInBattle=true）；孤儿会战（record 已死）跳过
                        mrec = await _battle_mission(store, wb0['id'])
                        if mrec:
                            for a in joined:
                                events.append(
                                    ('set', b'missionresource',
                                     msg_missionresource_row(
                                         a, mrec['id'], war_id)))
                        for a in joined:
                            await _reinforce_live_battle(wb0, a)
                    bypass = [a['id'] for a in group if a not in joined]
                    log(f'[war] field battle {wb0["id"]} reinforced by '
                        f'{[a["id"] for a in joined]} on supplyline {sl_id}'
                        + (f'（{bypass} 第三阵营照常行军）' if bypass else ''))
                continue
            if len(facs) < 2:
                continue
            # ---- position：各 AT 当前段进度 → 补给线绝对位置均值 ----
            sl = sl_by_id.get(sl_id) or {}
            ap1 = int(sl.get('ap1') or 0)
            positions = []
            for a in group:
                mv = await store.get_active_movement(a['id'])
                if not mv:
                    continue
                segs = movement_segments(mv)
                idx = current_segment_index(segs, now_ms)
                if idx < 0:
                    continue
                seg = segs[idx]
                span = max(1, int(seg['end_ms']) - int(seg['begin_ms']))
                prog = min(1.0, max(0.0, (now_ms - int(seg['begin_ms']))
                                    / span))
                positions.append(prog if int(seg.get('from_ap') or 0) == ap1
                                 else 1.0 - prog)
            position = (sum(positions) / len(positions)
                        if positions else 0.5)
            # ---- 类型：双方纯步兵 = encounter，否则 skirmish ----
            btype = (BATTLE_TYPE_ENCOUNTER
                     if all(is_infantry_at(a) for a in group)
                     else BATTLE_TYPE_SKIRMISH)
            factions = [{'at_id': a['id'], 'faction': a.get('faction') or 0}
                        for a in group]
            wb = await store.create_war_battle(
                war_id, sl_id, factions, battle_type=btype,
                position=position)
            for a in group:
                await store.update_command_node(a['id'], state=2)
            if not wb:
                continue
            # 缺口 3 同源（war-para-battle-briefing-investigation.md）：
            # mission record 创建失败回滚整个成局（关会战 + 恢复行军
            # state=1，battle 行不推送），避免"有 battle 无 mission"半态
            # （战图标记可点但简报恒 43 秒关）
            try:
                attacker = int(group[0].get('faction') or 0)
                defender = next(
                    (int(a.get('faction') or 0) for a in group
                     if int(a.get('faction') or 0) != attacker), 0)
                # 简报落点：遭遇段起点城市（真实 battlefield id，
                # matchmaking battlefield_pos 回退用）
                near_bf = 0
                mv = await store.get_active_movement(group[0]['id'])
                if mv:
                    segs = movement_segments(mv)
                    idx = current_segment_index(segs, now_ms)
                    if idx >= 0:
                        near_bf = int(segs[idx].get('from_bf') or 0)
                mid = await store.create_mission_record(
                    _preset_for_field_battle(btype),
                    attacker_faction=attacker,
                    defender_faction=defender,
                    name=f'war battle {wb["id"]} supplyline {sl_id}',
                    map_entity_id=near_bf,
                    battle_id=int(wb['id']))
                log(f'[war] field battle {wb["id"]} formed on supplyline '
                    f'{sl_id} type={btype} pos={position:.2f} '
                    f'factions={sorted(facs)} → mission {mid}')
                events.append(('set', b'battle', msg_battle_row(wb)))
                # 参战 AT 全部入战：missionresource 行（新行 ADD，客户端
                # isInBattle=true；missionid=本次成局的 record id）
                for a in group:
                    events.append(('set', b'missionresource',
                                   msg_missionresource_row(
                                       a, mid, war_id)))
                field_battles[sl_id] = wb
            except Exception as ex:
                log(f'[war] create field mission record failed: {ex} '
                    f'(battle {wb["id"]} rolled back)')
                try:
                    await store.end_war_battle(int(wb['id']), 0)
                    for a in group:
                        await store.update_command_node(a['id'], state=1)
                except Exception as ex2:
                    log(f'[war] rollback field battle {wb["id"]} '
                        f'failed: {ex2}')
        return events
    except Exception as e:
        log(f'[war] _check_field_encounters failed: {e}')
        return events


async def rebuild_orphan_battle_missions(store):
    """重启孤儿会战修复：活跃 war_battle 的 mission record 已死（启动
    清理 close_stale_missions 只关 record 不动 war_battles）→ 重建
    state=0 record。

    battle→mission 链（详情 GetMissionDetails / 加入 _start_match / 增援
    add_reinforcement_to_live_mission 统一走 get_mission_by_battle
    states 0/1）断链期间：战图战斗标记仍可点但详情回 43 秒关、加入
    战局直接退池。攻守口径与成局一致（factions 首行=攻、首异阵营=守）；
    野外局简报落点 = 首个参战 AT 当前段起点城市。返回重建条数。"""
    if store is None or getattr(store, 'pool', None) is None:
        return 0
    try:
        war = await ensure_war(store)
        if war is None:
            return 0
        now_ms = int(time.time() * 1000)
        preset_pool = (mm_config.gamemode_cfg(10)
                       .get('fallback_presets') or [])
        repaired = 0
        for wb in await store.list_active_war_battles(war['id']):
            try:
                if await store.get_mission_by_battle(int(wb['id'])):
                    continue          # record 存活，无需重建
            except Exception:
                continue
            facs = [int(f.get('faction') or 0)
                    for f in (wb.get('factions') or [])]
            btype = int(wb.get('battle_type') or BATTLE_TYPE_ASSAULT)
            if btype == BATTLE_TYPE_ASSAULT:
                # 城战攻守 = 城主守方 / 非城主攻方（与成局口径一致）
                atk, dfn = _assault_sides(
                    await _city_owner_faction(
                        store, war['id'], wb.get('battlefield_id')), facs)
            else:
                atk = facs[0] if facs else 0
                dfn = next((f for f in facs if f != atk), 0)
            try:
                if btype == BATTLE_TYPE_ASSAULT:
                    mid = await store.create_mission_record(
                        _preset_for_battlefield(
                            preset_pool, wb.get('battlefield_id')),
                        attacker_faction=atk, defender_faction=dfn,
                        name=f'war battle {wb["id"]} battlefield '
                             f'{wb["battlefield_id"]}',
                        map_entity_id=int(wb.get('battlefield_id') or 0),
                        battle_id=int(wb['id']))
                else:
                    near_bf = 0
                    entries = wb.get('factions') or []
                    if entries:
                        mv = await store.get_active_movement(
                            int(entries[0].get('at_id') or 0))
                        if mv:
                            segs = movement_segments(mv)
                            idx = current_segment_index(segs, now_ms)
                            if idx >= 0:
                                near_bf = int(segs[idx].get('from_bf') or 0)
                    mid = await store.create_mission_record(
                        _preset_for_field_battle(btype),
                        attacker_faction=atk, defender_faction=dfn,
                        name=f'war battle {wb["id"]} supplyline '
                             f'{wb["battlefield_id"]}',
                        map_entity_id=near_bf,
                        battle_id=int(wb['id']))
                repaired += 1
                log(f'[war] rebuilt mission record id={mid} for active '
                    f'battle {wb["id"]} (restart orphan, atk={atk} '
                    f'def={dfn})')
            except Exception as e:
                log(f'[war] rebuild mission record for battle '
                    f'{wb["id"]} failed: {e}')
        return repaired
    except Exception as e:
        log(f'[war] rebuild_orphan_battle_missions failed: {e}')
        return 0


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
                if (int(wb.get('battle_type') or BATTLE_TYPE_ASSAULT)
                        != BATTLE_TYPE_ASSAULT):
                    continue  # 野外会战挂在补给线上，不锁城市归属
                if (int(wb.get('battlefield_id') or 0) == bf_id
                        and int(wb['id']) != int(ignore_battle_id or 0)):
                    return []
            for o in await store.list_war_command_nodes(war_id):
                if (o.get('is_deployed')
                        and not is_reserve(o)
                        and int(o.get('state') or 0) < 3
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


def _armyresource_row_at_id(payload):
    """armyresource 事件行的归属 AT id：set 行解析 proto f1（id=at*1000+cat）
    前导 varint；del 行 payload 即行 id。解析失败（HQ base|cat 行等）返回
    None（调用方按"非 reserve AT"放行）。"""
    try:
        if isinstance(payload, (bytes, bytearray)):
            if not payload or payload[0] != 0x08:      # field 1, varint
                return None
            val = shift = 0
            for i in range(1, len(payload)):
                b = payload[i]
                val |= (b & 0x7F) << shift
                if not (b & 0x80):
                    return val // 1000
                shift += 7
            return None
        return int(payload) // 1000
    except Exception:
        return None


async def _reserve_owner_map(store):
    """当前战争 reserve AT 归属表 {at_id: owner_player_id}（广播过滤用）。

    查询失败返回 {}（等同不过滤，维持可用性——快照/属主路径不受影响）。"""
    try:
        war = await ensure_war(store)
        if war is None:
            return {}
        ats = await store.list_war_command_nodes(war['id'])
        return {int(a['id']): int(a.get('owner_player_id') or 0)
                for a in ats if is_reserve(a)}
    except Exception as e:
        log(f'[war] reserve owner map failed: {e}')
        return {}


async def broadcast_war_events(events, store=None):
    """战争事件广播：全部 warmapview 订阅会话（含敌我视角）。整批事件
    单条 KeyValueChangeSet 推送（原子性见 _push_kvcs_batch——到达等
    跨行状态迁移要求 set 与 del 行在同一派发批内互相可见）。
    preserve_order=True：到达/途经中止推序是 del segment → set
    instance → del destination 的交错序列（客户端 AT 列表 status
    重算时机依赖此序，2026-09-15 城市名修复），不能被 dels-first
    重排；其余流程事件本身即 del 先 set 后，保序模式下派发不变。

    reserve AT（补给队/空降拆分运输机队）的 armyresource 行只对属主
    会话可见（官方口径）：非属主客户端 updateArmyResources 把"他人
    reserve 的 parent 不在本人 AT 列表"写进 _reservesThatRequireParent
    Update，开战争图 handleReservesMissingParentInfo 把 null parent
    直传 addReserveResourcesToParentAssaultTeam → param2.templateId
    Error #1009 卡死（2026-09-15 19:31 VPS 实锤）。store=None 时退化为
    不过滤（兼容无 store 的旧调用面）。"""
    if not events:
        return
    from state import FLASH_SESSIONS
    from messages import _push_kvcs_batch
    restricted = {}
    if (store is not None and getattr(store, 'pool', None) is not None
            and any(e[1] == b'armyresource' for e in events)):
        restricted = await _reserve_owner_map(store)
    for sess in list(FLASH_SESSIONS.values()):
        cmd = getattr(sess, 'warmap_view_cmd', None)
        w = getattr(sess, 'writer', None)
        if not cmd or not w:
            continue
        out = events
        if restricted:
            try:
                pid = sess.ensure_player and await sess.ensure_player()
            except Exception:
                pid = None
            pid = int(pid) if pid is not None else 0
            out = []
            for e in events:
                if e[1] == b'armyresource':
                    at_id = _armyresource_row_at_id(e[2])
                    owner = restricted.get(at_id) if at_id else None
                    # pid=0（未知/未登录）：宁缺毋错，reserve 行全部丢弃
                    if owner is not None and (not pid or owner != pid):
                        continue
                out.append(e)
        try:
            # 大事件批次达阈值整体 gzip（zipchunk，2026-09-17 P1）；
            # combat 连接（ZServer）禁 zip——官方 warserver 从不给
            # 专用服发 KVCS
            await _push_kvcs_batch(
                w, cmd, out, preserve_order=True,
                allow_zip=not getattr(sess, 'is_combat_server', False))
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
    from .production import automission_tick, production_tick
    events = await advance_movements(store)
    events.extend(await _cleanup_supply_columns(store))
    await _sweep_ended_war_leftovers(store)
    await automission_tick(store)
    # 准备期到点的会战成局（排队玩家拉进 FPS 局；门控口径见
    # matchmaking.war_battle_formable；延迟 import 防循环依赖）
    try:
        import matchmaking as _mm
        await _mm.form_due_war_battles()
    except Exception as e:
        log(f'[war] form_due_war_battles failed: {e}')
    await production_tick(store)
    await supply_queue_tick(store)
    if events:
        await broadcast_war_events(events, store)
    # ★ 直接占领触发胜负判定（2026-09-08 补）：到达/换段/空运占城（尤其
    # 敌方主城）后立即 check_victory——此前仅会战结算路径检查，直接占领
    # 最后主城后战争挂起直到下一次会战结算才结束（用户报"占了对面主城
    # 战争没结束/没弹结算界面"）。胜利 → check_victory 内部广播 war 行
    # （结束行 f7=胜方 + 续战行 ADD）驱动客户端 WarIsOver 界面。
    if any(tag == b'battlefieldstatus' for _, tag, _ in events):
        try:
            victory = await check_victory(store)
            if victory:
                log(f'[war] war ended by direct capture: '
                    f'faction {victory} victorious')
        except Exception as e:
            log(f'[war] check_victory after direct capture failed: {e}')


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
                            loss_ratios=None, captured=None, is_auto=False):
    """FPS 会战结算回写（BattleReport 之后、release_match 之前调用）。

    loss_ratios：可选 (胜方损耗比, 败方损耗比) 覆盖（automission 用
    WAR_AUTOMISSION_* 系数）；缺省用 WAR_BATTLE_*_LOSS。
    captured：可选 {category_id: count} 战场缴获（败方 AT 未投入的池剩余，
    combat.py 从 force_pool 快照），入账胜方阵营库存并推送 armyresource 行。
    is_auto：True = 系统判定（automission 无人超时），war1.md「系统判定
    胜负不提供经验」——跳过 XP/rank/battles 计数与 warfunds 发放，
    编制损耗/士气/占城/终态/check_victory 照常。

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
        # 排队残留清理：本会战 war 桶内仍在排队的玩家出池回大厅
        # （automission 结算不开 FPS 局，不清则客户端永卡"匹配中"）。
        # 清理失败不阻断结算；延迟 import 防 war_campaign↔matchmaking 环
        try:
            import matchmaking as _mm
            await _mm.drop_war_battle_queue(int(battle_id))
        except Exception as ex:
            log(f'[war] settle drop queue(battle={battle_id}) failed: {ex}')
        winner = int(winner_faction or 0)
        # 野外会战（encounter/skirmish）：battlefield_id=补给线 id（与城市
        # 不同 id 空间）→ 城市易主/结算后再成局均不适用；幸存 AT 的冻结
        # 行军按会战时长顺延恢复（shift_movement_started）
        is_field = (int(wb.get('battle_type') or BATTLE_TYPE_ASSAULT)
                    != BATTLE_TYPE_ASSAULT)
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

        # ③/D 前置：攻方 + 城战目标城市（飞机战后返回判据需在结算循环内）
        attacker = 0
        try:
            mrec = await store.get_mission_record(int(mission_id))
            attacker = int((mrec or {}).get('attacker_faction') or 0)
        except Exception:
            pass
        dest_bf = 0 if is_field else int(wb.get('battlefield_id') or 0)

        for entry in (wb.get('factions') or []):
            at = await store.get_command_node(int(entry.get('at_id') or 0))
            if not at or int(at.get('state') or 0) != 2:
                continue                      # 已解散/不在战中
            # AT 离战：结算后必不留在 state=2（歼灭=3/投降·释放=0）→ 删
            # missionresource 行（客户端 isInBattle=false，撤退入口消失；
            # DELETE 分支按行 id=AT id 精确清 _assaultTeamsInBattle）
            events.append(('del', b'missionresource', at['id']))
            won = bool(winner) and int(at.get('faction') or 0) == winner
            ratio = win_loss if won else lose_loss
            new_res = _scale_resources(
                _as_resources(at.get('resources')), ratio)
            # ② 士气（官方快照+时间戳模型）：先物化当前值（含随时间
            #    回复，客户端 getMorale 同口径）再 ±，写回新快照并重置
            #    morale_updated_at —— 服务器判定与客户端显示不再分叉
            now_dt = datetime.datetime.now(datetime.timezone.utc)
            morale = current_morale(at, morale_cfg, now_dt)
            morale += (mm_config.WAR_MORALE_GAINED_PER_WIN if won
                       else -mm_config.WAR_MORALE_LOST_PER_BATTLE)
            morale = max(int(morale_cfg['min']),
                         min(int(at.get('max_morale') or 100), morale))
            # ⑤ warfunds（先行：与状态回写解耦，玩家不存在不阻断结算）；
            #    underdog 阵营（#1）按 f7 倍率放大每局/胜方加成；
            #    is_auto 系统判定不发放（war1.md）
            owner = int(at.get('owner_player_id') or 0)
            _, wf_mult = ud_mults.get(
                int(at.get('faction') or 0), (1.0, 1.0))
            if (owner and mm_config.WAR_WARFUNDS_PER_BATTLE
                    and not is_auto):
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
                    at['id'], state=3, resources={}, morale=morale,
                    morale_updated_at=now_dt)
                events.append(('del', b'CommandNodeWarInstance', at['id']))
                # 野外会战冻结的行军随歼灭中止（城战 AT 已到达无行军，
                # get_active_movement None 自然跳过）
                mv = await store.get_active_movement(at['id'])
                if mv:
                    await store.finish_movement(mv['id'], 2)
                    events.append(('del', b'transport_commandnode_segment',
                                   mv['id']))
                    events.append(('del',
                                   b'transport_commandnode_destination',
                                   mv['id']))
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
            #     underdog 阵营（#1）增量按 f6 倍率放大；
            #     is_auto 系统判定不提供经验（war1.md）：battles/won/xp/
            #     rank 保持原值不回写
            if is_auto:
                fields = {'resources': new_res, 'morale': morale,
                          'morale_updated_at': now_dt}
                rank_up = False
            else:
                battles = int(at.get('battles') or 0) + 1
                battles_won = int(at.get('battles_won') or 0) + (1 if won else 0)
                xp_mult, _ = ud_mults.get(
                    int(at.get('faction') or 0), (1.0, 1.0))
                xp = max(battles * 5 + battles_won * 25,
                         int(at.get('xp') or 0)
                         + int((5 + (25 if won else 0)) * xp_mult))
                rank = max(int(at.get('rank') or 0), rank_from_xp(xp))
                fields = {'resources': new_res, 'morale': morale,
                          'morale_updated_at': now_dt,
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
                    if is_field:
                        # 野外会战冻结的行军须中止（撤回首都，无在途动画）
                        mv = await store.get_active_movement(at['id'])
                        if mv:
                            await store.finish_movement(mv['id'], 2)
                            events.append((
                                'del', b'transport_commandnode_segment',
                                mv['id']))
                            events.append((
                                'del',
                                b'transport_commandnode_destination',
                                mv['id']))
                    log(f'[war] battle {battle_id}: AT {at["id"]} morale '
                        f'{morale} <= surrender {surrender}, retreated to '
                        f'capital {cap_bf}')
            if 'state' not in fields:
                # B 修复（会战生命周期审计 #2）：幸存 AT 释放回待命驻扎
                # 原地（state=2 → 0）——否则战后永远被 RC_COMMANDNODE_IN_
                # BATTLE 挡死，打过一仗的 AT 全部不能再移动/推进
                fields['state'] = 0
            await store.update_command_node(at['id'], **fields)
            if is_field and fields.get('state') == 0:
                # 野外会战冻结恢复：movement started_at 顺延会战时长，
                # 段绝对时间由 started_at 累加 → 从冻结点续行军
                mv = await store.get_active_movement(at['id'])
                if mv:
                    held_s = max(
                        0.0, (time.time() * 1000
                              - _ts_ms(wb.get('started_at'))) / 1000.0)
                    await store.shift_movement_started(mv['id'], held_s)
            at = await store.get_command_node(at['id'])
            # ★ 飞机战后返回原机场（用户纠正 #2）：战局结束飞机飞回出发
            # 机场；例外：攻击并占领敌方机场 → 留在占领机场（成为新基地）
            if (not is_field and is_aircraft(at)
                    and at.get('is_deployed')
                    and int(at.get('state') or 0) == 0):
                home = int(at.get('airbase_bf') or 0)
                if won and winner == attacker and is_airfield(dest_bf):
                    dest_home = int(dest_bf)   # 攻占敌方机场：留在占领机场
                else:
                    dest_home = home           # 其余：返回出发机场
                if dest_home:
                    await store.update_command_node(
                        at['id'], map_entity_id=dest_home,
                        map_entity_type=MAP_ENTITY_BATTLEFIELD,
                        airbase_bf=dest_home)
                    at = await store.get_command_node(at['id'])
                    events.append(('set', b'air_commandnode_base',
                                   msg_air_commandnode_base_row(
                                       at, war_id, dest_home)))
                    log(f'[war] battle {battle_id}: aircraft AT {at["id"]} '
                        f'{at.get("name")!r} returned to airfield {dest_home}')
            if rank_up:
                # rank 变化须重推 Definition 行（f7），客户端才会刷新升级选项
                events.append(('set', b'CommandNodeDefinition',
                               msg_commandnode_definition_row(at)))
            events.append(('set', b'CommandNodeWarInstance',
                           msg_commandnode_instance_row(at, war_id, caps)))
            # 士气快照/时间戳已变 → 重推 Morale 行（客户端按
            # moraleStartTimeStamp 本地回算当前士气，结算前未推会显示旧值）
            events.append(('set', b'CommandNodeMorale',
                           msg_commandnode_morale_row(at, war_id)))
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
            # ★ 伞兵战后落地（官方敌区交战时序收口，2026-09-11）：会战
            # 结算时运输机才返航——伞兵转落地变体驻留战场（胜方留城/
            # 败方原地待后续撤退），运输机拆分飞回原机场（官方原文：
            # 胜"夺取该城，运输机飞回出发的机场"/败"运输机会返回出发
            # 的机场"）。敌区到达不再提前拆分 → 空中原型带着 cat10 入战
            # （战斗内伞兵从运输机跳伞出生 + 战损/歼灭含运输机）。
            # ★方向防反跳：模板表 108↔114 双向 convert_to（落地变体
            # 回机场重新登机也走 convert_to）——仅空中原型（非
            # *_on_ground）拆分，落地变体幂等跳过；automission 败方
            # _paras_land 先行拆分后此处自然跳过。野外会战无空中原型。
            if (not is_field and at.get('is_deployed')
                    and int(at.get('state') or 0) == 0
                    and int(_tpl_of(at).get('convert_to') or 0)
                    and not str(_tpl_of(at).get('name') or '')
                    .endswith('_on_ground')):
                at, split_events = await land_paradrop_split(
                    store, war_id, at)
                events.extend(split_events)

        # ③ 城市易主：攻方胜 → 占领（守方胜城市不变）；野外会战无城可占
        #    （battlefield_id=补给线 id），dest_bf 置 0 同时跳过 ③/D
        if winner and attacker and winner == attacker and dest_bf:
            # force：会战胜方占领（结算语义，跳过敌驻军检查）；
            # 复用助手补推 supplylinestatus/accesspointstatus 关联行
            city_events = await _capture_city_events(
                store, war_id, winner, dest_bf, force=True)
            if city_events:
                events.extend(city_events)
                log(f'[war] battle {battle_id}: attacker faction {winner} '
                    f'captured battlefield {dest_bf}')
                # ③a 胜方驻守：占领后存活胜方 AT 从出兵点（进攻到达
                # 站位，type=2）移入城市本体，官服"胜方驻守"口径
                for entry in (wb.get('factions') or []):
                    wat = await store.get_command_node(
                        int(entry.get('at_id') or 0))
                    if (not wat or not wat.get('is_deployed')
                            or int(wat.get('faction') or 0) != winner
                            or int(wat.get('state') or 0) != 0
                            or int(wat.get('map_entity_type') or 0)
                            != MAP_ENTITY_ACCESSPOINT
                            or _ap_battlefield(int(
                                wat.get('map_entity_id') or 0)) != dest_bf):
                        continue    # 已歼灭 / 投降 / 非出兵点站位
                    await store.update_command_node(
                        wat['id'], map_entity_id=int(dest_bf),
                        map_entity_type=MAP_ENTITY_BATTLEFIELD)
                    wat = await store.get_command_node(wat['id'])
                    events.append(('set', b'CommandNodeWarInstance',
                                   msg_commandnode_instance_row(
                                       wat, war_id, caps)))
                    log(f'[war] battle {battle_id}: winner AT {wat["id"]} '
                        f'{wat.get("name")!r} moved in from accesspoint '
                        f'to battlefield {dest_bf}')

        # ③b 城战非胜方撤退（官方 war1.md：战败方撤退离场、胜方驻守）：
        #     败方 AT 原地释放会与胜方同城对峙 → D 修复立即重开新局 →
        #     无限连环消耗编制（2026-09-14 用户实测：无人加入的局每
        #     5.5 分钟结算一次反复扣兵、战局永不结束）。撤退落点优先
        #     进攻路线上最近的己方据点：沿本次进攻 movement 路径从
        #     dest_bf 逐段回退，取路线上最近的仍己方城市；路线全丢或
        #     无路径 → 最近友方城（补给线 BFS），无友方城回首都兜底。
        #     tie（winner=0）同样撤退（2026-09-16 缺口 1，
        #     war-para-battle-briefing-investigation.md）：原 `and winner`
        #     门控下 tie 双方原地释放（state=0 留同城）→ D 修复立即
        #     再成局 → formed/settled 无限循环（battle 行 churn = 战图
        #     交战标记反复消失重建、客户端"城市信息图"反复被自动关掉）。
        #     留守口径：有胜方 = 胜方留守（原行为）；tie = 城主（守方）
        #     留守、攻方撤退；城主未知（0）时 tie 全撤（宁撤不循环）。
        if not is_field and dest_bf:
            city_owners = dict(await store.get_war_cities(war_id))
            stay = ({int(winner)} if winner
                    else {int(city_owners.get(int(dest_bf)) or 0)}
                    - {0})
            for entry in (wb.get('factions') or []):
                at = await store.get_command_node(
                    int(entry.get('at_id') or 0))
                if (not at or not at.get('is_deployed')
                        or int(at.get('faction') or 0) in stay
                        or int(at.get('state') or 0) != 0
                        or at_current_battlefield(at) != dest_bf):
                    continue    # 胜方/城主驻守 / 已歼灭 / 已投降撤回 / 已离场
                f_at = int(at.get('faction') or 0)
                # 进攻路线撤退：沿最近一次 movement 路径尾部通往 dest_bf
                # 的连续段由近及远回退，取路线上最近的仍己方据点
                # （多段进攻不再一路退回出发点）
                omv = await store.get_last_movement(at['id'])
                target = 0
                expected = int(dest_bf)
                for seg in reversed((omv or {}).get('path') or []):
                    if int(seg.get('to_bf') or 0) != expected:
                        continue    # 只认通往 dest_bf 的连续尾段
                    hop = int(seg.get('from_bf') or 0)
                    if not hop:
                        break
                    if int(city_owners.get(hop) or 0) == f_at:
                        target = hop
                        break
                    expected = hop    # 该城已丢 → 沿路线继续回退
                if not target:
                    target = _nearest_friendly_bf(
                        dest_bf, f_at, city_owners) or caps.get(f_at)
                if not target:
                    continue
                # 撤退走真实行军（segment/destination 行驱动动画，
                # 与手动撤退一致，不再闪现）；行军中 state=1 自动脱离
                # 会战判定，到达走 advance_movements 通用落位
                rmv, rcode = await start_retreat_movement(store, at, target)
                if rmv is None:
                    # 行军发起失败（无航路等）：直接落位兜底
                    await store.update_command_node(
                        at['id'], map_entity_id=int(target),
                        map_entity_type=MAP_ENTITY_BATTLEFIELD)
                    at = await store.get_command_node(at['id'])
                    events.append(('set', b'CommandNodeWarInstance',
                                   msg_commandnode_instance_row(at, war_id, caps)))
                else:
                    at = await store.get_command_node(at['id'])
                    rsegs = movement_segments(rmv)
                    if rsegs:
                        events.append(
                            ('set', b'transport_commandnode_segment',
                             msg_transport_segment_row(
                                 at, war_id, rmv, rsegs[0])))
                    events.append(
                        ('set', b'transport_commandnode_destination',
                         msg_transport_destination_row(
                             at, war_id, rmv, rsegs, retreat=True)))
                    events.append(
                        ('set', b'CommandNodeWarInstance',
                         msg_commandnode_instance_row(at, war_id, caps)))
                log(f'[war] battle {battle_id}: AT {at["id"]} '
                    f'{at.get("name")!r} (faction {f_at}) retreated '
                    f'from battlefield {dest_bf} to {target}'
                    + ('' if winner else '（tie 攻方撤退）'))

        # ⑤b 缴获入账（war 局）：败方 AT 未投入的池剩余 → 胜方阵营库存，
        #     推送 HQ armyresource 行（在线客户端资源视图实时上涨）
        if winner and captured:
            try:
                await store.adjust_war_faction_resources(
                    war_id, winner, captured)
                import mm_factions      # 延迟 import（防循环依赖）
                hq = mm_factions.FACTION_SUPREME_COMMANDS.get(winner)
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

        # D 修复（会战生命周期审计 #4）：结算后再成局——城里可能还驻
        # 有敌对 AT（连环三方局：美 vs 德打完 → 美胜占城 → 与苏 AT 同
        # 城对峙），以非城主阵营 AT 为基准再跑一次成局判定，否则两军
        # 永久僵持（谁也不打谁）。基准口径（2026-09-14 用户实测修正）：
        # 攻方=非城主阵营（城主=守方）——原"胜方 AT 为基准"在守方胜
        # 时把驻守方标成攻方 → 攻防反转；正常两方局败方已 ③b 撤退，
        # 无候选自然不成新局（战局终结、战图标记消失）。
        # 候选从 wb.factions 扩为全城驻军（2026-09-16 缺口 2，
        # war-para-battle-briefing-investigation.md）：战斗期间到场的
        # 第三阵营 AT 被两方制排除、不在 factions 名单 → 只扫 factions
        # 时其与城主的对峙永不再成局（城上长期挂 AT 图标但永远没有
        # 会战 → 点击只能开"未有交战"的城市信息图）；候选口径与
        # _check_battle_formation enemies 判据同源（已部署/非补给队/
        # state=0 驻城/非城主）。
        if dest_bf:
            owner_after = await _city_owner_faction(store, war_id, dest_bf)
            candidates = sorted(
                (o for o in await store.list_war_command_nodes(war_id)
                 if o.get('is_deployed') and not is_reserve(o)
                 and int(o.get('state') or 0) == 0
                 and (not owner_after
                      or int(o.get('faction') or 0) != owner_after)
                 and at_current_battlefield(o) == int(dest_bf)),
                key=lambda o: int(o['id']))
            if candidates:
                events.extend(await _check_battle_formation(
                    store, war_id, candidates[0], dest_bf))
        return events
    except Exception as e:
        log(f'[war] settle_war_battle({battle_id}) failed: {e}')
        return []
