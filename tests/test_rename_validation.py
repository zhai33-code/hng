# -*- coding: utf-8 -*-
"""2026-09-01 改名校验修复（commit 52da0c2）的回归测试（纯单元级，无需服务器/DB）。

  1. _official_name_error 官方规则校验器：
     trim 后 空 → no_name(53)；<3 / >上限 / 控制字符 → invalid_name(36)
     角色名上限 20（NAME_MAX_LENGTH），小队名上限 15（SQUAD_NAME_LENGTH）
  2. 真实报文回放（2026-09-01 00:39 realtest_game.log）：
     - SetSquadDetailsRequest（小队改名，solo 队解散后触发 161 事故的那条）
     - RenameCharacterRequest（士兵改名 '111'，当时零校验直接扣款 15800 银）
  3. handler 级（mock STORE/_charge_player/推送）：
     - 角色/建号：空名→53 且不扣款不落库；短名→36；控制字符→36；
       合法+扣款失败→56；合法+扣款成功→result=1 且 rename_character 落库
     - 小队：无小队→161（线上事故根因）；非队长→164；扣款失败→56；
       队长+扣款成功→result=1 且 rename_squad 落库
"""
import asyncio
import contextlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('HNG_GAME_DIR', r'd:\HnG-test')

from protocol import (                               # noqa: E402
    Chunk,
    build_response,
    pb_int32,
    pb_parse,
    pb_string,
    pb_varint,
    )
from player_service import (                         # noqa: E402
    CHARACTER_NAME_MAX_LENGTH,
    RC_INVALID_NAME,
    RC_NO_NAME,
    SQUAD_NAME_MAX_LENGTH,
    _official_name_error,
    )
from handlers import characters_purchase as chars_mod   # noqa: E402
from handlers import squad as squad_mod              # noqa: E402

PASS = FAIL = 0


def check(label, ok, detail=''):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f'[PASS] {label}{("  " + detail) if detail else ""}')
    else:
        FAIL += 1
        print(f'[FAIL] {label}{("  " + detail) if detail else ""}')


@contextlib.contextmanager
def patched(obj, **overrides):
    """临时替换模块属性（测试后恢复），用于 mock handler 的外部依赖。"""
    saved = {k: getattr(obj, k) for k in overrides}
    for k, v in overrides.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(obj, k, v)


class FakeSess:
    """最小会话桩：handler 只用 ensure_player()/视图 cmd 字段。"""

    def __init__(self, pid=12345):
        self.pid = pid
        self.soldier_view_cmd = None
        self.playerview_cmd = None

    async def ensure_player(self):
        return self.pid


class FakeWriter:
    def __init__(self):
        self.written = []

    def write(self, data):
        self.written.append(bytes(data))

    async def drain(self):
        pass


class FakeCharge:
    """_charge_player 替身：记录调用，返回可控 (cost, ok)。"""

    def __init__(self, ok=True, cost=8400):
        self.calls = []
        self.ok = ok
        self.cost = cost

    async def __call__(self, sess, pricing_id, currency, **kw):
        self.calls.append((pricing_id, currency))
        return self.cost, self.ok


class FakeCharStore:
    def __init__(self):
        self.renames = []
        self.creates = []
        self.chars = []

    async def rename_character(self, pid, cid, name):
        self.renames.append((pid, cid, name))
        return True

    async def create_character(self, *a, **k):
        self.creates.append((a, k))
        return {'id': 9001, 'uniform_variant_id': 0}

    async def list_characters(self, pid):
        return self.chars


class FakeSquadStore:
    def __init__(self, by_char=None, by_player=None):
        self.by_char = by_char or {}
        self.by_player = by_player or {}
        self.renames = []

    async def get_squad_for_character(self, cid):
        return self.by_char.get(cid)

    async def get_squad_for_player(self, pid):
        return self.by_player.get(pid)

    async def rename_squad(self, squad_id, name):
        self.renames.append((squad_id, name))


class FakePush:
    def __init__(self):
        self.calls = []

    async def __call__(self, *a, **k):
        self.calls.append(a)


def last_response(writer):
    """解析 FakeWriter 收到的最后一条响应 → (cmd_id, title, proto_bytes)。"""
    chunk, _ = Chunk.parse(writer.written[-1])
    title_chunk = chunk.subs[0]
    proto = title_chunk.subs[0].data if title_chunk.subs else b''
    return chunk.as_int(), title_chunk.as_str(), proto


