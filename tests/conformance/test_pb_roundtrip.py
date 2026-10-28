# -*- coding: utf-8 -*-
"""P3 conformance：我们手写 protobuf 编码 vs hagcp 官方 proto（protoc 编译）
双向 round-trip 对拍。

官方 bindings（HnG_States_pb2 等 5 个，780 消息）由 fix_and_compile.py 从
hagcp proto 生成并提交（测试环境零依赖：无需 protoc/grpcio-tools）。

每条 case（消息名 + 我们构建 bytes 的工厂）三层验证：
  L1 wire 合规：官方 ParseFromString(ours) 无异常——错字段号 / 错 wire
     type（如 sfixed64 写成 varint）/ 非法 varint 在此炸；
  L2 解码重建等价：pb_parse_multi(ours) → 按官方 DESCRIPTOR 类型重建官方
     消息 → 两者 ListFields 逐字段等价（证明字段号与 wire type 全部与
     官方 schema 一致，值往返无损）；
  L3 官方编码 → 我们解码：m1.SerializeToString() → pb_parse_multi 结果
     与 ours 的解码完全相同（我们解码器对官方字节同样无损）。

覆盖范围（首批 27 条，2026-09-17）：
  - war 大地图/battle 行 12 条（warmap_view + battle_msgs）
  - SOLDIER_VIEW 装备行 14 条（messages._msg_*）
  - statrequests.Ribbon 1 条
不在本测试范围（无官方 schema 可对拍）：
  - warserver_messages 全部（PlayerSettings/CharacterEntry/WeaponSetting/
    ResponseBattleFieldSettings 等）——hng.exe/player.dll 私有协议，
    schema 源是 player.dll 逆向描述符（RVA 0x7b3f78），hagcp 未覆盖；
  - pb_packed_bool：实为 proto2 标准 non-packed 逐元素编码，与官方
    proto2 repeated bool 天然一致（character f21 unlockBits 已覆盖验证）。

用法：python tests/conformance/test_pb_roundtrip.py（或 run_all 收录）。
"""
import collections
import datetime
import struct
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))                    # *_pb2.py
sys.path.insert(0, str(HERE.parent.parent))      # server/（兼容任意 cwd）

import HnG_States_pb2 as hng                     # noqa: E402
import statrequests_pb2 as sreq                  # noqa: E402
from protocol import pb_parse_multi              # noqa: E402

import messages                                  # noqa: E402
import wardb                                     # noqa: E402
from war_campaign import battle_msgs             # noqa: E402
from war_campaign import warmap_view             # noqa: E402

# 固定 playerid（messages._msg_* 内部 _cur_pid() 取会话上下文，测试里钉死）
messages._cur_pid = lambda: 33554432
PID = 33554432

DT = datetime.datetime(2026, 9, 1, 12, 0, 0,
                       tzinfo=datetime.timezone.utc)

AT = dict(id=99001, name='AT-Conformance', abbreviation='CT9',
          owner_player_id=77, faction=1, template_id=103, rank=2,
          general_character_id=501,
          stats_updated_at=DT, created_at=DT,
          parent_command_node_id=0, map_entity_id=20, map_entity_type=1,
          morale=88, max_morale=100, is_deployed=True,
          has_gotten_soldier_free=False, has_gotten_vehicle_free=True,
          resources={1: 120, 12: 8})
WB = dict(id=5501, war_id=9, battlefield_id=20, battle_type=1, position=0.0,
          factions=[{'at_id': 99001, 'faction': 1},
                    {'at_id': 99002, 'faction': 2}],
          started_at=DT)
MV = dict(id=66001, started_at=DT, dest_ap=5)
SEG = dict(sl_id=31, from_ap=3, to_ap=4, from_bf=10, to_bf=20,
           begin_ms=1756732800000, end_ms=1756736400000)


def _sl_rows():
    """supplylinestatus 行（依赖真实 WarDatabase 补给线表，空表返回 None）。"""
    sls = wardb.supplylines()
    if not sls:
        return None
    sl = sls[0]
    b1 = warmap_view._ap_battlefield(sl.get('ap1'))
    b2 = warmap_view._ap_battlefield(sl.get('ap2'))
    rows = warmap_view.msg_supplylinestatus_rows(9, {b1: 1, b2: 1})
    return rows[0] if rows else None


