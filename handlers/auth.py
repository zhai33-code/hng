# -*- coding: utf-8 -*-
"""登录认证 / 会话保活域（QueryServerInfo、login2 全流程、keepalive）。

从 realtest_server.dispatch 机械迁移，函数体保持原样；
统一签名 (sess, writer, cmd_id, req_type, proto)。
"""


import base64
import hashlib
import hmac
import secrets
import time

from protocol import (
    Chunk,
    RC,
    build_response,
    msg_login2_challenge,
    msg_login2_result,
    pb_int32,
    pb_int64,
    pb_parse,
    pb_string,
    )

from state import (
    STORE,
    log,
    )


async def handle_queryserverinfo(sess, writer, cmd_id, req_type, proto):
    # servertime 必须是数字毫秒时间戳字符串，客户端用 Number() 转换
    # 如果发送格式化日期字符串，Number() 返回 NaN，导致 serverTimeAsNumber 为 NaN
    # 进而使 DiscountModel 的 Timer delay 为 NaN，触发 RangeError #2066
    now_ms = str(int(time.time() * 1000))
    resp_proto = (pb_string(1, now_ms) + pb_int32(2, 0) + pb_int32(3, 1) +
                  pb_string(4, '173293') + pb_string(5, '') +
                  pb_string(6, '') + pb_string(7, ''))
    resp = build_response(cmd_id, 'QueryServerInfoResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} QueryServerInfoResponse (servertime={now_ms})')



async def handle_querybannedmachinerequest(sess, writer, cmd_id, req_type, proto):
    resp_proto = pb_int32(1, 0) + pb_string(2, '')  # isBanned=false, banReason=""
    resp = build_response(cmd_id, 'QueryBannedMachineResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} QueryBannedMachineResponse (not banned)')



async def handle_startlogin(sess, writer, cmd_id, req_type, proto):
    # 登录排队：容量未满立即 mayProceed=true（行为同旧版）；满员则入队，
    # 由 login_queue（断线/登出事件 + 1s 周期 pump）用原 cmdId 持续推送
    # LoginQueueUpdate，放行时发 mayProceed=true 解锁客户端请求队列。
    # LoginQueueUpdate.result 是 ResponseCodes 枚举：fail=0, ok=1
    # （必须设 1(ok)，否则 queueCallback 直接 return，登录流程中断）。
    import login_queue
    await login_queue.enter(sess, writer, cmd_id)



async def handle_login2_begin(sess, writer, cmd_id, req_type, proto):
    try:
        username = pb_parse(proto).get(1, b'').decode('utf-8', 'replace')
    except Exception:
        username = 'DemoPlayer'
    # 从数据库加载该账号密码；账号不存在或 DB 异常一律标记 user_exists=False
    # （响应阶段直接拒登）——不再回退任何固定密码，防止绕过认证。
    user_exists = False
    pw = b''  # 未命中时占位，响应阶段不会走到密码校验
    try:
        stored = await STORE.get_password(username)
        if stored is None:
            log(f'      [auth] user {username!r} NOT in database, will reject')
        else:
            pw = stored.encode('utf-8')
            user_exists = True
    except Exception as e:
        log(f'[DB] auth lookup failed ({e}), rejecting login')
    sess.username = username
    sess.pw = pw
    sess.user_exists = user_exists
    # salt 必须按账号固定（不用随机）：客户端"记住密码"保存的是
    # sha1(密码+salt)，再次登录时直接把该 hash 当密码参与认证，不再用明文。
    # salt 随机会导致第二次登录 hash 与服务器算的 sha1(密码+新salt) 不一致而拒登。
    salt = hashlib.sha1(username.encode('utf-8')).digest()[:16]
    temp_sid = secrets.token_bytes(16)
    session_key = secrets.token_bytes(20)
    pw_hash = hashlib.sha1(pw + salt).digest()
    k1 = hashlib.sha1(pw_hash + temp_sid).digest()
    enc = bytes(b ^ k1[i % len(k1)] for i, b in enumerate(session_key))
    sess.login_ctx = (salt, temp_sid, session_key)
    resp = build_response(cmd_id, 'login2_challenge',
                          msg_login2_challenge(base64.b64encode(salt).decode(),
                                               base64.b64encode(temp_sid).decode(),
                                               base64.b64encode(enc).decode()))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} login2_challenge user={username!r} exists={user_exists}')



