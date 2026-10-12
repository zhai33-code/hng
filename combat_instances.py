"""战斗服生命周期管理（combat_instances）。

多实例 combat server 管理（每场战局一个实例，动态 UDP 端口）：按需拉起
hng.exe /D 专用服、握手认领（FIFO）、进程退出监视兜底回收、端口池分配、
战局结束释放与并发上限排队消费。从 matchmaking.py 拆出
（2026-09-06 重构，纯移动行为不变）。

多机部署（2026-09-08）：主机列表/各机容量/端口池由 server_config
'combat_hosts' 热配置（combat_hosts.py，管理后台保存即生效）；成局时
matchmaking._start_match 选机（pick_host）→ reserve_combat_instance 同步
占坑 → 本函数补进程：本机 spawn 或远端 combat_agent HTTP 拉起。未配置时
单机回退与改造前 env 行为一致。
"""
import asyncio
import json
import os
import random
import socket
import subprocess
import time

import combat_agent
import combat_hosts
from state import PUBLIC_HOST
import mm_config
from mm_state import _MM_MISSION_DETAILS, _MM_MISSION_SQUADS, _log, _spawn


# ---------------------------------------------------------------- combat server
# 真实 warserver 流程：匹配成功 → 按需拉起 combat server 实例（hng.exe /D）→
# 实例回连 warserver 完成 RequestBattleFieldSettings 握手 + 加载地图 bind UDP →
# warserver 才推 PlayerMissionSession Admitted（客户端拉起 hng.exe 进战斗）。
# 这里复刻该链路：_matchmade 先 _ensure_combat_server()，再推 Admitted。
# 路径解析顺序（打包部署友好）：
#   1. 环境变量 HNG_COMBAT_SERVER_DIR（显式指定，最高优先级）
#   2. 包目录同级的 combat-server（zip 解压到客户端根目录时命中，
#      如 d:\HnG-test\hng-server + d:\HnG-test\combat-server）
#   3. 客户端根目录（state.GAME_DIR）下的 combat-server
#   4. GAME_DIR 自身即 combat-server（warserver 与 combat-server 同层部署：
#      state.py 探测到的客户端根就是 combat-server，hng.exe 直接在其中）
#   5. 兜底 GAME_DIR\combat-server
def _detect_combat_dir():
    env = os.environ.get('HNG_COMBAT_SERVER_DIR')
    if env:
        return os.path.abspath(env)
    from state import GAME_DIR
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for cand in (os.path.join(pkg_parent, 'combat-server'),
                 os.path.join(GAME_DIR, 'combat-server'),
                 GAME_DIR):
        if os.path.isfile(os.path.join(cand, 'hng.exe')):
            return cand
    return os.path.join(GAME_DIR, 'combat-server')


COMBAT_SERVER_DIR = _detect_combat_dir()
COMBAT_UDP_PORT = 5678                 # 兼容保留：旧单实例端口 = 端口池起点
# combat server 回连 warserver 做握手（RequestBattleFieldSettings）。
# 默认回环（combat 与 warserver 同机部署，走 127.0.0.1 最稳）；
# combat server 分离部署时用 HNG_COMBAT_WARSERVER 指向 warserver 对外地址。
COMBAT_WARSERVER = (os.environ.get('HNG_COMBAT_WARSERVER')
                    or '127.0.0.1:15110')
# 远端 combat_agent 的 HMAC 共享密钥（combat_agent.py --token 同值；跨公网
# 明文 HTTP 下密钥只进签名不出链路，抓包抓不到凭证）。拉起/停止/查询的每个
# 请求带 X-Agent-Timestamp/Nonce/Signature 签名三头（combat_agent.sign_request）。
# 不设则 agent 端也须不设 = 开放，仅限本机调试。模块级读取，改 .env 须重启。
AGENT_TOKEN = os.environ.get('HNG_AGENT_TOKEN', '')
AGENT_HTTP_TIMEOUT = 10
# 战局开局等待（热身）时间覆盖，单位【秒】；None = 不覆盖（player.dll 默认）。
# 依据 work/hng-server-re/warserver-serversystemevents.md 4.3 节：
#   开局倒计时由 battlefield 实体(class 5) block+4 驱动，服务端默认 7200s 硬编码
#   （ZGameMode::CreateInstance @0x1802A4796）；正确覆盖方式是启动参数
#   /R controllers.overrideStartCountdown=<秒>（ZOption group controllers），
#   而非改 missionpreset f6（实测不生效）。
COMBAT_START_COUNTDOWN_OVERRIDE = (
    mm_config.COMBAT_START_COUNTDOWN_OVERRIDE or None)

