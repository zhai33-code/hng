# -*- coding: utf-8 -*-
"""login_events 端到端验证：模拟客户端连 15110 发 QueryBannedMachineRequest
（带测试机器码）+ login2 全流程，随后直查 login_events 表核对记录。

一次性验证脚本（手动运行）：python test_login_event_e2e.py
"""
import asyncio
import base64
import hashlib
import hmac
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from protocol import Chunk, build_response, pb_string, pb_int64

HOST = '127.0.0.1'
GAME_PORT = 15110
USERNAME = sys.argv[3] if len(sys.argv) > 3 else 'BotUS1'
PASSWORD = sys.argv[1] if len(sys.argv) > 1 else 'botbot123'
EXPECT_CODE = int(sys.argv[2]) if len(sys.argv) > 2 else 97
MACHINE_ID = 'e2e-test-11112222-3333-4444-555566'
MACHINE_ID_OLD = 'e2e-old-99998888-7777-6666-555544'


def parse_fields(buf):
    fields = {}
    i, n = 0, len(buf)
    while i < n:
        tag, shift = 0, 0
        while i < n:
            b = buf[i]; i += 1
            tag |= (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                break
        fn, wt = tag >> 3, tag & 7
        if wt == 0:
            val, s = 0, 0
            while i < n:
                b = buf[i]; i += 1
                val |= (b & 0x7F) << s
                s += 7
                if not b & 0x80:
                    break
            fields[fn] = val
        elif wt == 2:
            ln, s = 0, 0
            while i < n:
                b = buf[i]; i += 1
                ln |= (b & 0x7F) << s
                s += 7
                if not b & 0x80:
                    break
            fields[fn] = buf[i:i + ln]; i += ln
        else:
            break
    return fields


async def read_until(r, state, want_title, timeout=15):
    import time as _t
    deadline = _t.monotonic() + timeout
    buf = state['buf']
    while True:
        chunk, off = Chunk.parse(buf, 0)
        if chunk is None:
            remain = deadline - _t.monotonic()
            if remain <= 0:
                raise TimeoutError(f'等待 {want_title} 超时')
            data = await asyncio.wait_for(r.read(65536), timeout=remain)
            if not data:
                raise ConnectionError('断开')
            buf += data
            continue
        buf = buf[off:]
        state['buf'] = buf
        title = chunk.subs[0].as_str() if chunk.subs else ''
        if title.lower() == want_title.lower():
            return chunk


async def main():
    r, w = await asyncio.open_connection(HOST, GAME_PORT)
    state = {'buf': b''}

    def send(cmd_id, title, proto=b''):
        w.write(build_response(cmd_id, title, proto))

    # 1. QueryBannedMachineRequest（cmd=2，与真实客户端一致）
    send(2, 'QueryBannedMachineRequest',
         pb_string(1, MACHINE_ID) + pb_string(2, MACHINE_ID_OLD))
    await w.drain()
    await read_until(r, state, 'QueryBannedMachineResponse')
    print('[1] QueryBannedMachineResponse 收到')

    # 2. login2_begin
    send(1, 'login2_begin',
         pb_string(1, USERNAME) + pb_string(2, 'windows-e2e')
         + pb_string(3, 'true') + pb_string(4, 'true'))
    await w.drain()
    chunk = await read_until(r, state, 'login2_challenge')
    f = parse_fields(chunk.subs[0].subs[0].data)
    salt = base64.b64decode(f[1])
    temp_sid = base64.b64decode(f[2])
    enc_key = base64.b64decode(f[3])
    pw_hash = hashlib.sha1(PASSWORD.encode() + salt).digest()
    k1 = hashlib.sha1(pw_hash + temp_sid).digest()
    session_key = bytes(b ^ k1[i % len(k1)] for i, b in enumerate(enc_key))
    digest = hmac.new(session_key, temp_sid, hashlib.sha1).digest()

    # 3. login2_response
    send(2, 'login2_response',
         pb_string(1, f[2]) + pb_string(2, base64.b64encode(digest).decode()))
    await w.drain()
    chunk = await read_until(r, state, 'login2_result')
    f = parse_fields(chunk.subs[0].subs[0].data)
    v = f.get(3, 0)
    code = v if isinstance(v, int) else int.from_bytes(v, 'little', signed=True)
    print(f'[2] login2_result code={code} (97=成功)')
    w.close()

    # 4. 查库核对
    await asyncio.sleep(1)
    import asyncpg
    conn = await asyncpg.connect(
        'postgresql://hng:hng_password@172.25.166.165:5432/hng')
    row = await conn.fetchrow(
        'SELECT player_id, username, ip, machine_id, machine_id_old, '
        'result, source FROM login_events ORDER BY id DESC LIMIT 1')
    print(f'[3] login_events 最新记录: {dict(row)}')
    await conn.close()

    ok = (row and row['username'] == USERNAME and row['ip'] == '127.0.0.1'
          and row['machine_id'] == MACHINE_ID
          and row['machine_id_old'] == MACHINE_ID_OLD
          and row['result'] == EXPECT_CODE and row['source'] == 'game')
    print('[4] 端到端验证: ' + ('PASS' if ok else 'FAIL'))
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    asyncio.run(main())
