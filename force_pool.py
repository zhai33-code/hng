# -*- coding: utf-8 -*-
"""对局兵力池模块：按模式随机抽取突击队(AT)编制 → 资源池 = Σ AT 编制原值。

数据源与机制（2026-08-26 调研定稿，方案表见 docs/force-config-plans.xlsx）：

- AT 编制表 WarDatabase/commandnodetemplatearmyresourcetemplaterelation
  （105 行 = 20 种 AT × 3 阵营同构；f1 行id f2 资源类别id f3 AT模板id
   f4 count f5 hasVehicleData f6 vehicleSpawnSlots）
- 池 = 抽中 AT 编制原值之和：乘员/飞行员与载具比例天然保持官方
  1:1~1.25:1，伞兵:运输机 = 4:1，不做任何事后推导或修正
- 镜像规则：一场只抽一份 AT 名单，双侧同池（AI 局抓包实锤：
  我方 SU 303/320/316 ↔ AI 方 GE 203/220/216 同名跨阵营翻译）
- 官方锚点（match_to_game.pcapng 2026-08-18 实测）：
  First Encounter = infantry / motorized_infantry / motorized_guard 各 ×2
  → 步兵 432 / 地形车 60 / 摩托 24

模式抽取计划（gamemode 名称来自 mm_config.GAMEMODES）：

  training(1)          固定官方锚点组合（可复现）
  capture_hold_inf(2)  步兵族×5 + 侦察族×1
  capture_hold_tank(3) 步兵族×3 + 装甲族×3（官方坦克对坦克 + AI 步兵）
  capture_hold_all(4)  步兵族×4 + 侦察族×1 + 装甲族×2 + 航空族×1
  assault_infantry(5)  步兵族×6 + 侦察族×1 + 伞兵族×1
  assault_battle(6)    步兵族×7 + 侦察族×1 + 伞兵族×1 + 装甲族×2 + 航空族×1
  war(10) / quickplay(11) / default  同上两种全兵种计划

对外接口：
  init_mission(mission_id, gamemode_id, attacker, defender,
               require_cats=())                成局建池（类别覆盖保证）
  pool(mission_id)                → {faction: {cat: count}} | None
  counts_for(mission_id, faction) → {cat: count}（无池回退锚点值）
  categories(...)                 → [(cat, spawnInAir)]（渲染类别序）
  consume(mission_id, faction, cat, n=1, ap=None)  出生扣减（ap → 同步扣分线池）
  assign_lines(mission_id, n_lines, ap_templates)   攻方 AT 分线（官方语义，幂等）
  line_counts(mission_id, line_idx) → {cat: count} | None（攻方某条激活线）
  release(mission_id)             局结束清理
  assault_team_names(mission_id)  → [AT 名] | None（f4 渲染跨阵营翻译用）
"""
import random

import mm_config
from state import log
from wardb import _parse_wardb_table, commandnode_template

# ---------------------------------------------------------------- AT 族划分
AT_FAMILIES = {
    'infantry': ('guard', 'motorized_guard', 'infantry',
                 'motorized_infantry', 'mechanized_infantry'),
    'recon': ('recon', 'motorized_recon', 'mechanized_recon'),
    # 伞兵族只抽空降变体（带运输机/侦察机成对下发，spawnInAir=1）；
    # on_ground 变体是战略层载机耗尽后的地面形态，staged 对局不用
    'para': ('paratroopers', 'pathfinders'),
    'air': ('fighter_squadron', 'heavy_fighter_squadron', 'fighter_recon'),
    'armor': ('light_armor', 'medium_armor', 'medium_tank_destroyer',
              'heavy_armor', 'heavy_tank_destroyer'),
}

# 族内约束（防极端组合）：
# - 步兵族 ≥⌈n/3⌉ 支线列 AT（保证基础池不缩水）；
#   motorized/mechanized 系限量（载具不泛滥）
# - 装甲族 ≥1 支 medium 系（避免全轻/全重极端）
# - 航空族 heavy_fighter_squadron ≤1
_LINE_ATS = ('infantry', 'motorized_infantry', 'mechanized_infantry')
_MEDIUM_ATS = ('medium_armor', 'medium_tank_destroyer')
_FAMILY_CAPS = {
    'infantry': {'motorized_infantry': 2, 'mechanized_infantry': 1,
                 'motorized_guard': 2},
    'air': {'heavy_fighter_squadron': 1},
}

