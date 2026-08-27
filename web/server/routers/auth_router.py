# -*- coding: utf-8 -*-
"""管理 API：登录/登出/会话。"""
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from .. import auth

router = APIRouter(prefix='/api/auth', tags=['auth'])


class LoginBody(BaseModel):
    username: str
    password: str


@router.post('/login')
async def login(body: LoginBody, request: Request, response: Response):
    admin, token = await auth.login(request, body.username.strip(), body.password)
    auth.set_session_cookie(response, token)
    return {'ok': True, 'username': admin.username}


@router.post('/logout')
async def logout(response: Response):
    auth.clear_session_cookie(response)
    return {'ok': True}


@router.get('/me')
async def me(admin: auth.AdminUser = Depends(auth.current_admin)):
    return {'username': admin.username}
