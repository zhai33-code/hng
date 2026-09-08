# -*- coding: utf-8 -*-
"""战争图城市点击 → 战局详情链路回归（2026-09-08）。

客户端有会战城市点击 → GetMissionDetailsRequest{f1=0, f2=battleId}
（HandleWarBattleCommand 恒发 missionId=0）。此前服务端只读 f1 →
恒查不到 → 回 43 → MissionDetailsWindow 秒关（用户报"点不开城市详情"）。

测试注入 state.STORE 验证 f2 分支（参照 test_battlepass.py 注入模式）。
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import state
import mm_handler
from protocol import pb_int32, pb_parse, build_response, pb_message


class FakeWriter:
    def __init__(self):
        self.buf = b''

    def write(self, data):
        self.buf += data

    async def drain(self):
        pass


class FakeSess:
    def __init__(self):
        self.player_id = 42


class FakeBattleStore:
    """get_mission_by_battle / get_mission_record 的最小 mock。"""

    def __init__(self, by_battle=None, by_mid=None):
        self.by_battle = by_battle or {}
        self.by_mid = by_mid or {}

    async def get_mission_by_battle(self, battle_id, states=(0, 1)):
        return self.by_battle.get(int(battle_id))

    async def get_mission_record(self, mid):
        return self.by_mid.get(int(mid))


async def _call(proto):
    """调 _handle_mission_details 返回 (writer, 响应解析结果)。"""
    w = FakeWriter()
    sess = FakeSess()
    await mm_handler._handle_mission_details(sess, w, 900, proto)
    # build_response 的响应体在 frame 里；pb 解析 write 的字节提取 proto
    # （响应 = frame 头 + GetMissionDetailsResponse proto；直接用
    # mm_handler 的回退路径验证：43 = resp proto 第一字段）
    return w


def _is_not_found(writer):
    """响应是否 mission_not_found(43)：pb_int32(1, 43) 编码 = b'\\x08\\x2b'。

    （frame 头干扰 varint 扫描，改用字节序列判定。）"""
    return b'\x08\x2b' in writer.buf


async def main():
    ok = 0
    fail = 0

    def check(name, cond, detail=''):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f'  PASS {name}')
        else:
            fail += 1
            print(f'  FAIL {name} {detail}')

    rec = {'id': 4242, 'name': 'war battle', 'preset': 3, 'state': 1,
           'attacker_faction': 1, 'defender_faction': 2,
           'owner_player_id': 42, 'map_entity_id': 20, 'battle_id': 77}

    # T1: f1=0 + f2=battleId → 按 battle 反查 record → 成功响应（≠43）
    orig_store = state.STORE
    state.STORE = FakeBattleStore(by_battle={77: rec})
    try:
        w = await _call(pb_int32(1, 0) + pb_int32(2, 77))
        check('T1 城市点击链（f1=0,f2=battleId）非 mission_not_found',
              not _is_not_found(w))
    finally:
        state.STORE = orig_store

    # T2: f1=0 + f2=0 + 无匹配 → 43（维持原语义）
    state.STORE = FakeBattleStore()
    try:
        w = await _call(pb_int32(1, 0))
        check('T2 空请求维持 43', _is_not_found(w))
    finally:
        state.STORE = orig_store

    # T3: f1=0 + f2=battleId 但 battle 无 record → 43
    state.STORE = FakeBattleStore()
    try:
        w = await _call(pb_int32(1, 0) + pb_int32(2, 999))
        check('T3 battle 无关联 record → 43', _is_not_found(w))
    finally:
        state.STORE = orig_store

    print(f'\n==== war_battle_details 回归：{ok}/{ok + fail} PASS ====')
    if fail:
        sys.exit(1)


if __name__ == '__main__':
    asyncio.run(main())
