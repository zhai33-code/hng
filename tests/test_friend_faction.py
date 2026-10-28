"""Friend faction comes from the current war selection, including live updates.

Run: python -m unittest discover -s tests -p test_friend_faction.py
The production SELECT is executed against an in-memory SQL fixture; only
PostgreSQL BOOL_OR needs a compatibility aggregate.
"""
import os
import sqlite3
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store.social import SocialGraphMixin
from handlers import social, views
from protocol import Chunk, pb_int32, pb_parse


class BoolOr:
    def __init__(self):
        self.value = False

    def step(self, value):
        self.value = self.value or bool(value)

    def finalize(self):
        return self.value


class SqlConnection:
    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.create_aggregate('BOOL_OR', 1, BoolOr)
        self.db.executescript('''
            CREATE TABLE players (id INTEGER PRIMARY KEY, username TEXT);
            CREATE TABLE friends (player_id INTEGER, friend_id INTEGER, status INTEGER);
            CREATE TABLE characters (id INTEGER, player_id INTEGER, faction INTEGER);
            CREATE TABLE wars (id INTEGER, state INTEGER);
            CREATE TABLE player_war_factions (player_id INTEGER, war_id INTEGER, faction INTEGER);
            INSERT INTO players VALUES (1, 'viewer'), (2, 'friend');
            INSERT INTO friends VALUES (1, 2, 1), (2, 1, 1);
            INSERT INTO characters VALUES (10, 2, 3), (11, 2, 2);
            INSERT INTO wars VALUES (6, 2), (7, 1);
            INSERT INTO player_war_factions VALUES (2, 6, 1), (2, 7, 3);
        ''')

    async def fetch(self, query, *args):
        return self.db.execute(query, args).fetchall()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class FriendStore(SocialGraphMixin):
    def __init__(self, conn):
        self.pool = SimpleNamespace(acquire=lambda: conn)


class Writer:
    def __init__(self):
        self.data = b''

    def write(self, data):
        self.data += data

    async def drain(self):
        pass


def friend_rows(data):
    rows = []
    offset = 0
    while offset < len(data):
        chunk, offset = Chunk.parse(data, offset)
        for title in chunk.subs:
            if title.data == b'KeyValueChangeSet':
                for action in title.subs:
                    for table in action.subs:
                        if table.data == b'friendinfo':
                            rows.extend(pb_parse(row.data) for row in table.subs)
    return rows


class FriendFactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn = SqlConnection()
        self.addCleanup(self.conn.db.close)
        self.store = FriendStore(self.conn)

    async def test_selected_soviet_overrides_latest_german_character(self):
        rows = await self.store.list_friend_rows(1)
        self.assertEqual(rows, [dict(id=2, gamertag='friend', faction=3,
                                    also_friend=True, initiated_by_me=True)])

    async def test_all_factions_and_unselected(self):
        for faction in (1, 2, 3):
            self.conn.db.execute('UPDATE player_war_factions SET faction=? WHERE war_id=7',
                                 (faction,))
            self.assertEqual((await self.store.list_friend_rows(1))[0]['faction'], faction)
        self.conn.db.execute('DELETE FROM player_war_factions WHERE war_id=7')
        self.assertEqual((await self.store.list_friend_rows(1))[0]['faction'], 0)

    async def test_new_war_does_not_inherit_previous_choice(self):
        self.conn.db.executescript('UPDATE wars SET state=2; INSERT INTO wars VALUES (8,1);')
        self.assertEqual((await self.store.list_friend_rows(1))[0]['faction'], 0)

    async def test_push_encodes_selected_faction_online_and_offline(self):
        self.store.get_active_war = AsyncMock(return_value={'id': 7, 'name': 'war'})
        self.store.get_squad_for_player = AsyncMock(return_value=None)
        for online in (False, True):
            writer = Writer()
            with patch.object(social, 'STORE', self.store), patch.object(
                    social, 'FLASH_SESSIONS', {'friend': object()} if online else {}), patch.object(
                    social.matchmaking, '_find_player_live_pms', return_value=(None, None, 0)):
                await social.push_friend_rows(SimpleNamespace(playerview_cmd=3), writer, 1)
            rows = friend_rows(writer.data)
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0][4], rows[0][5], rows[0][8]), (3, 3, 7))

    async def test_join_notifies_friends_only_after_new_selection(self):
        store = SimpleNamespace(get_player_war_faction=AsyncMock(return_value=None),
                                set_player_war_faction=AsyncMock())
        sess = SimpleNamespace(ensure_player=AsyncMock(return_value=2), playerview_cmd=3)
        notify = AsyncMock()
        with patch.object(views, 'STORE', store), patch.object(
                views.war_campaign, 'ensure_war', AsyncMock(return_value={'id': 7})), patch.object(
                views, '_msg_player_for', AsyncMock(return_value=b'')), patch.object(
                views, 'notify_friends_of', notify):
            await views.handle_join_war_request(sess, Writer(), 3, 'join_war_request', pb_int32(2, 3))
            store.set_player_war_faction.assert_awaited_once_with(2, 7, 3)
            notify.assert_awaited_once_with(2)
            notify.reset_mock()
            store.get_player_war_faction.return_value = 3
            for faction in (3, 2, 0):
                await views.handle_join_war_request(sess, Writer(), 3, 'join_war_request',
                                                    pb_int32(2, faction))
            notify.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
