# -*- coding: utf-8 -*-
"""warserver 侧（游戏进程 hng.exe/player.dll，ZEngine C++）消息构建。

与 messages.py（Flash/actionhost HnG_States 协议族）平行：本模块构建
游戏进程协议的响应消息 —— PlayerSettings / CharacterEntry / WeaponSetting /
VehicleSetting / RibbonProgress / ResponseBattleFieldSettings。
schema 来源：player.dll protobuf 描述符 @ RVA 0x7b3f78（逆向确认，40 字段）。
注意：描述符 schema 与字段名表(0x7b2e58)完全不同 —— 名表是 HTTP/Flash 客户端
协议字段，描述符定义的是 hng.exe/player.dll 实际使用的 protobuf schema。
请求解析与分发见 game_server.py；装备 DB 聚合见 player_service._load_db_loadout。
"""
import time

from protocol import (
    pb_bool,
    pb_float,
    pb_int32,
    pb_int64,
    pb_message,
    pb_string,
    pb_uint32,
    pb_varint,
    )

from state import log

import force_pool
from wardb import (
    _RIBBON_LEVEL_XP,
    _VEHICLE_VARIANT_PARENT,
    _load_accesspoint_templates,
    _load_preset,
    _rank_from_combat_xp,
    _ribbon_level_from_xp,
    assault_ap_decision,
    battlefield_entity_for_gamemap,
    battlefield_pos,
    charactertemplate_actionid,
    default_uniform_variant,
    is_general_character,
    spawn_clips_for,
    vehicle_ammo_spawn_clips,
    )


# player.dll 描述符 @ RVA 0x7b3f78（PlayerSettings 响应，40 字段）
# 逆向方法：tmp_dump_desc2.py 解析 64 字节字段描述符，提取 field_number / type_name / object_offset
# 与字段名表(0x7b2e58)完全不同 —— 名表用于 HTTP/Flash 协议，描述符用于游戏进程 protobuf
#
#  field#  type_name                        offset  size  wire_type(推断)
#    1     player_id                        0x000   8     varint(int64)  ← 必须非零
#    2     gamertag                          0x008   8     string
#    3     invertmouse                      0x010   4     varint(bool)
#    4     invertmouseairplane              0x014   4     varint(bool)
#    5     invertmouseturret                0x018   4     varint(bool)
#    6     mousesensitivity                  0x01c   4     varint(int32)
#    7     factionTemplateId                0x020   8     varint(int64)
#    8     mousecontrolledcars              0x028   4     varint(bool)
#    9     showcrosshair                    0x02c   4     varint(bool)
#   10     showcrosshair_vehicle             0x030   4     varint(bool)
#   11     clothing                          0x038   8     (skip: string/int64 ambiguous)
#   12     characterlist (repeated msg)      0x040  120    (empty)
#   13     chatchannels (repeated msg)       0x050  16     (empty)
#   -- field 14 not in descriptor --
#   15     missionCharacterId                0x060   8     varint(int64)
#   16     squadId                           0x068   8     varint(int64)
#   17     squadSlotIndex                    0x070   4     varint(int32)
#   18     squadPoints                       0x074   4     varint(int32)
#   19     wasInvitedToSquad                 0x078   4     varint(bool)
#   20     yellowGriefPoints                 0x07c   4     varint(int32)
#   21     redGriefPoints                    0x080   4     varint(int32)
#   22     metricsUid                        0x088   8     varint(int64)
#   23     metricsSessionId                  0x090   8     varint(int64)
#   24     firstActionGame                   0x098   4     varint(int32)
#   25     missionSessionId                  0x0a0   8     varint(int64)  ← pms_id
#   26     playerMembershipBenefitTemplateId 0x0a8   8     varint(int64)
#   27     defaultMembershipBenefitTemplateId 0x0b0   8     varint(int64)
#   28     achievementsUnlocked (repeated)    0x0b8   8     (empty)
#   29     platform                          0x0c8   8     (skip: string/int64 ambiguous)
#   30     isQuickPlay                       0x0d0   4     varint(bool)
#   31     isQuickPlaySelectedFaction        0x0d4   4     varint(bool)
#   32     countryCode                       0x0d8   8     string
#   33     lastLoginTimestampInSecond        0x0e0   8     varint(int64)
#   34     playerPartnerId                   0x0e8   4     varint(int32)
#   35     activeSoldierXPBoosterId          0x0f0   8     varint(int64)
#   36     soldierWarBonusXPPercentage       0x0f8   4     varint(int32)
#   37     careerOrderStatus                 0x100   8     (skip: string/int64 ambiguous)
#   38     triggeredTutorialTemplateIds(rep) 0x108   8     (empty)
#   39     isChatModerator                   0x110   4     varint(bool)
#  999     eos_openid_token                  0x118   8     string         ★ 关键！
# 1000     eos_openid_expires_in             0x120   4     varint(int32)


# ===== 回放对齐常量已清除（2026-08-20）=====
# 他服抓包的 PVT_* 静态值全部移除：
#   player/char/gamertag → 账号库（PMS_TO_SESSION 会话 → PostgreSQL players/characters）
#   squad/metrics/battle-session id → matchmaking._next_match_id 按局发号
#   warId → matchmaking._WAR_ID（服务器启动时生成）
#   mapEntityId / hostingCenterId → matchmaking 库引用常量（WarDatabase battlefield 行 id）


