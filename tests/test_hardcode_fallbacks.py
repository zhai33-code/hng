import asyncio
import inspect

from handlers import auth
from handlers import characters_purchase
from handlers import squad
import player_service
import matchmaking
import messages
import mm_handler
import warserver_messages
from handlers import acks
from handlers import uniforms
from protocol import pb_int32, pb_int64, pb_parse_loose, msg_player
from war_campaign.production import _production_interval_ms
import wardb


class _Writer:
    def __init__(self):
        self.frames = []

    def write(self, frame):
        self.frames.append(frame)

    async def drain(self):
        pass


class _Session:
    soldier_view_cmd = None
    playerview_cmd = None

    async def ensure_player(self):
        return 42


class _FailingUniformStore:
    def __init__(self):
        self.refunds = []

    async def create_uniform_variant(self, *_args):
        raise RuntimeError('forced insert failure')

    async def get_character(self, player_id, character_id):
        return {'id': character_id, 'player_id': player_id}

    async def add_currency(self, player_id, **amount):
        self.refunds.append((player_id, amount))


def test_purchase_insert_failure_refunds_and_does_not_make_fake_id(monkeypatch):
    store = _FailingUniformStore()
    writer = _Writer()
    session = _Session()

    async def charged(*_args, **_kwargs):
        return 125, True

    monkeypatch.setattr(uniforms, 'STORE', store)
    monkeypatch.setattr(player_service, 'STORE', store)
    monkeypatch.setattr(uniforms, '_charge_player', charged)
    proto = (pb_int64(1, 7) + pb_int64(5, 99) + pb_int64(6, 123)
             + pb_int32(8, 1))

    asyncio.run(uniforms.handle_createuniformvariantrequest(
        session, writer, 5, '', proto))

    assert store.refunds == [(42, {'credits': 125})]
    assert len(writer.frames) == 1
    assert not hasattr(session, '_fallback_uniform_variant_id')


def test_production_interval_comes_from_wardb_rows():
    assert _production_interval_ms([
        {'run_every_seconds': 600, 'produce_per_run': 1},
        {'run_every_seconds': 600, 'produce_per_run': 2},
    ]) == 600_000
    assert _production_interval_ms([]) is None


def test_unknown_preset_cannot_silently_launch_mill():
    preset_id = 987654321
    assert wardb._load_preset(preset_id) == {}
    try:
        wardb.preset_scene(preset_id)
    except LookupError:
        pass
    else:
        raise AssertionError('unknown preset silently selected a scene')


def test_missing_player_and_war_data_stays_empty():
    player = pb_parse_loose(msg_player('Unknown'))
    assert player[1] == 0
    assert player[10] == 0
    assert player[11] == 0
    assert player[14] == 0
    assert player[17] == 0
    assert player[24] == 0
    assert player[26] == b''
    catalogue = pb_parse_loose(messages.msg_query_war_catalogue_response())
    assert 1 not in catalogue
    assert catalogue[2] == 0


def test_runtime_battlefield_ids_are_scoped_to_mission():
    a = warserver_messages._runtime_entity_id(101, 'battlefield')
    b = warserver_messages._runtime_entity_id(102, 'battlefield')
    assert a != b
    assert a not in (1, 880001, 880100, 880101)


def test_ordinary_missions_do_not_claim_a_strategy_city():
    ai = pb_parse_loose(
        matchmaking._msg_mission_info(10, 42, 'zulu', 2))
    assert ai[5] == 0
    assert ai[6] == 0

    staged = pb_parse_loose(mm_handler._msg_mission_info_multi(
        11, {
            'players': [{'player_id': 42}],
            'preset': 10,
            'battle_id': 0,
            'defender_faction': 2,
        }, 'zulu'))
    assert staged[5] == 0
    assert staged[6] == 0


def test_unimplemented_mutations_do_not_return_fake_success():
    for request in (
            'SteamFinalizeTransactionRequest', 'DebugSetPlayerGoldRequest',
            'DebugResupplyATRequest', 'AdminToolExecuteSqlRequest'):
        assert request not in acks.HANDLERS


def test_production_auth_has_no_demo_identity_fallback():
    source = inspect.getsource(auth)
    assert 'DemoPlayer' not in source


def test_missing_identity_and_unknown_pack_have_no_placeholder_fallbacks():
    assert "sess.username or 'Player'" not in inspect.getsource(squad)
    source = inspect.getsource(characters_purchase)
    assert "char_name = 'Soldier'" not in source
    assert 'faction, template, rank = 1, 1, 0' not in source


def test_missing_force_pool_does_not_make_fake_assault_teams(monkeypatch):
    monkeypatch.setattr(matchmaking.force_pool, 'assault_team_names',
                        lambda *_args: [])
    assert matchmaking._assault_team_nodes(1, 999) == []
