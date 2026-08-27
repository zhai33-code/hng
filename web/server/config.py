# -*- coding: utf-8 -*-
"""管理端配置：.env 加载 / 绑定参数。

（模板数据已固化在 templates_data.py，不依赖客户端 GAME_DIR/WarDatabase。）
"""
import os
import secrets
import sys

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_WEB_DIR = os.path.dirname(_PKG_DIR)          # web/（仓库内）
_REPO_DIR = os.path.dirname(_WEB_DIR)         # 仓库根（hng-re，游戏服同仓）


def _load_dotenv(path):
    """极简 .env 解析：KEY=VALUE 逐行，# 注释跳过；已存在的环境变量不覆盖。"""
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, _, v = line.partition('=')
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass


_load_dotenv(os.path.join(_REPO_DIR, '.env'))          # 与游戏服共用同一份 .env


# ---- 数据库 ----
DATABASE_URL = os.environ.get(
    'HNG_DATABASE_URL', 'postgresql://hng:hng_password@localhost:15432/hng')

# ---- 监听（默认仅回环：公网不可达，远程走 SSH 隧道）----
ADMIN_BIND = os.environ.get('HNG_ADMIN_BIND', '127.0.0.1').strip() or '127.0.0.1'
ADMIN_PORT = int(os.environ.get('HNG_ADMIN_PORT', '8080') or '8080')

# ---- 游戏服 web_server（同机 80 端口，/online.json 内部在线统计）----
GAME_HTTP_BASE = (os.environ.get('HNG_GAME_HTTP', 'http://127.0.0.1')
                  .rstrip('/')) or 'http://127.0.0.1'

# ---- JWT 签发密钥 ----
# 优先环境变量 HNG_ADMIN_SECRET；未设置时自动生成随机密钥落 .admin_secret
# （gitignore，不入库）。删除该文件 = 应急全体登出。
_SECRET_FILE = os.path.join(_PKG_DIR, '.admin_secret')


def _load_admin_secret():
    env = os.environ.get('HNG_ADMIN_SECRET')
    if env:
        return env.strip()
    try:
        with open(_SECRET_FILE, encoding='utf-8') as f:
            s = f.read().strip()
            if s:
                return s
    except OSError:
        pass
    s = secrets.token_hex(32)
    try:
        with open(_SECRET_FILE, 'w', encoding='utf-8') as f:
            f.write(s)
    except OSError as e:
        print(f'[warn] 无法写入 .admin_secret: {e}', file=sys.stderr)
    return s


ADMIN_SECRET = _load_admin_secret()

# 前端构建产物（admin.html / assets / icons）：前端工程在 web/frontend/，
# 构建命令 pnpm build（frontend 目录），产物 frontend/dist
WEB_DIST = os.path.join(_WEB_DIR, 'frontend', 'dist')
