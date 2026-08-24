# -*- coding: utf-8 -*-
"""请求处理包：按域拆分原 realtest_server.dispatch 的 if/elif 链。

dispatch() 命中已注册请求返回 True；未命中返回 False，由入口继续走
matchmaking / game_server 委托与 HandlerErrorResponse 兜底。
匹配语义与原链一致：先精确（含 in 集合），再前缀（subscribe*/unsubscribe*）。
"""
from . import (
    acks,
    auth,
    battlereport,
    career,
    characters,
    combat,
    economy,
    misc,
    mission,
    shop,
    social,
    squad,
    uniforms,
    vehicles,
    views,
    weapons,
    )

_EXACT = {}
for _m in (acks, auth, battlereport, career, characters, combat, economy, misc,
           mission, shop, social, squad, uniforms, vehicles, views, weapons):
    _EXACT.update(_m.HANDLERS)

_PREFIX_HANDLERS = {
    'subscribe': views.handle_subscribe,
    'unsubscribe': views.handle_unsubscribe,
}


async def dispatch(sess, writer, cmd_id, req_type, proto):
    h = _EXACT.get(req_type)
    if h is not None:
        await h(sess, writer, cmd_id, req_type, proto)
        return True
    if req_type.lower().startswith('subscribe'):
        await views.handle_subscribe(sess, writer, cmd_id, req_type, proto)
        return True
    if req_type.startswith('unsubscribe'):
        await views.handle_unsubscribe(sess, writer, cmd_id, req_type, proto)
        return True
    return False