def response_code(writer, title):
    cmd_id, t, proto = last_response(writer)
    assert t == title, f'期望 {title}，实际 {t}'
    return int(pb_parse(proto).get(1, 0))


# 2026-09-01 00:39 线上真实报文（realtest_game.log）
HEX_SET_SQUAD_DETAILS = ('280018cb5008073001120fe6ada5e585b5e5b08f'
                         'e9989f3131312000')
HEX_RENAME_CHARACTER = '08072801189f0412033131312000'


def test_official_name_error():
    print('== 1. _official_name_error 官方校验器 ==')
    check('None → no_name(53)', _official_name_error(None) == RC_NO_NAME)
    check("'' → no_name(53)", _official_name_error('') == RC_NO_NAME)
    check("'   '（纯空白）→ no_name(53)",
          _official_name_error('   ') == RC_NO_NAME)
    check("'ab'（<3）→ invalid_name(36)",
          _official_name_error('ab') == RC_INVALID_NAME)
    check("'a' → invalid_name(36)",
          _official_name_error('a') == RC_INVALID_NAME)
    check("'111' → 0（合法，线上真实改名）", _official_name_error('111') == 0)
    check("'步兵小队111' → 0（合法）",
          _official_name_error('步兵小队111') == 0)

    # 角色/武器名上限 20（NAME_MAX_LENGTH）
    check('角色名 20 字符 → 0',
          _official_name_error('a' * CHARACTER_NAME_MAX_LENGTH) == 0)
    check('角色名 21 字符 → 36',
          _official_name_error('a' * (CHARACTER_NAME_MAX_LENGTH + 1))
          == RC_INVALID_NAME)
    # 小队名上限 15（SQUAD_NAME_LENGTH）
    check('小队名 15 字符 → 0',
          _official_name_error('a' * SQUAD_NAME_MAX_LENGTH,
                               max_length=SQUAD_NAME_MAX_LENGTH) == 0)
    check('小队名 16 字符 → 36',
          _official_name_error('a' * (SQUAD_NAME_MAX_LENGTH + 1),
                               max_length=SQUAD_NAME_MAX_LENGTH)
          == RC_INVALID_NAME)
    # trim 后判定：两侧空白不救短名，也不冤枉合法名
    check("'  valid_name  '（trim 后合法）→ 0",
          _official_name_error('  valid_name  ') == 0)
    check("'  ab  '（trim 后仍 <3）→ 36",
          _official_name_error('  ab  ') == RC_INVALID_NAME)
    # 控制字符
    check("'ab\\x01c'（含控制字符）→ 36",
          _official_name_error('ab\x01c') == RC_INVALID_NAME)
    check("'ab\\tc'（trim 不去中间 tab）→ 36",
          _official_name_error('ab\tc') == RC_INVALID_NAME)


def test_real_packet_replay():
    print('== 2. 线上真实报文回放 ==')
    f = pb_parse(bytes.fromhex(HEX_SET_SQUAD_DETAILS))
    name = f.get(2, b'').decode('utf-8')
    check('SetSquadDetails f1 characterId=7', int(f.get(1, 0)) == 7,
          f'f1={f.get(1)}')
    check("SetSquadDetails f2='步兵小队111'", name == '步兵小队111',
          f'f2={name!r}')
    check('SetSquadDetails f3 pricingId=10315（squad_rename）',
          int(f.get(3, 0)) == 10315, f'f3={f.get(3)}')
    check('SetSquadDetails f6 paymentCurrency=1（Credits）',
          int(f.get(6, 0)) == 1, f'f6={f.get(6)}')
    check('该名字过小队校验（3..15）',
          _official_name_error(name, max_length=SQUAD_NAME_MAX_LENGTH) == 0)

    f = pb_parse(bytes.fromhex(HEX_RENAME_CHARACTER))
    name = f.get(2, b'').decode('utf-8')
    check('RenameCharacter f1 characterId=7', int(f.get(1, 0)) == 7)
    check("RenameCharacter f2='111'", name == '111', f'f2={name!r}')
    check('RenameCharacter f3 pricingId=543（character_rename）',
          int(f.get(3, 0)) == 543, f'f3={f.get(3)}')
    check('RenameCharacter f5 paymentCurrency=1（Credits）',
          int(f.get(5, 0)) == 1)
    check("'111' 三字符在修复后属合法名（当时零校验直扣 15800 银）",
          _official_name_error(name) == 0)


