# -*- coding: utf-8 -*-
"""WarDatabase 静态数据层：解析游戏静态表，构建解锁/定价/勋带/经验等索引。

模块 import 时即解析 WarDatabase 目录（与原 realtest_server 行为一致）。
依赖 protocol(Chunk/pb_parse) 与 state(WARDDB_DIR/log)。
"""
import hashlib
import os
import random
import struct

from protocol import Chunk, pb_int32, pb_int64, pb_parse, pb_string
from state import WARDDB_DIR, log

# ---- 角色解锁位图（默认解锁序列 + 已购物品解锁需求）----
# EUnlockItemType: eWeapon=2, eWeaponModMapping=3, eVehicle=4, eWeaponVariant=14, eWeaponAmmoMapping=10, eVehiclesVariant=15
_E_WEAPON = 2
_E_WEAPON_MOD_MAPPING = 3
_E_VEHICLE = 4
_E_VEHICLE_UPGRADE_MAPPING = 8
_E_WEAPON_AMMO_MAPPING = 10
_E_VEHICLES_VARIANT = 15

# 从 WarDatabase 提取的有效解锁 ID（避免客户端查找不存在的条目导致 null 引用）
try:
    from unlock_ids import PROGRESSION_UNLOCK_IDS, PLAYER_LEVEL_UNLOCK_IDS
    _prog_max = max(PROGRESSION_UNLOCK_IDS) if PROGRESSION_UNLOCK_IDS else 0
    _player_max = max(PLAYER_LEVEL_UNLOCK_IDS) if PLAYER_LEVEL_UNLOCK_IDS else 0
    _prog_set = set(PROGRESSION_UNLOCK_IDS)
    # 角色模板解锁的 PlayerLevelUnlock id（unlockItemType==eCharacterTemplate）：
    # 7=伞兵(lv6) 9=侦察(lv9) 11=坦克兵(lv5) 12=飞行员(lv8) 26=将军(lv12)。
    # 按等级置位后客户端为每个阵营×已解锁兵种生成"可招募"占位士兵（fake
    # slot），点击弹输入名字 → CreateCharacterRequest（按定价扣银币建号，
    # 服务器 CHARACTER_PACKS 全支持）。将军（26）例外：将军不随等级赠送
    # （官方需 17 级军衔角色 + 单独解锁链路），恒 false。
    _PLAYER_CHARACTER_TEMPLATE_UNLOCKS = {7, 9, 11, 12, 26}
    _PLAYER_GENERAL_UNLOCK_ID = 26
    _player_set = set(PLAYER_LEVEL_UNLOCK_IDS) - _PLAYER_CHARACTER_TEMPLATE_UNLOCKS
    _char_unlock_bits = [i in _prog_set for i in range(_prog_max + 1)]
    _player_unlock_bits = [i in _player_set for i in range(_player_max + 1)]
except ImportError:
    _prog_max = 2854
    _player_max = 40
    _char_unlock_bits = [True] * 2855
    _player_unlock_bits = [True] * 41

# pack_id -> (factionTemplateId, characterTemplateId, rank)
try:
    from character_packs import CHARACTER_PACKS
except ImportError:
    CHARACTER_PACKS = {}


def _parse_wardb_table(name):
    """解析 WarDatabase 表文件 → [pb_parse dict, ...]（KeyValueChangeSet -> set -> entries）"""
    fpath = os.path.join(WARDDB_DIR, name)
    try:
        with open(fpath, 'rb') as f:
            root, _ = Chunk.parse(f.read(), 0)
        return [pb_parse(t.subs[0].data) for t in root.subs[0].subs]
    except Exception as e:
        log(f'[!] parse wardb table {name} failed: {e}')
        return []


def _build_unlock_maps():
    """构建解锁映射：
    - unlock_by_item[(itemId, itemType)] -> set(progressionUnlock ids)
    - char_default_unlocks[(charTemplate, faction)] -> set(progressionUnlock ids)
    - mod_mapping_ids[(modifierTemplateId, weaponTemplateId)] -> WeaponModMapping.id
    """
    unlock_by_item = {}
    for f in _parse_wardb_table('progressionUnlock'):
        uid = int(f.get(1, 0))
        itype = int(f.get(7, 0))
        iid = int(f.get(8, 0))
        unlock_by_item.setdefault((iid, itype), set()).add(uid)

    char_default = {}
    for f in _parse_wardb_table('CharacterUnlockTemplate'):
        ct = int(f.get(2, 0))
        ft = int(f.get(3, 0))
        seq_raw = f.get(4, b'')
        if isinstance(seq_raw, bytes):
            seq_raw = seq_raw.decode('utf-8', 'replace')
        ids = {int(x) for x in str(seq_raw).split(',') if x.strip().isdigit()}
        char_default[(ct, ft)] = ids

    mod_map = {}
    mod_by_weapon = {}
    for f in _parse_wardb_table('WeaponModMapping'):
        mid = int(f.get(1, 0))
        mdt = int(f.get(2, 0))
        wt = int(f.get(3, 0))
        mod_map[(mdt, wt)] = mid
        mod_by_weapon.setdefault(wt, []).append(mid)

    # weaponAmmoMapping { id=1, weaponTemplateId=2, ammoTemplateId=3, defaultAmmo=4 }
    ammo_by_weapon = {}
    ammo_map = {}
    for f in _parse_wardb_table('weaponAmmoMapping'):
        mid = int(f.get(1, 0))
        wt = int(f.get(2, 0))
        at = int(f.get(3, 0))
        default = bool(f.get(4, 0))
        ammo_by_weapon.setdefault(wt, []).append((mid, at, default))
        ammo_map[(at, wt)] = mid
    return unlock_by_item, char_default, mod_map, mod_by_weapon, ammo_by_weapon, ammo_map


_UNLOCK_BY_ITEM, _CHAR_DEFAULT_UNLOCKS, _MOD_MAPPING_IDS, _MOD_MAPPINGS_BY_WEAPON, \
    _AMMO_MAPPINGS_BY_WEAPON, _AMMO_MAPPING_IDS = _build_unlock_maps()

# uid → (factionTemplateId, characterType)：解锁校验按角色阵营/职业过滤需求 uid。
# 对齐客户端 hasCharacterUnlockedItem（getUnlocksRequiredForItem 按 faction 过滤、
# 匹配时 characterType == 角色模板或 0）。避免 RGD-33 等多阵营物品因其他阵营的
# uid 不在角色 bits 而被服务器误拒绝（如 RGD-33 三阵营 uid 261/262/301，苏联角色
# 只该满足 301）。
_UNLOCK_UID_META = {}
for _f in _parse_wardb_table('progressionUnlock'):
    _UNLOCK_UID_META[int(_f.get(1, 0))] = (int(_f.get(3, 0)), int(_f.get(4, 0)))

# unlockMetaLevel == 0 的条目不是购买门槛：专属勋带关联（如 Yak-9 的 uid 1401
# ribbon=68 "Yak-9 Specialist" level=0，共 700+ 条遍布武器/载具/军服/升级各类型），
# 语义是"使用该物品累计这条勋带"，勋带 0 级人人都算有——服务器把 level=0 的
# uid 直接放进角色 unlockBits（player_service._char_unlock_bits_for，无 XP 行的
# 勋带视为 0 级），客户端 hasCharacterUnlockedItem 纯按位图判定（无 level 0
# 特判），未拥有物品在商店也显示可购买。
# 购买校验一律用 _REQ_UNLOCK_BY_ITEM（剔除 level=0 条目），否则任何飞机的第一架
# 都会因"未拥有本机专属勋带"被错误拒绝（鸡生蛋问题）；授予位图仍用全量 _UNLOCK_BY_ITEM。
_LEVEL0_UIDS = {
    int(_f.get(1, 0)) for _f in _parse_wardb_table('progressionUnlock')
    if int(_f.get(6, 0)) == 0
}
_REQ_UNLOCK_BY_ITEM = {}
for (_iid, _itype), _uids in _UNLOCK_BY_ITEM.items():
    _req = _uids - _LEVEL0_UIDS
    if _req:
        _REQ_UNLOCK_BY_ITEM[(_iid, _itype)] = _req

# ShopMembershipItems { id=1, identifier=2, durationDays=3, ... }
# 会员商品时长（天）：AddMembershipRequest membershipTemplateId → durationDays
# （买 180 天老兵必须按表发 180 天，不能统一 30 天）
MEMBERSHIP_DAYS_BY_ID = {
    int(f.get(1, 0)): int(f.get(3, 0))
    for f in _parse_wardb_table('ShopMembershipItems') if int(f.get(1, 0))
}

# RibbonBoosterTemplate { id=1, ribbonId=2, minutes=3, multiplier=4(fixed32) }
# 勋带加成时长（分钟）：按模板发（24h 券不能发成 4h），WarDatabase 缺失时空表
# → 调用方回退 _BOOSTER_MINUTES
BOOSTER_MINUTES_BY_ID = {
    int(f.get(1, 0)): int(f.get(3, 0))
    for f in _parse_wardb_table('RibbonBoosterTemplate') if int(f.get(1, 0))
}

# 可购买物品类 unlockItemType 的全量解锁 uid 集合（白名单式统一剔除）。
# CharacterUnlockTemplate.unlockSequence 实际是该角色类型「全部勋带路径条目」
# 的清单——物品类 uid 一律不能随默认序列授予。历史四轮同根因泄漏：
# PPD-40/Bazooka（武器 uid=299/339）、L-41 弹药映射（uid=529）、Field Trigger
# 配件映射（uid=516）、ammo crate 载具升级映射（uid=87/94/122）、步兵勋带2级
# 战斗徽章（uid=50）。物品解锁只走三条正路：STARTING_GEAR 起始装备 /
# 勋带 XP 等级（unlockMetaLevel <= level）/ 已购物品实例。
# 非物品类保留默认授予：1=eCharacterType 5=eDiscount 6=eAbility
# 11=eCommandPoints（将军默认序列含 3 个）12=eSquadCustomizationPoints；
# 18/19 未知类型不在任何默认序列中，不处理。
_E_ITEM_TYPES = frozenset({
    _E_WEAPON,                    # 2 武器
    _E_WEAPON_MOD_MAPPING,        # 3 配件映射
    _E_VEHICLE,                   # 4 载具
    7,                            # eCombatBadge 战斗徽章
    _E_VEHICLE_UPGRADE_MAPPING,   # 8 载具升级映射
    _E_WEAPON_AMMO_MAPPING,       # 10 弹药映射
    14,                           # eWeaponVariant 武器变体
    _E_VEHICLES_VARIANT,          # 15 载具变体
    16,                           # eUniformVariant 军服变体
    17,                           # eUniformPart 军服部件
})
_ITEM_UNLOCK_UIDS = set()
for (_iid, _itype), _uids in _UNLOCK_BY_ITEM.items():
    if _itype in _E_ITEM_TYPES:
        _ITEM_UNLOCK_UIDS |= _uids


