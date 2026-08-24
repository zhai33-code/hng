"""匹配/战局冒烟测试：验证独立模块 matchmaking.py 的完整客户端流程。

流程（对齐客户端反编译结论）：
1. 登录 demo → 订阅 PLAYER_VIEW
2. JoinMatchMakingRequest → JoinMatchMakingResponse(ok) + PlayerMatchMaking 表推送（匹配中）
3. 等 ~5s → PlayerMissionSession 推送(connectionState=Admitted=2) + PlayerMatchMaking delete
4. GetMissionDetailsRequest(missionId) → GetMissionDetailsResponse（简报/部署数据）
5. JoinMatchMaking 后立即 Cancel → CancelJoinMatchMakingResponse + PlayerMatchMaking delete
"""
import asyncio
import base64
import hashlib
import hmac
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hngserver import Chunk, build_response
from player_store import PlayerStore


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


def pb_bool(field, value):
    return bytes([field << 3 | 0, 1 if value else 0])


def pb_string(field, value):
    v = value.encode() if isinstance(value, str) else value
    out = bytearray([field << 3 | 2])
    n = len(v)
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | 0x80 if n else b)
        if not n:
            break
    return bytes(out) + v


def read_varint(b, i):
    v = 0
    shift = 0
    while True:
        c = b[i]
        i += 1
        v |= (c & 0x7F) << shift
        shift += 7
        if not c & 0x80:
            break
    return v, i


def read_fields(b):
    fields = {}
    i = 0
    while i < len(b):
        tag, i = read_varint(b, i)
        fn, wt = tag >> 3, tag & 7
        if wt == 0:
            v, i = read_varint(b, i)
            fields[fn] = v
        elif wt == 2:
            ln, i = read_varint(b, i)
            fields[fn] = b[i:i + ln]
            i += ln
        elif wt == 5:
            fields[fn] = b[i:i + 4]
            i += 4
        elif wt == 1:
            fields[fn] = b[i:i + 8]
            i += 8
        else:
            break
    return fields


async def login(r, w, username, password):
    req = build_response(1, 'login2_begin',
                         pb_string(1, username) + pb_string(2, 'mm-test') +
                         pb_bool(3, True) + pb_bool(4, True))
    w.write(req); await w.drain()
    data = await r.read(65536)
    chunk, _ = Chunk.parse(data, 0)
    proto = chunk.subs[0].subs[0].data
    f = read_fields(proto)
    salt = base64.b64decode(f[1]); temp_sid = base64.b64decode(f[2])
    enc_key = base64.b64decode(f[3])
    pw_hash = hashlib.sha1(password + salt).digest()
    k1 = hashlib.sha1(pw_hash + temp_sid).digest()
    session_key = bytes(b ^ k1[i % len(k1)] for i, b in enumerate(enc_key))
    digest = hmac.new(session_key, temp_sid, hashlib.sha1).digest()
    m = pb_string(1, f[2]) + pb_string(2, base64.b64encode(digest).decode())
    w.write(build_response(2, 'login2_response', m)); await w.drain()
    data = await r.read(65536)
    chunk, _ = Chunk.parse(data, 0)
    code = read_fields(chunk.subs[0].subs[0].data)[3]
    return code


def parse_msgs(buf):
    out = []
    while buf:
        chunk, nxt = Chunk.parse(buf, 0)
        if chunk is None:
            break
        buf = buf[nxt:]
        title = chunk.subs[0].as_str() if chunk.subs else None
        out.append((chunk.as_int(), title, chunk))
    return out


async def request(r, w, cmd_id, req_type, proto=b''):
    """发送请求并收集 1.5s 内消息。"""
    w.write(build_response(cmd_id, req_type, proto)); await w.drain()
    return await collect(r, 1.5)


async def collect(r, seconds):
    buf = b''
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            data = await asyncio.wait_for(r.read(65536), timeout=0.3)
        except asyncio.TimeoutError:
            continue  # 还没到 deadline，继续等
        if not data:
            break  # 连接已关闭
        buf += data
    return parse_msgs(buf)


def kvcs_tables(chunk):
    """'set' 动作的 (table, proto)。"""
    out = []

    def walk(c):
        if c.subs and len(c.subs) == 1 and c.subs[0].subs == []:
            out.append((c.as_str(), c.subs[0].data))
        for s in c.subs:
            walk(s)
    walk(chunk)
    return out


def find_kvcs(msgs, table):
    for cmd, title, chunk in msgs:
        if title != 'KeyValueChangeSet':
            continue
        for tn, data in kvcs_tables(chunk):
            if tn == table:
                return read_fields(data)
    return None


