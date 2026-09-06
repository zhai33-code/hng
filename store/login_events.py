# -*- coding: utf-8 -*-
"""登录事件审计（login_events 表）：game 协议登录 + web 注册/导入。

IP 取 TCP peer（game）或 client_address（web）；机器码取客户端
QueryBannedMachineRequest 上报的 machineIdentifier/machineIdentifierOld。
成功与失败都记录，供撞库尝试与同机小号排查。
"""


class LoginEventsMixin:
    """登录事件审计：login_events 表写入与查询。"""

    async def record_login_event(self, player_id, username, ip, result,
                                 machine_id='', machine_id_old='',
                                 source='game'):
        """记录一次登录/注册/导入事件。

        result 与协议 ResponseCodes 一致：97=login_success、98=wrong_password、
        91=user_ban_permanent、92=user_ban_temporary（封禁拦截）。
        player_id 可空（登录失败查不到 id）；表无外键，删号后审计记录保留。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO login_events '
                '(player_id, username, ip, machine_id, machine_id_old, '
                ' result, source) VALUES ($1, $2, $3, $4, $5, $6, $7)',
                int(player_id) if player_id is not None else None,
                username or '', ip, machine_id or '', machine_id_old or '',
                int(result), source)

    async def get_login_events(self, username=None, player_id=None, ip=None,
                               machine_id=None, limit=50):
        """按任意组合查询登录事件（时间倒序）。参数全空 = 最近事件。"""
        conds, args = [], []
        if player_id is not None:
            args.append(int(player_id))
            conds.append(f'player_id = ${len(args)}')
        if username is not None:
            args.append(username)
            conds.append(f'username = ${len(args)}')
        if ip is not None:
            args.append(ip)
            conds.append(f'ip = ${len(args)}')
        if machine_id is not None:
            args.append(machine_id)
            conds.append(f'machine_id = ${len(args)}')
        where = ('WHERE ' + ' AND '.join(conds)) if conds else ''
        args.append(max(1, min(int(limit), 500)))
        sql = (f'SELECT id, player_id, username, ip, machine_id, machine_id_old, '
               f'result, source, created_at FROM login_events {where} '
               f'ORDER BY created_at DESC, id DESC LIMIT ${len(args)}')
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [dict(r) for r in rows]
