# -*- coding: utf-8 -*-
"""AT（Assault Team）生命周期请求域（Phase 5，docs/hng-rts-protocol.md §5.2）。

处理 HnG_States 请求族：
  CreateCommandNodeRequest / UpgradeCommandNodeRequest /
  DeployCommandNodeRequest / RetreatCommandNodeRequest /
  RenameCommandnodeRequest / dissolve_commandnode /
  query_commandnode_owner / LeaveSupplyQueueRequest / ReorderSupplyQueueRequest

业务校验走 war_campaign（DB-free 可测），扣款走 player_service._charge_player
（warfunds 原子扣减 + 定价缺失免单口径），AT 行变更后向本会话已订阅的
warmapview / commandnodeview 视图增量推送。
faction id 空间 = 1/2/3 恒等（project_rules 硬约束）。
"""

from protocol import (
    build_response,
    pb_int32,
    pb_int64,
    pb_parse,
    pb_string,
    )
from state import STORE, log

from messages import _push_kvcs, _push_kvcs_delete
from wardb import COMMAND_POINT_UIDS, commandnode_templates

from player_service import (
    _charge_player,
    _charge_player_multi,
    _push_player_refresh,
    )

import mm_config
import war_campaign


def _s(v):
    """bytes/str → str。"""
    if isinstance(v, bytes):
        return v.decode('utf-8', 'replace')
    return str(v or '')


