# -*- coding: utf-8 -*-
"""通行证载具皮肤 + 面部迷彩发放回归测试（2026-09-08）。

覆盖三项修复：
A. wardb.player_unlock_bits_for_level granted_ids 合并（纯函数）：
   通行证面部迷彩 progressionUnlock 2387-2395（eHeadSwap）不在
   PlayerLevelUnlock 表（id 1-40）——必须经 player_unlocks 显式登记，
   位图长度须覆盖这些 id（客户端 unlockBits.Count <= id 判越界恒 false）
B. career._grant_reward_xml（FakeStore）：
   · vehiclevarianttemplate → create_vehicle_variant 仓库行
     （char=0/vehicle=0；旧实现误发整车 + 默认弹药）
   · progressionunlock → add_player_unlocks 登记 + 全角色 extra uid
C. vehicles.handle_setvehiclevariantrequest（FakeStore）：
   · vehicle_id=0 仓库皮肤行按 parentVehicleTemplateId 校验装备并绑定
   · 不同 parent 拒绝；已绑定行走原路径；variantId=0 卸下

真实模板 id（WarDatabase 固定数据）：皮肤 470→parent 44、471→parent 95；
型号变体 44（同 parent 44）。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HERE = os.path.dirname(os.path.abspath(__file__))

for _cand in (os.environ.get('HNG_GAME_DIR'),
              os.path.abspath(os.path.join(_HERE, '..', '..', '..', '..'))):
    if _cand and os.path.isdir(os.path.join(_cand, 'WarDatabase')):
        os.environ['HNG_GAME_DIR'] = _cand
        break

from protocol import (Chunk, msg_player, pb_parse,     # noqa: E402
                      pb_parse_multi)
from wardb import (_PLAYER_LEVEL_UNLOCK_META,          # noqa: E402
                   _VEHICLE_VARIANT_PARENT,
                   player_unlock_bits_for_level)
from handlers import career, vehicles, uniforms           # noqa: E402

PASS = FAIL = 0

# 真实模板 id（测试前提校验用）
SKIN_SAME = 470      # 通行证皮肤 → parent 44
SKIN_DIFF = 471      # 通行证皮肤 → parent 95（与 44 不同）
VEH_MODEL = 44       # 同 parent 44 的型号变体（车辆本体模板）
CAMO_IDS = list(range(2387, 2396))
MASK_IDS = [101, 120, 122]   # 侦察口罩（德 101/苏 120/美 122，cls=6）
VEIL_IDS = [138, 139, 140]   # 侦察面纱（德 138/美 139/苏 140，cls=6）


def check(name, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  PASS  {name}')
    else:
        FAIL += 1
        print(f'  FAIL  {name}  {detail}')


def _run(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError
    except RuntimeError:
        return asyncio.run(coro)
    return loop.run_until_complete(coro)


# ------------------------------------------------------------ 测试桩

class _FakeWriter:
    def __init__(self):
        self.frames = []

    def write(self, data):
        self.frames.append(bytes(data))

    async def drain(self):
        pass


class _FakeSess:
    playerview_cmd = None
    soldier_view_cmd = 77
    username = 'skin-test'

    def __init__(self, pid=42):
        self._pid = pid

    async def ensure_player(self):
        return self._pid


class _FakeStore:
    """覆盖被断言的 STORE 方法；未知方法记录后 noop（回归断言用）。"""

    def __init__(self, vehicles=None, variants=None, chars=None,
                 parts=None):
        # vehicles: [{id, vehicle_variant_template_id, character_id, ...}]
        # variants: [{id, vehicle_id, character_id, vehicle_variant_template_id}]
        # parts: [{id, character_id, uniform_part_template_id, uniform_part_type}]
        self.vehicles = {int(v['id']): dict(v) for v in (vehicles or [])}
        self.variants = {int(v['id']): dict(v) for v in (variants or [])}
        self.parts = {int(p['id']): dict(p) for p in (parts or [])}
        self.chars = chars or [{'id': 1, 'template': 1, 'faction': 1,
                                'combat_xp': 0}]
        self.calls = []            # (method, args)
        self.unknown = []          # 未知方法名（误发整车回归断言源）
        self._next_vv = 1001

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)

        async def _noop(*_a, **_kw):
            return None
        self.unknown.append(name)
        return _noop

    # ---- vehicles 域 ----
    async def get_vehicle(self, player_id, vehicle_id):
        return self.vehicles.get(int(vehicle_id))

    async def list_vehicle_variants(self, player_id):
        return list(self.variants.values())

    async def set_vehicle_variant(self, player_id, vehicle_id, variant_id):
        self.calls.append(('set_vehicle_variant',
                           (player_id, vehicle_id, variant_id)))
        v = self.vehicles.get(int(vehicle_id))
        if v is not None:
            v['variant_id'] = int(variant_id)

    async def bind_vehicle_variant(self, player_id, variant_id, vehicle_id,
                                   character_id):
        self.calls.append(('bind_vehicle_variant',
                           (player_id, variant_id, vehicle_id, character_id)))
        row = self.variants.get(int(variant_id))
        if row is None or int(row['vehicle_id']) != 0:
            return None
        row['vehicle_id'] = int(vehicle_id)
        row['character_id'] = int(character_id)
        return dict(row)

    # ---- career 发放域 ----
    async def create_vehicle_variant(self, player_id, vehicle_id,
                                     character_id, template_id):
        self.calls.append(('create_vehicle_variant',
                           (player_id, vehicle_id, character_id, template_id)))
        vid = self._next_vv
        self._next_vv += 1
        row = {'id': vid, 'vehicle_id': int(vehicle_id),
               'character_id': int(character_id),
               'vehicle_variant_template_id': int(template_id)}
        self.variants[vid] = row
        return dict(row)

    async def create_uniform_part(self, player_id, character_id,
                                  template_id, part_type):
        self.calls.append(('create_uniform_part',
                           (player_id, character_id, template_id, part_type)))
        pid2 = 950 + len(self.parts)
        row = {'id': pid2, 'character_id': int(character_id),
               'uniform_part_template_id': int(template_id),
               'uniform_part_type': int(part_type)}
        self.parts[pid2] = row
        return dict(row)

    async def list_uniform_parts(self, player_id):
        return list(self.parts.values())

    async def set_character_headgear(self, player_id, char_id, part_id):
        self.calls.append(('set_character_headgear',
                           (player_id, char_id, part_id)))
        for ch in self.chars:
            if int(ch['id']) == int(char_id):
                ch['headgear_id'] = int(part_id)
        return True

    async def get_character(self, player_id, char_id):
        for ch in self.chars:
            if int(ch['id']) == int(char_id):
                c = dict(ch)
                c.setdefault('name', 'Test')
                c.setdefault('faction', 1)
                c.setdefault('template', 1)
                c.setdefault('combat_xp', 0)
                c.setdefault('combat_badge_0', 0)
                c.setdefault('combat_badge_1', 0)
                c.setdefault('uniform_variant_id', 0)
                c.setdefault('headgear_id', 0)
                c.setdefault('is_deleted', 0)
                c.setdefault('previous_template', 0)
                c.setdefault('head_id', 0)
                c.setdefault('is_on_leave', 0)
                return c
        return None

    async def get_squad_for_player(self, player_id):
        return None

    async def add_player_unlocks(self, player_id, uids):
        self.calls.append(('add_player_unlocks', (player_id, tuple(uids))))

    async def add_extra_unlock_uids(self, player_id, char_id, uids):
        self.calls.append(('add_extra_unlock_uids',
                           (player_id, char_id, tuple(uids))))

    async def list_characters(self, player_id):
        return list(self.chars)

    # ---- revalidate_state_orders / _char_unlock_bits_for 防御 ----
    # （noop None 会被 any()/迭代崩；list_* 带 *a 兼容按角色过滤调用）
    async def list_weapons(self, player_id, *a, **kw):
        return []

    async def list_career_orders(self, player_id):
        return {}

    async def list_vehicles(self, player_id, *a, **kw):
        return []

    async def list_weapon_modifiers(self, player_id, *a, **kw):
        return []

    async def list_vehicle_upgrades(self, player_id, *a, **kw):
        return []

    async def list_vehicle_ammo(self, player_id, *a, **kw):
        return []

    async def list_weapon_ammo(self, player_id, *a, **kw):
        return []

    async def get_ribbon_xp(self, player_id, char_id):
        return {}

    async def get_total_score(self, player_id):
        return 0


def _kvcs_rows(writer, table_name):
    """从 writer 帧解出指定 KVCS 表的行 → [pb_parse dict]。"""
    rows = []
    for frame in writer.frames:
        pos = 0
        while pos < len(frame):
            root, nxt = Chunk.parse(frame, pos)
            if root is None:
                break
            pos = nxt
            for title in root.subs:
                if title.data != b'KeyValueChangeSet':
                    continue
                for action in title.subs:
                    for table in action.subs:
                        if table.data == table_name:
                            for row in table.subs:
                                rows.append(pb_parse(row.data))
    return rows


def _response_field(writer, title, field):
    """从 writer 帧中解出响应 proto 的字段值（取首个匹配 title 的响应）。"""
    for frame in writer.frames:
        pos = 0
        while pos < len(frame):
            root, nxt = Chunk.parse(frame, pos)
            if root is None:
                break
            pos = nxt
            if root.data == b'' or not root.subs:
                continue
            for sub in root.subs:
                if sub.data == title.encode():
                    return pb_parse(sub.subs[0].data).get(field)
    return None


def _call(store, name):
    return [c for c in store.calls if c[0] == name]


# ------------------------------------------------------------ A. 位图合并

def test_unlock_bits():
    print('[A] player_unlock_bits_for_level granted 合并')
    # A1 设计前提：面部迷彩 id 超出 PlayerLevelUnlock 表范围（等级推导不含）
    check('A1 迷彩 id 超出等级表范围',
          max(_PLAYER_LEVEL_UNLOCK_META) < min(CAMO_IDS),
          f'max meta={max(_PLAYER_LEVEL_UNLOCK_META)}')

    # A2 granted 位置位 + 位图长度覆盖（客户端 Count<=id 越界保护）
    bits = player_unlock_bits_for_level(1, CAMO_IDS)
    check('A2 迷彩位全部置位',
          all(bits[i] for i in CAMO_IDS), f'len={len(bits)}')
    check('A2 位图长度覆盖最大 granted id',
          len(bits) > max(CAMO_IDS), f'len={len(bits)}')

    # A3 granted 不侵入等级推导前缀（模式/兵种门控回归）
    lv_max = max(_PLAYER_LEVEL_UNLOCK_META)
    for lvl in (1, 5, 12):
        check(f'A3 lv{lvl} 等级前缀不受 granted 影响',
              player_unlock_bits_for_level(lvl, CAMO_IDS)[:lv_max + 1]
              == player_unlock_bits_for_level(lvl)[:lv_max + 1])

    # A4 非法 id（<=0）过滤
    check('A4 非法 granted id 过滤',
          player_unlock_bits_for_level(3, (0, -7))
          == player_unlock_bits_for_level(3))

    # A5 等级位仍生效（回归：不是恒 false）
    b1 = player_unlock_bits_for_level(1)
    b12 = player_unlock_bits_for_level(12)
    check('A5 等级推导位生效（lv1 != lv12 且 lv12 有解锁位）',
          b1 != b12 and any(b12))


def test_msg_player_f37():
    print('[A2] msg_player f37 unlockBits 合并')
    proto = msg_player('camo-test', pid=42, level=1, unlock_ids=(2387, 2390))
    f37 = pb_parse_multi(proto).get(37)
    check('f37 解出 bool 列表且长度覆盖 granted id',
          isinstance(f37, list) and len(f37) > 2390, f'len={len(f37 or [])}')
    if f37:
        check('f37 迷彩位 2387/2390 置位',
              f37[2387] == 1 and f37[2390] == 1)
        check('f37 未登记迷彩位 2391 保持 false（长度外或 0）',
              len(f37) <= 2391 or f37[2391] == 0)
        check('f37 等级推导前缀与无 granted 一致',
              f37[:41] == pb_parse_multi(msg_player('x', pid=42, level=1)
                                         )[37][:41])


# ------------------------------------------------------------ B. 奖励发放

def test_grant_vehicle_skin():
    print('[B] _grant_reward_xml 载具皮肤 → 仓库行')
    old = career.STORE
    try:
        store = _FakeStore()
        career.STORE = store
        writer = _FakeWriter()
        granted = _run(career._grant_reward_xml(
            _FakeSess(), writer, 42,
            f"<items target='depot'><vehiclevarianttemplate id='{SKIN_SAME}'/>"
            f"</items>", 'test-skin'))
        check('B1 入账摘要标记 vehicleskin',
              any('vehicleskin' in g for g in granted), f'granted={granted}')
        created = _call(store, 'create_vehicle_variant')
        check('B1 创建仓库行 char=0/vehicle=0',
              len(created) == 1
              and created[0][1] == (42, 0, 0, SKIN_SAME),
              f'created={created}')
        check('B1 不再误发整车（无 create_vehicle 系调用）',
              not [u for u in store.unknown
                   if u.startswith('create_vehicle') and u != 'create_vehicle_variant']
              and not [u for u in store.unknown if 'bundle' in u],
              f'unknown={store.unknown}')
        rows = _kvcs_rows(writer, b'VehicleVariant')
        check('B1 KVCS 推 VehicleVariant 仓库行（f5=模板/4=vehicle=0/3=char=0）',
              len(rows) == 1 and rows[0].get(5) == SKIN_SAME
              and rows[0].get(4) == 0 and rows[0].get(3) == 0,
              f'rows={rows}')
    finally:
        career.STORE = old


def test_grant_progression_unlock():
    print('[B] _grant_reward_xml progressionunlock → player_unlocks')
    old = career.STORE
    try:
        store = _FakeStore()
        career.STORE = store
        writer = _FakeWriter()
        granted = _run(career._grant_reward_xml(
            _FakeSess(), writer, 42,
            "<items target='depot'><progressionunlock id='2387'/></items>",
            'test-camo'))
        check('B2 入账摘要标记 progressionunlock',
              any('progressionunlock' in g for g in granted),
              f'granted={granted}')
        added = _call(store, 'add_player_unlocks')
        check('B2 player_unlocks 登记 [2387]',
              len(added) == 1 and added[0][1] == (42, (2387,)),
              f'added={added}')
        extras = _call(store, 'add_extra_unlock_uids')
        check('B2 全角色 extra uid 登记',
              len(extras) == len(store.chars)
              and all(e[1][2] == (2387,) for e in extras),
              f'extras={extras}')
    finally:
        career.STORE = old


# ------------------------------------------------------------ C. 皮肤装备

def _equip(store, sess, vehicle_id, variant_id):
    """跑 SetVehicleVariantRequest 并返回 (response, writer)。"""
    from protocol import pb_varint
    writer = _FakeWriter()
    proto = pb_varint(1, vehicle_id) + pb_varint(2, variant_id)
    _run(vehicles.handle_setvehiclevariantrequest(
        sess, writer, 123, 'SetVehicleVariantRequest', proto))
    return _response_field(writer, 'SetVehicleVariantResponse', 2), writer


def test_equip_depot_skin():
    print('[C] SetVehicleVariantRequest 仓库皮肤行装备')
    # C0 测试前提：皮肤/车辆 parent 映射符合假设
    check('C0 皮肤 470 与型号 44 同 parent（44），皮肤 471 异 parent',
          _VEHICLE_VARIANT_PARENT.get(SKIN_SAME) == _VEHICLE_VARIANT_PARENT.get(VEH_MODEL) == 44
          and _VEHICLE_VARIANT_PARENT.get(SKIN_DIFF) != 44,
          f'p470={_VEHICLE_VARIANT_PARENT.get(SKIN_SAME)} '
          f'p44={_VEHICLE_VARIANT_PARENT.get(VEH_MODEL)} '
          f'p471={_VEHICLE_VARIANT_PARENT.get(SKIN_DIFF)}')

    old_v, old_c = vehicles.STORE, career.STORE
    try:
        # C1 仓库行 + 同 parent → 接受 + 绑定 + KVCS
        store = _FakeStore(
            vehicles=[{'id': 400, 'vehicle_variant_template_id': VEH_MODEL,
                       'character_id': 7, 'equipped_in_slot': 1,
                       'variant_id': 0}],
            variants=[{'id': 1001, 'vehicle_id': 0, 'character_id': 0,
                       'vehicle_variant_template_id': SKIN_SAME}])
        vehicles.STORE = career.STORE = store
        resp, writer = _equip(store, _FakeSess(), 400, 1001)
        check('C1 同 parent 仓库行装备接受（response=1）', resp == 1,
              f'resp={resp}')
        bound = _call(store, 'bind_vehicle_variant')
        check('C1 皮肤行绑定到载具（char 跟随车辆）',
              len(bound) == 1 and bound[0][1] == (42, 1001, 400, 7),
              f'bound={bound}')
        check('C1 store 皮肤行已更新 vehicle=400/char=7',
              store.variants[1001]['vehicle_id'] == 400
              and store.variants[1001]['character_id'] == 7)
        vv_rows = _kvcs_rows(writer, b'VehicleVariant')
        check('C1 KVCS 推绑定后 VehicleVariant 行（f1=1001/3=7/4=400/5=470）',
              len(vv_rows) == 1 and vv_rows[0].get(1) == 1001
              and vv_rows[0].get(3) == 7 and vv_rows[0].get(4) == 400
              and vv_rows[0].get(5) == SKIN_SAME, f'rows={vv_rows}')
        v_rows = _kvcs_rows(writer, b'Vehicle')
        check('C1 KVCS 重推 Vehicle 行（f9=装备皮肤实例 id）',
              any(r.get(1) == 400 and r.get(9) == 1001 for r in v_rows),
              f'rows={[(r.get(1), r.get(9)) for r in v_rows]}')

        # C2 仓库行 + 不同 parent → 拒绝
        store = _FakeStore(
            vehicles=[{'id': 400, 'vehicle_variant_template_id': VEH_MODEL,
                       'character_id': 7, 'equipped_in_slot': 1,
                       'variant_id': 0}],
            variants=[{'id': 1002, 'vehicle_id': 0, 'character_id': 0,
                       'vehicle_variant_template_id': SKIN_DIFF}])
        vehicles.STORE = career.STORE = store
        resp, _w = _equip(store, _FakeSess(), 400, 1002)
        check('C2 异 parent 皮肤拒绝（response=0）', resp == 0, f'resp={resp}')
        check('C2 拒绝时不绑定',
              not _call(store, 'bind_vehicle_variant'))

        # C3 已绑定到该载具的行 → 走原路径接受，不重复绑定
        store = _FakeStore(
            vehicles=[{'id': 400, 'vehicle_variant_template_id': VEH_MODEL,
                       'character_id': 7, 'equipped_in_slot': 1,
                       'variant_id': 0}],
            variants=[{'id': 1003, 'vehicle_id': 400, 'character_id': 7,
                       'vehicle_variant_template_id': SKIN_SAME}])
        vehicles.STORE = career.STORE = store
        resp, _w = _equip(store, _FakeSess(), 400, 1003)
        check('C3 已绑定行装备接受（response=1）', resp == 1, f'resp={resp}')
        check('C3 已绑定行不再触发 bind',
              not _call(store, 'bind_vehicle_variant'))

        # C4 卸下（variantId=0）
        store = _FakeStore(
            vehicles=[{'id': 400, 'vehicle_variant_template_id': VEH_MODEL,
                       'character_id': 7, 'equipped_in_slot': 1,
                       'variant_id': 1001}])
        vehicles.STORE = career.STORE = store
        resp, _w = _equip(store, _FakeSess(), 400, 0)
        check('C4 卸下接受（response=1）且不绑定',
              resp == 1 and not _call(store, 'bind_vehicle_variant'),
              f'resp={resp}')
    finally:
        vehicles.STORE, career.STORE = old_v, old_c


# ------------------------------------------------------------ D. 口罩/面纱

def test_uniform_part_type_semantics():
    print('[D] UniformPartTemplate f4 → EUniformPartType 语义')
    # 口罩/面纱与头盔同为 f4=1（eHelmet，走头盔槽装备）；头模型 f4=0
    # （eHead，characters.head_id）；手模型 f4=5（eHand）
    for tid in MASK_IDS + VEIL_IDS:
        check(f'D1 模板 {tid} 类型 = eHelmet(1)',
              career._uniform_part_type(tid) == 1)
    check('D1 头模型 89 类型 = eHead(0)', career._uniform_part_type(89) == 0)
    check('D1 手模型 91 类型 = eHand(5)', career._uniform_part_type(91) == 5)


def test_grant_mask_veil():
    print('[D] _grant_reward_xml 口罩/面纱 → 仓库行')
    for tid in (MASK_IDS[0], VEIL_IDS[0]):
        old = career.STORE
        try:
            store = _FakeStore()
            career.STORE = store
            writer = _FakeWriter()
            granted = _run(career._grant_reward_xml(
                _FakeSess(), writer, 42,
                f"<items target='depot'><uniformparttemplate id='{tid}'/>"
                f"</items>", 'test-mask'))
            check(f'D2 模板 {tid} 入账摘要标记 uniformpart',
                  any(g == f'uniformpart#{tid}' for g in granted),
                  f'granted={granted}')
            created = _call(store, 'create_uniform_part')
            check(f'D2 模板 {tid} 创建仓库行 char=0/type=1',
                  len(created) == 1
                  and created[0][1] == (42, 0, tid, 1),
                  f'created={created}')
            rows = _kvcs_rows(writer, b'UniformPart')
            check(f'D2 模板 {tid} KVCS 推 UniformPart 行（f3=0/f4=tid/f5=1）',
                  len(rows) == 1 and rows[0].get(3) == 0
                  and rows[0].get(4) == tid and rows[0].get(5) == 1,
                  f'rows={rows}')
        finally:
            career.STORE = old


def test_equip_mask():
    print('[D] SetUniformPartRequest 口罩装备')
    from protocol import pb_varint
    old_u, old_c = uniforms.STORE, career.STORE
    try:
        # D3 指派到角色的口罩行（char=7 type=1）→ 装备接受
        store = _FakeStore(
            chars=[{'id': 7, 'template': 6, 'faction': 2, 'combat_xp': 0}],
            parts=[{'id': 950, 'character_id': 7,
                    'uniform_part_template_id': MASK_IDS[0],
                    'uniform_part_type': 1}])
        uniforms.STORE = career.STORE = store

        writer = _FakeWriter()
        proto = (pb_varint(1, 7) + pb_varint(2, 950) + pb_varint(3, 1))
        _run(uniforms.handle_setuniformpartrequest(
            _FakeSess(), writer, 124, 'SetUniformPartRequest', proto))
        resp = _response_field(writer, 'SetUniformPartResponse', 2)
        check('D3 仓库指派口罩装备接受（response=1）', resp == 1, f'resp={resp}')
        hg = _call(store, 'set_character_headgear')
        check('D3 持久化 headgear_id=950',
              len(hg) == 1 and hg[0][1] == (42, 7, 950), f'hg={hg}')
        ch_rows = _kvcs_rows(writer, b'character')
        check('D3 重推 character 行（f18 headgearId=950）',
              any(r.get(18) == 950 for r in ch_rows),
              f'f18={[r.get(18) for r in ch_rows]}')

        # D4 类型不匹配（客户端发 5 eHand）→ 拒绝
        writer = _FakeWriter()
        proto = (pb_varint(1, 7) + pb_varint(2, 950) + pb_varint(3, 5))
        _run(uniforms.handle_setuniformpartrequest(
            _FakeSess(), writer, 124, 'SetUniformPartRequest', proto))
        resp = _response_field(writer, 'SetUniformPartResponse', 2)
        check('D4 类型不匹配拒绝（response=0）', resp == 0, f'resp={resp}')

        # D5 卸下（partId=0）
        writer = _FakeWriter()
        proto = (pb_varint(1, 7) + pb_varint(2, 0) + pb_varint(3, 0))
        _run(uniforms.handle_setuniformpartrequest(
            _FakeSess(), writer, 124, 'SetUniformPartRequest', proto))
        resp = _response_field(writer, 'SetUniformPartResponse', 2)
        check('D5 卸下接受（response=1）', resp == 1, f'resp={resp}')
    finally:
        uniforms.STORE, career.STORE = old_u, old_c


# ------------------------------------------------------------ main

if __name__ == '__main__':
    test_unlock_bits()
    test_msg_player_f37()
    test_grant_vehicle_skin()
    test_grant_progression_unlock()
    test_equip_depot_skin()
    test_uniform_part_type_semantics()
    test_grant_mask_veil()
    test_equip_mask()
    print(f'\n{"="*50}\nPASS={PASS} FAIL={FAIL}')
    sys.exit(1 if FAIL else 0)
