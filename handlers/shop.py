# -*- coding: utf-8 -*-
"""商店域（捆绑包、勋带等级、券包、EOS 权益、金价查询）。

从 realtest_server.dispatch 机械迁移，函数体保持原样；
统一签名 (sess, writer, cmd_id, req_type, proto)。
"""


import struct
import time

from protocol import (
    Chunk,
    build_response,
    pb_bool,
    pb_int64,
    pb_int32,
    pb_message,
    pb_parse,
    pb_string,
    )

from state import (
    STORE,
    log,
    )

from wardb import (
    _CUR_NAME,
    _RIBBON_LEVEL_XP,
    _RIBBON_META_LEVEL,
    _RIBBON_UPGRADES,
    _rank_from_combat_xp,
    )

from messages import (
    _msg_character,
    _push_kvcs,
    )

from player_service import (
    _char_unlock_bits_for,
    _charge_player,
    _msg_player_for,
    _push_player_refresh,
    )


async def handle_buystorebundlerequest(sess, writer, cmd_id, req_type, proto):
    # 商店捆绑包购买请求
    # BuyStoreBundleRequest { int64 storeBundleId=1; int64 pricingId=2;
    #   int64 storeDiscountId=3; int64 characterId=4; int32 paymentCurrency=5; }
    # BuyStoreBundleResponse { int32 result=1; }
    # paymentCurrency: 1=Credits, 2=Warfunds, 3=Gold, 4=Tickets
    # ResponseCodes: ok=1, not_enough_gold=57, not_enough_credits=56, not_enough_warfunds=153
    try:
        f = pb_parse(proto)
        bundle_id = f.get(1, 0)
        pricing_id = f.get(2, 0)
        discount_id = f.get(3, 0)
        char_id = f.get(4, 0)
        currency = f.get(5, 0)
        currency_names = {1: 'Credits', 2: 'Warfunds', 3: 'Gold', 4: 'Tickets'}
        log(f'      BuyStoreBundle: bundleId={bundle_id}, pricingId={pricing_id}, '
            f'discountId={discount_id}, charId={char_id}, currency={currency_names.get(currency, currency)}')
    except Exception as e:
        log(f'      BuyStoreBundle parse error: {e}')

    # 扣款；余额不足返回错误响应
    cost, ok = await _charge_player(sess, pricing_id, currency)
    if not ok:
        resp_proto = pb_int32(1, 56)  # not_enough_credits
        resp = build_response(cmd_id, 'BuyStoreBundleResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} BuyStoreBundleResponse (result=56 not_enough) cost={cost}')
        return

    # 返回 result=1 (ok)，私服默认购买成功
    # 客户端收到 ok 后 dispatch PurchaseCompletedSignal，显示购买成功弹窗
    resp_proto = pb_int32(1, 1)  # result = 1 (ok)
    resp = build_response(cmd_id, 'BuyStoreBundleResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} BuyStoreBundleResponse (result=1 ok)')

    # 通过 PLAYER_VIEW 推送更新后的 player 数据，刷新货币显示
    if sess.playerview_cmd is not None:
        player_proto = await _msg_player_for(sess)
        data_chunk = Chunk(player_proto)
        table_chunk = Chunk(b'player', [data_chunk])
        action_chunk = Chunk(b'set', [table_chunk])
        title_chunk = Chunk(b'KeyValueChangeSet', [action_chunk])
        cs_main = Chunk(struct.pack('<i', sess.playerview_cmd), [title_chunk])
        cs_bytes = cs_main.write(b'')
        writer.write(cs_bytes)
        await writer.drain()
        log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet (player refresh, {len(player_proto)}B)')



async def handle_queryshopwarbonditemsrequest(sess, writer, cmd_id, req_type, proto):
    # 查询战争债券物品（PlayerProfileMediator / WarBondsWindowMediator）
    # QueryShopWarBondItemsResponse { repeated ShopWarBondItem warBonds = 2; }
    # ShopWarBondItem { int64 id=1; int64 templateId=2; uint32 serialNumber=3;
    #   int64 playerId=4; string aquiredDate=5; string lastInterestDate=6;
    #   bool isExpired=7; }
    # 数据源 player_warbonds（AddWarBondRequest 购买入库）；
    # 注意：repeated 字段零条目时必须完全省略字段（b''），
    # 发送 pb_message(2, b'') 空条目会导致客户端解析越界（Buffer overflow）卡死队列
    resp_proto = b''
    pid = await sess.ensure_player()
    rows = []
    if pid is not None and STORE is not None:
        try:
            rows = await STORE.list_warbonds(int(pid))
        except Exception as e:
            log(f'      [DB] list_warbonds failed: {e}')
            rows = []
    for i, r in enumerate(rows, 1):
        m = pb_int64(1, r['template_id'])      # id（模板 id 当行 id）
        m += pb_int64(2, r['template_id'])     # templateId
        m += pb_int32(3, i)                    # serialNumber（第几张）
        m += pb_int64(4, int(pid))             # playerId
        m += pb_string(5, '')                  # aquiredDate
        m += pb_string(6, '')                  # lastInterestDate
        m += pb_bool(7, False)                 # isExpired
        resp_proto += pb_message(2, m)
    resp = build_response(cmd_id, 'QueryShopWarBondItemsResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} QueryShopWarBondItemsResponse '
        f'({len(rows)} war bonds)')



