# -*- coding: utf-8 -*-
"""AT（Assault Team）生命周期请求域（Phase 5，docs/hng-rts-protocol.md §5.2）。

处理 HnG_States 请求族：
  CreateCommandNodeRequest / UpgradeCommandNodeRequest /
  DeployCommandNodeRequest / RetreatCommandNodeRequest /
  SetCommandNodeEquipmentRequest / resupply_request /
  RenameCommandnodeRequest / dissolve_commandnode /
  query_commandnode_owner / LeaveSupplyQueueRequest / ReorderSupplyQueueRequest

业务校验走 war_campaign（DB-free 可测），扣款走 player_service._charge_player
（warfunds 原子扣减 + 定价缺失免单口径），AT 行变更后向本会话已订阅的
warmapview / commandnodeview 视图增量推送。
faction id 空间 = 1/2/3 恒等（project_rules 硬约束）。
"""

import time

from protocol import (
    build_response,
    pb_int32,
    pb_int64,
    pb_message,
    pb_parse,
    pb_parse_multi,
    pb_string,
    )
from state import STORE, log

from messages import _push_kvcs, _push_kvcs_delete
from wardb import (
    _CUR_KEY,
    _PRICING_MAP,
    COMMAND_POINT_UIDS,
    commandnode_templates,
    is_general_character,
    )

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


async def _push_at_deploy_rows(sess, writer, at, war_id):
    """部署专用行推送：CommandNodeWarInstance 行 DELETE → ADD，其余表
    UPDATE（Morale/armyresource；Definition 不推——部署不改定义数据，
    且客户端 definitionUpdated 依赖的 _warInstancesByDefinitionId 映射
    会被 DELETE 清掉，推了也会被跳过）。

    ★ 为什么部署不能像其他路径推 WarInstance UPDATE（2026-09-08 玩家
    实报"部署第二个 AT 后主城出现两个一样的图标"根因）：
    客户端 CommandnodeLayer.updatedWarInstance 的 UPDATE 分支
    handleDeployChanges（isDeployed 0→1 → addWarMapSprite）**没有
    checkLocationForStack 堆叠检查**；而 ADD 分支 addAssaultTeamToMap
    尾部有（checkLocationForStack → inMultipleMarker() 把图标收进
    MultiMarker 堆叠标记）。部署走 UPDATE 时：城上已有 AT#1（独立
    图标）→ AT#2 的 WarInstance UPDATE 先触发 AssaultTeamLocationMapping
    → MultipleMarkerLayer 创建 MultiMarker（把 AT#1 图标收进堆叠），
    随后 handleDeployChanges 把 AT#2 图标独立上图（无堆叠检查）→
    主城位置 MultiMarker + AT#2 独立图标两个图标叠加。
    DELETE+ADD 序列让部署走 ADD 分支（官服口径）：先 removeWarInstanceById
    （清理 joined schema / mapping / unit collection，无副作用——待命
    unit 本就不在地图层），再 addInstance → join（CommandNodeDefinition
    原子表行未删，直接复用）→ ADD 分支含堆叠检查，图标正确收编。
    移动到达路径无需此处理：到达 AT 的 unit 已存在，stack 创建时
    MultiMarker.setupMarker 直接对成员 inMultipleMarker()。"""
    cmd = _view_cmd(sess)
    if not cmd:
        return
    caps = war_campaign._faction_capital_bf()
    await _push_kvcs_delete(writer, cmd, b'CommandNodeWarInstance',
                            at['id'])
    await _push_kvcs(writer, cmd, b'CommandNodeWarInstance',
                     war_campaign.msg_commandnode_instance_row(
                         at, war_id, caps))
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
    if war_campaign.is_radius_unit(at):
        await _push_kvcs_delete(writer, cmd, b'air_commandnode_base',
                                at['id'])


def _currency_ok(currency):
    return _i(currency) in (1, 2, 3)


def _unit_price(pricing_id, currency_key):
    """Pricing 行单价（缺行/币种无价按 0，与 _charge_player 免单口径一致）。"""
    return int((_PRICING_MAP.get(_i(pricing_id)) or {}).get(currency_key) or 0)


def _at_missing_resources(at, tpl):
    """AT 缺编 {cat: missing}（模板编制 - 现有资源，负值截 0）。"""
    cur = war_campaign._as_resources(at.get('resources'))
    missing = {}
    for cat, full in war_campaign.template_resources(tpl).items():
        gap = int(full) - _i(cur.get(str(cat), cur.get(cat, 0)))
        if gap > 0:
            missing[int(cat)] = gap
    return missing


# --------------------------------------------------------- 装备 / 指挥点

def _template_matches_character(tpl, char_template):
    """官服 checkIfTeamAndCharacterIsMatching（AssaultTeamResources.as:519）：
    将军角色恒真 / 模板未限定兵种（f16 generalCharacterTemplateId=0）恒真 /
    否则须与角色兵种模板相等。"""
    if is_general_character(char_template):
        return True
    gct = _i((tpl or {}).get('general_character_template_id'))
    return gct == 0 or gct == _i(char_template)


async def _char_command_points(sess, ch):
    """角色指挥点上限 = Σ unlockBits[uid] × COMMAND_POINT_UIDS[uid]
    （与客户端 CharacterSchema.getUnlockedUnlocksForCharacter 同构）。"""
    from player_service import _char_unlock_bits_for
    bits = await _char_unlock_bits_for(
        sess, ch.get('template'), ch.get('faction'), ch.get('id'))
    return sum(int(val) for uid, val in COMMAND_POINT_UIDS.items()
               if uid < len(bits) and bits[uid])


