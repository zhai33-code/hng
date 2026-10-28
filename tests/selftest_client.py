"""Self-test: speak the HnG chunk protocol against hngserver.py like the real client."""
import asyncio
import base64
import hashlib
import hmac
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 2026-08-21 归档至 tests/
from protocol import Chunk, build_response  # reuse codec


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


def pb_bool(field, value):
    return bytes([field << 3 | 0, 1 if value else 0])


def pb_msg_login2_begin(username):
    m = pb_string(1, username)
    m += pb_string(2, 'windows-test')
    m += pb_bool(3, True)
    m += pb_bool(4, True)
    return m


async def login_attempt(username, password):
    r, w = await asyncio.open_connection('127.0.0.1', 15110)
    print(f'[client] connected ({username!r})')

    # 1. login2_begin
    req = build_response(1, 'login2_begin', pb_msg_login2_begin(username))
    w.write(req)
    await w.drain()
    print('[client] -> login2_begin')

    # 2. read challenge
    data = await r.read(65536)
    chunk, _ = Chunk.parse(data, 0)
    title = chunk.subs[0].as_str()
    proto = chunk.subs[0].subs[0].data
    print(f'[client] <- {title} ({len(proto)}B proto)')
    assert title == 'login2_challenge', title

    f = read_fields(proto)
    salt = base64.b64decode(f[1])
    temp_sid = base64.b64decode(f[2])
    enc_key = base64.b64decode(f[3])

    # client-side computation (mirrors StandaloneLoginCommand)
    pw_hash = hashlib.sha1(password + salt).digest()
    k1 = hashlib.sha1(pw_hash + temp_sid).digest()
    session_key = bytes(b ^ k1[i % len(k1)] for i, b in enumerate(enc_key))
    digest = hmac.new(session_key, temp_sid, hashlib.sha1).digest()
    print(f'[client] digest={base64.b64encode(digest).decode()[:20]}...')

    # 3. login2_response (field 1 = tempSessionid, NOT the salt)
    m = pb_string(1, f[2]) + pb_string(2, base64.b64encode(digest).decode())
    w.write(build_response(2, 'login2_response', m))
    await w.drain()
    print('[client] -> login2_response')

    # 4. read result
    data = await r.read(65536)
    chunk, _ = Chunk.parse(data, 0)
    title = chunk.subs[0].as_str()
    print(f'[client] <- {title}')
    assert title == 'login2_result', title
    resp_code = read_fields(chunk.subs[0].subs[0].data)[3]
    print(f'[client] login result code = {resp_code}')
    return r, w, resp_code


def read_fields(b):
    fields = {}
    i = 0
    while i < len(b):
        tag = b[i]; i += 1
        fn, wt = tag >> 3, tag & 7
        if wt == 2:
            ln = b[i]; i += 1
            fields[fn] = b[i:i + ln]; i += ln
        elif wt == 0:
            fields[fn] = b[i]; i += 1
    return fields


async def main():
    # --- case 1: correct password
    r, w, code = await login_attempt('TestUser', b'demodemo123')
    assert code == 97, f'expected 97 login_success, got {code}'
    print('[client] case1 OK: login_success')

    # 5. try a subscribe like the client does post-login
    w.write(build_response(3, 'subscribeplayerview'))
    await w.drain()
    data = await r.read(65536)
    chunk, _ = Chunk.parse(data, 0)
    print(f'[client] <- {chunk.subs[0].as_str()} (cmdId={chunk.as_int()})')
    assert chunk.subs[0].as_str() == 'subscriberesponse'
    w.close()

    # --- case 2: wrong password -> expect 98
    r2, w2, code2 = await login_attempt('TestUser', b'wrong-password')
    assert code2 == 98, f'expected 98 wrong_password, got {code2}'
    print('[client] case2 OK: wrong_password rejected')
    w2.close()

    print('\nSELF-TEST PASS: chunk codec + login2(digest verified) + subscribe all working')


asyncio.run(main())
