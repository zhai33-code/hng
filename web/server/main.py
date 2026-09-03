# -*- coding: utf-8 -*-
"""HnG 私服管理端入口（独立进程，与游戏服解耦，仅共享 PostgreSQL）。

启动（两种方式等价）：
  python -m server.main                    （web 目录下）
  python web/server/main.py                （任意目录，自动补包路径）

管理员账号管理（本机 CLI，不经网络）：
  python -m server.main create-admin <用户名>          创建首个管理员（生成随机密码并打印，仅当无管理员时）
  python -m server.main add-admin <用户名> <密码>       追加管理员
  python -m server.main set-password <用户名> <新密码>  改密（会使该管理员全部会话失效）
"""
import asyncio
import mimetypes
import os
import secrets
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

# Windows 注册表里没有 .webp 的 MIME 映射，StaticFiles 会按 text/plain 下发；
# 加上 nosniff 后浏览器拒绝渲染 /icons/ 下的压缩 webp → 手动注册
mimetypes.add_type('image/webp', '.webp')

# 兼容 python server/main.py 直接运行：无包上下文时把 web/ 注入 sys.path
_WEB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WEB_DIR not in sys.path:
    sys.path.insert(0, _WEB_DIR)

from server import auth, config, db
from server.routers import auth_router, mail, misc, players, reports, vouchers, war


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await db.init()          # 独立连接池 + admin 表迁移（含 players.banned 列）
    yield
    await db.close()


app = FastAPI(title='HnG 私服管理后端',
              docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)

app.include_router(auth_router.router)
app.include_router(players.router)
app.include_router(misc.router)
app.include_router(reports.router)
app.include_router(mail.router)
app.include_router(vouchers.router)
app.include_router(war.router)


@app.get('/')
async def index():
    return RedirectResponse(url='/admin.html')


@app.middleware('http')
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'same-origin'
    if request.url.path.startswith('/api'):
        response.headers['Cache-Control'] = 'no-store'
    return response


# 前端构建产物（pnpm build 后生成；缺 dist 时仅提供 API，打印警告）
if os.path.isdir(config.WEB_DIST):
    # mount '/' 放在所有路由注册之后：/api/* 优先命中路由，其余走静态
    app.mount('/', StaticFiles(directory=config.WEB_DIST, html=True), name='web')
else:
    print(f'[warn] 未找到前端构建产物 {config.WEB_DIST}，仅提供 API。'
          '请先在 web/ 目录执行 pnpm build。', file=sys.stderr)


# ------------------------------------------------------------------ CLI
async def _upsert_admin(username: str, password_hash: str):
    assert db.pool is not None
    async with db.pool.acquire() as conn:
        await conn.execute(
            'INSERT INTO admin_users (username, password_hash) '
            'VALUES ($1, $2) ON CONFLICT (username) DO UPDATE '
            'SET password_hash = $2, credentials_updated_at = now()',
            username, password_hash)


async def _cli(argv: list[str]) -> int:
    cmd = argv[0]
    if cmd == 'create-admin' and len(argv) == 2:
        username = argv[1].strip()
        pw = secrets.token_urlsafe(12)
        await db.init()
        try:
            assert db.pool is not None
            async with db.pool.acquire() as conn:
                n = await conn.fetchval('SELECT count(*) FROM admin_users')
            if n:
                print('已存在管理员账号；追加管理员请用 add-admin。')
                return 1
            await _upsert_admin(username, auth.hash_password(pw))
            print(f'管理员已创建（仅显示一次，请妥善保存）：\n'
                  f'  用户名：{username}\n  密码：{pw}')
            return 0
        finally:
            await db.close()
    if cmd == 'add-admin' and len(argv) == 3:
        await db.init()
        try:
            await _upsert_admin(argv[1].strip(), auth.hash_password(argv[2]))
            print(f'管理员已添加：{argv[1]}')
            return 0
        finally:
            await db.close()
    if cmd == 'set-password' and len(argv) == 3:
        await db.init()
        try:
            await _upsert_admin(argv[1].strip(), auth.hash_password(argv[2]))
            print(f'密码已更新：{argv[1]}（该账号全部会话已失效）')
            return 0
        finally:
            await db.close()
    print(__doc__)
    return 2


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ('create-admin', 'add-admin', 'set-password'):
        try:
            sys.exit(asyncio.run(_cli(sys.argv[1:])))
        except RuntimeError as e:
            print(e, file=sys.stderr)        # db.init 的友好报错（含 DSN 与排查提示）
            sys.exit(1)
    print(f'[admin] 监听 http://{config.ADMIN_BIND}:{config.ADMIN_PORT} '
          f'（dist: {os.path.isdir(config.WEB_DIST)}）')
    # log_level=info：启动完成后 uvicorn 会打印 "Uvicorn running on ..."，
    # 避免服务已就绪但无任何输出、被误认为卡住
    uvicorn.run(app, host=config.ADMIN_BIND, port=config.ADMIN_PORT, log_level='info')


if __name__ == '__main__':
    main()