async def _command_points_used(war_id, char_id, exclude_node=0):
    """角色已装备 AT 的指挥点消耗 = Σ 模板 size（与客户端
    AssaultTeamModel._characterCommandPointsUsed 同构）。"""
    used = 0
    try:
        for o in await STORE.list_war_command_nodes(war_id):
            if o['id'] == exclude_node:
                continue
            if _i(o.get('general_character_id')) != int(char_id):
                continue
            tpl = commandnode_templates().get(_i(o.get('template_id'))) or {}
            used += _i(tpl.get('size'))
    except Exception as e:
        log(f'      [war_at] command points used lookup failed: {e}')
    return used


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
                    # 官服买壳口径（§10.2 #4）：购买得空编 AT（generalCharacterId=0），
                    # 兵员经增援（resupply_request）首次免费/后续付费补齐
                    try:
                        at = await STORE.create_command_node(
                            war['id'], template_id, pid, faction, name,
                            resources={},
                            abbreviation=abbr,
                            rank=war_campaign.template_rank(tpl))
                    except Exception as e:
                        log(f'      [war_at] create_command_node failed: {e}')
                        code = war_campaign.RC_COMMANDNODE_TEMPLATE_NOT_FOUND
    code = code or war_campaign.RC_OK   # 内部 0=通过 → 协议 ok=1
    resp = build_response(cmd_id, 'CreateCommandNodeResponse',
                          pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} CreateCommandNodeResponse (result={code})')
    if at is not None:
        # 响应先于行推送（与升级/官服时序一致）：客户端先收 ack（关窗/解绑窗口
        # 监听器）再应用行变更；否则行应用链上的窗口监听器一旦抛异常，会卡死
        # 客户端请求管线（_runningRequest 永久为 true，之后任何请求都发不出）
        await _push_at_rows(sess, writer, at, at['war_id'])
        await _push_player_refresh(sess, writer)
        log(f'      [war] AT 购买 player={pid} template={template_id} '
            f'name={name!r} cost={cost}')


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
                elif int(at.get('rank') or 0) < int(tgt.get('level') or 0):
                    # 官服门槛：AT rank >= 目标模板 level（客户端
                    # AssaultTeamMultiUpgradeWindowMediator 同判定；服务端
                    # 兜底防改包 0 XP 直升）
                    code = war_campaign.RC_COMMANDNODE_TEMPLATE_NOT_FOUND
                    log(f'      [war_at] upgrade blocked: AT {at["id"]} '
                        f'rank={at.get("rank")} < target level={tgt.get("level")}')
                else:
                    cost, ok = await _charge_player(sess, pricing_id, currency)
                    if not ok:
                        code = war_campaign.RC_NOT_ENOUGH_WARFUNDS
                    else:
                        try:
                            # 买壳口径：升级只换模板不补编，缺编走 resupply
                            # 付费补齐（与 create resources={} 一致）
                            await STORE.update_command_node(
                                at['id'], template_id=target_tpl)
                            at = await STORE.get_command_node(at['id'])
                        except Exception as e:
                            log(f'      [war_at] upgrade failed: {e}')
                            code = war_campaign.RC_COMMANDNODE_TEMPLATE_NOT_FOUND
    ok = not code
    # 响应先于行推送（与买等级同理：客户端先收 ack 再按序消费行变更事件）
    code = code or war_campaign.RC_OK   # 内部 0=通过 → 协议 ok=1
    resp = build_response(cmd_id, 'UpgradeCommandNodeResponse',
                          pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} UpgradeCommandNodeResponse (result={code})')
    if at is not None and ok:
        await _push_at_rows(sess, writer, at, at['war_id'])
        await _push_player_refresh(sess, writer)
        log(f'      [war] AT 升级 node={node_id} -> template={target_tpl}')


# ------------------------------------------------------------- 部署 / 撤退

async def handle_deploy_command_node_request(sess, writer, cmd_id, req_type,
                                              proto):
    """DeployCommandNodeRequest {f1 instance, f2 soldierPricingId,
    f3 vehiclePricingId, f4 nearCapitalBattlefieldId?, f5/f6 discounts,
    f7 paymentCurrency} → DeployCommandNodeResponse{f1 msg}。

    官服口径（§10.2 #4/#5，2026-09-03 客户端实锤）：部署即补编——
    Deploy All 批量部署窗口响应后绝不发 resupply_request，缺编必须
    由部署请求内的计价 id 在服务端落地（单 AT 部署窗口的自动 resupply
    链仅在窗口开着 + UPDATE 非移动 + 缺编价 > 0 时触发，补满后价格归零
    自然关窗）。补编走 _apply_resupply：新 AT 首次免费（置位
    hasGotten*ForFree），阵营库存不足入队回 226（官服"入队成功"提示）；
    补编失败（余额不足等）不回滚部署——部署动作本身免费。"""
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
                    # 官服部署校验（§10.2 #4/#5）：主城非交战 + 周边有
                    # 非交战己方城 → 随机落位（主城∪周边）；失败 221
                    bf, dep_code = await war_campaign.resolve_deploy_battlefield(
                        STORE, war, at, near_capital)
                    if dep_code:
                        code = dep_code
                        log(f'      [war] AT 部署拒单 node={node_id} '
                            f'({dep_code})')
                    else:
                        try:
                            await STORE.update_command_node(
                                at['id'], is_deployed=True, map_entity_id=bf,
                                map_entity_type=war_campaign.
                                MAP_ENTITY_BATTLEFIELD,
                                airbase_bf=(bf
                                            if war_campaign.is_radius_unit(at)
                                            else 0))
                            at = await STORE.get_command_node(at['id'])
                            log(f'      [war] AT 部署 node={node_id} '
                                f'-> bf={bf}')
                        except Exception as e:
                            log(f'      [war_at] deploy failed: {e}')
                            code = (war_campaign.
                                    RC_COMMANDNODE_WAR_INSTANCE_NOT_FOUND)
                    if not code:
                        # 部署即补编（§10.2 #4/#5）：请求内计价 id 在此
                        # 消费——Deploy All 路径客户端绝不发 resupply
                        rcode, at = await _apply_resupply(
                            at, pid, war, currency,
                            soldier_pricing, vehicle_pricing)
                        if rcode == war_campaign.RC_COMMANDNODE_QUEUED_FOR_RESOURCES:
                            code = rcode    # 226：入队提示（官服语义）
                        elif rcode:
                            log(f'      [war_at] deploy resupply '
                                f'skipped: {rcode}')
    code = code or war_campaign.RC_OK   # 内部 0=通过 → 协议 ok=1
    resp = build_response(cmd_id, 'DeployCommandNodeResponse',
                          pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} DeployCommandNodeResponse (msg={code})')
    if at is not None and code in (
            1, war_campaign.RC_COMMANDNODE_QUEUED_FOR_RESOURCES):
        # 响应先于行推送（客户端先收 ack 关窗/解绑监听器，再应用行变更，
        # 避免行应用链上的窗口监听器抛异常卡死客户端请求管线）
        # 部署走 _push_at_deploy_rows（WarInstance DELETE+ADD，官服口径）：
        # UPDATE 推送会绕过客户端 ADD 分支的堆叠检查 → 主城多 AT 时
        # 图标不进 MultiMarker 叠放、出现两个独立图标（2026-09-08 实报）
        await _push_at_deploy_rows(sess, writer, at, at['war_id'])
        # §10.2 #7：空中半径单位部署落机场 → airbase 行（isAirborneUnit）
        cmd = _view_cmd(sess)
        if cmd and war_campaign.is_radius_unit(at):
            await _push_kvcs(writer, cmd, b'air_commandnode_base',
                             war_campaign.msg_air_commandnode_base_row(
                                 at, at['war_id'],
                                 int(at.get('airbase_bf') or 0)
                                 or war_campaign.at_current_battlefield(at)))
        await _push_player_refresh(sess, writer)


