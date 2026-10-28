"""SwapHeadRequest: free selection, persistence and lobby character refresh."""
import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from handlers import characters_purchase as handler
from handlers import characters
from protocol import Chunk, pb_int64, pb_int32, pb_parse


class Writer:
    def __init__(self):
        self.frames = []

    def write(self, data):
        self.frames.append(data)

    async def drain(self):
        pass


class SwapHeadTests(unittest.IsolatedAsyncioTestCase):
    async def run_swap(self, head, pricing=0, owned=True, persisted=True):
        row = dict(id=101, name='Soldier', faction=3, template=1, head_id=0)
        store = unittest.mock.Mock()
        store.get_character = AsyncMock(return_value=row if owned else None)
        store.list_characters = AsyncMock(side_effect=lambda pid: [dict(row)])
        store.list_uniform_parts = AsyncMock(return_value=[])
        store.create_uniform_part = AsyncMock(return_value={'id': 9001})

        async def save(pid, cid, template):
            if persisted:
                row['head_id'] = template
            return persisted

        store.swap_character_head = AsyncMock(side_effect=save)
        sess = unittest.mock.Mock(soldier_view_cmd=7, playerview_cmd=None)
        sess.ensure_player = AsyncMock(return_value=42)
        writer = Writer()
        request = (pb_int64(1, 101) + pb_int64(2, head) + pb_int32(3, 0)
                   + pb_int64(4, pricing) + pb_int32(6, 1))
        # Real charge routine rejects omitted pricing; retain it for free requests.
        charge = AsyncMock(return_value=(100, True)) if pricing else AsyncMock(wraps=handler._charge_player)
        refund = AsyncMock()
        with patch.object(handler, 'STORE', store), patch.object(handler, '_charge_player', charge), \
                patch.object(handler, '_refund_player_charge', refund), \
                patch.object(handler, '_push_player_refresh', AsyncMock()), \
                patch.object(characters, '_char_unlock_bits_for', AsyncMock(return_value=b'')):
            await handler.handle_swapheadrequest(sess, writer, 21, 'SwapHeadRequest', request)
        responses, updates = [], []
        for frame in writer.frames:
            main, _ = Chunk.parse(frame)
            title = main.subs[0]
            if title.data == b'SwapHeadResponse':
                responses.append(pb_parse(title.subs[0].data)[1])
            elif title.data == b'KeyValueChangeSet':
                table = title.subs[0].subs[0]
                if table.data == b'character':
                    updates.append(pb_parse(table.subs[0].data))
        return row, store, charge, refund, responses, updates

    async def test_free_heads_save_and_refresh_lobby(self):
        for head in (85, 86, 87, 88, 89, 90):
            with self.subTest(head=head):
                row, store, charge, refund, responses, updates = await self.run_swap(head)
                self.assertEqual(responses, [1])
                charge.assert_not_awaited()
                self.assertEqual(row['head_id'], head)
                self.assertEqual(updates[-1][22], head)
                self.assertNotEqual(updates[-1][22], 9001)

    async def test_default_head(self):
        row, store, charge, refund, responses, updates = await self.run_swap(0)
        self.assertEqual(responses, [1])
        self.assertEqual(updates[-1][22], 0)
        store.create_uniform_part.assert_not_awaited()

    async def test_foreign_character_rejected_before_charge(self):
        row, store, charge, refund, responses, updates = await self.run_swap(89, 123, owned=False)
        self.assertEqual(responses, [0])
        charge.assert_not_awaited()
        store.create_uniform_part.assert_not_awaited()
        self.assertEqual(updates, [])

    async def test_persistence_failure_refunds_and_does_not_refresh(self):
        row, store, charge, refund, responses, updates = await self.run_swap(89, 123, persisted=False)
        self.assertEqual(responses, [0])
        refund.assert_awaited_once()
        self.assertEqual(updates, [])

    async def test_paid_head_still_charged(self):
        row, store, charge, refund, responses, updates = await self.run_swap(89, 123)
        self.assertEqual(responses, [1])
        charge.assert_awaited_once()
        self.assertEqual(updates[-1][22], 89)


if __name__ == '__main__':
    unittest.main()
