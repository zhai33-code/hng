# -*- coding: utf-8 -*-
"""玩家账号 / 密码 / 封禁 / 教学 / 等级授予 / 最新角色。
拆分自 player_store.py（纯移动重构，方法体逐字保留）。
"""
class PlayersMixin:
    """玩家账号 / 密码 / 教学 / 等级授予 / 最新角色。"""

    # ---- players ----

    async def get_player(self, username):
        """按用户名查 player_id；账号不存在返回 None（不自动建号）。"""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                'SELECT id FROM players WHERE username = $1', username)

    async def create_player(self, username, password):
        """手动建号（add_user.py 使用）。已存在返回 None，否则返回 player_id。"""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                'INSERT INTO players (username, password) VALUES ($1, $2) '
                'ON CONFLICT (username) DO NOTHING RETURNING id',
                username, password)

    async def get_password(self, username):
        """返回账号密码；账号不存在或已软删除（管理后台 deleted_at）返回 None。"""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                'SELECT password FROM players '
                'WHERE username = $1 AND deleted_at IS NULL', username)

    async def get_ban_info(self, username):
        """登录封禁检查，返回 (active, ban_expires, ban_reason)。

        active=False 表示未封禁；限时封禁到期时惰性解封（清 banned/
        ban_expires/ban_reason 三列，管理端列表同步恢复"正常"）。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT banned, ban_expires, ban_reason, "
                "(banned AND (ban_expires IS NULL OR ban_expires > now())) "
                "  AS active, "
                "(banned AND ban_expires IS NOT NULL AND ban_expires <= now()) "
                "  AS expired "
                "FROM players WHERE username = $1 AND deleted_at IS NULL",
                username)
        if row is None:
            return False, None, ''
        if row['expired']:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "UPDATE players SET banned = false, ban_expires = NULL, "
                    "ban_reason = '' WHERE username = $1 AND banned", username)
            return False, None, ''
        return bool(row['active']), \
            row['ban_expires'] if row['active'] else None, \
            (row['ban_reason'] or '') if row['active'] else ''

    async def get_machine_ban(self, machine_id):
        """机器码封禁原因；未登记返回 None（空机器码直接放行）。"""
        if not machine_id:
            return None
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                'SELECT reason FROM machine_bans WHERE machine_id = $1',
                machine_id)

    async def get_extra_unlock_uids(self, player_id, character_id):
        """返回该角色勋带购买的额外解锁 uid 集合（character_extra_unlocks 行表）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT u.unlock_uid FROM character_extra_unlocks u '
                'JOIN characters c ON c.id = u.character_id '
                'WHERE c.id = $1 AND c.player_id = $2',
                character_id, player_id)
        return {int(r['unlock_uid']) for r in rows}

    async def add_extra_unlock_uids(self, player_id, character_id, uids):
        """追加角色勋带购买的额外解锁 uid（progressionUnlock.id），幂等。"""
        if not uids:
            return
        async with self.pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO character_extra_unlocks (character_id, unlock_uid) '
                'SELECT c.id, x FROM characters c, unnest($3::bigint[]) AS x '
                'WHERE c.id = $1 AND c.player_id = $2 '
                'ON CONFLICT DO NOTHING',
                character_id, player_id, [int(u) for u in uids])

    async def get_player_unlock_ids(self, player_id):
        """返回玩家永久解锁 uid 集合（player_unlocks 行表，player f37 位图源）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT unlock_id FROM player_unlocks WHERE player_id = $1',
                player_id)
        return {int(r['unlock_id']) for r in rows}

    async def add_player_unlocks(self, player_id, uids):
        """追加玩家永久解锁 uid（progressionUnlock.id，通行证奖励等非等级
        来源），幂等。"""
        if not uids:
            return
        async with self.pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO player_unlocks (player_id, unlock_id) '
                'SELECT $1, x FROM unnest($2::bigint[]) AS x '
                'ON CONFLICT DO NOTHING',
                int(player_id), [int(u) for u in uids])

    async def get_combat_xp(self, player_id, character_id):
        """返回角色战斗经验（characters.combat_xp），未找到返回 0。"""
        async with self.pool.acquire() as conn:
            val = await conn.fetchval(
                'SELECT combat_xp FROM characters '
                'WHERE id = $1 AND player_id = $2', character_id, player_id)
        return int(val or 0)

    async def add_combat_xp(self, player_id, character_id, amount):
        """给角色增加战斗经验并返回新值。"""
        async with self.pool.acquire() as conn:
            return int(await conn.fetchval(
                'UPDATE characters SET combat_xp = combat_xp + $1 '
                'WHERE id = $2 AND player_id = $3 RETURNING combat_xp',
                int(amount), character_id, player_id))

    async def set_password(self, username, password):
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE players SET password = $1 WHERE username = $2',
                password, username)

    async def get_tutorials_completed(self, player_id):
        """返回教程完成态（0=未完成 1=已完成；player KVCS f32）。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT tutorials_completed FROM players WHERE id = $1', player_id)
        return int(row['tutorials_completed']) if row else 0

    async def set_tutorials_completed(self, player_id, value):
        """持久化教程完成态（SetTutorialCompletedRequest）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE players SET tutorials_completed = $1 WHERE id = $2',
                int(value), int(player_id))

    async def get_level_granted(self, player_id):
        """玩家等级奖励已发到的等级（进展 tab PlayerLevelUnlock 奖励防重发）。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT level_granted FROM players WHERE id = $1', player_id)
        return int(row['level_granted']) if row else 1

    async def set_level_granted(self, player_id, level):
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE players SET level_granted = $1 WHERE id = $2',
                int(level), int(player_id))


    async def get_latest_character(self, player_id):
        """玩家最新角色或 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, name, faction, template, combat_xp FROM characters '
                'WHERE player_id = $1 ORDER BY id DESC LIMIT 1', int(player_id))
        return dict(row) if row else None

