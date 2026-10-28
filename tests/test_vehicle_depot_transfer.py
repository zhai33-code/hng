"""Offline regressions for paid and free vehicle transfers; no live DB needed."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import wardb
import player_service
from handlers import characters, career
from protocol import Chunk, pb_int32, pb_int64, pb_parse, pb_string


class Writer:
    def __init__(self):
        self.frames = []

    def write(self, frame):
        self.frames.append(frame)

    async def drain(self):
        pass


class Store:
    def __init__(self):
        self.vehicle = dict(id=1653, vehicle_variant_template_id=128,
                            character_id=643, equipped_in_slot=0, variant_id=0)
        self.character = dict(id=643, faction=1, template=3)
        self.balance = 1000000
        self.spends = []
        self.refunds = []
        self.moves = []
        self.marked = []
        self.persist = True

    async def get_item_character(self, pid, kind, object_id):
        return self.vehicle['character_id'] if pid == 1 and object_id == 1653 else None

    async def get_item_template(self, *args):
        return self.vehicle['vehicle_variant_template_id']

    async def get_vehicle(self, pid, object_id):
        return deepcopy(self.vehicle) if pid == 1 and object_id == 1653 else None

    async def list_characters(self, pid):
        return [deepcopy(self.character)]

    async def list_vehicle_variants(self, pid):
        return []

    async def list_vehicle_upgrades(self, pid):
        return []

    async def list_vehicle_ammo(self, pid):
        return []

    async def spend_currency(self, pid, key, cost, **kwargs):
        self.spends.append((key, cost))
        if self.balance < cost:
            return False, self.balance
        self.balance -= cost
        return True, self.balance

    async def add_currency(self, pid, **kwargs):
        self.refunds.append(kwargs)
        self.balance += sum(kwargs.values())

    async def send_item_to_depot(self, pid, kind, object_id):
        self.moves.append((pid, kind, object_id))
        if self.persist:
            self.vehicle.update(character_id=0, equipped_in_slot=0, variant_id=0)
        return self.persist

    async def mark_depot_item_new(self, pid, tab, object_id):
        self.marked.append((tab, object_id))


@pytest.fixture
def env(monkeypatch):
    store = Store()
    prices = {
        4520: dict(type='vehicletemplate', object_id=28, credits=291000, gold=3380, warfunds=100),
        4521: dict(type='vehicletemplate', object_id=7, credits=10, gold=10, warfunds=10),
        4522: dict(type='VehicleVariantTemplate', object_id=128, credits=10, gold=10, warfunds=10),
    }
    monkeypatch.setattr(wardb, '_VEHICLE_VARIANT_PARENT', {128: 28, 28: 28, 129: 29})
    monkeypatch.setattr(characters, '_VEHICLE_VARIANT_PARENT', wardb._VEHICLE_VARIANT_PARENT)
    monkeypatch.setattr(wardb, '_VEHICLE_DEPOT_UNLOCKABLE', {128: [(1, 3)], 129: [(1, 3)]})
    for module in (characters, player_service, wardb):
        monkeypatch.setattr(module, '_PRICING_MAP', prices)
    for module in (characters, player_service):
        monkeypatch.setattr(module, 'STORE', store)
    # Keep real handler/charge/Vehicle push; isolate unrelated progression work.
    for name in ('_repush_character_by_id', '_repush_depot_new_item', '_push_player_refresh'):
        monkeypatch.setattr(characters, name, AsyncMock())
    monkeypatch.setattr(career, 'complete_order_and_push', AsyncMock())
    return store


def transfer(store, pricing=4520, free=False, currency=1, object_id=1653, kind=2):
    async def player():
        return 1

    writer = Writer()
    sess = SimpleNamespace(ensure_player=player, soldier_view_cmd=13, playerview_cmd=None)
    proto = (pb_int32(1, kind) + pb_int64(2, object_id) + pb_int64(3, pricing)
             + pb_int32(5, currency) + pb_string(6, 'v17') + pb_int32(7, int(free)))
    asyncio.run(characters.handle_senditemtodepotrequest(
        sess, writer, 55, 'SendItemToDepotRequest', proto))
    chunk, _ = Chunk.parse(writer.frames[0])
    assert chunk.subs[0].as_str() == 'SendItemToDepotResponse'
    return pb_parse(chunk.subs[0].subs[0].data)[1], writer


def pushed_rows(writer):
    rows = {}
    for frame in writer.frames[1:]:
        root, _ = Chunk.parse(frame)
        for action in root.subs[0].subs:
            for table in action.subs:
                rows.setdefault(table.as_str(), []).append(pb_parse(table.subs[0].data))
    return rows


@pytest.mark.parametrize('currency,key,cost', [(1, 'credits', 145500), (3, 'gold', 1690)])
def test_paid_parent_price_and_real_vehicle_push(env, currency, key, cost):
    env.vehicle['equipped_in_slot'] = 3
    response, writer = transfer(env, currency=currency)
    assert response == 1
    assert env.spends == [(key, cost)]
    assert env.balance == 1000000 - cost
    assert env.vehicle['character_id'] == env.vehicle['equipped_in_slot'] == 0
    assert env.marked == [('v17', 1653)]
    # Real KVCS serializer emits the updated Vehicle, rather than only an ACK.
    row = pushed_rows(writer)['Vehicle'][0]
    assert (row[1], row[3], row[5], row[6], row[9]) == (1653, 128, 0, 0, 0)
    characters._repush_character_by_id.assert_awaited_once()
    characters._repush_depot_new_item.assert_awaited_once()


def test_same_id_base_variant_also_succeeds(env):
    env.vehicle['vehicle_variant_template_id'] = 28
    assert transfer(env)[0] == 1


def test_unknown_variant_cannot_use_numerically_matching_vehicle_price(env):
    env.vehicle['vehicle_variant_template_id'] = 7
    assert transfer(env, pricing=4521)[0] == 0
    assert not env.spends and not env.moves


def test_detached_skin_retains_its_own_variant_pricing(env):
    assert transfer(env, kind=10, pricing=4522)[0] == 1
    assert env.spends == [('credits', 5)]


def test_detached_skin_rejects_whole_vehicle_pricing(env):
    assert transfer(env, kind=10, pricing=4520)[0] == 252
    assert not env.spends and not env.moves


@pytest.mark.parametrize('pricing', [0, 999999, 4521, 4522])
def test_missing_wrong_vehicle_or_skin_price_cannot_charge_or_move(env, pricing):
    assert transfer(env, pricing=pricing)[0] == 252
    assert not env.spends and not env.moves and not env.marked


@pytest.mark.parametrize('currency,response', [(1, 56), (2, 153), (3, 57)])
def test_insufficient_balance_preserves_vehicle(env, currency, response):
    env.balance = 0
    assert transfer(env, currency=currency)[0] == response
    assert not env.moves and env.vehicle['character_id'] == 643


def test_repeat_is_idempotent_and_repushes_without_charging(env):
    assert transfer(env)[0] == 1
    response, writer = transfer(env, pricing=0)
    assert response == 1 and len(env.spends) == len(env.moves) == 1
    assert pushed_rows(writer)['Vehicle'][0][5] == 0


def test_attached_ammo_upgrades_and_skin_get_detached_pushes(env, monkeypatch):
    attached = {
        'list_vehicle_upgrades': dict(id=91, vehicle_id=1653, character_id=643,
                                     upgrade_template_id=3, equipped_slot_id=7),
        'list_vehicle_ammo': dict(id=92, vehicle_id=1653, character_id=643,
                                 ammo_template_id=10, slot_index=1, equipped_storage_id=5),
        'list_vehicle_variants': dict(id=93, vehicle_id=1653, character_id=643,
                                     vehicle_variant_template_id=300),
    }
    for method, row in attached.items():
        async def list_attached(pid, row=row):
            return [deepcopy(row)] if env.vehicle['character_id'] else []
        monkeypatch.setattr(env, method, list_attached)
    response, writer = transfer(env)
    assert response == 1
    rows = pushed_rows(writer)
    assert (rows['VehicleUpgrade'][0][3], rows['VehicleUpgrade'][0][6]) == (0, 0)
    assert (rows['VehicleAmmo'][0][3], rows['VehicleAmmo'][0][4],
            rows['VehicleAmmo'][0][7]) == (0, 0, 0)
    assert (rows['VehicleVariant'][0][3], rows['VehicleVariant'][0][4]) == (0, 0)


def test_persistence_failure_refunds_and_does_not_mark_or_push_item(env):
    env.persist = False
    response, writer = transfer(env)
    assert response == 0 and env.balance == 1000000
    assert env.refunds == [{'credits': 145500}]
    assert not env.marked and len(writer.frames) == 1


@pytest.mark.parametrize('faction,template', [(2, 3), (1, 1)])
def test_incompatible_vehicle_free_without_pricing(env, faction, template):
    env.character.update(faction=faction, template=template)
    assert transfer(env, pricing=0, free=True)[0] == 1
    assert not env.spends and len(env.moves) == 1


def test_unpriced_unequipped_vehicle_can_return_for_free(env):
    env.vehicle['vehicle_variant_template_id'] = 129
    assert transfer(env, pricing=0, free=True)[0] == 1
    assert not env.spends and len(env.moves) == 1


def test_unpriced_equipped_compatible_vehicle_is_not_free(env):
    env.vehicle.update(vehicle_variant_template_id=129, equipped_in_slot=1)
    assert transfer(env, pricing=0, free=True)[0] == 239
    assert not env.spends and not env.moves


@pytest.mark.parametrize('pricing', [0, 4520, 4521])
def test_forged_free_flag_cannot_bypass_transfer_fee(env, pricing):
    assert transfer(env, pricing=pricing, free=True)[0] == 239
    assert not env.spends and not env.moves


def test_missing_character_fails_closed(env):
    env.character['id'] = 999
    assert transfer(env, pricing=0, free=True)[0] == 239
    assert not env.spends and not env.moves


def test_unowned_item_never_charged_or_moved(env):
    assert transfer(env, object_id=999)[0] == 0
    assert not env.spends and not env.moves


def test_free_eligibility_database_failure_fails_closed(env, monkeypatch):
    monkeypatch.setattr(env, 'get_vehicle', AsyncMock(side_effect=RuntimeError('offline')))
    assert transfer(env, pricing=0, free=True)[0] == 239
    assert not env.spends and not env.moves


def test_unknown_template_and_missing_static_data_cannot_grant_free(env, monkeypatch):
    assert not wardb.vehicle_depot_is_free(999999, 1, 1, 0)
    monkeypatch.setattr(wardb, '_VEHICLE_DEPOT_UNLOCKABLE', {})
    assert not wardb.vehicle_depot_is_free(128, 1, 1, 0)


def test_real_pershing_price_and_variant_parent():
    # Independent of the synthetic fixture; the historical incident price.
    price = wardb._PRICING_MAP[4520]
    assert price['type'] == 'vehicletemplate' and price['object_id'] == 28
    assert any(parent == 28 for parent in wardb._VEHICLE_VARIANT_PARENT.values())


@pytest.mark.parametrize('template,pricing,cost', [(46, 5374, 28500), (225, 8814, 48000)])
def test_real_tank_and_plane_prices_through_transfer_handler(env, monkeypatch, template, pricing, cost):
    # Reload unmodified static rows, then replay with real pricing/charge code.
    prices = wardb._build_pricing_map()
    parents = {int(f[1]): int(f[3]) for f in wardb._parse_wardb_table('VehicleVariantTemplate')}
    monkeypatch.setattr(characters, '_VEHICLE_VARIANT_PARENT', parents)
    monkeypatch.setattr(characters, '_PRICING_MAP', prices)
    monkeypatch.setattr(player_service, '_PRICING_MAP', prices)
    env.vehicle['vehicle_variant_template_id'] = template
    assert transfer(env, pricing=pricing)[0] == 1
    assert env.spends == [('credits', cost)]


def test_real_pershing_incident_now_succeeds(env, monkeypatch):
    parents = {int(f[1]): int(f[3]) for f in wardb._parse_wardb_table('VehicleVariantTemplate')}
    prices = wardb._build_pricing_map()
    variant = next(variant for variant, parent in parents.items() if parent == 28)
    monkeypatch.setattr(characters, '_VEHICLE_VARIANT_PARENT', parents)
    monkeypatch.setattr(characters, '_PRICING_MAP', prices)
    monkeypatch.setattr(player_service, '_PRICING_MAP', prices)
    env.vehicle['vehicle_variant_template_id'] = variant
    assert transfer(env, pricing=4520)[0] == 1
    assert env.spends == [('credits', 145500)]


def test_xp_locked_but_compatible_vehicle_is_still_chargeable(env):
    # Compatibility, rather than current ribbon XP/ownership unlock bits,
    # determines the overlay's waiver. Low XP must not make transfers free.
    assert not wardb.vehicle_depot_is_free(128, 1, 3, 0)
    assert transfer(env, pricing=0, free=True)[0] == 239


def test_wildcard_faction_and_class_remain_compatible(env, monkeypatch):
    monkeypatch.setattr(wardb, '_VEHICLE_DEPOT_UNLOCKABLE', {128: [(0, 0)]})
    assert not wardb.vehicle_depot_is_free(128, 99, 99, 0)
