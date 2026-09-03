# -*- coding: utf-8 -*-
"""生涯/通行证域（career）：客户端生涯军令（CareerOrder）与战斗通行证（BattlePass）。

数据流（U0 反编译核对 swf-frontend/swf-rank + WarDatabase 表转储）：
- 模板表（CareerOrderTemplate 46 行 id==type、battle_pass_template 480 行、
  battle_pass_season_template 4 赛季）由客户端从本地 WarDatabase 文件加载
  （HandleDownloadDataCommand._staticTableNames），服务器不推模板；
  进度行（CareerOrder / battle_pass）经 KVCS 按表名路由
  （DataModelProxy.updateTablesFromChangeset → Database.registerTable），
  任意已订阅视图 cmd 均可承载。
- CareerOrderRequest {type=1, playerId=2}：大厅动作上报（ECareerOrderType 46 种：
  装备武器/进匹配/发聊天/加入频道/玩快速战等），客户端 hasComplete() 为
  false 才发；响应处理函数为空，进度全靠 KVCS CareerOrder 行刷新。
- CareerOrder 行 progress 为逗号串（下标=模板 id），rewardReceivedBits 为
  repeated bool（下标=type；id==type）。两个数组都必须有 47 个元素（0..46），
  否则客户端 careerOrderListMediator 越界走 else 分支无限重复触发领取。
- ClaimCareerOrderRewardRequest {careerOrderTemplateId=1, playerId=2}：领取军令
  奖励（模板 rewards XML：<items target='player'><credits amount='2000'/></items>），
  response=1 后客户端开奖励弹窗；credits/warfunds/gold 入账并刷 player 行。
- BattlePass：4 赛季 activeTo 全部为 2022/2023 已过期。GetBattlePassSeasonRequest
  在 UI 逻辑中无发送方（仅 proto 类存在），但 ReceiveBattlePassRewardRequest 由
  CheckForBattlePassRewardsCommand（战报结算后）对 pending 非空且已购买的行发出。
  全部实现防止请求队列卡死；订阅 playerview 时推 battle_pass 进度行 +
  延期赛季模板行（2099）让通行证界面可用。

消息 schema（decompiled/proto-files/HnG_States）：
- CareerOrderResponse / ClaimCareerOrderRewardResponse /
  ActivateBattlePassSeasonResponse { required int32 response = 1 }
- GetBattlePassSeasonResponse { response=1; activeBattlePassTemplates=2;
  battlePassSeasonTemplates=3 }
- PurchaseBattlePassSeasonResponse / PurchaseBattlePassTierResponse /
  ReceiveBattlePassRewardResponse { response=1; rewardedBattlePassTemplates=2 }
- BattlePassTemplate { id=1 tier=2 level=3 localizationPath=4 image=5 reward=6
  battlePassSeasonTemplateId=7 requiredScore=8 }
- BattlePassSeasonTemplate { id=1 name=2 localizationPath=3 activeFrom=4 activeTo=5
  isActive=6 }
- battle_pass 行（KVCS）{ id=1 tier=2 level=3 playerId=4 battlePassSeasonTemplateId=5
  score=6 isPurchased=7 pendingRewardBattlePassTemplateIds=8
  receivedRewardBattlePassTemplateIds=9 isActive=10 unlockDateTime=11 }
"""

import xml.etree.ElementTree as ET

from protocol import (
    build_response,
    pb_bool,
    pb_int32,
    pb_int64,
    pb_message,
    pb_packed_bool,
    pb_parse,
    pb_string,
    )
from state import STORE, log

from messages import (
    _push_kvcs,
    _msg_tracer_effect,
    _msg_uniform_part,
    _msg_uniform_variant,
    _msg_vehicle_upgrade,
    _msg_weapon_variant,
    )
from player_service import _push_player_refresh
from handlers.economy import (
    _BOOSTER_MINUTES,
    push_booster_rows,
    push_membership_rows,
    push_ticket_rows,
    )
from wardb import (
    BOOSTER_MINUTES_BY_ID,
    MEMBERSHIP_DAYS_BY_ID,
    XP_BOOSTER_MINUTES_BY_ID,
    _parse_wardb_table,
    _rank_from_combat_xp,
    _ribbon_level_from_xp,
    _VEHICLE_UPGRADE_MAXCOND,
    charactertemplate_actionid,
    is_general_character,
    weapon_template_category,
    )


# ------------------------------------------------------------- 模板缓存

_BP_SEASONS = None        # [{id, name, loc, from, to, s_tier, s_lvl, s_score}]
_BP_TEMPLATES = None      # [{id, tier, level, loc, image, reward, season, req}]
_CAREER_TEMPLATES = None  # {template_id: (type, required_count, rewards_xml)}

# 赛季延期目标（Zulu）：本地 WarDatabase 里 4 个赛季 activeTo 全部是
# 2022/2023 已过期，仅当前赛季（_ACTIVE_SEASON）延期到 2099 让
# battlePassIsRunning() 为 true、通行证界面可用；其余赛季保留原始
# activeTo（客户端 battlePassCollection 对每个 activeTo>now 的赛季都加
# "当前赛季"标签，全部延期会导致 4 个赛季同时显示进行中）。
_SEASON_EXTENDED_TO = '20990101000000Z'

# 当前激活赛季（最新 id）：battle_pass 行与延期模板都指向它。
_ACTIVE_SEASON = 4


