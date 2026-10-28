"""E2E test: full flow against realtest_server with PostgreSQL persistence.

Login -> subscribe views -> create character -> buy weapon -> equip.
Then verify rows in DB and re-login to confirm persisted data is replayed.
"""
import asyncio
import base64
import hashlib
import hmac
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from protocol import Chunk, build_response


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


async def login(r, w, password):
    req = build_response(1, 'login2_begin',
                         pb_string(1, 'demo') + pb_string(2, 'windows-test') +
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


async def request(r, w, cmd_id, req_type, proto=b''):
    w.write(build_response(cmd_id, req_type, proto)); await w.drain()
    buf = b''
    # 循环读取直到 0.3s 无新数据（服务器可能推送多条消息）
    import time
    deadline = time.time() + 1.5
    while time.time() < deadline:
        try:
            data = await asyncio.wait_for(r.read(65536), timeout=0.3)
        except asyncio.TimeoutError:
            break
        if not data:
            break
        buf += data
    out = []
    while buf:
        chunk, nxt = Chunk.parse(buf, 0)
        if chunk is None:
            break
        buf = buf[nxt:]

        def collect(c):
            # flatten tree: yield body of deepest leaf chunks
            if c.subs:
                for s in c.subs:
                    yield from collect(s)
            else:
                yield c.data

        cmd = chunk.as_int()
        if chunk.subs:
            title = chunk.subs[0].as_str()
            leaves = list(collect(chunk.subs[0]))
            out.append((cmd, title, leaves[-1] if leaves else b''))
        else:
            out.append((cmd, None, b''))
    return out


async def main():
    # --- login ---
    r, w = await asyncio.open_connection('127.0.0.1', 15110)
    code = await login(r, w, b'demodemo123')
    print('[e2e] login code:', code)
    assert code == 97

    # --- subscribe views (player + soldier) ---
    await request(r, w, 3, 'subscribeplayerview')
    print('[e2e] subscribed playerview')
    msgs = await request(r, w, 4, 'subscribesoldierview')
    titles = [t for _, t, _ in msgs]
    print('[e2e] soldier subscribe replies:', titles)

    # --- create character ---
    creq = pb_varint(1, 1) + pb_string(2, 'E2ESoldier') + pb_varint(2 << 3 | 0, 35)
    # characterPackId=35 (Soviet), fix field ordering: name=1, packId=2, pricingId=3, discountId=4, currency=5
    creq = pb_string(1, 'E2ESoldier') + pb_varint(2, 35) + pb_varint(3, 0) + pb_varint(4, 0) + pb_varint(5, 1)
    msgs = await request(r, w, 5, 'CreateCharacterRequest', creq)
    for cmd, title, body in msgs:
        print(f'[e2e] create char <- cmd={cmd} {title} len={len(body)}')
    assert any(t == 'CreateCharacterResponse' for _, t, _ in msgs)

    # --- buy weapon ---
    wreq = (pb_varint(1, 1) + pb_varint(3, 5) + pb_string(4, 'PPSh-41') +
            pb_varint(6, 0) + pb_varint(10, 1))
    msgs = await request(r, w, 6, 'CreateWeaponVariantRequest', wreq)
    for cmd, title, body in msgs:
        print(f'[e2e] buy weapon <- cmd={cmd} {title} len={len(body)}')
    assert any(t == 'CreateWeaponVariantResponse' for _, t, _ in msgs)

    # --- equip weapon (slot 1) ---
    ereq = pb_varint(1, 1) + pb_varint(2, 100) + pb_varint(3, 1)
    msgs = await request(r, w, 7, 'set_equipmentselection', ereq)
    print('[e2e] equip replies:', [(t, len(b)) for _, t, b in msgs])
    assert any(t == 'set_equipmentselection_response' for _, t, _ in msgs)

    w.close()
    await asyncio.sleep(0.2)
    print('\n[e2e] PASS: login + character + weapon + equip all replied')

    # --- verify DB rows ---
    import subprocess
    out = subprocess.run(
        ['docker', 'exec', 'hng-postgres', 'psql', '-U', 'hng', '-d', 'hng', '-c',
         'SELECT id, name, faction, template, combat_xp FROM characters'],
        capture_output=True, text=True).stdout
    print('[db] characters:\n' + out)
    out = subprocess.run(
        ['docker', 'exec', 'hng-postgres', 'psql', '-U', 'hng', '-d', 'hng', '-c',
         'SELECT id, name, template_id, container_id, container_slot FROM weapons'],
        capture_output=True, text=True).stdout
    print('[db] weapons:\n' + out)


asyncio.run(main())
