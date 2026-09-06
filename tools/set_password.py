"""修改游戏账号密码: python set_password.py <username> <new_password>"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 2026-08-21 归档至 tools/
from store import PlayerStore


async def main():
    if len(sys.argv) != 3:
        print('usage: python set_password.py <username> <new_password>')
        return
    username, password = sys.argv[1], sys.argv[2]
    s = PlayerStore()
    await s.start()
    try:
        pid = await s.get_player(username)
        if pid is None:
            print(f'[-] user {username!r} not found (use add_user.py to create)')
        else:
            await s.set_password(username, password)
            print(f'[+] password updated for {username!r}')
    except Exception as e:
        print(f'[-] failed: {e}')
    await s.close()


asyncio.run(main())
