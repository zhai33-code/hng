# -*- coding: utf-8 -*-
"""聊天频道创建/加入/退出/订阅。
拆分自 player_store.py（纯移动重构，方法体逐字保留）。
"""
from .base import _rowcount

class ChatMixin:
    """聊天频道创建/加入/退出/订阅。"""

    # ------------------------------------------------------------- 聊天频道

    async def create_channel(self, name, owner_id, tab='general', password='',
                             hidden=False, invitation_only=False):
        """创建频道并让创建者加入。返回 (channel_id, ok)。"""
        async with self.pool.acquire() as conn:
            try:
                cid = await conn.fetchval(
                    'INSERT INTO chat_channels (name, owner_id, display_name, '
                    'channel_tab, password, hidden, invitation_only) '
                    "VALUES ($1, $2, '', $3, $4, $5, $6) RETURNING id",
                    str(name), int(owner_id or 0), str(tab or 'general'),
                    str(password or ''), bool(hidden), bool(invitation_only))
            except Exception:
                return 0, False              # name UNIQUE 冲突 → 已存在
            await conn.execute(
                'INSERT INTO chat_channel_members (channel_id, player_id) '
                'VALUES ($1, $2) ON CONFLICT DO NOTHING', cid, int(owner_id))
        return int(cid), True

    async def join_channel(self, player_id, channel_id=None, channel_name=None):
        """加入频道。返回 (ok, code)：code 0=ok 9=channel_already_added
        12=channel_not_found。"""
        async with self.pool.acquire() as conn:
            if channel_id is None:
                row = await conn.fetchrow(
                    'SELECT id FROM chat_channels WHERE lower(name) = lower($1)',
                    str(channel_name or ''))
                if row is None:
                    return False, 12
                channel_id = row['id']
            else:
                row = await conn.fetchrow(
                    'SELECT id FROM chat_channels WHERE id = $1', int(channel_id))
                if row is None:
                    return False, 12
            dup = await conn.fetchrow(
                'SELECT 1 FROM chat_channel_members WHERE channel_id = $1 '
                'AND player_id = $2', int(channel_id), int(player_id))
            if dup is not None:
                return False, 9
            await conn.execute(
                'INSERT INTO chat_channel_members (channel_id, player_id) '
                'VALUES ($1, $2) ON CONFLICT DO NOTHING',
                int(channel_id), int(player_id))
        return True, 0

    async def leave_channel(self, player_id, channel_id):
        """退出频道。返回是否退成。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'DELETE FROM chat_channel_members WHERE channel_id = $1 '
                'AND player_id = $2', int(channel_id), int(player_id))
        return _rowcount(n) > 0

    async def update_channel(self, channel_id, owner_id=None, new_name=None,
                             password=None, invitation_only=None, hidden=None):
        """编辑频道（改名/密码/邀请制/隐藏），owner 校验可选。返回是否更新。"""
        sets, args = [], [int(channel_id)]
        if new_name is not None:
            args.append(str(new_name))
            sets.append(f'name = ${len(args)}')
        if password is not None:
            args.append(str(password))
            sets.append(f'password = ${len(args)}')
        if invitation_only is not None:
            args.append(bool(invitation_only))
            sets.append(f'invitation_only = ${len(args)}')
        if hidden is not None:
            args.append(bool(hidden))
            sets.append(f'hidden = ${len(args)}')
        if not sets:
            return False
        cond = ''
        if owner_id is not None:
            args.append(int(owner_id))
            cond = f' AND owner_id = ${len(args)}'
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                f'UPDATE chat_channels SET {", ".join(sets)} '
                f'WHERE id = $1{cond}', *args)
        return _rowcount(n) > 0

    async def list_subscribed_channels(self, player_id):
        """玩家已订阅频道（GetChatChannelsSubscribedResponse 源）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT c.id, c.name, c.display_name, c.channel_tab '
                'FROM chat_channel_members m JOIN chat_channels c '
                '  ON c.id = m.channel_id WHERE m.player_id = $1 '
                'ORDER BY c.id', int(player_id))
        return [{'id': int(r['id']), 'name': r['name'],
                 'display': r['display_name'] or r['name'],
                 'tab': r['channel_tab']} for r in rows]