# TTD 时间旅行录制（可选）：设环境变量 TTD_RECORD=1 后，combat server 由
# WinDbg 自带 TTD.exe 包裹启动，崩溃后 work\ttd-traces\ 下生成 .run 轨迹，
# 可在 WinDbgX 中倒放定位"哪条指令写坏了对象池内存"。
# 注意：录制需管理员权限（realtest 本就以管理员运行，子进程继承即可）。
TTD_EXE = r'd:\HnG-test\work\ttd\TTD.exe'
TTD_OUT_DIR = r'd:\HnG-test\work\ttd-traces'

# ---- 每实例 CPU 资源槽（亲核掩码 + packthreads；2026-09-09）----
# 以 B=最大并发战斗服数为主控推导（公式/根因见 mm_config COMBAT_CORES
# 注释与 combat_agent.plan_combat_resources），实例按 seq 轮转取槽。
# B=0（不限）时以端口池大小为有效并发推导。远端 agent 机不读本表——
# agent 读自己环境的同名变量算自己的核（combat_agent.resource_slot）。
_effective_b = (mm_config.MAX_CONCURRENT_COMBAT_SERVERS
                or (mm_config.COMBAT_PORT_MAX - mm_config.COMBAT_PORT_BASE + 1)
                or 1)
_detected_cores = combat_agent.detect_cores(mm_config.COMBAT_CORES)
if _detected_cores and _effective_b > _detected_cores:
    _log(f'WARNING: 有效并发局数 {_effective_b} > 本机核数 '
         f'{_detected_cores}（过订阅）：亲核表钳为每实例 1 核、'
         f'packthreads 退回串行，多人局会掉帧——建议调低 '
         f'MM_MAX_COMBAT_SERVERS')
COMBAT_RESOURCE_SLOTS = combat_agent.plan_combat_resources(
    _detected_cores, _effective_b,
    mm_config.COMBAT_PACKTHREADS, mm_config.COMBAT_AFFINITY_MASKS)
if COMBAT_RESOURCE_SLOTS:
    _log(f'combat 资源槽（{_detected_cores} 核 / B={_effective_b}，'
         f'实例按 seq 轮转）: '
         + ', '.join(f'#{i} mask={s["mask"]:#x} packthreads={s["packthreads"]}'
                     for i, s in enumerate(COMBAT_RESOURCE_SLOTS)))

# ===== 多实例 combat server 管理（每场战局一个实例，动态 UDP 端口）=====
# CombatInstance dict：{mission_id, port, proc, ready(asyncio.Event), seq, ttd}
COMBAT_INSTANCES = {}          # mission_id -> CombatInstance
PENDING_MATCHES = []           # 并发上限排队中的成局 [match, ...]（FIFO）
_combat_seq = random.randint(100000, 999999)   # combat 实例序号（也用于 logs/combat-server-<seq>.log
                                               # 文件名，随机起始避免重启后覆盖旧日志）
COMBAT_READY = asyncio.Event()  # 兼容回退：无法按 missionid 定位实例时置位
# 实测（realtest_game.log 全量历史）专用服 RequestBattleFieldSettings 的
# proto 为空（无 field1=missionid），无法自报归属 → 按启动顺序 FIFO 认领。
_PENDING_HANDSHAKE = []        # 已拉起、等待握手认领的 mission_id（启动顺序）


