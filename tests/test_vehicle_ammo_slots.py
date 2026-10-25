"""Regression: unlock permissions, owned counts and equipped slots are independent.

Exercise real grants, purchases, soldier replay and wire messages without a DB.
The SWF keeps offering purchases until the per-type owned count reaches capacity;
that UI threshold must never multiply a purchase or a default grant.
"""
import asyncio
import struct
from copy import deepcopy

import pytest

import handlers.characters as characters
import handlers.characters_purchase as recruits
import handlers.shop as shop
import handlers.vehicles_purchase as purchases
import handlers.weapons_purchase as weapons
import player_service as service
import wardb
import warserver_messages
from protocol import Chunk, pb_int64, pb_parse, pb_parse_multi


class Session:
    soldier_view_cmd = 12
    playerview_cmd = None

    async def ensure_player(self):
        return 1


class Writer:
    def __init__(self):
        self.frames = []

    def write(self, data):
        self.frames.append(data)

    async def drain(self):
        pass


class Store:
    def __init__(self):
        self.chars = [{'id': 10, 'name': 'Tanker', 'template': 5, 'faction': 1}]
        self.vehicles = [dict(id=20, character_id=10, vehicle_variant_template_id=91001,
                              equipped_in_slot=1, condition=100.0, variant_id=0)]
        self.ammo = []
        self.equips = []
        self.spent = []
        self.refunds = []
        self.xp = {}
        self.fail_insert = False
        self.can_pay = True

    async def list_characters(self, pid):
        return deepcopy(self.chars)

    async def list_vehicles(self, pid, cid=None):
        return deepcopy([v for v in self.vehicles
                         if cid is None or v['character_id'] == cid])

    async def list_vehicle_ammo(self, pid):
        return deepcopy(self.ammo)

    async def get_ribbon_xp(self, pid, cid):
        return dict(self.xp.get(cid, {}))

    async def create_vehicle(self, pid, template, cid):
        row = dict(self.vehicles[0], id=20 + len(self.vehicles),
                   vehicle_variant_template_id=template, character_id=cid,
                   equipped_in_slot=0)
        self.vehicles.append(row)
        return dict(row)

    async def set_vehicle_equipment(self, pid, vid, cid, slot):
        for v in self.vehicles:
            if v['id'] == vid:
                v['equipped_in_slot'] = slot
        return []

    async def create_vehicle_ammo(self, pid, template, vid, cid, condition):
        if self.fail_insert:
            raise RuntimeError('insert failed')
        row = dict(id=100 + len(self.ammo), ammo_template_id=template, vehicle_id=vid,
                   character_id=cid, equipped_storage_id=0, slot_index=0,
                   condition=condition)
        self.ammo.append(row)
        return dict(row)

    async def set_vehicle_ammo_equipment(self, pid, aid, sid, slot, equipped):
        self.equips.append((aid, sid, slot, equipped))
        for a in self.ammo:
            if a['id'] == aid:
                a.update(equipped_storage_id=sid if equipped else 0,
                         slot_index=slot if equipped else 0)

    async def spend_currency(self, pid, key, cost, conn=None):
        if not self.can_pay:
            return False, 0
        self.spent.append((key, cost))
        return True, 1000

    async def add_currency(self, pid, **amounts):
        self.refunds.append(amounts)

    def __getattr__(self, name):
        # Only the irrelevant read APIs used by soldier replay return empty rows.
        if name in {'list_weapons', 'list_weapon_ammo', 'list_weapon_modifiers',
                    'list_vehicle_upgrades', 'list_uniform_variants',
                    'list_weapon_variants', 'list_uniform_parts',
                    'list_vehicle_variants', 'list_tracer_effects',
                    'list_depot_new_items'}:
            async def empty(*args):
                return []
            return empty
        raise AttributeError(name)


