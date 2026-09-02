# -*- coding: utf-8 -*-
"""审计日志领域服务：分页查询（操作类型/管理员/目标关键字/近 N 天过滤）。"""


async def list_audit_log(pool, *, page: int, page_size: int, action: str,
                         admin: str, q: str, days: int) -> dict:
    assert pool is not None
    conds: list[str] = []
    args: list = []
    if action:
        args.append(action)
        conds.append(f'action = ${len(args)}')
    if admin:
        args.append(f'%{admin}%')
        conds.append(f'admin_name ILIKE ${len(args)}')
    if q:
        args.append(f'%{q}%')
        conds.append(f'target ILIKE ${len(args)}')
    if days:
        args.append(str(days))
        conds.append(f"created_at >= now() - (${len(args)} || ' days')::interval")
    where = ('WHERE ' + ' AND '.join(conds)) if conds else ''
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            f'SELECT count(*) FROM admin_audit_log {where}', *args)
        rows = await conn.fetch(
            f'SELECT id, admin_name, action, target, detail, ip, created_at '
            f'FROM admin_audit_log {where} ORDER BY id DESC '
            f'LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}',
            *args, page_size, (page - 1) * page_size)
    return {'total': total,
            'items': [{'id': r['id'], 'admin_name': r['admin_name'],
                       'action': r['action'], 'target': r['target'],
                       'detail': dict(r['detail']), 'ip': r['ip'],
                       'created_at': r['created_at']} for r in rows]}