async def _friendly_airfield_bf(at, war_id):
    """落地变体当前位置是否为己方机场 → 机场 bf（0=否）。

    方案 1：伞兵落地变体（*_on_ground）撤退到己方机场时空中重整——
    转回原型（恢复运输机 cat10 编制）+ 留驻机场 + airbase_bf 指向该机场，
    可再次空降；非机场则维持撤回待命（M1 仓库重部署才恢复空降能力）。"""
    try:
        owners = dict(await STORE.get_war_cities(war_id))
        cur_bf = war_campaign.at_current_battlefield(at)
    except Exception as e:
        log(f'      [war_at] 己方机场判定失败: {e}')
        return 0
    if (cur_bf and war_campaign.is_airfield(cur_bf)
            and int(owners.get(cur_bf, 0) or 0) == int(at.get('faction') or 0)):
        return cur_bf
    return 0


async def handle_retreat_command_node_request(sess, writer, cmd_id, req_type,
                                              proto):
    """RetreatCommandNodeRequest {f1 instance, f2 serverInitiated?}
    → RetreatCommandNodeResponse{f1 msg}。

    交战撤退（state==2，war1.md §撤退）：
    - 惩罚：损 WAR_RETREAT_LOSS(25%) 编制 + 扣 20 士气，无经验无战争币
      （撤退本无发放路径）；编制扣空 = 撤退途中溃散（state=3 软删）
    - 落点：方向随机沿友方路线 → 友方邻城落位（保持部署、模板不变——
      伞兵落地变体撤到友方城仍是步兵，war2.md「空投友方区域落地变步兵」）
    - 包围：四周补给线邻城全部驻有敌军 AT（WAR_ENCIRCLE_CHECK）→
      拒绝撤退（战败时由 automission 结算全歼）
    非交战撤退维持 M1 语义：撤回待命（落地变体转回伞兵原型，
    war2.md「伞兵机飞回」）；移动中撤退中止在途行军。"""
    node_id = 0
    try:
        f = pb_parse(proto)
        node_id = _i(f.get(1))
    except Exception as e:
        log(f'      [war_at] RetreatCommandNode parse error: {e}')

    pid = await sess.ensure_player()
    code = war_campaign.RC_OK
    at = None
    wiped = False
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
                    was_in_battle = int(at.get('state') or 0) == 2
                    fields = {}
                    dest_bf = 0
                    auto_flown = 0
                    retreat_mv = None
                    if was_in_battle:
                        # ---- 交战强撤惩罚（war1.md）：损 25% 编制 +
                        #      扣 20 士气 ----
                        new_res = war_campaign._scale_resources(
                            war_campaign._as_resources(
                                at.get('resources')),
                            mm_config.WAR_RETREAT_LOSS)
                        morale = max(
                            0, min(int(at.get('max_morale') or 100),
                                   int(at.get('morale') or 0)
                                   - mm_config.
                                   WAR_MORALE_LOST_PER_BATTLE))
                        if not any(v > 0 for v in new_res.values()):
                            # 编制扣空 = 撤退途中溃散（歼灭语义 state=3）
                            wiped = True
                            fields.update(state=3, resources={},
                                          morale=morale)
                        else:
                            fields.update(resources=new_res, morale=morale)
                        # ---- 包围判定 + 友方路线落点（war1.md）----
                        try:
                            owners = dict(await STORE.get_war_cities(
                                war['id']))
                        except Exception as e:
                            log(f'      [war_at] city owners read '
                                f'failed: {e}')
                            owners = {}
                        dest_bf, encircled = (
                            await war_campaign.retreat_eval(
                                STORE, war['id'], at, owners))
                        if encircled and mm_config.WAR_ENCIRCLE_CHECK:
                            code = (war_campaign.
                                    RC_COMMANDNODE_WAR_INSTANCE_NOT_FOUND)
                            log(f'      [war] AT 撤退拒单 node={node_id} '
                                f'(encircled)')
                    if not code:
                        if wiped:
                            await STORE.update_command_node(
                                at['id'], **fields)
                            log(f'      [war] AT 撤退溃散全歼 '
                                f'node={node_id}')
                        else:
                            # ---- 在途行军中止（移动中撤退清理 movement，
                            #      否则孤儿行军到点会把已撤 AT 拉回地图）----
                            for mv in await STORE.list_active_movements(
                                    war['id']):
                                if (int(mv.get('commandnode_id') or 0)
                                        != int(at['id'])):
                                    continue
                                await STORE.finish_movement(mv['id'], 2)
                                cmd = _view_cmd(sess)
                                if cmd:
                                    segs = (war_campaign.
                                            movement_segments(mv))
                                    is_air = bool(
                                        segs and segs[0].get('air'))
                                    await _push_kvcs_delete(
                                        writer, cmd,
                                        b'transport_commandnode_segment',
                                        mv['id'])
                                    await _push_kvcs_delete(
                                        writer, cmd,
                                        b'air_transport' if is_air else
                                        b'transport_commandnode_destination',
                                        mv['id'])
                            if was_in_battle and dest_bf:
                                # 友方邻城落位：保持部署一跳转移
                                await STORE.update_command_node(
                                    at['id'], state=0,
                                    map_entity_id=dest_bf,
                                    map_entity_type=(war_campaign.
                                                     MAP_ENTITY_BATTLEFIELD),
                                    **fields)
                                log(f'      [war] AT 交战撤退 node={node_id}'
                                    f' -> 友方邻城 bf={dest_bf}')
                            else:
                                # 非交战 / 无友方路线：撤回待命（M1 行为）；
                                # 落地变体转回原型（待命=仓库，重新部署
                                # 须恢复空降能力）
                                tpl = commandnode_templates().get(
                                    int(at.get('template_id') or 0)) or {}
                                rtpl = int(tpl.get('convert_to') or 0)
                                is_ground = (
                                    str(tpl.get('name') or '').endswith(
                                        '_on_ground')
                                    and rtpl
                                    and commandnode_templates().get(rtpl))
                                home_bf = 0
                                if is_ground:
                                    home_bf = await _friendly_airfield_bf(
                                        at, war['id'])
                                if home_bf:
                                    # 己方机场空中重整（方案1）：转回空中
                                    # 原型（恢复运输机 cat10）+ 留驻机场，
                                    # airbase_bf 指向机场可再次空降
                                    await STORE.update_command_node(
                                        at['id'], state=0,
                                        map_entity_id=home_bf,
                                        map_entity_type=(
                                            war_campaign.
                                            MAP_ENTITY_BATTLEFIELD),
                                        template_id=rtpl,
                                        airbase_bf=home_bf, **fields)
                                    log(f'      [war] 落地变体在己方机场'
                                        f'空中重整 node={node_id} '
                                        f'tpl={tpl.get("id")} -> {rtpl} '
                                        f'airfield={home_bf}')
                                elif is_ground:
                                    # 非机场落地变体：若驻留城为己方且
                                    # 有己方机场 → 转回空中原型并自动空运
                                    # 回最近己方机场重整（不再进仓库）；
                                    # 敌后/中立/无机场则撤回待命（仓库
                                    # 重部署才恢复空降能力）
                                    home_owners = {}
                                    try:
                                        home_owners = dict(
                                            await STORE.get_war_cities(
                                                war['id']))
                                    except Exception:
                                        pass
                                    cur_bf = (war_campaign
                                              .at_current_battlefield(at))
                                    fac = int(at.get('faction') or 0)
                                    auto_bf = 0
                                    if (cur_bf and fac
                                            and int(home_owners.get(
                                                cur_bf, 0) or 0) == fac):
                                        auto_bf = (war_campaign.
                                                   nearest_owned_airfield(
                                                       cur_bf, home_owners,
                                                       fac))
                                    if auto_bf and auto_bf != cur_bf:
                                        # 转回空中原型（恢复运输机 cat10
                                        # + 半径）再空运回机场
                                        await STORE.update_command_node(
                                            at['id'], template_id=rtpl)
                                        at = await STORE.get_command_node(
                                            at['id'])
                                        retreat_mv, rcode, _atk = (
                                            await war_campaign.
                                            start_radius_move(
                                                STORE, war, at, auto_bf))
                                        if (rcode == war_campaign.RC_OK
                                                and retreat_mv):
                                            await STORE.update_command_node(
                                                at['id'],
                                                airbase_bf=auto_bf)
                                            auto_flown = auto_bf
                                            log(f'      [war] 落地变体自动'
                                                f'空运回己方机场 node='
                                                f'{node_id} '
                                                f'tpl={tpl.get("id")} '
                                                f'-> {rtpl} '
                                                f'airfield={auto_bf}')
                                        else:
                                            # 空运失败（无半径航路）→
                                            # 撤回待命兜底
                                            await STORE.update_command_node(
                                                at['id'], is_deployed=False,
                                                map_entity_id=0, state=0,
                                                **fields)
                                            log(f'      [war] 落地变体自动'
                                                f'空运失败转待命 node='
                                                f'{node_id}')
                                    else:
                                        # 敌后/中立/无己方机场：转回原型
                                        # + 撤回待命
                                        await STORE.update_command_node(
                                            at['id'], is_deployed=False,
                                            map_entity_id=0, state=0,
                                            template_id=rtpl, **fields)
                                        log(f'      [war] 落地变体撤退转回'
                                            f'原型 node={node_id} '
                                            f'tpl={tpl.get("id")} '
                                            f'-> {rtpl}')
                                else:
                                    await STORE.update_command_node(
                                        at['id'], is_deployed=False,
                                        map_entity_id=0, state=0, **fields)
                                    log(f'      [war] AT 撤退 node={node_id}'
                                        f' -> 待命')
                        at = await STORE.get_command_node(at['id'])
                except Exception as e:
                    log(f'      [war_at] retreat failed: {e}')
                    code = war_campaign.RC_COMMANDNODE_WAR_INSTANCE_NOT_FOUND
    code = code or war_campaign.RC_OK   # 内部 0=通过 → 协议 ok=1
    resp = build_response(cmd_id, 'RetreatCommandNodeResponse',
                          pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} RetreatCommandNodeResponse (msg={code})')
    if at is not None and code == war_campaign.RC_OK:
        if wiped:
            # 溃散全歼：删行推送（与解散同表集合）
            await _push_at_delete(sess, writer, at)
            return
        # 响应先于行推送（客户端先收 ack 再应用行变更，理由同购买/部署）
        await _push_at_rows(sess, writer, at, at['war_id'])
        cmd = _view_cmd(sess)
        if not cmd:
            return
        if auto_flown:
            # 落地变体自动空运回己方机场：推 air_transport 行（飞行动画）
            # + airbase 行指向目标机场（在途仍属 airborne，基地=目的地）
            segs = (war_campaign.movement_segments(retreat_mv)
                    if retreat_mv else [])
            if segs:
                await _push_kvcs(writer, cmd, b'air_transport',
                                 war_campaign.msg_air_transport_row(
                                     at, at['war_id'], retreat_mv, segs[0]))
            await _push_kvcs(writer, cmd, b'air_commandnode_base',
                             war_campaign.msg_air_commandnode_base_row(
                                 at, at['war_id'], auto_flown))
        elif not at.get('is_deployed'):
            # §10.2 #7：空中半径单位撤回待命 → 删 airbase 行（重新部署
            # 时再按落位机场重建）
            if war_campaign.is_radius_unit(at):
                await _push_kvcs_delete(writer, cmd, b'air_commandnode_base',
                                        at['id'])
        elif war_campaign.is_radius_unit(at):
            # 交战撤退落位友方城的空中单位：airbase 行更新落点
            await _push_kvcs(writer, cmd, b'air_commandnode_base',
                             war_campaign.msg_air_commandnode_base_row(
                                 at, at['war_id'],
                                 war_campaign.at_current_battlefield(at)))


