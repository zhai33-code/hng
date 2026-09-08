# -*- coding: utf-8 -*-
"""经济域（economy）：每日奖励 / 兑换券 / 战争债券 / 会员 / 加成 / 门票 / 首金优惠。

消息 schema（decompiled/proto-files/HnG_States）：
- RedeemDailyLoginRewardRequest {playerId=1}
  → RedeemDailyLoginRewardResponse {response=1, infos=2 repeated VoucherPackInfo}
- SetDailyRewardRequest {rewardDay=1} → SetDailyRewardResponse {result=1}
- RedeemVoucherRequest {voucherKey=1} → RedeemVoucherResponse {result=1}
  （165 voucher_invalid_key / 168 voucher_already_used）
- AddWarBondRequest {warBondTemplateId=1, pricingId=2, discountId=3,
  paymentCurrency=4} → AddWarBondResponse {result=1}
- UpdateWarBondsRequest {dummy=1}
  → UpdateWarBondsResponse {warBonds=2 repeated UpdateWarBondsItem{
    warBondId=1, interestsEarnedGold=2, interestsEarnedCredits=3, isExpired=4}}
- JoinFactionBonusOfferRequest {warId=1}
  → JoinFactionBonusOfferResponse {result=1, bonusOfferId=2,
    bonusFactionIds=3 repeated}
- AddFirstGoldOfferRequest {dummy=1} → AddFirstGoldOfferResponse {response=1}
- AddMembershipRequest {membershipTemplateId=1, pricingId=2, discountId=3,
  promoName=4, paymentCurrency=5} → AddMembershipResponse {response=1}
- AddBoosterRequest {characterId=1, isBundle=2, boosterId=3, activateNow=4,
  pricingId=5, discountId=6, paymentCurrency=7} → AddBoosterResponse {response=1}
- ActivateBoosterRequest {boosterId=1} → ActivateBoosterResponse {response=1}
  （208 booster_not_found / 209 booster_already_activated）
- AddVariousBoosterRequest {boosterTemplateId=1, eBoosterType=2, activateNow=3,
  pricingId=4, discountId=5, paymentCurrency=6}
  → AddVariousBoosterResponse {response=1}
- ActivateVariousBoosterRequest {boosterId=1}
  → ActivateVariousBoosterResponse {response=1}
- AddTicketRequest {templateId=1, isTimedTicket=2, pricingId=3, discountId=4,
  paymentCurrency=5} → AddTicketResponse {response=1}
- ActivateTimedTicketRequest {ticketTimedId=1, playerId=2}
  → ActivateTimedTicketResponse {response=1}

客户端 UI 流程（U0 反编译 + swf-rankwf-frontend）：
- RibbonBoosterShopMediator.purchaseRibbonBooster → AddBoosterRequest（勋带加成，
  response==ok 才关窗并发 PurchaseSignal）；purchaseBooster → AddVariousBoosterRequest。
- WarBondsWindowMediator → AddWarBondRequest（战争债券购买）。
- VoucherRedemptionWindowMediator → RedeemVoucherRequest（兑换码兑换）。
- PlayerMembershipWindowMediator → AddMembershipRequest（会员购买）。
- VehicleRepairMediator / WeaponRepairMediator → ActivateTimedTicketRequest
  （修理票激活）。
- 加成/门票/会员的 UI 数据源是 KVCS 表 Booster(xmlTag) / RibbonBooster /
  Ticket / TicketTimed / PlayerMembershipItem，购买/激活后推送对应行刷新。
- Booster.expirationTime 为毫秒数字符串（Number() 直转）；
  TicketTimed.expiredDatetime / PlayerMembershipItem.acquisitionDate/expireDate
  为 Zulu 串（YYYYMMDDHHmmssZ）。
- paymentCurrency: 1=Credits 2=Warfunds 3=Gold 4=Tickets；
  余额不足码 56 not_enough_credits / 153 not_enough_warfunds / 57 not_enough_gold。
"""

import calendar
import os
import random
import time
import xml.etree.ElementTree as ET

from protocol import (
    build_response,
    pb_bool,
    pb_int64,
    pb_int32,
    pb_message,
    pb_parse,
    pb_string,
    pb_uint32,
    )

from state import GAME_DIR, STORE, log

from wardb import (
    BOOSTER_MINUTES_BY_ID,
    MEMBERSHIP_DAYS_BY_ID,
    XP_BOOSTER_MINUTES_BY_ID,
    _RIBBON_BOOSTERS,
    _load_wardb_kvcs,
    )

from messages import _push_kvcs
from player_service import _PaymentFailed, _charge_player, _push_player_refresh

RC_OK = 1

# VirtualCurrencyType → 余额不足 ResponseCodes
_NOT_ENOUGH = {1: 56, 2: 153, 3: 57, 4: 56}

_CUR_NAME = {1: 'Credits', 2: 'Warfunds', 3: 'Gold', 4: 'Tickets'}

# 加成持续时间（分钟）：模板表在客户端 WarDatabase，服务器侧用固定 240 分钟
_BOOSTER_MINUTES = 240
# 限时门票持续时间（小时）
_TICKET_HOURS = 72
# 会员默认时长（天）：仅作 ShopMembershipItems 查表未命中时的兜底，
# 实际时长按 membershipTemplateId 查客户端表（30/90/180/365 天等）
_MEMBERSHIP_DAYS = 30