# 模式 → 族抽数计划（键 = mm_config.GAMEMODES[*]['name']）
# 官方对齐（2026-09-02）：
# - 官方战争局兵力 = 将军部署的 AT 之和（WarDatabase 编制原值，不推导）；
#   staged 对局固定小池（≈官方 First Encounter 锚点量级：步兵 432 ≈ 24/槽）
# - 官方核心单位保底规则：步兵/伞兵 ≥12 出生/玩家槽、核心 ≥50% 槽位
#   （18 人局 → ≥216）——下列各池步兵均满足
# - war 为 AT 驱动大池（官方战争局典型 15~20 支 AT），staged 各模式按
#   官方 staged 语义压到锚点量级
MODE_PLANS = {
    'capture_hold_inf': {'infantry': 5, 'recon': 1},
    # ★ 官方 C&H Tanks（坦克掌控，模式 3）：8v8 坦克对坦克 + AI 步兵，
    #   无航空/侦察（1.14 更新日志）。缺此条目会回退 DEFAULT_PLAN
    #   （全兵种含 air）→ 坦克局出现飞机等资源（2026-09-13 实锤）
    'capture_hold_tank': {'infantry': 3, 'armor': 3},
    'capture_hold_all': {'infantry': 4, 'recon': 1, 'armor': 2, 'air': 1},
    'capture_hold_pilot': {'infantry': 3, 'air': 2},    # C&H Pilot：飞行员主力 + 步兵地面点
    'capture_hold_recon': {'infantry': 3, 'recon': 3},  # C&H Recon：侦察主力 + 步兵
    'assault_infantry': {'infantry': 6, 'recon': 1, 'para': 1},
    'assault_battle': {'infantry': 7, 'recon': 1, 'para': 1,
                       'armor': 2, 'air': 1},
}
MODE_PLANS['war'] = {'infantry': 9, 'recon': 2, 'para': 1,
                     'armor': 3, 'air': 2}
MODE_PLANS['quickplay'] = MODE_PLANS['capture_hold_all']
DEFAULT_PLAN = MODE_PLANS['capture_hold_all']

# 官方锚点组合（First Encounter 抓包实锤；training 模式固定使用，可复现）
ANCHOR_ATS = ('infantry', 'motorized_infantry', 'motorized_guard') * 2

# 资源类别渲染序（士兵 → 地面载具 → 飞机）与 spawnInAir 标记
# （armyresourcecategory f3 域：3=飞机 + 伞兵跳伞 + 飞行员随机出生）
_CAT_ORDER = (1, 6, 4, 5, 2,            # 士兵
              11, 15, 16, 9, 12, 7, 8, 17, 18,   # 地面载具
              10, 20, 3, 21)            # 飞机
_AIR_CATS = {2, 3, 4, 10, 20, 21}

# commandnodetemplate 模板 id → AT 名（war 会战按 AT 实例 template_id 查编制）
_TEMPLATE_NAMES = {}


def _load_at_compositions():
    """解析 AT 编制表 → {AT 名: {资源类别id: count}}（取单一阵营，编制同构）。

    ★ 同名 AT 模板每阵营一行（US 1xx / GE 2xx / SU 3xx）——编制同构，
    只取一个阵营的行；按名字跨阵营累加会 3 倍虚增（2026-09-02 实锤：
    infantry 表值 90/阵营，累加后 270 → training 锚点池 1296 ≠ 官方抓包
    432）。确定性取最小编号阵营。"""
    # commandnodetemplate id → (名字, 阵营)（f1 id f2 name f3 factionTemplateId）
    names = {}
    try:
        for r in _parse_wardb_table('commandnodetemplate'):
            nid = int(r.get(1, 0) or 0)
            nm = r.get(2, b'')
            if nid and isinstance(nm, (bytes, bytearray)) and nm:
                names[nid] = (nm.decode('utf-8', 'replace'),
                              int(r.get(3, 0) or 0))
    except Exception as e:
        log(f'[force_pool] commandnodetemplate parse failed: {e}')
        return {}
    _TEMPLATE_NAMES.update({nid: nm for nid, (nm, _fac) in names.items()})
    by_name = {}   # name -> {faction: {cat: count}}
    try:
        for r in _parse_wardb_table(
                'commandnodetemplatearmyresourcetemplaterelation'):
            at_id = int(r.get(3, 0) or 0)   # f3 AT 模板 id
            cat = int(r.get(2, 0) or 0)     # f2 资源类别 id
            cnt = int(r.get(4, 0) or 0)     # f4 count
            info = names.get(at_id)
            if not info or not cat:
                continue
            nm, fac = info
            fac_counts = by_name.setdefault(nm, {}).setdefault(fac, {})
            fac_counts[cat] = fac_counts.get(cat, 0) + cnt
    except Exception as e:
        log(f'[force_pool] AT relation parse failed: {e}')
        return {}
    return {nm: facs[min(facs)] for nm, facs in by_name.items()}


