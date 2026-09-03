# -*- coding: utf-8 -*-
"""战争（RTS 层）领域服务：战争概览 / AT 列表 / 在途移动 / 会战 / 城市归属。

数据源为游戏服 war_campaign / handlers 创建的表（wars / war_cities /
command_nodes / commandnode_movements / war_battles），管理端只读展示；
表由游戏服建（管理端可能先于游戏服启动）→ 查询前探测存在性，缺表返回
空结果 + rts_ready=False，不报错。

faction id 空间 = factiontemplateid 恒等映射 1/2/3（project_rules 硬约束）。
AT state：0=待命 1=移动中 2=会战中 3=已解散。
map_entity_type：1=城市(battlefield) 2=出兵点(accesspoint) 3=补给线(在途)。
"""

from ..templates_data import FACTIONS

AT_STATE_NAMES = {0: '待命', 1: '移动中', 2: '会战中', 3: '已解散'}
MOVEMENT_STATE_NAMES = {0: '在途', 1: '已到达', 2: '已取消'}
ENTITY_TYPE_NAMES = {1: '城市', 2: '出兵点', 3: '补给线'}

_RTS_TABLES = ('wars', 'war_cities', 'command_nodes', 'commandnode_movements',
               'war_battles')


def faction_name(fid: int) -> str:
    return FACTIONS.get(int(fid or 0), f'未知({fid})')


async def _tables_ready(conn) -> bool:
    """RTS 表是否已由游戏服创建（管理端可能先启动）。"""
    for t in _RTS_TABLES:
        if await conn.fetchval('SELECT to_regclass($1)', f'public.{t}') is None:
            return False
    return True


async def overview(pool) -> dict:
    """当前战争概览：war 行 + 阵营城市/AT 统计 + 在途/会战计数 + 近期战争史。"""
    assert pool is not None
    async with pool.acquire() as conn:
        if not await _tables_ready(conn):
            return {'rts_ready': False}
        war = await conn.fetchrow(
            'SELECT id, name, begintime, endtime, winning_faction_id, '
            'points_needed_for_victory, sequel_war_id, mapid, state, '
            'config_json FROM wars WHERE state = 1 ORDER BY id DESC LIMIT 1')
        if war is None:
            war = await conn.fetchrow(
                'SELECT id, name, begintime, endtime, winning_faction_id, '
                'points_needed_for_victory, sequel_war_id, mapid, state, '
                'config_json FROM wars ORDER BY id DESC LIMIT 1')
        wid = war['id'] if war else 0
        city_counts = {int(r['owner_faction']): int(r['n'])
                       for r in await conn.fetch(
                           'SELECT owner_faction, count(*) AS n FROM war_cities '
                           'WHERE war_id = $1 GROUP BY owner_faction', wid)} \
            if wid else {}
        at_rows = await conn.fetch(
            'SELECT faction, state, count(*) AS n FROM command_nodes '
            'WHERE war_id = $1 AND state < 3 GROUP BY faction, state', wid) \
            if wid else []
        at_stats: dict[int, dict] = {}
        for r in at_rows:
            fid = int(r['faction'])
            s = at_stats.setdefault(fid, {'total': 0, 'idle': 0, 'moving': 0,
                                           'battle': 0})
            s['total'] += int(r['n'])
            if int(r['state']) == 0:
                s['idle'] += int(r['n'])
            elif int(r['state']) == 1:
                s['moving'] += int(r['n'])
            elif int(r['state']) == 2:
                s['battle'] += int(r['n'])
        moving = await conn.fetchval(
            'SELECT count(*) FROM commandnode_movements '
            'WHERE war_id = $1 AND state = 0', wid) if wid else 0
        battles = await conn.fetchval(
            'SELECT count(*) FROM war_battles '
            'WHERE war_id = $1 AND state = 0', wid) if wid else 0
        history = await conn.fetch(
            'SELECT id, name, begintime, endtime, winning_faction_id, '
            'points_needed_for_victory, sequel_war_id, state '
            'FROM wars ORDER BY id DESC LIMIT 10')
        return {
            'rts_ready': True,
            'war': dict(war) if war else None,
            'city_counts': {str(k): v for k, v in city_counts.items()},
            'at_stats': {str(k): v for k, v in at_stats.items()},
            'active_movements': int(moving or 0),
            'active_battles': int(battles or 0),
            'history': [{
                'id': h['id'],
                'name': h['name'],
                'begintime': h['begintime'],
                'endtime': h['endtime'],
                'winning_faction_id': h['winning_faction_id'],
                'winning_faction': faction_name(h['winning_faction_id'])
                if h['winning_faction_id'] else '',
                'points_needed_for_victory': h['points_needed_for_victory'],
                'sequel_war_id': h['sequel_war_id'],
                'state': h['state'],
            } for h in history],
        }