def _msg_player_settings(player_id, pms_id, char_id, eos_token=b'',
                         display_name='Player',
                         metrics_uid='',
                         metrics_session_id=0,
                         mission_session_id=0,
                         squad_id=0,
                         squad_slot=0,
                         faction=3,
                         db_chars=None, loadout=None,
                         quickplay=None, first_action_game=None):
    """构建 PlayerSettings 响应（GetPlayerSettings 的应答）—— 2026-08-20 起全动态：

    f12 characterlist 直接由账号库角色 + 装备聚合构建（不再用回放字节做底、
    也不再克隆回放装备）：库里有什么角色/装备就发什么。
    其余账号级字段（鼠标灵敏度/教程/解锁串等）无库表来源，用逆向确认的默认值。
    """
    # field 12 characterlist：账号库角色（每角色武器/载具/徽章/军衔/军服全 DB 构建）
    # ★ 只发当前参战阵营的角色：charactersetting 无阵营字段，战斗客户端部署
    #   兵种轮播不做过滤 —— 官方服按参战阵营下发（此前全量下发导致 SU 局
    #   能选到 GE 士兵，2026-08-21 修复）。车库/军械库不受影响（character
    #   KVCS 仍全量推送并带真实阵营）。
    # db_chars 缺失（DB 读失败）时退 1 条最小条目（joined char_id 来自会话）
    joined = None
    if db_chars:
        for ch in db_chars:
            if int(ch['id']) == int(char_id or 0):
                joined = ch
                break
    faction_chars = [ch for ch in (db_chars or [])
                     if int(ch.get('faction', 0) or 0) == int(faction or 0)]
    # ★ 将军不进备选士兵列表（官方设计将军只在大地图指挥 AssaultTeam；
    #   其 actionid 与伞兵同为 7，混进 f12 会被客户端当伞兵选中。
    #   入口拦截（JoinMatchMaking/_squad_eligible_members）只挡"用将军
    #   点匹配"，此处清洗已进局玩家同阵营的全量角色 —— 2026-09-05 修复）
    combat_chars = [ch for ch in faction_chars
                    if not is_general_character(ch.get('template', 0))]
    if len(combat_chars) != len(faction_chars):
        log(f'      [FULL] PlayerSettings: filtered out '
            f'{len(faction_chars) - len(combat_chars)} general char(s) '
            f'from characterlist (generals cannot enter combat)')
    char_entries = [_msg_character_entry(ch, loadout) for ch in combat_chars]
    if not char_entries:
        e = b''
        e += pb_int64(1, int(char_id or 0))
        e += pb_string(2, 'Soldier')
        e += pb_int32(7, 0)                      # 7 rank
        e += pb_int32(8, 0)                      # 8 level
        e += pb_int32(9, 0)                      # 9 xp
        e += pb_int32(11, 0)                     # 11 ribbonCount
        char_entries.append(pb_message(12, e))
    # f18 squadPoints ← 本次进局角色的 characters.squad_points
    squad_points = int(joined.get('squad_points', 0) or 0) if joined else 0

    # field 28 triggeredTutorialTemplateIds: 私服 = [1,2,3,4,6,7,8,...,29] (跳过 5)
    tutorial_ids = [1, 2, 3, 4] + list(range(6, 30))  # 28 个值

    # field 37 careerOrderStatus: 私服 93 字符逗号分隔 0/1 串
    weapon_unlocks = ('0,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,0,1,1,0,1,1,1,1,1,1,1,1,1,'
                      '0,1,1,1,1,0,1,1,1,0,1,1,1,0,0,1')

    m = b''
    m += pb_int64(1, player_id)                 # 1 player_id — 必须等于 JWT sub
    m += pb_string(2, display_name)              # 2 gamertag
    m += pb_bool(3, False)                       # 3 invertmouse
    m += pb_bool(4, False)                       # 4 invertmouseairplane
    m += pb_bool(5, False)                       # 5 invertmouseturret
    m += pb_float(6, 0.4)                        # 6 mousesensitivity (f32)
    m += pb_int64(7, faction)                    # 7 factionTemplateId（1US/2GE/3SU）
    m += pb_bool(8, False)                       # 8 mousecontrolledcars
    m += pb_bool(9, True)                        # 9 showcrosshair
    m += pb_bool(10, True)                       # 10 showcrosshair_vehicle
    m += pb_string(11, '')                       # 11 clothing (空串)
    m += b''.join(char_entries)                  # 12 characterlist（账号库角色）
    m += pb_int64(15, char_id)                   # 15 missionCharacterId
    m += pb_int64(16, squad_id)                  # 16 squadId
    m += pb_int32(17, int(squad_slot or 0))      # 17 squadSlotIndex：必须与 SquadResponse
                                                #    playerSlots 里实际存在的 index 对齐
                                                #    （成局时 matchmaking 按队内槽位发号；
                                                #    队长=0，其余成员依次 1..N-1）。
                                                #    此前=2 而 squad 定义只有 index 0 →
                                                #    ZServer InitPlayer "#error3 specified squad
                                                #    character slot not found"，客户端部署逻辑
                                                #    按槽位 2 查询得 NULL → 空指针崩溃
                                                #    (player+0x394CD6, dump 36764 实锤)。
    m += pb_int32(18, squad_points)              # 18 squadPoints（← characters.squad_points）
    m += pb_bool(19, False)                      # 19 wasInvitedToSquad
    m += pb_int32(20, 0)                         # 20 yellowGriefPoints
    m += pb_int32(21, 0)                         # 21 redGriefPoints
    m += pb_string(22, metrics_uid)              # 22 metricsUid（=PMS f17）
    m += pb_int64(23, metrics_session_id)        # 23 metricsSessionId（=PMS f18）
    m += pb_bool(24, bool(first_action_game))    # 24 firstActionGame（全角色 xp==0 → 首局）
    m += pb_int64(25, mission_session_id)        # 25 missionSessionId（=PMS f9 同源）
    m += pb_int64(26, 2)                         # 26 playerMembershipBenefitTemplateId
    m += pb_int64(27, 2)                         # 27 defaultMembershipBenefitTemplateId
    for tid in tutorial_ids:                     # 28 triggeredTutorialTemplateIds (repeated)
        m += pb_int32(28, tid)
    m += pb_string(29, 'HnG')                    # 29 platform
    m += pb_bool(30, bool(quickplay))            # 30 isQuickPlay（← 匹配请求）
    m += pb_bool(31, False)                      # 31 isQuickPlaySelectedFaction
    m += pb_string(32, 'CN')                     # 32 countryCode
    m += pb_int64(33, int(time.time()))           # 33 lastLoginTimestampInSecond
    m += pb_int32(34, 0)                          # 34 playerPartnerId
    m += pb_int64(35, 0)                          # 35 activeSoldierXPBoosterId
    m += pb_int32(36, 0)                         # 36 soldierWarBonusXPPercentage
    m += pb_string(37, weapon_unlocks)           # 37 careerOrderStatus (逗号串)
    m += pb_string(38, '3,1,2,8,18,22,')         # 38 (逗号串)
    m += pb_bool(39, False)                      # 39 isChatModerator
    if eos_token:
        m += pb_string(999, eos_token)            # 999 eos_openid_token ★
        m += pb_int32(1000, 300)                  # 1000 eos_openid_expires_in
    log(f'      [FULL] PlayerSettings: field1(player_id)={player_id}, field15(char)={char_id}, '
        f'chars={len(char_entries)}, field22(metricsUid)={metrics_uid}, '
        f'field25(missionSession)={mission_session_id}, '
        f'field999(token)={len(eos_token)}B')
    return m


