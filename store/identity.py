# -*- coding: utf-8 -*-
"""玩家身份（gamertag/用户名/阵营）与战争阵营归属。
拆分自 player_store.py（纯移动重构，方法体逐字保留）。
"""
class IdentityMixin:
    """玩家身份（gamertag/用户名/阵营）与战争阵营归属。"""

    async def get_player_by_gamertag(self, gamertag):
        """按 gamertag（players.username）查玩家：{id, username} 或 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, username FROM players WHERE lower(username) = lower($1)',
                str(gamertag))
        if row is None:
            return None
        return {'id': int(row['id']), 'username': row['username']}

    async def get_username(self, player_id):
        """按 players.id 反查 username，无则返回 None。"""
        async with self.pool.acquire() as conn:
            val = await conn.fetchval(
                'SELECT username FROM players WHERE id = $1', int(player_id))
        return val

    async def get_player_faction(self, player_id):
        """玩家阵营：本场战争显式选择（player_war_factions）优先 → 名下 AT
        faction → 最新角色（AT/购买链路用；player f11 用
        get_player_war_faction 严格口径，未选择发 0）。

        战争模式阵营以 AT 阵营为准——玩家用其他阵营士兵打普通模式
        （characters 表 faction 变化）不应导致战争模式阵营漂移。
        2026-09-03 官服口径：本场战争的显式选择（player_war_factions，
        join_war_request 落库）最优先；未选择回落 AT/角色派生。"""
        pid = int(player_id)
        async with self.pool.acquire() as conn:
            war_id = await conn.fetchval(
                'SELECT id FROM wars WHERE state = 1 '
                'ORDER BY id DESC LIMIT 1')
            if war_id is not None:
                sel = await conn.fetchval(
                    'SELECT faction FROM player_war_factions '
                    'WHERE player_id = $1 AND war_id = $2', pid, int(war_id))
                if sel is not None:
                    return int(sel)
            val = await conn.fetchval(
                'SELECT faction FROM command_nodes '
                'WHERE owner_player_id = $1 AND state < 3 '
                'ORDER BY id DESC LIMIT 1', pid)
            if val is None:
                val = await conn.fetchval(
                    'SELECT faction FROM characters WHERE player_id = $1 '
                    'AND deleted = FALSE ORDER BY id DESC LIMIT 1', pid)
        return int(val or 0)

    async def get_player_war_faction(self, player_id, war_id):
        """本场战争的阵营选择（官服 join_war_request 落库行）。

        未选择返回 None（调用方据此把 player f11 发 0，客户端弹
        FactionSelectionWindow / 首页 CTA）。"""
        async with self.pool.acquire() as conn:
            val = await conn.fetchval(
                'SELECT faction FROM player_war_factions '
                'WHERE player_id = $1 AND war_id = $2',
                int(player_id), int(war_id))
        return int(val) if val is not None else None

    async def set_player_war_faction(self, player_id, war_id, faction):
        """记录/更新本场战争的阵营选择（UPSERT；同 war 重选覆盖）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO player_war_factions (player_id, war_id, faction) '
                'VALUES ($1, $2, $3) '
                'ON CONFLICT (player_id, war_id) DO UPDATE SET faction = $3',
                int(player_id), int(war_id), int(faction))
        return True

