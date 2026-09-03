# -*- coding: utf-8 -*-
"""玩家领域服务：列表/详情/角色装备明细 + 玩家操作（删号/改密/货币/封禁）。

SQL 与响应组装都在本层；路由只做参数解析与审计。
"""
from .. import wardb_meta
from .battles_svc import battle_row_dict


async def list_players(pool, *, page: int, page_size: int, q: str,
                       order: str, state: str) -> dict:
    """玩家分页列表（q 按用户名包含匹配；state=active/deleted/all 软删除筛选）。"""
    assert pool is not None
    conds: list[str] = []
    args: list = []
    if q:
        args.append(f'%{q}%')
        conds.append(f'username ILIKE ${len(args)}')
    if state == 'active':
        conds.append('deleted_at IS NULL')
    elif state == 'deleted':
        conds.append('deleted_at IS NOT NULL')
    where = ('WHERE ' + ' AND '.join(conds)) if conds else ''
    order_sql = {
        'created_desc': 'created_at DESC',
        'created_asc': 'created_at ASC',
        'name_asc': 'username ASC',
        'name_desc': 'username DESC',
    }.get(order, 'created_at DESC')
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            f'SELECT count(*) FROM players {where}', *args)
        rows = await conn.fetch(
            f'SELECT id, username, credits, warfunds, gold, banned, '
            f'deleted_at, created_at '
            f'FROM players {where} ORDER BY {order_sql} '
            f'LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}',
            *args, page_size, (page - 1) * page_size)
    return {
        'total': total,
        'items': [dict(r) for r in rows],
    }


async def batch_delete(pool, ids: list[int], hard: bool) -> dict:
    """批量删除账号。

    软删除：players.deleted_at 置为 now()，账号数据保留，登录侧拒登；
    硬删除：DELETE players，经 FK ON DELETE CASCADE 级联清掉角色/装备等
    全部关联数据（战报 battle_report_players 无外键，历史战报保留）。
    """
    # 去重 + 上限（防误粘贴超大列表）
    ids = list(dict.fromkeys(ids))
    if not ids:
        raise ValueError('未选择任何账号')
    if len(ids) > 500:
        raise ValueError('单次最多操作 500 个账号')
    assert pool is not None
    async with pool.acquire() as conn:
        if hard:
            rows = await conn.fetch(
                'DELETE FROM players WHERE id = ANY($1) '
                'RETURNING username', ids)
        else:
            rows = await conn.fetch(
                'UPDATE players SET deleted_at = now() '
                'WHERE id = ANY($1) AND deleted_at IS NULL '
                'RETURNING username', ids)
    if not rows:
        raise LookupError('账号不存在（或已软删除）')
    names = [r['username'] for r in rows]
    return {'count': len(names), 'usernames': names}


def character_dict(c) -> dict:
    """characters 行 -> 概览 dict（玩家详情与角色明细共用组装）。"""
    xp = int(c['combat_xp'])
    return {
        'id': c['id'],
        'name': c['name'],
        'faction': c['faction'],
        'faction_name': wardb_meta.faction_name(c['faction']),
        'template': c['template'],
        'class_name': wardb_meta.character_name(c['template']),
        'combat_xp': xp,
        'rank': wardb_meta.rank_from_xp(xp),
        'badge_0': wardb_meta.badge_name(int(c['combat_badge_0']))
                   if int(c['combat_badge_0']) else '',
        'badge_1': wardb_meta.badge_name(int(c['combat_badge_1']))
                   if int(c['combat_badge_1']) else '',
        'badge_0_id': int(c['combat_badge_0']),
        'badge_1_id': int(c['combat_badge_1']),
        'created_at': c['created_at'],
    }


