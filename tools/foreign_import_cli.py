# -*- coding: utf-8 -*-
"""外服账号迁移 CLI：命令行跑完整导入流程（等价 web /api/import 后端逻辑）。

用法（server 目录下）：
  # 把本地服当"外服"测试（用已有账号做源）
  $env:HNG_PW = "源账号密码"
  python tools/foreign_import_cli.py --host 127.0.0.1 --port 15110 `
      --foreign-user [admin]DoubleBird --password-env HNG_PW `
      --local-user TestMigrated --local-pass 123456

  # 连真实外服
  python tools/foreign_import_cli.py --host 51.68.235.145 --port 15120 `
      --foreign-user MyName --password-env HNG_PW `
      --local-user MyName --local-pass 123456

选项：
  --duration 采集窗口秒数（默认 15）
  --db PostgreSQL DSN（默认走 HNG_DATABASE_URL / 本地默认）
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import foreign_import
from store import PlayerStore


def main():
    ap = argparse.ArgumentParser(description='外服账号迁移导入 CLI')
    ap.add_argument('--host', required=True)
    ap.add_argument('--port', type=int, required=True)
    ap.add_argument('--foreign-user', required=True, help='外服账号')
    ap.add_argument('--password-env', help='外服密码所在环境变量名')
    ap.add_argument('--foreign-pass', help='外服密码明文（不建议，用 --password-env）')
    ap.add_argument('--local-user', required=True, help='新建本地账号用户名')
    ap.add_argument('--local-pass', required=True, help='新建本地账号密码')
    ap.add_argument('--duration', type=int, default=15, help='采集窗口秒数')
    ap.add_argument('--db', help='PostgreSQL DSN（默认 HNG_DATABASE_URL/本地）')
    args = ap.parse_args()

    fpass = args.foreign_pass or (
        os.environ.get(args.password_env) if args.password_env else None)
    if not fpass:
        print('错误：外服密码未提供（--password-env 或 --foreign-pass）')
        sys.exit(2)

    async def run():
        store = PlayerStore(args.db)
        await store.start()
        try:
            summary = await foreign_import.run_import(
                store, args.host, args.port, args.foreign_user, fpass,
                args.local_user, args.local_pass,
                collect_duration=args.duration, log=print)
            print('\n=== 导入成功 ===')
            print('本地账号: %s (id=%s)' % (summary['username'],
                                            summary['local_player_id']))
            print('外服账号: %s (id=%s)' % (summary['foreign_gamertag'],
                                            summary['foreign_player_id']))
            print('货币: %s' % summary['currencies'])
            for k, v in sorted(summary['counts'].items()):
                print('  %-18s %d' % (k, v))
            print('dump: %s' % summary['dump_dir'])
        finally:
            await store.close()

    try:
        asyncio.run(run())
    except foreign_import.AlreadyImportedError as e:
        print('\n[拒绝] %s' % e)
        sys.exit(3)
    except foreign_import.ForeignLoginError as e:
        print('\n[外服登录失败] %s (code=%d)' % (e, e.code))
        sys.exit(4)
    except foreign_import.UsernameTakenError as e:
        print('\n[拒绝] %s' % e)
        sys.exit(5)
    except foreign_import.ForeignCollectError as e:
        print('\n[采集失败] %s' % e)
        sys.exit(6)


if __name__ == '__main__':
    main()
