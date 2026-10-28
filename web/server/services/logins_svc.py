# -*- coding: utf-8 -*-
"""登录事件反查（login_events 表）：按用户名 / IP / 机器码搜全局登录记录。

供管理端"登录记录"页使用；响应附带涉及机器码/IP 上登过的全部账号名
（小号排查：某台机器 / 某个 IP 一共登过哪些号）。
"""
import ipaddress


def _is_ip(q: str) -> bool:
    try:
        ipaddress.ip_address(q)
        return True
    except ValueError:
        return False


async def search_login_events(pool, q: str, limit: int = 200,
                              page: int = 1) -> dict:
    """按关键字搜登录事件（用户名包含 / IP 精确 / 机器码包含）。

    附 machines/ips 汇总：匹配行涉及的每个机器码/IP 上登过的全部用户名。
    关键字为空时返回最近事件（最近优先）。
    分页：limit=页大小、page 从 1 起；响应 total=匹配总行数，
    rows/machines/ips 仅反映当前页。"""
    assert pool is not None
    q = (q or '').strip()
    args: list = []
    where = ''
    if q:
        args.append(f'%{q}%')
        args.append(q if _is_ip(q) else None)
        where = 'WHERE username ILIKE $1 OR machine_id ILIKE $1 OR ip = $2'
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            f'SELECT count(*) FROM login_events {where}', *args)
        rows = await conn.fetch(
            'SELECT id, player_id, username, ip, machine_id, '
            'machine_id_old, result, source, created_at FROM login_events '
            f'{where} ORDER BY id DESC '
            f'LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}',
            *args, limit, (page - 1) * limit)
        # 涉及机器码/IP 上登过的全部账号（含不在匹配行里的）
        machine_ids = [r['machine_id'] for r in rows if r['machine_id']]
        ips = list({r['ip'] for r in rows if r['ip']})
        machines: dict[str, list[str]] = {}
        ips_map: dict[str, list[str]] = {}
        if machine_ids:
            m_rows = await conn.fetch(
                'SELECT DISTINCT machine_id, username FROM login_events '
                'WHERE machine_id = ANY($1)', machine_ids)
            for r in m_rows:
                machines.setdefault(r['machine_id'], []).append(r['username'])
        if ips:
            i_rows = await conn.fetch(
                'SELECT DISTINCT ip, username FROM login_events '
                'WHERE ip = ANY($1) AND ip <> $2', ips, '127.0.0.1')
            for r in i_rows:
                ips_map.setdefault(r['ip'], []).append(r['username'])
    return {
        'total': total,
        'rows': [dict(r) for r in rows],
        'machines': {k: sorted(v) for k, v in machines.items()},
        'ips': {k: sorted(v) for k, v in ips_map.items()},
    }
