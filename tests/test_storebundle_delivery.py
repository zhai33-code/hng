# -*- coding: utf-8 -*-
"""商城礼包购买发货（BuyStoreBundle）回归测试（2026-09-01 修复）。

事故：handle_buystorebundlerequest 只扣款并返回 result=1，礼包物品从未
入库——"钱扣了东西不到账"。修复后扣款成功即解析 WarDatabase
StoreBundle.items XML 逐项发货（入库 + SOLDIER_VIEW KVCS 推送）。

分四部分：
A. wardb 礼包数据映射（纯单测）：全部 bundle items XML 可解析、
   武器变体 / 弹药 / 配件 mapping id 全部可解析（防发货时静默跳过）
B. _deliver_store_bundle 发货逻辑（内存 FakeStore + MockWriter）：
   玩家礼包（武器+加成+会员）、角色礼包（新角色+嵌套武器/载具+装备槽）
C. 容错：单项失败不影响其余物品、未知元素跳过、未知 bundle 不抛
D. handle_buystorebundlerequest 完整链路：扣款成功→发货+result=1、
   余额不足→result=56 且不发货
"""
import asyncio
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('HNG_GAME_DIR', r'd:\HnG-test')

import wardb                                          # noqa: E402
import player_service                                 # noqa: E402
from protocol import Chunk, pb_parse                    # noqa: E402
from handlers import shop as shop_handlers             # noqa: E402
from handlers import vehicles_purchase as vp_handlers  # noqa: E402
from handlers import weapons_purchase as wp_handlers   # noqa: E402

PASS = FAIL = 0


def check(label, ok, detail=''):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f'[PASS] {label}')
    else:
        FAIL += 1
        print(f'[FAIL] {label}  {detail}')
        raise AssertionError(f'{label}: {detail}')


# ---------- 测试替身 ----------

class _MockWriter:
    def __init__(self):
        self.sent = b''

    def write(self, data):
        self.sent += bytes(data)

    async def drain(self):
        return


class _MockSess:
    def __init__(self, pid=77):
        self.pid = pid
        self.soldier_view_cmd = 12     # SOLDIER_VIEW 已订阅（发货 KVCS 推送）
        self.playerview_cmd = None     # 跳过 player 刷新（FakeStore 无余额表）

    async def ensure_player(self):
        return self.pid


