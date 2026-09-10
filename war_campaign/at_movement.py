# -*- coding: utf-8 -*-
"""CommandNode（AT）移动：行军分段与速度、空运、半径移动及其校验。

原 war_campaign.py 阶段 3.5 拆分（docs/warserver-refactor-plan.md §3.5）：
符号逐字搬运，行为不变。"""

import datetime
import math
import random
import mm_config
import wardb
from .common import (
    _as_resources,
    MAP_ENTITY_BATTLEFIELD,
    MAP_ENTITY_ACCESSPOINT,
    MAP_ENTITY_SUPPLYLINE,
    _ts_ms,
    find_supplyline_path,
    supplyline_adjacency,
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
    is_aircraft,
    _tpl_of,
    is_reserve,
    is_parked_planes_at,
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


def segment_seconds(from_bf, to_bf, speed, faction=0, owners=None,
                    air=False):
    """单段移动时长（秒）= 城市坐标距离 / (speed × timescale × move_scale × 地形系数)。

    官服语义：步兵 AT 数小时跨越半张图；speed 0.45-5.6（实测表值）、
    城市间典型距离 1100-6400 坐标单位，默认参数下首都对首都 ~1-4 小时。
    地形减速（#2，仅地面补给线段，air=True 空中段不适用——飞机/伞兵
    直线飞行不受海面/中立地形减速，海阈值 550 本按补给线段长标定，
    空中直线航程 700-1900 常态超阈，误用会把空运拖慢 4 倍）：
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
    if not air:
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
    type3=在途（改道走 divert_movement 不经此处；此函数仅回退用）
    → 补给线起端 0）。"""
    ent, ent_type = _at_map_entity(at, _faction_capital_bf())
    if ent_type == MAP_ENTITY_BATTLEFIELD and ent:
        return ent
    if ent_type == MAP_ENTITY_ACCESSPOINT and ent:
        bf = _ap_battlefield(ent)
        if bf:
            return bf
    return 0


async def retreat_eval(store, war_id, at, owners=None):
    """交战撤退判定（war1.md §撤退：方向随机、只能沿友方路线；
    四周路线全被敌军占据时无法撤退）。

    返回 (dest_bf, encircled)：
    - dest_bf > 0：撤退落点 = 友方邻城中随机一座（多条友方路线随机取向）
    - dest_bf = 0 且 encircled=False：无友方路线（邻城中立/敌空城）→
      调用方回退现有行为（撤回待命）
    - encircled=True：所有补给线邻城均驻有敌军 AT（敌空城/中立/己方
      不算占据——war2.md 拦路伞兵战术口径：空城可通行、有敌军才堵路）
      → 无法撤退，战败时全歼（automission 结算侧消费同判据）

    owners：{battlefield_id: faction} 城市归属（调用方可预取）。"""
    fac = int(at.get('faction') or 0)
    cur_bf = at_current_battlefield(at)
    if not cur_bf:
        return 0, False
    neighbors = sorted({e[3] for e in supplyline_adjacency().get(cur_bf, ())})
    if not neighbors:
        return 0, False
    if owners is None:
        try:
            owners = dict(await store.get_war_cities(war_id))
        except Exception:
            owners = {}
    friendly = [nb for nb in neighbors
                if int(owners.get(nb) or 0) == fac]
    if friendly:
        return random.choice(friendly), False
    # 无友方邻城 → 包围判定：邻城是否全部驻有敌军 AT
    hostile = set()
    try:
        for o in await store.list_war_command_nodes(war_id):
            if (int(o.get('state') or 0) >= 3
                    or int(o.get('faction') or 0) == fac
                    or is_reserve(o)):
                continue
            bf = at_current_battlefield(o)
            if bf in neighbors:
                hostile.add(bf)
    except Exception:
        pass
    return 0, len(hostile) >= len(neighbors)


def moving_ats_by_supplyline(ats):
    """在途 AT 按当前补给线段分组 {sl_id: [at…]}（war1.md 野外遭遇判据：
    state==1 且 map_entity 在补给线上）。"""
    out = {}
    for at in ats or []:
        if (int(at.get('state') or 0) == 1
                and int(at.get('map_entity_type') or 0)
                == MAP_ENTITY_SUPPLYLINE):
            out.setdefault(int(at.get('map_entity_id') or 0), []).append(at)
    return out


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


async def divert_movement(store, at, mv, target_bf, override_ap=0,
                          override_move=False, now=None):
    """在途 AT 改道（官服语义：走完当前段，到达下一节点后转向新目标）。

    保留已完成/进行中段的原始时间轴（started_at 与前缀段不动），从当前
    段终点城市（转向点）重新 BFS 寻路拼接新尾段；movement id 不变，
    客户端按同 id 覆盖 destination/segment 行即可更新箭头。
    新目标 = 当前段终点（含仅改出兵点）→ 剩余行程就是当前段，无新尾。

    返回 (movement, error_code)：
    - 空运/半径单位单段 air 在途不可改道 → (None, RC_ALREADY_IN_TRANSPORT)
    - 转向点到新目标不可达 → (None, RC_CANNOT_FIND_COMMANDNODE)"""
    segs = movement_segments(mv)
    if not segs or segs[0].get('air'):
        return None, RC_ALREADY_IN_TRANSPORT
    target_bf = int(target_bf or 0)
    if not target_bf:
        return None, RC_CANNOT_FIND_COMMANDNODE
    now = now or datetime.datetime.now(datetime.timezone.utc)
    idx = current_segment_index(segs, _ts_ms(now))
    raw = mv.get('path') or []
    if idx < 0 or idx >= len(raw):
        return None, RC_CANNOT_FIND_COMMANDNODE
    pivot = int(segs[idx]['to_bf'])
    head = [dict(s) for s in raw[:idx + 1]]
    tail = find_supplyline_path(pivot, target_bf)
    if tail is None:
        return None, RC_CANNOT_FIND_COMMANDNODE
    if tail:
        speed = at_speed(at)
        try:
            owners = dict(await store.get_war_cities(at['war_id']))
        except Exception:
            owners = {}
        fac = int(at.get('faction') or 0)
        for seg in tail:
            seg['seconds'] = (0 if override_move
                              else segment_seconds(seg['from_bf'],
                                                   seg['to_bf'], speed,
                                                   fac, owners))
    new_path = head + tail
    total = sum(float(s.get('seconds') or 0) for s in new_path)
    arrive = mv.get('started_at') + datetime.timedelta(seconds=total)
    mv = await store.update_movement_path(
        mv['id'], new_path, arrive_at=arrive, dest_ap=int(override_ap or 0))
    if mv is None:
        return None, RC_CANNOT_FIND_COMMANDNODE
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
      dist ≤ attackradius/2 → 任意战场（攻击/参战/空降作战）；
        **战斗机（is_aircraft 且非补给队）例外**（用户纠正 #1）：只能飞
          入**已开战**的城战（target_bf ∈ battling），未开战的非机场据点
          一律不可落——战斗机正常情况下仅能在友方机场之间移动；
        **伞兵己方机场例外**（用户纠正 #2）：拖到己方**非交战**机场
          按换基地处理（机队随队整体转场），不空降自家机场、不拆分
          返航；己方**交战中**机场仍为空降参战（防御空降，机队返航）；
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
    fac = int(at.get('faction') or 0)
    try:
        owner = int((owners or {}).get(target_bf, 0) or 0)
    except (TypeError, ValueError):
        owner = 0
    battling = set(battling or ())
    if dist * 2 <= attack:
        # 己方非交战机场（伞兵含攻击半径内）→ 换基地，不按攻击
        friendly_airfield = (is_airfield(target_bf) and owner == fac
                             and target_bf not in battling)
        if not friendly_airfield and not (
                is_aircraft(at) and not is_reserve(at)
                and target_bf not in battling):
            return True, True
        # 己方非交战机场 / 战斗机且目标未开战 → 落入 transport 分支
        # （仅己方机场换基地）
    if dist * 2 <= transport:
        ok = (is_airfield(target_bf) and owner == fac
              and (target_bf not in battling or is_reserve(at)))
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
               else segment_seconds(cur_bf, target_bf, at_speed(at),
                                    air=True))
    seg = {'from_bf': cur_bf, 'to_bf': target_bf, 'sl_id': 0,
           'from_ap': 0, 'to_ap': 0, 'seconds': seconds, 'air': True,
           'radius_attack': is_attack}
    mv = await store.create_movement(
        at['id'], at['war_id'], [seg], started_at=now,
        arrive_at=now + datetime.timedelta(seconds=seconds))
    await store.update_command_node(at['id'], state=1)
    return mv, RC_OK, is_attack