def _ap_rows():
    aps = wardb.accesspoints()
    if not aps:
        return None
    ap = aps[0]
    rows = warmap_view.msg_accesspointstatus_rows(9, {ap.get('battlefield'): 2})
    return rows[0] if rows else None


# ---- case 表：消息名（官方 proto）→ 构建 bytes 的工厂（返回 None = SKIP）----
CASES = [
    # war 大地图 / battle 行（warmap_view + battle_msgs）
    ('battlefieldstatus',
     lambda: warmap_view.msg_battlefieldstatus_row(1234567890123, 77, 3)),
    ('supplylinestatus', _sl_rows),
    ('accesspointstatus', _ap_rows),
    ('CommandNodeDefinition',
     lambda: warmap_view.msg_commandnode_definition_row(AT)),
    ('CommandNodeWarInstance',
     lambda: warmap_view.msg_commandnode_instance_row(AT, 9, {})),
    ('CommandNodeMorale',
     lambda: warmap_view.msg_commandnode_morale_row(AT, 9)),
    ('armyresource',
     lambda: warmap_view.msg_armyresource_rows(AT, 9)[0]),
    ('battle', lambda: battle_msgs.msg_battle_row(WB)),
    ('missionresource',
     lambda: battle_msgs.msg_missionresource_row(AT, 501, 9)),
    ('transport_commandnode_segment',
     lambda: battle_msgs.msg_transport_segment_row(AT, 9, MV, SEG)),
    ('transport_commandnode_destination',
     lambda: battle_msgs.msg_transport_destination_row(
         AT, 9, MV, [SEG], now_ms=1756733000000)),
    ('air_transport',
     lambda: battle_msgs.msg_air_transport_row(AT, 9, MV, SEG)),
    # SOLDIER_VIEW 装备行（messages._msg_*）
    ('character',
     lambda: messages._msg_character(
         name='Conformance', faction=2, template=1, rank=7, char_id=1001,
         combat_badge_0=55, combat_badge_1=0, stat_update_ts=1756732800000,
         uniform_variant_id=900, headgear_id=901, marked_deleted=0,
         previous_template=3, head_id=89, head_swap_type=1, is_on_leave=0,
         unlock_bits=[True, False, True])),
    ('weapon',
     lambda: messages._msg_weapon(
         2001, 'Test Rifle', 4401, 1001, container_id=1001, container_slot=2,
         variant_id=3001, clip_count=-2, auto_repair=1, condition=87.5)),
    ('WeaponModifier',
     lambda: messages._msg_weapon_modifier(
         2101, 5501, 1001, 2001, auto_repair=1, condition=0.87)),
    ('WeaponAmmo',
     lambda: messages._msg_weapon_ammo(
         2201, 6601, 1001, 2001, slot_index=1, condition=0.87, auto_repair=0)),
    ('WeaponVariant',
     lambda: messages._msg_weapon_variant(3001, 1001, 2001, 7701)),
    ('Vehicle',
     lambda: messages._msg_vehicle(
         4001, 8801, 1001, equipped_in_slot=1, variant_id=5001,
         auto_repair=1, condition=66.0)),
    ('VehicleVariant',
     lambda: messages._msg_vehicle_variant(5001, 1001, 4001, 9901)),
    ('VehicleUpgrade',
     lambda: messages._msg_vehicle_upgrade(
         4101, 9951, 4001, 2, 1001, condition=100.0, auto_repair=0)),
    ('VehicleAmmo',
     lambda: messages._msg_vehicle_ammo(
         4201, 6651, 4001, 1, 0, 1001, condition=100.0, auto_repair=0)),
    ('UniformVariant',
     lambda: messages._msg_uniform_variant(6001, 11001, 1001)),
    ('UniformPart',
     lambda: messages._msg_uniform_part(6101, 1001, 11101, 1)),
    ('TracerEffect',
     lambda: messages._msg_tracer_effect(
         7001, 12001, 1001, 2001, is_equipped=True)),
    ('UnlockNotification',
     lambda: messages._msg_unlock_notification(
         1, PID, 1001, 12345, 101, 3)),
    ('DepotNewItem',
     lambda: messages._msg_depot_new_item(
         1, PID, 'weapons_2001,2002|vehicles_4001')),
    # statserver 协议族
    ('Ribbon', lambda: messages._msg_ribbon(101, 123456)),
]


def _cls(name):
    return getattr(hng, name, None) or getattr(sreq, name, None)