class _FakeStore:
    """内存版 PlayerStore：记录全部发货调用，供断言。"""

    def __init__(self):
        self.seq = 100
        self.weapons = []
        self.weapon_variants = []
        self.weapon_ammo = []
        self.weapon_modifiers = []
        self.weapon_containers = []      # (wid, container_id, slot)
        self.vehicles = []
        self.vehicle_equips = []         # (vid, char_id, slot)
        self.vehicle_ammo = []
        self.vehicle_ammo_equips = []    # (va_id, storage_id, slot, do_equip)
        self.characters = []
        self.boosters = []
        self.memberships = []
        self.uniform_parts = []
        self.gold_added = 0
        self.spends = []
        self.spend_ok = True
        self.fail_weapon_calls = set()   # 第 n 次 create_weapon 尝试抛异常（n 从 1 起）
        self.weapon_attempts = 0

    def _id(self):
        self.seq += 1
        return self.seq

    # ---- 武器 ----
    async def create_weapon(self, player_id, name, template_id, character_id,
                            variant_template_id=0):
        self.weapon_attempts += 1
        if self.weapon_attempts in self.fail_weapon_calls:
            raise RuntimeError('boom: create_weapon')
        w = {'id': self._id(), 'player_id': player_id, 'name': name,
             'template_id': template_id, 'character_id': character_id,
             'container_id': 0, 'container_slot': 0,
             'variant_template_id': variant_template_id, 'variant_id': 0,
             'clip_count': 0, 'auto_repair': 0, 'condition': 1.0}
        self.weapons.append(w)
        return dict(w)

    async def create_weapon_variant(self, player_id, weapon_id, character_id,
                                    weapon_variant_template_id):
        v = {'id': self._id(), 'weapon_id': weapon_id,
             'character_id': character_id,
             'weapon_variant_template_id': weapon_variant_template_id}
        self.weapon_variants.append(v)
        return dict(v)

    async def set_weapon_variant(self, player_id, weapon_id, variant_id):
        for w in self.weapons:
            if w['id'] == weapon_id:
                w['variant_id'] = variant_id
        return None

    async def update_weapon_container(self, player_id, weapon_id,
                                      container_id, container_slot):
        self.weapon_containers.append((weapon_id, container_id, container_slot))
        for w in self.weapons:
            if w['id'] == weapon_id:
                w['container_id'] = container_id
                w['container_slot'] = container_slot
        return None

    async def create_weapon_ammo(self, player_id, ammo_template_id,
                                 character_id, weapon_id, slot_index=0,
                                 condition=0):
        a = {'id': self._id(), 'player_id': player_id,
             'ammo_template_id': ammo_template_id,
             'character_id': character_id, 'weapon_id': weapon_id,
             'slot_index': slot_index, 'condition': condition}
        self.weapon_ammo.append(a)
        return dict(a)

    async def create_weapon_modifier(self, player_id, template_id,
                                     character_id, weapon_id):
        m = {'id': self._id(), 'player_id': player_id,
             'template_id': template_id, 'character_id': character_id,
             'weapon_id': weapon_id, 'condition': 1.0}
        self.weapon_modifiers.append(m)
        return dict(m)

    # ---- 载具 ----
    async def create_vehicle(self, player_id, vehicle_variant_template_id,
                             character_id, condition=None):
        v = {'id': self._id(), 'player_id': player_id,
             'vehicle_variant_template_id': vehicle_variant_template_id,
             'character_id': character_id, 'equipped_in_slot': 0,
             'variant_id': 0,
             'condition': condition if condition is not None
             else wardb.vehicle_full_condition(vehicle_variant_template_id)}
        self.vehicles.append(v)
        return dict(v)

    async def set_vehicle_equipment(self, player_id, vehicle_id,
                                     character_id, equip_in_slot):
        self.vehicle_equips.append((vehicle_id, character_id, equip_in_slot))
        for v in self.vehicles:
            if v['id'] == vehicle_id:
                v['equipped_in_slot'] = equip_in_slot
        return []

    async def create_vehicle_ammo(self, player_id, ammo_template_id,
                                  vehicle_id, character_id, condition):
        a = {'id': self._id(), 'player_id': player_id,
             'ammo_template_id': ammo_template_id, 'vehicle_id': vehicle_id,
             'character_id': character_id, 'equipped_storage_id': 0,
             'slot_index': 0, 'condition': condition}
        self.vehicle_ammo.append(a)
        return dict(a)

    async def set_vehicle_ammo_equipment(self, player_id, ammo_id,
                                         storage_id, slot_index, do_equip):
        self.vehicle_ammo_equips.append(
            (ammo_id, storage_id, slot_index, do_equip))
        return []

    # ---- 角色 / 经济 ----
    async def create_character(self, player_id, name, faction, template,
                               combat_xp=0, uniform_variant_template_id=0):
        c = {'id': self._id(), 'player_id': player_id, 'name': name,
             'faction': faction, 'template': template,
             'combat_xp': combat_xp,
             'uniform_variant_id': 9000 + (uniform_variant_template_id or 0)}
        self.characters.append(c)
        return dict(c)

    async def list_characters(self, player_id):
        return [dict(c) for c in self.characters
                if c['player_id'] == player_id]

    async def get_character(self, player_id, character_id):
        return next((dict(c) for c in self.characters
                     if c['player_id'] == player_id and c['id'] == character_id), None)

    async def add_booster(self, player_id, template_id, duration_minutes=240,
                          activate_now=False, conn=None, is_ribbon=False):
        self.boosters.append({'player_id': player_id,
                              'template_id': template_id,
                              'duration_minutes': duration_minutes,
                              'activate_now': activate_now,
                              'is_ribbon': is_ribbon})
        return len(self.boosters)

    async def add_membership(self, player_id, template_id, duration_days=30,
                             conn=None):
        self.memberships.append({'player_id': player_id,
                                 'template_id': template_id,
                                 'duration_days': duration_days})
        return {'id': len(self.memberships), 'expires_at': 'running'}

    async def add_currency(self, player_id, credits=0, warfunds=0, gold=0):
        self.gold_added += int(gold)
        return {'credits': credits, 'warfunds': warfunds, 'gold': gold}

    async def create_uniform_part(self, player_id, character_id,
                                  uniform_part_template_id, uniform_part_type):
        p = {'id': self._id(), 'player_id': player_id,
             'character_id': character_id,
             'uniform_part_template_id': uniform_part_template_id,
             'uniform_part_type': uniform_part_type}
        self.uniform_parts.append(p)
        return dict(p)

    async def spend_currency(self, player_id, currency_key, cost, conn=None):
        self.spends.append((player_id, currency_key, cost))
        return self.spend_ok, 4242

    # ---- _char_unlock_bits_for 依赖的查询 ----
    async def get_ribbon_xp(self, player_id, character_id):
        return {}

    async def list_weapons(self, player_id, character_id=None):
        return [dict(w) for w in self.weapons
                if character_id is None
                or w['character_id'] == character_id]

    async def list_weapon_modifiers(self, player_id, character_id=None):
        return [dict(m) for m in self.weapon_modifiers
                if character_id is None
                or m['character_id'] == character_id]

    async def list_weapon_ammo(self, player_id, character_id=None):
        return [dict(a) for a in self.weapon_ammo
                if character_id is None
                or a['character_id'] == character_id]

    async def list_vehicles(self, player_id, character_id=None):
        return [dict(v) for v in self.vehicles
                if character_id is None
                or v['character_id'] == character_id]

    async def list_vehicle_ammo(self, player_id):
        return [dict(a) for a in self.vehicle_ammo]


