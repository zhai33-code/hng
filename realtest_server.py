"""HnG real-machine test server（入口）。

服务端按模块划分：
  protocol.py        Chunk 帧编解码 + protobuf wire 编码 + 登录基础消息
  state.py           全局状态：STORE / Session / 路径 / 日志
  wardb.py           WarDatabase 静态数据层（解锁/定价/勋带/经验索引）
  messages.py        HnG_States.* 消息构建（_msg_*）+ KVCS 推送助手
  player_service.py  玩家数据服务（扣款/解锁位图/角色数据回放）
  handlers/          请求处理域模块（auth/views/characters/shop/weapons/
                     vehicles/uniforms/misc）
  matchmaking.py     匹配 / 战局详情 / combat server 拉起
  game_server.py     游戏进程(warserver)协议（GetPlayerSettings 等）
  web_server.py      HTTP(80) 独立模块：注册页 / settings.js / WarDatabase 下发 /
                     静态文件（安全敏感面集中在此）
  realtest_server.py 本入口：TCP(15110) 游戏协议 + ping 应答 + main 编排

Hosts file must redirect game.hng-group.com to 127.0.0.1.

Run:  python realtest_server.py
Requires:  admin (for port 80)  +  hosts entry
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import asyncio
import time

import game_server
import handlers
import irc_server
import login_queue
import matchmaking
import mm_handler
import mm_state
import mm_config
import combat_instances
import state
import statistics_server
import war_campaign
import web_server

from protocol import (
    Chunk,
    build_response,
    parse_request,
    pb_int64,
    )

from state import (
    FLASH_SESSIONS,
    STORE,
    TCP_SESSIONS,
    Session,
    log,
    )

from messages import _push_kvcs

from handlers.social import notify_friends_of


# 客户端连接活性：MainLayoutMediator.pulseTimerTicks 每 5 秒检查一次，
# 距上次收到服务器数据 >20s（SERVER_SLOW_LIMIT_SECONDS）就显示左上角 loading +
# "server slow"，>40s 还会发 keepalive。计数器只被 ViewHandlerPulse 数据行清零
# （普通请求/响应不重置），因此必须固定间隔推送，不能依赖连接空闲触发。
async def _send_pulse(sess, writer):
    """向已登录会话推送 ViewHandlerPulse（id/lastTickTime/serverTime，单位毫秒）。
    ★ game server 连接是新 TCP 流，playerview_cmd 未设（客户端未在 game server 连接上
      SubscribePlayerView）。用 cmd_id=0 fallback（与私服抓包一致: frame 62 ViewHandlerPulse
      在 game server 连接上 cmdId=0），否则 game server 连接 20s 无数据 → 超时 → 10005。"""
    view_cmd = sess.playerview_cmd if sess.playerview_cmd is not None else 0
    now_ms = int(time.time() * 1000)
    m = pb_int64(1, 1) + pb_int64(2, now_ms) + pb_int64(3, now_ms)
    await _push_kvcs(writer, view_cmd, b'ViewHandlerPulse', m)
    log(f'S->C cmd={view_cmd:<4} KeyValueChangeSet (ViewHandlerPulse, serverTime={now_ms})')


PULSE_INTERVAL = 5  # 秒；私服 PMS→Pulse 间隔约 5s，客户端 SERVER_SLOW_LIMIT_SECONDS=20
MAX_RECEIVE_BUFFER = 16 * 1024 * 1024
READ_IDLE_TIMEOUT = 120


async def _pulse_loop(sess, writer, fd):
    """固定 5s 推送 ViewHandlerPulse（不依赖连接空闲）。
    ★ 不能用「reader.read 超时才推」的空闲触发：玩家连点（勋带解锁每 3-4s 一请求）
      时 read 永不超时 → pulse 发不出去 → 客户端计数器 20s 越界 → 左上角
      "server slow" loading 误报（remote-log 实测 39 个 >20s pulse 空档中
      34 个发生在勋带连点期间，期间服务端响应全部秒回）。
    ★ ZServer wardatacomm 连接（BFSettings 握手时打标 is_combat_server）不推：
      官方 warserver 从不给专用服发 KVCS，ZServer 的 Unhandled 路径
      疑似堆损坏源（崩溃 0x4AD46D / 0x269E2，时间与 pulse 逐条吻合）。
      该连接建立后立即发握手，打标远早于本循环首次推送（sleep 5s 之后），
      无「标记前误推一帧」窗口。"""
    try:
        while True:
            await asyncio.sleep(PULSE_INTERVAL)
            if getattr(sess, 'is_combat_server', False):
                continue
            await _send_pulse(sess, writer)
    except asyncio.CancelledError:
        pass
    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
        pass
    except Exception as e:
        log(f'[pulse] loop failed fd={fd}: {e}')


async def handle_tcp(reader, writer):
    addr = writer.get_extra_info('peername')
    sock = writer.get_extra_info('socket')
    fd = sock.fileno() if sock else -1
    log(f'TCP CONNECT from {addr} (fd={fd})')
    sess = Session(writer)
    sess.remote_ip = addr[0] if addr else None
    TCP_SESSIONS.append(sess)   # 封禁踢下线（handlers/ban.py）按此匹配
    pulse_task = asyncio.create_task(_pulse_loop(sess, writer, fd))
    buf = b''
    try:
        while True:
            data = await asyncio.wait_for(
                reader.read(65536), timeout=READ_IDLE_TIMEOUT)
            if not data:
                break
            buf += data
            if len(buf) > MAX_RECEIVE_BUFFER:
                raise ValueError('receive buffer limit exceeded')
            while True:
                chunk, nxt = Chunk.parse(buf, 0)
                if chunk is None:
                    break
                buf = buf[nxt:]
                await dispatch(sess, chunk, writer)
    except (ConnectionResetError, asyncio.IncompleteReadError,
            asyncio.TimeoutError):
        pass
    except Exception as e:
        log(f'[handle_tcp] UNEXPECTED {addr}: {type(e).__name__}: {e}')
    finally:
        pulse_task.cancel()
        if sess in TCP_SESSIONS:
            TCP_SESSIONS.remove(sess)
        log(f'TCP DISCONNECT {addr} (fd={fd})')
        # IRC 会话注销（postlogin 注册；未走 logout 的断线兜底）
        _irc_sid = getattr(sess, 'irc_sid', None)
        if _irc_sid is not None:
            irc_server.unregister_session(_irc_sid)
        # Flash 连接注册表注销（仅当注册的就是本连接；重登录已被覆盖时不动）
        went_offline = False
        if sess.username and FLASH_SESSIONS.get(sess.username) is sess:
            del FLASH_SESSIONS[sess.username]
            went_offline = True
        # 下线广播：在线好友的好友列表里把我的 onlinestatus 刷回 0
        # （先注销再广播，此时 onlinestatus 自然算离线）
        if went_offline and sess.player_id is not None:
            try:
                await notify_friends_of(int(sess.player_id))
            except Exception as e:
                log(f'[social] offline notify failed: {type(e).__name__}: {e}')
        # 登录排队：排队/已放行未注册的会话出队释放名额；随后跑一次
        # pump（含 went_offline 释放 FLASH_SESSIONS 名额的情况）放行队首
        login_queue.on_disconnect(sess)
        try:
            await login_queue.pump()
        except Exception as e:
            log(f'[queue] pump on disconnect failed: {type(e).__name__}: {e}')
        # 匹配排队清理：断线玩家出池（防幽灵条目虚增人数/被撮合成局；
        # 已成局的不动，战局照常进行）
        try:
            matchmaking.drop_player(sess.player_id)
        except Exception as e:
            log(f'[drop_player] {type(e).__name__}: {e}')
        # 玩家游戏进程（hng.exe）连接断开 = 离开战局（崩溃/退出/被杀，
        # 优雅退出另有 PlayerExitBatch 上报）：起 rejoin 掉线保护——
        # 记 300s deadline + 给在线 Flash 会话推 PMS Admitted(2)
        # （客户端 REJOIN 300s 倒计时；超时 rejoin_sweep_loop 释放席位）
        if getattr(sess, 'is_game_client', False) and sess.player_id:
            try:
                await matchmaking.on_game_client_closed(int(sess.player_id))
            except Exception as e:
                log(f'[rejoin] game-conn closed hook failed: '
                    f'{type(e).__name__}: {e}')
        # 战斗服（ZServer wardatacomm）连接断开兜底：远端实例的主要退出
        # 监视（确认 agent pid 已死才回收坑位；本机实例另有
        # _watch_proc_exit，幂等）。进程仍活（网络抖动）不回收，防误杀活局。
        cm = int(getattr(sess, 'combat_mission', 0) or 0)
        if getattr(sess, 'is_combat_server', False) and cm:
            try:
                combat_instances.on_combat_conn_closed(cm)
            except Exception as e:
                log(f'[combat] conn-closed hook failed: {type(e).__name__}: {e}')
        writer.close()


async def dispatch(sess, chunk, writer):
    try:
        cmd_id, req_type, proto = parse_request(chunk)
    except Exception as e:
        log(f'PARSE-ERR {e}')
        return
    log(f'C->S cmd={cmd_id:<4} type={req_type:<45} proto={len(proto)}B'
        + (f' hex={proto[:32].hex()}' if proto else ''))

    # 单请求处理异常兜底：任何 handler 抛异常只记日志 + 回 HandlerErrorResponse，
    # 绝不让异常冒泡到 handle_tcp 杀掉整条连接（客户端请求队列串行，
    # 无响应会永久堵塞；连接断开则会话全丢——一个 handler 的 bug
    # 不应等于玩家掉线）。
    try:
        if await handlers.dispatch(sess, writer, cmd_id, req_type, proto):
            return
        if req_type in ('JoinMatchMakingRequest', 'CancelJoinMatchMakingRequest',
                        'GetMissionDetailsRequest'):
            if not getattr(sess, 'authenticated', False):
                writer.write(build_response(
                    cmd_id, 'HandlerErrorResponse', b''))
                await writer.drain()
                log(f'[auth] rejected unauthenticated matchmaking request '
                    f'{req_type}')
                return
            # 战斗匹配：匹配请求 / 取消匹配 / 战局详情（简报部署界面），独立模块实现
            await mm_handler.handle_request(sess, writer, cmd_id, req_type, proto)
            return
        if await game_server.handle_request(sess, writer, cmd_id, req_type, proto):
            # 游戏进程(warserver)握手请求：GetPlayerSettings 等由独立模块处理
            return
    except Exception as e:
        import traceback
        log(f'[dispatch] HANDLER-ERR {req_type}: {type(e).__name__}: {e}')
        log(traceback.format_exc())
        try:
            resp = build_response(cmd_id, 'HandlerErrorResponse', b'')
            writer.write(resp)
            await writer.drain()
        except Exception:
            pass                    # 连接已断：handle_tcp 会收尾
        return
    # 未知请求：必须回复 HandlerErrorResponse 解锁客户端请求队列！
    # 客户端 ServiceHandler 串行发送请求，未收到响应的请求会永久堵塞队列
    # （HandlerErrorResponse 不解析 proto 内容，客户端只显示错误提示并继续）
    log(f'      (no handler for {req_type}, sending HandlerErrorResponse to unblock queue)')
    resp = build_response(cmd_id, 'HandlerErrorResponse', b'')
    writer.write(resp)
    await writer.drain()


async def start_tcp():
    server = await asyncio.start_server(handle_tcp, '0.0.0.0', 15110)
    log('TCP game server listening on 0.0.0.0:15110')
    async with server:
        await server.serve_forever()


# HostingCenter ping 应答器：daemon 的 CMD_PING 只收 hostname（不带端口），
# 固定 ping 到 20201 端口（真实 HnG 托管中心的 ping 服务端口）。
# 收到连接后记录 daemon 发来的探测内容并延迟 50ms 回 HTTP 200（RTT>=1ms 过客户端 int()>=1 过滤）。
async def _handle_ping_probe(reader, writer):
    peer = writer.get_extra_info('peername')
    try:
        try:
            data = await asyncio.wait_for(reader.read(4096), timeout=2)
            if data:
                log(f'[PING] probe from {peer}: {data[:120]!r}')
        except asyncio.TimeoutError:
            log(f'[PING] probe from {peer}: (no data, waiting for server reply)')
        # 最小延迟：确保 RTT 经 daemon 取整后 >=1ms（客户端 HostingCenterTable
        # 过滤 pingResponse>=1，否则弹 "unable to ping game server"）。
        # 本地 loopback 实测 ≥1ms 即满足，无需 50ms。
        await asyncio.sleep(0.001)
        writer.write(b'HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\n1\r\n')
        await writer.drain()
    except Exception as e:
        log(f'[PING] responder error: {e}')
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def start_ping_responder():
    """HostingCenter ping 应答器（TCP+UDP 双监听 20201）。
    daemon 的 CMD_PING 只收 hostname，固定 ping 到 20201 端口（真实 HnG 托管中心 ping 端口）。
    实测 TCP 监听收不到任何连接 → 判定为 UDP 探测，故 UDP 收到数据报后记录并延迟 50ms 回显
    （RTT>=1ms 过客户端 int()>=1 过滤）。"""
    class _UdpProbe(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, data, addr):
            log(f'[PING] UDP probe from {addr}: {data[:120]!r}')

            async def _reply():
                await asyncio.sleep(0.05)
                try:
                    self.transport.sendto(data, addr)   # 回显探测内容
                except Exception:
                    pass
            asyncio.ensure_future(_reply())

    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(_UdpProbe,
                                                       local_addr=('0.0.0.0', 20201))
    # TCP 20201 可能落在 Windows 保留端口段（Hyper-V/WSL WinNAT excludedportrange，
    # 报 WSAEACCES 13）——实测 daemon 走 UDP 探测、TCP 从未收到连接，故 TCP 绑定
    # 失败只降级告警，不杀 UDP 应答器（此前异常上抛还会泄漏 UDP transport，
    # 导致 supervise 重启时自己占着自己端口报 10048）。
    server = None
    try:
        server = await asyncio.start_server(_handle_ping_probe, '0.0.0.0', 20201)
        log('Ping responder listening on 0.0.0.0:20201 (TCP+UDP, HostingCenter ping)')
    except OSError as e:
        log(f'[PING] TCP 20201 bind failed ({e}) -- UDP-only 降级运行 '
            f'(Windows excludedportrange)')
    try:
        if server is not None:
            async with server:
                await server.serve_forever()
        else:
            await asyncio.Event().wait()   # 无 TCP 监听：挂住保持 UDP 应答
    finally:
        transport.close()


# ================================================================ main


async def main():
    # 匹配模块日志注入（DB 统一走 state.STORE，无需再注入 store）
    mm_state.configure_log(log)
    # 登录排队（容量 HNG_MAX_ONLINE，事件驱动 + 1s 周期 pump 放行）
    login_queue.start()
    # OpenID provider：audience = EOS client_id（私服抓包确认）
    # ★ 私服 JWT aud = "xyza7891uO05dmOamk2giEDkkLHD6Taj"，非 Epic Deployment ID
    try:
        from eos import oidc_issuer
        oidc_issuer.configure('xyza7891uO05dmOamk2giEDkkLHD6Taj')
        log('[oidc] configured (audience=xyza7891uO05dmOamk2giEDkkLHD6Taj)')
    except Exception as e:
        log(f'[oidc] configure FAILED: {e}')
    # Run HTTP in a thread, TCP in asyncio
    import threading
    # 连接数据库（失败不阻断启动，仅失去持久化）
    try:
        await STORE.start()
        log(f'[DB] PostgreSQL ready: {STORE.dsn.split("@")[-1]}')
        # 重启孤儿清理：上一次运行遗留的组队中/运行中任务全部置为结束
        #（内存态已丢，不清会被 GetMissionList 当活局展示空壳局）
        try:
            n = await STORE.close_stale_missions()
            if n:
                log(f'[DB] closed {n} stale mission_records (server restart)')
        except Exception as e:
            log(f'[DB] close_stale_missions failed: {e}')
        # 活跃会战 record 重建：close_stale_missions 只关 record 不动
        # war_battles——战图战斗标记仍在，但 battle→mission 链（详情/
        # 加入/增援统一 get_mission_by_battle states 0/1）全断 → 点开
        # 战局详情回 43 秒关、加入战局直接退池。
        try:
            n = await war_campaign.rebuild_orphan_battle_missions(STORE)
            if n:
                log(f'[DB] rebuilt {n} mission_records for active '
                    f'war battles (server restart)')
        except Exception as e:
            log(f'[DB] rebuild orphan battle missions failed: {e}')
        # 选中士兵注册表回放（player_selected_char → 内存，
        # 组队入队规则"被邀者当前选中角色"跨重启保留）
        try:
            from state import PLAYER_SELECTED_CHAR
            loaded = await STORE.load_all_selected_chars()
            PLAYER_SELECTED_CHAR.update(loaded)
            if loaded:
                log(f'[DB] restored {len(loaded)} player_selected_char rows')
        except Exception as e:
            log(f'[DB] load_all_selected_chars failed: {e}')
        # 新邮件事件监听（LISTEN hng_new_mail）：管理端 web 发信后 NOTIFY，
        # 游戏服零轮询实时推 MessageRecipient KVCS 给在线会话
        from handlers.social import start_mail_listener
        start_mail_listener()
        # 封禁踢下线监听（LISTEN hng_ban_player）：管理端封禁落库后 NOTIFY，
        # 游戏服关闭该玩家全部在线连接（登录拦截在 login2 封禁检查）
        from handlers.ban import start_ban_listener
        start_ban_listener()
    except Exception as e:
        log(f'[DB] PostgreSQL UNAVAILABLE ({e}) -- 持久化已禁用')
    # HTTP 线程模块注入主 loop（/register 经它投递 create_player 协程；
    # /online.json 经它防抖刷新 combat_hosts 缓存 → 存 state 供各模块取）
    state.MAIN_LOOP = asyncio.get_running_loop()
    web_server.init(asyncio.get_running_loop())
    http_thread = threading.Thread(target=web_server.start_http, daemon=True)
    http_thread.start()
    log(f'[HTTP] register page: http://127.0.0.1/register')
    # TCP / ping 监听异常不死进程：内层异常重启服务（端口瞬时占用等场景），
    # 连续快速失败 5 次才放弃（避免错误配置下无限重启刷日志）。
    async def _supervise(name, factory):
        failures = 0
        while True:
            try:
                await factory()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                failures += 1
                log(f'[{name}] CRASHED ({type(e).__name__}: {e}), '
                    f'restart {failures}/5')
                if failures >= 5:
                    log(f'[{name}] giving up after 5 consecutive failures')
                    raise
                await asyncio.sleep(min(2 ** failures, 10))
            else:
                failures = 0        # 正常退出（不应发生）→ 也重启

    async def _war_ticker():
        """战争推进 tick（Phase 6）：AT 在途移动换段/到达/会战成局桩
        + warmapview 事件广播。间隔 MM_WAR_TICK_SECONDS（默认 5s）。"""
        while True:
            await asyncio.sleep(mm_config.WAR_TICK_SECONDS)
            try:
                await war_campaign.war_tick(STORE)
            except Exception as e:
                log(f'[WAR] tick failed: {e}')
    try:
        # rejoin 掉线保护扫描：hng.exe 断开/优雅退出后 REJOIN_TIMEOUT 秒
        # 未重进 → 释放战局会话（PMS 清理 + 空局回收 combat server 坑位）
        from handlers.mission import rejoin_sweep_loop
        await asyncio.gather(_supervise('TCP', start_tcp),
                             _supervise('PING', start_ping_responder),
                             _supervise('STATS', statistics_server.start),
                             _supervise('IRC', irc_server.start),
                             _supervise('REJOIN-SWEEP', rejoin_sweep_loop),
                             _war_ticker())
    finally:
        await STORE.close()


if __name__ == '__main__':
    asyncio.run(main())
