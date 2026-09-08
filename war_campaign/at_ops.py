# -*- coding: utf-8 -*-
"""CommandNode（AT）静态操作：命名规则、创建校验、模板资源/军衔换算、阵营玩家统计与 underdog 倍率、部署落位解析、单位分类（预备役/半径单位/飞机/空投）与机场表。

原 war_campaign.py 阶段 3.5 拆分（docs/warserver-refactor-plan.md §3.5）：
符号逐字搬运，行为不变。"""

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


RC_COMMANDNODE_LIMIT_REACHED = 114


RC_COMMANDNODE_ALREADY_DEPLOYED = 150


RC_NOT_ENOUGH_WARFUNDS = 153


RC_INVALID_OWNER = 189


RC_INVALID_CURRENCY = 240


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
    if len(ats) >= mm_config.WAR_AT_MAX:
        return RC_COMMANDNODE_LIMIT_REACHED, None
    return 0, tpl


def template_resources(tpl):
    """AT 模板编制资源 {cat: count}（int 键；command_nodes.resources JSON 用 str 键）。"""
    return {int(cat): int(cnt)
            for cat, cnt in (tpl.get('resources') or {}).items()}


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


def msg_air_commandnode_base_row(at, war_id, base_bf):
    """air_commandnode_base 行（proto 实锤 f1-f6）：f1 id / f2
    commandNodeWarInstanceId / f3 battlefieldid（基地机场）/ f4
    targetentityid / f5 targetentitytype（'battlefield'，String）/ f6 warid。

    行 id = AT id；客户端 AirCommandnodeBaseTable 按 f2 join →
    isAirborneUnit 判定（有行 = airborne，应用半径规则）。"""
    return (pb_int64(1, int(at['id'])) + pb_int64(2, int(at['id']))
            + pb_int64(3, int(base_bf or 0)) + pb_int64(4, int(base_bf or 0))
            + pb_string(5, 'battlefield') + pb_int64(6, int(war_id)))


# ---- 补给队（Reserve，官服 §10.2 #6）----


RESERVE_TEMPLATE_OFFSET = 4       # template_id = faction*100 + 4（104/204/304）


def is_reserve(at):
    """补给队（Reserve）判定：template_id % 100 == 4
    （客户端 locTemplateId = templateId - faction*100 → 4 = AT_TEMPLATE_RESERVE）。"""
    return int(at.get('template_id') or 0) % 100 == RESERVE_TEMPLATE_OFFSET


def reserve_template_id(faction):
    """阵营补给队模板 id（faction*100+4；wardb commandnodetemplate 104/
    204/304 'reserve'，speed=1.0）。"""
    return int(faction or 0) * 100 + RESERVE_TEMPLATE_OFFSET