async def handle_queryvoucherpacksrequest(sess, writer, cmd_id, req_type, proto):
    # 查询兑换券包，返回空列表（无待兑换券包）
    # QueryVoucherPacksResponse { int32 response=1; repeated VoucherPackInfo infos=2;
    #   string validFactionTemplateIds=3; string validCharacterTemplateIds=4; }
    # 客户端 ServiceHandler 的请求队列是严格串行的：
    # 一个请求没收到响应，后续所有请求（包括购买）都会堵在队列里（左上角 loading 转圈）
    resp_proto = b''
    resp_proto += pb_int32(1, 1)       # response = 1 (ok)
    resp_proto += pb_string(3, '')      # validFactionTemplateIds (空)
    resp_proto += pb_string(4, '')      # validCharacterTemplateIds (空)
    resp = build_response(cmd_id, 'QueryVoucherPacksResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} QueryVoucherPacksResponse (0 voucher packs)')



async def handle_epicqueryentitlementsrequest(sess, writer, cmd_id, req_type, proto):
    # Epic 渠道周期性查询用户拥有的道具权益（DLC / 兑换券）
    # 触发点：VoucherRedemptionModel.checkForEpicEntitlements —— 只要 Global.isEpic=true 就会
    # 在登录后、UI 转场后周期性发送，客户端串行队列，必须回复解锁。
    # 客户端 epicQueryEntitlementsResponse 只读 response 字段就 checkFlow(true) 继续流程，
    # 不消费任何 entitlement 数据，故返回 response=ok + epicResponseCode=0 即可。
    # EpicQueryEntitlementsResponse { ResponseCodes response=1; int32 epicResponseCode=2; }
    resp_proto = b''
    resp_proto += pb_int32(1, 1)       # response = 1 (ResponseCodes.ok)
    resp_proto += pb_int32(2, 0)       # epicResponseCode = 0
    resp = build_response(cmd_id, 'EpicQueryEntitlementsResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} EpicQueryEntitlementsResponse (response=1 ok, no entitlements)')



async def handle_queryactivesurveyrequest(sess, writer, cmd_id, req_type, proto):
    # 查询活跃问卷调查，返回空 URL（无问卷，客户端跳过弹窗继续流程）
    # QueryActiveSurveyResponse { string queryURL=1; string imageURL=2; }
    resp_proto = b''
    resp_proto += pb_string(1, '')      # queryURL (空 = 无问卷)
    resp_proto += pb_string(2, '')      # imageURL (空)
    resp = build_response(cmd_id, 'QueryActiveSurveyResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} QueryActiveSurveyResponse (no survey)')



async def handle_getgoldpricesrequest(sess, writer, cmd_id, req_type, proto):
    # 查询金币价格表，返回空列表（无充值渠道；私服玩家货币由服务器发放）
    # GetGoldPricesResponse { repeated Price prices=1; string currencyCode=2; int32 response=3; }
    # 注意：repeated 字段零条目时必须完全省略（b''），否则客户端解析越界
    resp_proto = b''
    resp_proto += pb_string(2, 'EUR')   # currencyCode
    resp_proto += pb_int32(3, 1)        # response = 1 (ok)
    resp = build_response(cmd_id, 'GetGoldPricesResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} GetGoldPricesResponse (0 gold prices)')