def _i(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


async def _load_at(pid, node_id, war=None):
    """取本战争存活 AT 并校验归属。返回 (at, error_code)。"""
    at = await STORE.get_command_node(_i(node_id))
    if (not at or int(at.get('state') or 0) >= 3
            or (war is not None and int(at.get('war_id') or 0) != int(war['id']))):
        return None, war_campaign.RC_COMMANDNODE_WAR_INSTANCE_NOT_FOUND
    if pid is not None and int(at.get('owner_player_id') or 0) != int(pid):
        return None, war_campaign.RC_INVALID_OWNER
    return at, 0


async def _has_command_points(sess, pid, faction):
    """玩家是否拥有指挥点角色（官服口径，对齐客户端
    HandleAssaultTeamShopCommand：commandPointsUnlocked > 0，无 isGeneral
    检查）。判定 = 玩家任一同阵营角色的 unlockBits 命中 eCommandPoints uid
    （wardb.COMMAND_POINT_UIDS）：
    - 将军角色默认序列含 uid 342/344/346（勋带 3000 的 4/8/12 级）恒有 3 点；
    - 普通角色军衔路径 uid 280-289（rank 12/15-22 每级 +1 点）。
    请求消息无 characterId 字段，服务端只能按"任一同阵营角色"判定。"""
    try:
        from player_service import _char_unlock_bits_for
        for ch in await STORE.list_characters(int(pid)):
            if int(ch.get('faction') or 0) != int(faction):
                continue
            bits = await _char_unlock_bits_for(
                sess, ch.get('template'), ch.get('faction'), ch.get('id'))
            if any(bits[uid] for uid in COMMAND_POINT_UIDS):
                return True
    except Exception as e:
        log(f'      [war_at] command points check failed: {e}')
    return False


def _view_cmd(sess):
    """本会话可用的战争视图 cmd（warmapview 优先）。"""
    return getattr(sess, 'warmap_view_cmd', None) \
        or getattr(sess, 'commandnode_view_cmd', None)


async def _push_at_rows(sess, writer, at, war_id):
    """AT 行增量推送（Definition/WarInstance/Morale/armyresource）。"""
    cmd = _view_cmd(sess)
    if not cmd:
        return
    caps = war_campaign._faction_capital_bf()
    await _push_kvcs(writer, cmd, b'CommandNodeDefinition',
                     war_campaign.msg_commandnode_definition_row(at))
    await _push_kvcs(writer, cmd, b'CommandNodeWarInstance',
                     war_campaign.msg_commandnode_instance_row(at, war_id, caps))
    await _push_kvcs(writer, cmd, b'CommandNodeMorale',
                     war_campaign.msg_commandnode_morale_row(at, war_id))
    for p in war_campaign.msg_armyresource_rows(at, war_id):
        await _push_kvcs(writer, cmd, b'armyresource', p)


async def _push_at_delete(sess, writer, at):
    """解散后删除行推送（与快照同表集合）。"""
    cmd = _view_cmd(sess)
    if not cmd:
        return
    await _push_kvcs_delete(writer, cmd, b'CommandNodeWarInstance', at['id'])
    await _push_kvcs_delete(writer, cmd, b'CommandNodeDefinition', at['id'])
    await _push_kvcs_delete(writer, cmd, b'CommandNodeMorale', at['id'])
    tpl = commandnode_templates().get(int(at.get('template_id') or 0)) or {}
    for cat in war_campaign.template_resources(tpl):
        await _push_kvcs_delete(writer, cmd, b'armyresource',
                                int(at['id']) * 1000 + int(cat))


def _currency_ok(currency):
    return _i(currency) in (1, 2, 3)


# ------------------------------------------------------------- 购买 / 升级

async def handle_create_command_node_request(sess, writer, cmd_id, req_type,
                                             proto):
    """CreateCommandNodeRequest {f1 name, f2 abbreviation,
    f3 commandnodeTemplateId, f4 pricingId, f5 discountId, f6 paymentCurrency}
    → CreateCommandNodeResponse{f1 result}。"""
    name = abbr = ''
    template_id = pricing_id = currency = 0
    try:
        f = pb_parse(proto)
        name = _s(f.get(1))
        abbr = _s(f.get(2))
        template_id = _i(f.get(3))
        pricing_id = _i(f.get(4))
        currency = _i(f.get(6))
    except Exception as e:
        log(f'      [war_at] CreateCommandNode parse error: {e}')

    pid = await sess.ensure_player()
    code = war_campaign.RC_OK
    at = None
    cost = 0
    if pid is None or STORE is None:
        code = war_campaign.RC_CHARACTER_NOT_FOUND
    else:
        war = await war_campaign.ensure_war(STORE)
        if war is None:
            code = war_campaign.RC_WAR_NOT_FOUND
        elif not _currency_ok(currency):
            code = war_campaign.RC_INVALID_CURRENCY
        else:
            faction = await STORE.get_player_faction(pid)
            has_cp = await _has_command_points(sess, pid, faction)
            code, tpl = await war_campaign.validate_create_at(
                STORE, war, pid, faction, name, abbr, template_id,
                has_command_points=has_cp)
            if not code:
                cost, ok = await _charge_player(sess, pricing_id, currency)
                if not ok:
                    code = war_campaign.RC_NOT_ENOUGH_WARFUNDS
                else:
                    try:
                        at = await STORE.create_command_node(
                            war['id'], template_id, pid, faction, name,
                            resources={str(k): v for k, v in
                                       war_campaign.template_resources(tpl).items()},
                            abbreviation=abbr,
                            rank=war_campaign.template_rank(tpl))
                    except Exception as e:
                        log(f'      [war_at] create_command_node failed: {e}')
                        code = war_campaign.RC_COMMANDNODE_TEMPLATE_NOT_FOUND
    if at is not None:
        await _push_at_rows(sess, writer, at, at['war_id'])
        await _push_player_refresh(sess, writer)
        log(f'      [war] AT 购买 player={pid} template={template_id} '
            f'name={name!r} cost={cost}')
    code = code or war_campaign.RC_OK   # 内部 0=通过 → 协议 ok=1
    resp = build_response(cmd_id, 'CreateCommandNodeResponse',
                          pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} CreateCommandNodeResponse (result={code})')


async def handle_upgrade_command_node_request(sess, writer, cmd_id, req_type,
                                              proto):
    """UpgradeCommandNodeRequest {f1 instance, f2 targetTemplate, f3 pricingId,
    f4 discountId, f5 paymentCurrency} → UpgradeCommandNodeResponse{f1 result}。"""
    node_id = target_tpl = pricing_id = currency = 0
    try:
        f = pb_parse(proto)
        node_id = _i(f.get(1))
        target_tpl = _i(f.get(2))
        pricing_id = _i(f.get(3))
        currency = _i(f.get(5))
    except Exception as e:
        log(f'      [war_at] UpgradeCommandNode parse error: {e}')

    pid = await sess.ensure_player()
    code = war_campaign.RC_OK
    at = None
    if pid is None or STORE is None:
        code = war_campaign.RC_CHARACTER_NOT_FOUND
    else:
        war = await war_campaign.ensure_war(STORE)
        if war is None:
            code = war_campaign.RC_WAR_NOT_FOUND
        elif not _currency_ok(currency):
            code = war_campaign.RC_INVALID_CURRENCY
        else:
            at, code = await _load_at(pid, node_id, war)
            if not code:
                cur = commandnode_templates().get(int(at.get('template_id') or 0))
                tgt = commandnode_templates().get(target_tpl)
                if not tgt or not war_campaign.upgrade_allowed(cur, tgt):
                    code = war_campaign.RC_COMMANDNODE_TEMPLATE_NOT_FOUND
                else:
                    cost, ok = await _charge_player(sess, pricing_id, currency)
                    if not ok:
                        code = war_campaign.RC_NOT_ENOUGH_WARFUNDS
                    else:
                        try:
                            await STORE.update_command_node(
                                at['id'], template_id=target_tpl,
                                resources={str(k): v for k, v in
                                           war_campaign.template_resources(tgt).items()})
                            at = await STORE.get_command_node(at['id'])
                        except Exception as e:
                            log(f'      [war_at] upgrade failed: {e}')
                            code = war_campaign.RC_COMMANDNODE_TEMPLATE_NOT_FOUND
    if at is not None and not code:
        await _push_at_rows(sess, writer, at, at['war_id'])
        await _push_player_refresh(sess, writer)
        log(f'      [war] AT 升级 node={node_id} -> template={target_tpl}')
    code = code or war_campaign.RC_OK   # 内部 0=通过 → 协议 ok=1
    resp = build_response(cmd_id, 'UpgradeCommandNodeResponse',
                          pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} UpgradeCommandNodeResponse (result={code})')


# ------------------------------------------------------------- 部署 / 撤退

async def handle_deploy_command_node_request(sess, writer, cmd_id, req_type,
                                              proto):
    """DeployCommandNodeRequest {f1 instance, f2 soldierPricingId,
    f3 vehiclePricingId, f4 nearCapitalBattlefieldId?, f5/f6 discounts,
    f7 paymentCurrency} → DeployCommandNodeResponse{f1 msg}。"""
    node_id = soldier_pricing = vehicle_pricing = near_capital = currency = 0
    try:
        f = pb_parse(proto)
        node_id = _i(f.get(1))
        soldier_pricing = _i(f.get(2))
        vehicle_pricing = _i(f.get(3))
        near_capital = _i(f.get(4))
        currency = _i(f.get(7))
    except Exception as e:
        log(f'      [war_at] DeployCommandNode parse error: {e}')

    pid = await sess.ensure_player()
    code = war_campaign.RC_OK
    at = None
    if pid is None or STORE is None:
        code = war_campaign.RC_CHARACTER_NOT_FOUND
    else:
        war = await war_campaign.ensure_war(STORE)
        if war is None:
            code = war_campaign.RC_WAR_NOT_FOUND
        elif not _currency_ok(currency):
            code = war_campaign.RC_INVALID_CURRENCY
        elif (mm_config.WAR_AT_REQUIRE_COMMAND_POINTS
              and not await _has_command_points(
                  sess, pid, await STORE.get_player_faction(pid))):
            # 官服口径：AT 操作（购买/部署）gate = 指挥点，与购买同源
            code = war_campaign.RC_CHARACTER_NOT_FOUND
        else:
            at, code = await _load_at(pid, node_id, war)
            if not code:
                if at.get('is_deployed'):
                    code = war_campaign.RC_COMMANDNODE_ALREADY_DEPLOYED
                else:
                    pricings = [p for p in (soldier_pricing, vehicle_pricing) if p]
                    if pricings:
                        cost, ok = await _charge_player_multi(
                            sess, pricings, currency)
                    else:
                        cost, ok = 0, True
                    if not ok:
                        code = war_campaign.RC_NOT_ENOUGH_WARFUNDS
                    else:
                        bf = war_campaign.deploy_battlefield(
                            at, at.get('faction'), near_capital)
                        try:
                            await STORE.update_command_node(
                                at['id'], is_deployed=True, map_entity_id=bf)
                            at = await STORE.get_command_node(at['id'])
                            log(f'      [war] AT 部署 node={node_id} -> bf={bf}')
                        except Exception as e:
                            log(f'      [war_at] deploy failed: {e}')
                            code = war_campaign.RC_COMMANDNODE_WAR_INSTANCE_NOT_FOUND
    if at is not None and not code:
        await _push_at_rows(sess, writer, at, at['war_id'])
        await _push_player_refresh(sess, writer)
    code = code or war_campaign.RC_OK   # 内部 0=通过 → 协议 ok=1
    resp = build_response(cmd_id, 'DeployCommandNodeResponse',
                          pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} DeployCommandNodeResponse (msg={code})')


async def handle_retreat_command_node_request(sess, writer, cmd_id, req_type,
                                              proto):
    """RetreatCommandNodeRequest {f1 instance, f2 serverInitiated?}
    → RetreatCommandNodeResponse{f1 msg}。

    M1 语义：撤回己方首都待命（is_deployed=0, map_entity_id=0）；
    移动中折返由 Phase 6 移动系统接管。"""
    node_id = 0
    try:
        f = pb_parse(proto)
        node_id = _i(f.get(1))
    except Exception as e:
        log(f'      [war_at] RetreatCommandNode parse error: {e}')

    pid = await sess.ensure_player()
    code = war_campaign.RC_OK
    at = None
    if pid is None or STORE is None:
        code = war_campaign.RC_CHARACTER_NOT_FOUND
    else:
        war = await war_campaign.ensure_war(STORE)
        if war is None:
            code = war_campaign.RC_WAR_NOT_FOUND
        else:
            at, code = await _load_at(pid, node_id, war)
            if not code:
                try:
                    await STORE.update_command_node(
                        at['id'], is_deployed=False, map_entity_id=0)
                    at = await STORE.get_command_node(at['id'])
                    log(f'      [war] AT 撤退 node={node_id} -> 待命')
                except Exception as e:
                    log(f'      [war_at] retreat failed: {e}')
                    code = war_campaign.RC_COMMANDNODE_WAR_INSTANCE_NOT_FOUND
    if at is not None and not code:
        await _push_at_rows(sess, writer, at, at['war_id'])
    code = code or war_campaign.RC_OK   # 内部 0=通过 → 协议 ok=1
    resp = build_response(cmd_id, 'RetreatCommandNodeResponse',
                          pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} RetreatCommandNodeResponse (msg={code})')


# ------------------------------------------------------------- 改名 / 解散 / 查询

async def handle_rename_commandnode_request(sess, writer, cmd_id, req_type,
                                             proto):
    """RenameCommandnodeRequest {f1 instance, f2 newName, f3 newAbbreviation,
    f4 pricingId, f5 discountId, f6 paymentCurrency}
    → RenameCommandnodeResponse{f1 result}。"""
    node_id = pricing_id = currency = 0
    new_name = new_abbr = ''
    try:
        f = pb_parse(proto)
        node_id = _i(f.get(1))
        new_name = _s(f.get(2))
        new_abbr = _s(f.get(3))
        pricing_id = _i(f.get(4))
        currency = _i(f.get(6))
    except Exception as e:
        log(f'      [war_at] RenameCommandnode parse error: {e}')

    pid = await sess.ensure_player()
    code = war_campaign.RC_OK
    at = None
    if pid is None or STORE is None:
        code = war_campaign.RC_CHARACTER_NOT_FOUND
    else:
        war = await war_campaign.ensure_war(STORE)
        if war is None:
            code = war_campaign.RC_WAR_NOT_FOUND
        elif not _currency_ok(currency):
            code = war_campaign.RC_INVALID_CURRENCY
        else:
            at, code = await _load_at(pid, node_id, war)
            if not code:
                code = war_campaign.at_name_error(new_name, new_abbr)
                if not code:
                    cost, ok = await _charge_player(sess, pricing_id, currency)
                    if not ok:
                        code = war_campaign.RC_NOT_ENOUGH_WARFUNDS
                    else:
                        try:
                            await STORE.update_command_node(
                                at['id'], name=new_name,
                                abbreviation=new_abbr)
                            at = await STORE.get_command_node(at['id'])
                            log(f'      [war] AT 改名 node={node_id} '
                                f'-> {new_name!r}/{new_abbr!r}')
                        except Exception as e:
                            log(f'      [war_at] rename failed: {e}')
                            code = war_campaign.RC_COMMANDNODE_WAR_INSTANCE_NOT_FOUND
    if at is not None and not code:
        await _push_at_rows(sess, writer, at, at['war_id'])
        await _push_player_refresh(sess, writer)
    code = code or war_campaign.RC_OK   # 内部 0=通过 → 协议 ok=1
    resp = build_response(cmd_id, 'RenameCommandnodeResponse',
                          pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} RenameCommandnodeResponse (result={code})')


async def handle_dissolve_commandnode(sess, writer, cmd_id, req_type, proto):
    """dissolve_commandnode {f1 instance} → dissolve_commandnode_response{f1 msg}。

    解散返还 = 模板定价 × MM_WAR_DISSOLVE_REFUND（warfunds 流水记账）。"""
    node_id = 0
    try:
        f = pb_parse(proto)
        node_id = _i(f.get(1))
    except Exception as e:
        log(f'      [war_at] dissolve_commandnode parse error: {e}')

    pid = await sess.ensure_player()
    code = war_campaign.RC_OK
    at = None
    if pid is None or STORE is None:
        code = war_campaign.RC_CHARACTER_NOT_FOUND
    else:
        war = await war_campaign.ensure_war(STORE)
        if war is None:
            code = war_campaign.RC_WAR_NOT_FOUND
        else:
            at, code = await _load_at(pid, node_id, war)
            if not code:
                tpl = commandnode_templates().get(
                    int(at.get('template_id') or 0)) or {}
                refund = war_campaign.dissolve_refund(tpl)
                try:
                    await STORE.delete_command_node(at['id'])
                    if refund > 0:
                        await STORE.add_war_funds(
                            int(pid), refund, reason=f'dissolve_at:{at["id"]}')
                    log(f'      [war] AT 解散 node={node_id} refund={refund}')
                except Exception as e:
                    log(f'      [war_at] dissolve failed: {e}')
                    code = war_campaign.RC_COMMANDNODE_WAR_INSTANCE_NOT_FOUND
    if at is not None and not code:
        await _push_at_delete(sess, writer, at)
        await _push_player_refresh(sess, writer)
    code = code or war_campaign.RC_OK   # 内部 0=通过 → 协议 ok=1
    resp = build_response(cmd_id, 'dissolve_commandnode_response',
                          pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} dissolve_commandnode_response (msg={code})')


async def handle_query_commandnode_owner(sess, writer, cmd_id, req_type,
                                         proto):
    """query_commandnode_owner {f1 instance}
    → query_commandnode_owner_response{f1 name}。"""
    node_id = 0
    try:
        f = pb_parse(proto)
        node_id = _i(f.get(1))
    except Exception as e:
        log(f'      [war_at] query_commandnode_owner parse error: {e}')

    name = ''
    if STORE is not None:
        try:
            at = await STORE.get_command_node(node_id)
            if at:
                name = await STORE.get_username(
                    int(at.get('owner_player_id') or 0)) or ''
        except Exception as e:
            log(f'      [war_at] owner lookup failed: {e}')
    resp = build_response(cmd_id, 'query_commandnode_owner_response',
                          pb_string(1, name))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} query_commandnode_owner_response '
        f'(name={name!r})')