async def test_rename_character_handler():
    print('== 3a. handle_renamecharacterrequest（mock） ==')
    CMD = 777

    async def run(proto, charge, store):
        sess, writer = FakeSess(), FakeWriter()
        pushes = FakePush()
        with patched(chars_mod, _charge_player=charge, STORE=store,
                     _repush_character_row=FakePush(),
                     _push_player_refresh=pushes):
            await chars_mod.handle_renamecharacterrequest(
                sess, writer, CMD, 'RenameCharacterRequest', proto)
        return writer, pushes

    # 真实报文改空名 → 53，且不扣款不落库（修复点：校验先于扣款）
    proto = (pb_varint(1, 7) + pb_string(2, '') + pb_varint(3, 543)
             + pb_varint(5, 1))
    charge, store = FakeCharge(), FakeCharStore()
    writer, _ = await run(proto, charge, store)
    check('角色改空名 → result=53 no_name',
          response_code(writer, 'RenameCharacterResponse') == RC_NO_NAME)
    check('角色改空名：未扣款', not charge.calls)
    check('角色改空名：未落库', not store.renames)

    # 纯空白名 → 53
    proto = pb_varint(1, 7) + pb_string(2, '   ') + pb_varint(3, 543)
    writer, _ = await run(proto, FakeCharge(), FakeCharStore())
    check("角色改纯空白名 '   ' → 53",
          response_code(writer, 'RenameCharacterResponse') == RC_NO_NAME)

    # 短名 'ab' → 36，不扣款
    proto = pb_varint(1, 7) + pb_string(2, 'ab') + pb_varint(3, 543)
    charge = FakeCharge()
    writer, _ = await run(proto, charge, FakeCharStore())
    check("角色改短名 'ab' → 36",
          response_code(writer, 'RenameCharacterResponse') == RC_INVALID_NAME)
    check("角色改短名 'ab'：未扣款", not charge.calls)

    # 超长名（21 > 20）→ 36
    proto = pb_varint(1, 7) + pb_string(2, 'a' * 21) + pb_varint(3, 543)
    writer, _ = await run(proto, FakeCharge(), FakeCharStore())
    check('角色改 21 字符超长名 → 36',
          response_code(writer, 'RenameCharacterResponse') == RC_INVALID_NAME)

    # 控制字符 → 36
    proto = pb_varint(1, 7) + pb_string(2, 'ab\x01c') + pb_varint(3, 543)
    writer, _ = await run(proto, FakeCharge(), FakeCharStore())
    check('角色改控制字符名 → 36',
          response_code(writer, 'RenameCharacterResponse') == RC_INVALID_NAME)

    # 合法名但余额不足 → 56，不落库
    proto = (bytes.fromhex(HEX_RENAME_CHARACTER))
    charge, store = FakeCharge(ok=False), FakeCharStore()
    writer, _ = await run(proto, charge, store)
    check("合法名 '111' 扣款失败 → 56",
          response_code(writer, 'RenameCharacterResponse') == 56)
    check('扣款失败：未落库', not store.renames)

    # 合法名 + 扣款成功 → result=1，rename_character(12345, 7, '111')
    charge, store = FakeCharge(), FakeCharStore()
    store.chars = [{'id': 7, 'name': '111', 'faction': 3, 'template': 1,
                    'combat_xp': 0}]
    writer, pushes = await run(proto, charge, store)
    check("合法名 '111' 全流程 → result=1",
          response_code(writer, 'RenameCharacterResponse') == 1)
    check('rename_character 收到 trim 后名字',
          store.renames == [(12345, 7, '111')], f'renames={store.renames}')
    check('扣款参数为线上值 (543, Credits)',
          charge.calls == [(543, 1)], f'charge={charge.calls}')
    check('改名成功后刷新货币面板', len(pushes.calls) == 1)

    # trim：客户端带两端空白的合法名 → 落库用 trim 后的名字
    proto = pb_varint(1, 7) + pb_string(2, '  Ivan  ') + pb_varint(3, 543)
    store = FakeCharStore()
    await run(proto, FakeCharge(), store)
    check("名字 '  Ivan  ' 落库为 'Ivan'",
          store.renames and store.renames[0][2] == 'Ivan',
          f'renames={store.renames}')


