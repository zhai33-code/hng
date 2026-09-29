# -*- coding: utf-8 -*-
"""CommandNode（AT）静态操作：命名规则、创建校验、模板资源/军衔换算、阵营玩家统计与 underdog 倍率、部署落位解析、单位分类（预备役/半径单位/飞机/空投）与机场表。

原 war_campaign.py 阶段 3.5 拆分（docs/warserver-refactor-plan.md §3.5）：
符号逐字搬运，行为不变。"""

import datetime
import math
import random
from protocol import (
    pb_int64,
    pb_string,
    )
import mm_config
import wardb
from state import log
from .common import (
    supplyline_adjacency,
    _bf_pos,
    _SOLDIER_CATS,
    _as_resources,
    MAP_ENTITY_BATTLEFIELD,
    )
from .lifecycle import (
    ensure_war,
    _faction_capital_bf,
    )


# ResponseCodes（HnG_States/ResponseCodes.as，AT 生命周期相关子集）
RC_OK = 1


RC_CHARACTER_NOT_FOUND = 15


RC_NO_NAME = 53


RC_INVALID_NAME = 36


RC_WAR_NOT_FOUND = 77


RC_COMMANDNODE_TEMPLATE_NOT_FOUND = 106


RC_COMMANDNODE_WAR_INSTANCE_NOT_FOUND = 107


RC_COMMANDNODE_NOT_IN_QUEUE = 111


RC_COMMANDNODE_LAST_IN_QUEUE = 112


RC_COMMANDNODE_LIMIT_REACHED = 114


RC_COMMANDNODE_ALREADY_DEPLOYED = 150


RC_NOT_ENOUGH_WARFUNDS = 153


RC_INVALID_OWNER = 189


RC_INVALID_CURRENCY = 240

RC_PRICE_NOT_FOUND = 193
RC_STORE_DISCOUNT_NOT_FOUND = 249
RC_STORE_DISCOUNT_NOT_ACTIVE = 250
RC_STORE_DISCOUNT_MISMATCH = 251
RC_PRICING_MISMATCH = 252


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
    # 持有上限只统计真实 AT：补给列（spawn_supply_column）与空降运输机队
    # （split_aircraft_back）同为玩家名下 reserve 模板行，计入会把活跃
    # 玩家顶到 MM_WAR_AT_MAX 导致购买全回 commandnode_limit_reached(114)
    ats = [a for a in ats if not is_reserve(a)]
    if len(ats) >= mm_config.WAR_AT_MAX:
        return RC_COMMANDNODE_LIMIT_REACHED, None
    return 0, tpl


def template_resources(tpl):
    """AT 模板编制资源 {cat: count}（int 键；command_nodes.resources JSON 用 str 键）。"""
    return {int(cat): int(cnt)
            for cat, cnt in (tpl.get('resources') or {}).items()}


