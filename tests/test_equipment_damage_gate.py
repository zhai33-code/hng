"""Damage gates and FPS serialization, including soldiers switched to in-game."""
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
def wear_tables(monkeypatch):
    monkeypatch.setitem(wardb._WEAPON_WEAR, 90001, (1.0, 0.001, 0.0))
    monkeypatch.setitem(wardb._WEAPON_WEAR, 90002, (0.0, 0.0, 0.0))
    monkeypatch.setitem(wardb._WEAPON_MOD_WEAR, 90003, (1.0, 0.001, 0.0))


def weapon(cid, condition, wid=1, slot=1, template=90001):
    return {'id': wid, 'character_id': cid, 'template_id': template,
            'container_slot': slot, 'condition': condition,
            'clip_count': 0, 'name': 'Test'}


@pytest.mark.parametrize('condition,broken', [(0, True), (0.09, True),
                                             (0.1, False), (1, False)])
def test_damage_threshold(wear_tables, condition, broken):
    assert wardb.is_weapon_broken(weapon(1, condition)) is broken
    assert wardb.is_weapon_modifier_broken(
        {'template_id': 90003, 'condition': condition}) is broken


def test_non_wearing_weapon_at_zero_is_usable(wear_tables):
    w = weapon(1, 0, template=90002)
    assert not wardb.is_weapon_broken(w)
    assert messages._msg_weapon_setting(w, [], [], 1)


def test_all_soldiers_filtered_and_repair_restores_weapon(wear_tables):
    chars = [{'id': cid, 'name': str(cid), 'template': 1, 'faction': 1}
             for cid in (101, 102)]
    broken = weapon(102, 0, wid=2)
    loadout = {'weapons': {101: [weapon(101, 1)], 102: [broken]},
               'w_ammo': {}, 'w_mods': {}, 'vehicles': {}, 'v_ups': {},
               'v_ammo': {}, 'uv_tmpl': {}}
    data = messages._msg_player_settings(1, 2, 101, faction=1, db_chars=chars,
                                         loadout=loadout)
    entries = [pb_parse_multi(row) for row in pb_parse_multi(data)[12]]
    by_id = {row[1][0]: row for row in entries}
    assert len(by_id[101][3]) == 1
    assert 3 not in by_id[102]  # the other, switchable soldier has no broken gun
    assert broken['condition'] == 0  # serialization never changes the DB row
    broken['condition'] = 1.0
    data = messages._msg_player_settings(1, 2, 101, faction=1, db_chars=chars,
                                         loadout=loadout)
    entries = [pb_parse_multi(row) for row in pb_parse_multi(data)[12]]
    assert all(3 in row for row in entries)


def test_only_damaged_modifiers_removed(wear_tables):
    mods = [{'id': mid, 'template_id': 90003, 'condition': condition}
            for mid, condition in ((1, 0), (2, 0.09), (3, 0.1), (4, 1))]
    raw = messages._msg_weapon_setting(weapon(1, 1), [], mods, 1)
    setting = pb_parse_multi(pb_parse_multi(raw)[3][0])
    assert [pb_parse_multi(row)[1][0] for row in setting[4]] == [3, 4]
    assert len(mods) == 4


class Writer:
    def __init__(self):
        self.data = b''

    def write(self, data):
        self.data += data

    async def drain(self):
        pass


@pytest.mark.parametrize('squad,mode,mission', [(False, 2, 0), (False, 12, 0),
                                               (False, 2, 123), (True, 2, 0)])
def test_matchmaking_rejects_before_any_queue_mutation(
        monkeypatch, wear_tables, squad, mode, mission):
    pid, cid = 998001, 101
    char = {'id': cid, 'name': 'Test', 'template': 1, 'faction': 1}

    class Store:
        async def list_vehicles(self, _pid, character_id=None):
            return []

        async def list_characters(self, _pid):
            return [char]

        async def get_mission_record(self, _mid):
            return None

        async def list_weapons(self, player_id, character_id=None):
            if squad and player_id == pid:
                return [weapon(cid, 1)]
            return [weapon(102 if squad else cid, 0)]

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
    sess = SimpleNamespace(username='DamageTest', playerview_cmd=None,
                           ensure_player=ensure_player)
    writer = Writer()
    request = pb_int64(1, cid) + pb_int32(7, mode) + pb_int64(2, mission)
    asyncio.run(mm_handler._handle_join(sess, writer, 55, request))
    assert writer.data == build_response(55, 'JoinMatchMakingResponse',
                                         pb_int32(1, 231))
    assert mm_state.POOL.get(pid) is None
    assert pid not in mm_state._MM_QUEUES


def test_quickplay_skips_broken_character(monkeypatch):
    chars = [{'id': 101, 'template': 1, 'faction': 1},
             {'id': 102, 'template': 1, 'faction': 1}]

    async def vehicle_cats(*_args):
        return ()

    monkeypatch.setattr(mm_handler, '_char_vehicle_cats', vehicle_cats)
    monkeypatch.setattr(mm_handler, '_find_joinable_mission', lambda *_a, **_k: None)
    picked, info = asyncio.run(mm_handler._pick_quickplay_character(
        1, chars, chars[0], 11, broken_character_ids={101}))
    assert picked['id'] == 102
    assert len(info) == 1


@pytest.mark.parametrize('condition,slot', [(0.1, 1), (1.0, 1), (0.0, 0)])
def test_healthy_or_unequipped_damage_allows_matchmaking(
        monkeypatch, wear_tables, condition, slot):
    pid, cid = 998011, 101

    class Store:
        async def list_vehicles(self, _pid, character_id=None):
            return []

        async def list_characters(self, _pid):
            return [{'id': cid, 'name': 'Test', 'template': 1, 'faction': 1}]

        async def list_weapons(self, _pid):
            return [weapon(cid, condition, slot=slot)]

        async def set_player_selected_char(self, *_args):
            pass

    async def eligible(_pid):
        return None, None

    async def ensure_player():
        return pid

    async def no_op(*_args, **_kwargs):
        pass

    async def vehicle_cats(*_args):
        return ()

    monkeypatch.setattr(state, 'STORE', Store())
    monkeypatch.setattr(mm_handler, '_squad_eligible_members', eligible)
    monkeypatch.setattr(mm_handler, '_find_player_active_mission', lambda _: None)
    monkeypatch.setattr(mm_handler, '_find_joinable_mission', lambda *_a, **_k: None)
    monkeypatch.setattr(mm_handler, '_char_vehicle_cats', vehicle_cats)
    monkeypatch.setattr(mm_handler, '_try_form_all', no_op)
    monkeypatch.setattr(mm_handler, '_arm_ai_fill', lambda *_a: None)
    monkeypatch.setattr(mm_handler, 'POOL', type(mm_handler.POOL)())
    monkeypatch.setattr(mm_handler, '_MM_QUEUES', {})
    monkeypatch.setattr(mm_handler, '_MATCH_PMM_IDS', {})
    monkeypatch.setattr(mm_handler, '_SOLO_SQUADS', {})
    monkeypatch.setattr(mm_handler, 'PLAYER_SELECTED_CHAR', {})
    sess = SimpleNamespace(username='HealthyTest', playerview_cmd=None,
                           ensure_player=ensure_player)
    writer = Writer()
    asyncio.run(mm_handler._handle_join(
        sess, writer, 55, pb_int64(1, cid) + pb_int32(7, 2)))
    assert writer.data == build_response(55, 'JoinMatchMakingResponse', pb_int32(1, 1))
    assert mm_handler.POOL.get(pid) is not None
