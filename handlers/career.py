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

from messages import _push_kvcs
from player_service import _push_player_refresh
from wardb import _parse_wardb_table


# ------------------------------------------------------------- 模板缓存

_BP_SEASONS = None        # [{id, name, loc, from, to, s_tier, s_lvl, s_score}]
_BP_TEMPLATES = None      # [{id, tier, level, loc, image, reward, season, req}]
_CAREER_TEMPLATES = None  # {template_id: (type, required_count, rewards_xml)}

# 赛季延期目标（Zulu）：本地 WarDatabase 里 4 个赛季 activeTo 全部是
# 2022/2023 已过期，延期后 battlePassIsRunning() 为 true，通行证界面可用。
_SEASON_EXTENDED_TO = '20990101000000Z'

# 当前激活赛季（最新 id）：battle_pass 行与延期模板都指向它。
_ACTIVE_SEASON = 4


def _str(v):
    if isinstance(v, bytes):
        return v.decode('utf-8', 'replace')
    return str(v)


def _bp_seasons():
    """battle_pass_season_template 行缓存（activeTo 已延期）。"""
    global _BP_SEASONS
    if _BP_SEASONS is None:
        rows = []
        for f in _parse_wardb_table('battle_pass_season_template'):
            rows.append({
                'id': int(f.get(1, 0) or 0),
                'name': _str(f.get(2, '')),
                'loc': _str(f.get(3, '')),
                'from': _str(f.get(4, '')),
                'to': _SEASON_EXTENDED_TO,
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

async def _grant_reward_xml(pid, xml_str, tag):
    """解析奖励 XML（<items target='player'><credits amount='2000'/></items>），
    credits/warfunds/gold 入账，其他物品类型记日志跳过。"""
    granted = []
    try:
        root = ET.fromstring(xml_str)
    except Exception as e:
        log(f'      [career] reward xml parse error ({tag}): {e}')
        return granted
    if STORE is None:
        return granted
    delta = {}
    for item in root:
        obj = item.tag
        amount = int(float(item.get('amount', '0') or 0))
        if obj in ('credits', 'warfunds', 'gold') and amount > 0:
            delta[obj] = delta.get(obj, 0) + amount
            granted.append(f'{obj}+{amount}')
    if delta:
        # 原子增量入账（读-改-写绝对值会覆盖并发变动，如结算加钱）
        await STORE.add_currency(pid, **delta)
    skipped = [item.tag for item in root if item.tag not in delta]
    if skipped:
        log(f'      [career] reward items skipped ({tag}): {skipped}')
    return granted


# ------------------------------------------------------------- KVCS 推送

async def push_career_order_row(sess, writer, pid):
    """推玩家 CareerOrder 进度行（单行 id=playerId）。

    progress 逗号串与 rewardReceivedBits 均为 47 槽（下标 0 占位），
    career_orders 表 status：0 进行中 1 已完成 2 已领取。"""
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
                         pending, received, tier=1, level=1):
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
    m += pb_string(11, '')                  # unlockDateTime（未用解锁流程）
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
    m = _msg_battle_pass_row(row_id, pid, season_id, score,
                             bp['purchased'], bp['active'], pending, received,
                             tier, level)
    await _push_kvcs(writer, sess.playerview_cmd, b'battle_pass', m)
    log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet '
        f'(battle_pass id={row_id}, score={score}, tier={tier}, lvl={level}, '
        f'pending={len(pending)})')


async def push_season_templates(sess, writer):
    """推延期赛季模板行（battle_pass_season_template 表 UPDATE）。

    本地文件 4 赛季全部过期；KVCS 覆盖 activeTo 为 2099 后
    BattlePassModel.battlePassIsRunning() 为 true，通行证界面显示进行中。"""
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
                granted = await _grant_reward_xml(int(pid), tpl[2],
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
        await push_battle_pass_row(sess, writer, int(pid))

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
        await push_battle_pass_row(sess, writer, int(pid), season_id)

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
    discountId=4 paymentCurrency=5} → PurchaseBattlePassTierResponse{response=1}。

    购买层级完成：score 提升到该模板 requiredScore + battle_pass 行刷新。"""
    template_id = 0
    try:
        f = pb_parse(proto)
        template_id = int(f.get(1, 0) or 0)
    except Exception as e:
        log(f'      [career] PurchaseBattlePassTier parse error: {e}')

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
            await push_battle_pass_row(sess, writer, int(pid), tpl['season'])

    resp = build_response(cmd_id, 'PurchaseBattlePassTierResponse', pb_int32(1, 1))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} PurchaseBattlePassTierResponse '
        f'(response=1, template={template_id})')


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
        try:
            bp = await STORE.get_battle_pass(int(pid), season_id)
        except Exception as e:
            log(f'      [DB] get_battle_pass failed: {e}')
            bp = None
        if bp is not None:
            score = bp['score']
            received = [int(x) for x in bp.get('claimed', [])]
            granted_ids = []
            for t in _bp_templates():
                if (t['season'] == season_id and t['req'] <= score
                        and t['id'] not in received):
                    first, _ = await STORE.claim_battle_pass_reward(
                        int(pid), season_id, t['id'])
                    if first:
                        granted_ids.append(t['id'])
                        await _grant_reward_xml(int(pid), t['reward'],
                                                f'battle pass tier {t["id"]}')
                        resp_proto += pb_message(2, _msg_bp_template(t))
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
