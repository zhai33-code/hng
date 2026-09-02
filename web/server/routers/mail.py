# -*- coding: utf-8 -*-
"""管理 API：管理员发邮件（单发 / 全服系统邮件）。

写入与收件人解析在 services/mail_svc.py；本层只做参数解析、审计与响应。
"""
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from .. import audit, auth, db
from ..services import mail_svc
from . import api_errors

router = APIRouter(prefix='/api/mail', tags=['mail'],
                   dependencies=[Depends(auth.current_admin)])


class SendMailBody(BaseModel):
    player_id: int = 0        # 0 = 全服（所有未删除账号）
    subject: str
    body: str


@router.post('/send', dependencies=[Depends(auth.write_guard)])
@api_errors
async def send_mail(body: SendMailBody, request: Request,
                    admin: auth.AdminUser = Depends(auth.current_admin)):
    count = await mail_svc.send_mail(
        db.pool, player_id=body.player_id, subject=body.subject,
        text=body.body)
    target = (f'玩家 {body.player_id}' if body.player_id
              else f'全服（{count} 人）')
    await audit.record(admin, request, 'send_mail', target,
                       {'subject': body.subject.strip(),
                        'recipients': count})
    return {'ok': True, 'count': count}
