"""PMS Entered 只能由真实玩家游戏连接推进的回归测试。"""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import game_server
import matchmaking
from protocol import pb_varint
from state import FLASH_SESSIONS


class FakeWriter:
    def __init__(self):
        self.data = b''

    def write(self, data):
        self.data += bytes(data)

    async def drain(self):
        pass


async def main():
    pms_id = 610099
    mission_id = 910099
    tag = 'EnteredRegression'
    flash_writer = FakeWriter()
    FLASH_SESSIONS[tag] = types.SimpleNamespace(
        writer=flash_writer, playerview_cmd=105)
    matchmaking.PMS_TO_SESSION[pms_id] = {
        'pms_id': pms_id, 'player_id': 700099, 'char_id': 42,
        'gamertag': tag, 'char_name': tag, 'rank': 1, 'faction': 3,
        'metrics_uid': 'uid-entered', 'metrics_session_id': 700100,
        'battle_session_id': 700101, 'squad_id': 700102,
        'squad_slot': 0, 'quickplay': False,
    }
    matchmaking.PMS_TO_MISSION[pms_id] = mission_id
    old_store = game_server.STORE
    old_builder = game_server._msg_player_settings
    game_server.STORE = None
    # 本测试只验证连接状态推进；PlayerSettings 完整载荷由独立测试覆盖。
    game_server._msg_player_settings = lambda **kwargs: b''
    try:
        # 战斗服自身启动握手也会用首名玩家 PMS，不能误判为玩家已进入。
        server_sess = types.SimpleNamespace(is_combat_server=True)
        await game_server.handle_request(
            server_sess, FakeWriter(), 1, 'GetPlayerSettings',
            pb_varint(1, pms_id))
        assert not flash_writer.data
        assert not matchmaking.PMS_TO_SESSION[pms_id].get('pms_entered')

        # 玩家 hng.exe 真正连上后才推 Entered。
        player_sess = types.SimpleNamespace(is_combat_server=False)
        await game_server.handle_request(
            player_sess, FakeWriter(), 2, 'GetPlayerSettings',
            pb_varint(1, pms_id))
        assert matchmaking.PMS_TO_SESSION[pms_id]['pms_entered'] is True
        assert b'PlayerMissionSession' in flash_writer.data
        assert b'\x70\x03' in flash_writer.data  # PMS f14 connectionState=3

        # EAC/普通路径重复请求不得重复推送。
        size = len(flash_writer.data)
        await game_server.handle_request(
            player_sess, FakeWriter(), 3, 'GetPlayerSettings',
            pb_varint(1, pms_id) + pb_varint(2, 1))
        assert len(flash_writer.data) == size
    finally:
        game_server.STORE = old_store
        game_server._msg_player_settings = old_builder
        FLASH_SESSIONS.pop(tag, None)
        matchmaking.PMS_TO_SESSION.pop(pms_id, None)
        matchmaking.PMS_TO_MISSION.pop(pms_id, None)

    print('PMS_ENTERED: ALL PASS (3/3 passed)')


if __name__ == '__main__':
    asyncio.run(main())