# 配件/弹药映射 → (勋带模板 id, 所需勋带等级)：购买配件/弹药时校验角色勋带等级，
# 防止客户端越级购买（镜子/扳机/高级弹药需先购买对应勋带等级）。
# progressionUnlock { uid=1, unlockMetaName=5(勋带模板), unlockMetaLevel=6(所需等级),
#   unlockItemType=7, unlockItemId=8(映射 id) }
def _build_mapping_req_levels():
    mod_req = {}
    ammo_req = {}
    for f in _parse_wardb_table('progressionUnlock'):
        itype = int(f.get(7, 0))
        if itype not in (_E_WEAPON_MOD_MAPPING, _E_WEAPON_AMMO_MAPPING):
            continue
        iid = int(f.get(8, 0))
        meta = f.get(5, b'')
        if isinstance(meta, bytes):
            meta = meta.decode('utf-8', 'replace')
        if not meta.strip().isdigit():
            continue
        level = int(f.get(6, 0))
        dst = mod_req if itype == _E_WEAPON_MOD_MAPPING else ammo_req
        cur = dst.get(iid)
        if cur is None or level < cur[1]:
            dst[iid] = (int(meta), level)
    return mod_req, ammo_req


_MOD_REQ_LEVEL, _AMMO_REQ_LEVEL = _build_mapping_req_levels()


# WeaponModifierTemplate { id=1, ..., weaponModCategoryId=28, ... } +
# WeaponModCategory { id=1, name=2, weaponClassId=3, slotIndex=4 }：
# 每个配件模板的槽位分类。基础分类 1=Ammunition 2=Barrel 3=Internal 4=Sight
# 5=Trigger；11-75 为各枪械子类的同名分类（weaponClassId=11..17），f4 即槽位类型。
# 同武器同槽位只允许装备一个配件（客户端 ModifyWeaponMediator 按槽位互斥渲染，
# 与弹药 weaponAmmoMapping 一枪一发同理）——服务器装备配件时按此互斥卸下旧的。
_MOD_CATEGORY_BY_TEMPLATE = {}
for _f in _parse_wardb_table('WeaponModifierTemplate'):
    _MOD_CATEGORY_BY_TEMPLATE[int(_f.get(1, 0))] = int(_f.get(28, 0))

_MOD_CATEGORY_SLOT = {}
for _f in _parse_wardb_table('WeaponModCategory'):
    _MOD_CATEGORY_SLOT[int(_f.get(1, 0))] = int(_f.get(4, 0))


# ammotemplate { id=1, ..., maxCondition=34 }：弹药满耐久值（新弹药/修复后 condition）
def _build_ammo_maxcond():
    m = {}
    for f in _parse_wardb_table('ammotemplate'):
        aid = int(f.get(1, 0))
        raw = f.get(34, b'')
        if isinstance(raw, bytes) and len(raw) == 4:
            m[aid] = struct.unpack('<f', raw)[0]
    return m


_AMMO_MAXCOND = _build_ammo_maxcond()


# vehicleupgradetemplate { id=1, ..., maxCondition=6 }: 载具升级满耐久值（新购/修复后 condition）
def _build_vehicle_upgrade_maxcond():
    m = {}
    for f in _parse_wardb_table('vehicleupgradetemplate'):
        tid = int(f.get(1, 0))
        raw = f.get(6, b'')
        if isinstance(raw, bytes) and len(raw) == 4:
            m[tid] = struct.unpack('<f', raw)[0]
    return m


_VEHICLE_UPGRADE_MAXCOND = _build_vehicle_upgrade_maxcond()


# VehicleUpgradeSlot { id=1, vehicleTemplateId=2, vehicleUpgradeTemplateId=3 }：
# (vehicleTemplateId, upgradeTemplateId) -> slot id。客户端按 parentVehicleTemplateId +
# upgradeTemplateId 查槽位，解锁校验（eVehicleUpgradeMapping=8）也按 slot id 查需求 uid。
_VEHICLE_UPGRADE_SLOT = {}
for _f in _parse_wardb_table('VehicleUpgradeSlot'):
    _VEHICLE_UPGRADE_SLOT.setdefault((int(_f.get(2, 0)), int(_f.get(3, 0))),
                                     int(_f.get(1, 0)))


# VehicleVariantTemplate { id=1, ..., parentVehicleTemplateId=3 }: 载具变体 -> 基础载具模板
_VEHICLE_VARIANT_PARENT = {}
for _f in _parse_wardb_table('VehicleVariantTemplate'):
    _VEHICLE_VARIANT_PARENT[int(_f.get(1, 0))] = int(_f.get(3, 0))


# ===== 装备耐久/磨损静态表（客户端 WeaponUtilities/VehicleUtilities 逆向确认）=====
# 量纲：武器与配件 maxCondition=1.0（0~1 比例，按每发磨损 wearPerShot）；
# 载具/载具升级 maxCondition 为点数（25~220），按每次 spawn 磨损 wearPerSpawn；
# 弹药 maxCondition 即满弹发数，wearPerShot 即每发消耗（1.0/0.5/0）。
# 字段号（HnG_States/*.as）：
#   weapontemplate          f61 maxCondition / f62 wearPerShot / f63 wearPerBattle
#   weaponmodifiertemplate  f23 maxCondition / f24 wearPerShot / f25 wearPerBattle
#   ammotemplate            f34 maxCondition / f35 wearPerShot / f36 wearPerBattle
#   vehicletemplate         f35 maxCondition / f36 wearPerSpawn / f37 wearPerBattle
#   vehicleupgradetemplate  f6  maxCondition / f7  wearPerSpawn / f8  wearPerBattle
def _build_wear_map(table, f_max, f_shot, f_battle):
    m = {}
    for f in _parse_wardb_table(table):
        tid = int(f.get(1, 0) or 0)
        if not tid:
            continue

        def fv(n):
            raw = f.get(n, b'')
            return struct.unpack('<f', raw)[0] \
                if isinstance(raw, bytes) and len(raw) == 4 else 0.0
        m[tid] = (fv(f_max), fv(f_shot), fv(f_battle))
    return m


_WEAPON_WEAR = _build_wear_map('weapontemplate', 61, 62, 63)
_WEAPON_MOD_WEAR = _build_wear_map('WeaponModifierTemplate', 23, 24, 25)
_AMMO_WEAR = _build_wear_map('ammotemplate', 34, 35, 36)
_VEHICLE_WEAR = _build_wear_map('vehicletemplate', 35, 36, 37)
_VEHICLE_UPGRADE_WEAR = _build_wear_map('vehicleupgradetemplate', 6, 7, 8)


def weapon_wear_params(template_id):
    """weapontemplate → (maxCondition, wearPerShot, wearPerBattle)；未知返回 None。"""
    return _WEAPON_WEAR.get(int(template_id or 0))


def weapon_modifier_wear_params(template_id):
    """WeaponModifierTemplate → (maxCondition, wearPerShot, wearPerBattle)；未知返回 None。"""
    return _WEAPON_MOD_WEAR.get(int(template_id or 0))


def ammo_wear_params(template_id):
    """ammotemplate → (maxCondition, wearPerShot, wearPerBattle)；未知返回 None。"""
    return _AMMO_WEAR.get(int(template_id or 0))


def vehicle_wear_params(template_id):
    """vehicletemplate → (maxCondition, wearPerSpawn, wearPerBattle)；未知返回 None。"""
    return _VEHICLE_WEAR.get(int(template_id or 0))


def vehicle_upgrade_wear_params(template_id):
    """vehicleupgradetemplate → (maxCondition, wearPerSpawn, wearPerBattle)；未知返回 None。"""
    return _VEHICLE_UPGRADE_WEAR.get(int(template_id or 0))


def vehicle_wear_params_for_variant(variant_template_id):
    """载具变体模板 → 基础载具模板磨损参数（vehicletemplate f35-f37）；
    变体 id 自身出现在 vehicletemplate 时兜底直查，均查不到返回 None。"""
    parent = _VEHICLE_VARIANT_PARENT.get(int(variant_template_id or 0), 0)
    return _VEHICLE_WEAR.get(parent) or \
        _VEHICLE_WEAR.get(int(variant_template_id or 0))


def vehicle_full_condition(variant_template_id):
    """载具变体模板 → 基础载具模板的满耐久（vehicletemplate f35）。
    新购/起始载具 condition 的单源；变体无父或父表缺失回退 100.0。"""
    params = vehicle_wear_params_for_variant(variant_template_id)
    return params[0] if params and params[0] > 0 else 100.0


# VehicleWeaponSlot { id=1, vehicleTemplateId=2, weaponTemplateId=3, firingSequence=4,
#   weaponSlotUser=5(eDriver=1), vehicleAmmoStorageId=6, weaponPriority=7(ePrimaryWeapon=0) }
# 载具弹药归属链：载具变体 -> parentVehicleTemplateId -> VehicleWeaponSlot(驾驶员主武器)
# -> weaponAmmoMapping(weaponTemplateId -> ammoTemplateId)。客户端 VehicleUtilities
# getAvailableVehicleAmmoStorage 按同样链路找弹药仓。
# _VEHICLE_WEAPON_SLOTS[parent] = [(weaponTemplateId, ammoStorageId), ...]（仅驾驶员主武器）
_VEHICLE_WEAPON_SLOTS = {}
for _f in _parse_wardb_table('VehicleWeaponSlot'):
    _user = int(_f.get(5, 0))
    _prio = int(_f.get(7, 0))
    if _user != 1 or _prio != 0:      # eDriver=1, ePrimaryWeapon=0
        continue
    _VEHICLE_WEAPON_SLOTS.setdefault(int(_f.get(2, 0)), []).append(
        (int(_f.get(3, 0)), int(_f.get(6, 0))))


