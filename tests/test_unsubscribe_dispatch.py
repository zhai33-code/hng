# -*- coding: utf-8 -*-
"""退订路由大小写回归（2026-09-08 WarIsOver 卡死修复）。

客户端 UnSubscribeViewsCommand 换战争链串行发 6 个退订请求，第 4 步
'UnsubscribePlayerMissionViewRequest'（大写 U + Request 后缀）此前被
case-sensitive 前缀匹配漏掉 → HandlerErrorResponse → 客户端
viewUnsubscribedSignal 不触发 → 6 步退订链卡死在第 4 步 →
WarIsOver"继续下一场战争"按钮点了没反应。

handlers.dispatch 的 unsubscribe 前缀匹配已改为大小写不敏感；本测试
钉死该行为：全部 6 个退订请求名（含大写变体）都必须命中
handle_unsubscribe，不落入 HandlerErrorResponse 兜底。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import handlers
from handlers import views

RESULTS = []


def check(label, ok, detail=''):
    RESULTS.append((label, ok))
    print(f'[{"PASS" if ok else "FAIL"}] {label}{("  " + detail) if detail else ""}')


class _FakeSess:
    """dispatch 只在 username 非空时解析 pid——用 None 走最简路径。"""

    username = None


async def main():
    # 拦截 handle_unsubscribe：记录命中，不实际写响应
    hits = []

    async def fake_unsub(sess, writer, cmd_id, req_type, proto):
        hits.append(req_type)

    orig_unsub = views.handle_unsubscribe
    orig_sub = views.handle_subscribe
    views.handle_unsubscribe = fake_unsub
    sub_hits = []

    async def fake_sub(sess, writer, cmd_id, req_type, proto):
        sub_hits.append(req_type)

    views.handle_subscribe = fake_sub
    try:
        # UnSubscribeViewsCommand 6 步退订链的请求名（ServiceHandler 常量）：
        # 第 4 步 UnsubscribePlayerMissionViewRequest 是大小写回归主角
        names = [
            'unsubscribecommandnodeview',
            'unsubscribewarmapview',
            'unsubscriberesourceview',
            'UnsubscribePlayerMissionViewRequest',
            'unsubscribewarmaplightview',
            'unsubscribebattlesview',
        ]
        for i, name in enumerate(names):
            hits.clear()
            ok = await handlers.dispatch(_FakeSess(), None, 100 + i, name, b'')
            check(f'退订链第{i + 1}步 {name} 命中 handle_unsubscribe',
                  ok and hits == [name])

        # 订阅侧同款大小写（登录链 SubscribePlayerMissionViewRequest 大写 S）
        hits.clear()
        sub_hits.clear()
        ok = await handlers.dispatch(
            _FakeSess(), None, 200, 'SubscribePlayerMissionViewRequest', b'')
        check('SubscribePlayerMissionViewRequest（大写 S）命中 handle_subscribe',
              ok and sub_hits == ['SubscribePlayerMissionViewRequest']
              and not hits)

        # 非退订请求不得误命中（兜底返回 False 走 HandlerErrorResponse）
        hits.clear()
        ok = await handlers.dispatch(_FakeSess(), None, 300, 'foobar', b'')
        check('未知请求返回 False（不误吞兜底）', not ok and not hits)
    finally:
        views.handle_unsubscribe = orig_unsub
        views.handle_subscribe = orig_sub

    fails = [l for l, ok in RESULTS if not ok]
    print(f'\n{"="*50}\nunsubscribe dispatch 回归：'
          f'{len(RESULTS)-len(fails)}/{len(RESULTS)} PASS')
    if fails:
        for l in fails:
            print(f'  FAIL: {l}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
