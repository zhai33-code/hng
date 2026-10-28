# -*- coding: utf-8 -*-
"""管理端列表页登录功能验证：list_players 最近登录摘要（LATERAL）、
player_logins 展开行懒加载、player_detail 重构后无回归。
手动运行（需 HNG_DATABASE_URL 指本地库）：
  python admin_list_expand_check.py
"""
import asyncio
import os
import sys

_WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'server', 'web')
sys.path.insert(0, _WEB)

from server import db
from server.services import players_svc


async def main():
    await db.init()
    try:
        assert db.pool is not None
        # 1. 列表：最近登录摘要
        r = await players_svc.list_players(
            db.pool, page=1, page_size=5, q='', order='created_desc',
            state='all')
        print(f"[1] 列表 {r['total']} 玩家，首页 {len(r['items'])} 行")
        for it in r['items'][:3]:
            print(f"    {it['username']:<15} last_login_at={it['last_login_at']} ip={it['last_login_ip']}")
        has_field = all('last_login_at' in it and 'last_login_ip' in it
                        for it in r['items'])
        # 2. 排序/筛选不回归（q + state）
        r2 = await players_svc.list_players(
            db.pool, page=1, page_size=5, q='demo', order='name_asc',
            state='active')
        print(f"[2] 搜索 demo：{len(r2['items'])} 行（{r2['total']} 命中）")
        # 3. 展开行懒加载
        demo_id = r2['items'][0]['id'] if r2['items'] else 1000126
        lp = await players_svc.player_logins(db.pool, demo_id)
        print(f"[3] player_logins(demo id={demo_id}): {len(lp['login_events'])} 条, "
              f"linked={lp['linked_accounts']}")
        # 4. 详情页无回归（重构后共用 _login_profile）
        d = await players_svc.player_detail(db.pool, demo_id)
        same = (d['login_events'] == lp['login_events']
                and d['linked_accounts'] == lp['linked_accounts'])
        print(f"[4] player_detail 与 player_logins 一致: {'PASS' if same else 'FAIL'}")
        # 5. 不存在的玩家
        try:
            await players_svc.player_logins(db.pool, 999999999)
            print('[5] 不存在玩家未报错: FAIL')
            ok = False
        except LookupError:
            print('[5] 不存在玩家 LookupError: PASS')
            ok = has_field and same and len(r2['items']) > 0
        print('[6] 总体: ' + ('PASS' if ok else 'FAIL'))
        sys.exit(0 if ok else 1)
    finally:
        await db.close()


if __name__ == '__main__':
    asyncio.run(main())