def _str(v):
    if isinstance(v, bytes):
        return v.decode('utf-8', 'replace')
    return str(v)


def _bp_seasons():
    """battle_pass_season_template 行缓存（仅当前赛季 activeTo 延期）。"""
    global _BP_SEASONS
    if _BP_SEASONS is None:
        rows = []
        for f in _parse_wardb_table('battle_pass_season_template'):
            sid = int(f.get(1, 0) or 0)
            rows.append({
                'id': sid,
                'name': _str(f.get(2, '')),
                'loc': _str(f.get(3, '')),
                'from': _str(f.get(4, '')),
                'to': (_SEASON_EXTENDED_TO if sid == _ACTIVE_SEASON
                       else _str(f.get(5, ''))),
                's_tier': int(f.get(6, 1) or 1),
                's_lvl': int(f.get(7, 10) or 10),
                's_score': int(f.get(8, 0) or 0),
            })
        _BP_SEASONS = rows
    return _BP_SEASONS


def _bp_templates():
    """battle_pass_template 行缓存（480 行）。"""
    global _BP_TEMPLATES
    if _BP_TEMPLATES is None:
        rows = []
        for f in _parse_wardb_table('battle_pass_template'):
            rows.append({
                'id': int(f.get(1, 0) or 0),
                'tier': int(f.get(2, 1) or 1),
                'level': int(f.get(3, 1) or 1),
                'loc': _str(f.get(4, '')),
                'image': _str(f.get(5, '')),
                'reward': _str(f.get(6, '')),
                'season': int(f.get(7, 1) or 1),
                'req': int(f.get(8, 0) or 0),
            })
        _BP_TEMPLATES = rows
    return _BP_TEMPLATES


def _career_templates():
    """CareerOrderTemplate 缓存：{template_id: (type, requiredCount, rewards)}。"""
    global _CAREER_TEMPLATES
    if _CAREER_TEMPLATES is None:
        tmap = {}
        for f in _parse_wardb_table('CareerOrderTemplate'):
            tmap[int(f.get(1, 0) or 0)] = (
                int(f.get(2, 0) or 0),
                int(f.get(4, 1) or 1),
                _str(f.get(7, '')),
            )
        _CAREER_TEMPLATES = tmap
    return _CAREER_TEMPLATES


# ------------------------------------------------------------- 奖励入账

_UNIFORM_PART_TYPE = None


def _uniform_part_type(template_id):
    """UniformPartTemplate f4 → EUniformPartType（部件类型，eHelmet=1）。
    缓存全表；模板缺失回退 1（通行证奖励全为头盔/面罩类）。"""
    global _UNIFORM_PART_TYPE
    if _UNIFORM_PART_TYPE is None:
        _UNIFORM_PART_TYPE = {
            int(f.get(1, 0)): int(f.get(4, 1) or 1)
            for f in _parse_wardb_table('UniformPartTemplate')
        }
    return _UNIFORM_PART_TYPE.get(int(template_id), 1)