def _with_fakes(store):
    """把 FakeStore 挂到所有发货路径模块的 STORE（shop/player_service/
    vehicles_purchase/weapons_purchase——后两者的公共发放函数
    grant_default_vehicle_ammo/grant_weapon_ammo 用各自模块级 STORE）。"""
    old_shop = shop_handlers.STORE
    old_ps = player_service.STORE
    old_vp = vp_handlers.STORE
    old_wp = wp_handlers.STORE
    shop_handlers.STORE = store
    player_service.STORE = store
    vp_handlers.STORE = store
    wp_handlers.STORE = store

    def restore():
        shop_handlers.STORE = old_shop
        player_service.STORE = old_ps
        vp_handlers.STORE = old_vp
        wp_handlers.STORE = old_wp
    return restore


def _find_bundle(pred):
    """按谓词找真实 StoreBundle，返回 (id, info, root)。

    多阵营礼包取首个 <items> 块（与生产行为一致：角色未知时回落首块）。"""
    for bid, info in shop_handlers._STORE_BUNDLES.items():
        root = shop_handlers._bundle_item_blocks(info[3])[0]
        if pred(bid, info[0], info[1], info[2], root):
            return bid, info, root
    return None, None, None


def _count(root, tag, nested=False):
    """统计顶层（或 charactertemplate 嵌套）指定标签元素数。"""
    n = sum(1 for el in root if el.tag.lower() == tag)
    if nested:
        for el in root:
            if el.tag.lower() == 'charactertemplate':
                n += sum(1 for sub in el if sub.tag.lower() == tag)
    return n


def _vehicle_ammo_matches(st, detail=False):
    """每个已创建载具的弹药行数 == 独立默认弹药仓数（0 允许）。
    同一仓多条武器/默认映射只发一份，并装备第 1 槽。"""
    ammo_by_vid = {}
    for a in st.vehicle_ammo:
        ammo_by_vid[a['vehicle_id']] = ammo_by_vid.get(a['vehicle_id'], 0) + 1
    rows = []
    ok = True
    for v in st.vehicles:
        parent = wardb._VEHICLE_VARIANT_PARENT.get(
            int(v['vehicle_variant_template_id']))
        expect = len({sid for sid, _at in wardb._default_vehicle_ammo_for(parent)}) \
            if parent is not None else 0
        got = ammo_by_vid.get(v['id'], 0)
        rows.append((v['id'], v['vehicle_variant_template_id'], expect, got))
        ok = ok and got == expect
    return rows if detail else ok


# ---------- A. wardb 礼包数据映射 ----------

