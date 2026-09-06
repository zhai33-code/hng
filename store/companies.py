# -*- coding: utf-8 -*-
"""军团（连队）CRUD / 邀请 / 徽章。
拆分自 player_store.py（纯移动重构，方法体逐字保留）。
"""
import json
from .base import _rowcount

class CompaniesMixin:
    """军团（连队）CRUD / 邀请 / 徽章。"""

    # ------------------------------------------------------------- 军团（U5）

    async def create_company(self, name_long, name_short, leader_player_id,
                             description=''):
        """创建军团（CreateCompanyRequest），创建者 role=2（队长）。返回 company_id。"""
        async with self.pool.acquire() as conn:
            cid = await conn.fetchval(
                'INSERT INTO companies (name, name_short, leader_player_id, '
                'description) VALUES ($1, $2, $3, $4) RETURNING id',
                str(name_long), str(name_short), int(leader_player_id),
                str(description or ''))
            await conn.execute(
                'INSERT INTO company_members (company_id, player_id, role) '
                'VALUES ($1, $2, 2)', int(cid), int(leader_player_id))
        return int(cid)

    async def company_name_exists(self, name_long):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT 1 FROM companies WHERE lower(name) = lower($1)',
                str(name_long))
        return row is not None

    async def get_company_for_player(self, player_id):
        """玩家所在军团 {id, name, leader_player_id, role} 或 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT c.id, c.name, c.leader_player_id, m.role '
                'FROM companies c JOIN company_members m ON m.company_id = c.id '
                'WHERE m.player_id = $1', int(player_id))
        return dict(row) if row else None

    async def add_company_member(self, company_id, player_id, role=0):
        async with self.pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO company_members (company_id, player_id, role) '
                'VALUES ($1, $2, $3) ON CONFLICT (company_id, player_id) '
                'DO NOTHING', int(company_id), int(player_id), int(role))

    async def remove_company_member(self, company_id, player_id):
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'DELETE FROM company_members WHERE company_id = $1 '
                'AND player_id = $2', int(company_id), int(player_id))
        return _rowcount(n) > 0

    async def invite_to_company(self, company_id, player_id, invited_by):
        """军团邀请。返回 (invitation_id, code)：1 ok / 174 already in company
        / 175 invitation pending。"""
        async with self.pool.acquire() as conn:
            member = await conn.fetchrow(
                'SELECT 1 FROM company_members WHERE company_id = $1 '
                'AND player_id = $2', int(company_id), int(player_id))
            if member is not None:
                return 0, 174
            pend = await conn.fetchrow(
                'SELECT id FROM company_invitations WHERE company_id = $1 '
                'AND player_id = $2', int(company_id), int(player_id))
            if pend is not None:
                return 0, 175
            iid = await conn.fetchval(
                'INSERT INTO company_invitations (company_id, player_id, '
                'invited_by) VALUES ($1, $2, $3) RETURNING id',
                int(company_id), int(player_id), int(invited_by))
        return int(iid), 1

    async def respond_company_invitation(self, invitation_id, accept):
        """接受/拒绝军团邀请。返回 (code, company_id)：
        1 ok / 185 invitation_not_found / 174 already in company。

        删邀请与入团在同一事务内（FOR UPDATE 锁邀请行防并发双兑）。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    'SELECT id, company_id, player_id FROM company_invitations '
                    'WHERE id = $1 FOR UPDATE', int(invitation_id))
                if row is None:
                    return 185, 0
                await conn.execute(
                    'DELETE FROM company_invitations WHERE id = $1', int(invitation_id))
                if accept:
                    in_co = await conn.fetchrow(
                        'SELECT 1 FROM company_members WHERE player_id = $1',
                        int(row['player_id']))
                    if in_co is not None:
                        return 174, 0
                    await conn.execute(
                        'INSERT INTO company_members (company_id, player_id, role) '
                        'VALUES ($1, $2, 0) ON CONFLICT DO NOTHING',
                        int(row['company_id']), int(row['player_id']))
        return 1, int(row['company_id'])

    async def leave_company(self, player_id):
        """退出军团；队长退出且仍有成员 → 移交队长，无成员 → 解散。
        返回 (code, company_id)：1 ok / 186 no company。

        退团与队长移交/解散在同一事务内（锁军团行防交错）。"""
        co = await self.get_company_for_player(player_id)
        if co is None:
            return 186, 0
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # 锁军团行：并发退团/解散时串行化移交与解散判定
                locked = await conn.fetchval(
                    'SELECT id FROM companies WHERE id = $1 FOR UPDATE',
                    int(co['id']))
                if locked is None:
                    return 186, 0
                await conn.execute(
                    'DELETE FROM company_members WHERE company_id = $1 '
                    'AND player_id = $2', int(co['id']), int(player_id))
                if int(co['leader_player_id']) == int(player_id):
                    nxt = await conn.fetchrow(
                        'SELECT player_id FROM company_members WHERE company_id = $1 '
                        'ORDER BY role DESC, player_id LIMIT 1', int(co['id']))
                    if nxt is None:
                        await conn.execute(
                            'DELETE FROM companies WHERE id = $1', int(co['id']))
                    else:
                        await conn.execute(
                            'UPDATE companies SET leader_player_id = $2 WHERE id = $1',
                            int(co['id']), int(nxt['player_id']))
        return 1, int(co['id'])

    async def create_insignia(self, player_id, parts):
        """军团徽章创建（CreateInsigniaRequest）。parts 为 InsigniaPart
        dict 列表（partType/partId/fieldId/scale/offset），存 JSONB。
        返回新行 id。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO player_insignias (player_id, parts) '
                'VALUES ($1, $2::jsonb) RETURNING id',
                int(player_id), json.dumps(parts or []))
            return int(row['id'])

    async def remove_insignia(self, player_id, insignia_id):
        """军团徽章删除（RemoveInsigniaRequest）。返回是否删除。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'DELETE FROM player_insignias WHERE id = $1 AND player_id = $2',
                int(insignia_id), int(player_id))
        return _rowcount(n) > 0

