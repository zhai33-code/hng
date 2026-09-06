"""战斗服生命周期管理（combat_instances）。

多实例 combat server 管理（每场战局一个实例，动态 UDP 端口）：按需拉起
hng.exe /D 专用服、握手认领（FIFO）、进程退出监视兜底回收、端口池分配、
战局结束释放与并发上限排队消费。从 matchmaking.py 拆出
（2026-09-06 重构，纯移动行为不变）。
"""
import asyncio
import os
import random
import socket
import subprocess
import time

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
# 战局开局等待（热身）时间覆盖，单位【秒】；None = 不覆盖（player.dll 默认）。
# 依据 work/hng-server-re/warserver-serversystemevents.md 4.3 节：
#   开局倒计时由 battlefield 实体(class 5) block+4 驱动，服务端默认 7200s 硬编码
#   （ZGameMode::CreateInstance @0x1802A4796）；正确覆盖方式是启动参数
#   /R controllers.overrideStartCountdown=<秒>（ZOption group controllers），
#   而非改 missionpreset f6（实测不生效）。
COMBAT_START_COUNTDOWN_OVERRIDE = 10

# TTD 时间旅行录制（可选）：设环境变量 TTD_RECORD=1 后，combat server 由
# WinDbg 自带 TTD.exe 包裹启动，崩溃后 work\ttd-traces\ 下生成 .run 轨迹，
# 可在 WinDbgX 中倒放定位"哪条指令写坏了对象池内存"。
# 注意：录制需管理员权限（realtest 本就以管理员运行，子进程继承即可）。
TTD_EXE = r'd:\HnG-test\work\ttd\TTD.exe'
TTD_OUT_DIR = r'd:\HnG-test\work\ttd-traces'

# ===== 多实例 combat server 管理（每场战局一个实例，动态 UDP 端口）=====
# CombatInstance dict：{mission_id, port, proc, ready(asyncio.Event), seq, ttd}
COMBAT_INSTANCES = {}          # mission_id -> CombatInstance
PENDING_MATCHES = []           # 并发上限排队中的成局 [match, ...]（FIFO）
_combat_seq = random.randint(100000, 999999)   # combat 实例序号（也用于 combat-server-<seq>.log
                                               # 文件名，随机起始避免重启后覆盖旧日志）
COMBAT_READY = asyncio.Event()  # 兼容回退：无法按 missionid 定位实例时置位
# 实测（realtest_game.log 全量历史）专用服 RequestBattleFieldSettings 的
# proto 为空（无 field1=missionid），无法自报归属 → 按启动顺序 FIFO 认领。
_PENDING_HANDSHAKE = []        # 已拉起、等待握手认领的 mission_id（启动顺序）


def claim_pending_combat_instance():
    """空 proto 握手认领：返回最早一个「已拉起且未 ready」实例的 mission_id。

    跳过已就绪/已释放的陈旧条目（懒清理）；全部无效返回 None。
    每个专用服连接只发一次 RequestBattleFieldSettings，认领即出队。
    """
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


async def _watch_proc_exit(inst, mission_id):
    """combat server 进程退出监视：退出而坑位未释放（崩溃/被杀/earnings
    处理中途异常，正常倒计时关服路径 release_match 已先到则 no-op）时
    兜底回收，保证「进程关了 = 坑位必释放」。"""
    try:
        await inst['proc'].wait()
    except Exception:
        pass
    if COMBAT_INSTANCES.get(int(mission_id)) is inst:
        _log(f'combat server #{inst["seq"]} exited without release '
             f'(mission={mission_id}, code={inst["proc"].returncode}) - '
             f'reclaiming slot')
        # ★ 未走 earnings 即退出（玩家中途退出 code=10016 / 崩溃）：必须补
        #   _finish_mission_sessions（squad 重置 + PMS DELETE KVCS），客户端
        #   closing 流程等的就是这个会话结束信号；不推则只能每秒轮询
        #   GetMissionDetails 拿 mission_not_found，直到超时自行 Abandon
        #   （2026-08-27 卡 closing 56s 根因）。earnings 正常路径已先
        #   release_match（实例出队），这里不会重复触发；且函数末尾统一
        #   清 PMS 映射，天然幂等。延迟 import 防 combat→matchmaking 循环。
        from handlers import combat as _combat_mod
        _spawn(_combat_mod._finish_mission_sessions(int(mission_id), None),
               f'finish_sessions#{inst["seq"]}')
        release_match(int(mission_id))


def _alloc_combat_port():
    """从端口池分配空闲 UDP 端口（跳过运行中实例已占用的）。"""
    used = {i['port'] for i in COMBAT_INSTANCES.values()}
    for p in range(mm_config.COMBAT_PORT_BASE, mm_config.COMBAT_PORT_MAX + 1):
        if p not in used:
            return p
    return None


async def _stop_instance(inst):
    """停掉单个战局实例（TTD 包裹时杀进程树防 hng.exe 孤儿；模拟实例关 UDP）。"""
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
    """并发上限释放后，按成局先后顺序拉起排队战局。"""
    import matchmaking  # 延迟 import：撮合核心在 matchmaking（防环）
    while PENDING_MATCHES:
        if (mm_config.MAX_CONCURRENT_COMBAT_SERVERS and
                len(COMBAT_INSTANCES) >= mm_config.MAX_CONCURRENT_COMBAT_SERVERS):
            break
        match = PENDING_MATCHES.pop(0)
        _log(f'pending match launching (mission players='
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
             f'(map load slow or failed) - check combat-server\\combat-server-{seq}.log')


