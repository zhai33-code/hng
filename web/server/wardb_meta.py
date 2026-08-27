# -*- coding: utf-8 -*-
"""WarDatabase 模板元数据：template_id -> 名称/父模板 映射。

角色装备明细的显示基础：DB 表存 template_id（weapons.template_id、
vehicles.vehicle_variant_template_id 等），名称在客户端 WarDatabase 模板表里。
启动时解析各模板表并缓存；解析失败降级返回原始 id（不阻断管理端）。

Chunk/pb 解码原语精简自 hng-re/protocol.py（保持管理端不 import 游戏服代码）。
字段号经实际探测固化（2026-08-27）：
  weapontemplate          f1=id  f2=name
  vehicletemplate          f1=id  f2=name
  VehicleVariantTemplate  f1=id  f2=name  f3=parent(vehicletemplate id)
  WeaponModifierTemplate  f1=id  f2=name
  ammotemplate            f1=id  f3=name
  ribbontemplate          f1=id  f2=name
  charactertemplate       f1=id  f2=name
  factiontemplate         f1=id  f2=name  f3=short
  combatbadgetemplate     f1=id  f3=name
  ExperienceLevels        f2=逗号分隔军衔经验阈值
"""
import os
import struct
import sys

from . import config

# ---------------------------------------------------------------- chunk codec


class Chunk:
    __slots__ = ('data', 'subs')

    def __init__(self, data=b'', subs=None):
        self.data = data
        self.subs = subs if subs is not None else []

    @staticmethod
    def parse(buf, off=0):
        if off + 4 > len(buf):
            return None, off
        total = struct.unpack_from('<i', buf, off)[0]
        if total < 8 or off + total > len(buf):
            return None, off
        self_size = struct.unpack_from('<i', buf, off + 4)[0]
        data_end = off + 8 + (self_size - 4)
        c = Chunk(buf[off + 8:data_end])
        pos = data_end
        remaining = total - 4 - self_size
        while remaining > 0:
            sub, nxt = Chunk.parse(buf, pos)
            if sub is None:
                break
            c.subs.append(sub)
            remaining -= nxt - pos
            pos = nxt
        return c, off + total


# ---------------------------------------------------------------- protobuf wire


def _rd_varint(buf, off):
    val = 0
    shift = 0
    while True:
        b = buf[off]
        off += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, off
        shift += 7


def pb_parse(b):
    fields = {}
    i = 0
    while i < len(b):
        tag, i = _rd_varint(b, i)
        fn, wt = tag >> 3, tag & 7
        if wt == 0:
            val, i = _rd_varint(b, i)
        elif wt == 1:
            val = b[i:i + 8]
            i += 8
        elif wt == 2:
            ln, i = _rd_varint(b, i)
            val = b[i:i + ln]
            i += ln
        elif wt == 5:
            val = b[i:i + 4]
            i += 4
        else:
            raise ValueError(f'unsupported wiretype {wt}')
        fields[fn] = val
    return fields


def _entries(table):
    """root.subs[0].subs -> [pb fields dict]；文件缺失/异常返回 []。"""
    path = f'{config.WARDDB_DIR}/{table}'
    try:
        with open(path, 'rb') as f:
            root, _ = Chunk.parse(f.read(), 0)
        return [pb_parse(t.subs[0].data) for t in root.subs[0].subs]
    except (OSError, IndexError, ValueError, AttributeError):
        return []


def _s(v):
    if isinstance(v, bytes):
        try:
            return v.decode('utf-8')
        except UnicodeDecodeError:
            return v.decode('utf-8', 'replace')
    return str(v)


# ---------------------------------------------------------------- 映射表


WEAPON_NAMES: dict[int, str] = {}            # weaponTemplate id -> name
VEHICLE_VARIANTS: dict[int, dict] = {}       # variant id -> {name, parent}
VEHICLE_NAMES: dict[int, str] = {}           # vehicleTemplate id -> name
MODIFIER_NAMES: dict[int, str] = {}          # weaponModifierTemplate id -> name
AMMO_NAMES: dict[int, str] = {}              # ammotemplate id -> name
RIBBON_NAMES: dict[int, str] = {}            # ribbontemplate id -> name
CHARACTER_NAMES: dict[int, str] = {}         # charactertemplate id -> name
FACTION_NAMES: dict[int, str] = {}           # factiontemplate id -> name
BADGE_NAMES: dict[int, str] = {}             # combatbadgetemplate id -> name
RANK_THRESHOLDS: list[int] = []              # 军衔经验阈值（升序）


