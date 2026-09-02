# -*- coding: utf-8 -*-
"""管理 API：玩家列表/详情、角色装备明细、玩家操作（重置密码/货币/封禁）。

SQL 与组装在 services/players_svc.py；本层只做参数解析、审计与响应。
"""
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from .. import audit, auth, db
from ..services import players_svc
from . import api_errors

router = APIRouter(prefix='/api/players', tags=['players'],
                   dependencies=[Depends(auth.current_admin)])


@router.get('')
async def list_players(page: int = Query(1, ge=1),
                       page_size: int = Query(20, ge=1, le=100),
                       q: str = Query('', max_length=64),
                       order: str = Query('created_desc'),
                       state: str = Query('active')):
    """玩家分页列表（q 按用户名包含匹配；state=active/deleted/all 软删除筛选）。"""
    return await players_svc.list_players(
        db.pool, page=page, page_size=page_size, q=q, order=order, state=state)


class BatchDeleteBody(BaseModel):
    ids: list[int]
    hard: bool = False


@router.post('/batch-delete', dependencies=[Depends(auth.write_guard)])
@api_errors
async def batch_delete(body: BatchDeleteBody, request: Request,
                       admin: auth.AdminUser = Depends(auth.current_admin)):
    """批量删除账号（多选；软删除可恢复，硬删除级联清关联数据）。"""
    result = await players_svc.batch_delete(db.pool, body.ids, body.hard)
    await audit.record(admin, request,
                       'hard_delete' if body.hard else 'soft_delete',
                       ', '.join(result['usernames']),
                       {'count': result['count'], 'ids': body.ids})
    return {'ok': True, **result}


@router.get('/{player_id}')
@api_errors
async def player_detail(player_id: int):
    """玩家账号级详情：账号信息 + 三货币 + 角色概览 + 该玩家最近参战记录。"""
    return await players_svc.player_detail(db.pool, player_id)


@router.get('/{player_id}/characters/{character_id}')
@api_errors
async def character_detail(player_id: int, character_id: int):
    """角色装备明细：武器（含配件/弹药）、载具（含升级件）、勋带、徽章。"""
    return await players_svc.character_detail(db.pool, player_id, character_id)


# ---------------- 玩家操作（写操作：write_guard CSRF + 审计）----------------

class ResetPasswordBody(BaseModel):
    new_password: str


@router.post('/{player_id}/reset-password',
             dependencies=[Depends(auth.write_guard)])
@api_errors
async def reset_password(player_id: int, body: ResetPasswordBody,
                         request: Request,
                         admin: auth.AdminUser = Depends(auth.current_admin)):
    username = await players_svc.reset_password(
        db.pool, player_id, body.new_password)
    await audit.record(admin, request, 'reset_password', username)
    return {'ok': True}


class CurrencyBody(BaseModel):
    currency: str          # credits | warfunds | gold
    delta: int             # 正发放/负扣减


@router.post('/{player_id}/currency', dependencies=[Depends(auth.write_guard)])
@api_errors
async def adjust_currency(player_id: int, body: CurrencyBody,
                          request: Request,
                          admin: auth.AdminUser = Depends(auth.current_admin)):
    result = await players_svc.adjust_currency(
        db.pool, player_id, body.currency, body.delta)
    await audit.record(admin, request, 'adjust_currency', result['username'],
                       {'currency': body.currency, 'delta': body.delta,
                        'after': result['balance']})
    return {'ok': True, 'balance': result['balance']}


class BanBody(BaseModel):
    banned: bool


@router.post('/{player_id}/ban', dependencies=[Depends(auth.write_guard)])
@api_errors
async def set_ban(player_id: int, body: BanBody,
                  request: Request,
                  admin: auth.AdminUser = Depends(auth.current_admin)):
    result = await players_svc.set_ban(db.pool, player_id, body.banned)
    await audit.record(admin, request, 'set_ban', result['username'],
                       {'banned': body.banned})
    return {'ok': True, 'banned': body.banned}
