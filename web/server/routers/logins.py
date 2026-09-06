# -*- coding: utf-8 -*-
"""管理 API：登录事件反查（/api/logins）。"""
from fastapi import APIRouter, Depends, Query

from .. import auth, db
from ..services import logins_svc

router = APIRouter(prefix='/api/logins', tags=['logins'],
                   dependencies=[Depends(auth.current_admin)])


@router.get('')
async def search(q: str = Query('', max_length=128),
                 limit: int = Query(200, ge=1, le=500)):
    """按用户名/IP/机器码搜登录事件；附涉及机器码/IP 的全部关联账号。"""
    return await logins_svc.search_login_events(db.pool, q, limit)
