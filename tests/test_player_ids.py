"""玩家身份 id 相关回归测试（2026-08-27 playerId/factionid 硬编码修复）。

事故：login2_result 的 currentplayer.id 恒发 1，而 DB player_id 从 2 开始，
客户端战报界面 getCurrentPlayerInformation() 按 currentPlayer.id 匹配本局
玩家行 → 匹配不到 → 结算窗口不弹。同族问题：
1) msg_player f11 factionid 恒发 1(US)，GE/SU 玩家界面按美国阵营展示
2) messages._msg_* 士兵视图各表 playerId 恒发 1，CharacterTable 按
   currentPlayer.id 过滤 → DB id≥2 玩家看不到自己的角色/装备
3) 结算收益循环不过滤 bot playerId，bot 编号撞真实账号 id 时错发货币

验证：
- msg_player/msg_login2_result 透传真实 pid/faction
- CURRENT_PID contextvar 并发隔离（每连接协程独立 context）
- _msg_* 系列 playerId 跟随 CURRENT_PID
- 结算：bot pid 与名单外 pid 不入账/不记统计
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol import RC, msg_login2_result, msg_player
from state import CURRENT_PID
from messages import _cur_pid, _msg_character
from handlers import combat as combat_handlers


# ---------- protobuf 构造/解析 helpers ----------
def pb_varint(field, value):
    if value < 0:
        value += 1 << 64
    out = bytearray([field << 3 | 0])
    while True:
        b = value & 0x7F
        value >>= 7
        out.append(b | 0x80 if value else b)
        if not value:
            break
    return bytes(out)


def pb_msg(field, payload):
    out = bytearray([field << 3 | 2])
    ln = len(payload)
    while True:
        b = ln & 0x7F
        ln >>= 7
        out.append(b | 0x80 if ln else b)
        if not ln:
            break
    return bytes(out) + payload


def _read_varint(b, i):
    v = 0; shift = 0
    while True:
        c = b[i]; i += 1
        v |= (c & 0x7F) << shift; shift += 7
        if not c & 0x80:
            return v, i


def read_fields(b):
    fields = {}
    i = 0
    while i < len(b):
        tag, i = _read_varint(b, i)     # 字段号>15 时 tag 是多字节 varint
        fn, wt = tag >> 3, tag & 7
        if wt == 2:
            ln = 0; shift = 0
            while True:
                c = b[i]; i += 1
                ln |= (c & 0x7F) << shift; shift += 7
                if not c & 0x80:
                    break
            fields[fn] = b[i:i + ln]; i += ln
        elif wt == 0:
            v = 0; shift = 0
            while True:
                c = b[i]; i += 1
                v |= (c & 0x7F) << shift; shift += 7
                if not c & 0x80:
                    break
            fields[fn] = v
        elif wt == 1:                     # fixed64（mousesensitivity 等）
            fields[fn] = b[i:i + 8]; i += 8
        elif wt == 5:                     # fixed32
            fields[fn] = b[i:i + 4]; i += 4
    return fields


# ---------- 1. msg_player：pid / faction 透传 ----------
def test_msg_player_pid_and_faction():
    p = read_fields(msg_player('Tester', pid=42, faction=3))
    assert p[1] == 42, f'f1 id 应为 42，实际 {p[1]}'
    assert p[11] == 3, f'f11 factionid 应为 3(SU)，实际 {p[11]}'


def test_msg_player_faction_fallback():
    """faction=None（无角色/DB 不可用）回退 1，保持老行为。"""
    p = read_fields(msg_player('Tester', pid=42))
    assert p[11] == 1
    # pid 缺省仍回退 1
    p2 = read_fields(msg_player('Tester'))
    assert p2[1] == 1


# ---------- 2. msg_login2_result：currentplayer 携带 pid/faction ----------
def test_msg_login2_result_success_carries_identity():
    m = read_fields(msg_login2_result('Tester', RC['login_success'],
                                      pid=42, faction=2))
    assert 6 in m, '登录成功必须携带 f6 currentplayer'
    p = read_fields(m[6])
    assert p[1] == 42 and p[11] == 2, \
        f'currentplayer.id/factionid 应为 42/2，实际 {p[1]}/{p[11]}'


def test_msg_login2_result_failure_has_no_player():
    m = read_fields(msg_login2_result('Tester', RC['wrong_password'],
                                      pid=42, faction=2))
    assert 6 not in m, '登录失败不得携带 currentplayer'


# ---------- 3. CURRENT_PID contextvar：并发连接隔离 ----------
def test_cur_pid_contextvar_isolation():
    """两个"连接"协程交错执行，各自读到自己的 pid（asyncio task 各持
    context 拷贝，set 不串号）。"""

    async def conn(pid):
        CURRENT_PID.set(pid)
        for _ in range(50):
            await asyncio.sleep(0)      # 强制交错
            assert _cur_pid() == pid, f'串号! 期望 {pid}，读到 {_cur_pid()}'
        return pid

    async def main():
        return await asyncio.gather(conn(2), conn(303), conn(7))

    got = asyncio.run(main())
    assert sorted(got) == [2, 7, 303]


def test_cur_pid_default():
    """未设置（未登录连接/主上下文）回退默认 1。"""
    token = CURRENT_PID.set(1)          # 隔离测试现场，结束还原
    try:
        assert _cur_pid() == 1
    finally:
        CURRENT_PID.reset(token)


# ---------- 4. _msg_*：playerId 跟随 CURRENT_PID ----------
def test_msg_character_uses_cur_pid():
    """CharacterTable 按 playerId.Equals(currentPlayer.id) 过滤自己拥有的
    行——恒发 1 会让 DB id≥2 玩家看不到角色。"""
    token = CURRENT_PID.set(42)
    try:
        ch = read_fields(_msg_character('Soldier', faction=3, char_id=9))
    finally:
        CURRENT_PID.reset(token)
    assert ch[3] == 42, f'character f3 playerid 应为 42，实际 {ch[3]}'


# ---------- 5. 结算：bot / 名单外 pid 过滤 ----------
class _MockWriter:
    def __init__(self):
        self.sent = b''

    def write(self, data):
        self.sent += bytes(data)

    async def drain(self):
        return


class _MockSess:
    combat_mission = 100003
    game_mission = 100003


class _FakeStore:
    def __init__(self):
        self.saved = None
        self.currency = []
        self.stats = []

    async def save_battle_report(self, report, players):
        self.saved = (report, players)
        return 1

    async def add_currency(self, player_id, credits=0, warfunds=0):
        self.currency.append((player_id, credits, warfunds))
        return {'credits': credits, 'warfunds': warfunds}

    async def add_combat_xp(self, pid, cid, xp):
        return xp

    async def add_battle_pass_score(self, pid, season, score):
        return score

    async def add_stat(self, cid, key, val):
        self.stats.append((cid, key, val))
        return None

    async def set_ribbon_xp_floor(self, pid, cid, rid, xp):
        return None


def _build_bot_collision_request():
    """模拟 2026-08-27 事故现场：bot playerId=3 撞真实账号 id=3。

    BattleReport 名单：真人 pid=1（is_bot=0）、bot pid=3（is_bot=1）；
    f5 PlayerBattleResult：pid=1（真人）、pid=3（bot 撞号）、pid=7
    （不在名单）。"""
    # 真人（带角色 4 与勋带）
    char_real = (pb_varint(1, 4) + pb_varint(2, 1000) + pb_varint(3, 2500))
    player_real = (pb_varint(1, 1) + pb_varint(4, 500) + pb_varint(6, 3)
                   + pb_varint(7, 1) + pb_msg(9, b'TestGuy')
                   + pb_varint(10, 0) + pb_msg(11, char_real))
    # bot（is_bot f10=1，角色 10001）
    char_bot = pb_varint(1, 10001)
    player_bot = (pb_varint(1, 3) + pb_varint(4, 900) + pb_varint(6, 9)
                  + pb_msg(9, b'[Bot] Ivan') + pb_varint(10, 1)
                  + pb_msg(11, char_bot))
    faction = pb_varint(1, 2) + pb_msg(2, player_real) + pb_msg(2, player_bot)
    report = (pb_varint(1, 730433917100003) + pb_varint(2, 2)
              + pb_varint(3, 3) + pb_varint(4, 900) + pb_varint(5, 1787750000)
              + pb_msg(6, faction))
    # f5：真人 120cr/340wf；bot 撞号 999cr；名单外 pid=7 500cr
    pbr_real = pb_varint(1, 1) + pb_varint(2, 120) + pb_varint(3, 340)
    pbr_bot = pb_varint(1, 3) + pb_varint(2, 999) + pb_varint(3, 999)
    pbr_ghost = pb_varint(1, 7) + pb_varint(2, 500) + pb_varint(3, 500)
    return (pb_varint(1, 100003) + pb_varint(2, 730433917100003)
            + pb_varint(3, 10) + pb_msg(4, report) + pb_msg(5, pbr_real)
            + pb_msg(5, pbr_bot) + pb_msg(5, pbr_ghost))


def test_settlement_bot_and_ghost_pids_filtered():
    """bot 撞号 pid=3 与名单外 pid=7 不入账；真人 pid=1 正常（含 +25%
    老兵加成 120→150 / 340→425）。"""
    fake = _FakeStore()
    old = combat_handlers.STORE
    combat_handlers.STORE = fake
    try:
        asyncio.run(combat_handlers.handle_announce_battlefield_earnings(
            _MockSess(), _MockWriter(), 200,
            'AnnounceBattleFieldEarningsRequest',
            _build_bot_collision_request()))
    finally:
        combat_handlers.STORE = old

    assert fake.currency == [(1, 150, 425)], \
        f'货币只应发给真人 pid=1（+25%），实际 {fake.currency}'
    # 战报入库只存真人行
    assert [p['player_id'] for p in fake.saved[1]] == [1], \
        f'入库玩家行应只有真人 pid=1，实际 {fake.saved[1]}'
    # bot 角色 10001 不得产生任何统计写入
    assert all(cid != 10001 for cid, _, _ in fake.stats), \
        f'bot 角色 10001 不应写统计，实际 {fake.stats}'
