# -*- coding: utf-8 -*-
"""玩家数据服务层：DB/会话相关的业务函数（扣款、解锁位图、角色数据回放等）。

从 realtest_server 机械迁移；依赖 state(STORE/log)、wardb(静态索引)、
messages(消息构建/KVCS 推送)。被 handlers 各域模块与入口复用。
"""
import time

from protocol import msg_player

from state import (
    STORE,
    log,
    )

from wardb import (
    _AMMO_MAPPINGS_BY_WEAPON,
    _AMMO_MAPPING_IDS,
    _AMMO_MAXCOND,
    _CHAR_DEFAULT_UNLOCKS,
    _CUR_KEY,
    _CUR_NAME,
    _E_VEHICLE,
    _E_VEHICLES_VARIANT,
    _E_WEAPON,
    _E_WEAPON_AMMO_MAPPING,
    _E_WEAPON_MOD_MAPPING,
    _ITEM_UNLOCK_UIDS,
    _LEVEL0_UIDS,
    _MOD_MAPPING_IDS,
    _PRICING_MAP,
    _RANK_META_LEVEL,
    _RIBBON_BOOSTERS,
    _RIBBON_META_LEVEL,
    _UNLOCK_BY_ITEM,
    _UNLOCK_UID_META,
    _VEHICLE_VARIANT_PARENT,
    _default_ammo_for,
    _default_vehicle_ammo_for,
    _prog_max,
    _rank_from_combat_xp,
    _ribbon_level_from_xp,
    )
from starting_gear import STARTING_GEAR

from messages import (
    _msg_character,
    _msg_depot_new_item,
    _msg_uniform_part,
    _msg_uniform_variant,
    _msg_vehicle,
    _msg_vehicle_ammo,
    _msg_vehicle_upgrade,
    _msg_vehicle_variant,
    _msg_weapon,
    _msg_weapon_ammo,
    _msg_weapon_modifier,
    _msg_weapon_variant,
    _push_kvcs,
    )


# 官方 ResponseCodes（HnG_States/ResponseCodes.as）：no_name=53 / invalid_name=36
RC_NO_NAME = 53
RC_INVALID_NAME = 36

# 官方命名规则（客户端契约）：min_name_length 校验=3；
# 角色/武器名上限 NAME_MAX_LENGTH=20，小队名上限 SQUAD_NAME_LENGTH=15
NAME_MIN_LENGTH = 3
CHARACTER_NAME_MAX_LENGTH = 20
SQUAD_NAME_MAX_LENGTH = 15


def _official_name_error(name, max_length=CHARACTER_NAME_MAX_LENGTH):
    """按官方规则校验名字，返回 ResponseCodes 错误码（0=合法）。

    官方行为：trim 两侧空白后判定 —— 空 → no_name(53)；
    长度 <3 或 >上限、含控制字符 → invalid_name(36)。"""
    trimmed = (name or '').strip()
    if not trimmed:
        return RC_NO_NAME
    if len(trimmed) < NAME_MIN_LENGTH or len(trimmed) > max_length:
        return RC_INVALID_NAME
    if any(ord(c) < 0x20 for c in trimmed):
        return RC_INVALID_NAME
    return 0


async def _charge_player(sess, pricing_id, currency, conn=None, fraction=1.0):
    """按 pricing_id + 支付货币扣款，返回 (cost, ok)。定价缺失或货币类型无效时不扣款（返回 0, True）。

    原子扣款（事务内 FOR UPDATE + 增量扣减），杜绝并发丢更新/超额消费；
    传入 conn（STORE.transaction() 上下文）时并入外部事务，与发货同生共死。
    fraction 为价格比例（如仓库转运费 0.5），取整口径与客户端
    PriceObject.calculateFractionPrice 的 Math.max(1, int(base*fraction)) 一致。
    """
    if pricing_id is None or pricing_id <= 0:
        return 0, True
    price = _PRICING_MAP.get(int(pricing_id))
    if price is None:
        return 0, True
    key = _CUR_KEY.get(int(currency))
    if key is None:
        return 0, True
    cost = int(price[key] or 0)
    if cost <= 0:
        return 0, True
    if fraction != 1.0:
        cost = max(1, int(cost * fraction))
    pid = await sess.ensure_player()
    if pid is None:
        return cost, False
    ok, new_balance = await STORE.spend_currency(pid, key, cost, conn=conn)
    if not ok:
        return cost, False
    log(f'      [MONEY] -{cost} {_CUR_NAME.get(int(currency), currency)} for pricing {pricing_id} '
        f'({price["type"]}) -> {key}={new_balance}')
    return cost, True


class _PaymentFailed(Exception):
    """余额不足：在 STORE.transaction() 内抛出以触发整体回滚（含本事务已扣部分）。"""


