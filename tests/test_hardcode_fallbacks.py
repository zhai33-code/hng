import asyncio

import player_service
from handlers import uniforms
from protocol import pb_int32, pb_int64
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
