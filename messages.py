# -*- coding: utf-8 -*-
"""游戏状态消息构建层：HnG_States.* proto 构建函数（_msg_*）+ KVCS 推送助手。

除 _push_kvcs（writer I/O）外全部为纯函数；依赖 protocol 的 pb 编码原语与
wardb 的静态数据（弹药满耐久/勋带等级阈值/解锁位图长度）。
被 handlers 各域模块与 realtest_server 复用。
"""
import struct

from protocol import (
    Chunk,
    pb_string, pb_int32, pb_int64, pb_uint32, pb_float, pb_message, pb_packed_bool,
)
from wardb import _prog_max, _AMMO_MAXCOND, _RIBBON_LEVEL_XP, _ribbon_level_from_xp


# ---- war_catalogue 构建（用于 query_war_catalogue_response）----
def _msg_war_catalogue_faction(fid, ftid, tag):
    m = b''
    m += pb_int64(1, fid)        # factionId
    m += pb_int64(2, ftid)       # factionTemplateId (1=US 2=GE 3=SU)
    m += pb_string(3, tag)       # factionTag
    m += pb_int32(4, 0)          # factionVictoryPoints
    m += pb_int32(5, 1)          # factionPlayerCount
    m += pb_int32(6, 0)          # factionMinPlayerCount
    m += pb_int32(7, 100)        # factionMaxPlayerCount
    m += pb_int32(8, 1)          # factionPlayerOnlineCount
    m += pb_int32(9, 0)          # factionBonus
    m += pb_int32(10, 0)         # factionDeployedCommandPointsInfantry
    m += pb_int32(11, 0)         # factionDeployedCommandPointsArmor
    m += pb_int32(12, 0)         # factionDeployedCommandPointsAir
    m += pb_int32(13, 0)         # factionControlledBattlefields
    m += pb_uint32(14, 0)        # battlesWon
    m += pb_uint32(15, 0)        # battlesLost
    m += pb_uint32(16, 0)        # infantryLost
    m += pb_uint32(17, 0)        # vehiclesLost
    m += pb_uint32(18, 0)        # tanksLost
    m += pb_uint32(19, 0)        # planesLost
    return m


def _msg_war_catalogue():
    m = b''
    m += pb_int64(1, 1)                   # id (与 player.war=1 匹配)
    m += pb_string(2, 'Demo War')         # name
    m += pb_string(3, '2026-08-15 04:00:00')  # wartime
    m += pb_int32(4, 100)                 # pointsNeededForVictory
    m += pb_int64(5, 0)                  # winningFactionId (0=进行中)
    m += pb_int32(6, 1)                  # battlefieldCount
    m += pb_int32(7, 3)                  # assaultTeamCount
    m += pb_int32(8, 3)                  # playerCount
    m += pb_int32(9, 0)                  # generalCount
    m += pb_uint32(10, 100)              # infantryCount
    m += pb_uint32(11, 10)               # tankCount
    m += pb_uint32(12, 5)                # planeCount
    m += pb_uint32(13, 10)               # vehicleCount
    m += pb_int64(14, 0)                 # sequelWarId
    m += pb_message(15, _msg_war_catalogue_faction(1, 1, 'US'))  # US
    m += pb_message(15, _msg_war_catalogue_faction(2, 2, 'GE'))  # GE
    m += pb_message(15, _msg_war_catalogue_faction(3, 3, 'SU'))  # SU
    return m