def test_storebundle_maps_complete():
    print('== A. wardb 礼包数据映射 ==')
    bundles = shop_handlers._STORE_BUNDLES
    check('StoreBundle 表非空', len(bundles) > 0, f'count={len(bundles)}')

    bad_xml = []
    unknown_wvt = []
    unknown_ammo = []
    unknown_mod = []
    bad_char_tpl = []
    n_wvt = n_ammo = n_mod = 0
    for bid, (ttype, factions, ident, xml) in bundles.items():
        try:
            blocks = shop_handlers._bundle_item_blocks(xml)
        except Exception as e:
            bad_xml.append((bid, str(e)))
            continue
        for root in blocks:
            for el in root.iter():
                tag = el.tag.lower()
                if tag == 'weaponvarianttemplate':
                    n_wvt += 1
                    meta = shop_handlers._WEAPON_VARIANT_META.get(
                        int(el.get('id', 0) or 0))
                    if meta is None or meta[1] <= 0:
                        unknown_wvt.append((bid, el.get('id')))
                elif tag == 'weaponammomapping':
                    n_ammo += 1
                    if int(el.get('id', 0) or 0) not in \
                            shop_handlers._AMMO_TPL_BY_MAPPING:
                        unknown_ammo.append((bid, el.get('id')))
                elif tag == 'weaponmodmapping':
                    n_mod += 1
                    if int(el.get('id', 0) or 0) not in \
                            shop_handlers._MOD_TPL_BY_MAPPING:
                        unknown_mod.append((bid, el.get('id')))
                elif tag == 'charactertemplate':
                    if int(el.get('id', 0) or 0) <= 0:
                        bad_char_tpl.append((bid, el.get('id')))
    check('全部 bundle items XML 可解析', not bad_xml, f'bad={bad_xml[:3]}')
    check(f'全部 {n_wvt} 个武器变体 id 可解析到基础模板',
          n_wvt > 0 and not unknown_wvt, f'unknown={unknown_wvt[:5]}')
    check(f'全部 {n_ammo} 个嵌套弹药 mapping id 可解析',
          not unknown_ammo, f'unknown={unknown_ammo[:5]}')
    check(f'全部 {n_mod} 个嵌套配件 mapping id 可解析',
          not unknown_mod, f'unknown={unknown_mod[:5]}')
    check('charactertemplate id 均为正数', not bad_char_tpl,
          f'bad={bad_char_tpl[:5]}')

    # _first_faction 解析
    ff = shop_handlers._first_faction
    check("_first_faction('1,2,3') == 1", ff('1,2,3') == 1)
    check("_first_faction('') 回退 1", ff('') == 1)
    check("_first_faction(' 2 ') == 2", ff(' 2 ') == 2)

    # 多阵营礼包（多 <items factionTemplateId=N> 根）：块数 = 阵营数，
    # 各块武器互不相同（发货按请求角色阵营选块，不能三份全发）
    multi = [(bid, shop_handlers._bundle_item_blocks(info[3]))
             for bid, info in bundles.items()
             if len(shop_handlers._bundle_item_blocks(info[3])) > 1]
    check('存在多阵营多 <items> 根礼包且按阵营分块',
          any(len(blocks) == 3 for _, blocks in multi),
          f'multi={[(bid, len(b)) for bid, b in multi[:5]]}')
    for bid, blocks in multi[:1]:
        facs = [(b.get('factionTemplateId') or '').strip() for b in blocks]
        wvt = [tuple(sorted(e.get('id') for e in b
                            if e.tag.lower() == 'weaponvarianttemplate'))
               for b in blocks]
        check(f'bundle {bid} 三块 factionTemplateId 互异（{facs}）',
              len(set(facs)) == len(facs))
        check(f'bundle {bid} 各阵营块武器互不相同', len(set(wvt)) == len(wvt))


# ---------- B. 发货逻辑 ----------

