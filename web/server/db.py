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

-- 举报处理标记（管理端写入；游戏服只写举报本身）
ALTER TABLE player_reports ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ;
ALTER TABLE player_reports ADD COLUMN IF NOT EXISTS resolved_by TEXT NOT NULL DEFAULT '';

-- 兑换码兑换记录（游戏服 redeem_voucher 写入；管理端读取展示）
ALTER TABLE voucher_packs ADD COLUMN IF NOT EXISTS redeemed_by BIGINT NOT NULL DEFAULT 0;
ALTER TABLE voucher_packs ADD COLUMN IF NOT EXISTS redeemed_at TIMESTAMPTZ;
-- 批次备注（管理端生成时填写，便于区分用途）
ALTER TABLE voucher_packs ADD COLUMN IF NOT EXISTS note TEXT NOT NULL DEFAULT '';

-- 兑换码体系 v2（docs/voucher-redemption-design.md）：时效 + 限量/无限次 + 道具
-- player_id 是购买流程遗留，管理端生成不传 → 改可空
ALTER TABLE voucher_packs ALTER COLUMN player_id DROP NOT NULL;
ALTER TABLE voucher_packs ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
-- 1=一码一用（存量语义）；N=限量 N 次；0=无限次（每人仍限一次，靠流水 UNIQUE）
ALTER TABLE voucher_packs ADD COLUMN IF NOT EXISTS max_redemptions INT NOT NULL DEFAULT 1;
-- 兑换流水：UNIQUE(码, 玩家) = 每人限一次；status 1=pending(待选士兵) 2=granted 3=耗尽跳过
CREATE TABLE IF NOT EXISTS voucher_redemptions (
    id SERIAL PRIMARY KEY,
    voucher_pack_id INT NOT NULL REFERENCES voucher_packs(id) ON DELETE CASCADE,
    player_id BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    character_id BIGINT NOT NULL DEFAULT 0,
    status SMALLINT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    granted_at TIMESTAMPTZ,
    UNIQUE (voucher_pack_id, player_id)
);
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