async def _charge_player_multi(sess, pricing_ids, currency):
    """多笔定价合并为一次原子扣款（同一货币），返回 (total_cost, ok)。

    余额不足时整体不扣，杜绝"第一笔已扣、第二笔不足"的钱货两空；
    定价缺失/货币无效的条目按 0 计（与 _charge_player 口径一致）。"""
    key = _CUR_KEY.get(int(currency)) if currency is not None else None
    if key is None:
        return 0, True
    total = 0
    for prid in pricing_ids:
        if prid is None or int(prid) <= 0:
            continue
        price = _PRICING_MAP.get(int(prid))
        if price is not None:
            total += int(price[key] or 0)
    if total <= 0:
        return 0, True
    pid = await sess.ensure_player()
    if pid is None:
        return total, False
    ok, new_balance = await STORE.spend_currency(pid, key, total)
    if not ok:
        return total, False
    log(f'      [MONEY] -{total} {_CUR_NAME.get(int(currency), currency)} for pricings '
        f'{[int(p) for p in pricing_ids if p]} -> {key}={new_balance}')
    return total, True



async def _push_player_refresh(sess, writer):
    """购买/扣款后推送 player 更新货币显示。"""
    if sess.playerview_cmd is not None:
        player_proto = await _msg_player_for(sess)
        await _push_kvcs(writer, sess.playerview_cmd, b'player', player_proto)