# ------------------------------------------------------------- 增援（#2）

async def _pending_column_resources(war_id, at_id):
    """父 AT 在途补给队载量 {cat: n}（官服口径缺编计价扣除 OnTheWay：
    getNumberOfInfantryMissing = 编制 - 现有 - 在途，客户端实锤）。"""
    out = {}
    try:
        for o in await STORE.list_war_command_nodes(war_id):
            if (int(o.get('parent_command_node_id') or 0) == int(at_id)
                    and war_campaign.is_reserve(o)
                    and int(o.get('state') or 0) < 3):
                for cat, n in war_campaign._as_resources(
                        o.get('resources')).items():
                    try:
                        out[int(cat)] = out.get(int(cat), 0) + int(n)
                    except (TypeError, ValueError):
                        continue
    except Exception as e:
        log(f'      [war_at] pending column lookup failed: {e}')
    return out


async def _apply_resupply(at, pid, war, currency,
                          soldier_pricing, vehicle_pricing,
                          near_capital=0):
    """缺编补编公共链（deploy/resupply 共用，§10.2 #4/#5）：
    每侧首次免费（has_gotten_*_free 置位后转付费）→ 按模板 Pricing 计价
    扣款 → 交付：

    - AT 所在城市在己方补给网络内（归属本方且沿补给线连通本方首都；
      或 MM_WAR_SUPPLY_COLUMN_MODE=0）：阵营库存（war_faction_resources）
      即时交付 → 缺口入 war_supply_queue（supply_queue_tick 周期 FIFO
      交付）。
    - 网络外（MM_WAR_SUPPLY_COLUMN_MODE=1 自动 / 2 一律）：官服补给队
      （Reserve）机制——购买兵员装上补给队从阵营首都沿补给线机动，
      到达与父 AT 汇合入编（war_campaign.spawn_supply_column /
      _arrive_supply_column）；此路径不动阵营库存、不入 226 队列。

    在途补给队载量先从缺编中扣除（防重复购买，同客户端计价口径）。

    返回 (code, at)：0=已处理（全额即时交付/已满/补给队已派出），
    RC_COMMANDNODE_QUEUED_FOR_RESOURCES=有缺口入队（客户端特判 226），
    其他=失败码（余额不足 153；不可达拒单 7，均不扣款，AT 未变更）。
    状态校验（101/116）由调用方自理。"""
    tpl = commandnode_templates().get(_i(at.get('template_id'))) or {}
    fac = int(at.get('faction') or 0)
    missing = _at_missing_resources(at, tpl)
    pending = (await _pending_column_resources(war['id'], at['id'])
               if missing else {})
    if pending:
        missing = {c: n - pending.get(c, 0)
                   for c, n in missing.items()}
        missing = {c: n for c, n in missing.items() if n > 0}
    if not missing:
        return war_campaign.RC_NO_RESUPPLY_ALREADY_FULL, at
    veh_cats = tpl.get('vehicle_cats') or set()
    m_sold = sum(n for c, n in missing.items() if c not in veh_cats)
    m_veh = sum(n for c, n in missing.items() if c in veh_cats)
    key = _CUR_KEY.get(currency)
    s_free = (m_sold > 0 and not
              bool(at.get('has_gotten_soldier_free')))
    v_free = (m_veh > 0 and not
              bool(at.get('has_gotten_vehicle_free')))
    # ---- 补给队模式判定（官服 §10.2 #6；置于扣款前——不可达直接
    # 拒单不扣钱（RC_CANNOT_FIND_COMMANDNODE，真·孤岛理论不达））----
    use_column = False
    mode = mm_config.WAR_SUPPLY_COLUMN_MODE
    if mode and at.get('is_deployed'):
        if mode >= 2:
            use_column = True
        else:
            try:
                owners = dict(await STORE.get_war_cities(war['id']))
            except Exception as e:
                log(f'      [war_at] city owners read failed: {e}')
                owners = {}
            use_column = not war_campaign.bf_in_faction_supply(
                owners, fac,
                war_campaign.at_current_battlefield(at))
    if use_column:
        _sbf, _pbf, ok = await war_campaign.column_route(
            STORE, war, at, near_capital_bf=near_capital)
        if not ok:
            log('      [war_at] resupply rejected: column route unreachable')
            return war_campaign.RC_CANNOT_FIND_COMMANDNODE, at
    total = 0
    if m_sold and not s_free:
        total += _unit_price(soldier_pricing, key) * m_sold
    if m_veh and not v_free:
        total += _unit_price(vehicle_pricing, key) * m_veh
    if total > 0:
        ok_pay, _bal = await STORE.spend_currency(int(pid), key, total)
        if not ok_pay:
            return war_campaign.RC_NOT_ENOUGH_WARFUNDS, at
    if use_column:
        upd = {}
        if s_free:
            upd['has_gotten_soldier_free'] = True
        if v_free:
            upd['has_gotten_vehicle_free'] = True
        try:
            if upd:
                await STORE.update_command_node(at['id'], **upd)
            # 官服 #3：机械化部队补队时人员/车辆分队（各一队 Reserve
            # 分别承载，同城汇合/在途到达各自并入父 AT）
            col_ids = []
            for part, label in (
                    ({c: n for c, n in missing.items() if c not in veh_cats},
                     'soldier'),
                    ({c: n for c, n in missing.items() if c in veh_cats},
                     'vehicle')):
                if not part:
                    continue
                col, events, ecode = await war_campaign.spawn_supply_column(
                    STORE, war, at, part, near_capital_bf=near_capital)
                if col is None and ecode:
                    log(f'      [war_at] supply column ({label}) '
                        f'spawn failed: {ecode}')
                    continue
                col_ids.append(col['id'] if col else 0)
                await war_campaign.broadcast_war_events(events)
            at = await STORE.get_command_node(at['id'])
            log(f'      [war] AT 补编(补给队) node={at["id"]} '
                f'infantry={m_sold} vehicles={m_veh} '
                f'free={int(s_free) + int(v_free)} cost={total} '
                f'columns={col_ids}')
        except Exception as e:
            log(f'      [war_at] resupply column failed: {e}')
            return war_campaign.RC_COMMANDNODE_WAR_INSTANCE_NOT_FOUND, at
        return 0, at
    # ---- M3-2 库存核算：即时交付 + 缺口入队 ----
    give, queued = {}, {}
    try:
        stock = {int(r['category_id']):
                 int(r['count']) for r in
                 await STORE.list_war_faction_resources(
                     war['id'], fac)}
    except Exception as e:
        log(f'      [war_at] stock read failed: {e}')
        stock = None    # 库存未知 → 全额即时交付（保守）
    for cat, need in missing.items():
        if stock is None:
            continue    # 无库存数据 → 全部即时
        have = stock.get(cat, 0)
        if mm_config.WAR_SUPPLY_INSTANT_PARTIAL:
            g = min(need, have)
        else:
            g = need if have >= need else 0
        if g > 0:
            give[cat] = g
        if need - g > 0:
            queued[cat] = need - g
    res = {str(k): int(v) for k, v in
           war_campaign._as_resources(
               at.get('resources')).items()}
    for c, n in give.items():
        res[str(c)] = int(res.get(str(c), 0)) + n
    upd = {'resources': res}
    if s_free:
        upd['has_gotten_soldier_free'] = True
    if v_free:
        upd['has_gotten_vehicle_free'] = True
    try:
        if give:
            await STORE.adjust_war_faction_resources(
                war['id'], fac,
                {c: -n for c, n in give.items()})
        await STORE.update_command_node(at['id'], **upd)
        if queued:
            await STORE.enqueue_war_supply(
                war['id'], fac, at['id'], queued)
            # AT 级队列行重推（§10.2 #3）：AssaultTeamModel.onQueueUpdates
            # 消费 CommandNodeSupplyQueueItem；226 触发的 RESOURCE_VIEW
            # 订阅在响应后，新订阅者由 views 快照覆盖
            try:
                await war_campaign.push_supply_queue_rows(STORE, war)
            except Exception as e:
                log(f'      [war_at] queue row push failed: {e}')
        at = await STORE.get_command_node(at['id'])
        log(f'      [war] AT 补编 node={at["id"]} '
            f'infantry={m_sold} vehicles={m_veh} '
            f'free={int(s_free) + int(v_free)} '
            f'cost={total} given={give} '
            f'queued={queued}')
    except Exception as e:
        log(f'      [war_at] resupply failed: {e}')
        return war_campaign.RC_COMMANDNODE_WAR_INSTANCE_NOT_FOUND, at
    code = (war_campaign.RC_COMMANDNODE_QUEUED_FOR_RESOURCES
            if queued else 0)
    return code, at


