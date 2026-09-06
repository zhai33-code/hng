# -*- coding: utf-8 -*-
"""一次性预置假玩家账号+角色（直接写本地数据库，幂等可重复执行）。

用法（服务器无需运行，只需数据库在）：
  python setup_bots.py            # 建 3 个机器人：US/GE/SU 各一个步兵
  python setup_bots.py 5          # 每阵营各 5 个

之后 fake_teammate.py --username BotUS1 ... 直接登录排队，
不再需要 HTTP 注册和协议建号。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wardb
from store import PlayerStore


async def main():
    per_faction = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    st = PlayerStore()
    await st.start()
    try:
        for faction, tag in ((1, 'US'), (2, 'GE'), (3, 'SU')):
            uv = wardb.default_uniform_variant(1, faction)  # 步兵基础迷彩
            for i in range(1, per_faction + 1):
                uname = f'Bot{tag}{i}'
                pw = 'botbot123'
                pid = await st.create_player(uname, pw)
                created = pid is not None
                if not created:
                    pid = await st.get_player(uname)
                if pid is None:
                    print(f'[skip] {uname} 建号失败')
                    continue
                # 已有同阵营角色则跳过建角色
                existing = [c for c in await st.list_characters(pid)
                            if int(c['faction']) == faction]
                if existing:
                    print(f'[ok] {uname} 已存在 pid={pid} '
                          f'chars={[c["name"] for c in existing]}')
                    continue
                ch = await st.create_character(
                    pid, f'{uname}Soldier', faction, 1,
                    uniform_variant_template_id=uv)
                print(f'[{ "new" if created else "add"}] {uname} pid={pid} '
                      f'char={ch["name"]} id={ch["id"]} faction={faction}')
    finally:
        await st.close()
    print('预置完成。')


asyncio.run(main())
