# -*- coding: utf-8 -*-
"""匹配凑队（auto squad）解散回归测试（2026-09-02 "解散后没有退出小队"）。

背景：野队匹配把 2+ 散人合并进临时凑队（matchmaking._MM_MISSION_SQUADS，
仅存内存）后，客户端点"解散小队"发 RemoveSquadRequest：
  - 旧代码只查 solo 注册表与 DB：第一次点击被合并前残留的 solo 条目劫持
    （删早已不存在的旧 solo 行、返回"成功"，凑队行没动 → UI 没退出小队），
    且该 solo 条目被消费掉（战斗结束恢复 solo 视图失效）；
    第二次点击 solo 空、DB 无 → 161 squad not found 弹错。
  - 修复：handle_remove_squad 新增凑队分支（排在 solo 分支前），删本人
    连接上的整队凑队行 + 其他在线成员连接上的本人成员行，返回 OK；
    solo 注册表条目保留（战斗结束恢复用）。

用例：
 1. 凑队成员解散 → response=1，本人连接收到全队成员行 + SquadSession 删除，
    队友连接只收到本人成员行删除，服务端成员表移除本人
 2. 解散后 solo 注册表残留条目未被消费（战斗结束可恢复 solo 视图）★回归
 3. squad_db_id≠0 的真实小队条目不走凑队分支（保持 DB 分支语义）
 4. 纯 solo 隐式小队路径不回归（无凑队时仍走 solo 分支删行 + 清注册表）
"""
import asyncio
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol import Chunk
import matchmaking
import handlers.squad as squad_mod
from handlers.squad import (
    RC_OK, RC_SQUAD_NOT_FOUND, RC_SQUAD_SLOT_NOT_FOUND,
    handle_remove_squad, handle_remove_squad_member,
    handle_set_squad_matchmaking_flag,
    )
from state import FLASH_SESSIONS

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


class FakeSess:
    def __init__(self, pid, username, playerview_cmd=55):
        self.pid = pid
        self.username = username
        self.playerview_cmd = playerview_cmd
        self.writer = FakeWriter()

    async def ensure_player(self):
        return self.pid


def _collect_deletes(w):
    """解析 writer 中全部 KVCS delete：(table_name, row_id) 列表。"""
    out = []
    off = 0
    while off < len(w.data):
        chunk, off2 = Chunk.parse(w.data, off)
        if chunk is None:
            break
        off = off2
        title = chunk.subs[0] if chunk.subs else None
        if title is None or title.as_str() != 'KeyValueChangeSet':
            continue
        action = title.subs[0] if title.subs else None
        if action is None or action.as_str() != 'delete':
            continue
        table = action.subs[0] if action.subs else None
        if table is None or not table.subs:
            continue
        for row in table.subs:
            out.append((table.as_str(),
                        struct.unpack('<q', row.data)[0]))
    return out


def _last_response_code(w):
    """取最后一个 chunk 的 f1 varint（响应码）。"""
    main, off = Chunk.parse(w.data, 0)
    while off < len(w.data):
        nxt, off2 = Chunk.parse(w.data, off)
        if nxt is None:
            break
        main, off = nxt, off2
    body = main.subs[0].subs[0].data
    i = shift = v = 0
    while True:
        b = body[i]; i += 1
        v |= (b & 0x7F) << shift; shift += 7
        if not (b & 0x80):
            break
    if (v & 7) != 0:
        return 0
    val = shift = 0
    while i < len(body):
        b = body[i]; i += 1
        val |= (b & 0x7F) << shift; shift += 7
        if not (b & 0x80):
            break
    return val


async def _remove_squad(sess):
    """发一条 RemoveSquadRequest（proto 同实机抓包：f1=0）。"""
    w = FakeWriter()
    await handle_remove_squad(sess, w, 44, 'RemoveSquadRequest', b'\x08\x00')
    return _last_response_code(w), w