# VehicleAmmoStorage { id=1, clipCount=2, storageSlots=3 }：弹药仓默认出生弹夹数。
# 官方回放锚点（pvt_player_settings.bin）：T-38 同轴 DT（storage 56）spawnClipCount=21、
# Yak-9D ShVAK（storage 72）=1、R-Z 后机枪（storage 114）=1，均等于本表 f2 clipCount。
# 客户端备弹 = spawnClipCount × weapontemplate f20 clipsize × 炮数
# （La-7 B-20：1 × 100 × 3 = 300 发；此前误发 16 → 4800 发）。
_VEHICLE_AMMO_STORAGE_CLIPS = {int(f.get(1, 0)): int(f.get(2, 0) or 1)
                               for f in _parse_wardb_table('VehicleAmmoStorage')}


def vehicle_ammo_spawn_clips(storage_id):
    """弹药仓默认出生弹夹数（VehicleAmmoStorage f2 clipCount）；未知仓返回 1。"""
    return _VEHICLE_AMMO_STORAGE_CLIPS.get(int(storage_id), 1)


def _vehicle_supports_ammo(parent_vehicle_tid, ammo_template_id):
    """载具基础模板是否能装指定弹药（驾驶员主武器经 weaponAmmoMapping 推导）。"""
    for (wt, _storage) in _VEHICLE_WEAPON_SLOTS.get(int(parent_vehicle_tid), ()):
        for (_mid, at, _dflt) in _AMMO_MAPPINGS_BY_WEAPON.get(wt, ()):
            if at == int(ammo_template_id):
                return True
    return False


def _default_vehicle_ammo_for(parent_vehicle_tid):
    """返回载具基础模板的默认弹药映射列表 [(ammoStorageId, ammoTemplateId), ...]
    （defaultAmmo=true 的驾驶员主武器弹药，自动补建用）。
    同一 (storage, ammo) 会经多个武器槽重复命中，先集合去重。"""
    seen = set()
    out = []
    for (wt, storage) in _VEHICLE_WEAPON_SLOTS.get(int(parent_vehicle_tid), ()):
        for (_mid, at, dflt) in _AMMO_MAPPINGS_BY_WEAPON.get(wt, ()):
            if dflt and (storage, at) not in seen:
                seen.add((storage, at))
                out.append((storage, at))
    return out


# Pricing { id=1, type=2, objectId=3, costGold=4, costWarfunds=5, costCredits=6, relativeGoldValue=7 }
# 购买请求中的 pricingId 即 Pricing.id（客户端 PriceObject.priceId = pricing.id）
def _build_pricing_map():
    m = {}
    for f in _parse_wardb_table('Pricing'):
        pid = int(f.get(1, 0))
        def f4(b):
            return struct.unpack('<f', b)[0] if isinstance(b, bytes) and len(b) == 4 else 0.0
        m[pid] = {
            'type': (f.get(2, b'') or b'').decode('utf-8', 'replace'),
            # objectId：购买目标 id（兵种切换 pricing → 新 CharacterTemplate id）
            'object_id': int(f.get(3, 0) or 0),
            'gold': f4(f.get(4, b'')),
            'warfunds': f4(f.get(5, b'')),
            'credits': f4(f.get(6, b'')),
        }
    return m


_PRICING_MAP = _build_pricing_map()

# 维修定价索引：(type, objectId) -> Pricing 行。客户端 PriceModel.
# createItemMaintenancePurchaseOptions 按 HnGGUIGamePrices.TYPE_*_REPAIR +
# 模板 id 查价，维修价 = 基础价 x 损耗比例（PriceObject.calculateFractionPrice：
# max(1, int(base * (max-cur)/max))）。objectId：武器/配件/弹药/升级 = 各自
# 模板 id；载具 = 基础载具模板 id（变体的 parentVehicleTemplateId）。
_REPAIR_PRICING = {}
for _p in _PRICING_MAP.values():
    if 'Repair' in _p['type']:
        _REPAIR_PRICING[(_p['type'], _p['object_id'])] = _p


def repair_base_price(price_type, template_id, currency_key):
    """维修基础价（满耐久）：(定价类型, 模板 id) -> 该货币的基础价；缺失返回 0。"""
    p = _REPAIR_PRICING.get((price_type, int(template_id or 0)))
    return int(p[currency_key] or 0) if p else 0


def vehicle_parent_template_id(variant_template_id):
    """载具变体模板 -> 基础载具模板 id（无父或未知回退变体 id 自身）。"""
    vid = int(variant_template_id or 0)
    return _VEHICLE_VARIANT_PARENT.get(vid, 0) or vid

# VirtualCurrencyType：1=Credits 2=Warfunds 3=Gold 4=Tickets
_CUR_KEY = {1: 'credits', 2: 'warfunds', 3: 'gold'}
_CUR_NAME = {1: 'Credits', 2: 'Warfunds', 3: 'Gold', 4: 'Tickets'}


# RibbonLevelUpgradeTemplate { id=1, ribbonTemplateId=2, ribbonLevelId=3 }
def _build_ribbon_upgrades():
    m = {}
    for f in _parse_wardb_table('RibbonLevelUpgradeTemplate'):
        m[int(f.get(1, 0))] = (int(f.get(2, 0)), int(f.get(3, 0)))
    return m


_RIBBON_UPGRADES = _build_ribbon_upgrades()


# RibbonBoosterTemplate { id=1, ribbonTemplateId=2, durationInMinutes=3,
#   multiplier=4(fixed32 float), forSale=5 }（HnG_States/RibbonBoosterTemplate.as）：
# 勋带加成模板 → (目标勋带模板 id, 经验乘数)。charactersetting f5 ribbonBoosters
# {f1 ribbonTemplateId, f2 boosterMultiplier(fixed32)} 的下发源；
# 结算端 f13 ribbons endXp 由客户端按乘数放大后回报（服务端 set_ribbon_xp_floor 落库）
_RIBBON_BOOSTERS = {}
for _f in _parse_wardb_table('RibbonBoosterTemplate'):
    _rb_rid = int(_f.get(2, 0) or 0)
    _rb_raw = _f.get(4, b'')
    _rb_mul = struct.unpack('<f', _rb_raw)[0] \
        if isinstance(_rb_raw, bytes) and len(_rb_raw) == 4 else 0.0
    if _rb_rid and _rb_mul > 1.0:
        _RIBBON_BOOSTERS[int(_f.get(1, 0))] = (_rb_rid, _rb_mul)


# progressionUnlock { uid=1, unlockMetaName=5, unlockMetaLevel=6, ... }：
# 勋带等级购买后解锁该勋带（unlockMetaName==str(ribbonTemplateId)）下
# unlockMetaLevel <= 购买等级 的全部弹药/配件映射解锁需求
def _build_ribbon_meta_level():
    m = {}
    for f in _parse_wardb_table('progressionUnlock'):
        uid = int(f.get(1, 0))
        meta_name = f.get(5, b'')
        if isinstance(meta_name, bytes):
            meta_name = meta_name.decode('utf-8', 'replace')
        level = int(f.get(6, 0))
        m.setdefault(str(meta_name), set()).add((uid, level))
    return m


_RIBBON_META_LEVEL = _build_ribbon_meta_level()


# unlockType=2（EUnlockType.eRank 军衔）条目 → (uid, 所需军衔, unlockItemType)。
# metaName 恒 '0'（哨兵，非勋带 id）：解锁按角色 combat_xp 推导的军衔走，
# 如 uid 210 First Blood badge（rank 1）、uid 943 ability（rank 6）、
# uid 280-289 小队自定义点数（rank 12+）。旧实现只推导勋带等级（eRibbon
# 路径），rank 类全部漏掉 → First Blood 等永锁（2026-08-27 反馈）。
# 不含 itemType=1（eCharacterType 职业模板，uid 101/102/103/17）：
# 职业招募占位走 PlayerLevelUnlock（player bits）体系，保持不自动解锁。
def _build_rank_meta_level():
    m = set()
    for f in _parse_wardb_table('progressionUnlock'):
        if int(f.get(2, 0)) == 2 and int(f.get(7, 0)) != 1:
            m.add((int(f.get(1, 0)), int(f.get(6, 0)), int(f.get(7, 0))))
    return m


_RANK_META_LEVEL = _build_rank_meta_level()


# ---- 玩家等级（进展 tab：PlayerLevel / PlayerLevelUnlock）----
# PlayerLevel { id=1(等级), value=2(该级 totalScore 阈值) }：
# 客户端 PlayerSchema.playerLevel 按 totalScore(f36) 对阈值表取最大等级。
# PlayerLevelUnlock { id=1, unlockMetaLevel=2(等级), unlockItemType=3,
#   unlockItemId=4(数额/模板id), available=6, unlockable=7 }。
# EPlayerLevelUnlockItemType: eCharacterTemplate=1, eMissionType=2, eGoldBonus=3,
# eCreditsBonus=4, eWarfundsBonus=5, eMembershipItem=6, eWar=7, eWeaponGroup=8,
# eFactions=9, eMatchmakingGroup=10, eReferralBonus=11。
def _build_player_levels():
    return sorted((int(f.get(1, 0)), int(f.get(2, 0)))
                  for f in _parse_wardb_table('PlayerLevel'))


_PLAYER_LEVELS = _build_player_levels()


def player_level_from_score(score):
    """totalScore → 玩家等级（客户端 PlayerSchema.playerLevel 同款算法）。"""
    lvl = 0
    for lv, threshold in _PLAYER_LEVELS:
        if threshold <= int(score) and lv > lvl:
            lvl = lv
    return lvl