async def player_detail(pool, player_id: int) -> dict:
    """玩家账号级详情：账号信息 + 三货币 + 角色概览 + 该玩家最近参战记录。"""
    assert pool is not None
    async with pool.acquire() as conn:
        p = await conn.fetchrow(
            'SELECT id, username, credits, warfunds, gold, banned, created_at '
            'FROM players WHERE id = $1', player_id)
        if p is None:
            raise LookupError('玩家不存在')
        chars = await conn.fetch(
            'SELECT id, name, faction, template, combat_xp, '
            'combat_badge_0, combat_badge_1, created_at '
            'FROM characters WHERE player_id = $1 ORDER BY id', player_id)
        # 该玩家真实参战的最近战局（battle_report_players 按 player_id 反查；
        # 顺带带回本人战绩，is_bot 过滤防止 bot 小 id 撞真实账号 id）
        battles = await conn.fetch(
            'SELECT r.id, r.mission_session_id, r.mission_id, '
            'r.mission_preset_id, r.winner_faction_id, r.defender_faction_id, '
            'r.mission_time_seconds, r.battle_start_time, r.created_at, '
            'p.score, p.kills, p.deaths, p.captures '
            'FROM battle_report_players p '
            'JOIN battle_reports r ON r.id = p.report_id '
            'WHERE p.player_id = $1 AND NOT p.is_bot '
            'ORDER BY r.created_at DESC LIMIT 10', player_id)
    recent = []
    for b in battles:
        d = battle_row_dict(b)
        d['own'] = {'score': b['score'], 'kills': b['kills'],
                    'deaths': b['deaths'], 'captures': b['captures']}
        recent.append(d)
    return {
        'player': dict(p),
        'characters': [character_dict(c) for c in chars],
        'recent_battles': recent,
    }


async def character_detail(pool, player_id: int, character_id: int) -> dict:
    """角色装备明细：武器（含配件/弹药）、载具（含升级件）、勋带、徽章。"""
    assert pool is not None
    async with pool.acquire() as conn:
        c = await conn.fetchrow(
            'SELECT id, player_id, name, faction, template, combat_xp, '
            'combat_badge_0, combat_badge_1, created_at '
            'FROM characters WHERE id = $1', character_id)
        if c is None or int(c['player_id']) != player_id:
            raise LookupError('角色不存在')

        weapons = await conn.fetch(
            'SELECT id, name, template_id, character_id, container_id, '
            'container_slot, variant_template_id, variant_id, created_at '
            'FROM weapons WHERE player_id = $1 AND (character_id = $2 '
            'OR character_id = 0) ORDER BY id', player_id, character_id)
        modifiers = await conn.fetch(
            'SELECT id, template_id, weapon_id, created_at FROM weapon_modifiers '
            'WHERE player_id = $1 AND (character_id = $2 OR character_id = 0)',
            player_id, character_id)
        ammo = await conn.fetch(
            'SELECT id, ammo_template_id, weapon_id, slot_index, "condition" '
            'FROM weapon_ammo WHERE player_id = $1 '
            'AND (character_id = $2 OR character_id = 0)',
            player_id, character_id)
        vehicles = await conn.fetch(
            'SELECT id, vehicle_variant_template_id, equipped_in_slot, '
            'character_id, created_at FROM vehicles WHERE player_id = $1 '
            'AND (character_id = $2 OR character_id = 0)',
            player_id, character_id)
        upgrades = await conn.fetch(
            'SELECT id, upgrade_template_id, vehicle_id, equipped_slot_id '
            'FROM vehicle_upgrades WHERE player_id = $1 '
            'AND (character_id = $2 OR character_id = 0)',
            player_id, character_id)
        ribbons = await conn.fetch(
            'SELECT ribbon_template_id, total_xp FROM character_ribbon_xp '
            'WHERE character_id = $1 ORDER BY ribbon_template_id', character_id)

    # 武器 -> 组装配件/弹药；载具 -> 组装升级件
    mod_by_weapon: dict[int, list] = {}
    for m in modifiers:
        mod_by_weapon.setdefault(int(m['weapon_id']), []).append({
            'id': m['id'],
            'template_id': m['template_id'],
            'name': wardb_meta.modifier_name(int(m['template_id'])),
        })
    ammo_by_weapon: dict[int, list] = {}
    for a in ammo:
        ammo_by_weapon.setdefault(int(a['weapon_id']), []).append({
            'id': a['id'],
            'template_id': a['ammo_template_id'],
            'name': wardb_meta.ammo_name(int(a['ammo_template_id'])),
            'slot_index': a['slot_index'],
            'condition': float(a['condition']),
        })
    upg_by_vehicle: dict[int, list] = {}
    for u in upgrades:
        upg_by_vehicle.setdefault(int(u['vehicle_id']), []).append({
            'id': u['id'],
            'template_id': u['upgrade_template_id'],
            'equipped_slot_id': int(u['equipped_slot_id']),
        })

    return {
        'character': character_dict(c),
        'weapons': [{
            'id': w['id'],
            'name': w['name'],
            'template_id': w['template_id'],
            'template_name': wardb_meta.weapon_name(int(w['template_id'])),
            'variant_template_id': int(w['variant_template_id']),
            'equipped': int(w['character_id']) == character_id,
            'container_id': int(w['container_id']),
            'container_slot': int(w['container_slot']),
            'modifiers': mod_by_weapon.get(int(w['id']), []),
            'ammo': ammo_by_weapon.get(int(w['id']), []),
        } for w in weapons],
        'vehicles': [{
            'id': v['id'],
            'variant_template_id': v['vehicle_variant_template_id'],
            **wardb_meta.vehicle_info(int(v['vehicle_variant_template_id'])),
            'equipped': int(v['character_id']) == character_id,
            'equipped_in_slot': int(v['equipped_in_slot']),
            'upgrades': upg_by_vehicle.get(int(v['id']), []),
        } for v in vehicles],
        'ribbons': [{
            'ribbon_template_id': r['ribbon_template_id'],
            'name': wardb_meta.ribbon_name(int(r['ribbon_template_id'])),
            # 后端直接拼好最终路径：专属图或类别底图（官方 Specialist 渲染方式），前端零试错
            'icon': wardb_meta.ribbon_icon(int(r['ribbon_template_id'])),
            # 官方 pip 叠加图（灰度武器/载具图，与底图同画布）+ 缺失回退全尺寸图标
            'overlay_icon': wardb_meta.ribbon_overlay(int(r['ribbon_template_id'])),
            'overlay_fallback': wardb_meta.ribbon_overlay_fallback(int(r['ribbon_template_id'])),
            # 官方 PipTransform 摆放参数（scale/x/y），前端按此缩放定位叠加
            'overlay_style': wardb_meta.ribbon_overlay_style(int(r['ribbon_template_id'])),
            'total_xp': float(r['total_xp']),
        } for r in ribbons],
    }


