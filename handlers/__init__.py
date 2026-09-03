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
    war_at,
    weapons,
    )
from state import CURRENT_PID

_EXACT = {}
for _m in (acks, auth, battlereport, career, characters, combat, economy, misc,
           mission, shop, social, squad, uniforms, vehicles, views, war_at,
           weapons):
    _EXACT.update(_m.HANDLERS)

_PREFIX_HANDLERS = {
    'subscribe': views.handle_subscribe,
    'unsubscribe': views.handle_unsubscribe,
}


async def dispatch(sess, writer, cmd_id, req_type, proto):
    # 绑定当前会话的数据库 player_id：messages._msg_* 构建士兵视图各表行
    # 的 playerId 字段从 contextvar 读（客户端按 currentPlayer.id 过滤）。
    # ★ 只在 username 已设置时解析：首条消息几乎总是 login2_begin，此时
    #   username 还是 None，若先调 ensure_player 会走 'demo' 兜底并把 demo
    #   的 pid 缓存进会话——登录后也永不重解析 → 整个会话错绑 demo 账号
    #   （2026-08-27 回归测试全红的根因：测试号建的角色全落到 demo 名下）。
    #   未登录连接保持默认 1。
    try:
        if sess.username:
            CURRENT_PID.set(int(await sess.ensure_player() or 1))
    except Exception:
        pass
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