async def list_ats(pool, *, page: int, page_size: int, faction: int,
                   state: int) -> dict:
    """AT（Assault Team）实例分页列表。

    faction 0=全部；state -1=全部存活（默认只看存活 state<3）。
    """
    assert pool is not None
    conds = ['c.war_id = (SELECT id FROM wars ORDER BY id DESC LIMIT 1)']
    args: list = []
    if faction:
        args.append(faction)
        conds.append(f'c.faction = ${len(args)}')
    if state >= 0:
        args.append(state)
        conds.append(f'c.state = ${len(args)}')
    else:
        conds.append('c.state < 3')
    where = ' AND '.join(conds)
    async with pool.acquire() as conn:
        if not await _tables_ready(conn):
            return {'rts_ready': False, 'total': 0, 'items': []}
        total = await conn.fetchval(
            f'SELECT count(*) FROM command_nodes c WHERE {where}', *args)
        rows = await conn.fetch(
            'SELECT c.id, c.name, c.abbreviation, c.template_id, c.faction, '
            'c.owner_player_id, p.username AS owner_name, c.upgrade_level, '
            'c.rank, c.xp, c.morale, c.max_morale, c.state, c.map_entity_id, '
            'c.map_entity_type, c.is_deployed, c.resources, c.created_at '
            'FROM command_nodes c '
            'LEFT JOIN players p ON p.id = c.owner_player_id '
            f'WHERE {where} ORDER BY c.id '
            f'LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}',
            *args, page_size, (page - 1) * page_size)
    return {
        'rts_ready': True,
        'total': total,
        'items': [{
            'id': r['id'],
            'name': r['name'],
            'abbreviation': r['abbreviation'],
            'template_id': r['template_id'],
            'faction': r['faction'],
            'faction_name': faction_name(r['faction']),
            'owner_player_id': r['owner_player_id'],
            'owner_name': r['owner_name'] or '',
            'upgrade_level': r['upgrade_level'],
            'rank': r['rank'],
            'xp': r['xp'],
            'morale': r['morale'],
            'max_morale': r['max_morale'],
            'state': r['state'],
            'state_name': AT_STATE_NAMES.get(int(r['state']), '?'),
            'map_entity_id': r['map_entity_id'],
            'map_entity_type': r['map_entity_type'],
            'map_entity_type_name': ENTITY_TYPE_NAMES.get(
                int(r['map_entity_type'] or 0), '未知'),
            'is_deployed': r['is_deployed'],
            'resources': r['resources'],
            'created_at': r['created_at'],
        } for r in rows],
    }


async def list_movements(pool) -> dict:
    """在途 AT 移动（state=0），按预计到达排序。"""
    assert pool is not None
    async with pool.acquire() as conn:
        if not await _tables_ready(conn):
            return {'rts_ready': False, 'items': []}
        rows = await conn.fetch(
            'SELECT m.id, m.commandnode_id, c.name AS at_name, c.faction, '
            'm.war_id, m.path_json, m.started_at, m.arrive_at, m.state, '
            'm.dest_ap FROM commandnode_movements m '
            'JOIN command_nodes c ON c.id = m.commandnode_id '
            'WHERE m.state = 0 ORDER BY m.arrive_at')
    items = []
    for r in rows:
        path = r['path_json'] if isinstance(r['path_json'], list) else []
        dest_bf = path[-1]['to_bf'] if path else 0
        items.append({
            'id': r['id'],
            'commandnode_id': r['commandnode_id'],
            'at_name': r['at_name'],
            'faction': r['faction'],
            'faction_name': faction_name(r['faction']),
            'dest_bf': dest_bf,
            'dest_ap': r['dest_ap'],
            'segments': len(path),
            'started_at': r['started_at'],
            'arrive_at': r['arrive_at'],
            'state': r['state'],
            'state_name': MOVEMENT_STATE_NAMES.get(int(r['state']), '?'),
        })
    return {'rts_ready': True, 'items': items}


async def list_battles(pool) -> dict:
    """会战登记（war_battles，M1 桩）列表。"""
    assert pool is not None
    async with pool.acquire() as conn:
        if not await _tables_ready(conn):
            return {'rts_ready': False, 'items': []}
        rows = await conn.fetch(
            'SELECT b.id, b.war_id, b.battlefield_id, b.factions_json, '
            'b.state, b.started_at, b.ended_at FROM war_battles b '
            'ORDER BY b.started_at DESC LIMIT 100')
    return {
        'rts_ready': True,
        'items': [{
            'id': r['id'],
            'war_id': r['war_id'],
            'battlefield_id': r['battlefield_id'],
            'factions': r['factions_json'],
            'state': r['state'],
            'started_at': r['started_at'],
            'ended_at': r['ended_at'],
        } for r in rows],
    }


