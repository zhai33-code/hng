# -*- coding: utf-8 -*-
"""CommandNode（AT）移动：行军分段与速度、空运、半径移动及其校验。

原 war_campaign.py 阶段 3.5 拆分（docs/warserver-refactor-plan.md §3.5）：
符号逐字搬运，行为不变。"""

import datetime
import math
import mm_config
import wardb
from .common import (
    _as_resources,
    MAP_ENTITY_BATTLEFIELD,
    MAP_ENTITY_ACCESSPOINT,
    MAP_ENTITY_SUPPLYLINE,
    _ts_ms,
    find_supplyline_path,
    _bf_pos,
    )
from .lifecycle import (
    _faction_capital_bf,
    )
from .at_ops import (
    RC_OK,
    _battling_battlefields,
    RC_CANNOT_FIND_COMMANDNODE,
    is_airfield,
    _tpl_of,
    is_reserve,
    )
from .warmap_view import (
    _ap_battlefield,
    _at_map_entity,
    )


# 0=fail 语义族的通用失败码（SetCommandNodeEquipmentResponse 实锤 0=fail）；
# transport 族响应 0=ok，禁用本码表达失败（用 7/33/109/152 等非 0 实码）
RC_FAIL = 0


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
