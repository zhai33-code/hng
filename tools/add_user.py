"""手动添加游戏账号: python add_user.py <username> <password>

账号必须先在数据库中存在才能登录游戏（服务端不自动建号）。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 2026-08-21 归档至 tools/
from store import PlayerStore


async def main():
    if len(sys.argv) != 3:
        print('usage: python add_user.py <username> <password>')
        return
    username, password = sys.argv[1], sys.argv[2]
    s = PlayerStore()
    await s.start()
    try:
        pid = await s.create_player(username, password)
        if pid is None:
            print(f'[-] user {username!r} already exists (use set_password.py to change)')
        else:
            print(f'[+] user {username!r} created (id={pid}, password={password!r})')
    except Exception as e:
        print(f'[-] failed: {e}')
    await s.close()


asyncio.run(main())