# ---- 每日签到（Daily Rewards）----
# 客户端 DailyRewardsWindowMediator 从文件服务器拉 dailyRewards.xml 渲染
# 365 天循环表，服务器按同一文件发放当日奖励，保证 UI 与到账一致
# （combat-server 副本优先，与 WARDDB_DIR 同源逻辑）。
_DAILY_REWARDS = {}


def _load_daily_rewards_xml():
    global _DAILY_REWARDS
    # 候选文件不存在时直接跳过（GAME_DIR 自身即 combat-server 的布局下，
    # GAME_DIR\combat-server\... 不存在，避免误报一条 load failed 日志）
    for cand in (os.path.join(GAME_DIR, 'combat-server', 'dailyRewards.xml'),
                 os.path.join(GAME_DIR, 'dailyRewards.xml')):
        if not os.path.isfile(cand):
            continue
        try:
            with open(cand, 'r', encoding='utf-8-sig') as f:
                root = ET.fromstring(f.read())
            for r in root.iter('reward'):
                day = int(r.get('day', 0) or 0)
                if day:
                    _DAILY_REWARDS[day] = str(r.get('type', '') or '')
            return
        except Exception as e:
            log(f'[daily] load {cand} failed: {e}')


_load_daily_rewards_xml()


def _adjust_day(n):
    """客户端 DailyRewardsWindowMediator.adjustDay：365 天循环折算。"""
    return n if n < 365 else n % 365 + 1


# 奖励类型 → 发放规格（数值与客户端 setRewardDescription 硬编码展示一致）
_DAILY_REWARD_GRANTS = {
    'currency1': ('gold', 10), 'currency2': ('gold', 20),
    'currency3': ('credits', 1000), 'currency4': ('credits', 2000),
    'currency5': ('credits', 3000), 'currency6': ('credits', 5000),
    'currency7': ('credits', 100000), 'currency8': ('credits', 150000),
    'currency9': ('warfunds', 100000), 'currency10': ('warfunds', 150000),
    'ribbonbooster1': ('ribbon_booster', 3),
    'ribbonbooster2': ('ribbon_booster', 5),
    'ribbonbooster3': ('ribbon_booster', 10),
    'veteranmembership1': ('membership', 11),   # ShopMembershipItems 11 = 1 天
    'veteranmembership2': ('membership', 14),   # ShopMembershipItems 14 = 3 天
}

# 随机缎带加成模板池（可售区间 1..99，排除促销/特殊模板）
_RIBBON_BOOSTER_POOL = sorted(
    i for i in _RIBBON_BOOSTERS if 1 <= i <= 99)

# 连续登录 bonus（客户端 bonus_reward 列 2..7 天）：
# WarDatabase/consecutive_daily_login_rewards（KVCS 录制表，与客户端
# ConsecutiveDailyLoginRewardsTable 同源）→ {day: [(kind, val), ...]}
_CONSECUTIVE_REWARDS = {}


def _load_consecutive_rewards():
    """解析 consecutive_daily_login_rewards 表（客户端连续登录奖励 2..7 天）。

    行 items 为 XML：`<items><credits amount="2000"/></items>` /
    `<ShopMembershipItems id="11"/>`。day1 无 bonus（首日无连续奖励）。"""
    global _CONSECUTIVE_REWARDS
    chunk = _load_wardb_kvcs('consecutive_daily_login_rewards')
    if chunk is None:
        return
    try:
        for action in chunk.subs:                 # 'set'
            for table in action.subs:             # 'consecutive_daily_login_rewards'
                for row in table.subs:
                    f = pb_parse(row.data)
                    day = int(f.get(2, 0) or 0)
                    if not day:
                        continue
                    try:
                        root = ET.fromstring(_str(f.get(5, '') or ''))
                    except Exception:
                        continue
                    items = []
                    for it in root:
                        tag = it.tag.lower()
                        if tag in ('credits', 'gold', 'warfunds'):
                            items.append((tag, int(it.get('amount', 0) or 0)))
                        elif tag == 'shopmembershipitems':
                            items.append(
                                ('membership', int(it.get('id', 0) or 0)))
                    if items:
                        _CONSECUTIVE_REWARDS[day] = items
    except Exception as e:
        log(f'[daily] load consecutive rewards failed: {e}')


def _str(v):
    if isinstance(v, bytes):
        return v.decode('utf-8', 'replace')
    return str(v)


_load_consecutive_rewards()


def _zulu(dt):
    """datetime → Zulu 串（YYYYMMDDHHmmssZ）。"""
    if dt is None:
        return ''
    return time.strftime('%Y%m%d%H%M%S', dt.utctimetuple()) + 'Z'


def _ms_str(dt):
    """datetime → 毫秒数字符串（Booster.expirationTime 格式）。

    客户端 getTimeLeft 用 Number(expirationTime) - serverTimeAsNumber
    （均为 UTC epoch ms），须用 timegm 而非 mktime（后者按本地时区解析
    UTC 分量，非 UTC 时区服务器上激活中的加成会被立即判过期）。"""
    if dt is None:
        return '0'
    return str(calendar.timegm(dt.utctimetuple()) * 1000)


# ------------------------------------------------------------- KVCS 推送