def test_deliver_player_bundle():
    """玩家礼包（武器+勋带加成+会员）：全部入库 + KVCS 推送。"""
    print('== B1. 玩家礼包发货 ==')
    found = _find_bundle(
        lambda bid, tt, fac, ident, root:
        _count(root, 'charactertemplate') == 0
        and _count(root, 'weaponvarianttemplate') >= 2
        and _count(root, 'ribbonboostertemplate') >= 1
        and _count(root, 'shopmembershipitems') >= 1)
    bid, info, root = found
    check('找到含武器+加成+会员的玩家礼包', bid is not None)
    if bid is None:
        return
    n_w = _count(root, 'weaponvarianttemplate')
    n_b = _count(root, 'ribbonboostertemplate')
    n_m = _count(root, 'shopmembershipitems')
    print(f'      bundle {bid} {info[2]!r}: 武器x{n_w} 加成x{n_b} 会员x{n_m}')

    st = _FakeStore()
    restore = _with_fakes(st)
    try:
        sess, writer = _MockSess(), _MockWriter()
        rb, rm = asyncio.run(shop_handlers._deliver_store_bundle(
            sess, writer, 77, bid, 55))
    finally:
        restore()

    # 图标缺失的变体（Factory 成色款等）按裸武器发货，只有图标可用的变体才挂实例
    n_var_ok = sum(
        1 for el in root if el.tag.lower() == 'weaponvarianttemplate'
        and int(el.get('id', 0) or 0) in shop_handlers._VARIANT_ICON_OK)
    check(f'创建 {n_w} 把武器（{n_var_ok} 把挂图标可用的皮肤变体）',
          len(st.weapons) == n_w
          and len(st.weapon_variants) == n_var_ok
          and sum(1 for w in st.weapons if w['variant_id']) == n_var_ok,
          f'weapons={len(st.weapons)} variants={len(st.weapon_variants)}')
    check('每把武器带默认/嵌套弹药',
          len(st.weapon_ammo) >= n_w
          and all(a['weapon_id'] in {w['id'] for w in st.weapons}
                  for a in st.weapon_ammo))
    check(f'入库 {n_b} 个勋带加成（is_ribbon=True 且不立即激活）',
          len(st.boosters) == n_b
          and all(b['is_ribbon'] and not b['activate_now']
                  for b in st.boosters),
          f'boosters={st.boosters}')
    mid = int(next(el.get('id') for el in root
                   if el.tag.lower() == 'shopmembershipitems'))
    days = wardb.MEMBERSHIP_DAYS_BY_ID.get(mid)
    check(f'入库会员（模板 {mid}，天数查表 {days}）',
          len(st.memberships) == 1
          and st.memberships[0]['template_id'] == mid
          and st.memberships[0]['duration_days'] == days,
          f'memberships={st.memberships}')
    check('返回计数 (加成, 会员) 供 handler 决定表刷新',
          (rb, rm) == (n_b, n_m), f'got={(rb, rm)}')
    kvcs_ok = b'weapon' in writer.sent and b'WeaponAmmo' in writer.sent
    if n_var_ok:
        kvcs_ok = kvcs_ok and b'WeaponVariant' in writer.sent
    check('KVCS 推送含 weapon / WeaponAmmo（有变体时含 WeaponVariant）表',
          kvcs_ok)
    check('玩家礼包不创建角色', not st.characters and not st.vehicles)


def test_deliver_character_bundle():
    """角色礼包：新角色 + 嵌套武器/载具自动装备槽 1 + 默认载具弹药。"""
    print('== B2. 角色礼包发货 ==')
    found = _find_bundle(
        lambda bid, tt, fac, ident, root:
        _count(root, 'charactertemplate') == 1
        and _count(root, 'vehiclevarianttemplate', nested=True) >= 1
        and _count(root, 'weaponvarianttemplate', nested=True) >= 1)
    bid, info, root = found
    check('找到含新角色+嵌套武器/载具的礼包（Soldier Pack）', bid is not None)
    if bid is None:
        return
    print(f'      bundle {bid} {info[2]!r}')

    st = _FakeStore()
    restore = _with_fakes(st)
    try:
        sess, writer = _MockSess(), _MockWriter()
        rb, rm = asyncio.run(shop_handlers._deliver_store_bundle(
            sess, writer, 77, bid, 55))
    finally:
        restore()

    check('创建 1 个新角色', len(st.characters) == 1,
          f'chars={st.characters}')
    if not st.characters:
        return
    ch = st.characters[0]
    expect_name = info[2].split(':')[-1].strip() if ':' in info[2] else ''
    if not (3 <= len(expect_name) <= 20):
        expect_name = 'Soldier'
    check(f'角色名取礼包名（{expect_name!r}）', ch['name'] == expect_name,
          f'got={ch["name"]!r}')
    el = next(el for el in root if el.tag.lower() == 'charactertemplate')
    rank = int(el.get('rank', 0) or 0)
    expect_xp = wardb._EXPERIENCE_LEVELS[rank] \
        if 0 <= rank < len(wardb._EXPERIENCE_LEVELS) else 0
    check(f'rank={rank} → 初始 combat_xp={expect_xp}',
          ch['combat_xp'] == expect_xp, f'got={ch["combat_xp"]}')

    n_v = _count(root, 'vehiclevarianttemplate', nested=True)
    check(f'嵌套载具 x{n_v} 创建并装备到新角色槽 1',
          len(st.vehicles) == n_v
          and all(e[1] == ch['id'] and e[2] == 1
                  for e in st.vehicle_equips),
          f'equips={st.vehicle_equips}')
    check('载具默认弹药按父模板映射创建（无弹药载具为 0，如自行车）',
          _vehicle_ammo_matches(st),
          f'detail={_vehicle_ammo_matches(st, detail=True)}')
    check('每条已创建载具弹药均按弹药仓装备',
          len(st.vehicle_ammo_equips) == len(st.vehicle_ammo)
          and all(e[3] for e in st.vehicle_ammo_equips),
          f'equips={st.vehicle_ammo_equips}')
    check('嵌套武器装备到新角色槽 1',
          len(st.weapon_containers) >= 1
          and any(c[1] == ch['id'] and c[2] == 1
                  for c in st.weapon_containers),
          f'containers={st.weapon_containers}')
    check('KVCS 推送含 UniformVariant / character / Vehicle 表',
          b'UniformVariant' in writer.sent and b'character' in writer.sent
          and b'Vehicle' in writer.sent)
    uvp = writer.sent.index(b'UniformVariant')
    cvp = writer.sent.index(b'character')
    check('UniformVariant 先于 character 推送（客户端查表顺序）', uvp < cvp)