def _seed_auto_squad():
    """凑队：A(pid=2, row 101) + B(pid=11, row 102)，mission 223287。"""
    matchmaking._MM_MISSION_SQUADS.clear()
    matchmaking._SOLO_SQUADS.clear()
    sq_id = 8072128442240809119
    matchmaking._MM_MISSION_SQUADS[223287] = {
        sq_id: {
            'name': 'Infantry Squad 1', 'faction': 1, 'template': 1,
            'squad_db_id': 0,
            'members': [
                {'player_id': 2, 'char_id': 25, 'gamertag': 'DoubleBird',
                 'rank': 5, 'slot': 0, 'row_id': 101},
                {'player_id': 11, 'char_id': 31, 'gamertag': 'BotSU1',
                 'rank': 0, 'slot': 1, 'row_id': 102},
            ],
        },
    }
    # 合并前残留的 solo 注册表条目（战斗结束恢复 solo 视图用）
    matchmaking._SOLO_SQUADS[2] = {
        'squad_row': 8072128442240809115, 'member_row': 9001,
        'template': 1, 'faction': 1, 'mission_id': 223287,
        'gamertag': 'DoubleBird', 'char_id': 25, 'char_name': 'DB',
        'rank': 5,
    }
    return sq_id


async def main():
    # --- 用例 1：凑队成员解散 → OK + 全队行删除 + 队友收到本人行删除 ---
    sq_id = _seed_auto_squad()
    sess_a = FakeSess(2, 'DoubleBird')
    sess_b = FakeSess(11, 'BotSU1', playerview_cmd=77)
    FLASH_SESSIONS['BotSU1'] = sess_b
    old_store = squad_mod.STORE
    squad_mod.STORE = None          # 凑队分支不触 DB；None 兜底防误走 DB 分支
    try:
        code, w = await _remove_squad(sess_a)
        check('凑队解散 response=1（旧代码 161）', code == RC_OK,
              f'got {code}')
        dels = _collect_deletes(w)
        check('本人连接删除整队成员行(101,102) + SquadSession',
              (('SquadMemberSession', 101) in dels
               and ('SquadMemberSession', 102) in dels
               and ('SquadSession', sq_id) in dels),
              f'deletes={dels}')
        b_dels = _collect_deletes(sess_b.writer)
        check('队友连接只删除本人成员行(101)',
              b_dels == [('SquadMemberSession', 101)],
              f'BotSU1 deletes={b_dels}')
        members = (matchmaking._MM_MISSION_SQUADS[223287][sq_id]
                   ['members'])
        check('服务端凑队成员表移除本人',
              [int(m['player_id']) for m in members] == [11],
              f'got {[m.get("player_id") for m in members]}')

        # --- 用例 2：solo 注册表残留条目未被消费 ★核心回归点 ---
        check('solo 注册表条目保留（战斗结束恢复 solo 视图）',
              2 in matchmaking._SOLO_SQUADS
              and matchmaking._SOLO_SQUADS[2]['squad_row']
              == 8072128442240809115,
              '（旧代码第一次解散即被 solo 分支消费掉）')
    finally:
        squad_mod.STORE = old_store
        FLASH_SESSIONS.pop('BotSU1', None)

    # --- 用例 3：squad_db_id≠0 的真小队条目不走凑队分支 ---
    _seed_auto_squad()
    matchmaking._MM_MISSION_SQUADS[223287][
        8072128442240809119]['squad_db_id'] = 123
    matchmaking._SOLO_SQUADS.clear()     # 无 solo 条目 → 只能落到 DB 分支
    sess_a = FakeSess(2, 'DoubleBird')
    code, w = await _remove_squad(sess_a)
    check('真小队条目(squad_db_id≠0)不走凑队分支',
          code == RC_SQUAD_NOT_FOUND and not _collect_deletes(w),
          f'got code={code}（凑队分支误入会返回 1 并推删除行）')

    # --- 用例 4：纯 solo 隐式小队路径不回归 ---
    matchmaking._MM_MISSION_SQUADS.clear()
    matchmaking._SOLO_SQUADS.clear()
    matchmaking._SOLO_SQUADS[2] = {
        'squad_row': 7001, 'member_row': 7002, 'template': 1, 'faction': 1,
        'mission_id': 0, 'gamertag': 'DoubleBird', 'char_id': 25,
        'char_name': 'DB', 'rank': 5,
    }
    sess_a = FakeSess(2, 'DoubleBird')
    code, w = await _remove_squad(sess_a)
    dels = _collect_deletes(w)
    check('solo 路径：response=1 + 删 solo 两行 + 注册表清空',
          code == RC_OK
          and ('SquadSession', 7001) in dels
          and ('SquadMemberSession', 7002) in dels
          and 2 not in matchmaking._SOLO_SQUADS,
          f'code={code} deletes={dels}')

    # --- 用例 5：凑队成员离队（RemoveSquadMember）→ OK + 双端删行 ---
    _seed_auto_squad()
    sess_a = FakeSess(2, 'DoubleBird')
    sess_b = FakeSess(11, 'BotSU1', playerview_cmd=77)
    FLASH_SESSIONS['DoubleBird'] = sess_a
    FLASH_SESSIONS['BotSU1'] = sess_b
    squad_mod.STORE = None
    try:
        # BotSU1（成员）离队：member_row=102（varint 102 = 0x66）
        w = FakeWriter()
        await handle_remove_squad_member(sess_b, w, 45,
                                         'RemoveSquadMemberRequest', b'\x08\x66')
        code = _last_response_code(w)
        dels = _collect_deletes(w)
        a_dels = _collect_deletes(sess_a.writer)
        check('成员离队 response=1（旧代码 158）', code == RC_OK,
              f'got {code}')
        check('成员离队：本人+队友连接都只删成员行(102)',
              dels == [('SquadMemberSession', 102)]
              and a_dels == [('SquadMemberSession', 102)],
              f'B={dels} A={a_dels}')
        members = (matchmaking._MM_MISSION_SQUADS[223287]
                   [8072128442240809119]['members'])
        check('成员离队后服务端只剩本人',
              [int(m['player_id']) for m in members] == [2],
              f'got {[m.get("player_id") for m in members]}')

        # --- 用例 6：越权者删凑队成员行 → 不放行，返回 158 ---
        _seed_auto_squad()
        outsider = FakeSess(999, 'Outsider')
        w = FakeWriter()
        await handle_remove_squad_member(outsider, w, 46,
                                         'RemoveSquadMemberRequest', b'\x08\x66')
        check('越权删凑队成员 → 158 不放行',
              _last_response_code(w) == RC_SQUAD_SLOT_NOT_FOUND
              and len(_seed_members()) == 2,
              f'code={_last_response_code(w)}')

        # --- 用例 7：内存小队 SetSquadMatchMakingFlag → 静默 OK ---
        _seed_auto_squad()
        w = FakeWriter()
        sq_varint = _varint(8072128442240809119)
        await handle_set_squad_matchmaking_flag(
            sess_a, w, 47, 'SetSquadMatchMakingFlagRequest',
            b'\x08' + sq_varint + b'\x18\x01')
        check('凑队 id SetSquadMatchMakingFlag → 1（旧代码 161+DB 溢出）',
              _last_response_code(w) == RC_OK,
              f'got {_last_response_code(w)}')
        # solo 小队 id 同样静默 OK
        w = FakeWriter()
        solo_varint = _varint(8072128442240809115)
        await handle_set_squad_matchmaking_flag(
            sess_a, w, 48, 'SetSquadMatchMakingFlagRequest',
            b'\x08' + solo_varint + b'\x18\x00')
        check('solo 小队 id SetSquadMatchMakingFlag → 1',
              _last_response_code(w) == RC_OK,
              f'got {_last_response_code(w)}')
    finally:
        squad_mod.STORE = old_store
        FLASH_SESSIONS.pop('DoubleBird', None)
        FLASH_SESSIONS.pop('BotSU1', None)
        matchmaking._MM_MISSION_SQUADS.clear()
        matchmaking._SOLO_SQUADS.clear()

    ok = all(r[1] for r in RESULTS)
    print()
    print('RESULT:', 'PASS' if ok else 'FAIL',
          f'({sum(1 for r in RESULTS if r[1])}/{len(RESULTS)} checks passed)')
    sys.exit(0 if ok else 1)


def _varint(v):
    out = b''
    while True:
        b7 = v & 0x7F
        v >>= 7
        if v:
            out += bytes([b7 | 0x80])
        else:
            out += bytes([b7])
            return out


def _seed_members():
    return (matchmaking._MM_MISSION_SQUADS.get(223287, {})
            .get(8072128442240809119, {})
            .get('members') or [])


if __name__ == '__main__':
    asyncio.run(main())
