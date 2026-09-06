# -*- coding: utf-8 -*-
"""管理 API：动态配置（server_config 表）读写。

各配置项的校验/读写逻辑在 services/config_svc.py（CONFIG_REGISTRY 注册），
本层只做 key 分发、审计与响应；新增配置项路由层零改动。
"""
from fastapi import APIRouter, Depends, Request

from .. import audit, auth, db
from ..services import config_svc
from . import api_errors

router = APIRouter(prefix='/api/config', tags=['config'],
                   dependencies=[Depends(auth.current_admin)])


def _require_key(key: str) -> str:
    if key not in config_svc.CONFIG_REGISTRY:
        raise LookupError(f'未知配置项：{key}')
    return key


@router.get('/{key}')
@api_errors
async def get_config(key: str):
    """读一项配置（无行返回内置默认 + configured=False）。"""
    return await config_svc.get_config_item(db.pool, _require_key(key))


@router.put('/{key}', dependencies=[Depends(auth.write_guard)])
@api_errors
async def save_config(key: str, body: dict, request: Request,
                      admin: auth.AdminUser = Depends(auth.current_admin)):
    """保存一项配置（UPSERT server_config；游戏服逐局读取，保存即生效）。"""
    value = await config_svc.save_config_item(
        db.pool, _require_key(key), body, admin.username)
    await audit.record(admin, request, 'config_save', key, value)
    return {'ok': True, 'config': value}