# ===== PlayerSettings 账号库动态组装（2026-08-20 全动态定稿）=====
# 历史：曾以他服抓包字节（pvt_player_settings.bin）打底做字段替换/装备模板
# 克隆，现已全部移除 —— 库里有什么角色/装备就发什么（Alexei/Hans 无装备时
# 发空装备条目，repeated 空字段整体省略），无任何回放兜底路径。
# 关键 id 对齐事实（保留备查）：
#   - 客户端部署界面与 RequestReadCharacterStats 用 characters 表小 id
#   - ZServer sub_1802AE3E0(gamecontroller.cpp) 用 f17 在 SquadResponse
#     playerSlots 里找槽位 → f17 必须与 playerSlots index 对齐(=0)
def _msg_character_entry(ch, loadout=None):
    """PlayerSettings f12 角色条目（charactersetting，§7.2）—— 100% 账号库构建。

      f1 id / f2 name / f3 武器(含弹药/配件/耐久/出生弹夹) / f4 载具(含迷彩/升级/弹药)
      f5 勋带加成{f1 勋带模板, f2 乘数}(fixed32)
      f6 战斗徽章 / f7 兵种 actionid(charactertemplate.f4，游戏端部署方式判定) /
      f8 军衔(combat_xp 推导)
      f9 军服迷彩模板 / f10 已装备军装部件(头盔) / f11 squadPoints / f12 receiveDailyBonus
    ★ f3 只下发已装备武器（weapons.container_slot>0，槽位用真实值）；
      f4 只下发已装备载具（vehicles.equipped_in_slot>0）——
      未装备武器/载具只在军械库/车库（KVCS）显示，不进战斗配装。
      没有已装备武器/载具的角色发空装备（repeated 空字段整体省略）。
      loadout=None（DB 装备聚合失败）等同空装备。
    """
    if loadout is None:
        loadout = {'weapons': {}, 'w_ammo': {}, 'w_mods': {}, 'vehicles': {},
                   'v_ups': {}, 'v_ammo': {}, 'vv_tmpl': {},
                   'wv_tmpl': {}, 'wv_owned': {}, 'tr_tmpl': {},
                   'uv_tmpl': {}, 'u_parts': {}, 'up_tmpl': {},
                   'rboosters': {}}
    xp = int(ch.get('combat_xp', 0) or 0)
    rank = _rank_from_combat_xp(xp)

    # ---- DB 装备路径 ----
    cid = int(ch['id'])
    out = b''
    out += pb_int64(1, cid)
    out += pb_string(2, ch['name'])

    # f3 武器：仅下发已装备武器（container_slot>0，槽位用真实值）。
    # 未装备武器只存在于军械库（weapon KVCS containerSlot=0），不进战斗配装
    # —— 此前给未装备武器伪造空闲槽位 1-4，导致"拥有的武器全部上阵"（2026-08-21 修复）
    for w in loadout['weapons'].get(cid, []):
        slot = int(w['container_slot'] or 0)
        if slot <= 0:
            continue
        out += _msg_weapon_setting(
            w, loadout['w_ammo'].get(int(w['id']), []),
            loadout['w_mods'].get(int(w['id']), []), slot,
            loadout.get('wv_tmpl'), loadout.get('wv_owned'),
            loadout.get('tr_tmpl'))

    # f4 载具：仅下发已装备载具（equipped_in_slot>0，与 f3 武器同原则）。
    # 未装备载具只在车库（Vehicle KVCS）显示，不进战斗配装
    for v in loadout['vehicles'].get(cid, []):
        if int(v.get('equipped_in_slot', 0) or 0) <= 0:
            continue
        out += _msg_vehicle_setting(
            v, loadout['v_ups'].get(int(v['id']), []),
            loadout['v_ammo'].get(int(v['id']), []),
            loadout.get('vv_tmpl'))

    # f5 勋带加成（账号级激活中的 RibbonBooster，每个角色条目同发）：
    # {f1 ribbonTemplateId, f2 boosterMultiplier(fixed32)}——战斗客户端按乘数
    # 放大局内勋带经验并在结算 BattleReport f13 回报放大后的 endXp，
    # 服务端 set_ribbon_xp_floor 落库（此前缺发 → 激活的加成对战斗无效果）
    for rb_rid, rb_mul in sorted(loadout.get('rboosters', {}).items()):
        bm = pb_int64(1, int(rb_rid))
        bm += pb_float(2, float(rb_mul))
        out += pb_message(5, bm)

    # f6 战斗徽章（DB characters.combat_badge_0/1，0 = 空槽省略）
    for b in (ch.get('combat_badge_0', 0), ch.get('combat_badge_1', 0)):
        if int(b or 0):
            out += pb_int32(6, int(b))
    # f7 兵种 actionid（charactertemplate.f4：rifleman=2/fighterpilot=11/
    # paratrooper=7/tankcrew=10/recon=6/general=7）—— 游戏端按此识别兵种：
    # 飞行员(11)只能空中出生且飞机才显示在部署槽位；此前误发 rank 导致
    # 飞行员被当步兵 → 徒步部署、飞机不显示（2026-08-25 回放对照修复：
    # Yaroslav(飞行员) f7=11 / Kaspar(步兵) f7=2 / Arkady(坦克) f7=10）
    # f8 军衔（回放对照：Artyom f8=13 == PMS characterRank）
    out += pb_int32(7, charactertemplate_actionid(ch.get('template', 1)))
    out += pb_int32(8, rank)
    # f9 军服迷彩模板（characters.uniform_variant_id 实例 id → uniform_variants 模板；
    # 无实例时回退 (兵种,阵营) 默认变体 —— 回放里官方服每个角色 f9 恒非零，
    # 缺失则客户端无法构建士兵外观模型，角色不显示）
    uvt = loadout['uv_tmpl'].get(int(ch.get('uniform_variant_id', 0) or 0), 0)
    if not uvt:
        uvt = default_uniform_variant(ch.get('template', 1), ch.get('faction', 0))
    if uvt:
        out += pb_int64(9, int(uvt))
    # f10 军装部件模板：只发已装备的（characters.headgear_id → uniform_parts
    # 实例 → 模板）。此前发角色名下全部 uniform_parts（含未装备的），
    # 买了第二顶头盔未装备时战斗外观会被顶掉（2026-08-28 修复，
    # 与 f3/f4/f6 只发已装备同原则）；未装备（headgear_id=0）= 默认外观，省略
    hid = int(ch.get('headgear_id', 0) or 0)
    if hid:
        hpt = int(loadout.get('up_tmpl', {}).get(hid, 0) or 0)
        if hpt:
            out += pb_int64(10, hpt)
    # f11 squadPoints / f12 receiveDailyBonus（官方服恒发，含 0）
    out += pb_int32(11, int(ch.get('squad_points', 0) or 0))
    out += pb_bool(12, bool(ch.get('receive_daily_bonus', True)))
    return pb_message(12, out)


