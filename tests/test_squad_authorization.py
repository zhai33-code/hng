# -*- coding: utf-8 -*-
"""小队写接口身份、锁状态与踢人同步回归。"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol import Chunk, pb_varint
import handlers.squad as squad_mod
from handlers.squad import (
    RC_OK, RC_PLAYER_NOT_OWNER, RC_SQUAD_IN_LOCKED_STATE,
    RC_SQUAD_NOT_OWNED, RC_SQUAD_SLOT_NOT_FOUND,
    handle_modify_squad,
    handle_remove_squad, handle_remove_squad_member,
    handle_set_squad_matchmaking_flag, handle_set_squad_member_status,
    handle_squad_respond_to_invite,
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
        return {'id': mid, 'member_id': mid, 'squad_id': 10,
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
    store = Store()
    squad_mod.STORE = store
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

        store.squad['matchmaking_flag'] = True
        code, _ = await invoke(handle_remove_squad_member, leader,
                               pb_varint(1, 2))
        check('排队锁定期间不能踢人',
              code == RC_SQUAD_IN_LOCKED_STATE and 2 in store.members)
        store.squad['matchmaking_flag'] = False

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
        for name in ('leader', 'member', 'pending'):
            FLASH_SESSIONS.pop(name, None)

    print(f'\n{sum(checks)}/{len(checks)} checks passed')
    return 0 if all(checks) else 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