def claim_pending_combat_instance(remote_ip=None):
    """空 proto 握手认领：返回最早一个「已拉起且未 ready」实例的 mission_id。

    跳过已就绪/已释放的陈旧条目（懒清理）；全部无效返回 None。
    每个专用服连接只发一次 RequestBattleFieldSettings，认领即出队。

    多机部署（remote_ip 给定）：只在来源主机（combat_hosts.host_matches_ip
    匹配 addr/public_addr/agent 主机名；127.0.0.1 视为本机 spawn 条目）的
    pending 实例中 FIFO——不同机实例握手到达顺序 ≠ 启动顺序，全局 FIFO 会
    把 A 机的图错配给 B 机的专用服。来源 IP 不匹配任何主机（NAT 配错等）
    → 记日志回退全局 FIFO（兼容旧手动分离部署）。
    """
    if remote_ip:
        names = {h['name'] for h in combat_hosts.last_hosts()
                 if combat_hosts.host_matches_ip(h, remote_ip)}
        if names:
            for mid in list(_PENDING_HANDSHAKE):
                inst = COMBAT_INSTANCES.get(mid)
                if inst is None or inst['ready'].is_set():
                    _PENDING_HANDSHAKE.remove(mid)   # 陈旧条目懒清理（按值删）
                    continue
                if (inst.get('host') or {}).get('name') in names:
                    _PENDING_HANDSHAKE.remove(mid)
                    return mid
            return None          # 该机无待认领实例：绝不认领他机实例（防错配）
        _log(f'combat handshake from unknown host ip={remote_ip} - '
             f'falling back to global FIFO claim（检查 combat_hosts addr 配置）')
    while _PENDING_HANDSHAKE:
        mid = _PENDING_HANDSHAKE[0]
        inst = COMBAT_INSTANCES.get(mid)
        if inst is None or inst['ready'].is_set():
            _PENDING_HANDSHAKE.pop(0)
            continue
        _PENDING_HANDSHAKE.pop(0)
        return mid
    return None

def notify_combat_ready(mission_id=None):
    """game_server.py 响应 RequestBattleFieldSettings 后调用：
    按 missionid 定位实例置 ready（可推 Admitted）；查不到回退全局事件。"""
    import matchmaking  # 延迟 import：撮合核心在 matchmaking（防环）
    inst = COMBAT_INSTANCES.get(mission_id) if mission_id else None
    if inst is not None:
        inst['ready'].set()
    else:
        COMBAT_READY.set()
    # populating→running 转变：刷新排队玩家 UI 的战局统计（f22/f23）
    _spawn(matchmaking._push_queue_counts(), 'push_queue_counts')


def _combat_alive():
    return any(i['proc'] is not None and i['proc'].returncode is None
               for i in COMBAT_INSTANCES.values())


def _reclaim_dead_instance(inst, mission_id, reason):
    """实例死亡（崩溃/被杀/未走 earnings 即退出）兜底回收，幂等（实例已
    出队则 no-op）。必须补 _finish_mission_sessions（squad 重置 + PMS
    DELETE KVCS）：客户端 closing 流程等的就是这个会话结束信号；不推则
    只能每秒轮询 GetMissionDetails 拿 mission_not_found，直到超时自行
    Abandon（2026-08-27 卡 closing 56s 根因）。延迟 import 防环。"""
    if COMBAT_INSTANCES.get(int(mission_id)) is not inst:
        return
    _log(f'combat server #{inst["seq"]} exited without release '
         f'(mission={mission_id}, {reason}) - reclaiming slot')
    from handlers import combat as _combat_mod
    _spawn(_combat_mod._finish_mission_sessions(int(mission_id), None),
           f'finish_sessions#{inst["seq"]}')
    release_match(int(mission_id))


async def _watch_proc_exit(inst, mission_id):
    """本机 combat server 进程退出监视：退出而坑位未释放（崩溃/被杀/earnings
    处理中途异常，正常倒计时关服路径 release_match 已先到则 no-op）时
    兜底回收，保证「进程关了 = 坑位必释放」。"""
    try:
        await inst['proc'].wait()
    except Exception:
        pass
    _reclaim_dead_instance(inst, mission_id, f'code={inst["proc"].returncode}')


async def _agent_pid_alive(host, pid):
    """查 agent：pid 是否仍在运行实例列表（agent 不可达抛异常由调用方定夺）。"""
    if not pid:
        return False
    data = await _agent_request(host, 'GET', '/status')
    return any(int(it.get('pid') or 0) == int(pid)
               for it in data.get('instances', []))


async def _watch_agent_instance(inst, mission_id):
    """远端 agent 实例看门狗（对齐本机 _watch_proc_exit）：轮询 agent pid
    存活，确认死亡即回收。覆盖「拉起后即崩溃、握手都没发生」的坑位泄漏
    （该场景无 TCP 连接，断连钩子收不到）。agent 不可达继续等（网络抖动/
    agent 重启不误杀活局），连接断开钩子另有兜底。"""
    host = inst.get('host') or {}
    pid = inst.get('agent_pid')
    while COMBAT_INSTANCES.get(int(mission_id)) is inst:
        await asyncio.sleep(15)
        try:
            alive = await _agent_pid_alive(host, pid)
        except Exception as e:
            _log(f'agent status poll failed (host={host.get("name")}, '
                 f'pid={pid}): {type(e).__name__}: {e}')
            continue
        if not alive:
            break
    _reclaim_dead_instance(inst, mission_id, f'agent pid={pid} gone')


