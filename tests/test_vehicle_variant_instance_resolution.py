"""Client getVariantId selects the first account-wide copy, not the vehicle's copy."""
import pytest

from test_battlepass_skin_unlock import (
    _FakeSess, _FakeStore, _equip, _kvcs_rows,
)
from handlers import career, vehicles


def vehicle(vid, char=711, model=440, skin=0):
    return dict(id=vid, character_id=char, vehicle_variant_template_id=model,
                equipped_in_slot=1, variant_id=skin)


def variant(vid, vehicle_id, char=711, template=441):
    return dict(id=vid, vehicle_id=vehicle_id, character_id=char,
                vehicle_variant_template_id=template)


@pytest.fixture
def equip_store(monkeypatch):
    async def no_career(*args):
        pass
    monkeypatch.setattr(career, 'complete_order_and_push', no_career)

    def make(cars, skins):
        store = _FakeStore(vehicles=cars, variants=skins)
        monkeypatch.setattr(vehicles, 'STORE', store)
        return store
    return make


@pytest.mark.parametrize('target,local_id,char,model,template', [
    (1748, 1272, 711, 440, 441),  # R75 Ambush DB: real failed request
    (1754, 1276, 714, 440, 441),  # same account, third R75
    (1757, 1279, 716, 2, 412),    # also observed on another vehicle model
])
def test_client_first_copy_resolves_to_vehicle_owned_copy(
        equip_store, target, local_id, char, model, template):
    first = variant(1266, 1699, 689, template)
    local = variant(local_id, target, char, template)
    store = equip_store([vehicle(1699, 689, model, 1266),
                         vehicle(target, char, model)], [first, local])
    response, writer = _equip(store, _FakeSess(), target, 1266)
    assert response == 1
    assert store.vehicles[target]['variant_id'] == local_id
    assert store.vehicles[1699]['variant_id'] == 1266
    assert store.variants[1266] == first
    assert store.variants[local_id] == local
    assert not any(c[0] == 'bind_vehicle_variant' for c in store.calls)
    assert any(r.get(1) == target and r.get(9) == local_id
               for r in _kvcs_rows(writer, b'Vehicle'))


@pytest.mark.parametrize('skins,cars', [
    # An account copy on another vehicle is not a purchase for this vehicle.
    ([variant(1266, 1699, 689)], [vehicle(1748)]),
    # Same base vehicle but a different paint is insufficient.
    ([variant(1266, 1699, 689), variant(1272, 1748, template=442)], [vehicle(1748)]),
    # A copy on a third vehicle is also insufficient.
    ([variant(1266, 1699, 689), variant(1272, 1754)], [vehicle(1748)]),
    # A requested instance absent from this player's inventory is rejected.
    ([variant(1272, 1748)], [vehicle(1748)]),
    # A target vehicle absent from this player's inventory is rejected.
    ([variant(1266, 1699, 689), variant(1272, 1748)], []),
    # Even two corrupt rows cannot equip paint from a different vehicle family.
    ([variant(1266, 1699, 689, 412), variant(1272, 1748, template=412)], [vehicle(1748)]),
])
def test_invalid_copy_is_rejected_without_mutations(equip_store, skins, cars):
    store = equip_store(cars, skins)
    response, writer = _equip(store, _FakeSess(), 1748, 1266)
    assert response == 0
    assert not store.calls
    assert not _kvcs_rows(writer, b'Vehicle')


def test_direct_copy_and_unload_still_work(equip_store):
    store = equip_store([vehicle(1748)], [variant(1272, 1748)])
    assert _equip(store, _FakeSess(), 1748, 1272)[0] == 1
    assert store.vehicles[1748]['variant_id'] == 1272
    assert _equip(store, _FakeSess(), 1748, 0)[0] == 1
    assert store.vehicles[1748]['variant_id'] == 0
