# -*- coding: utf-8 -*-
"""装备维护损耗（condition 磨损）回归测试。

覆盖 2026-08-31 两项修复：
1. 对局结算无维护消耗 → combat._apply_equipment_wear 按 timeActive 估算扣减
   （武器/配件按发、载具/升级按 spawn、弹药按发数）
2. 刚买回来就有一点损耗 → condition 量纲错乱修复：
   - 载具 maxCondition 为点数（25~220），创建/迁移按模板满耐久，不再默认 100.0
   - 武器/配件量纲 0~1，推送 fallback 不再是 100.0

分五部分：
A. wardb 磨损静态表（纯单测，无需 DB）
B. _est_shots/_est_spawns 估算钳制（纯单测）
C. _apply_equipment_wear 磨损数学 + 跳过规则（内存 FakeStore）
D. 完整结算链路触达磨损（handle_announce_battlefield_earnings + FakeStore）
E. 真实 DB 集成（事务内回滚，不改库）：迁移归一化 / create_vehicle / 磨损扣减
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wardb
from handlers import combat as combat_handlers
from player_store import PlayerStore

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


def _weapon_template(wear=True):
    """wear=True 取一个可磨损武器模板，否则取磨损率全 0 的模板。"""
    for t, p in wardb._WEAPON_WEAR.items():
        if bool(p[1] or p[2]) == wear:
            return t
    raise AssertionError('wardb 武器表为空')


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


# ---------- B. 估算钳制 ----------

def test_est_helpers_clamped():
    assert combat_handlers._est_shots(1) == 100          # 下限钳制
    assert combat_handlers._est_shots(100) == 100        # 100s*0.75=75 → 钳 100
    assert combat_handlers._est_shots(900) == 675
    assert combat_handlers._est_shots(10 ** 9) == 2000   # 上限钳制
    assert combat_handlers._est_spawns(1) == 1           # 下限钳制
    assert combat_handlers._est_spawns(900) == 3
    assert combat_handlers._est_spawns(10 ** 9) == 6     # 上限钳制


# ---------- C. 磨损数学与跳过规则（内存 FakeStore）----------

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

    # ---- 结算入库（D 部分用）----
    async def save_battle_report(self, report, players):
        return 1

    async def add_currency(self, player_id, credits=0, warfunds=0):
        return {'credits': credits, 'warfunds': warfunds}

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
    """构造 pid=1 char=11 的装备快照（est 900s：shots=675 spawns=3）。

    wA 主武器（可磨损，slot1）      → 全额定损
    wB 副武器（同模板，slot2）      → 30% 定损
    wC 不可维护武器（磨损率全 0）   → 不扣
    wD 已损坏（condition=0）        → 不扣（不为负）
    wE 仓库武器（container_slot=0） → 不扣
    mA wA 上的配件                  → 全额定损
    mE wE 上的配件                  → 不扣
    aA wA 的在膛弹药（slot 1）      → 按发扣
    aR wA 的储备弹药（slot 0）      → 不扣
    vA 已装备载具（60 点）          → 60-3=57
    vR 仓库载具（slot 0）           → 不扣
    uA vA 槽位 5 上的升级           → 100-3
    uR vA 上未装备的升级（slot 0）  → 不扣
    va vA 弹药仓 2 的载具弹药       → 按发扣
    """
    st = _WearStore()
    wt = _weapon_template(wear=True)              # 可磨损武器模板
    wt0 = _weapon_template(wear=False)            # 磨损率全 0 模板
    wp = wardb.weapon_wear_params(wt)
    mt = next(t for t, p in wardb._WEAPON_MOD_WEAR.items() if p[1])
    mp = wardb.weapon_modifier_wear_params(mt)
    at = next(t for t, p in wardb._AMMO_WEAR.items() if p[1] == 1.0)
    vt = _variant_for_parent_max(60.0)
    ut = next(iter(wardb._VEHICLE_UPGRADE_WEAR))
    st.weapons = [
        {'id': 101, 'player_id': 1, 'template_id': wt, 'character_id': 11,
         'container_id': 11, 'container_slot': 1, 'condition': 1.0},
        {'id': 102, 'player_id': 1, 'template_id': wt, 'character_id': 11,
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
        {'id': 401, 'player_id': 1, 'vehicle_variant_template_id': vt,
         'character_id': 11, 'equipped_in_slot': 1, 'condition': 60.0},
        {'id': 402, 'player_id': 1, 'vehicle_variant_template_id': vt,
         'character_id': 11, 'equipped_in_slot': 0, 'condition': 60.0},
    ]
    st.vehicle_upgrades = [
        {'id': 501, 'player_id': 1, 'upgrade_template_id': ut, 'vehicle_id': 401,
         'character_id': 11, 'equipped_slot_id': 5, 'condition': 100.0},
        {'id': 502, 'player_id': 1, 'upgrade_template_id': ut, 'vehicle_id': 401,
         'character_id': 11, 'equipped_slot_id': 0, 'condition': 100.0},
    ]
    st.vehicle_ammo = [
        {'id': 601, 'player_id': 1, 'ammo_template_id': at, 'vehicle_id': 401,
         'character_id': 11, 'equipped_storage_id': 2, 'condition': 1323.0},
    ]
    return st, {'wp': wp, 'mp': mp}


def test_apply_equipment_wear_math_and_skips():
    st, p = _build_loadout()
    old = combat_handlers.STORE
    combat_handlers.STORE = st
    try:
        asyncio.run(combat_handlers._apply_equipment_wear({(1, 11): 900}))
    finally:
        combat_handlers.STORE = old
    w = {x['id']: x['condition'] for x in st.weapons}
    m = {x['id']: x['condition'] for x in st.modifiers}
    a = {x['id']: x['condition'] for x in st.weapon_ammo}
    v = {x['id']: x['condition'] for x in st.vehicles}
    u = {x['id']: x['condition'] for x in st.vehicle_upgrades}
    va = {x['id']: x['condition'] for x in st.vehicle_ammo}
    wp, mp = p['wp'], p['mp']
    # 主武器全额定损 / 副武器 30%
    assert w[101] == 1.0 - wp[1] * 675, w[101]
    assert w[102] == 1.0 - wp[1] * int(675 * 0.3), w[102]
    # 不可维护 / 已损坏 / 未装备 → 不扣
    assert w[103] == 1.0 and w[104] == 0.0 and w[105] == 1.0
    # 配件随已装备武器；挂仓库武器不扣
    assert m[201] == 1.0 - mp[1] * 675, m[201]
    assert m[202] == 1.0
    # 在膛弹药按发扣（2000 发扣 675）；储备不扣
    assert a[301] == 2000.0 - 675, a[301]
    assert a[302] == 500.0
    # 载具按 spawn：60 - 1.0*3；仓库载具不扣
    assert v[401] == 57.0, v[401]
    assert v[402] == 60.0
    # 升级同载具规则；未入槽不扣
    assert u[501] == 97.0, u[501]
    assert u[502] == 100.0
    # 载具弹药按发扣
    assert va[601] == 1323.0 - 675, va[601]


def test_apply_equipment_wear_zero_active_skipped():
    """timeActive=0（未参战）角色整体跳过，任何装备不扣。"""
    st, _ = _build_loadout()
    old = combat_handlers.STORE
    combat_handlers.STORE = st
    try:
        asyncio.run(combat_handlers._apply_equipment_wear({(1, 11): 0}))
    finally:
        combat_handlers.STORE = old
    assert all(x['condition'] == 1.0 or x['condition'] == 0.0
               for x in st.weapons), '未参战不应扣武器'
    assert st.vehicles[0]['condition'] == 60.0, '未参战不应扣载具'


def test_apply_equipment_wear_worn_not_negative():
    """低耐久物品扣减后钳到 0，不为负。"""
    st, _ = _build_loadout()
    st.weapons[0]['condition'] = 0.001          # 剩余耐久不足一局定损
    st.vehicles[0]['condition'] = 2.0           # 60 点车仅剩 2 点
    old = combat_handlers.STORE
    combat_handlers.STORE = st
    try:
        asyncio.run(combat_handlers._apply_equipment_wear({(1, 11): 1800}))
    finally:
        combat_handlers.STORE = old
    assert st.weapons[0]['condition'] == 0.0
    assert st.vehicles[0]['condition'] == 0.0


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


def test_settlement_path_applies_wear():
    """AnnounceBattleFieldEarningsRequest f7 timeActive=900 → 装备受损。

    顺带验证：结算响应仍先行发出（磨损异常不得拖垮结算链路）。"""
    # CharacterBattleResult{f1 charId=11, f7 timeActive=900}
    char_result = _varint(1, 11) + _varint(7, 900)
    # PlayerBattleResult{f1 playerId=1, f6 repeated char}
    pbr = _varint(1, 1) + _msg(6, char_result)
    # BattleReportCharacter{f1 charId=11}（is_bot 名单按 BattleReport 判定）
    br_char = _varint(1, 11)
    # BattleReportPlayer{f1 playerId=1, f10 isBot=0, f11 repeated char}
    br_player = _varint(1, 1) + _varint(10, 0) + _msg(11, br_char)
    # BattleReport{f1 msid, f6 faction{f1 id, f2 players}}
    faction = _varint(1, 2) + _msg(2, br_player)
    report = _varint(1, 730433917100099) + _msg(6, faction)
    proto = (_varint(1, 100003) + _varint(2, 730433917100099)
             + _varint(3, 10) + _msg(4, report) + _msg(5, pbr))

    st, p = _build_loadout()
    old = combat_handlers.STORE
    combat_handlers.STORE = st
    try:
        writer = _MockWriter()
        asyncio.run(combat_handlers.handle_announce_battlefield_earnings(
            _MockSess(), writer, 200,
            'AnnounceBattleFieldEarningsRequest', proto))
    finally:
        combat_handlers.STORE = old
    assert b'AnnounceBattleFieldEarningsResponse' in writer.sent, '响应先行'
    wp = p['wp']
    w = {x['id']: x['condition'] for x in st.weapons}
    assert w[101] == 1.0 - wp[1] * 675, f'结算未扣武器磨损: {w[101]}'
    assert st.vehicles[0]['condition'] == 57.0, \
        f'结算未扣载具磨损: {st.vehicles[0]["condition"]}'


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
    3) _apply_equipment_wear：已装备武器/载具按 timeActive 扣减
    DB 不可达时打印 SKIP 并通过（本地无 Docker 环境不阻塞回归）。"""
    import re
    import asyncpg
    import player_store

    dsn = player_store.DEFAULT_DSN
    ip = player_store._wsl_docker_ip()
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

            # 3) 战后磨损（装备重置为满耐久后断言精确扣减）
            wrow = await conn.fetchrow(
                'SELECT w.id, w.template_id, w.container_slot FROM weapons w '
                'WHERE w.player_id = $1 AND w.character_id = $2 '
                'AND w.container_id = $2 AND w.container_slot > 0 '
                'ORDER BY w.container_slot LIMIT 1', pid, cid)
            assert wrow is not None, '前置条件丢失（已装备武器）'
            await conn.execute(
                'UPDATE weapons SET condition = 1.0 WHERE id = $1', wrow['id'])
            if vrow is not None:
                await conn.execute(
                    'UPDATE vehicles SET condition = $2 WHERE id = $1',
                    vrow['id'],
                    wardb.vehicle_full_condition(
                        vrow['vehicle_variant_template_id']))
            old = combat_handlers.STORE
            combat_handlers.STORE = st
            try:
                await combat_handlers._apply_equipment_wear({(pid, cid): 900})
            finally:
                combat_handlers.STORE = old
            wcond = float(await conn.fetchval(
                'SELECT condition FROM weapons WHERE id = $1', wrow['id']))
            wp = wardb.weapon_wear_params(wrow['template_id'])
            shots = 675 if int(wrow['container_slot']) == 1 else int(675 * 0.3)
            expect = (1.0 - wp[1] * shots) if wp and (wp[1] or wp[2]) else 1.0
            assert abs(wcond - expect) < 1e-9, \
                f'武器磨损 {wcond} != 预期 {expect}（模板 {wrow["template_id"]}）'
            print(f'  PASS  战后武器磨损（{wrow["id"]}: 1.0 -> {wcond:.6f}）')
            if vrow is not None:
                vcond = float(await conn.fetchval(
                    'SELECT condition FROM vehicles WHERE id = $1', vrow['id']))
                vfull = wardb.vehicle_full_condition(
                    vrow['vehicle_variant_template_id'])
                assert vcond == vfull - 3.0, \
                    f'载具磨损 {vcond} != 预期 {vfull - 3.0}'
                print(f'  PASS  战后载具磨损（{vrow["id"]}: {vfull} -> {vcond}）')
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
        test_est_helpers_clamped,
        test_apply_equipment_wear_math_and_skips,
        test_apply_equipment_wear_zero_active_skipped,
        test_apply_equipment_wear_worn_not_negative,
        test_settlement_path_applies_wear,
        test_db_normalize_create_and_wear,
    ]
    for t in tests:
        t()
    print(f'test_equipment_wear: {len(tests)} 项全部通过')
