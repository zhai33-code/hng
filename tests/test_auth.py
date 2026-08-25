"""Auth persistence E2E: DB-backed password check against running server.

1. demo/demodemo123 -> success (user exists in DB)
2. nobody/xxx       -> fail (user not in DB, no auto-create)
3. add tester       -> tester/secret123 success
4. set_password     -> old password fails, new password succeeds
"""
import asyncio
import base64
import hashlib
import hmac
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from protocol import Chunk, build_response


def pb_varint(field, value):
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


def read_fields(b):
    fields = {}
    i = 0
    while i < len(b):
        tag = b[i]; i += 1
        fn, wt = tag >> 3, tag & 7
        if wt == 2:
            ln = 0; shift = 0
            while True:
                c = b[i]; i += 1
                ln |= (c & 0x7F) << shift; shift += 7
                if not c & 0x80:
                    break
            fields[fn] = b[i:i + ln]; i += ln
        elif wt == 0:
            v = 0; shift = 0
            while True:
                c = b[i]; i += 1
                v |= (c & 0x7F) << shift; shift += 7
                if not c & 0x80:
                    break
            fields[fn] = v
    return fields


async def login(username, password):
    r, w = await asyncio.open_connection('127.0.0.1', 15110)
    req = build_response(1, 'login2_begin',
                         pb_string(1, username) + pb_string(2, 'auth-test') +
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
    w.close()
    return code


def expect(label, code, want):
    ok = code == want
    print(f'{"[PASS]" if ok else "[FAIL]"} {label}: code={code} (want {want})')
    return ok


async def main():
    results = []
    results.append(expect('demo/demodemo123 (existing user)',
                          await login('demo', b'demodemo123'), 97))
    results.append(expect('nobody/whatever (not in DB)',
                          await login('nobody', b'whatever'), 98))
    await login('nobody', b'whatever')  # ensure no auto-create
    # add tester via DB directly (simulates add_user.py)
    import sys as _s
    from player_store import PlayerStore
    store = PlayerStore()
    await store.start()
    pid = await store.create_player('tester', 'secret123')
    print(f'[+] created tester: pid={pid}')
    await store.close()
    results.append(expect('tester/secret123 (just added)',
                          await login('tester', b'secret123'), 97))
    results.append(expect('tester/wrongpw',
                          await login('tester', b'wrongpw'), 98))
    # set_password then verify
    store = PlayerStore()
    await store.start()
    await store.set_password('tester', 'newpass')
    await store.close()
    results.append(expect('tester/secret123 (after change)',
                          await login('tester', b'secret123'), 98))
    results.append(expect('tester/newpass (after change)',
                          await login('tester', b'newpass'), 97))
    print('\nAUTH-TEST:', 'ALL PASS' if all(results) else 'SOME FAILED')


asyncio.run(main())