def on_combat_conn_closed(mission_id):
    """战斗服 TCP 连接断开（进程退出/崩溃）兜底：确认死亡才回收坑位。

    realtest_server.handle_tcp finally 调用（sess.is_combat_server 且
    combat_mission 已登记）。远端实例的主要退出监视；本机实例
    _watch_proc_exit 已覆盖，此处幂等。网络抖动/临时断开（进程仍活）不
    回收，防误杀活局。"""
    _spawn(_on_combat_conn_closed(int(mission_id)),
           f'combat_conn_closed#{mission_id}')


async def _on_combat_conn_closed(mission_id):
    inst = COMBAT_INSTANCES.get(int(mission_id))
    if inst is None:
        return                      # 已正常释放（release_match 先到）
    host = inst.get('host') or {}
    if host.get('agent'):
        try:
            alive = await _agent_pid_alive(host, inst.get('agent_pid'))
        except Exception as e:
            # agent 不可达 = 该机进程已失管（整机下线/防火墙变动），
            # 坑位留着也永远无法释放 → 按死亡回收（战斗服随之失联会被
            # 客户端超时兜底；日志留痕供排查）
            _log(f'agent unreachable on conn-close (host={host.get("name")}, '
                 f'mission={mission_id}): {type(e).__name__}: {e} - '
                 f'reclaiming slot')
            alive = False
    else:
        proc = inst.get('proc')
        alive = proc is not None and proc.returncode is None
    if alive:
        _log(f'combat server #{inst["seq"]} connection dropped but process '
             f'alive (mission={mission_id}) - keep slot')
        return
    _reclaim_dead_instance(inst, mission_id, 'connection closed')


def _alloc_combat_port(host=None):
    """从端口池分配空闲 UDP 端口。

    host 给定 → 该机独立端口池（只避开同机实例端口，多机互不挤占）；
    host=None → 旧全局口径（env 端口池、避开所有实例端口，兼容未迁移调用）。
    """
    if host is not None:
        return combat_hosts.alloc_port(host, COMBAT_INSTANCES)
    used = {i['port'] for i in COMBAT_INSTANCES.values()}
    for p in range(mm_config.COMBAT_PORT_BASE, mm_config.COMBAT_PORT_MAX + 1):
        if p not in used:
            return p
    return None


def reserve_combat_instance(mission_id, host, port):
    """成局选机后同步登记实例（占坑 + 占端口，防并发成局双占同端口：
    select 与 spawn 间存在 await 间隙，先登记后拉起即无竞态窗口）。

    进程参数由 _ensure_combat_server 补齐（本机 spawn / 远端 agent）。
    """
    global _combat_seq
    _combat_seq += 1
    inst = {'mission_id': int(mission_id), 'port': int(port), 'proc': None,
            'ready': asyncio.Event(), 'seq': _combat_seq, 'ttd': False,
            'udp': None, 'host': host, 'agent_pid': None}
    COMBAT_INSTANCES[int(mission_id)] = inst
    return inst


async def _stop_instance(inst):
    """停掉单个战局实例（远端经 agent taskkill；本机 TTD 包裹时杀进程树
    防 hng.exe 孤儿；模拟实例关 UDP）。"""
    host = inst.get('host') or {}
    if host.get('agent') and inst.get('agent_pid'):
        try:
            await _agent_stop(host, int(inst['agent_pid']))
        except Exception as e:
            # 失败仅记日志：连接断开钩子/看门狗会兜底回收坑位
            _log(f'stop remote combat server FAILED (host={host.get("name")}, '
                 f'pid={inst["agent_pid"]}, mission={inst["mission_id"]}): '
                 f'{type(e).__name__}: {e}')
        return
    udp = inst.get('udp')
    if udp is not None:
        try:
            udp.close()
        except Exception:
            pass
    proc = inst.get('proc')
    if proc is not None and proc.returncode is None:
        try:
            if inst.get('ttd'):
                subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)],
                               capture_output=True)
                await proc.wait()
            else:
                proc.kill()
                await proc.wait()
        except Exception as e:
            _log(f'stop combat server (mission={inst["mission_id"]}) failed: {e}')


