import asyncio
import struct

import handlers
import player_service
import state
from protocol import Chunk, pb_parse


class _Writer:
    def __init__(self):
        self.frames = []

    def write(self, data):
        self.frames.append(data)

    async def drain(self):
        pass


def test_session_never_resolves_player_before_authentication(monkeypatch):
    class Store:
        calls = []

        async def get_player(self, username):
            self.calls.append(username)
            return 17

    store = Store()
    monkeypatch.setattr(state, 'STORE', store)
    sess = state.Session(_Writer())
    sess.username = 'victim'

    assert asyncio.run(sess.ensure_player()) is None
    assert store.calls == []

    sess.authenticated = True
    assert asyncio.run(sess.ensure_player()) == 17
    assert store.calls == ['victim']


def test_dispatch_rejects_prelogin_business_request():
    sess = state.Session(_Writer())
    writer = _Writer()
    handled = asyncio.run(handlers.dispatch(
        sess, writer, 9, 'RenameWeaponRequest', b''))

    assert handled is True
    frame, _ = Chunk.parse(writer.frames[0])
    assert frame.subs[0].as_str() == 'HandlerErrorResponse'


def test_charge_rejects_missing_and_mismatched_pricing(monkeypatch):
    class Store:
        calls = []

        async def spend_currency(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return True, 90

    class Session:
        async def ensure_player(self):
            return 3

    store = Store()
    monkeypatch.setattr(player_service, 'STORE', store)
    monkeypatch.setattr(player_service, '_PRICING_MAP', {
        10: {'type': 'WeaponVariantTemplate', 'object_id': 100,
             'credits': 10, 'warfunds': 1, 'gold': 1},
    })

    assert asyncio.run(player_service._charge_player(
        Session(), 0, 1)) == (0, False)
    assert asyncio.run(player_service._charge_player(
        Session(), 10, 1, expected_object_id=999,
        expected_types='WeaponVariantTemplate')) == (0, False)
    assert store.calls == []

    assert asyncio.run(player_service._charge_player(
        Session(), 10, 1, expected_object_id=100,
        expected_types='WeaponVariantTemplate')) == (10, True)
    assert len(store.calls) == 1


def test_protocol_rejects_malformed_sizes_and_truncated_fields():
    malformed = struct.pack('<ii', 8, 100)
    try:
        Chunk.parse(malformed)
    except ValueError:
        pass
    else:
        raise AssertionError('oversized self_size accepted')

    try:
        pb_parse(b'\x0a\x05x')
    except ValueError:
        pass
    else:
        raise AssertionError('truncated length-delimited field accepted')
