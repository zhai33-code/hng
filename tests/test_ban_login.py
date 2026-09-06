# -*- coding: utf-8 -*-
"""封禁体系回归测试（纯单元级，无需服务器/DB）。

  1. protocol：msg_login2_result 封禁分支 —— 91 带 f5 原因不带 f4/f6；
     92 带 f4（epoch 毫秒字符串）+ f5；97 不受影响（无 f4/f5）
  2. handler 级（mock STORE）：login2_response 密码正确但
     - 账号永久封禁 → 91 + f5，login_events 记 91
     - 账号限时封禁 → 92 + f4 + f5
     - 账号未封禁 + 机器码封禁 → 91（纵深防御）
     - 均未封禁 → 97（回归：正常登录不受影响）
  3. QueryBannedMachineRequest：机器码命中 machine_bans → isBanned=1 + 原因
  4. kick_player_sessions：按 pid/username 匹配关闭会话，其他会话不动
"""
import asyncio
import contextlib
import hmac
import hashlib
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('HNG_GAME_DIR', r'd:\HnG-test')

from protocol import (                               # noqa: E402
    Chunk,
    RC,
    msg_login2_result,
    pb_parse,
    pb_string,
    )

from handlers import auth as auth_mod                # noqa: E402
from handlers import ban as ban_mod                 # noqa: E402
from state import TCP_SESSIONS                      # noqa: E402

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
    saved = {k: getattr(obj, k) for k in overrides}
    for k, v in overrides.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(obj, k, v)


class FakeWriter:
    def __init__(self):
        self.written = []
        self.closed = False

    def write(self, data):
        self.written.append(bytes(data))

    async def drain(self):
        pass

    def close(self):
        self.closed = True


class FakeSess:
    """login2_response 所需的最小会话桩。"""

    def __init__(self, username='bantest', machine_id='', pid=1000001):
        self.username = username
        self.login_ctx = (b'\x11' * 16, b'\x22' * 16, b'\x33' * 20)
        self.machine_id = machine_id
        self.machine_id_old = ''
        self.remote_ip = '127.0.0.1'
        self.player_id = None
        self.user_exists = True
        self._pid = pid

    async def ensure_player(self):
        self.player_id = self._pid
        return self._pid


class FakeStore:
    """get_ban_info / get_machine_ban / login_events 记录桩。"""

    def __init__(self, ban=(False, None, ''), machine_reason=None):
        self.ban = ban
        self.machine_reason = machine_reason
        self.events = []
        self.pool = object()   # _record_login_event 的可用性探针

    async def get_ban_info(self, username):
        return self.ban

    async def get_machine_ban(self, machine_id):
        return self.machine_reason if machine_id else None

    async def record_login_event(self, **kw):
        self.events.append(kw)

    async def get_player(self, username):
        return 1000001


def login_digest(sess):
    _, temp_sid, session_key = sess.login_ctx
    return hmac.new(session_key, temp_sid, hashlib.sha1).digest()


def run_login(store, sess):
    """跑一次 handle_login2_response（digest 正确），返回
    (resp_chunk 的 proto fields, store.events)。"""
    writer = FakeWriter()
    import base64
    proto = pb_string(1, 'x') + pb_string(
        2, base64.b64encode(login_digest(sess)).decode())
    with patched(auth_mod, STORE=store):
        asyncio.run(auth_mod.handle_login2_response(
            sess, writer, 2, 0, proto))
    chunk = Chunk.parse(writer.written[0], 0)[0]
    resp_proto = chunk.subs[0].subs[0].data
    return pb_parse(resp_proto), store.events


def test_msg_login2_result():
    m = pb_parse(msg_login2_result('u', RC['user_ban_permanent'],
                                   ban_reason='cheating'))
    check('91 永久：f3=91 且带 f5 原因',
          m.get(3) == 91 and m.get(5) == b'cheating', f'fields={sorted(m)}')
    check('91 永久：无 f4 到期 / 无 f6 currentplayer',
          4 not in m and 6 not in m)

    exp = datetime.now(timezone.utc) + timedelta(hours=2)
    m = pb_parse(msg_login2_result('u', RC['user_ban_temporary'],
                                   ban_reason='griefing', ban_expires=exp))
    want_ms = str(int(exp.timestamp() * 1000)).encode()
    check('92 限时：f4=epoch 毫秒字符串 + f5 原因',
          m.get(3) == 92 and m.get(4) == want_ms and m.get(5) == b'griefing',
          f'f4={m.get(4)!r}')

    m = pb_parse(msg_login2_result('u', RC['login_success']))
    check('97 成功：无 f4/f5（回归）', 4 not in m and 5 not in m and 6 in m)


