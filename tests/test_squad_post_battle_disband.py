"""Battle exit must not leave auto-squad ownership shadowing the lobby squad."""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mm_state
import state
import matchmaking
from handlers import combat, mission, progression, social, squad
from protocol import Chunk, pb_parse
from test_auto_squad_disband import (
    FakeSess, _collect_deletes, _collect_pushes, _remove_squad,
)

MID = 223287
SID = 8072128442240809119


async def _noop(*args, **kwargs):
    pass


@pytest.fixture
def roster(monkeypatch):
    tables = [mm_state._MM_MISSION_SQUADS, mm_state._SOLO_SQUADS,
              mm_state.PMS_TO_SESSION, mm_state.PMS_TO_MISSION,
              mm_state._MM_QUEUES, mm_state._MM_MISSION_DETAILS,
              state.FLASH_SESSIONS]
    saved = [dict(table) for table in tables]
    for table in tables:
        table.clear()
    for module in (state, mission, squad, combat):
        monkeypatch.setattr(module, 'STORE', None)
    monkeypatch.setattr(social, 'notify_friends_of', _noop)
    monkeypatch.setattr(progression, 'check_player_level', _noop)
    sessions = {}
    for pid, cid, tag, rid in ((2, 25, 'Leader', 101),
                               (11, 31, 'Member', 102)):
        session = FakeSess(pid, tag)
        session.soldier_view_cmd = None
        sessions[pid] = session
        state.FLASH_SESSIONS[tag] = session
        mm_state._SOLO_SQUADS[pid] = {
            'squad_row': 70000 + pid, 'member_row': 90000 + pid,
            'template': 1, 'faction': 1, 'mission_id': MID,
            'gamertag': tag, 'char_id': cid, 'char_name': tag, 'rank': 5}
        mm_state.PMS_TO_SESSION[80000 + pid] = {
            'player_id': pid, 'gamertag': tag, 'squad_db_id': 0,
            'auto_sq_id': SID, 'auto_sq_rows': [101, 102]}
        mm_state.PMS_TO_MISSION[80000 + pid] = MID
    mm_state._MM_MISSION_SQUADS[MID] = {SID: {
        'name': 'Auto', 'faction': 1, 'template': 1,
        'squad_db_id': 0, 'leader_char_id': 25,
        'members': [
            {'player_id': 2, 'char_id': 25, 'gamertag': 'Leader',
             'rank': 5, 'slot': 0, 'row_id': 101},
            {'player_id': 11, 'char_id': 31, 'gamertag': 'Member',
             'rank': 5, 'slot': 1, 'row_id': 102}]}}
    mm_state._MM_MISSION_DETAILS[MID] = {
        'players': [{'player_id': 2}, {'player_id': 11}]}
    try:
        yield sessions
    finally:
        for table, snapshot in zip(tables, saved):
            table.clear()
            table.update(snapshot)


def _session_fields(writer):
    rows = []
    offset = 0
    while offset < len(writer.data):
        chunk, offset = Chunk.parse(writer.data, offset)
        title = chunk.subs[0]
        if title.as_str() != 'KeyValueChangeSet':
            continue
        action = title.subs[0]
        table = action.subs[0]
        if action.as_str() == 'set' and table.as_str() == 'SquadSession':
            rows.extend(pb_parse(row.data) for row in table.subs)
    return rows


@pytest.mark.parametrize('pid', [2, 11])
def test_abandon_then_disband_lobby_squad(roster, pid):
    async def exercise():
        session = roster[pid]
        await mission.release_player_mission_sessions(
            pid, session.writer, session.playerview_cmd)
        info = mm_state._MM_MISSION_SQUADS[MID][SID]
        other_pid = 11 if pid == 2 else 2
        assert [m['player_id'] for m in info['members']] == [other_pid]
        assert 80000 + pid not in mm_state.PMS_TO_SESSION
        assert mm_state._SOLO_SQUADS[pid]['mission_id'] == 0
        assert ('SquadSession', 70000 + pid) in _collect_pushes(session.writer)
        assert ('SquadMemberSession', 101 if pid == 2 else 102) in (
            _collect_deletes(roster[other_pid].writer))
        if pid == 2:
            assert info['leader_char_id'] == 31
            assert _session_fields(roster[11].writer)[-1][2] == 11
        before = session.writer.data
        await mission.release_player_mission_sessions(
            pid, session.writer, session.playerview_cmd)
        assert session.writer.data == before, 'repeat release must be a no-op'
        code, writer = await _remove_squad(session)
        assert code == squad.RC_OK
        assert _collect_deletes(writer) == [
            ('SquadSession', 70000 + pid),
            ('SquadMemberSession', 90000 + pid)]
        assert [m['player_id'] for m in info['members']] == [other_pid]
    asyncio.run(exercise())


def test_finish_detaches_before_any_lobby_push(roster, monkeypatch):
    original_push = matchmaking._push_kvcs
    observed = []

    async def push(writer, cmd, table, row):
        lobby_row = (pb_parse(row) if table == b'SquadSession' else {})
        if lobby_row and lobby_row.get(17) == 0:
            assert MID not in mm_state._MM_MISSION_SQUADS
        await original_push(writer, cmd, table, row)
        if lobby_row.get(1) == 70011:
            # Handle a request immediately after the member's lobby view is
            # published, while settlement has not returned to release_match.
            code, response = await _remove_squad(roster[11])
            assert code == squad.RC_OK
            assert ('SquadSession', 70011) in _collect_deletes(response)
            observed.append(code)

    monkeypatch.setattr(matchmaking, '_push_kvcs', push)
    # Both players must be detached before even the first lobby row is sent.
    asyncio.run(combat._finish_mission_sessions(MID, None))
    assert observed
    assert MID not in mm_state._MM_MISSION_SQUADS
    assert not mm_state.PMS_TO_SESSION