def _msg_weapon_setting(w, ammos, mods, slot, wv_tmpl=None, wv_owned=None,
                        tr_tmpl=None):
    """charactersetting.f3 WeaponSetting（§7.2，wire 格式按回放实测）：
    f1 id / f2 template / f3 弹药(含耐久 fixed32) / f4 配件 / f5 tracer /
    f6 decals(官方服恒发空消息，按回放字节镜像) / f7 spawnClips /
    f8 equipmentSlot / f9 condition(fixed32) / f10 皮肤模板 / f11 name。

    f10 皮肤：以装备位 weapons.variant_id（→ weapon_variants 实例 →
    weapon_variant_template_id，wv_tmpl 映射）为准 —— 此前恒发建武器时的
    反规范化列 variant_template_id，导致"武器+皮肤合买"以外的路径（单买
    皮肤后装备）战斗内皮肤不生效。variant_id=0（已卸下）时仅当该武器没有
    任何皮肤实例行（旧库从未持久化 variant_id）才回退反规范化列；
    有实例行而未装备 = 卸下 → 默认涂装（与 f3 弹药只发已装备同原则）。

    f5 tracer：weapon_id → 已装备曳光弹模板（tr_tmpl 映射，来源
    tracer_effects 行 is_equipped）；无装备行发 0（默认曳光弹）。
    """
    vid = int(w.get('variant_id', 0) or 0)
    vtmpl = int((wv_tmpl or {}).get(vid, 0) or 0) if vid else 0
    if not vtmpl and not vid \
            and not (wv_owned or {}).get(int(w['id'])):
        vtmpl = int(w.get('variant_template_id', 0) or 0)
    m = b''
    m += pb_int64(1, int(w['id']))
    m += pb_int64(2, int(w['template_id']))
    # f3 只下发已装备弹药（slot_index>0）——官方回放（pvt_player_settings.bin
    # Artyom/Kaspar/Arkady 等所有角色 weapon f3 ammos 均只含 1 条已装备弹药，
    # 无 equippedAmmoSlotIndex 字段，客户端按"列表中所有 ammo 均视为已装备"处理）。
    # 若把武器名下全部弹药（含未装备 slot=0 的）都下发，客户端会把列表里第一条
    # 当作已装备 → 用户改子弹后战局仍显示旧弹药（2026-08-27 修复，与 f4 载具弹药
    # equipped_storage_id>0 过滤同原则）
    for a in ammos:
        if int(a.get('slot_index') or 0) <= 0:
            continue
        am = pb_int64(1, int(a['id']))
        am += pb_int64(2, int(a['ammo_template_id']))
        am += pb_float(3, float(a['condition'] or 1.0))
        m += pb_message(3, am)
    for md in mods:
        mm = pb_int64(1, int(md['id']))
        mm += pb_int64(2, int(md['template_id']))
        mm += pb_float(3, float(md.get('condition', 1.0) or 1.0))
        m += pb_message(4, mm)
    # f5 tracer：已装备曳光弹模板（tracer_effects 行 is_equipped → tr_tmpl；
    # 无装备行发 0=默认曳光弹）；f6 decals 官方服恒发空消息（按回放字节镜像）
    m += pb_varint(5, int((tr_tmpl or {}).get(int(w['id']), 0) or 0))
    m += pb_message(6, b'')
    m += pb_int32(7, spawn_clips_for(
        int(w.get('clip_count', 0) or 0), int(w['template_id'])))
    m += pb_int32(8, int(slot))
    m += pb_float(9, float(w.get('condition', 1.0) or 1.0))
    if vtmpl:
        m += pb_int64(10, vtmpl)
    m += pb_string(11, w.get('name') or '')
    return pb_message(3, m)


