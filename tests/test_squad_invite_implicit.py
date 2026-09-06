# -*- coding: utf-8 -*-
"""SquadInvitePlayerRequest 隐式建队回归测试（2026-08-27 "squad not found"）。

背景：客户端从不发 CreateSquadRequest（SquadService.inviteFriend 直接发
SquadInvitePlayerRequest），私服 get_squad_for_player 查不到邀请者的队 →
回 161 squad_not_found 弹错误。修复后：邀请时无队则隐式建队
（ownerCharacterId，兜底最新角色）+ 清残留 solo 隐式小队行。

2026-08-28 官服入队规则（混阵营组队 → 客户端 SquadModel #1009 崩溃）：
A 邀请 B 时 B 必须换到与 A 同阵营+同兵种的角色 —— B 当前选中士兵匹配则
直接入队，否则取 B 该阵营该兵种第一个角色；无匹配 → 17 拒绝。

用例：
 1. 邀请者无队：隐式建队（角色/阵营取 ownerCharacterId）→ result=1
    + SquadSession/SquadMemberSession KVCS 行（队长 InLobby + 好友 pending）
 2. ownerCharacterId 不属于邀请者 → 兜底最新角色建队
 3. 已有真队：走原路径（不新建队，仅加 pending 成员行）
 4. 邀请者有残留 solo 隐式小队 → 注册表清空 + 删行 KVCS 先推
 R1. B 当前选中士兵同阵营同兵种 → 直接入队（不换角）
 R2. B 选中士兵不同阵营 → 取该阵营该兵种第一个角色入队
 R3. B 选中同阵营不同兵种 → 取该阵营该兵种第一个角色入队
 R4. B 无对应阵营+兵种角色 → 17 拒绝且不隐式建队
 R5. B 已在别的真小队 → 157 player_already_in_squad
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol import Chunk, pb_varint
import handlers.squad as squad_mod
from handlers.squad import (
    RC_OK, RC_CHARACTER_NOT_OWNED, RC_PLAYER_ALREADY_IN_SQUAD,
    RC_SQUAD_NOT_FOUND, RC_SQUAD_SLOT_NOT_FOUND,
    ST_INVITE_PENDING, ST_IN_LOBBY,
    handle_squad_invite_player,
    )
from state import FLASH_SESSIONS, PLAYER_SELECTED_CHAR

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
        self.selected_chars = {}  # player_id -> char_id（持久化选中）

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

    async def update_squad_member(self, mid, character_id=None, status=None):
        m = self.members.get(int(mid))
        if m is None:
            return
        if character_id is not None:
            m['character_id'] = int(character_id)
        if status is not None:
            m['status'] = int(status)

    async def update_squad_leader(self, sid, leader_char_id, faction):
        s = self.squads.get(int(sid))
        if s is None:
            return
        s['leader_character_id'] = int(leader_char_id)
        s['faction'] = int(faction)

    async def remove_squad_member(self, mid):
        return self.members.pop(int(mid), None) is not None

    async def set_player_selected_char(self, pid, char_id):
        self.selected_chars[int(pid)] = int(char_id)

    async def get_player_selected_char(self, pid):
        return self.selected_chars.get(int(pid), 0)


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
    # --- 基础数据：A(pid=2)：char31 德军步兵/char55 苏军步兵/char17 德军侦察
    #     （template 1=步兵 6=侦察，wardb 真实映射）
    #     B(pid=11)：char49 苏军步兵/char12 德军步兵/char70 美军步兵
    #                /char14 德军侦察
    store = FakeStore()
    store.chars[31] = {'id': 31, 'player_id': 2, 'name': 'zhai33GE',
                       'faction': 2, 'template': 1, 'combat_xp': 5000}
    store.chars[55] = {'id': 55, 'player_id': 2, 'name': 'zhai33SU',
                       'faction': 3, 'template': 1, 'combat_xp': 5000}
    store.chars[17] = {'id': 17, 'player_id': 2, 'name': 'zhai33GERecon',
                       'faction': 2, 'template': 6, 'combat_xp': 5000}
    store.chars[49] = {'id': 49, 'player_id': 11, 'name': 'summercatOVO',
                       'faction': 3, 'template': 1, 'combat_xp': 8000}
    store.chars[12] = {'id': 12, 'player_id': 11, 'name': 'qqGE',
                       'faction': 2, 'template': 1, 'combat_xp': 1000}
    store.chars[70] = {'id': 70, 'player_id': 11, 'name': 'qqUS',
                       'faction': 1, 'template': 1, 'combat_xp': 2000}
    store.chars[14] = {'id': 14, 'player_id': 11, 'name': 'qqGERecon',
                       'faction': 2, 'template': 6, 'combat_xp': 900}
    store.usernames[2] = 'zhai33'
    store.usernames[11] = 'summercatOVO'

    sess_a = FakeSess(2, 'zhai33')

    def _fresh_store(drop_chars=()):
        """干净副本（基础角色，可剔除指定 char）。"""
        st = FakeStore()
        st.chars.update(store.chars)
        st.usernames.update(store.usernames)
        for cid in drop_chars:
            st.chars.pop(cid, None)
        return st

    async def _pending_char(st, squad):
        for m in await st.list_squad_members(squad['id']):
            if m['status'] == ST_INVITE_PENDING:
                return int(m['character_id'])
        return 0

    # 用例 1：无队邀请 → 隐式建队（ownerCharacterId=31 → 德军队）
    PLAYER_SELECTED_CHAR.clear()      # 隔离模块级"当前选中"注册表
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
        check('官服规则：B 无选中记录 → 德军步兵第一个(char12)',
              await _pending_char(store, squad) == 12,
              f'got {await _pending_char(store, squad)}')
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

    # --- 官服入队规则（2026-08-28 混阵营组队 #1009 修复）
    # 用例 R1：B 当前选中士兵（SU 步兵 49）同阵营同兵种 → 直接入队
    PLAYER_SELECTED_CHAR.clear()
    PLAYER_SELECTED_CHAR[11] = 49
    st_r1 = _fresh_store()
    result_r1, _ = await _invite(st_r1, sess_a, 55, 11)
    sq_r1 = await st_r1.get_squad_for_player(2)
    check('R1 选中匹配 → result=1 建苏军队',
          result_r1 == RC_OK and sq_r1 is not None and
          int(sq_r1['faction']) == 3,
          f'result={result_r1} faction={sq_r1 and sq_r1["faction"]}')
    check('R1 选中匹配 → 直接入队不换角（pending char=49）',
          sq_r1 is not None and await _pending_char(st_r1, sq_r1) == 49,
          f'got {sq_r1 and await _pending_char(st_r1, sq_r1)}')

    # 用例 R2：B 选中是美军步兵（阵营不同）→ 取 B 苏军步兵第一个 = 49
    PLAYER_SELECTED_CHAR.clear()
    PLAYER_SELECTED_CHAR[11] = 70
    st_r2 = _fresh_store()
    result_r2, _ = await _invite(st_r2, sess_a, 55, 11)
    sq_r2 = await st_r2.get_squad_for_player(2)
    check('R2 选中不同阵营 → 取该阵营该兵种第一个（pending char=49）',
          result_r2 == RC_OK and sq_r2 is not None and
          await _pending_char(st_r2, sq_r2) == 49,
          f'result={result_r2} char='
          f'{sq_r2 and await _pending_char(st_r2, sq_r2)}')

    # 用例 R3：B 选中德军步兵，A 用德军侦察(char17)邀请
    #          → 取 B 德军侦察第一个 = char14
    PLAYER_SELECTED_CHAR.clear()
    PLAYER_SELECTED_CHAR[11] = 12
    st_r3 = _fresh_store()
    result_r3, _ = await _invite(st_r3, sess_a, 17, 11)
    sq_r3 = await st_r3.get_squad_for_player(2)
    check('R3 同阵营不同兵种 → 取该兵种第一个（pending char=14）',
          result_r3 == RC_OK and sq_r3 is not None and
          await _pending_char(st_r3, sq_r3) == 14,
          f'result={result_r3} char='
          f'{sq_r3 and await _pending_char(st_r3, sq_r3)}')

    # 用例 R4：A 德军侦察邀请、B 无德军侦察角色 → 17 拒绝且不隐式建队
    PLAYER_SELECTED_CHAR.clear()
    st_r4 = _fresh_store(drop_chars=(14,))
    result_r4, _ = await _invite(st_r4, sess_a, 17, 11)
    check('R4 无对应阵营+兵种 → 17 拒绝',
          result_r4 == RC_CHARACTER_NOT_OWNED, f'got {result_r4}')
    check('R4 拒绝后不隐式建队', not st_r4.squads,
          f'got {list(st_r4.squads)}')

    # 用例 R5：B 已在别的真小队 → 157 player_already_in_squad
    PLAYER_SELECTED_CHAR.clear()
    st_r5 = _fresh_store()
    await st_r5.create_squad(49, 3, 'other squad')   # B 已建队
    result_r5, _ = await _invite(st_r5, sess_a, 55, 11)
    check('R5 B 已在别的队 → 157', result_r5 == RC_PLAYER_ALREADY_IN_SQUAD,
          f'got {result_r5}')
    PLAYER_SELECTED_CHAR.clear()

    # --- 满员上限 + 队长换兵种（2026-08-28 官服对齐四项）
    async def _set_member(store, sess, slot_id, char_id, status=0):
        """发一条 SetSquadMemberStatusRequest，返回响应 writer。"""
        from handlers.squad import handle_set_squad_member_status
        proto = (pb_varint(1, slot_id) + pb_varint(2, char_id) +
                 pb_varint(3, status))
        w = FakeWriter()
        old = squad_mod.STORE
        squad_mod.STORE = store
        try:
            await handle_set_squad_member_status(
                sess, w, 200, 'SetSquadMemberStatusRequest', proto)
        finally:
            squad_mod.STORE = old
        for cid in (200, sess.playerview_cmd):
            pass
        return w

    # 用例 R6：满员（4/4）再邀请 → 158 拒绝
    PLAYER_SELECTED_CHAR.clear()
    st_r6 = _fresh_store()
    st_r6.next_id = 500           # 隔离 id 空间
    st_r6.chars[77] = {'id': 77, 'player_id': 12, 'name': 'p12',
                       'faction': 2, 'template': 1, 'combat_xp': 100}
    st_r6.chars[78] = {'id': 78, 'player_id': 13, 'name': 'p13',
                       'faction': 2, 'template': 1, 'combat_xp': 100}
    st_r6.usernames[12] = 'p12'
    st_r6.usernames[13] = 'p13'
    sid_r6 = await st_r6.create_squad(31, 2, 'full squad')   # 队长 1/4
    for cid in (12, 77, 78):      # 3 名不同玩家 → 4/4 满
        await st_r6.add_squad_member(sid_r6, cid, status=ST_IN_LOBBY)
    result_r6, _ = await _invite(st_r6, sess_a, 31, 11)
    check('R6 满员 4/4 → 158 拒绝', result_r6 == RC_SQUAD_SLOT_NOT_FOUND,
          f'got {result_r6}')

    # 用例 R7：队长换兵种（步兵→侦察 char17）→ 小队跟随 + 成员自动换角
    #   队 A：char31 步兵（队长）+ B char12 步兵 → 队长换 char17 侦察
    #   B 有侦察角色 char14 → 自动换角；无匹配成员（加一个只有步兵的
    #   pid=12 char77）→ 请离
    PLAYER_SELECTED_CHAR.clear()
    st_r7 = _fresh_store()
    st_r7.next_id = 600
    st_r7.chars[77] = {'id': 77, 'player_id': 12, 'name': 'infOnly',
                       'faction': 2, 'template': 1, 'combat_xp': 100}
    st_r7.usernames[12] = 'infonly'
    sid_r7 = await st_r7.create_squad(31, 2, 'r7 squad')
    m_b = await st_r7.add_squad_member(sid_r7, 12, status=ST_IN_LOBBY)
    m_c = await st_r7.add_squad_member(sid_r7, 77, status=ST_IN_LOBBY)
    leader_row = None
    for m in (await st_r7.list_squad_members(sid_r7)):
        if m['character_id'] == 31:
            leader_row = m['id']
    w_r7 = await _set_member(st_r7, sess_a, leader_row, 17, 0)
    sq_r7 = st_r7.squads[sid_r7]
    check('R7 队长换兵种 → squads.leader 更新为 char17',
          int(sq_r7['leader_character_id']) == 17 and
          int(sq_r7['faction']) == 2,
          f'leader={sq_r7["leader_character_id"]}')
    mem_r7 = {m['character_id']: m for m in
              await st_r7.list_squad_members(sid_r7)}
    check('R7 B 自动换角 char12 → char14（跟随侦察）',
          14 in mem_r7 and 12 not in mem_r7,
          f'chars={sorted(mem_r7)}')
    check('R7 无匹配成员请离（char77 行删除）',
          77 not in mem_r7, f'chars={sorted(mem_r7)}')

    # 用例 R8：成员换不兼容兵种 → 17 拒绝（防混兵种崩溃）
    st_r8 = _fresh_store()
    st_r8.next_id = 700
    sid_r8 = await st_r8.create_squad(31, 2, 'r8 squad')
    m_b8 = await st_r8.add_squad_member(sid_r8, 12, status=ST_IN_LOBBY)
    w_r8 = await _set_member(st_r8, sess_a, m_b8, 14, 0)   # B 换侦察
    check('R8 响应已回（SetSquadMemberStatusResponse）',
          b'SetSquadMemberStatusResponse' in w_r8.data)
    mem_r8 = {m['character_id'] for m in
              await st_r8.list_squad_members(sid_r8)}
    check('R8 拒绝后成员行角色不变（仍 char12）',
          mem_r8 == {31, 12}, f'chars={sorted(mem_r8)}')
    PLAYER_SELECTED_CHAR.clear()

    # --- 小队匹配 charId=0 解析（EnterMatchmakingCommand 整队分支）
    # 用例 5：小队成员点匹配（客户端发 characterId=0）→ 按成员行解析角色，
    #         JoinMatchMakingResponse 必须是 ok=1（修复前 rejected=0）
    import mm_handler
    import state
    store4 = FakeStore()
    store4.chars.update(store.chars)
    store4.usernames.update(store.usernames)
    sid = await store4.create_squad(31, 2, 'Squad of zhai33')
    await store4.add_squad_member(sid, 49, status=ST_IN_LOBBY)  # B 已接受入队
    old_store = state.STORE
    state.STORE = store4
    try:
        proto = pb_varint(1, 0) + pb_varint(7, 1)   # charId=0, gamemode=1
        wj = FakeWriter()
        # 成员均不在线（FLASH_SESSIONS 空）→ _enqueue_squad 空成员 no-op，
        # 不触碰 POOL；只验证响应与角色解析
        await mm_handler._handle_join(sess_a, wj, 21, proto)
        mainc, _ = Chunk.parse(wj.data, 0)
        body = mainc.subs[0].subs[0].data
        jres = body[1] if len(body) > 1 else 0    # f1 varint（值 1/0 单字节）
        check('小队匹配 charId=0 → 响应 ok（修复前 rejected）',
              jres == 1, f'got {jres}')
    finally:
        state.STORE = old_store

    # 用例 6：无任何小队且 charId=0 → 仍拒绝（原兜底不回归）
    store5 = FakeStore()
    store5.chars.update(store.chars)
    store5.usernames.update(store.usernames)
    state.STORE = store5
    try:
        proto = pb_varint(1, 0) + pb_varint(7, 1)
        wj2 = FakeWriter()
        await mm_handler._handle_join(sess_a, wj2, 22, proto)
        mainc, _ = Chunk.parse(wj2.data, 0)
        body = mainc.subs[0].subs[0].data
        jres2 = body[1] if len(body) > 1 else 0
        check('无小队 charId=0 → 仍拒绝', jres2 == 0, f'got {jres2}')
    finally:
        state.STORE = old_store

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