async def handle_addribbonlevelrequest(sess, writer, cmd_id, req_type, proto):
    # 购买勋带等级（ProgressionOverviewWindowMediator.levelPriceClicked）
    # AddRibbonLevelRequest { int64 ribbonLevelUpgradeTemplateId=1; int64 characterId=2;
    #   int64 pricingId=3; int64 discountId=4; int32 paymentCurrency=5; }
    # AddRibbonLevelResponse { int32 result = 1; }
    upgrade_template_id = 0
    pricing_id = 0
    try:
        f = pb_parse(proto)
        upgrade_template_id = f.get(1, 0)
        char_id = f.get(2, 1)
        pricing_id = f.get(3, 0)
        currency = f.get(5, 0)
        log(f'      AddRibbonLevel: ribbonLevelUpgradeTemplateId={upgrade_template_id}, '
            f'charId={char_id}, pricingId={pricing_id}, '
            f'currency={_CUR_NAME.get(int(currency), currency)}')
    except Exception as e:
        log(f'      AddRibbonLevel parse error: {e}')

    # 扣款（购买勋带等级费用）；余额不足返回错误响应
    cost, ok = await _charge_player(sess, pricing_id, currency)
    if not ok:
        resp_proto = pb_int32(1, 56)  # not_enough_credits
        resp = build_response(cmd_id, 'AddRibbonLevelResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} AddRibbonLevelResponse (result=56 not_enough) cost={cost}')
        return

    # 解锁该勋带等级对应的全部解锁需求（unlockMetaName==ribbonTemplateId,
    # unlockMetaLevel <= ribbonLevelId），持久化后客户端 isUnlocked=true
    unlocked_uids = set()
    info = _RIBBON_UPGRADES.get(int(upgrade_template_id))
    if info is not None:
        ribbon_template_id, ribbon_level_id = info
        for (uid, level) in _RIBBON_META_LEVEL.get(str(ribbon_template_id), set()):
            if level <= ribbon_level_id:
                unlocked_uids.add(uid)
    pid = await sess.ensure_player()
    if pid is not None:
        if unlocked_uids:
            try:
                await STORE.add_extra_unlock_uids(pid, char_id, unlocked_uids)
                log(f'      AddRibbonLevel: unlocked {len(unlocked_uids)} uid(s) (ribbon={info})')
            except Exception as e:
                log(f'[DB] add_extra_unlock_uids failed: {e}')
        if info is not None:
            # 持久化勋带经验到角色（人物级；勋带单源 = total_xp，等级由阈值推导。
            # ResponseReadCharacterStats attributes 按角色显示 XP/等级/进度）
            try:
                min_xp = _RIBBON_LEVEL_XP.get(ribbon_level_id, 0)
                await STORE.set_ribbon_xp_floor(pid, char_id,
                                                ribbon_template_id, min_xp)
                log(f'      AddRibbonLevel: char {char_id} ribbon {ribbon_template_id} '
                    f'totalXp -> max(prev, {min_xp}) (level {ribbon_level_id})')
            except Exception as e:
                log(f'[DB] set_ribbon_xp_floor failed: {e}')

    resp_proto = pb_int32(1, 1)  # result = 1 (ok)
    resp = build_response(cmd_id, 'AddRibbonLevelResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} AddRibbonLevelResponse (result=1 ok, +{len(unlocked_uids)} uids)')

    # 推送 player 刷新货币显示（购买扣款后）
    await _push_player_refresh(sess, writer)

    # 重新推送目标角色（unlockBits 更新 + statUpdateTimeStamp 变化 → 客户端
    # CharacterModel.checkStatsWasUpdated 检测到 stats 过期 → 重新请求
    # RequestReadCharacterStats → attributes 刷新，进度条/按钮/图标立即更新）
    if sess.soldier_view_cmd is not None:
        try:
            pid_c = await sess.ensure_player()
            if pid_c is not None:
                new_ts = int(time.time() * 1000)
                for ch in await STORE.list_characters(pid_c):
                    if int(ch['id']) != int(char_id):
                        continue
                    bits = await _char_unlock_bits_for(sess, ch['template'], ch['faction'], ch['id'])
                    # 尾部字段传全：客户端 setObject 整体替换 schema，漏传
                    # head_id/previous_template 等会被抹回默认值（外观回退）
                    await _push_kvcs(writer, sess.soldier_view_cmd, b'character',
                                     _msg_character(ch['name'], ch['faction'], ch['template'],
                                                    _rank_from_combat_xp(ch.get('combat_xp', 0)),
                                                    ch['id'], bits,
                                                    ch.get('combat_badge_0', 0),
                                                    ch.get('combat_badge_1', 0),
                                                    new_ts,
                                                    ch.get('uniform_variant_id', 0),
                                                    ch.get('headgear_id', 0),
                                                    1 if ch.get('is_deleted') else 0,
                                                    ch.get('previous_template', 0),
                                                    ch.get('head_id', 0),
                                                    0,
                                                    1 if ch.get('is_on_leave') else 0))
                    log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet (character '
                        f'{ch["name"]} re-pushed, unlockBits={sum(bits)} true, '
                        f'statTs={new_ts})')
        except Exception as e:
            log(f'[DB] reload character after ribbon level failed: {e}')


HANDLERS = {
    'BuyStoreBundleRequest': handle_buystorebundlerequest,
    'QueryShopWarBondItemsRequest': handle_queryshopwarbonditemsrequest,
    'QueryVoucherPacksRequest': handle_queryvoucherpacksrequest,
    'EpicQueryEntitlementsRequest': handle_epicqueryentitlementsrequest,
    'QueryActiveSurveyRequest': handle_queryactivesurveyrequest,
    'GetGoldPricesRequest': handle_getgoldpricesrequest,
    'AddRibbonLevelRequest': handle_addribbonlevelrequest,
}