def _msg_character(name='Soldier', faction=1, template=1, rank=0, char_id=1,
                   unlock_bits=None, combat_badge_0=0, combat_badge_1=0,
                   stat_update_ts=0, uniform_variant_id=0, headgear_id=0,
                   marked_deleted=0, previous_template=0, head_id=0,
                   head_swap_type=0, is_on_leave=0):
    """构建 HnG_States.character proto，用于 SOLDIER_VIEW 推送"""
    m = b''
    m += pb_int64(1, char_id)               # id（数据库 characters.id）
    m += pb_string(2, name)                # name
    m += pb_int64(3, 1)                    # playerid
    m += pb_int64(4, template)             # charactertemplateid (从 pack 解析)
    m += pb_int64(5, faction)              # factiontemplateid (从 pack 解析)
    m += pb_int64(10, combat_badge_0)      # combatbadgeslot0
    m += pb_int64(11, combat_badge_1)      # combatbadgeslot1
    m += pb_int32(13, rank)                # rank (由 combat_xp 经 ExperienceLevels 阈值推导)
    m += pb_int32(14, marked_deleted)      # markedDeleted（软删标记，DeleteCharacterRequest 后置 1）
    m += pb_int32(15, 0)                   # firstBloodStatus
    # specialCharacterFlag (field 16): 飞行员/伞兵/将军是官方特殊角色
    # （客户端 setupRankUnlocks 等按特殊角色渲染军衔树），其余职业为普通角色
    m += pb_int32(16, 1 if int(template) in (2, 4, 7) else 0)
    m += pb_int64(17, uniform_variant_id)  # uniformVariantId（装备的军服迷彩变体实例 id）
    m += pb_int64(18, headgear_id)         # headgearId（装备的头盔实例 id，UniformPart.id）
    m += pb_int64(19, previous_template)   # previousCharacterTemplateId（兵种切换前的旧模板）
    # statUpdateTimeStamp (field 20): 变化时客户端 CharacterModel.checkStatsWasUpdated
    # 会重新请求 RequestReadCharacterStats → attributes 刷新（勋带等级/进度条/按钮恢复）。
    # 购买勋带等级后必须传新时间戳，否则客户端看不到进度更新。
    m += pb_int64(20, stat_update_ts)
    # unlockBits (field 21): repeated bool, 位索引 = progressionUnlock.id
    # 由 _char_unlock_bits_for 生成：默认解锁序列 + 已购物品解锁需求
    if unlock_bits is None:
        unlock_bits = [False] * (_prog_max + 1)
    m += pb_packed_bool(21, unlock_bits)
    # headId (field 22): UniformPartTemplate 模板 id（faction3 可选 0/89/90），
    # 不是 UniformPart 实例 id——客户端 UniformPartTemplateTable.getFromId(f22)
    # 按模板表查询，实例 id 会查不到 → getUniformPartIdsEquipped #1009 崩溃
    m += pb_int64(22, head_id)             # 0=默认头
    m += pb_int32(23, head_swap_type)      # eHeadSwapType（0=eFree 1=eNeedPayment）
    m += pb_int32(24, is_on_leave)         # isOnLeave（SetCharacterActiveStatusRequest）
    return m


def _msg_weapon(wid, name, template_id, char_id, container_id=0, container_slot=0,
                variant_id=0, clip_count=0, auto_repair=0, condition=100.0):
    """构建 HnG_States.weapon proto（玩家武器实例），用于 SOLDIER_VIEW 推送。

    clipCount f14：三态弹夹选择（负=最少 / 0=默认 / 正=最大，weapon KVCS 必填）。"""
    m = b''
    m += pb_int64(1, wid)                  # id
    m += pb_string(2, name)                # name
    m += pb_int64(3, template_id)          # weapontemplateid
    m += pb_int64(4, 1)                    # playerid
    m += pb_int64(5, char_id)              # characterId (0 = 仓库)
    m += pb_int32(6, 0)                    # posx
    m += pb_int32(7, 0)                    # posy
    m += pb_string(8, '')                  # decals
    m += pb_string(9, '')                  # containerType
    m += pb_int64(10, container_id)        # containerId (装备时 = character.id)
    m += pb_int32(11, container_slot)      # containerSlot (1-4; 0 = 未装备)
    m += pb_float(12, condition)           # condition
    m += pb_int32(13, auto_repair)         # autoRepair（SetWeaponAutoRepairRequest）
    m += pb_int32(14, int(clip_count))     # clipCount（三态弹夹选择）
    m += pb_int64(15, variant_id)          # equippedWeaponVariantId
    return m


def _msg_weapon_modifier(mid, template_id, char_id, weapon_id, auto_repair=0,
                         condition=1.0):
    """构建 HnG_States.WeaponModifier proto（武器配件实例），用于 SOLDIER_VIEW 推送。

    condition 量纲是 0~1 分数（WeaponModifierTemplate.maxCondition 全表=1.0），
    满=1.0；推 100.0 会被客户端算出负的损坏比例 → Math.max(1,…) = 1 块钱维修费。"""
    m = b''
    m += pb_int64(1, mid)              # id
    m += pb_int64(2, template_id)      # templateId
    m += pb_int64(3, 1)                # playerId
    m += pb_int64(4, char_id)          # characterId
    m += pb_int64(5, weapon_id)        # weaponId
    m += pb_float(6, condition)        # condition
    m += pb_int32(7, auto_repair)      # autoRepair（SetWeaponModiferAutoRepairRequest）
    return m


