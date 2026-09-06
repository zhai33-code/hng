# -*- coding: utf-8 -*-
"""好友 / 搜索 / 屏蔽 / 举报。
拆分自 player_store.py（纯移动重构，方法体逐字保留）。
"""
from .base import _rowcount

class SocialGraphMixin:
    """好友 / 搜索 / 屏蔽 / 举报。"""

    # ------------------------------------------------------------- 好友 / 屏蔽

    async def add_friend(self, player_id, friend_id):
        """添加好友（单向请求行 status=0；对方也已请求则双向转正 status=1）。

        返回 'ok'（请求已发出）/ 'accepted'（直接成为好友）/ 'already' /
        'self'（不能加自己）。"""
        if int(player_id) == int(friend_id):
            return 'self'
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT 1 FROM friends WHERE player_id = $1 AND friend_id = $2 '
                'AND status = 1', int(player_id), int(friend_id))
            if row is not None:
                return 'already'
            reverse = await conn.fetchrow(
                'SELECT 1 FROM friends WHERE player_id = $2 AND friend_id = $1 '
                'AND status = 0', int(player_id), int(friend_id))
            # 无反向请求 → status=0（重复邀请幂等，不单方面转正）；
            # 有反向请求 → 本行直接置 1，与下方反向行 UPDATE 保持对称
            await conn.execute(
                'INSERT INTO friends (player_id, friend_id, status) '
                'VALUES ($1, $2, $3) ON CONFLICT (player_id, friend_id) '
                'DO UPDATE SET status = EXCLUDED.status',
                int(player_id), int(friend_id), 1 if reverse is not None else 0)
            if reverse is not None:
                await conn.execute(
                    'UPDATE friends SET status = 1 '
                    'WHERE player_id = $2 AND friend_id = $1',
                    int(player_id), int(friend_id))
                return 'accepted'
        return 'ok'

    async def remove_friend(self, player_id, friend_id):
        """删除好友（双向）。返回是否有行被删。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'DELETE FROM friends WHERE (player_id = $1 AND friend_id = $2) '
                'OR (player_id = $2 AND friend_id = $1)',
                int(player_id), int(friend_id))
        return _rowcount(n) > 0

    async def list_friend_rows(self, player_id):
        """好友行（friendinfo KVCS 推送源）：

        双向查询（我发起的 + 收到的），按对方玩家聚合：
        - 任一方向 status=1 → otherPlayerAlsoFriend=true（正式好友）
        - 存在 player_id=我的行 → friendshipInitiatedByMe=true
        收到的待处理邀请：initiated_by_me=false + also_friend=false，
        客户端据此显示在待确认列表。
        返回 [{id, gamertag, faction, also_friend, initiated_by_me}]。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT p.id AS pid, p.username, '
                '       (SELECT faction FROM characters c '
                '        WHERE c.player_id = p.id ORDER BY c.id DESC LIMIT 1) '
                '   AS faction, '
                '       BOOL_OR(f.status = 1) AS confirmed, '
                '       BOOL_OR(f.player_id = $1) AS i_initiated '
                'FROM friends f '
                'JOIN players p ON p.id = '
                '     CASE WHEN f.player_id = $1 THEN f.friend_id '
                '          ELSE f.player_id END '
                'WHERE f.player_id = $1 OR f.friend_id = $1 '
                'GROUP BY p.id, p.username', int(player_id))
        return [{
            'id': int(r['pid']),
            'gamertag': r['username'],
            'faction': int(r['faction'] or 0),
            'also_friend': bool(r['confirmed']),
            'initiated_by_me': bool(r['i_initiated']),
        } for r in rows]

    async def search_players(self, keyword, limit=20, exclude_id=None):
        """按用户名模糊搜索（排除自己）：[username]。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT username FROM players '
                'WHERE username ILIKE $1 AND id <> $2 '
                'ORDER BY username LIMIT $3',
                f'%{str(keyword)}%', int(exclude_id or 0), int(limit))
        return [r['username'] for r in rows]

    async def ignore_player(self, player_id, gamertag):
        """屏蔽玩家。返回 0=Ok 1=AlreadyIgnored 3=Fail（玩家不存在）。"""
        target = await self.get_player_by_gamertag(gamertag)
        if target is None:
            return 3
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT 1 FROM ignored_players WHERE player_id = $1 '
                'AND ignored_player_id = $2', int(player_id), target['id'])
            if row is not None:
                return 1
            await conn.execute(
                'INSERT INTO ignored_players (player_id, ignored_player_id) '
                'VALUES ($1, $2) ON CONFLICT DO NOTHING',
                int(player_id), target['id'])
        return 0

    async def unignore_player(self, player_id, gamertag):
        """取消屏蔽。返回 0=Ok 2=NotCurrentlyIgnored 3=Fail。"""
        target = await self.get_player_by_gamertag(gamertag)
        if target is None:
            return 3
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'DELETE FROM ignored_players WHERE player_id = $1 '
                'AND ignored_player_id = $2', int(player_id), target['id'])
        return 0 if _rowcount(n) > 0 else 2

    async def list_ignored_rows(self, player_id):
        """屏蔽名单（ignoredplayerwithname KVCS 推送源）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT ip.ignored_player_id, p.username '
                'FROM ignored_players ip JOIN players p '
                '  ON p.id = ip.ignored_player_id '
                'WHERE ip.player_id = $1', int(player_id))
        return [{'ignored_id': int(r['ignored_player_id']),
                 'ignoredname': r['username']} for r in rows]

    async def save_player_report(self, reporter_id, reported_player_id, category,
                                 action_game_session=0, chatlogs=''):
        """保存玩家举报（AddPlayerReportRequest）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO player_reports (reporter_id, reported_player_id, '
                'category, action_game_session, chatlogs) '
                'VALUES ($1, $2, $3, $4, $5)',
                int(reporter_id), int(reported_player_id), int(category),
                int(action_game_session), str(chatlogs or ''))

