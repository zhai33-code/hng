# -*- coding: utf-8 -*-
"""WarDatabase 静态数据层：解析游戏静态表，构建解锁/定价/勋带/经验等索引。

模块 import 时即解析 WarDatabase 目录（与原 realtest_server 行为一致）。
依赖 protocol(Chunk/pb_parse) 与 state(WARDDB_DIR/log)。
"""
import hashlib
import os
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
    # 角色模板解锁的 PlayerLevelUnlock id（unlockItemType==eCharacterTemplate，解析自
    # WarDatabase/PlayerLevelUnlock）：置 true 会让客户端为每个阵营×每个已解锁模板生成
    # 一个"可招募"占位士兵（fake slot），点击就弹输入名字创建。保持未解锁，
    # 客户端只显示默认步兵槽位，与官方一致。
    _PLAYER_CHARACTER_TEMPLATE_UNLOCKS = {7, 9, 11, 12, 26}
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

# ShopMembershipItems { id=1, identifier=2, durationDays=3, ... }
# 会员商品时长（天）：AddMembershipRequest membershipTemplateId → durationDays
# （买 180 天老兵必须按表发 180 天，不能统一 30 天）
MEMBERSHIP_DAYS_BY_ID = {
    int(f.get(1, 0)): int(f.get(3, 0))
    for f in _parse_wardb_table('ShopMembershipItems') if int(f.get(1, 0))
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


def _generate_wardb_unique():
    """扫描 WarDatabase 目录，生成 wardb_unique manifest
    格式: "tableName",sha256hex-size\n
    """
    lines = []
    if os.path.isdir(WARDDB_DIR):
        for name in sorted(os.listdir(WARDDB_DIR)):
            fpath = os.path.join(WARDDB_DIR, name)
            if not os.path.isfile(fpath):
                continue
            with open(fpath, 'rb') as f:
                data = f.read()
            sha = hashlib.sha256(data).hexdigest()
            size = len(data)
            lines.append(f'"{name}",{sha}-{size}')
    return ('\n'.join(lines) + '\n').encode('utf-8')


# WarDatabase 文件清单：列出所有静态表文件
WARDB_UNIQUE = _generate_wardb_unique()

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

    无对应行时回退 MISSION_MAP_ENTITY_ID 常量值（112741325='Benoit Village'，
    Mill 图实测行），保证客户端简报查表可用。
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