async def test_create_character_handler():
    print('== 3b. handle_createcharacterrequest（mock，拒绝路径） ==')
    CMD = 778

    async def run(proto, store):
        sess, writer = FakeSess(), FakeWriter()
        with patched(chars_mod, STORE=store):
            await chars_mod.handle_createcharacterrequest(
                sess, writer, CMD, 'CreateCharacterRequest', proto)
        return writer

    # 建号空名 → 53，不落库（修复前：'Soldier' 兜底照样建号）
    store = FakeCharStore()
    proto = pb_string(1, '') + pb_varint(2, 35)
    writer = await run(proto, store)
    check('建号空名 → result=53 no_name',
          response_code(writer, 'CreateCharacterResponse') == RC_NO_NAME)
    check('建号空名：未落库', not store.creates)

    # 建号纯空白 → 53
    proto = pb_string(1, '   ') + pb_varint(2, 35)
    writer = await run(proto, FakeCharStore())
    check("建号纯空白名 '   ' → 53",
          response_code(writer, 'CreateCharacterResponse') == RC_NO_NAME)

    # 建号短名 → 36
    proto = pb_string(1, 'ab') + pb_varint(2, 35)
    store = FakeCharStore()
    writer = await run(proto, store)
    check("建号短名 'ab' → 36",
          response_code(writer, 'CreateCharacterResponse') == RC_INVALID_NAME)
    check("建号短名 'ab'：未落库", not store.creates)

    # 建号超长名（21）→ 36
    proto = pb_string(1, 'a' * 21) + pb_varint(2, 35)
    writer = await run(proto, FakeCharStore())
    check('建号 21 字符超长名 → 36',
          response_code(writer, 'CreateCharacterResponse') == RC_INVALID_NAME)

    # ---- 收费判定（镜像客户端 isSoldierFree）----

    async def run_charge(proto, store, charge):
        sess, writer = FakeSess(), FakeWriter()
        with patched(chars_mod, STORE=store, _charge_player=charge):
            await chars_mod.handle_createcharacterrequest(
                sess, writer, CMD, 'CreateCharacterRequest', proto)
        return writer

    # 该阵营首个该兵种（rank<1）→ 免费：不扣款直接建号（fake slot / 新手）
    store, charge = FakeCharStore(), FakeCharge()
    proto = pb_string(1, 'IvanPetrov') + pb_varint(2, 35) \
        + pb_varint(3, 543) + pb_varint(5, 1)
    writer = await run_charge(proto, store, charge)
    check('首个该兵种 rank0 → result=1 免费建号',
          response_code(writer, 'CreateCharacterResponse') == 1)
    check('首个该兵种：未扣款', not charge.calls)
    check('首个该兵种：已落库', len(store.creates) == 1)

    # 已拥有同兵种（第二个步兵）→ 扣款路径
    store, charge = FakeCharStore(), FakeCharge()
    store.chars = [{'id': 7, 'faction': 3, 'template': 1}]
    writer = await run_charge(proto, store, charge)
    check('第二个同兵种 → result=1 且扣款 (543, Credits)',
          response_code(writer, 'CreateCharacterResponse') == 1
          and charge.calls == [(543, 1)])
    check('第二个同兵种：已落库', len(store.creates) == 1)

    # 已拥有同兵种 + 余额不足 → 56 不建号
    store, charge = FakeCharStore(), FakeCharge(ok=False)
    store.chars = [{'id': 7, 'faction': 3, 'template': 1}]
    writer = await run_charge(proto, store, charge)
    check('余额不足 → result=56 n_credits',
          response_code(writer, 'CreateCharacterResponse') == 56)
    check('余额不足：未落库', not store.creates)

    # 高军衔包（rank>=1）即使无拥有也扣款
    store, charge = FakeCharStore(), FakeCharge()
    proto_rank = pb_string(1, 'IvanPetrov') + pb_varint(2, 17) \
        + pb_varint(3, 543) + pb_varint(5, 1)
    writer = await run_charge(proto_rank, store, charge)
    check('高军衔包 rank12 → 扣款后建号',
          response_code(writer, 'CreateCharacterResponse') == 1
          and charge.calls == [(543, 1)])
    check('高军衔包：已落库', len(store.creates) == 1)


