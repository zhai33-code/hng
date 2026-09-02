# -*- coding: utf-8 -*-
"""战报领域服务：行组装 / 仪表盘 / 战报列表与详情 / 注册统计。"""
from datetime import datetime, timezone

from .. import templates_data, wardb_meta


def _iso_epoch(ts) -> str | None:
    """battle_start_time（epoch 秒，0=缺省）-> UTC ISO 串；前端按浏览器时区显示。"""
    ts = int(ts or 0)
    if ts <= 0:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def battle_row_dict(r) -> dict:
    """battle_reports 行 -> API dict：preset 转模式/地图名、阵营 id 转名、
    battle_start_time(epoch) 转统一时区 ISO 串。players_svc 玩家详情复用。"""
    info = wardb_meta.preset_info(r['mission_preset_id'])
    winner = r['winner_faction_id']
    defender = r['defender_faction_id']
    return {
        'id': r['id'],
        'mission_session_id': r['mission_session_id'],
        'mission_id': r['mission_id'],
        'mission_preset_id': r['mission_preset_id'],
        'preset_name': info['name'],
        'mode_name': info['mode_name'],
        'map_name': info['map'],
        'winner_faction_id': winner,
        'winner_faction': wardb_meta.faction_name(winner) if winner else '',
        'defender_faction_id': defender,
        'defender_faction': wardb_meta.faction_name(defender) if defender else '',
        'mission_time_seconds': r['mission_time_seconds'],
        'battle_start_time': r['battle_start_time'],
        'battle_start_at': _iso_epoch(r['battle_start_time']),
        'created_at': r['created_at'],
    }


async def dashboard(pool) -> dict:
    assert pool is not None
    async with pool.acquire() as conn:
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


async def list_battles(pool, *, page: int, page_size: int) -> dict:
    assert pool is not None
    async with pool.acquire() as conn:
        total = await conn.fetchval('SELECT count(*) FROM battle_reports')
        rows = await conn.fetch(
            'SELECT id, mission_session_id, mission_id, mission_preset_id, '
            'winner_faction_id, defender_faction_id, mission_time_seconds, '
            'battle_start_time, created_at FROM battle_reports '
            'ORDER BY created_at DESC '
            'LIMIT $1 OFFSET $2', page_size, (page - 1) * page_size)
    return {'total': total, 'items': [battle_row_dict(r) for r in rows]}


async def battle_detail(pool, battle_id: int) -> dict:
    """战报详情：表头 + 参战玩家（含每兵种收益明细）+ 据点归属终态。"""
    assert pool is not None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT id, mission_session_id, mission_id, mission_preset_id, '
            'winner_faction_id, defender_faction_id, mission_time_seconds, '
            'battle_start_time, capturepoints_json, created_at '
            'FROM battle_reports WHERE id = $1', battle_id)
        if row is None:
            raise LookupError('战报不存在')
        prows = await conn.fetch(
            'SELECT player_id, gamertag, squad_name, score, captures, kills, '
            'deaths, is_bot, characters_json FROM battle_report_players '
            'WHERE report_id = $1 ORDER BY score DESC', battle_id)

    # characters_json 里的 characterId 反查角色名/兵种（bot 无 DB 行则留空）
    char_ids = sorted({int(c.get('characterId', 0))
                       for p in prows
                       for c in (p['characters_json'] or [])
                       if isinstance(c, dict)} - {0})
    chars_meta: dict[int, dict] = {}
    if char_ids:
        assert pool is not None
        async with pool.acquire() as conn:
            for c in await conn.fetch(
                    'SELECT id, name, template FROM characters '
                    'WHERE id = ANY($1)', char_ids):
                chars_meta[int(c['id'])] = {
                    'name': c['name'],
                    'class_name': wardb_meta.character_name(int(c['template'])),
                }

    players = []
    for p in prows:
        chars = []
        for c in (p['characters_json'] or []):
            if not isinstance(c, dict):
                continue
            meta = chars_meta.get(int(c.get('characterId', 0)), {})
            chars.append({
                'character_id': int(c.get('characterId', 0)),
                'name': meta.get('name', ''),
                'class_name': meta.get('class_name', ''),
                'xp_earned': max(0, int(c.get('endXp', 0))
                                 - int(c.get('startXp', 0))),
                'credits': int(c.get('creditEarned', 0)),
                'warfunds': int(c.get('warfundEarned', 0)),
            })
        players.append({
            'player_id': p['player_id'],
            'gamertag': p['gamertag'],
            'squad_name': p['squad_name'],
            'score': p['score'],
            'captures': p['captures'],
            'kills': p['kills'],
            'deaths': p['deaths'],
            'is_bot': p['is_bot'],
            'characters': chars,
        })

    cps = row['capturepoints_json'] or []
    owner_counts: dict[int, int] = {}
    for cp in cps:
        if isinstance(cp, dict):
            owner_counts[int(cp.get('owner', 0))] = \
                owner_counts.get(int(cp.get('owner', 0)), 0) + 1

    out = battle_row_dict(row)
    out['players'] = players
    out['capturepoints'] = cps
    out['capture_owner_counts'] = {
        wardb_meta.faction_name(k) if k else '中立': v
        for k, v in sorted(owner_counts.items())}
    return out


async def registration_stats(pool, *, days: int) -> dict:
    assert pool is not None
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT to_char(date_trunc('day', created_at), 'YYYY-MM-DD') AS day, "
            "count(*) AS n FROM players "
            "WHERE created_at >= now() - ($1 || ' days')::interval "
            "GROUP BY 1 ORDER BY 1", str(days))
    return {'days': days, 'items': [{'day': r['day'], 'count': r['n']}
                                    for r in rows]}