async def _grant_reward_xml(sess, writer, pid, xml_str, tag):
    """解析奖励 XML（<items target='player|depot'>…</items>）并入账。

    官方语义（2026-09-03 对齐，奖励 XML + 客户端反编译实锤）：
    - target='player'：货币/加成/会员/门票直接入账。加成时长按模板表查：
      boostertemplate（通用 XP）与 ribbonboostertemplate（勋带）分表
      同 id 空间但时长不同（#4 官方 60min vs 勋带表 240min），各查各表。
    - target='depot'：一律插 characterId=0 的账户级仓库行 + KVCS 推送，
      不创建整机/整武器、不绑角色（客户端仓库列表只列 char-0 行：
      WeaponDepotViewMediator.getDepotSkins / UniformVariantUtilities
      .getUniformObjectsInDepot / VehicleDepotViewMediator.setupVehiclesInDepot
      / TracerEffectUtilities.getTracerEffectsInDepot）：
      · weaponvarianttemplate  → WeaponVariant depot 行（weaponId=0）
      · vehiclevarianttemplate → Vehicle 整车入仓 + 默认载具弹药
      · uniformvarianttemplate → UniformVariant depot 行
      · uniformparttemplate    → UniformPart depot 行（类型取模板 f4）
      · tracereffecttemplate   → TracerEffect depot 行（weaponId=0）
      · vehicleupgradetemplate → VehicleUpgrade depot 行（vehicleId=0）
      · progressionunlock      → 全角色 add_extra_unlock_uids（幂等登记）
    返回入账摘要列表。
    """
    granted = []
    try:
        root = ET.fromstring(xml_str)
    except Exception as e:
        log(f'      [career] reward xml parse error ({tag}): {e}')
        return granted
    if STORE is None:
        return granted

    delta = {}
    n_boosters = n_memberships = n_tickets = n_timed = 0
    for item in root:
        obj = item.tag.lower()
        tid = int(item.get('id', 0) or 0)
        amount = int(float(item.get('amount', '0') or 0))
        try:
            if obj in ('credits', 'warfunds', 'gold') and amount > 0:
                delta[obj] = delta.get(obj, 0) + amount
                granted.append(f'{obj}+{amount}')
            elif obj in ('ribbonboostertemplate', 'boostertemplate'):
                # 时长分表：通用加成查 BoosterTemplate，勋带加成查
                # RibbonBoosterTemplate（同 id 不同时长，混查会把 #4 的
                # 60min 发成 240min）
                minutes = (XP_BOOSTER_MINUTES_BY_ID if obj == 'boostertemplate'
                           else BOOSTER_MINUTES_BY_ID).get(tid, _BOOSTER_MINUTES)
                await STORE.add_booster(
                    pid, tid, duration_minutes=minutes,
                    activate_now=False,
                    is_ribbon=(obj == 'ribbonboostertemplate'))
                n_boosters += 1
                granted.append(f'{obj}#{tid}')
            elif obj == 'shopmembershipitems':
                await STORE.add_membership(
                    pid, tid,
                    duration_days=MEMBERSHIP_DAYS_BY_ID.get(tid, 30))
                n_memberships += 1
                granted.append(f'membership#{tid}')
            elif obj == 'tickettemplate':
                await STORE.add_ticket(pid, tid, is_timed=False)
                n_tickets += 1
                granted.append(f'ticket#{tid}')
            elif obj == 'tickettimedtemplate':
                await STORE.add_ticket(pid, tid, is_timed=True)
                n_timed += 1
                granted.append(f'tickettimed#{tid}')
            elif obj == 'weaponvarianttemplate':
                # 皮肤本体入仓（不创建武器/弹药）；装备时客户端发
                # SetWeaponVariantRequest 绑定到具体武器
                vv = await STORE.create_weapon_variant(pid, 0, 0, tid)
                if sess.soldier_view_cmd is not None:
                    await _push_kvcs(
                        writer, sess.soldier_view_cmd, b'WeaponVariant',
                        _msg_weapon_variant(vv['id'], 0, 0, tid))
                granted.append(f'weaponvariant#{tid}')
            elif obj == 'vehiclevarianttemplate':
                # 官方语义：载具奖励=整车入仓（Vehicle 行 char=0）
                from handlers.shop import _bundle_deliver_vehicle
                await _bundle_deliver_vehicle(
                    sess, writer, pid, 0, tid, equip_slot1=False)
                granted.append(f'vehicle#{tid}')
            elif obj == 'uniformvarianttemplate':
                uv = await STORE.create_uniform_variant(pid, tid, 0)
                if sess.soldier_view_cmd is not None:
                    await _push_kvcs(
                        writer, sess.soldier_view_cmd, b'UniformVariant',
                        _msg_uniform_variant(uv['id'], tid, 0))
                granted.append(f'uniformvariant#{tid}')
            elif obj == 'uniformparttemplate':
                ptype = _uniform_part_type(tid)
                part = await STORE.create_uniform_part(pid, 0, tid, ptype)
                if sess.soldier_view_cmd is not None:
                    await _push_kvcs(
                        writer, sess.soldier_view_cmd, b'UniformPart',
                        _msg_uniform_part(part['id'], 0, tid, ptype))
                granted.append(f'uniformpart#{tid}')
            elif obj == 'tracereffecttemplate':
                t = await STORE.create_tracer_effect(pid, tid)
                if sess.soldier_view_cmd is not None:
                    await _push_kvcs(
                        writer, sess.soldier_view_cmd, b'TracerEffect',
                        _msg_tracer_effect(t['id'], tid, 0, 0, False))
                granted.append(f'tracereffect#{tid}')
            elif obj == 'vehicleupgradetemplate':
                u = await STORE.create_vehicle_upgrade(
                    pid, tid, 0, 0,
                    _VEHICLE_UPGRADE_MAXCOND.get(int(tid), 100.0))
                if sess.soldier_view_cmd is not None:
                    await _push_kvcs(
                        writer, sess.soldier_view_cmd, b'VehicleUpgrade',
                        _msg_vehicle_upgrade(u['id'], tid, 0, 0, 0,
                                             u.get('condition', 100.0)))
                granted.append(f'vehicleupgrade#{tid}')
            elif obj == 'progressionunlock':
                # 角色解锁（如三阵营头部外观）：全角色幂等登记
                # （当前奖励 uid 已被 level0/rank 默认路径授予，此处
                # 防御未来 metaLevel>0 条目漏发）
                for ch in await STORE.list_characters(pid):
                    await STORE.add_extra_unlock_uids(pid, ch['id'], [tid])
                granted.append(f'progressionunlock#{tid}')
            else:
                log(f'      [career] reward item skipped ({tag}): '
                    f'<{item.tag} id={tid}>')
        except Exception as e:
            # 单项失败不影响其余奖励发货
            log(f'      [career] reward item <{item.tag} id={tid}> '
                f'grant failed ({tag}): {e}')
    if delta:
        # 原子增量入账（读-改-写绝对值会覆盖并发变动，如结算加钱）
        await STORE.add_currency(pid, **delta)
    # 库存表 KVCS 推送（有该类物品发放时）
    try:
        if n_boosters:
            await push_booster_rows(sess, writer, int(pid))
        if n_tickets:
            await push_ticket_rows(sess, writer, int(pid))
        if n_timed:
            await push_ticket_rows(sess, writer, int(pid), timed=True)
        if n_memberships:
            await push_membership_rows(sess, writer, int(pid))
    except Exception as e:
        log(f'      [career] reward row push failed ({tag}): {e}')
    return granted


# ------------------------------------------------------------- 状态推导补全

# PlayerLevelUnlock（level=10 解锁项的最大 requiredScore）：玩家等级≥10 判定阈值
_PLAYER_LEVEL10_SCORE = 20000


