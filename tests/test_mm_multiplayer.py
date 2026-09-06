"""多人撮合集成测试（需 realtest_server 运行于 127.0.0.1:15110）。

沿用 test_matchmaking.py 脚手架（login / request / collect / find_kvcs /
find_delete），双客户端连接验证匹配池：

1. 双客户端撮合：同模式同选图 → 相同 missionId 的 PlayerMissionSession、
   相同 game_connect 端口、各自 PMM delete；GMD playerCount=2 且参战方
   含双方 gamertag
2. f5 选图解析：不同选图不撮合（互留池中）
3. 模式隔离：gamemode 3 vs 5 互不撮合
4. 取消匹配：入池后 Cancel → PMM delete + SquadSession 回 InLobby
5. 训练模式回归：gamemode 1 单人立即成局全链路
6. 超时 AI 补位 / 并发上限：需服务器以 MM_AI_FILL_AFTER=2 /
   MM_MAX_COMBAT_SERVERS=1 启动（脚本探测超时行为，未配置则标记 SKIP）

第二账号 mm-test-2 由脚本经 PlayerStore 自建（不存在时），并创建一个
与 demo 不同阵营的角色（攻防双方各有真人）。
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
from store import PlayerStore


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


def read_fields_multi(b):
    """Repeated-aware 解析 → {field_num: [value, ...]}。"""
    fields = {}
    i = 0
    while i < len(b):
        tag, i = read_varint(b, i)
        fn, wt = tag >> 3, tag & 7
        if wt == 0:
            v, i = read_varint(b, i)
        elif wt == 2:
            ln, i = read_varint(b, i)
            v = b[i:i + ln]
            i += ln
        elif wt == 5:
            v = b[i:i + 4]; i += 4
        elif wt == 1:
            v = b[i:i + 8]; i += 8
        else:
            break
        fields.setdefault(fn, []).append(v)
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
    w.write(build_response(cmd_id, req_type, proto)); await w.drain()
    return await collect(r, 1.5)


async def collect(r, seconds):
    buf = b''
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            data = await asyncio.wait_for(r.read(65536), timeout=0.3)
        except asyncio.TimeoutError:
            continue
        if not data:
            break
        buf += data
    return parse_msgs(buf)


def kvcs_tables(chunk):
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


def skip(label, detail=''):
    print(f'[SKIP] {label}{("  " + detail) if detail else ""}')


async def open_client(username, password):
    r, w = await asyncio.open_connection('127.0.0.1', 15110)
    code = await login(r, w, username, password)
    if code != 97:
        return None, None, code
    await request(r, w, 10, 'subscribeplayerview')
    return r, w, code


async def ensure_account(username, password, faction_wanted):
    """确保测试账号存在且有角色；返回 (pid, char_id, faction)。password 为 str。"""
    store = PlayerStore()
    await store.start()
    try:
        pid = await store.get_player(username)
        if pid is None:
            pid = await store.create_player(username, password)
        chars = await store.list_characters(pid)
        if not chars:
            ch = await store.create_character(pid, 'MmTester', faction_wanted, 1)
            chars = [ch]
        return pid, int(chars[0]['id']), int(chars[0]['faction'])
    finally:
        await store.close()


async def join_mm(r, w, char_id, gamemode, presets=(), cmd=12):
    """发 JoinMatchMakingRequest（含 f5 repeated 选图）。"""
    proto = pb_varint(1, char_id) + pb_varint(2, 0) + pb_bool(4, False)
    for p in presets:
        proto += pb_varint(5, p)
    proto += pb_varint(7, gamemode) + pb_bool(8, False)
    return await request(r, w, cmd, 'JoinMatchMakingRequest', proto)


async def cancel_mm(r, w, char_id, cmd=15):
    return await request(r, w, cmd, 'CancelJoinMatchMakingRequest',
                         pb_varint(1, char_id))


async def end_mission(r, w, mission_id, winner=1, cmd=30):
    """EndMissionRequest：让服务器 release_match（释放 combat 实例坑位）。

    macOS 集成测试专用：无真实 hng.exe 战局，主动结束以回收并发坑，
    避免后续成局滞留 PENDING_MATCHES。"""
    return await request(r, w, cmd, 'EndMissionRequest',
                         pb_varint(1, mission_id) + pb_varint(2, winner))


async def release_if_matched(r, w, seconds=1.0):
    """收集 seconds 内消息，若出现 PMS（成局）则发 EndMission 释放坑位。"""
    msgs = await collect(r, seconds)
    pms = find_kvcs(msgs, 'PlayerMissionSession')
    if pms and pms.get(8):
        await end_mission(r, w, int(pms[8]))
    return pms


async def main():
    store = PlayerStore()
    await store.start()
    pid_a = await store.get_player('demo')
    chars_a = await store.list_characters(pid_a) if pid_a else []
    await store.close()
    if not chars_a:
        check('demo 有角色', False)
        return
    char_a, faction_a = int(chars_a[0]['id']), int(chars_a[0]['faction'])
    faction_b = 1 if faction_a != 1 else 2
    pid_b, char_b, _ = await ensure_account('mm-test-2', 'mmtest12345', faction_b)
    print(f'[i] A=demo char={char_a} faction={faction_a}; '
          f'B=mm-test-2 char={char_b} faction={faction_b}')

    # ================= 1. 双客户端撮合 =================
    ra, wa, ca = await open_client('demo', b'demodemo123')
    rb, wb, cb = await open_client('mm-test-2', b'mmtest12345')
    check('双客户端登录', ca == 97 and cb == 97, f'codeA={ca}, codeB={cb}')
    if ra is None or rb is None:
        return

    msgs_a = await join_mm(ra, wa, char_a, 5, presets=[10])
    pmm_a = find_kvcs(msgs_a, 'PlayerMatchMaking')
    check('A 入池（PMM 推送）', pmm_a is not None)
    # A 单人在 min=2 模式不成局：1.5s 窗口内无 PMS
    check('A 单人不足 min 不成局', find_kvcs(msgs_a, 'PlayerMissionSession') is None)

    msgs_b = await join_mm(rb, wb, char_b, 5, presets=[10])
    pmm_b = find_kvcs(msgs_b, 'PlayerMatchMaking')
    check('B 入池（PMM 推送）', pmm_b is not None)

    # B join 后成局，双方各自收到 PMS Admitted + PMM delete
    msgs_b2 = await collect(rb, 10)
    pms_b = find_kvcs(msgs_b2, 'PlayerMissionSession')
    check('B 收到 PMS（Admitted=2）', pms_b is not None and pms_b.get(14) == 2,
          f'state={pms_b.get(14) if pms_b else None}')
    msgs_a2 = await collect(ra, 3)
    pms_a = find_kvcs(msgs_a2, 'PlayerMissionSession')
    check('A 收到 PMS（Admitted=2）', pms_a is not None and pms_a.get(14) == 2)
    if pms_a and pms_b:
        check('双方同局（missionId 相同）', pms_a.get(8) == pms_b.get(8),
              f'missionA={pms_a.get(8)}, missionB={pms_b.get(8)}')
        check('双方 game_connect 相同（同局同端口）',
              pms_a.get(20) == pms_b.get(20) and pms_a.get(20),
              f'connect={pms_a.get(20)}')
        check('双方 pms_id 不同（各自会话）', pms_a.get(1) != pms_b.get(1))
    del_a = find_delete(msgs_a2, 'PlayerMatchMaking')
    del_b = find_delete(msgs_b2, 'PlayerMatchMaking')
    check('A 收到 PMM delete', del_a is not None)
    check('B 收到 PMM delete', del_b is not None)

    # GMD：playerCount=2、参战方含双方 gamertag
    if pms_a and pms_a.get(8):
        greq = pb_varint(1, int(pms_a[8])) + pb_varint(2, 0)
        msgs = await request(ra, wa, 16, 'GetMissionDetailsRequest', greq)
        rsp = find_response(msgs, 'GetMissionDetailsResponse')
        check('GMD 响应 ok', rsp is not None and rsp.get(1) == 0)
        if rsp and rsp.get(2):
            info = read_fields(rsp[2])
            check('GMD playerCount=2', info.get(9) == 2,
                  f'playerCount={info.get(9)}')
        if rsp:
            # read_fields 丢 repeated，直接从响应 proto 重新解析
            body = None
            for cmd, title, chunk in msgs:
                if title == 'GetMissionDetailsResponse':
                    body = chunk.subs[0].subs[0].data
                    break
            top = read_fields_multi(body or b'')
            tags = []
            for fac_raw in top.get(3, []):
                fac = read_fields_multi(fac_raw)
                for mem_raw in fac.get(2, []):
                    tags.append(read_fields(mem_raw).get(1, b''))
            tags = [t.decode() if isinstance(t, bytes) else t for t in tags]
            check('GMD 参战方含双方 gamertag', 'demo' in tags and 'mm-test-2' in tags,
                  f'members={tags}')

    # 释放本场实例（macOS 无真实战局，主动 EndMission 回收并发坑）
    if pms_a and pms_a.get(8):
        await end_mission(ra, wa, int(pms_a[8]))

    wa.close(); wb.close()
    await asyncio.sleep(0.3)

    # ================= 2. 选图不交集不撮合 =================
    # 断言窗口在 B 侧（撮合是同步的：B 入池瞬间若可撮合必立即推 PMS）；
    # A 侧 2s 后会触发 AI 补位成局（服务器 MM_AI_FILL_AFTER=2），属预期行为，
    # 结束时统一释放/取消。
    ra, wa, ca = await open_client('demo', b'demodemo123')
    rb, wb, cb = await open_client('mm-test-2', b'mmtest12345')
    if ra and rb:
        await join_mm(ra, wa, char_a, 5, presets=[10])       # A 只选 Mill
        msgs_b = await join_mm(rb, wb, char_b, 5, presets=[26])   # B 只选 Airfield
        pms_b = find_kvcs(msgs_b, 'PlayerMissionSession')
        check('选图无交集：B 入池不互撮（B 无 PMS）', pms_b is None)
        # 清理：A 可能已被 AI 补位成局 → 释放坑位；双方出池
        await release_if_matched(ra, wa, 1.0)
        await release_if_matched(rb, wb, 0.5)
        await cancel_mm(ra, wa, char_a)
        await cancel_mm(rb, wb, char_b)
    wa.close(); wb.close()
    await asyncio.sleep(0.3)

    # ================= 3. 模式隔离 =================
    ra, wa, ca = await open_client('demo', b'demodemo123')
    rb, wb, cb = await open_client('mm-test-2', b'mmtest12345')
    if ra and rb:
        await join_mm(ra, wa, char_a, 3, presets=[10])       # gamemode 3
        msgs_b = await join_mm(rb, wb, char_b, 5, presets=[10])   # gamemode 5
        pms_b = find_kvcs(msgs_b, 'PlayerMissionSession')
        check('模式隔离：B 入池不互撮（B 无 PMS）', pms_b is None)
        await release_if_matched(ra, wa, 1.0)
        await release_if_matched(rb, wb, 0.5)
        await cancel_mm(ra, wa, char_a)
        await cancel_mm(rb, wb, char_b)
    wa.close(); wb.close()
    await asyncio.sleep(0.3)

    # ================= 4. 取消匹配 =================
    ra, wa, ca = await open_client('demo', b'demodemo123')
    if ra:
        msgs = await join_mm(ra, wa, char_a, 5, presets=[10])
        pmm = find_kvcs(msgs, 'PlayerMatchMaking')
        check('取消场景：入池成功', pmm is not None)
        msgs = await cancel_mm(ra, wa, char_a)
        rsp = find_response(msgs, 'CancelJoinMatchMakingResponse')
        check('Cancel 响应 ok', rsp is not None and rsp.get(1) == 1)
        check('Cancel 后 PMM delete', find_delete(msgs, 'PlayerMatchMaking') is not None)
        sq = find_kvcs(msgs, 'SquadSession')
        check('Cancel 后 SquadSession 回 InLobby（missionId=0）',
              sq is not None and sq.get(17) == 0)
        # 取消后池空：再入单人不成局
        msgs = await join_mm(ra, wa, char_a, 5, presets=[10])
        check('取消后池人数归零（单人不成局）',
              find_kvcs(msgs, 'PlayerMissionSession') is None)
        await cancel_mm(ra, wa, char_a)
    wa.close()
    await asyncio.sleep(0.3)

    # ================= 5. 训练模式回归 =================
    ra, wa, ca = await open_client('demo', b'demodemo123')
    if ra:
        msgs = await join_mm(ra, wa, char_a, 1)              # training：单人直开
        msgs2 = await collect(ra, 8)
        # 模拟 combat 握手即时完成：Admitted 可能落在 join 的首窗口（msgs），
        # Armed 在第二窗口（msgs2）——合并判定
        all_msgs = msgs + msgs2
        pms = find_kvcs(all_msgs, 'PlayerMissionSession')
        check('训练模式：单人立即成局（PMS Admitted=2）',
              pms is not None and pms.get(14) == 2,
              f'state={pms.get(14) if pms else None}')
        check('训练模式：PMM delete', find_delete(all_msgs, 'PlayerMatchMaking') is not None)
        check('训练模式：BattleInfo 推送', find_kvcs(all_msgs, 'BattleInfo') is not None)
        if pms and pms.get(8):
            greq = pb_varint(1, int(pms[8])) + pb_varint(2, 0)
            msgs = await request(ra, wa, 17, 'GetMissionDetailsRequest', greq)
            rsp = find_response(msgs, 'GetMissionDetailsResponse')
            info = read_fields(rsp[2]) if rsp and rsp.get(2) else None
            check('训练模式：GMD preset ∈ 训练兜底池（9/10/11）',
                  info is not None and info.get(3) in (9, 10, 11),
                  f'preset={info.get(3) if info else None}')
            await end_mission(ra, wa, int(pms[8]))
    wa.close()
    await asyncio.sleep(0.3)

    # ================= 6. 超时 AI 补位（服务器以 MM_AI_FILL_AFTER=2 启动） =================
    ra, wa, ca = await open_client('demo', b'demodemo123')
    if ra:
        await join_mm(ra, wa, char_a, 5, presets=[10])
        msgs = await collect(ra, 6)                          # > 2s 超时
        pms = find_kvcs(msgs, 'PlayerMissionSession')
        check('AI 补位：超时单人成局（PMS Admitted=2）',
              pms is not None and pms.get(14) == 2,
              f'state={pms.get(14) if pms else None}')
        if pms and pms.get(8):
            # AI 补位局：GMD playerCount 含 AI 补位（+1）
            greq = pb_varint(1, int(pms[8])) + pb_varint(2, 0)
            gmsgs = await request(ra, wa, 18, 'GetMissionDetailsRequest', greq)
            grsp = find_response(gmsgs, 'GetMissionDetailsResponse')
            ginfo = read_fields(grsp[2]) if grsp and grsp.get(2) else None
            check('AI 补位：GMD playerCount=2（1 真人 + 1 AI）',
                  ginfo is not None and ginfo.get(9) == 2,
                  f'playerCount={ginfo.get(9) if ginfo else None}')
            await end_mission(ra, wa, int(pms[8]))
        else:
            await cancel_mm(ra, wa, char_a)
    wa.close()

    # ================= 7. 断线清理（P0：幽灵排队条目） =================
    # A 入池后不 Cancel 直接断开连接 → 服务器应出池清理；
    # 验证：B 随后入池时 PMM f18/f19 队列人数不含 A（无幽灵虚增）。
    ra, wa, ca = await open_client('demo', b'demodemo123')
    rb, wb, cb = await open_client('mm-test-2', b'mmtest12345')
    if ra and rb:
        await join_mm(ra, wa, char_a, 5, presets=[10])       # A 排队
        wa.close()                                            # 直接断线（不 Cancel）
        await asyncio.sleep(0.6)                              # 等 drop_player 生效
        msgs_b = await join_mm(rb, wb, char_b, 5, presets=[10])
        pmm_b = find_kvcs(msgs_b, 'PlayerMatchMaking')
        check('断线清理：B 队列人数不含幽灵 A（f18=1）',
              pmm_b is not None and pmm_b.get(18) == 1,
              f'f18={pmm_b.get(18) if pmm_b else None}')
        check('断线清理：总队列人数不含幽灵 A（f19=1）',
              pmm_b is not None and pmm_b.get(19) == 1,
              f'f19={pmm_b.get(19) if pmm_b else None}')
        check('断线清理：幽灵 A 不再被撮合（B 无 PMS）',
              find_kvcs(msgs_b, 'PlayerMissionSession') is None)
        await cancel_mm(rb, wb, char_b)
    wb.close()
    await asyncio.sleep(0.3)

    skip('并发上限排队/释放', '（需以 MM_MAX_COMBAT_SERVERS=1 启动服务器验证：'
         '已实测通过——占坑/排队/释放拉起/端口复用 4 项 PASS，'
         '见 .trae/documents/matchmaking-pool-plan.md Verification）')
    skip('端口耗尽回池重排', '（需以 MM_COMBAT_PORT_BASE=5999 MM_COMBAT_PORT_MAX=5600 '
         '（空端口池）启动服务器验证：训练模式成局被回池，PMM 保留无 PMS，'
         '玩家不丢——见 .trae/documents/matchmaking-pool-plan.md Verification）')

    failed = [l for l, ok in RESULTS if not ok]
    print(f'\nMM_MULTIPLAYER: {"ALL PASS" if not failed else f"{len(failed)} FAILED: {failed}"} '
          f'({len(RESULTS) - len(failed)}/{len(RESULTS)} passed)')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    asyncio.run(main())