async def _char_unlock_bits_for(sess, template, faction, char_id=0):
    """生成角色 unlockBits：
    - 官方默认解锁序列（CharacterUnlockTemplate.unlockSequence）
    - 已购武器/配件/弹药/载具的解锁需求（已购物品不锁定）
    - 已购武器的默认弹药映射解锁
    - 勋带经验（character_ribbon_xp）推导等级后的解锁需求（unlockMetaLevel <= 等级）
    配件/非默认弹药不因购买武器解锁，只按武器勋带等级（metaLevel 2~12）解锁，
    其余武器/载具按默认序列/勋带等级显示锁定（按勋带/军衔进度解锁）。"""
    bits = [False] * (_prog_max + 1)
    ids = set(_CHAR_DEFAULT_UNLOCKS.get((int(template), int(faction)), ()))
    # 默认序列只保留非物品类 uid（eCharacterType/eDiscount/eAbility/eCommandPoints/
    # eSquadCustomizationPoints 等状态类）。unlockSequence 实际是该角色类型「全部
    # 勋带路径条目」的清单，武器/载具/变体/配件/弹药/载具升级/战斗徽章/军服等
    # 一切可购买物品类 uid 一律剔除（wardb._ITEM_UNLOCK_UIDS，历史四轮同根因
    # 泄漏：PPD-40/Bazooka、L-41 弹药、Field Trigger 配件、ammo crate 载具
    # 升级、步兵勋带2级战斗徽章）。物品解锁只走三条正路：
    # STARTING_GEAR 起始装备 / 勋带 XP 等级 / 已购物品实例。
    ids -= _ITEM_UNLOCK_UIDS
    # starting_gear 指定的 (faction, template) → 允许解锁的武器/载具/瞄具
    # STARTING_GEAR key 顺序 = (faction, template)（与 handlers/characters.py
    # 中 STARTING_GEAR.get((faction, template), {}) 保持一致）
    _keep_gear_uids = set()
    gear = STARTING_GEAR.get((int(faction), int(template)))
    if gear:
        _wt = gear.get('weapon')
        if _wt:
            _keep_gear_uids |= _UNLOCK_BY_ITEM.get((int(_wt), _E_WEAPON), set())
        _vv = gear.get('vehicle_variant')
        if _vv:
            # variant 205/204/206（侦察机）这类在 progressionUnlock 里是
            # E_VEHICLE(itemType=4) 不是 E_VEHICLES_VARIANT(15)；
            # 坦克/飞机的常规 variant 则是 itemType=15 + 父级 E_VEHICLE。
            # 三层都查，确保 variant 自身任何一个 unlock uid 都能被解锁。
            _keep_gear_uids |= _UNLOCK_BY_ITEM.get((int(_vv), _E_VEHICLES_VARIANT), set())
            _keep_gear_uids |= _UNLOCK_BY_ITEM.get((int(_vv), _E_VEHICLE), set())
            _vt_parent = _VEHICLE_VARIANT_PARENT.get(int(_vv))
            if _vt_parent:
                _keep_gear_uids |= _UNLOCK_BY_ITEM.get((int(_vt_parent), _E_VEHICLE), set())
        _modt = gear.get('sight_mod')
        if _modt and _wt:
            # 瞄具是 WeaponModifierTemplate (sight_mod)。客户端解锁瞄具购买需要
            # WeaponModMapping 的 unlockUid（_E_WEAPON_MOD_MAPPING），由
            # MOD_MAPPING_IDS[(modifierTemplateId, weaponTemplateId)] → mapping id
            # → _UNLOCK_BY_ITEM[(mid, E_WEAPON_MOD_MAPPING)] → unlock uids。
            mid = _MOD_MAPPING_IDS.get((int(_modt), int(_wt)))
            if mid:
                _keep_gear_uids |= _UNLOCK_BY_ITEM.get((mid, _E_WEAPON_MOD_MAPPING), set())
    ids |= _keep_gear_uids
    pid = await sess.ensure_player()
    if pid is not None:
        try:
            # 已获得勋带等级 → 该勋带下 unlockMetaLevel <= 等级的全部解锁需求 uid。
            # 以 character_ribbon_xp 为唯一事实源（与 AddRibbonLevelRequest 写入逻辑一致），
            # 避免 extra_unlock_uids 与等级不同步导致提前/错误解锁。
            ribbon_xps = await STORE.get_ribbon_xp(pid, char_id)
            for ribbon_id, total_xp in ribbon_xps.items():
                lv = _ribbon_level_from_xp(total_xp)
                for (uid, meta_lv) in _RIBBON_META_LEVEL.get(str(ribbon_id), set()):
                    if meta_lv <= lv:
                        ids.add(uid)
            # 勋带 0 级恒满足：无 XP 行的勋带等级视为 0（官方语义：勋带人人从
            # 0 级开始，_ribbon_level_from_xp(0)==0），unlockMetaLevel==0 的解锁
            # uid 对满足阵营/职业过滤的角色一律授予。客户端 hasCharacterUnlockedItem
            # 纯按 unlockBits 判定（无 level 0 特判），缺这些位时兵种默认迷彩
            # （uid 1156/1203-1216）会在军服界面显示成"需解锁"（2026-09-01 反馈）。
            # level=0 条目不含 itemType=1（职业模板），不影响"职业招募走
            # PlayerLevelUnlock"的设计；历史泄漏 uid（299/339/529/516/87/94/122/50）
            # 均 level>=1，不由此路径授予。
            for uid in _LEVEL0_UIDS:
                _fac, _ct = _UNLOCK_UID_META.get(uid, (0, 0))
                if (_fac and _fac != int(faction)) or \
                   (_ct and _ct != int(template)):
                    continue
                ids.add(uid)
            # eRank（军衔）解锁：unlockType=2、metaName 恒 '0' 哨兵——
            # First Blood badge（rank 1）、职业模板（rank 5/6/8/9）、
            # 小队自定义点数（rank 12+）等 26 条。旧实现漏掉此路径，
            # 这些条目永锁。faction/charType 过滤同勋带路径语义。
            if char_id:
                _xp = 0
                for ch in await STORE.list_characters(pid):
                    if int(ch['id']) == int(char_id):
                        _xp = int(ch.get('combat_xp', 0) or 0)
                        break
                _rank = _rank_from_combat_xp(_xp)
                for (uid, meta_lv, _itype) in _RANK_META_LEVEL:
                    if meta_lv > _rank:
                        continue
                    _fac, _ct = _UNLOCK_UID_META.get(uid, (0, 0))
                    if (_fac and _fac != int(faction)) or \
                       (_ct and _ct != int(template)):
                        continue
                    ids.add(uid)
            # ★已购物品按角色查询（官方解锁按士兵独立）：store 层 character_id
            # 过滤（SQL 级），账号级不过滤会把其他角色（含已删号）的武器解锁
            # uid 泄漏进当前角色位图 —— 例如已删旧号的 t24 PPD-40 / t52 Bazooka
            # 会让所有步兵角色商店显示已解锁。
            _cid = int(char_id) if char_id else None
            weapons = await STORE.list_weapons(pid, _cid)
            for w in weapons:
                ids |= _UNLOCK_BY_ITEM.get((int(w['template_id']), _E_WEAPON), set())
                if w.get('variant_template_id'):
                    ids |= _UNLOCK_BY_ITEM.get((int(w['variant_template_id']), 14), set())
            w_templates = {w['id']: w['template_id'] for w in weapons}
            # 配件：不因购买武器而解锁，只按武器勋带等级解锁（XP 推导）。
            # 已购配件实例（weapon_modifiers）单独加回，避免已购物品显示锁定。
            # 弹药：只解锁默认弹药映射（defaultAmmo=true），非默认弹药按默认序列/
            # 勋带购买判断（客户端 setupAmmoModifications 中 isUnlocked=false 时锁定）
            for wt in set(w_templates.values()):
                for (mid, _at, default) in _AMMO_MAPPINGS_BY_WEAPON.get(int(wt), ()):
                    if default:
                        ids |= _UNLOCK_BY_ITEM.get((mid, _E_WEAPON_AMMO_MAPPING), set())
            for m in await STORE.list_weapon_modifiers(pid, _cid):
                wt = w_templates.get(m['weapon_id'])
                if wt is None:
                    continue
                mapping_id = _MOD_MAPPING_IDS.get((int(m['template_id']), int(wt)))
                if mapping_id:
                    ids |= _UNLOCK_BY_ITEM.get((mapping_id, _E_WEAPON_MOD_MAPPING), set())
            for a in await STORE.list_weapon_ammo(pid, _cid):
                wt = w_templates.get(a['weapon_id'])
                if wt is None:
                    continue
                mapping_id = _AMMO_MAPPING_IDS.get((int(a['ammo_template_id']), int(wt)))
                if mapping_id:
                    ids |= _UNLOCK_BY_ITEM.get((mapping_id, _E_WEAPON_AMMO_MAPPING), set())
            # 已购载具变体：加回其解锁需求 uid（eVehicle=4 与 eVehiclesVariant=15，
            # 客户端 VehicleUtilities 用 15 判断载具是否解锁），避免已购载具显示锁定
            for v in await STORE.list_vehicles(pid, _cid):
                vvid = int(v.get('vehicle_variant_template_id') or 0)
                if vvid:
                    ids |= _UNLOCK_BY_ITEM.get((vvid, _E_VEHICLE), set())
                    ids |= _UNLOCK_BY_ITEM.get((vvid, _E_VEHICLES_VARIANT), set())
        except Exception as e:
            log(f'[DB] unlock bits load failed: {e}')
    for uid in ids:
        if 0 <= uid <= _prog_max:
            bits[uid] = True
    return bits



