# -*- coding: utf-8 -*-
"""管理端数据库层：独立 asyncpg 连接池 + 自建表迁移。

与游戏服进程共用同一 PostgreSQL 实例与 hng 库，但持有独立连接池
（管理端重启不影响游戏服连接）。自建表：
  admin_users      管理员账号（scrypt 哈希）
  admin_audit_log  全操作审计（谁/何时/做了什么/目标/前后值/IP）
另为 players 补充 banned 列（封禁标记，游戏登录侧生效见 README 说明）。
"""
import asyncpg

from . import config

# 连接超时（秒）：连不上时快速失败并给出明确报错，避免默认 60s 静默挂起
CONNECT_TIMEOUT = 5

pool: asyncpg.Pool | None = None


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS admin_users (
    id                     BIGSERIAL PRIMARY KEY,
    username               TEXT UNIQUE NOT NULL,
    password_hash          TEXT NOT NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at          TIMESTAMPTZ,
    credentials_updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id          BIGSERIAL PRIMARY KEY,
    admin_id    BIGINT NOT NULL,
    admin_name  TEXT NOT NULL,
    action      TEXT NOT NULL,
    target      TEXT NOT NULL DEFAULT '',
    detail      JSONB NOT NULL DEFAULT '{}'::jsonb,
    ip          TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_admin_audit_created
    ON admin_audit_log (created_at DESC);

-- 封禁标记（游戏登录侧配合检查后实时生效）
ALTER TABLE players ADD COLUMN IF NOT EXISTS banned BOOL NOT NULL DEFAULT FALSE;
-- 软删除标记（游戏登录侧 get_password 排除已删除账号 → 拒登；置空即恢复）
ALTER TABLE players ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
"""


async def init() -> asyncpg.Pool:
    global pool
    try:
        pool = await asyncpg.create_pool(
            config.DATABASE_URL, min_size=1, max_size=4, timeout=CONNECT_TIMEOUT)
    except Exception as e:
        # 连不上/认证失败/库不存在：快速失败并给出可定位的报错（而非静默挂起或裸栈）
        raise RuntimeError(
            f'[db] PostgreSQL 连接失败：{type(e).__name__}: {e}\n'
            f'[db] 目标：{_safe_dsn(config.DATABASE_URL)}\n'
            '[db] 请确认：PostgreSQL 已启动 / .env 的 HNG_DATABASE_URL 正确 /'
            ' 服务器防火墙放行 5432。') from e
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
    return pool


def _safe_dsn(dsn: str) -> str:
    """DSN 打码：隐藏密码后展示 host:port/db。"""
    import re
    return re.sub(r':([^:@/]+)@', ':***@', dsn)


async def close():
    global pool
    if pool is not None:
        await pool.close()
        pool = None
