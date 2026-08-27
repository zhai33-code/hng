# -*- coding: utf-8 -*-
"""管理 API：玩家列表/详情、角色装备明细、玩家操作（重置密码/货币/封禁）。"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from .. import audit, auth, wardb_meta
from ..db import pool

router = APIRouter(prefix='/api/players', tags=['players'],
                   dependencies=[Depends(auth.current_admin)])


@router.get('')
async def list_players(page: int = Query(1, ge=1),
                       page_size: int = Query(20, ge=1, le=100),
                       q: str = Query('', max_length=64),
                       order: str = Query('created_desc')):
    """玩家分页列表（q 按用户名包含匹配）。"""
    assert pool is not None
    where = ''
    args: list = []
    if q:
        args.append(f'%{q}%')
        where = f'WHERE username ILIKE ${len(args)}'
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
            f'SELECT id, username, credits, warfunds, gold, banned, created_at '
            f'FROM players {where} ORDER BY {order_sql} '
            f'LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}',
            *args, page_size, (page - 1) * page_size)
    return {
        'total': total,
        'items': [dict(r) for r in rows],
    }


@router.get('/{player_id}')
async def player_detail(player_id: int):
    """玩家账号级详情：账号信息 + 三货币 + 角色概览 + 最近战报。"""
    assert pool is not None
    async with pool.acquire() as conn:
        p = await conn.fetchrow(
            'SELECT id, username, credits, warfunds, gold, banned, created_at '
            'FROM players WHERE id = $1', player_id)
        if p is None:
            raise HTTPException(404, '玩家不存在')
        chars = await conn.fetch(
            'SELECT id, name, faction, template, combat_xp, '
            'combat_badge_0, combat_badge_1, created_at '
            'FROM characters WHERE player_id = $1 ORDER BY id', player_id)
        battles = await conn.fetch(
            'SELECT id, mission_id, winner_faction_id, battle_start_time, '
            'created_at FROM battle_reports '
            'ORDER BY created_at DESC LIMIT 5')
    return {
        'player': dict(p),
        'characters': [{
            'id': c['id'],
            'name': c['name'],
            'faction': c['faction'],
            'faction_name': wardb_meta.faction_name(c['faction']),
            'template': c['template'],
            'class_name': wardb_meta.character_name(c['template']),
            'combat_xp': int(c['combat_xp']),
            'rank': wardb_meta.rank_from_xp(int(c['combat_xp'])),
            'badge_0': wardb_meta.badge_name(int(c['combat_badge_0'])),
            'badge_1': wardb_meta.badge_name(int(c['combat_badge_1'])),
            'created_at': c['created_at'],
        } for c in chars],
        'recent_battles': [dict(b) for b in battles],
    }


@router.get('/{player_id}/characters/{character_id}')
async def character_detail(player_id: int, character_id: int):
    """角色装备明细：武器（含配件/弹药）、载具（含升级件）、勋带、徽章。"""
    assert pool is not None
    async with pool.acquire() as conn:
        c = await conn.fetchrow(
            'SELECT id, player_id, name, faction, template, combat_xp, '
            'combat_badge_0, combat_badge_1, created_at '
            'FROM characters WHERE id = $1', character_id)
        if c is None or int(c['player_id']) != player_id:
            raise HTTPException(404, '角色不存在')

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
            'created_at FROM vehicles WHERE player_id = $1 '
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
        'character': {
            'id': c['id'],
            'name': c['name'],
            'faction': c['faction'],
            'faction_name': wardb_meta.faction_name(c['faction']),
            'template': c['template'],
            'class_name': wardb_meta.character_name(c['template']),
            'combat_xp': int(c['combat_xp']),
            'rank': wardb_meta.rank_from_xp(int(c['combat_xp'])),
            'badge_0': wardb_meta.badge_name(int(c['combat_badge_0'])),
            'badge_1': wardb_meta.badge_name(int(c['combat_badge_1'])),
            'created_at': c['created_at'],
        },
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
            'equipped_in_slot': int(v['equipped_in_slot']),
            'upgrades': upg_by_vehicle.get(int(v['id']), []),
        } for v in vehicles],
        'ribbons': [{
            'ribbon_template_id': r['ribbon_template_id'],
            'name': wardb_meta.ribbon_name(int(r['ribbon_template_id'])),
            'total_xp': float(r['total_xp']),
        } for r in ribbons],
    }


# ---------------- 玩家操作（写操作：write_guard CSRF + 审计）----------------

class ResetPasswordBody(BaseModel):
    new_password: str


@router.post('/{player_id}/reset-password',
             dependencies=[Depends(auth.write_guard)])
async def reset_password(player_id: int, body: ResetPasswordBody,
                         request: Request,
                         admin: auth.AdminUser = Depends(auth.current_admin)):
    pw = body.new_password
    if not (4 <= len(pw) <= 64):
        raise HTTPException(400, '密码长度需为 4-64 位')
    assert pool is not None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'UPDATE players SET password = $1 WHERE id = $2 '
            'RETURNING username', pw, player_id)
    if row is None:
        raise HTTPException(404, '玩家不存在')
    await audit.record(admin, request, 'reset_password', row['username'])
    return {'ok': True}


class CurrencyBody(BaseModel):
    currency: str          # credits | warfunds | gold
    delta: int             # 正发放/负扣减


@router.post('/{player_id}/currency', dependencies=[Depends(auth.write_guard)])
async def adjust_currency(player_id: int, body: CurrencyBody,
                          request: Request,
                          admin: auth.AdminUser = Depends(auth.current_admin)):
    if body.currency not in ('credits', 'warfunds', 'gold'):
        raise HTTPException(400, '非法货币类型')
    if body.delta == 0 or abs(body.delta) > 10 ** 9:
        raise HTTPException(400, '变动值非法')
    assert pool is not None
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f'UPDATE players SET {body.currency} = {body.currency} + $1 '
                f'WHERE id = $2 RETURNING username, {body.currency}',
                body.delta, player_id)
            if row is None:
                raise HTTPException(404, '玩家不存在')
            if int(row[body.currency]) < 0:
                raise HTTPException(400, '扣减后余额不能为负')
    await audit.record(admin, request, 'adjust_currency', row['username'],
                       {'currency': body.currency, 'delta': body.delta,
                        'after': int(row[body.currency])})
    return {'ok': True, 'balance': int(row[body.currency])}


class BanBody(BaseModel):
    banned: bool


@router.post('/{player_id}/ban', dependencies=[Depends(auth.write_guard)])
async def set_ban(player_id: int, body: BanBody,
                  request: Request,
                  admin: auth.AdminUser = Depends(auth.current_admin)):
    assert pool is not None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'UPDATE players SET banned = $1 WHERE id = $2 RETURNING username',
            body.banned, player_id)
    if row is None:
        raise HTTPException(404, '玩家不存在')
    await audit.record(admin, request, 'set_ban', row['username'],
                       {'banned': body.banned})
    return {'ok': True, 'banned': body.banned}
