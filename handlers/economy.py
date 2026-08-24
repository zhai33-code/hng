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

客户端 UI 流程（U0 反编译 + swf-rank/swf-frontend）：
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

import time

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

from state import STORE, log

from wardb import MEMBERSHIP_DAYS_BY_ID

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
# 每日登录奖励 credits
_DAILY_REWARD_CREDITS = 2500


def _str(v):
    if isinstance(v, bytes):
        return v.decode('utf-8', 'replace')
    return str(v)


def _zulu(dt):
    """datetime → Zulu 串（YYYYMMDDHHmmssZ）。"""
    if dt is None:
        return ''
    return time.strftime('%Y%m%d%H%M%S', dt.utctimetuple()) + 'Z'


def _ms_str(dt):
    """datetime → 毫秒数字符串（Booster.expirationTime 格式）。"""
    if dt is None:
        return '0'
    return str(int(time.mktime(dt.timetuple()) * 1000))


# ------------------------------------------------------------- KVCS 推送

async def push_booster_rows(sess, writer, pid, ribbon=False):
    """推送加成 KVCS 行（Booster 或 RibbonBooster 表，xmlTag 区分）。

    ribbon=True 推 RibbonBooster（勋带加成，AddBoosterRequest 购买）；
    否则推 Booster（通用加成，AddVariousBoosterRequest 购买）。"""
    if STORE is None or sess.playerview_cmd is None:
        return
    try:
        rows = await STORE.list_boosters(pid)
    except Exception as e:
        log(f'      [DB] list_boosters failed: {e}')
        return
    table = b'RibbonBooster' if ribbon else b'Booster'
    for r in rows:
        m = pb_int64(1, r['id'])                          # id
        m += pb_int64(2, pid)                             # playerId
        m += pb_int64(3, r['template_id'])                # boosterTemplateId
        m += pb_uint32(4, 1)                              # itemsAccumulated
        m += pb_string(5, _ms_str(r['expires_at']))       # expirationTime
        await _push_kvcs(writer, sess.playerview_cmd, table, m)
    if rows:
        log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet '
            f'({table.decode()} x{len(rows)}, player={pid})')


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
    """推送会员 KVCS 行（PlayerMembershipItem 表）。"""
    if STORE is None or sess.playerview_cmd is None:
        return
    try:
        rows = await STORE.list_memberships(pid)
    except Exception as e:
        log(f'      [DB] list_memberships failed: {e}')
        return
    for r in rows:
        m = pb_int64(1, r['id'])                          # id
        m += pb_int64(2, pid)                             # playerId
        m += pb_int64(3, r['template_id'])                # shopMembershipItemsId
        m += pb_string(4, _zulu(r['started_at']))         # acquisitionDate
        m += pb_string(5, _zulu(r['expires_at']))         # expireDate
        m += pb_string(7, _zulu(r['expires_at']))         # timeLeft
        m += pb_bool(8, False)                            # onPause
        await _push_kvcs(writer, sess.playerview_cmd, b'PlayerMembershipItem', m)
    if rows:
        log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet '
            f'(PlayerMembershipItem x{len(rows)}, player={pid})')


async def push_economy_rows(sess, writer, pid):
    """subscribeplayerview 时推送全部经济道具行（会员/加成/门票）。"""
    await push_membership_rows(sess, writer, pid)
    await push_booster_rows(sess, writer, pid)
    await push_ticket_rows(sess, writer, pid)
    await push_ticket_rows(sess, writer, pid, timed=True)


# ------------------------------------------------------------- 每日奖励

async def handle_redeem_daily_login_reward(sess, writer, cmd_id, req_type, proto):
    """RedeemDailyLoginRewardRequest {playerId=1}
    → RedeemDailyLoginRewardResponse{response=1}。

    当天首次领取发 credits（幂等）；infos 为空（无附赠券包，repeated 省略）。"""
    req_player_id = 0
    try:
        f = pb_parse(proto)
        req_player_id = int(f.get(1, 0) or 0)
    except Exception as e:
        log(f'      [economy] RedeemDailyLoginReward parse error: {e}')

    pid = await sess.ensure_player()
    code = RC_OK
    granted = 0
    if pid is not None and STORE is not None:
        try:
            ok, granted = await STORE.redeem_daily_reward(
                int(pid), credits=_DAILY_REWARD_CREDITS)
            code = RC_OK if ok else 64            # player_not_found
            log(f'      [DB] redeem_daily_reward player={pid}: '
                f'granted={granted}')
        except Exception as e:
            log(f'      [DB] redeem_daily_reward failed: {e}')
            code = 64
    if granted:
        await _push_player_refresh(sess, writer)

    resp = build_response(cmd_id, 'RedeemDailyLoginRewardResponse',
                          pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} RedeemDailyLoginRewardResponse '
        f'(player={req_player_id or pid}, response={code}, granted={granted})')


async def handle_set_daily_reward(sess, writer, cmd_id, req_type, proto):
    """SetDailyRewardRequest {rewardDay=1} → SetDailyRewardResponse{result=1}。

    客户端本地记录连续登录天数，服务器确认即可（领取幂等性由
    redeem_daily_reward 的 last_daily_reward_at 保证）。"""
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
    """RedeemVoucherRequest {voucherKey=1} → RedeemVoucherResponse{result=1}。"""
    key = ''
    try:
        f = pb_parse(proto)
        key = _str(f.get(1, '') or '')
    except Exception as e:
        log(f'      [economy] RedeemVoucher parse error: {e}')

    pid = await sess.ensure_player()
    code, granted = 165, {}
    if pid is not None and STORE is not None and key:
        try:
            code, granted = await STORE.redeem_voucher(int(pid), key)
            log(f'      [DB] redeem_voucher player={pid} key={key!r}: '
                f'code={code} granted={granted}')
        except Exception as e:
            log(f'      [DB] redeem_voucher failed: {e}')
    if granted:
        await _push_player_refresh(sess, writer)

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
                await STORE.add_membership(int(pid), template_id,
                                           duration_days=days,
                                           conn=tx)
                log(f'      [DB] add_membership player={pid} '
                    f'template={template_id} days={days}')
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
                await STORE.add_booster(int(pid), booster_id,
                                        duration_minutes=_BOOSTER_MINUTES,
                                        activate_now=activate_now, conn=tx)
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
            await push_booster_rows(sess, writer, int(pid), ribbon=True)

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
            code = await STORE.activate_booster(int(pid), booster_row,
                                                duration_minutes=_BOOSTER_MINUTES)
        except Exception as e:
            log(f'      [DB] activate_booster failed: {e}')
            code = 208
    if code == RC_OK and pid is not None:
        await push_booster_rows(sess, writer, int(pid), ribbon=True)

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
                await STORE.add_booster(int(pid), template_id,
                                        duration_minutes=_BOOSTER_MINUTES,
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
            code = await STORE.activate_booster(int(pid), booster_row,
                                                duration_minutes=_BOOSTER_MINUTES)
        except Exception as e:
            log(f'      [DB] activate_booster failed: {e}')
            code = 208
    if code == RC_OK and pid is not None:
        await push_booster_rows(sess, writer, int(pid))

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
