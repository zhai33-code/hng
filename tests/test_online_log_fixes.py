"""Regressions for issues identified by the 2026-10-26 online log audit."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from handlers import squad
import mm_config
import mm_handler
from protocol import Chunk, pb_int32, pb_int64, pb_parse


SID = 7699723661786535486


class Writer:
    def __init__(self):
        self.frames = []

    def write(self, frame):
        self.frames.append(frame)

    async def drain(self):
        pass


def session(pid):
    return SimpleNamespace(player_id=pid, playerview_cmd=6, writer=Writer(),
                           ensure_player=AsyncMock(return_value=pid))


def set_flag(sess, sid, flag=True):
    asyncio.run(squad.handle_set_squad_matchmaking_flag(
        sess, sess.writer, 99, 'SetSquadMatchMakingFlagRequest',
        pb_int64(1, sid) + pb_int32(3, flag)))
    root, _ = Chunk.parse(sess.writer.frames[-1])
    assert root.subs[0].as_str() == 'SetSquadMatchMakingFlagResponse'
    return pb_parse(root.subs[0].subs[0].data)[1]


@pytest.fixture
def squads(monkeypatch):
    store = SimpleNamespace(get_squad=AsyncMock(), list_squad_members=AsyncMock(),
                            set_squad_flag=AsyncMock(return_value=True))
    monkeypatch.setattr(squad, 'STORE', store)
    monkeypatch.setattr(squad, '_SOLO_SQUADS', {})
    monkeypatch.setattr(squad, '_MM_MISSION_SQUADS', {})
    monkeypatch.setattr(squad, 'FLASH_SESSIONS', {})
    monkeypatch.setattr(squad, '_push_kvcs', AsyncMock())
    monkeypatch.setattr(squad, 'log', Mock())
    return store


@pytest.mark.parametrize('kind', ['solo', 'auto'])
@pytest.mark.parametrize('flag', [False, True])
def test_owned_implicit_squad_never_queries_database(squads, kind, flag):
    if kind == 'solo':
        squad._SOLO_SQUADS[11] = {'squad_row': SID}
    else:
        squad._MM_MISSION_SQUADS[88] = {SID: {
            'leader_char_id': 31,
            'members': [{'player_id': 2, 'char_id': 25},
                        {'player_id': 11, 'char_id': 31}]}}
    squads.get_squad.side_effect = OverflowError('value out of int32 range')
    sess = session(11)
    assert set_flag(sess, SID, flag) == squad.RC_OK
    squads.get_squad.assert_not_awaited()
    squads.list_squad_members.assert_not_awaited()
    squads.set_squad_flag.assert_not_awaited()
    squad._push_kvcs.assert_not_awaited()
    assert len(sess.writer.frames) == 1
    assert not any('failed' in str(call) for call in squad.log.call_args_list)


@pytest.mark.parametrize('kind', ['solo', 'auto'])
def test_unowned_implicit_squad_still_rejects_without_database(squads, kind):
    if kind == 'solo':
        squad._SOLO_SQUADS[11] = {'squad_row': SID}
    else:
        squad._MM_MISSION_SQUADS[88] = {SID: {
            'members': [{'player_id': 11, 'char_id': 31},
                        {'player_id': 2, 'char_id': 25}]}}
    assert set_flag(session(2), SID) == squad.RC_PLAYER_NOT_OWNER
    squads.get_squad.assert_not_awaited()
    squads.set_squad_flag.assert_not_awaited()


@pytest.fixture
def persistent(squads, monkeypatch):
    leader, member = session(11), session(2)
    row = dict(id=123, leader_character_id=31, matchmaking_flag=False)
    members = [dict(player_id=11, character_id=31, gamertag='leader', faction=1),
               dict(player_id=2, character_id=25, gamertag='member', faction=1)]
    squads.get_squad.return_value = row
    squads.list_squad_members.return_value = members
    async def update(sid, flag):
        row['matchmaking_flag'] = flag
        return True
    squads.set_squad_flag.side_effect = update
    monkeypatch.setattr(squad, 'FLASH_SESSIONS', {'leader': leader, 'member': member})
    monkeypatch.setattr(squad, '_msg_squad_session', Mock(return_value=b'row'))
    return squads, leader, member, row


def test_persistent_leader_still_updates_and_syncs_only_peer_session(persistent):
    store, leader, member, row = persistent
    assert set_flag(leader, 123) == squad.RC_OK
    store.set_squad_flag.assert_awaited_once_with(123, True)
    assert row['matchmaking_flag'] is True
    squad._push_kvcs.assert_awaited_once_with(member.writer, 6, b'SquadSession', b'row')
    squad._msg_squad_session.assert_called_once()


def test_persistent_nonleader_cannot_change_or_sync_flag(persistent):
    store, leader, member, row = persistent
    assert set_flag(member, 123) == squad.RC_SQUAD_NOT_OWNED
    store.set_squad_flag.assert_not_awaited()
    squad._push_kvcs.assert_not_awaited()
    assert row['matchmaking_flag'] is False


def test_unknown_large_id_is_not_automatically_accepted(squads):
    squads.get_squad.return_value = None
    assert set_flag(session(11), SID) == squad.RC_SQUAD_NOT_FOUND
    squads.set_squad_flag.assert_not_awaited()


def test_failed_persistent_update_is_not_acknowledged_as_success(persistent):
    store, leader, _, _ = persistent
    store.set_squad_flag.side_effect = RuntimeError('DB unavailable')
    assert set_flag(leader, 123) == squad.RC_SQUAD_NOT_FOUND
    squad._push_kvcs.assert_not_awaited()


@pytest.fixture
def join_env(monkeypatch):
    logs = []
    store = SimpleNamespace(get_mission_record=AsyncMock(return_value=None))
    details = dict(preset=12, gamemode=3, attacker_faction=3,
                   defender_faction=2, players=[{'player_id': 11}])
    monkeypatch.setattr(mm_handler.state, 'STORE', store)
    monkeypatch.setattr(mm_handler, '_MM_MISSION_DETAILS', {88: details})
    monkeypatch.setattr(mm_handler, 'COMBAT_INSTANCES', {88: object()})
    monkeypatch.setattr(mm_handler, '_log', logs.append)
    return store, details, logs


def join(faction=2, category=5):
    return asyncio.run(mm_handler._specified_mission_join_error(
        88, faction, category, char_id=643))


@pytest.mark.parametrize('faction,category,reason', [
    (1, 5, 'faction_not_in_battle'),
    (2, 1, 'character_category_not_allowed'),
])
def test_same_response_62_logs_distinct_reasons(join_env, faction, category, reason):
    _, details, logs = join_env
    before = deepcopy(details)
    assert join(faction, category) == 62
    assert len(logs) == 1
    for field in ('mission=88', 'charId=643', f'charCategory={category}',
                  f'faction={faction}', 'gamemode=3', 'allowedCategories=[5]',
                  'attacker=3', 'defender=2', f'reason={reason}', 'response=62'):
        assert field in logs[0]
    assert details == before


@pytest.mark.parametrize('unavailable', ['missing', 'no_preset', 'no_instance'])
def test_unavailable_mission_still_returns_43_with_reason(join_env, unavailable):
    _, details, logs = join_env
    if unavailable == 'missing':
        mm_handler._MM_MISSION_DETAILS.clear()
    elif unavailable == 'no_preset':
        details.pop('preset')
    else:
        mm_handler.COMBAT_INSTANCES.clear()
    assert join() == 43
    assert 'reason=mission_unavailable' in logs[-1]


def test_invalid_factions_get_their_own_reason(join_env):
    _, details, logs = join_env
    details['attacker_faction'] = 0
    assert join() == 62
    assert 'reason=invalid_battle_factions' in logs[-1]


def test_full_mission_still_returns_227(join_env):
    _, details, logs = join_env
    limit = mm_config.gamemode_cfg(3).get('max_players', 18)
    details['players'] = [{}] * limit
    assert join() == 227
    assert 'reason=mission_full' in logs[-1]
    assert f'players={limit}' in logs[-1]


@pytest.mark.parametrize('category', [0, 5])
def test_valid_or_unknown_category_keeps_existing_acceptance(join_env, category):
    assert join(category=category) is None
    assert not join_env[2]


@pytest.mark.parametrize('state,faction,code,reason', [
    (2, 1, 43, 'mission_ended'), (3, 1, 43, 'mission_ended'),
    (1, 3, 62, 'faction_not_in_battle'),
])
def test_war_rejections_keep_codes_and_log_context(join_env, state, faction, code, reason):
    store, _, logs = join_env
    store.get_mission_record.return_value = dict(
        battle_id=7, state=state, attacker_faction=1, defender_faction=2)
    assert join(faction=faction) == code
    assert 'source=war' in logs[-1] and 'gamemode=10' in logs[-1]
    assert f'reason={reason}' in logs[-1]
    assert 'attacker=1 defender=2' in logs[-1]


def test_active_war_without_instance_remains_requeueable(join_env):
    store, _, logs = join_env
    store.get_mission_record.return_value = dict(
        battle_id=7, state=1, attacker_faction=1, defender_faction=2)
    mm_handler.COMBAT_INSTANCES.clear()
    assert join(faction=1) is None
    assert not logs
