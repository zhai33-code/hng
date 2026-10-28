"""Vehicle condition gates and FPS filtering for all switchable soldiers."""
import asyncio
from types import SimpleNamespace

import pytest

import mm_handler
import mm_state
import state
import wardb
import warserver_messages as messages
from protocol import build_response, pb_int32, pb_int64, pb_parse_multi


@pytest.fixture
def vehicle_tables(monkeypatch):
    monkeypatch.setitem(wardb._VEHICLE_VARIANT_PARENT, 91001, 91002)
    monkeypatch.setitem(wardb._VEHICLE_WEAR, 91002, (100.0, 1.0, 0.0))
    monkeypatch.setitem(wardb._VEHICLE_UPGRADE_WEAR, 91003, (50.0, 1.0, 0.0))
    monkeypatch.setitem(wardb._AMMO_WEAR, 91004, (1000.0, 1.0, 0.0))
    monkeypatch.setitem(wardb._AMMO_WEAR, 91005, (0.0, 0.0, 0.0))


def vehicle(condition=100, cid=101, vid=1, slot=1):
    return {'id': vid, 'vehicle_variant_template_id': 91001,
            'character_id': cid, 'condition': condition, 'equipped_in_slot': slot}


def ammo(condition=1000, storage=1, slot=1, template=91004):
    return {'id': 2, 'vehicle_id': 1, 'ammo_template_id': template,
            'condition': condition, 'equipped_storage_id': storage, 'slot_index': slot}


@pytest.mark.parametrize('condition,broken', [(0, True), (9, True),
                                             (10, False), (100, False)])
def test_vehicle_threshold_uses_parent_full_condition(vehicle_tables, condition, broken):
    assert wardb.is_vehicle_broken(vehicle(condition)) is broken
    assert bool(messages._msg_vehicle_setting(vehicle(condition), [], [])) is not broken


@pytest.mark.parametrize('condition,depleted', [(0, True), (99, True),
                                               (100, False), (1000, False)])
def test_ammo_threshold_and_vehicle_filter(vehicle_tables, condition, depleted):
    rows = [ammo(condition)]
    assert wardb.vehicle_has_depleted_ammo(rows) is depleted
    assert bool(messages._msg_vehicle_setting(vehicle(), [], rows)) is not depleted


def test_optional_upgrades_filtered_without_mutating_rows(vehicle_tables):
    ups = [{'id': i, 'upgrade_template_id': 91003, 'equipped_slot_id': 1,
            'condition': condition} for i, condition in enumerate((0, 4, 5, 50), 1)]
    raw = messages._msg_vehicle_setting(vehicle(), ups, [ammo()])
    setting = pb_parse_multi(pb_parse_multi(raw)[4][0])
    assert [pb_parse_multi(row)[1][0] for row in setting[5]] == [3, 4]
    assert len(ups) == 4 and ups[0]['condition'] == 0


def test_unmounted_and_nonwearing_ammo_do_not_disable_vehicle(vehicle_tables):
    rows = [ammo(0, storage=0), ammo(0, slot=0), ammo(0, template=91005)]
    assert not wardb.vehicle_has_depleted_ammo(rows)
    raw = messages._msg_vehicle_setting(vehicle(), [], rows)
    setting = pb_parse_multi(pb_parse_multi(raw)[4][0])
    assert len(setting[6]) == 1  # only mounted, non-wearing ammo survives


def test_other_soldier_vehicle_filtered_and_repair_restores_it(vehicle_tables):
    chars = [{'id': cid, 'name': str(cid), 'template': 1, 'faction': 1}
             for cid in (101, 102)]
    broken = vehicle(0, cid=102, vid=2)
    loadout = {'weapons': {}, 'w_ammo': {}, 'w_mods': {}, 'vehicles': {
        101: [vehicle()], 102: [broken]}, 'v_ups': {}, 'v_ammo': {}, 'uv_tmpl': {}}

    def entries():
        raw = messages._msg_player_settings(1, 2, 101, faction=1,
                                             db_chars=chars, loadout=loadout)
        rows = [pb_parse_multi(row) for row in pb_parse_multi(raw)[12]]
        return {row[1][0]: row for row in rows}

    data = entries()
    assert 4 in data[101] and 4 not in data[102]
    broken['condition'] = 100
    assert 4 in entries()[102]


class Writer:
    def __init__(self):
        self.data = b''

    def write(self, data):
        self.data += data

    async def drain(self):
        pass


@pytest.mark.parametrize('kind,code', [('vehicle', 233), ('ammo', 232)])
@pytest.mark.parametrize('squad,mode,mission', [(False, 2, 0), (False, 12, 0),
                                               (False, 2, 123), (True, 2, 0)])
def test_matching_damage_response_before_queue(
        monkeypatch, vehicle_tables, kind, code, squad, mode, mission):
    pid, cid = 998021, 101

    class Store:
        async def list_characters(self, _pid):
            return [{'id': cid, 'name': 'Driver', 'template': 1, 'faction': 1}]

        async def get_mission_record(self, _mid):
            return None

        async def list_weapons(self, _pid):
            return []

        async def list_vehicles(self, player_id):
            if squad and player_id == pid:
                return []
            return [vehicle(0 if kind == 'vehicle' else 100,
                            cid=102 if squad else cid)]

        async def list_vehicle_ammo(self, _pid):
            return [ammo(0 if kind == 'ammo' else 1000)]

    members = [{'player_id': pid, 'character_id': cid},
               {'player_id': pid + 1, 'character_id': 102}] if squad else None

    async def eligible(_pid):
        return ({'id': 99}, members) if squad else (None, None)

    async def ensure_player():
        return pid

    async def vehicle_cats(*_args):
        return ()

    monkeypatch.setattr(state, 'STORE', Store())
    monkeypatch.setattr(mm_handler, '_squad_eligible_members', eligible)
    monkeypatch.setattr(mm_handler, '_find_player_active_mission', lambda _: None)
    monkeypatch.setattr(mm_handler, '_char_vehicle_cats', vehicle_cats)
    sess = SimpleNamespace(username='VehicleDamage', playerview_cmd=None,
                           ensure_player=ensure_player)
    writer = Writer()
    request = pb_int64(1, cid) + pb_int32(7, mode) + pb_int64(2, mission)
    asyncio.run(mm_handler._handle_join(sess, writer, 55, request))
    assert writer.data == build_response(55, 'JoinMatchMakingResponse', pb_int32(1, code))
    assert mm_state.POOL.get(pid) is None and pid not in mm_state._MM_QUEUES


@pytest.mark.parametrize('condition,slot,ammo_condition', [(10, 1, 100),
                                                         (100, 1, 1000), (0, 0, 0)])
def test_working_or_unequipped_vehicle_allows_join_validation(
        monkeypatch, vehicle_tables, condition, slot, ammo_condition):
    class Store:
        async def list_weapons(self, _pid):
            return []

        async def list_vehicles(self, _pid):
            return [vehicle(condition, slot=slot)]

        async def list_vehicle_ammo(self, _pid):
            return [ammo(ammo_condition)]

    monkeypatch.setattr(state, 'STORE', Store())
    assert asyncio.run(mm_handler._equipment_join_errors(1)) == {}