async def handle_login2_response(sess, writer, cmd_id, req_type, proto):
    if sess.login_ctx is None:
        log('login2_response without challenge')
        return
    salt, temp_sid, session_key = sess.login_ctx
    # 账号不在数据库中：直接拒登（不回 challenge 逻辑，客户端收 wrong_password）
    if not sess.user_exists:
        code = RC['wrong_password']
        resp_proto = msg_login2_result(sess.username or 'DemoPlayer', code)
        resp = build_response(cmd_id, 'login2_result', resp_proto)
        log(f'S->C cmd={cmd_id:<4} login2_result -- wrong_password=98 (user not found)')
        writer.write(resp)
        await writer.drain()
        return
    f = {}
    digest = b''
    try:
        f = pb_parse(proto)
        digest_b64 = f.get(2, b'')
        digest = base64.b64decode(digest_b64)
    except Exception as e:
        log(f'      parse error: {e}, proto={proto.hex()}')
    expected = hmac.new(session_key, temp_sid, hashlib.sha1).digest()
    log(f'      login2_response proto fields: {f}')
    log(f'      digest(client) = {digest.hex()} ({len(digest)}B)')
    log(f'      expected(server) = {expected.hex()} ({len(expected)}B)')
    log(f'      salt = {salt.hex()}, temp_sid = {temp_sid.hex()}, session_key = {session_key.hex()}')
    if hmac.compare_digest(digest, expected):
        code = RC['login_success']
        note = 'login_success=97'
    else:
        code = RC['wrong_password']
        note = 'wrong_password=98'
    # currentplayer.id 用数据库真实 player_id（客户端战报界面按它
    # 匹配本局玩家行，恒 1 会让 DB id≥2 的玩家结算窗口不弹）；
    # f10/f11 官服口径：当前战争 id + 本场阵营选择（无选择发 0 →
    # 客户端弹 FactionSelectionWindow，join_war_request 落库后修正）
    pid = await sess.ensure_player() if code == RC['login_success'] else None
    faction = None
    war = None
    if pid:
        try:
            import war_campaign
            war = await war_campaign.ensure_war(STORE)
            if war is not None:
                sel = await STORE.get_player_war_faction(pid, war['id'])
                faction = int(sel) if sel is not None else 0
        except Exception as e:
            log(f'[DB] player war faction failed: {e}')
    resp_proto = msg_login2_result(sess.username or 'DemoPlayer', code,
                                   pid=pid or 1, faction=faction,
                                   war=war['id'] if war is not None else 0)
    resp = build_response(cmd_id, 'login2_result', resp_proto)
    log(f'S->C cmd={cmd_id:<4} login2_result -- {note}')
    log(f'      resp bytes ({len(resp)}B): {resp.hex()}')
    log(f'      proto bytes ({len(resp_proto)}B): {resp_proto.hex()}')
    try:
        echo, _ = Chunk.parse(resp, 0)
        echo_cmd = echo.as_int() if echo else None
        echo_title = echo.subs[0].as_str() if echo and echo.subs else None
        echo_proto = echo.subs[0].subs[0].data if echo and echo.subs and echo.subs[0].subs else None
        log(f'      round-trip: cmd={echo_cmd} title={echo_title!r} proto={echo_proto.hex() if echo_proto else None}')
        parsed = pb_parse(echo_proto) if echo_proto else None
        log(f'      proto fields: {parsed}')
        if parsed and 6 in parsed:
            psub = pb_parse(parsed[6])
            log(f'      player fields: {list(psub.keys())}')
            log(f'      currentplayer proto: {parsed[6].hex() if isinstance(parsed[6], bytes) else parsed[6]}')
    except Exception as e:
        log(f'      round-trip FAILED: {e}')
    writer.write(resp)
    await writer.drain()



async def handle_login2_postlogin(sess, writer, cmd_id, req_type, proto):
    # post-login 请求，返回 login2_postlogin_result (result=97 login_success)
    # 客户端收到后设置 loggedIn=true, sessionID, 并调用 startWarDataDownload()
    # login2_postlogin_result { required int64 sessionid=1; required string platform=2; required string deleteDate=3; required int32 result=4; }
    resp_proto = b''
    resp_proto += pb_int64(1, 1)              # sessionid
    resp_proto += pb_string(2, 'pc')          # platform
    resp_proto += pb_string(3, '')            # deleteDate (空=未标记删除)
    resp_proto += pb_int32(4, 97)             # result = 97 (login_success)
    resp = build_response(cmd_id, 'login2_postlogin_result', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} login2_postlogin_result (result=97 login_success)')



async def handle_keepalive(sess, writer, cmd_id, req_type, proto):
    pass


async def handle_logout(sess, writer, cmd_id, req_type, proto):
    """logout {dummy=1} → logoutresponse {msg=1}。

    LogoutCommand（菜单"退出登录"）清本地凭证后发送本请求；
    客户端无回调（走 addNewData 路径，无表注册 → 载荷不被消费），
    但响应必须回显以解锁请求队列——此前无 handler 走兜底
    HandlerErrorResponse，客户端 1s 内重发一次 logout（实测日志
    20:22:15/16 连发两条）后仍继续后续流程。msg=1 沿用 ok 语义。"""
    resp = build_response(cmd_id, 'logoutresponse', pb_int32(1, 1))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} logoutresponse (msg=1)')


HANDLERS = {
    'QueryServerInfo': handle_queryserverinfo,
    'QueryBannedMachineRequest': handle_querybannedmachinerequest,
    'StartLogin': handle_startlogin,
    'login2_begin': handle_login2_begin,
    'login2_response': handle_login2_response,
    'login2_postlogin': handle_login2_postlogin,
    'keepalive': handle_keepalive,
    'keepaliverequest': handle_keepalive,
    'keepaliveresponse': handle_keepalive,
    'login2_player_updated_response': handle_keepalive,
    'logout': handle_logout,
}
