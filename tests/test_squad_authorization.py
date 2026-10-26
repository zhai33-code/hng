# -*- coding: utf-8 -*-
"""小队写接口身份、锁状态与踢人同步回归。"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol import Chunk, pb_parse, pb_varint
import handlers.squad as squad_mod
import matchmaking
import state
from mm_state import _MM_QUEUES
from handlers.squad import (
    RC_OK, RC_PLAYER_NOT_OWNER, RC_SQUAD_IN_LOCKED_STATE,
    RC_SQUAD_NOT_OWNED, RC_SQUAD_SLOT_NOT_FOUND,
    handle_modify_squad,
    handle_create_squad, handle_remove_squad, handle_remove_squad_member,
    handle_set_squad_matchmaking_flag, handle_set_squad_member_status,
    handle_squad_invite_player, handle_squad_respond_to_invite,
    handle_update_squad_resource,
)
from state import FLASH_SESSIONS


class Writer:
    def __init__(self):
        self.data = b''

    def write(self, data):
        self.data += data

    async def drain(self):
        pass


class Session:
    def __init__(self, pid, username):
        self.pid = pid
        self.player_id = pid
        self.username = username
        self.playerview_cmd = 55
        self.writer = Writer()

    async def ensure_player(self):
        return self.pid


class Store:
    def __init__(self):
        self.squad = {'id': 10, 'name': 'S', 'faction': 1,
                      'leader_character_id': 101,
                      'matchmaking_flag': False}
        self.members = {
            1: self._member(1, 1, 'leader', 101, 2),
            2: self._member(2, 2, 'member', 102, 2),
            3: self._member(3, 3, 'pending', 103, 1),
        }
        self.flags = []
        self.dissolved = []

    @staticmethod
    def _member(mid, pid, tag, char_id, status):
        return {'id': mid, 'squad_id': 10,
                'player_id': pid, 'gamertag': tag,
                'character_id': char_id, 'status': status,
                'faction': 1, 'template': 1, 'combat_xp': 0,
                'char_name': tag}

    async def get_squad(self, sid):
        return self.squad if int(sid) == 10 and not self.dissolved else None

    async def get_squad_for_player(self, pid):
        return self.squad if any(m['player_id'] == int(pid)
                                 for m in self.members.values()) else None

    async def list_squad_members(self, sid):
        return [dict(m) for m in self.members.values()]

    async def get_squad_member(self, mid):
        m = self.members.get(int(mid))
        return ({k: m[k] for k in ('id', 'squad_id', 'character_id', 'status')}
                if m else None)

    async def remove_squad_member(self, mid):
        return self.members.pop(int(mid), None) is not None

    async def update_squad_member(self, mid, character_id=None, status=None):
        m = self.members.get(int(mid))
        if m is None:
            return False
        if character_id is not None:
            m['character_id'] = int(character_id)
        if status is not None:
            m['status'] = int(status)
        return True

    async def accept_squad_invite(self, mid, pid, character_id):
        m = self.members.get(int(mid))
        if (m is None or int(m['player_id']) != int(pid)
                or int(m['status']) != 1):
            return 0
        m['character_id'] = int(character_id)
        m['status'] = 2
        return int(m['squad_id'])

    async def decline_squad_invite(self, mid, pid):
        m = self.members.get(int(mid))
        if (m is None or int(m['player_id']) != int(pid)
                or int(m['status']) != 1):
            return 0
        self.members.pop(int(mid))
        return int(m['squad_id'])

    async def set_squad_flag(self, sid, flag):
        self.squad['matchmaking_flag'] = bool(flag)
        self.flags.append((int(sid), bool(flag)))
        return True

    async def dissolve_squad(self, sid):
        self.dissolved.append(int(sid))
        self.members.clear()
        return True


def response_code(writer):
    off = 0
    last = None
    while off < len(writer.data):
        last, off = Chunk.parse(writer.data, off)
    data = last.subs[0].subs[0].data
    # Every response used here is f1 varint and all codes fit two bytes.
    i = 1
    value = shift = 0
    while i < len(data):
        byte = data[i]
        i += 1
        value |= (byte & 0x7f) << shift
        if not byte & 0x80:
            return value
        shift += 7
    return 0


async def invoke(handler, sess, proto, cmd=90):
    writer = Writer()
    await handler(sess, writer, cmd, '', proto)
    return response_code(writer), writer


async def main():
    old_store = squad_mod.STORE
    old_state_store = state.STORE
    store = Store()
    squad_mod.STORE = store
    state.STORE = store
    leader = Session(1, 'leader')
    member = Session(2, 'member')
    pending = Session(3, 'pending')
    outsider = Session(99, 'outsider')
    FLASH_SESSIONS.update({'leader': leader, 'member': member,
                           'pending': pending})
    checks = []

    def check(label, condition):
        checks.append(bool(condition))
        print(f'[{"PASS" if condition else "FAIL"}] {label}')

    try:
        code, _ = await invoke(handle_remove_squad_member, outsider,
                               pb_varint(1, 2))
        check('外部玩家不能删除成员',
              code == RC_PLAYER_NOT_OWNER and 2 in store.members)

        code, _ = await invoke(handle_remove_squad, member, pb_varint(1, 0))
        check('非队长不能解散小队',
              code == RC_SQUAD_NOT_OWNED and not store.dissolved)

        code, _ = await invoke(handle_squad_respond_to_invite, outsider,
                               pb_varint(1, 3) + pb_varint(2, 0))
        check('非邀请属主不能接受/拒绝邀请',
              code == RC_SQUAD_SLOT_NOT_FOUND and 3 in store.members)

        code, _ = await invoke(handle_set_squad_member_status, outsider,
                               pb_varint(1, 2) + pb_varint(3, 3))
        check('外部玩家不能改成员 Ready 状态',
              code == RC_PLAYER_NOT_OWNER and store.members[2]['status'] == 2)

        code, _ = await invoke(handle_set_squad_matchmaking_flag, member,
                               pb_varint(1, 10) + pb_varint(3, 1))
        check('非队长不能修改匹配标记',
              code == RC_SQUAD_NOT_OWNED and not store.flags)

        code, _ = await invoke(handle_modify_squad, member, pb_varint(1, 10))
        check('非队长不能修改支援槽', code == RC_SQUAD_NOT_OWNED)

        code, _ = await invoke(handle_update_squad_resource, member,
                               pb_varint(1, 10))
        check('非队长不能购买小队资源', code == RC_SQUAD_NOT_OWNED)

        code, _ = await invoke(handle_squad_invite_player, leader,
                               pb_varint(1, 101) + pb_varint(2, 1))
        check('不能邀请自己创建双成员行',
              code == squad_mod.RC_PLAYER_ALREADY_IN_SQUAD)

        _MM_QUEUES[99] = {'pmm_id': 78, 'mission_id': 0, 'ctx': {}}
        code, _ = await invoke(handle_create_squad, outsider,
                               pb_varint(1, 999))
        check('匹配队列中不能新建真小队',
              code == RC_SQUAD_IN_LOCKED_STATE)
        _MM_QUEUES.pop(99, None)

        squad, eligible = await matchmaking._squad_eligible_members(3)
        revived = await matchmaking._revive_own_membership(10, 3)
        check('pending 邀请不被匹配路径视为正式入队',
              squad is None and eligible is None and not revived
              and store.members[3]['status'] == 1)

        store.members[3]['status'] = 4
        revived = await matchmaking._revive_own_membership(10, 3)
        check('InBattle 残留使用真实存储字段 id 恢复 InLobby',
              revived and store.members[3]['status'] == 2)
        store.members[3]['status'] = 1

        # f12 selects war missions; it is not the authoritative queue state.
        store.squad['matchmaking_flag'] = True
        row = pb_parse(squad_mod._msg_squad_session(
            store.squad, list(store.members.values())))
        check('选择战争模式不会下发小队锁定',
              row[12] == 1 and row[20] == 0 and row[21] == 0)
        code, _ = await invoke(handle_set_squad_member_status, member,
                               pb_varint(1, 2) + pb_varint(3, 3))
        check('选择战争模式后队员仍可准备',
              code == RC_OK and store.members[2]['status'] == 3)
        code, _ = await invoke(handle_set_squad_member_status, member,
                               pb_varint(1, 2) + pb_varint(3, 2))
        check('未排队时队员可取消准备',
              code == RC_OK and store.members[2]['status'] == 2)

        _MM_QUEUES[2] = {'pmm_id': 77, 'mission_id': 0, 'ctx': {}}
        row = pb_parse(squad_mod._msg_squad_session(
            store.squad, list(store.members.values())))
        check('实际排队时下发小队锁定', row[20] == 1 and row[21] == 1)
        code, _ = await invoke(handle_squad_respond_to_invite, pending,
                               pb_varint(1, 3) + pb_varint(2, 1))
        check('小队锁定时不能接受邀请',
              code == RC_SQUAD_IN_LOCKED_STATE
              and store.members[3]['status'] == 1)
        code, _ = await invoke(handle_squad_respond_to_invite, pending,
                               pb_varint(1, 3) + pb_varint(2, 0))
        check('小队锁定时仍可拒绝邀请',
              code == RC_OK and 3 not in store.members)
        _MM_QUEUES.pop(2, None)
        row = pb_parse(squad_mod._msg_squad_session(
            store.squad, list(store.members.values())))
        check('撤队后战争选项保留也会解除锁定',
              row[12] == 1 and row[20] == 0 and row[21] == 0)
        store.squad['matchmaking_flag'] = False

        store.members[2]['status'] = 4
        code, _ = await invoke(handle_remove_squad_member, leader,
                               pb_varint(1, 2))
        check('战斗锁定期间不能踢人',
              code == RC_SQUAD_IN_LOCKED_STATE and 2 in store.members)
        store.members[2]['status'] = 2

        _MM_QUEUES[2] = {'pmm_id': 77, 'mission_id': 0, 'ctx': {}}
        code, _ = await invoke(handle_remove_squad_member, leader,
                               pb_varint(1, 2))
        check('客户端清掉 flag 后权威队列仍保持锁定',
              code == RC_SQUAD_IN_LOCKED_STATE and 2 in store.members)
        _MM_QUEUES.pop(2, None)

        code, _ = await invoke(handle_remove_squad_member, leader,
                               pb_varint(1, 1))
        check('队长成员行不能被单独删除',
              code == RC_SQUAD_NOT_OWNED and 1 in store.members)

        member.writer.data = b''
        code, _ = await invoke(handle_remove_squad_member, leader,
                               pb_varint(1, 2))
        check('队长可踢普通成员', code == RC_OK and 2 not in store.members)
        check('被踢者收到成员 DELETE', b'SquadMemberSession' in member.writer.data)
    finally:
        squad_mod.STORE = old_store
        state.STORE = old_state_store
        _MM_QUEUES.clear()
        for name in ('leader', 'member', 'pending'):
            FLASH_SESSIONS.pop(name, None)

    print(f'\n{sum(checks)}/{len(checks)} checks passed')
    return 0 if all(checks) else 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
