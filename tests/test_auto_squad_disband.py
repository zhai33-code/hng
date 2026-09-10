# -*- coding: utf-8 -*-
"""匹配凑队（auto squad）解散回归测试（2026-09-02 "解散后没有退出小队"）。

背景：野队匹配把 2+ 散人合并进临时凑队（mm_state._MM_MISSION_SQUADS，
仅存内存）后，客户端点"解散小队"发 RemoveSquadRequest：
  - 旧代码只查 solo 注册表与 DB：第一次点击被合并前残留的 solo 条目劫持
    （删早已不存在的旧 solo 行、返回"成功"，凑队行没动 → UI 没退出小队），
    且该 solo 条目被消费掉（战斗结束恢复 solo 视图失效）；
    第二次点击 solo 空、DB 无 → 161 squad not found 弹错。
  - 修复：handle_remove_squad 新增凑队分支（排在 solo 分支前），删本人
    连接上的整队凑队行 + 其他在线成员连接上的本人成员行，返回 OK；
    solo 注册表条目保留（战斗结束恢复用）。

用例：
 1. 凑队成员解散 → response=1，本人连接收到 SquadSession 先删 + 全队成员行删除
    （2026-09-09 顺序调整：SquadSession 必须先删，客户端 resetSquad 才被触发），
    不再重推 solo 行（★重推方案=小队永不消失，已回退）；队友连接只收到
    本人成员行删除，服务端成员表移除本人
 2. 解散后 solo 注册表残留条目未被消费（战斗结束可恢复 solo 视图）★回归
 3. squad_db_id≠0 的真实小队条目不走凑队分支（保持 DB 分支语义）
 4. 纯 solo 隐式小队解散 → 删两行（★SquadSession 先删，客户端 resetSquad
    触发前提）+ 清注册表，不重推
    （★2026-09-09 终版语义：v2"不删行重推 idle solo 行"导致小队永不
    消失——用户"现在小队都没法解散了"；v1"成员行先删"导致 resetSquad
    被跳过 isOwnedByPlayer 残留 → 卡"小队未准备好"。终版=SquadSession
    先删再删成员行，两症皆消）
 4b. 排队中的 solo 小队解散 → 撤队（出池/清队列/撤 PMM 行）且 PMM delete
    先于 SquadSession delete（客户端 squadSession=null 时 onLobbySignal
    Updates 读 missionId 抛 #1009 的防线）
 4c. 带 solo 残留条目的真小队解散 → 不被 ① 分支劫持，走 ② DB 分支真删
    （solo 条目在加入真小队后不清 → 消费旧条目回 OK 但真小队未删）
"""
import asyncio
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol import Chunk
import mm_state
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


def _read_varint(data, i):
    v = shift = 0
    while i < len(data):
        b = data[i]; i += 1
        v |= (b & 0x7F) << shift; shift += 7
        if not (b & 0x80):
            break
    return v, i


