# -*- coding: utf-8 -*-
"""管理 API：仪表盘 / 战报 / 服务器状态 / 在线统计 / 注册统计 / 审计日志。

各领域逻辑在 services/{battles,status,audit}_svc.py，本层只做参数解析与响应。
"""
import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import auth, db, wardb_meta
from ..services import audit_svc, battles_svc, status_svc
from . import api_errors

router = APIRouter(prefix='/api', tags=['misc'],
                   dependencies=[Depends(auth.current_admin)])

_STARTED_AT = time.time()

# 游戏服端口健康检查（TCP 探测）
_PROBES = (('game_http_80', 80),
           ('game_tcp_15110', 15110),
           ('stats_tcp_15115', 15115))


@router.get('/dashboard')
async def dashboard():
    return await battles_svc.dashboard(db.pool)


@router.get('/battles')
async def battles(page: int = Query(1, ge=1),
                  page_size: int = Query(20, ge=1, le=100)):
    return await battles_svc.list_battles(
        db.pool, page=page, page_size=page_size)


@router.get('/battles/{battle_id}')
@api_errors
async def battle_detail(battle_id: int):
    """战报详情：表头 + 参战玩家（含每兵种收益明细）+ 据点归属终态。"""
    return await battles_svc.battle_detail(db.pool, battle_id)


@router.get('/stats/registrations')
async def registration_stats(days: int = Query(30, ge=1, le=90)):
    return await battles_svc.registration_stats(db.pool, days=days)


@router.get('/status')
async def status():
    """游戏服端口健康检查（TCP 探测 80/15110/15115）+ DB + uptime。"""
    return {
        'uptime_seconds': int(time.time() - _STARTED_AT),
        'database': await status_svc.database_ok(db.pool),
        **{name: status_svc.probe('127.0.0.1', port)
           for name, port in _PROBES},
    }


@router.get('/online')
async def online():
    """实时在线统计：转发游戏服 web_server 的 /online.json。

    数据源是游戏服进程内存（FLASH_SESSIONS + 匹配/战局注册表），
    零延迟无落库；游戏服不可达 / 未更新到含 /online.json 的版本 → 502。
    转发时把战局 preset id 补成官方模式/地图名（与战报页同源
    wardb_meta.preset_info，游戏服快照保持纯数字不动）。"""
    data = await asyncio.to_thread(status_svc.fetch_online_snapshot)
    if data is None:
        raise HTTPException(502, '游戏服在线统计不可达（服务未启动或版本过旧）')
    for m in data.get('missions', []):
        info = wardb_meta.preset_info(int(m.get('preset') or 0))
        m['mode_name'] = info['mode_name']
        m['map_name'] = info['map']
    return data


@router.get('/audit-log')
async def audit_log(page: int = Query(1, ge=1),
                    page_size: int = Query(50, ge=1, le=200),
                    action: str = Query('', max_length=32),
                    admin: str = Query('', max_length=64),
                    q: str = Query('', max_length=64),
                    days: int = Query(0, ge=0, le=365)):
    """审计日志分页（可按操作类型/管理员/目标关键字/近 N 天过滤）。"""
    return await audit_svc.list_audit_log(
        db.pool, page=page, page_size=page_size, action=action,
        admin=admin, q=q, days=days)