def _build_level_unlocks():
    """PlayerLevelUnlock → {等级: {'credits'/'gold'/'warfunds': 数额,
    'modes': [任务类型 id]}}。available && unlockable 才计入（客户端
    PlayerModel.addToUnlockLevel 同款过滤）。"""
    rewards = {}
    for f in _parse_wardb_table('PlayerLevelUnlock'):
        if not int(f.get(6, 0)) or not int(f.get(7, 0)):
            continue
        lv = int(f.get(2, 0))
        itype = int(f.get(3, 0))
        item = int(f.get(4, 0))
        if item < 0:
            item = 0
        ent = rewards.setdefault(lv, {'credits': 0, 'gold': 0,
                                      'warfunds': 0, 'modes': []})
        if itype == 3:            # eGoldBonus
            ent['gold'] += item
        elif itype == 4:          # eCreditsBonus
            ent['credits'] += item
        elif itype == 5:          # eWarfundsBonus
            ent['warfunds'] += item
        elif itype == 2 and item != 1:
            # eMissionType（1=默认 Assault，客户端 NotificationModel 跳过）
            ent['modes'].append(item)
    return rewards


_LEVEL_UNLOCKS = _build_level_unlocks()


def _build_player_level_unlock_meta():
    """PlayerLevelUnlock 行 id → (unlockMetaLevel, available)。
    供 player unlockBits 按等级置位。"""
    meta = {}
    for f in _parse_wardb_table('PlayerLevelUnlock'):
        uid = int(f.get(1, 0))
        if uid <= 0:
            continue
        meta[uid] = (int(f.get(2, 0)), int(f.get(6, 0)))
    return meta


_PLAYER_LEVEL_UNLOCK_META = _build_player_level_unlock_meta()
_PLAYER_LEVEL_UNLOCK_MAX = (max(_PLAYER_LEVEL_UNLOCK_META)
                            if _PLAYER_LEVEL_UNLOCK_META else 0)


def player_unlock_bits_for_level(level):
    """按玩家等级生成 player unlockBits（位索引 = PlayerLevelUnlock.id）。

    客户端 PlayerUtilities.hasPlayerLevelUnlock 读该位图门控 War 模式
    （eWar lv10）/匹配模式（eMissionType）/武器组（eWeaponGroup）等——
    全 true 会让低级玩家也解锁全部模式（进展限制失效的根因）。
    位 true = 条目 available 且 unlockMetaLevel <= level；
    将军（_PLAYER_GENERAL_UNLOCK_ID）恒 false（不随等级赠送）；
    其余兵种（坦克兵 lv5/伞兵 lv6/飞行员 lv8/侦察 lv9）按等级置位 →
    客户端生成"可招募"占位士兵，招募走 CreateCharacterRequest。"""
    lvl = int(level)
    return [uid in _PLAYER_LEVEL_UNLOCK_META
            and _PLAYER_LEVEL_UNLOCK_META[uid][1] != 0
            and _PLAYER_LEVEL_UNLOCK_META[uid][0] <= lvl
            and uid != _PLAYER_GENERAL_UNLOCK_ID
            for uid in range(_PLAYER_LEVEL_UNLOCK_MAX + 1)]


# ribbonlevel { id=1(等级), value=2(该级经验阈值) }：勋带单源 = total_xp 经阈值推导等级
def _build_ribbon_level_xp():
    m = {}
    for f in _parse_wardb_table('ribbonlevel'):
        m[int(f.get(1, 0))] = int(f.get(2, 0))
    return m


_RIBBON_LEVEL_XP = _build_ribbon_level_xp()


# ExperienceLevels { id=1, rankXp=2(逗号分隔军衔阈值) }：军衔单源 = combat_xp 经阈值推导等级。
# 客户端 ExperienceCalculation.getCombatProgression(xp) 取最大 i 使 rankXp[i] <= xp。
def _build_experience_levels():
    for f in _parse_wardb_table('ExperienceLevels'):
        rxp = f.get(2, b'')
        if isinstance(rxp, bytes):
            rxp = rxp.decode('utf-8', 'replace')
        return [int(x) for x in rxp.split(',') if x.strip().isdigit()]
    return [0]


_EXPERIENCE_LEVELS = _build_experience_levels()


def _rank_from_combat_xp(xp):
    """军衔等级：最大的 i 使 _EXPERIENCE_LEVELS[i] <= xp（与客户端 getCombatProgression 一致）。"""
    rank = 0
    for i, thr in enumerate(_EXPERIENCE_LEVELS):
        if thr <= xp:
            rank = i
        else:
            break
    return rank


def _ribbon_level_from_xp(total_xp):
    """勋带等级：最大的 lv 使 _RIBBON_LEVEL_XP[lv] <= total_xp（与客户端 getRibbonLevel 一致）。"""
    lv = 0
    for l, thr in sorted(_RIBBON_LEVEL_XP.items()):
        if thr <= total_xp:
            lv = l
        else:
            break
    return lv


# UniformVariantTemplate { id=1, name=2, characterTemplateId=3, factionTemplateId=4, ... }
# (兵种, 阵营) -> 默认军服变体模板 id：取该组合下 id 最小的变体（基础迷彩：
# SU Red Army / GE Field Grey / US Army Green 系）。
# 回放实测锚点：官方服角色 f9 variantTemplateId 恒非零，SU 步兵=28/坦克=29/
# 伞兵=30/飞行员=32 —— 与最小 id 规则一致。缺失 f9 时客户端无法构建士兵外观，
# 角色在部署界面不显示（2026-08-21 修复）。
def _build_default_uniform_variants():
    m = {}
    for f in _parse_wardb_table('UniformVariantTemplate'):
        key = (int(f.get(3, 0)), int(f.get(4, 0)))
        vid = int(f.get(1, 0))
        if key not in m or vid < m[key]:
            m[key] = vid
    return m


_DEFAULT_UNIFORM_VARIANT = _build_default_uniform_variants()


def default_uniform_variant(character_template_id, faction_template_id):
    """(兵种, 阵营) 默认军服变体模板 id；角色无 DB 军服实例时 f9 兜底，未知组合返回 0。"""
    return _DEFAULT_UNIFORM_VARIANT.get(
        (int(character_template_id or 0), int(faction_template_id or 0)), 0)


# CharacterTemplate { id=1, name=2, cost=3, actionid=4, ... deployPool*=9-11 }
# 兵种 → 游戏引擎实体类型（actionid）：rifleman=2 / fighterpilot=11 /
# paratrooper=7 / tankcrew=10 / recon=6 / general=7。
# 回放实测锚点（pvt_player_settings.bin）：charactersetting f7 恒等于
# charactertemplate.actionid —— Yaroslav(飞行员) f7=11、Kaspar(步兵) f7=2、
# Arkady(坦克) f7=10。游戏端按 f7 识别兵种决定部署方式（飞行员只能空中
# 出生、飞机才显示在部署槽位），f7≠actionid 时角色被当步兵 → 徒步部署。
_CHAR_TMPL_ACTIONID = {int(f.get(1, 0)): int(f.get(4, 0))
                      for f in _parse_wardb_table('charactertemplate')}


def charactertemplate_actionid(character_template_id):
    """兵种模板 → 引擎 actionid（charactersetting.f7）；未知模板返回 0。"""
    return _CHAR_TMPL_ACTIONID.get(int(character_template_id or 0), 0)


def _build_general_template_ids():
    """charactertemplate name='general' 的模板 id 集合（实测 id=7、cost=0）。"""
    ids = set()
    for f in _parse_wardb_table('charactertemplate'):
        name = f.get(2, b'')
        if isinstance(name, (bytes, bytearray)):
            name = name.decode('utf-8', 'replace')
        if str(name).strip().lower() == 'general':
            ids.add(int(f.get(1, 0)))
    return frozenset(ids)


GENERAL_TEMPLATE_IDS = _build_general_template_ids()


def is_general_character(character_template_id):
    """是否将军兵种模板（charactertemplate name='general'）。

    官方设计：将军只在大地图指挥 AssaultTeam，不能进 FPS 战斗局；其
    actionid 与伞兵同为 7（引擎实体同款），匹配不挡会混进战斗当伞兵。
    """
    return int(character_template_id or 0) in GENERAL_TEMPLATE_IDS


# SquadTemplate（WarDatabase/SquadTemplate 共 5 行）→ 官方小队类型名
# （客户端 texts/HnG_Strategy_en.xml squadtemplate/NTitle，N=表 name 列）：
# 1=Infantry Squad / 2=Fighter Squad / 3=Paratrooper Squad / 4=Armor Squad /
# 5=Recon Squad。大厅 UI 对无小队玩家按兵种显示默认模板名（solo
# SquadSession f10 name=''，客户端按 f3 characterTemplateId 兵种取模板标题），
# 局内 SquadResponse f4 name 必须与之同源，否则局内/大厅小队名对不上。
# 兵种 actionid → 小队模板：rifleman(2)→1 / fighterpilot(11)→2 /
# paratrooper(7)→3 / tankcrew(10)→4 / recon(6)→5（general 同伞兵=3）。
_ACTIONID_SQUAD_TEMPLATE = {2: 1, 11: 2, 7: 3, 10: 4, 6: 5}
SQUAD_TEMPLATE_NAMES = {1: 'Infantry Squad', 2: 'Fighter Squad',
                        3: 'Paratrooper Squad', 4: 'Armor Squad',
                        5: 'Recon Squad'}


def squad_template_for_character(character_template_id):
    """兵种模板 → 小队模板 id（1-5）；未知兵种回退 1（Infantry）。"""
    return _ACTIONID_SQUAD_TEMPLATE.get(
        charactertemplate_actionid(character_template_id), 1)


# SquadPlayerSlotTemplate 表（15 行：f1 id / f2 squadTemplateId /
# f3 defaultSlot / f4 slotNumber（1 基）/ f5 customizationCost）→
# 每个小队模板的玩家槽位列表 [(slotTemplateId, slotNumber-1)]。
# 战斗服 ActionGameSquadDefinition f5 playerSlots 必须发模板全量槽：
# 战斗服对同一 squadId 只在首个成员加入时向 warserver 拉一次定义并缓存
# （PhaseUpdatePlayerInit 反编译证实），按当前成员数发槽会让 late join
# 补进小队的成员分到不存在的槽 → "#error3 specified squad character
# slot not found"（2026-09-02 late join 小队名空白根因）。
# 客户端 SquadModel MAX_CHARACTERS_PER_SQUAD=4，槽位 UI 同按此表渲染。
SQUAD_TEMPLATE_SLOTS = {}
for _f in _parse_wardb_table('SquadPlayerSlotTemplate'):
    _st = int(_f.get(2, 0) or 0)
    if _st:
        SQUAD_TEMPLATE_SLOTS.setdefault(_st, []).append(
            (int(_f.get(1, 0) or 0), int(_f.get(4, 0) or 0) - 1))