async def test_set_squad_details_handler():
    print('== 3c. handle_set_squad_details（mock） ==')
    CMD = 779
    SQUAD = {'id': 42, 'leader_character_id': 7}
    NOT_MINE = {'id': 43, 'leader_character_id': 99}

    async def run(proto, store, charge):
        sess, writer = FakeSess(), FakeWriter()
        p1, p2 = FakePush(), FakePush()
        with patched(squad_mod, STORE=store, _charge_player=charge,
                     push_squad_rows=p1, push_squad_rows_to_all_members=p2):
            await squad_mod.handle_set_squad_details(
                sess, writer, CMD, 'SetSquadDetailsRequest', proto)
        return writer, p1, p2

    # 线上事故回放：真报文 + DB 无小队（solo 队已解散）→ 161
    proto = bytes.fromhex(HEX_SET_SQUAD_DETAILS)
    store, charge = FakeSquadStore(), FakeCharge()
    writer, _, _ = await run(proto, store, charge)
    check('无小队（线上事故场景）→ response=161 squad_not_found',
          response_code(writer, 'SetSquadDetailsResponse') == 161)
    check('无小队：未扣款', not charge.calls)
    check('无小队：未改名', not store.renames)

    # 有小队但请求的 characterId 非队长 → 164
    store = FakeSquadStore(by_char={7: NOT_MINE})
    writer, _, _ = await run(proto, store, FakeCharge())
    check('非队长改名 → response=164 squad_not_owned',
          response_code(writer, 'SetSquadDetailsResponse') == 164)

    # 队长但余额不足 → 56
    store = FakeSquadStore(by_char={7: SQUAD})
    charge = FakeCharge(ok=False)
    writer, _, _ = await run(proto, store, charge)
    check('队长扣款失败 → response=56 not_enough_credits',
          response_code(writer, 'SetSquadDetailsResponse') == 56)
    check('扣款失败：未改名', not store.renames)

    # 队长 + 扣款成功 → 1，rename_squad(42, '步兵小队111')，推送全员
    store = FakeSquadStore(by_char={7: SQUAD})
    charge = FakeCharge(cost=8400)
    writer, p1, p2 = await run(proto, store, charge)
    check('队长改名成功 → response=1 ok',
          response_code(writer, 'SetSquadDetailsResponse') == 1)
    check("rename_squad(42, '步兵小队111')",
          store.renames == [(42, '步兵小队111')], f'renames={store.renames}')
    check('扣款参数为线上值 (10315, Credits)',
          charge.calls == [(10315, 1)], f'charge={charge.calls}')
    check('改名后推送 SquadSession 行', p1.calls and p1.calls[0][2] == 42)
    check('改名后同步全体在线成员', p2.calls and p2.calls[0][0] == 42)

    # charId 查不到小队时按玩家兜底查找（修复点：不再只看 player）
    store = FakeSquadStore(by_player={12345: SQUAD})
    writer, _, _ = await run(proto, store, FakeCharge())
    check('按玩家兜底找到小队 → 1',
          response_code(writer, 'SetSquadDetailsResponse') == 1)

    # 空名 / 短名 / 超长：先于小队查找拒绝（53/36）
    for name, expect in (('', 53), ('   ', 53), ('ab', 36), ('a' * 16, 36)):
        proto = (pb_varint(1, 7) + pb_string(2, name) + pb_varint(3, 10315)
                 + pb_varint(6, 1))
        store = FakeSquadStore(by_char={7: SQUAD})
        writer, p1, _ = await run(proto, store, FakeCharge())
        check(f'小队改空名/非法名 {name!r} → {expect}（不查库不扣款）',
              response_code(writer, 'SetSquadDetailsResponse') == expect
              and not store.renames and not p1.calls)


def main():
    test_official_name_error()
    test_real_packet_replay()
    asyncio.run(test_rename_character_handler())
    asyncio.run(test_create_character_handler())
    asyncio.run(test_set_squad_details_handler())
    print(f'\n结果: PASS={PASS} FAIL={FAIL}')
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