def _msg_weapon_ammo(aid, ammo_template_id, char_id, weapon_id, slot_index=0,
                     condition=None, auto_repair=0):
    """构建 HnG_States.WeaponAmmo proto（武器弹药实例），用于 SOLDIER_VIEW 推送"""
    if condition is None:
        condition = _AMMO_MAXCOND.get(int(ammo_template_id), 100.0)
    m = b''
    m += pb_int64(1, aid)                  # id
    m += pb_int64(2, ammo_template_id)     # ammoTemplateId
    m += pb_int64(3, 1)                    # playerId
    m += pb_int64(4, char_id)              # characterId
    m += pb_int64(5, weapon_id)            # weaponId
    m += pb_int32(6, slot_index)           # equippedAmmoSlotIndex (0=未装备)
    m += pb_float(7, condition)            # condition（新弹药=模板 maxCondition 满耐久）
    m += pb_int32(8, auto_repair)          # autoRepair（SetWeaponAmmoAutoRepairRequest）
    return m


async def _push_kvcs(writer, view_cmd, table_name, proto_data):
    """通过已订阅视图的 cmd_id 推送 KeyValueChangeSet 表数据（单行）"""
    data_chunk = Chunk(proto_data)
    table_chunk = Chunk(table_name, [data_chunk])
    action_chunk = Chunk(b'set', [table_chunk])
    title_chunk = Chunk(b'KeyValueChangeSet', [action_chunk])
    main = Chunk(struct.pack('<i', view_cmd), [title_chunk])
    writer.write(main.write(b''))
    await writer.drain()


async def _push_kvcs_delete(writer, view_cmd, table_name, row_id):
    """通过已订阅视图的 cmd_id 推送 KeyValueChangeSet delete 行
    （行内容为 8 字节 little-endian 行 id，与 matchmaking 战局清理一致）"""
    id_chunk = Chunk(struct.pack('<q', int(row_id)))
    table_chunk = Chunk(table_name, [id_chunk])
    action_chunk = Chunk(b'delete', [table_chunk])
    title_chunk = Chunk(b'KeyValueChangeSet', [action_chunk])
    main = Chunk(struct.pack('<i', view_cmd), [title_chunk])
    writer.write(main.write(b''))
    await writer.drain()


def _msg_store_campaign(campaign_id, ctype):
    """构建 HnG_States.StoreCampaign proto（商店战役/折扣条目）。
    activeFromTime/activeToTime 为 Zulu 格式 YYYYMMDDHHmmss（GMT），
    客户端 TimeUtilities.convertZuluTimeToMilliseconds 解析；须覆盖当前服务器时间，
    否则 checkIfCampaignShouldBeShownInCurrent 过滤掉，轮播仍为空。"""
    m = b''
    m += pb_int64(1, campaign_id)             # id
    m += pb_int32(2, ctype)                   # type: 1=eDiscount 2=eCampaign
    m += pb_string(3, str(campaign_id))       # assetIdentifier → campaignAssetId → images/Campaign/<id>.png
    m += pb_string(4, '')                     # callToAction
    m += pb_string(5, '')                     # iconType
    m += pb_string(6, '20260816000000')       # activeFromTime
    m += pb_string(7, '20990101000000')       # activeToTime
    m += pb_string(8, f'Campaign {campaign_id}')
    m += pb_int32(9, 0)                       # countdownHours
    return m


def _msg_weapon_variant(variant_id, char_id, weapon_id, variant_template_id):
    """构建 HnG_States.WeaponVariant proto（武器皮肤实例）"""
    m = b''
    m += pb_int64(1, variant_id)              # id
    m += pb_int64(2, 1)                       # playerId
    m += pb_int64(3, char_id)                 # characterId
    m += pb_int64(4, weapon_id)               # weaponId
    m += pb_int64(5, variant_template_id)     # weaponVariantTemplateId
    return m