for _v in SQUAD_TEMPLATE_SLOTS.values():
    _v.sort(key=lambda r: r[1])


def squad_template_slot_count(squad_template_id):
    """小队模板玩家槽数（凑队/late join 人数上限）；未知模板回退 1。"""
    return len(SQUAD_TEMPLATE_SLOTS.get(int(squad_template_id or 0), [])) or 1


def squad_template_name(squad_template_id):
    """小队模板 id → 官方类型名；未知返回 'Infantry Squad'。"""
    return SQUAD_TEMPLATE_NAMES.get(int(squad_template_id or 0),
                                    'Infantry Squad')


# 武器模板 → 勋带模板 id：从 progressionUnlock（配件 type=3 / 弹药 type=10）
# 的 unlockMetaName 反查（每个武器的配件/弹药解锁需求都指向同一武器勋带）。
# 用于 ResponseReadCharacterStats 下发 attributes（Ribbon[]）时只包含玩家持有武器的勋带。
def _build_weapon_ribbons():
    mod_to_weapon = {}
    for f in _parse_wardb_table('WeaponModMapping'):
        mod_to_weapon[int(f.get(1, 0))] = int(f.get(3, 0))
    ammo_to_weapon = {}
    for f in _parse_wardb_table('weaponAmmoMapping'):
        ammo_to_weapon[int(f.get(1, 0))] = int(f.get(2, 0))
    weapon_ribbons = {}
    for f in _parse_wardb_table('progressionUnlock'):
        itype = int(f.get(7, 0))
        iid = int(f.get(8, 0))
        meta_name = f.get(5, b'')
        if isinstance(meta_name, bytes):
            meta_name = meta_name.decode('utf-8', 'replace')
        if not meta_name.strip().isdigit():
            continue
        wt = None
        if itype == _E_WEAPON_MOD_MAPPING:
            wt = mod_to_weapon.get(iid)
        elif itype == _E_WEAPON_AMMO_MAPPING:
            wt = ammo_to_weapon.get(iid)
        if wt:
            weapon_ribbons.setdefault(wt, set()).add(int(meta_name))
    return weapon_ribbons


_WEAPON_RIBBONS = _build_weapon_ribbons()


def _default_ammo_for(weapon_template_id):
    """返回武器模板的默认弹药映射列表 [(mappingId, ammoTemplateId), ...]（defaultAmmo=true）。"""
    return [(m, a) for (m, a, default) in _AMMO_MAPPINGS_BY_WEAPON.get(int(weapon_template_id), ())
            if default]


def _ensure_store_campaign_file():
    """WarDatabase/StoreCampaign 缺失时现场生成（tools/gen_storecampaign.py 的
    自动化替代）。轮播数据源 CampaignAndInfoModel 依赖 StoreCampaignTable，空表
    会导致 changeToNextCampaign→easeDone→setSelectedCampaign(0) 在空集合上取
    index 0 抛 RangeError，客户端每次登录崩溃。
    内容：2 条 campaign（id=1 eDiscount、id=2 eCampaign），wardb KVCS 格式
    （KeyValueChangeSet → set → StoreCampaign×2）；proto 字段号与客户端
    HnG_States/StoreCampaign.as 核对一致（1=id … 9=countdownHours）。
    注意：不能引用 messages._msg_store_campaign —— messages import wardb，
    会循环导入，故本地等价实现。"""
    fpath = os.path.join(WARDDB_DIR, 'StoreCampaign')
    if os.path.isfile(fpath):
        return
    try:
        def _campaign(cid, ctype):
            m = b''
            m += pb_int64(1, cid)                # id
            m += pb_int32(2, ctype)              # type: 1=eDiscount 2=eCampaign
            m += pb_string(3, str(cid))          # assetIdentifier → images/Campaign/<id>.png
            m += pb_string(4, '')                # callToAction
            m += pb_string(5, '')                # iconType
            m += pb_string(6, '20260816000000')  # activeFromTime（Zulu，须覆盖当前时间，
                                                 # 否则被 checkIfCampaignShouldBeShownInCurrent 过滤）
            m += pb_string(7, '20990101000000')  # activeToTime
            m += pb_string(8, f'Campaign {cid}') # name
            m += pb_int32(9, 0)                  # countdownHours
            return m

        pairs = [Chunk(b'StoreCampaign', [Chunk(_campaign(1, 1))]),   # eDiscount
                 Chunk(b'StoreCampaign', [Chunk(_campaign(2, 2))])]   # eCampaign
        action = Chunk(b'set', pairs)
        title = Chunk(b'KeyValueChangeSet', [action])
        data = title.write(b'')
        os.makedirs(WARDDB_DIR, exist_ok=True)
        with open(fpath, 'wb') as f:
            f.write(data)
        log(f'[wardb] generated missing StoreCampaign file ({len(data)}B)')
    except Exception as e:
        log(f'[!] generate StoreCampaign file failed: {e}')


# StoreCampaign 表文件须在 manifest 扫描之前就位，保证 wardb_unique 同步收录
_ensure_store_campaign_file()


def _scan_wardb_entries():
    """扫描 WarDatabase 目录 → [(表名, sha256hex, 字节数)]，两种清单共用。"""
    entries = []
    if os.path.isdir(WARDDB_DIR):
        for name in sorted(os.listdir(WARDDB_DIR)):
            fpath = os.path.join(WARDDB_DIR, name)
            if not os.path.isfile(fpath):
                continue
            with open(fpath, 'rb') as f:
                data = f.read()
            entries.append((name, hashlib.sha256(data).hexdigest(), len(data)))
    return entries


_WARDB_ENTRIES = _scan_wardb_entries()


def _generate_wardb_unique():
    """swf 用（HandleDownloadDataCommand）：2 列 "表名",sha256hex-size。"""
    lines = [f'"{n}",{s}-{z}' for n, s, z in _WARDB_ENTRIES]
    return ('\n'.join(lines) + '\n').encode('utf-8')


def _generate_wardb_sync():
    """hngsync 用（登录同步 set:desktop_client）：官方 3 列 wire 清单格式
    "相对路径",<sha256>-<size>_<flag>,<size>，目录累积式相对路径（首条全路径、
    后续相对上一条目录 —— 与官方 desktop_client 清单 tmp/manifest_desktop.txt
    同构，hngsync UniqueFileList_Parse 按 sCurrentDir+名字拼接解析）。
    列 2 必须是完整 CAS 名 <sha256>-<size>_<flag>：hngsync 发现本地文件
    哈希/大小不符时以 GET /<列2> 下载本体修复。"""
    import posixpath as _pp
    lines = []
    cur_dir = ''
    for n, s, z in _WARDB_ENTRIES:
        rel = _pp.join('WarDatabase', n)
        rel_path = rel if not cur_dir else _pp.relpath(rel, cur_dir)
        lines.append(f'"{rel_path}",{s}-{z}_0,{z}')
        cur_dir = 'WarDatabase'
    return ('\n'.join(lines) + '\n').encode('utf-8')


# WarDatabase 文件清单（swf 登录链 wardb_unique 用）
WARDB_UNIQUE = _generate_wardb_unique()

# hngsync desktop_client 同步清单（远程客户端改表推送通道）
WARDB_SYNC_UNIQUE = _generate_wardb_sync()

# CAS 名 → 表文件路径：hngsync GET /<sha256>-<size>_<flag> 时直接回源
# WarDatabase 目录，改动过的表无需手工复制进 web_server 的 cas/ 目录
WARDB_CAS_MAP = {f'{s}-{z}_0': os.path.join(WARDDB_DIR, n)
                 for n, s, z in _WARDB_ENTRIES}

# 商店表文件列表（ShopView 订阅时从 WarDatabase 回放推送）
SHOP_TABLE_FILES = [
    'StoreBundle',               # 捆绑包（必需，商店核心商品）
    'Pricing',                   # 定价（必需，商品价格显示）
    'ShopMembershipItems',       # 会员商品
    'ShopCharacterPacks',        # 角色包
    'ShopWarBondItemTemplate',   # 战争债券模板
    'BoosterTemplate',           # 加成模板
    'RibbonBoosterTemplate',     # 缎带加成模板
    'RibbonBoosterTemplateBundle',  # 缎带加成模板包
    # 商店战役/折扣轮播（CampaignAndInfoModel 依赖 StoreCampaignTable，空则首页
    # 轮播集合为空，changeToNextCampaign→easeDone→setSelectedCampaign(0) 抛
    # RangeError "Index '0' specified is out of bounds" 导致客户端崩溃 10005。
    # 文件由 tools 生成，与 StoreBundle 同构：KeyValueChangeSet -> set -> [pair×2]
    'StoreCampaign',
]

# 已加载的商店表 KeyValueChangeSet title chunk 缓存: filename -> Chunk
_shop_kvcs_cache = {}


def _load_wardb_kvcs(name):
    """加载 WarDatabase 文件，解析为 KeyValueChangeSet title Chunk 并缓存。
    文件本身就是 KeyValueChangeSet chunk 的二进制载荷，可直接用 cmd_id 包装后发送。
    """
    if name in _shop_kvcs_cache:
        return _shop_kvcs_cache[name]
    fpath = os.path.join(WARDDB_DIR, name)
    if not os.path.isfile(fpath):
        return None
    try:
        with open(fpath, 'rb') as f:
            data = f.read()
        chunk, _ = Chunk.parse(data, 0)
        if chunk is None:
            log(f'[!] Failed to parse wardb chunk: {name}')
            return None
        _shop_kvcs_cache[name] = chunk
        return chunk
    except Exception as e:
        log(f'[!] Error loading wardb chunk {name}: {e}')
        return None