def release_match(mission_id):
    """战局结束清理：杀实例、释放端口、清详情，并消费排队战局。

    由 handlers/mission.py 的 EndMission / AbandonMission 调用。
    """
    import matchmaking  # 延迟 import：撮合核心在 matchmaking（防环）
    inst = COMBAT_INSTANCES.pop(int(mission_id), None)
    if inst is not None:
        _log(f'releasing combat server #{inst["seq"]} '
             f'(mission={mission_id}, udp/{inst["port"]})')
        _spawn(_stop_instance(inst), f'stop_instance#{inst["seq"]}')
    _MM_MISSION_DETAILS.pop(int(mission_id), None)
    _MM_MISSION_SQUADS.pop(int(mission_id), None)
    matchmaking.release_mission_resources(mission_id)
    _drain_pending_matches()
    # 端口/坑位已释放：池内可能有待撮合的玩家（如端口耗尽时回池的局）
    _spawn(matchmaking._try_form_all(), 'try_form_all(release)')
    # 战局数变化：刷新排队玩家 UI 的战局统计（f20-f23）
    _spawn(matchmaking._push_queue_counts(), 'push_queue_counts(release)')


def _drain_pending_matches():
    """坑位释放后重派排队战局：全部重新分发（容量判定在 _start_match 内
    ——选机需读库热配置；仍满的局自行重新排队到队尾，顺序保持 FIFO）。

    无容量检查即 pop-all 是刻意的：同步上下文（release_match）无法读库，
    而「容量误判滞留队列直到下次释放」是饥饿 bug（调大容量/下线主机后
    排队局永不起动），重派一次的成本只是几条日志。"""
    import matchmaking  # 延迟 import：撮合核心在 matchmaking（防环）
    while PENDING_MATCHES:
        match = PENDING_MATCHES.pop(0)
        _log(f'pending match re-dispatch (mission~'
             f'{getattr(match, "mission_id", 0)}, players='
             f'{len(match.players)}, queued={len(PENDING_MATCHES)})')
        matchmaking._dispatch_match(match)


