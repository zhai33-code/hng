"""统计链路回归测试（2026-08-27 stats 全量修复）。

覆盖：
 1. _stat_db_key 各段位 ctx 编码（武器 ctx1 / 载具 ctx1 / 模式 ctx2>>16 /
    武×载 ctx1+ctx2 / 标量）—— 模式段 ctx 语义来自 player.dll
    send_stats sub_18038DA00 逆向实锤
 2. _parse_type6 合成载荷解析（含维度统计 + 勋带）
 3. _stats_entries_for_char 反向映射（DB 键 → wire 条目），与
    _stat_db_key 往返一致
 4. _build_totals 字节布局（numStats/numRibbons + 28B 条目）
 5. game_server._group_dim_stats 维度分组（wv 前缀不误入 w 组）
 6. RequestReadCharacterStats 端到端：f1-f48 标量回读 + f50/f51/f53
    vehicleStatistics/weaponStatistics/gamemodeStatistics 数组下发
"""
import asyncio
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import game_server
import statistics_server as ss
from protocol import Chunk, pb_varint

RESULTS = []


def check(label, ok, detail=''):
    RESULTS.append((label, ok))
    print(f'[{"PASS" if ok else "FAIL"}] {label}{("  " + detail) if detail else ""}')


class FakeWriter:
    def __init__(self):
        self.data = b''

    def write(self, data):
        self.data += data

    async def drain(self):
        pass


class FakeStore:
    """get_stats 直接回静态字典（按 char 区分）。"""

    def __init__(self, tables):
        self.tables = tables

    async def get_stats(self, cid):
        return dict(self.tables.get(cid, {}))

    async def list_characters(self, pid):
        return []

    async def get_ribbon_xp(self, pid, cid):
        return {}


def _pb_multi(b):
    """顶层字段 → {fn: [val...]}（varint→int，message→bytes）。"""
    from protocol import pb_parse_multi
    return pb_parse_multi(b)


def test_db_key():
    check('db_key 武器段 ctx1=武器id',
          ss._stat_db_key(517, 12345, 0) == 'w12345/WeaponKills',
          ss._stat_db_key(517, 12345, 0))
    check('db_key 载具段 ctx1=载具id',
          ss._stat_db_key(770, 678, 0) == 'v678/VehicleDestroyed',
          ss._stat_db_key(770, 678, 0))
    check('db_key 模式段 gamemode id 取 ctx2>>16',
          ss._stat_db_key(1282, 0, 4 << 16) == 'gm4/GamemodeCPCaptures',
          ss._stat_db_key(1282, 0, 4 << 16))
    check('db_key 模式段 ctx1 非零不影响 key',
          ss._stat_db_key(1282, 999, 4 << 16) == 'gm4/GamemodeCPCaptures')
    check('db_key 武×载段 ctx1=武器 ctx2低16=载具',
          ss._stat_db_key(1025, 111, 222) == 'wv111_222/WeaponVehicleKill',
          ss._stat_db_key(1025, 111, 222))
    check('db_key 标量段',
          ss._stat_db_key(3, 0, 0) == 'PlayerKills')
    check('db_key 未知 id 落 x 前缀',
          ss._stat_db_key(9999, 1, 2).startswith('x9999/'))


def test_parse_type6():
    payload = struct.pack('<qI', 4242, 1)
    payload += struct.pack('<qI', 31, 3)
    payload += struct.pack('<Iqqq', 517, 12345, 0, 7)          # 武器击杀
    payload += struct.pack('<Iqqq', 770, 678, 0, 2)            # 载具击毁
    payload += struct.pack('<Iqqq', 1282, 0, 4 << 16, 3)       # 模式占领
    payload += struct.pack('<I', 1) + struct.pack('<qdId', 13, 500.0, 3, 900.0)
    sid, chars = ss._parse_type6(payload)
    cid, stats, ribbons = chars[0]
    check('type6 解析 session/char', sid == 4242 and cid == 31)
    check('type6 解析 3 条统计', len(stats) == 3 and stats[0] == (517, 12345, 0, 7),
          f'stats={stats}')
    check('type6 解析勋带', ribbons == [(13, 500.0, 3, 900.0)])
    # 截断报错
    try:
        ss._parse_type6(payload[:-10])
        check('type6 截断报 ValueError', False)
    except ValueError:
        check('type6 截断报 ValueError', True)


