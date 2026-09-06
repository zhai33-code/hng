# -*- coding: utf-8 -*-
"""自动维修（AutoRepair）三态修复 e2e 验证。

场景：登录 demo → 订阅 soldier view → 对一把真实武器：
  1. DB 预置 condition=37.5 → 发 SetWeaponAutoRepairRequest(autoRepair=2 金条)
  2. 断言响应 ok，且重推的 weapon KVCS f13=2（三态原样回传）、f12=37.5（耐久不重置）
  3. 重连重登 → 初始 weapon 推送 f13=2（重登状态保持）
  4. 清理：auto_repair 还原 0、condition 还原
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
from protocol import Chunk, build_response  # noqa: E402


def pb_varint(field, value):
    out = bytearray([field << 3 | 0])
    v = value
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | 0x80 if v else b)
        if not v:
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


def read_pb(b):
    """全量 protobuf 解析（varint/fixed64/fixed32/bytes）。"""
    fields, i = {}, 0
    while i < len(b):
        tag, i = _varint(b, i)
        fn, wt = tag >> 3, tag & 7
        if wt == 0:
            fields[fn], i = _varint(b, i)
        elif wt == 1:
            fields[fn] = struct.unpack('<q', b[i:i + 8])[0]; i += 8
        elif wt == 5:
            fields[fn] = struct.unpack('<f', b[i:i + 4])[0]; i += 4
        elif wt == 2:
            ln, i = _varint(b, i)
            fields[fn] = b[i:i + ln]; i += ln
        else:
            raise ValueError(f'wiretype {wt}')
    return fields


def _varint(b, i):
    v, shift = 0, 0
    while True:
        byte = b[i]; i += 1
        v |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return v, i
        shift += 7


async def login(r, w, password):
    w.write(build_response(1, 'login2_begin',
                           pb_string(1, 'demo') + pb_string(2, 'windows-test') +
                           pb_bool(3, True) + pb_bool(4, True)))
    await w.drain()
    data = await r.read(65536)
    chunk, _ = Chunk.parse(data, 0)
    f = read_pb(chunk.subs[0].subs[0].data)
    salt = base64.b64decode(f[1]); temp_sid = base64.b64decode(f[2])
    enc_key = base64.b64decode(f[3])
    pw_hash = hashlib.sha1(password + salt).digest()
    k1 = hashlib.sha1(pw_hash + temp_sid).digest()
    session_key = bytes(x ^ k1[i % len(k1)] for i, x in enumerate(enc_key))
    digest = hmac.new(session_key, temp_sid, hashlib.sha1).digest()
    m = pb_string(1, f[2]) + pb_string(2, base64.b64encode(digest).decode())
    w.write(build_response(2, 'login2_response', m)); await w.drain()
    data = await r.read(65536)
    chunk, _ = Chunk.parse(data, 0)
    return read_pb(chunk.subs[0].subs[0].data)[3]


async def request(r, w, cmd_id, req_type, proto=b''):
    w.write(build_response(cmd_id, req_type, proto)); await w.drain()
    buf = b''
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
            if c.subs:
                for s in c.subs:
                    yield from collect(s)
            else:
                yield c.data

        if chunk.subs:
            title = chunk.subs[0].as_str()
            leaves = list(collect(chunk.subs[0]))
            out.append((chunk.as_int(), title, leaves[-1] if leaves else b''))
        else:
            out.append((chunk.as_int(), None, b''))
    return out


def weapon_pushes(msgs):
    """从消息流里取 weapon 表 KVCS 的行 proto。"""
    out = []
    for cmd, title, body in msgs:
        if title == 'KeyValueChangeSet' and body[:1] == b'\x08':
            f = read_pb(body)
            if 13 in f and 3 in f and 2 in f:  # weapon 行：f13 autoRepair
                out.append(f)
    return out


async def main():
    from store import PlayerStore
    store = PlayerStore()
    await store.start()

    # 取 demo 玩家一把武器做测试
    async with store.pool.acquire() as conn:
        pid = await conn.fetchval(
            "SELECT id FROM players WHERE username = 'demo'")
        wid = await conn.fetchval(
            'SELECT id FROM weapons WHERE player_id = $1 ORDER BY id LIMIT 1', pid)
        orig_cond = await conn.fetchval(
            'SELECT condition FROM weapons WHERE id = $1', wid)
        orig_ar = await conn.fetchval(
            'SELECT auto_repair FROM weapons WHERE id = $1', wid)
    print(f'[ar] player={pid} weapon={wid} '
          f'orig condition={orig_cond} auto_repair={orig_ar}')

    # 预置：耐久 37.5、自动维修关
    async with store.pool.acquire() as conn:
        await conn.execute('UPDATE weapons SET condition = 37.5, auto_repair = 0 '
                           'WHERE id = $1', wid)

    # --- 会话 1：登录 + 订阅 + 勾选金条自动维修 (autoRepair=2) ---
    r, w = await asyncio.open_connection('127.0.0.1', 15110)
    code = await login(r, w, b'demodemo123')
    assert code == 97, code
    await request(r, w, 3, 'subscribeplayerview')
    msgs = await request(r, w, 4, 'subscribesoldierview')
    init_push = [f for f in weapon_pushes(msgs) if f[1] == wid]
    assert init_push and init_push[0][13] == 0, '初始推送 autoRepair 应为 0'
    print(f'[ar] 初始推送 f13={init_push[0][13]} f12={init_push[0][12]:.1f} (期望 0/37.5)')
    assert abs(init_push[0][12] - 37.5) < 0.01, '初始推送耐久应为 37.5'

    # SetWeaponAutoRepairRequest { weaponId=2; autoRepair=12 } ← 字段号特殊
    req = pb_varint(2, wid) + pb_varint(12, 2)
    msgs = await request(r, w, 10, 'SetWeaponAutoRepairRequest', req)
    resp = [b for _, t, b in msgs if t == 'SetWeaponAutoRepairResponse']
    assert resp and read_pb(resp[0])[2] == 1, '响应应为 ok'
    push = [f for f in weapon_pushes(msgs) if f[1] == wid]
    assert push, '勾选后应重推 weapon KVCS'
    print(f'[ar] 勾选金条后重推 f13={push[0][13]} f12={push[0][12]:.1f} (期望 2/37.5)')
    assert push[0][13] == 2, f'autoRepair 应原样回传 2，实际 {push[0][13]}'
    assert abs(push[0][12] - 37.5) < 0.01, f'耐久不应被重置，实际 {push[0][12]}'
    w.close()
    print('[ar] PASS: 勾选金条 → 三态回传 2 + 耐久保持')

    # --- 会话 2：重登验证持久化 ---
    await asyncio.sleep(0.3)
    r2, w2 = await asyncio.open_connection('127.0.0.1', 15110)
    code = await login(r2, w2, b'demodemo123')
    assert code == 97, code
    await request(r2, w2, 3, 'subscribeplayerview')
    msgs = await request(r2, w2, 4, 'subscribesoldierview')
    init_push = [f for f in weapon_pushes(msgs) if f[1] == wid]
    assert init_push and init_push[0][13] == 2, '重登后初始推送 autoRepair 应为 2'
    print(f'[ar] 重登初始推送 f13={init_push[0][13]} f12={init_push[0][12]:.1f} (期望 2/37.5)')
    assert abs(init_push[0][12] - 37.5) < 0.01
    w2.close()
    print('[ar] PASS: 重登后勾选状态保持')

    # --- 清理：还原 DB ---
    async with store.pool.acquire() as conn:
        await conn.execute('UPDATE weapons SET condition = $2, auto_repair = $3 '
                           'WHERE id = $1', wid, orig_cond, orig_ar)
    await store.close()
    print('\n[ar] ALL PASS: autoRepair 三态回传 + 耐久保持 + 重登持久化')


asyncio.run(main())
