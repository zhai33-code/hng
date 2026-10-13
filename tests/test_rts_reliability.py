"""RTS regression scenarios with real transaction binding and fault injection.

Run directly. The connection adapter models advisory locks and rollback, without
requiring a developer PostgreSQL database; it does not validate PostgreSQL SQL.
"""
import asyncio
import copy
import datetime
import os
import uuid
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

import test_war_supply_column as f
import war_campaign as wc
from war_campaign import production, supply
from store.war import WarStoreMixin


DATA = ('wars', 'city_owners', 'ats', 'movements', 'battles', 'balances',
        'stock', 'supply_queue', 'adjust_log', 'spend_log')


class Connection:
    def __init__(self, root):
        self.root = root
        self.held = []

    @asynccontextmanager
    async def transaction(self):
        self.snapshot = None
        try:
            yield
        except BaseException:
            for key, value in (self.snapshot or {}).items():
                current = getattr(self.root, key)
                current.clear()
                current.update(value) if isinstance(current, dict) else current.extend(value)
            raise
        finally:
            for lock in reversed(self.held):
                lock.release()
            self.held.clear()

    async def execute(self, sql, *args):
        if 'pg_advisory_xact_lock' not in sql:
            raise AssertionError(f'Unexpected SQL in transaction adapter: {sql}')
        lock = self.root.db_locks.setdefault(tuple(args), asyncio.Lock())
        await lock.acquire()
        self.held.append(lock)
        self.snapshot = {key: copy.deepcopy(getattr(self.root, key)) for key in DATA}


class Pool:
    def __init__(self, root):
        self.root = root

    @asynccontextmanager
    async def acquire(self):
        yield Connection(self.root)


class Store(f.FakeStore):
    rts_transaction = WarStoreMixin.rts_transaction

    def __init__(self):
        super().__init__()
        self.root = self
        self.pool = Pool(self)
        self.db_locks = {}
        self.fail_at = None

    def fail(self, stage):
        if self.root.fail_at == stage:
            self.root.fail_at = None
            raise RuntimeError(f'injected {stage} failure')

    async def get_active_war(self):
        snapshot = await f.FakeStore.get_active_war(self.root)
        await asyncio.sleep(0)  # A real SELECT yields to competing requests.
        return snapshot

    async def create_war(self, *args, **kwargs):
        return await f.FakeStore.create_war(self.root, *args, **kwargs)

    async def create_command_node(self, *args, **kwargs):
        return await f.FakeStore.create_command_node(self.root, *args, **kwargs)

    async def create_movement(self, *args, **kwargs):
        return await f.FakeStore.create_movement(self.root, *args, **kwargs)

    async def init_war_cities(self, *args):
        await f.FakeStore.init_war_cities(self.root, *args)
        self.fail('initialization')

    async def adjust_war_faction_resources(self, *args):
        result = await f.FakeStore.adjust_war_faction_resources(self.root, *args)
        self.fail('stock')
        return result

    async def update_war_production(self, *args):
        war_id, cursor = args
        self.root.wars[war_id]['production_ms'] = cursor
        self.fail('cursor')

    async def dequeue_war_supply(self, *args):
        result = await f.FakeStore.dequeue_war_supply(self.root, *args)
        self.fail('dequeue')
        return result

    async def update_command_node(self, *args, **kwargs):
        result = await f.FakeStore.update_command_node(self.root, *args, **kwargs)
        self.fail('node')
        return result

    async def get_mission_by_battle(self, battle_id):
        return {'id': 700 + int(battle_id)}

    async def get_war_battle(self, battle_id):
        return copy.deepcopy(self.battles.get(int(battle_id)))

    async def create_war_battle(self, war_id, battlefield_id, factions,
                               battle_type=1, position=0):
        wb = await f.FakeStore.create_war_battle(self.root, war_id, battlefield_id, factions)
        self.battles[wb['id']].update(battle_type=battle_type, position=position)
        return copy.deepcopy(self.battles[wb['id']])

    async def shift_movement_started(self, mv_id, seconds):
        mv = self.movements[mv_id]
        shift = datetime.timedelta(seconds=seconds)
        mv['started_at'] += shift
        if mv.get('arrive_at'):
            mv['arrive_at'] += shift
        return True