async def _weapon_template_id(sess, weapon_id):
    """按 weapon_id 查武器模板 id（先会话缓存，后数据库）。"""
    w = getattr(sess, 'weapons', {}).get(weapon_id)
    if w:
        return int(w['template_id'])
    pid = await sess.ensure_player()
    if pid is None:
        return 0
    try:
        for wrow in await STORE.list_weapons(pid):
            if wrow['id'] == weapon_id:
                return int(wrow['template_id'])
    except Exception as e:
        log(f'[DB] weapon template lookup failed: {e}')
    return 0



async def _char_ribbon_level(sess, char_id, ribbon_id):
    """返回角色勋带等级（character_ribbon_xp 的 total_xp 经阈值推导），未获得返回 0。"""
    pid = await sess.ensure_player()
    if pid is None:
        return 0
    try:
        xps = await STORE.get_ribbon_xp(pid, char_id)
        return _ribbon_level_from_xp(xps.get(int(ribbon_id), 0))
    except Exception as e:
        log(f'[DB] ribbon level lookup failed: {e}')
        return 0



async def _char_bits_for(sess, char_id):
    """计算角色完整 unlockBits 的 uid 集合（默认序列 + 勋带等级 + 已购物品）。

    载具购买校验用：载具可经默认序列免费授予（如 GAZ-67），不能只看勋带等级。"""
    pid = await sess.ensure_player()
    if pid is None:
        return set()
    try:
        for ch in await STORE.list_characters(pid):
            if int(ch['id']) == int(char_id):
                bits = await _char_unlock_bits_for(sess, ch['template'], ch['faction'], char_id)
                return {i for i, b in enumerate(bits) if b}
    except Exception as e:
        log(f'[DB] char bits lookup failed: {e}')
    return set()



async def _unlock_need_for_char(sess, char_id, req_uids):
    """按角色阵营/职业过滤解锁需求 uid，返回该角色实际需要满足的 uid 子集。

    对齐客户端 hasCharacterUnlockedItem：getUnlocksRequiredForItem 按 faction 过滤，
    匹配时 requiredUnlock.characterType == 角色模板 或 0。同一物品多个阵营各有
    解锁 uid（如 RGD-33 的 261/262/301），角色只需满足本阵营的条目。"""
    if not req_uids:
        return set()
    pid = await sess.ensure_player()
    faction, template = 0, 0
    if pid is not None:
        try:
            for ch in await STORE.list_characters(pid):
                if int(ch['id']) == int(char_id):
                    faction, template = int(ch['faction']), int(ch['template'])
                    break
        except Exception as e:
            log(f'[DB] char meta lookup failed: {e}')
    need = set()
    for uid in req_uids:
        meta = _UNLOCK_UID_META.get(int(uid))
        if meta is None:
            continue
        ft, ct = meta
        if ft and ft != faction:
            continue
        if ct and ct != template:
            continue
        need.add(int(uid))
    return need



# ---- war_catalogue 构建（用于 query_war_catalogue_response）----
async def _msg_player_for(sess, gamertag=None):
    """从数据库读取货币/教程完成态并构建 player proto；DB 不可用时回退默认值。"""
    cur = {'credits': 100000, 'warfunds': 100000, 'gold': 100000}
    tutorials_completed = 0
    faction = None
    total_score = 0
    first_action_date = ''
    pid = await sess.ensure_player()
    if pid is not None:
        try:
            cur = await STORE.get_currency(pid)
        except Exception as e:
            log(f'[DB] get_currency failed: {e}')
        try:
            tutorials_completed = await STORE.get_tutorials_completed(pid)
        except Exception as e:
            log(f'[DB] get_tutorials_completed failed: {e}')
        try:
            faction = await STORE.get_player_faction(pid)
        except Exception as e:
            log(f'[DB] get_player_faction failed: {e}')
        # ★ firstActionGameDate（player f19，YYYYMMDD）：签到窗口连续登录
        # 天数计算的起点（客户端以 firstActionGameDate 与今天比对）。
        # 恒发空串会让该计算退化；无首局回落注册日。
        try:
            metrics = await STORE.get_daily_metrics(pid)
            if metrics:
                first_action_date = metrics['fagd'] or ''
        except Exception as e:
            log(f'[DB] get_daily_metrics failed: {e}')
        # ★ totalScore（player f36）：统计界面"分数"显示值 = 全角色 score 汇总
        #   （type-6 上报的 PlayerScore 优先，legacy 结算键 score 回落，二者
        #   同源不叠加）。此前恒发 0 → 统计页分数为 0（2026-09-01）。
        try:
            for ch in await STORE.list_characters(pid):
                st = await STORE.get_stats(ch['id']) or {}
                v = st.get('PlayerScore', st.get('score', 0))
                total_score += int(v or 0)
        except Exception as e:
            log(f'[DB] totalScore sum failed: {e}')
    return msg_player(gamertag or sess.username or 'DemoPlayer',
                      tutorials_completed=tutorials_completed,
                      pid=pid or 1, faction=faction, total_score=total_score,
                      first_action_date=first_action_date,
                      **cur)