async def revalidate_state_orders(pid):
    """状态型生涯军令服务端推导补全（推 KVCS 前调用）。

    客户端 46 种 ECareerOrderType 里只有 5 种有 UI 上报点（4 战争地图 /
    8 快速战斗 / 9 读邮件 / 10 聊天频道 / 11 聊天消息），战斗侧另有部分
    由 hng.exe 的 CareerOrderActionGameRequest 上报（实测 22 切兵 /
    25 载具部署）；其余为状态型任务，官方由服务端从玩家数据推导。
    这里按 DB 状态补全可判定的任务，已完成（progress>=1）的不重复累加。
    """
    if STORE is None:
        return
    try:
        chars = await STORE.list_characters(pid)
        weapons = await STORE.list_weapons(pid)
        done = {oid for oid, (p, _st)
                in (await STORE.list_career_orders(pid)).items() if p >= 1}
    except Exception as e:
        log(f'      [career] revalidate load failed: {e}')
        return

    actionids = [charactertemplate_actionid(c['template']) for c in chars]
    is_general = [is_general_character(c['template']) for c in chars]
    completed = set()

    if any(w.get('character_id') for w in weapons):
        completed.add(1)     # 装备武器
    if weapons:
        completed.add(2)     # 购买武器（宽松：拥有非空武器库）
    if sum(1 for a in actionids if a == 2) >= 2:
        completed.add(5)     # 拥有 2 个步兵
    if {c['faction'] for c in chars} >= {1, 2, 3}:
        completed.add(15)    # 三阵营各一个士兵
    if 10 in actionids:
        completed.add(33)    # 创建坦克兵
    if any(a == 7 and not g for a, g in zip(actionids, is_general)):
        completed.add(37)    # 创建伞兵（actionid=7 同将军，排除之）
    if 11 in actionids:
        completed.add(38)    # 创建飞行员
    if 6 in actionids:
        completed.add(39)    # 创建侦察兵
    if any(is_general):
        completed.add(41)    # 拥有将军

    # ---- 装备状态类（weapons.container_id / vehicles.equipped_in_slot /
    #      weapon_modifiers.weapon_id / vehicle_upgrades.equipped_slot_id /
    #      vehicle_ammo.equipped_storage_id，非 0 即已装备）----
    equipped_cats = {weapon_template_category(w['template_id'])
                     for w in weapons if w.get('container_id')}
    if 16 in equipped_cats:               # WeaponCategory.Submachinegun
        completed.add(18)    # 装备冲锋枪
    if 8 in equipped_cats:                # WeaponCategory.AntiTankWeapon
        completed.add(28)    # 装备反坦克武器
    if any(c.get('uniform_variant_id') or c.get('headgear_id') for c in chars):
        completed.add(35)    # 更改士兵外观（非默认迷彩/头盔）
    try:
        vehicles = await STORE.list_vehicles(pid)
    except Exception:
        vehicles = []
    if any(v.get('equipped_in_slot') for v in vehicles):
        completed.add(24)    # 装备载具
    if any(v.get('variant_id') for v in vehicles):
        completed.add(36)    # 更改载具外观（非默认涂装）
    try:
        if any(m.get('weapon_id') for m
               in await STORE.list_weapon_modifiers(pid)):
            completed.add(30)    # 装备武器配件（挂接到武器）
    except Exception:
        pass
    try:
        if any(u.get('equipped_slot_id') for u
               in await STORE.list_vehicle_upgrades(pid)):
            completed.add(31)    # 装备载具升级
    except Exception:
        pass
    try:
        if any(a.get('equipped_storage_id') or a.get('slot_index') for a
               in await STORE.list_vehicle_ammo(pid)):
            completed.add(32)    # 装备载具自定义弹药
    except Exception:
        pass

    total_score = 0
    for c in chars:
        try:
            xps = await STORE.get_ribbon_xp(pid, c['id'])
        except Exception:
            xps = {}
        if any(_ribbon_level_from_xp(v) >= 3 for v in xps.values()):
            completed.add(16)    # 任意绶带到 3 级
        if _rank_from_combat_xp(c['combat_xp']) >= 12:
            completed.add(23)    # 任意士兵军衔 12
        try:
            st = await STORE.get_stats(c['id']) or {}
            total_score += int(st.get('PlayerScore', st.get('score', 0)) or 0)
        except Exception:
            pass
    if total_score >= _PLAYER_LEVEL10_SCORE:
        completed.add(17)    # 玩家等级 10

    newly = sorted(completed - done)
    for oid in newly:
        try:
            await STORE.upsert_career_order_progress(pid, oid)
            log(f'      [career] state order {oid} completed by revalidation')
        except Exception as e:
            log(f'      [career] revalidate order {oid} failed: {e}')
    if newly:
        log(f'      [career] revalidated {len(newly)} state order(s): {newly}')


_MISSIONTYPE_CACHE = {}


def _missiontype_for_preset(preset_id):
    """missionpreset 表 id → missiontype（f4；1=Assault、3=Skirmish、
    5-8=Encounter 系列即 Capture&Hold）。未知返回 0。"""
    key = int(preset_id or 0)
    if key in _MISSIONTYPE_CACHE:
        return _MISSIONTYPE_CACHE[key]
    mt = 0
    for r in _parse_wardb_table('missionpreset'):
        if int(r.get(1, 0) or 0) == key:
            f4 = r.get(4, 0)
            if not isinstance(f4, (bytes, bytearray)):
                mt = int(f4 or 0)
            break
    _MISSIONTYPE_CACHE[key] = mt
    return mt


