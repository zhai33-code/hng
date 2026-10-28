# -*- coding: utf-8 -*-
"""外服账号迁移登记（foreign_imports 表：一个外服账号只许导入一次）。"""


class ForeignImportsMixin:
    """外服账号迁移登记。"""

    async def get_foreign_import(self, foreign_server, foreign_player_id):
        """查询某外服账号是否已导入（含本地账号 id），未导入返回 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, foreign_server, foreign_player_id, foreign_username, '
                'local_player_id, dump_path, characters, weapons, vehicles, created_at '
                'FROM foreign_imports '
                'WHERE foreign_server = $1 AND foreign_player_id = $2',
                foreign_server, int(foreign_player_id))
        return dict(row) if row else None

    async def get_foreign_import_by_local(self, local_player_id):
        """查本地账号来自哪次外服导入（管理端审计用）。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, foreign_server, foreign_player_id, foreign_username, '
                'local_player_id, dump_path, characters, weapons, vehicles, created_at '
                'FROM foreign_imports WHERE local_player_id = $1',
                int(local_player_id))
        return dict(row) if row else None

    async def record_foreign_import(self, conn, foreign_server, foreign_player_id,
                                    foreign_username, local_player_id, dump_path='',
                                    characters=0, weapons=0, vehicles=0):
        """登记一次导入（在导入事务内调用，随事务提交/回滚）。

        UNIQUE(foreign_server, foreign_player_id) 冲突时抛
        asyncpg.UniqueViolationError——并发重复导入的兜底。"""
        return await conn.fetchval(
            'INSERT INTO foreign_imports '
            '(foreign_server, foreign_player_id, foreign_username, local_player_id, '
            ' dump_path, characters, weapons, vehicles) '
            'VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id',
            foreign_server, int(foreign_player_id), foreign_username,
            int(local_player_id), dump_path, int(characters), int(weapons),
            int(vehicles))
