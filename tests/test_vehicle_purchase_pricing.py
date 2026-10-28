"""Replay tank/plane purchase failures with real WarDatabase prices and charge code."""
import asyncio

import pytest

import handlers.vehicles_purchase as purchases
import player_service
from protocol import Chunk, pb_int64, pb_parse


class Writer:
    def __init__(self):
        self.frames = []

    def write(self, data):
        self.frames.append(data)

    async def drain(self):
        pass


class Session:
    soldier_view_cmd = None
    playerview_cmd = None

    async def ensure_player(self):
        return 1


class Store:
    def __init__(self):
        self.spent = []
        self.created = []
        self.skins = []

    async def spend_currency(self, pid, key, cost):
        self.spent.append((pid, key, cost))
        return True, 1000000 - cost

    async def create_vehicle(self, pid, template, cid):
        self.created.append((pid, template, cid))
        return {'id': 99, 'condition': 100.0}

    async def list_vehicles(self, pid):
        return [{'id': 99, 'character_id': 347}]

    async def get_vehicle(self, pid, vid):
        return {'id': vid, 'character_id': 347,
                'vehicle_variant_template_id': 225, 'condition': 0.0}

    async def create_vehicle_variant(self, pid, vid, cid, template):
        self.skins.append((pid, vid, cid, template))
        return {'id': 100}


@pytest.fixture
def purchase_store(monkeypatch):
    store = Store()
    monkeypatch.setattr(purchases, 'STORE', store)
    monkeypatch.setattr(player_service, 'STORE', store)

    async def owned(*args):
        return True

    async def unlocked(*args):
        return set()

    async def grant_ammo(*args):
        return []

    monkeypatch.setattr(purchases, '_character_owned', owned)
    monkeypatch.setattr(purchases, '_unlock_need_for_char', unlocked)
    monkeypatch.setattr(purchases, 'grant_default_vehicle_ammo', grant_ammo)
    return store


def purchase(request):
    writer = Writer()
    asyncio.run(purchases.handle_createvehiclevariantrequest(
        Session(), writer, 33, 'CreateVehicleVariantRequest', request))
    frame, _ = Chunk.parse(writer.frames[0])
    assert frame.subs[0].as_str() == 'CreateVehicleVariantResponse'
    return pb_parse(frame.subs[0].subs[0].data)[1]


@pytest.mark.parametrize('template,pricing,currency,key,cost', [
    (46, 5374, 1, 'credits', 57000),
    (225, 8814, 1, 'credits', 96000),
    (46, 5374, 3, 'gold', 680),
    (225, 8814, 3, 'gold', 1150),
])
def test_factory_vehicle_uses_parent_price(purchase_store, template, pricing, currency, key, cost):
    request = (pb_int64(1, 347) + pb_int64(2, template)
               + pb_int64(3, pricing) + pb_int64(8, currency))
    assert purchase(request) == 1
    assert purchase_store.spent == [(1, key, cost)]
    assert purchase_store.created == [(1, template, 347)]


@pytest.mark.parametrize('template,pricing', [
    (225, 5374),  # another vehicle's price
    (225, 8828),  # the plane's skin price cannot buy its airframe
    (46, 11450),  # same numeric id but wrong pricing type
    (999999, 5374),  # unknown variant
    (225, 0),
])
def test_wrong_target_or_skin_price_cannot_buy_vehicle(purchase_store, template, pricing):
    request = (pb_int64(1, 347) + pb_int64(2, template)
               + pb_int64(3, pricing) + pb_int64(8, 1))
    assert purchase(request) != 1
    assert not purchase_store.spent and not purchase_store.created


def test_skin_purchase_still_uses_variant_price(purchase_store):
    request = (pb_int64(1, 347) + pb_int64(4, 99) + pb_int64(5, 225)
               + pb_int64(6, 8828) + pb_int64(8, 1))
    assert purchase(request) == 1
    assert purchase_store.spent == [(1, 'credits', 15000)]
    assert purchase_store.skins == [(1, 99, 347, 225)]
    assert not purchase_store.created