# ------------------------------------------------------------- 移动（Phase 6）

async def _dest_semantic_code(store, war, at, dest_bf):
    """移动成功语义码：目的城市有进行中会战 → 23（join battle）；
    驻有敌对 AT → 22（attack supply）；否则 ok(1)。
    客户端 CommandnodeArrow 对 1/21/22/23/24 全按成功处理。"""
    try:
        for wb in await store.list_active_war_battles(war['id']):
            if int(wb.get('battlefield_id') or 0) == int(dest_bf):
                return war_campaign.RC_TRANSPORTING_TO_JOIN_BATTLE
        for o in await store.list_war_command_nodes(war['id']):
            if (o['id'] != at['id'] and o.get('is_deployed')
                    and int(o.get('faction') or 0) != int(at.get('faction') or 0)
                    and war_campaign.at_current_battlefield(o) == int(dest_bf)):
                return war_campaign.RC_TRANSPORTING_TO_ATTACK_SUPPLY
    except Exception as e:
        log(f'      [war_at] dest semantic lookup failed: {e}')
    return war_campaign.RC_OK


async def handle_transport_commandnode(sess, writer, cmd_id, req_type,
                                       proto):
    """transport_commandnode {f1 instance, f2 targetId, f3 targetType,
    f4 overrideTargetAccesspointId, f5 overrideMove, f6 path(忽略)}
    → transport_commandnode_response{f1 instance, f2 battlefieldId, f3 msg}。

    targetType（CommandnodeArrow 实锤）：battlefield（targetId=城市 id；
    拖到出兵点时 targetId=所属城市 + f4=出兵点 id）/ commandnode
    （targetId=友邻 AT warInstanceId，移动到其驻地）。
    客户端 f6 path 恒空，服务端自行 BFS 寻路。
    overrideMove=shift 拖拽/GM 瞬移：各段时长 0，发起后立即推进到到达。"""
    node_id = target_id = override_ap = 0
    target_type = ''
    override_move = False
    try:
        f = pb_parse(proto)
        node_id = _i(f.get(1))
        target_id = _i(f.get(2))
        target_type = _s(f.get(3))
        override_ap = _i(f.get(4))
        override_move = bool(f.get(5))
    except Exception as e:
        log(f'      [war_at] transport_commandnode parse error: {e}')

    pid = await sess.ensure_player()
    code = war_campaign.RC_OK
    at = None
    dest_bf = 0
    if pid is None or STORE is None:
        code = war_campaign.RC_CHARACTER_NOT_FOUND
    else:
        war = await war_campaign.ensure_war(STORE)
        if war is None:
            code = war_campaign.RC_WAR_NOT_FOUND
        else:
            at, code = await _load_at(pid, node_id, war)
            if not code:
                if not at.get('is_deployed'):
                    code = war_campaign.RC_COMMANDNODE_NOT_DEPLOYABLE
                elif int(at.get('state') or 0) == 2:
                    code = war_campaign.RC_COMMANDNODE_IN_BATTLE
                elif int(at.get('state') or 0) == 1:
                    code = war_campaign.RC_ALREADY_IN_TRANSPORT
                else:
                    # ---- 目标解析 ----
                    t = (target_type or '').strip().lower()
                    if t == 'battlefield':
                        dest_bf = target_id
                    elif t == 'commandnode':
                        other = await STORE.get_command_node(target_id)
                        dest_bf = (war_campaign.at_current_battlefield(other)
                                   if other else 0)
                        if not dest_bf:
                            code = war_campaign.RC_CANNOT_FIND_COMMANDNODE
                    else:
                        code = war_campaign.RC_FAIL
                    if not code and not dest_bf:
                        code = war_campaign.RC_FAIL
                    if not code:
                        mv, mcode = await war_campaign.start_movement(
                            STORE, at, dest_bf, override_ap=override_ap,
                            override_move=override_move)
                        if mcode:
                            code = mcode
                        elif mv is not None:
                            at = await STORE.get_command_node(at['id'])
                            segs = war_campaign.movement_segments(mv)
                            caps = war_campaign._faction_capital_bf()
                            events = []
                            if segs:
                                events.append((
                                    'set', b'transport_commandnode_segment',
                                    war_campaign.msg_transport_segment_row(
                                        at, war['id'], mv, segs[0])))
                            events.append((
                                'set', b'transport_commandnode_destination',
                                war_campaign.msg_transport_destination_row(
                                    at, war['id'], mv, segs)))
                            events.append((
                                'set', b'CommandNodeWarInstance',
                                war_campaign.msg_commandnode_instance_row(
                                    at, war['id'], caps)))
                            if override_move:
                                # 瞬移：立即推进入途到达（含会战成局桩）
                                events.extend(
                                    await war_campaign.advance_movements(
                                        STORE, war))
                            await war_campaign.broadcast_war_events(events)
                            log(f'      [war] AT 移动 node={node_id} '
                                f'-> bf={dest_bf} segs={len(segs)}')
                            code = await _dest_semantic_code(
                                STORE, war, at, dest_bf)
                        # 同地目标（mv=None）→ ok，无在途推送
    resp = build_response(
        cmd_id, 'transport_commandnode_response',
        pb_int64(1, node_id) + pb_int64(2, dest_bf) + pb_int32(3, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} transport_commandnode_response '
        f'(node={node_id} bf={dest_bf} msg={code})')


