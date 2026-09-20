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



async def _record_login_event(sess, result, pid=None):
    """登录结果写 login_events（审计：IP/机器码，成功失败都记）。

    失败仅记日志不影响登录流程。"""
    if STORE is None or STORE.pool is None:
        return
    try:
        await STORE.record_login_event(
            player_id=pid, username=sess.username or '',
            ip=sess.remote_ip, result=result,
            machine_id=sess.machine_id or '',
            machine_id_old=sess.machine_id_old or '')
    except Exception as e:
        log(f'[DB] record_login_event failed: {e}')


async def handle_querybannedmachinerequest(sess, writer, cmd_id, req_type, proto):
    # 客户端每次启动登录前上报本机机器码（hngsync.exe 生成的 36 字符类 GUID），
    # 挂到 session 供 login2 结果一起落库（login_events.machine_id）。
    # 机器码封禁（machine_bans 表，管理端"连带封禁"写入）：命中即回
    # isBanned=true，客户端 FrontScreenMediator 直接弹封禁提示并中止同步。
    try:
        f = pb_parse(proto)
        mid = (f.get(1, b'') or b'').decode('utf-8', 'replace')
        mid_old = (f.get(2, b'') or b'').decode('utf-8', 'replace')
        if mid:
            sess.machine_id = mid
        if mid_old:
            sess.machine_id_old = mid_old
        log(f'      machine id = {sess.machine_id!r} (old={sess.machine_id_old!r})')
    except Exception as e:
        log(f'      machine id parse error: {e}')
    ban_reason = ''
    if STORE is not None and (sess.machine_id or sess.machine_id_old):
        try:
            ban_reason = (await STORE.get_machine_ban(sess.machine_id)
                          or await STORE.get_machine_ban(sess.machine_id_old)
                          or '')
        except Exception as e:
            log(f'      [DB] get_machine_ban failed: {e}')
    if ban_reason:
        resp_proto = pb_int32(1, 1) + pb_string(2, ban_reason)
        note = f'BANNED machine={sess.machine_id!r}'
    else:
        resp_proto = pb_int32(1, 0) + pb_string(2, '')  # isBanned=false
        note = 'not banned'
    resp = build_response(cmd_id, 'QueryBannedMachineResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} QueryBannedMachineResponse ({note})')



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
        await _record_login_event(sess, code, pid=None)
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
        # 保留会话密钥：login2_postlogin 用它注册 IRC 会话（IRC PASS 的
        # HMAC 验签密钥，irc_server.register_session）
        sess.session_key = session_key
    else:
        code = RC['wrong_password']
        note = 'wrong_password=98'
    # 封禁拦截（密码正确才查，密码错本身已拒登）：账号封禁回 91 永久 /
    # 92 限时（客户端登录窗弹封禁文案 + 原因/到期时间）；机器码封禁回
    # 91（纵深防御：QueryBannedMachineRequest 已拦一道，防改包跳过）。
    ban_reason = None
    ban_expires = None
    if code == RC['login_success'] and STORE is not None:
        try:
            active, ban_expires, ban_reason = \
                await STORE.get_ban_info(sess.username)
        except Exception as e:
            active = False
            log(f'      [DB] get_ban_info failed ({e}), allowing login')
        if active:
            code = (RC['user_ban_temporary'] if ban_expires is not None
                    else RC['user_ban_permanent'])
            note = (f'ban temporary until {ban_expires}'
                    if ban_expires is not None else 'ban permanent')
        elif sess.machine_id or sess.machine_id_old:
            try:
                m_reason = (await STORE.get_machine_ban(sess.machine_id)
                            or await STORE.get_machine_ban(sess.machine_id_old))
            except Exception as e:
                m_reason = None
                log(f'      [DB] machine ban lookup failed ({e})')
            if m_reason:
                code = RC['user_ban_permanent']
                ban_reason = m_reason
                note = f'ban machine={sess.machine_id!r}'
    # currentplayer.id 用数据库真实 player_id（客户端战报界面按它
    # 匹配本局玩家行，恒 1 会让 DB id≥2 的玩家结算窗口不弹）；
    # f10/f11 官服口径：当前战争 id + 本场阵营选择（无选择发 0 →
    # 客户端弹 FactionSelectionWindow，join_war_request 落库后修正）
    # 封禁拦截也取 pid：login_events 落库要记是哪个账号被拦
    pid = await sess.ensure_player() \
        if code in (RC['login_success'], RC['user_ban_permanent'],
                    RC['user_ban_temporary']) else None
    await _record_login_event(sess, code, pid=pid)
    faction = None
    war = None
    if pid and code == RC['login_success']:
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
                                   war=war['id'] if war is not None else 0,
                                   ban_reason=ban_reason or None,
                                   ban_expires=ban_expires)
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
    # sessionid 唯一分配（官方实测 PASS 前缀为 19 位随机 int64）：客户端把
    # sessionID 拼进 IRC PASS `<sid>::<HMAC>`，服务端凭 sid 注册表验签
    # （irc_server.SESSIONS，R19 身份贯通）
    import irc_server
    sid = secrets.randbits(63)
    sess.irc_sid = sid
    if getattr(sess, 'session_key', None) is not None:
        irc_server.register_session(sid, int(sess.player_id or 1),
                                    sess.username or 'player',
                                    sess.session_key)
    resp_proto = b''
    resp_proto += pb_int64(1, sid)            # sessionid
    resp_proto += pb_string(2, 'pc')          # platform
    resp_proto += pb_string(3, '')            # deleteDate (空=未标记删除)
    resp_proto += pb_int32(4, 97)             # result = 97 (login_success)
    resp = build_response(cmd_id, 'login2_postlogin_result', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} login2_postlogin_result (result=97 login_success, sid={sid})')



async def handle_keepalive(sess, writer, cmd_id, req_type, proto):
    pass


async def handle_logout(sess, writer, cmd_id, req_type, proto):
    """logout {dummy=1} → logoutresponse {msg=1} + 关闭连接。

    LogoutCommand（菜单"退出登录"/不活跃超时/致命错误弹窗）清本地凭证
    后发送本请求；客户端对 logoutresponse 无回调（载荷不被消费），回
    登录界面完全依赖 socket Event.CLOSE → ServiceHandler.closeHandler
    → clearAndConnectSignal(RESET_CONNECTION) → ClearAndConnectCommand
    清库并置 loggedIn=false——只回包不关连接则客户端原地不动（菜单
    注销"没反应"的根因）。关连接后 handle_tcp finally 统一断线清理
    （登出广播/排队释放/匹配出池），客户端 resetConnection 会自动重连
    回登录界面。msg=1 沿用 ok 语义。"""
    resp = build_response(cmd_id, 'logoutresponse', pb_int32(1, 1))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} logoutresponse (msg=1)')
    # IRC 会话注销（名下已连的 IRC 连接一并断开）
    sid = getattr(sess, 'irc_sid', None)
    if sid is not None:
        import irc_server
        irc_server.unregister_session(sid)
        sess.irc_sid = None
    writer.close()


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