async def push_booster_rows(sess, writer, pid):
    """推送加成 KVCS 行，按 is_ribbon 列分表（全量刷新，幂等）：

    - 勋带加成（AddBoosterRequest 购买）→ RibbonBooster 表
      （激活链路 RibbonBoosterTable→ProgressionModel→ActivateBoosterCommand）；
    - 通用加成（AddVariousBoosterRequest 购买）→ Booster 表。

    两张模板表 id 有重叠（BoosterTemplate 1-5 是 RibbonBoosterTemplate
    子集），不能按模板 id 分类，须以购买请求类型记录的 is_ribbon 为准。
    勋带行漏推 RibbonBooster 表时，重登后客户端表为空 → 已购勋带加成
    无法激活（激活按钮回落购买态）；勋带行误推 Booster 表时，客户端
    BoosterTemplateTable 查不到模板引发空引用。"""
    if STORE is None or sess.playerview_cmd is None:
        return
    try:
        rows = await STORE.list_boosters(pid)
    except Exception as e:
        log(f'      [DB] list_boosters failed: {e}')
        return
    counts = {}
    for r in rows:
        table = b'RibbonBooster' if r.get('is_ribbon') else b'Booster'
        counts[table] = counts.get(table, 0) + 1
        m = pb_int64(1, r['id'])                          # id
        m += pb_int64(2, pid)                             # playerId
        m += pb_int64(3, r['template_id'])                # boosterTemplateId
        m += pb_uint32(4, 1)                              # itemsAccumulated
        m += pb_string(5, _ms_str(r['expires_at']))       # expirationTime
        await _push_kvcs(writer, sess.playerview_cmd, table, m)
    for table, n in counts.items():
        log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet '
            f'({table.decode()} x{n}, player={pid})')


async def push_ticket_rows(sess, writer, pid, timed=False):
    """推送门票 KVCS 行（Ticket 或 TicketTimed 表）。"""
    if STORE is None or sess.playerview_cmd is None:
        return
    try:
        rows = await STORE.list_tickets(pid, timed_only=timed)
    except Exception as e:
        log(f'      [DB] list_tickets failed: {e}')
        return
    for r in rows:
        if timed:
            m = pb_int64(1, r['id'])                      # id
            m += pb_string(2, _zulu(r['expires_at']))     # expiredDatetime
            m += pb_int64(3, pid)                         # playerId
            m += pb_int64(4, r['template_id'])            # ticketTimedTemplateId
            m += pb_int64(5, 1)                           # earnedTimedTicket
            m += pb_int64(6, 0)                           # consumedTimedTicket
            await _push_kvcs(writer, sess.playerview_cmd, b'TicketTimed', m)
        else:
            m = pb_int64(1, r['id'])                      # id
            m += pb_int64(2, r['template_id'])            # ticketTemplateId
            m += pb_int64(3, pid)                         # playerId
            m += pb_int64(4, 1)                           # earnedTicket
            m += pb_int64(5, 0)                           # consumedTicket
            await _push_kvcs(writer, sess.playerview_cmd, b'Ticket', m)
    if rows:
        table = 'TicketTimed' if timed else 'Ticket'
        log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet '
            f'({table} x{len(rows)}, player={pid})')


async def push_membership_rows(sess, writer, pid):
    """推送会员 KVCS 行（PlayerMembershipItem 表）。

    推送前先清扫：删已过期行、无进行中行时激活最老排队行（官方语义：
    同一时刻只有一份在跑，多份按购买顺序排队激活）。行序保证进行中行在
    最前（客户端 getCurrentMembership 取第一行判 isPremium）；排队行
    expireDate/timeLeft 发空串（isCurrentMembership=false，客户端 tooltip
    按 ShopMembershipItems.durationDays 累加排队天数算最终到期）。"""
    if STORE is None or sess.playerview_cmd is None:
        return
    try:
        deleted, activated = await STORE.sweep_memberships(
            pid, MEMBERSHIP_DAYS_BY_ID, _MEMBERSHIP_DAYS)
        if deleted or activated:
            log(f'      [DB] membership sweep player={pid}: deleted {deleted} '
                f'expired row(s)'
                + (f', activated id={activated["id"]} '
                   f'template={activated["template_id"]} '
                   f'days={activated["days"]}' if activated else ''))
        rows = await STORE.list_memberships(pid)
    except Exception as e:
        log(f'      [DB] list_memberships failed: {e}')
        return
    queued = sum(1 for r in rows if not r['expires_at'])
    for r in rows:
        exp = _zulu(r['expires_at'])
        m = pb_int64(1, r['id'])                          # id
        m += pb_int64(2, pid)                             # playerId
        m += pb_int64(3, r['template_id'])                # shopMembershipItemsId
        m += pb_string(4, _zulu(r['started_at']))         # acquisitionDate
        m += pb_string(5, exp)                            # expireDate
        m += pb_string(7, exp)                            # timeLeft
        m += pb_bool(8, False)                            # onPause
        await _push_kvcs(writer, sess.playerview_cmd, b'PlayerMembershipItem', m)
    if rows:
        log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet '
            f'(PlayerMembershipItem x{len(rows)}, player={pid}, '
            f'{len(rows) - queued} running, {queued} queued)')


async def push_economy_rows(sess, writer, pid):
    """subscribeplayerview 时推送全部经济道具行（会员/加成/门票）。"""
    await push_membership_rows(sess, writer, pid)
    await push_booster_rows(sess, writer, pid)
    await push_ticket_rows(sess, writer, pid)
    await push_ticket_rows(sess, writer, pid, timed=True)


# ------------------------------------------------------------- 每日奖励