async def split_aircraft_back(store, at, home_bf=None, drop_bf=None,
                              now=None):
    """空降落地拆分（官服 Spaatz 更新实锤："Dropping paratroopers/
    pathfinders, returns the transport planes to the origin of the
    transportation"）：按空中原型 vehicle_cats 把 AT 持有的运输机
    剥离，创建**玩家名下独立运输机队**——reserve 模板 + 只带载具资源
    + parent 指向伞兵 AT（客户端战地图按 reserve 资源类别显示**运输机
    剪影图标**、列表/tooltip 挂父 AT 名，官服老视频同款），并从空投点
    **飞回**原机场（单段 air movement，非瞬移；到达后停机场待命，
    落地伞兵回机场补给时免费 re-use）。

    返回 (剥离后 resources, 运输机队, 返航 movement)；无可剥离 / 无原
    机场可回 / 建队失败返回 (None, None, None)（调用方不动 resources，
    落地变体按已有资源继续，旧机重装由机场补给合并路径兜底）。"""
    tpl = _tpl_of(at)
    pv = {int(c) for c in (tpl.get('vehicle_cats') or set())}
    if not pv:
        return None, None, None
    if home_bf is None:
        home_bf = at.get('airbase_bf')
    home_bf = int(home_bf or 0)
    if not home_bf:
        return None, None, None     # 无出发机场（异常路径）：不拆
    drop_bf = int(drop_bf or 0)
    res = _as_resources(at.get('resources'))
    back = {}
    for cat, _full in template_resources(tpl).items():
        cat = int(cat)
        if cat not in pv:
            continue
        have = int(res.get(str(cat), res.get(cat, 0)) or 0)
        if have > 0:
            back[cat] = have
    if not back:
        return None, None, None
    fac = int(at.get('faction') or 0)
    # 官服 reserve 命名：name=父 AT 名（客户端按 parent 替换显示），
    # abbreviation 带后缀 "(R)"（客户端 updateAssaultTeamNameAndAbbreviation
    # 同款约定，去 (R) 后参与排序）
    name = str(at.get('name') or 'transport')[:AT_NAME_MAX_LENGTH]
    abbr = (str(at.get('abbreviation') or '') + '(R)')[:AT_ABBR_MAX_LENGTH]
    try:
        planes = await store.create_command_node(
            int(at.get('war_id') or 0), reserve_template_id(fac),
            int(at.get('owner_player_id') or 0), fac,
            name, {str(c): int(n) for c, n in back.items()},
            abbreviation=abbr, parent_command_node_id=int(at['id']))
        await store.update_command_node(
            planes['id'], is_deployed=True,
            map_entity_id=drop_bf or home_bf,
            map_entity_type=MAP_ENTITY_BATTLEFIELD, airbase_bf=home_bf)
    except Exception as e:
        log(f'[at_ops] 运输机拆分失败 node={at.get("id")}: {e}')
        return None, None, None
    for cat in back:
        res.pop(str(cat), None)
        res.pop(cat, None)
    # ---- 返航：空投点 → 原机场（单段 air；空投点=机场或坐标缺失则
    #      直接待命无航程）。段带 paradrop_return 旗标，advance_movements
    #      到达分支据此落位（不走补给队汇合/追击）。
    mv = None
    if drop_bf and drop_bf != home_bf:
        from .at_movement import at_speed, segment_seconds
        now = now or datetime.datetime.now(datetime.timezone.utc)
        seconds = max(
            1, int(segment_seconds(drop_bf, home_bf, at_speed(at),
                                   air=True)))
        seg = {'from_bf': drop_bf, 'to_bf': home_bf, 'sl_id': 0,
               'from_ap': 0, 'to_ap': 0, 'seconds': seconds, 'air': True,
               'paradrop_return': True}
        try:
            mv = await store.create_movement(
                planes['id'], int(at.get('war_id') or 0), [seg],
                started_at=now,
                arrive_at=now + datetime.timedelta(seconds=seconds))
            await store.update_command_node(planes['id'], state=1)
        except Exception as e:
            log(f'[at_ops] 运输机返航创建失败 node={planes["id"]}: {e}')
            mv = None
    planes = await store.get_command_node(planes['id'])
    return {str(k): int(v) for k, v in res.items()}, planes, mv