def find_response(msgs, resp_type):
    for cmd, title, chunk in msgs:
        if title == resp_type:
            body = chunk.subs[0].subs[0].data if chunk.subs[0].subs else b''
            return read_fields(body)
    return None


def find_delete(msgs, table):
    """'delete' 动作的 (table, id_bytes)。"""
    for cmd, title, chunk in msgs:
        if title != 'KeyValueChangeSet':
            continue
        for act in chunk.subs[0].subs:
            if act.as_str() == 'delete':
                for tab in act.subs:
                    if tab.as_str() == table:
                        return tab.subs[0].data
    return None


RESULTS = []


def check(label, ok, detail=''):
    RESULTS.append((label, ok))
    print(f'[{"PASS" if ok else "FAIL"}] {label}{("  " + detail) if detail else ""}')


async def main():
    r, w = await asyncio.open_connection('127.0.0.1', 15110)
    code = await login(r, w, 'demo', b'demodemo123')
    check('登录成功', code == 97, f'code={code}')
    if code != 97:
        return

    await request(r, w, 10, 'subscribeplayerview')

    # 取一个角色 id
    store = PlayerStore()
    await store.start()
    pid = await store.get_player('demo')
    chars = await store.list_characters(pid)
    await store.close()
    if not chars:
        check('demo 有角色', False)
        return
    char_id = int(chars[0]['id'])
    print(f'[i] 使用角色 id={char_id} ({chars[0]["name"]})')

    # ---- 1. JoinMatchMakingRequest ----
    jreq = pb_varint(1, char_id) + pb_varint(2, 0) + pb_bool(4, True) + \
        pb_varint(7, 0) + pb_bool(8, True)
    msgs = await request(r, w, 12, 'JoinMatchMakingRequest', jreq)
    rsp = find_response(msgs, 'JoinMatchMakingResponse')
    check('Join 响应 ok', rsp is not None and rsp.get(1) == 1)
    pmm = find_kvcs(msgs, 'PlayerMatchMaking')
    check('PlayerMatchMaking 推送（匹配中）', pmm is not None,
          f'isMatchmaking={pmm.get(10) if pmm else None}')

    # ---- 2. 等 ~5s 匹配成功 ----
    msgs = await collect(r, 5.5)
    pms = find_kvcs(msgs, 'PlayerMissionSession')
    check('PlayerMissionSession 推送（Admitted=2 拉起 hng.exe 的触发器）',
          pms is not None and pms.get(14) == 2,
          f'connectionState={pms.get(14) if pms else None}')
    mission_id = int(pms[8]) if pms else None
    del_row = find_delete(msgs, 'PlayerMatchMaking')
    check('PlayerMatchMaking delete（退出匹配中）', del_row is not None)
    if pmm is not None and del_row is not None:
        check('delete id 匹配', struct.unpack('<q', del_row)[0] == int(pmm[1]))

    # ---- 3. GetMissionDetailsRequest ----
    if mission_id:
        greq = pb_varint(1, mission_id) + pb_varint(2, 0)
        msgs = await request(r, w, 13, 'GetMissionDetailsRequest', greq)
        rsp = find_response(msgs, 'GetMissionDetailsResponse')
        check('战局详情响应 ok', rsp is not None and rsp.get(1) == 0)  # 他服实测 response=0
        info = read_fields(rsp[2]) if rsp and rsp.get(2) else None
        check('MissionInfo 有效（missionpreset=10 AI 模式）',
              info is not None and info.get(3) == 10,
              f'missionId={info.get(1) if info else None}')
        check('factions 参战方存在', rsp is not None and 3 in rsp)
        check('accessPoints 部署点', rsp is not None and 6 in rsp)

    # ---- 4. Join 后立即 Cancel ----
    msgs = await request(r, w, 14, 'JoinMatchMakingRequest', jreq)
    pmm2 = find_kvcs(msgs, 'PlayerMatchMaking')
    check('再次 Join 成功', pmm2 is not None)
    creq = pb_varint(1, char_id)
    msgs = await request(r, w, 15, 'CancelJoinMatchMakingRequest', creq)
    rsp = find_response(msgs, 'CancelJoinMatchMakingResponse')
    check('Cancel 响应 ok', rsp is not None and rsp.get(1) == 1)
    del_row = find_delete(msgs, 'PlayerMatchMaking')
    check('Cancel 后 delete 匹配行', del_row is not None)

    w.close()
    await asyncio.sleep(0.2)
    failed = [l for l, ok in RESULTS if not ok]
    print(f'\nMATCHMAKING: {"ALL PASS" if not failed else f"{len(failed)} FAILED: {failed}"} '
          f'({len(RESULTS) - len(failed)}/{len(RESULTS)} passed)')
    sys.exit(1 if failed else 0)


asyncio.run(main())