# ===== 武器弹夹（客户端 WeaponUtilities / ModifyWeaponMediator 逆向确认）=====
# weapon.clipCount 为持久化三态选择（weapon KVCS f14，存 weapons.clip_count）：
#   负 -(T-1) = 最少弹药（1 个弹夹）/ 0 = 默认（T 个弹夹）/ 正 +(T-1) = 最大（2T-1 个弹夹）
# T = weapontemplate f55 clipCount（最大弹夹数，WarDatabase 单源）。
_WT_CLIPCOUNT = None


def weapon_template_clipcount(template_id):
    """weapontemplate f55 clipCount（最大弹夹数），全表缓存后按模板查。"""
    global _WT_CLIPCOUNT
    if _WT_CLIPCOUNT is None:
        _WT_CLIPCOUNT = {int(f.get(1, 0) or 0): int(f.get(55, 0) or 0)
                         for f in _parse_wardb_table('weapontemplate')}
    return _WT_CLIPCOUNT.get(int(template_id), 0)


# weapontemplate f57 weaponcategoryid（客户端 WeaponCategory 枚举：
# 8=AntiTankWeapon 16=Submachinegun 10=CloseCombat 17=Handgun 等，
# swf HnG_States.WeaponCategory + WeaponTemplateSchema 逆向确认）
_WT_CATEGORY = None


def weapon_template_category(template_id):
    """weapontemplate f57 weaponcategoryid，全表缓存后按模板查（未知 0）。"""
    global _WT_CATEGORY
    if _WT_CATEGORY is None:
        _WT_CATEGORY = {int(f.get(1, 0) or 0): int(f.get(57, 0) or 0)
                        for f in _parse_wardb_table('weapontemplate')}
    return _WT_CATEGORY.get(int(template_id), 0)


def spawn_clips_for(clip_count, template_id):
    """出生弹夹数推导（PlayerSettings WeaponSetting f7 / WeaponSetting f7）：
    clip_count<0 → 1（最少）、==0 → T（默认）、>0 → 2T-1（最大）。"""
    t = weapon_template_clipcount(template_id)
    if t <= 0:
        return max(t, 0)
    if clip_count < 0:
        return 1
    if clip_count == 0:
        return t
    return 2 * t - 1


# ===== 战斗服务器（hng.exe /D 专用服）BattleFieldSettings 静态数据 =====
_PRESET_CACHE = {}
_AP_TMPL_CACHE = {}


def _load_accesspoint_templates(gamemap_id):
    """本图的通道点模板 id 列表（accesspointtemplate 表 f2 == gamemap.f1 分组，
    逆向 sub_1800B6600 + WarDatabase 对齐确认：gamemap f8 与模板无关，
    分组键是 gamemap id 本身 —— T01M08_Mill=13 → 模板 8(A点)/15(B点)）。
    返回按 id 升序（A 在前 B 在后，f5 名字 A/B/C 即攻防顺序）。"""
    if gamemap_id in _AP_TMPL_CACHE:
        return _AP_TMPL_CACHE[gamemap_id]
    ids = []
    try:
        for r in _parse_wardb_table('accesspointtemplate'):
            # 过滤占位行（f1=52/90/91 的 f2=0 全零模板）
            if int(r.get(2, 0) or 0) == gamemap_id and int(r.get(1, 0)) != 0:
                ids.append(int(r.get(1, 0)))
    except Exception as e:
        log(f'[!] accesspointtemplate parse failed: {e}')
    ids.sort()
    _AP_TMPL_CACHE[gamemap_id] = ids
    return ids


_AP_DECISION_CACHE = {}


_CP_AP_MAP_CACHE = {}


def _capturepoint_ap_map(gamemap_id):
    """本图据点 → 挂靠出兵点模板映射（CapturepointTemplate f3，{cp_id: ap_id}）。

    Assault 图每条外围线的入口据点（Airfield A1=56→1164235、C1=58→
    2717391、B1=54→2881042、D1=60→3386581）f3 非零挂出兵点；线中段
    （A2/B2/...）与 O 点（f4 objectiveId）f3=0 不挂。大厅简报 f7
    capturePointStatuses 用：只下发激活线入口据点 → 客户端 auto 路径
    getAutoGeneratedActiveLines 按 owner!=0 据点名首字母判激活线
    （BattlefieldStatusView.as L152-171），未激活线/守方线不画。"""
    key = int(gamemap_id)
    if key in _CP_AP_MAP_CACHE:
        return _CP_AP_MAP_CACHE[key]
    mapping = {}
    try:
        for r in _parse_wardb_table('CapturepointTemplate'):
            f2 = r.get(2, 0)
            f3 = r.get(3, 0)
            if isinstance(f2, (bytes, bytearray)) or \
                    isinstance(f3, (bytes, bytearray)):
                continue
            if int(f2 or 0) == key and int(r.get(1, 0) or 0) \
                    and int(f3 or 0):
                mapping[int(r.get(1, 0))] = int(f3)
    except Exception as e:
        log(f'[!] CapturepointTemplate ap-map parse failed: {e}')
    _CP_AP_MAP_CACHE[key] = mapping
    return mapping


O_ANCHOR_AP_TEMPLATE = 1234567890
"""Assault 图 O 锚虚拟出兵点模板 id（引擎硬编码，player.dll sub_1803AD540
O 锚分支写 AP+128=1234567890，无对应 accesspointtemplate 表行）。守方 f12
收编 + f14 资源挂此点 → 置 HasResources 位 → 小队/坦克部署到中心 O 区
（assault-map-spawn-investigation.md §9.2）。"""


def assault_ap_decision(gamemap_id, mission_id):
    """Assault 图出兵线激活决策（每局一次，按 mission_id 播种确定性）。

    2026-09-02 O 锚方案（§9.2，替代 2026-09-01 的"守方随机 1 条边线"）：
    守方不再分边线出兵点，改分 O 锚 1234567890（引擎 gamemode Setup 无条件
    创建的虚拟 AP，sub_1803A0500/sub_1803AD540 实证）——f12 发
    {1234567890, 守方} 收编 O 锚（同时满足 SetupGame 守线判负检查
    sub_1803EDFA0，无 "#error1 Not enough free accesspoints"）；f14 守方
    资源挂 O 锚 → sub_1803ADF00 置 HasResources 位 → 守方小队候选过滤
    （sub_180398A10）通过 → Relocated 到 O → 载具在 O 区（Airfield 机场）
    出生。进攻方随机激活 3..n 条边线（n=图内出兵点数，等概率——官方
    Assault 语义"不一定占满攻击线，有时仅 3 条"，4 线图 3/4 随机、
    5 线图 3/4/5 随机（可全开）；不足 3 线的图开全部）；
    ★未激活线直接不开启（不出现在 BattleFieldSettings f12 / 大厅简报 f6 /
    战斗 layout 中，图上不显示）。

    seeded RNG（random.Random(mission_id)）保证同一局内多处调用决策
    一致：BattleFieldSettings 握手 f12、combat.py 10s 轮询 f2/f3/f5、
    大厅简报 f4/f5/f6 必须同源——否则 combat.ap_sync_layout 检测到
    布局变化 → 全量重发 → 引擎 ChangeAccesspointOwner 噪音、
    大厅图标与进战出生侧错位。

    返回 (defender_ap, attacker_aps, inactive_aps)：
      defender_ap   守方出兵点模板 id（恒 O_ANCHOR_AP_TEMPLATE=1234567890）
      attacker_aps  攻方激活边线（3..n 个等概率，n=图内出兵点数）
      inactive_aps  未开启边线（不进任何消息；可能空）
    """
    key = (int(gamemap_id), int(mission_id or 0))
    if key in _AP_DECISION_CACHE:
        return _AP_DECISION_CACHE[key]
    # 不截断：按图实际出兵点数参与随机（Town/Desert 等 5 线图 E 线同样
    # 参与；2~4 线图行为与此前 [:4] 一致）
    aps = sorted(_load_accesspoint_templates(key[0]))
    rng = random.Random(key[1])
    rng.shuffle(aps)
    n = len(aps)
    defender_ap = O_ANCHOR_AP_TEMPLATE
    # randint(3, max(n,3)) 防 n<3 时 randint 下界>上界；min(...,n) → 不足
    # 3 线的图开全部
    atk_n = min(rng.randint(3, max(n, 3)), n) if n else 0
    attacker_aps = aps[:atk_n]
    inactive_aps = aps[atk_n:]
    _AP_DECISION_CACHE[key] = (defender_ap, attacker_aps, inactive_aps)
    return _AP_DECISION_CACHE[key]


_CAPTUREPOINT_TMPL_CACHE = {}


def _load_capturepoint_templates(gamemap_id):
    """本图据点模板 id 列表（CapturepointTemplate 表 f2 == gamemap id 分组，
    返回按 id 升序）。Mill(13) → [120]；换图据点集合随表变化
    （Town(70439) 22 个、Mountain(201) 17 个）。"""
    if gamemap_id in _CAPTUREPOINT_TMPL_CACHE:
        return _CAPTUREPOINT_TMPL_CACHE[gamemap_id]
    ids = []
    try:
        for r in _parse_wardb_table('CapturepointTemplate'):
            f2 = r.get(2, 0)
            if isinstance(f2, (bytes, bytearray)):
                continue                      # 非分组键行（如 f2=名字）
            if int(f2 or 0) == int(gamemap_id) and int(r.get(1, 0) or 0):
                ids.append(int(r.get(1, 0)))
    except Exception as e:
        log(f'[!] CapturepointTemplate parse failed: {e}')
    ids.sort()
    _CAPTUREPOINT_TMPL_CACHE[gamemap_id] = ids
    return ids


_OBJ_CP_TMPL_CACHE = {}