async def land_paradrop_split(store, war_id, at, drop_bf=None, now=None):
    """伞兵落地（空降到达 / 战败落地共用）：模板转 convert_to 落地变体
    + 运输机拆成玩家名下独立运输机队从空投点飞回原机场
    （split_aircraft_back）+ 行推送（含 air_transport 在途动画行）。
    返回 (落地后 AT, 推送事件)。"""
    from .warmap_view import (
        msg_commandnode_definition_row,
        msg_commandnode_instance_row,
        msg_commandnode_morale_row,
        msg_armyresource_rows,
        )
    from .battle_msgs import msg_air_transport_row
    from .at_movement import movement_segments
    caps = _faction_capital_bf()
    tpl = _tpl_of(at)
    cvt = int(tpl.get('convert_to') or 0)
    home = int(at.get('airbase_bf') or 0)
    if not drop_bf:
        drop_bf = (int(at.get('map_entity_id') or 0)
                   if int(at.get('map_entity_type') or 0)
                   == MAP_ENTITY_BATTLEFIELD else 0)
    ground_res, planes, mv = await split_aircraft_back(
        store, at, home, drop_bf=drop_bf, now=now)
    upd = {'airbase_bf': 0}
    if cvt and cvt in wardb.commandnode_templates():
        upd['template_id'] = cvt
    if ground_res is not None:
        upd['resources'] = ground_res
    await store.update_command_node(at['id'], **upd)
    at = await store.get_command_node(at['id'])
    events = []
    if upd.get('template_id'):
        events.append(('set', b'CommandNodeDefinition',
                       msg_commandnode_definition_row(at)))
        if ground_res is not None:
            # 被剥离载具类的 armyresource 行收缩（落地变体编制不含）
            for cat in tpl.get('vehicle_cats') or set():
                events.append(('del', b'armyresource',
                               int(at['id']) * 1000 + int(cat)))
    events.append(('set', b'CommandNodeWarInstance',
                   msg_commandnode_instance_row(at, war_id, caps)))
    for p in msg_armyresource_rows(at, war_id):
        events.append(('set', b'armyresource', p))
    if planes is not None:
        events.append(('set', b'CommandNodeDefinition',
                       msg_commandnode_definition_row(planes)))
        events.append(('set', b'CommandNodeWarInstance',
                       msg_commandnode_instance_row(planes, war_id, caps)))
        events.append(('set', b'CommandNodeMorale',
                       msg_commandnode_morale_row(planes, war_id)))
        for p in msg_armyresource_rows(planes, war_id):
            events.append(('set', b'armyresource', p))
        if mv is not None:
            # 在途飞行动画（客户端 AirTransportTable → 运输机图标从
            # 空投点滑向机场；到达 del 行回静止位）
            segs = movement_segments(mv)
            if segs:
                events.append(('set', b'air_transport',
                               msg_air_transport_row(planes, war_id, mv,
                                                     segs[0])))
        log(f'[war] paradrop split: node={at["id"]} on_ground + planes '
            f'AT {planes["id"]} {planes.get("name")!r} '
            f'{drop_bf} -> airfield {home}'
            + (' (flying back)' if mv is not None else ''))
    events.append(('del', b'air_commandnode_base', at['id']))
    return at, events


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
    """升级链校验（对齐客户端 AssaultTeamUpgradeOptions.getUpgradeOption
    AS3 L33-49 实锤）：同阵营 + 目标模板 parent_template_id == 当前模板 id
    （升级选项 = 挂我为 parent 的下一级；**不是 convertTo**——f11 在官服
    数据里是伞兵空降⇄落地变体互转字段，108⇄114/111⇄112，绝非升级路径，
    2026-09-08 修正：原 convert_to 分支曾放行伞兵升级成 *_on_ground 步行
    变体）+ 客户端显式排除落地变体（L37 locTemplateId ==
    AT_TEMPLATE_PARATROOPERS_ON_GROUND）+ motorized_guard 特例。

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
    # 客户端排除落地变体（paratroopers_on_ground 114/214/314：
    # name 后缀判定，与 AT_TEMPLATE_PARATROOPERS_ON_GROUND 同口径）
    if str(target_tpl.get('name') or '').endswith('_on_ground'):
        return False
    if int(target_tpl.get('parent_template_id') or 0) == cur_id:
        return True
    # 官服 f11 互转语义（落地变体 → 空中原型）：伞兵空降到达后服务端
    # 转成 *_on_ground（编制只含伞兵不含运输机 cat10 → resupply 恒
    # "已满编"），玩家唯一官方恢复通道是 f11 反向链转回空中原型
    # （111⇄112/311⇄312 双向 convert_to），重新装备运输机后再空降。
    # 反方向（空中原型 → 落地变体）仍被上面的落地变体目标拒绝挡住
    # （落地转换只由服务端空降到达/战败逻辑驱动，防升级窗误操作步行化）。
    if (str(cur_tpl.get('name') or '').endswith('_on_ground')
            and int(cur_tpl.get('convert_to') or 0) == tgt):
        return True
    return (str(cur_tpl.get('name') or '') == 'motorized_guard'
            and str(target_tpl.get('name') or '') == 'motorized_infantry')


RC_DEPLOY_LOCATION_NOT_FOUND = 221    # 官服：主城交战/无可用落位城


async def owned_capital_bfs(store, war_id, faction, owners=None):
    """己方当前占领的主城 battlefield id 集合（**动态归属**）。

    客户端实锤（AssaultTeamReinforcementWindowMediator.requestDeployment
    AS3 L470-480）：部署窗口列出 CapitalTable.allEntries 中
    battlefieldStatus.factionid == 己方阵营的全部主城 → 占领敌方主城后
    该城同样进入部署选项（旧实现按静态 default_faction 判定 → 新占领的
    敌方主城 f4 被无视、AT 落回出厂主城 = 玩家报"新占领的城市没办法
    部署"）。owners 缺省自查 store；存储不可用回退静态 default_faction
    （与旧口径一致，宁部署不错杀）。"""
    if owners is None:
        try:
            owners = dict(await store.get_war_cities(war_id))
        except Exception:
            owners = None
    out = set()
    for c in wardb.capitals():
        bf = c.get('battlefield_id')
        if not bf:
            continue
        if owners is not None:
            own = int(owners.get(int(bf), 0) or 0)
        else:
            own = int(c.get('default_faction') or 0)
        if own == int(faction or 0):
            out.add(int(bf))
    return out


async def _battling_battlefields(store, war_id):
    """交战城市集合 {battlefield_id}（活跃城战所在城市）。
    野外会战（battle_type != ASSAULT）挂在补给线上，battlefield_id 与
    城市 id 不同空间，不进本集合（撞号会误锁无关城市部署）。
    存储无该接口/异常时回退空集（视为全域非交战，维持可部署）。"""
    from .common import BATTLE_TYPE_ASSAULT
    try:
        rows = await store.list_active_war_battles(war_id)
    except Exception:
        return set()
    out = set()
    for b in rows or ():
        try:
            if (int(b.get('battle_type') or BATTLE_TYPE_ASSAULT)
                    != BATTLE_TYPE_ASSAULT):
                continue
            bid = int(b.get('battlefield_id') or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if bid:
            out.add(bid)
    return out


async def resolve_deploy_battlefield(store, war, at, near_capital_bf=0):
    """官服部署落位（§10.2 #4/#5）：锚点主城非交战 → random.choice(
    锚点 ∪ 锚点周边非交战己方城)（war1.md「就近原则」= 出生点分布池，
    非部署门槛——官服开局仅首都 owned，周边全中立必须可部署，
    2026-09-08 修正：周边无己方城时 AT 落主城本身）。

    锚点 = near_capital_bf（请求 f4 玩家所选城）在**己方当前占领的主城
    集合**内时生效（2026-09-08 修正：客户端部署窗口按动态归属列出全部
    己方主城，占领的敌方主城可选——旧静态 default_faction 判定会把部署
    落回出厂主城）；否则回退集合内任一（排序取首，确定性）；集合空 =
    丢光全部主城无处部署。返回 (battlefield, 0)=成功；失败
    (0, 221)=锚点交战、(0, 7)=无己方主城。"""
    fac = int(at.get('faction') or 0)
    try:
        owners = dict(await store.get_war_cities(war['id']))
    except Exception:
        owners = None
    owned_caps = await owned_capital_bfs(store, war['id'], fac, owners)
    capital = int(near_capital_bf or 0)
    if capital not in owned_caps:
        capital = sorted(owned_caps)[0] if owned_caps else 0
    if not capital:
        return 0, RC_CANNOT_FIND_COMMANDNODE
    battling = await _battling_battlefields(store, war['id'])
    if capital in battling:
        return 0, RC_DEPLOY_LOCATION_NOT_FOUND
    cands = [capital]
    for edge in supplyline_adjacency().get(capital, ()):
        nb = int(edge[3] or 0)
        if nb in battling or nb == capital:
            continue
        try:
            if int((owners or {}).get(nb, 0) or 0) != fac:
                continue
        except (TypeError, ValueError):
            continue
        cands.append(nb)
    if is_radius_unit(at):
        # §10.2 #7：飞机/伞兵（airborne）只出生在机场——候选池 ∩ 机场；
        # 空池回退距主城最近的**己方占领**机场（2026-09-08 修复：机场须先
        # 占领才能部署，与半径移动侧"敌方/中立机场拒单"同口径——旧实现
        # 不查归属，开局只有主城 owned 时飞机直接落中立机场 Biggin Hill/
        # Kastrup/Érd；fac1 首都邻接无机场 → 开局需先占二跳机场）。
        # 己方无任何机场 → 拒单 221
        af_cands = [c for c in cands if is_airfield(c)]
        if not af_cands:
            near = nearest_owned_airfield(capital, owners, fac)
            if not near:
                return 0, RC_DEPLOY_LOCATION_NOT_FOUND
            af_cands = [near]
        return random.choice(af_cands), 0
    return random.choice(cands), 0


# ---- AT 移动（Phase 6，docs/hng-rts-protocol.md §5.3）----


RC_CANNOT_FIND_COMMANDNODE = 7


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


def is_infantry_at(at):
    """纯步兵族 AT（模板编制类别全部属士兵族；war1.md 野外遭遇两子类
    判据：双方纯步兵=encounter / 任一方含载具（侦/装）=skirmish）。"""
    cats = {int(c) for c in (_tpl_of(at).get('resources') or {})}
    return bool(cats) and cats <= _SOLDIER_CATS


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


def is_air_family_at(at):
    """空军系 AT（飞行中队 + 伞兵族含落地变体）：补给的飞行员/飞机/
    伞兵/运输机出生在主城附近机场**原地等待**本体来合并（官服 war2.md
    「补给只发生在主城附近的机场……飞机与驾驶员分开补给，伞兵补给也
    可用同样方法」+ 用户实报「等本体飞过来合并就完成补给」）。"""
    if is_aircraft(at) or is_paradrop_unit(at):
        return True
    tpl = _tpl_of(at)
    if str(tpl.get('name') or '').endswith('_on_ground'):
        proto = wardb.commandnode_templates().get(
            int(tpl.get('convert_to') or 0)) or {}
        try:
            return int(proto.get('actionid') or 0) in PARADROP_ACTIONIDS
        except (TypeError, ValueError):
            return False
    return False


def bf_distance(from_bf, to_bf):
    """城市坐标直线距离；任一端无坐标返回 None（回退全允许）。"""
    p = _bf_pos()
    a, b = p.get(int(from_bf or 0)), p.get(int(to_bf or 0))
    if not a or not b:
        return None
    return math.hypot(a[0] - b[0], a[1] - b[1])


def nearest_airfield(bf_id):
    """距指定城市最近的机场（不查归属；部署回退请用
    nearest_owned_airfield，本函数仅供存储异常回退/勘查口径）。无坐标
    返回 0。"""
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


def nearest_owned_airfield(bf_id, owners, faction):
    """距指定城市最近的**己方占领**机场（部署回退口径：机场须先占领）。

    owners：{battlefield_id: faction}（war_cities 归属）；None = 存储
    不可用，回退 nearest_airfield（宁部署不错杀，与 owned_capital_bfs
    静态回退同哲学）。无己方机场/无坐标返回 0。"""
    if owners is None:
        return nearest_airfield(bf_id)
    src = _bf_pos().get(int(bf_id or 0))
    if not src:
        return 0
    fac = int(faction or 0)
    best, best_d = 0, None
    for af in airfield_bfs():
        try:
            if int(owners.get(af, 0) or 0) != fac:
                continue
        except (TypeError, ValueError):
            continue
        q = _bf_pos().get(af)
        if not q:
            continue
        d = (src[0] - q[0]) ** 2 + (src[1] - q[1]) ** 2
        if best_d is None or d < best_d:
            best, best_d = af, d
    return best


def msg_air_commandnode_base_row(at, war_id, base_bf, target_bf=None):
    """air_commandnode_base 行（proto 实锤 f1-f6）：f1 id / f2
    commandNodeWarInstanceId / f3 battlefieldid（基地机场）/ f4
    targetentityid / f5 targetentitytype（'battlefield'，String）/ f6 warid。

    行 id = AT id；客户端 AirCommandnodeBaseTable 按 f2 join →
    isAirborneUnit 判定（有行 = airborne，应用半径规则）。
    target_bf：目的地实体（CommandnodeArrow 文字/箭头 ← f4）；换基地
    = 基地机场（缺省同 base_bf），攻击飞行 = 目标城（f3 保持出发机场）。"""
    tgt = int(target_bf if target_bf is not None else base_bf or 0)
    return (pb_int64(1, int(at['id'])) + pb_int64(2, int(at['id']))
            + pb_int64(3, int(base_bf or 0)) + pb_int64(4, tgt)
            + pb_string(5, 'battlefield') + pb_int64(6, int(war_id)))


# ---- 补给队（Reserve，官服 §10.2 #6）----


RESERVE_TEMPLATE_OFFSET = 4       # template_id = faction*100 + 4（104/204/304）


def is_reserve(at):
    """补给队（Reserve）判定：template_id % 100 == 4
    （客户端 locTemplateId = templateId - faction*100 → 4 = AT_TEMPLATE_RESERVE）。"""
    return int(at.get('template_id') or 0) % 100 == RESERVE_TEMPLATE_OFFSET


def reserve_template_id(faction):
    """阵营补给队模板 id（faction*100+4；wardb commandnodetemplate 104/
    204/304 'reserve'，speed=1.0）。合成 wardb（测试 501-509 族）无
    104 时回退同阵营 %100==4 模板（504）。"""
    faction = int(faction or 0)
    tid = faction * 100 + RESERVE_TEMPLATE_OFFSET
    tpls = wardb.commandnode_templates()
    if tid in tpls:
        return tid
    for k, v in sorted(tpls.items()):
        if (k % 100 == RESERVE_TEMPLATE_OFFSET
                and int(v.get('faction') or 0) == faction):
            return k
    return tid


def is_parked_planes_at(at):
    """独立运输机队（空降拆分产物，split_aircraft_back）：reserve 模板
    且停驻机场（airbase_bf>0）。区别于补给队（同 reserve 模板但
    airbase_bf 恒 0，走补给线）。官服口径：独立运输机只能飞己方机场
    （客户端 CommandnodeArrow L504 `isAirfield && (!underAttack ||
    isReserve)`——reserve 例外允许交战机场）。"""
    return (is_reserve(at)
            and int(at.get('airbase_bf') or 0) > 0)


async def merge_parked_planes(store, at, proto, air_bf, war_id):
    """停驻同机场的同主运输机队**免费**并入 AT 直到补满 proto 载具编制
    （官服 Spaatz "re-use old transport planes" 核心实现；f11 补给入口
    与落地伞兵走回机场自动重整共用）。遍历玩家名下停驻该机场的
    reserve 机队（state<3、is_deployed、airbase_bf=机场），按
    proto.vehicle_cats 满编口径取机：取尽软删 + del 行，部分取回写 +
    set armyresource 行。返回 (take {cat: n}, events)——空 take = 无可
    合并/不适用；events 由调用方决定广播（handler）或随 tick 汇总
    （tick 路径，advance_movements 返回后统一广播）。"""
    pv = {int(c) for c in (proto.get('vehicle_cats') or set())}
    if not pv:
        return {}, []
    try:
        parked = await store.list_player_command_nodes(
            int(at.get('owner_player_id') or 0), int(war_id))
    except Exception as e:
        log(f'      [war_at] 停驻机队查询失败: {e}')
        return {}, []
    parked = [p for p in parked
              if int(p.get('id') or 0) != int(at.get('id') or 0)
              and is_reserve(p)
              and int(p.get('airbase_bf') or 0) == int(air_bf)
              and p.get('is_deployed')
              and int(p.get('state') or 0) < 3]
    if not parked:
        return {}, []
    res = _as_resources(at.get('resources'))
    full = {int(c): int(n) for c, n in template_resources(proto).items()
            if int(c) in pv}
    take = {}
    events = []
    for p in sorted(parked, key=lambda x: int(x.get('id') or 0)):
        pres = _as_resources(p.get('resources'))
        left = {}
        for cat in sorted(pv):
            have = int(pres.get(str(cat), pres.get(cat, 0)) or 0)
            if have <= 0:
                continue
            cur = (int(res.get(str(cat), res.get(cat, 0)) or 0)
                   + take.get(cat, 0))
            g = min(full.get(cat, 0) - cur, have)
            if g > 0:
                take[cat] = take.get(cat, 0) + g
            left[cat] = have - max(g, 0)
        if not any(v > 0 for v in take.values()):
            break                       # 已取满：剩余机队保留待下次
        if not any(v > 0 for v in left.values()):
            # 取尽：软删 planes-AT + del 行（与 _push_at_delete 同表集）
            await store.delete_command_node(p['id'])
            events.append(('del', b'CommandNodeWarInstance', p['id']))
            events.append(('del', b'CommandNodeDefinition', p['id']))
            events.append(('del', b'CommandNodeMorale', p['id']))
            for cat in pv:
                events.append(('del', b'armyresource',
                               int(p['id']) * 1000 + int(cat)))
            events.append(('del', b'air_commandnode_base', p['id']))
        else:
            from .warmap_view import msg_armyresource_rows
            left_res = {str(c): int(n) for c, n in left.items() if n > 0}
            await store.update_command_node(p['id'], resources=left_res)
            p2 = await store.get_command_node(p['id'])
            if p2:
                for row in msg_armyresource_rows(p2, war_id):
                    events.append(('set', b'armyresource', row))
    if not take:
        return {}, []
    for cat, n in take.items():
        res[str(cat)] = int(res.get(str(cat), res.get(cat, 0)) or 0) + n
    await store.update_command_node(
        at['id'], resources={str(k): int(v) for k, v in res.items()})
    log(f'      [war] 旧机队合并(免费) node={at["id"]} take={take} '
        f'parked={[p["id"] for p in parked]}')
    return take, events


async def remount_on_ground_at_airfield(store, war_id, at):
    """落地伞兵走回己方机场自动重整（f11 补给 / 撤退重整之外的第三
    入口）：地面行军**到达**己方机场，且玩家名下停驻同机场的运输机队
    有机可并时——免费并入（merge_parked_planes）+ 转回空中原型（恢复
    cat10/半径）+ airbase_bf 指向机场，可再次空降。

    无机可并时若 AT 已持满空中原型载具（计价补给队合并新买的运输机
    后）同样转回原型；否则非机场 / 非己方城 / 非落地变体返回 (at, [])
    原样不动（玩家仍可到站后手动补给买机，本函数不替代计价补给）。"""
    if is_reserve(at):
        return at, []
    tpl = _tpl_of(at)
    if not str(tpl.get('name') or '').endswith('_on_ground'):
        return at, []
    proto = wardb.commandnode_templates().get(
        int(tpl.get('convert_to') or 0)) or None
    if (proto is None
            or int(proto.get('faction') or 0) != int(tpl.get('faction') or 0)
            or 10 not in {int(c) for c in
                          (proto.get('vehicle_cats') or set())}):
        return at, []
    from .at_movement import at_current_battlefield
    bf = at_current_battlefield(at)
    if not bf or not is_airfield(bf):
        return at, []
    try:
        owners = dict(await store.get_war_cities(war_id))
    except Exception as e:
        log(f'[at_ops] 走回机场重整归属判定失败: {e}')
        return at, []
    if int(owners.get(bf, 0) or 0) != int(at.get('faction') or 0):
        return at, []
    take, events = await merge_parked_planes(store, at, proto, bf, war_id)
    if take:
        at = await store.get_command_node(at['id'])
    # 登机门槛：并入旧机 >0（旧口径）或 AT 已持满原型载具（计价补给
    # 合并新买的运输机后重新登机——merge_waiting_columns 补给汇合路径）
    res_now = _as_resources(at.get('resources'))
    pv = {int(c) for c in (proto.get('vehicle_cats') or set())}
    holds_full = all(
        int(res_now.get(str(c), res_now.get(c, 0)) or 0) >= int(n)
        for c, n in template_resources(proto).items() if int(c) in pv)
    if not take and not holds_full:
        return at, []
    await store.update_command_node(at['id'], template_id=int(proto['id']),
                                    airbase_bf=int(bf))
    at = await store.get_command_node(at['id'])
    from .warmap_view import (
        msg_commandnode_definition_row,
        msg_commandnode_instance_row,
        msg_armyresource_rows,
        )
    caps = _faction_capital_bf()
    events.append(('set', b'CommandNodeDefinition',
                   msg_commandnode_definition_row(at)))
    events.append(('set', b'CommandNodeWarInstance',
                   msg_commandnode_instance_row(at, war_id, caps)))
    for p in msg_armyresource_rows(at, war_id):
        events.append(('set', b'armyresource', p))
    events.append(('set', b'air_commandnode_base',
                   msg_air_commandnode_base_row(at, war_id, bf)))
    log(f'[war] 落地伞兵走回机场重整 node={at["id"]} '
        f'tpl={tpl.get("id")} -> {proto["id"]} take={take} airfield={bf}')
    return at, events


async def remount_parked_planes_on_arrival(store, war_id, fleet):
    """运输机队飞抵机场自动重整（"运输机找伞兵"反向入口，与
    remount_on_ground_at_airfield 伞兵找运输机对称）：返航/换防到达
    落位后，同机场停有同 owner 落地伞兵变体（*_on_ground，parent AT
    优先）→ 机免费并入它 + 伞兵转回空中原型（可再次空降）+ 机队取尽
    软删 / 部分取回保留余机。无伞兵 / 满员返回 (fleet, []) 原样停机
    （玩家仍可 f11 手动补给/合并）。"""
    if not is_parked_planes_at(fleet):
        return fleet, []
    dest_bf = int(fleet.get('airbase_bf') or 0)
    if not dest_bf:
        return fleet, []
    try:
        nodes = await store.list_player_command_nodes(
            int(fleet.get('owner_player_id') or 0), int(war_id))
    except Exception as e:
        log(f'[at_ops] 机队到达重整候选查询失败: {e}')
        return fleet, []
    from .at_movement import at_current_battlefield
    parent_id = int(fleet.get('parent_command_node_id') or 0)
    cands = []
    for n in nodes:
        if (is_reserve(n)
                or int(n.get('state') or 0) != 0
                or not n.get('is_deployed')
                or at_current_battlefield(n) != dest_bf):
            continue
        ntpl = _tpl_of(n)
        if not str(ntpl.get('name') or '').endswith('_on_ground'):
            continue
        proto = wardb.commandnode_templates().get(
            int(ntpl.get('convert_to') or 0)) or None
        if (proto is None
                or int(proto.get('faction') or 0)
                != int(ntpl.get('faction') or 0)
                or 10 not in {int(c) for c in
                              (proto.get('vehicle_cats') or set())}):
            continue
        cands.append((0 if int(n['id']) == parent_id else 1,
                      int(n['id']), n, proto))
    if not cands:
        return fleet, []
    cands.sort(key=lambda x: (x[0], x[1]))
    _, _, at, proto = cands[0]
    # 取机：按原型满编口径从本机队取（同 merge_parked_planes 口径）
    pv = {int(c) for c in (proto.get('vehicle_cats') or set())}
    full = {int(c): int(n) for c, n in template_resources(proto).items()
            if int(c) in pv}
    pres = _as_resources(fleet.get('resources'))
    res = _as_resources(at.get('resources'))
    take = {}
    left = {}
    for cat in sorted(pv):
        have = int(pres.get(str(cat), pres.get(cat, 0)) or 0)
        cur = int(res.get(str(cat), res.get(cat, 0)) or 0)
        g = min(full.get(cat, 0) - cur, have)
        if g > 0:
            take[cat] = g
        left[cat] = have - max(g, 0)
    if not take:
        return fleet, []                # 伞兵满员：机队保留停驻
    for cat, n in take.items():
        res[str(cat)] = int(res.get(str(cat), res.get(cat, 0)) or 0) + n
    await store.update_command_node(
        at['id'], template_id=int(proto['id']), airbase_bf=dest_bf,
        resources={str(k): int(v) for k, v in res.items()})
    at = await store.get_command_node(at['id'])
    events = []
    if any(v > 0 for v in left.values()):
        # 部分取回：机队保留余机（armyresource 行刷新）
        await store.update_command_node(
            fleet['id'],
            resources={str(c): int(n) for c, n in left.items() if n > 0})
        from .warmap_view import msg_armyresource_rows
        fleet2 = await store.get_command_node(fleet['id'])
        if fleet2:
            for row in msg_armyresource_rows(fleet2, war_id):
                events.append(('set', b'armyresource', row))
    else:
        # 取尽：软删机队 + del 行（与 merge_parked_planes 同表集）
        await store.delete_command_node(fleet['id'])
        events.append(('del', b'CommandNodeWarInstance', fleet['id']))
        events.append(('del', b'CommandNodeDefinition', fleet['id']))
        events.append(('del', b'CommandNodeMorale', fleet['id']))
        for cat in pv:
            events.append(('del', b'armyresource',
                           int(fleet['id']) * 1000 + int(cat)))
        events.append(('del', b'air_commandnode_base', fleet['id']))
    from .warmap_view import (
        msg_commandnode_definition_row,
        msg_commandnode_instance_row,
        msg_armyresource_rows,
        )
    caps = _faction_capital_bf()
    events.append(('set', b'CommandNodeDefinition',
                   msg_commandnode_definition_row(at)))
    events.append(('set', b'CommandNodeWarInstance',
                   msg_commandnode_instance_row(at, war_id, caps)))
    for p in msg_armyresource_rows(at, war_id):
        events.append(('set', b'armyresource', p))
    events.append(('set', b'air_commandnode_base',
                   msg_air_commandnode_base_row(at, war_id, dest_bf)))
    log(f'[war] 运输机队到达并入落地伞兵 node={at["id"]} '
        f'tpl=-> {proto["id"]} take={take} fleet={fleet["id"]} '
        f'airfield={dest_bf}')
    return at, events