_AT_COMP = _load_at_compositions()

if not _AT_COMP:
    log('[force_pool] ERROR: no AT compositions loaded from WarDatabase; '
        'missions requiring a force pool must not be started')

_missing = [n for fam in AT_FAMILIES.values() for n in fam if n not in _AT_COMP]
if _missing:
    log(f'[force_pool] WARNING: AT missing in WarDatabase: {_missing}')

# 资源类别 → 提供族 {family: [AT 名]} 反查（require_cats 查缺补齐用）
_CAT_PROVIDERS = {}
for _fam, _ats in AT_FAMILIES.items():
    for _nm in _ats:
        if _nm not in _AT_COMP:
            continue
        for _c in _AT_COMP[_nm]:
            _CAT_PROVIDERS.setdefault(int(_c), {}).setdefault(_fam, []).append(_nm)


def template_name(template_id):
    """commandnodetemplate 模板 id → AT 兵种名（未知返回 None）。"""
    return _TEMPLATE_NAMES.get(int(template_id or 0))


def composition_for_template(template_id):
    """commandnodetemplate 模板 id → 编制 {资源类别id: count}（未知返回 None）。"""
    nm = _TEMPLATE_NAMES.get(int(template_id or 0))
    if nm is None or nm not in _AT_COMP:
        return None
    return dict(_AT_COMP[nm])


