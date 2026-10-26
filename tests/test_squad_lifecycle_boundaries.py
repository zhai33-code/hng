"""Pending invitations must not acquire membership or control another queue."""
import asyncio
from unittest.mock import AsyncMock

import pytest

import matchmaking
import mm_pool
import state
from handlers import squad
from protocol import pb_parse, pb_varint
from test_squad_authorization import Store, Session, invoke


@pytest.fixture
def lobby(monkeypatch):
    store = Store()
    monkeypatch.setattr(squad, 'STORE', store)
    monkeypatch.setattr(state, 'STORE', store)
    monkeypatch.setattr(squad, 'FLASH_SESSIONS', {})
    monkeypatch.setattr(squad, '_MM_QUEUES', {})
    monkeypatch.setattr(matchmaking, '_MM_QUEUES', squad._MM_QUEUES)
    monkeypatch.setattr(matchmaking, 'FLASH_SESSIONS', {})
    monkeypatch.setattr(matchmaking, 'POOL', mm_pool.MatchPool())
    monkeypatch.setattr(matchmaking, '_MATCH_PMM_IDS', {})
    monkeypatch.setattr(squad, '_push_kvcs', AsyncMock())
    return store


@pytest.mark.parametrize('status', [2, 3])
def test_pending_cannot_join_via_status_request(lobby, status):
    code, _ = asyncio.run(invoke(squad.handle_set_squad_member_status,
        Session(3, 'pending'), pb_varint(1, 3) + pb_varint(3, status)))
    assert code == squad.RC_SQUAD_SLOT_NOT_FOUND
    assert lobby.members[3]['status'] == squad.ST_INVITE_PENDING


def test_pending_solo_queue_does_not_lock_inviting_squad(lobby):
    squad._MM_QUEUES[3] = {'mission_id': 0, 'ctx': {}}
    assert not squad._squad_is_locked(lobby.squad, lobby.members.values())


def test_pending_cannot_accept_while_in_own_solo_queue(lobby):
    squad._MM_QUEUES[3] = {'mission_id': 0, 'ctx': {}}
    code, _ = asyncio.run(invoke(squad.handle_squad_respond_to_invite,
        Session(3, 'pending'), pb_varint(1, 3) + pb_varint(2, 1)))
    assert code == squad.RC_SQUAD_IN_LOCKED_STATE
    assert lobby.members[3]['status'] == squad.ST_INVITE_PENDING


@pytest.mark.parametrize('war_selected', [False, True])
def test_reset_preserves_pending_and_war_filter(lobby, war_selected):
    lobby.squad['matchmaking_flag'] = war_selected
    lobby.members[1]['status'] = 3
    asyncio.run(matchmaking.reset_real_squad_to_lobby(10, push_rows=False))
    assert lobby.members[1]['status'] == 2
    assert lobby.members[3]['status'] == 1
    assert lobby.squad['matchmaking_flag'] is war_selected
    assert not lobby.flags


@pytest.mark.parametrize('pending', [False, True])
def test_cancel_does_not_remove_unrelated_solo_queue(lobby, pending):
    pid = 3 if pending else 2
    queue = {'mission_id': 0, 'pmm_id': 701, 'ctx': {}}
    matchmaking._MM_QUEUES[pid] = queue
    entry = mm_pool.QueueEntry(player_id=pid, char_id=100 + pid,
                              faction=1, gamemode_id=6)
    matchmaking.POOL.enqueue(entry)
    asyncio.run(matchmaking._cancel_squad_queue(10, push_rows=False))
    assert matchmaking._MM_QUEUES.get(pid) is queue
    assert matchmaking.POOL.get(pid) is entry
    assert lobby.members[3]['status'] == 1


def test_cancel_removes_only_own_squad_queue(lobby):
    matchmaking._MM_QUEUES[2] = {
        'mission_id': 0, 'pmm_id': 701, 'ctx': {'squad_db_id': 10}}
    matchmaking.POOL.enqueue(mm_pool.QueueEntry(
        player_id=2, char_id=102, faction=1, gamemode_id=6, group_key=10))
    asyncio.run(matchmaking._cancel_squad_queue(10, push_rows=False))
    assert 2 not in matchmaking._MM_QUEUES
    assert matchmaking.POOL.get(2) is None


def test_removing_middle_member_republishes_compacted_slots(lobby):
    remaining = Session(3, 'pending')
    squad.FLASH_SESSIONS['pending'] = remaining
    lobby.members[3]['status'] = 2
    code, _ = asyncio.run(invoke(squad.handle_remove_squad_member,
        Session(1, 'leader'), pb_varint(1, 2)))
    assert code == squad.RC_OK
    rows = [pb_parse(c.args[3]) for c in squad._push_kvcs.await_args_list
            if c.args[0] is remaining.writer and c.args[2] == b'SquadMemberSession']
    assert any(row[1] == 3 and row[3] == 2 for row in rows)


def test_one_player_exit_keeps_other_player_in_battle(lobby, monkeypatch):
    lobby.members[1]['status'] = lobby.members[2]['status'] = 4
    sessions = {tag: Session(pid, tag) for pid, tag in ((1, 'leader'), (2, 'member'))}
    monkeypatch.setattr(squad, 'FLASH_SESSIONS', sessions)
    matchmaking._MM_QUEUES[2] = {'mission_id': 700, 'ctx': {'squad_db_id': 10}}
    asyncio.run(matchmaking.reset_real_squad_to_lobby(10, player_ids={1}))
    assert lobby.members[1]['status'] == 2
    assert lobby.members[2]['status'] == 4
    views = {c.args[0]: pb_parse(c.args[3])
             for c in squad._push_kvcs.await_args_list
             if c.args[2] == b'SquadSession'}
    assert views[sessions['leader'].writer][17] == 0
    assert views[sessions['member'].writer][17] == 700


def test_filter_sync_keeps_peer_battle_binding(lobby):
    peer = Session(2, 'member')
    squad.FLASH_SESSIONS['member'] = peer
    lobby.members[2]['status'] = 4
    squad._MM_QUEUES[2] = {'mission_id': 700, 'ctx': {'squad_db_id': 10}}
    code, _ = asyncio.run(invoke(squad.handle_set_squad_matchmaking_flag,
        Session(1, 'leader'), pb_varint(1, 10) + pb_varint(3, 1)))
    assert code == squad.RC_OK
    rows = [pb_parse(c.args[3]) for c in squad._push_kvcs.await_args_list
            if c.args[0] is peer.writer and c.args[2] == b'SquadSession']
    assert rows[-1][17] == 700


def test_declining_invite_during_battle_keeps_existing_members_bound(lobby):
    peer = Session(2, 'member')
    squad.FLASH_SESSIONS['member'] = peer
    lobby.members[2]['status'] = 4
    squad._MM_QUEUES[2] = {'mission_id': 700, 'ctx': {'squad_db_id': 10}}
    code, _ = asyncio.run(invoke(squad.handle_squad_respond_to_invite,
        Session(3, 'pending'), pb_varint(1, 3) + pb_varint(2, 0)))
    assert code == squad.RC_OK
    rows = [pb_parse(c.args[3]) for c in squad._push_kvcs.await_args_list
            if c.args[0] is peer.writer and c.args[2] == b'SquadSession']
    assert rows[-1][17] == 700