def _msg_vehicle_setting(v, ups, vammos, vv_tmpl=None):
    """charactersetting.f4 VehicleSetting（§7.2）：
    f1 id / f2 载具变体模板 / f3 condition(fixed32) / f4 equippedInSlot /
    f5 升级 / f6 弹药{f2 仓id f3 槽位 f4 模板 f5 耐久 f6 出生弹夹}(fixed32)。

    f2 迷彩：VehicleSetting 描述符只有 f1-f6（player.dll 逆向，无独立皮肤
    字段），载具涂装经 f2 本身传递 —— 迷彩涂装与原厂变体同属
    VehicleVariantTemplate（共享 parentVehicleTemplateId，同载具不同漆），
    装备迷彩时 f2 发皮肤实例的涂装模板（vv_tmpl：实例 id → 模板 id，
    vehicles.variant_id 装备位），未装备/皮肤与基础模板不同 parent（脏数据，
    会换成另一辆载具）时回退基础模板。与武器 f10 皮肤、军服 f9 迷彩同模式。
    """
    base_tmpl = int(v['vehicle_variant_template_id'])
    tmpl = base_tmpl
    camo = int((vv_tmpl or {}).get(int(v.get('variant_id', 0) or 0), 0) or 0)
    if (camo and _VEHICLE_VARIANT_PARENT.get(camo) is not None
            and _VEHICLE_VARIANT_PARENT.get(camo)
            == _VEHICLE_VARIANT_PARENT.get(base_tmpl)):
        tmpl = camo
    m = b''
    m += pb_int64(1, int(v['id']))
    m += pb_int64(2, tmpl)
    m += pb_float(3, float(v.get('condition', 100.0) or 100.0))
    m += pb_int32(4, int(v['equipped_in_slot'] or 0))
    for u in ups:
        # f5 只下发已安装升级（equipped_slot_id>0）—— 购买的升级件初始
        # slot=0（车库升级列表待安装），未安装不应在战斗生效（2026-08-28
        # 修复，与 f3/f4/f6 只发已装备同原则）
        if int(u.get('equipped_slot_id', 0) or 0) <= 0:
            continue
        um = pb_int64(1, int(u['id']))
        # f2 是 VehicleUpgradeSlot 表 id（equipped_slot_id，车库 schema 的
        # equippedVehicleUpgradeSlotId）而非升级模板 id：客户端 spawn 侧
        # （player.dll vehicles.cpp "vehicle mod slot" 循环，db kind=0xa）拿
        # f2 查 eTableVehicleUpgradeSlot，再由行内 vehicleUpgradeTemplateId
        # 得到真正模板。误发 upgrade_template_id 会撞表内同 id 行（id 空间
        # 重叠：slot14=(vehicle61,tpl1)、slot3=(vehicle71,tpl1)），解析出
        # 错误模板 1 且挂载失败，被 #error3 insufeciant slots 丢弃（2026-08-31
        # hng_boot.log 实证：mod slot 14/3 → template 1 → 丢弃）
        um += pb_int64(2, int(u['equipped_slot_id']))
        um += pb_float(3, float(u['condition'] or 1.0))
        m += pb_message(5, um)
    # f6 只下发已装备弹药（equipped_storage_id>0）——官方回放（pvt_player_settings.bin
    # Yaroslav 载具）f6 仅含 storage=72 slot=1 一条；未装备弹药（storage=0，车库库存）
    # 下发会被客户端判无效移除并刷 #error2 Removing Invalid Vehicle Ammo
    for a in vammos:
        if int(a.get('equipped_storage_id') or 0) <= 0:
            continue
        am = pb_int64(1, int(a['id']))
        am += pb_int64(2, int(a['equipped_storage_id']))
        am += pb_int32(3, int(a['slot_index'] or 0))
        am += pb_int64(4, int(a['ammo_template_id']))
        am += pb_float(5, float(a['condition'] or 0))
        # f6 spawnClipCount：按弹药仓取 WarDatabase VehicleAmmoStorage.f2（官方回放
        # 锚点全部吻合），DB 旧默认 16 会导致备弹 16×clipsize×炮数（La-7=4800 发）
        am += pb_float(6, float(vehicle_ammo_spawn_clips(a.get('equipped_storage_id') or 0)))
        m += pb_message(6, am)
    return pb_message(4, m)


def _msg_ribbon_progress(ribbon_template_id, total_xp):
    """statrequests.Ribbon：currentValue=累计XP / currentLevel / nextValue=下级阈值。"""
    lv = _ribbon_level_from_xp(total_xp)
    next_xp = _RIBBON_LEVEL_XP.get(lv + 1, _RIBBON_LEVEL_XP.get(lv, 0))
    m = pb_int64(1, int(ribbon_template_id))
    m += pb_int32(2, int(total_xp))
    m += pb_int32(3, lv)
    m += pb_int32(4, int(next_xp))
    return m


# ===== 战斗服务器（hng.exe /D 专用服）BattleFieldSettings =====
# 启动链（hng-combat-protocol.md §B.7/B.8，player.dll 逆向确认）：
#   hng.exe /D <udp端口> /W <warserver> → ZWarDataComm 连 warserver（TCP Chunk 协议，
#   与 Flash 客户端同帧格式）→ 发 warcomm::RequestBattleFieldSettings(field1=missionid)
#   → 10s 内等 ResponseBattleFieldSettings（cmd_id 回显匹配，超时 "#error3"）。
#   专用服按 missionPresetId 查本地 WarDatabase/missionpreset → gamemapId →
#   WarDatabase/gamemap → 'T01M08_Mill.scn' → 加载 _packed Gamemaps/T01M08_Mill(.fst)
#   → bind UDP <端口> 等待玩家（客户端 PMS field20 game_connect 指向该端口）。
#
# 换图：改 MISSION_PRESET_ID + start_server.bat 的 /M（两者必须对应同一张图）。
#   preset 10 → T01M08_Mill | preset 29 → T02M01_Mountain | preset 26 → T02M04_Airfield
MISSION_PRESET_ID = 10        # 与 matchmaking.AI_MISSIONPRESET 同源（客户端简报一致）
BF_DEFENDER_FACTION_ID = 2    # 防守方 runtime factionId（必须在 battlefieldFactions 列表内）