# ------------------------------------------------------------- 补给队列（M1 桩）

async def handle_leave_supply_queue_request(sess, writer, cmd_id, req_type,
                                            proto):
    """LeaveSupplyQueueRequest {f1 instance} → LeaveSupplyQueueResponse{f2 response}。

    M1 无补给队列（faction 资源生产属 M3）→ commandnode_not_in_queue(111)。"""
    resp = build_response(
        cmd_id, 'LeaveSupplyQueueResponse',
        pb_int32(2, war_campaign.RC_COMMANDNODE_NOT_IN_QUEUE))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} LeaveSupplyQueueResponse (111 not in queue)')


async def handle_reorder_supply_queue_request(sess, writer, cmd_id, req_type,
                                              proto):
    """ReorderSupplyQueueRequest {f1 instance, f2 direction}
    → ReorderSupplyQueueResponse{f2 response}。M1 桩同上。"""
    resp = build_response(
        cmd_id, 'ReorderSupplyQueueResponse',
        pb_int32(2, war_campaign.RC_COMMANDNODE_NOT_IN_QUEUE))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} ReorderSupplyQueueResponse (111 not in queue)')


HANDLERS = {
    'CreateCommandNodeRequest': handle_create_command_node_request,
    'UpgradeCommandNodeRequest': handle_upgrade_command_node_request,
    'DeployCommandNodeRequest': handle_deploy_command_node_request,
    'RetreatCommandNodeRequest': handle_retreat_command_node_request,
    'RenameCommandnodeRequest': handle_rename_commandnode_request,
    'dissolve_commandnode': handle_dissolve_commandnode,
    'query_commandnode_owner': handle_query_commandnode_owner,
    'transport_commandnode': handle_transport_commandnode,
    'LeaveSupplyQueueRequest': handle_leave_supply_queue_request,
    'ReorderSupplyQueueRequest': handle_reorder_supply_queue_request,
}