@pytest.fixture
def tables(monkeypatch):
    # One 3-slot cannon, a separate coax store, and duplicated shared weapon slots.
    monkeypatch.setitem(wardb._VEHICLE_VARIANT_PARENT, 91001, 91002)
    monkeypatch.setitem(wardb._VEHICLE_WEAPON_SLOTS, 91002,
                        [(92001, 93001), (92001, 93001), (92002, 93002)])
    monkeypatch.setitem(wardb._AMMO_MAPPINGS_BY_WEAPON, 92001,
                        [(94001, 95001, True), (94002, 95002, False)])
    monkeypatch.setitem(wardb._AMMO_MAPPINGS_BY_WEAPON, 92002,
                        [(94003, 95003, True)])
    monkeypatch.setitem(wardb._VEHICLE_AMMO_STORAGE_SLOTS, 93001, 3)
    monkeypatch.setitem(wardb._VEHICLE_AMMO_STORAGE_SLOTS, 93002, 1)
    monkeypatch.setitem(wardb._VEHICLE_AMMO_STORAGE_CLIPS, 93001, 60)
    for mid, uid in [(94001, 1901), (94002, 1902), (94003, 1903)]:
        monkeypatch.setitem(wardb._UNLOCK_BY_ITEM,
                            (mid, wardb._E_WEAPON_AMMO_MAPPING), {uid})
        monkeypatch.setitem(wardb._REQ_UNLOCK_BY_ITEM,
                            (mid, wardb._E_WEAPON_AMMO_MAPPING), {uid})
        monkeypatch.setitem(wardb._UNLOCK_UID_META, uid, (1, 5))
    monkeypatch.setattr(service, '_CHAR_DEFAULT_UNLOCKS', {})
    monkeypatch.setattr(service, 'STARTING_GEAR', {})
    monkeypatch.setattr(service, '_LEVEL0_UIDS', set())
    monkeypatch.setattr(service, '_RANK_META_LEVEL', [])
    monkeypatch.setattr(service, '_RIBBON_META_LEVEL', {'91': {(1902, 2)}})
    monkeypatch.setattr(service, '_ribbon_level_from_xp', int)
    for at in (95001, 95002, 95003):
        monkeypatch.setitem(wardb._AMMO_MAXCOND, at, 1000.0)
        monkeypatch.setitem(wardb._PRICING_MAP, at, {
            'type': 'ammotemplate', 'object_id': at, 'credits': 100, 'gold': 10})


@pytest.fixture
def store(monkeypatch, tables):
    st = Store()
    for module in (service, purchases, recruits, shop, weapons, characters):
        monkeypatch.setattr(module, 'STORE', st)
    return st


def kvcs(writer):
    rows = []
    for raw in writer.frames:
        frame, _ = Chunk.parse(raw)
        title = frame.subs[0]
        if title.data == b'KeyValueChangeSet':
            table = title.subs[0].subs[0]
            fields = pb_parse(table.subs[0].data)
            if table.data == b'character':
                fields[21] = pb_parse_multi(table.subs[0].data)[21]
            rows.append((table.data, fields))
    return rows


def response(writer):
    frame, _ = Chunk.parse(writer.frames[0])
    return pb_parse(frame.subs[0].subs[0].data)[1]


def bits(cid=10):
    return asyncio.run(service._char_bits_for(Session(), cid))


def purchase(st, template=95002, vid=20, price=None):
    writer = Writer()
    request = (pb_int64(1, template) + pb_int64(2, vid)
               + pb_int64(3, template if price is None else price) + pb_int64(5, 1))
    asyncio.run(purchases.handle_createvehicleammorequest(
        Session(), writer, 99, 'CreateVehicleAmmoRequest', request))
    return writer


def add_ammo(st, template=95002, cid=10, vid=20, sid=0, slot=0):
    row = dict(id=100 + len(st.ammo), ammo_template_id=template, vehicle_id=vid,
               character_id=cid, equipped_storage_id=sid, slot_index=slot,
               condition=1000.0)
    st.ammo.append(row)
    return row


