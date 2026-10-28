# -*- coding: utf-8 -*-
"""兑换码领域服务：生成校验 + 批量入库 + 列表查询（纯 asyncpg，不依赖 FastAPI）。

体系设计见 docs/voucher-redemption-design.md：
- 时效：expires_at（expires_days 天后过期，输码返回 280）
- 次数：max_redemptions（1=一码一用 / N=限量 / 0=无限次，每人限一次靠流水 UNIQUE）
- 内容：items_json 支持货币 + 道具（武器皮肤/载具涂装/头盔/勋带加成/老兵会员）
兑换流水在 voucher_redemptions（游戏服写），本端只生成与展示。
"""
import json
import secrets

# 与官方兑换码观感一致的无歧义字符集（去 0/O/1/I/l）
_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'

# 道具类型白名单（与游戏服 _deliver_voucher_items 支持范围一致）
_CURRENCIES = ('credits', 'warfunds', 'gold')
_ITEM_TYPES = ('weaponvarianttemplate', 'vehiclevarianttemplate',
               'uniformparttemplate', 'ribbonboostertemplate',
               'shopmembershipitems')


def _gen_code(length: int = 12) -> str:
    return ''.join(secrets.choice(_ALPHABET) for _ in range(length))


def normalize_items(raw: list[dict]) -> list[dict]:
    """道具规格校验/归一（[{type,id,amount}]），非法抛 ValueError。"""
    items = []
    for it in raw:
        t = str(it.get('type', '')).lower()
        if t in _CURRENCIES:
            amount = int(it.get('amount', 0))
            if amount <= 0:
                raise ValueError(f'货币 {t} 数量需大于 0')
            items.append({'type': t, 'amount': amount})
        elif t in _ITEM_TYPES:
            tid = int(it.get('id', 0))
            if tid <= 0:
                raise ValueError(f'道具 {t} 需填写模板 id')
            items.append({'type': t, 'id': tid})
        else:
            raise ValueError(f'不支持的类型 {it.get("type")!r}')
    if not items:
        raise ValueError('至少配置一项内容')
    return items


async def generate(pool, *, count: int, items: list[dict],
                   expires_days: int, max_redemptions: int,
                   note: str) -> dict:
    """批量生成兑换码（同批内容相同；一码限一人，批次内多码各归一人）。

    返回 {'codes', 'items', 'note'}（items/note 为归一化后的值，供审计）。
    """
    if not (1 <= count <= 500):
        raise ValueError('单次生成 1-500 个')
    if not (0 <= expires_days <= 3650):
        raise ValueError('有效期需为 0（永久）- 3650 天')
    if not (0 <= max_redemptions <= 100000):
        raise ValueError('限量需为 0（无限）- 100000')
    items = normalize_items(items)
    note = note.strip()[:128]
    assert pool is not None
    codes = []
    async with pool.acquire() as conn:
        async with conn.transaction():
            for _ in range(count):
                # 本批内去重；跨批撞码概率 ~0（UNIQUE 冲突时事务回滚重来）
                while True:
                    code = _gen_code()
                    if code not in codes:
                        break
                await conn.execute(
                    'INSERT INTO voucher_packs '
                    '(pack_name, items_json, note, expires_at, max_redemptions) '
                    'VALUES ($1, $2, $3, '
                    'CASE WHEN $4::int > 0 '
                    'THEN now() + make_interval(days => $4::int) ELSE NULL END, '
                    '$5::int)',
                    code, json.dumps(items), note,
                    int(expires_days), int(max_redemptions))
                codes.append(code)
    return {'codes': codes, 'items': items, 'note': note}


async def list_vouchers(pool, *, page: int, page_size: int, state: str) -> dict:
    """兑换码分页列表（state=unused/used/exhausted/expired/all）。

    used_count = granted 流水数 + legacy redeemed（旧一码一用直接置位无流水）。
    """
    assert pool is not None
    conds: list[str] = []
    if state == 'unused':
        conds.append('(SELECT count(*) FROM voucher_redemptions vr '
                     'WHERE vr.voucher_pack_id = v.id AND vr.status = 2) = 0 '
                     'AND v.redeemed = FALSE '
                     'AND (v.expires_at IS NULL OR v.expires_at > now())')
    elif state == 'used':
        conds.append('(SELECT count(*) FROM voucher_redemptions vr '
                     'WHERE vr.voucher_pack_id = v.id AND vr.status = 2) > 0 '
                     'OR v.redeemed = TRUE')
    elif state == 'exhausted':
        conds.append('v.max_redemptions > 0 AND ('
                     '(SELECT count(*) FROM voucher_redemptions vr '
                     'WHERE vr.voucher_pack_id = v.id AND vr.status = 2) '
                     '+ (CASE WHEN v.redeemed THEN 1 ELSE 0 END)) '
                     '>= v.max_redemptions')
    elif state == 'expired':
        conds.append('v.expires_at IS NOT NULL AND v.expires_at <= now()')
    where = ('WHERE ' + ' AND '.join(conds)) if conds else ''
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            f'SELECT count(*) FROM voucher_packs v {where}')
        rows = await conn.fetch(
            'SELECT v.id, v.pack_name, v.items_json, v.redeemed, '
            'v.expires_at, v.max_redemptions, v.note, v.created_at, '
            '(SELECT count(*) FROM voucher_redemptions vr '
            ' WHERE vr.voucher_pack_id = v.id AND vr.status = 2) '
            '  AS used_count, '
            '(SELECT p.username FROM voucher_redemptions vr '
            ' JOIN players p ON p.id = vr.player_id '
            ' WHERE vr.voucher_pack_id = v.id AND vr.status = 2 '
            ' ORDER BY vr.granted_at DESC NULLS LAST LIMIT 1) '
            '  AS last_redeemer '
            'FROM voucher_packs v '
            f'{where} ORDER BY v.id DESC '
            'LIMIT $1 OFFSET $2',
            page_size, (page - 1) * page_size)
    items_out = []
    for r in rows:
        used = int(r['used_count']) + (1 if r['redeemed'] else 0)
        items_out.append({
            'id': r['id'],
            'code': r['pack_name'],
            'items': json.loads(r['items_json'] or '[]'),
            'note': r['note'],
            'expires_at': r['expires_at'],
            'max_redemptions': int(r['max_redemptions'] or 0),
            'used_count': used,
            'last_redeemer': r['last_redeemer'] or '',
            'created_at': r['created_at'],
        })
    return {'total': total, 'items': items_out}
