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
 7. （2026-09-01 统计界面修复）Flash 菜单端 _msg_character_stats_entry：
    cstats=None 退化 / 全量回读（legacy 回落、f36 跳过、复合武器 id）/
    f53 gm 合并为 gameMode=0（客户端 setGameModeStats 只接受 0）
 8. （2026-09-01 分数修复）player f36 totalScore：msg_player 编码 +
    _msg_player_for 全角色汇总（PlayerScore 优先、legacy score 回落不叠加）
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
    # 单角色布局：charId → numStats → stats → numRibbons → ribbons
    # （numRibbons 在 stats 之后，客户端 sub_18000ADF0 读取顺序）
    data = ss._build_totals(7, [(31, [(517, 12345, 0, 7)], [(13, 500.0, 3, 900.0)])])
    sid, n = struct.unpack_from('<qI', data, 0)
    cid, ns = struct.unpack_from('<qI', data, 12)
    entry = struct.unpack_from('<Iqqq', data, 24)
    nr, = struct.unpack_from('<I', data, 52)
    rib = struct.unpack_from('<qdId', data, 56)
    check('build_totals 布局（numRibbons 在 stats 后）',
          sid == 7 and n == 1 and cid == 31 and ns == 1 and nr == 1
          and entry == (517, 12345, 0, 7) and rib == (13, 500.0, 3, 900.0),
          f'len={len(data)}')
    # 双角色回归（2026-08-27 双人进战局崩溃）：按客户端读取顺序流式
    # 解析整包，第二个角色头部必须落在正确偏移上，读完恰好到包尾
    two = ss._build_totals(1, [
        (53, [(11, 0, 0, 111), (50, 0, 0, 278)], [(28, 1500.0, 2, 3000.0)]),
        (54, [(93, 37, 0, 100809)], []),
    ])
    off = 0
    sid2, n2 = struct.unpack_from('<qI', two, off); off += 12
    ok = sid2 == 1 and n2 == 2
    for cid2, nst, stats, ribs in (
            (53, 2, [(11, 0, 0, 111), (50, 0, 0, 278)], [(28, 1500.0, 2, 3000.0)]),
            (54, 1, [(93, 37, 0, 100809)], [])):
        c, s = struct.unpack_from('<qI', two, off); off += 12
        ok = ok and c == cid2 and s == nst
        for i in range(nst):
            e = struct.unpack_from('<Iqqq', two, off); off += 28
            ok = ok and e == stats[i]
        r, = struct.unpack_from('<I', two, off); off += 4
        ok = ok and r == len(ribs)
        for i in range(len(ribs)):
            b = struct.unpack_from('<qdId', two, off); off += 28
            ok = ok and b == ribs[i]
    ok = ok and off == len(two)
    check('build_totals 双角色流式解析（顺序读不错位不越界）', ok,
          f'off={off} len={len(two)}')


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
        # 2026-09-01：客户端 setGameModeStats 只接受 gameMode==0，服务端把
        # 所有 gm 组合并成一条 gameMode=0 回发（gm4 → 合并进 0）
        check('stats f53 GameModeStats 合并为 gameMode=0 cPCaptures=3',
              gms.get(1, [0])[0] == 0 and gms.get(2, [0])[0] == 3, f'gm={gms}')
    finally:
        game_server.STORE = old


