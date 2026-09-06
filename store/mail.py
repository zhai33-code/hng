# -*- coding: utf-8 -*-
"""站内信发送与收件箱。
拆分自 player_store.py（纯移动重构，方法体逐字保留）。
"""
from .base import _rowcount

class MailMixin:
    """站内信发送与收件箱。"""

    # ------------------------------------------------------------- 邮件

    async def send_mail(self, sender_id, recipient_ids, subject, body, reply_to=0):
        """发送邮件（每个收件人一行）。返回 [message_id]。"""
        ids = []
        async with self.pool.acquire() as conn:
            for rid in recipient_ids:
                mid = await conn.fetchval(
                    'INSERT INTO mail_messages (sender_id, recipient_id, subject, '
                    'body, reply_to) VALUES ($1, $2, $3, $4, $5) RETURNING id',
                    int(sender_id or 0), int(rid), str(subject or ''),
                    str(body or ''), int(reply_to or 0))
                # 新话题：thread_id 指向首条消息；回复：继承原线程
                if int(reply_to or 0) == 0:
                    await conn.execute(
                        'UPDATE mail_messages SET thread_id = $1 WHERE id = $1', mid)
                else:
                    await conn.execute(
                        'UPDATE mail_messages SET thread_id = '
                        'COALESCE(NULLIF((SELECT thread_id FROM mail_messages '
                        'WHERE id = $2), 0), $2) WHERE id = $1',
                        mid, int(reply_to))
                ids.append(int(mid))
        return ids

    async def get_mail_threads(self, player_id, message_ids=None, limit=20):
        """收件箱线程组装（MessageThread KVCS 源）。

        返回 [{thread_id, messages: [{id, thread_id, reply_to, sender, subject,
        body, created_at, sender_name}], recipients: [player_id]}]。"""
        async with self.pool.acquire() as conn:
            if message_ids:
                rows = await conn.fetch(
                    'SELECT m.*, p.username AS sender_name FROM mail_messages m '
                    'LEFT JOIN players p ON p.id = m.sender_id '
                    'WHERE m.recipient_id = $1 AND m.id = ANY($2::bigint[]) '
                    'ORDER BY m.created_at DESC LIMIT $3',
                    int(player_id), [int(x) for x in message_ids], int(limit))
            else:
                rows = await conn.fetch(
                    'SELECT m.*, p.username AS sender_name FROM mail_messages m '
                    'LEFT JOIN players p ON p.id = m.sender_id '
                    'WHERE m.recipient_id = $1 AND NOT m.deleted '
                    'ORDER BY m.created_at DESC LIMIT $2',
                    int(player_id), int(limit))
        threads = {}
        for r in rows:
            tid = int(r['thread_id'] or r['id'])
            t = threads.setdefault(tid, {'thread_id': tid, 'messages': []})
            t['messages'].append({
                'id': int(r['id']),
                'thread_id': tid,
                'reply_to': int(r['reply_to'] or 0),
                'sender': int(r['sender_id'] or 0),
                'sender_name': r['sender_name'] or '',
                'subject': r['subject'],
                'body': r['body'],
                'created_at': r['created_at'],
                'is_read': bool(r['is_read']),
            })
        return list(threads.values())

    async def set_mail_flag(self, player_id, message_ids, column, value):
        """设置邮件标记（is_read / deleted），仅限本人邮件。返回更新行数。"""
        assert column in ('is_read', 'deleted')
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                f'UPDATE mail_messages SET {column} = $3 '
                'WHERE recipient_id = $1 AND id = ANY($2::bigint[])',
                int(player_id), [int(x) for x in message_ids], bool(value))
        return _rowcount(n)

    async def list_mail_recipient_rows(self, player_id, limit=200):
        """收件箱行（客户端 MessageRecipient KVCS 表的数据源）。

        本库一封邮件一行（send_mail 按收件人各插一行），因此
        MessageRecipient.id = messageId = mail_messages.id；
        客户端读写/删除标记请求回写的也是这组 id（set_mail_flag）。"""
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                'SELECT id, is_read, deleted FROM mail_messages '
                'WHERE recipient_id = $1 ORDER BY id DESC LIMIT $2',
                int(player_id), int(limit))

    async def get_mail_rows(self, player_id, message_ids):
        """按 id 取本人邮件行（标记回推用），返回与 list_mail_recipient_rows
        同构的行（id/is_read/deleted）。"""
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                'SELECT id, is_read, deleted FROM mail_messages '
                'WHERE recipient_id = $1 AND id = ANY($2::bigint[]) '
                'ORDER BY id',
                int(player_id), [int(x) for x in message_ids])