async def handle_resupply_request(sess, writer, cmd_id, req_type, proto):
    """resupply_request {f1 instance, f2 soldierPricingId, f3 soldierDiscountId,
    f4 vehiclePricingId, f5 vehicleDiscountId, f6 nearCapitalBattlefieldId?,
    f7 path(老客户端选路，现客户端不发，忽略——服务端自动寻路), f8
    paymentCurrency} → resupply_request_response{f2 response}。

    增援链（§10.2 #2/#6）：状态校验（101/116）后走 _apply_resupply 公共
    补编链（首次免费/计价扣款/阵营库存交付/缺口入队 226；AT 在补给
    网络外时改由补给队从己方城市机动汇合）。f6 为老客户端指定的
    补给队出生城市（须己方领地，否则忽略走服务端就近选择）。客户端
    ReinforcementWindowMediator.issueSupplyRequest 发送，response=ok 关窗，
    否则错误反馈。"""
    node_id = soldier_pricing = vehicle_pricing = currency = 0
    near_capital = 0
    try:
        f = pb_parse(proto)
        node_id = _i(f.get(1))
        soldier_pricing = _i(f.get(2))
        vehicle_pricing = _i(f.get(4))
        near_capital = _i(f.get(6))
        currency = _i(f.get(8))
    except Exception as e:
        log(f'      [war_at] resupply_request parse error: {e}')

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
                st = int(at.get('state') or 0)
                if st == 2:
                    code = war_campaign.RC_NO_RESUPPLY_IN_BATTLE
                elif st == 1:
                    code = war_campaign.RC_NO_RESUPPLY_IN_TRANSPORT
                else:
                    code, at = await _apply_resupply(
                        at, pid, war, currency,
                        soldier_pricing, vehicle_pricing,
                        near_capital=near_capital)
    code = code or war_campaign.RC_OK   # _load_at 成功返回 0 → 协议 ok=1
    resp = build_response(cmd_id, 'resupply_request_response',
                          pb_int32(2, code))          # 响应 f2=response（AS3 实锤）
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} resupply_request_response (response={code})')
    if at is not None and code in (
            1, war_campaign.RC_COMMANDNODE_QUEUED_FOR_RESOURCES):
        # 响应先于行推送（与升级/购买一致）：客户端 requestSuppliesResponse
        # 收 ok 先关窗（解绑 ArmyResourceTable 等表的窗口监听器），行变更
        # 之后再应用——否则补编行在窗口开着时同步触发监听器重算，升级后
        # AT 模型中间态下任一监听器抛异常会卡死客户端请求管线
        # （_runningRequest 永久 true，之后所有请求都发不出，重登才恢复）
        await _push_at_rows(sess, writer, at, at['war_id'])