async def reset_password(pool, player_id: int, new_password: str) -> str:
    """重置密码，返回用户名（供审计）。"""
    if not (6 <= len(new_password) <= 64):
        raise ValueError('密码长度需为 6-64 位')
    assert pool is not None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'UPDATE players SET password = $1 WHERE id = $2 '
            'RETURNING username', new_password, player_id)
    if row is None:
        raise LookupError('玩家不存在')
    return row['username']


async def adjust_currency(pool, player_id: int, currency: str,
                          delta: int) -> dict:
    """发放/扣减货币（同一事务内校验余额），返回用户名与新余额（供审计）。"""
    if currency not in ('credits', 'warfunds', 'gold'):
        raise ValueError('非法货币类型')
    if delta == 0 or abs(delta) > 10 ** 9:
        raise ValueError('变动值非法')
    assert pool is not None
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f'UPDATE players SET {currency} = {currency} + $1 '
                f'WHERE id = $2 RETURNING username, {currency}',
                delta, player_id)
            if row is None:
                raise LookupError('玩家不存在')
            if int(row[currency]) < 0:
                raise ValueError('扣减后余额不能为负')
    return {'username': row['username'], 'balance': int(row[currency])}


async def set_ban(pool, player_id: int, banned: bool) -> dict:
    """封禁/解封，返回用户名（供审计）。"""
    assert pool is not None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'UPDATE players SET banned = $1 WHERE id = $2 RETURNING username',
            banned, player_id)
    if row is None:
        raise LookupError('玩家不存在')
    return {'username': row['username'], 'banned': banned}
