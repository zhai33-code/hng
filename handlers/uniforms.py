# -*- coding: utf-8 -*-
"""军服域（迷彩变体、头盔部件：购买/装备）。

从 realtest_server.dispatch 机械迁移，函数体保持原样；
统一签名 (sess, writer, cmd_id, req_type, proto)。
"""


import time

from protocol import (
    build_response,
    pb_int32,
    pb_parse,
    )

from state import (
    STORE,
    log,
    )

from wardb import (
    _CUR_NAME,
    _rank_from_combat_xp,
    )

from messages import (
    _msg_character,
    _msg_uniform_part,
    _msg_uniform_variant,
    _push_kvcs,
    )

from player_service import (
    _char_unlock_bits_for,
    _charge_player,
    _fail_persisted_purchase,
    _msg_player_for,
    )


async def handle_createuniformvariantrequest(sess, writer, cmd_id, req_type, proto):
    # 军服迷彩变体购买（SoldierAppearanceDepotMediator.purchaseUniformClickHandler /
    # ProgressionOverviewWindowMediator.onPurchaseUniformVariantClick）
    # CreateUniformVariantRequest { int64 characterId=1; int64 characterPackId=2;
    #   string characterName=3; int64 characterPricingId=4;
    #   int64 uniformVariantTemplateId=5; int64 variantPricingId=6;
    #   int64 discountId=7; int32 paymentCurrency=8; }
    # CreateUniformVariantResponse { int32 response = 1; }
    uv_char_id = 0
    uv_template_id = 0
    uv_pricing_id = 0
    uv_currency = 0
    try:
        f = pb_parse(proto)
        uv_char_id = f.get(1, 0)
        uv_template_id = f.get(5, 0)
        uv_pricing_id = f.get(6, 0)
        uv_currency = f.get(8, 0)
        log(f'      CreateUniformVariant: charId={uv_char_id}, '
            f'uniformVariantTemplateId={uv_template_id}, variantPricingId={uv_pricing_id}, '
            f'currency={_CUR_NAME.get(int(uv_currency), uv_currency)}')
    except Exception as e:
        log(f'      CreateUniformVariant parse error: {e}')

    # 扣款（迷彩购买费用）；余额不足返回错误响应
    cost, ok = await _charge_player(sess, uv_pricing_id, uv_currency)
    if not ok:
        resp_proto = pb_int32(1, 56)  # not_enough_credits
        resp = build_response(cmd_id, 'CreateUniformVariantResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} CreateUniformVariantResponse (response=56 not_enough) cost={cost}')
        return

    # 持久化军服迷彩变体实例；失败时退款并返回失败。
    pid = await sess.ensure_player()
    if pid is not None:
        try:
            uvr = await STORE.create_uniform_variant(pid, uv_template_id, uv_char_id)
            uvid = uvr['id']
        except Exception as e:
            log(f'[DB] create_uniform_variant failed: {e}')
            uvid = None
    else:
        uvid = None
    if uvid is None:
        await _fail_persisted_purchase(
            sess, writer, cmd_id, 'CreateUniformVariantResponse', 1,
            uv_currency, cost)
        return

    # 1. 返回 CreateUniformVariantResponse (response=1 ok)
    resp_proto = pb_int32(1, 1)
    resp = build_response(cmd_id, 'CreateUniformVariantResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} CreateUniformVariantResponse (response=1 ok, variantId={uvid})')

    # 2. 通过 SOLDIER_VIEW 推送 UniformVariant 数据（客户端 UniformVariantTable
    #    识别已购买，SoldierAppearanceDepotMediator 显示可装备迷彩）
    if sess.soldier_view_cmd is not None:
        vproto = _msg_uniform_variant(uvid, uv_template_id, uv_char_id)
        await _push_kvcs(writer, sess.soldier_view_cmd, b'UniformVariant', vproto)
        log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet (UniformVariant id={uvid}, '
            f'template={uv_template_id}, char={uv_char_id})')

    # 3. 推送 player 刷新货币显示
    if sess.playerview_cmd is not None:
        player_proto = await _msg_player_for(sess)
        await _push_kvcs(writer, sess.playerview_cmd, b'player', player_proto)



