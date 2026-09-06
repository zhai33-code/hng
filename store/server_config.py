# -*- coding: utf-8 -*-
"""动态配置（server_config 表）：web 管理端可写、游戏服运行时读。

key 为配置项标识（如 'weather'），value 为 JSONB。优先级：有行 >
代码内置默认（无行时环境变量兜底照旧生效）。成局等低频路径直接
读库（保存即生效）；未来热路径项迁入时再在 get_config 内部加 TTL
缓存 + pg_notify 失效，接口不变。
"""
import json


class ServerConfigMixin:
    """动态配置：server_config 表读写。"""

    async def get_config(self, key):
        """读一项动态配置，返回 dict；无行返回 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT value FROM server_config WHERE key = $1', str(key))
        # asyncpg 未注册 codec 时 JSONB 列返回 str（war.py 同款坑）
        return json.loads(row['value']) if row else None

    async def set_config(self, key, value, updated_by=''):
        """写一项动态配置（UPSERT），返回写入后的完整值。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO server_config (key, value, updated_by) '
                'VALUES ($1, $2::jsonb, $3) '
                'ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, '
                'updated_at = now(), updated_by = EXCLUDED.updated_by '
                'RETURNING value',
                str(key),
                json.dumps(value, ensure_ascii=False, separators=(',', ':')),
                str(updated_by or ''))
        return json.loads(row['value'])
