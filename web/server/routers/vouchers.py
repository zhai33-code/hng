# -*- coding: utf-8 -*-
"""管理 API：兑换码管理 v2（批量生成 / 列表查询）。

体系设计见 docs/voucher-redemption-design.md；
生成与查询逻辑在 services/vouchers_svc.py，本层只做参数解析、审计与响应。
"""
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from .. import audit, auth, db
from ..services import vouchers_svc
from . import api_errors

router = APIRouter(prefix='/api/vouchers', tags=['vouchers'],
                   dependencies=[Depends(auth.current_admin)])


class ItemSpec(BaseModel):
    type: str
    id: int = 0        # 道具模板 id（道具类型必填）
    amount: int = 0    # 数量（货币类型必填）


class GenerateBody(BaseModel):
    count: int = 1
    items: list[ItemSpec] = []
    expires_days: int = 0      # 0 = 永久
    max_redemptions: int = 1   # 1 = 一码一用；N = 限量 N 次；0 = 无限次
    note: str = ''


@router.post('/generate', dependencies=[Depends(auth.write_guard)])
@api_errors
async def generate(body: GenerateBody, request: Request,
                   admin: auth.AdminUser = Depends(auth.current_admin)):
    """批量生成兑换码（同批内容相同；一码限一人，批次内多码各归一人）。"""
    result = await vouchers_svc.generate(
        db.pool,
        count=body.count,
        items=[{'type': it.type, 'id': it.id, 'amount': it.amount}
               for it in body.items],
        expires_days=body.expires_days,
        max_redemptions=body.max_redemptions,
        note=body.note)
    await audit.record(admin, request, 'generate_vouchers',
                       f'{body.count} 个兑换码',
                       {'count': body.count, 'items': result['items'],
                        'expires_days': body.expires_days,
                        'max_redemptions': body.max_redemptions,
                        'note': result['note']})
    return {'ok': True, 'count': len(result['codes']),
            'codes': result['codes']}


@router.get('')
async def list_vouchers(page: int = Query(1, ge=1),
                        page_size: int = Query(20, ge=1, le=100),
                        state: str = Query('all')):
    """兑换码分页列表（state=unused/used/exhausted/expired/all）。"""
    return await vouchers_svc.list_vouchers(
        db.pool, page=page, page_size=page_size, state=state)