def _read_fields(b):
    """极简 protobuf 字段解析（varint / length-delimited），用于断言 proto。"""
    fields = {}
    i = 0
    while i < len(b):
        tag = 0; shift = 0
        while True:
            c = b[i]; i += 1
            tag |= (c & 0x7F) << shift; shift += 7
            if not c & 0x80:
                break
        fn, wt = tag >> 3, tag & 7
        if wt == 0:
            v = 0; shift = 0
            while True:
                c = b[i]; i += 1
                v |= (c & 0x7F) << shift; shift += 7
                if not c & 0x80:
                    break
            fields[fn] = v
        elif wt == 2:
            ln = 0; shift = 0
            while True:
                c = b[i]; i += 1
                ln |= (c & 0x7F) << shift; shift += 7
                if not c & 0x80:
                    break
            fields[fn] = b[i:i + ln]; i += ln
    return fields


def test_deliver_character_proto_badge_slots():
    """角色礼包 character proto 徽章槽位回归（2026-09-02 修复）。

    事故：_create_bundle_character 把 uv_id 按位置传进 combat_badge_0，
    客户端 combatbadgeslot0=军服变体实例 id → CombatBadgeTemplateTable
    查不到模板 → SoldierOverviewMediator.updateBadgeObject Error #1009
    （买礼包后士兵面板必崩）。修复：uv_id 改按关键字传
    uniform_variant_id，徽章槽保持 0。
    """
    print('== B4. 角色礼包 character proto 徽章槽位 ==')
    found = _find_bundle(
        lambda bid, tt, fac, ident, root:
        _count(root, 'charactertemplate') == 1)
    bid, info, root = found
    check('找到含新角色的礼包', bid is not None)
    if bid is None:
        return
    print(f'      bundle {bid} {info[2]!r}')

    calls = []
    orig = shop_handlers._msg_character

    def spy(*a, **kw):
        out = orig(*a, **kw)
        calls.append((a, kw, out))
        return out

    st = _FakeStore()
    restore = _with_fakes(st)
    shop_handlers._msg_character = spy
    try:
        sess, writer = _MockSess(), _MockWriter()
        asyncio.run(shop_handlers._deliver_store_bundle(
            sess, writer, 77, bid, 55))
    finally:
        shop_handlers._msg_character = orig
        restore()

    check('character proto 构建恰好 1 次', len(calls) == 1,
          f'calls={len(calls)}')
    if not calls or not st.characters:
        return
    proto = calls[0][2]
    f = _read_fields(proto)
    uv_id = st.characters[0]['uniform_variant_id']
    check('combatbadgeslot0(f10)=0（修复前 uv_id 错位泄漏至此）',
          f.get(10, 0) == 0, f'f10={f.get(10, 0)}')
    check('combatbadgeslot1(f11)=0', f.get(11, 0) == 0,
          f'f11={f.get(11, 0)}')
    check(f'uniformVariantId(f17)={uv_id}（军服变体实例 id 正确传入）',
          f.get(17, 0) == uv_id,
          f'f17={f.get(17, 0)} expected={uv_id}')
    check('proto 角色 id 与入库角色一致',
          f.get(1, 0) == st.characters[0]['id'],
          f'f1={f.get(1, 0)} expected={st.characters[0]["id"]}')


