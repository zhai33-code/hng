# -*- coding: utf-8 -*-
"""任务记录（战局持久化）。
拆分自 player_store.py（纯移动重构，方法体逐字保留）。
"""
from .base import _rowcount

class MissionsMixin:
    """任务记录（战局持久化）。"""

    # ------------------------------------------------------------- 任务记录

    async def create_mission_record(self, preset, attacker_faction=0,
                                    defender_faction=0, owner_player_id=0,
                                    name='', map_entity_id=0, battle_id=0):
        """CreateMissionRequest / war 会战成局落库（state=0 组队中）。
        battle_id>0 = war 会战局（M2）。返回 mission_id。"""
        async with self.pool.acquire() as conn:
            mid = await conn.fetchval(
                'INSERT INTO mission_records (name, preset, state, '
                'attacker_faction, defender_faction, owner_player_id, '
                'map_entity_id, battle_id) VALUES ($1, $2, 0, $3, $4, $5, '
                '$6, $7) RETURNING id',
                str(name or ''), int(preset or 0), int(attacker_faction or 0),
                int(defender_faction or 0), int(owner_player_id or 0),
                int(map_entity_id or 0), int(battle_id or 0))
        return int(mid)

    async def set_mission_state(self, mission_id, state):
        """更新任务状态：0 组队中 1 运行 2 结束 3 放弃。返回是否更新。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE mission_records SET state = $2 WHERE id = $1',
                int(mission_id), int(state))
        return _rowcount(n) > 0

    async def close_stale_missions(self):
        """服务器启动清理：所有 state IN (0,1) 的任务置为 state=2（结束）。

        重启后内存态（COMBAT_INSTANCES / 匹配池）已丢失，DB 里的组队中/
        运行中记录不可能再推进——不清掉会被 GetMissionList 当活局展示
        （玩家点进去是空壳）。返回清理行数。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE mission_records SET state = 2 WHERE state IN (0, 1)')
        return _rowcount(n)

    async def list_missions_by_state(self, states, limit=50):
        """按状态查任务记录。"""
        if not states:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, name, preset, state, attacker_faction, '
                'defender_faction, owner_player_id, map_entity_id, battle_id '
                'FROM mission_records WHERE state = ANY($1::int[]) '
                'ORDER BY id DESC LIMIT $2', [int(s) for s in states],
                int(limit))
        return [dict(r) for r in rows]

    async def get_mission_record(self, mission_id):
        """单条任务记录或 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, name, preset, state, attacker_faction, '
                'defender_faction, owner_player_id, map_entity_id, battle_id '
                'FROM mission_records WHERE id = $1', int(mission_id))
        return dict(row) if row else None

    async def get_mission_by_battle(self, battle_id, states=(0, 1)):
        """按 war battle 查活任务记录（M2：撮合/automission 判定）。

        返回最新一条或 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, name, preset, state, attacker_faction, '
                'defender_faction, owner_player_id, map_entity_id, battle_id '
                'FROM mission_records WHERE battle_id = $1 '
                'AND state = ANY($2::int[]) ORDER BY id DESC LIMIT 1',
                int(battle_id), [int(s) for s in states])
        return dict(row) if row else None