async def _push_stored_soldier_data(sess, writer):
    """subscribesoldierview 时回放数据库中的角色/武器/皮肤变体。"""
    pid = await sess.ensure_player()
    if pid is None or sess.soldier_view_cmd is None:
        return
    try:
        chars = await STORE.list_characters(pid)
        weapons = await STORE.list_weapons(pid)
        modifiers = await STORE.list_weapon_modifiers(pid)
        ammo_list = await STORE.list_weapon_ammo(pid)
        vehicles = await STORE.list_vehicles(pid)
        v_upgrades = await STORE.list_vehicle_upgrades(pid)
        v_ammo_list = await STORE.list_vehicle_ammo(pid)
        uniform_variants = await STORE.list_uniform_variants(pid)
        weapon_variants = await STORE.list_weapon_variants(pid)
        uniform_parts = await STORE.list_uniform_parts(pid)
        vehicle_variants = await STORE.list_vehicle_variants(pid)
        depot_new = await STORE.list_depot_new_items(pid)
    except Exception as e:
        log(f'[DB] load soldier data failed: {e}')
        return
    # 仓库"新物品"通知行（DepotNewItemTable）：客户端按 newItem 字符串点亮
    # 仓库 tab 红点；不推则 wasNewItem 恒 false，红点永不出现
    if depot_new:
        new_item_str = '|'.join(
            f'{tab}_{",".join(str(i) for i in ids)}'
            for tab, ids in sorted(depot_new.items()))
        await _push_kvcs(writer, sess.soldier_view_cmd, b'DepotNewItem',
                         _msg_depot_new_item(int(pid), int(pid), new_item_str))
        log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet '
            f'(DepotNewItem: {len(depot_new)} tabs, '
            f'{sum(len(v) for v in depot_new.values())} unviewed)')
    # 清理历史 bug 产生的假武器（template_id=0，客户端 WeaponTemplateTable 查不到模板
    # → updateWeaponObject 空指针 Error #1009 崩溃）。删除后不参与回放，关联弹药/配件/皮肤一并过滤。
    fake_weapons = [w for w in weapons if not w['template_id']]
    if fake_weapons:
        fake_ids = {int(fw['id']) for fw in fake_weapons}
        weapons = [w for w in weapons if w['template_id']]
        ammo_list = [a for a in ammo_list if int(a['weapon_id']) not in fake_ids]
        modifiers = [m for m in modifiers if int(m['weapon_id']) not in fake_ids]
        weapon_variants = [v for v in weapon_variants if int(v['weapon_id']) not in fake_ids]
        for fw in fake_weapons:
            log(f'[DB] removing fake weapon id={fw["id"]} (template_id=0, name={fw["name"]!r})')
            try:
                await STORE.delete_weapon(pid, fw['id'])
            except Exception as e:
                log(f'[DB] delete fake weapon failed: {e}')
    if not chars and not weapons and not modifiers and not ammo_list and not vehicles and not v_upgrades:
        return
    cmd = sess.soldier_view_cmd
    # UniformVariant/UniformPart 必须先于 character 推送：客户端 changeset 串行队列
    # （queueChangeset→handleNextChangeset→publishPendingTableUpdates）处理 character 行时，
    # getUniformVariantIdEquipped() 会按 uniformVariantId 查本地 UniformVariantTable——
    # 实例未入表则 getFromId 返回 null → Error #1009 未捕获异常中断发布循环，
    # 后续所有 KVCS（含装备/归仓操作）不被 publish，UI 全部失灵
    for uv in uniform_variants:
        await _push_kvcs(writer, cmd, b'UniformVariant',
                         _msg_uniform_variant(uv['id'], uv['uniform_variant_template_id'],
                                              uv['character_id']))
    for up in uniform_parts:
        await _push_kvcs(writer, cmd, b'UniformPart',
                         _msg_uniform_part(up['id'], up['character_id'],
                                           up['uniform_part_template_id'],
                                           up['uniform_part_type']))
    for ch in chars:
        bits = await _char_unlock_bits_for(sess, ch['template'], ch['faction'], ch['id'])
        # ★尾部字段必须传全：漏传 head_id（f22）时换头保存被抹回默认头
        #  （Appearance "无法保存"根因——DB 已存 89，回放却发 0）
        await _push_kvcs(writer, cmd, b'character',
                         _msg_character(ch['name'], ch['faction'], ch['template'],
                                        _rank_from_combat_xp(ch.get('combat_xp', 0)),
                                        ch['id'], bits,
                                        ch.get('combat_badge_0', 0),
                                        ch.get('combat_badge_1', 0),
                                        int(time.time() * 1000),
                                        ch.get('uniform_variant_id', 0),
                                        ch.get('headgear_id', 0),
                                        1 if ch.get('is_deleted') else 0,
                                        ch.get('previous_template', 0),
                                        ch.get('head_id', 0),
                                        0,
                                        1 if ch.get('is_on_leave') else 0))
        log(f'S->C cmd={cmd:<4} KeyValueChangeSet (character {ch["name"]} id={ch["id"]} '
            f'unlockBits={sum(bits)} true)')
    for vv in weapon_variants:
        await _push_kvcs(writer, cmd, b'WeaponVariant',
                         _msg_weapon_variant(vv['id'], vv['character_id'],
                                             vv['weapon_id'], vv['weapon_variant_template_id']))
    for w in weapons:
        await _push_kvcs(writer, cmd, b'weapon',
                         _msg_weapon(w['id'], w['name'], w['template_id'], w['character_id'],
                                     w['container_id'], w['container_slot'], w['variant_id'],
                                     w.get('clip_count', 0),
                                     w.get('auto_repair', 0),
                                     w.get('condition', 100.0)))
    for mod in modifiers:
        await _push_kvcs(writer, cmd, b'WeaponModifier',
                         _msg_weapon_modifier(mod['id'], mod['template_id'],
                                              mod['character_id'], mod['weapon_id'],
                                              mod.get('auto_repair', 0),
                                              mod.get('condition', 1.0)))
    # 为没有弹药的武器补创建默认弹药（老账号重启前买的武器）并装备到槽位 1；
    # 已持有但未装备（slot=0）的默认弹药在武器无其他已装备弹药时也补装备。
    # 若武器已装有其他弹药（slot>0），默认弹药只持有不装备，避免槽位冲突。
    existing = {(a['weapon_id'], a['ammo_template_id']) for a in ammo_list}
    equipped_weapons = {a['weapon_id'] for a in ammo_list if a['slot_index'] > 0}
    for w in weapons:
        for i, (mapping_id, ammo_template_id) in enumerate(_default_ammo_for(w['template_id'])):
            slot_ammo = (1 if i == 0 else 0) if w['id'] not in equipped_weapons else 0
            if (w['id'], ammo_template_id) in existing:
                # 已有该默认弹药实例：若未装备且武器无已装备弹药，则补装备到 slot 1
                if slot_ammo > 0:
                    try:
                        for a in ammo_list:
                            if (a['weapon_id'] == w['id']
                                    and a['ammo_template_id'] == ammo_template_id
                                    and a['slot_index'] == 0):
                                await STORE.update_weapon_ammo_slot(pid, a['id'], slot_ammo)
                                await _push_kvcs(writer, cmd, b'WeaponAmmo',
                                                 _msg_weapon_ammo(a['id'], ammo_template_id,
                                                                  w['character_id'], w['id'],
                                                                  slot_ammo,
                                                                  a.get('condition', 100.0)))
                                log(f'S->C cmd={cmd:<4} KeyValueChangeSet (WeaponAmmo id={a["id"]} '
                                    f'equipped default ammo for weapon {w["id"]}, '
                                    f'template={ammo_template_id})')
                                break
                    except Exception as e:
                        log(f'[DB] equip existing default ammo failed: {e}')
                continue
            try:
                a = await STORE.create_weapon_ammo(pid, ammo_template_id,
                                                   w['character_id'], w['id'],
                                                   slot_ammo,
                                                   _AMMO_MAXCOND.get(int(ammo_template_id), 100.0))
                await _push_kvcs(writer, cmd, b'WeaponAmmo',
                                 _msg_weapon_ammo(a['id'], ammo_template_id,
                                                  w['character_id'], w['id'], slot_ammo,
                                                  a.get('condition', 100.0)))
                log(f'S->C cmd={cmd:<4} KeyValueChangeSet (WeaponAmmo id={a["id"]} '
                    f'auto-created for weapon {w["id"]}, template={ammo_template_id}, '
                    f'equippedSlot={slot_ammo})')
            except Exception as e:
                log(f'[DB] auto-create default ammo failed: {e}')
    for a in ammo_list:
        await _push_kvcs(writer, cmd, b'WeaponAmmo',
                         _msg_weapon_ammo(a['id'], a['ammo_template_id'],
                                          a['character_id'], a['weapon_id'], a['slot_index'],
                                          a.get('condition', 100.0),
                                          a.get('auto_repair', 0)))
    # 先推 VehicleVariant（皮肤实例），再推 Vehicle，与购买流程推送顺序一致。
    # 皮肤实例优先来自 vehicle_variants 表；兼容旧库 vehicles.variant_id 遗留数据。
    pushed_vv_ids = set()
    for vv in vehicle_variants:
        await _push_kvcs(writer, cmd, b'VehicleVariant',
                         _msg_vehicle_variant(vv['id'], vv['character_id'],
                                              vv['vehicle_id'], vv['vehicle_variant_template_id']))
        pushed_vv_ids.add(int(vv['id']))
    for v in vehicles:
        if v.get('variant_id') and int(v['variant_id']) not in pushed_vv_ids:
            await _push_kvcs(writer, cmd, b'VehicleVariant',
                             _msg_vehicle_variant(v['variant_id'], v['character_id'],
                                                  v['id'], 0))
        await _push_kvcs(writer, cmd, b'Vehicle',
                         _msg_vehicle(v['id'], v['vehicle_variant_template_id'],
                                      v['character_id'], v['equipped_in_slot'],
                                      v.get('variant_id', 0),
                                      v.get('auto_repair', 0),
                                      v.get('condition', 100.0)))
    for u in v_upgrades:
        await _push_kvcs(writer, cmd, b'VehicleUpgrade',
                         _msg_vehicle_upgrade(u['id'], u['upgrade_template_id'],
                                              u['vehicle_id'], u['equipped_slot_id'],
                                              u['character_id'], u.get('condition', 100.0),
                                              u.get('auto_repair', 0)))
    # 为没有弹药的载具补创建默认弹药并装备到对应弹药仓（老账号重启前买的载具；
    # 客户端 VehicleUpgradeUtilities 用 equippedVehicleAmmoStorageId != 0 判定已装备）。
    # 已持有但未装备（storage=0）的默认弹药在该仓未被占用时也补装备。
    # 若该载具的弹药仓已装有其他弹药（storage 槽被占），默认弹药只持有不装备，避免槽位冲突。
    v_existing = {(a['vehicle_id'], a['ammo_template_id']) for a in v_ammo_list}
    v_equipped = {(a['vehicle_id'], a['equipped_storage_id']) for a in v_ammo_list
                  if a['equipped_storage_id'] > 0}
    for v in vehicles:
        parent = _VEHICLE_VARIANT_PARENT.get(int(v['vehicle_variant_template_id']))
        if parent is None:
            continue
        storage_slots = {}
        for (storage_id, ammo_template_id) in _default_vehicle_ammo_for(parent):
            slot_ammo = storage_slots.get(storage_id, 1)
            storage_slots[storage_id] = slot_ammo + 1
            if (v['id'], storage_id) in v_equipped:
                continue  # 该弹药仓已被占用，默认弹药持有但不装备
            if (v['id'], ammo_template_id) in v_existing:
                # 已有该默认弹药实例：若未装备（storage=0）则补装备到该仓
                try:
                    for a in v_ammo_list:
                        if (a['vehicle_id'] == v['id']
                                and a['ammo_template_id'] == ammo_template_id
                                and a['equipped_storage_id'] == 0):
                            try:
                                await STORE.set_vehicle_ammo_equipment(pid, a['id'],
                                                                       storage_id,
                                                                       slot_ammo, True)
                            except Exception as e:
                                log(f'[DB] equip default vehicle ammo failed: {e}')
                            await _push_kvcs(writer, cmd, b'VehicleAmmo',
                                             _msg_vehicle_ammo(a['id'], ammo_template_id,
                                                               v['id'], storage_id,
                                                               slot_ammo, v['character_id'],
                                                               a.get('condition', 100.0)))
                            log(f'S->C cmd={cmd:<4} KeyValueChangeSet (VehicleAmmo id={a["id"]} '
                                f'equipped default ammo for vehicle {v["id"]}, '
                                f'template={ammo_template_id}, storage={storage_id}, '
                                f'slot={slot_ammo})')
                            break
                except Exception as e:
                    log(f'[DB] equip existing vehicle default ammo failed: {e}')
                continue
            try:
                va = await STORE.create_vehicle_ammo(pid, ammo_template_id, v['id'],
                                                     v['character_id'],
                                                     _AMMO_MAXCOND.get(int(ammo_template_id), 100.0))
                try:
                    await STORE.set_vehicle_ammo_equipment(pid, va['id'], storage_id,
                                                           slot_ammo, True)
                except Exception as e:
                    log(f'[DB] equip default vehicle ammo failed: {e}')
                await _push_kvcs(writer, cmd, b'VehicleAmmo',
                                 _msg_vehicle_ammo(va['id'], ammo_template_id, v['id'],
                                                   storage_id, slot_ammo, v['character_id'],
                                                   va.get('condition', 100.0)))
                log(f'S->C cmd={cmd:<4} KeyValueChangeSet (VehicleAmmo id={va["id"]} '
                    f'auto-created for vehicle {v["id"]}, template={ammo_template_id}, '
                    f'storage={storage_id}, slot={slot_ammo})')
            except Exception as e:
                log(f'[DB] auto-create default vehicle ammo failed: {e}')
    for a in v_ammo_list:
        await _push_kvcs(writer, cmd, b'VehicleAmmo',
                         _msg_vehicle_ammo(a['id'], a['ammo_template_id'], a['vehicle_id'],
                                           a['equipped_storage_id'], a['slot_index'],
                                           a['character_id'], a.get('condition', 100.0),
                                           a.get('auto_repair', 0)))
    # UniformVariant/UniformPart 已在 character 循环之前推送（见上）
    log(f'S->C cmd={cmd:<4} KeyValueChangeSet (stored data: {len(chars)} chars, '
        f'{len(weapons)} weapons, {len(modifiers)} modifiers, {len(ammo_list)} ammo, '
        f'{len(vehicles)} vehicles, {len(v_upgrades)} vehicle upgrades, '
        f'{len(v_ammo_list)} vehicle ammo, {len(uniform_variants)} uniform variants, '
        f'{len(uniform_parts)} uniform parts)')