# f14 armyResourceList 资源类别（resourceCategoryId == WarDatabase/armyresourcecategory.f1）：
#   1=infantryman 2=fighter_pilot 4=paratrooper 5=tank_crew 6=recon
#   3=heavy_fighter_plane 10=transport_plane 20=fighter_plane 21=recon_plane
# 空军类 spawnInAir=True。count=可用生命数。
# 【无池回退静态表】有资源池（force_pool.init_mission 抽 AT 生成）时类别集
# 与数值均由池驱动，本表仅作无池旧路径兜底（GMD f5 / 战斗服 f14 单一事实）。
# 伞兵 spawnInAir=True（回放 pvt_gmd_response.bin 实锤：cat=4 count=72 spawnInAir=1，
# 与 cat=10 transport_plane 成对 —— 伞兵从运输机跳伞出生）。
BF_RES_CATS = (
    (1, False, 1800),    # infantryman（主力生命池）
    (6, False, 60),      # recon
    (5, False, 60),      # tank_crew
    (4, True, 60),       # paratrooper（运输机跳伞出生）
    (2, True, 30),       # fighter_pilot
    (3, True, 10),       # heavy_fighter_plane
    (20, True, 10),      # fighter_plane
    (21, True, 10),      # recon_plane
    (10, True, 18),      # transport_plane（伞兵载机，4:1 比例配 60 伞兵）
)


def _accesspoint_layout(preset_id=MISSION_PRESET_ID,
                        defender_faction=BF_DEFENDER_FACTION_ID,
                        attacker_faction=3,
                        mission_id=None):
    """本局通道点布局：[(accesspointtemplate, owner factionId), ...]（最多 4 点 A-D）。

    ★归属按 missiontype 分支（preset f4；1=Assault / 5=Encounter，
    assault-map-spawn-investigation.md §9.2）：
    - Assault（Airfield 式四角围攻中心）：守方 = O 锚 1234567890（引擎
      无条件创建的虚拟 AP，f12 收编 + f14 资源挂点 → HasResources 置位
      → 守方小队/坦克部署中心 O 区）；进攻方随机激活 2~3 条边线
      （wardb.assault_ap_decision，按 mission_id 播种确定性，受图点数
      封顶）；★未激活线直接不进本列表（不开启——不出现在 f12/图上，
      官方 Airfield"有时仅 3 条激活"语义；不是中立 owner=0）。
    - Encounter（Mill 式两点对峙）：交替分配，首点=进攻方出生侧（他服抓包
      实测 Mill：模板 8=进攻、15=防守）。★必须与大厅简报
      GetMissionDetailsResponse f6 同规则——起始方向互换会导致"大厅概览图
      攻防图标位置与进战局后实际出生侧互换"。
    combat.py 的 ActiveMissionRequestEventListResponse f3 归属状态与此同源
    （ReportMissionStatusRequest f4 捕获上报可覆盖 combat._CAPTURE_OWNERS）。
    preset_id 由匹配池成局时下发（不同地图 → 不同模板组）。
    mission_id： Assault 决策种子——同一局的握手 f12 / 轮询 f2/f3/f5 /
    大厅简报 f6 必须传同一值（否则随机布局不同 → 增量同步错乱 + 图标错位）；
    None/0 时 seed=0 仍确定性（兼容旧默认路径）。
    """
    preset = _load_preset(preset_id)
    gamemap_id = int(preset.get(3, 0) or 0)      # preset f3 → gamemap 表行
    missiontype = int(preset.get(4, 0) or 0)     # preset f4 → missiontype
    ap_templates = _load_accesspoint_templates(gamemap_id)
    aps4 = ap_templates                          # 不截断（5 线图 A-E 全参与）
    layout = []
    if missiontype == 1:
        # Assault（§9.2 O 锚方案）：守方 = O 锚 1234567890（引擎无条件
        # 创建的虚拟 AP——f12 收编判负检查通过 + f14 资源挂此点置
        # HasResources → 守方小队/坦克部署中心 O 区）；攻方随机激活
        # 2~3 条边线，未激活线不开启。O 点归属走 BattleFieldSettings
        # f3 defender（H3 已实测证实，与出兵点归属无关）。
        d_ap, a_aps, _inactive = assault_ap_decision(gamemap_id, mission_id)
        for tmpl in aps4:
            if tmpl in a_aps:
                layout.append((tmpl, attacker_faction))
            # 未激活线：跳过（不开启）
        if d_ap:
            layout.append((d_ap, defender_faction))   # O 锚（守方）
    else:                                        # Encounter：交替（首点=进攻方）
        for i, tmpl in enumerate(aps4):
            owner = attacker_faction if (i % 2 == 0) else defender_faction
            layout.append((tmpl, owner))
    return layout