def test_deliver_gold_and_uniform_part():
    """gold / uniformparttemplate 元素：金币入账 + 头盔实例。"""
    print('== B3. 金币与头盔元素 ==')
    test_id = 990001
    old = shop_handlers._STORE_BUNDLES.get(test_id)
    shop_handlers._STORE_BUNDLES[test_id] = (
        'player', '1,2,3', 'Test Gold Bundle',
        '<items><gold amount="1337"/>'
        '<uniformparttemplate id="2176"/>'
        '</items>')
    st = _FakeStore()
    restore = _with_fakes(st)
    try:
        sess, writer = _MockSess(), _MockWriter()
        asyncio.run(shop_handlers._deliver_store_bundle(
            sess, writer, 77, test_id, 55))
    finally:
        restore()
        if old is None:
            shop_handlers._STORE_BUNDLES.pop(test_id, None)
        else:
            shop_handlers._STORE_BUNDLES[test_id] = old
    check('gold amount=1337 入账', st.gold_added == 1337,
          f'gold_added={st.gold_added}')
    check('头盔实例发给请求角色（template 2176）',
          len(st.uniform_parts) == 1
          and st.uniform_parts[0]['uniform_part_template_id'] == 2176
          and st.uniform_parts[0]['character_id'] == 55,
          f'parts={st.uniform_parts}')
    check('KVCS 推送含 UniformPart 表', b'UniformPart' in writer.sent)


# ---------- C. 容错 ----------

def test_delivery_fault_isolation():
    """单项失败不影响其余物品（钱已扣，尽量多发货）。"""
    print('== C1. 单项失败隔离 ==')
    test_id = 990002
    wvt = next(iter(shop_handlers._WEAPON_VARIANT_META))
    old = shop_handlers._STORE_BUNDLES.get(test_id)
    shop_handlers._STORE_BUNDLES[test_id] = (
        'player', '1', 'Test Fault Bundle',
        f'<items><weaponvarianttemplate id="{wvt}"/>'
        f'<weaponvarianttemplate id="{wvt}"/>'
        f'<ribbonboostertemplate id="1"/>'
        f'</items>')
    st = _FakeStore()
    st.fail_weapon_calls = {1}     # 第 1 把武器创建失败
    restore = _with_fakes(st)
    try:
        sess, writer = _MockSess(), _MockWriter()
        rb, rm = asyncio.run(shop_handlers._deliver_store_bundle(
            sess, writer, 77, test_id, 55))
    finally:
        restore()
        if old is None:
            shop_handlers._STORE_BUNDLES.pop(test_id, None)
        else:
            shop_handlers._STORE_BUNDLES[test_id] = old
    check('失败武器跳过、第二把照发', len(st.weapons) == 1,
          f'weapons={len(st.weapons)}')
    check('加成不受武器失败影响照发', len(st.boosters) == 1,
          f'boosters={len(st.boosters)}')
    check('返回计数仍准确 (1 加成)', (rb, rm) == (1, 0), f'got={(rb, rm)}')


def test_delivery_unknown_and_tolerances():
    """未知 bundle / 未知元素不抛异常。"""
    print('== C2. 未知 bundle 与未知元素 ==')
    st = _FakeStore()
    restore = _with_fakes(st)
    try:
        sess, writer = _MockSess(), _MockWriter()
        rb, rm = asyncio.run(shop_handlers._deliver_store_bundle(
            sess, writer, 77, 999999999, 55))
        check('未知 bundle id → (0,0) 不抛', (rb, rm) == (0, 0))
        check('未知 bundle 不发货', not st.weapons and not st.characters)

        test_id = 990003
        old = shop_handlers._STORE_BUNDLES.get(test_id)
        shop_handlers._STORE_BUNDLES[test_id] = (
            'player', '1', 'Test Junk Bundle', '<items><foobar id="1"/></items>')
        try:
            rb2, rm2 = asyncio.run(shop_handlers._deliver_store_bundle(
                sess, writer, 77, test_id, 55))
            check('未知元素跳过不抛 → (0,0)', (rb2, rm2) == (0, 0))
        finally:
            if old is None:
                shop_handlers._STORE_BUNDLES.pop(test_id, None)
            else:
                shop_handlers._STORE_BUNDLES[test_id] = old
    finally:
        restore()


