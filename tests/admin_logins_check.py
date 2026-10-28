# -*- coding: utf-8 -*-
"""管理端登录记录 API 数据层验证：直接调 logins_svc / players_svc
（绕过 HTTP/登录），核对 player_detail 的 login_events/linked_accounts
与全局反查 search_login_events 的返回结构。
手动运行：python admin_logins_check.py（web 目录下）
"""
import asyncio
import os
import sys

_WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'server', 'web')
sys.path.insert(0, _WEB)

from server import db
from server.services import logins_svc, players_svc


async def main():
    await db.init()
    try:
        assert db.pool is not None
        # 1. 玩家详情（demo=1000126，有真实测试登录记录）
        d = await players_svc.player_detail(db.pool, 1000126)
        ev = d['login_events']
        print(f"[1] demo login_events: {len(ev)} 条")
        if ev:
            print('    最新:', {k: ev[0][k] for k in
                  ('username', 'ip', 'machine_id', 'result', 'source')})
        print(f"[2] linked_accounts: {d['linked_accounts']}")

        # 2. 全局反查（空 q = 最近）
        s = await logins_svc.search_login_events(db.pool, '', 10)
        print(f"[3] 反查(空): {len(s['rows'])} 行, machines={len(s['machines'])}, ips={len(s['ips'])}")

        # 3. 按 IP 反查
        s2 = await logins_svc.search_login_events(db.pool, '127.0.0.1', 10)
        print(f"[4] 反查(ip=127.0.0.1): {len(s2['rows'])} 行")
        # 4. 按用户名反查
        s3 = await logins_svc.search_login_events(db.pool, 'demo', 10)
        print(f"[5] 反查(username=demo): {len(s3['rows'])} 行")
        ok = (len(ev) > 0 and isinstance(d['linked_accounts']['same_machine'], list)
              and len(s2['rows']) > 0 and len(s3['rows']) > 0)
        print('[6] 数据层验证: ' + ('PASS' if ok else 'FAIL'))
        sys.exit(0 if ok else 1)
    finally:
        await db.close()


if __name__ == '__main__':
    asyncio.run(main())
