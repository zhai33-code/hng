# -*- coding: utf-8 -*-
"""举报领域服务：列表查询 / 标记处理。

数据源 player_reports 由游戏服 AddPlayerReportRequest 落库；
resolved_at/resolved_by 为管理端专用列（db.py 迁移追加）。
"""

# 客户端 EPlayerReportCategory 枚举（swf HnG_States/EPlayerReportCategory.as）
CATEGORIES = {1: '挂机', 2: '骚扰辱骂', 3: '作弊', 4: '恶意破坏'}


def category_name(cid: int) -> str:
    return CATEGORIES.get(cid, f'未知({cid})')


async def list_reports(pool, *, page: int, page_size: int, state: str,
                       category: int) -> dict:
    """举报分页列表（state=pending/resolved/all；category 0=全部）。

    reporter / reported 双侧 LEFT JOIN players 取用户名
    （角色可能已被硬删，删后名字留空）。
    """
    assert pool is not None
    conds: list[str] = []
    args: list = []
    if state == 'pending':
        conds.append('r.resolved_at IS NULL')
    elif state == 'resolved':
        conds.append('r.resolved_at IS NOT NULL')
    if category:
        args.append(category)
        conds.append(f'r.category = ${len(args)}')
    where = ('WHERE ' + ' AND '.join(conds)) if conds else ''
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            f'SELECT count(*) FROM player_reports r {where}', *args)
        rows = await conn.fetch(
            'SELECT r.id, r.reporter_id, r.reported_player_id, r.category, '
            'r.action_game_session, r.chatlogs, r.created_at, '
            'r.resolved_at, r.resolved_by, '
            'rep.username AS reporter_name, '
            'ted.username AS reported_name, ted.banned AS reported_banned '
            'FROM player_reports r '
            'LEFT JOIN players rep ON rep.id = r.reporter_id '
            'LEFT JOIN players ted ON ted.id = r.reported_player_id '
            f'{where} ORDER BY r.created_at DESC '
            f'LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}',
            *args, page_size, (page - 1) * page_size)
    return {
        'total': total,
        'items': [{
            'id': r['id'],
            'reporter_id': r['reporter_id'],
            'reporter_name': r['reporter_name'] or '',
            'reported_player_id': r['reported_player_id'],
            'reported_name': r['reported_name'] or '',
            'reported_banned': r['reported_banned'],
            'category': r['category'],
            'category_name': category_name(int(r['category'])),
            'action_game_session': r['action_game_session'],
            'chatlogs': r['chatlogs'],
            'created_at': r['created_at'],
            'resolved_at': r['resolved_at'],
            'resolved_by': r['resolved_by'],
        } for r in rows],
    }


async def resolve_report(pool, report_id: int, resolved: bool,
                         admin_username: str) -> int:
    """标记举报已处理 / 重新打开，返回 reported_player_id（供审计）。"""
    assert pool is not None
    async with pool.acquire() as conn:
        if resolved:
            row = await conn.fetchrow(
                'UPDATE player_reports SET resolved_at = now(), resolved_by = $1 '
                'WHERE id = $2 AND resolved_at IS NULL RETURNING reported_player_id',
                admin_username, report_id)
        else:
            row = await conn.fetchrow(
                'UPDATE player_reports SET resolved_at = NULL, resolved_by = \'\' '
                'WHERE id = $1 AND resolved_at IS NOT NULL RETURNING reported_player_id',
                report_id)
    if row is None:
        raise LookupError('举报不存在（或状态未变化）')
    return int(row['reported_player_id'])
