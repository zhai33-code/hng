# -*- coding: utf-8 -*-
"""管理 API：RTS 战争页（概览 / AT / 移动 / 会战 / 城市查询 + 运写操作）。

查询与写操作在 services/war_svc.py；本层只做参数解析、审计与响应。
"""

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from .. import audit, auth, db
from ..services import war_svc
from . import api_errors

router = APIRouter(prefix='/api/war', tags=['war'],
                   dependencies=[Depends(auth.current_admin)])


@router.get('/overview')
async def war_overview():
    """当前战争概览：war 行 + 阵营统计 + 在途/会战计数 + 近期战争史。"""
    return await war_svc.overview(db.pool)


@router.get('/ats')
async def list_ats(page: int = Query(1, ge=1),
                  page_size: int = Query(20, ge=1, le=100),
                  faction: int = Query(0),
                  state: int = Query(-1)):
    """AT 实例分页列表（faction 0=全部；state -1=全部存活）。"""
    return await war_svc.list_ats(db.pool, page=page, page_size=page_size,
                                  faction=faction, state=state)


@router.get('/movements')
async def list_movements():
    """在途 AT 移动列表。"""
    return await war_svc.list_movements(db.pool)


@router.get('/battles')
async def list_battles(war_id: int = Query(0)):
    """会战登记（war_battles）列表（war_id 0=最新战争）。"""
    return await war_svc.list_battles(db.pool, war_id)


@router.get('/cities')
async def list_cities(war_id: int = Query(0)):
    """城市归属列表（war_id 0=最新战争）。"""
    return await war_svc.list_cities(db.pool, war_id)


# ------------------------------------------------------------------ 写操作

@router.post('/end', dependencies=[Depends(auth.write_guard)])
@api_errors
async def force_end_war(request: Request,
                        admin: auth.AdminUser = Depends(auth.current_admin)):
    """强制结束当前战争（游戏服下个 tick 自动续战）。"""
    r = await war_svc.force_end_war(db.pool, admin.username)
    await audit.record(admin, request, 'war_force_end',
                       f'战争 #{r["war_id"]}', r)
    return r


class WarfundsBody(BaseModel):
    player_id: int = Field(gt=0)
    delta: int


@router.post('/warfunds', dependencies=[Depends(auth.write_guard)])
@api_errors
async def adjust_warfunds(body: WarfundsBody, request: Request,
                          admin: auth.AdminUser = Depends(auth.current_admin)):
    """调整玩家 warfunds（正=发放/负=扣回）。"""
    r = await war_svc.adjust_warfunds(db.pool, body.player_id, body.delta,
                                      admin.username)
    await audit.record(admin, request, 'war_adjust_warfunds',
                       f'玩家 #{body.player_id}', r)
    return r


class CreateAtBody(BaseModel):
    owner_player_id: int = Field(gt=0)
    template_id: int = Field(gt=0)
    faction: int = Field(ge=1, le=3)
    name: str = Field(default='', max_length=64)


@router.post('/ats', dependencies=[Depends(auth.write_guard)])
@api_errors
async def create_at(body: CreateAtBody, request: Request,
                    admin: auth.AdminUser = Depends(auth.current_admin)):
    """生成 AT（作弊，不扣款）。"""
    r = await war_svc.create_at(db.pool, owner_player_id=body.owner_player_id,
                                template_id=body.template_id,
                                faction=body.faction, name=body.name)
    await audit.record(admin, request, 'war_create_at',
                       f'AT #{r["id"]}',
                       {**r, 'owner_player_id': body.owner_player_id,
                        'template_id': body.template_id,
                        'faction': body.faction, 'name': body.name})
    return r


@router.delete('/ats/{at_id}', dependencies=[Depends(auth.write_guard)])
@api_errors
async def dissolve_at(at_id: int, request: Request,
                      admin: auth.AdminUser = Depends(auth.current_admin)):
    """解散 AT（作弊，不退款）。"""
    r = await war_svc.dissolve_at(db.pool, at_id)
    await audit.record(admin, request, 'war_dissolve_at',
                       f'AT #{at_id}', r)
    return r