async def handle_setuniformvariantrequest(sess, writer, cmd_id, req_type, proto):
    # 装备/卸下军服迷彩（CharacterService.setUniform → CharacterSetUniformCommand）
    # SetUniformVariantRequest { int64 characterId=1; int64 variantId=2; }
    # SetUniformVariantResponse { int32 response = 2; }  ← 响应字段号是 2！
    # variantId 是 UniformVariant 实例 id（0 = 卸下，恢复默认军服）
    suv_char_id = 0
    suv_variant_id = 0
    try:
        f = pb_parse(proto)
        suv_char_id = f.get(1, 0)
        suv_variant_id = f.get(2, 0)
        log(f'      SetUniformVariant: charId={suv_char_id}, variantId={suv_variant_id}')
    except Exception as e:
        log(f'      SetUniformVariant parse error: {e}')

    # 装备校验：非卸下时 variant 实例必须属于该玩家该角色
    valid = False
    pid = await sess.ensure_player()
    if pid is not None:
        try:
            if int(suv_variant_id) == 0:
                valid = True
            else:
                for uv in await STORE.list_uniform_variants(pid):
                    if (uv['id'] == int(suv_variant_id)
                            and uv['character_id'] == int(suv_char_id)):
                        valid = True
                        break
        except Exception as e:
            log(f'[DB] uniform variant ownership check failed: {e}')
    if not valid:
        resp_proto = pb_int32(2, 0)  # fail（迷彩不存在或不属于该角色）；response 字段号是 2
        resp = build_response(cmd_id, 'SetUniformVariantResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} SetUniformVariantResponse (rejected: variant={suv_variant_id} '
            f'char={suv_char_id} not owned)')
        return

    # 持久化到角色 uniform_variant_id 列
    if pid is not None:
        try:
            await STORE.set_uniform_variant(pid, suv_char_id, suv_variant_id)
        except Exception as e:
            log(f'[DB] set_uniform_variant failed: {e}')

    # 在队内换迷彩 → 重推整队行（SquadMemberSession f8 变化，
    # 其他成员的小队头像立即刷新，否则要重登才可见）
    if pid is not None:
        try:
            squad = await STORE.get_squad_for_player(pid)
            if squad is not None:
                from handlers.squad import push_squad_rows_to_all_members
                await push_squad_rows_to_all_members(int(squad['id']))
        except Exception as e:
            log(f'[DB] squad rows refresh after uniform set failed: {e}')

    resp_proto = pb_int32(2, 1)  # response = 1 (ok)；SetUniformVariantResponse 字段号是 2
    resp = build_response(cmd_id, 'SetUniformVariantResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetUniformVariantResponse (response=1 ok, '
        f'uniformVariantId={suv_variant_id})')

    # 重推角色（uniformVariantId 字段17 更新 → 客户端 SoldierAppearanceDepot 刷新迷彩显示）
    if sess.soldier_view_cmd is not None and pid is not None:
        try:
            for ch in await STORE.list_characters(pid):
                if int(ch['id']) != int(suv_char_id):
                    continue
                bits = await _char_unlock_bits_for(sess, ch['template'], ch['faction'], ch['id'])
                await _push_kvcs(writer, sess.soldier_view_cmd, b'character',
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
                log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet (character '
                    f'{ch["name"]} re-pushed after uniform set)')
        except Exception as e:
            log(f'[DB] reload character after uniform set failed: {e}')

    # 生涯军令 35 eChangeSoldierAppearance（uniform_variant_id 状态推导）
    # + 推行实时刷新
    if pid is not None and int(suv_variant_id) != 0:
        from handlers.career import complete_order_and_push
        await complete_order_and_push(sess, writer, pid)



async def handle_purchaseuniformpartrequest(sess, writer, cmd_id, req_type, proto):
    # 头盔购买（SoldierAppearanceDepotMediator.purchaseHeadgearClickHandler）
    # PurchaseUniformPartRequest { int64 characterId=1; int32 uniformPartType=2;
    #   int64 uniformPartTemplateId=3; int64 uniformPartPricingId=4;
    #   int64 discountId=5; int32 paymentCurrency=6; }
    # PurchaseUniformPartResponse { int32 response = 1; }
    # EUniformPartType: eHelmet=1
    hp_char_id = 0
    hp_part_type = 0
    hp_template_id = 0
    hp_pricing_id = 0
    hp_currency = 0
    try:
        f = pb_parse(proto)
        hp_char_id = f.get(1, 0)
        hp_part_type = f.get(2, 0)
        hp_template_id = f.get(3, 0)
        hp_pricing_id = f.get(4, 0)
        hp_currency = f.get(6, 0)
        log(f'      PurchaseUniformPart: charId={hp_char_id}, partType={hp_part_type}, '
            f'uniformPartTemplateId={hp_template_id}, pricingId={hp_pricing_id}, '
            f'currency={_CUR_NAME.get(int(hp_currency), hp_currency)}')
    except Exception as e:
        log(f'      PurchaseUniformPart parse error: {e}')

    # 扣款（头盔价格）；余额不足返回错误响应
    cost, ok = await _charge_player(sess, hp_pricing_id, hp_currency)
    if not ok:
        resp_proto = pb_int32(1, 56)  # not_enough_credits
        resp = build_response(cmd_id, 'PurchaseUniformPartResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} PurchaseUniformPartResponse (response=56 not_enough) cost={cost}')
        return

    # 持久化头盔实例；失败时退款并返回失败。
    pid = await sess.ensure_player()
    part_id = None
    if pid is not None:
        try:
            up = await STORE.create_uniform_part(pid, hp_char_id, hp_template_id, hp_part_type)
            part_id = up['id']
        except Exception as e:
            log(f'[DB] create_uniform_part failed: {e}')
    if part_id is None:
        await _fail_persisted_purchase(
            sess, writer, cmd_id, 'PurchaseUniformPartResponse', 1,
            hp_currency, cost)
        return

    # 1. 返回 PurchaseUniformPartResponse (response=1 ok)
    resp_proto = pb_int32(1, 1)
    resp = build_response(cmd_id, 'PurchaseUniformPartResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} PurchaseUniformPartResponse (response=1 ok, partId={part_id})')

    # 2. 通过 SOLDIER_VIEW 推送 UniformPart 数据（客户端 UniformPartTable
    #    识别已拥有，onHeadgearUpdates 刷新头盔列表）
    if sess.soldier_view_cmd is not None:
        uproto = _msg_uniform_part(part_id, hp_char_id, hp_template_id, hp_part_type)
        await _push_kvcs(writer, sess.soldier_view_cmd, b'UniformPart', uproto)
        log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet (UniformPart id={part_id}, '
            f'template={hp_template_id}, type={hp_part_type}, char={hp_char_id})')

    # 3. 推送 player 刷新货币显示
    if sess.playerview_cmd is not None:
        player_proto = await _msg_player_for(sess)
        await _push_kvcs(writer, sess.playerview_cmd, b'player', player_proto)



async def handle_setuniformpartrequest(sess, writer, cmd_id, req_type, proto):
    # 头盔装备/卸下（CharacterService.setHeadgear → CharacterSetUniformCommand）
    # SetUniformPartRequest { int64 characterId=1; int64 uniformPartId=2; int32 uniformPartType=3; }
    # SetUniformPartResponse { int32 response = 2; }  ← 响应字段号是 2！
    # uniformPartId 是 UniformPart 实例 id（0 = 卸下，恢复默认头盔）
    sp_char_id = 0
    sp_part_id = 0
    sp_part_type = 0
    try:
        f = pb_parse(proto)
        sp_char_id = f.get(1, 0)
        sp_part_id = f.get(2, 0)
        sp_part_type = f.get(3, 0)
        log(f'      SetUniformPart: charId={sp_char_id}, partId={sp_part_id}, '
            f'partType={sp_part_type}')
    except Exception as e:
        log(f'      SetUniformPart parse error: {e}')

    # 装备校验：非卸下时部件实例必须属于该玩家该角色且类型匹配
    valid = False
    pid = await sess.ensure_player()
    if pid is not None:
        try:
            if int(sp_part_id) == 0:
                valid = True
            else:
                for up in await STORE.list_uniform_parts(pid):
                    if (int(up['id']) == int(sp_part_id)
                            and int(up['character_id']) == int(sp_char_id)
                            and int(up['uniform_part_type']) == int(sp_part_type)):
                        valid = True
                        break
        except Exception as e:
            log(f'[DB] uniform part ownership check failed: {e}')
    if not valid:
        resp_proto = pb_int32(2, 0)  # fail（头盔不存在或不属于该角色）；response 字段号是 2
        resp = build_response(cmd_id, 'SetUniformPartResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} SetUniformPartResponse (rejected: part={sp_part_id} '
            f'char={sp_char_id} not owned)')
        return

    # 持久化到角色 headgear_id 列（0 = 卸下）
    if pid is not None:
        try:
            await STORE.set_character_headgear(pid, sp_char_id, sp_part_id)
        except Exception as e:
            log(f'[DB] set_character_headgear failed: {e}')

    # 在队内换头盔 → 重推整队行（SquadMemberSession f19 变化，
    # 其他成员的小队头像立即刷新）
    if pid is not None:
        try:
            squad = await STORE.get_squad_for_player(pid)
            if squad is not None:
                from handlers.squad import push_squad_rows_to_all_members
                await push_squad_rows_to_all_members(int(squad['id']))
        except Exception as e:
            log(f'[DB] squad rows refresh after headgear set failed: {e}')

    resp_proto = pb_int32(2, 1)  # response = 1 (ok)；SetUniformPartResponse 字段号是 2
    resp = build_response(cmd_id, 'SetUniformPartResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetUniformPartResponse (response=1 ok, '
        f'headgearPartId={sp_part_id})')

    # 重推角色（headgearId 字段18 更新 → 客户端刷新头盔显示）
    if sess.soldier_view_cmd is not None and pid is not None:
        try:
            ch = await STORE.get_character(pid, int(sp_char_id))
            if ch is not None:
                bits = await _char_unlock_bits_for(sess, ch['template'], ch['faction'], ch['id'])
                await _push_kvcs(writer, sess.soldier_view_cmd, b'character',
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
                log(f'S->C cmd={sess.soldier_view_cmd:<4} KeyValueChangeSet (character '
                    f'{ch["name"]} re-pushed after headgear set)')
        except Exception as e:
            log(f'[DB] reload character after headgear set failed: {e}')

    # 生涯军令 35 eChangeSoldierAppearance（headgear_id 状态推导）+ 推行刷新
    if pid is not None and int(sp_part_id) != 0:
        from handlers.career import complete_order_and_push
        await complete_order_and_push(sess, writer, pid)


HANDLERS = {
    'CreateUniformVariantRequest': handle_createuniformvariantrequest,
    'SetUniformVariantRequest': handle_setuniformvariantrequest,
    'PurchaseUniformPartRequest': handle_purchaseuniformpartrequest,
    'SetUniformPartRequest': handle_setuniformpartrequest,
}