class RTSReliability(unittest.IsolatedAsyncioTestCase):
    async def setup_world(self, owners=None):
        st = Store()
        war = await f._war(st, owners or {10: 1, 20: 1, 30: 1})
        st.plentiful = False
        parent = f._mk_at(st, 500, deployed=True, map_entity_id=30, resources={'1': 2})
        return st, war, parent

    async def enqueue(self, st, parent=500, count=5, column=True):
        await st.enqueue_war_supply(1, 1, parent, {1: count})
        st.supply_queue[-1].update(deliver_as_column=column,
                                   near_capital_battlefield_id=10)
        st.stock[(1, 1, 1)] = 20

    async def test_failed_push_does_not_redeliver(self):
        st, war, _ = await self.setup_world()
        await self.enqueue(st)
        async def broken(*args):
            raise RuntimeError('push failed')
        with patch.object(wc.tick, 'broadcast_war_events', broken):
            await wc.supply_queue_tick(st)
        self.assertEqual(st.supply_queue, [])
        self.assertEqual(st.stock[(1, 1, 1)], 15)
        await wc.supply_queue_tick(st)
        self.assertEqual(len(f._cols(st, 500)), 1)
        self.assertEqual(st.stock[(1, 1, 1)], 15)

    async def test_queue_write_failure_rolls_back_and_retry_delivers_once(self):
        for stage in ('node', 'stock', 'dequeue'):
            with self.subTest(stage=stage):
                st, war, _ = await self.setup_world()
                await self.enqueue(st)
                st.fail_at = stage
                await wc.supply_queue_tick(st)
                self.assertEqual(st.stock[(1, 1, 1)], 20)
                self.assertEqual(len(st.supply_queue), 1)
                self.assertEqual(f._cols(st, 500), [])
                self.assertEqual(st.movements, {})
                await wc.supply_queue_tick(st)
                self.assertEqual(st.stock[(1, 1, 1)], 15)
                self.assertEqual(len(f._cols(st, 500)), 1)
                self.assertEqual(st.supply_queue, [])

    async def test_immediate_delivery_failure_rolls_back_parent(self):
        st, war, _ = await self.setup_world()
        await self.enqueue(st, column=False)
        st.fail_at = 'dequeue'
        await wc.supply_queue_tick(st)
        self.assertEqual(st.ats[500]['resources'], {'1': 2})
        self.assertEqual(st.stock[(1, 1, 1)], 20)
        await wc.supply_queue_tick(st)
        self.assertEqual(st.ats[500]['resources'], {'1': 7})
        self.assertEqual(st.stock[(1, 1, 1)], 15)

    async def test_concurrent_queue_ticks_only_deliver_once(self):
        st, war, _ = await self.setup_world()
        await self.enqueue(st)
        await asyncio.gather(wc.supply_queue_tick(st), wc.supply_queue_tick(st))
        self.assertEqual(len(f._cols(st, 500)), 1)
        self.assertEqual(st.stock[(1, 1, 1)], 15)

    async def test_dead_and_zero_heads_removed_before_stock_check(self):
        st, war, _ = await self.setup_world()
        await self.enqueue(st, parent=999, count=100)
        await self.enqueue(st)
        st.supply_queue.insert(0, dict(st.supply_queue[0], id=99, count=0))
        await wc.supply_queue_tick(st)
        self.assertEqual(st.supply_queue, [])
        self.assertEqual(len(f._cols(st, 500)), 1)
        self.assertEqual(st.stock[(1, 1, 1)], 15)

    async def test_valid_unfunded_head_preserves_fifo(self):
        st, war, _ = await self.setup_world()
        await self.enqueue(st, count=100)
        await self.enqueue(st)
        await wc.supply_queue_tick(st)
        self.assertEqual(len(st.supply_queue), 2)
        self.assertEqual(f._cols(st, 500), [])

    async def test_production_rollback_cursor_failure_and_no_double_credit(self):
        for stage in ('stock', 'cursor'):
            with self.subTest(stage=stage):
                st, war, _ = await self.setup_world()
                now = 2000000000.
                cursor = int(now * 1000) - 600000
                st.wars[1]['production_ms'] = cursor
                rows = [{'produce_per_run': 10, 'run_every_seconds': 600,
                         'faction': 1, 'category_id': 1}]
                with patch.object(production.time, 'time', return_value=now), \
                        patch.object(f.wardb, 'armyresource_production', return_value=rows):
                    st.fail_at = stage
                    with patch.object(production, '_PROD_LAST_RUN', 0):
                        await production.production_tick(st)
                    self.assertEqual(st.stock, {})
                    self.assertEqual(st.wars[1]['production_ms'], cursor)
                    with patch.object(production, '_PROD_LAST_RUN', 0):
                        await production.production_tick(st)
                    self.assertEqual(st.stock[(1, 1, 1)], 10)
                    with patch.object(production, '_PROD_LAST_RUN', 0):
                        await production.production_tick(st)
                    self.assertEqual(st.stock[(1, 1, 1)], 10)

    async def test_concurrent_creation_and_atomic_initialization(self):
        st = Store()
        results = await asyncio.gather(wc.ensure_war(st), wc.ensure_war(st))
        self.assertEqual(results[0]['id'], results[1]['id'])
        self.assertEqual(len(st.wars), 1)
        self.assertIn(results[0]['id'], st.city_owners)
        failed = Store()
        failed.fail_at = 'initialization'
        self.assertIsNone(await wc.ensure_war(failed))
        self.assertEqual(failed.wars, {})
        self.assertEqual(failed.city_owners, {})
        retry = await wc.ensure_war(failed)
        self.assertIsNotNone(retry)
        self.assertEqual(len(failed.wars), 1)

    async def test_concurrent_production_credits_each_interval_once(self):
        st, war, _ = await self.setup_world()
        now = 2000000000.
        st.wars[1]['production_ms'] = int(now * 1000) - 600000
        rows = [{'produce_per_run': 10, 'run_every_seconds': 600,
                 'faction': 1, 'category_id': 1}]
        async def tick():
            production._PROD_LAST_RUN = 0
            await production.production_tick(st)
        with patch.object(production.time, 'time', return_value=now), \
                patch.object(f.wardb, 'armyresource_production', return_value=rows), \
                patch.object(production, '_PROD_LAST_RUN', 0):
            await asyncio.gather(tick(), tick())
        self.assertEqual(st.stock[(1, 1, 1)], 10)
        self.assertEqual(st.wars[1]['production_ms'], int(now * 1000))

    async def test_cancelled_transaction_rolls_back_and_skips_callbacks(self):
        st, war, _ = await self.setup_world()
        callbacks = []
        async def committed():
            callbacks.append(True)
        with self.assertRaises(asyncio.CancelledError):
            async with st.rts_transaction('production', war['id']) as bound:
                await bound.adjust_war_faction_resources(war['id'], 1, {1: 10})
                bound._rts_after_commit.append(committed)
                raise asyncio.CancelledError()
        self.assertEqual(st.stock, {})
        self.assertEqual(callbacks, [])

    async def test_late_tick_checks_guard_and_captures_all_intermediate_cities(self):
        st, war, _ = await self.setup_world({10: 1, 20: 2, 30: 1})
        st.ats[500].update(map_entity_id=10)
        f._mk_at(st, 600, template=201, faction=2, deployed=True, map_entity_id=20)
        mv, _ = await wc.start_movement(st, st.ats[500], 30, now=f._NOW)
        await wc.advance_movements(st, war, now=mv['arrive_at'] + datetime.timedelta(seconds=1))
        self.assertEqual((st.ats[500]['map_entity_id'], st.ats[500]['state']), (20, 2))
        self.assertEqual(len(st.battles), 1)
        empty, w2, _ = await self.setup_world({10: 1, 20: 0, 30: 1})
        empty.ats[500]['map_entity_id'] = 10
        mv2, _ = await wc.start_movement(empty, empty.ats[500], 30, now=f._NOW)
        await wc.advance_movements(empty, w2, now=mv2['arrive_at'] + datetime.timedelta(seconds=1))
        self.assertEqual(empty.city_owners[1][20], 1)
        self.assertEqual(empty.ats[500]['map_entity_id'], 30)

    async def test_finished_city_battle_releases_before_encirclement(self):
        st, war, _ = await self.setup_world({10: 2, 20: 2, 30: 2})
        col = f._mk_at(st, 700, template=104, deployed=True, map_entity_id=20,
                       resources={'1': 5})
        st.ats[700].update(state=2, parent_command_node_id=500,
            supply_state={'source': 'purchase', 'blocked_bf': 20, 'blocked_battle_id': 1})
        st.battles[1] = {'id': 1, 'war_id': 1, 'battlefield_id': 20,
                         'state': 1, 'battle_type': 1, 'factions': []}
        await wc._cleanup_supply_columns(st, war)
        self.assertEqual(st.ats[700]['state'], 1)
        self.assertEqual(st.ats[700]['resources'], {'1': 5})

    async def test_field_reserve_blocks_without_contributing_and_resumes(self):
        st, war, parent = await self.setup_world({10: 1, 20: 1, 30: 1})
        col, _, _ = await wc.spawn_supply_column(st, war, parent, {1: 5},
                                                 near_capital_bf=10, now=f._NOW)
        st.battles[1] = {'id': 1, 'war_id': 1, 'battlefield_id': 101,
            'state': 0, 'battle_type': 5, 'started_at': f._NOW,
            'factions': [{'at_id': 600, 'faction': 2}]}
        mv = await st.get_active_movement(col['id'])
        arrival = mv['arrive_at'] + datetime.timedelta(seconds=1)
        await wc.advance_movements(st, war, now=arrival)
        self.assertEqual((st.ats[col['id']]['state'], st.ats[col['id']]['map_entity_id']), (2, 101))
        self.assertEqual(st.ats[500]['resources'], {'1': 2})
        self.assertEqual(len(st.battles[1]['factions']), 1)
        rows = await wc.build_warmap_snapshot(st, war, player_id=100)
        self.assertTrue(any(tag == b'missionresource' for tag, _ in rows))
        await wc._cleanup_supply_columns(st, war)
        self.assertEqual(st.ats[col['id']]['resources'], {'1': 5})
        st.battles[1]['state'] = 1
        st.city_owners[1].update({10: 2, 20: 2})
        await supply._check_field_supply(st, war, st.ats[col['id']], now=arrival)
        self.assertEqual(st.ats[col['id']]['state'], 1)
        resumed = await st.get_active_movement(col['id'])
        await wc.advance_movements(st, war,
            now=resumed['arrive_at'] + datetime.timedelta(seconds=1))
        self.assertEqual(st.ats[col['id']]['state'], 3)
        self.assertEqual(st.ats[500]['resources'], {'1': 7})

    async def test_field_encirclement_while_active(self):
        st, war, parent = await self.setup_world()
        col, _, _ = await wc.spawn_supply_column(st, war, parent, {1: 5},
                                                 near_capital_bf=10, now=f._NOW)
        st.battles[1] = {'id': 1, 'war_id': 1, 'battlefield_id': 101,
            'state': 0, 'battle_type': 5, 'started_at': f._NOW, 'factions': []}
        await wc.advance_movements(st, war, now=f._NOW + datetime.timedelta(seconds=1))
        self.assertEqual(st.ats[col['id']]['state'], 2)
        st.city_owners[1].update({10: 2, 20: 2})
        await wc._cleanup_supply_columns(st, war)
        self.assertEqual(st.ats[col['id']]['state'], 3)
        self.assertIsNone(await st.get_active_movement(col['id']))

    async def test_field_battle_formed_this_tick_catches_reserve(self):
        st, war, parent = await self.setup_world()
        col, _, _ = await wc.spawn_supply_column(st, war, parent, {1: 5},
                                                 near_capital_bf=10, now=f._NOW)
        async def form(*args):
            st.battles[1] = {'id': 1, 'war_id': 1, 'battlefield_id': 101,
                'state': 0, 'battle_type': 5, 'started_at': f._NOW, 'factions': []}
            return []
        with patch.object(wc.tick, '_check_field_encounters', form):
            await wc.advance_movements(st, war, now=f._NOW + datetime.timedelta(seconds=1))
        self.assertEqual(st.ats[col['id']]['state'], 2)
        self.assertEqual(supply.supply_state(st.ats[col['id']])['blocked_field_id'], 1)

    async def test_live_pool_only_changes_after_successful_queue_commit(self):
        import force_pool
        import state
        st, war, parent = await self.setup_world()
        st.ats[500].update(map_entity_id=10, state=2)
        st.battles[1] = {'id': 1, 'war_id': 1, 'battlefield_id': 10,
            'state': 0, 'battle_type': 1, 'factions': [{'at_id': 500, 'faction': 1}]}
        force_pool.init_mission_war(701, 1, 2, {1: [{1: 2}], 2: [{1: 12}]},
            {1: ['Main'], 2: ['Enemy']}, {1: [{'id': 500}], 2: [{'id': 600}]})
        try:
            await self.enqueue(st)
            st.fail_at = 'stock'
            with patch.object(state, 'STORE', st):
                await wc.supply_queue_tick(st)
                self.assertEqual(force_pool.counts_for(701, 1)[1], 2)
                self.assertEqual(st.ats[500]['resources'], {'1': 2})
                await wc.supply_queue_tick(st)
                self.assertEqual(force_pool.counts_for(701, 1)[1], 7)
                self.assertEqual(st.ats[500]['resources'], {'1': 7})
        finally:
            force_pool.release(701)

    async def test_late_tick_visits_multiple_intermediate_nodes(self):
        edge = {'id': 103, 'ap1': 3, 'ap2': 4}
        f._SYN_SLS.append(edge)
        wc.common._ADJACENCY_CACHE = None
        try:
            st, war, _ = await self.setup_world({10: 1, 20: 0, 30: 0, 40: 0})
            st.ats[500]['map_entity_id'] = 10
            mv, _ = await wc.start_movement(st, st.ats[500], 40, now=f._NOW)
            await wc.advance_movements(st, war,
                now=mv['arrive_at'] + datetime.timedelta(seconds=1))
            self.assertEqual([st.city_owners[1][bf] for bf in (20, 30, 40)], [1, 1, 1])
        finally:
            f._SYN_SLS.remove(edge)
            wc.common._ADJACENCY_CACHE = None


