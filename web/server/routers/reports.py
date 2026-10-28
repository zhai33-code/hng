# -*- coding: utf-8 -*-
"""管理 API：玩家举报处理（列表/过滤/标记已处理）。

查询与状态变更在 services/reports_svc.py；本层只做参数解析、审计与响应。
"""
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from .. import audit, auth, db
from ..services import reports_svc
from . import api_errors

router = APIRouter(prefix='/api/reports', tags=['reports'],
                   dependencies=[Depends(auth.current_admin)])


@router.get('')
async def list_reports(page: int = Query(1, ge=1),
                       page_size: int = Query(20, ge=1, le=100),
                       state: str = Query('pending'),
                       category: int = Query(0)):
    """举报分页列表（state=pending/resolved/all；category 0=全部）。"""
    return await reports_svc.list_reports(
        db.pool, page=page, page_size=page_size, state=state,
        category=category)


class ResolveBody(BaseModel):
    resolved: bool = True


@router.post('/{report_id}/resolve',
             dependencies=[Depends(auth.write_guard)])
@api_errors
async def resolve_report(report_id: int, body: ResolveBody,
                         request: Request,
                         admin: auth.AdminUser = Depends(auth.current_admin)):
    """标记举报已处理 / 重新打开。"""
    reported_id = await reports_svc.resolve_report(
        db.pool, report_id, body.resolved, admin.username)
    await audit.record(admin, request,
                       'resolve_report' if body.resolved else 'reopen_report',
                       f'举报 #{report_id}',
                       {'reported_player_id': reported_id})
    return {'ok': True, 'resolved': body.resolved}