async def push_player_offer_metrics_row(sess, writer, pid):
    """推 PlayerOfferMetrics KVCS 行（每日签到窗口数据源）。

    客户端 OpenDailyRewardWindowCommand（登录自动弹窗）/ HomeScreenMediator
    （主界面签到 tile）/ DailyRewardsWindowMediator（窗口渲染）全部按
    playerId 索引查本表行——行缺失时签到窗口永不自动弹出，手动点开时
    取行得 null 直接空引用。
    dailyRewardCount = 已领次数 +（今天可领 ? 1 : 0）：客户端弹窗条件
    redeemedCount < dailyRewardCount，今天已领则两者相等不再弹。"""
    if STORE is None or sess.playerview_cmd is None:
        return
    try:
        metrics = await STORE.get_daily_metrics(pid)
    except Exception as e:
        log(f'      [DB] get_daily_metrics failed: {e}')
        return
    if metrics is None:
        return
    redeemed = int(metrics['n'])
    count = redeemed + (1 if metrics['available'] else 0)
    m = pb_int64(1, pid)                   # id（行 id=playerId，同 CareerOrder 约定）
    m += pb_int64(2, pid)                  # playerId
    m += pb_int32(3, 0)                    # matchesPlayed
    m += pb_int32(4, 0)                    # goldOffersCount
    m += pb_int32(5, count)                # dailyRewardCount
    m += pb_int32(6, redeemed)             # dailyRewardRedeemedCount
    m += pb_string(7, '')                  # unredeemedConsecutiveDayRewards
    m += pb_int32(8, int(metrics['c']))    # consecutiveDayCount
    m += pb_string(9, '')                  # lastFirstBattleDate
    # f10 lastUpdatedConsecutiveDayCount optional，不发
    await _push_kvcs(writer, sess.playerview_cmd, b'PlayerOfferMetrics', m)
    log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet '
        f'(PlayerOfferMetrics id={pid}, day={count}, '
        f'{"claimable" if metrics["available"] else "redeemed"}, '
        f'consecutive={metrics["c"]})')


def _msg_voucher_pack_item(object_type, object_id, info=''):
    """VoucherPackItem {objectType=1 str, objectId=2 i64, info=3 str opt}。"""
    m = pb_string(1, object_type)
    m += pb_int64(2, int(object_id))
    if info:
        m += pb_string(3, info)
    return m


def _msg_voucher_pack_info(item_protos, identifier='daily_login_reward'):
    """VoucherPackInfo {identifier=1, factionTemplateId=2, status=3,
    names=4 repeated, items=5 repeated}。status=1 granted。

    names/items 均为 repeated：零元素必须整体省略（客户端空 infos 走
    checkFlow 不弹窗）。"""
    m = pb_string(1, identifier)
    m += pb_int64(2, 0)                    # factionTemplateId
    m += pb_int32(3, 1)                    # status = granted
    for it in item_protos:
        m += pb_message(5, it)
    return m


async def _grant_daily_reward(pid, day):
    """发放当日签到奖励（按 dailyRewards.xml day 条目）。

    返回 {'items': [VoucherPackItem proto], 'push': set}：
    push 标记需要重推的 KVCS 行（player 货币 / booster / membership）。
    货币走 add_currency；缎带加成随机模板入 player_boosters（is_ribbon）；
    会员按 ShopMembershipItems 时长入 player_memberships。"""
    rtype = _DAILY_REWARDS.get(day, '')
    spec = _DAILY_REWARD_GRANTS.get(rtype)
    out = {'items': [], 'push': set()}
    if STORE is None or spec is None:
        return out
    kind, val = spec
    try:
        if kind in ('gold', 'credits', 'warfunds'):
            await STORE.add_currency(pid, **{kind: val})
            out['items'].append(
                _msg_voucher_pack_item(kind, val))
            out['push'].add('player')
        elif kind == 'ribbon_booster':
            for _ in range(val):
                tid = random.choice(_RIBBON_BOOSTER_POOL) \
                    if _RIBBON_BOOSTER_POOL else 1
                await STORE.add_booster(
                    pid, tid,
                    duration_minutes=BOOSTER_MINUTES_BY_ID.get(
                        tid, _BOOSTER_MINUTES),
                    is_ribbon=True)
                out['items'].append(
                    _msg_voucher_pack_item('ribbonboostertemplate', tid))
            out['push'].add('booster')
        elif kind == 'membership':
            days = MEMBERSHIP_DAYS_BY_ID.get(val, _MEMBERSHIP_DAYS)
            await STORE.add_membership(pid, val, duration_days=days)
            out['items'].append(
                _msg_voucher_pack_item('shopmembershipitems', val))
            out['push'].add('membership')
    except Exception as e:
        log(f'      [DB] grant daily reward (day={day}, {rtype}) failed: {e}')
    return out


async def _grant_consecutive_bonus(pid, consecutive):
    """发放连续登录 bonus（客户端 bonus_reward 列，2..7 天）。

    客户端 DailyRewardsWindowMediator.setUpConsecutiveRewards 的 TODAY 项
    isRedeemed 直接镜像主签到 TODAY（同一次 RedeemDailyLoginReward 领取
    即两奖齐发）；折算镜像客户端：连续天数 >=8 → 回到第 2 天开新一轮。
    返回结构同 _grant_daily_reward。"""
    cday = 2 if int(consecutive) >= 8 else int(consecutive)
    out = {'items': [], 'push': set()}
    if STORE is None:
        return out
    for kind, val in _CONSECUTIVE_REWARDS.get(cday, []):
        try:
            if kind in ('gold', 'credits', 'warfunds'):
                await STORE.add_currency(pid, **{kind: val})
                out['items'].append(_msg_voucher_pack_item(kind, val))
                out['push'].add('player')
            elif kind == 'membership':
                days = MEMBERSHIP_DAYS_BY_ID.get(val, _MEMBERSHIP_DAYS)
                await STORE.add_membership(pid, val, duration_days=days)
                out['items'].append(
                    _msg_voucher_pack_item('shopmembershipitems', val))
                out['push'].add('membership')
        except Exception as e:
            log(f'      [DB] grant consecutive bonus (day={cday}, '
                f'{kind} {val}) failed: {e}')
    return out