def _msg_vehicle(vid, vehicle_variant_template_id, char_id, equipped_in_slot=0, variant_id=0,
                 auto_repair=0, condition=100.0):
    """构建 HnG_States.Vehicle proto（玩家载具实例），用于 SOLDIER_VIEW 推送。
    表名注册为 'Vehicle'（VehicleTable.xmlTag），与武器 'weapon' 不同注意大小写。"""
    m = b''
    m += pb_int64(1, vid)                        # id
    m += pb_int64(3, vehicle_variant_template_id)  # vehicleVariantTemplateId
    m += pb_int64(4, 1)                          # playerId
    m += pb_int64(5, char_id)                    # characterId
    m += pb_int64(6, equipped_in_slot)           # equippedInSlot
    m += pb_float(7, condition)                  # condition
    m += pb_int32(8, auto_repair)                # autoRepair（SetVehicleAutoRepairRequest）
    m += pb_int64(9, variant_id)                 # equippedVehicleVariantId（装备的皮肤实例 id）
    return m


def _msg_vehicle_variant(vid, char_id, vehicle_id, variant_template_id):
    """构建 HnG_States.VehicleVariant proto（载具皮肤实例），用于 SOLDIER_VIEW 推送。
    表名注册为 'VehicleVariant'（VehicleVariantTable.xmlTag）。"""
    m = b''
    m += pb_int64(1, vid)                       # id
    m += pb_int64(2, 1)                         # playerId
    m += pb_int64(3, char_id)                   # characterId
    m += pb_int64(4, vehicle_id)                # vehicleId
    m += pb_int64(5, variant_template_id)       # vehicleVariantTemplateId
    return m


def _msg_vehicle_upgrade(uid, upgrade_template_id, vehicle_id, slot_id, char_id,
                         condition=100.0, auto_repair=0):
    """构建 HnG_States.VehicleUpgrade proto（载具升级/补给箱实例），用于 SOLDIER_VIEW 推送。
    表名注册为 'VehicleUpgrade'（VehicleUpgradeTable.xmlTag）。"""
    m = b''
    m += pb_int64(1, uid)                       # id
    m += pb_int64(2, upgrade_template_id)       # vehicleUpgradeTemplateId
    m += pb_int64(3, vehicle_id)                # vehicleId
    m += pb_int64(4, slot_id)                   # equippedVehicleUpgradeSlotId (0=未装备)
    m += pb_int64(5, 1)                         # playerId
    m += pb_int64(6, char_id)                   # characterId
    m += pb_float(7, condition)                 # condition
    m += pb_int32(8, auto_repair)               # autoRepair（SetVehicleUpgradeAutoRepairRequest）
    return m


def _msg_vehicle_ammo(aid, ammo_template_id, vehicle_id, storage_id, slot_index,
                      char_id, condition=100.0, auto_repair=0):
    """构建 HnG_States.VehicleAmmo proto（载具弹药实例），用于 SOLDIER_VIEW 推送。
    表名注册为 'VehicleAmmo'（VehicleAmmoTable.xmlTag）。"""
    m = b''
    m += pb_int64(1, aid)                       # id
    m += pb_int64(2, ammo_template_id)          # ammoTemplateId
    m += pb_int64(3, vehicle_id)                # vehicleId
    m += pb_int64(4, storage_id)                # equippedVehicleAmmoStorageId (0=未装备)
    m += pb_int32(5, slot_index)                # equippedAmmoSlotIndex (0=未装备)
    m += pb_int64(6, 1)                         # playerId
    m += pb_int64(7, char_id)                   # characterId
    m += pb_float(8, condition)                 # condition
    m += pb_int32(9, auto_repair)               # autoRepair（SetVehicleAmmoAutoRepairRequest）
    return m


def _msg_uniform_variant(vid, template_id, char_id):
    """构建 HnG_States.UniformVariant proto（军服迷彩变体实例），用于 SOLDIER_VIEW 推送。
    表名注册为 'UniformVariant'（UniformVariantTable.xmlTag）。"""
    m = b''
    m += pb_int64(1, vid)                       # id
    m += pb_int64(2, 1)                         # playerId
    m += pb_int64(3, char_id)                   # characterId
    m += pb_int64(4, template_id)               # uniformVariantTemplateId
    return m


def _msg_uniform_part(pid2, char_id, template_id, part_type):
    """构建 HnG_States.UniformPart proto（头盔/军装部件实例），用于 SOLDIER_VIEW 推送。
    表名注册为 'UniformPart'（UniformPartTable.xmlTag）。"""
    m = b''
    m += pb_int64(1, pid2)                 # id
    m += pb_int64(2, 1)                    # playerId
    m += pb_int64(3, char_id)              # characterId
    m += pb_int64(4, template_id)          # uniformPartTemplateId
    m += pb_int32(5, part_type)            # uniformPartType (EUniformPartType: eHelmet=1)
    return m