# ------------------------------------------------------------- 装备（#1）

async def handle_set_command_node_equipment_request(sess, writer, cmd_id,
                                                    req_type, proto):
    """SetCommandNodeEquipmentRequest {f1 equipmentItems REPEATED
    SetCommandNodeEquipmentItem{f1 characterId, f2 commandNodeWarInstanceId,
    f3 doEquip, f4 leaveSupplyQueue}} → SetCommandNodeEquipmentResponse{f1 msg}。

    官服 AT-角色绑定模型（§5.2）：装备 = 指挥点消耗的权威落点——
    general_character_id 0→charId，校验 模板 size ≤ commandPointsUnlocked -
    used（与客户端同构复算，超出 → equipment_not_allowed 219）；
    幂等回 217/218；客户端按 Definition 增量推送对称增减 used
    （AssaultTeamModel:447-462）。f4 leaveSupplyQueue 属阵营资源队列
    （M3）语义，M1 无队列忽略。客户端每请求恒发单条 item
    （AssaultTeamService.resetCommandNode），REPEATED 兼容批量。"""
    items = []
    try:
        f = pb_parse_multi(proto)
        for raw in f.get(1) or []:
            it = pb_parse(raw) if isinstance(raw, (bytes, bytearray)) else raw
            items.append((_i(it.get(1)), _i(it.get(2)), bool(it.get(3))))
    except Exception as e:
        log(f'      [war_at] SetCommandNodeEquipment parse error: {e}')

    pid = await sess.ensure_player()
    code = war_campaign.RC_OK
    changed = []                                   # [(at, war_id), ...]
    if pid is None or STORE is None:
        code = war_campaign.RC_CHARACTER_NOT_FOUND
    elif not items:
        code = war_campaign.RC_FAIL
    else:
        war = await war_campaign.ensure_war(STORE)
        if war is None:
            code = war_campaign.RC_WAR_NOT_FOUND
        else:
            chars = None
            for char_id, node_id, do_equip in items:
                at, ec = await _load_at(pid, node_id, war)
                if ec:
                    code = ec
                    break
                tpl = commandnode_templates().get(
                    _i(at.get('template_id'))) or {}
                cur = _i(at.get('general_character_id'))
                if do_equip:
                    if cur == int(char_id):
                        code = war_campaign.RC_COMMANDNODE_ALREADY_EQUIPPED
                        break
                    # 角色归属 + 阵营一致 + 模板-兵种匹配
                    if chars is None:
                        try:
                            chars = {int(c.get('id') or 0): c for c in
                                     await STORE.list_characters(pid)}
                        except Exception:
                            chars = {}
                    ch = chars.get(int(char_id))
                    if ch is None:
                        code = war_campaign.RC_CHARACTER_NOT_FOUND
                        break
                    if (int(ch.get('faction') or 0)
                            != int(at.get('faction') or 0)
                            or not _template_matches_character(
                                tpl, ch.get('template'))):
                        code = war_campaign.RC_EQUIPMENT_NOT_ALLOWED
                        break
                    if mm_config.WAR_AT_REQUIRE_COMMAND_POINTS:
                        unlocked = await _char_command_points(sess, ch)
                        used = await _command_points_used(
                            war['id'], char_id, exclude_node=at['id'])
                        if _i(tpl.get('size')) > unlocked - used:
                            code = war_campaign.RC_EQUIPMENT_NOT_ALLOWED
                            break
                elif not cur or cur != int(char_id):
                    code = war_campaign.RC_COMMANDNODE_ALREADY_UNEQUIPPED
                    break
                try:
                    await STORE.update_command_node(
                        at['id'],
                        general_character_id=(int(char_id) if do_equip else 0))
                    at = await STORE.get_command_node(at['id'])
                    changed.append((at, at['war_id']))
                    log(f'      [war] AT 装备 node={node_id} char={char_id} '
                        f'equip={do_equip}')
                except Exception as e:
                    log(f'      [war_at] equipment update failed: {e}')
                    code = war_campaign.RC_COMMANDNODE_WAR_INSTANCE_NOT_FOUND
                    break
    # 注意：此处无 0→1 重映射——本 handler code 恒为 RC_OK(1) 或协议错误码，
    # 空 items 的 RC_FAIL(0) 必须原样回传（重映射会翻成假成功）。
    resp = build_response(cmd_id, 'SetCommandNodeEquipmentResponse',
                          pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetCommandNodeEquipmentResponse (msg={code})')
    for at, war_id in changed:
        # 响应先于行推送（理由同购买/部署/补编）
        await _push_at_rows(sess, writer, at, war_id)


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
    code = code or war_campaign.RC_OK   # 内部 0=通过 → 协议 ok=1
    resp = build_response(cmd_id, 'RenameCommandnodeResponse',
                          pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} RenameCommandnodeResponse (result={code})')
    if at is not None and code == war_campaign.RC_OK:
        # 响应先于行推送（理由同购买/部署/补编）
        await _push_at_rows(sess, writer, at, at['war_id'])
        await _push_player_refresh(sess, writer)


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
    code = code or war_campaign.RC_OK   # 内部 0=通过 → 协议 ok=1
    resp = build_response(cmd_id, 'dissolve_commandnode_response',
                          pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} dissolve_commandnode_response (msg={code})')
    if at is not None and code == war_campaign.RC_OK:
        # 响应先于行删除推送（理由同购买/部署/补编）
        await _push_at_delete(sess, writer, at)
        await _push_player_refresh(sess, writer)


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
            if (int(wb.get('battle_type')
                    or war_campaign.BATTLE_TYPE_ASSAULT)
                    != war_campaign.BATTLE_TYPE_ASSAULT):
                continue  # 野外会战=补给线 id 空间，不算目的城交战
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
            mv_cur = None          # 在途移动（state=1 改道用）
            if not code:
                if not at.get('is_deployed'):
                    code = war_campaign.RC_COMMANDNODE_NOT_DEPLOYABLE
                elif int(at.get('state') or 0) == 2:
                    code = war_campaign.RC_COMMANDNODE_IN_BATTLE
                elif int(at.get('state') or 0) == 1:
                    # 在途改道（官服语义）：走完当前段、到达下一节点后
                    # 转向新目标；空运/半径单位单段 air 在途不可改道 → 33
                    mv_cur = await STORE.get_active_movement(at['id'])
                    segs_cur = (war_campaign.movement_segments(mv_cur)
                                if mv_cur else [])
                    if not segs_cur or segs_cur[0].get('air'):
                        code = war_campaign.RC_ALREADY_IN_TRANSPORT
                if not code:
                    # ---- 目标解析（发起移动 / 在途改道共用）----
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
                    if not code and war_campaign.is_radius_unit(at):
                        # ---- 空中半径单位（§10.2 #6/#7）：不走补给线，
                        # 按攻击/运输半径直达（CommandnodeArrow 半径规则）；
                        # 不消耗 cat10 运输机（空运回退仅地面 AT）----
                        if override_ap:
                            # 官服 detectBattleField：airborne 单位
                            # 不可落出兵点
                            code = war_campaign.RC_CANNOT_FIND_COMMANDNODE
                        else:
                            rmv, rcode, atk = (
                                await war_campaign.start_radius_move(
                                    STORE, war, at, dest_bf,
                                    override_move=override_move))
                            if rcode != war_campaign.RC_OK or rmv is None:
                                code = rcode
                            else:
                                at = await STORE.get_command_node(at['id'])
                                segs = war_campaign.movement_segments(rmv)
                                caps = war_campaign._faction_capital_bf()
                                events = [
                                    ('set', b'air_transport',
                                     war_campaign.msg_air_transport_row(
                                         at, war['id'], rmv, segs[0])),
                                    ('set', b'CommandNodeWarInstance',
                                     war_campaign.
                                     msg_commandnode_instance_row(
                                         at, war['id'], caps))]
                                for p in war_campaign.msg_armyresource_rows(
                                        at, war['id']):
                                    events.append(
                                        ('set', b'armyresource', p))
                                if override_move:
                                    # 瞬移：立即推进入途到达（airbase 行
                                    # 更新/伞兵落地变体含在内）
                                    events.extend(
                                        await war_campaign.advance_movements(
                                            STORE, war))
                                await war_campaign.broadcast_war_events(events)
                                log(f'      [war] 空中单位飞行 node={node_id}'
                                    f' -> bf={dest_bf} attack={atk}')
                                code = (
                                    war_campaign.RC_RELOCATING_TO_NEW_AIRBASE
                                    if not atk
                                    else await _dest_semantic_code(
                                        STORE, war, at, dest_bf))
                    elif not code:
                        if mv_cur is not None:
                            # ---- 在途改道：保留当前段时间轴，从转向点
                            # （当前段终点城市）拼接新路径 ----
                            mv, mcode = await war_campaign.divert_movement(
                                STORE, at, mv_cur, dest_bf,
                                override_ap=override_ap,
                                override_move=override_move)
                        else:
                            mv, mcode = await war_campaign.start_movement(
                                STORE, at, dest_bf, override_ap=override_ap,
                                override_move=override_move)
                        # 注意：start_movement/start_air_transport 返回协议
                        # 风格码（RC_OK=1 / RC_FAIL=0），真值判断会把成功
                        # （1）误当错误吞掉段行广播 → 客户端无移动动画。
                        if (mv_cur is None
                                and mcode == war_campaign.RC_CANNOT_FIND_COMMANDNODE):
                            # 地面不可达 → 空运回退（M3 §9.4）：携运输机
                            # （cat10≥1）消耗 1 架直达目标城市，响应 21
                            # （改道在途不走空运回退：转向点不可达直接回 7）
                            amv, acode = await war_campaign.start_air_transport(
                                STORE, at, dest_bf)
                            if acode != war_campaign.RC_OK or amv is None:
                                code = acode or mcode
                            else:
                                at = await STORE.get_command_node(at['id'])
                                segs = war_campaign.movement_segments(amv)
                                caps = war_campaign._faction_capital_bf()
                                events = [
                                    ('set', b'air_transport',
                                     war_campaign.msg_air_transport_row(
                                         at, war['id'], amv, segs[0])),
                                    ('set', b'CommandNodeWarInstance',
                                     war_campaign.msg_commandnode_instance_row(
                                         at, war['id'], caps))]
                                for p in war_campaign.msg_armyresource_rows(
                                        at, war['id']):
                                    events.append(('set', b'armyresource', p))
                                await war_campaign.broadcast_war_events(events)
                                log(f'      [war] AT 空运 node={node_id} '
                                    f'-> bf={dest_bf} '
                                    f'({mm_config.WAR_AIR_TRANSIT_SECONDS}s)')
                                code = (
                                    war_campaign.RC_RELOCATING_TO_NEW_AIRBASE)
                        elif mcode and mcode != war_campaign.RC_OK:
                            code = mcode
                        elif mv is not None:
                            at = await STORE.get_command_node(at['id'])
                            segs = war_campaign.movement_segments(mv)
                            caps = war_campaign._faction_capital_bf()
                            events = []
                            if segs:
                                # 改道在途：推当前段（前缀段时间轴未变）；
                                # 新发起移动：推首段
                                seg_i = (war_campaign.current_segment_index(
                                    segs, int(time.time() * 1000))
                                    if mv_cur is not None else 0)
                                events.append((
                                    'set', b'transport_commandnode_segment',
                                    war_campaign.msg_transport_segment_row(
                                        at, war['id'], mv, segs[seg_i])))
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
                            log(f'      [war] AT {"改道" if mv_cur is not None else "移动"} '
                                f'node={node_id} '
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


# ------------------------------------------------------------- AT 统计 / 买等级

async def handle_get_assault_team_stats_request(sess, writer, cmd_id, req_type,
                                                proto):
    """GetAssaultTeamStatsRequest {f1 repeated int64 commandNodeDefinitionIds}
    → GetAssaultTeamStatsResponse {f1 repeated GetAssaultTeamStatsRecord}。

    进展页 / AT 详情数据源：客户端 CommandnodeTable.setCommandNodeDefinitionStats
    缓存 record（rank/xp/nextLevelUpXp/battles/battlesWon），驱动
    CharacterCareerRequirementInfo 进度条、买等级价格 scale 与升级按钮可用性。
    id 口径 = command_nodes.id（与 CommandNodeDefinition.id 同源）。
    未知 id 跳过不回 record（客户端 stats==null 下次再拉，不报错）。"""
    node_ids = []
    try:
        f = pb_parse_multi(proto)
        node_ids = [int(v) for v in (f.get(1) or []) if int(v)]
    except Exception as e:
        log(f'      [war_at] GetAssaultTeamStats parse error: {e}')

    records = b''
    n = 0
    if STORE is not None:
        for node_id in node_ids:
            at = None
            try:
                at = await STORE.get_command_node(node_id)
            except Exception as e:
                log(f'      [war_at] get_command_node({node_id}) failed: {e}')
            if not at:
                continue
            rank = int(at.get('rank') or 0)
            records += pb_message(1, (
                pb_int32(1, war_campaign.RC_OK)             # result (ok)
                + pb_int32(2, rank)                          # rank
                + pb_int32(3, int(at.get('xp') or 0))        # xp
                + pb_int32(4, war_campaign.next_level_xp(rank))
                + pb_int32(5, int(at.get('battles') or 0))   # battles
                + pb_int32(6, int(at.get('battles_won') or 0))
                + pb_int64(7, node_id)))                     # definitionId
            n += 1
    resp = build_response(cmd_id, 'GetAssaultTeamStatsResponse', records)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} GetAssaultTeamStatsResponse '
        f'({n}/{len(node_ids)} records)')