# ---------------------------------------------------------------- 抽取
def _family_ok(family, picks):
    """族内约束校验。"""
    for name, cap in _FAMILY_CAPS.get(family, {}).items():
        if picks.count(name) > cap:
            return False
    if family == 'infantry':
        need = -(-len(picks) // 3)   # ⌈n/3⌉ 线列 AT
        if sum(p in _LINE_ATS for p in picks) < need:
            return False
    if family == 'armor' and not any(p in _MEDIUM_ATS for p in picks):
        return False
    return True


def _draw_family(family, n):
    """族内等权随机抽 n 支 AT（拒绝采样满足约束，200 次后放弃约束兜底）。"""
    cands = [x for x in AT_FAMILIES[family] if x in _AT_COMP]
    if not cands or n <= 0:
        return []
    picks = [random.choice(cands) for _ in range(n)]
    for _ in range(200):
        if _family_ok(family, picks):
            return picks
        picks = [random.choice(cands) for _ in range(n)]
    return picks


def _ensure_required_cats(names, require_cats):
    """官服口径"进局必有装备载具之一"：抽取后按 require_cats 查缺补齐。

    优先同族替换（不增编制、保 _family_ok 族约束、不破坏其他必需类别
    的覆盖）；无合适替换时追加 provider AT 兜底。
    返回 (names, appended)；appended = 追加的 AT 名（替换不产生）。
    """
    require = {int(c) for c in require_cats if int(c)}
    if not require:
        return list(names), []

    def provided(ns):
        cats = set()
        for nm in ns:
            cats.update(_AT_COMP.get(nm, {}))
        return cats

    appended = []
    for cat in sorted(require):
        if cat in provided(names):
            continue
        fams = _CAT_PROVIDERS.get(cat) or {}
        # 候选族序：名单里已有该族 AT 的在前（同族替换最省编制）
        fam_order = [f for f in fams
                     if any(n in AT_FAMILIES.get(f, ()) for n in names)]
        fam_order += [f for f in fams if f not in fam_order]
        swapped = False
        for fam in fam_order:
            fam_set = set(AT_FAMILIES.get(fam, ()))
            for prov in fams[fam]:
                if prov not in _AT_COMP:
                    continue
                for i, nm in enumerate(names):
                    if nm not in fam_set:
                        continue
                    cand = names[:i] + [prov] + names[i + 1:]
                    if not _family_ok(fam, [x for x in cand if x in fam_set]):
                        continue
                    if (require - {cat}) - provided(cand):
                        continue    # 被换掉的 AT 是其他必需类别的唯一来源
                    names = cand
                    swapped = True
                    break
                if swapped:
                    break
            if swapped:
                break
        if not swapped:
            # 追加兜底：池增一支 provider AT，保证类别可部署
            prov = next((p for f in fam_order for p in fams[f]
                         if p in _AT_COMP), None)
            if prov:
                names = names + [prov]
                appended.append(prov)
    return names, appended


def draw_at_names(gamemode_id, require_cats=()):
    """按模式抽一份 AT 名单（一场一份，双侧镜像）。

    require_cats：本局必须覆盖的资源类别（成局玩家装备载具类别并集，
    官服口径"坦克兵/飞行员进局必有装备载具之一，否则无法部署"）；
    抽取后查缺补齐，见 _ensure_required_cats。
    """
    mode_name = mm_config.gamemode_cfg(gamemode_id).get('name', 'default')
    if mode_name == 'training':
        names, _appended = _ensure_required_cats(list(ANCHOR_ATS),
                                                require_cats)
        return names, mode_name
    plan = MODE_PLANS.get(mode_name, DEFAULT_PLAN)
    names = []
    for family in ('infantry', 'recon', 'para', 'armor', 'air'):
        names += _draw_family(family, plan.get(family, 0))
    names, _appended = _ensure_required_cats(names, require_cats)
    return names, mode_name


# ---------------------------------------------------------------- 每局池状态
# mission_id -> {'mode': 名, 'names': [AT 名],
#                'attacker'/'defender': 阵营 id,
#                'pool': {faction: {cat: count}}（主池=聚合，f5 简报/扣减基准）,
#                'lines': [ [AT 名], ... ] 攻方分线（官方：每线=部署在该线的 AT）,
#                'line_pool': [ {cat: count}, ... ] 各线资源（f14/f2 按线挂载）,
#                'ap_line': {ap_tmpl: 线序} 攻方激活线 AP → 线（出生上报按线扣）}
_MISSIONS = {}


def init_mission(mission_id, gamemode_id, attacker, defender,
                 require_cats=()):
    """成局建池：按模式抽 AT 名单，双侧镜像同编制。

    require_cats：本局必须覆盖的资源类别（成局玩家装备载具类别并集，
    官服口径保证坦克兵/飞行员进局必有装备载具之一，否则无法部署）。"""
    names, mode_name = draw_at_names(gamemode_id, require_cats)
    counts = {}
    for name in names:
        for cat, cnt in _AT_COMP.get(name, {}).items():
            counts[cat] = counts.get(cat, 0) + cnt
    _MISSIONS[int(mission_id)] = {
        'mode': mode_name, 'names': names,
        'attacker': int(attacker), 'defender': int(defender),
        'pool': {int(attacker): dict(counts), int(defender): dict(counts)},
        'lines': None, 'line_pool': None, 'ap_line': {},
    }
    log(f'[force_pool] mission={mission_id} mode={mode_name} '
        f'ATs[{len(names)}]={",".join(names)} pool={counts}'
        + (f' require_cats={sorted(set(int(c) for c in require_cats))}'
           if require_cats else ''))
    return True


def init_mission_war(mission_id, attacker, defender, comps_by_faction,
                     names_by_faction, ats_by_faction=None):
    """war 会战建池（M2）：兵力 = 双方实际参战 AT 编制之和（官方口径：
    战争局兵力 = 将军部署的 AT 之和，攻守各自独立、不镜像）。

    comps_by_faction: {faction: [{资源类别id: count}, ...]} 每 AT 一份编制
    names_by_faction: {faction: [AT 兵种名, ...]}（f4 渲染按阵营翻译）
    'names' 存攻方名单（assign_lines 攻方分线兼容：分线池合计=攻方主池）。
    ats_by_faction: {faction: [{id, name, owner_player_id, template_id}, ...]}
    AT 实例信息（command_nodes 行）——结算战报 f11.f3 归属用（客户端
    ownerPlayerId=0 判"自动生成 AT"，war 局是玩家部署的 AT，须发真实
    owner + 实例名，否则结算页丢失 AT 属主与加好友按钮）。
    """
    pools = {}
    for fac, comps in (comps_by_faction or {}).items():
        counts = {}
        for comp in (comps or []):
            for cat, cnt in comp.items():
                counts[int(cat)] = counts.get(int(cat), 0) + int(cnt)
        pools[int(fac)] = counts
    names_by_faction = {int(f): [str(n) for n in (v or [])]
                        for f, v in (names_by_faction or {}).items()}
    # ★per-AT 实际编制随 war_ats 携带（comp 与 ats 平行同序，2026-09-15：
    #   分线池 line_pool 改按 AT 实际存量累加，与主池/简报总量同口径，
    #   不再用模板满编 _AT_COMP——hover 单线数值 = 总面板数值的线拆分）
    war_ats = {}
    for _f, _v in (ats_by_faction or {}).items():
        _comps = (comps_by_faction or {}).get(int(_f)) or []
        war_ats[int(_f)] = [
            dict(a, comp=_comps[_i]) if _i < len(_comps) else dict(a)
            for _i, a in enumerate(_v or [])]
    # war 攻方进攻方向（激活线决策 prefer_aps，用户口径 2026-09-14：
    # war 哪条线有进攻兵力就激活哪条）。成局时定格——增援不改（激活线
    # 布局开局后不可变，改了会引发 f12/f6 布局重发噪音；增援 AT 来向
    # 未激活时 assign_lines 蛇形补位）。
    prefer_aps = sorted({
        int((a or {}).get('ap_tmpl') or 0)
        for a in (ats_by_faction or {}).get(int(attacker)) or []
        if int((a or {}).get('ap_tmpl') or 0)})
    _MISSIONS[int(mission_id)] = {
        'mode': 'war',
        'names': list(names_by_faction.get(int(attacker)) or []),
        'names_by_faction': names_by_faction,
        'war_ats': war_ats,
        'attacker': int(attacker), 'defender': int(defender),
        'pool': pools,
        'prefer_aps': prefer_aps,
        'lines': None, 'line_pool': None, 'ap_line': {},
    }
    log(f'[force_pool] mission={mission_id} mode=war (battle ATs) '
        f'atk ATs[{len(names_by_faction.get(int(attacker)) or [])}]='
        f'{",".join(names_by_faction.get(int(attacker)) or [])} '
        f'pool={pools.get(int(attacker))} | dfd ATs['
        f'{len(names_by_faction.get(int(defender)) or [])}]='
        f'{",".join(names_by_faction.get(int(defender)) or [])} '
        f'pool={pools.get(int(defender))}')
    return True


def add_war_reinforcement(mission_id, faction, comp, name, at=None):
    """war 局中途增援：AT 编制并入该阵营主池（出生消耗/结算缴获/战报
    AT 名单均以池为准，并入即生效——f2 轮询读实时池，战斗服下一轮
    即见新兵力）。

    攻方已分线（assign_lines）时重置分线缓存，下一次 f14/f2 渲染的
    assign_lines 按扩后名单重分（全线总量仍 = 攻方主池 1 份不翻倍）；
    重置与重分之间 line_counts 返回 None，调用方回退全量池挂载。"""
    rec = _MISSIONS.get(int(mission_id or 0))
    if rec is None or rec.get('mode') != 'war':
        return False
    fac = int(faction)
    counts = rec['pool'].setdefault(fac, {})
    for cat, cnt in (comp or {}).items():
        counts[int(cat)] = counts.get(int(cat), 0) + int(cnt)
    rec.setdefault('names_by_faction', {}).setdefault(fac, []).append(str(name))
    if at is not None:
        # comp 随 AT 携带（分线池按实际存量累加，同 init_mission_war）
        rec.setdefault('war_ats', {}).setdefault(fac, []).append(
            dict(at, comp=comp))
    if fac == rec.get('attacker'):
        rec.setdefault('names', []).append(str(name))
        if rec.get('lines') is not None:
            rec['lines'] = rec['line_pool'] = None
            rec['ap_line'] = {}
    log(f'[force_pool] mission={mission_id} reinforcement +{name} '
        f'faction={fac} pool={counts}')
    return True


def pool(mission_id):
    """本局资源池 {faction: {cat: count}}；无池返回 None。"""
    rec = _MISSIONS.get(int(mission_id or 0))
    return rec['pool'] if rec else None


def counts_for(mission_id, faction):
    """某阵营各类剩余量；池/阵营缺失返回空表。"""
    rec = _MISSIONS.get(int(mission_id or 0))
    if rec and int(faction) in rec['pool']:
        return rec['pool'][int(faction)]
    return {}


def categories(mission_id=None, counts_by_faction=None):
    """渲染用有序类别表 [(cat, spawnInAir)]。

    优先取池内实际出现的类别（含坦克/载具等池内新增类别）；
    无池时返回空类别表，禁止伪造一组静态兵力。
    """
    counts = None
    if counts_by_faction:
        counts = next(iter(counts_by_faction.values()), None)
    elif mission_id is not None:
        rec = _MISSIONS.get(int(mission_id or 0))
        if rec:
            counts = next(iter(rec['pool'].values()))
    if not counts:
        return []
    cats = [c for c in _CAT_ORDER if c in counts]
    cats += sorted(set(counts) - set(_CAT_ORDER))   # 未知类别兜底追加
    return [(c, c in _AIR_CATS) for c in cats]


def consume(mission_id, faction, cat, n=1, ap=None):
    """出生消耗（按兵种类别扣 n）。返回扣后余量；池/类别缺失返回 None。

    ap（ReportArmyResourceSpawnCount f1 出兵点模板）给定时，攻方出生同步
    扣对应分线池（守方/未知 AP 只扣主池）—— f14/f2 按线挂载的数值随
    出生上报递减，与引擎侧资源追踪一致。"""
    rec = _MISSIONS.get(int(mission_id or 0))
    if rec is None:
        return None
    counts = rec['pool'].get(int(faction))
    if counts is None or int(cat) not in counts:
        return None
    counts[int(cat)] = max(0, counts[int(cat)] - int(n))
    # 攻方分线池同步扣减（ap_line 由 assign_lines 按布局顺序记录）
    if ap and int(faction) == rec.get('attacker'):
        line_idx = rec.get('ap_line', {}).get(int(ap))
        if line_idx is not None and rec.get('line_pool'):
            line_counts_ = rec['line_pool'][line_idx]
            if int(cat) in line_counts_:
                line_counts_[int(cat)] = max(0, line_counts_[int(cat)] - int(n))
    return counts[int(cat)]


def assign_lines(mission_id, n_lines, ap_templates=()):
    """攻方 AT 名单分线（官方语义：每条攻击线兵力 = 部署在该线的
    AT 之和，全线总量 = 1 份池，不随激活线数翻倍；守方不分线——
    全部守 O 锚，与官方守军集中防守一致）。

    ★兵线按进攻方向（2026-09-14 用户核实官服口径：war 地图图标叠
    城，方向差异体现在详情兵线）：AT 进攻 movement 末段出兵点 →
    war accesspoint.template（= 对局图 accesspointtemplate.id，wardb
    实锤 22 取值全命中）→ 分到对应该模板的线；无方向（空降/部署/
    未知来向）或方向线未激活 → 蛇形随机补位（官方"伞兵生成路线随机
    分布在 A-D 线"）。

    幂等：首次调用按 mission_id 播种洗牌蛇形均分并缓存；后续 f14 握手 /
    f2 轮询复用同一分法（布局按 mission_id 确定性 → 同局同 n_lines）。
    ap_templates：攻方激活线 AP 模板（布局顺序）→ 记 ap→线序映射，
    ReportMissionStatus f2 出生上报按 AP 扣对应线池。
    """
    rec = _MISSIONS.get(int(mission_id))
    if rec is None:
        return
    n = max(1, int(n_lines))
    if rec.get('lines') is not None and len(rec['lines']) != n:
        # 布局理论上确定；防御性重分（保持幂等约束）
        rec['lines'] = rec['line_pool'] = None
        rec['ap_line'] = {}
    if rec.get('lines') is None:
        rng = random.Random(int(mission_id) ^ 0xA77)
        names = list(rec['names'])
        # 进攻方向分组：rec['names'][i] ↔ war_ats[attacker][i] 同序
        # （init_mission_war / add_war_reinforcement 平行追加）
        ap_line_idx = {int(ap): i for i, ap in enumerate(ap_templates)}
        wats = (rec.get('war_ats') or {}).get(
            int(rec.get('attacker') or 0)) or []

        def _comp_of(i, name):
            # per-AT 实际存量优先（war_ats 携带 comp，2026-09-15 口径统一：
            # 分线池合计 = 攻方主池，hover 单线 = 总面板的线拆分），
            # 缺省回退模板满编
            at = wats[i] if i < len(wats) else None
            c = (at or {}).get('comp')
            if isinstance(c, dict) and c:
                return c
            return _AT_COMP.get(name, {})

        lines = [[] for _ in range(n)]
        line_pool = [dict() for _ in range(n)]

        def _put(i, name, idx):
            lines[idx].append(name)
            lc = line_pool[idx]
            for cat, cnt in _comp_of(i, name).items():
                lc[int(cat)] = lc.get(int(cat), 0) + int(cnt)

        leftover = []
        for i, name in enumerate(names):
            at = wats[i] if i < len(wats) else {}
            idx = ap_line_idx.get(int((at or {}).get('ap_tmpl') or 0), -1)
            if idx >= 0:
                _put(i, name, idx)
            else:
                leftover.append((i, name))
        rng.shuffle(leftover)
        for i, name in leftover:         # 无方向 AT：蛇形填兵力最薄的线
            _put(i, name, min(range(n), key=lambda k: len(lines[k])))
        rec['lines'] = lines
        rec['line_pool'] = line_pool
    for i, ap in enumerate(ap_templates):
        if i < n:
            rec['ap_line'][int(ap)] = i


def attacker_ap_tmpls(mission_id):
    """war 局攻方进攻方向模板集合（prefer_aps，成局时定格）——激活线
    决策 assault_ap_decision(strict=True) 只开这些来向线。非 war 局 /
    无方向返回空元组（激活决策回退随机旧语义）。"""
    rec = _MISSIONS.get(int(mission_id or 0))
    if not rec or rec.get('mode') != 'war':
        return ()
    return tuple(sorted(set(int(x) for x in (rec.get('prefer_aps') or ()))))


def mission_factions(mission_id):
    """war 局 (attacker, defender) 实际阵营；非 war 返回 (0, 0)。"""
    rec = _MISSIONS.get(int(mission_id or 0))
    if not rec or rec.get('mode') != 'war':
        return (0, 0)
    return (int(rec.get('attacker') or 0), int(rec.get('defender') or 0))


def set_defender_lanes(mission_id, lanes):
    """记录 war 局守方红线模板（成局时定格，_init_war_mission_resources
    计算：目标城边线连向的相邻城市为守方占领 → 该线归属守方）。"""
    rec = _MISSIONS.get(int(mission_id or 0))
    if rec:
        rec['dfn_lanes'] = sorted({int(x) for x in (lanes or ()) if int(x)})


def defender_lanes(mission_id):
    """war 局守方红线模板（可能有 0~n 条）；非 war 局 / 未计算返回空。"""
    rec = _MISSIONS.get(int(mission_id or 0))
    if not rec:
        return ()
    return tuple(rec.get('dfn_lanes') or ())


def set_at_lanes(mission_id, lane_by_id):
    """记录 war 局每个 AT（command_node id）的激活线模板。

    多人版简报 f4 逐 AT 挂线用（war_pending 阶段简报走 war_at_rows，
    行序与名单无关，按 AT 实例 id 反查比按名单序可靠）。"""
    rec = _MISSIONS.get(int(mission_id or 0))
    if rec:
        rec['at_lanes'] = {int(k): int(v or 0)
                           for k, v in (lane_by_id or {}).items()}


def at_lane_by_id(mission_id, at_id):
    """war 局 AT 实例（command_node id）的激活线模板（0=未分线/无方向）；
    非 war 局 / 无记录返回 None。"""
    rec = _MISSIONS.get(int(mission_id or 0))
    if not rec:
        return None
    return int((rec.get('at_lanes') or {}).get(int(at_id), 0) or 0)


def lane_counts_by_ap(mission_id):
    """攻方各激活线分线池 {ap_tmpl: {cat: count}}（ap_line 映射）。

    war 局大厅简报 f5 逐线分行用（用户口径 2026-09-15：大厅详情各
    出兵点兵力按进攻方向分开，不整方挤一个点）。未分线返回 {}（调用
    方回退整方挂一个点的旧口径）。"""
    rec = _MISSIONS.get(int(mission_id or 0))
    if not rec or not rec.get('ap_line') or not rec.get('line_pool'):
        return {}
    out = {}
    for ap, idx in rec['ap_line'].items():
        if 0 <= int(idx) < len(rec['line_pool']):
            out[int(ap)] = dict(rec['line_pool'][int(idx)] or {})
    return out


def at_lane_aps(mission_id, faction):
    """本局名单每 AT 的激活线模板（按 names 顺序，未分线/无方向=0）。

    大厅简报 f4 逐 AT 挂线用（2026-09-15 反编译实锤
    BattlefieldUtilities.setupMissionAssaultTeamOnMap：图标位置 =
    f4.accessPointTemplateId → 线入口据点，全员挂同锚 = 挤一条线根因）。
    攻方 = 分线结果（ap_line 反查）；守方不分线全 0（调用方挂守方锚）。
    普通局使用镜像名单，war 局使用各阵营实际名单。"""
    rec = _MISSIONS.get(int(mission_id or 0))
    if not rec:
        return None
    names = assault_team_names(mission_id, faction) or []
    lines = rec.get('lines')
    if not lines:
        return None
    if int(faction or 0) != int(rec.get('attacker') or 0):
        # 分线只属于攻方（assign_lines 只分攻方名单）；守方/未知阵营
        # 全 0（调用方挂守方锚），避免同名 AT 误配攻方线
        return [0] * len(names)
    line_ap = {int(i): int(ap)
               for ap, i in (rec.get('ap_line') or {}).items()}
    out = []
    rest = [list(ln) for ln in lines]   # 匹配即消耗：同名 AT 各归各线
    for _n in names:
        ap = 0
        for i, ln in enumerate(rest):
            if _n in ln:
                ap = line_ap.get(i, 0)
                ln.remove(_n)
                break
        out.append(ap)
    return out


def line_counts(mission_id, line_idx):
    """攻方第 line_idx 条激活线的资源 {cat: count}（0-based，官方分线语义）。

    未分线 / 越界返回 None（调用方回退全量池挂载）。"""
    rec = _MISSIONS.get(int(mission_id or 0))
    if not rec or not rec.get('line_pool'):
        return None
    if not (0 <= int(line_idx) < len(rec['line_pool'])):
        return None
    return rec['line_pool'][int(line_idx)]


def release(mission_id):
    """局结束清理（与 mission details 同生命周期）。"""
    _MISSIONS.pop(int(mission_id or 0), None)


def assault_team_names(mission_id, faction=None):
    """本局 AT 名单（f4 渲染按阵营查 commandnodetemplate 翻译）。

    faction 给定且为 war 局（names_by_faction）→ 该阵营名单（攻守独立）；
    否则镜像名单（staged 双侧同编制）。"""
    rec = _MISSIONS.get(int(mission_id or 0))
    if rec is None:
        return None
    if faction is not None and rec.get('names_by_faction'):
        return list(rec['names_by_faction'].get(int(faction)) or [])
    return list(rec['names']) if rec.get('names') else None


def war_assault_teams(mission_id):
    """war 局参战 AT 实例名单 {faction: [{id, name, owner_player_id,
    template_id, ap_tmpl, comp}, ...]}（战斗内 f5 assaultTeams 行数据源，
    中途增援 add_war_reinforcement 并入即出现在下一轮轮询）；
    非 war 局 / 无名单 → None。"""
    rec = _MISSIONS.get(int(mission_id or 0))
    if rec is None or rec.get('mode') != 'war':
        return None
    wats = rec.get('war_ats')
    return wats or None


def staged_assault_teams(mission_id):
    """普通局系统 AT：与资源池抽取名单同源，空属主触发 High Command。

    ID 按 mission/faction/名单序号确定，同名多支仍各有 ID，轮询不变；
    攻方使用资源分线结果，守方/单线 ap=0 由调用方挂本方锚点。
    战争局与缺失的资源池不生成系统 AT。
    """
    mid = int(mission_id or 0)
    rec = _MISSIONS.get(mid)
    if not rec or rec.get('mode') == 'war':
        return None
    teams = {}
    for faction in rec['pool']:
        names = assault_team_names(mid, faction) or []
        aps = at_lane_aps(mid, faction) or []
        rows = []
        for i, name in enumerate(names):
            template_id = commandnode_template(faction, name)
            if not template_id:
                log(f'[force_pool] skip staged AT {name}: '
                    f'no template for faction {faction}')
                continue
            rows.append({
                'id': (mid << 32) | (int(faction) << 24) | (i + 1),
                'name': name, 'template_id': template_id,
                'owner_player_id': 0,
                'ap_tmpl': aps[i] if i < len(aps) else 0,
            })
        teams[int(faction)] = rows
    return teams


def faction_stats(mission_id):
    """结算战报用：每方 {names, ats, initial, remaining}（资源类别 → 数量）。

    initial 由本方 AT 名单编制重算（init 未存初值，池即剩余量）；
    ats 是 AT 实例列表 [{name, tmpl, owner_player_id}]（war 局有实例归属，
    staged 局为 None → 调用方回退兵种名+owner=0 即"自动生成 AT"）；
    无池返回 None。调用须在 release()（release_match）之前。"""
    rec = _MISSIONS.get(int(mission_id or 0))
    if rec is None:
        return None
    stats = {}
    war_ats = rec.get('war_ats') or {}
    for fac in rec['pool']:
        names = assault_team_names(mission_id, fac) or []
        initial = {}
        for name in names:
            for cat, cnt in _AT_COMP.get(name, {}).items():
                initial[int(cat)] = initial.get(int(cat), 0) + int(cnt)
        ats = None
        if war_ats.get(int(fac)):
            ats = [{'name': str(a.get('name') or ''),
                    'tmpl': int(a.get('template_id', 0) or 0),
                    'owner_player_id': int(a.get('owner_player_id', 0) or 0)}
                   for a in war_ats[int(fac)]]
        stats[int(fac)] = {
            'names': names,
            'ats': ats,
            'initial': initial,
            'remaining': {int(c): int(n) for c, n in rec['pool'][fac].items()},
        }
    return stats