# ---------- D. handler 完整链路 ----------

def _varint(field, value):
    out = bytearray([field << 3 | 0])
    while True:
        b = value & 0x7F
        value >>= 7
        out.append(b | 0x80 if value else b)
        if not value:
            return bytes(out)


def _response_result(sent):
    """按帧解析响应，不能把后续 character/库存推送中的字节当作结果。"""
    offset = 0
    while offset < len(sent):
        frame, consumed = Chunk.parse(sent[offset:])
        offset += consumed
        title = frame.subs[0]
        if title.data == b'BuyStoreBundleResponse':
            return pb_parse(title.subs[0].data)[1]
    raise AssertionError('BuyStoreBundleResponse missing')


def test_handler_purchase_flow():
    print('== D. handle_buystorebundlerequest 完整链路 ==')
    found = _find_bundle(
        lambda bid, tt, fac, ident, root:
        _count(root, 'weaponvarianttemplate') >= 1
        and _count(root, 'charactertemplate') == 0
        and any(d.get('type') == 'StoreBundle' and d.get('object_id') == bid
                and int(d.get('credits') or 0) > 0
                for d in wardb._PRICING_MAP.values()))
    bid, info, root = found
    check('找到一个纯物品礼包做链路测试', bid is not None)
    if bid is None:
        return
    n_b = _count(root, 'ribbonboostertemplate')
    price_id = next(p for p, d in wardb._PRICING_MAP.items()
                    if d.get('type') == 'StoreBundle' and d.get('object_id') == bid
                    and int(d.get('credits') or 0) > 0)

    st = _FakeStore()
    st.characters.append({'id': 55, 'player_id': 77, 'name': 'Owner',
                          'faction': 1, 'template': 1})
    restore = _with_fakes(st)
    try:
        # D1: 匹配礼包的真实价格 + 合法归属角色 → 扣款、发货 + result=1
        proto = (_varint(1, bid) + _varint(2, price_id) + _varint(3, 0)
                 + _varint(4, 55) + _varint(5, 1))
        sess, writer = _MockSess(), _MockWriter()
        asyncio.run(shop_handlers.handle_buystorebundlerequest(
            sess, writer, 999, 'BuyStoreBundleRequest', proto))
        check('响应 result=1（ok）', _response_result(writer.sent) == 1,
              f'result={_response_result(writer.sent)}')
        check('礼包武器已发货（修复前此处为 0——只扣款不发货）',
              len(st.weapons) == _count(root, 'weaponvarianttemplate'),
              f'weapons={len(st.weapons)}')
        if n_b:
            check('勋带加成已入库', len(st.boosters) == n_b,
                  f'boosters={len(st.boosters)}')

        # D2: 余额不足（真实 pricing + spend 失败）→ result=56 且不发货
        st2 = _FakeStore()
        st2.characters = [dict(st.characters[0])]
        st2.spend_ok = False
        shop_handlers.STORE = st2
        player_service.STORE = st2
        vp_handlers.STORE = st2
        wp_handlers.STORE = st2
        proto2 = (_varint(1, bid) + _varint(2, price_id) + _varint(3, 0)
                  + _varint(4, 55) + _varint(5, 1))
        sess2, writer2 = _MockSess(), _MockWriter()
        asyncio.run(shop_handlers.handle_buystorebundlerequest(
            sess2, writer2, 999, 'BuyStoreBundleRequest', proto2))
        check('余额不足响应 result=56（not_enough）',
              _response_result(writer2.sent) == 56,
              f'result={_response_result(writer2.sent)}')
        check('余额不足不发货（钱货一致）',
              not st2.weapons and not st2.boosters and len(st2.characters) == 1,
              f'weapons={len(st2.weapons)} boosters={len(st2.boosters)}')
        check('扣款尝试发生（spend_currency 被调用）',
              len(st2.spends) == 1, f'spends={st2.spends}')
    finally:
        restore()


def main():
    test_storebundle_maps_complete()
    test_deliver_player_bundle()
    test_deliver_character_bundle()
    test_deliver_character_proto_badge_slots()
    test_deliver_gold_and_uniform_part()
    test_delivery_fault_isolation()
    test_delivery_unknown_and_tolerances()
    test_handler_purchase_flow()
    print(f'\n结果: PASS={PASS} FAIL={FAIL}')
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