def _msg_army_resource_list(preset_id=MISSION_PRESET_ID,
                            defender_faction=BF_DEFENDER_FACTION_ID,
                            attacker_faction=3,
                            counts_by_faction=None,
                            mission_id=None):
    """ActionGameArmyResourceDefinition 条目列表：每 (faction, accesspointtemplate) × 池内类别。

    ResponseBattleFieldSettings f14 与 ActiveMissionRequestEventListResponse f2
    共用同一构建（单源，见 warserver-battlefield-settings.md §5.3/§6.1）。
    counts_by_faction：每局资源池 {factionTemplateId: {cat: count}}（
    force_pool.init_mission 抽 AT 生成），给定时类别集与数值均由池驱动
    （含坦克/载具等池内新增类别）—— 匹配界面 f5 / 战斗内 f2/f14 同源同值、
    消耗后递减；None 回退静态 BF_RES_CATS 初始表。
    mission_id：Assault 随机布局种子（未激活线不进 layout → 此处自然不挂
    资源；须与 _accesspoint_layout 同值）。

    资源逐点挂所属方（Assault：攻方挂激活边线、守方挂 O 锚 1234567890
    ——引擎 sub_1802BACC0 按 AP+128 匹配落资源、sub_1803ADF00 置 O 锚
    HasResources 位 → 守方小队/坦克部署 O 区，见 §9.2）。

    ★ 官方分线语义（2026-09-02）：攻方激活多条线时，每条线只挂
    force_pool.assign_lines 分到该线的 AT 资源之和（全线总量 = 1 份池，
    不随线数翻倍；各线兵力因 AT 组成不同而不同——官方"不同攻击线兵力
    不一样"即 AT 分线部署的自然结果）。守方全部 AT 守 O 锚（全量）。
    单线 / 未分线回退全量池挂载（行为与旧版一致）。"""
    res_msgs = []
    layout = _accesspoint_layout(preset_id, defender_faction,
                                 attacker_faction,
                                 mission_id=mission_id)
    # 攻方激活线序（布局顺序）→ 分线（幂等：f14 握手与 f2 轮询同局同分法）
    atk_aps = [tmpl for tmpl, owner in layout
               if int(owner) == int(attacker_faction)]
    if counts_by_faction and mission_id and len(atk_aps) > 1:
        force_pool.assign_lines(mission_id, len(atk_aps), atk_aps)
    atk_seen = 0
    for tmpl, owner in layout:
        is_atk = int(owner) == int(attacker_faction)
        pool_counts = counts_by_faction.get(int(owner)) \
            if counts_by_faction else None
        if pool_counts is not None and is_atk and len(atk_aps) > 1:
            line = force_pool.line_counts(mission_id, atk_seen) \
                if mission_id else None
            if line is not None:
                pool_counts = line          # 该线只挂分到的 AT 资源
        if is_atk:
            atk_seen += 1
        if pool_counts is not None:
            cat_list = [(cat, air, int(pool_counts.get(int(cat), 0)))
                        for cat, air in
                        force_pool.categories(
                            counts_by_faction={0: pool_counts})]
        else:
            cat_list = list(BF_RES_CATS)
        for cat, in_air, cnt in cat_list:
            r = b''
            r += pb_int64(1, owner)        # factionTemplateId（1US/2GE/3SU）
            r += pb_int64(2, tmpl)         # accesspointtemplate（资源挂载点）
            r += pb_int64(3, cat)          # resourceCategoryId
            r += pb_bool(4, in_air)        # spawnInAir
            r += pb_uint32(5, cnt)         # count
            res_msgs.append(r)
    return res_msgs


def _msg_battlefield_settings_faction(faction_id, template_id):
    """BattlefieldSettingsFaction 子消息（field 18 条目）。"""
    m = b''
    m += pb_int64(1, faction_id)       # factionId（本局 runtime id）
    m += pb_int64(2, template_id)      # factionTemplateId（1=US 2=GE 3=SU，玩家=3）
    m += pb_float(3, 1.0)              # underdogBonusXpMultiplier
    m += pb_float(4, 1.0)              # underdogBonusWarfundsMultiplier
    return m


# 战局时段（weatherTimeOfDay 枚举）→ timeofday 小时（BattleFieldSettings f9，
# 24 小时制浮点，战斗内光照/太阳角度）：0=夜 1=黎明 2=白天 3=黄昏
_TOD_HOURS = {0: 0.0, 1: 6.0, 2: 12.0, 3: 18.0}