def load():
    """启动时加载全部映射；逐表独立容错。"""
    global RANK_THRESHOLDS
    if not config.GAME_DIR:
        return
    if not os.path.isdir(config.WARDDB_DIR):
        # .env 常是从服务器拷来的（HNG_GAME_DIR 为 Windows 路径），本机无效属预期
        print(f'[warn] GAME_DIR 无效（{config.GAME_DIR}）：装备名称将降级为 #id 显示。'
              '本机开发可忽略；服务器部署需确认 HNG_GAME_DIR。',
              file=sys.stderr)
        return
    for e in _entries('weapontemplate'):
        WEAPON_NAMES[int(e.get(1, 0))] = _s(e[2]) if 2 in e else ''
    for e in _entries('VehicleVariantTemplate'):
        VEHICLE_VARIANTS[int(e.get(1, 0))] = {
            'name': _s(e[2]) if 2 in e else '',
            'parent': int(e.get(3, 0)),
        }
    for e in _entries('vehicletemplate'):
        VEHICLE_NAMES[int(e.get(1, 0))] = _s(e[2]) if 2 in e else ''
    for e in _entries('WeaponModifierTemplate'):
        MODIFIER_NAMES[int(e.get(1, 0))] = _s(e[2]) if 2 in e else ''
    for e in _entries('ammotemplate'):
        AMMO_NAMES[int(e.get(1, 0))] = _s(e[3]) if 3 in e else ''
    for e in _entries('ribbontemplate'):
        RIBBON_NAMES[int(e.get(1, 0))] = _s(e[2]) if 2 in e else ''
    for e in _entries('charactertemplate'):
        CHARACTER_NAMES[int(e.get(1, 0))] = _s(e[2]) if 2 in e else ''
    for e in _entries('factiontemplate'):
        FACTION_NAMES[int(e.get(1, 0))] = _s(e[2]) if 2 in e else ''
    for e in _entries('combatbadgetemplate'):
        BADGE_NAMES[int(e.get(1, 0))] = _s(e[3]) if 3 in e else ''
    try:
        lv = _entries('ExperienceLevels')[0]
        RANK_THRESHOLDS = [int(x) for x in _s(lv[2]).split(',') if x]
    except (IndexError, KeyError, ValueError):
        RANK_THRESHOLDS = []


def rank_from_xp(xp: int) -> int:
    """军衔等级 = combat_xp 经 ExperienceLevels 阈值推导（rank 0 起）。"""
    rank = 0
    for t in RANK_THRESHOLDS:
        if xp >= t:
            rank += 1
        else:
            break
    return rank


def weapon_name(template_id: int) -> str:
    return WEAPON_NAMES.get(template_id, f'#{template_id}')


def vehicle_info(variant_template_id: int) -> dict:
    v = VEHICLE_VARIANTS.get(variant_template_id)
    if not v:
        return {'name': f'#{variant_template_id}', 'parent': 0,
                'parent_name': ''}
    return {'name': v['name'],
            'parent': v['parent'],
            'parent_name': VEHICLE_NAMES.get(v['parent'], '')}


def modifier_name(template_id: int) -> str:
    return MODIFIER_NAMES.get(template_id, f'#{template_id}')


def ammo_name(template_id: int) -> str:
    return AMMO_NAMES.get(template_id, f'#{template_id}')


def ribbon_name(template_id: int) -> str:
    return RIBBON_NAMES.get(template_id, f'#{template_id}')


def character_name(template_id: int) -> str:
    return CHARACTER_NAMES.get(template_id, f'#{template_id}')


def faction_name(faction_id: int) -> str:
    return FACTION_NAMES.get(faction_id, str(faction_id))


def badge_name(template_id: int) -> str:
    return BADGE_NAMES.get(template_id, f'#{template_id}')
