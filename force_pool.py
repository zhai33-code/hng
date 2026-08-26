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
  capture_hold_inf(3)  步兵族×6 + 侦察族×2
  capture_hold_all(4)  步兵族×6 + 侦察族×2 + 装甲族×2 + 航空族×2
  assault_infantry(5)  步兵族×8 + 侦察族×2 + 伞兵族×1
  assault_battle(6)    步兵族×8 + 侦察族×2 + 伞兵族×1 + 装甲族×3 + 航空族×2
  war(10) / quickplay(11) / default  同上两种全兵种计划

对外接口：
  init_mission(mission_id, gamemode_id, attacker, defender)  成局建池
  pool(mission_id)                → {faction: {cat: count}} | None
  counts_for(mission_id, faction) → {cat: count}（无池回退锚点值）
  categories(...)                 → [(cat, spawnInAir)]（渲染类别序）
  consume(mission_id, faction, cat, n=1)  出生扣减
  release(mission_id)             局结束清理
  assault_team_names(mission_id)  → [AT 名] | None（f4 渲染跨阵营翻译用）
"""
import random

import mm_config
from state import log
from wardb import _parse_wardb_table

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
MODE_PLANS = {
    'capture_hold_inf': {'infantry': 6, 'recon': 2},
    'capture_hold_all': {'infantry': 6, 'recon': 2, 'armor': 2, 'air': 2},
    'assault_infantry': {'infantry': 8, 'recon': 2, 'para': 1},
    'assault_battle': {'infantry': 8, 'recon': 2, 'para': 1,
                       'armor': 3, 'air': 2},
}
MODE_PLANS['war'] = MODE_PLANS['assault_battle']
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

# 无池回退值（官方锚点池：步兵432/地形车60/摩托24）
FALLBACK_COUNTS = {1: 432, 15: 60, 11: 24}

# WarDatabase 解析失败时的编制兜底（与官方表同值，20 种 AT）
_FALLBACK_COMP = {
    'guard': {1: 36},
    'motorized_guard': {1: 36, 11: 12},
    'infantry': {1: 90},
    'motorized_infantry': {1: 90, 15: 30},
    'mechanized_infantry': {1: 100, 9: 10},
    'recon': {6: 20},
    'motorized_recon': {6: 24, 11: 12},
    'mechanized_recon': {6: 28, 16: 14},
    'paratroopers': {4: 72, 10: 18},
    'pathfinders': {4: 36, 21: 9},
    'paratroopers_on_ground': {4: 72},
    'pathfinders_on_ground': {4: 36},
    'fighter_squadron': {2: 24, 20: 24},
    'heavy_fighter_squadron': {2: 24, 3: 24},
    'fighter_recon': {2: 20, 21: 16},
    'light_armor': {5: 20, 12: 16},
    'medium_armor': {5: 20, 7: 18},
    'medium_tank_destroyer': {5: 16, 8: 14},
    'heavy_armor': {5: 16, 17: 14},
    'heavy_tank_destroyer': {5: 12, 18: 10},
}


def _load_at_compositions():
    """解析 AT 编制表 → {AT 名: {资源类别id: count}}（取任一阵营，编制同构）。"""
    # commandnodetemplate id → 名字（f1 id f2 name f3 factionTemplateId）
    names = {}
    try:
        for r in _parse_wardb_table('commandnodetemplate'):
            nid = int(r.get(1, 0) or 0)
            nm = r.get(2, b'')
            if nid and isinstance(nm, (bytes, bytearray)) and nm:
                names[nid] = nm.decode('utf-8', 'replace')
    except Exception as e:
        log(f'[force_pool] commandnodetemplate parse failed: {e}')
        return {}
    comp = {}
    try:
        for r in _parse_wardb_table(
                'commandnodetemplatearmyresourcetemplaterelation'):
            at_id = int(r.get(3, 0) or 0)   # f3 AT 模板 id
            cat = int(r.get(2, 0) or 0)     # f2 资源类别 id
            cnt = int(r.get(4, 0) or 0)     # f4 count
            name = names.get(at_id)
            if not name or not cat:
                continue
            comp.setdefault(name, {})[cat] = comp.get(name, {}).get(cat, 0) + cnt
    except Exception as e:
        log(f'[force_pool] AT relation parse failed: {e}')
        return {}
    return comp


_AT_COMP = _load_at_compositions() or dict(_FALLBACK_COMP)

_missing = [n for fam in AT_FAMILIES.values() for n in fam if n not in _AT_COMP]
if _missing:
    log(f'[force_pool] WARNING: AT missing in WarDatabase: {_missing}')


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


def draw_at_names(gamemode_id):
    """按模式抽一份 AT 名单（一场一份，双侧镜像）。"""
    mode_name = mm_config.gamemode_cfg(gamemode_id).get('name', 'default')
    if mode_name == 'training':
        return list(ANCHOR_ATS), mode_name
    plan = MODE_PLANS.get(mode_name, DEFAULT_PLAN)
    names = []
    for family in ('infantry', 'recon', 'para', 'armor', 'air'):
        names += _draw_family(family, plan.get(family, 0))
    return names, mode_name


# ---------------------------------------------------------------- 每局池状态
_MISSIONS = {}   # mission_id -> {'mode': 名, 'names': [AT 名], 'pool': {faction: {cat: count}}}


def init_mission(mission_id, gamemode_id, attacker, defender):
    """成局建池：按模式抽 AT 名单，双侧镜像同编制。"""
    names, mode_name = draw_at_names(gamemode_id)
    counts = {}
    for name in names:
        for cat, cnt in _AT_COMP.get(name, {}).items():
            counts[cat] = counts.get(cat, 0) + cnt
    _MISSIONS[int(mission_id)] = {
        'mode': mode_name, 'names': names,
        'pool': {int(attacker): dict(counts), int(defender): dict(counts)},
    }
    log(f'[force_pool] mission={mission_id} mode={mode_name} '
        f'ATs[{len(names)}]={",".join(names)} pool={counts}')
    return True


def pool(mission_id):
    """本局资源池 {faction: {cat: count}}；无池返回 None。"""
    rec = _MISSIONS.get(int(mission_id or 0))
    return rec['pool'] if rec else None


def counts_for(mission_id, faction):
    """某阵营各类剩余量；池/阵营缺失回退锚点值。"""
    rec = _MISSIONS.get(int(mission_id or 0))
    if rec and int(faction) in rec['pool']:
        return rec['pool'][int(faction)]
    return dict(FALLBACK_COUNTS)


def categories(mission_id=None, counts_by_faction=None):
    """渲染用有序类别表 [(cat, spawnInAir)]。

    优先取池内实际出现的类别（含坦克/载具等池内新增类别）；
    无池回退锚点池类别（1/15/11）。
    """
    counts = None
    if counts_by_faction:
        counts = next(iter(counts_by_faction.values()), None)
    elif mission_id is not None:
        rec = _MISSIONS.get(int(mission_id or 0))
        if rec:
            counts = next(iter(rec['pool'].values()))
    if not counts:
        counts = FALLBACK_COUNTS
    cats = [c for c in _CAT_ORDER if c in counts]
    cats += sorted(set(counts) - set(_CAT_ORDER))   # 未知类别兜底追加
    return [(c, c in _AIR_CATS) for c in cats]


def consume(mission_id, faction, cat, n=1):
    """出生消耗（按兵种类别扣 n）。返回扣后余量；池/类别缺失返回 None。"""
    rec = _MISSIONS.get(int(mission_id or 0))
    if rec is None:
        return None
    counts = rec['pool'].get(int(faction))
    if counts is None or int(cat) not in counts:
        return None
    counts[int(cat)] = max(0, counts[int(cat)] - int(n))
    return counts[int(cat)]


def release(mission_id):
    """局结束清理（与 mission details 同生命周期）。"""
    _MISSIONS.pop(int(mission_id or 0), None)


def assault_team_names(mission_id):
    """本局抽中的 AT 名单（f4 渲染按阵营查 commandnodetemplate 翻译）。"""
    rec = _MISSIONS.get(int(mission_id or 0))
    return list(rec['names']) if rec else None
