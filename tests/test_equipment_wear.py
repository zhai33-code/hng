# -*- coding: utf-8 -*-
"""装备维护损耗（condition 磨损）回归测试。

覆盖三项演进：
1. 2026-08-31：结算无维护消耗 → _apply_equipment_wear 按 timeActive 估算
   扣减；condition 量纲错乱修复（载具点数 25~220 / 武器配件 0~1）
2. 2026-08-31：autoRepair 三态扣费修复（价格公式/余额不足/定价缺失）
3. 2026-09-09：估算 → type6 实际用量（官服按发/按出生计费语义）：
   - statistics_server._accumulate_battle_usage 累积每局每武器模板实际
     发数（stat 519）与角色出生数（stat 12），session 尾数=missionId 关联
   - 载具武器发数双报（裸模板 + 载具<<16|模板）同帧去重
   - 无 type6 数据的局/角色整体跳过磨损（2026-09-08 1000093
     "带了手雷没用也扣维护费"事故——估算算法对 0 发玩家按满额扣费）

分五部分：
A. wardb 磨损静态表（纯单测，无需 DB）
B. type6 用量累积/取走（纯单测，statistics_server 内存态）
C. _apply_equipment_wear 实际用量磨损数学 + 跳过规则（内存 FakeStore）
D. 完整结算链路触达磨损（handle_announce_battlefield_earnings + FakeStore）
E. 真实 DB 集成（事务内回滚，不改库）：迁移归一化 / create_vehicle / 磨损扣减
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import statistics_server
import wardb
from handlers import combat as combat_handlers
from store import PlayerStore

# ---- wardb 模板锚点（WarDatabase 实测分布，2026-08-31 dump）----
# 载具满耐久集合；未知模板回退 100.0
_VEH_MAX_SET = {25.0, 35.0, 40.0, 44.0, 45.0, 50.0, 55.0, 60.0, 65.0,
                75.0, 200.0, 220.0}


def _variant_for_parent_max(max_cond):
    """找一个基础模板满耐久 == max_cond 的载具变体 id。"""
    by_max = {t for t, p in wardb._VEHICLE_WEAR.items() if p[0] == max_cond}
    for vid, parent in wardb._VEHICLE_VARIANT_PARENT.items():
        if parent in by_max:
            return vid
    raise AssertionError(f'wardb 中找不到 maxCondition={max_cond} 的变体')


def _weapon_template(wear=True, exclude=()):
    """wear=True 取一个可磨损武器模板（跳过 exclude），否则取磨损率全 0 的。"""
    for t, p in wardb._WEAPON_WEAR.items():
        if t in exclude:
            continue
        if bool(p[1] or p[2]) == wear:
            return t
    raise AssertionError('wardb 武器表为空')


def _vehicle_ammo_combo():
    """找一个 (变体, 基础模板, 武器模板, 弹药模板) 组合：
    载具有驾驶员主武器，且该武器有 wearPerShot>0 的弹药映射
    （载具弹药磨损按发计费的归属链前提）。"""
    for parent, slots in sorted(wardb._VEHICLE_WEAPON_SLOTS.items()):
        for (wt, _storage) in slots:
            for (_mid, at, _dflt) in sorted(
                    wardb._AMMO_MAPPINGS_BY_WEAPON.get(wt, ())):
                p = wardb.ammo_wear_params(at)
                if p and p[1] > 0:
                    for vid, par in wardb._VEHICLE_VARIANT_PARENT.items():
                        if par == parent:
                            return vid, parent, wt, at
    raise AssertionError('wardb 中找不到带可磨损弹药的载具武器组合')


# ---------- A. wardb 磨损静态表 ----------

def test_wardb_vehicle_wear_table():
    """载具表：满耐久 25~220 点、全表 wearPerSpawn=1.0、wearPerBattle=0。"""
    vv = wardb._VEHICLE_WEAR
    assert vv, 'vehicletemplate 磨损表为空'
    assert all(p[0] > 0 for p in vv.values()), '存在 maxCondition<=0 的载具'
    assert {p[0] for p in vv.values()} == _VEH_MAX_SET, \
        f'满耐久集合漂移: {sorted({p[0] for p in vv.values()})}'
    assert all(p[1] == 1.0 for p in vv.values()), 'wearPerSpawn 应全表 1.0'
    assert all(p[2] == 0.0 for p in vv.values()), 'wearPerBattle 应全表 0'


def test_wardb_vehicle_full_condition():
    """变体 → 基础模板满耐久；未知模板回退 100.0。"""
    assert wardb.vehicle_full_condition(_variant_for_parent_max(25.0)) == 25.0
    assert wardb.vehicle_full_condition(_variant_for_parent_max(200.0)) == 200.0
    assert wardb.vehicle_full_condition(999999999) == 100.0
    # 磨损参数查询与满耐久同源
    params = wardb.vehicle_wear_params_for_variant(_variant_for_parent_max(60.0))
    assert params == (60.0, 1.0, 0.0), params


def test_wardb_weapon_and_mod_wear_tables():
    """武器/配件量纲 0~1：可磨损条目 maxCondition 恒 1.0（120 个不可维护
    武器模板 maxCondition=0 且磨损率全 0——匕首/手雷/望远镜等），存在可磨损条目。"""
    for table, params_fn in ((wardb._WEAPON_WEAR, wardb.weapon_wear_params),
                             (wardb._WEAPON_MOD_WEAR,
                              wardb.weapon_modifier_wear_params)):
        assert table, '磨损表为空'
        # 可磨损条目（wearPerShot/wearPerBattle 非零）满耐久必为 1.0
        wearable = [t for t, p in table.items() if p[1] or p[2]]
        assert wearable, '应存在可磨损条目'
        assert all(table[t][0] == 1.0 for t in wearable), \
            '可磨损条目 maxCondition 应为 1.0（0~1 量纲）'
        # maxCondition=0（不可维护）的条目磨损率必为 0
        assert all(p[1] == 0 and p[2] == 0
                   for p in table.values() if p[0] == 0.0), \
            '不可维护条目（maxCondition=0）不应带磨损率'
        # wearPerShot 量级：武器 8e-6~3.13e-4、配件 1.93e-4~1.984e-3
        bound = 1e-3 if table is wardb._WEAPON_WEAR else 5e-3
        assert 0 < max(p[1] for p in table.values()) < bound, \
            f'wearPerShot 量级异常（应 < {bound}）'
        # 查询函数与表一致；未知模板返回 None（不可维护，跳过磨损）
        t0 = wearable[0]
        assert params_fn(t0) == table[t0]
        assert params_fn(999999999) is None
    # 配件全表可维护（98/98）
    assert all(p[0] == 1.0 for p in wardb._WEAPON_MOD_WEAR.values())


def test_wardb_ammo_wear_table():
    """弹药 wearPerShot ∈ {0, 0.5, 1.0}（condition 即剩余发数）。"""
    rates = {p[1] for p in wardb._AMMO_WEAR.values()}
    assert rates <= {0.0, 0.5, 1.0}, f'出现未知弹药磨损率: {rates}'
    assert rates & {1.0}, '应存在按发消耗（1.0）的弹药'
    assert wardb.ammo_wear_params(999999999) is None


def test_wardb_vehicle_ammo_weapon_templates():
    """载具弹药 → 武器模板归属链（按发计费的发数来源）。"""
    vid, parent, wt, at = _vehicle_ammo_combo()
    assert wt in wardb.vehicle_ammo_weapon_templates(parent, at)
    # 未知弹药/未知载具 → 空集合（磨损 0，宁可不扣）
    assert wardb.vehicle_ammo_weapon_templates(parent, 999999999) == set()
    assert wardb.vehicle_ammo_weapon_templates(999999999, at) == set()
    # 实测锚点：vehicle 58 主武器 88 可发射默认弹药模板 139
    # （w88 映射表 (291, 139, True)：291 是映射 id，139 才是弹药模板 id）
    assert 88 in wardb.vehicle_ammo_weapon_templates(58, 139)


# ---------- B. type6 用量累积/取走 ----------

def _usage_cleanup():
    statistics_server._BATTLE_USAGE.clear()
    statistics_server._BATTLE_USAGE_KEYS.clear()
    statistics_server._BATTLE_USAGE_DONE.clear()


def test_battle_usage_accumulates_and_links_by_mission_suffix():
    """session 尾数 = missionId（730433917100003 → 100003）；发数/出生数累积；
    跨帧（增量语义）累加；取走即删，二次取走返回 None。"""
    _usage_cleanup()
    try:
        sid = 730433917100003
        # 帧 1：武器 20 打 500 发、出生 2 次
        statistics_server._accumulate_battle_usage(
            sid, [(11, [(519, 20, 0, 500), (12, 0, 0, 2)], [])])
        # 帧 2（增量）：武器 20 再 100 发、出生 1 次
        statistics_server._accumulate_battle_usage(
            sid, [(11, [(519, 20, 0, 100), (12, 0, 0, 1)], [])])
        usage = statistics_server.pop_battle_usage(100003)
        assert usage == {11: {'shots': {20: 600}, 'spawns': 3}}, usage
        assert statistics_server.pop_battle_usage(100003) is None, '取走即删'
    finally:
        _usage_cleanup()


def test_battle_usage_dual_form_dedup():
    """载具武器发数双报去重：裸模板 w88 与 (载具 58<<16|88) 同值并存，
    按低 16 位模板取 max 只计一次（char 177 实测 30/30 → 30 非 60）。"""
    _usage_cleanup()
    try:
        statistics_server._accumulate_battle_usage(
            730433917100099,
            [(177, [(519, 88, 0, 30), (519, (58 << 16) | 88, 0, 30)], [])])
        usage = statistics_server.pop_battle_usage(100099)
        assert usage[177]['shots'] == {88: 30}, usage
    finally:
        _usage_cleanup()


def test_battle_usage_fifo_eviction_and_bad_sid():
    """超过 _BATTLE_USAGE_MAX 局未结算 → FIFO 淘汰最旧；尾数 0 的 sid 忽略。
    （730433917100000 尾数 = 100000，非 0）"""
    _usage_cleanup()
    try:
        base = 730433917100000       # 尾数 100000
        for i in range(statistics_server._BATTLE_USAGE_MAX + 5):
            statistics_server._accumulate_battle_usage(
                base + i, [(11, [(12, 0, 0, 1)], [])])
        assert len(statistics_server._BATTLE_USAGE) == \
            statistics_server._BATTLE_USAGE_MAX
        assert 100000 not in statistics_server._BATTLE_USAGE, '最旧局应被淘汰'
        # 最后存入的局 id = 100000 + MAX + 4 仍在
        assert 100000 + statistics_server._BATTLE_USAGE_MAX + 4 in \
            statistics_server._BATTLE_USAGE
        statistics_server._accumulate_battle_usage(
            730433917000000, [(11, [(12, 0, 0, 1)], [])])   # 尾数 0
        assert 0 not in statistics_server._BATTLE_USAGE
    finally:
        _usage_cleanup()


def _type6_body(session_id, chars):
    """构造 type6 解压后载荷：<q session><I nChars>{<q char><I nStats>
    {<Iqqq stat>}<I nRibbons>{<qdId ribbon>}}。"""
    import struct
    buf = struct.pack('<q', session_id) + struct.pack('<I', len(chars))
    for cid, stats, ribbons in chars:
        buf += struct.pack('<q', cid) + struct.pack('<I', len(stats))
        for st in stats:
            buf += struct.pack('<Iqqq', *st)
        buf += struct.pack('<I', len(ribbons))
        for rb in ribbons:
            buf += struct.pack('<qdId', *rb)
    return buf


def test_type6_accumulates_before_persist():
    """入账必须先于落库：落库逐条 await STORE.add_stat 会反复让出事件
    循环，结算可在落库间隙插队 pop——若先落库后累积，pop 时用量未入账，
    整局磨损被误判"无 type6 数据"跳过（2026-09-09 mission=356460
    四角色全漏扣的根因）。"""
    _usage_cleanup()
    order = []
    orig_persist = statistics_server._persist_type6

    async def fake_persist(chars):
        order.append('usage-ready'
                     if statistics_server._BATTLE_USAGE.get(100777)
                     else 'usage-missing')
        return await orig_persist(chars)

    statistics_server._persist_type6 = fake_persist
    try:
        body = _type6_body(730433917100777,
                           [(11, [(519, 20, 0, 7), (12, 0, 0, 1)], [])])
        assert body[:1] != b'\x78', '不应误入 zlib 变体分支'
        asyncio.run(statistics_server.process_type6_body(body, peer='t'))
        assert order == ['usage-ready'], order
        assert statistics_server.pop_battle_usage(100777) == \
            {11: {'shots': {20: 7}, 'spawns': 1}}
    finally:
        statistics_server._persist_type6 = orig_persist
        _usage_cleanup()


def test_wait_battle_usage_semantics():
    """结算等待消耗数据到达才计费：①在途→等到并取走；②已取走过的局
    （重复结算）→ 立即 None 不空等；③始终不到 → 超时 None（宁可不扣）；
    ④尾数 0 的非法 missionId 立即 None。"""
    _usage_cleanup()
    try:
        # ① 数据在路上（0.2s 后到达），等待应取到
        async def late_arrival():
            task = asyncio.ensure_future(
                statistics_server.wait_battle_usage(100555, timeout=2.0))
            await asyncio.sleep(0.2)
            statistics_server._accumulate_battle_usage(
                730433917100555, [(11, [(12, 0, 0, 1)], [])])
            return await task
        assert asyncio.run(late_arrival()) == \
            {11: {'shots': {}, 'spawns': 1}}
        # ② 重复结算：已 pop 的局立即返回 None（不空等）
        t0 = time.monotonic()
        assert asyncio.run(statistics_server.wait_battle_usage(
            100555, timeout=5.0)) is None
        assert time.monotonic() - t0 < 1.0, '已结算局不应等待'
        # ③ 数据始终不来：超时 None
        t0 = time.monotonic()
        assert asyncio.run(statistics_server.wait_battle_usage(
            100666, timeout=0.1)) is None
        assert time.monotonic() - t0 >= 0.1, '应等满超时'
        # ④ 非法 missionId（尾数 0）
        assert asyncio.run(statistics_server.wait_battle_usage(
            0, timeout=0.1)) is None
    finally:
        _usage_cleanup()


# ---------- C. 实际用量磨损数学与跳过规则（内存 FakeStore）----------

class _WearStore:
    """内存装备表替身：list_* 按角色过滤，update_* 原地改 condition，
    另带结算入库空实现（供 D 部分完整 handler 复用）。"""

    def __init__(self):
        self.weapons = []
        self.modifiers = []
        self.weapon_ammo = []
        self.vehicles = []
        self.vehicle_upgrades = []
        self.vehicle_ammo = []
        self.balances = {}      # (pid, currency_key) -> 余额
        self.spent = []         # [(pid, currency_key, cost), ...]

    # ---- 结算入库（D 部分用）----
    async def save_battle_report(self, report, players):
        return 1

    async def add_currency(self, player_id, credits=0, warfunds=0):
        return {'credits': credits, 'warfunds': warfunds}

    async def spend_currency(self, player_id, currency_key, cost, conn=None):
        bal = self.balances.get((player_id, currency_key), 0)
        if bal < cost:
            return False, bal
        self.balances[(player_id, currency_key)] = bal - cost
        self.spent.append((player_id, currency_key, cost))
        return True, bal - cost

    async def add_combat_xp(self, pid, cid, xp):
        return xp

    async def add_battle_pass_score(self, pid, season, score):
        return score

    async def add_stat(self, cid, key, val):
        return None

    async def set_ribbon_xp_floor(self, pid, cid, rid, xp):
        return None

    # ---- 装备查询 ----
    async def list_weapons(self, player_id, character_id=None):
        return [dict(w) for w in self.weapons
                if w['player_id'] == player_id
                and (character_id is None
                     or w['character_id'] == character_id)]

    async def list_weapon_modifiers(self, player_id, character_id=None):
        return [dict(m) for m in self.modifiers
                if m['player_id'] == player_id
                and (character_id is None
                     or m['character_id'] == character_id)]

    async def list_weapon_ammo(self, player_id, character_id=None):
        return [dict(a) for a in self.weapon_ammo
                if a['player_id'] == player_id
                and (character_id is None
                     or a['character_id'] == character_id)]

    async def list_vehicles(self, player_id, character_id=None):
        return [dict(v) for v in self.vehicles
                if v['player_id'] == player_id
                and (character_id is None
                     or v['character_id'] == character_id)]

    async def list_vehicle_upgrades(self, player_id):
        return [dict(u) for u in self.vehicle_upgrades
                if u['player_id'] == player_id]

    async def list_vehicle_ammo(self, player_id):
        return [dict(a) for a in self.vehicle_ammo
                if a['player_id'] == player_id]

    # ---- condition 更新 ----
    async def _set(self, rows, item_id, condition):
        for r in rows:
            if r['id'] == item_id:
                r['condition'] = float(condition)
                return

    async def update_weapon_condition(self, pid, wid, cond):
        await self._set(self.weapons, wid, cond)

    async def update_weapon_modifier_condition(self, pid, mid, cond):
        await self._set(self.modifiers, mid, cond)

    async def update_weapon_ammo_condition(self, pid, aid, cond):
        await self._set(self.weapon_ammo, aid, cond)

    async def update_vehicle_condition(self, pid, vid, cond):
        await self._set(self.vehicles, vid, cond)

    async def update_vehicle_upgrade_condition(self, pid, uid, cond):
        await self._set(self.vehicle_upgrades, uid, cond)

    async def update_vehicle_ammo_condition(self, pid, aid, cond):
        await self._set(self.vehicle_ammo, aid, cond)


def _build_loadout():
    """构造 pid=1 char=11 的装备快照（usage：wA 打 500 发、载具主武器打
    500 发、出生 3 次）。

    wA 主武器（可磨损，slot1）        → 按模板实际发数 500 定损
    wB 副武器（异模板，未开火）       → 不扣（实际 0 发）
    wC 不可维护武器（磨损率全 0）     → 不扣
    wD 已损坏（condition=0）          → 不扣（不为负）
    wE 仓库武器（container_slot=0）   → 不扣
    mA wA 上的配件                    → 随 wA 发数定损
    mE wE 上的配件                    → 不扣
    aA wA 的在膛弹药（slot 1）        → 按发扣
    aR wA 的储备弹药（slot 0）        → 不扣
    vA 已装备载具                     → 满耐久 - 出生数 3
    vR 仓库载具（slot 0）             → 不扣
    uA vA 槽位 5 上的升级             → 满耐久 - 3
    uR vA 上未装备的升级（slot 0）    → 不扣
    va vA 弹药仓 2 的载具弹药         → 按载具主武器发数扣
    """
    st = _WearStore()
    wt = _weapon_template(wear=True)              # 可磨损武器模板（wA）
    # wB 用另一个可磨损模板（真实装备槽类别互斥，装备中不会有同模板双件；
    # 测试用它验证"未开火武器 0 磨损"——官服语义）
    wt2 = _weapon_template(wear=True, exclude={wt})
    wt0 = _weapon_template(wear=False)            # 磨损率全 0 模板
    wp = wardb.weapon_wear_params(wt)
    mt = next(t for t, p in wardb._WEAPON_MOD_WEAR.items() if p[1])
    mp = wardb.weapon_modifier_wear_params(mt)
    at = next(t for t, p in wardb._AMMO_WEAR.items() if p[1] == 1.0)
    vvid, _vparent, vwt, vat = _vehicle_ammo_combo()
    vmax = wardb.vehicle_full_condition(vvid)
    vamax = wardb.ammo_wear_params(vat)[0]
    ut = next(iter(wardb._VEHICLE_UPGRADE_WEAR))
    st.weapons = [
        {'id': 101, 'player_id': 1, 'template_id': wt, 'character_id': 11,
         'container_id': 11, 'container_slot': 1, 'condition': 1.0},
        {'id': 102, 'player_id': 1, 'template_id': wt2, 'character_id': 11,
         'container_id': 11, 'container_slot': 2, 'condition': 1.0},
        {'id': 103, 'player_id': 1, 'template_id': wt0, 'character_id': 11,
         'container_id': 11, 'container_slot': 3, 'condition': 1.0},
        {'id': 104, 'player_id': 1, 'template_id': wt, 'character_id': 11,
         'container_id': 11, 'container_slot': 4, 'condition': 0.0},
        {'id': 105, 'player_id': 1, 'template_id': wt, 'character_id': 11,
         'container_id': 0, 'container_slot': 0, 'condition': 1.0},
    ]
    st.modifiers = [
        {'id': 201, 'player_id': 1, 'template_id': mt, 'character_id': 11,
         'weapon_id': 101, 'condition': 1.0},
        {'id': 202, 'player_id': 1, 'template_id': mt, 'character_id': 11,
         'weapon_id': 105, 'condition': 1.0},
    ]
    st.weapon_ammo = [
        {'id': 301, 'player_id': 1, 'ammo_template_id': at, 'character_id': 11,
         'weapon_id': 101, 'slot_index': 1, 'condition': 2000.0},
        {'id': 302, 'player_id': 1, 'ammo_template_id': at, 'character_id': 11,
         'weapon_id': 101, 'slot_index': 0, 'condition': 500.0},
    ]
    st.vehicles = [
        {'id': 401, 'player_id': 1, 'vehicle_variant_template_id': vvid,
         'character_id': 11, 'equipped_in_slot': 1, 'condition': vmax},
        {'id': 402, 'player_id': 1, 'vehicle_variant_template_id': vvid,
         'character_id': 11, 'equipped_in_slot': 0, 'condition': vmax},
    ]
    st.vehicle_upgrades = [
        {'id': 501, 'player_id': 1, 'upgrade_template_id': ut, 'vehicle_id': 401,
         'character_id': 11, 'equipped_slot_id': 5, 'condition': 100.0},
        {'id': 502, 'player_id': 1, 'upgrade_template_id': ut, 'vehicle_id': 401,
         'character_id': 11, 'equipped_slot_id': 0, 'condition': 100.0},
    ]
    st.vehicle_ammo = [
        {'id': 601, 'player_id': 1, 'ammo_template_id': vat, 'vehicle_id': 401,
         'character_id': 11, 'equipped_storage_id': 2, 'condition': vamax},
    ]
    # 载具武器发数取弹药容量一半（不同组合的弹药容量 25~400 不等，防钳底）
    vshots = max(1, int(vamax // 2))
    usage = {11: {'shots': {wt: 500, vwt: vshots}, 'spawns': 3}}
    return st, {'wp': wp, 'mp': mp, 'wt': wt, 'wt2': wt2, 'at': at,
                'vvid': vvid, 'vwt': vwt, 'vat': vat, 'vmax': vmax,
                'vamax': vamax, 'vshots': vshots, 'usage': usage}


def test_apply_equipment_wear_math_and_skips():
    st, p = _build_loadout()
    old = combat_handlers.STORE
    combat_handlers.STORE = st
    try:
        asyncio.run(combat_handlers._apply_equipment_wear(
            {(1, 11): 900}, p['usage']))
    finally:
        combat_handlers.STORE = old
    w = {x['id']: x['condition'] for x in st.weapons}
    m = {x['id']: x['condition'] for x in st.modifiers}
    a = {x['id']: x['condition'] for x in st.weapon_ammo}
    v = {x['id']: x['condition'] for x in st.vehicles}
    u = {x['id']: x['condition'] for x in st.vehicle_upgrades}
    va = {x['id']: x['condition'] for x in st.vehicle_ammo}
    wp, mp = p['wp'], p['mp']
    # 主武器按模板实际发数定损；副武器异模板未开火 → 0 磨损
    assert w[101] == 1.0 - wp[1] * 500, w[101]
    assert w[102] == 1.0, w[102]
    # 不可维护 / 已损坏 / 未装备 → 不扣
    assert w[103] == 1.0 and w[104] == 0.0 and w[105] == 1.0
    # 配件随已装备武器发数；挂仓库武器不扣
    assert m[201] == 1.0 - mp[1] * 500, m[201]
    assert m[202] == 1.0
    # 在膛弹药按发扣（2000 发扣 500）；储备不扣
    assert a[301] == 2000.0 - 500, a[301]
    assert a[302] == 500.0
    # 载具按实际出生数：vmax - 1.0*3；仓库载具不扣
    assert v[401] == p['vmax'] - 3.0, v[401]
    assert v[402] == p['vmax']
    # 升级同载具规则；未入槽不扣
    assert u[501] == 97.0, u[501]
    assert u[502] == 100.0
    # 载具弹药按该载具主武器实际发数扣
    vrate = wardb.ammo_wear_params(p['vat'])[1]
    assert va[601] == p['vamax'] - vrate * p['vshots'], va[601]


def test_apply_equipment_wear_zero_usage_never_charges():
    """usage 有数据但 0 发 0 出生 → 一分不扣（1000093 事故场景：
    带了手雷一发未扔，维护费应为 0 而非估算满额）。"""
    st, p = _build_loadout()
    old = combat_handlers.STORE
    combat_handlers.STORE = st
    try:
        asyncio.run(combat_handlers._apply_equipment_wear(
            {(1, 11): 900}, {11: {'shots': {}, 'spawns': 0}}))
    finally:
        combat_handlers.STORE = old
    assert all(x['condition'] == 1.0 or x['condition'] == 0.0
               for x in st.weapons), '0 发不应扣武器'
    assert st.weapon_ammo[0]['condition'] == 2000.0, '0 发不应扣弹药'
    assert st.vehicles[0]['condition'] == p['vmax'], '0 出生不应扣载具'
    assert st.vehicle_ammo[0]['condition'] == p['vamax'], '0 发不应扣载具弹药'
    assert st.spent == [], '不应产生任何维修扣费'


def test_apply_equipment_wear_missing_usage_skips():
    """无 type6 数据（usage=None 或角色不在其中）→ 整体跳过磨损
    （宁可不扣不多扣——统计链路异常时不编造损耗）。"""
    st, _ = _build_loadout()
    old = combat_handlers.STORE
    combat_handlers.STORE = st
    try:
        asyncio.run(combat_handlers._apply_equipment_wear({(1, 11): 900}, None))
        # 局内有其他角色数据但本角色缺失
        asyncio.run(combat_handlers._apply_equipment_wear(
            {(1, 11): 900}, {999: {'shots': {1: 1}, 'spawns': 1}}))
    finally:
        combat_handlers.STORE = old
    assert all(x['condition'] == 1.0 or x['condition'] == 0.0
               for x in st.weapons), '无数据不应扣武器'
    assert st.vehicles[0]['condition'] > 0, '无数据不应扣载具'


def test_apply_equipment_wear_zero_active_skipped():
    """timeActive=0（未参战）角色整体跳过，任何装备不扣。"""
    st, p = _build_loadout()
    old = combat_handlers.STORE
    combat_handlers.STORE = st
    try:
        asyncio.run(combat_handlers._apply_equipment_wear(
            {(1, 11): 0}, p['usage']))
    finally:
        combat_handlers.STORE = old
    assert all(x['condition'] == 1.0 or x['condition'] == 0.0
               for x in st.weapons), '未参战不应扣武器'
    assert st.vehicles[0]['condition'] == p['vmax'], '未参战不应扣载具'


def test_apply_equipment_wear_worn_not_negative():
    """低耐久物品扣减后钳到 0，不为负。"""
    st, p = _build_loadout()
    st.weapons[0]['condition'] = 0.001          # 剩余耐久不足一局定损
    st.vehicles[0]['condition'] = 2.0           # 满耐久车仅剩 2 点
    usage = {11: {'shots': {p['wt']: 10 ** 9}, 'spawns': 10}}
    old = combat_handlers.STORE
    combat_handlers.STORE = st
    try:
        asyncio.run(combat_handlers._apply_equipment_wear(
            {(1, 11): 1800}, usage))
    finally:
        combat_handlers.STORE = old
    assert st.weapons[0]['condition'] == 0.0
    assert st.vehicles[0]['condition'] == 0.0


# ---------- C2. 战后自动维修（autoRepair 扣费 + 修复满耐久）----------

def _priced_weapon_template():
    """找一个可磨损且有维修定价的武器模板。"""
    for t, p in wardb._WEAPON_WEAR.items():
        if (p[1] or p[2]) and wardb.repair_base_price(
                'WeaponMaxConditionRepair', t, 'credits') > 0:
            return t
    raise AssertionError('wardb 中找不到带维修定价的可磨损武器模板')


def test_autorepair_credits_charges_and_restores():
    """autoRepair=1(Credits)：战后按实际损耗扣费修复到满耐久，费用与客户端
    PriceObject.calculateFractionPrice 同式 max(1, int(基础价×损耗比例))；
    同角色未开启自动维修的物品照常磨损。"""
    st, p = _build_loadout()
    wt = _priced_weapon_template()
    wp = wardb.weapon_wear_params(wt)
    base = wardb.repair_base_price('WeaponMaxConditionRepair', wt, 'credits')
    st.weapons[0]['template_id'] = wt     # wA 主武器 slot1
    st.weapons[0]['auto_repair'] = 1
    usage = {11: {'shots': {wt: 500}, 'spawns': 3}}
    st.balances[(1, 'credits')] = 10 ** 9
    old = combat_handlers.STORE
    combat_handlers.STORE = st
    try:
        asyncio.run(combat_handlers._apply_equipment_wear(
            {(1, 11): 900}, usage))
    finally:
        combat_handlers.STORE = old
    worn = 1.0 - wp[1] * 500
    expect_cost = max(1, int(base * (1.0 - worn) / 1.0))
    assert st.weapons[0]['condition'] == 1.0, '应修复到满耐久'
    assert st.spent == [(1, 'credits', expect_cost)], st.spent
    assert st.balances[(1, 'credits')] == 10 ** 9 - expect_cost
    # 副武器（异模板未开火）无磨损、无扣费
    assert st.weapons[1]['condition'] == 1.0


def test_autorepair_insufficient_funds_keeps_worn():
    """autoRepair=2(Gold) 余额不足：不扣款、保持磨损。"""
    st, _ = _build_loadout()
    wt = _priced_weapon_template()
    wp = wardb.weapon_wear_params(wt)
    st.weapons[0]['template_id'] = wt
    st.weapons[0]['auto_repair'] = 2
    usage = {11: {'shots': {wt: 500}, 'spawns': 3}}
    st.balances[(1, 'gold')] = 0
    old = combat_handlers.STORE
    combat_handlers.STORE = st
    try:
        asyncio.run(combat_handlers._apply_equipment_wear(
            {(1, 11): 900}, usage))
    finally:
        combat_handlers.STORE = old
    assert st.weapons[0]['condition'] == 1.0 - wp[1] * 500, '应保持磨损'
    assert st.spent == [], '不应扣款'


def test_autorepair_missing_pricing_repairs_free():
    """定价缺失（基础价 0）：免费修复（与手动维修定价缺失免费口径一致）。"""
    st, _ = _build_loadout()
    wt = _priced_weapon_template()
    st.weapons[0]['template_id'] = wt
    st.weapons[0]['auto_repair'] = 1
    usage = {11: {'shots': {wt: 500}, 'spawns': 3}}
    old_store = combat_handlers.STORE
    old_price = combat_handlers.repair_base_price
    combat_handlers.STORE = st
    combat_handlers.repair_base_price = lambda *a: 0
    try:
        asyncio.run(combat_handlers._apply_equipment_wear(
            {(1, 11): 900}, usage))
    finally:
        combat_handlers.STORE = old_store
        combat_handlers.repair_base_price = old_price
    assert st.weapons[0]['condition'] == 1.0, '应免费修复到满耐久'
    assert st.spent == []


def test_autorepair_vehicle_gold():
    """载具 autoRepair=2(Gold)：按实际出生数磨损比例扣 Gold 修复到满耐久。"""
    st, p = _build_loadout()
    vt = st.vehicles[0]['vehicle_variant_template_id']
    base = wardb.repair_base_price('VehicleMaxConditionRepair',
                                   wardb.vehicle_parent_template_id(vt), 'gold')
    assert base > 0, '测试前提：载具模板需有维修定价'
    st.vehicles[0]['auto_repair'] = 2
    st.balances[(1, 'gold')] = 10 ** 9
    old = combat_handlers.STORE
    combat_handlers.STORE = st
    try:
        asyncio.run(combat_handlers._apply_equipment_wear(
            {(1, 11): 900}, p['usage']))
    finally:
        combat_handlers.STORE = old
    expect_cost = max(1, int(base * 3 / p['vmax']))
    assert st.vehicles[0]['condition'] == p['vmax'], '应修复到满耐久'
    assert (1, 'gold', expect_cost) in st.spent, st.spent


def test_autorepair_ammo_refill():
    """弹药 autoRepair=1：按实际消耗发数比例扣费回满。"""
    st, p = _build_loadout()
    at = st.weapon_ammo[0]['ammo_template_id']
    amax = wardb.ammo_wear_params(at)[0]
    base = wardb.repair_base_price('AmmoMaxConditionRepair', at, 'credits')
    assert base > 0, '测试前提：弹药模板需有维修定价'
    st.weapon_ammo[0]['condition'] = amax
    st.weapon_ammo[0]['auto_repair'] = 1
    st.balances[(1, 'credits')] = 10 ** 9
    old = combat_handlers.STORE
    combat_handlers.STORE = st
    try:
        asyncio.run(combat_handlers._apply_equipment_wear(
            {(1, 11): 900}, p['usage']))
    finally:
        combat_handlers.STORE = old
    expect_cost = max(1, int(base * 500 / amax))
    assert st.weapon_ammo[0]['condition'] == amax, '应回满'
    assert (1, 'credits', expect_cost) in st.spent, st.spent


# ---------- D. 完整结算链路触达磨损 ----------

class _MockWriter:
    def __init__(self):
        self.sent = b''

    def write(self, data):
        self.sent += bytes(data)

    async def drain(self):
        return


class _MockSess:
    combat_mission = 0
    game_mission = 0


def _varint(field, value):
    out = bytearray([field << 3 | 0])
    while True:
        b = value & 0x7F
        value >>= 7
        out.append(b | 0x80 if value else b)
        if not value:
            return bytes(out)


def _msg(field, payload):
    out = bytearray([field << 3 | 2])
    ln = len(payload)
    while True:
        b = ln & 0x7F
        ln >>= 7
        out.append(b | 0x80 if ln else b)
        if not ln:
            return bytes(out) + payload


def _earnings_proto(mission_id=100003, char_id=11, time_active=900):
    """AnnounceBattleFieldEarningsRequest：单玩家单角色。"""
    # CharacterBattleResult{f1 charId, f7 timeActive}
    char_result = _varint(1, char_id) + _varint(7, time_active)
    # PlayerBattleResult{f1 playerId=1, f6 repeated char}
    pbr = _varint(1, 1) + _msg(6, char_result)
    # BattleReportCharacter{f1 charId}（is_bot 名单按 BattleReport 判定）
    br_char = _varint(1, char_id)
    # BattleReportPlayer{f1 playerId=1, f10 isBot=0, f11 repeated char}
    br_player = _varint(1, 1) + _varint(10, 0) + _msg(11, br_char)
    # BattleReport{f1 msid, f6 faction{f1 id, f2 players}}
    faction = _varint(1, 2) + _msg(2, br_player)
    report = _varint(1, 730433917100099) + _msg(6, faction)
    return (_varint(1, mission_id) + _varint(2, 730433917100099)
            + _varint(3, 10) + _msg(4, report) + _msg(5, pbr))


def test_settlement_path_applies_wear():
    """AnnounceBattleFieldEarnings + type6 用量（session 尾数=missionId）→
    按实际发数/出生数受损；重复结算不二次计损（usage 取走即删）。

    顺带验证：结算响应仍先行发出（磨损异常不得拖垮结算链路）。"""
    st, p = _build_loadout()
    old = combat_handlers.STORE
    combat_handlers.STORE = st
    _usage_cleanup()
    try:
        # type6 上报（结算前同秒到达）：session 730433917100003 → mission 100003
        statistics_server._accumulate_battle_usage(
            730433917100003,
            [(11, [(519, p['wt'], 0, 500), (519, p['vwt'], 0, p['vshots']),
                   (12, 0, 0, 3)], [])])
        writer = _MockWriter()
        asyncio.run(combat_handlers.handle_announce_battlefield_earnings(
            _MockSess(), writer, 200,
            'AnnounceBattleFieldEarningsRequest', _earnings_proto()))
        assert b'AnnounceBattleFieldEarningsResponse' in writer.sent, '响应先行'
        wp = p['wp']
        w = {x['id']: x['condition'] for x in st.weapons}
        assert w[101] == 1.0 - wp[1] * 500, \
            f'结算未按实际发数扣武器磨损: {w[101]}'
        assert st.vehicles[0]['condition'] == p['vmax'] - 3.0, \
            f'结算未按实际出生数扣载具磨损: {st.vehicles[0]["condition"]}'
        # 重复结算：usage 已被取走 → 不再计损
        asyncio.run(combat_handlers.handle_announce_battlefield_earnings(
            _MockSess(), _MockWriter(), 201,
            'AnnounceBattleFieldEarningsRequest', _earnings_proto()))
        w2 = {x['id']: x['condition'] for x in st.weapons}
        assert w2[101] == 1.0 - wp[1] * 500, '重复结算不应二次计损'
        assert st.vehicles[0]['condition'] == p['vmax'] - 3.0, \
            '重复结算不应二次计损（载具）'
    finally:
        combat_handlers.STORE = old
        _usage_cleanup()


# ---------- E. 真实 DB 集成（事务内回滚）----------

class _TxFetch:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


class _TxPool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _TxFetch(self.conn)


def test_db_normalize_create_and_wear():
    """真实库事务（ROLLBACK 收尾）：
    1) _normalize_conditions：condition=100.0 存量载具 → 模板满耐久（幂等）
    2) create_vehicle 缺省 → 满耐久入库
    3) _apply_equipment_wear：按 type6 实际用量（发数/出生数）扣减
    DB 不可达时打印 SKIP 并通过（本地无 Docker 环境不阻塞回归）。"""
    import re
    import asyncpg
    import store

    dsn = store.DEFAULT_DSN
    ip = store._wsl_docker_ip()
    if ip:
        dsn = re.sub(r'@(127\.0\.0\.1|localhost):', f'@{ip}:', dsn)

    async def _run():
        # 连接与事务必须在同一个 event loop 内创建/使用
        try:
            conn = await asyncpg.connect(dsn)
        except Exception as e:
            print(f'  SKIP  真实 DB 不可达（{e}）')
            return
        st = PlayerStore.__new__(PlayerStore)
        st.pool = _TxPool(conn)
        try:
            await conn.execute('BEGIN')

            # 找一个已装备武器（可磨损）+ 已装备载具的角色
            row = await conn.fetchrow(
                "SELECT c.id AS cid, c.player_id AS pid FROM characters c "
                "WHERE NOT c.is_deleted AND EXISTS ("
                "  SELECT 1 FROM weapons w WHERE w.character_id = c.id "
                "  AND w.container_id = c.id AND w.container_slot > 0) LIMIT 1")
            if row is None:
                print('  SKIP  库中无已装备武器的角色')
                return
            pid, cid = int(row['pid']), int(row['cid'])

            # 1) 迁移归一化：把已装备载具打成 100.0（旧默认值脏数据）
            vrow = await conn.fetchrow(
                'SELECT id, vehicle_variant_template_id FROM vehicles '
                'WHERE player_id = $1 AND character_id = $2 '
                'AND equipped_in_slot > 0 LIMIT 1', pid, cid)
            if vrow is not None:
                full = wardb.vehicle_full_condition(
                    vrow['vehicle_variant_template_id'])
                await conn.execute(
                    'UPDATE vehicles SET condition = 100.0 WHERE id = $1',
                    vrow['id'])
                await PlayerStore._normalize_conditions(st, conn)
                got = float(await conn.fetchval(
                    'SELECT condition FROM vehicles WHERE id = $1', vrow['id']))
                assert got == full, f'迁移后应为满耐久 {full}，实际 {got}'
                # 幂等：再跑一遍值不变
                await PlayerStore._normalize_conditions(st, conn)
                got2 = float(await conn.fetchval(
                    'SELECT condition FROM vehicles WHERE id = $1', vrow['id']))
                assert got2 == full, f'迁移不幂等: {got2} != {full}'
                print(f'  PASS  迁移归一化（vehicle {vrow["id"]} 100.0 -> {full}，幂等）')

            # 2) create_vehicle 满耐久
            vvt = _variant_for_parent_max(25.0)
            v = await st.create_vehicle(pid, vvt, cid)
            assert float(v['condition']) == 25.0, v['condition']
            print(f'  PASS  create_vehicle 满耐久（variant {vvt} -> 25.0）')

            # 3) 战后磨损（装备重置为满耐久后按实际用量断言精确扣减）
            wrow = await conn.fetchrow(
                'SELECT w.id, w.template_id, w.container_slot FROM weapons w '
                'WHERE w.player_id = $1 AND w.character_id = $2 '
                'AND w.container_id = $2 AND w.container_slot > 0 '
                'ORDER BY w.container_slot LIMIT 1', pid, cid)
            assert wrow is not None, '前置条件丢失（已装备武器）'
            await conn.execute(
                'UPDATE weapons SET condition = 1.0, auto_repair = 0 '
                'WHERE id = $1', wrow['id'])
            if vrow is not None:
                await conn.execute(
                    'UPDATE vehicles SET condition = $2, auto_repair = 0 '
                    'WHERE id = $1',
                    vrow['id'],
                    wardb.vehicle_full_condition(
                        vrow['vehicle_variant_template_id']))
            usage = {cid: {'shots': {int(wrow['template_id']): 500},
                           'spawns': 2}}
            old = combat_handlers.STORE
            combat_handlers.STORE = st
            try:
                await combat_handlers._apply_equipment_wear(
                    {(pid, cid): 900}, usage)
            finally:
                combat_handlers.STORE = old
            wcond = float(await conn.fetchval(
                'SELECT condition FROM weapons WHERE id = $1', wrow['id']))
            wp = wardb.weapon_wear_params(wrow['template_id'])
            expect = (1.0 - wp[1] * 500) if wp and (wp[1] or wp[2]) else 1.0
            assert abs(wcond - expect) < 1e-9, \
                f'武器磨损 {wcond} != 预期 {expect}（模板 {wrow["template_id"]}）'
            print(f'  PASS  战后武器磨损（{wrow["id"]}: 1.0 -> {wcond:.6f}，500 发）')
            if vrow is not None:
                vcond = float(await conn.fetchval(
                    'SELECT condition FROM vehicles WHERE id = $1', vrow['id']))
                vfull = wardb.vehicle_full_condition(
                    vrow['vehicle_variant_template_id'])
                assert vcond == vfull - 2.0, \
                    f'载具磨损 {vcond} != 预期 {vfull - 2.0}'
                print(f'  PASS  战后载具磨损（{vrow["id"]}: {vfull} -> {vcond}，2 次出生）')
        finally:
            try:
                await conn.execute('ROLLBACK')
            finally:
                await conn.close()

    asyncio.run(_run())


if __name__ == '__main__':
    tests = [
        test_wardb_vehicle_wear_table,
        test_wardb_vehicle_full_condition,
        test_wardb_weapon_and_mod_wear_tables,
        test_wardb_ammo_wear_table,
        test_wardb_vehicle_ammo_weapon_templates,
        test_battle_usage_accumulates_and_links_by_mission_suffix,
        test_battle_usage_dual_form_dedup,
        test_battle_usage_fifo_eviction_and_bad_sid,
        test_type6_accumulates_before_persist,
        test_wait_battle_usage_semantics,
        test_apply_equipment_wear_math_and_skips,
        test_apply_equipment_wear_zero_usage_never_charges,
        test_apply_equipment_wear_missing_usage_skips,
        test_apply_equipment_wear_zero_active_skipped,
        test_apply_equipment_wear_worn_not_negative,
        test_autorepair_credits_charges_and_restores,
        test_autorepair_insufficient_funds_keeps_worn,
        test_autorepair_missing_pricing_repairs_free,
        test_autorepair_vehicle_gold,
        test_autorepair_ammo_refill,
        test_settlement_path_applies_wear,
        test_db_normalize_create_and_wear,
    ]
    for t in tests:
        t()
    print(f'test_equipment_wear: {len(tests)} 项全部通过')