@pytest.mark.parametrize('entry', ['grant', 'recruit', 'bundle', 'shop'])
def test_initial_vehicle_grants_one_per_independent_store(store, monkeypatch, entry):
    writer = Writer()
    if entry == 'grant':
        asyncio.run(purchases.grant_default_vehicle_ammo(1, 10, 20, 91002,
                                                         Session(), writer))
    elif entry == 'recruit':
        asyncio.run(recruits._grant_starting_gear(1, 10, {'vehicle_variant': 91001}))
    elif entry == 'bundle':
        asyncio.run(shop._bundle_deliver_vehicle(Session(), writer, 1, 10, 91001, True))
    else:
        async def owned(*args):
            return True

        async def charge(*args, **kwargs):
            return 100, True

        monkeypatch.setattr(purchases, '_character_owned', owned)
        monkeypatch.setattr(purchases, '_charge_player_multi', charge)
        request = pb_int64(1, 10) + pb_int64(2, 91001) + pb_int64(3, 123) + pb_int64(8, 1)
        asyncio.run(purchases.handle_createvehiclevariantrequest(
            Session(), writer, 99, '', request))
        assert response(writer) == 1
        names = [name for name, _ in kvcs(writer)]
        assert names.index(b'character') < names.index(b'VehicleAmmo')
    assert len(store.ammo) == 2
    assert {(a['ammo_template_id'], a['equipped_storage_id'], a['slot_index'])
            for a in store.ammo} == {(95001, 93001, 1), (95003, 93002, 1)}
    assert all(a['condition'] == 1000 for a in store.ammo)


def test_owning_vehicle_unlocks_only_its_default_ammo_without_any_ammo_rows(store):
    assert {1901, 1903} <= bits()
    assert 1902 not in bits()
    assert not store.ammo


@pytest.mark.parametrize('count', [1, 2, 3])
@pytest.mark.parametrize('equipped', [False, True])
def test_owned_ammo_permission_does_not_depend_on_count_or_equipment(store, count, equipped):
    for slot in range(1, count + 1):
        add_ammo(store, sid=93001 if equipped else 0, slot=slot if equipped else 0)
    assert 1902 in bits()


@pytest.mark.parametrize('cid,vid,template', [
    (11, 20, 95002), (10, 0, 95002), (10, 999, 95002), (10, 20, 95999)])
def test_other_character_depot_or_incompatible_ammo_cannot_grant_permission(
        store, cid, vid, template):
    add_ammo(store, template, cid, vid)
    assert 1902 not in bits()


def test_other_characters_vehicle_does_not_grant_default_or_owned_ammo(store):
    store.vehicles[0]['character_id'] = 11
    add_ammo(store)
    assert not ({1901, 1902, 1903} & bits())


def test_ribbon_unlocks_advanced_ammo_without_inventory(store):
    store.xp[10] = {91: 1}
    assert 1902 not in bits()
    store.xp[10] = {91: 2}
    assert 1902 in bits()
    assert not store.ammo


def test_any_matching_unlock_requirement_allows_purchase(store, monkeypatch):
    monkeypatch.setitem(wardb._REQ_UNLOCK_BY_ITEM,
                        (94002, wardb._E_WEAPON_AMMO_MAPPING), {1902, 1904})
    monkeypatch.setitem(wardb._UNLOCK_UID_META, 1904, (1, 5))
    monkeypatch.setitem(service._RIBBON_META_LEVEL, '92', {(1904, 1)})
    store.xp[10] = {92: 1}
    assert 1902 not in bits() and 1904 in bits()
    assert response(purchase(store)) == 1
    assert len(store.ammo) == 1


@pytest.mark.parametrize('metadata', [(2, 5), (1, 1)])
def test_other_faction_or_class_unlock_cannot_bypass_purchase_gate(store, monkeypatch, metadata):
    monkeypatch.setitem(wardb._REQ_UNLOCK_BY_ITEM,
                        (94002, wardb._E_WEAPON_AMMO_MAPPING), {1902, 1904})
    monkeypatch.setitem(wardb._UNLOCK_UID_META, 1904, metadata)
    monkeypatch.setitem(service._RIBBON_META_LEVEL, '92', {(1904, 1)})
    store.xp[10] = {92: 1}
    assert response(purchase(store)) == 0
    assert not store.spent and not store.ammo


def test_locked_purchase_rejected_before_charge_or_inventory_mutation(store):
    writer = purchase(store)
    assert response(writer) == 0
    assert not store.spent and not store.ammo and not kvcs(writer)