async def handle_redeem_daily_login_reward(sess, writer, cmd_id, req_type, proto):
    """RedeemDailyLoginRewardRequest {playerId=1}
    → RedeemDailyLoginRewardResponse{response=1, infos=2 repeated VoucherPackInfo}。

    当天首次领取：daily_reward_redeemed_count+1，按 dailyRewards.xml 当日
    条目发放（货币/随机缎带加成/会员天数）；连续登录第 2..7 天再随领
    一并发放 bonus（consecutive_daily_login_rewards 表），响应带两个
    VoucherPackInfo（客户端 BundleWindow 显示获得内容），并重推
    PlayerOfferMetrics / 货币 / 加成 / 会员 KVCS 行实时刷新。
    当天已领：幂等不发（infos 省略）。"""
    req_player_id = 0
    try:
        f = pb_parse(proto)
        req_player_id = int(f.get(1, 0) or 0)
    except Exception as e:
        log(f'      [economy] RedeemDailyLoginReward parse error: {e}')

    pid = await sess.ensure_player()
    code = RC_OK
    day = 0
    consecutive = 0
    if pid is not None and STORE is not None:
        try:
            ok, day, redeemed, consecutive = \
                await STORE.claim_daily_reward(int(pid))
            code = RC_OK if ok else 64            # player_not_found
            log(f'      [DB] claim_daily_reward player={pid}: '
                f'day={day}, redeemed={redeemed}, '
                f'consecutive={consecutive}')
        except Exception as e:
            log(f'      [DB] claim_daily_reward failed: {e}')
            code = 64

    infos = b''
    packs = []
    if day:
        granted = await _grant_daily_reward(int(pid), _adjust_day(day))
        bonus = await _grant_consecutive_bonus(int(pid), consecutive)
        if granted['items']:
            packs.append(_msg_voucher_pack_info(granted['items']))
        if bonus['items']:
            packs.append(_msg_voucher_pack_info(
                bonus['items'], 'consecutive_daily_login_rewards'))
        if packs:
            infos = b''.join(pb_message(2, p) for p in packs)
        granted['push'] |= bonus['push']
        # KVCS 实时刷新：签到进度行 + 获得物品对应表
        await push_player_offer_metrics_row(sess, writer, int(pid))
        if 'booster' in granted['push']:
            await push_booster_rows(sess, writer, int(pid))
        if 'membership' in granted['push']:
            await push_membership_rows(sess, writer, int(pid))
        if 'player' in granted['push']:
            await _push_player_refresh(sess, writer)
        # 生涯军令 12 eRedeemDailyReward（客户端无上报点）+ 推行实时刷新
        from handlers.career import complete_order_and_push
        await complete_order_and_push(sess, writer, pid, 12)

    resp = build_response(cmd_id, 'RedeemDailyLoginRewardResponse',
                          pb_int32(1, code) + infos)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} RedeemDailyLoginRewardResponse '
        f'(player={req_player_id or pid}, response={code}, '
        f'day={day or "-"}, packs={len(packs)})')


async def handle_set_daily_reward(sess, writer, cmd_id, req_type, proto):
    """SetDailyRewardRequest {rewardDay=1} → SetDailyRewardResponse{result=1}。

    客户端本地记录连续登录天数，服务器确认即可（领取幂等性由
    claim_daily_reward 的 last_daily_reward_at 保证）。"""
    reward_day = 0
    try:
        f = pb_parse(proto)
        reward_day = int(f.get(1, 0) or 0)
    except Exception as e:
        log(f'      [economy] SetDailyReward parse error: {e}')

    resp = build_response(cmd_id, 'SetDailyRewardResponse', pb_int32(1, RC_OK))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetDailyRewardResponse '
        f'(rewardDay={reward_day}, result=1)')


# ------------------------------------------------------------- 兑换券

async def handle_redeem_voucher(sess, writer, cmd_id, req_type, proto):
    """RedeemVoucherRequest {voucherKey=1} → RedeemVoucherResponse{result=1}。

    统一弹窗流程（docs/voucher-redemption-design.md）：输码只记 pending
    流水，不立即发放；客户端收到 ok/168 后自动重发
    QueryVoucherPacksRequest{0} → 264 弹士兵选择 → 选定后 Query{charId}
    才真正发货。code：1 ok / 165 invalid / 168 used / 280 expired。"""
    key = ''
    try:
        f = pb_parse(proto)
        key = _str(f.get(1, '') or '')
    except Exception as e:
        log(f'      [economy] RedeemVoucher parse error: {e}')

    pid = await sess.ensure_player()
    code = 165
    if pid is not None and STORE is not None and key:
        try:
            code = await STORE.redeem_voucher(int(pid), key)
            log(f'      [DB] redeem_voucher player={pid} key={key!r}: '
                f'code={code}')
        except Exception as e:
            log(f'      [DB] redeem_voucher failed: {e}')

    resp = build_response(cmd_id, 'RedeemVoucherResponse', pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} RedeemVoucherResponse '
        f'(key={key!r}, result={code})')


# ------------------------------------------------------------- 战争债券