async def complete_battle_orders(pid, character_ids=(), missiontype=0):
    """战斗结算挂钩：按本局模式与参战角色补全战斗型生涯军令。

    - 3 eJoinBattle：任意一场战斗结算
    - 29 ePlayCaptureAndHold：missiontype 5-8（Encounter 系列）
    - 34 ePlayAssault：missiontype 1
    - 21 eJoinAsTanker：参战角色含坦克兵（actionid=10）
    - 40 ePlayWarBattle：战争模式（本服战局均 matchmaking 快速战，暂不判）
    """
    if STORE is None or not pid:
        return
    try:
        done = {oid for oid, (p, _st)
                in (await STORE.list_career_orders(pid)).items() if p >= 1}
    except Exception:
        return
    orders = {3}
    if missiontype == 1:
        orders.add(34)
    elif missiontype in (5, 6, 7, 8):
        orders.add(29)
    for cid in character_ids:
        try:
            ch = await STORE.get_character_any(pid, cid)
        except Exception:
            ch = None
        if ch and charactertemplate_actionid(ch['template']) == 10:
            orders.add(21)     # 以坦克兵参战
            break
    newly = sorted(orders - done)
    for oid in newly:
        try:
            await STORE.upsert_career_order_progress(pid, oid)
            log(f'      [career] battle order {oid} completed (player={pid})')
        except Exception as e:
            log(f'      [career] battle order {oid} failed: {e}')


async def complete_order_once(pid, oid):
    """动作型生涯军令单点补全（对应 handler 成功路径调用，幂等：
    progress>=1 的不重复累加）。"""
    if STORE is None or not pid:
        return
    try:
        done = {o for o, (p, _st)
                in (await STORE.list_career_orders(pid)).items() if p >= 1}
        if oid in done:
            return
        await STORE.upsert_career_order_progress(pid, oid)
        log(f'      [career] order {oid} completed by action hook '
            f'(player={pid})')
    except Exception as e:
        log(f'      [career] order {oid} action hook failed: {e}')


async def complete_order_and_push(sess, writer, pid, oid=None):
    """动作型军令完成 + CareerOrder 行实时推送（大厅会话钩子统一入口）。

    客户端军令 UI 只认 KVCS CareerOrder 行刷新 —— complete_order_once 只写
    DB 不推的话，玩家要重登才能看到进度（"做了任务没反应"的根因）。
    oid=None 时仅做状态推导补全 + 推行（装备类动作后调用，18/24/28/30/31/
    32/35/36 等由 revalidate_state_orders 从 DB 状态推导）；
    playerview 未订阅时仍会完成入库，下次登录/请求再收敛。"""
    if not pid:
        return
    if oid is not None:
        await complete_order_once(int(pid), oid)
    await push_career_order_row(sess, writer, int(pid))


async def push_career_order_row(sess, writer, pid):
    """推玩家 CareerOrder 进度行（单行 id=playerId）。

    progress 逗号串与 rewardReceivedBits 均为 47 槽（下标 0 占位），
    career_orders 表 status：0 进行中 1 已完成 2 已领取。"""
    # 推行前先跑状态推导补全（客户端无上报点的任务按 DB 状态完成）
    await revalidate_state_orders(pid)
    if STORE is None or sess.playerview_cmd is None:
        return
    try:
        orders = await STORE.list_career_orders(pid)
    except Exception as e:
        log(f'      [DB] list_career_orders failed: {e}')
        return
    prog = [0] * 47
    bits = [False] * 47
    for oid, (p, st) in orders.items():
        if 0 < oid < 47:
            prog[oid] = int(p)
            bits[oid] = (int(st) == 2)
    m = pb_int64(1, pid)                    # id
    m += pb_int64(2, pid)                   # playerId
    m += pb_string(3, ','.join(str(x) for x in prog))
    m += pb_packed_bool(4, bits)            # rewardReceivedBits
    await _push_kvcs(writer, sess.playerview_cmd, b'CareerOrder', m)
    log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet '
        f'(CareerOrder id={pid}, {sum(1 for x in prog if x)} orders with progress)')


def _msg_battle_pass_row(row_id, pid, season, score, purchased, active,
                         pending, received, tier=1, level=1, unlock=''):
    """battle_pass 进度行 proto。"""
    m = pb_int64(1, row_id)
    m += pb_int32(2, tier)
    m += pb_int32(3, level)
    m += pb_int64(4, pid)
    m += pb_int64(5, season)
    m += pb_int32(6, score)
    m += pb_bool(7, purchased)
    m += pb_string(8, ','.join(str(x) for x in pending))
    m += pb_string(9, ','.join(str(x) for x in received))
    m += pb_bool(10, active)
    # unlockDateTime 非空 = 已解锁可随时激活（客户端 canBeActivated 判定：
    # 赛季已结束时 isPurchased && unlockDateTime.length 才显示"激活"按钮）
    m += pb_string(11, unlock)
    return m