def _collect_pushes(w):
    """解析 writer 中全部 KVCS set：(table_name, 首字段行 id) 列表。"""
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
        if action is None or action.as_str() != 'set':
            continue
        table = action.subs[0] if action.subs else None
        if table is None or not table.subs:
            continue
        for row in table.subs:
            tag, i = _read_varint(row.data, 0)
            rid = 0
            if tag == 0x08:            # f1 varint = 行 id
                rid, _ = _read_varint(row.data, i)
            out.append((table.as_str(), rid))
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
    mm_state._MM_MISSION_SQUADS.clear()
    mm_state._SOLO_SQUADS.clear()
    sq_id = 8072128442240809119
    mm_state._MM_MISSION_SQUADS[223287] = {
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
    mm_state._SOLO_SQUADS[2] = {
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
        check('★SquadSession 先于成员行删除（客户端 resetSquad 前置条件）',
              dels.index(('SquadSession', sq_id))
              < dels.index(('SquadMemberSession', 101)),
              f'deletes={dels}')
        pushes = _collect_pushes(w)
        check('解散后不重推 solo 行（★重推会让小队永不消失，2026-09-09 回退）',
              not pushes,
              f'pushes={pushes}')
        b_dels = _collect_deletes(sess_b.writer)
        check('队友连接只删除本人成员行(101)',
              b_dels == [('SquadMemberSession', 101)],
              f'BotSU1 deletes={b_dels}')
        members = (mm_state._MM_MISSION_SQUADS[223287][sq_id]
                   ['members'])
        check('服务端凑队成员表移除本人',
              [int(m['player_id']) for m in members] == [11],
              f'got {[m.get("player_id") for m in members]}')

        # --- 用例 2：solo 注册表残留条目未被消费 ★核心回归点 ---
        check('solo 注册表条目保留（战斗结束恢复 solo 视图）',
              2 in mm_state._SOLO_SQUADS
              and mm_state._SOLO_SQUADS[2]['squad_row']
              == 8072128442240809115,
              '（旧代码第一次解散即被 solo 分支消费掉）')
    finally:
        squad_mod.STORE = old_store
        FLASH_SESSIONS.pop('BotSU1', None)

    # --- 用例 3：squad_db_id≠0 的真小队条目不走凑队分支 ---
    _seed_auto_squad()
    mm_state._MM_MISSION_SQUADS[223287][
        8072128442240809119]['squad_db_id'] = 123
    mm_state._SOLO_SQUADS.clear()     # 无 solo 条目 → 只能落到 DB 分支
    sess_a = FakeSess(2, 'DoubleBird')
    code, w = await _remove_squad(sess_a)
    check('真小队条目(squad_db_id≠0)不走凑队分支',
          code == RC_SQUAD_NOT_FOUND and not _collect_deletes(w),
          f'got code={code}（凑队分支误入会返回 1 并推删除行）')

    # --- 用例 4：纯 solo 隐式小队解散 → 删两行 + 清注册表（终版语义） ---
    #   ★2026-09-09 三版演化：
    #   - v1"成员行先删"：handleClearingSlot 置空 _squadSession →
    #     SquadSession DELETE 变 no-op，resetSquad 被跳过 → isOwnedByPlayer
    #     残留 → 重登玩家切屏卡 READY_UP"小队未准备好"。
    #   - v2"不删行改重推 idle solo 行"：小队永不消失（用户
    #     "现在小队都没法解散了"，log 5 次点击全部 ★solo squad reset
    #     on disband 且行 id 不变）。
    #   - 终版：SquadSession 先删（resetSquad 全量清理 isOwnedByPlayer）
    #     + 成员行删 + 清注册表 + 排队中一并撤队。
    mm_state._MM_MISSION_SQUADS.clear()
    mm_state._SOLO_SQUADS.clear()
    mm_state._SOLO_SQUADS[2] = {
        'squad_row': 7001, 'member_row': 7002, 'template': 1, 'faction': 1,
        'mission_id': 0, 'gamertag': 'DoubleBird', 'char_id': 25,
        'char_name': 'DB', 'rank': 5,
    }
    sess_a = FakeSess(2, 'DoubleBird')
    code, w = await _remove_squad(sess_a)
    dels = _collect_deletes(w)
    pushes = _collect_pushes(w)
    check('solo 解散：response=1 + 删两行（终版：小队必须消失）',
          code == RC_OK
          and ('SquadSession', 7001) in dels
          and ('SquadMemberSession', 7002) in dels,
          f'code={code} deletes={dels}')
    check('solo 解散：★SquadSession 先于成员行删除（resetSquad 前置条件）',
          dels.index(('SquadSession', 7001))
          < dels.index(('SquadMemberSession', 7002)),
          f'deletes={dels}')
    check('solo 解散：不重推 idle 行（重推=小队永不消失）',
          not pushes, f'pushes={pushes}')
    check('solo 解散：注册表清掉（下次排队重建行）',
          2 not in mm_state._SOLO_SQUADS, '')

    # --- 用例 4b：排队中的 solo 小队解散 → 撤队 + PMM delete 先推 ---
    import mm_pool
    mm_state._MM_QUEUES.clear()
    mm_state._MATCH_PMM_IDS.clear()
    mm_state._AI_FILL_TASKS.clear()
    mm_state.POOL.remove(2)
    mm_state.POOL.enqueue(mm_pool.QueueEntry(
        player_id=2, char_id=25, faction=1, gamemode_id=1))
    mm_state._MM_QUEUES[2] = {'pmm_id': 555, 'mission_id': 0,
                              'gamemode': 1, 'ctx': None}
    mm_state._MATCH_PMM_IDS[2] = 555
    mm_state._SOLO_SQUADS[2] = {
        'squad_row': 7001, 'member_row': 7002, 'template': 1, 'faction': 1,
        'mission_id': 0, 'gamertag': 'DoubleBird', 'char_id': 25,
        'char_name': 'DB', 'rank': 5,
    }
    sess_a = FakeSess(2, 'DoubleBird')
    code, w = await _remove_squad(sess_a)
    dels = _collect_deletes(w)
    check('排队中解散：PMM delete 先于 SquadSession（#1009 NPE 防线）',
          code == RC_OK
          and ('PlayerMatchMaking', 555) in dels
          and dels.index(('PlayerMatchMaking', 555))
          < dels.index(('SquadSession', 7001)),
          f'code={code} deletes={dels}')
    check('排队中解散：撤队出池 + 清队列/注册表',
          mm_state.POOL.get(2) is None
          and 2 not in mm_state._MM_QUEUES
          and 2 not in mm_state._MATCH_PMM_IDS
          and 2 not in mm_state._SOLO_SQUADS,
          f'pool={mm_state.POOL.get(2)} '
          f'q={2 in mm_state._MM_QUEUES}')
    mm_state._MM_QUEUES.clear()
    mm_state._MATCH_PMM_IDS.clear()

    # --- 用例 4c：带 solo 残留条目的真小队解散 → 不被 ① 劫持，走 ② 真删 ---
    #   场景：玩家单人匹配过（solo 条目残留）→ 接受邀请入真小队（旧实现
    #   不清条目）→ 队长解散：① 分支若消费旧条目 → 删旧 solo 行 + 回 OK，
    #   真小队未删 → "没法解散"。
    class FakeStore:
        def __init__(self):
            self.dissolved = []

        async def get_squad_for_player(self, pid):
            return {'id': 888, 'leader_character_id': 25, 'faction': 1,
                    'name': 'Squad of DoubleBird', 'matchmaking_flag': False}

        async def list_squad_members(self, squad_id):
            return [{'id': 9001, 'player_id': 2, 'gamertag': 'DoubleBird',
                     'character_id': 25, 'faction': 1, 'template': 1,
                     'status': 2, 'combat_xp': 0, 'char_name': 'DB'}]

        async def dissolve_squad(self, squad_id):
            self.dissolved.append(int(squad_id))

    mm_state._MM_MISSION_SQUADS.clear()
    mm_state._SOLO_SQUADS.clear()
    mm_state._SOLO_SQUADS[2] = {
        'squad_row': 7001, 'member_row': 7002, 'template': 1, 'faction': 1,
        'mission_id': 0, 'gamertag': 'DoubleBird', 'char_id': 25,
        'char_name': 'DB', 'rank': 5,
    }
    fake = FakeStore()
    sess_a = FakeSess(2, 'DoubleBird')
    FLASH_SESSIONS['DoubleBird'] = sess_a
    squad_mod.STORE = fake
    try:
        code, w = await _remove_squad(sess_a)
        # ② 分支经 live_sessions 推到会话自身的 writer（非响应 writer）
        dels = _collect_deletes(w) + _collect_deletes(sess_a.writer)
        check('真小队+solo残留：走 ② 真删（dissolve_squad 被调）',
              code == RC_OK and fake.dissolved == [888],
              f'code={code} dissolved={fake.dissolved}')
        check('真小队+solo残留：删的是真小队行(888/9001) 非 solo 行(7001)',
              ('SquadSession', 888) in dels
              and ('SquadMemberSession', 9001) in dels
              and ('SquadSession', 7001) not in dels,
              f'deletes={dels}')
        check('真小队+solo残留：solo 条目未被消费（供 ① 下次正常使用）',
              2 in mm_state._SOLO_SQUADS
              and mm_state._SOLO_SQUADS[2]['squad_row'] == 7001,
              '')
    finally:
        squad_mod.STORE = old_store
        FLASH_SESSIONS.pop('DoubleBird', None)
        mm_state._SOLO_SQUADS.clear()

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
        members = (mm_state._MM_MISSION_SQUADS[223287]
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
        mm_state._MM_MISSION_SQUADS.clear()
        mm_state._SOLO_SQUADS.clear()

    # --- 用例 8：DB 真小队 SetSquadMatchMakingFlag → 不再全量回推 ---
    #   2026-09-09 闪烁风暴根因：客户端每条 SquadMemberSession SET →
    #   evaluatePlayerFactions → 队长（owner 且 >1 人）自动重发本请求；
    #   旧实现全量回推 2 遍整队行集 → 死循环（VPS 一小时档 4100 请求，
    #   队员 UI 每秒重绘"一直在闪"）。修复：发起者零推送，其他成员只收
    #   SquadSession 单行（无成员行 → 不进 evaluatePlayerFactions 链）。
    class FlagStore:
        def __init__(self):
            self.flag_set = None

        async def get_squad(self, sid):
            return {'id': int(sid), 'leader_character_id': 25, 'faction': 1,
                    'name': 'Squad of DoubleBird', 'matchmaking_flag': False}

        async def list_squad_members(self, sid):
            return [
                {'id': 9001, 'player_id': 2, 'gamertag': 'DoubleBird',
                 'character_id': 25, 'faction': 1, 'template': 1,
                 'status': 2, 'combat_xp': 0, 'char_name': 'DB'},
                {'id': 9002, 'player_id': 11, 'gamertag': 'BotSU1',
                 'character_id': 31, 'faction': 1, 'template': 1,
                 'status': 2, 'combat_xp': 0, 'char_name': 'BS'},
            ]

        async def set_squad_flag(self, sid, flag):
            self.flag_set = (int(sid), bool(flag))
            return True

    flag_store = FlagStore()
    sess_a = FakeSess(2, 'DoubleBird', playerview_cmd=60)
    sess_b = FakeSess(11, 'BotSU1', playerview_cmd=61)
    FLASH_SESSIONS['DoubleBird'] = sess_a
    FLASH_SESSIONS['BotSU1'] = sess_b
    squad_mod.STORE = flag_store
    try:
        w = FakeWriter()
        await handle_set_squad_matchmaking_flag(
            sess_a, w, 49, 'SetSquadMatchMakingFlagRequest',
            b'\x08' + _varint(888) + b'\x18\x01')   # f1=888 f3=1
        check('flag：DB 落库成功 + 回 OK',
              _last_response_code(w) == RC_OK
              and flag_store.flag_set == (888, True),
              f'code={_last_response_code(w)} flag={flag_store.flag_set}')
        check('flag：发起者连接零 KVCS set（旧实现回推 2 遍整队行集）',
              _collect_pushes(w) == []
              and _collect_pushes(sess_a.writer) == [],
              f'resp={_collect_pushes(w)} pv={_collect_pushes(sess_a.writer)}')
        b_pushes = _collect_pushes(sess_b.writer)
        check('flag：队友只收 SquadSession 单行、无成员行',
              b_pushes == [('SquadSession', 888)],
              f'B={b_pushes}')
    finally:
        squad_mod.STORE = old_store
        FLASH_SESSIONS.pop('DoubleBird', None)
        FLASH_SESSIONS.pop('BotSU1', None)

    # --- 用例 9：排队中真小队解散 → 撤队路径不再推整队 SET 行集 ---
    #   旧链 handle_remove_squad → _cancel_squad_queue →
    #   reset_real_squad_to_lobby 会先给全体推 SET 行集再 dissolve+DELETE
    #   ——SET 喂队长客户端重发 SetSquadMatchMakingFlag（在途请求跨过
    #   解散全变 161，"解散没用"观感）。修复：push_rows=False。
    class DisbandStore(FlagStore):
        def __init__(self):
            super().__init__()
            self.dissolved = []
            self.member_updates = []

        async def get_squad_for_player(self, pid):
            return {'id': 888, 'leader_character_id': 25, 'faction': 1,
                    'name': 'Squad of DoubleBird', 'matchmaking_flag': True}

        async def update_squad_member(self, row_id, status=None,
                                      **kw):
            self.member_updates.append((row_id, status))
            return True

        async def dissolve_squad(self, sid):
            self.dissolved.append(int(sid))

    disband_store = DisbandStore()
    sess_a = FakeSess(2, 'DoubleBird', playerview_cmd=62)
    sess_b = FakeSess(11, 'BotSU1', playerview_cmd=63)
    FLASH_SESSIONS['DoubleBird'] = sess_a
    FLASH_SESSIONS['BotSU1'] = sess_b
    squad_mod.STORE = disband_store
    try:
        code, w = await _remove_squad(sess_a)
        check('排队真小队解散：OK + dissolve',
              code == RC_OK and disband_store.dissolved == [888],
              f'code={code} dissolved={disband_store.dissolved}')
        check('解散全程零 KVCS set（撤队不推 SET，只推 DELETE）',
              _collect_pushes(w) == []
              and _collect_pushes(sess_a.writer) == []
              and _collect_pushes(sess_b.writer) == [],
              f'resp={_collect_pushes(w)} '
              f'A={_collect_pushes(sess_a.writer)} '
              f'B={_collect_pushes(sess_b.writer)}')
    finally:
        squad_mod.STORE = old_store
        FLASH_SESSIONS.pop('DoubleBird', None)
        FLASH_SESSIONS.pop('BotSU1', None)

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
    return (mm_state._MM_MISSION_SQUADS.get(223287, {})
            .get(8072128442240809119, {})
            .get('members') or [])


if __name__ == '__main__':
    asyncio.run(main())