async def handle_add_warbond(sess, writer, cmd_id, req_type, proto):
    """AddWarBondRequest → AddWarBondResponse{result=1}（购买入库+扣款）。"""
    template_id, pricing_id, currency = 0, 0, 0
    try:
        f = pb_parse(proto)
        template_id = int(f.get(1, 0) or 0)
        pricing_id = int(f.get(2, 0) or 0)
        currency = int(f.get(4, 0) or 0)
    except Exception as e:
        log(f'      [economy] AddWarBond parse error: {e}')

    pid = await sess.ensure_player()
    cost, ok = 0, True
    code = RC_OK
    try:
        # 扣款+发货同一事务：任一步失败整体回滚（钱未扣、货未发）
        async with STORE.transaction() as tx:
            cost, ok = await _charge_player(sess, pricing_id, currency, conn=tx)
            if not ok:
                raise _PaymentFailed()
            if pid is not None and template_id:
                await STORE.add_warbond(int(pid), template_id, conn=tx)
                log(f'      [DB] add_warbond player={pid} template={template_id}')
    except _PaymentFailed:
        code = _NOT_ENOUGH.get(currency, 56)
    except Exception as e:
        log(f'      [DB] add_warbond failed (rolled back): {e}')
        cost, ok = 0, True

    if ok:
        await _push_player_refresh(sess, writer)

    resp = build_response(cmd_id, 'AddWarBondResponse', pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} AddWarBondResponse '
        f'(template={template_id}, result={code}, cost={cost})')


async def handle_update_warbonds(sess, writer, cmd_id, req_type, proto):
    """UpdateWarBondsRequest {dummy=1} → UpdateWarBondsResponse{warBonds=2}。

    结算利息：私服按 0 利息返回持有行（isExpired=false）；
    零条目时 repeated 字段整体省略。"""
    pid = await sess.ensure_player()
    rows = []
    if pid is not None and STORE is not None:
        try:
            rows = await STORE.list_warbonds(int(pid))
        except Exception as e:
            log(f'      [DB] list_warbonds failed: {e}')

    resp_proto = b''
    for r in rows:
        m = pb_int64(1, r['template_id'])         # warBondId
        m += pb_int32(2, 0)                        # interestsEarnedGold
        m += pb_int32(3, 0)                        # interestsEarnedCredits
        m += pb_bool(4, False)                     # isExpired
        resp_proto += pb_message(2, m)

    resp = build_response(cmd_id, 'UpdateWarBondsResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} UpdateWarBondsResponse ({len(rows)} warbonds)')


# ------------------------------------------------------------- 阵营奖励 / 首金

async def handle_join_faction_bonus_offer(sess, writer, cmd_id, req_type, proto):
    """JoinFactionBonusOfferRequest {warId=1}
    → JoinFactionBonusOfferResponse{result=1, bonusOfferId=2}。

    无阵营奖励活动：result=ok + bonusOfferId=0（客户端据此跳过优惠 UI），
    bonusFactionIds 空省略。"""
    war_id = 0
    try:
        f = pb_parse(proto)
        war_id = int(f.get(1, 0) or 0)
    except Exception as e:
        log(f'      [economy] JoinFactionBonusOffer parse error: {e}')

    resp_proto = pb_int32(1, RC_OK)
    resp_proto += pb_int64(2, 0)
    resp = build_response(cmd_id, 'JoinFactionBonusOfferResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} JoinFactionBonusOfferResponse '
        f'(warId={war_id}, result=1, bonusOfferId=0)')


async def handle_add_first_gold_offer(sess, writer, cmd_id, req_type, proto):
    """AddFirstGoldOfferRequest → AddFirstGoldOfferResponse{response=1}。

    标记首金优惠已展示（players.first_gold_offer_shown）。"""
    pid = await sess.ensure_player()
    if pid is not None and STORE is not None:
        try:
            await STORE.set_first_gold_offer_shown(int(pid))
        except Exception as e:
            log(f'      [DB] set_first_gold_offer_shown failed: {e}')

    resp = build_response(cmd_id, 'AddFirstGoldOfferResponse',
                          pb_int32(1, RC_OK))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} AddFirstGoldOfferResponse (response=1)')


# ------------------------------------------------------------- 会员

async def handle_add_membership(sess, writer, cmd_id, req_type, proto):
    """AddMembershipRequest → AddMembershipResponse{response=1}
    （购买入库+扣款+推 PlayerMembershipItem 行）。"""
    template_id, pricing_id, currency = 0, 0, 0
    try:
        f = pb_parse(proto)
        template_id = int(f.get(1, 0) or 0)
        pricing_id = int(f.get(2, 0) or 0)
        currency = int(f.get(5, 0) or 0)
    except Exception as e:
        log(f'      [economy] AddMembership parse error: {e}')

    pid = await sess.ensure_player()
    cost, ok = 0, True
    code = RC_OK
    try:
        # 扣款+发货同一事务：任一步失败整体回滚（钱未扣、货未发）
        async with STORE.transaction() as tx:
            cost, ok = await _charge_player(sess, pricing_id, currency, conn=tx)
            if not ok:
                raise _PaymentFailed()
            if pid is not None and template_id:
                days = MEMBERSHIP_DAYS_BY_ID.get(template_id, _MEMBERSHIP_DAYS)
                row = await STORE.add_membership(int(pid), template_id,
                                                 duration_days=days,
                                                 conn=tx)
                if row['expires_at'] is None:
                    log(f'      [DB] add_membership player={pid} '
                        f'template={template_id} days={days} queued '
                        f'(row {row["id"]}, membership already active)')
                else:
                    log(f'      [DB] add_membership player={pid} '
                        f'template={template_id} days={days} started '
                        f'(row {row["id"]})')
    except _PaymentFailed:
        code = _NOT_ENOUGH.get(currency, 56)
    except Exception as e:
        log(f'      [DB] add_membership failed (rolled back): {e}')
        cost, ok = 0, True

    if ok:
        await _push_player_refresh(sess, writer)
        if pid is not None:
            await push_membership_rows(sess, writer, int(pid))

    resp = build_response(cmd_id, 'AddMembershipResponse', pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} AddMembershipResponse '
        f'(template={template_id}, response={code}, cost={cost})')