async def handle_complete_assault_team_rank_request(sess, writer, cmd_id,
                                                    req_type, proto):
    """CompleteAssaultTeamRankRequest {f1 commandNodeDefinitionId, f2 pricingId,
    f3 storeDiscountId, f4 paymentCurrency} → CompleteAssaultTeamRankResponse
    {f1 response}。

    买等级（跳过剩余 XP）：价格按剩余比例计（客户端
    finalWarfundFractionPrice(scale)，scale=(下级阈值-已有xp)/下级阈值），
    扣款后 rank+1、xp 置新等级阈值+5（客户端段位基线，裸阈值会被
    percentageToNextATLevel 渲染成满条），先回响应再推
    CommandNodeDefinition 行——客户端响应 code=1 置 isAwaitingRankUpdate，
    由随后行内更高 rank（rankHasChanged→UPDATE_RANK）解锁按钮。
    满级回 template_not_found。"""
    node_id = pricing_id = currency = 0
    try:
        f = pb_parse(proto)
        node_id = _i(f.get(1))
        pricing_id = _i(f.get(2))
        currency = _i(f.get(4))
    except Exception as e:
        log(f'      [war_at] CompleteAssaultTeamRank parse error: {e}')

    # 余额不足码随货币（客户端 handleCompleteAssaultTeamRankResponse 按
    # not_enough_warfunds/gold/credits 分支弹购买反馈）
    _NO_FUNDS = {1: 56, 2: 153, 3: 57}    # credits/warfunds/gold

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
                rank = int(at.get('rank') or 0)
                next_xp = war_campaign.next_level_xp(rank)
                if next_xp <= 0:
                    code = war_campaign.RC_COMMANDNODE_TEMPLATE_NOT_FOUND
                    log(f'      [war_at] rank purchase blocked: AT {at["id"]} '
                        f'rank={rank} already max')
                else:
                    # 剩余 XP 比例计价（与客户端 setPrices scale 同口径；
                    # rank>0 时客户端对下限阈值 +5 显示偏移，一并跟齐）
                    prev_xp = 0 if rank == 0 else \
                        war_campaign.next_level_xp(rank - 1) + 5
                    scale = max(
                        0.0, (next_xp - (int(at.get('xp') or 0) - prev_xp))
                        / next_xp)
                    cost, ok = await _charge_player(sess, pricing_id, currency,
                                                    fraction=scale)
                    if not ok:
                        code = _NO_FUNDS.get(int(currency),
                                             war_campaign.RC_NOT_ENOUGH_WARFUNDS)
                    else:
                        try:
                            # 官服 xp 口径 = 阈值 + 5：客户端段位基线
                            # （setPrices getXpNeededForAtLevel(rank)+5；
                            # percentageToNextATLevel 以 threshold+1 分段，
                            # xp=裸阈值会渲染成上一段 100% 满条）
                            new_xp = max(int(at.get('xp') or 0), next_xp + 5)
                            await STORE.update_command_node(
                                at['id'], rank=rank + 1, xp=new_xp)
                            at = await STORE.get_command_node(at['id'])
                        except Exception as e:
                            log(f'      [war_at] rank purchase failed: {e}')
                            code = war_campaign.RC_COMMANDNODE_TEMPLATE_NOT_FOUND
    ok = not code
    # 响应必须先于行推送：客户端 handleCompleteAssaultTeamRankResponse
    # code=1 时置 isAwaitingRankUpdate=true，等随后 Definition 行的更高
    # rank 解锁（AssaultTeamObject.rank setter）；行先到会被响应重新锁死
    # → 经验条消失、买等级按钮永久禁用
    code = code or war_campaign.RC_OK
    resp = build_response(cmd_id, 'CompleteAssaultTeamRankResponse',
                          pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} CompleteAssaultTeamRankResponse '
        f'(response={code})')
    if at is not None and ok:
        await _push_at_rows(sess, writer, at, at['war_id'])
        await _push_player_refresh(sess, writer)
        log(f'      [war] AT 买等级 node={node_id} -> rank={at.get("rank")}'
            f' (cost={cost})')


HANDLERS = {
    'CreateCommandNodeRequest': handle_create_command_node_request,
    'UpgradeCommandNodeRequest': handle_upgrade_command_node_request,
    'DeployCommandNodeRequest': handle_deploy_command_node_request,
    'RetreatCommandNodeRequest': handle_retreat_command_node_request,
    'SetCommandNodeEquipmentRequest': handle_set_command_node_equipment_request,
    'resupply_request': handle_resupply_request,
    'RenameCommandnodeRequest': handle_rename_commandnode_request,
    'dissolve_commandnode': handle_dissolve_commandnode,
    'query_commandnode_owner': handle_query_commandnode_owner,
    'transport_commandnode': handle_transport_commandnode,
    'LeaveSupplyQueueRequest': handle_leave_supply_queue_request,
    'ReorderSupplyQueueRequest': handle_reorder_supply_queue_request,
    'GetAssaultTeamStatsRequest': handle_get_assault_team_stats_request,
    'CompleteAssaultTeamRankRequest': handle_complete_assault_team_rank_request,
}