async def test_reverse_map():
    old = ss.STORE
    ss.STORE = FakeStore({31: {
        'PlayerKills': 12,
        'w12345/WeaponKills': 7,
        'w12345/WeaponShotsFired': 100,
        'v678/VehicleDestroyed': 2,
        'gm4/GamemodeCPCaptures': 3,
        'wv111_222/WeaponVehicleKill': 1,
        'kills': 99,          # legacy 键：不发（避免双计数）
        'unknown_thing': 5,   # 未知键：不发
    }})
    try:
        entries = await ss._stats_entries_for_char(31)
        got = {(e[0], e[1], e[2]): e[3] for e in entries}
        check('reverse 标量 → (3,0,0)=12', got.get((3, 0, 0)) == 12)
        check('reverse 武器 → (517,12345,0)=7', got.get((517, 12345, 0)) == 7)
        check('reverse 载具 → (770,678,0)=2', got.get((770, 678, 0)) == 2)
        check('reverse 模式 → (1282,0,4<<16)=3',
              got.get((1282, 0, 4 << 16)) == 3)
        check('reverse 武×载 → (1025,111,222)=1', got.get((1025, 111, 222)) == 1)
        check('reverse legacy/未知键不发', len(entries) == 6, f'n={len(entries)}')
        # 往返：entry → _stat_db_key → 原 key
        rt = all(ss._stat_db_key(w, c1, c2) in
                 {'PlayerKills', 'w12345/WeaponKills', 'w12345/WeaponShotsFired',
                  'v678/VehicleDestroyed', 'gm4/GamemodeCPCaptures',
                  'wv111_222/WeaponVehicleKill'}
                 for w, c1, c2, _ in entries)
        check('reverse↔db_key 往返一致', rt)
    finally:
        ss.STORE = old


def test_build_totals():
    data = ss._build_totals(7, [(31, [(517, 12345, 0, 7)], [(13, 500.0, 3, 900.0)])])
    sid, n = struct.unpack_from('<qI', data, 0)
    cid, ns, nr = struct.unpack_from('<qII', data, 12)
    entry = struct.unpack_from('<Iqqq', data, 28)
    rib = struct.unpack_from('<qdId', data, 56)
    check('build_totals 布局',
          sid == 7 and n == 1 and cid == 31 and ns == 1 and nr == 1
          and entry == (517, 12345, 0, 7) and rib == (13, 500.0, 3, 900.0),
          f'len={len(data)}')


def test_group_dim():
    stats = {'w12345/WeaponKills': 1, 'w12345/WeaponShotsFired': 2,
             'wv111_222/WeaponVehicleKill': 3, 'v678/VehicleDestroyed': 4,
             'gm4/GamemodeCPCaptures': 5, 'PlayerKills': 6}
    w = game_server._group_dim_stats(stats, 'w')
    v = game_server._group_dim_stats(stats, 'v')
    gm = game_server._group_dim_stats(stats, 'gm')
    check('group w 组（wv 不误入）',
          w == {12345: {'WeaponKills': 1, 'WeaponShotsFired': 2}}, f'w={w}')
    check('group v 组', v == {678: {'VehicleDestroyed': 4}})
    check('group gm 组', gm == {4: {'GamemodeCPCaptures': 5}})


async def test_read_character_stats():
    old = game_server.STORE
    game_server.STORE = FakeStore({31: {
        'PlayerKills': 12, 'PlayerDeaths': 5, 'PlayerScore': 900,
        'kills': 99,                       # legacy 回落：PlayerKills 优先
        'w12345/WeaponKills': 7, 'w12345/WeaponShotsFired': 100,
        'v678/VehicleDestroyed': 2, 'v678/VehicleLost': 1,
        'gm4/GamemodeCPCaptures': 3,
    }})
    try:
        w = FakeWriter()
        await game_server.handle_request(None, w, 7, 'RequestReadCharacterStats',
                                         pb_varint(1, 31))
        main, _ = Chunk.parse(w.data, 0)
        body = main.subs[0].subs[0].data
        entries = _pb_multi(body)[1]
        ent = _pb_multi(entries[0])
        cs = _pb_multi(ent[2][0])
        check('stats 标量 f2 kills=12（WIRE 优先于 legacy 99）',
              cs.get(2, [0])[0] == 12, f'f2={cs.get(2)}')
        check('stats 标量 f3 deaths=5', cs.get(3, [0])[0] == 5)
        check('stats 标量 f1 score=900', cs.get(1, [0])[0] == 900)
        check('stats f56 key 存在', cs.get(56, [b''])[0] == b'')
        wep = _pb_multi(cs[51][0])
        check('stats f51 WeaponStats weapon=12345 kills=7 shots=100',
              wep.get(1, [0])[0] == 12345 and wep.get(5, [0])[0] == 7
              and wep.get(2, [0])[0] == 100, f'wep={wep}')
        veh = _pb_multi(cs[50][0])
        check('stats f50 VehicleStats vehicle=678 destroyed=2 lost=1',
              veh.get(1, [0])[0] == 678 and veh.get(2, [0])[0] == 2
              and veh.get(3, [0])[0] == 1, f'veh={veh}')
        gms = _pb_multi(cs[53][0])
        check('stats f53 GameModeStats gameMode=4 cPCaptures=3',
              gms.get(1, [0])[0] == 4 and gms.get(2, [0])[0] == 3, f'gm={gms}')
    finally:
        game_server.STORE = old


async def run():
    test_db_key()
    test_parse_type6()
    await test_reverse_map()
    test_build_totals()
    test_group_dim()
    await test_read_character_stats()


def main():
    asyncio.run(run())
    ok = all(r[1] for r in RESULTS)
    print('\nRESULT:', 'PASS' if ok else 'FAIL')
    print(f'{sum(1 for r in RESULTS if r[1])}/{len(RESULTS)} checks passed')
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
