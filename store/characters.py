# -*- coding: utf-8 -*-
"""角色 CRUD / 每日奖励 / 勋带经验 / 战斗徽章 / 军服变体。
拆分自 player_store.py（纯移动重构，方法体逐字保留）。
"""
from .base import _DAILY_BONUS_AVAILABLE_SQL

class CharactersMixin:
    """角色 CRUD / 每日奖励 / 勋带经验 / 战斗徽章 / 军服变体。"""

    # ---- characters ----

    async def list_characters(self, player_id):
        """返回 [{id, name, faction, template, combat_xp, combat_badge_0, combat_badge_1,
        uniform_variant_id, headgear_id, squad_points, receive_daily_bonus,
        last_daily_battle_at, is_on_leave, previous_template, head_id}]，按创建
        顺序；软删角色（is_deleted）不返回。军衔等级由 combat_xp 经
        ExperienceLevels 阈值推导（单源）。

        last_daily_battle_at 原值随行返回：KVCS DailyBonus 行 lastReceived
        的数据源（客户端 DailyBonusModel 按其 + 05:00 UTC 窗口自行判定）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, name, faction, template, combat_xp, combat_badge_0, combat_badge_1, '
                'uniform_variant_id, headgear_id, squad_points, '
                f'{_DAILY_BONUS_AVAILABLE_SQL} AS receive_daily_bonus, '
                'last_daily_battle_at, '
                'is_on_leave, previous_template, head_id '
                'FROM characters WHERE player_id = $1 AND NOT is_deleted '
                'ORDER BY id', player_id)
            return [dict(r) for r in rows]

    async def get_character(self, player_id, character_id):
        """按 id 查询单个角色（软删角色视为不存在），未找到返回 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, name, faction, template, combat_xp, combat_badge_0, combat_badge_1, '
                'uniform_variant_id, headgear_id, squad_points, '
                f'{_DAILY_BONUS_AVAILABLE_SQL} AS receive_daily_bonus, '
                'last_daily_battle_at, '
                'is_on_leave, previous_template, head_id '
                'FROM characters WHERE id = $1 AND player_id = $2 AND NOT is_deleted',
                character_id, player_id)
            return dict(row) if row else None

    async def consume_daily_bonus(self, player_id, character_id):
        """每日首战 +50% XP 资格消费：当天未用则记账并返回 True。

        单条 UPDATE 原子判定（WHERE 带资格条件 + RETURNING），当前窗口
        已消费（last_daily_battle_at 晚于最近 05:00 UTC 重置点）返回 False
        不重复记账；跨过重置点后推导自动回 TRUE，无需定时任务。
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'UPDATE characters SET last_daily_battle_at = now() '
                'WHERE id = $1 AND player_id = $2 AND NOT is_deleted '
                f'AND {_DAILY_BONUS_AVAILABLE_SQL} '
                'RETURNING id',
                int(character_id), int(player_id))
            return row is not None

    async def create_character(self, player_id, name, faction, template, combat_xp=0,
                               uniform_variant_template_id=0):
        """创建角色（军衔单源：起始军衔由调用方换算为初始 combat_xp）。

        uniform_variant_template_id 非零时同步创建默认军服实例并装备
        （(兵种,阵营) 基础迷彩，由调用方经 wardb.default_uniform_variant 解析；
        官方服角色生来带基础迷彩，缺失则 PlayerSettings f9 为空、角色不显示）。
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    'INSERT INTO characters (player_id, name, faction, template, combat_xp) '
                    'VALUES ($1, $2, $3, $4, $5) '
                    'RETURNING id, name, faction, template, combat_xp, '
                    'combat_badge_0, combat_badge_1, uniform_variant_id, headgear_id, '
                    'squad_points, receive_daily_bonus',
                    player_id, name, faction, template, combat_xp)
                if uniform_variant_template_id:
                    uv = await conn.fetchrow(
                        'INSERT INTO uniform_variants '
                        '(player_id, uniform_variant_template_id, character_id) '
                        'VALUES ($1, $2, $3) RETURNING id',
                        player_id, uniform_variant_template_id, row['id'])
                    row = await conn.fetchrow(
                        'UPDATE characters SET uniform_variant_id = $1 WHERE id = $2 '
                        'RETURNING id, name, faction, template, combat_xp, '
                        'combat_badge_0, combat_badge_1, uniform_variant_id, headgear_id, '
                        'squad_points, receive_daily_bonus',
                        uv['id'], row['id'])
                return dict(row)

    # ---- character ribbon XP（勋带单源）----

    async def get_ribbon_xp(self, player_id, character_id):
        """返回该角色 {ribbonTemplateId: total_xp}（勋带累计经验，等级由阈值推导）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT r.ribbon_template_id, r.total_xp '
                'FROM character_ribbon_xp r JOIN characters c ON c.id = r.character_id '
                'WHERE c.id = $1 AND c.player_id = $2',
                character_id, player_id)
            return {int(r['ribbon_template_id']): float(r['total_xp']) for r in rows}

    async def get_ribbon_xp_by_char(self, character_id):
        """按角色 id 直接取勋带 XP（统计服务器用：客户端 GetTotals 只报 charId）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT ribbon_template_id, total_xp FROM character_ribbon_xp '
                'WHERE character_id = $1',
                character_id)
            return {int(r['ribbon_template_id']): float(r['total_xp']) for r in rows}

    async def set_ribbon_xp_floor(self, player_id, character_id,
                                  ribbon_template_id, min_xp):
        """购买勋带等级 = 把该勋带 XP 补到门槛（total_xp = max(total_xp, min_xp)）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO character_ribbon_xp (character_id, ribbon_template_id, total_xp) '
                'SELECT id, $2, $3 FROM characters WHERE id = $1 AND player_id = $4 '
                'ON CONFLICT (character_id, ribbon_template_id) '
                'DO UPDATE SET total_xp = GREATEST(character_ribbon_xp.total_xp, EXCLUDED.total_xp)',
                character_id, ribbon_template_id, min_xp, player_id)

    async def add_ribbon_xp(self, player_id, character_id,
                            ribbon_template_id, amount):
        """战后结算：给勋带增加经验（total_xp += amount），返回新值。"""
        async with self.pool.acquire() as conn:
            val = await conn.fetchval(
                'INSERT INTO character_ribbon_xp (character_id, ribbon_template_id, total_xp) '
                'SELECT id, $2, $3 FROM characters WHERE id = $1 AND player_id = $4 '
                'ON CONFLICT (character_id, ribbon_template_id) '
                'DO UPDATE SET total_xp = character_ribbon_xp.total_xp + EXCLUDED.total_xp '
                'RETURNING character_ribbon_xp.total_xp',
                character_id, ribbon_template_id, amount, player_id)
            return float(val or 0)

    async def set_combat_badge(self, player_id, character_id, slot, badge_id):
        """设置角色战斗徽章槽（slot=0/1）；badge_id=0 表示卸下。

        官方语义是"移动"而非"复制"：客户端把已装备徽章拖到另一槽时只发
        set 新槽一条请求、不发卸下旧槽（SoldierOverviewMediator.
        badgesList_dragDropHandler:960-973），故装备非 0 徽章时若另一槽
        持有同一徽章需同步清 0，否则两槽同 id → UI 显示"复制"。
        """
        if int(slot) == 0:
            column, other = 'combat_badge_0', 'combat_badge_1'
        else:
            column, other = 'combat_badge_1', 'combat_badge_0'
        badge_id = int(badge_id)
        async with self.pool.acquire() as conn:
            if badge_id:
                await conn.execute(
                    'UPDATE characters SET {0} = $1, '
                    '{1} = CASE WHEN {1} = $1 THEN 0 ELSE {1} END '
                    'WHERE id = $2 AND player_id = $3'.format(column, other),
                    badge_id, int(character_id), int(player_id))
            else:
                await conn.execute(
                    'UPDATE characters SET {0} = 0 '
                    'WHERE id = $1 AND player_id = $2'.format(column),
                    int(character_id), int(player_id))

    async def set_uniform_variant(self, player_id, character_id, variant_id):
        """装备/卸下角色军服迷彩变体（variant_id=0 卸下），返回该角色 uniform_variant_id。"""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                'UPDATE characters SET uniform_variant_id = $1 '
                'WHERE id = $2 AND player_id = $3 RETURNING uniform_variant_id',
                int(variant_id), int(character_id), int(player_id))