@unittest.skipUnless(os.environ.get('HNG_RTS_TEST_DSN'),
                     'Set HNG_RTS_TEST_DSN to run isolated PostgreSQL integration tests')
class PostgreSQLTransactions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg
        from store import PlayerStore
        from store.schema import _SCHEMA
        self.schema = 'rts_test_' + uuid.uuid4().hex
        self.admin = await asyncpg.connect(os.environ['HNG_RTS_TEST_DSN'])
        self.pool = None
        self.addAsyncCleanup(self.cleanup_schema)
        await self.admin.execute(f'CREATE SCHEMA {self.schema}')
        async def initialize(conn):
            await conn.execute(f'SET search_path TO {self.schema}')
        self.pool = await asyncpg.create_pool(os.environ['HNG_RTS_TEST_DSN'],
                                             min_size=1, max_size=4, setup=initialize)
        async with self.pool.acquire() as conn:
            await conn.execute(_SCHEMA)
        self.store = PlayerStore()
        self.store.pool = self.pool

    async def cleanup_schema(self):
        if self.pool:
            await self.pool.close()
        await self.admin.execute(f'DROP SCHEMA IF EXISTS {self.schema} CASCADE')
        await self.admin.close()

    async def test_creation_serializes_real_connections(self):
        results = await asyncio.gather(*(wc.ensure_war(self.store) for _ in range(4)))
        self.assertEqual(len({r['id'] for r in results}), 1)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM wars WHERE state=1'), 1)
        self.assertTrue(await self.store.get_war_cities(results[0]['id']))

    async def test_production_writes_rollback_on_real_connection(self):
        war = await self.store.create_war('production transaction test')
        with self.assertRaises(RuntimeError):
            async with self.store.rts_transaction('production', war['id']) as bound:
                await bound.adjust_war_faction_resources(war['id'], 1, {1: 10})
                await bound.update_war_production(war['id'], 600000)
                raise RuntimeError('interrupt after inventory and cursor writes')
        self.assertEqual(await self.store.list_war_faction_resources(war['id'], 1), [])
        self.assertEqual((await self.store.get_war(war['id']))['production_ms'], 0)

    async def test_queue_delivery_rollback_on_real_connection(self):
        war = await self.store.create_war('queue transaction test')
        await self.store.init_war_cities(war['id'], {10: 1, 20: 1, 30: 1})
        parent = await self.store.create_command_node(war['id'], 101, 100, 1,
                                                      'Main', resources={'1': 2})
        await self.store.update_command_node(parent['id'], is_deployed=True,
                                             map_entity_id=30, map_entity_type=1)
        await self.store.adjust_war_faction_resources(war['id'], 1, {1: 20})
        await self.store.enqueue_war_supply(war['id'], 1, parent['id'], {1: 5},
            metadata={'deliver_as_column': True, 'near_capital_battlefield_id': 10})
        with self.assertRaises(RuntimeError):
            async with self.store.rts_transaction('supply', war['id'], 1) as bound:
                await supply._deliver_supply_queue(bound, war, 1)
                raise RuntimeError('interrupt after delivery before commit')
        self.assertEqual(len(await self.store.list_war_command_nodes(war['id'])), 1)
        self.assertEqual(await self.store.list_active_movements(war['id']), [])
        self.assertEqual(len(await self.store.list_war_supply_queue(war['id'], 1)), 1)
        self.assertEqual((await self.store.list_war_faction_resources(war['id'], 1))[0]['count'], 20)


if __name__ == '__main__':
    unittest.main()
