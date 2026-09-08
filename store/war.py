# -*- coding: utf-8 -*-
"""战争（RTS）域：war/city/command_node/movement/battle/funds/resources/supply。
拆分自 player_store.py（纯移动重构，方法体逐字保留）。
"""
import json
from .base import _rowcount

class WarStoreMixin:
    """战争（RTS）域：war/city/command_node/movement/battle/funds/resources/supply。"""

    # ================================================================
    # RTS 战争域（docs/hng-rts-protocol.md Phase 2；war_campaign.py 使用）
    # ================================================================

    _WAR_COLUMNS = ('id, name, begintime, endtime, winning_faction_id, '
                    'points_needed_for_victory, sequel_war_id, mapid, state, '
                    'config_json, production_ms')

    @staticmethod
    def _war_row(row):
        if row is None:
            return None
        d = dict(row)
        cfg = d.pop('config_json')
        if isinstance(cfg, (str, bytes)):
            # asyncpg 无 codec 时 JSONB 返回 str（2026-09-03 VPS 实锤：
            # build_war_proto 拿到 str 直接 AttributeError，登录卡死）
            try:
                cfg = json.loads(cfg) if cfg else {}
            except ValueError:
                cfg = {}
        d['config'] = cfg or {}
        return d

    async def war_count(self):
        """历史战争总数（wars 只增不删，count 即战争轮次；_next_war_name 用）。"""
        async with self.pool.acquire() as conn:
            return int(await conn.fetchval('SELECT count(*) FROM wars'))

    async def create_war(self, name, points_needed_for_victory=15, mapid=1,
                         config=None):
        """开一场新战争（war_campaign.ensure_war）。返回 war 行 dict。"""
        import json as _json
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO wars (name, points_needed_for_victory, mapid, '
                'config_json) VALUES ($1, $2, $3, $4) RETURNING ' + self._WAR_COLUMNS,
                str(name or ''), int(points_needed_for_victory or 15),
                int(mapid or 1), _json.dumps(config or {}))
        return self._war_row(row)

    async def get_war(self, war_id):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT ' + self._WAR_COLUMNS + ' FROM wars WHERE id = $1',
                int(war_id))
        return self._war_row(row)

    async def get_active_war(self):
        """当前 running 的战争（state=1）；无则 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT ' + self._WAR_COLUMNS +
                ' FROM wars WHERE state = 1 ORDER BY id DESC LIMIT 1')
        return self._war_row(row)

    async def list_wars(self, limit=20):
        """战争目录列表（新→旧，war_campaign.build_war_catalogue 用）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT ' + self._WAR_COLUMNS +
                ' FROM wars ORDER BY id DESC LIMIT $1', int(limit))
        return [self._war_row(r) for r in rows]

    async def end_war(self, war_id, winning_faction_id, sequel_war_id=0):
        """战争结算（state=2 + 胜方 + 续战 id）。返回是否更新。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE wars SET state = 2, endtime = now(), '
                'winning_faction_id = $2, sequel_war_id = $3 WHERE id = $1 '
                'AND state = 1',
                int(war_id), int(winning_faction_id or 0),
                int(sequel_war_id or 0))
        return _rowcount(n) > 0

    async def update_war_sequel(self, war_id, sequel_war_id):
        """回填续战 id（war_campaign.check_victory 续战后调用）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE wars SET sequel_war_id = $2 WHERE id = $1',
                int(war_id), int(sequel_war_id or 0))

    # ---- war_cities ----

    async def init_war_cities(self, war_id, owner_map):
        """批量写入城市归属 {battlefield_id: owner_faction}（开战时一次性）。"""
        if not owner_map:
            return
        async with self.pool.acquire() as conn:
            await conn.executemany(
                'INSERT INTO war_cities (war_id, battlefield_id, owner_faction) '
                'VALUES ($1, $2, $3) ON CONFLICT (war_id, battlefield_id) '
                'DO UPDATE SET owner_faction = EXCLUDED.owner_faction',
                [(int(war_id), int(bf), int(owner))
                 for bf, owner in owner_map.items()])

    async def get_war_cities(self, war_id):
        """[(battlefield_id, owner_faction), ...]。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT battlefield_id, owner_faction FROM war_cities '
                'WHERE war_id = $1 ORDER BY battlefield_id', int(war_id))
        return [(r['battlefield_id'], r['owner_faction']) for r in rows]

    async def set_city_owner(self, war_id, battlefield_id, owner_faction):
        async with self.pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO war_cities (war_id, battlefield_id, owner_faction) '
                'VALUES ($1, $2, $3) ON CONFLICT (war_id, battlefield_id) '
                'DO UPDATE SET owner_faction = $3',
                int(war_id), int(battlefield_id), int(owner_faction))

    async def count_war_city_owners(self, war_id, battlefield_ids=None):
        """各阵营城市占有数 {faction: count}（首都名单由 wardb.battlefields
        提供静态判定，war_campaign 层负责交集过滤）。"""
        wanted = (None if battlefield_ids is None
                  else set(int(b) for b in battlefield_ids))
        counts = {}
        for bf, owner in await self.get_war_cities(war_id):
            if owner > 0 and (wanted is None or bf in wanted):
                counts[owner] = counts.get(owner, 0) + 1
        return counts

    # ---- command_nodes（AT 实例）----

    async def create_command_node(self, war_id, template_id, owner_player_id,
                                  faction, name, resources=None,
                                  abbreviation='', rank=0,
                                  parent_command_node_id=0):
        """购买 AT → 新实例（state=0 首都待命）。返回 AT 行 dict。"""
        import json as _json
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO command_nodes (war_id, template_id, '
                'owner_player_id, faction, name, abbreviation, rank, '
                'resources, parent_command_node_id) '
                'VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) '
                'RETURNING *',
                int(war_id), int(template_id), int(owner_player_id),
                int(faction), str(name or ''), str(abbreviation or ''),
                int(rank or 0), _json.dumps(resources or {}),
                int(parent_command_node_id or 0))
        return dict(row) if row else None

    async def get_command_node(self, node_id):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT * FROM command_nodes WHERE id = $1', int(node_id))
        return dict(row) if row else None

    async def list_player_command_nodes(self, player_id, war_id=None):
        """玩家名下 AT 列表（war_id=None 取全部战争）。"""
        q = ('SELECT * FROM command_nodes WHERE owner_player_id = $1 '
             'AND state < 3')
        args = [int(player_id)]
        if war_id is not None:
            q += ' AND war_id = $2'
            args.append(int(war_id))
        q += ' ORDER BY id'
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q, *args)
        return [dict(r) for r in rows]

    async def list_war_command_nodes(self, war_id):
        """本战争全部存活 AT（含各玩家），warmapview 快照用。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT * FROM command_nodes WHERE war_id = $1 AND state < 3 '
                'ORDER BY id', int(war_id))
        return [dict(r) for r in rows]

    async def get_command_nodes_by_ids(self, node_ids):
        """按 id 集合查 AT（M2 会战兵力聚合：war_battles.factions_json）。"""
        ids = [int(i) for i in (node_ids or []) if int(i)]
        if not ids:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT * FROM command_nodes WHERE id = ANY($1::bigint[]) '
                'ORDER BY id', ids)
        return [dict(r) for r in rows]

    async def update_command_node(self, node_id, **fields):
        """AT 字段更新（name/rank/xp/battles/battles_won/resources/morale/
        state/map_entity_id/map_entity_type/general_character_id/is_deployed/
        abbreviation）。rank/xp/battles/battles_won 任一变更时自动刷新
        stats_updated_at（Definition f10 → 客户端重拉 GetAssaultTeamStats）。
        返回是否更新。"""
        allowed = {'name', 'abbreviation', 'rank', 'xp', 'battles',
                   'battles_won', 'resources', 'morale',
                   'max_morale', 'state', 'map_entity_id', 'map_entity_type',
                   'general_character_id', 'is_deployed', 'upgrade_level',
                   'owner_player_id', 'faction', 'template_id',
                   'has_gotten_soldier_free', 'has_gotten_vehicle_free',
                   'parent_command_node_id'}
        import json as _json
        sets, args = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == 'resources' and not isinstance(v, str):
                v = _json.dumps(v or {})
            args.append(v)
            sets.append(f'{k} = ${len(args)}')
        if not sets:
            return False
        if {'rank', 'xp', 'battles', 'battles_won'} & fields.keys():
            sets.append('stats_updated_at = now()')
        args.append(int(node_id))
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                f'UPDATE command_nodes SET {", ".join(sets)} '
                f'WHERE id = ${len(args)}', *args)
        return _rowcount(n) > 0

    async def delete_command_node(self, node_id):
        """解散 AT（state=3 标记，保留历史行）。返回是否更新。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE command_nodes SET state = 3 WHERE id = $1 AND state < 3',
                int(node_id))
        return _rowcount(n) > 0

    # ---- commandnode_movements ----

    async def create_movement(self, commandnode_id, war_id, path,
                              started_at=None, arrive_at=None, dest_ap=0):
        """AT 移动登记（path=段列表；dest_ap=目标出兵点，0=城市本体）。"""
        import json as _json
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO commandnode_movements (commandnode_id, war_id, '
                'path_json, started_at, arrive_at, dest_ap) '
                'VALUES ($1, $2, $3, $4, $5, $6) '
                'RETURNING id, commandnode_id, war_id, path_json, started_at, '
                'arrive_at, state, dest_ap',
                int(commandnode_id), int(war_id), _json.dumps(list(path or [])),
                started_at, arrive_at, int(dest_ap or 0))
        if row:
            d = dict(row)
            import json as _j
            d['path'] = _j.loads(d.pop('path_json') or '[]')
            return d
        return None

    async def get_active_movement(self, commandnode_id):
        """AT 当前进行中的移动（state=0）；无则 None。"""
        import json as _j
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, commandnode_id, war_id, path_json, started_at, '
                'arrive_at, state, dest_ap FROM commandnode_movements '
                'WHERE commandnode_id = $1 AND state = 0 '
                'ORDER BY id DESC LIMIT 1', int(commandnode_id))
        if not row:
            return None
        d = dict(row)
        d['path'] = _j.loads(d.pop('path_json') or '[]')
        return d

    async def list_active_movements(self, war_id):
        """本战争全部进行中移动（增量推进器轮询用）。"""
        import json as _j
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, commandnode_id, war_id, path_json, started_at, '
                'arrive_at, state, dest_ap FROM commandnode_movements '
                'WHERE war_id = $1 AND state = 0 ORDER BY id', int(war_id))
        out = []
        for r in rows:
            d = dict(r)
            d['path'] = _j.loads(d.pop('path_json') or '[]')
            out.append(d)
        return out

    async def shift_movement_started(self, movement_id, seconds):
        """在途行军整体顺延（野外会战冻结恢复：started_at += seconds，
        段绝对时间由 movement_segments 从 started_at 累加，均匀后移）。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE commandnode_movements SET started_at = '
                'started_at + make_interval(secs => $2) '
                'WHERE id = $1 AND state = 0',
                int(movement_id), float(seconds or 0))
        return _rowcount(n) > 0

    async def update_movement_path(self, movement_id, path,
                                   arrive_at=None, dest_ap=0):
        """在途移动改道：整体替换 path_json（前缀段保持原时间轴，
        started_at 不变），同步 arrive_at / dest_ap。返回更新后的
        movement 行（无进行中行返回 None）。"""
        import json as _json
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'UPDATE commandnode_movements SET path_json = $2, '
                'arrive_at = COALESCE($3, arrive_at), dest_ap = $4 '
                'WHERE id = $1 AND state = 0 '
                'RETURNING id, commandnode_id, war_id, path_json, started_at, '
                'arrive_at, state, dest_ap',
                int(movement_id), _json.dumps(list(path or [])),
                arrive_at, int(dest_ap or 0))
        if not row:
            return None
        d = dict(row)
        d['path'] = _json.loads(d.pop('path_json') or '[]')
        return d

    async def finish_movement(self, movement_id, state=1):
        """移动结束（1=arrived 2=cancelled）。返回是否更新。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE commandnode_movements SET state = $2 '
                'WHERE id = $1 AND state = 0', int(movement_id), int(state))
        return _rowcount(n) > 0

    # ---- war_battles（M1 桩）----

    async def create_war_battle(self, war_id, battlefield_id, factions,
                                battle_type=1, position=0.0):
        """敌对 AT 相遇 → 会战登记（factions=[{at_id, faction}]）。

        battle_type：官方 missiontype（1=assault 城战 / 3=skirmish /
        5=encounter，war_campaign.BATTLE_TYPE_*）；position：补给线会战
        位置（0-1，城战 0）。返回行。"""
        import json as _json
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO war_battles (war_id, battlefield_id, factions_json, '
                'battle_type, position) '
                'VALUES ($1, $2, $3, $4, $5) RETURNING id, war_id, '
                'battlefield_id, factions_json, battle_type, position, '
                'state, winning_faction, started_at, ended_at',
                int(war_id), int(battlefield_id),
                _json.dumps(list(factions or [])),
                int(battle_type or 1), float(position or 0.0))
        if not row:
            return None
        d = dict(row)
        d['factions'] = _json.loads(d.pop('factions_json') or '[]')
        return d

    async def append_war_battle_faction(self, battle_id, at_id, faction):
        """追加参战 AT（参战阵营增援/野外遭遇后到方）。返回 factions 列表。

        未追加到 factions 名单的 state=2 AT 结算遍历不到 → 永不释放
        （会战生命周期审计 #2 关联修复）。已存在幂等返回。"""
        wb = await self.get_war_battle(battle_id)
        if wb is None:
            return None
        import json as _json
        factions = list(wb.get('factions') or [])
        if any(int(f.get('at_id') or 0) == int(at_id) for f in factions):
            return factions
        factions.append({'at_id': int(at_id), 'faction': int(faction or 0)})
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE war_battles SET factions_json = $2 WHERE id = $1',
                int(battle_id), _json.dumps(factions))
        return factions

    async def list_active_war_battles(self, war_id):
        import json as _j
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, war_id, battlefield_id, factions_json, '
                'battle_type, position, state, winning_faction, started_at, '
                'ended_at FROM war_battles '
                'WHERE war_id = $1 AND state = 0 ORDER BY id', int(war_id))
        out = []
        for r in rows:
            d = dict(r)
            d['factions'] = _j.loads(d.pop('factions_json') or '[]')
            out.append(d)
        return out

    async def get_war_battle(self, battle_id):
        """单查会战（含已结束）；无则 None。"""
        import json as _j
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, war_id, battlefield_id, factions_json, '
                'battle_type, position, state, winning_faction, started_at, '
                'ended_at FROM war_battles WHERE id = $1', int(battle_id))
        if not row:
            return None
        d = dict(row)
        d['factions'] = _j.loads(d.pop('factions_json') or '[]')
        return d

    async def end_war_battle(self, battle_id, winning_faction=0):
        """会战结束（state=1，M2：写入胜方阵营）。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE war_battles SET state = 1, ended_at = now(), '
                'winning_faction = $2 WHERE id = $1 AND state = 0',
                int(battle_id), int(winning_faction))
        return _rowcount(n) > 0

    async def sweep_ended_war_leftovers(self):
        """已结束战争的残留清理（war_tick 兜底；强制结束/续战竞态遗留）：
        ① state=0 会战所属战争已非 running → 关账（state=1，无胜方）；
        ② state=2（会战中）AT 所属战争已非 running → 释放回待命（state=0）。
        automission 只结算当前战争，旧战争孤儿会战靠此兜底。
        返回 (closed_battle_ids, released_at_ids)。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                closed = [int(r['id']) for r in await conn.fetch(
                    'UPDATE war_battles b SET state = 1, ended_at = now() '
                    'FROM wars w WHERE w.id = b.war_id AND w.state <> 1 '
                    'AND b.state = 0 RETURNING b.id')]
                released = [int(r['id']) for r in await conn.fetch(
                    'UPDATE command_nodes c SET state = 0 '
                    'FROM wars w WHERE w.id = c.war_id AND w.state <> 1 '
                    'AND c.state = 2 RETURNING c.id')]
        return closed, released

    # ---- warfunds 流水 ----

    async def add_war_funds(self, player_id, amount, reason=''):
        """战争资金变动（事务：余额 + 流水）。amount 可负；玩家不存在时
        事务回滚并抛 ValueError。返回新余额。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                balance = await conn.fetchval(
                    'UPDATE players SET warfunds = warfunds + $2 '
                    'WHERE id = $1 RETURNING warfunds',
                    int(player_id), int(amount))
                if balance is None:
                    raise ValueError(f'player {player_id} not found')
                await conn.execute(
                    'INSERT INTO war_funds_ledger (player_id, amount, reason, '
                    'balance_after) VALUES ($1, $2, $3, $4)',
                    int(player_id), int(amount), str(reason or ''),
                    int(balance))
        return balance

    # ---- war_faction_resources（M3 阵营资源库存）----

    async def init_war_faction_resources(self, war_id, initial):
        """幂等初始化阵营库存。initial={faction: {category_id: count}}；
        已有行不覆盖（ON CONFLICT DO NOTHING）。"""
        rows = []
        for fid, cats in (initial or {}).items():
            for cat, cnt in cats.items():
                if cnt:
                    rows.append((int(war_id), int(fid), int(cat), int(cnt)))
        if not rows:
            return
        async with self.pool.acquire() as conn:
            await conn.executemany(
                'INSERT INTO war_faction_resources '
                '(war_id, faction, category_id, count) '
                'VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING', rows)

    async def list_war_faction_resources(self, war_id, faction=None):
        """阵营库存行。faction=None 时全阵营。"""
        sql = ('SELECT war_id, faction, category_id, count '
               'FROM war_faction_resources WHERE war_id = $1')
        args = [int(war_id)]
        if faction is not None:
            sql += ' AND faction = $2'
            args.append(int(faction))
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql + ' ORDER BY faction, category_id', *args)
        return [dict(r) for r in rows]

    async def adjust_war_faction_resources(self, war_id, faction, deltas):
        """按类别批量增减库存（delta 可负，clamp 到 0）。返回更新后行。"""
        if not deltas:
            return []
        sets = sorted((int(cat), int(d)) for cat, d in deltas.items() if d)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                for cat, d in sets:
                    await conn.execute(
                        'INSERT INTO war_faction_resources '
                        '(war_id, faction, category_id, count) '
                        'VALUES ($1, $2, $3, GREATEST($4, 0)) '
                        'ON CONFLICT (war_id, faction, category_id) DO UPDATE '
                        'SET count = GREATEST(war_faction_resources.count + $4, 0)',
                        int(war_id), int(faction), cat, d)
            rows = await conn.fetch(
                'SELECT war_id, faction, category_id, count '
                'FROM war_faction_resources WHERE war_id = $1 AND faction = $2 '
                'ORDER BY category_id', int(war_id), int(faction))
        return [dict(r) for r in rows]

    async def update_war_production(self, war_id, production_ms):
        """推进生产游标（M3 tick）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE wars SET production_ms = $2 WHERE id = $1',
                int(war_id), int(production_ms))

    # ---- war_supply_queue（M3-2 阵营资源增援队列）----

    async def enqueue_war_supply(self, war_id, faction, command_node_id,
                                 items):
        """AT 增援缺口入队（FIFO）：items={category_id: count}，逐行写
        queue_number = 阵营内 COALESCE(MAX,0)+1 单调分配。返回入队行数。"""
        rows = [(int(war_id), int(faction), int(command_node_id),
                 int(cat), int(cnt)) for cat, cnt in (items or {}).items()
                if int(cnt) > 0]
        if not rows:
            return 0
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                for wid, fac, cnid, cat, cnt in rows:
                    await conn.execute(
                        'INSERT INTO war_supply_queue '
                        '(war_id, faction, command_node_id, category_id, '
                        'count, queue_number) VALUES '
                        '($1, $2, $3, $4, $5, COALESCE('
                        '(SELECT MAX(queue_number) FROM war_supply_queue '
                        ' WHERE war_id = $1 AND faction = $2), 0) + 1)',
                        wid, fac, cnid, cat, cnt)
        return len(rows)

    async def list_war_supply_queue(self, war_id, faction=None,
                                    command_node_id=None):
        """队列行（FIFO 序：faction, queue_number）。可按阵营/AT 过滤。"""
        sql = ('SELECT id, war_id, faction, command_node_id, category_id, '
               'count, queue_number, created_at '
               'FROM war_supply_queue WHERE war_id = $1')
        args = [int(war_id)]
        if faction is not None:
            sql += ' AND faction = $2'
            args.append(int(faction))
        if command_node_id is not None:
            sql += f' AND command_node_id = ${len(args) + 1}'
            args.append(int(command_node_id))
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql + ' ORDER BY faction, queue_number',
                                    *args)
        return [dict(r) for r in rows]

    async def dequeue_war_supply(self, row_ids):
        """按行 id 批量出队（交付/AT 歼灭解散清理）。返回删除行数。"""
        ids = [int(i) for i in (row_ids or [])]
        if not ids:
            return 0
        async with self.pool.acquire() as conn:
            res = await conn.execute(
                'DELETE FROM war_supply_queue WHERE id = ANY($1::bigint[])',
                ids)
        try:
            return int(res.split()[-1])
        except (ValueError, IndexError):
            return len(ids)