async def _load_db_loadout(player_id):
    """聚合玩家全部装备表，按角色/武器/载具分组（PlayerSettings f12 DB 化）。

    返回 None = 装载失败（调用方按空装备处理，不回退任何回放）。
    """
    try:
        weapons = await STORE.list_weapons(player_id)
        w_vars = await STORE.list_weapon_variants(player_id)
        w_ammo = await STORE.list_weapon_ammo(player_id)
        w_mods = await STORE.list_weapon_modifiers(player_id)
        vehicles = await STORE.list_vehicles(player_id)
        v_vars = await STORE.list_vehicle_variants(player_id)
        v_ups = await STORE.list_vehicle_upgrades(player_id)
        v_ammo = await STORE.list_vehicle_ammo(player_id)
        u_vars = await STORE.list_uniform_variants(player_id)
        u_parts = await STORE.list_uniform_parts(player_id)
        boosters = await STORE.list_active_boosters(player_id, ribbon_only=True)
    except Exception as e:
        log(f'[DB] load loadout failed: {e}')
        return None
    loadout = {'weapons': {}, 'w_ammo': {}, 'w_mods': {}, 'vehicles': {},
               'v_ups': {}, 'v_ammo': {}, 'vv_tmpl': {},
               'wv_tmpl': {}, 'wv_owned': {},
               'uv_tmpl': {}, 'u_parts': {}, 'up_tmpl': {},
               'rboosters': {}}
    for w in weapons:
        loadout['weapons'].setdefault(int(w['character_id'] or 0), []).append(w)
    for a in w_ammo:
        loadout['w_ammo'].setdefault(int(a['weapon_id'] or 0), []).append(a)
    for md in w_mods:
        loadout['w_mods'].setdefault(int(md['weapon_id'] or 0), []).append(md)
    for v in vehicles:
        loadout['vehicles'].setdefault(int(v['character_id'] or 0), []).append(v)
    # vv_tmpl：载具迷彩皮肤实例 id → 涂装变体模板 id（VehicleSetting f2 用）
    for vv in v_vars:
        loadout['vv_tmpl'][int(vv['id'])] = int(vv['vehicle_variant_template_id'])
    for u in v_ups:
        loadout['v_ups'].setdefault(int(u['vehicle_id'] or 0), []).append(u)
    for a in v_ammo:
        loadout['v_ammo'].setdefault(int(a['vehicle_id'] or 0), []).append(a)
    for uv in u_vars:
        loadout['uv_tmpl'][int(uv['id'])] = int(uv['uniform_variant_template_id'])
    for up in u_parts:
        loadout['u_parts'].setdefault(int(up['character_id'] or 0), []).append(
            int(up['uniform_part_template_id']))
        # up_tmpl：军装部件实例 id → 模板（charactersetting f10 按 headgear_id 取已装备）
        loadout['up_tmpl'][int(up['id'])] = int(up['uniform_part_template_id'])
    # wv_tmpl：武器皮肤实例 id → 涂装模板（WeaponSetting f10 用）；
    # wv_owned：weapon_id → 已拥有皮肤模板集（旧库 variant_template_id 兜底判定用：
    # 有实例行且 variant_id=0 = 已卸下 → 发默认涂装）
    for wv in w_vars:
        tmpl = int(wv['weapon_variant_template_id'])
        loadout['wv_tmpl'][int(wv['id'])] = tmpl
        loadout['wv_owned'].setdefault(int(wv['weapon_id'] or 0), []).append(tmpl)
    # rboosters：激活中的勋带加成 → {勋带模板 id: 乘数}（账号级，所有角色条目
    # 同发；同一勋带多个加成取最大乘数，不叠乘）。模板不在
    # RibbonBoosterTemplate 表 / 乘数<=1 的行跳过
    for b in boosters:
        rb = _RIBBON_BOOSTERS.get(int(b['template_id']))
        if not rb:
            continue
        rid, mul = rb
        if mul > loadout['rboosters'].get(rid, 0):
            loadout['rboosters'][rid] = mul
    return loadout