# ------------------------------------------------------------- 加成

async def handle_add_booster(sess, writer, cmd_id, req_type, proto):
    """AddBoosterRequest（勋带加成）→ AddBoosterResponse{response=1}。

    购买入 player_boosters + 扣款 + 推 RibbonBooster KVCS 行。"""
    booster_id, activate_now, pricing_id, currency, char_id = 0, False, 0, 0, 0
    try:
        f = pb_parse(proto)
        char_id = int(f.get(1, 0) or 0)
        booster_id = int(f.get(3, 0) or 0)
        activate_now = bool(f.get(4, 0))
        pricing_id = int(f.get(5, 0) or 0)
        currency = int(f.get(7, 0) or 0)
    except Exception as e:
        log(f'      [economy] AddBooster parse error: {e}')

    pid = await sess.ensure_player()
    cost, ok = 0, True
    code = RC_OK
    try:
        # 扣款+发货同一事务：任一步失败整体回滚（钱未扣、货未发）
        async with STORE.transaction() as tx:
            cost, ok = await _charge_player(sess, pricing_id, currency, conn=tx)
            if not ok:
                raise _PaymentFailed()
            if pid is not None and booster_id:
                await STORE.add_booster(
                    int(pid), booster_id,
                    duration_minutes=BOOSTER_MINUTES_BY_ID.get(
                        booster_id, _BOOSTER_MINUTES),
                    activate_now=activate_now, conn=tx, is_ribbon=True)
                log(f'      [DB] add_booster(ribbon) player={pid} '
                    f'template={booster_id} activate_now={activate_now}')
    except _PaymentFailed:
        code = _NOT_ENOUGH.get(currency, 56)
    except Exception as e:
        log(f'      [DB] add_booster failed (rolled back): {e}')
        cost, ok = 0, True

    if ok:
        await _push_player_refresh(sess, writer)
        if pid is not None:
            await push_booster_rows(sess, writer, int(pid))

    resp = build_response(cmd_id, 'AddBoosterResponse', pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} AddBoosterResponse '
        f'(booster={booster_id}, response={code}, cost={cost})')


async def handle_activate_booster(sess, writer, cmd_id, req_type, proto):
    """ActivateBoosterRequest {boosterId=1}（勋带加成激活）
    → ActivateBoosterResponse{response=1/208/209}。"""
    booster_row = 0
    try:
        f = pb_parse(proto)
        booster_row = int(f.get(1, 0) or 0)
    except Exception as e:
        log(f'      [economy] ActivateBooster parse error: {e}')

    pid = await sess.ensure_player()
    code = 208
    if pid is not None and STORE is not None and booster_row:
        try:
            # 时长按行模板分表解析（store 侧），handler 不传死值
            code = await STORE.activate_booster(int(pid), booster_row)
        except Exception as e:
            log(f'      [DB] activate_booster failed: {e}')
            code = 208
    if code == RC_OK and pid is not None:
        await push_booster_rows(sess, writer, int(pid))
        # 生涯军令 46 eActivateRibbonXPBooster（客户端无上报点）+ 推行实时刷新
        from handlers.career import complete_order_and_push
        await complete_order_and_push(sess, writer, pid, 46)

    resp = build_response(cmd_id, 'ActivateBoosterResponse', pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} ActivateBoosterResponse '
        f'(booster={booster_row}, response={code})')


async def handle_add_various_booster(sess, writer, cmd_id, req_type, proto):
    """AddVariousBoosterRequest（通用加成）→ AddVariousBoosterResponse。"""
    template_id, activate_now, pricing_id, currency, booster_type = 0, False, 0, 0, 0
    try:
        f = pb_parse(proto)
        template_id = int(f.get(1, 0) or 0)
        booster_type = int(f.get(2, 0) or 0)
        activate_now = bool(f.get(3, 0))
        pricing_id = int(f.get(4, 0) or 0)
        currency = int(f.get(6, 0) or 0)
    except Exception as e:
        log(f'      [economy] AddVariousBooster parse error: {e}')

    pid = await sess.ensure_player()
    cost, ok = 0, True
    code = RC_OK
    try:
        # 扣款+发货同一事务：任一步失败整体回滚（钱未扣、货未发）
        async with STORE.transaction() as tx:
            cost, ok = await _charge_player(sess, pricing_id, currency, conn=tx)
            if not ok:
                raise _PaymentFailed()
            if pid is not None and template_id:
                await STORE.add_booster(
                    int(pid), template_id,
                    duration_minutes=XP_BOOSTER_MINUTES_BY_ID.get(
                        template_id, _BOOSTER_MINUTES),
                    activate_now=activate_now, conn=tx)
                log(f'      [DB] add_booster(various) player={pid} '
                    f'template={template_id} type={booster_type}')
    except _PaymentFailed:
        code = _NOT_ENOUGH.get(currency, 56)
    except Exception as e:
        log(f'      [DB] add_booster failed (rolled back): {e}')
        cost, ok = 0, True

    if ok:
        await _push_player_refresh(sess, writer)
        if pid is not None:
            await push_booster_rows(sess, writer, int(pid))

    resp = build_response(cmd_id, 'AddVariousBoosterResponse',
                          pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} AddVariousBoosterResponse '
        f'(template={template_id}, response={code}, cost={cost})')