def _load_objective_capturepoints(gamemap_id):
    """本图 O 点（objective）据点模板 id 列表（CapturepointTemplate f2 ==
    gamemap 分组中 f4 objectiveId 非零的行，升序）。

    Assault 图中心 O 点（Airfield=204 → O3=63/O2=64/O1=65，f4=1）即此组；
    连线中段 X1/X2 与 Encounter（Mill）单点 f4=0 不入。大厅简报 f7 据点
    初始归属用：Assault 局 O 点=防守方、其余=0 中立
    （assault-map-spawn-investigation.md §6.2）。"""
    key = int(gamemap_id)
    if key in _OBJ_CP_TMPL_CACHE:
        return _OBJ_CP_TMPL_CACHE[key]
    ids = []
    try:
        for r in _parse_wardb_table('CapturepointTemplate'):
            f2 = r.get(2, 0)
            if isinstance(f2, (bytes, bytearray)):
                continue                  # 非分组键行（如 f2=名字）
            f4 = r.get(4, 0)
            if isinstance(f4, (bytes, bytearray)):
                continue
            if int(f2 or 0) == key and int(r.get(1, 0) or 0) \
                    and int(f4 or 0):
                ids.append(int(r.get(1, 0)))
    except Exception as e:
        log(f'[!] CapturepointTemplate objective parse failed: {e}')
    ids.sort()
    _OBJ_CP_TMPL_CACHE[key] = ids
    return ids


_MISSIONTYPE_CACHE = {}


def _missiontype_for_gamemap(gamemap_id):
    """gamemap → missiontype（missionpreset 表 f3==gamemap 行的 f4；
    1=Assault、5=Encounter，同图多 preset 同型取首行）。

    出兵点归属按模式分支的唯一判据：Assault 四角出兵点全归进攻方、
    防守方 0 出兵点；Encounter 维持交替分配
    （assault-map-spawn-investigation.md §6.1）。查不到回 0（按 Encounter
    交替处理，保持旧行为）。"""
    key = int(gamemap_id)
    if key in _MISSIONTYPE_CACHE:
        return _MISSIONTYPE_CACHE[key]
    mt = 0
    try:
        for r in _parse_wardb_table('missionpreset'):
            f3 = r.get(3, 0)
            if isinstance(f3, (bytes, bytearray)):
                continue
            if int(f3 or 0) != key or not int(r.get(1, 0) or 0):
                continue
            f4 = r.get(4, 0)
            if not isinstance(f4, (bytes, bytearray)) and int(f4 or 0):
                mt = int(f4)
            break
    except Exception as e:
        log(f'[!] missionpreset missiontype parse failed: {e}')
    _MISSIONTYPE_CACHE[key] = mt
    return mt


_COMMANDNODE_NAME_CACHE = {}     # factionTemplateId -> {兵种名: 行 id}


_ARMY_RESOURCE_CATEGORY_CACHE = None


def army_resource_categories():
    """armyresourcecategory 表 [(行 id, 名称), ...]（缓存）。

    阵营资源总览窗口（FactionResourceOverviewMediator）按分类渲染行，
    FactionResourceProduction/Consumption 行需逐分类配对推送。
    注意：本表 f6 是 speed（移动速度 0.9~3.3）不是产量——产量/间隔/开战
    库存取 ArmyResourceProductionTemplate（armyresource_production()，
    docs/hng-rts-protocol.md §3.12.1）。"""
    global _ARMY_RESOURCE_CATEGORY_CACHE
    if _ARMY_RESOURCE_CATEGORY_CACHE is None:
        out = []
        try:
            for r in _parse_wardb_table('armyresourcecategory'):
                rid = int(r.get(1, 0) or 0)
                if not rid:
                    continue
                name = r.get(2, b'')
                if isinstance(name, (bytes, bytearray)):
                    name = name.decode('utf-8', 'replace')
                out.append((rid, name))
        except Exception as e:
            log(f'[!] armyresourcecategory parse failed: {e}')
        _ARMY_RESOURCE_CATEGORY_CACHE = out
    return _ARMY_RESOURCE_CATEGORY_CACHE


def commandnode_template(faction, name):
    """阵营兵种的 commandNode 模板 id（commandnodetemplate 表 f2 名字 ×
    f3 factionTemplateId 匹配）。

    突击队模板是【阵营相关】库值：US=101-124 / GE=201-224 / SU=301-324 段，
    同名兵种每阵营一行（infantry: US 103 / GE 203 / SU 303）。
    查不到返回 0。"""
    faction = int(faction)
    if faction not in _COMMANDNODE_NAME_CACHE:
        by_name = {}
        try:
            for r in _parse_wardb_table('commandnodetemplate'):
                if int(r.get(3, 0) or 0) != faction:
                    continue
                nm = r.get(2, b'')
                if isinstance(nm, (bytes, bytearray)) and nm:
                    by_name[nm.decode('utf-8', 'replace')] = int(r.get(1, 0) or 0)
        except Exception as e:
            log(f'[!] commandnodetemplate parse failed: {e}')
        _COMMANDNODE_NAME_CACHE[faction] = by_name
    return _COMMANDNODE_NAME_CACHE[faction].get(name, 0)


def _load_preset(preset_id):
    """读 WarDatabase/missionpreset 行（缓存）。查不到回退 preset 10 硬编码值。"""
    if preset_id in _PRESET_CACHE:
        return _PRESET_CACHE[preset_id]
    row = None
    try:
        for r in _parse_wardb_table('missionpreset'):
            if int(r.get(1, 0)) == preset_id:
                row = r
                break
    except Exception as e:
        log(f'[!] missionpreset parse failed: {e}')
    if row is None:
        # preset 10: Training - Encounter - Mill（WarDatabase 实测行）
        row = {1: 10, 3: 13, 4: 5, 5: b'Training - Encounter - Mill', 7: 0, 11: 24}
        log(f'[!] preset {preset_id} not in WarDatabase, fallback to preset 10')
        preset_id = 10
    _PRESET_CACHE[preset_id] = row
    return row


# ===== 匹配池地图数据链（missionpreset → gamemap → scenefile / battlefield 行）=====
_GAMEMAP_SCENE_CACHE = {}       # gamemap_id -> scenefile str
_BATTLEFIELD_ENTITY_CACHE = {}  # gamemap_id -> battlefield 表行 id


def _load_gamemap_row(gamemap_id):
    """读 WarDatabase/gamemap 行（缓存）。gamemap 表字段（实测 dump 确认）：
    f1=id f2=gamemapname f3=identifier f4=scenefile（如 13 → 'T01M08_Mill.scn'）。"""
    if gamemap_id in _GAMEMAP_SCENE_CACHE:
        return _GAMEMAP_SCENE_CACHE[gamemap_id]
    row = None
    try:
        for r in _parse_wardb_table('gamemap'):
            if int(r.get(1, 0) or 0) == int(gamemap_id):
                row = r
                break
    except Exception as e:
        log(f'[!] gamemap parse failed: {e}')
    _GAMEMAP_SCENE_CACHE[gamemap_id] = row
    return row


def _load_gamemap_scene(gamemap_id):
    """gamemap → 场景名（combat server /M 启动参数，不含 .scn 后缀）。

    WarDatabase 存 'T01M08_Mill.scn'，启动参数为 'T01M08_Mill'
    （start_server.bat 实测）。查不到回退 Mill。
    """
    row = _load_gamemap_row(gamemap_id)
    scene = ''
    if row is not None:
        v = row.get(4, b'')         # f4 = scenefile
        if isinstance(v, bytes) and v:
            scene = v.decode('utf-8', 'replace')
    if scene.endswith('.scn'):
        scene = scene[:-4]
    if not scene:
        log(f'[!] gamemap {gamemap_id} scenefile missing, '
            f'fallback to T01M08_Mill')
        return 'T01M08_Mill'
    return scene


def preset_scene(preset_id):
    """missionpreset → gamemap → scenefile（combat server /M 单一来源）。"""
    preset = _load_preset(preset_id)
    gamemap_id = int(preset.get(3, 0) or 0) or 13
    return _load_gamemap_scene(gamemap_id)


def battlefield_entity_for_gamemap(gamemap_id):
    """battlefield 表 f7==gamemap 的行 id（MissionInfo f5 mapEntityId）。

    battlefield 表只覆盖战役地图（f7 ∈ {70439 Town, 201 Mountain, 203 Factory,
    204 Airfield, 205 Forward Airfield}）；Mill(13) 等遭遇战/训练图无行，
    回退 MISSION_MAP_ENTITY_ID 常量值（112741325='Benoit Village'），
    保证客户端简报查表可用。
    """
    if gamemap_id in _BATTLEFIELD_ENTITY_CACHE:
        return _BATTLEFIELD_ENTITY_CACHE[gamemap_id]
    entity_id = 112741325
    try:
        for r in _parse_wardb_table('battlefield'):
            if int(r.get(7, 0) or 0) == int(gamemap_id):
                entity_id = int(r.get(1, 0) or 0) or entity_id
                break
    except Exception as e:
        log(f'[!] battlefield parse failed: {e}')
    _BATTLEFIELD_ENTITY_CACHE[gamemap_id] = entity_id
    return entity_id


_BATTLEFIELD_POS_CACHE = {}      # battlefield 行 id -> (posX, posY)


def battlefield_pos(entity_id):
    """battlefield 表行坐标 (posX, posY)（f5/f6 float，战役地图上的城镇位置）。

    GMD f8/f9 与 BattleInfo 的战局位置用：与 mapEntityId 同行取值，
    简报战场名与地图标记保持一致。行不存在/字段缺失返回 None。"""
    if entity_id in _BATTLEFIELD_POS_CACHE:
        return _BATTLEFIELD_POS_CACHE[entity_id]
    pos = None
    try:
        for r in _parse_wardb_table('battlefield'):
            if int(r.get(1, 0) or 0) != int(entity_id):
                continue
            x, y = r.get(5), r.get(6)
            if (isinstance(x, (bytes, bytearray)) and len(x) == 4 and
                    isinstance(y, (bytes, bytearray)) and len(y) == 4):
                pos = (struct.unpack('<f', bytes(x))[0],
                       struct.unpack('<f', bytes(y))[0])
            break
    except Exception as e:
        log(f'[!] battlefield pos parse failed: {e}')
    _BATTLEFIELD_POS_CACHE[entity_id] = pos
    return pos