def test_offline_finish_clears_membership(roster):
    state.FLASH_SESSIONS.clear()
    asyncio.run(combat._finish_mission_sessions(MID, None))
    assert MID not in mm_state._MM_MISSION_SQUADS
    assert not mm_state.PMS_TO_SESSION
    assert not mm_state.PMS_TO_MISSION
    asyncio.run(combat._finish_mission_sessions(MID, None))


def test_offline_departure_and_failed_peer_push(roster):
    class BrokenWriter:
        def write(self, data):
            raise ConnectionError('peer disconnected')

    state.FLASH_SESSIONS.pop('Leader')
    roster[11].writer = BrokenWriter()
    asyncio.run(mission.release_player_mission_sessions(2))
    info = mm_state._MM_MISSION_SQUADS[MID][SID]
    assert [m['player_id'] for m in info['members']] == [11]
    assert info['leader_char_id'] == 31
    assert 80002 not in mm_state.PMS_TO_SESSION


def test_cleanup_scoped_to_mission_and_temporary_squads(roster):
    import copy
    other = copy.deepcopy(mm_state._MM_MISSION_SQUADS[MID][SID])
    persistent = copy.deepcopy(other)
    persistent['squad_db_id'] = 123
    mm_state._MM_MISSION_SQUADS[MID + 1] = {SID + 1: other}
    mm_state._MM_MISSION_SQUADS[MID][123] = persistent
    asyncio.run(squad.release_auto_squad_memberships(MID, {2, 11}))
    assert mm_state._MM_MISSION_SQUADS[MID] == {123: persistent}
    assert len(persistent['members']) == len(other['members']) == 2
    before = [session.writer.data for session in roster.values()]
    asyncio.run(squad.release_auto_squad_memberships(MID, {2, 11}))
    assert before == [session.writer.data for session in roster.values()]


def test_current_auto_squad_still_rejects_nonleader(roster):
    code, writer = asyncio.run(_remove_squad(roster[11]))
    assert code == squad.RC_SQUAD_NOT_OWNED
    assert not _collect_deletes(writer)
    assert len(mm_state._MM_MISSION_SQUADS[MID][SID]['members']) == 2


def test_new_persistent_squad_after_exit_is_not_shadowed(roster, monkeypatch):
    class Store:
        def __init__(self):
            self.dissolved = []

        async def get_squad_for_player(self, pid):
            return {'id': 123, 'leader_character_id': 31}

        async def list_squad_members(self, sid):
            return [{'id': 555, 'player_id': 11, 'character_id': 31,
                     'gamertag': 'Member'}]

        async def dissolve_squad(self, sid):
            self.dissolved.append(sid)

    async def exercise():
        session = roster[11]
        await mission.release_player_mission_sessions(
            11, session.writer, session.playerview_cmd)
        store = Store()
        monkeypatch.setattr(squad, 'STORE', store)
        monkeypatch.setattr(squad, '_cancel_squad_queue', _noop)
        code, _ = await _remove_squad(session)
        assert code == squad.RC_OK
        assert store.dissolved == [123]
        assert ('SquadSession', 123) in _collect_deletes(session.writer)
        assert [m['player_id'] for m in (
            mm_state._MM_MISSION_SQUADS[MID][SID]['members'])] == [2]
    asyncio.run(exercise())


@pytest.mark.parametrize('online', [False, True])
@pytest.mark.parametrize('pid', [2, 11])
def test_persistent_departure_restores_only_leaver(roster, monkeypatch, online, pid):
    from test_squad_authorization import Store
    store = Store()
    store.squad['leader_character_id'] = 25
    store.members = {
        1: Store._member(1, 2, 'Leader', 25, 4),
        2: Store._member(2, 11, 'Member', 31, 4)}
    monkeypatch.setattr(state, 'STORE', store)
    monkeypatch.setattr(squad, 'STORE', store)
    for player_id in (2, 11):
        roster[player_id].player_id = player_id
        mm_state.PMS_TO_SESSION[80000 + player_id]['squad_db_id'] = 10
        mm_state._MM_QUEUES[player_id] = {
            'mission_id': MID, 'ctx': {'squad_db_id': 10}}
    mm_state._SOLO_SQUADS.clear()
    if not online:
        state.FLASH_SESSIONS.pop('Leader' if pid == 2 else 'Member')
    session = roster[pid]
    asyncio.run(mission.release_player_mission_sessions(
        pid, session.writer if online else None,
        session.playerview_cmd if online else None))
    statuses = {m['player_id']: m['status'] for m in store.members.values()}
    other_pid = 11 if pid == 2 else 2
    assert statuses == {pid: 2, other_pid: 4}
    assert _session_fields(roster[other_pid].writer)[-1][17] == MID
    assert mm_state._MM_QUEUES[other_pid]['mission_id'] == MID
    if online:
        assert _session_fields(session.writer)[-1][17] == 0
