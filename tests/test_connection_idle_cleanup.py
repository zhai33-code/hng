"""Regression: quiet gameplay TCP and concurrent IRC disconnect cleanup."""
import asyncio
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import irc_server
import realtest_server


class ConnectionRegressionTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def game_session(pid=1, authenticated=True, closing=False, is_game=True):
        return types.SimpleNamespace(
            player_id=pid, authenticated=authenticated, is_game_client=is_game,
            writer=types.SimpleNamespace(is_closing=lambda: closing))

    async def test_old_game_disconnect_keeps_new_game_connection(self):
        old = self.game_session()
        new = self.game_session()
        hook = AsyncMock()
        with patch.object(realtest_server, 'TCP_SESSIONS', [old, new]), \
                patch.object(realtest_server.matchmaking, 'on_game_client_closed', hook), \
                patch.object(realtest_server, 'log'):
            await realtest_server._on_game_connection_closed(old)
            hook.assert_not_awaited()

    async def test_last_game_disconnect_still_arms_protection(self):
        departing = self.game_session()
        hook = AsyncMock()
        with patch.object(realtest_server, 'TCP_SESSIONS', []), \
                patch.object(realtest_server.matchmaking, 'on_game_client_closed', hook):
            await realtest_server._on_game_connection_closed(departing)
            hook.assert_awaited_once_with(1)

    async def test_non_live_or_other_player_connections_do_not_mask_drop(self):
        departing = self.game_session()
        cases = [self.game_session(pid=2), self.game_session(authenticated=False),
                 self.game_session(closing=True), self.game_session(is_game=False)]
        for other in cases:
            with self.subTest(connection=vars(other)):
                hook = AsyncMock()
                with patch.object(realtest_server, 'TCP_SESSIONS', [other]), \
                        patch.object(realtest_server.matchmaking, 'on_game_client_closed', hook):
                    await realtest_server._on_game_connection_closed(departing)
                    hook.assert_awaited_once_with(1)

    async def test_lobby_disconnect_does_not_arm_game_protection(self):
        lobby = self.game_session(is_game=False)
        hook = AsyncMock()
        with patch.object(realtest_server.matchmaking, 'on_game_client_closed', hook):
            await realtest_server._on_game_connection_closed(lobby)
            hook.assert_not_awaited()

    async def test_authenticated_game_survives_idle_and_reads_again(self):
        reader = asyncio.StreamReader()
        sess = types.SimpleNamespace(is_game_client=True, authenticated=True)
        with patch.object(realtest_server, 'READ_IDLE_TIMEOUT', 0.02):
            task = asyncio.create_task(realtest_server._read_tcp_data(reader, sess))
            try:
                await asyncio.sleep(0.06)
                self.assertFalse(task.done(), 'quiet gameplay must stay connected')
                reader.feed_data(b'next-request')
                self.assertEqual(await asyncio.wait_for(task, 1), b'next-request')
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_other_connections_keep_idle_limit(self):
        cases = [types.SimpleNamespace(),
                 types.SimpleNamespace(authenticated=False, is_game_client=True)]
        with patch.object(realtest_server, 'READ_IDLE_TIMEOUT', 0.01):
            for sess in cases:
                with self.subTest(session=vars(sess)):
                    with self.assertRaises(asyncio.TimeoutError):
                        await realtest_server._read_tcp_data(asyncio.StreamReader(), sess)

    async def test_authenticated_lobby_survives_idle(self):
        reader = asyncio.StreamReader()
        sess = types.SimpleNamespace(is_game_client=False, authenticated=True)
        with patch.object(realtest_server, 'READ_IDLE_TIMEOUT', 0.02):
            task = asyncio.create_task(realtest_server._read_tcp_data(reader, sess))
            try:
                await asyncio.sleep(0.06)
                self.assertFalse(task.done(), 'quiet lobby must stay connected')
                reader.feed_eof()
                self.assertEqual(await asyncio.wait_for(task, 1), b'')
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_game_eof_and_reset_are_still_detected(self):
        sess = types.SimpleNamespace(is_game_client=True, authenticated=True)
        reader = asyncio.StreamReader()
        reader.feed_eof()
        self.assertEqual(await realtest_server._read_tcp_data(reader, sess), b'')
        reader = asyncio.StreamReader()
        reader.set_exception(ConnectionResetError('peer closed'))
        with self.assertRaises(ConnectionResetError):
            await realtest_server._read_tcp_data(reader, sess)

    async def test_concurrent_irc_disconnect_deletes_channel_once(self):
        first_sending = asyncio.Event()
        second_closed = asyncio.Event()
        channel = types.SimpleNamespace(key='#regression', members=set())
        clients = {}

        class Reader:
            def __init__(self, name):
                self.name = name

            async def read(self, size):
                if self.name == 'second':
                    await first_sending.wait()
                return b''

        class Writer:
            def __init__(self, name):
                self.name = name
                self.closed = False

            def get_extra_info(self, name):
                return ('127.0.0.1', 0)

            def close(self):
                self.closed = True

            async def wait_closed(self):
                if self.name == 'second':
                    second_closed.set()

        class Client:
            def __init__(self, reader, writer):
                self.writer = writer
                self.nick = self.user = writer.name
                self.ip = '127.0.0.1'
                self.registered = True
                self.cleaned = False

            async def notice_auth(self, message):
                pass

            async def notice_uid(self, message):
                pass

            async def send(self, nick, message):
                if self.nick == 'second' and message.startswith('QUIT'):
                    first_sending.set()
                    await second_closed.wait()

            def cleanup(self):
                self.cleaned = True

        def make_client(reader, writer):
            return clients[writer.name]

        writers = [Writer('first'), Writer('second')]
        readers = [Reader('first'), Reader('second')]
        for reader, writer in zip(readers, writers):
            client = Client(reader, writer)
            clients[writer.name] = client
            channel.members.add(client)
        channels = {channel.key: channel}
        with patch.object(irc_server, 'IrcClient', make_client), \
                patch.object(irc_server, 'CHANNELS', channels), \
                patch.object(irc_server, 'log'):
            await asyncio.wait_for(asyncio.gather(*(
                irc_server.handle_conn(r, w) for r, w in zip(readers, writers))), 1)
        self.assertEqual(channels, {})
        self.assertTrue(all(c.cleaned for c in clients.values()))
        self.assertTrue(all(w.closed for w in writers))


if __name__ == '__main__':
    unittest.main()
