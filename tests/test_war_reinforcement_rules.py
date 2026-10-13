"""Document reinforcement rules; DB-free, run directly or with unittest."""
import copy
import datetime
import json
import unittest
from unittest.mock import patch

import test_war_supply_column as f
import force_pool
import war_campaign as wc
from war_campaign import supply


class Store(f.FakeStore):
    async def get_mission_by_battle(self, battle_id):
        return {'id': 700} if self.battles.get(battle_id, {}).get('state') == 0 else None

    async def update_movement_path(self, movement_id, path, arrive_at=None, dest_ap=0):
        mv = self.movements[movement_id]
        mv.update(path=copy.deepcopy(path), arrive_at=arrive_at, dest_ap=dest_ap)
        return copy.deepcopy(mv)


class ReinforcementRules(unittest.IsolatedAsyncioTestCase):
    async def make(self, owners=None, template=101):
        st = Store()
        war = await f._war(st, owners or {10: 1, 20: 1, 30: 1, 50: 1})
        parent = f._mk_at(st, 500, template=template, deployed=True,
                          map_entity_id=30, resources={'1': 2})
        return st, war, parent

    async def spawn(self, st, war, parent, resources=None, spawn=10):
        col, _, rc = await wc.spawn_supply_column(st, war, parent,
            resources or {1: 5}, near_capital_bf=spawn, now=f._NOW)
        self.assertEqual(rc, 0)
        return col

    async def test_near_capital_and_fallback(self):
        st, war, parent = await self.make({10: 1, 20: 1, 30: 2})
        spawn, dest, ok = await wc.column_route(st, war, parent)
        self.assertEqual((spawn, dest, ok), (20, 30, True))
        st.city_owners[war['id']][20] = 2
        self.assertEqual((await wc.column_route(st, war, parent))[0], 10)

    async def test_manual_ground_destination_and_stay(self):
        st, war, parent = await self.make()
        col = await self.spawn(st, war, parent)
        # End the automatic order and issue an independent order to B.
        await st.finish_movement((await st.get_active_movement(col['id']))['id'], 2)
        await st.update_command_node(col['id'], state=0, map_entity_id=10,
                                     map_entity_type=1)
        col = await st.get_command_node(col['id'])
        dest, rc, _ = await f.war_at._transport_supply_order(st, war, col, 20, 'battlefield')
        self.assertEqual((dest, rc), (20, wc.RC_OK))
        mv = await st.get_active_movement(col['id'])
        await wc.advance_movements(st, war, now=mv['arrive_at'] + datetime.timedelta(seconds=1))
        await wc._cleanup_supply_columns(st, war)
        self.assertEqual(st.ats[col['id']]['map_entity_id'], 20)
        self.assertIsNone(await st.get_active_movement(col['id']))
        st.city_owners[war['id']][30] = 2
        dest, rc, _ = await f.war_at._transport_supply_order(st, war,
            await st.get_command_node(col['id']), parent['id'], 'commandnode')
        self.assertNotEqual(rc, wc.RC_OK)
        st.city_owners[war['id']][30] = 0
        _, rc, _ = await f.war_at._transport_supply_order(st, war,
            await st.get_command_node(col['id']), 30, 'battlefield')
        self.assertNotEqual(rc, wc.RC_OK)

    async def test_manual_ground_diversion(self):
        st, war, parent = await self.make()
        col = await self.spawn(st, war, parent)
        # Use a future started_at so the order redirects at the first node.
        mv = await st.get_active_movement(col['id'])
        st.movements[mv['id']]['started_at'] = datetime.datetime.now(datetime.timezone.utc)
        _, rc, _ = await f.war_at._transport_supply_order(st, war, col, 10, 'battlefield')
        self.assertEqual(rc, wc.RC_OK)
        redirected = await st.get_active_movement(col['id'])
        self.assertEqual([s['to_bf'] for s in redirected['path']], [20, 10])
        self.assertTrue(supply.supply_state(st.ats[col['id']])['manual'])

    async def test_battle_block_no_resources_release_and_reconnect(self):
        st, war, parent = await self.make({10: 1, 20: 2, 30: 1})
        col = await self.spawn(st, war, parent)
        enemy = f._mk_at(st, 600, faction=2, template=201, deployed=True,
                         map_entity_id=20, state=2, resources={'1': 12})
        st.battles[1] = {'id': 1, 'war_id': war['id'], 'battlefield_id': 20,
            'state': 0, 'factions': [{'at_id': 600, 'faction': 2}], 'battle_type': 1}
        mv = await st.get_active_movement(col['id'])
        # Skip the entire path in one tick: B must intercept before C.
        events = await wc.advance_movements(st, war, now=mv['arrive_at'] + datetime.timedelta(seconds=1))
        blocked = await st.get_command_node(col['id'])
        # Match PostgreSQL rows received through asyncpg without a JSON codec.
        st.ats[col['id']]['supply_state'] = json.dumps(blocked['supply_state'])
        self.assertEqual((blocked['state'], blocked['map_entity_id']), (2, 20))
        self.assertEqual(st.ats[500]['resources'], {'1': 2})
        self.assertEqual(st.ats[600]['resources'], {'1': 12})
        self.assertEqual(len(st.battles[1]['factions']), 1)
        self.assertTrue(any(tag == b'missionresource' and kind == 'set'
                            for kind, tag, _ in events))
        await wc._cleanup_supply_columns(st, war)
        self.assertEqual(st.ats[col['id']]['resources'], {'1': 5})
        rows = await wc.build_warmap_snapshot(st, player_id=100)
        self.assertTrue(any(tag == b'missionresource' for tag, _ in rows))
        st.battles[1]['state'] = 1
        # Enemy remains here after winning; the ended original battle still releases us.
        await wc._cleanup_supply_columns(st, war)
        self.assertEqual(st.ats[col['id']]['state'], 1)
        new_mv = await st.get_active_movement(col['id'])
        self.assertEqual(new_mv['path'][0]['from_bf'], 20)
        self.assertEqual(new_mv['path'][-1]['to_bf'], 30)
        await wc.advance_movements(st, war, now=new_mv['arrive_at'] + datetime.timedelta(seconds=1))
        self.assertEqual(st.ats[col['id']]['state'], 3)
        self.assertEqual(st.ats[500]['resources'], {'1': 7})
        st.ats[500]['resources'] = {'1': 2}
        duplicate = await supply._merge_column_events(st, war['id'], col,
                                                     parent, wc._faction_capital_bf())
        self.assertEqual(duplicate, [])
        self.assertEqual(st.ats[500]['resources'], {'1': 2})

    async def test_encirclement_immediate_and_periodic(self):
        st, war, parent = await self.make({10: 1, 20: 2, 30: 2})
        col = await self.spawn(st, war, parent)
        f._mk_at(st, 600, faction=2, template=201, deployed=True, map_entity_id=20)
        st.battles[1] = {'id': 1, 'war_id': war['id'], 'battlefield_id': 20,
                         'state': 0, 'battle_type': 1, 'factions': []}
        mv = await st.get_active_movement(col['id'])
        await wc.advance_movements(st, war, now=mv['arrive_at'] + datetime.timedelta(seconds=1))
        self.assertEqual(st.ats[col['id']]['state'], 2)
        st.city_owners[war['id']][10] = 2
        await wc._cleanup_supply_columns(st, war)
        self.assertEqual(st.ats[col['id']]['state'], 3)
        st2, war2, parent2 = await self.make({10: 2, 20: 2, 30: 2})
        # Spawn directly as the reserve is already approaching a surrounded battle.
        reserve = f._mk_at(st2, 700, template=104, deployed=True, map_entity_id=10)
        st2.ats[700].update(parent_command_node_id=500, resources={'1': 5},
            supply_state={'source': 'purchase'})
        f._mk_at(st2, 600, faction=2, template=201, deployed=True, map_entity_id=20)
        st2.battles[1] = {'id': 1, 'war_id': war2['id'], 'battlefield_id': 20,
                          'state': 0, 'battle_type': 1, 'factions': []}
        mv2, _ = await supply.start_supply_movement(st2, war2, st2.ats[700], 30, now=f._NOW)
        await wc.advance_movements(st2, war2, now=mv2['arrive_at'] + datetime.timedelta(seconds=1))
        self.assertEqual(st2.ats[700]['state'], 3)

    async def test_plane_orders_and_airfield_loss(self):
        st, war, parent = await self.make(template=108)
        col = await self.spawn(st, war, parent, {20: 4})
        self.assertEqual((col['state'], col['map_entity_id']), (0, 50))
        _, rc, _ = await f.war_at._transport_supply_order(st, war, col, 20, 'battlefield')
        self.assertNotEqual(rc, wc.RC_OK)  # Not an airport.
        # Same airfield is a valid manual order, even in a defense battle.
        st.battles[1] = {'id': 1, 'war_id': war['id'], 'battlefield_id': 50,
                         'state': 0, 'battle_type': 1, 'factions': []}
        _, rc, _ = await f.war_at._transport_supply_order(st, war, col, 50, 'battlefield')
        self.assertEqual(rc, wc.RC_OK)
        st.city_owners[war['id']][50] = 2
        await wc._cleanup_supply_columns(st, war)
        self.assertEqual(st.ats[col['id']]['state'], 3)

    async def test_parent_deletion_cancels_movement(self):
        st, war, parent = await self.make()
        col = await self.spawn(st, war, parent)
        st.ats[500]['state'] = 3
        events = await wc._cleanup_supply_columns(st, war)
        self.assertEqual(st.ats[col['id']]['state'], 3)
        self.assertIsNone(await st.get_active_movement(col['id']))
        self.assertTrue(any(tag == b'transport_commandnode_segment' for _, tag, _ in events))

    async def test_plane_flight_defense_block_and_range(self):
        additions = [
            {'id': 60, 'posx': 600, 'posy': 400, 'gamemap': 204},
            {'id': 70, 'posx': 10000, 'posy': 10000, 'gamemap': 204}]
        f._SYN_BFS.extend(additions)
        wc.common._BF_POS_CACHE = None
        wc.at_ops._AIRFIELD_CACHE = None
        try:
            st, war, parent = await self.make(template=108)
            st.city_owners[war['id']].update({60: 1, 70: 1})
            col = await self.spawn(st, war, parent, {20: 4})
            _, rc, _ = await f.war_at._transport_supply_order(st, war, col, 70, 'battlefield')
            self.assertNotEqual(rc, wc.RC_OK)
            st.city_owners[war['id']][60] = 2
            _, rc, _ = await f.war_at._transport_supply_order(st, war, col, 60, 'battlefield')
            self.assertNotEqual(rc, wc.RC_OK)
            st.city_owners[war['id']][60] = 1
            st.battles[1] = {'id': 1, 'war_id': war['id'], 'battlefield_id': 60,
                            'state': 0, 'battle_type': 1, 'factions': []}
            _, rc, _ = await f.war_at._transport_supply_order(st, war, col, 60, 'battlefield')
            self.assertEqual(rc, wc.RC_OK)
            mv = await st.get_active_movement(col['id'])
            self.assertTrue(mv['path'][0]['air'])
            await wc.advance_movements(st, war, now=mv['arrive_at'] + datetime.timedelta(seconds=1))
            self.assertEqual((st.ats[col['id']]['state'], st.ats[col['id']]['airbase_bf']), (2, 60))
            await wc._cleanup_supply_columns(st, war)
            self.assertEqual(st.ats[col['id']]['resources'], {'20': 4})
            st.battles[1]['state'] = 1
            await wc._cleanup_supply_columns(st, war)
            self.assertEqual(st.ats[col['id']]['state'], 0)
            self.assertEqual(st.ats[col['id']]['map_entity_id'], 60)
        finally:
            for item in additions:
                f._SYN_BFS.remove(item)
            wc.common._BF_POS_CACHE = None
            wc.at_ops._AIRFIELD_CACHE = None

    async def test_plane_only_purchase_can_wait_without_ground_route(self):
        st, war, parent = await self.make(template=108)
        airport, _, ok = await wc.column_route(st, war, parent, resources={20: 4})
        self.assertEqual((airport, ok), (50, True))
        _, _, ok = await wc.column_route(st, war, parent, resources={2: 4, 20: 4})
        self.assertFalse(ok)

    async def test_guarded_city_waits_without_starting_battle_and_empty_city_passes(self):
        st, war, parent = await self.make({10: 1, 20: 2, 30: 2})
        col = await self.spawn(st, war, parent)
        f._mk_at(st, 600, faction=2, template=201, deployed=True, map_entity_id=20)
        st.city_owners[war['id']][10] = 2
        mv = await st.get_active_movement(col['id'])
        await wc.advance_movements(st, war, now=mv['arrive_at'] + datetime.timedelta(seconds=1))
        self.assertEqual((st.ats[col['id']]['state'], st.ats[col['id']]['map_entity_id']), (2, 20))
        self.assertEqual(st.battles, {})
        self.assertEqual(st.ats[600]['state'], 0)
        await wc._cleanup_supply_columns(st, war)
        self.assertEqual(st.ats[col['id']]['state'], 2)  # Not an encircled battle.
        st.ats[600]['state'] = 3
        await wc._cleanup_supply_columns(st, war)
        self.assertEqual(st.ats[col['id']]['state'], 1)
        st2, war2, parent2 = await self.make({10: 1, 20: 2, 30: 2})
        col2 = await self.spawn(st2, war2, parent2)
        mv2 = await st2.get_active_movement(col2['id'])
        await wc.advance_movements(st2, war2, now=mv2['arrive_at'] + datetime.timedelta(seconds=1))
        self.assertEqual(st2.ats[col2['id']]['state'], 3)
        self.assertEqual(st2.city_owners[war2['id']][20], 2)
        self.assertEqual(st2.battles, {})

    async def test_delivery_into_parent_battle_updates_live_pool_once(self):
        import state
        st, war, parent = await self.make()
        col = await self.spawn(st, war, parent)
        st.ats[parent['id']]['state'] = 2
        st.battles[1] = {'id': 1, 'war_id': war['id'], 'battlefield_id': 30,
                        'state': 0, 'battle_type': 1,
                        'factions': [{'at_id': parent['id'], 'faction': 1}]}
        force_pool.init_mission_war(700, 1, 2, {1: [{1: 2}], 2: [{1: 12}]},
            {1: ['Main'], 2: ['Enemy']}, {1: [{'id': 500}], 2: [{'id': 600}]})
        try:
            with patch.object(state, 'STORE', st):
                mv = await st.get_active_movement(col['id'])
                await wc.advance_movements(st, war,
                    now=mv['arrive_at'] + datetime.timedelta(seconds=1))
                self.assertEqual(st.ats[500]['resources'], {'1': 7})
                self.assertEqual(force_pool.counts_for(700, 1)[1], 7)
                await wc._cleanup_supply_columns(st, war)
                self.assertEqual(force_pool.counts_for(700, 1)[1], 7)
        finally:
            force_pool.release(700)

    def test_ground_route_chooses_travel_time_over_hop_count(self):
        # Direct 10→30 is a slow sea crossing; the two short land legs are faster.
        adjacency = {10: [(104, 1, 3, 30), (101, 1, 2, 20)],
                     20: [(102, 2, 3, 30)], 30: []}
        import mm_config
        with patch.object(supply, 'supplyline_adjacency', return_value=adjacency), \
                patch.object(mm_config, 'WAR_SEA_DISTANCE', 550), \
                patch.object(mm_config, 'WAR_SEA_SPEED_FACTOR', 0.25):
            path = supply.fastest_supply_path(10, 30,
                {'template_id': 104, 'faction': 1}, {10: 1, 20: 1, 30: 1})
        self.assertEqual([s['sl_id'] for s in path], [101, 102])

    def test_live_pool_delivery_does_not_duplicate_parent(self):
        force_pool.init_mission_war(990, 1, 2, {1: [{1: 2}], 2: [{1: 12}]},
            {1: ['Main'], 2: ['Enemy']}, {1: [{'id': 500}], 2: [{'id': 600}]})
        try:
            self.assertTrue(force_pool.add_war_supply(990, 1, 500, {1: 5}))
            self.assertEqual(force_pool.counts_for(990, 1)[1], 7)
            self.assertFalse(force_pool.add_war_supply(990, 1, 999, {1: 10}))
            self.assertEqual(force_pool.counts_for(990, 1)[1], 7)
        finally:
            force_pool.release(990)


if __name__ == '__main__':
    unittest.main()