_INT_NEG_WRAP = 1 << 63


def _scalar(f, v):
    """pb_parse_multi 单值 → 官方字段 Python 值（按 FieldDescriptor 类型）。"""
    if f.type == f.TYPE_FLOAT:                 # wt5 4B
        return struct.unpack('<f', v)[0]
    if f.type == f.TYPE_DOUBLE:                # wt1 8B
        return struct.unpack('<d', v)[0]
    if f.type == f.TYPE_SFIXED64:
        return struct.unpack('<q', v)[0]
    if f.type == f.TYPE_FIXED64:
        return struct.unpack('<Q', v)[0]
    if f.type == f.TYPE_SFIXED32:
        return struct.unpack('<i', v)[0]
    if f.type == f.TYPE_FIXED32:
        return struct.unpack('<I', v)[0]
    if f.type == f.TYPE_STRING:
        return v.decode('utf-8')
    if f.type == f.TYPE_BYTES:
        return v
    # varint 族（int32/int64/uint32/uint64/bool/enum）：负数按补码折返
    if isinstance(v, int) and v >= _INT_NEG_WRAP:
        v -= 1 << 64
    if f.type == f.TYPE_BOOL:
        return bool(v)
    return v


def _repeated_fields(cls):
    """官方 schema 中 repeated 标量字段名集合（upb FieldDescriptor 无
    label 属性，实例化探测 MutableSequence）。"""
    m = cls()
    return {f.name for f in cls.DESCRIPTOR.fields
            if isinstance(getattr(m, f.name, None),
                          collections.abc.MutableSequence)}


def _rebuild_kwargs(cls, dec):
    """我们解码 dict → 官方消息构造 kwargs（嵌套 message 字段跳过）。"""
    rep = _repeated_fields(cls)
    kw = {}
    for f in cls.DESCRIPTOR.fields:
        if f.number not in dec or f.message_type is not None:
            continue
        raw = dec[f.number]
        if f.name in rep:
            kw[f.name] = [_scalar(f, v) for v in raw]
        else:
            kw[f.name] = _scalar(f, raw[0])
    return kw


def _flat(msg):
    """ListFields → 可比较的 [(field_number, value)]（嵌套 message 跳过）。"""
    out = []
    for f, v in msg.ListFields():
        if f.message_type is not None:
            continue
        if isinstance(v, (list, tuple)):
            out.append((f.number, tuple(v)))
        else:
            out.append((f.number, v))
    return sorted(out, key=lambda t: t[0])


def run_case(name, build):
    """三层验证，返回 (结果, 说明)：结果 in PASS/FAIL/SKIP。"""
    cls = _cls(name)
    if cls is None:
        return 'FAIL', '官方 proto 缺消息类 ' + name
    ours = build()
    if not ours:
        return 'SKIP', '构建为空（WarDatabase 表无数据）'
    try:
        m1 = cls()
        m1.ParseFromString(ours)               # L1 wire 合规
    except Exception as e:
        return 'FAIL', 'L1 ParseFromString: %s' % e
    dec = pb_parse_multi(ours)
    try:
        m2 = cls(**_rebuild_kwargs(cls, dec))  # L2 解码重建
    except Exception as e:
        return 'FAIL', 'L2 rebuild: %s' % e
    if _flat(m1) != _flat(m2):
        return 'FAIL', ('L2 字段不等价 ours=%s rebuilt=%s'
                        % (_flat(m1), _flat(m2)))
    m3 = cls()                                 # L3 官方编码 → 我们解码
    m3.ParseFromString(m2.SerializeToString())
    dec3 = pb_parse_multi(m3.SerializeToString())
    if dec3 != dec:
        return 'FAIL', ('L3 解码不一致 ours=%s official=%s'
                        % (dec, dec3))
    return 'PASS', '%dB / %d 字段' % (len(ours), len(_flat(m1)))


def main():
    npass = nskip = 0
    fails = []
    for name, build in CASES:
        res, note = run_case(name, build)
        print('  [%s] %-32s %s' % (res, name, note))
        if res == 'PASS':
            npass += 1
        elif res == 'SKIP':
            nskip += 1
        else:
            fails.append(name)
    total = len(CASES)
    print('\nconformance round-trip：%d/%d PASS，%d SKIP，%d FAIL'
          % (npass, total, nskip, len(fails)))
    if fails:
        print('FAIL 明细：', ', '.join(fails))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