async def push_battle_pass_row(sess, writer, pid, season_id=_ACTIVE_SEASON):
    """推玩家 battle_pass 进度行（默认当前激活赛季，可指定其他赛季）。

    tier/level 按 score 对 battle_pass_template.requiredScore 推导；
    pending = 已达分数且未领取的模板 id（客户端据此发 ReceiveBattlePassRewardRequest）。"""
    if STORE is None or sess.playerview_cmd is None:
        return
    bp = None
    try:
        bp = await STORE.get_battle_pass(pid, season_id)
    except Exception as e:
        log(f'      [DB] get_battle_pass failed: {e}')
        return
    if bp is None:
        return                              # 无进度行不推（客户端走赛季默认值）
    score = bp['score']
    tier, level = _tier_level_for_score(score, season_id)
    received = [int(x) for x in bp.get('claimed', [])]
    pending = [t['id'] for t in _bp_templates()
               if t['season'] == season_id and t['req'] <= score
               and t['id'] not in received]
    row_id = pid * 100 + season_id
    # 已购行兜底非空解锁时间（迁移前旧行 unlock_at 为空时也显示激活按钮）
    unlock = bp.get('unlock') or ('20990101000000Z' if bp['purchased'] else '')
    m = _msg_battle_pass_row(row_id, pid, season_id, score,
                             bp['purchased'], bp['active'], pending, received,
                             tier, level, unlock)
    await _push_kvcs(writer, sess.playerview_cmd, b'battle_pass', m)
    log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet '
        f'(battle_pass id={row_id}, score={score}, tier={tier}, lvl={level}, '
        f'pending={len(pending)})')


async def push_all_battle_pass_rows(sess, writer, pid):
    """推玩家全部有进度的 battle_pass 行（每个赛季一行）。

    登录/激活切换用：客户端 BattlePassTable 只有已推过的行，查看
    未推的赛季会回落到模板默认值（startingScore=0）——"登录后进度
    归零"的根因。无进度行的赛季 push_battle_pass_row 自行跳过。"""
    for s in _bp_seasons():
        await push_battle_pass_row(sess, writer, pid, s['id'])


async def push_season_templates(sess, writer):
    """推赛季模板行（battle_pass_season_template 表 UPDATE）。

    本地文件 4 赛季全部过期；仅当前赛季的 activeTo 延期为 2099，KVCS
    覆盖后 BattlePassModel.battlePassIsRunning() 为 true，通行证界面
    显示进行中；其余赛季保留原始 activeTo（历史赛季不显示进行中）。"""
    if sess.playerview_cmd is None:
        return
    for s in _bp_seasons():
        m = pb_int64(1, s['id'])
        m += pb_string(2, s['name'])
        m += pb_string(3, s['loc'])
        m += pb_string(4, s['from'])
        m += pb_string(5, s['to'])
        # battle_pass_season_template 运行时 schema（swf-rank 实类）：
        # 6=startingTier 7=startingLevel 8=startingScore（无 isActive，
        # KVCS set 为整行替换，写错字段会覆盖客户端本地起始值）
        m += pb_int32(6, s['s_tier'])
        m += pb_int32(7, s['s_lvl'])
        m += pb_int32(8, s['s_score'])
        await _push_kvcs(writer, sess.playerview_cmd,
                         b'battle_pass_season_template', m)
    log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet '
        f'(battle_pass_season_template x{len(_bp_seasons())}, extended to '
        f'{_SEASON_EXTENDED_TO})')


def _tier_level_for_score(score, season_id=_ACTIVE_SEASON):
    """分数 → (tier, level)：取该赛季 requiredScore<=score 的最大 (tier, level)。"""
    best = (1, 1)
    for t in _bp_templates():
        if t['season'] == season_id and t['req'] <= score:
            if (t['tier'], t['level']) > best:
                best = (t['tier'], t['level'])
    return best


# ------------------------------------------------------------- 生涯军令

async def handle_career_order(sess, writer, cmd_id, req_type, proto):
    """CareerOrderRequest {type=1, playerId=2} → CareerOrderResponse{response=1}。

    大厅动作上报（客户端 hasComplete() 为 false 才发），进度累加后推
    CareerOrder 行；requiredCount 全表为 1，progress>=1 即完成。"""
    order_type = 0
    try:
        f = pb_parse(proto)
        order_type = int(f.get(1, 0) or 0)
    except Exception as e:
        log(f'      [career] CareerOrderRequest parse error: {e}')

    pid = await sess.ensure_player()
    if pid is not None and STORE is not None and order_type:
        try:
            progress, status = await STORE.upsert_career_order_progress(
                int(pid), order_type)
            log(f'      [DB] career order {order_type}: progress={progress} '
                f'status={status} (player={pid})')
            await push_career_order_row(sess, writer, int(pid))
        except Exception as e:
            log(f'      [DB] upsert_career_order_progress failed: {e}')

    resp = build_response(cmd_id, 'CareerOrderResponse', pb_int32(1, 1))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} CareerOrderResponse (response=1, type={order_type})')