# ===== RTS（将军/战略层）静态数据接口（docs/hng-rts-protocol.md §2.2/§3）=====
# 字段号均为客户端 Descriptor 实锤；macOS 开发机无 WarDatabase 时各接口
# 返回空结构（沿用 battlefield_pos 返回 None 的容忍策略，测试侧跳过）。

_RTS_CACHE = {}


def _wardb_str(v):
    return v.decode('utf-8', 'replace') if isinstance(v, (bytes, bytearray)) and v else ''


def _wardb_f32(v):
    if isinstance(v, (bytes, bytearray)) and len(v) == 4:
        return struct.unpack('<f', bytes(v))[0]
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def capitals():
    """capital 表行 [{id, battlefield_id, default_faction, victory_points}, ...]。

    战争地图城市节点：battlefield_id 关联 battlefield 表（即"城市"），
    default_faction 为开战初始归属（factiontemplateid 1/2/3 空间）。"""
    if 'capitals' not in _RTS_CACHE:
        out = []
        try:
            for r in _parse_wardb_table('capital'):
                rid = int(r.get(1, 0) or 0)
                if not rid:
                    continue
                out.append({
                    'id': rid,
                    'battlefield_id': int(r.get(2, 0) or 0),
                    'default_faction': int(r.get(3, 0) or 0),
                    'victory_points': int(r.get(4, 0) or 0),
                })
        except Exception as e:
            log(f'[!] capital parse failed: {e}')
        _RTS_CACHE['capitals'] = out
    return _RTS_CACHE['capitals']


def battlefields():
    """battlefield 表全量行 [{id, mapid, title, sector, posx, posy, gamemap}, ...]。

    战役地图上的城镇/工厂/机场节点（RTS 拓扑的"城市"本体）。"""
    if 'battlefields' not in _RTS_CACHE:
        out = []
        try:
            for r in _parse_wardb_table('battlefield'):
                rid = int(r.get(1, 0) or 0)
                if not rid:
                    continue
                out.append({
                    'id': rid,
                    'mapid': int(r.get(2, 0) or 0),
                    'title': _wardb_str(r.get(3, b'')),
                    'sector': _wardb_str(r.get(4, b'')),
                    'posx': _wardb_f32(r.get(5)),
                    'posy': _wardb_f32(r.get(6)),
                    'gamemap': int(r.get(7, 0) or 0),
                })
        except Exception as e:
            log(f'[!] battlefields parse failed: {e}')
        _RTS_CACHE['battlefields'] = out
    return _RTS_CACHE['battlefields']


def supplylines():
    """supplyline 表行 [{id, mapid, ap1, ap2, template_id}, ...]。

    城市间连接（边）：ap1/ap2 为两端 accesspoint 行 id，
    accesspoint.f4 再关联所属 battlefield（城市）。"""
    if 'supplylines' not in _RTS_CACHE:
        out = []
        try:
            for r in _parse_wardb_table('supplyline'):
                rid = int(r.get(1, 0) or 0)
                if not rid:
                    continue
                out.append({
                    'id': rid,
                    'mapid': int(r.get(2, 0) or 0),
                    'ap1': int(r.get(3, 0) or 0),
                    'ap2': int(r.get(4, 0) or 0),
                    'template_id': int(r.get(5, 0) or 0),
                })
        except Exception as e:
            log(f'[!] supplyline parse failed: {e}')
        _RTS_CACHE['supplylines'] = out
    return _RTS_CACHE['supplylines']


def accesspoints():
    """accesspoint 表行 [{id, mapid, template, battlefield}, ...]（RTS 拓扑端点）。"""
    if 'accesspoints' not in _RTS_CACHE:
        out = []
        try:
            for r in _parse_wardb_table('accesspoint'):
                rid = int(r.get(1, 0) or 0)
                if not rid:
                    continue
                out.append({
                    'id': rid,
                    'mapid': int(r.get(2, 0) or 0),
                    'template': int(r.get(3, 0) or 0),
                    'battlefield': int(r.get(4, 0) or 0),
                })
        except Exception as e:
            log(f'[!] accesspoint parse failed: {e}')
        _RTS_CACHE['accesspoints'] = out
    return _RTS_CACHE['accesspoints']


def commandnode_templates():
    """AT（Assault Team）模板注册表 {template_id: {...}, ...}。

    commandnodetemplate 主表 + armyresourcetemplaterelation（编制资源
    {armyresourcecategoryid: count}）+ charactertemplaterelation（可携带士兵
    [(charactertemplateid, spawnslots), ...]）整合。字段号见
    docs/hng-rts-protocol.md：f4 speed/float、f8 actionid、f10 level、
    f11 convertTo（升级链）、f12 parenttemplateid、f13/f14 战利 warfund 加成。"""
    if 'cn_templates' not in _RTS_CACHE:
        out = {}
        try:
            for r in _parse_wardb_table('commandnodetemplate'):
                tid = int(r.get(1, 0) or 0)
                if not tid:
                    continue
                out[tid] = {
                    'id': tid,
                    'name': _wardb_str(r.get(2, b'')),
                    'faction': int(r.get(3, 0) or 0),
                    'speed': _wardb_f32(r.get(4)),
                    'attackradius': int(r.get(6, 0) or 0),
                    'transportradius': int(r.get(7, 0) or 0),
                    'actionid': int(r.get(8, 0) or 0),
                    'size': int(r.get(9, 0) or 0),
                    'level': int(r.get(10, 0) or 0),
                    'convert_to': int(r.get(11, 0) or 0),
                    'parent_template_id': int(r.get(12, 0) or 0),
                    'victory_warfund_bonus': int(r.get(13, 0) or 0),
                    'participation_warfund_bonus': int(r.get(14, 0) or 0),
                    'view_range': int(r.get(15, 0) or 0),
                    'general_character_template_id': int(r.get(16, 0) or 0),
                    'resources': {},
                    'characters': [],
                }
            for r in _parse_wardb_table(
                    'commandnodetemplatearmyresourcetemplaterelation'):
                tid = int(r.get(3, 0) or 0)
                if tid in out:
                    cat = int(r.get(2, 0) or 0)
                    out[tid]['resources'][cat] = int(r.get(4, 0) or 0)
            for r in _parse_wardb_table(
                    'commandnodetemplatecharactertemplaterelation'):
                tid = int(r.get(2, 0) or 0)
                if tid in out:
                    out[tid]['characters'].append(
                        (int(r.get(3, 0) or 0), int(r.get(4, 0) or 0)))
        except Exception as e:
            log(f'[!] commandnode templates parse failed: {e}')
        _RTS_CACHE['cn_templates'] = out
    return _RTS_CACHE['cn_templates']


def at_levels():
    """AssaultTeamLevel 表 [(level, xp), ...]（AT 等级经验阈值，升序）。"""
    if 'at_levels' not in _RTS_CACHE:
        out = []
        try:
            for r in _parse_wardb_table('AssaultTeamLevel'):
                out.append((int(r.get(2, 0) or 0), int(r.get(3, 0) or 0)))
            out.sort()
        except Exception as e:
            log(f'[!] AssaultTeamLevel parse failed: {e}')
        _RTS_CACHE['at_levels'] = out
    return _RTS_CACHE['at_levels']


def morale_template():
    """CommandNodeMoraleTemplate 首行（战争 morale 参数缺省值来源）。

    返回 {min, max, lost_per_battle, gained_per_win, gained_per_minute,
    surrender_limit}；无表（macOS 开发机）返回 None。"""
    if 'morale_template' not in _RTS_CACHE:
        row = None
        try:
            for r in _parse_wardb_table('CommandNodeMoraleTemplate'):
                row = {
                    'min': int(r.get(2, 0) or 0),
                    'max': int(r.get(3, 0) or 0),
                    'lost_per_battle': int(r.get(4, 0) or 0),
                    'gained_per_win': int(r.get(5, 0) or 0),
                    'gained_per_minute': _wardb_f32(r.get(6)),
                    'surrender_limit': int(r.get(7, 0) or 0),
                }
                break
        except Exception as e:
            log(f'[!] CommandNodeMoraleTemplate parse failed: {e}')
        _RTS_CACHE['morale_template'] = row
    return _RTS_CACHE['morale_template']


def armyresource_production():
    """ArmyResourceProductionTemplate 表行 [{id, faction, category_id,
    run_every_seconds, produce_per_run, war_start_value}, ...]（M3 生产 tick 用）。"""
    if 'ar_production' not in _RTS_CACHE:
        out = []
        try:
            for r in _parse_wardb_table('ArmyResourceProductionTemplate'):
                rid = int(r.get(1, 0) or 0)
                if not rid:
                    continue
                out.append({
                    'id': rid,
                    'faction': int(r.get(2, 0) or 0),
                    'category_id': int(r.get(3, 0) or 0),
                    'run_every_seconds': int(r.get(4, 0) or 0),
                    'produce_per_run': int(r.get(5, 0) or 0),
                    'war_start_value': int(r.get(6, 0) or 0),
                })
        except Exception as e:
            log(f'[!] ArmyResourceProductionTemplate parse failed: {e}')
        _RTS_CACHE['ar_production'] = out
    return _RTS_CACHE['ar_production']


def at_pricing():
    """AT 购买/升级定价 {objectId: {gold, warfunds, credits}, ...}。

    Pricing 表（f2 type 字符串 / f3 objectId / f4-f6 三币种价格）中
    type 含 'commandnode'（不区分大小写）的行；过滤后为空（type 命名
    未知/macOS 无表）则退回全表按 objectId 索引。"""
    if 'at_pricing' not in _RTS_CACHE:
        matched, allrows = {}, {}
        try:
            for r in _parse_wardb_table('Pricing'):
                oid = int(r.get(3, 0) or 0)
                if not oid:
                    continue
                price = {
                    'gold': _wardb_f32(r.get(4)),
                    'warfunds': _wardb_f32(r.get(5)),
                    'credits': _wardb_f32(r.get(6)),
                }
                allrows.setdefault(oid, price)
                if 'commandnode' in _wardb_str(r.get(2, b'')).lower():
                    matched[oid] = price
        except Exception as e:
            log(f'[!] Pricing parse failed: {e}')
        _RTS_CACHE['at_pricing'] = matched or allrows
    return _RTS_CACHE['at_pricing']

