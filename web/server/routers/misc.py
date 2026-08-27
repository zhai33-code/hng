# -*- coding: utf-8 -*-
"""管理 API：仪表盘 / 战报 / 服务器状态 / 注册统计 / 审计日志。"""
import socket
import time

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import auth, templates_data
from .. import db

router = APIRouter(prefix='/api', tags=['misc'],
                   dependencies=[Depends(auth.current_admin)])

_STARTED_AT = time.time()


@router.get('/dashboard')
async def dashboard():
    assert db.pool is not None
    async with db.pool.acquire() as conn:
        players = await conn.fetchval('SELECT count(*) FROM players')
        today = await conn.fetchval(
            "SELECT count(*) FROM players "
            "WHERE created_at >= date_trunc('day', now())")
        characters = await conn.fetchval('SELECT count(*) FROM characters')
        battles = await conn.fetchval('SELECT count(*) FROM battle_reports')
        recent = await conn.fetch(
            'SELECT id, username, created_at FROM players '
            'ORDER BY created_at DESC LIMIT 10')
    return {
        'players': players,
        'today_registrations': today,
        'characters': characters,
        'battles': battles,
        'recent_players': [dict(r) for r in recent],
        'wardb_loaded': len(templates_data.WEAPONS),   # 固化数据条目数（weapons）
    }


@router.get('/battles')
async def battles(page: int = Query(1, ge=1),
                  page_size: int = Query(20, ge=1, le=100)):
    assert db.pool is not None
    async with db.pool.acquire() as conn:
        total = await conn.fetchval('SELECT count(*) FROM battle_reports')
        rows = await conn.fetch(
            'SELECT id, mission_session_id, mission_id, mission_preset_id, '
            'winner_faction_id, defender_faction_id, mission_time_seconds, '
            'battle_start_time, created_at FROM battle_reports '
            'ORDER BY created_at DESC '
            'LIMIT $1 OFFSET $2', page_size, (page - 1) * page_size)
    return {'total': total, 'items': [dict(r) for r in rows]}


@router.get('/battles/{battle_id}')
async def battle_detail(battle_id: int):
    assert db.pool is not None
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT id, mission_session_id, mission_id, mission_preset_id, '
            'winner_faction_id, defender_faction_id, mission_time_seconds, '
            'battle_start_time, created_at FROM battle_reports WHERE id = $1',
            battle_id)
    if row is None:
        raise HTTPException(404, '战报不存在')
    return dict(row)


@router.get('/stats/registrations')
async def registration_stats(days: int = Query(30, ge=1, le=90)):
    assert db.pool is not None
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT to_char(date_trunc('day', created_at), 'YYYY-MM-DD') AS day, "
            "count(*) AS n FROM players "
            "WHERE created_at >= now() - ($1 || ' days')::interval "
            "GROUP BY 1 ORDER BY 1", str(days))
    return {'days': days, 'items': [{'day': r['day'], 'count': r['n']}
                                    for r in rows]}


@router.get('/status')
async def status():
    """游戏服端口健康检查（TCP 探测 80/15110/15115）+ DB + uptime。"""

    def probe(host: str, port: int, timeout: float = 2.0) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    db_ok = False
    if db.pool is not None:
        try:
            async with db.pool.acquire() as conn:
                db_ok = await conn.fetchval('SELECT 1') == 1
        except Exception:
            db_ok = False
    return {
        'uptime_seconds': int(time.time() - _STARTED_AT),
        'database': db_ok,
        'game_http_80': probe('127.0.0.1', 80),
        'game_tcp_15110': probe('127.0.0.1', 15110),
        'stats_tcp_15115': probe('127.0.0.1', 15115),
    }


@router.get('/audit-log')
async def audit_log(page: int = Query(1, ge=1),
                    page_size: int = Query(50, ge=1, le=200)):
    assert db.pool is not None
    async with db.pool.acquire() as conn:
        total = await conn.fetchval('SELECT count(*) FROM admin_audit_log')
        rows = await conn.fetch(
            'SELECT id, admin_name, action, target, detail, ip, created_at '
            'FROM admin_audit_log ORDER BY id DESC '
            'LIMIT $1 OFFSET $2', page_size, (page - 1) * page_size)
    return {'total': total,
            'items': [{'id': r['id'], 'admin_name': r['admin_name'],
                       'action': r['action'], 'target': r['target'],
                       'detail': dict(r['detail']), 'ip': r['ip'],
                       'created_at': r['created_at']} for r in rows]}