async def handle_claim_career_order_reward(sess, writer, cmd_id, req_type, proto):
    """ClaimCareerOrderRewardRequest {careerOrderTemplateId=1, playerId=2}
    → ClaimCareerOrderRewardResponse{response=1}。

    career_orders.status 1→2（幂等失败也回 ok，防队列卡死），
    模板 rewards XML 入账 + player 行刷新 + CareerOrder 行刷新。"""
    template_id = 0
    try:
        f = pb_parse(proto)
        template_id = int(f.get(1, 0) or 0)
    except Exception as e:
        log(f'      [career] ClaimCareerOrderReward parse error: {e}')

    pid = await sess.ensure_player()
    if pid is not None and STORE is not None and template_id:
        ok = False
        try:
            ok, _ = await STORE.claim_career_order(int(pid), template_id)
        except Exception as e:
            log(f'      [DB] claim_career_order failed: {e}')
        if ok:
            tpl = _career_templates().get(template_id)
            if tpl:
                granted = await _grant_reward_xml(sess, writer, int(pid),
                                                  tpl[2],
                                                  f'career order {template_id}')
                if granted:
                    log(f'      [MONEY] career order {template_id} reward: '
                        f'{granted}')
                    await _push_player_refresh(sess, writer)
            await push_career_order_row(sess, writer, int(pid))

    resp = build_response(cmd_id, 'ClaimCareerOrderRewardResponse', pb_int32(1, 1))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} ClaimCareerOrderRewardResponse '
        f'(response=1, template={template_id})')


# ------------------------------------------------------------- 通行证

def _msg_bp_template(t):
    """BattlePassTemplate（响应模板条目）。"""
    m = pb_int64(1, t['id'])
    m += pb_int32(2, t['tier'])
    m += pb_int32(3, t['level'])
    m += pb_string(4, t['loc'])
    m += pb_string(5, t['image'])
    m += pb_string(6, t['reward'])
    m += pb_int64(7, t['season'])
    m += pb_int32(8, t['req'])
    return m


async def handle_get_battle_pass_season(sess, writer, cmd_id, req_type, proto):
    """GetBattlePassSeasonRequest {} → GetBattlePassSeasonResponse。

    response=1 + 赛季模板（activeTo 延期）。UI 无发送方（仅 proto 类存在），
    兜底实现防队列卡死；模板数据客户端本地已有，响应里只带赛季。"""
    resp_proto = pb_int32(1, 1)
    for s in _bp_seasons():
        m = pb_int64(1, s['id'])
        m += pb_string(2, s['name'])
        m += pb_string(3, s['loc'])
        m += pb_string(4, s['from'])
        m += pb_string(5, s['to'])
        m += pb_bool(6, s['id'] == _ACTIVE_SEASON)
        resp_proto += pb_message(3, m)

    pid = await sess.ensure_player()
    if pid is not None:
        await push_season_templates(sess, writer)
        await push_all_battle_pass_rows(sess, writer, int(pid))

    resp = build_response(cmd_id, 'GetBattlePassSeasonResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} GetBattlePassSeasonResponse '
        f'(response=1, {len(_bp_seasons())} seasons)')


async def handle_activate_battle_pass_season(sess, writer, cmd_id, req_type, proto):
    """ActivateBattlePassSeasonRequest {battlePassSeasonTemplateId=1, playerId=2}
    → ActivateBattlePassSeasonResponse{response=1} + battle_pass 行刷新。"""
    season_id = 0
    try:
        f = pb_parse(proto)
        season_id = int(f.get(1, 0) or 0)
    except Exception as e:
        log(f'      [career] ActivateBattlePassSeason parse error: {e}')

    pid = await sess.ensure_player()
    if pid is not None and STORE is not None and season_id:
        try:
            ok = await STORE.activate_battle_pass(int(pid), season_id)
            log(f'      [DB] activate battle pass season {season_id}: {ok}')
        except Exception as e:
            log(f'      [DB] activate_battle_pass failed: {e}')
        # 互斥激活后旧激活行 isActive 变 false：把所有有进度的赛季行
        # 都推一遍（无进度行的赛季 push_battle_pass_row 自行跳过），
        # 客户端才能看到切换后的激活态
        await push_all_battle_pass_rows(sess, writer, int(pid))

    resp = build_response(cmd_id, 'ActivateBattlePassSeasonResponse', pb_int32(1, 1))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} ActivateBattlePassSeasonResponse '
        f'(response=1, season={season_id})')


async def handle_purchase_battle_pass_season(sess, writer, cmd_id, req_type, proto):
    """PurchaseBattlePassSeasonRequest {seasonTemplateId=1 playerId=2 pricingId=3
    discountId=4 paymentCurrency=5 unlockPricingId=6 unlockDiscountId=7
    unlockPaymentCurrency=8} → PurchaseBattlePassSeasonResponse{response=1}。

    私服定价表无通行证条目（_charge_player 找不到 pricing 返回 0,True 免费），
    purchased=true + battle_pass 行刷新。"""
    season_id = 0
    try:
        f = pb_parse(proto)
        season_id = int(f.get(1, 0) or 0)
    except Exception as e:
        log(f'      [career] PurchaseBattlePassSeason parse error: {e}')

    pid = await sess.ensure_player()
    if pid is not None and STORE is not None and season_id:
        try:
            ok, gold = await STORE.purchase_battle_pass(int(pid), season_id, 0)
            log(f'      [DB] purchase battle pass season {season_id}: '
                f'{ok}, gold={gold}')
        except Exception as e:
            log(f'      [DB] purchase_battle_pass failed: {e}')
        await push_battle_pass_row(sess, writer, int(pid), season_id)

    resp = build_response(cmd_id, 'PurchaseBattlePassSeasonResponse', pb_int32(1, 1))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} PurchaseBattlePassSeasonResponse '
        f'(response=1, season={season_id})')


