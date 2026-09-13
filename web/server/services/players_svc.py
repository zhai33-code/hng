# -*- coding: utf-8 -*-
"""玩家领域服务：列表/详情/角色装备明细 + 玩家操作（删号/改密/货币/封禁）。

SQL 与响应组装都在本层；路由只做参数解析与审计。
"""
from datetime import datetime, timezone

from .. import wardb_meta
from .battles_svc import battle_row_dict


async def list_players(pool, *, page: int, page_size: int, q: str,
                       order: str, state: str) -> dict:
    """玩家分页列表（q 按用户名包含匹配；state=active/deleted/all 软删除筛选）。
    每行附最近一次登录 IP/时间（LATERAL，player_id 命中 idx_login_events_player；
    失败登录 player_id 为 NULL，故摘要=最近一次成功登录）。"""
    assert pool is not None
    conds: list[str] = []
    args: list = []
    if q:
        args.append(f'%{q}%')
        conds.append(f'p.username ILIKE ${len(args)}')
    if state == 'active':
        conds.append('p.deleted_at IS NULL')
    elif state == 'deleted':
        conds.append('p.deleted_at IS NOT NULL')
    where = ('WHERE ' + ' AND '.join(conds)) if conds else ''
    order_sql = {
        'created_desc': 'p.created_at DESC',
        'created_asc': 'p.created_at ASC',
        'name_asc': 'p.username ASC',
        'name_desc': 'p.username DESC',
    }.get(order, 'p.created_at DESC')
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            f'SELECT count(*) FROM players p {where}', *args)
        rows = await conn.fetch(
            f'SELECT p.id, p.username, p.credits, p.warfunds, p.gold, '
            f'p.banned, p.ban_reason, p.ban_expires, p.deleted_at, '
            f'p.created_at, l.ip AS last_login_ip, l.created_at AS last_login_at '
            f'FROM players p '
            f'LEFT JOIN LATERAL ('
            f'  SELECT ip, created_at FROM login_events '
            f'  WHERE player_id = p.id ORDER BY id DESC LIMIT 1'
            f') l ON true '
            f'{where} ORDER BY {order_sql} '
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


async def _char_score_rows(conn, player_id: int) -> list[tuple]:
    """账号经验（totalScore）明细：全角色 character_stats 的
    PlayerScore/score 生效值（PlayerScore 键优先、legacy score 回落，
    与游戏服 player_service._msg_player_for / progression._total_score
    同源）。返回 [(character_id, 生效键, 值), ...] 按 character_id 升序。"""
    rows = await conn.fetch(
        'SELECT cs.character_id, cs.stat_key, cs.value '
        'FROM character_stats cs '
        'JOIN characters c ON c.id = cs.character_id '
        "WHERE c.player_id = $1 AND cs.stat_key IN ('PlayerScore', 'score')",
        player_id)
    by_char: dict[int, tuple] = {}
    for r in rows:
        cid = int(r['character_id'])
        key = r['stat_key']
        # PlayerScore 优先：覆盖 legacy score（二者同源不叠加）
        if cid not in by_char or key == 'PlayerScore':
            by_char[cid] = (key, float(r['value']))
    return [(cid, kv[0], kv[1]) for cid, kv in sorted(by_char.items())]


async def _login_profile(conn, player_id: int, username: str) -> dict:
    """登录事件 + 关联账号（玩家详情页与列表展开行共用）。

    关联账号 = 该玩家用过的机器码/IP 上还登过哪些其他账号（小号排查）；
    排除空机器码与回环 IP（本机测试连接噪音）。"""
    logins = await conn.fetch(
        'SELECT id, username, ip, machine_id, machine_id_old, result, '
        'source, created_at FROM login_events '
        'WHERE player_id = $1 OR username = $2 ORDER BY id DESC LIMIT 15',
        player_id, username)
    linked_machines = await conn.fetch(
        'SELECT DISTINCT l2.username FROM login_events l2 '
        'WHERE l2.username <> $1 AND l2.machine_id <> $2 AND l2.machine_id IN ('
        '  SELECT machine_id FROM login_events '
        '  WHERE (player_id = $3 OR username = $1) AND machine_id <> $2)',
        username, '', player_id)
    linked_ips = await conn.fetch(
        'SELECT DISTINCT l2.username FROM login_events l2 '
        'WHERE l2.username <> $1 AND l2.ip IS NOT NULL AND l2.ip <> $2 '
        'AND l2.ip IN (SELECT ip FROM login_events '
        '  WHERE (player_id = $3 OR username = $1) '
        '    AND ip IS NOT NULL AND ip <> $2)',
        username, '127.0.0.1', player_id)
    return {
        'login_events': [dict(r) for r in logins],
        'linked_accounts': {
            'same_machine': sorted(r['username'] for r in linked_machines),
            'same_ip': sorted(r['username'] for r in linked_ips),
        },
    }


async def player_logins(pool, player_id: int) -> dict:
    """单个玩家的登录档案（列表页展开行懒加载；轻量，不含详情页其他部分）。"""
    assert pool is not None
    async with pool.acquire() as conn:
        p = await conn.fetchrow('SELECT username FROM players WHERE id = $1',
                                player_id)
        if p is None:
            raise LookupError('玩家不存在')
        return await _login_profile(conn, player_id, p['username'])


async def player_detail(pool, player_id: int) -> dict:
    """玩家账号级详情：账号信息 + 三货币 + 账号经验/等级 + 角色概览 +
    该玩家最近参战记录。"""
    assert pool is not None
    async with pool.acquire() as conn:
        p = await conn.fetchrow(
            'SELECT id, username, credits, warfunds, gold, banned, '
            'ban_reason, ban_expires, created_at '
            'FROM players WHERE id = $1', player_id)
        if p is None:
            raise LookupError('玩家不存在')
        machines = await conn.fetch(
            'SELECT machine_id, reason, created_at FROM machine_bans '
            'WHERE banned_player_id = $1 ORDER BY created_at', player_id)
        total_score = int(sum(v for _, _, v in
                              await _char_score_rows(conn, player_id)))
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
        # 登录事件 + 关联账号（与列表展开行共用 _login_profile）
        login_profile = await _login_profile(conn, player_id, p['username'])
    recent = []
    for b in battles:
        d = battle_row_dict(b)
        d['own'] = {'score': b['score'], 'kills': b['kills'],
                    'deaths': b['deaths'], 'captures': b['captures']}
        recent.append(d)
    player = dict(p)
    # 账号等级（进展 tab）：totalScore 经 PlayerLevel 阈值推导（12 级封顶）
    player['total_score'] = total_score
    player['level'] = wardb_meta.player_level_from_score(total_score)
    return {
        'player': player,
        'characters': [character_dict(c) for c in chars],
        'recent_battles': recent,
        'machine_bans': [dict(m) for m in machines],
        'login_events': login_profile['login_events'],
        'linked_accounts': login_profile['linked_accounts'],
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


async def adjust_score(pool, player_id: int, delta: int) -> dict:
    """账号经验（totalScore，进展 tab 等级来源）加/减。

    正数：计入当前经验贡献最高的角色（全 0 / 无统计时计入最早角色，
    无键则新建 PlayerScore）；负数：按各角色贡献从高到低扣减（每角色
    扣到 0 为止）。扣减后总额不能为负。

    升级奖励不在此处发放：玩家下次登录（views.subscribeplayerview
    check_player_level notify=False）或战局结算时由游戏服按
    players.level_granted 补账并弹升级弹窗，与正常对战升级同链路。
    返回用户名、新 totalScore 与新等级（供审计与回显）。"""
    if delta == 0 or abs(delta) > 10 ** 9:
        raise ValueError('变动值非法')
    assert pool is not None
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                'SELECT username FROM players WHERE id = $1', player_id)
            if row is None:
                raise LookupError('玩家不存在')
            chars = await conn.fetch(
                'SELECT id FROM characters WHERE player_id = $1 ORDER BY id',
                player_id)
            if not chars:
                raise ValueError('该玩家暂无角色，无法调整经验')
            entries = await _char_score_rows(conn, player_id)
            total = sum(v for _, _, v in entries)
            if total + delta < 0:
                raise ValueError('扣减后经验不能为负')
            if delta > 0:
                # 目标：贡献最高角色（并列取 id 最小）；全 0 / 无统计取最早角色
                if entries:
                    cid, key, val = max(entries, key=lambda e: (e[2], -e[0]))
                else:
                    cid, key, val = int(chars[0]['id']), 'PlayerScore', 0.0
                # 相对增量（与游戏服 add_stat 同款 upsert，并发战局结算不丢账）
                await conn.execute(
                    'INSERT INTO character_stats (character_id, stat_key, value) '
                    'VALUES ($1, $2, $3) '
                    'ON CONFLICT (character_id, stat_key) '
                    'DO UPDATE SET value = character_stats.value + EXCLUDED.value',
                    cid, key, float(delta))
            else:
                # 从贡献最高到低逐角色扣减，各扣到 0 为止（相对减法）
                remaining = float(-delta)
                for cid, key, val in sorted(entries, key=lambda e: (-e[2], e[0])):
                    take = min(val, remaining)
                    if take <= 0:
                        break
                    await conn.execute(
                        'UPDATE character_stats SET value = value - $1 '
                        'WHERE character_id = $2 AND stat_key = $3',
                        take, cid, key)
                    remaining -= take
    new_total = int(total + delta)
    return {'username': row['username'], 'total_score': new_total,
            'level': wardb_meta.player_level_from_score(new_total)}


async def set_ban(pool, player_id: int, banned: bool, reason: str = '',
                  expires_at: datetime | None = None,
                  ban_machines: bool = False) -> dict:
    """封禁/解封，返回用户名（供审计）。

    封禁：reason 展示在客户端登录弹窗（≤200 字）；expires_at 空 = 永久，
    到期由游戏服登录时惰性解封（get_ban_info）；ban_machines=True 连带
    把该玩家登录过的机器码登记进 machine_bans（客户端启动即拦截，防小号）。
    事务内 NOTIFY hng_ban_player，游戏服（LISTEN）即时踢掉在线会话
    （COMMIT 后送达，未提交不误踢）。解封：清三列 + 清理本玩家登记的
    机器码封禁，不踢在线连接。"""
    reason = (reason or '').strip()
    if banned and len(reason) > 200:
        raise ValueError('封禁原因最多 200 字')
    if banned and expires_at is not None:
        if expires_at.tzinfo is None:
            raise ValueError('封禁到期时间需带时区（ISO 8601）')
        if expires_at <= datetime.now(timezone.utc):
            raise ValueError('封禁到期时间必须晚于当前时间')
    assert pool is not None
    async with pool.acquire() as conn:
        async with conn.transaction():
            if banned:
                row = await conn.fetchrow(
                    'UPDATE players SET banned = true, ban_reason = $1, '
                    'ban_expires = $2 WHERE id = $3 RETURNING username',
                    reason, expires_at, player_id)
                if row is None:
                    raise LookupError('玩家不存在')
                if ban_machines:
                    # ★ $2 同用于 SELECT 列表与 WHERE：INSERT..SELECT 的列表
                    #   参数不会按目标列定型（默认 text），与 WHERE 的 bigint
                    #   冲突 → AmbiguousParameterError（HTTP 500）。必须显式
                    #   cast；$1 一并 cast 保持明确。
                    await conn.execute(
                        'INSERT INTO machine_bans (machine_id, reason, '
                        'banned_player_id) '
                        'SELECT DISTINCT machine_id, $1::text, $2::bigint '
                        "FROM login_events "
                        "WHERE player_id = $2 AND machine_id <> '' "
                        'ON CONFLICT (machine_id) DO NOTHING',
                        reason, player_id)
                await conn.execute('SELECT pg_notify($1, $2)',
                                   'hng_ban_player', str(player_id))
            else:
                row = await conn.fetchrow(
                    "UPDATE players SET banned = false, ban_reason = '', "
                    'ban_expires = NULL WHERE id = $1 RETURNING username',
                    player_id)
                if row is None:
                    raise LookupError('玩家不存在')
                await conn.execute(
                    'DELETE FROM machine_bans WHERE banned_player_id = $1',
                    player_id)
    return {'username': row['username'], 'banned': banned}