@pytest.mark.parametrize('template', [95001, 95002])
def test_each_purchase_creates_one_unmounted_row_and_pushes_permission_first(store, template):
    store.xp[10] = {91: 2}
    for count in range(1, 4):
        writer = purchase(store, template)
        assert response(writer) == 1
        assert len(store.ammo) == count
        assert len(store.spent) == count
        assert store.spent[-1] == ('credits', 100)
        assert not store.equips
        assert all(a['equipped_storage_id'] == a['slot_index'] == 0 for a in store.ammo)
        rows = kvcs(writer)
        assert [name for name, _ in rows] == [b'character', b'VehicleAmmo']
        # Actual repeated-bool field 21 is visible to the SWF before ammo ADD.
        uid = 1901 if template == 95001 else 1902
        assert rows[0][1][21][uid] == 1
        assert rows[1][1][2] == template


def test_previously_owned_advanced_ammo_can_be_repurchased_without_xp(store):
    add_ammo(store)
    assert response(purchase(store)) == 1
    assert len(store.ammo) == 2 and store.spent == [('credits', 100)]


@pytest.mark.parametrize('template,vid,price', [
    (95999, 20, None), (95001, 999, None), (95001, 20, 95002)])
def test_invalid_ammo_vehicle_or_price_never_creates_or_charges(store, template, vid, price):
    assert response(purchase(store, template, vid, price)) != 1
    assert not store.spent and not store.ammo


@pytest.mark.parametrize('failure', ['balance', 'insert'])
def test_failed_purchase_has_no_fake_inventory_and_refunds_only_completed_charge(store, failure):
    store.can_pay = failure != 'balance'
    store.fail_insert = failure == 'insert'
    writer = purchase(store, 95001)
    assert response(writer) == (56 if failure == 'balance' else 0)
    assert not store.ammo and not kvcs(writer)
    assert store.refunds == ([{'credits': 100}] if failure == 'insert' else [])


@pytest.mark.parametrize('slots', [[], [0], [1], [1, 2], [1, 2, 3], [1, 0, 0], [2]])
def test_soldier_replay_preserves_empty_slots_inventory_and_existing_extra_rows(store, slots):
    for slot in slots:
        add_ammo(store, template=95001, sid=93001 if slot else 0, slot=slot)
    before = deepcopy(store.ammo)
    for _ in range(2):
        writer = Writer()
        asyncio.run(service._push_stored_soldier_data(Session(), writer))
        assert store.ammo == before
        assert not store.equips and not store.spent
        rows = [row for name, row in kvcs(writer) if name == b'VehicleAmmo']
        assert len(rows) == len(slots)
        assert [(r[4], r[5]) for r in rows] == [
            (a['equipped_storage_id'], a['slot_index']) for a in before]


@pytest.mark.parametrize('slots', [[1], [1, 2], [1, 2, 3]])
def test_fps_loadout_contains_only_mounted_rows_and_keeps_spawn_clip_count(store, slots):
    for slot in slots + [0]:
        add_ammo(store, template=95001, sid=93001 if slot else 0, slot=slot)
    raw = warserver_messages._msg_vehicle_setting(store.vehicles[0], [], store.ammo)
    setting = pb_parse_multi(pb_parse_multi(raw)[4][0])
    rows = [pb_parse(r) for r in setting[6]]
    assert [r[3] for r in rows] == slots
    assert all(r[2] == 93001 for r in rows)
    assert all(struct.unpack('<f', r[6])[0] == 60.0 for r in rows)


def test_real_database_multislot_defaults_are_not_multiplied(monkeypatch):
    parent = next(parent for parent in wardb._VEHICLE_WEAPON_SLOTS
                  if any(wardb._VEHICLE_AMMO_STORAGE_SLOTS.get(sid, 1) == 3
                         for sid, _ in wardb._default_vehicle_ammo_for(parent)))
    store = Store()
    monkeypatch.setattr(purchases, 'STORE', store)
    rows = asyncio.run(purchases.grant_default_vehicle_ammo(1, 10, 20, parent))
    expected = {sid for sid, _ in wardb._default_vehicle_ammo_for(parent)}
    assert len(rows) == len(expected) == len(store.ammo)
    assert {sid for sid, _, _ in rows} == expected
    assert all(slot == 1 for _, slot, _ in rows)
