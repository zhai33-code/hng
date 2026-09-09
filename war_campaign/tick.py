# -*- coding: utf-8 -*-
"""战争 tick：移动推进、接战判定、城市占领事件、战争事件广播、战后清扫、战斗结算与资源缩放。

原 war_campaign.py 阶段 3.5 拆分（docs/warserver-refactor-plan.md §3.5）：
符号逐字搬运，行为不变。"""

import random
import time
from protocol import (
    pb_int64,
    pb_sfixed64,
    )
import mm_config
import wardb
from state import log
from .common import (
    morale_defaults,
    _as_resources,
    MAP_ENTITY_BATTLEFIELD,
    MAP_ENTITY_ACCESSPOINT,
    MAP_ENTITY_SUPPLYLINE,
    BATTLE_TYPE_ASSAULT,
    BATTLE_TYPE_SKIRMISH,
    BATTLE_TYPE_ENCOUNTER,
    _ts_ms,
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
    )
from .at_ops import (
    rank_from_xp,
    underdog_multipliers,
    faction_player_counts,
    _tpl_of,
    is_radius_unit,
    is_paradrop_unit,
    is_aircraft,
    is_airfield,
    is_infantry_at,
    msg_air_commandnode_base_row,
    is_reserve,
    )
from .warmap_view import (
    msg_battlefieldstatus_row,
    _ap_battlefield,
    msg_commandnode_definition_row,
    msg_commandnode_instance_row,
    msg_armyresource_rows,
    msg_hq_resource_rows,
    )
from .at_movement import (
    at_current_battlefield,
    movement_segments,
    current_segment_index,
    moving_ats_by_supplyline,
    )
from .supply import (
    _arrive_supply_column,
    _cleanup_supply_columns,
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
                        # 换基地到达：airbase 行 + airbase_bf 指向新机场
                        await store.update_command_node(
                            at['id'], airbase_bf=int(segs[-1]['to_bf']))
                        events.append((
                            'set', b'air_commandnode_base',
                            msg_air_commandnode_base_row(
                                at, war_id, segs[-1]['to_bf'])))
                    elif is_paradrop_unit(at):
                        # 空降作战到达：伞兵落地变体（convert_to，*_on_
                        # ground 半径无限、地面速度）+ 删 airbase 行 +
                        # airbase_bf 清 0
                        cvt = int(_tpl_of(at).get('convert_to') or 0)
                        if cvt and wardb.commandnode_templates().get(cvt):
                            await store.update_command_node(
                                at['id'], template_id=cvt, airbase_bf=0)
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
                        else:
                            await store.update_command_node(
                                at['id'], airbase_bf=0)
                        events.append(('del', b'air_commandnode_base',
                                       at['id']))
                    # 飞机作战到达：airbase 行/airbase_bf 保持原基地
                    # （作战后结算拖回机场，届时更新）
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
        # ---- 野外遭遇成局（§2）：同补给线段在途敌对 AT 相遇 ----
        events.extend(await _check_field_encounters(store, war_id, now_ms))
        return events
    except Exception as e:
        log(f'[war] advance_movements failed: {e}')
        return []


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
        field_active = {(int(wb.get('battlefield_id') or 0)
                         for wb in active
                         if int(wb.get('battle_type')
                                or BATTLE_TYPE_ASSAULT) != BATTLE_TYPE_ASSAULT)}
        sl_by_id = {int(s.get('id') or 0): s
                    for s in wardb.supplylines()}
        for sl_id, group in sorted(groups.items()):
            group = [a for a in group if not is_reserve(a)
                     and a.get('is_deployed')]
            facs = {int(a.get('faction') or 0) for a in group}
            if len(facs) < 2 or sl_id in field_active:
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
            events.append(('set', b'battle', msg_battle_row(wb)))
            field_active.add(sl_id)
            try:
                preset_pool = (mm_config.gamemode_cfg(10)
                               .get('fallback_presets') or [])
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
                    random.choice(preset_pool) if preset_pool else 0,
                    attacker_faction=attacker,
                    defender_faction=defender,
                    name=f'war battle {wb["id"]} supplyline {sl_id}',
                    map_entity_id=near_bf,
                    battle_id=int(wb['id']))
                log(f'[war] field battle {wb["id"]} formed on supplyline '
                    f'{sl_id} type={btype} pos={position:.2f} '
                    f'factions={sorted(facs)} → mission {mid}')
            except Exception as e:
                log(f'[war] create field mission record failed: {e}')
        return events
    except Exception as e:
        log(f'[war] _check_field_encounters failed: {e}')
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
    from .production import automission_tick, production_tick
    events = await advance_movements(store)
    events.extend(await _cleanup_supply_columns(store))
    await _sweep_ended_war_leftovers(store)
    await automission_tick(store)
    await production_tick(store)
    await supply_queue_tick(store)
    if events:
        await broadcast_war_events(events)
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
            won = bool(winner) and int(at.get('faction') or 0) == winner
            ratio = win_loss if won else lose_loss
            new_res = _scale_resources(
                _as_resources(at.get('resources')), ratio)
            morale = int(at.get('morale') or 0)
            morale += (mm_config.WAR_MORALE_GAINED_PER_WIN if won
                       else -mm_config.WAR_MORALE_LOST_PER_BATTLE)
            morale = max(0, min(int(at.get('max_morale') or 100), morale))
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
                    at['id'], state=3, resources={}, morale=morale)
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
                fields = {'resources': new_res, 'morale': morale}
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

        # D 修复（会战生命周期审计 #4）：结算后再成局——胜方占城后城里
        # 可能还驻有敌对 AT（连环三方局：美 vs 德打完 → 美胜占城 →
        # 与苏 AT 同城对峙），以本场胜方 AT 为基准再跑一次成局判定，
        # 否则两军永久僵持（谁也不打谁）。
        if dest_bf:
            for entry in (wb.get('factions') or []):
                at2 = await store.get_command_node(
                    int(entry.get('at_id') or 0))
                if (at2 and int(at2.get('state') or 0) == 0
                        and at2.get('is_deployed')
                        and int(at2.get('faction') or 0) == winner
                        and at_current_battlefield(at2) == dest_bf):
                    events.extend(await _check_battle_formation(
                        store, war_id, at2, dest_bf))
                    break
        return events
    except Exception as e:
        log(f'[war] settle_war_battle({battle_id}) failed: {e}')
        return []