def test_character_stats_entry_menu():
    """Flash 菜单端统计入口（2026-09-01 统计界面修复）：
    messages.character_stats_payload / _msg_character_stats_entry(cstats)。"""
    import messages
    # 1. cstats=None 退化为旧行为：仅 f35 XP，其余标量 0，无维度数组
    m = messages._msg_character_stats_entry(31, [(13, 2)], 851)
    ent = _pb_multi(m)
    cs = _pb_multi(ent[2][0])
    check('menu 无 cstats 退化：f35 xp=851 / f2 kills=0 / f56 存在',
          cs.get(35, [0])[0] == 851 and cs.get(2, [0])[0] == 0
          and cs.get(56, [b''])[0] == b'', f'f35={cs.get(35)}')
    check('menu 无 cstats 退化：无 f50/f51/f53 数组',
          50 not in cs and 51 not in cs and 53 not in cs)
    check('menu entry 布局：f1 characterId / f3 ribbon',
          ent.get(1, [0])[0] == 31 and _pb_multi(ent[3][0]).get(1, [0])[0] == 13)
    # 2. 全量回读：WIRE 名 + legacy 回落 + f36 跳过
    m = messages._msg_character_stats_entry(31, [], 851, {
        'PlayerScore': 336, 'PlayerKills': 24, 'PlayerDeaths': 3,
        'time_active': 484,            # legacy 回落（PlayerTimePlayed 缺失）
        'player_damage': 1569,         # legacy 回落（f22）
        'w590340/WeaponKills': 4,      # 高 16 位带载具 id 的复合武器键
    })
    cs = _pb_multi(_pb_multi(m)[2][0])
    check('menu 全量：f1/f2/f3 score/kills/deaths',
          cs.get(1, [0])[0] == 336 and cs.get(2, [0])[0] == 24
          and cs.get(3, [0])[0] == 3)
    check('menu 全量：f14 timePlayed 走 legacy time_active=484',
          cs.get(14, [0])[0] == 484, f'f14={cs.get(14)}')
    check('menu 全量：f22 damageDealt 走 legacy player_damage=1569',
          cs.get(22, [0])[0] == 1569)
    check('menu 全量：f36 不存在（proto 无此字段）', 36 not in cs)
    wep = _pb_multi(cs[51][0])
    check('menu 全量：f51 复合武器 id 590340 原样透传 kills=4',
          wep.get(1, [0])[0] == 590340 and wep.get(5, [0])[0] == 4)
    # 3. f53 gm 合并：客户端 setGameModeStats 只接受 gameMode==0，
    #    gm1 组的"击杀占领中敌人"等必须并入 gameMode=0 条目
    m = messages._msg_character_stats_entry(31, [], 0, {
        'gm0/GamemodeCPCaptures': 2, 'gm0/GamemodeCPNotSpotted': 1,
        'gm1/GamemodeKillAnEnemyCapturing': 2,
        'gm1/GamemodeKillAnEnemyWhileCapturing': 10,
    })
    cs = _pb_multi(_pb_multi(m)[2][0])
    gms = _pb_multi(cs[53][0])
    check('menu f53 合并：单条 gameMode=0',
          len(cs[53]) == 1 and gms.get(1, [0])[0] == 0, f'n={len(cs.get(53, []))}')
    check('menu f53 合并：gm0 键保留 cPCaptures=2 cpNotSpotted=1',
          gms.get(2, [0])[0] == 2 and gms.get(9, [0])[0] == 1)
    check('menu f53 合并：gm1 键并入 killAnEnemyCapturing=2 whileCapturing=10',
          gms.get(7, [0])[0] == 2 and gms.get(10, [0])[0] == 10, f'gm={gms}')


def test_msg_player_total_score():
    """player f36 totalScore（2026-09-01 统计页分数修复）：
    统计界面"分数"读 Global.currentPlayer.totalScore（player proto f36）。"""
    from protocol import msg_player
    f = _pb_multi(msg_player('tester', total_score=1122))
    check('player f36 totalScore=1122', f.get(36, [0])[0] == 1122,
          f'f36={f.get(36)}')
    f = _pb_multi(msg_player('tester'))
    check('player f36 缺省为 0', f.get(36, [0])[0] == 0)


async def test_msg_player_for_total_score():
    """_msg_player_for 全角色 score 汇总（PlayerScore 优先、legacy score 回落，
    同角色二者同时存在时不叠加）。"""
    import player_service

    class MenuFakeStore:
        async def get_currency(self, pid):
            return {'credits': 1, 'warfunds': 2, 'gold': 3}

        async def get_tutorials_completed(self, pid):
            return 0

        async def get_player_faction(self, pid):
            return None

        async def list_characters(self, pid):
            return [{'id': 31}, {'id': 53}, {'id': 7}]

        async def get_stats(self, cid):
            return {31: {'PlayerScore': 900, 'score': 999},   # WIRE 优先不叠加
                    53: {'score': 222},                        # legacy 回落
                    7: {},                                     # 无统计 → 0
                    }[cid]

    class FakeSess:
        username = 'tester'

        async def ensure_player(self):
            return 7

    old = player_service.STORE
    player_service.STORE = MenuFakeStore()
    try:
        m = await player_service._msg_player_for(FakeSess())
        f = _pb_multi(m)
        check('_msg_player_for f36 totalScore=900+222=1122（不叠加 legacy）',
              f.get(36, [0])[0] == 1122, f'f36={f.get(36)}')
    finally:
        player_service.STORE = old


async def run():
    test_db_key()
    test_parse_type6()
    await test_reverse_map()
    test_build_totals()
    test_group_dim()
    await test_read_character_stats()
    test_character_stats_entry_menu()
    test_msg_player_total_score()
    await test_msg_player_for_total_score()


def main():
    asyncio.run(run())
    ok = all(r[1] for r in RESULTS)
    print('\nRESULT:', 'PASS' if ok else 'FAIL')
    print(f'{sum(1 for r in RESULTS if r[1])}/{len(RESULTS)} checks passed')
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