async def list_cities(pool, war_id: int = 0) -> dict:
    """城市归属（war_cities；war_id 0=最新战争）。"""
    assert pool is not None
    async with pool.acquire() as conn:
        if not await _tables_ready(conn):
            return {'rts_ready': False, 'items': []}
        if not war_id:
            war_id = await conn.fetchval(
                'SELECT id FROM wars ORDER BY id DESC LIMIT 1') or 0
        if not war_id:
            return {'rts_ready': True, 'war_id': 0, 'items': []}
        rows = await conn.fetch(
            'SELECT battlefield_id, owner_faction FROM war_cities '
            'WHERE war_id = $1 ORDER BY battlefield_id', war_id)
    return {
        'rts_ready': True,
        'war_id': war_id,
        'items': [{
            'battlefield_id': r['battlefield_id'],
            'owner_faction': r['owner_faction'],
            'owner_name': faction_name(r['owner_faction'])
            if r['owner_faction'] else '中立',
        } for r in rows],
    }


# ------------------------------------------------------------------ 写操作（作弊/运维）

async def force_end_war(pool, admin_username: str) -> dict:
    """强制结束当前 running 战争（游戏服 ensure_war 下个 tick 自动续战，
    MM_WAR_AUTO_CREATE=0 时则保持结束状态）。"""
    assert pool is not None
    async with pool.acquire() as conn:
        if not await _tables_ready(conn):
            raise LookupError('RTS 数据表尚未创建')
        row = await conn.fetchrow(
            'UPDATE wars SET state = 2, endtime = now() '
            'WHERE state = 1 RETURNING id, name')
    if row is None:
        raise LookupError('没有进行中的战争')
    return {'war_id': row['id'], 'name': row['name']}


async def adjust_warfunds(pool, player_id: int, delta: int,
                          admin_username: str) -> dict:
    """调整玩家 warfunds（正=发放/负=扣回），落 war_funds_ledger 流水。"""
    assert pool is not None
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                'UPDATE players SET warfunds = warfunds + $1 '
                'WHERE id = $2 RETURNING warfunds, username',
                delta, player_id)
            if row is None:
                raise LookupError('玩家不存在')
            await conn.execute(
                'INSERT INTO war_funds_ledger '
                '(player_id, amount, reason, balance_after) '
                'VALUES ($1, $2, $3, $4)',
                player_id, delta, f'admin:{admin_username}',
                row['warfunds'])
    return {'player_id': player_id, 'balance': row['warfunds'],
            'delta': delta}


async def create_at(pool, *, owner_player_id: int, template_id: int,
                    faction: int, name: str) -> dict:
    """管理端生成 AT（作弊，不扣款；resources 留空，is_deployed=false
    待命于首都，玩家可正常部署/移动）。"""
    assert pool is not None
    async with pool.acquire() as conn:
        if not await _tables_ready(conn):
            raise LookupError('RTS 数据表尚未创建')
        war_id = await conn.fetchval(
            'SELECT id FROM wars WHERE state = 1 ORDER BY id DESC LIMIT 1')
        if war_id is None:
            raise LookupError('没有进行中的战争')
        player = await conn.fetchrow(
            'SELECT id FROM players WHERE id = $1', owner_player_id)
        if player is None:
            raise LookupError('玩家不存在')
        if faction not in FACTIONS:
            raise ValueError('faction 必须是 1/2/3')
        if int(template_id) <= 0:
            raise ValueError('template_id 非法')
        row = await conn.fetchrow(
            'INSERT INTO command_nodes '
            '(war_id, template_id, owner_player_id, faction, name) '
            'VALUES ($1, $2, $3, $4, $5) RETURNING id',
            war_id, int(template_id), owner_player_id, faction,
            (name or '').strip())
    return {'id': row['id'], 'war_id': war_id}


async def dissolve_at(pool, at_id: int) -> dict:
    """解散 AT（软删 state=3，与游戏服 delete_command_node 同语义；
    管理端作弊不退款）。"""
    assert pool is not None
    async with pool.acquire() as conn:
        if not await _tables_ready(conn):
            raise LookupError('RTS 数据表尚未创建')
        row = await conn.fetchrow(
            'UPDATE command_nodes SET state = 3 '
            'WHERE id = $1 AND state < 3 RETURNING id, name', at_id)
    if row is None:
        raise LookupError('AT 不存在（或已解散）')
    return {'id': row['id'], 'name': row['name']}
