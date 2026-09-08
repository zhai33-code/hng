# -*- coding: utf-8 -*-
"""货币（金币/战争资金/金条）/ 日常奖励 / 兑换券 / 战争债券 / 会员 / 加成 / 票券。
    注意: get_username 在此二次定义以保持原 PlayerStore 的覆盖语义。
拆分自 player_store.py（纯移动重构，方法体逐字保留）。
"""
import json

class EconomyMixin:
    """货币（金币/战争资金/金条）/ 日常奖励 / 兑换券 / 战争债券 / 会员 / 加成 / 票券。
    注意: get_username 在此二次定义以保持原 PlayerStore 的覆盖语义。"""

    async def get_currency(self, player_id):
        """返回 {credits, warfunds, gold}；账号不存在返回默认值。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT credits, warfunds, gold FROM players WHERE id = $1', player_id)
        if row is None:
            return {'credits': 100000, 'warfunds': 100000, 'gold': 100000}
        return {'credits': row['credits'], 'warfunds': row['warfunds'],
                'gold': row['gold']}

    async def set_currency(self, player_id, credits=None, warfunds=None, gold=None):
        """更新账号货币（只更新非 None 字段）。"""
        sets, args = [], []
        if credits is not None:
            args.append(credits); sets.append('credits = $%d' % len(args))
        if warfunds is not None:
            args.append(warfunds); sets.append('warfunds = $%d' % len(args))
        if gold is not None:
            args.append(gold); sets.append('gold = $%d' % len(args))
        if not sets:
            return
        args.append(player_id)
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE players SET ' + ', '.join(sets) +
                ' WHERE id = $%d' % len(args), *args)

    async def add_currency(self, player_id, credits=0, warfunds=0, gold=0):
        """结算加钱：credits/warfunds/gold 增量累加，返回新余额 dict。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'UPDATE players SET credits = credits + $2, warfunds = warfunds + $3, '
                'gold = gold + $4 WHERE id = $1 '
                'RETURNING credits, warfunds, gold',
                player_id, int(credits), int(warfunds), int(gold))
        if row is None:
            return None
        return {'credits': row['credits'], 'warfunds': row['warfunds'],
                'gold': row['gold']}

    # 货币列白名单（spend_currency 拼接列名前校验，防注入）
    _CURRENCY_KEYS = ('credits', 'warfunds', 'gold')

    async def spend_currency(self, player_id, currency_key, cost, conn=None):
        """原子扣款：事务内 FOR UPDATE 锁行，余额足够才扣减（增量扣，杜绝丢更新）。

        返回 (ok, 剩余余额)：余额不足/账号不存在 → (False, 当前余额)。
        传入 conn 时并入外部事务（外部事务内自动降级为 savepoint）。
        """
        key = str(currency_key).lower()
        if key not in self._CURRENCY_KEYS:
            return False, 0
        async with self._conn(conn) as c:
            async with c.transaction():
                balance = await c.fetchval(
                    f'SELECT {key} FROM players WHERE id = $1 FOR UPDATE',
                    int(player_id))
                if balance is None:
                    return False, 0
                if int(balance) < int(cost):
                    return False, int(balance)
                new_val = await c.fetchval(
                    f'UPDATE players SET {key} = {key} - $2 WHERE id = $1 '
                    f'RETURNING {key}', int(player_id), int(cost))
        return True, int(new_val)

    # ------------------------------------------------------------- 经济道具（U5）

    async def claim_daily_reward(self, player_id):
        """每日签到领取（RedeemDailyLoginRewardRequest）。

        当天首次领取 → daily_reward_redeemed_count+1、last_daily_reward_at=now()
        返回 (True, day, redeemed_count, consecutive)；当天已领 →
        (True, 0, n, c) 幂等不发；账号不存在 → (False, 0, 0, 0)。
        day 即客户端 dailyRewards.xml 的奖励日（连续计数，handler 侧做
        365 天循环折算）；consecutive 为连续登录天数（handler 侧随每日
        奖励一并发放 2..7 天连续 bonus）。发放内容（货币/加成/会员）
        由 handler 按当日 day 执行——标记与计数在同一事务内（FOR UPDATE
        锁行防并发双领），发放失败不回滚（已计入的 day 只少发一次奖励，
        不会重复发）。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    'SELECT last_daily_reward_at::date AS d, '
                    'daily_reward_redeemed_count AS n, '
                    'consecutive_day_count AS c FROM players '
                    'WHERE id = $1 FOR UPDATE', int(player_id))
                if row is None:
                    return False, 0, 0, 0
                if row['d'] is not None:
                    today = await conn.fetchval('SELECT now()::date')
                    if row['d'] == today:
                        return True, 0, int(row['n']), int(row['c'])
                n = int(row['n']) + 1
                await conn.execute(
                    'UPDATE players SET last_daily_reward_at = now(), '
                    'daily_reward_redeemed_count = $2 WHERE id = $1',
                    int(player_id), n)
        return True, n, n, int(row['c'])

    async def touch_daily_login(self, player_id):
        """登录即记连续天数（subscribeplayerview 推 player 行前调用）：
        同日幂等；昨日已登录 → +1；断签 → 重置 1。返回连续天数。
        同时把 first_action_game_date（player f19）刷成当天——它是客户端
        DailyRewardsWindowMediator 连续奖励链的基准日（C = f8 +
        (f19 != 今天 ? 1 : 0)，f19 距今 >1 天则链条判死恒为 1），必须与
        consecutive_day_count 同基准日，否则第 2 天起 bonus 列错位/卡死。"""
        async with self.pool.acquire() as conn:
            return int(await conn.fetchval(
                'UPDATE players SET consecutive_day_count = CASE '
                'WHEN last_login_date = CURRENT_DATE '
                '  THEN consecutive_day_count '
                'WHEN last_login_date = CURRENT_DATE - 1 '
                '  THEN consecutive_day_count + 1 '
                'ELSE 1 END, '
                'first_action_game_date = CURRENT_DATE, '
                'last_login_date = CURRENT_DATE '
                'WHERE id = $1 RETURNING consecutive_day_count',
                int(player_id)) or 1)

    async def get_daily_metrics(self, player_id):
        """PlayerOfferMetrics KVCS 行源（签到窗口数据）。

        available=今天可领（last_daily_reward_at 非今天）；
        fagd=firstActionGameDate（YYYYMMDD，无首局回落注册日）。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT daily_reward_redeemed_count AS n, '
                'consecutive_day_count AS c, '
                "COALESCE(last_daily_reward_at::date <> now()::date, "
                "TRUE) AS available, "
                "to_char(COALESCE(first_action_game_date, created_at::date), "
                "'YYYYMMDD') AS fagd "
                'FROM players WHERE id = $1', int(player_id))
        return dict(row) if row else None

    async def mark_first_action_game(self, player_id):
        """首局日期（player f19 firstActionGameDate）：结算时调用，幂等只写一次。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE players SET first_action_game_date = '
                'COALESCE(first_action_game_date, now()::date) '
                'WHERE id = $1', int(player_id))

    async def redeem_voucher(self, player_id, key):
        """兑换码 v2（RedeemVoucherRequest）：校验 + 记 pending 流水，不立即发放。

        统一走弹窗流程（docs/voucher-redemption-design.md）：输码成功只记
        pending，客户端自动发 QueryVoucherPacksRequest{0} → 264 弹士兵选择 →
        玩家选定后 Query{charId} 才真正发货（voucher_claim）。
        返回 code：1 ok（含 pending 幂等重入）/ 165 invalid_key /
        168 already_used（本玩家已兑或总量耗尽）/ 280 expired。

        FOR UPDATE 锁包行防并发；已用次数 = granted 流水数 + legacy redeemed
        （旧流程直接置位无流水，兼容存量一码一用语义）。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    'SELECT id, redeemed, expires_at, max_redemptions '
                    'FROM voucher_packs WHERE lower(pack_name) = lower($1) '
                    'FOR UPDATE', str(key or ''))
                if row is None:
                    return 165
                if row['expires_at'] is not None:
                    # 过期时间在锁行后取库端 now() 判定，避免本地时钟偏差
                    expired = await conn.fetchval(
                        'SELECT $1 <= now()', row['expires_at'])
                    if expired:
                        return 280
                granted_n = int(await conn.fetchval(
                    'SELECT count(*) FROM voucher_redemptions '
                    'WHERE voucher_pack_id = $1 AND status = 2', row['id']))
                used = granted_n + (1 if row['redeemed'] else 0)
                max_n = int(row['max_redemptions'] or 0)
                mine = await conn.fetchrow(
                    'SELECT status FROM voucher_redemptions '
                    'WHERE voucher_pack_id = $1 AND player_id = $2',
                    row['id'], int(player_id))
                if mine is not None:
                    # 本玩家已有流水：granted=已兑过 168；pending=幂等重入 1
                    return 168 if mine['status'] == 2 else 1
                if max_n > 0 and used >= max_n:
                    return 168
                await conn.execute(
                    'INSERT INTO voucher_redemptions (voucher_pack_id, player_id) '
                    'VALUES ($1, $2)', row['id'], int(player_id))
                return 1

    async def voucher_pending_packs(self, player_id):
        """待选士兵的兑换包（QueryVoucherPacksRequest characterId=0 → 264）。

        返回 [{pack_name, note, items}]，items 为 items_json 解析结果。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT v.pack_name, v.note, v.items_json '
                'FROM voucher_redemptions r '
                'JOIN voucher_packs v ON v.id = r.voucher_pack_id '
                'WHERE r.player_id = $1 AND r.status = 1 ORDER BY r.id',
                int(player_id))
        packs = []
        for r in rows:
            try:
                items = json.loads(r['items_json'] or '[]')
            except Exception:
                items = []
            packs.append({'pack_name': r['pack_name'], 'note': r['note'],
                          'items': items})
        return packs

    async def voucher_claim(self, player_id, character_id):
        """选士兵后确认发货（QueryVoucherPacksRequest characterId>0）。

        pending → granted（记录所选角色）；限量码在锁行后重检总量，
        已耗尽的 pending 行置 status=3 跳过（宁缺毋错，不重复发放）。
        返回本次确认发放的包列表 [{pack_name, note, items}]，
        实际物品发放由调用方（shop.py _deliver_voucher_items）执行——
        先标记后发放：发放中途失败不会导致重试时二次发放。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                pend = await conn.fetch(
                    'SELECT r.id AS rid, v.id AS vid, v.pack_name, v.note, '
                    'v.items_json, v.max_redemptions '
                    'FROM voucher_redemptions r '
                    'JOIN voucher_packs v ON v.id = r.voucher_pack_id '
                    'WHERE r.player_id = $1 AND r.status = 1 '
                    'ORDER BY r.id FOR UPDATE OF r, v', int(player_id))
                packs = []
                for r in pend:
                    # 锁住包行后重检：并发 claim 同一限量码时，后到者在此
                    # 读到先到者已提交的 granted 计数（READ COMMITTED 每条
                    # 语句取新快照）
                    used = int(await conn.fetchval(
                        'SELECT count(*) FROM voucher_redemptions '
                        'WHERE voucher_pack_id = $1 AND status = 2',
                        r['vid']))
                    max_n = int(r['max_redemptions'] or 0)
                    if max_n > 0 and used >= max_n:
                        # 输码与发货之间被其他玩家抢先兑完（限量码竞态）
                        await conn.execute(
                            'UPDATE voucher_redemptions SET status = 3 '
                            'WHERE id = $1', r['rid'])
                        continue
                    await conn.execute(
                        'UPDATE voucher_redemptions SET status = 2, '
                        'character_id = $2, granted_at = now() WHERE id = $1',
                        r['rid'], int(character_id or 0))
                    try:
                        items = json.loads(r['items_json'] or '[]')
                    except Exception:
                        items = []
                    packs.append({'pack_name': r['pack_name'],
                                  'note': r['note'], 'items': items})
                return packs

    async def add_warbond(self, player_id, template_id, conn=None):
        """购买战争债券（AddWarBondRequest）。返回债券条目 id。
        传入 conn 时并入外部事务（与扣款同生共死）。"""
        async with self._conn(conn) as c:
            wid = await c.fetchval(
                'INSERT INTO player_warbonds (player_id, warbond_item_id, count) '
                'VALUES ($1, $2, 1) ON CONFLICT (player_id, warbond_item_id) '
                'DO UPDATE SET count = player_warbonds.count + 1 '
                'RETURNING warbond_item_id', int(player_id), int(template_id))
        return int(wid)

    async def list_warbonds(self, player_id):
        """玩家战争债券（UpdateWarBondsResponse 源）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT warbond_item_id, count FROM player_warbonds '
                'WHERE player_id = $1 ORDER BY warbond_item_id', int(player_id))
        return [{'template_id': int(r['warbond_item_id']),
                 'count': int(r['count'])} for r in rows]

    async def set_first_gold_offer_shown(self, player_id):
        """标记首金优惠已展示（AddFirstGoldOfferRequest）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE players SET first_gold_offer_shown = TRUE WHERE id = $1',
                int(player_id))

    async def add_membership(self, player_id, template_id, duration_days=30,
                             conn=None):
        """购买会员（AddMembershipRequest）。返回插入行 {id, expires_at}。
        传入 conn 时并入外部事务（与扣款同生共死）。

        官方排队语义（客户端 PlayerModel：isCurrentMembership = expireDate
        非空，getCurrentMembership 只认第一行判 isPremium，最终到期 =
        当前行到期 + Σ排队行 durationDays）：已有进行中的会员
        （expires_at > now()）→ 插排队行 expires_at=NULL，前一份到期后由
        sweep_memberships 激活；没有 → 立即起跑。EXISTS 判定并入同一条
        INSERT...SELECT，避免"先查后插"窗口。"""
        async with self._conn(conn) as c:
            row = await c.fetchrow(
                'INSERT INTO player_memberships (player_id, '
                'membership_template_id, started_at, expires_at) '
                'SELECT $1, $2, now(), CASE WHEN EXISTS ('
                '  SELECT 1 FROM player_memberships WHERE player_id = $1 '
                '  AND expires_at IS NOT NULL AND expires_at > now()) '
                'THEN NULL ELSE now() + make_interval(days => $3) END '
                'RETURNING id, expires_at',
                int(player_id), int(template_id), int(duration_days))
        return {'id': int(row['id']), 'expires_at': row['expires_at']}

    async def sweep_memberships(self, player_id, days_by_template=None,
                                default_days=30):
        """会员行清扫+激活（登录回放/购买后推送前调用）。

        1) 删已过期行——客户端 getCurrentMembership 取表中第一行（不校验
           是否已过期），残留过期行会把老兵判定永久卡成 false；
        2) 无进行中行时激活最老排队行（expires_at=NULL → now() ~
           now()+days，天数按 days_by_template[membership_template_id]）。
        返回 (删除行数, 激活行 {id, template_id, days} 或 None)。"""
        if days_by_template is None:
            days_by_template = {}
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                tag = await conn.execute(
                    'DELETE FROM player_memberships WHERE player_id = $1 '
                    'AND expires_at IS NOT NULL AND expires_at <= now()',
                    int(player_id))
                deleted = int(tag.split()[-1]) if tag else 0
                row = await conn.fetchrow(
                    'SELECT m.id, m.membership_template_id '
                    'FROM player_memberships m WHERE m.player_id = $1 '
                    'AND m.expires_at IS NULL AND NOT EXISTS ('
                    '  SELECT 1 FROM player_memberships r '
                    '  WHERE r.player_id = $1 AND r.expires_at IS NOT NULL '
                    '  AND r.expires_at > now()) '
                    'ORDER BY m.id LIMIT 1', int(player_id))
                if row is None:
                    return deleted, None
                days = int(days_by_template.get(
                    int(row['membership_template_id']), default_days))
                await conn.execute(
                    'UPDATE player_memberships SET started_at = now(), '
                    'expires_at = now() + make_interval(days => $2) '
                    'WHERE id = $1', int(row['id']), days)
                return deleted, {'id': int(row['id']),
                                 'template_id': int(row['membership_template_id']),
                                 'days': days}

    async def get_active_membership(self, player_id):
        """未过期的最新会员行或 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, membership_template_id AS template_id, '
                'started_at, expires_at '
                'FROM player_memberships WHERE player_id = $1 '
                'AND (expires_at IS NULL OR expires_at > now()) '
                'ORDER BY id DESC LIMIT 1', int(player_id))
        return dict(row) if row else None

    async def add_booster(self, player_id, template_id, duration_minutes=240,
                          activate_now=False, conn=None, is_ribbon=False):
        """购买加成（AddBooster/AddVariousBoosterRequest）。返回 booster 行 id。
        传入 conn 时并入外部事务（与扣款同生共死）。
        is_ribbon：勋带加成（AddBoosterRequest）为 True，通用加成
        （AddVariousBoosterRequest）为 False —— 两张模板表 id 有重叠，
        推送 KVCS 时靠该列决定进 RibbonBooster 还是 Booster 表。"""
        async with self._conn(conn) as c:
            if activate_now:
                bid = await c.fetchval(
                    'INSERT INTO player_boosters (player_id, booster_template_id, '
                    'activated_at, expires_at, is_ribbon) VALUES ($1, $2, now(), '
                    'now() + make_interval(mins => $3), $4) RETURNING id',
                    int(player_id), int(template_id), int(duration_minutes),
                    bool(is_ribbon))
            else:
                bid = await c.fetchval(
                    'INSERT INTO player_boosters (player_id, booster_template_id, '
                    'is_ribbon) VALUES ($1, $2, $3) RETURNING id',
                    int(player_id), int(template_id), bool(is_ribbon))
        return int(bid)

    async def activate_booster(self, player_id, booster_row_id, duration_minutes=240):
        """激活已购加成（ActivateBooster/ActivateVariousBoosterRequest）。
        返回 ResponseCodes：1 ok / 208 booster_not_found / 209 already_activated。
        时长按行模板分表解析：勋带行查 RibbonBoosterTemplate、通用行查
        BoosterTemplate（两表 id 有重叠但时长不同，如 #3001 勋带 2880min），
        模板查不到回退 duration_minutes 参数。"""
        # 循环依赖约定：wardb 延迟导入（上提会成 store→wardb→state→store 环，
        # 见 store/__init__.py 头注释）
        from wardb import BOOSTER_MINUTES_BY_ID, XP_BOOSTER_MINUTES_BY_ID
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, activated_at, booster_template_id, is_ribbon '
                'FROM player_boosters '
                'WHERE id = $1 AND player_id = $2',
                int(booster_row_id), int(player_id))
            if row is None:
                return 208
            if row['activated_at'] is not None:
                return 209
            minutes = ((BOOSTER_MINUTES_BY_ID if row['is_ribbon']
                        else XP_BOOSTER_MINUTES_BY_ID).get(
                            row['booster_template_id'], duration_minutes))
            await conn.execute(
                'UPDATE player_boosters SET activated_at = now(), '
                'expires_at = now() + make_interval(mins => $2) WHERE id = $1',
                int(booster_row_id), int(minutes))
        return 1

    async def add_ticket(self, player_id, template_id, is_timed=False,
                         duration_hours=72, conn=None):
        """购买门票（AddTicketRequest）。返回 ticket 行 id。
        传入 conn 时并入外部事务（与扣款同生共死）。"""
        async with self._conn(conn) as c:
            if is_timed:
                tid = await c.fetchval(
                    'INSERT INTO player_tickets (player_id, ticket_template_id, '
                    'expires_at) VALUES ($1, $2, '
                    'now() + make_interval(hours => $3)) RETURNING id',
                    int(player_id), int(template_id), int(duration_hours))
            else:
                tid = await c.fetchval(
                    'INSERT INTO player_tickets (player_id, ticket_template_id) '
                    'VALUES ($1, $2) RETURNING id',
                    int(player_id), int(template_id))
        return int(tid)

    async def activate_ticket(self, player_id, ticket_row_id, duration_hours=72):
        """激活限时门票（ActivateTimedTicketRequest）。
        返回 1 ok / 208 not found / 209 already activated。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, expires_at FROM player_tickets '
                'WHERE id = $1 AND player_id = $2',
                int(ticket_row_id), int(player_id))
            if row is None:
                return 208
            if row['expires_at'] is not None:
                return 209
            await conn.execute(
                'UPDATE player_tickets SET expires_at = '
                'now() + make_interval(hours => $2) WHERE id = $1',
                int(ticket_row_id), int(duration_hours))
        return 1

    async def list_boosters(self, player_id):
        """玩家加成行（Booster/RibbonBooster KVCS 源）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, booster_template_id, activated_at, expires_at, '
                'is_ribbon FROM player_boosters WHERE player_id = $1 '
                'ORDER BY id',
                int(player_id))
        return [{'id': int(r['id']),
                 'template_id': int(r['booster_template_id']),
                 'activated_at': r['activated_at'],
                 'expires_at': r['expires_at'],
                 'is_ribbon': bool(r['is_ribbon'])} for r in rows]

    async def list_active_boosters(self, player_id, ribbon_only=False):
        """激活中的加成行（activated_at 非空且未过期；未激活/已过期不发）。

        战斗 PlayerSettings charactersetting f5 ribbonBoosters 的数据源；
        ribbon_only=True 只取勋带加成（is_ribbon）。"""
        sql = ('SELECT id, booster_template_id, is_ribbon FROM player_boosters '
               'WHERE player_id = $1 AND activated_at IS NOT NULL '
               'AND expires_at > now()')
        if ribbon_only:
            sql += ' AND is_ribbon'
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql + ' ORDER BY id', int(player_id))
        return [{'id': int(r['id']),
                 'template_id': int(r['booster_template_id']),
                 'is_ribbon': bool(r['is_ribbon'])} for r in rows]

    async def list_tickets(self, player_id, timed_only=False):
        """玩家门票行（Ticket/TicketTimed KVCS 源）。"""
        if timed_only:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    'SELECT id, ticket_template_id, expires_at FROM player_tickets '
                    'WHERE player_id = $1 AND expires_at IS NOT NULL ORDER BY id',
                    int(player_id))
        else:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    'SELECT id, ticket_template_id, expires_at FROM player_tickets '
                    'WHERE player_id = $1 ORDER BY id', int(player_id))
        return [{'id': int(r['id']),
                 'template_id': int(r['ticket_template_id']),
                 'expires_at': r['expires_at']} for r in rows]

    async def list_memberships(self, player_id):
        """玩家会员行（PlayerMembershipItem KVCS 源）。

        进行中行（expires_at 非空，过期行已由 sweep_memberships 删除）
        在前、排队行（expires_at IS NULL）在后——客户端 getCurrentMembership
        取表中第一行判 isPremium，行序即推送序，必须 running 在最前。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, membership_template_id, started_at, expires_at '
                'FROM player_memberships WHERE player_id = $1 '
                'ORDER BY (expires_at IS NULL), id',
                int(player_id))
        return [{'id': int(r['id']),
                 'template_id': int(r['membership_template_id']),
                 'started_at': r['started_at'],
                 'expires_at': r['expires_at']} for r in rows]

    async def get_username(self, player_id):
        """按 id 查用户名，不存在返回 None。"""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                'SELECT username FROM players WHERE id = $1', int(player_id))
