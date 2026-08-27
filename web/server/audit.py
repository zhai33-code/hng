# -*- coding: utf-8 -*-
"""审计日志：全部管理写操作留痕（谁/何时/做了什么/目标/变动值/IP）。"""
import json

from .auth import AdminUser
from .db import pool


async def record(admin: AdminUser, request, action: str,
                 target: str = '', detail: dict | None = None):
    ip = request.client.host if request.client else ''
    assert pool is not None
    async with pool.acquire() as conn:
        await conn.execute(
            'INSERT INTO admin_audit_log (admin_id, admin_name, action, target, '
            'detail, ip) VALUES ($1, $2, $3, $4, $5::jsonb, $6)',
            admin.id, admin.username, action, target,
            json.dumps(detail or {}, ensure_ascii=False), ip)