def _msg_ribbon(ribbon_template_id, total_xp):
    """构建 statrequests.Ribbon proto（武器勋带进度）。
    currentValue=累计XP, currentLevel=XP 推导等级, nextValue=下一级阈值。
    客户端 getProgressionPercentages 用 currentValue/nextLevel.value 计算进度。"""
    lv = _ribbon_level_from_xp(total_xp)
    next_xp = _RIBBON_LEVEL_XP.get(lv + 1, _RIBBON_LEVEL_XP.get(lv, 0))
    m = b''
    m += pb_int64(1, ribbon_template_id)   # ribbonTemplateId
    m += pb_int32(2, int(total_xp))        # currentValue (累计XP)
    m += pb_int32(3, lv)                   # currentLevel (XP 推导)
    m += pb_int32(4, next_xp)              # nextValue (下一级阈值)
    return m


def _msg_character_stats_entry(char_id, ribbons, combat_xp=0):
    """构建 statrequests.ResponseReadCharacterStatsEntry proto：
    characterId=1, alltimeCharacterStatistics=2, attributes=3 (repeated Ribbon)。
    alltimeCharacterStatistics.characterXp 是字段 35（客户端 Global.characterExperience
    ["combat"] 从这里读取，用于军衔进度条）；其余统计字段暂为 0。"""
    stats = b''
    for field_num in range(1, 50):  # fields 1-49 (skip 36, which is missing in proto)
        if field_num == 36:
            continue
        val = combat_xp if field_num == 35 else 0
        stats += pb_int32(field_num, val)
    stats += pb_string(56, '')  # key (required string)
    m = b''
    m += pb_int64(1, char_id)          # characterId
    m += pb_message(2, stats)          # alltimeCharacterStatistics
    for (rid, lv) in ribbons:
        m += pb_message(3, _msg_ribbon(rid, lv))  # attributes (repeated Ribbon)
    return m


def msg_query_war_catalogue_response():
    m = b''
    m += pb_message(1, _msg_war_catalogue())  # warcataloguedata (repeated)
    m += pb_int64(2, 0)                        # bonusId (required)
    return m


# ---- war 消息（用于 KeyValueChangeSet 中的 "war" 表数据）----
def _msg_war():
    """构建 HnG_States.war proto，id=1 与 player.war 匹配"""
    m = b''
    m += pb_int64(1, 1)                    # id
    m += pb_string(2, 'Demo War')          # name
    m += pb_string(3, '2026-08-15 04:00:00')  # begintime
    m += pb_string(4, '')                  # endtime (空=进行中)
    m += pb_string(5, '')                  # web
    m += pb_string(6, '0')                 # friendlyfire
    m += pb_int64(7, 0)                    # winningfactionid (0=无获胜方)
    m += pb_int32(8, 100)                  # pointsNeededForVictory
    m += pb_uint32(9, 0)                   # automissionnormal
    m += pb_uint32(10, 0)                  # automissionskirmish
    m += pb_uint32(11, 0)                  # automissionsupply
    m += pb_float(12, 1.0)                 # strategytimescale
    m += pb_float(13, 1.0)                 # daycycletimescale
    m += pb_uint32(14, 0)                  # disablestrategy
    m += pb_int32(15, 100)                 # maxPlayerPerFaction
    m += pb_int64(16, 0)                  # sequelwarid
    m += pb_int64(17, 1)                  # mapid
    m += pb_int32(18, 0)                   # cleanupStatus
    m += pb_int32(19, 0)                   # fowGridSize
    m += pb_int32(20, 0)                   # fowGridOriginX
    m += pb_int32(21, 0)                   # fowGridOriginY
    m += pb_int32(22, 0)                   # queueReinforcements (bool=false)
    m += pb_int32(23, 0)                   # firstResourceFree (bool=false)
    m += pb_uint32(24, 0)                  # moraleMinValue
    m += pb_uint32(25, 100)                # moraleMaxValue
    m += pb_uint32(26, 0)                  # moraleLostPerBattle
    m += pb_uint32(27, 0)                  # moraleGainedPerWin
    m += pb_float(28, 0.0)                 # moraleGainedPerMinute
    m += pb_uint32(29, 0)                  # moraleSurrenderLimit
    return m
