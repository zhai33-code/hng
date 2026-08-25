"""Regression test: 近期新增功能全链路（武器/载具皮肤、军服迷彩、头盔、解锁过滤）。

依赖：
- 运行中的 realtest_server（必须是最新代码，重启后生效）
- PostgreSQL 可用（PlayerStore 建号 + 写勋带解锁）

流程：唯一账号 → 登录 → 订阅视图 → 建苏军角色(pack 35) →
1) 未解锁买武器 → 期望解锁拒绝       （验证 _unlock_need_for_char 过滤）
2) 直写 DB 解锁全部勋带 12 级
3) 买武器 → 买皮肤 → 装备/卸下皮肤    （SetWeaponVariant 响应字段号 2）
4) 买载具 → 买皮肤 → 装备皮肤         （SetVehicleVariant 响应字段号 2）
5) 买军服迷彩 → 装备/卸下             （SetUniformVariant 响应字段号 2）
6) 买头盔 → 装备/卸下                 （SetUniformPart 响应字段号 2）
7) 验证 DB 新表行（weapon_variants / vehicle_variants / uniform_parts / uniform_variants）
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
from protocol import Chunk, build_response
from player_store import PlayerStore

# 静态数据（WarDatabase 推导；import 时会做模块级初始化）
import wardb
_RIBBON_LEVEL_XP = wardb._RIBBON_LEVEL_XP
_RIBBON_UPGRADES = wardb._RIBBON_UPGRADES

# ---------- protobuf helpers（同 test_e2e） ----------
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
        elif wt == 5:  # fixed32 / float
            fields[fn] = b[i:i + 4]
            i += 4
        elif wt == 1:  # fixed64 / double
            fields[fn] = b[i:i + 8]
            i += 8
        else:
            break  # 未知 wire type，停止避免死循环
    return fields


# ---------- protocol helpers ----------
async def login(r, w, username, password):
    req = build_response(1, 'login2_begin',
                         pb_string(1, username) + pb_string(2, 'reg-test') +
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
    """发送请求并收集所有消息，返回 [(cmd, title, chunk)]。"""
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
        cmd = chunk.as_int()
        title = chunk.subs[0].as_str() if chunk.subs else None
        out.append((cmd, title, chunk))
    return out


def kvcs_tables(chunk):
    """从消息 chunk 中提取 [(table_name, proto_data)]（KeyValueChangeSet 的 set 帧）。"""
    out = []

    def walk(c):
        if c.subs and len(c.subs) == 1 and c.subs[0].subs == []:
            out.append((c.as_str(), c.subs[0].data))
        for s in c.subs:
            walk(s)
    walk(chunk)
    return out


def find_response(msgs, resp_type):
    for cmd, title, chunk in msgs:
        if title == resp_type:
            body = chunk.subs[0].subs[0].data if chunk.subs[0].subs else b''
            return read_fields(body)
    return None


def find_kvcs(msgs, table):
    for cmd, title, chunk in msgs:
        if title != 'KeyValueChangeSet':
            continue
        for tn, data in kvcs_tables(chunk):
            if tn == table:
                return read_fields(data)
    return None


def kvcs_all(msgs, table):
    rows = []
    for cmd, title, chunk in msgs:
        if title != 'KeyValueChangeSet':
            continue
        for tn, data in kvcs_tables(chunk):
            if tn == table:
                rows.append(read_fields(data))
    return rows


RESULTS = []


def check(label, ok, detail=''):
    RESULTS.append((label, ok))
    print(f'[{"PASS" if ok else "FAIL"}] {label}{("  " + detail) if detail else ""}')


# ---------- main ----------
async def main():
    username = f'regtest{int(time.time())}'
    password = 'regpass123'
    store = PlayerStore()
    await store.start()
    try:
        await store.create_player(username, password)
    except Exception as e:
        print(f'[!] create_player: {e}')
    pid = await store.get_player(username)
    print(f'[i] test account: {username} (pid={pid})')
    await store.close()

    r, w = await asyncio.open_connection('127.0.0.1', 15110)
    code = await login(r, w, username, password.encode())
    check('登录成功', code == 97, f'code={code}')
    if code != 97:
        return

    await request(r, w, 10, 'subscribeplayerview')
    msgs = await request(r, w, 11, 'subscribesoldierview')

    # 建苏军角色（pack 35 = faction 3, template 1）
    creq = pb_string(1, 'RegSoldier') + pb_varint(2, 35) + pb_varint(3, 0) + \
        pb_varint(4, 0) + pb_varint(5, 1)
    msgs = await request(r, w, 12, 'CreateCharacterRequest', creq)
    rsp = find_response(msgs, 'CreateCharacterResponse')
    check('创建角色响应', rsp is not None and rsp.get(1) == 1)
    char = find_kvcs(msgs, 'character')
    char_id = int(char[1]) if char else None
    check('创建角色推送 character', char is not None, f'charId={char_id}')

    # ---- 1. 未解锁时买 PPSh-41 (template 5) → 期望解锁拒绝 ----
    wreq = pb_varint(1, char_id) + pb_varint(3, 5) + pb_string(4, 'PPSh-41') + \
        pb_varint(6, 0) + pb_varint(10, 1)
    msgs = await request(r, w, 13, 'CreateWeaponVariantRequest', wreq)
    rsp = find_response(msgs, 'CreateWeaponVariantResponse')
    unlocked_by_default = (rsp is not None and rsp.get(1) == 1)
    check('未解锁买武器被拒绝', not unlocked_by_default,
          'response=0(拒绝)' if not unlocked_by_default else '默认已解锁(跳过拒绝断言)')

    # ---- 2. 直写 DB 解锁全部勋带 12 级 ----
    store = PlayerStore()
    await store.start()
    max_xp = max(_RIBBON_LEVEL_XP.values())
    ribbons = sorted({rid for (rid, _lv) in _RIBBON_UPGRADES.values()})
    for rid in ribbons:
        try:
            await store.set_ribbon_xp_floor(pid, char_id, rid, max_xp)
        except Exception as e:
            print(f'[!] ribbon {rid}: {e}')
    await store.close()
    check(f'直写勋带解锁（{len(ribbons)} 个 12 级）', len(ribbons) > 0)

    # ---- 3. 买武器 PPSh-41 ----
    msgs = await request(r, w, 14, 'CreateWeaponVariantRequest', wreq)
    rsp = find_response(msgs, 'CreateWeaponVariantResponse')
    check('解锁后买武器成功', rsp is not None and rsp.get(1) == 1)
    wrow = find_kvcs(msgs, 'weapon')
    weapon_id = int(wrow[1]) if wrow else None
    check('武器推送 weapon', wrow is not None, f'weaponId={weapon_id}')

    # ---- 4. 买武器皮肤（weaponTemplateId=0 + weaponId=已有武器 + variantTemplateId=3）----
    sreq = pb_varint(1, char_id) + pb_varint(2, weapon_id) + pb_varint(3, 0) + \
        pb_varint(6, 3) + pb_varint(10, 1)
    msgs = await request(r, w, 15, 'CreateWeaponVariantRequest', sreq)
    rsp = find_response(msgs, 'CreateWeaponVariantResponse')
    check('买武器皮肤成功', rsp is not None and rsp.get(1) == 1)
    wv = find_kvcs(msgs, 'WeaponVariant')
    variant_id = int(wv[1]) if wv else None
    check('皮肤推送 WeaponVariant', wv is not None, f'variantId={variant_id}')
    wrow2 = find_kvcs(msgs, 'weapon')
    check('皮肤购买后重推 weapon（防卡loading）', wrow2 is not None)

    # ---- 5. 装备皮肤 SetWeaponVariantRequest（响应字段号 2）----
    ereq = pb_varint(1, weapon_id) + pb_varint(2, variant_id)
    msgs = await request(r, w, 16, 'SetWeaponVariantRequest', ereq)
    rsp = find_response(msgs, 'SetWeaponVariantResponse')
    check('装备武器皮肤（字段2响应）', rsp is not None and rsp.get(2) == 1)
    wrow3 = find_kvcs(msgs, 'weapon')
    check('装备后 weapon 重推 variantId 更新',
          wrow3 is not None and wrow3.get(15) == variant_id)

    # 卸下皮肤
    ereq = pb_varint(1, weapon_id) + pb_varint(2, 0)
    msgs = await request(r, w, 17, 'SetWeaponVariantRequest', ereq)
    rsp = find_response(msgs, 'SetWeaponVariantResponse')
    check('卸下武器皮肤', rsp is not None and rsp.get(2) == 1)

    # ---- 6. 买载具 variant 26（无解锁需求 + 有默认弹药）----
    vreq = pb_varint(1, char_id) + pb_varint(2, 26) + pb_varint(3, 0) + \
        pb_varint(8, 1)
    msgs = await request(r, w, 18, 'CreateVehicleVariantRequest', vreq)
    rsp = find_response(msgs, 'CreateVehicleVariantResponse')
    check('买载具成功', rsp is not None and rsp.get(1) == 1)
    vrow = find_kvcs(msgs, 'Vehicle')
    vehicle_id = int(vrow[1]) if vrow else None
    check('载具推送 Vehicle', vrow is not None, f'vehicleId={vehicle_id}')
    vammo = find_kvcs(msgs, 'VehicleAmmo')
    check('载具默认弹药推送 VehicleAmmo', vammo is not None)

    if vehicle_id is None:
        # 前置失败：跳过载具皮肤/装备步骤，但继续迷彩/头盔
        check('买载具成功(前置失败跳过后续)', False, 'vehicle_id=None')
    else:
        # ---- 7. 买载具皮肤（vehicleId + vehicleVariantTemplateId=0 + extra=21）----
        vsreq = pb_varint(1, char_id) + pb_varint(2, 0) + pb_varint(3, 0) + \
            pb_varint(4, vehicle_id) + pb_varint(5, 21) + pb_varint(6, 0) + \
            pb_varint(7, 0) + pb_varint(8, 1)
        msgs = await request(r, w, 19, 'CreateVehicleVariantRequest', vsreq)
        rsp = find_response(msgs, 'CreateVehicleVariantResponse')
        check('买载具皮肤成功', rsp is not None and rsp.get(1) == 1)
        vv = find_kvcs(msgs, 'VehicleVariant')
        vv_id = int(vv[1]) if vv else None
        check('载具皮肤推送 VehicleVariant', vv is not None, f'vvId={vv_id}')

        # ---- 8. 装备载具皮肤 SetVehicleVariantRequest（响应字段号 2）----
        vereq = pb_varint(1, vehicle_id) + pb_varint(2, vv_id)
        msgs = await request(r, w, 20, 'SetVehicleVariantRequest', vereq)
        rsp = find_response(msgs, 'SetVehicleVariantResponse')
        check('装备载具皮肤（字段2响应）', rsp is not None and rsp.get(2) == 1)
        vrow2 = find_kvcs(msgs, 'Vehicle')
        check('装备后 Vehicle 重推 variantId 更新',
              vrow2 is not None and vrow2.get(9) == vv_id)

    # ---- 9. 买军服迷彩 CreateUniformVariantRequest ----
    ureq = pb_varint(1, char_id) + pb_varint(5, 1) + pb_varint(6, 0) + pb_varint(8, 1)
    msgs = await request(r, w, 21, 'CreateUniformVariantRequest', ureq)
    rsp = find_response(msgs, 'CreateUniformVariantResponse')
    check('买军服迷彩成功', rsp is not None and rsp.get(1) == 1)
    uv = find_kvcs(msgs, 'UniformVariant')
    uv_id = int(uv[1]) if uv else None
    check('迷彩推送 UniformVariant', uv is not None, f'uvId={uv_id}')

    # ---- 10. 装备迷彩 SetUniformVariantRequest（响应字段号 2）----
    suv = pb_varint(1, char_id) + pb_varint(2, uv_id)
    msgs = await request(r, w, 22, 'SetUniformVariantRequest', suv)
    rsp = find_response(msgs, 'SetUniformVariantResponse')
    check('装备迷彩（字段2响应）', rsp is not None and rsp.get(2) == 1)
    char2 = find_kvcs(msgs, 'character')
    check('装备后 character 重推 uniformVariantId 更新',
          char2 is not None and char2.get(17) == uv_id)

    # ---- 11. 买头盔 PurchaseUniformPartRequest ----
    hp = pb_varint(1, char_id) + pb_varint(2, 1) + pb_varint(3, 1) + \
        pb_varint(4, 0) + pb_varint(6, 1)
    msgs = await request(r, w, 23, 'PurchaseUniformPartRequest', hp)
    rsp = find_response(msgs, 'PurchaseUniformPartResponse')
    check('买头盔成功', rsp is not None and rsp.get(1) == 1)
    up = find_kvcs(msgs, 'UniformPart')
    up_id = int(up[1]) if up else None
    check('头盔推送 UniformPart', up is not None, f'upId={up_id}')

    # ---- 12. 装备头盔 SetUniformPartRequest（响应字段号 2）----
    sup = pb_varint(1, char_id) + pb_varint(2, up_id) + pb_varint(3, 1)
    msgs = await request(r, w, 24, 'SetUniformPartRequest', sup)
    rsp = find_response(msgs, 'SetUniformPartResponse')
    check('装备头盔（字段2响应）', rsp is not None and rsp.get(2) == 1)
    char3 = find_kvcs(msgs, 'character')
    check('装备后 character 重推 headgearId 更新',
          char3 is not None and char3.get(18) == up_id)

    # 卸下头盔
    sup = pb_varint(1, char_id) + pb_varint(2, 0) + pb_varint(3, 1)
    msgs = await request(r, w, 25, 'SetUniformPartRequest', sup)
    rsp = find_response(msgs, 'SetUniformPartResponse')
    check('卸下头盔', rsp is not None and rsp.get(2) == 1)

    w.close()
    await asyncio.sleep(0.2)

    # ---- 13. DB 验证新表行 ----
    store = PlayerStore()
    await store.start()
    try:
        wvs = await store.list_weapon_variants(pid)
        vvs = await store.list_vehicle_variants(pid)
        ups = await store.list_uniform_parts(pid)
        uvs = await store.list_uniform_variants(pid)
        check('DB weapon_variants 有行', len(wvs) >= 1, f'{len(wvs)}')
        check('DB vehicle_variants 有行', len(vvs) >= 1, f'{len(vvs)}')
        check('DB uniform_parts 有行', len(ups) >= 1, f'{len(ups)}')
        check('DB uniform_variants 有行', len(uvs) >= 1, f'{len(uvs)}')
    except Exception as e:
        check('DB 新表查询', False, str(e))
    await store.close()

    failed = [l for l, ok in RESULTS if not ok]
    print(f'\nREGRESSION: {"ALL PASS" if not failed else f"{len(failed)} FAILED: {failed}"} '
          f'({len(RESULTS) - len(failed)}/{len(RESULTS)} passed)')
    sys.exit(1 if failed else 0)


asyncio.run(main())
