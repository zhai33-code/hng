# -*- coding: utf-8 -*-
"""SquadInvitePlayerRequest 隐式建队回归测试（2026-08-27 "squad not found"）。

背景：客户端从不发 CreateSquadRequest（SquadService.inviteFriend 直接发
SquadInvitePlayerRequest），私服 get_squad_for_player 查不到邀请者的队 →
回 161 squad_not_found 弹错误。修复后：邀请时无队则隐式建队
（ownerCharacterId，兜底最新角色）+ 清残留 solo 隐式小队行。

用例：
 1. 邀请者无队：隐式建队（角色/阵营取 ownerCharacterId）→ result=1
    + SquadSession/SquadMemberSession KVCS 行（队长 InLobby + 好友 pending）
 2. ownerCharacterId 不属于邀请者 → 兜底最新角色建队
 3. 已有真队：走原路径（不新建队，仅加 pending 成员行）
 4. 邀请者有残留 solo 隐式小队 → 注册表清空 + 删行 KVCS 先推
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol import Chunk, pb_varint
import handlers.squad as squad_mod
from handlers.squad import (
    RC_OK, RC_SQUAD_NOT_FOUND, ST_INVITE_PENDING, ST_IN_LOBBY,
    handle_squad_invite_player,
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


class FakeStore:
    """最小 STORE：只实现邀请路径用到的方法。"""

    def __init__(self):
        self.next_id = 100
        self.squads = {}          # squad_id -> row
        self.members = {}         # member_row -> {id, squad_id, character_id, status}
        self.chars = {}           # char_id -> {'id','player_id','name','faction'}
        self.usernames = {}       # player_id -> username

    async def get_squad_for_player(self, pid):
        for s in self.squads.values():
            for m in self.members.values():
                if m['squad_id'] == s['id']:
                    c = self.chars.get(m['character_id'])
                    if c and int(c['player_id']) == int(pid):
                        return s
        return None

    async def get_character(self, pid, char_id):
        c = self.chars.get(int(char_id))
        return c if c and int(c['player_id']) == int(pid) else None

    async def get_latest_character(self, pid):
        owned = sorted((c for c in self.chars.values()
                        if int(c['player_id']) == int(pid)),
                       key=lambda c: c['id'])
        return owned[-1] if owned else None

    async def list_characters(self, pid):
        return sorted((dict(c) for c in self.chars.values()
                       if int(c['player_id']) == int(pid)),
                      key=lambda c: c['id'])

    async def create_squad(self, char_id, faction, name, flag=False):
        sid = self.next_id
        self.next_id += 1
        self.squads[sid] = {'id': sid, 'name': name, 'faction': int(faction),
                            'leader_character_id': int(char_id),
                            'matchmaking_flag': bool(flag)}
        mid = self.next_id
        self.next_id += 1
        self.members[mid] = {'id': mid, 'squad_id': sid,
                             'character_id': int(char_id), 'status': ST_IN_LOBBY}
        return sid

    async def get_squad(self, sid):
        return self.squads.get(int(sid))

    async def add_squad_member(self, sid, char_id, status=1):
        for m in self.members.values():
            if m['squad_id'] == int(sid) and m['character_id'] == int(char_id):
                m['status'] = status
                return m['id']
        mid = self.next_id
        self.next_id += 1
        self.members[mid] = {'id': mid, 'squad_id': int(sid),
                             'character_id': int(char_id), 'status': status}
        return mid

    async def list_squad_members(self, sid):
        out = []
        for m in sorted(self.members.values(), key=lambda x: x['id']):
            if m['squad_id'] != int(sid):
                continue
            c = self.chars.get(m['character_id']) or {}
            out.append({'id': m['id'], 'character_id': m['character_id'],
                        'status': m['status'],
                        'player_id': int(c.get('player_id', 0)),
                        'gamertag': self.usernames.get(
                            int(c.get('player_id', 0)), ''),
                        'template': int(c.get('template', 1)),
                        'combat_xp': int(c.get('combat_xp', 0)),
                        'char_name': c.get('name', ''),
                        'faction': int(c.get('faction', 0))})
        return out

    async def get_squad_member(self, mid):
        return self.members.get(int(mid))

    async def get_username(self, pid):
        return self.usernames.get(int(pid))


class FakeSess:
    def __init__(self, pid, username):
        self.pid = pid
        self.username = username
        self.playerview_cmd = 55
        self.writer = FakeWriter()

    async def ensure_player(self):
        return self.pid


async def _invite(store, sess, owner_char, friend_pid):
    """发一条 SquadInvitePlayerRequest，返回 (result, writer)。"""
    proto = pb_varint(1, owner_char) + pb_varint(2, friend_pid)
    w = FakeWriter()
    old = squad_mod.STORE
    squad_mod.STORE = store
    try:
        await handle_squad_invite_player(sess, w, 9,
                                         'SquadInvitePlayerRequest', proto)
    finally:
        squad_mod.STORE = old
    # 解析响应 result（f1 varint）：KVCS 推送先写、响应在后 → 取最后一个 chunk
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
    result = 0
    if (v & 7) == 0:
        val = shift2 = 0
        while True:
            b = body[i]; i += 1
            val |= (b & 0x7F) << shift2; shift2 += 7
            if not (b & 0x80):
                break
        result = val
    return result, w


async def main():
    # --- 基础数据：A(pid=2, char31 德军/char55 苏军)、B(pid=11, char49 苏军)
    store = FakeStore()
    store.chars[31] = {'id': 31, 'player_id': 2, 'name': 'zhai33GE',
                       'faction': 2, 'template': 1, 'combat_xp': 5000}
    store.chars[55] = {'id': 55, 'player_id': 2, 'name': 'zhai33SU',
                       'faction': 3, 'template': 1, 'combat_xp': 5000}
    store.chars[49] = {'id': 49, 'player_id': 11, 'name': 'summercatOVO',
                       'faction': 3, 'template': 1, 'combat_xp': 8000}
    store.usernames[2] = 'zhai33'
    store.usernames[11] = 'summercatOVO'

    sess_a = FakeSess(2, 'zhai33')

    # 用例 1：无队邀请 → 隐式建队（ownerCharacterId=31 → 德军队）
    result, w = await _invite(store, sess_a, 31, 11)
    check('无队邀请 result=1（修复前 161 squad_not_found）', result == RC_OK,
          f'got {result}')
    squad = await store.get_squad_for_player(2)
    check('隐式建队：邀请者已有 DB 小队', squad is not None)
    if squad is not None:
        check('队阵营=ownerCharacterId 阵营(2)', int(squad['faction']) == 2,
              f'got {squad["faction"]}')
        mems = await store.list_squad_members(squad['id'])
        pend = [m for m in mems if m['status'] == ST_INVITE_PENDING]
        check('好友 pending 成员行已加', len(pend) == 1 and
              pend[0]['player_id'] == 11)
        leader = [m for m in mems if m['player_id'] == 2]
        check('队长 InLobby 行已建', len(leader) == 1 and
              leader[0]['status'] == ST_IN_LOBBY)
    check('邀请者收到 SquadSession 行', w.data.count(b'SquadSession') >= 1)
    check('邀请者收到成员行（队长+pending）',
          w.data.count(b'SquadMemberSession') >= 2)

    # 用例 2：ownerCharacterId 不属于邀请者 → 兜底最新角色（char55 → 苏军）
    store2 = FakeStore()
    store2.chars.update(store.chars)
    store2.usernames.update(store.usernames)
    result2, _ = await _invite(store2, sess_a, 999, 11)
    squad2 = await store2.get_squad_for_player(2)
    check('非法 ownerCharacterId 兜底最新角色仍建队',
          result2 == RC_OK and squad2 is not None and
          int(squad2['faction']) == 3,
          f'result={result2} faction={squad2 and squad2["faction"]}')

    # 用例 3：已有真队 → 不新建，仅加 pending 成员
    before = set(store.squads)
    result3, _ = await _invite(store, sess_a, 31, 11)
    check('已有队：result=1 且不重复建队',
          result3 == RC_OK and set(store.squads) == before)

    # 用例 4：残留 solo 隐式小队 → 注册表清空 + 新真队建立
    from matchmaking import _SOLO_SQUADS
    _SOLO_SQUADS[2] = {'squad_row': 777, 'member_row': 778, 'mission_id': 0,
                       'faction': 2, 'template': 1}
    store3 = FakeStore()
    store3.chars.update(store.chars)
    store3.usernames.update(store.usernames)
    FLASH_SESSIONS.pop('zhai33', None)
    result4, w4 = await _invite(store3, sess_a, 31, 11)
    check('solo 残留：邀请后注册表清空', 2 not in _SOLO_SQUADS)
    check('solo 残留：删行 KVCS 已推（member/squad delete）',
          b'SquadMemberSession' in w4.data and
          b'SquadSession' in w4.data)
    squad3 = await store3.get_squad_for_player(2)
    check('solo 残留：新真队建立', result4 == RC_OK and squad3 is not None)
    _SOLO_SQUADS.pop(2, None)

    # --- 小队匹配 charId=0 解析（EnterMatchmakingCommand 整队分支）
    # 用例 5：小队成员点匹配（客户端发 characterId=0）→ 按成员行解析角色，
    #         JoinMatchMakingResponse 必须是 ok=1（修复前 rejected=0）
    import matchmaking
    store4 = FakeStore()
    store4.chars.update(store.chars)
    store4.usernames.update(store.usernames)
    sid = await store4.create_squad(31, 2, 'Squad of zhai33')
    await store4.add_squad_member(sid, 49, status=ST_IN_LOBBY)  # B 已接受入队
    old_store = matchmaking.STORE
    matchmaking.STORE = store4
    try:
        proto = pb_varint(1, 0) + pb_varint(7, 1)   # charId=0, gamemode=1
        wj = FakeWriter()
        # 成员均不在线（FLASH_SESSIONS 空）→ _enqueue_squad 空成员 no-op，
        # 不触碰 POOL；只验证响应与角色解析
        await matchmaking._handle_join(sess_a, wj, 21, proto)
        mainc, _ = Chunk.parse(wj.data, 0)
        body = mainc.subs[0].subs[0].data
        jres = body[1] if len(body) > 1 else 0    # f1 varint（值 1/0 单字节）
        check('小队匹配 charId=0 → 响应 ok（修复前 rejected）',
              jres == 1, f'got {jres}')
    finally:
        matchmaking.STORE = old_store

    # 用例 6：无任何小队且 charId=0 → 仍拒绝（原兜底不回归）
    store5 = FakeStore()
    store5.chars.update(store.chars)
    store5.usernames.update(store.usernames)
    matchmaking.STORE = store5
    try:
        proto = pb_varint(1, 0) + pb_varint(7, 1)
        wj2 = FakeWriter()
        await matchmaking._handle_join(sess_a, wj2, 22, proto)
        mainc, _ = Chunk.parse(wj2.data, 0)
        body = mainc.subs[0].subs[0].data
        jres2 = body[1] if len(body) > 1 else 0
        check('无小队 charId=0 → 仍拒绝', jres2 == 0, f'got {jres2}')
    finally:
        matchmaking.STORE = old_store

    # 用例 7：混阵营小队 SquadSession.f4 按观察者下发（2026-08-27
    #   匹配界面 AB 都显示苏军：客户端 MatchmakingView 阵营图标绑
    #   SquadSession.f4，官方语义=全队同阵营；建队者阵营(3)直接下发
    #   会让德军成员也显示苏军）
    store6 = FakeStore()
    store6.chars.update(store.chars)
    store6.usernames.update(store.usernames)
    sid6 = await store6.create_squad(49, 3, 'SU squad')       # B(苏军)建队
    await store6.add_squad_member(sid6, 31, status=ST_IN_LOBBY)  # A(德军)入队
    captured = []
    orig_push = squad_mod._push_kvcs

    async def spy_push(writer, view_cmd, table_name, proto_data):
        captured.append((table_name, proto_data))

    def first_squad_f4():
        """捕获到的第一条 SquadSession 的 f4（varint 字段序解析）。"""
        for t, p in captured:
            if t != b'SquadSession':
                continue
            i = 0
            while i < len(p):
                key = p[i]; i += 1
                if key & 7 != 0:
                    break
                v = s = 0
                while True:
                    b = p[i]; i += 1
                    v |= (b & 0x7F) << s; s += 7
                    if not (b & 0x80):
                        break
                if key >> 3 == 4:
                    return v
        return None

    squad_mod._push_kvcs = spy_push
    try:
        old_store6 = squad_mod.STORE
        squad_mod.STORE = store6
        try:
            for pid, expect in ((2, 2), (11, 3)):
                captured.clear()
                w7 = FakeWriter()
                await squad_mod.push_squad_rows_to_writer(w7, 55, sid6, 0,
                                                          viewer_pid=pid)
                f4 = first_squad_f4()
                check(f'混阵营小队：观察者 pid={pid} 收到 f4={expect}',
                      f4 == expect, f'got {f4}')
            captured.clear()
            w8 = FakeWriter()
            await squad_mod.push_squad_rows_to_writer(w8, 55, sid6, 0)
            f4d = first_squad_f4()
            check('不传观察者：f4 回退建队者阵营(3)', f4d == 3,
                  f'got {f4d}')
        finally:
            squad_mod.STORE = old_store6
    finally:
        squad_mod._push_kvcs = orig_push

    passed = sum(1 for _, ok in RESULTS if ok)
    print(f'\n{passed}/{len(RESULTS)} checks passed')
    if passed != len(RESULTS):
        sys.exit(1)


if __name__ == '__main__':
    asyncio.run(main())
