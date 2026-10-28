# -*- coding: utf-8 -*-
"""系统邮件领域服务（管理端单发 / 全服群发）。

写入 mail_messages（sender_id=0 系统邮件），游戏内客户端
GetMailMessagesRequest 请求式读取，无需游戏服推送配合。
thread_id 语义与游戏服 send_mail 一致：新话题指向消息自身 id。
"""


async def send_mail(pool, *, player_id: int, subject: str, text: str) -> int:
    """发送系统邮件（player_id=0 为全服），返回收件人数。"""
    subject = subject.strip()
    text = text.strip()
    if not (1 <= len(subject) <= 128):
        raise ValueError('标题长度需为 1-128 字符')
    if not (1 <= len(text) <= 4000):
        raise ValueError('正文长度需为 1-4000 字符')
    assert pool is not None
    async with pool.acquire() as conn:
        async with conn.transaction():
            if player_id:
                row = await conn.fetchrow(
                    'SELECT username FROM players '
                    'WHERE id = $1 AND deleted_at IS NULL', player_id)
                if row is None:
                    raise LookupError('玩家不存在（或已删除）')
                recipients = [player_id]
            else:
                ids = await conn.fetch(
                    'SELECT id FROM players WHERE deleted_at IS NULL')
                if not ids:
                    raise ValueError('无可用收件人')
                recipients = [int(r['id']) for r in ids]
            # 批量插入系统邮件（sender_id=0；thread_id = 首条消息自身 id，
            # 与游戏服 send_mail 的新话题语义一致）
            for rid in recipients:
                mid = await conn.fetchval(
                    'INSERT INTO mail_messages '
                    '(sender_id, recipient_id, subject, body) '
                    'VALUES (0, $1, $2, $3) RETURNING id',
                    rid, subject, text)
                await conn.execute(
                    'UPDATE mail_messages SET thread_id = $1 WHERE id = $1', mid)
            # 通知游戏服推送（LISTEN hng_new_mail，COMMIT 后送达）：
            # payload=收件人 id 列表（单发定向推）；全服群发收件人可能
            # 超过 NOTIFY payload 8000 字节上限，超长降级为空 payload
            # （游戏服按"推所有在线会话差量"兜底）
            payload = ','.join(str(r) for r in recipients)
            if len(payload) > 8000:
                payload = ''
            await conn.execute('SELECT pg_notify($1, $2)',
                               'hng_new_mail', payload)
    return len(recipients)