async def handle_activate_various_booster(sess, writer, cmd_id, req_type,
                                          proto):
    """ActivateVariousBoosterRequest {boosterId=1}（通用加成激活）。"""
    booster_row = 0
    try:
        f = pb_parse(proto)
        booster_row = int(f.get(1, 0) or 0)
    except Exception as e:
        log(f'      [economy] ActivateVariousBooster parse error: {e}')

    pid = await sess.ensure_player()
    code = 208
    if pid is not None and STORE is not None and booster_row:
        try:
            # 时长按行模板分表解析（store 侧），handler 不传死值
            code = await STORE.activate_booster(int(pid), booster_row)
        except Exception as e:
            log(f'      [DB] activate_booster failed: {e}')
            code = 208
    if code == RC_OK and pid is not None:
        await push_booster_rows(sess, writer, int(pid))
        # 生涯军令 46 eActivateRibbonXPBooster（客户端无上报点）+ 推行实时刷新
        from handlers.career import complete_order_and_push
        await complete_order_and_push(sess, writer, pid, 46)

    resp = build_response(cmd_id, 'ActivateVariousBoosterResponse',
                          pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} ActivateVariousBoosterResponse '
        f'(booster={booster_row}, response={code})')


# ------------------------------------------------------------- 门票

async def handle_add_ticket(sess, writer, cmd_id, req_type, proto):
    """AddTicketRequest → AddTicketResponse{response=1}
    （购买入库+扣款+推 Ticket/TicketTimed 行）。"""
    template_id, is_timed, pricing_id, currency = 0, False, 0, 0
    try:
        f = pb_parse(proto)
        template_id = int(f.get(1, 0) or 0)
        is_timed = bool(f.get(2, 0))
        pricing_id = int(f.get(3, 0) or 0)
        currency = int(f.get(5, 0) or 0)
    except Exception as e:
        log(f'      [economy] AddTicket parse error: {e}')

    pid = await sess.ensure_player()
    cost, ok = 0, True
    code = RC_OK
    try:
        # 扣款+发货同一事务：任一步失败整体回滚（钱未扣、货未发）
        async with STORE.transaction() as tx:
            cost, ok = await _charge_player(sess, pricing_id, currency, conn=tx)
            if not ok:
                raise _PaymentFailed()
            if pid is not None and template_id:
                await STORE.add_ticket(int(pid), template_id, is_timed=is_timed,
                                       duration_hours=_TICKET_HOURS, conn=tx)
                log(f'      [DB] add_ticket player={pid} template={template_id} '
                    f'timed={is_timed}')
    except _PaymentFailed:
        code = _NOT_ENOUGH.get(currency, 56)
    except Exception as e:
        log(f'      [DB] add_ticket failed (rolled back): {e}')
        cost, ok = 0, True

    if ok:
        await _push_player_refresh(sess, writer)
        if pid is not None:
            await push_ticket_rows(sess, writer, int(pid), timed=is_timed)

    resp = build_response(cmd_id, 'AddTicketResponse', pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} AddTicketResponse '
        f'(template={template_id}, timed={is_timed}, response={code}, '
        f'cost={cost})')


async def handle_activate_timed_ticket(sess, writer, cmd_id, req_type, proto):
    """ActivateTimedTicketRequest {ticketTimedId=1, playerId=2}
    → ActivateTimedTicketResponse{response=1/208/209}。"""
    ticket_row, req_pid = 0, 0
    try:
        f = pb_parse(proto)
        ticket_row = int(f.get(1, 0) or 0)
        req_pid = int(f.get(2, 0) or 0)
    except Exception as e:
        log(f'      [economy] ActivateTimedTicket parse error: {e}')

    pid = await sess.ensure_player()
    code = 208
    if pid is not None and STORE is not None and ticket_row:
        try:
            code = await STORE.activate_ticket(int(pid), ticket_row,
                                               duration_hours=_TICKET_HOURS)
        except Exception as e:
            log(f'      [DB] activate_ticket failed: {e}')
            code = 208
    if code == RC_OK and pid is not None:
        await push_ticket_rows(sess, writer, int(pid), timed=True)

    resp = build_response(cmd_id, 'ActivateTimedTicketResponse',
                          pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} ActivateTimedTicketResponse '
        f'(ticket={ticket_row}, response={code})')


HANDLERS = {
    'RedeemDailyLoginRewardRequest': handle_redeem_daily_login_reward,
    'SetDailyRewardRequest': handle_set_daily_reward,
    'RedeemVoucherRequest': handle_redeem_voucher,
    'AddWarBondRequest': handle_add_warbond,
    'UpdateWarBondsRequest': handle_update_warbonds,
    'JoinFactionBonusOfferRequest': handle_join_faction_bonus_offer,
    'AddFirstGoldOfferRequest': handle_add_first_gold_offer,
    'AddMembershipRequest': handle_add_membership,
    'AddBoosterRequest': handle_add_booster,
    'ActivateBoosterRequest': handle_activate_booster,
    'AddVariousBoosterRequest': handle_add_various_booster,
    'ActivateVariousBoosterRequest': handle_activate_various_booster,
    'AddTicketRequest': handle_add_ticket,
    'ActivateTimedTicketRequest': handle_activate_timed_ticket,
}