def _msg_response_battlefield_settings(preset_id=MISSION_PRESET_ID,
                                        defender_faction=BF_DEFENDER_FACTION_ID,
                                        attacker_faction=3,
                                        enable_bots=True,
                                        enable_sa_bots=False,
                                        weather_time_of_day=1,
                                        weather_condition=0,
                                        timeofday=None,
                                        counts_by_faction=None,
                                        mission_id=None):
    """构建 ResponseBattleFieldSettings（31 字段，spbc 描述符 @0x1807B1120）。

    天气/时段：weather_time_of_day / weather_condition 取客户端枚举
    （WeatherTimeOfDay 0=夜 1=黎明 2=白天 3=黄昏；WeatherCondition
    0=少云 1=阴 2=雾），timeofday 未显式给定时按时段枚举推导小时。
    调用方（game_server）应传成局时抽取的 _MM_MISSION_DETAILS 天气，
    与简报 MissionInfo f12/f13 同源（避免"简报白天、进图黑夜"错位）。

    schema：decompiled/proto-files/HnG_States/ResponseBattleFieldSettings.proto。
    ★ 2026-08-20 补 f12 accesspoints + f14 armyResourceList（逆向 sub_1800B6600
      wardatacomm.cpp / sub_1803D30B0 team.cpp 实锤）：
      - f12 为空 → ZServer type25(accesspoint) 集合空 → skirmish/encounter 初始化
        "Not enough free accesspoints" + team.cpp 判负 "%s has lost their
        accesspoints"（USSR Total Squads: 1 即此路径）。
      - f14 为空 → 资源树空 → sub_1802A7A50 UpdateAvailableLives 位检查失败
        → "UpdateAvailableLives: 0,0,0" + "%s has run out of resources"。
      通道点模板按 gamemap 分组动态取自 WarDatabase/accesspointtemplate
      （f2 == gamemap id；Mill=13 → 模板 8/15 = A/B 点，坐标在图内）。
    参数由 game_server 按匹配池成局 preset 下发（多人局攻防 faction 亦可配）：
      preset_id        missionpreset 行 id（地图链入口）
      defender_faction 防守方 factionTemplateId（1US/2GE/3SU）
      attacker_faction 进攻方 factionTemplateId
      enable_bots      f20 enableBots——战斗服本地 bot 系统（CreateSoldierEntity
                      a6≠0 → SpawnBot，身份名=avatar 模板名）：官方训练局
                      （preset 9/10/11）即此路径；战争模式（gamemode 10）False
      enable_sa_bots  f31 enableSaBots——官方 SA bot 系统（sub_180334460 门控
                      *(mgr+405)，数量=战斗服本地 missionpreset f7
                      aiSaBotCountPerFaction；出生 CreateSoldierEntity a6=0 →
                      GetFactionDefaultCharName，killfeed 显示阵营默认名
                      Soldat/Red Army Man/Recruit）。官方多人局（preset 1/8/
                      19-29 等 f7>0）即此路径（与 f20 互斥，避免两套 bot 混编）
    """
    preset = _load_preset(preset_id)
    preset_id = int(preset.get(1, preset_id))
    name = preset.get(5, b'')
    if isinstance(name, bytes):
        name = name.decode('utf-8', 'replace')
    timelimit = float(preset.get(7, 0) or 0)     # preset f7（Training=0 不限时）
    max_players = int(preset.get(11, 24) or 24)  # preset f11
    weather_time_of_day = int(weather_time_of_day)
    weather_condition = int(weather_condition)
    if timeofday is None:
        timeofday = _TOD_HOURS.get(weather_time_of_day, 12.0)
    timeofday = float(timeofday)

    # f12 accesspoints（与大厅简报 f6 同规则——详见 _accesspoint_layout；
    # Assault：攻方随机激活边线 + ★守方 O 锚 1234567890（引擎 zgm 880-952
    # 按 AP+128==template 匹配收编 O 锚 → SetOwner(守Team)+active，§9.2），
    # 未开启线不在此）。
    # accesspointid 为运行时 id（真实局由 warserver 生成，私服固定自增段），
    # 低 32 位必须非零（sub_1803A7350 以低 32 位做非零校验后查 type25 树）。
    ap_msgs = []
    for i, (tmpl, owner) in enumerate(_accesspoint_layout(
            preset_id, defender_faction, attacker_faction,
            mission_id=mission_id)):
        ap = b''
        ap += pb_int64(1, owner)          # accesspointowner（factionId）
        ap += pb_int64(2, 880001 + i)     # accesspointid ★动态构建（非回放值）
        ap += pb_uint32(3, tmpl)          # accesspointtemplate（出生坐标来源）
        ap += pb_int64(4, 1)              # accesspointbattlefieldid
        ap_msgs.append(ap)

    # f14 armyResourceList（与 ActiveMissionRequestEventListResponse f2 同源；
    # counts_by_faction=每局资源池 → 与匹配界面 f5 同值，消耗后递减）
    res_msgs = _msg_army_resource_list(preset_id, defender_faction,
                                       attacker_faction,
                                       counts_by_faction=counts_by_faction,
                                       mission_id=mission_id)

    m = b''
    m += pb_int64(1, 1)                            # 1 battlefieldid（battlefield 表行 1）
    m += pb_int64(2, preset_id)                    # 2 missionPresetId ★地图链入口
    m += pb_int64(3, defender_faction)             # 3 defender（factionId）
    m += pb_float(4, 0.0)                          # 4 friendlyfire
    m += pb_string(5, name)                        # 5 gamemode（preset 名）
    m += pb_float(6, timelimit)                    # 6 timelimit
    m += pb_uint32(7, 0)                           # 7 fraglimit
    m += pb_uint32(8, 0)                           # 8 deathlimit
    m += pb_float(9, timeofday)                    # 9 timeofday（按本局时段枚举推导/下发）
    for ap in ap_msgs:                             # 12 accesspoints ★动态构建
        m += pb_message(12, ap)
    for r in res_msgs:                             # 14 armyResourceList ★动态构建
        m += pb_message(14, r)
    m += pb_int32(15, 0)                           # 15 battlefieldRotation
    m += pb_int32(16, 0)                           # 16 daysSinceWarStarted
    m += pb_bool(17, False)                        # 17 isCampaignMission
    m += pb_message(18, _msg_battlefield_settings_faction(1, 1))  # 18 US
    m += pb_message(18, _msg_battlefield_settings_faction(2, 2))  # 18 GE
    m += pb_message(18, _msg_battlefield_settings_faction(3, 3))  # 18 SU（factions 表必须含 factionId=3，否则 AddTeam team=3 越界崩溃）
    m += pb_int32(19, max_players)                 # 19 maxTotalPlayers
    m += pb_bool(20, enable_bots)                  # 20 enableBots（本地 bot：官方训练局路径）
    m += pb_int32(21, weather_time_of_day)         # 21 weatherTimeOfDay
    m += pb_int32(22, weather_condition)           # 22 weatherCondition
    m += pb_string(23, name)                       # 23 metricsMissionName
    m += pb_int64(24, 0)                           # 24 supplyLineId
    m += pb_float(25, 0.0)                         # 25 supplyLineOffsetFromAP1
    # f29/f30 stagedPos：战斗内 SpawnMenu 的 IngameEuropeMapOverlay 缩放定位
    # （player.dll SpawnMenu/IngameEuropeMapOverlayBlend + WarMap/deployTarget）。
    # 取本局 gamemap 对应 battlefield 行坐标（与简报 GMD f8/f9、大厅地图同源
    # 同值）；遭遇战图无行时 battlefield_entity_for_gamemap 回退 Benoit Village
    # 锚点。恒为 0.0 会让 overlay 缩放到欧洲地图左上角（与大厅位置不一致）。
    gamemap_id = int(preset.get(3, 0) or 0) or 13
    pos = battlefield_pos(battlefield_entity_for_gamemap(gamemap_id)) or (0.0, 0.0)
    m += pb_float(29, pos[0])                      # 29 stagedPosX
    m += pb_float(30, pos[1])                      # 30 stagedPosY
    m += pb_bool(31, enable_sa_bots)                # 31 enableSaBots（SA bot：官方多人局路径，killfeed=阵营默认名）
    return m