async def _wait_udp_bound(port, timeout=90):
    """等待专用服 bind UDP 端口（= 地图加载完成，进入可接受玩家状态）。

    探测法：尝试 bind 同端口，失败(WinError 10048) = 专用服已占用 = 已监听。
    探测 socket 立即关闭，无副作用（客户端此刻尚未收到 Admitted，不会连入）。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind(('0.0.0.0', port))
            s.close()
            await asyncio.sleep(2)     # 能 bind = 还没人监听，继续等
        except OSError:
            return True                # 端口被占 = 专用服已 bind
        finally:
            try:
                s.close()
            except OSError:
                pass
    return False


async def _log_udp_bound_bg(seq, port):
    """后台探测专用服 UDP bind（地图加载完成），仅记日志不阻塞流程。"""
    t0 = time.monotonic()
    ok = await _wait_udp_bound(port, timeout=180)
    if ok:
        _log(f'combat server #{seq} READY (udp/{port} bound, '
             f'map load took {time.monotonic() - t0:.1f}s)')
    else:
        _log(f'WARNING: combat server #{seq} udp/{port} not bound after 180s '
             f'(map load slow or failed) - check combat-server\\logs\\combat-server-{seq}.log')


def _host_statserver(host):
    """统计服地址 = 该机可达 warserver 的地址 host 部分（15115 统计服与
    warserver 同机）：combat server 能连 warserver 就必能连同机统计服。
    单机回退场景 = 127.0.0.1（原 PUBLIC_HOST 写法在公网部署时绕外网回环，
    现统一走内网最稳路径）。"""
    ws = (host.get('warserver') or COMBAT_WARSERVER).strip()
    return ws.split(':')[0] or PUBLIC_HOST


async def _agent_request(host, method, path, body=None):
    """warserver → combat_agent 裸 HTTP/1.1 客户端（零第三方依赖；自家
    agent、固定 API 形状，无重定向/chunked，裸实现足够）。
    每请求 HMAC 签名（combat_agent.sign_request，密钥不上链路 + 防重放）。
    非 200 抛 RuntimeError（调用方记日志 + 主机冷却）。"""
    url = host['agent']
    rest = url[7:] if url.startswith('http://') else url
    hp, _, path_prefix = rest.partition('/')
    h, _, p = hp.partition(':')
    port = int(p or 80)
    prefix = ('/' + path_prefix).rstrip('/') if path_prefix else ''
    payload = json.dumps(body).encode() if body is not None else None
    lines = [f'{method} {prefix}{path} HTTP/1.1', f'Host: {h}']
    if AGENT_TOKEN:
        for k, v in combat_agent.sign_request(
                method, prefix + path, payload or b'', AGENT_TOKEN).items():
            lines.append(f'{k}: {v}')
    lines.append('Connection: close')
    headers = '\r\n'.join(lines) + '\r\n'
    if payload is not None:
        headers += (f'Content-Type: application/json\r\n'
                    f'Content-Length: {len(payload)}\r\n')
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(h, port), AGENT_HTTP_TIMEOUT)
    try:
        writer.write(headers.encode() + b'\r\n' + (payload or b''))
        await writer.drain()
        raw = b''
        while True:
            chunk = await asyncio.wait_for(reader.read(65536),
                                           AGENT_HTTP_TIMEOUT)
            if not chunk:
                break
            raw += chunk
    finally:
        try:
            writer.close()
        except Exception:
            pass
    head, _, body_bytes = raw.partition(b'\r\n\r\n')
    parts = head.split(b' ', 2)
    status = int(parts[1]) if len(parts) >= 2 else 0
    try:
        data = json.loads(body_bytes) if body_bytes else {}
    except Exception:
        data = {}
    if status != 200:
        raise RuntimeError(f'agent {host.get("name")} {method} {path} '
                           f'HTTP {status}: {data.get("error", data)}')
    return data


async def _agent_launch(host, inst, pms_id, combat_map):
    """经 agent 拉起远端 combat server；返回 (ok, pid)。"""
    body = {'port': int(inst['port']), 'pms_id': int(pms_id),
            'mission_id': int(inst['mission_id']), 'map': combat_map,
            'warserver': host.get('warserver') or COMBAT_WARSERVER,
            'statserver_host': _host_statserver(host),
            'seq': int(inst['seq']),
            'countdown': COMBAT_START_COUNTDOWN_OVERRIDE}
    try:
        data = await _agent_request(host, 'POST', '/launch', body)
        return True, int(data.get('pid') or 0)
    except Exception as e:
        _log(f'agent launch FAILED (host={host.get("name")}, '
             f'mission={inst["mission_id"]}, udp/{inst["port"]}): '
             f'{type(e).__name__}: {e}')
        return False, None


async def _agent_stop(host, pid):
    await _agent_request(host, 'POST', '/stop', {'pid': int(pid)})


def _combat_spawn_args(port, mission_id, pms_id, combat_map, seq,
                       warserver_addr, statserver_host,
                       packthreads=0, countdown=None):
    """本机 spawn 启动参数（与 combat_agent.build_launch_args 同构）。

    /R 选项必须用完整注册名（组.选项）：ZOptionSet 按全名查找，
    无组前缀的名字会落入未注册选项桶，游戏代码不会读取。
    """
    args = [os.path.join(COMBAT_SERVER_DIR, 'hng.exe'),
            '/D', str(port),
            '/S', f'session-{mission_id}',
            '/P', str(pms_id),                # boot.playersessionid = MissionSession ID
            '/PS', f'login-{pms_id}',
            '/M', combat_map,
            '/W', warserver_addr,
            '/R', 'boot.disable_eac=1',
            # boot.logfile 有专用命令行开关；/R 路径在真机上未覆盖
            # hng_boot.log，改用直接映射 boot.logfile 的 /L。
            '/L', os.path.join('logs', f'combat-server-{seq}.log'),
            # 战斗服进程 player.dll 进图时 GetTotals 初始化勋带状态（a2[7]），
            # 失败→整局无 XP 累加→无 Statistics_Update→客户端无升级弹窗
            '/R', f'boot.statserver={statserver_host}',
            '/R', 'boot.statserverport=15115']
    if packthreads > 0:
        # 实体状态打包并行度（ZWorkQueue 工作线程数；引擎默认 0=串行是
        # 多人局 SendPump 饱和根因，推导依据见 mm_config COMBAT_CORES 注释）
        args += ['/R', f'server.packthreads={int(packthreads)}']
    if countdown is not None:
        # 开局等待时间覆盖（秒），见文件头 COMBAT_START_COUNTDOWN_OVERRIDE 注释
        args += ['/R',
                 f'controllers.overrideStartCountdown={int(countdown)}']
    return args


async def _ensure_combat_server(mission_id, pms_id, combat_map, timeout=120):
    """拉起本场战局的专用战斗服并等待 BattleFieldSettings 握手完成。

    实例登记由 reserve_combat_instance 先行完成（matchmaking._start_match
    选机即占坑）；本函数补进程：本机 spawn / 远端 agent 拉起（host['agent']）。
    未预留（旧调用路径）→ 按 env 单机回退就地登记。

    多实例并行：/D 端口按局分配；不再杀旧实例（战局生命周期由
    release_match 管理）。超时也放行（返回 False）：客户端 hng.exe
    连不上 UDP 会重试，且日志有记录便于排查。
    """
    inst = COMBAT_INSTANCES.get(int(mission_id))
    if inst is None:
        # 旧路径兜底：未预留就地登记（env 单机行为）
        inst = reserve_combat_instance(
            mission_id, combat_hosts.fallback_host(),
            _alloc_combat_port() or 0)
    host = inst['host']
    seq = inst['seq']
    port = inst['port']

    # macOS/Linux 等无 hng.exe 环境（MM_SIMULATE_COMBAT=1）：内嵌模拟——
    # bind UDP 端口（占坑 + 让 _wait_udp_bound 探测判定 READY）+ 直接完成
    # 握手。实例登记/并发上限/释放与真实路径完全一致。
    if mm_config.SIMULATE_COMBAT:
        _log(f'combat server #{seq} SIMULATED (mission={mission_id}, '
             f'udp/{port}, map={combat_map}, host={host.get("name")})')
        try:
            _transport, _proto = await asyncio.get_event_loop() \
                .create_datagram_endpoint(asyncio.DatagramProtocol,
                                          local_addr=('0.0.0.0', port))
            inst['udp'] = _transport
        except OSError as e:
            _log(f'simulated combat udp bind failed (udp/{port}): {e}')
        inst['ready'].set()
        return True

    # ★ 远端 agent 拉起（多机部署）
    if host.get('agent'):
        ok, pid = await _agent_launch(host, inst, pms_id, combat_map)
        if not ok:
            # 与本机「两次 spawn 失败」同路径：回收登记 + 主机冷却 + 消费
            # 排队局；PMS 推送照旧放行（客户端连不上会重试）
            COMBAT_INSTANCES.pop(int(mission_id), None)
            combat_hosts.mark_host_failed(host['name'])
            _drain_pending_matches()
            return False
        inst['agent_pid'] = pid
        _log(f'combat server #{seq} launched via agent {host["name"]} '
             f'(pid={pid}, mission={mission_id}, udp/{port})')
        _PENDING_HANDSHAKE.append(int(mission_id))
        _spawn(_watch_agent_instance(inst, mission_id),
               f'watch_agent#{seq}')
        try:
            await asyncio.wait_for(inst['ready'].wait(), timeout)
            _log(f'combat server #{seq} handshake ok (agent pid={pid}, '
                 f'mission={mission_id})')
            _spawn(_log_udp_bound_bg(seq, port), f'udp_probe#{seq}')
            return True
        except asyncio.TimeoutError:
            _log(f'combat server handshake TIMEOUT after {timeout}s '
                 f'(agent {host["name"]}, pid={pid}) - 推 Admitted 继续，'
                 f'查 {host["name"]} logs\\combat-server-{seq}.log')
            return False

    # ---- 本机 spawn（原路径；host['warserver'] 可按机覆盖 /W 回连地址）----
    # CPU 资源槽（亲核掩码 + packthreads）：按实例 seq 轮转，推导依据见
    # 模块头 COMBAT_RESOURCE_SLOTS 注释
    slot = (COMBAT_RESOURCE_SLOTS[seq % len(COMBAT_RESOURCE_SLOTS)]
            if COMBAT_RESOURCE_SLOTS else None)
    packthreads = slot['packthreads'] if slot else 0
    affinity_mask = slot['mask'] if slot else 0
    args = _combat_spawn_args(
        port, mission_id, pms_id, combat_map, seq,
        host.get('warserver') or COMBAT_WARSERVER,
        _host_statserver(host), packthreads,
        COMBAT_START_COUNTDOWN_OVERRIDE)
    launch_args = args
    if os.environ.get('TTD_RECORD') and os.path.exists(TTD_EXE):
        os.makedirs(TTD_OUT_DIR, exist_ok=True)
        launch_args = [TTD_EXE, '-out', TTD_OUT_DIR,
                       '-timestampFileName', '-noUI', '-maxFile', '8192'] + args
        inst['ttd'] = True
        _log(f'TTD recording ON -> {TTD_OUT_DIR}')
    _log(f'launching combat server: {" ".join(args[1:])}')
    for attempt in (1, 2):              # spawn 失败重试一次（换端口）
        try:
            os.makedirs(os.path.join(COMBAT_SERVER_DIR, 'logs'), exist_ok=True)
            inst['proc'] = await asyncio.create_subprocess_exec(
                *launch_args, cwd=COMBAT_SERVER_DIR,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            break
        except Exception as e:
            _log(f'launch combat server FAILED (attempt {attempt}/2): {e}')
            if attempt == 1:
                # 换端口重试一次（原端口可能被外部进程抢占）
                new_port = _alloc_combat_port(host)
                if new_port is None:
                    break               # 端口池也空：走下方统一回收
                port = new_port
                inst['port'] = int(port)
                i = args.index('/D')
                args[i + 1] = str(port)
                launch_args = [TTD_EXE, '-out', TTD_OUT_DIR,
                               '-timestampFileName', '-noUI', '-maxFile',
                               '8192'] + args if inst['ttd'] else args
                _log(f'retrying with port {port}')
    else:
        # 两次 spawn 均失败（exe 缺失 / Exec format error 等）：回收实例登记，
        # 否则并发坑位永久泄漏 → 后续成局全部滞留 PENDING_MATCHES。
        # PMS 推送照旧放行（与握手超时同一哲学：客户端连不上会重试）。
        COMBAT_INSTANCES.pop(int(mission_id), None)
        _drain_pending_matches()
        return False
    # 进程优先级 + 亲核（Windows；失败仅告警不影响开局——调度优化非
    # 正确性；TTD 包裹时优先级/亲核设在 TTD 进程，hng.exe 子进程按
    # Windows 默认继承掩码与优先级）
    prio = combat_agent.combat_priority()
    if prio and not combat_agent.apply_priority(inst['proc'].pid, prio):
        _log(f'WARNING: combat server #{seq} priority set failed '
             f'(pid={inst["proc"].pid}, prio={prio!r})')
    if affinity_mask:
        if combat_agent.apply_affinity(inst['proc'].pid, affinity_mask):
            _log(f'combat server #{seq} pinned to cpu mask '
                 f'{affinity_mask:#x} (packthreads={packthreads}, '
                 f'prio={prio!r})')
        else:
            _log(f'WARNING: combat server #{seq} affinity set failed '
                 f'(pid={inst["proc"].pid}, mask={affinity_mask:#x})')
    # 进程退出监视（崩溃/被杀兜底回收坑位；正常关服路径幂等 no-op）
    _spawn(_watch_proc_exit(inst, mission_id),
           f'watch_proc#{seq}')
    # 登记待认领队列：专用服回连握手 proto 为空（无 field1），
    # game_server 按来源主机（FIFO）认领本实例并回正确的 preset/地图。
    _PENDING_HANDSHAKE.append(int(mission_id))

    try:
        await asyncio.wait_for(inst['ready'].wait(), timeout)
        _log(f'combat server #{seq} handshake ok '
             f'(pid={inst["proc"].pid}, mission={mission_id})')
        # ★ 地图加载等待不再阻塞 Admitted：握手成功即返回，让客户端 hng.exe
        #   的地图加载与专用服地图加载【并行】（客户端连不上 UDP 会自动重试，
        #   此前串行等待 = 30-45s ZServer 加载 + 20-30s 客户端加载 ≈ 60-75s）。
        #   bind 探测改为后台任务，仅记日志便于排查"地图加载慢 vs 启动失败"。
        _spawn(_log_udp_bound_bg(seq, port), f'udp_probe#{seq}')
        return True
    except asyncio.TimeoutError:
        alive = inst['proc'].returncode is None
        _log(f'combat server handshake TIMEOUT after {timeout}s '
             f'(alive={alive}, exit={inst["proc"].returncode}) - 推 Admitted 继续，'
             f'查 combat-server\\logs\\combat-server-{seq}.log')
        return False
