# -*- coding: utf-8 -*-
"""生涯军令（CareerOrder）触发链回归测试（2026-09-02 "职业晋升任务不触发"）。

背景：46 种 ECareerOrderType 里客户端 UI 只上报 5 种（4/8/9/10/11）、
hng.exe 上报 2 种（22/25），其余全靠服务端推导。本次修复：
 1. revalidate_state_orders 扩展装备状态推导：18 装备SMG / 24 装备载具 /
    28 装备反坦克武器 / 30 武器配件 / 31 载具升级 / 32 载具自定义弹药 /
    35 士兵外观 / 36 载具外观（weapons.container_id + weapontemplate f57
    类别 16=SMG 8=AT、vehicles.equipped_in_slot/variant_id、
    weapon_modifiers.weapon_id、vehicle_upgrades.equipped_slot_id、
    vehicle_ammo.equipped_storage_id）；
 2. complete_order_and_push：动作钩子统一入口 = complete_order_once 入库
    + push_career_order_row 推 KVCS（此前只入库不推 → 玩家要重登才能看到
    进度，"做了任务没反应"的根因）。

用例：
 A. weapon_template_category（真实 WarDatabase：PPSh-41→16、Mosin→12、
    AT 表非空、未知模板→0）
 B. revalidate_state_orders：空玩家 / SMG·AT·步枪装备与未装备 / 载具
    装备与涂装 / 配件·升级·弹药 / 士兵迷彩·头盔 / 幂等 / 已完成不重记 /
    list_vehicles 抛错容错
 C. complete_order_and_push：oid 入库+推行（47 槽不变量）/ oid=None 状态
    推导 / pid 空 no-op / playerview 未订阅只入库 / 幂等
 D. handle_setvehiclevariantrequest 集成：装涂装→36 完成+CareerOrder 行；
    卸涂装（variantId=0）不触发；非持有皮肤被拒且不触发
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol import Chunk, pb_varint
import handlers.career as career_mod
from handlers.career import (
    complete_order_and_push,
    revalidate_state_orders,
    )
import handlers.vehicles as vehicles_mod
from handlers.vehicles import handle_setvehiclevariantrequest
from state import STORE as GLOBAL_STORE
import wardb

RESULTS = []


def check(label, ok, detail=''):
    RESULTS.append((label, ok))
    print(f'[{"PASS" if ok else "FAIL"}] {label}{("  " + detail) if detail else ""}')


# ------------------------------------------------------------- 测试基建

class FakeWriter:
    def __init__(self):
        self.data = b''

    def write(self, data):
        self.data += data

    async def drain(self):
        pass


class FakeSess:
    def __init__(self, pid, playerview_cmd=55, soldier_view_cmd=56):
        self.pid = pid
        self.playerview_cmd = playerview_cmd
        self.soldier_view_cmd = soldier_view_cmd

    async def ensure_player(self):
        return self.pid


class FakeStore:
    """最小 STORE：career 进度 + 装备列表（revalidate / 钩子路径用到的方法）。"""

    def __init__(self):
        self.chars = {}            # cid -> {'id','player_id','template','faction',
        #                              'combat_xp','uniform_variant_id','headgear_id'}
        self.weapons = {}          # wid -> {'id','template_id','character_id',
        #                              'container_id'}
        self.vehicles = {}         # vid -> {'id','vehicle_variant_template_id',
        #                              'character_id','equipped_in_slot','variant_id'}
        self.vehicle_variants = {}  # vvid -> {'id','vehicle_id'}
        self.weapon_modifiers = []  # [{'id','weapon_id'}]
        self.vehicle_upgrades = []  # [{'id','equipped_slot_id'}]
        self.vehicle_ammo = []      # [{'id','equipped_storage_id','slot_index'}]
        self.career = {}            # oid -> (progress, status)
        self.upserts = []           # [(pid, oid)] 调用记录
        self.fail_list_vehicles = False

    # ---- career ----
    async def list_career_orders(self, pid):
        return dict(self.career)

    async def upsert_career_order_progress(self, pid, oid, delta=1):
        self.upserts.append((int(pid), int(oid)))
        p, s = self.career.get(int(oid), (0, 0))
        p += int(delta)
        if p >= 1 and s == 0:
            s = 1
        self.career[int(oid)] = (p, s)
        return p, s

    # ---- revalidate 依赖 ----
    async def list_characters(self, pid):
        return [dict(c) for c in self.chars.values()
                if int(c['player_id']) == int(pid)]

    async def list_weapons(self, pid):
        return [dict(w) for w in self.weapons.values()]

    async def list_vehicles(self, pid):
        if self.fail_list_vehicles:
            raise RuntimeError('boom')
        return [dict(v) for v in self.vehicles.values()]

    async def list_weapon_modifiers(self, pid):
        return [dict(m) for m in self.weapon_modifiers]

    async def list_vehicle_upgrades(self, pid):
        return [dict(u) for u in self.vehicle_upgrades]

    async def list_vehicle_ammo(self, pid):
        return [dict(a) for a in self.vehicle_ammo]

    async def get_ribbon_xp(self, pid, cid):
        return {}

    async def get_stats(self, cid):
        return {}

    async def get_total_score(self, pid):
        return 0

    # ---- vehicles 皮肤 handler 依赖 ----
    async def list_vehicle_variants(self, pid):
        return [dict(vv) for vv in self.vehicle_variants.values()]

    async def set_vehicle_variant(self, pid, vid, variant_id):
        v = self.vehicles.get(int(vid))
        if v is not None:
            v['variant_id'] = int(variant_id)

    async def get_vehicle(self, pid, vid):
        v = self.vehicles.get(int(vid))
        return dict(v) if v else None


def _kvcs_rows(data, table):
    """从 writer 累积字节里提取指定表的 KVCS set 行 proto 列表。"""
    rows = []
    off = 0
    while off < len(data):
        main, off = Chunk.parse(data, off)
        if main is None or not main.subs:
            break
        title = main.subs[0]
        if title.data != b'KeyValueChangeSet':
            continue
        for action in title.subs:
            if action.data != b'set':
                continue
            for tbl in action.subs:
                if tbl.data == table and tbl.subs:
                    rows.append(tbl.subs[0].data)
    return rows


def _rd_varint(buf, off):
    val = shift = 0
    while True:
        b = buf[off]
        off += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, off
        shift += 7


def _pb_fields(data):
    """{field: [value,...]}（wt0 varint / wt2 bytes）。"""
    out = {}
    i, n = 0, len(data)
    while i < n:
        key, i = _rd_varint(data, i)
        fn, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _rd_varint(data, i)
        elif wt == 2:
            ln, i = _rd_varint(data, i)
            v = data[i:i + ln]
            i += ln
        else:
            raise ValueError(f'wiretype {wt}')
        out.setdefault(fn, []).append(v)
    return out


def _career_orders_done(store):
    """已入库完成的 oid 集合。"""
    return {oid for oid, (p, _s) in store.career.items() if p >= 1}


def _resp_field(data, req_type, field):
    """从 writer 字节里找指定响应块并取字段值（varint）。"""
    off = 0
    while off < len(data):
        main, off = Chunk.parse(data, off)
        if main is None or not main.subs:
            break
        t = main.subs[0]
        if t.data == req_type.encode() and t.subs:
            f = _pb_fields(t.subs[0].data)
            return f.get(field, [None])[0]
    return None


# ------------------------------------------------------------- 用例

async def main():
    # ============ A. weapon_template_category（真实 WarDatabase） ============
    print('--- A. weapon_template_category ---')
    check('PPSh-41(template 5) → 16 Submachinegun',
          wardb.weapon_template_category(5) == 16,
          f'got {wardb.weapon_template_category(5)}')
    check('Mosin-Nagant(template 12) → 12 栓动步枪（非 SMG/AT）',
          wardb.weapon_template_category(12) == 12,
          f'got {wardb.weapon_template_category(12)}')
    check('Bazooka M1A1(template 52) → 8 反坦克',
          wardb.weapon_template_category(52) == 8,
          f'got {wardb.weapon_template_category(52)}')
    at_ids = [int(f.get(1, 0) or 0) for f in wardb._parse_wardb_table('weapontemplate')
              if int(f.get(57, 0) or 0) == 8]
    check('WarDatabase 存在反坦克武器（category=8）', len(at_ids) > 0,
          f'{len(at_ids)} AT templates')
    if at_ids:
        check('AT 模板映射返回 8（取第一个）',
              wardb.weapon_template_category(at_ids[0]) == 8,
              f'template {at_ids[0]}')
    check('未知模板 → 0', wardb.weapon_template_category(999999) == 0)

    # ============ B. revalidate_state_orders 装备状态推导 ============
    print('--- B. revalidate_state_orders ---')

    def base_store():
        st = FakeStore()
        st.chars[31] = {'id': 31, 'player_id': 7, 'template': 1, 'faction': 2,
                        'combat_xp': 100, 'uniform_variant_id': 0,
                        'headgear_id': 0}
        return st

    old_store = career_mod.STORE

    async def _reval(st):
        """挂上 FakeStore 跑一次状态推导（B 段统一入口）。"""
        career_mod.STORE = st
        await revalidate_state_orders(7)
        career_mod.STORE = old_store

    try:
        # B1 空玩家（1 步兵无武器）→ 无任务完成
        st = base_store()
        await _reval(st)
        check('空玩家：无任务完成', _career_orders_done(st) == set(),
              f'got {sorted(_career_orders_done(st))}')

        # B2 装备 SMG → 18；军械库未装备 → 不完成
        st = base_store()
        st.weapons[118] = {'id': 118, 'template_id': 5, 'character_id': 31,
                           'container_id': 0}
        await _reval(st)
        check('SMG 未装备（container_id=0）：18 不完成',
              18 not in _career_orders_done(st))
        st.weapons[118]['container_id'] = 1
        await _reval(st)
        done = _career_orders_done(st)
        check('SMG 已装备：18 完成', 18 in done, f'got {sorted(done)}')
        check('SMG 已装备：28 不误完成', 28 not in done)

        # B3 装备 AT 武器 → 28；步枪装备 → 两者皆不
        st = base_store()
        st.weapons[201] = {'id': 201, 'template_id': at_ids[0],
                           'character_id': 31, 'container_id': 1}
        await _reval(st)
        check('AT 已装备：28 完成', 28 in _career_orders_done(st))
        st = base_store()
        st.weapons[119] = {'id': 119, 'template_id': 12, 'character_id': 31,
                           'container_id': 1}
        await _reval(st)
        done = _career_orders_done(st)
        check('步枪(Mosin t12)已装备：18/28 均不完成',
              18 not in done and 28 not in done, f'got {sorted(done)}')

        # B4 载具装备 → 24；涂装 → 36
        st = base_store()
        st.vehicles[409] = {'id': 409, 'vehicle_variant_template_id': 7,
                            'character_id': 31, 'equipped_in_slot': 0,
                            'variant_id': 0}
        await _reval(st)
        check('载具未装备无涂装：24/36 不完成',
              24 not in _career_orders_done(st) and
              36 not in _career_orders_done(st))
        st.vehicles[409]['equipped_in_slot'] = 2
        st.vehicles[409]['variant_id'] = 77
        await _reval(st)
        done = _career_orders_done(st)
        check('载具已装备+涂装：24/36 完成', 24 in done and 36 in done,
              f'got {sorted(done)}')

        # B5 配件/升级/弹药 → 30/31/32
        st = base_store()
        st.weapon_modifiers = [{'id': 501, 'weapon_id': 0}]
        st.vehicle_upgrades = [{'id': 601, 'equipped_slot_id': 0}]
        st.vehicle_ammo = [{'id': 701, 'equipped_storage_id': 0,
                            'slot_index': 0}]
        await _reval(st)
        check('配件/升级/弹药均未挂接：30/31/32 不完成',
              not ({30, 31, 32} & _career_orders_done(st)))
        st.weapon_modifiers[0]['weapon_id'] = 118
        st.vehicle_upgrades[0]['equipped_slot_id'] = 3
        st.vehicle_ammo[0]['equipped_storage_id'] = 5
        await _reval(st)
        done = _career_orders_done(st)
        check('配件挂接+升级装备+弹药装备：30/31/32 完成',
              {30, 31, 32} <= done, f'got {sorted(done)}')

        # B6 士兵外观：迷彩 / 头盔
        st = base_store()
        st.chars[31]['uniform_variant_id'] = 9
        await _reval(st)
        check('非默认迷彩：35 完成', 35 in _career_orders_done(st))
        st = base_store()
        st.chars[31]['headgear_id'] = 4
        await _reval(st)
        check('非默认头盔：35 完成', 35 in _career_orders_done(st))

        # B7 幂等：重复推导不重复入库
        st = base_store()
        st.weapons[118] = {'id': 118, 'template_id': 5, 'character_id': 31,
                           'container_id': 1}
        await _reval(st)
        n1 = len(st.upserts)
        await _reval(st)
        check('幂等：第二次推导无新入库', len(st.upserts) == n1,
              f'{n1} -> {len(st.upserts)}')

        # B8 已完成任务（progress>=1）不再累加
        st = base_store()
        st.weapons[118] = {'id': 118, 'template_id': 5, 'character_id': 31,
                           'container_id': 1}
        st.career[18] = (1, 1)
        await _reval(st)
        check('已完成(18,1)不重复入库', (7, 18) not in st.upserts)
        check('已完成任务 progress 保持 1', st.career[18][0] == 1)

        # B9 list_vehicles 抛错 → 武器侧推导照常、不崩
        st = base_store()
        st.fail_list_vehicles = True
        st.weapons[118] = {'id': 118, 'template_id': 5, 'character_id': 31,
                           'container_id': 1}
        await _reval(st)
        check('list_vehicles 抛错：18 照常完成且不崩',
              18 in _career_orders_done(st))

        # ============ C. complete_order_and_push ============
        print('--- C. complete_order_and_push ---')

        # C1 oid 入库 + KVCS 推行（47 槽不变量）
        st = base_store()
        w = FakeWriter()
        career_mod.STORE = st
        await complete_order_and_push(FakeSess(7), w, 7, 27)
        check('oid=27：入库一次', st.upserts.count((7, 27)) == 1,
              f'got {st.upserts}')
        rows = _kvcs_rows(w.data, b'CareerOrder')
        check('oid=27：CareerOrder KVCS 行已推', len(rows) == 1)
        if rows:
            f = _pb_fields(rows[0])
            prog = f[3][0].decode().split(',')
            check('CareerOrder 行：47 个进度槽（客户端越界防线）',
                  len(prog) == 47, f'got {len(prog)}')
            check('CareerOrder 行：progress[27]==1', prog[27] == '1')
            check('CareerOrder 行：rewardReceivedBits 47 个',
                  len(f.get(4, [])) == 47)
            check('CareerOrder 行：id/playerId = pid',
                  f[1][0] == 7 and f[2][0] == 7)

        # C2 幂等：再次完成同 oid 不重复累加
        await complete_order_and_push(FakeSess(7), FakeWriter(), 7, 27)
        check('幂等：再次调用不重复入库 27',
              st.upserts.count((7, 27)) == 1 and st.career[27][0] == 1)

        # C3 oid=None → 纯状态推导 + 推行
        st = base_store()
        st.weapons[118] = {'id': 118, 'template_id': 5, 'character_id': 31,
                           'container_id': 1}
        w = FakeWriter()
        career_mod.STORE = st
        await complete_order_and_push(FakeSess(7), w, 7)
        check('oid=None：装备 SMG 状态推导完成 18', 18 in _career_orders_done(st))
        rows = _kvcs_rows(w.data, b'CareerOrder')
        check('oid=None：CareerOrder 行已推', len(rows) == 1 and
              _pb_fields(rows[0])[3][0].decode().split(',')[18] == '1')

        # C4 pid 空 → no-op
        st = base_store()
        w = FakeWriter()
        career_mod.STORE = st
        await complete_order_and_push(FakeSess(0), w, 0, 27)
        check('pid=0：no-op（无入库无推送）',
              not st.upserts and w.data == b'')

        # C5 playerview 未订阅 → 只入库不推
        st = base_store()
        w = FakeWriter()
        career_mod.STORE = st
        await complete_order_and_push(FakeSess(7, playerview_cmd=None), w, 7, 26)
        check('playerview 未订阅：26 仍入库', st.upserts == [(7, 26)])
        check('playerview 未订阅：无 KVCS 字节', w.data == b'')

        # ============ D. handle_setvehiclevariantrequest 集成 ============
        print('--- D. 载具涂装 handler 钩子（36） ---')

        async def _variant_call(st, vehicle_id, variant_id):
            w2 = FakeWriter()
            old_v = vehicles_mod.STORE
            old_c = career_mod.STORE
            vehicles_mod.STORE = st
            career_mod.STORE = st
            try:
                proto = pb_varint(1, vehicle_id) + pb_varint(2, variant_id)
                await handle_setvehiclevariantrequest(
                    FakeSess(7), w2, 30, 'SetVehicleVariantRequest', proto)
            finally:
                vehicles_mod.STORE = old_v
                career_mod.STORE = old_c
            return w2

        # D1 装涂装（持有）→ response=1 + 36 完成 + CareerOrder 行
        st = base_store()
        st.vehicles[409] = {'id': 409, 'vehicle_variant_template_id': 7,
                            'character_id': 31, 'equipped_in_slot': 2,
                            'variant_id': 0}
        st.vehicle_variants[77] = {'id': 77, 'vehicle_id': 409}
        w2 = await _variant_call(st, 409, 77)
        check('装涂装：response=1（字段号 2）',
              _resp_field(w2.data, 'SetVehicleVariantResponse', 2) == 1)
        check('装涂装：vehicles.variant_id 已持久化',
              st.vehicles[409]['variant_id'] == 77)
        check('装涂装：36 eChangeVehicleAppearance 完成',
              36 in _career_orders_done(st), f'got {sorted(st.career)}')
        check('装涂装：CareerOrder KVCS 行已推（UI 实时刷新）',
              len(_kvcs_rows(w2.data, b'CareerOrder')) == 1)

        # D2 卸涂装（variantId=0）→ 不触发 36
        st = base_store()
        st.vehicles[409] = {'id': 409, 'vehicle_variant_template_id': 7,
                            'character_id': 31, 'equipped_in_slot': 2,
                            'variant_id': 0}
        w2 = await _variant_call(st, 409, 0)
        check('卸涂装：response=1 但 36 不完成',
              _resp_field(w2.data, 'SetVehicleVariantResponse', 2) == 1 and
              36 not in _career_orders_done(st))

        # D3 非持有皮肤 → 拒绝且不触发
        st = base_store()
        st.vehicles[409] = {'id': 409, 'vehicle_variant_template_id': 7,
                            'character_id': 31, 'equipped_in_slot': 2,
                            'variant_id': 0}
        st.vehicle_variants[77] = {'id': 77, 'vehicle_id': 409}
        w2 = await _variant_call(st, 409, 999)
        check('非持有皮肤：response=0 拒绝',
              _resp_field(w2.data, 'SetVehicleVariantResponse', 2) == 0)
        check('非持有皮肤：不持久化不推军令行',
              st.vehicles[409]['variant_id'] == 0 and
              not _career_orders_done(st) and
              not _kvcs_rows(w2.data, b'CareerOrder'))
    finally:
        career_mod.STORE = old_store
        vehicles_mod.STORE = GLOBAL_STORE

    passed = sum(1 for _, ok in RESULTS if ok)
    print(f'\n{passed}/{len(RESULTS)} checks passed')
    if passed != len(RESULTS):
        sys.exit(1)


if __name__ == '__main__':
    asyncio.run(main())

