# -*- coding: utf-8 -*-
"""小队 CRUD 与成员管理。
拆分自 player_store.py（纯移动重构，方法体逐字保留）。
"""
from .base import _rowcount

class SquadsMixin:
    """小队 CRUD 与成员管理。"""

    # ------------------------------------------------------------- 小队（U5）

    async def create_squad(self, char_id, faction, name, matchmaking_flag=False):
        """创建小队 + 队长成员会话（CreateSquadRequest）。返回 squad_id。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                player_id = await conn.fetchval(
                    'SELECT player_id FROM characters WHERE id = $1',
                    int(char_id))
                if player_id is None:
                    raise ValueError('leader character not found')
                await conn.execute('SELECT pg_advisory_xact_lock($1)',
                                   int(player_id))
                occupied = await conn.fetchval(
                    'SELECT 1 FROM squad_members m JOIN characters c '
                    'ON c.id = m.character_id WHERE c.player_id = $1 LIMIT 1',
                    int(player_id))
                if occupied:
                    raise ValueError('player already in squad')
                sid = await conn.fetchval(
                    'INSERT INTO squads (name, faction, leader_character_id, '
                    'matchmaking_flag) VALUES ($1, $2, $3, $4) RETURNING id',
                    str(name or ''), int(faction or 0), int(char_id),
                    bool(matchmaking_flag))
                await conn.execute(
                    'INSERT INTO squad_members (squad_id, character_id, status) '
                    'VALUES ($1, $2, 2)', int(sid), int(char_id))  # 2=InLobby
        return int(sid)

    async def create_squad_with_invite(self, leader_char_id, faction, name,
                                       invited_char_id, status=1):
        """Atomically create an implicit squad, its leader, and first invite."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                leader_pid = await conn.fetchval(
                    'SELECT player_id FROM characters WHERE id = $1',
                    int(leader_char_id))
                invited_pid = await conn.fetchval(
                    'SELECT player_id FROM characters WHERE id = $1',
                    int(invited_char_id))
                if leader_pid is None or invited_pid is None:
                    raise ValueError('squad character not found')
                for player_id in sorted({int(leader_pid), int(invited_pid)}):
                    await conn.execute('SELECT pg_advisory_xact_lock($1)',
                                       player_id)
                occupied = await conn.fetchval(
                    'SELECT 1 FROM squad_members m JOIN characters c '
                    'ON c.id = m.character_id '
                    'WHERE c.player_id = ANY($1::bigint[]) LIMIT 1',
                    [int(leader_pid), int(invited_pid)])
                if occupied:
                    raise ValueError('player already in squad')
                sid = await conn.fetchval(
                    'INSERT INTO squads (name, faction, leader_character_id, '
                    'matchmaking_flag) VALUES ($1, $2, $3, FALSE) RETURNING id',
                    str(name or ''), int(faction or 0), int(leader_char_id))
                await conn.execute(
                    'INSERT INTO squad_members (squad_id, character_id, status) '
                    'VALUES ($1, $2, 2)', int(sid), int(leader_char_id))
                mid = await conn.fetchval(
                    'INSERT INTO squad_members (squad_id, character_id, status) '
                    'VALUES ($1, $2, $3) RETURNING id',
                    int(sid), int(invited_char_id), int(status))
        return int(sid), int(mid)

    async def update_squad_leader(self, squad_id, leader_char_id, faction):
        """队长换兵种/换角色（官服语义：小队类型跟随队长）。
        更新 squads.leader_character_id + faction。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE squads SET leader_character_id = $2, faction = $3 '
                'WHERE id = $1', int(squad_id), int(leader_char_id),
                int(faction))

    async def get_squad(self, squad_id):
        """小队行或 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, name, faction, leader_character_id, matchmaking_flag '
                'FROM squads WHERE id = $1', int(squad_id))
        return dict(row) if row else None

    async def get_squad_for_character(self, char_id):
        """角色所在小队或 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT s.id, s.name, s.faction, s.leader_character_id, '
                's.matchmaking_flag FROM squads s '
                'JOIN squad_members m ON m.squad_id = s.id '
                'WHERE m.character_id = $1', int(char_id))
        return dict(row) if row else None

    async def get_squad_for_player(self, player_id):
        """玩家（任一角色）所在小队或 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT s.id, s.name, s.faction, s.leader_character_id, '
                's.matchmaking_flag FROM squads s '
                'JOIN squad_members m ON m.squad_id = s.id '
                'JOIN characters c ON c.id = m.character_id '
                'WHERE c.player_id = $1 '
                'ORDER BY CASE WHEN m.status = 1 THEN 1 ELSE 0 END, s.id '
                'LIMIT 1', int(player_id))
        return dict(row) if row else None

    async def list_squad_members(self, squad_id):
        """小队成员（SquadMemberSession KVCS 源）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT m.id AS member_id, m.character_id, m.status, '
                'p.id AS player_id, p.username, c.template, c.combat_xp, '
                'c.name AS char_name, c.faction '
                'FROM squad_members m '
                'JOIN characters c ON c.id = m.character_id '
                'JOIN players p ON p.id = c.player_id '
                'WHERE m.squad_id = $1 ORDER BY m.id', int(squad_id))
        return [{'id': int(r['member_id']),
                 'character_id': int(r['character_id']),
                 'status': int(r['status']),
                 'player_id': int(r['player_id']),
                 'gamertag': r['username'],
                 'template': int(r['template']),
                 'combat_xp': int(r['combat_xp']),
                 'char_name': r['char_name'],
                 'faction': int(r['faction'])} for r in rows]

    async def set_player_selected_char(self, player_id, character_id):
        """持久化"当前选中士兵"（官服组队规则需要被邀者的选中角色）。

        客户端无独立选角消息，服务器从携带 characterId 的请求侧面感知
        （JoinMatchMaking / 邀请 / 接受邀请 / GetMissionList），写内存
        注册表 state.PLAYER_SELECTED_CHAR 时同步落库，重启后回放。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO player_selected_char (player_id, character_id, '
                'updated_at) VALUES ($1, $2, now()) '
                'ON CONFLICT (player_id) DO UPDATE SET '
                'character_id = $2, updated_at = now()',
                int(player_id), int(character_id))

    async def get_player_selected_char(self, player_id):
        """玩家持久化的选中士兵 id；无记录返回 0。"""
        async with self.pool.acquire() as conn:
            cid = await conn.fetchval(
                'SELECT character_id FROM player_selected_char '
                'WHERE player_id = $1', int(player_id))
        return int(cid) if cid else 0

    async def load_all_selected_chars(self):
        """全量回放 player_id → character_id（服务器启动时灌内存注册表）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT player_id, character_id FROM player_selected_char')
        return {int(r['player_id']): int(r['character_id']) for r in rows}

    async def add_squad_member(self, squad_id, char_id, status=1):
        """添加成员会话行（1=InvitationPending 2=InLobby 3=Ready 4=InCombat）。
        返回成员行 id。"""
        async with self.pool.acquire() as conn:
            mid = await conn.fetchval(
                'INSERT INTO squad_members (squad_id, character_id, status) '
                'VALUES ($1, $2, $3) ON CONFLICT (squad_id, character_id) '
                'DO UPDATE SET status = $3 RETURNING id',
                int(squad_id), int(char_id), int(status))
        return int(mid)

    async def add_squad_member_checked(self, squad_id, char_id, status=1,
                                       max_members=4):
        """Serialize invitations and enforce one squad per player/capacity."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.fetchrow('SELECT id FROM squads WHERE id = $1 '
                                    'FOR UPDATE', int(squad_id))
                player_id = await conn.fetchval(
                    'SELECT player_id FROM characters WHERE id = $1',
                    int(char_id))
                if player_id is None:
                    return 0, 'missing'
                await conn.execute('SELECT pg_advisory_xact_lock($1)',
                                   int(player_id))
                occupied = await conn.fetchval(
                    'SELECT 1 FROM squad_members m JOIN characters c '
                    'ON c.id = m.character_id WHERE c.player_id = $1 LIMIT 1',
                    int(player_id))
                if occupied:
                    return 0, 'already'
                count = await conn.fetchval(
                    'SELECT count(*) FROM squad_members WHERE squad_id = $1',
                    int(squad_id))
                if int(count or 0) >= int(max_members):
                    return 0, 'full'
                mid = await conn.fetchval(
                    'INSERT INTO squad_members (squad_id, character_id, status) '
                    'VALUES ($1, $2, $3) RETURNING id',
                    int(squad_id), int(char_id), int(status))
        return int(mid), 'ok'

    async def reconfigure_squad_members(self, squad_id, leader_member_id,
                                        leader_char_id, faction, status,
                                        replacements, removals):
        """Atomically switch squad type and apply all member migrations."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                locked = await conn.fetchrow(
                    'SELECT id FROM squads WHERE id = $1 FOR UPDATE',
                    int(squad_id))
                if locked is None:
                    return False
                await conn.execute(
                    'UPDATE squads SET leader_character_id = $2, faction = $3 '
                    'WHERE id = $1', int(squad_id), int(leader_char_id),
                    int(faction))
                if status:
                    await conn.execute(
                        'UPDATE squad_members SET character_id = $2, status = $3 '
                        'WHERE id = $1', int(leader_member_id),
                        int(leader_char_id), int(status))
                else:
                    await conn.execute(
                        'UPDATE squad_members SET character_id = $2 '
                        'WHERE id = $1', int(leader_member_id),
                        int(leader_char_id))
                for member_id, char_id in replacements:
                    await conn.execute(
                        'UPDATE squad_members SET character_id = $2 '
                        'WHERE id = $1', int(member_id), int(char_id))
                if removals:
                    await conn.execute(
                        'DELETE FROM squad_members WHERE id = ANY($1::bigint[])',
                        [int(x) for x in removals])
        return True

    async def get_squad_member(self, member_row_id):
        """按成员行 id 查（RemoveSquadMember/SquadRespondToInvite）。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, squad_id, character_id, status '
                'FROM squad_members WHERE id = $1', int(member_row_id))
        return dict(row) if row else None

    async def get_squad_member_by_char(self, squad_id, char_id):
        """按 (squad_id, character_id) 查（SetSquadMemberStatus）。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, squad_id, character_id, status '
                'FROM squad_members WHERE squad_id = $1 AND character_id = $2',
                int(squad_id), int(char_id))
        return dict(row) if row else None

    async def update_squad_member(self, member_row_id, character_id=None,
                                  status=None):
        """更新成员会话（接受邀请换角色/改状态）。返回是否更新。"""
        sets, args = [], [int(member_row_id)]
        if character_id is not None:
            args.append(int(character_id))
            sets.append(f'character_id = ${len(args)}')
        if status is not None:
            args.append(int(status))
            sets.append(f'status = ${len(args)}')
        if not sets:
            return False
        try:
            async with self.pool.acquire() as conn:
                n = await conn.execute(
                    f'UPDATE squad_members SET {", ".join(sets)} '
                    'WHERE id = $1', *args)
        except Exception as exc:
            # character_id 换成同队已有角色 → PK 冲突，退化为仅改状态
            if getattr(exc, 'sqlstate', None) != '23505' or status is None:
                raise
            async with self.pool.acquire() as conn:
                n = await conn.execute(
                    'UPDATE squad_members SET status = $2 WHERE id = $1',
                    int(member_row_id), int(status))
        return _rowcount(n) > 0

    async def remove_squad_member(self, member_row_id):
        """删除成员会话行。返回是否删除。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'DELETE FROM squad_members WHERE id = $1', int(member_row_id))
        return _rowcount(n) > 0

    async def dissolve_squad(self, squad_id):
        """解散小队（级联删成员行）。返回是否删除。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'DELETE FROM squads WHERE id = $1', int(squad_id))
        return _rowcount(n) > 0

    async def rename_squad(self, squad_id, name):
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE squads SET name = $2 WHERE id = $1',
                int(squad_id), str(name))
        return _rowcount(n) > 0

    async def set_squad_flag(self, squad_id, flag):
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE squads SET matchmaking_flag = $2 WHERE id = $1',
                int(squad_id), bool(flag))
        return _rowcount(n) > 0