def test_login_ban_enforcement():
    # 永久封禁 → 91 + 原因，login_events 记 91
    store = FakeStore(ban=(True, None, '使用外挂'))
    f, events = run_login(store, FakeSess())
    check('永久封禁拦截：f3=91 + f5 原因',
          f.get(3) == 91 and f.get(5) == '使用外挂'.encode('utf-8'),
          f'f3={f.get(3)}')
    check('永久封禁：不推 currentplayer（f6）', 6 not in f)
    check('永久封禁：login_events 记 91 + pid',
          len(events) == 1 and events[0]['result'] == 91
          and events[0]['player_id'] == 1000001,
          f'events={events}')

    # 限时封禁 → 92 + f4 + f5
    exp = datetime.now(timezone.utc) + timedelta(days=3)
    store = FakeStore(ban=(True, exp, '恶意辱骂'))
    f, _ = run_login(store, FakeSess())
    check('限时封禁拦截：f3=92 + f4 到期 + f5 原因',
          f.get(3) == 92 and f.get(4) is not None
          and f.get(5) == '恶意辱骂'.encode('utf-8'))

    # 机器码封禁（账号未封）→ 91
    store = FakeStore(machine_reason='作弊')
    f, events = run_login(store, FakeSess(machine_id='mach-1'))
    check('机器码封禁拦截：f3=91 + f5 原因',
          f.get(3) == 91 and f.get(5) == '作弊'.encode('utf-8'),
          f'f3={f.get(3)}')
    check('机器码封禁：login_events 记 91', events[0]['result'] == 91)

    # 均未封禁 → 97 正常登录（回归）
    store = FakeStore()
    f, events = run_login(store, FakeSess(machine_id='mach-1'))
    check('未封禁：f3=97 正常放行', f.get(3) == 97 and 6 in f)
    check('未封禁：login_events 记 97', events[0]['result'] == 97)


def test_query_banned_machine():
    sess = FakeSess(machine_id='mach-banned')
    store = FakeStore(machine_reason='cheating')
    writer = FakeWriter()
    with patched(auth_mod, STORE=store):
        asyncio.run(auth_mod.handle_querybannedmachinerequest(
            sess, writer, 2, 0,
            pb_string(1, 'mach-banned') + pb_string(2, '')))
    chunk = Chunk.parse(writer.written[0], 0)[0]
    f = pb_parse(chunk.subs[0].subs[0].data)
    check('机器码封禁：QueryBannedMachine 回 isBanned=1 + 原因',
          f.get(1) == 1 and f.get(2) == b'cheating', f'fields={sorted(f)}')

    sess = FakeSess(machine_id='mach-ok')
    store = FakeStore(machine_reason=None)
    writer = FakeWriter()
    with patched(auth_mod, STORE=store):
        asyncio.run(auth_mod.handle_querybannedmachinerequest(
            sess, writer, 2, 0,
            pb_string(1, 'mach-ok') + pb_string(2, '')))
    chunk = Chunk.parse(writer.written[0], 0)[0]
    f = pb_parse(chunk.subs[0].subs[0].data)
    check('机器码未封：回 isBanned=0（回归）', f.get(1) == 0)


def test_kick_sessions():
    s_target = FakeSess(username='badguy')
    s_target.player_id = 42
    s_target.writer = FakeWriter()
    s_other = FakeSess(username='other')
    s_other.player_id = 99
    s_other.writer = FakeWriter()
    TCP_SESSIONS[:] = [s_target, s_other]
    n = asyncio.run(ban_mod.kick_player_sessions(42, 'badguy'))
    try:
        check('踢下线：目标会话被关闭', s_target.writer.closed and n == 1)
        check('踢下线：其他会话不受影响', not s_other.writer.closed)
    finally:
        TCP_SESSIONS[:] = []


if __name__ == '__main__':
    test_msg_login2_result()
    test_login_ban_enforcement()
    test_query_banned_machine()
    test_kick_sessions()
    print(f'\n{PASS} passed, {FAIL} failed')
    sys.exit(1 if FAIL else 0)
