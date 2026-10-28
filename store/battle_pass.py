# -*- coding: utf-8 -*-
"""战斗通行证（赛季/积分/购买/激活/领奖）。
拆分自 player_store.py（纯移动重构，方法体逐字保留）。
"""
import json
from datetime import datetime

class BattlePassMixin:
    """战斗通行证（赛季/积分/购买/激活/领奖）。"""

    # ------------------------------------------------------------- battle pass

    async def get_battle_pass(self, player_id, season_id):
        """读取通行证进度行：{score, purchased, active, claimed, unlock} 或 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT xp, purchased, is_active, claimed_tiers, unlock_at '
                'FROM battle_pass_progress WHERE player_id = $1 AND season_id = $2',
                player_id, int(season_id))
        if row is None:
            return None
        return {
            'score': int(row['xp']),
            'purchased': bool(row['purchased']),
            'active': bool(row['is_active']),
            'claimed': json.loads(row['claimed_tiers'] or '[]'),
            'unlock': row['unlock_at'] or '',
        }

    async def add_battle_pass_score(self, player_id, season_id, delta):
        """通行证分数累加（结算时调用）。返回新分数。"""
        async with self.pool.acquire() as conn:
            val = await conn.fetchval(
                'INSERT INTO battle_pass_progress (player_id, season_id, xp) '
                'VALUES ($1, $2, $3) '
                'ON CONFLICT (player_id, season_id) '
                'DO UPDATE SET xp = battle_pass_progress.xp + EXCLUDED.xp '
                'RETURNING xp',
                player_id, int(season_id), int(delta))
        return int(val or 0)

    async def set_battle_pass_score(self, player_id, season_id, score):
        """通行证分数设值（买层级完成：提升到 requiredScore，只升不降）。

        xp = GREATEST(xp, score)，返回新分数。"""
        async with self.pool.acquire() as conn:
            val = await conn.fetchval(
                'INSERT INTO battle_pass_progress (player_id, season_id, xp) '
                'VALUES ($1, $2, $3) '
                'ON CONFLICT (player_id, season_id) '
                'DO UPDATE SET xp = GREATEST(battle_pass_progress.xp, '
                'EXCLUDED.xp) '
                'RETURNING xp',
                player_id, int(season_id), int(score))
        return int(val or 0)

    async def purchase_battle_pass(self, player_id, season_id, gold_cost=0):
        """购买通行证：扣金币 + purchased=true。返回 (ok, 剩余gold)。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                gold = await conn.fetchval(
                    'SELECT gold FROM players WHERE id = $1 FOR UPDATE', player_id)
                if gold is None:
                    return False, 0
                if int(gold) < int(gold_cost):
                    return False, int(gold)
                gold = await conn.fetchval(
                    'UPDATE players SET gold = gold - $2 WHERE id = $1 '
                    'RETURNING gold', player_id, int(gold_cost))
                await conn.execute(
                    'INSERT INTO battle_pass_progress '
                    '    (player_id, season_id, purchased, unlock_at) '
                    'VALUES ($1, $2, TRUE, $3) '
                    'ON CONFLICT (player_id, season_id) '
                    'DO UPDATE SET purchased = TRUE, unlock_at = $3',
                    player_id, int(season_id),
                    datetime.utcnow().strftime('%Y%m%d%H%M%SZ'))
        return True, int(gold)

    async def activate_battle_pass(self, player_id, season_id):
        """激活已购买的通行证（is_active=true，需先 purchased）。

        互斥激活：同一玩家同时只有一个激活赛季（客户端 activeOrNewest
        先取 isActive 行；多个同时为真时行为不确定），激活成功后把
        其他赛季的 is_active 清掉。返回是否激活成功。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    'INSERT INTO battle_pass_progress (player_id, season_id) '
                    'VALUES ($1, $2) '
                    'ON CONFLICT (player_id, season_id) DO UPDATE SET is_active = TRUE '
                    'WHERE battle_pass_progress.purchased '
                    'RETURNING is_active',
                    player_id, int(season_id))
                ok = bool(row['is_active']) if row else False
                if ok:
                    await conn.execute(
                        'UPDATE battle_pass_progress SET is_active = FALSE '
                        'WHERE player_id = $1 AND season_id <> $2',
                        player_id, int(season_id))
        return ok

    async def get_active_battle_pass_season(self, player_id):
        """玩家当前激活的通行证赛季 id（无激活行返回 None）。

        结算涨分路由用：激活哪个赛季就涨哪个（客户端"激活"切换语义），
        未激活任何赛季时由调用方回落到当前赛季。"""
        async with self.pool.acquire() as conn:
            val = await conn.fetchval(
                'SELECT season_id FROM battle_pass_progress '
                'WHERE player_id = $1 AND is_active AND purchased '
                'ORDER BY season_id DESC LIMIT 1', player_id)
        return int(val) if val is not None else None

    async def claim_battle_pass_reward(self, player_id, season_id, template_id):
        """领取通行证奖励模板：写 claimed_tiers（幂等，需已购买）。

        返回 (首次领取, 全部已领列表)。
        FOR UPDATE 锁行必须在事务内才有效（否则行锁随语句自动提交即释放）。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    'SELECT claimed_tiers FROM battle_pass_progress '
                    'WHERE player_id = $1 AND season_id = $2 AND purchased '
                    'FOR UPDATE', player_id, int(season_id))
                if row is None:
                    return False, []
                claimed = json.loads(row['claimed_tiers'] or '[]')
                first = int(template_id) not in claimed
                if first:
                    claimed.append(int(template_id))
                    await conn.execute(
                        'UPDATE battle_pass_progress SET claimed_tiers = $3::jsonb '
                        'WHERE player_id = $1 AND season_id = $2',
                        player_id, int(season_id), json.dumps(claimed))
        return first, claimed

    async def rollback_battle_pass_reward_claim(self, player_id, season_id,
                                                template_id):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    'SELECT claimed_tiers FROM battle_pass_progress '
                    'WHERE player_id = $1 AND season_id = $2 FOR UPDATE',
                    int(player_id), int(season_id))
                if row is None:
                    return
                claimed = json.loads(row['claimed_tiers'] or '[]')
                claimed = [x for x in claimed if int(x) != int(template_id)]
                await conn.execute(
                    'UPDATE battle_pass_progress SET claimed_tiers = $3::jsonb '
                    'WHERE player_id = $1 AND season_id = $2',
                    int(player_id), int(season_id), json.dumps(claimed))