async def start_planes_rebase(store, war, at, target_bf, override_move=False,
                              now=None):
    """独立运输机队换防（is_parked_planes_at）：只能飞己方机场（交战机场
    亦可——reserve 例外），单段 air 无限半径（reserve 模板半径 -1）。
    移速 = 母伞兵 AT 模板速度（paratroopers 2.7 / pathfinders 3.0——
    运输机 cat10 的巡航速度，split_aircraft_back 返航同款口径）；
    reserve 模板自身 speed=1.0 是地面补给队口径，直接用会拖慢 ~3 倍。
    段带 paradrop_return 旗标 → 到达走运输机队落位分支
    （不汇合/不追击/不占城/不成战）并更新 airbase_bf（旧机 re-use
    合并以 airbase_bf 匹配）。

    返回 (movement, error_code)；目标非机场/非己方 → RC_CANNOT_FIND_COMMANDNODE。"""
    cur_bf = at_current_battlefield(at)
    target_bf = int(target_bf or 0)
    if (not cur_bf or not target_bf or cur_bf == target_bf
            or not is_parked_planes_at(at)):
        return None, RC_CANNOT_FIND_COMMANDNODE
    if not is_airfield(target_bf):
        return None, RC_CANNOT_FIND_COMMANDNODE
    try:
        owners = dict(await store.get_war_cities(war['id']))
    except Exception:
        owners = {}
    if int(owners.get(target_bf, 0) or 0) != int(at.get('faction') or 0):
        return None, RC_CANNOT_FIND_COMMANDNODE
    # 移速：伞兵空中原型速度（用户实报"用伞兵部队的空中速度"）——
    # 优先母 AT；母 AT 是落地变体（*_on_ground 地面速度 0.48，机队
    # 换防最常见场景：伞兵落地步行中机队调防）→ 取其 convert_to
    # 空中原型速度；母 AT 已不在（理论不达：汇合软删时机队已并入）
    # 回退自身模板速度
    speed = at_speed(at)
    try:
        parent = await store.get_command_node(
            int(at.get('parent_command_node_id') or 0))
    except Exception:
        parent = None
    if parent:
        ptpl = wardb.commandnode_templates().get(
            int(parent.get('template_id') or 0)) or {}
        if str(ptpl.get('name') or '').endswith('_on_ground'):
            ptpl = (wardb.commandnode_templates().get(
                int(ptpl.get('convert_to') or 0)) or ptpl)
        try:
            speed = float(ptpl.get('speed') or 0) or 1.0
        except (TypeError, ValueError):
            speed = 1.0
    now = now or datetime.datetime.now(datetime.timezone.utc)
    seconds = (0 if override_move
               else segment_seconds(cur_bf, target_bf, speed, air=True))
    seg = {'from_bf': cur_bf, 'to_bf': target_bf, 'sl_id': 0,
           'from_ap': 0, 'to_ap': 0, 'seconds': seconds, 'air': True,
           'paradrop_return': True}
    mv = await store.create_movement(
        at['id'], at['war_id'], [seg], started_at=now,
        arrive_at=now + datetime.timedelta(seconds=seconds))
    await store.update_command_node(at['id'], state=1)
    return mv, RC_OK
