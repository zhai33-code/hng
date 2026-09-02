# -*- coding: utf-8 -*-
"""管理 API：登录/登出/会话。"""
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from .. import audit, auth

router = APIRouter(prefix='/api/auth', tags=['auth'])


class LoginBody(BaseModel):
    username: str
    password: str


@router.post('/login')
async def login(body: LoginBody, request: Request, response: Response):
    username = body.username.strip()
    try:
        admin, token = await auth.login(request, username, body.password)
    except HTTPException as e:
        # 凭证错误（401）入审计：admin_id=0 + 尝试的用户名，留爆破痕迹
        # （429 限速拒登不记——此前的 5 次失败已在案）
        if e.status_code == 401:
            try:
                await audit.record(auth.AdminUser(0, username[:64]), request,
                                   'login_fail')
            except Exception:
                pass
        raise
    auth.set_session_cookie(response, token)
    try:
        await audit.record(admin, request, 'login_ok')
    except Exception:
        pass    # 审计写失败不阻断登录
    return {'ok': True, 'username': admin.username}


@router.post('/logout')
async def logout(response: Response):
    auth.clear_session_cookie(response)
    return {'ok': True}


@router.get('/me')
async def me(admin: auth.AdminUser = Depends(auth.current_admin)):
    return {'username': admin.username}