async def handle_purchase_battle_pass_tier(sess, writer, cmd_id, req_type, proto):
    """PurchaseBattlePassTierRequest {battlePassTemplateId=1 playerId=2 pricingId=3
    discountId=4 paymentCurrency=5} → PurchaseBattlePassTierResponse
    {response=1, rewardedBattlePassTemplates=2}。

    购买层级完成：score 提升到该模板 requiredScore（只升不降），并立即
    发放所有已达成的奖励（客户端响应处理从 field 2 读奖励列表弹窗，
    不会另发 ReceiveBattlePassRewardRequest）。"""
    template_id = 0
    try:
        f = pb_parse(proto)
        template_id = int(f.get(1, 0) or 0)
    except Exception as e:
        log(f'      [career] PurchaseBattlePassTier parse error: {e}')

    resp_proto = pb_int32(1, 1)
    granted_templates = []
    pid = await sess.ensure_player()
    if pid is not None and STORE is not None and template_id:
        tpl = next((t for t in _bp_templates() if t['id'] == template_id), None)
        if tpl is not None:
            try:
                # 购买层级完成 = score 提升到该模板 requiredScore（只升不降）
                score = await STORE.set_battle_pass_score(
                    int(pid), tpl['season'], max(0, tpl['req']))
                log(f'      [DB] purchase tier template {template_id}: '
                    f'score -> {score}')
            except Exception as e:
                log(f'      [DB] set_battle_pass_score failed: {e}')
            # 立即发放买层级跳过等级的奖励并在响应带回（弹奖励窗）
            granted_templates, granted_ids = await _claim_reached_rewards(
                sess, writer, int(pid), tpl['season'])
            for m in granted_templates:
                resp_proto += pb_message(2, m)
            if granted_ids:
                log(f'      [MONEY] battle pass tier purchase rewards: '
                    f'{granted_ids}')
                await _push_player_refresh(sess, writer)
            await push_battle_pass_row(sess, writer, int(pid), tpl['season'])

    resp = build_response(cmd_id, 'PurchaseBattlePassTierResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} PurchaseBattlePassTierResponse '
        f'(response=1, template={template_id}, '
        f'rewarded={len(granted_templates)})')


async def _claim_reached_rewards(sess, writer, pid, season_id):
    """领取该赛季所有已达成分数且未领取的通行证奖励。

    返回 (granted_templates, granted_ids)：granted_templates 为
    BattlePassTemplate proto 列表（响应 field 2 用），granted_ids 为
    模板 id 列表（日志用）。领奖与入账幂等（claimed_tiers 查重）。"""
    granted_templates = []
    granted_ids = []
    try:
        bp = await STORE.get_battle_pass(pid, season_id)
    except Exception as e:
        log(f'      [DB] get_battle_pass failed: {e}')
        return granted_templates, granted_ids
    if bp is None:
        return granted_templates, granted_ids
    score = bp['score']
    received = [int(x) for x in bp.get('claimed', [])]
    for t in _bp_templates():
        if (t['season'] == season_id and t['req'] <= score
                and t['id'] not in received):
            first, _ = await STORE.claim_battle_pass_reward(
                pid, season_id, t['id'])
            if first:
                granted_ids.append(t['id'])
                await _grant_reward_xml(sess, writer, pid, t['reward'],
                                        f'battle pass tier {t["id"]}')
                granted_templates.append(_msg_bp_template(t))
    return granted_templates, granted_ids


async def handle_receive_battle_pass_reward(sess, writer, cmd_id, req_type, proto):
    """ReceiveBattlePassRewardRequest {battlePassSeasonTemplateId=1}
    → ReceiveBattlePassRewardResponse{response=1, rewardedBattlePassTemplates=2}。

    CheckForBattlePassRewardsCommand（战报结算后）对 pending 非空且已购买的行
    发出；服务器把已达成未领取的模板全部领取（claimed_tiers）+ 奖励入账，
    响应带 rewardedBattlePassTemplates 供客户端开奖励弹窗。"""
    season_id = 0
    try:
        f = pb_parse(proto)
        season_id = int(f.get(1, 0) or 0)
    except Exception as e:
        log(f'      [career] ReceiveBattlePassReward parse error: {e}')
    if not season_id:
        season_id = _ACTIVE_SEASON

    resp_proto = pb_int32(1, 1)
    pid = await sess.ensure_player()
    if pid is not None and STORE is not None:
        granted_templates, granted_ids = await _claim_reached_rewards(
            sess, writer, int(pid), season_id)
        for m in granted_templates:
            resp_proto += pb_message(2, m)
        if granted_ids:
            log(f'      [MONEY] battle pass rewards granted: {granted_ids}')
            await _push_player_refresh(sess, writer)
        await push_battle_pass_row(sess, writer, int(pid), season_id)

    resp = build_response(cmd_id, 'ReceiveBattlePassRewardResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} ReceiveBattlePassRewardResponse '
        f'(response=1, season={season_id})')


HANDLERS = {
    'CareerOrderRequest': handle_career_order,
    'ClaimCareerOrderRewardRequest': handle_claim_career_order_reward,
    'GetBattlePassSeasonRequest': handle_get_battle_pass_season,
    'ActivateBattlePassSeasonRequest': handle_activate_battle_pass_season,
    'PurchaseBattlePassSeasonRequest': handle_purchase_battle_pass_season,
    'PurchaseBattlePassTierRequest': handle_purchase_battle_pass_tier,
    'ReceiveBattlePassRewardRequest': handle_receive_battle_pass_reward,
}