async def _ensure_combat_server(mission_id, port, pms_id, combat_map,
                                timeout=120):
    """拉起本场战局的专用战斗服并等待 BattleFieldSettings 握手完成。

    多实例并行：/D 端口按局分配；不再杀旧实例（战局生命周期由
    release_match 管理）。超时也放行（返回 False）：客户端 hng.exe
    连不上 UDP 会重试，且日志有记录便于排查。
    """
    global _combat_seq
    _combat_seq += 1
    inst = {'mission_id': int(mission_id), 'port': int(port), 'proc': None,
            'ready': asyncio.Event(), 'seq': _combat_seq, 'ttd': False,
            'udp': None}

    # macOS/Linux 等无 hng.exe 环境（MM_SIMULATE_COMBAT=1）：内嵌模拟——
    # bind UDP 端口（占坑 + 让 _wait_udp_bound 探测判定 READY）+ 直接完成
    # 握手。实例登记/并发上限/释放与真实路径完全一致。
    if mm_config.SIMULATE_COMBAT:
        COMBAT_INSTANCES[int(mission_id)] = inst
        _log(f'combat server #{_combat_seq} SIMULATED (mission={mission_id}, '
             f'udp/{port}, map={combat_map})')
        try:
            _transport, _proto = await asyncio.get_event_loop() \
                .create_datagram_endpoint(asyncio.DatagramProtocol,
                                          local_addr=('0.0.0.0', port))
            inst['udp'] = _transport
        except OSError as e:
            _log(f'simulated combat udp bind failed (udp/{port}): {e}')
        inst['ready'].set()
        return True

    COMBAT_INSTANCES[int(mission_id)] = inst
    exe = os.path.join(COMBAT_SERVER_DIR, 'hng.exe')
    args = [exe,
            '/D', str(port),
            '/S', f'session-{mission_id}',
            '/P', str(pms_id),                # boot.playersessionid = MissionSession ID
            '/PS', f'login-{pms_id}',
            '/M', combat_map,
            '/W', COMBAT_WARSERVER,
            # /R 选项必须用完整注册名（组.选项）：ZOptionSet 按全名查找，
            # 无组前缀的名字会落入未注册选项桶，游戏代码不会读取
            '/R', 'boot.disable_eac=1',
            '/R', f'boot.logfile=combat-server-{_combat_seq}.log',
            # 战斗服进程 player.dll 进图时 GetTotals 初始化勋带状态（a2[7]），
            # 失败→整局无 XP 累加→无 Statistics_Update→客户端无升级弹窗
            '/R', f'boot.statserver={PUBLIC_HOST}',
            '/R', 'boot.statserverport=15115']
    if COMBAT_START_COUNTDOWN_OVERRIDE is not None:
        # 开局等待时间覆盖（秒），见文件头 COMBAT_START_COUNTDOWN_OVERRIDE 注释
        args += ['/R',
                 f'controllers.overrideStartCountdown={COMBAT_START_COUNTDOWN_OVERRIDE}']
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
            inst['proc'] = await asyncio.create_subprocess_exec(
                *launch_args, cwd=COMBAT_SERVER_DIR,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            break
        except Exception as e:
            _log(f'launch combat server FAILED (attempt {attempt}/2): {e}')
            if attempt == 1:
                # 换端口重试一次（原端口可能被外部进程抢占）
                new_port = _alloc_combat_port()
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
    # 进程退出监视（崩溃/被杀兜底回收坑位；正常关服路径幂等 no-op）
    _spawn(_watch_proc_exit(inst, mission_id),
           f'watch_proc#{inst["seq"]}')
    # 登记待认领队列：专用服回连握手 proto 为空（无 field1），
    # game_server 按 FIFO 认领本实例并回正确的 preset/地图。
    _PENDING_HANDSHAKE.append(int(mission_id))

    try:
        await asyncio.wait_for(inst['ready'].wait(), timeout)
        _log(f'combat server #{_combat_seq} handshake ok '
             f'(pid={inst["proc"].pid}, mission={mission_id})')
        # ★ 地图加载等待不再阻塞 Admitted：握手成功即返回，让客户端 hng.exe
        #   的地图加载与专用服地图加载【并行】（客户端连不上 UDP 会自动重试，
        #   此前串行等待 = 30-45s ZServer 加载 + 20-30s 客户端加载 ≈ 60-75s）。
        #   bind 探测改为后台任务，仅记日志便于排查"地图加载慢 vs 启动失败"。
        _spawn(_log_udp_bound_bg(_combat_seq, port),
               f'udp_probe#{_combat_seq}')
        return True
    except asyncio.TimeoutError:
        alive = inst['proc'].returncode is None
        _log(f'combat server handshake TIMEOUT after {timeout}s '
             f'(alive={alive}, exit={inst["proc"].returncode}) - 推 Admitted 继续，'
             f'查 combat-server\\combat-server-{_combat_seq}.log')
        return False
