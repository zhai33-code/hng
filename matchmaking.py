"""匹配/战局模块（matchmaking）。

客户端匹配流程（HnG 协议，依据反编译源码）：
  1. 点击 To Battle → 发 JoinMatchMakingRequest → 服务器回 JoinMatchMakingResponse(ok)
  2. 服务器推 PlayerMatchMaking 表 'set'（新行）→ 客户端进入"匹配中"界面
     （MatchmakingModel.onMatchMakingChange ADD）
  3. 匹配成功（本模块模拟：延迟数秒）：推 PlayerMissionSession 'set'
     (connectionState=Admitted=2) → 客户端拉 GetMissionDetailsRequest(missionId)
     显示简报/部署界面；同时 PlayerMatchMaking 'delete'（客户端退出匹配中）
  4. CancelJoinMatchMakingRequest → CancelJoinMatchMakingResponse + 'delete' 匹配行

关键事实（来自反编译）：
- 表名 xmlTag：PlayerMatchMakingTable='PlayerMatchMaking'，
  PlayerMissionSessionTable='PlayerMissionSession'（表推送走 KeyValueChangeSet）
- 时间格式：客户端 TimeUtilities.convertZuluTimeToMilliseconds 解析 'YYYYMMDDHHmmssZ'
- delete 动作 id：8 字节小端 int64（客户端 Chunk.getAsInt64）
- PlayerMatchMaking DELETE 后客户端只做本地 FSM 重置（不发网络请求，无死循环）
- GetMissionDetailsResponse 的 repeated 空字段必须完全省略（hurlant 库 Buffer overflow 坑）

本模块依赖 protocol 编解码、wardb 静态表（军衔阈值）与注入的 STORE/log，
由 realtest_server.py 的 dispatch 分发调用。
"""
import asyncio
import os
import random
import socket
import struct
import subprocess
import time
import uuid

from protocol import Chunk, build_response, pb_parse, pb_bool, pb_parse_multi
from protocol import (
    pb_float, pb_int32, pb_int64, pb_message, pb_string, pb_uint32, pb_varint,
)
from state import FLASH_SESSIONS, PUBLIC_HOST, STORE
from wardb import (
    _load_accesspoint_templates, _load_capturepoint_templates, _load_preset,
    _rank_from_combat_xp, battlefield_entity_for_gamemap, battlefield_pos,
    commandnode_template, preset_scene, squad_template_for_character,
    squad_template_name,
)
import mm_config
import mm_pool
import force_pool

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
#   4. 兜底开发机默认路径
def _detect_combat_dir():
    env = os.environ.get('HNG_COMBAT_SERVER_DIR')
    if env:
        return os.path.abspath(env)
    from state import GAME_DIR
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for cand in (os.path.join(pkg_parent, 'combat-server'),
                 os.path.join(GAME_DIR, 'combat-server')):
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


def _spawn(coro, label=''):
    """后台任务统一入口：异常兜底记日志（裸 ensure_future 的异常只会在
    GC 时打 "exception was never retrieved"，且无人善后）。"""
    async def _wrapped():
        try:
            await coro
        except asyncio.CancelledError:
            pass
        except Exception as e:
            import traceback
            _log(f'[mm-task] {label or coro} CRASHED: {type(e).__name__}: {e}')
            _log(traceback.format_exc())
    try:
        return asyncio.ensure_future(_wrapped())
    except RuntimeError:
        coro.close()                 # 事件循环未运行（测试环境）：丢弃避免告警
        return None


def notify_combat_ready(mission_id=None):
    """game_server.py 响应 RequestBattleFieldSettings 后调用：
    按 missionid 定位实例置 ready（可推 Admitted）；查不到回退全局事件。"""
    inst = COMBAT_INSTANCES.get(mission_id) if mission_id else None
    if inst is not None:
        inst['ready'].set()
    else:
        COMBAT_READY.set()
    # populating→running 转变：刷新排队玩家 UI 的战局统计（f22/f23）
    _spawn(_push_queue_counts(), 'push_queue_counts')


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
    inst = COMBAT_INSTANCES.pop(int(mission_id), None)
    if inst is not None:
        _log(f'releasing combat server #{inst["seq"]} '
             f'(mission={mission_id}, udp/{inst["port"]})')
        _spawn(_stop_instance(inst), f'stop_instance#{inst["seq"]}')
    _MM_MISSION_DETAILS.pop(int(mission_id), None)
    _MM_MISSION_SQUADS.pop(int(mission_id), None)
    release_mission_resources(mission_id)
    _drain_pending_matches()
    # 端口/坑位已释放：池内可能有待撮合的玩家（如端口耗尽时回池的局）
    _spawn(_try_form_all(), 'try_form_all(release)')
    # 战局数变化：刷新排队玩家 UI 的战局统计（f20-f23）
    _spawn(_push_queue_counts(), 'push_queue_counts(release)')


def _drain_pending_matches():
    """并发上限释放后，按成局先后顺序拉起排队战局。"""
    while PENDING_MATCHES:
        if (mm_config.MAX_CONCURRENT_COMBAT_SERVERS and
                len(COMBAT_INSTANCES) >= mm_config.MAX_CONCURRENT_COMBAT_SERVERS):
            break
        match = PENDING_MATCHES.pop(0)
        _log(f'pending match launching (mission players='
             f'{len(match.players)}, queued={len(PENDING_MATCHES)})')
        _dispatch_match(match)


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
                _release_combat_port(int(port))
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


# 由 realtest_server.py 注入（避免重复建 PostgreSQL 连接池 / 日志句柄）
STORE = None
_LOG_FN = lambda msg: None


def configure(store, log_fn):
    """注入 player_store 实例与日志函数（realtest_server 启动时调用）。"""
    global STORE, _LOG_FN
    STORE = store
    _LOG_FN = log_fn


def _log(msg):
    _LOG_FN(f'[MM] {msg}')


# ---------------------------------------------------------------- 状态
# ===== 运行时 id / 库引用常量（他服回放值已清除：id 现生成，库值读 WarDatabase）=====
# 客户端设备描述串（PMS f19 metricsDeviceId）：非 id、无库表可读，本服固定值
METRICS_DEVICE_ID = 'Flash;HnG;WIN 32,0,0,170;mozilla/5.0 (windows; u; zh-cn) applewebkit/533'
# 本服战争 id：战争是服务器侧运行时实体（客户端无本地表可查，任意值行为一致），
# 每次服务器启动生成新 id = 开一场新战争；faction 表行 f5 war 与 PMS f15 warId 同源用它
_WAR_ID = random.getrandbits(63)
# mission_info f5 mapEntityId：必须是客户端 WarDatabase battlefield 表真实行 id
# （112741325 = 'Benoit Village'）。Mill 等遭遇战图在 battlefield 表无行时用它兜底，
# 客户端简报按它查战场名（battlefield_entity_for_gamemap 动态匹配的回退值）。
MISSION_MAP_ENTITY_ID = 112741325
# mission_info f10 hostingCenterId：服务器侧基础设施 id（WarDatabase 无对应表，
# 客户端仅存储不查表）→ 每次启动生成（原值为他服回放 id）
MISSION_HOSTING_CENTER_ID = random.getrandbits(63)

# ===== 他服私服 AI 战局实测值（2026-08-18 抓包：双人局 ai_battle_15130.pcapng / 单人局 match_to_game.pcapng）=====
# 单人局（match_to_game.pcapng, 2026-08-18 21:23, DoubleBird 单人 vs AI 苏德）：
#   GetMissionDetailsResponse 758B 完整 dump 见 solo_gmd_dump.txt / solo_gmd_response.bin
#   我方 6 队（303×2/320×2/316×2）+ AI 6 队（203×2/220×2/216×2），资源 20 条
#   资源总量：步兵 90×4+36×2=432、汽车 30×2=60、摩托 12×2=24（与游戏内一致）

# faction 表行 id / supremecommand：服务器侧运行时实体（客户端无本地表可查，
# 任意值行为一致，仅要求 faction 行与 GMD/PMS/社交列表引用同源）→ 每次启动生成，
# 与 _WAR_ID 同策略（原值为他服 solo_kvcs_dump2 回放 id，已清除）。
# 客户端 FactionTable(xmlTag='faction') 只有收到这些 set 才有数据，
# GetMissionDetailsResponse 的 f3/f4/f5 field1 必须用这些行 id（不是 1/2/3 小整数），
# 否则 FactionTable.getFromId 返回 null → setPlayersInMatch TypeError #1009。
FACTION_ROW_IDS = {tmpl: random.getrandbits(63) for tmpl in (1, 2, 3)}
FACTION_SUPREME_COMMANDS = {tmpl: random.getrandbits(63) for tmpl in (1, 2, 3)}


def _rival_faction(faction):
    """单阵营局的 AI 对手阵营（GE 玩家 → SU，其余 → GE）。

    原先该规则在 _split_factions(L921) 与 AI 局简报(L850) 各写一份
    `2 if x != 2 else 3`——改一漏一即错位，收敛到这一处。
    """
    f = int(faction)
    return 3 if f == 2 else 2


def _faction_pair(det):
    """读取战局详情的攻防两阵营，字段缺失/非法时报错并返回 None。

    ★ 与 _find_joinable_mission 同一教训（2026-08-27 美德局漏放苏军）：
    det 阵营字段默认值（3=SU/2=GE）看似无害，实为静默放行错阵营——
    BattleFieldSettings 会按默认阵营配出生侧（判负+观战）、简报 f8 攻防
    错位、FACTION_ROW_IDS.get 回退错行 id（客户端 #1009）。凡读取方
    拿到 None 必须走"拒绝/回退"路径，不允许再用默认值渲染。
    """
    atk = int(det.get('attacker_faction', 0) or 0)
    dfd = int(det.get('defender_faction', 0) or 0)
    if not (atk in (1, 2, 3) and dfd in (1, 2, 3) and atk != dfd):
        _log(f'ERROR: mission details faction fields invalid: '
             f'attacker={atk}, defender={dfd} (det={det.get("preset")})'
             f' - refusing defaults')
        return None
    return atk, dfd


def _msg_faction_rows():
    """KVS 'faction' 表 3 行（US/GE/SU，结构按他服 solo_kvcs_dump2 S2C#17 复刻）。"""
    rows = []
    for tmpl, fid in FACTION_ROW_IDS.items():
        m = pb_int64(1, fid)                                   # id
        m += pb_int64(2, tmpl)                                 # factiontemplateid
        m += pb_int64(4, FACTION_SUPREME_COMMANDS[tmpl])       # supremecommand
        m += pb_int64(5, _WAR_ID)                              # war（本服运行时战争 id）
        m += pb_float(6, 1.0)                                  # underdogBonusXpMultiplier
        m += pb_float(7, 1.0)                                  # underdogBonusWarfundsMultiplier
        rows.append(m)
    return rows
AI_MISSIONPRESET = 10                   # mission_info f3（AI 模式=10；多人模式=29）
AI_PLAYER_COUNT = 2                     # mission_info f9（他服单人AI局实测=2）

# ---- 战局天气/时段预设池（成局时随机抽取，简报与战斗服同源）----
# 枚举取自客户端 proto（decompiled proto-as3-classes/HnG_States）：
#   WeatherTimeOfDay 0=夜晚 1=黎明 2=白天 3=黄昏
#   WeatherCondition 0=少云 1=阴天 2=雾
# 三元组 = (weatherTimeOfDay, weatherCondition, timeofday 小时制)；
# timeofday 为 BattleFieldSettings f9（战斗内光照/太阳角度，24 小时浮点）。
# 同一组合重复列出 = 提高出现权重（白天·少云最常见，夜雾不设）。
WEATHER_PRESETS = [
    (2, 0, 12.0),   # 白天·少云
    (2, 0, 12.0),   # 白天·少云（权重加倍）
    (2, 1, 12.0),   # 白天·阴天
    (2, 2, 12.0),   # 白天·雾
    (1, 0, 6.0),    # 黎明·少云
    (1, 1, 6.0),    # 黎明·阴天
    (3, 0, 18.0),   # 黄昏·少云
    (3, 1, 18.0),   # 黄昏·阴天
    (0, 0, 0.0),    # 夜晚·少云
    (0, 1, 0.0),    # 夜晚·阴天
]


def roll_weather():
    """成局时随机抽一套天气/时段（存入 _MM_MISSION_DETAILS，全链路共用）。

    返回 dict 直接并入 mission details：weather_tod / weather_condition /
    timeofday（战斗服 BattleFieldSettings f9/f21/f22 与简报 MissionInfo
    f12/f13 均从这里取，保证简报图标与实际战斗一致）。"""
    tod, cond, hour = random.choice(WEATHER_PRESETS)
    return {'weather_tod': tod, 'weather_condition': cond, 'timeofday': hour}
# f4 突击队构成：本局 force_pool 抽取的 AT 名单（双侧镜像，名单见
# force_pool.init_mission 日志）。模板 id 是【阵营相关】库值
# （commandnodetemplate f2 兵种名 × f3 阵营：infantry US=103/GE=203/SU=303），
# 按参战阵营现查 WarDatabase。无池回退官方锚点组合（三兵种 ×2）。
ASSAULT_TEAM_NODE_NAMES = ('infantry', 'motorized_infantry', 'motorized_guard')


def _assault_team_nodes(faction, mission_id=None):
    """阵营突击队 commandNode 模板 id 序列（本局 AT 抽取名单跨阵营翻译）。"""
    names = force_pool.assault_team_names(mission_id) or \
        list(ASSAULT_TEAM_NODE_NAMES) * 2
    faction = int(faction) if int(faction) in (1, 2, 3) else 3
    return [commandnode_template(faction, n) for n in names]


def _mission_accesspoints(gamemap_id):
    """本图出兵点模板（WarDatabase/accesspointtemplate f2==gamemap 分组，升序
    = A/B 点攻防序）。"""
    return _load_accesspoint_templates(int(gamemap_id))


def _mission_capturepoints(gamemap_id):
    """本图据点模板（WarDatabase/CapturepointTemplate f2==gamemap 分组）。
    初始归属 0=中立（他服实测 f4=0）。"""
    return _load_capturepoint_templates(int(gamemap_id))


# ---------------------------------------------------------------- 每局资源池（单一事实源）
# 2026-08-26：资源池改由 force_pool 模块生成 —— 按模式随机抽取突击队(AT)
# 编制（WarDatabase/commandnodetemplatearmyresourcetemplaterelation 原值，
# 乘员:载具天然保持官方 1:1~1.25:1，伞兵:运输机=4:1），池 = Σ AT 编制，
# 双侧镜像同池。此前手写的 MISSION_RESOURCE_CATS / AI_TEAM_RESOURCES 静态
# 数值为自造值（非官方），已废弃删除。
# f5（匹配界面）/ f14（握手）/ f2（轮询）三处单一事实源，出生消耗由
# ReportMissionStatusRequest f2 statuslist（§8.1 ReportArmyResourceSpawnCount
# {f1 ap, f2 cat, f5 count, f6 faction}，实测每 10s 增量上报，AI/玩家出生
# 均含）按兵种扣减。渲染类别序与 spawnInAir 统一走 force_pool.categories()。
# 部署界面按资源条目判定各兵种可用生命数 —— 缺空军条目时飞行员即使有
# 飞机也不显示乘机槽（只能徒步部署）。

def _init_mission_resources(mission_id, gamemode_id, attacker, defender):
    """成局时建立资源池（按模式抽 AT，攻防双方镜像同编制）。"""
    force_pool.init_mission(mission_id, gamemode_id, attacker, defender)


def mission_resource_pool(mission_id):
    """读本局资源池；无池返回 None（调用方回退初始表）。"""
    return force_pool.pool(mission_id)


def _pool_counts(pool, faction):
    """池中某阵营各类剩余；池/阵营缺失回退锚点值（官方 Encounter 池）。"""
    if pool and int(faction) in pool:
        return pool[int(faction)]
    return force_pool.counts_for(None, faction)


def consume_mission_resource(mission_id, faction, cat, n=1):
    """玩家出生消耗资源（按兵种类别扣 n）。返回扣后余量；池/类别缺失返回 None。"""
    return force_pool.consume(mission_id, faction, cat, n)


def release_mission_resources(mission_id):
    """局结束清理（与 _MM_MISSION_DETAILS 同生命周期）。"""
    force_pool.release(mission_id)


def _msg_battle_info(battle_id, pos_x, pos_y):
    """HnG_States.BattleInfo proto（PMS Armed 后必须推送，否则客户端不连 game server）。

    私服抓包 frame 0xa636 字段：
      field 1 battleId (int64) = 5752758341220508668
      field 2 posX (f32)      = 9984.054
      field 3 posY (f32)      = 4387.6885
    """
    m = b''
    m += pb_int64(1, battle_id)         # battleId
    m += pb_float(2, pos_x)             # posX
    m += pb_float(3, pos_y)             # posY
    return m

# 局内动态 id 发号：基值每次进程启动随机初始化，避免重启后 id 从头复用
# （客户端可能缓存旧局 id，重启后撞号会导致实体绑定错乱）。
_MM_MISSION_ID = [random.randint(100001, 999000)]    # missionId 自增（保持 6 位小整数风格）
_MM_PMM_ID = [random.randint(200001, 999000)]        # PlayerMatchMaking 行 id 自增
_MM_PMS_ID = [random.getrandbits(63)]  # PlayerMissionSession 行 id 自增
                                     # (=握手 id1 / hng.exe /P 参数, 快照实体按它绑定, 必须大整数)
_MM_SOLO_SMS_ID = [random.getrandbits(63)]           # 单人隐式小队 SquadMemberSession 行 id 自增
# 每局动态 id 自增（2026-08-20：PlayerSettings 回放字段动态化）：
#   metrics_session_id → PMS f18 / PlayerSettings f23（必须同值）
#   battle_session_id  → PMS f9  / PlayerSettings f25（同源）
#   squad_id           → PlayerSettings f16 / SquadResponse f1（ZServer squad 树键）
_MM_METRICS_SESSION_ID = [random.getrandbits(63)]    # metricsSessionId 自增
_MM_BATTLE_SESSION_ID = [random.getrandbits(63)]     # battle session id 自增
_MM_SQUAD_ID = [random.getrandbits(63)]              # squadId 自增

# 局内小队分组登记：mission_id -> {squad_id: {
#   name, leader_char_id, faction, squad_db_id(真小队=DB id / 单人凑队=0),
#   members: [{player_id, char_id, gamertag, rank, slot}] }}
# _start_match 成局时写入；game_server SquadResponse 按此构建局内小队定义
# （队名/队长/每成员槽位）；release_match 清理。
_MM_MISSION_SQUADS = {}
_MM_SOLO_SQUAD_MAX = 6        # 单人匹配自动凑队的人数上限


def _next_match_id(seq):
    """局内动态 id 发号（[_MM_*] 序列共用：squad/metrics/battle session）。"""
    seq[0] += 1
    return seq[0]


_MM_MISSION_DETAILS = {}    # mission_id -> {preset, players, map_entity_id, ai_fill, gamemode}
                            #（旧 AI 局结构 {player_id, gamertag, faction} 兼容保留）
_MM_QUEUES = {}             # player_id -> {pmm_id, mission_id, gamemode, ctx}（取消匹配用）
_MATCH_PMM_IDS = {}         # player_id -> 排队中 PlayerMatchMaking 行 id（成局时 delete 用）
_SOLO_SQUADS = {}           # player_id -> {squad_row, member_row, mission_id, faction,
                            #              template, char_id, gamertag, char_name, rank}
                            # 单人隐式小队行（官方 solo_kvcs_dump2.txt：单人匹配也推
                            # SquadSession + SquadMemberSession，同一玩家会话复用行 id）
POOL = mm_pool.MatchPool()  # 匹配池（gamemode 分池 + 选图交集撮合，见 mm_pool.py）
_AI_FILL_TASKS = {}         # player_id -> 超时 AI 补位任务（人数不足时兜底开局）


def _msg_solo_squad_session(row_id, owner_pid, template, faction,
                            mission_id=0, action_games_entered=0):
    """单人隐式小队 SquadSession proto（官方 solo 抓包 S2C#4/#12/#16 同构）。

    官方值（solo_kvcs_dump2.txt）：f18 squadId=0（非真小队，UI 占位），
    f10 name=''，f8 totalCustomizationPoints=1；匹配成功后 f17 missionId=实际
    mission id、f19 actionGamesEntered=1。
    ★ f17/f19 必须更新：abandon 后 PMS DELETE → StatusMediator.onLobbySignalUpdates
      读 squadSession.missionId（null 时 TypeError #1009 卡 processing；非零则
      LOCKED_IN_COMBAT 卡战斗中状态）——missionId=0 → isOwnedByPlayer → BASE_STATE。
    """
    m = b''
    m += pb_int64(1, row_id)                 # id（=squadSessionId）
    m += pb_int64(2, owner_pid)              # ownerPlayerId（自己）
    m += pb_int64(3, template)               # characterTemplateId
    m += pb_int64(4, faction)                # factionTemplateId
    m += pb_int64(5, 0)                      # supportSlotTemplateId1
    m += pb_int64(6, 0)                      # supportSlotTemplateId2
    m += pb_int64(7, 0)                      # supportSlotTemplateId3
    m += pb_int32(8, 1)                      # totalCustomizationPoints（官方=1）
    m += pb_int32(9, 0)                      # usedCustomizationPoints
    m += pb_string(10, '')                   # name（官方单人=空）
    m += pb_bool(12, False)                  # matchMakeWarMissionFlag
    m += pb_int64(17, mission_id)            # missionId（匹配成功=战局 id，其余 0）
    m += pb_int64(18, 0)                     # squadId（★0=隐式小队，不可定制）
    m += pb_int32(19, action_games_entered)  # actionGamesEntered
    m += pb_bool(20, False)                  # isLockedForMembers
    m += pb_bool(21, False)                  # isLockedForCustomization
    m += pb_bool(22, False)                  # forceReady
    return m


async def _msg_solo_squad_member_session(row_id, squad_row_id, pid, gamertag,
                                   char_id, char_name, rank, status,
                                   player_session_id, faction):
    """单人隐式小队 SquadMemberSession proto（官方 solo 抓包同构）。

    status：3=Ready（匹配中，官方 S2C#4）、4=InBattle（进战局，S2C#12）、
            2=InLobby（回大厅）。f13 照官方推 (1,2)。
    f8/f19：角色装备的迷彩/头盔模板 id（小队头像按此渲染外观；
    0 = 未装备 → 默认军服）。
    """
    uvt, upt = 0, 0
    try:
        uvt, upt = await STORE.get_character_appearance(int(pid), int(char_id))
    except Exception as e:
        _log(f'      [DB] solo squad appearance lookup failed: {e}')
    m = b''
    m += pb_int64(1, row_id)                 # id
    m += pb_int64(2, squad_row_id)           # squadSessionId
    m += pb_int64(3, 1)                      # squadPlayerSlotTemplateId（官方=1）
    m += pb_int64(4, pid)                    # playerId
    m += pb_string(5, gamertag)              # gamerTag
    m += pb_int64(6, 0)                      # playerFactionTemplateId（官方=0）
    m += pb_int64(7, char_id)                # characterId
    m += pb_int64(8, uvt)                    # uniformVariantTemplateId
    m += pb_int64(9, 0)                      # weaponVariantTemplateId
    m += pb_int32(10, rank)                  # characterRank
    m += pb_int64(11, 0)                     # shopMembershipItemId
    m += pb_int32(12, status)                # squadMemberSessionStatus
    m += pb_int64(13, 1)                     # verifiedSquadResourceTemplates
    m += pb_int64(13, 2)
    m += pb_string(14, char_name)            # characterName
    m += pb_int64(15, player_session_id)     # playerSessionId（官方=PMS f18）
    m += pb_int32(16, 0)                     # playerTier
    m += pb_int32(17, 0)                     # squadCustomizationPoints
    m += pb_bool(18, False)                  # wasInvited
    m += pb_int64(19, upt)                   # uniformPartTemplateId
    m += pb_int64(20, faction)               # characterFactionTemplateId
    return m
PMS_TO_MISSION = {}         # pms_id -> mission_id（game_server 按启动参数 /M 反查）
PMS_TO_SESSION = {}         # pms_id -> {player_id, char_id, gamertag, faction,
                            #            metrics_uid, metrics_session_id,
                            #            battle_session_id, squad_id, quickplay}
                            # ★ game_server 用它把 PlayerSettings f12 characterlist
                            #   / f15 missionCharacterId 换成玩家账号库里的真实角色
                            #   （客户端 RequestReadCharacterStats 请求的就是这些
                            #    characters 表小 id，回放他服 6360... 大 id 完全对不上）。
                            #   metrics/squad/battle_session 每局动态生成，
                            #   PlayerSettings(回放补丁) 与 PMS/SquadResponse 同源取值。


def _zulu_now():
    """客户端 TimeUtilities 解析的 UTC 时间字符串 YYYYMMDDHHmmssZ。"""
    return time.strftime('%Y%m%d%H%M%S', time.gmtime()) + 'Z'


# ---------------------------------------------------------------- 推送
async def _push_kvcs(writer, view_cmd, table_name, proto_data):
    """KeyValueChangeSet 'set' 动作（表行 ADD/UPDATE）。"""
    data_chunk = Chunk(proto_data)
    table_chunk = Chunk(table_name, [data_chunk])
    action_chunk = Chunk(b'set', [table_chunk])
    title_chunk = Chunk(b'KeyValueChangeSet', [action_chunk])
    main = Chunk(struct.pack('<i', view_cmd), [title_chunk])
    writer.write(main.write(b''))
    await writer.drain()


async def _push_kvcs_delete(writer, view_cmd, table_name, row_id):
    """KeyValueChangeSet 'delete' 动作（删除表行，row_id 为 8 字节小端 int64）。"""
    id_chunk = Chunk(struct.pack('<q', int(row_id)))
    table_chunk = Chunk(table_name, [id_chunk])
    action_chunk = Chunk(b'delete', [table_chunk])
    title_chunk = Chunk(b'KeyValueChangeSet', [action_chunk])
    main = Chunk(struct.pack('<i', view_cmd), [title_chunk])
    writer.write(main.write(b''))
    await writer.drain()


# ---------------------------------------------------------------- proto 构造
def _mm_live_stats():
    """实时战局统计（PMM f20-f23，排队 UI「战斗中/组局中/进行中」数字）：
    in_game = 活跃会话数（PMS_TO_SESSION 中仍属运行实例的行）；
    populating = 并发上限排队局 + 已拉起未握手完成的实例；
    running = 握手完成（ready）的实例数。"""
    in_game = sum(1 for pms_id in PMS_TO_SESSION
                  if PMS_TO_MISSION.get(pms_id) in COMBAT_INSTANCES)
    populating = len(PENDING_MATCHES) + sum(
        1 for i in COMBAT_INSTANCES.values() if not i['ready'].is_set())
    running = sum(1 for i in COMBAT_INSTANCES.values() if i['ready'].is_set())
    return in_game, populating, running


def _msg_player_matchmaking(pmm_id, char_id, faction_id, mission_id, zulu_time,
                            queue_local=1, queue_total=1):
    """HnG_States.PlayerMatchMaking proto（匹配中状态行）。
    仅 'set' 推送；repeated 空字段完全省略（hurlant Buffer overflow 坑）。
    f18/f19 队列人数 / f20-f23 战局统计 = 实时统计（原硬编码 1/0）。"""
    in_game, populating, running = _mm_live_stats()
    m = b''
    m += pb_int64(1, pmm_id)            # id
    m += pb_int64(2, 0)                 # squadSessionId
    m += pb_string(4, zulu_time)        # timeEntered
    m += pb_string(5, '')               # metricsUserId
    m += pb_int64(6, 0)                 # metricsSessionId
    m += pb_string(7, '')               # metricsDeviceId
    m += pb_int64(8, mission_id or 0)   # missionId（0 = 随机匹配）
    m += pb_bool(10, True)              # isMatchmaking
    m += pb_int64(11, 1)                # warId
    m += pb_int64(12, faction_id)       # factionId
    m += pb_int32(13, 0)                # missionTypeMask
    m += pb_bool(14, False)             # forceStart
    m += pb_int32(15, 0)                # tier
    m += pb_float(16, 0.0)              # skillLevel
    m += pb_bool(17, False)             # playerVsBots
    m += pb_int32(18, queue_local)      # playersInQueueLocal（池内同模式人数）
    m += pb_int32(19, queue_total)      # playersInQueueTotal（池内总人数）
    m += pb_int32(20, in_game)          # playersInGameLocal（战斗中人数）
    m += pb_int32(21, in_game)          # playersInGameTotal（单服 = 同上）
    m += pb_int32(22, populating)       # missionsMatchingPopulating（组局中）
    m += pb_int32(23, running)          # missionsMatchingRunning（进行中）
    m += pb_int64(25, 0)                # missionCategoryId
    return m


def _msg_player_mission_session(pms_id, player_id, gamertag, char_id, faction_id,
                                char_name, rank, mission_id, zulu_time, game_connect,
                                metrics_uid='', metrics_session_id=0,
                                metrics_device_id='', platform='PC', connection_state=2,
                                battle_session_id=None):
    """HnG_States.PlayerMissionSession proto（匹配成功会话行）。

    私服抓包 frame 57 字段（按重要性排序）：
      field 1  id                = 大数 PMS 实例 id
      field 2  playerId          = 大数 (与 PlayerSettings field 1 一致)
      field 3  playerGamerTag    = "DoubleBird"
      field 4  characterId       = 大数 (与 PlayerSettings field 15 一致)
      field 5  characterFactionId= 大数
      field 6  characterName     = "Artyom Mironov"
      field 7  characterRank     = 13
      field 8  missionId         = 大数 (game server GetMissionDetailsRequest field 1)
      field 9  missionSessionId? = 大数 (非零)
      field 13 timeAdmitted      = "20260817141437Z"
      field 14 connectionState   = 3 ★ (本地原 2=Admitted，私服 3=Armed)
      field 15 warId?            = 大数
      field 17 metricsUserId    = UUID (与 PlayerSettings field 22 一致)
      field 18 metricsSessionId = 大数
      field 19 metricsDeviceId  = "Flash;HnG;WIN 32,0,0,170;mozilla/5.0..."
      field 20 gameConnect      = "ip:port"
      field 21 platform         = "HnG"

    connectionState 语义（HnG_States.PlayerMissionConnectionState，反编译确认）：
      1=Queued  2=Admitted  3=Entered
    ★ matchmaking 推送必须用 2 (Admitted)：ActionGameService.handleSessionUpdates
      仅在 Admitted 分支调 findPlayerMissionSessionAndLaunchGame() 拉起 hng.exe；
      推 3 (Entered) 走 "Fully entered action game" 分支 → 永不启动游戏进程。
      （GMD 请求由 KVCS ADD 事件 dispatch getBattleDetailsSignal 触发，与 state 值无关）
    game_server.py 的 hng.exe 连接侧推送用 3（触发 game server 连接上的 cmd=37）。
    """
    m = b''
    m += pb_int64(1, pms_id)                  # id（客户端 /P 启动参数）
    m += pb_int64(2, player_id)               # playerId
    m += pb_string(3, gamertag)               # playerGamerTag
    m += pb_int64(4, char_id)                 # career id（他服实测=JoinMM f1/PlayerSettings f15）
    m += pb_int64(5, FACTION_ROW_IDS.get(int(faction_id), int(faction_id)))
    # characterFactionId：必须是 KVS 'faction' 表行 id（FACTION_ROW_IDS 大数），
    # 客户端 MissionInformationWindowMediator.setAccessPoints 用它
    # FactionTable.getFromId() 查 faction 行——填 player_id/小整数都会查不到
    # → null.factiontemplateid → TypeError #1009（切换阵营颜色按钮触发）
    m += pb_string(6, char_name)              # characterName
    m += pb_int32(7, rank)                    # characterRank
    m += pb_int64(8, mission_id)              # missionId（客户端 /M 启动参数）
    if battle_session_id is None:              # 调用方未传则现发号（与 PlayerSettings f25 同源）
        battle_session_id = _next_match_id(_MM_BATTLE_SESSION_ID)
    m += pb_int64(9, battle_session_id)        # missionSessionId（非零大数，=PlayerSettings f25）
    m += pb_int64(11, 0)                      # missionAssaultTeamId
    m += pb_int32(12, 0)                      # missionAssaultTeamSlot
    m += pb_string(13, zulu_time)             # timeAdmitted
    m += pb_int32(14, connection_state)       # connectionState (2=Admitted, 3=Armed)
    m += pb_int64(15, _WAR_ID)                # warId（本服运行时战争 id，与 faction 表行 f5 同源）
    m += pb_int32(16, 0)                      # characterSpawned
    m += pb_string(17, metrics_uid)           # metricsUserId (与 PlayerSettings field 22 一致)
    m += pb_int64(18, metrics_session_id)      # metricsSessionId
    m += pb_string(19, metrics_device_id)      # metricsDeviceId
    m += pb_string(20, game_connect)          # gameConnect（游戏进程连接地址）
    m += pb_string(21, platform)              # platform (私服="HnG")
    return m


def _msg_mission_info(mission_id, player_id, zulu_time, defender_faction):
    """HnG_States.MissionInfo proto（AI 模式，他服私服 2026-08-18 抓包实测）。

    defender_faction = AI 对手阵营（_rival_faction(玩家阵营)）：f8 恒 2 的
    旧写法在 GE 玩家 AI 局会与实际 AI 方（SU）错位 → 简报攻防显示反。"""
    m = b''
    m += pb_int64(1, mission_id)               # missionId
    m += pb_int64(2, 0)                        # battleId
    m += pb_int64(3, AI_MISSIONPRESET)         # missionpreset（AI=10, 多人=29）
    m += pb_int64(5, MISSION_MAP_ENTITY_ID)    # mapEntityId（WarDatabase battlefield 表 'Benoit Village' 行 id）
    m += pb_int64(6, 1)                            # mapEntityTypeId（1=battlefield）
    m += pb_int32(7, 1)                            # isAutogenerated
    m += pb_int64(8, int(defender_faction))        # defenderFactionId（AI 局防守方）
    m += pb_int32(9, AI_PLAYER_COUNT)              # playerCount（他服单人AI局实测=2）
    m += pb_int64(10, MISSION_HOSTING_CENTER_ID)  # hostingCenterId（服务器侧基础设施 id，本服固定）
    m += pb_string(11, zulu_time)              # activationTimeStamp
    m += pb_int32(12, 1)                       # weatherTimeOfDay（他服单人AI局实测=1）
    m += pb_int32(13, 0)                       # weatherCondition
    return m


def _msg_mission_details_response(mission_id, player_id, gamertag, zulu_time,
                                  player_faction=3, gamemap_id=13):
    """GetMissionDetailsResponse proto（AI 模式，字段映射按客户端 SWC 描述符）。

    客户端 schema（proto_all/HnG_States）：
      f1 response(enum)=0 | f2 MissionInfo
      f3 MissionDetailsFaction ×2      {1: factionId, 2: players[{1: gamertag}]}
      f4 MissionDetailsAssaultTeam ×12 {1: factionId, 2: commandNodeTemplateId,
                                        3: accessPointTemplateId, 4: ownerPlayerId}
      f5 MissionDetailsArmyResource ×20{1: factionId, 2: armyResourceCategoryId,
                                        3: accessPointTemplateId, 4: count, 5: spawnInAir}
      f6 MissionDetailsAccessPoint ×2  {1: accessPointTemplateId, 2: factionTemplateId}
      f7 capturePointStatuses | f8 posX f32 | f9 posY f32
    ★ f3/f4/f5 的 factionId 是 KVS 'faction' 表的行 id（FACTION_ROW_IDS 大数），
      客户端 FactionTable.getFromId(行id) 解引用，行 id 不存在即 TypeError #1009。
    出兵点/据点模板按 gamemap 查 WarDatabase（缺省 Mill=13）；突击队模板按
    参战阵营查 commandnodetemplate。
    """
    ai_faction = _rival_faction(player_faction)
    rfid = FACTION_ROW_IDS[player_faction]   # 我方 faction 行 id
    afid = FACTION_ROW_IDS[ai_faction]       # AI 方 faction 行 id
    aps = _mission_accesspoints(gamemap_id)
    ours_ap, theirs_ap = aps[0], aps[1]
    m = b''
    m += pb_int32(1, 0)  # 他服私服实测 = 0（非 ResponseCodes.ok=1）
    m += pb_message(2, _msg_mission_info(mission_id, player_id, zulu_time,
                                         ai_faction))
    # f3 参战方：我方 {faction行id, 成员=[gamertag]}；AI 方无成员名单
    m += pb_message(3, pb_int64(1, rfid) +
                    pb_message(2, pb_string(1, gamertag)))
    m += pb_message(3, pb_int64(1, afid))
    # f4 突击队：本局 AT 抽取名单跨阵营翻译（无池回退官方锚点 ×2）
    for cmd_node in _assault_team_nodes(player_faction, mission_id):
        m += pb_message(4, pb_int64(1, rfid) + pb_int64(2, cmd_node) +
                        pb_int64(3, ours_ap) + pb_int64(4, 0))
    for cmd_node in _assault_team_nodes(ai_faction, mission_id):
        m += pb_message(4, pb_int64(1, afid) + pb_int64(2, cmd_node) +
                        pb_int64(3, theirs_ap) + pb_int64(4, 0))
    # f5 资源：从本局资源池渲染（与战斗内 f2/f14 同源同值，消耗后递减）；
    # 每方每类一条挂该方出兵点（客户端跨点求和 → 显示即池剩余量）
    pool = mission_resource_pool(mission_id)
    ours_counts = _pool_counts(pool, player_faction)
    theirs_counts = _pool_counts(pool, ai_faction)
    for res_cat, res_air in force_pool.categories(counts_by_faction=pool):
        m += pb_message(5, pb_int64(1, rfid) + pb_int64(2, res_cat) +
                        pb_int64(3, ours_ap) +
                        pb_int64(4, ours_counts.get(res_cat, 0)) +
                        pb_bool(5, res_air))
    for res_cat, res_air in force_pool.categories(counts_by_faction=pool):
        m += pb_message(5, pb_int64(1, afid) + pb_int64(2, res_cat) +
                        pb_int64(3, theirs_ap) +
                        pb_int64(4, theirs_counts.get(res_cat, 0)) +
                        pb_bool(5, res_air))
    # f6 出兵点归属：首个=我方，其余交替（Mill 实测 8=我方, 15=AI）
    for i, ap in enumerate(aps):
        m += pb_message(6, pb_int64(1, ap) +
                        pb_int64(2, ai_faction if i % 2 else player_faction))
    # f7 据点初始归属：0=中立（他服私服实测 {3:120, 4:0}）
    for cp in _mission_capturepoints(gamemap_id):
        m += pb_message(7, pb_int32(3, cp) + pb_int32(4, 0))
    pos = battlefield_pos(MISSION_MAP_ENTITY_ID)
    m += pb_float(8, pos[0])   # posX（mapEntityId 对应 battlefield 行坐标）
    m += pb_float(9, pos[1])   # posY
    return m


# ---------------------------------------------------------------- 处理器
def _multi_first(f, fn, default=0):
    """pb_parse_multi 结果取首个标量值（缺失/空 → default）。"""
    vals = f.get(fn)
    if not vals:
        return default
    v = vals[0]
    return v if not isinstance(v, (bytes, bytearray)) else default


def _split_factions(entries):
    """阵营分组 → (attacker_faction, defender_faction, 单侧真人)。

    最简平衡规则（计划 Assumption 5）：玩家保留各自角色 faction，
    人数最多组为进攻方、次多组为防守方；不足两个阵营时防守方由 AI 补位
    （ai_fill 局 / 同阵营匹配常见路径）。
    """
    groups = {}
    for e in entries:
        groups.setdefault(int(e.faction), []).append(e)
    if len(groups) >= 2:
        ordered = sorted(groups, key=lambda fc: (-len(groups[fc]), fc))
        return ordered[0], ordered[1], False
    attacker = next(iter(groups), 3)
    defender = _rival_faction(attacker)
    return attacker, defender, True


def _prune_match_factions(match, attacker, defender, gamemode_id):
    """非战争模式同局最多两阵营（防御性裁剪，就地修改 match.players）。

    返回被裁掉的 entries（空列表 = 无裁剪/战争模式不裁）；被裁玩家由
    调用方（_start_match）回池开下一局。
    """
    if int(gamemode_id) == 10:
        return []
    atk, dfd = int(attacker), int(defender)
    dropped = [e for e in match.players if int(e.faction) not in (atk, dfd)]
    if dropped:
        match.players = [e for e in match.players
                         if int(e.faction) in (atk, dfd)]
    return dropped


def _dispatch_match(match):
    """成局分发：并发上限内立即拉起，超限进 PENDING_MATCHES FIFO 排队。"""
    if (mm_config.MAX_CONCURRENT_COMBAT_SERVERS and
            len(COMBAT_INSTANCES) >= mm_config.MAX_CONCURRENT_COMBAT_SERVERS):
        PENDING_MATCHES.append(match)
        _log(f'match queued (combat servers busy: {len(COMBAT_INSTANCES)}/'
             f'{mm_config.MAX_CONCURRENT_COMBAT_SERVERS}, players='
             f'{len(match.players)}, pending={len(PENDING_MATCHES)})')
        return
    _spawn(_start_match(match), f'start_match#{match.mission_id}')


async def _try_form_all():
    """撮合并分发当前池内所有可成的局（入池/出池后调用）。"""
    while True:
        match = POOL.try_form()
        if match is None:
            break
        _dispatch_match(match)
    await _push_queue_counts()


async def _push_queue_counts():
    """向所有排队中玩家刷新 PlayerMatchMaking f18-f23（队列人数 + 战局统计，best-effort）。"""
    for pid, q in list(_MM_QUEUES.items()):
        if q.get('mission_id'):
            continue                     # 已成局，PMM 行已 delete
        ctx = q.get('ctx') or {}
        w, vc = ctx.get('writer'), ctx.get('view_cmd')
        if w is None or vc is None:
            continue
        try:
            await _push_kvcs(
                w, vc, b'PlayerMatchMaking',
                _msg_player_matchmaking(
                    q['pmm_id'], ctx['char_id'], ctx['faction'],
                    ctx.get('req_mission_id', 0), _zulu_now(),
                    POOL.waiting_count(q.get('gamemode', 0)),
                    POOL.waiting_count()))
        except Exception:
            pass                         # 连接已断：留给 cancel/abandon 清理


def _arm_ai_fill(pid, gamemode_id):
    """（重新）启动玩家的超时 AI 补位任务；ai_fill_after<=0 不启用。"""
    old = _AI_FILL_TASKS.pop(pid, None)
    if old is not None:
        old.cancel()
    cfg = mm_config.gamemode_cfg(gamemode_id)
    if cfg['ai_fill_after'] <= 0:
        return

    async def _fill():
        try:
            await asyncio.sleep(cfg['ai_fill_after'])
        except asyncio.CancelledError:
            return
        q = _MM_QUEUES.get(pid)
        if q is None or q.get('mission_id'):
            return                        # 已成局 / 已取消
        entry = POOL.get(pid)
        if entry is None or entry.gamemode_id != gamemode_id:
            return
        match = POOL.force_form(cfg, gamemode_id)
        if match is not None:
            _log(f'ai fill: gamemode={gamemode_id} timeout, forcing match '
                 f'(players={len(match.players)})')
            _dispatch_match(match)
            await _push_queue_counts()

    _AI_FILL_TASKS[pid] = _spawn(_fill(), f'ai_fill#{pid}')


def _cleanup_queued_player(pid):
    """出池玩家善后：取消 AI 补位任务、清 _MM_QUEUES。"""
    t = _AI_FILL_TASKS.pop(pid, None)
    if t is not None:
        t.cancel()
    _MM_QUEUES.pop(pid, None)


def drop_player(pid):
    """玩家断线清理：排队中 → 出池（防幽灵条目虚增人数 / 被撮合成局）。

    realtest_server.handle_tcp finally 调用（连接断开）。
    已成局（mission_id != 0）的玩家不处理：战局照常进行（等同中途退赛，
    由 combat/mission 域的 earnings/Abandon 流程收尾）。同步函数、幂等。
    """
    if pid is None:
        return
    q = _MM_QUEUES.get(pid)
    if q is None:
        return                          # 未在排队（含已成局/从未排队）
    if q.get('mission_id'):
        return                          # 已成局，交由战局生命周期管理
    gamemode = q.get('gamemode', 0)
    squad_db_id = int((q.get('ctx') or {}).get('squad_db_id') or 0)
    if squad_db_id:
        # 整队排队中掉线：整队出池回 InLobby（拆队不可部署）
        _spawn(_drop_squad_queue(squad_db_id, int(pid)),
               f'drop_squad:{pid}')
        return
    _cleanup_queued_player(pid)
    _MATCH_PMM_IDS.pop(pid, None)
    POOL.remove(pid)
    _log(f'mm: player {pid} dropped (disconnect, gamemode={gamemode})')
    _spawn(_try_form_all(), f'try_form_all(drop:{pid})')


# ---------------------------------------------------------------- 整队匹配
# 真实小队（CreateSquad 建立，DB squads 表）整队入池：
# 任何成员点匹配 → 全体在线 InLobby/Ready 成员作为原子单位入池
# （QueueEntry.group_key=squad_id，mm_pool 保证不拆队），成局同 mission 部署。
# 官方对应行为：队长激活小队（SquadSession f12 matchMakeWarMissionFlag）。

async def _squad_eligible_members(pid):
    """(squad, 可参战成员列表)；非小队场景返回 (None, None)。

    玩家在 DB 真实小队即走小队路径（即使队里只有自己在线——推真实
    小队行而非 solo 隐式行，避免同一玩家两张 SquadSession 冲突）。
    可参战成员 = InLobby/Ready 且 Flash 会话在线（离线成员本局不部署）。
    自己当前 InBattle（如已在局中重复点匹配）→ 返回 (squad, []) 由
    调用方跳过排队。"""
    if STORE is None or pid is None:
        return None, None
    try:
        squad = await STORE.get_squad_for_player(int(pid))
        if squad is None:
            return None, None
        members = await STORE.list_squad_members(squad['id'])
    except Exception as e:
        _log(f'      [DB] squad lookup failed: {e}')
        return None, None
    eligible = [m for m in members
                if int(m['status']) in (2, 3)
                and (s := FLASH_SESSIONS.get(m['gamertag'] or '')) is not None
                and s.playerview_cmd is not None]
    if not any(int(m['player_id']) == int(pid) for m in eligible):
        return squad, []         # 自己不在可参战列表（InBattle/离线）
    return squad, eligible


async def _enqueue_squad(squad, members, gamemode_id, staged_presets,
                         req_mission_id):
    """全队入池：每名在线成员建排队上下文 + PMM 行；DB 置 Ready +
    小队 matchmaking_flag；整队 KVCS 行推给全体（跨会话）。"""
    if not members:
        return                      # 无可参战成员（如自己已 InBattle）
    squad_id = int(squad['id'])
    entries = []
    for m in members:
        mpid = int(m['player_id'])
        flash = FLASH_SESSIONS[m['gamertag']]
        w, vc = flash.writer, flash.playerview_cmd
        # 此前单独排队残留清理（行 id 即将更换）
        old_q = _MM_QUEUES.get(mpid)
        if old_q is not None and not old_q.get('mission_id'):
            _cleanup_queued_player(mpid)
            POOL.remove(mpid)
            if old_q.get('pmm_id') is not None:
                await _push_kvcs_delete(w, vc, b'PlayerMatchMaking',
                                        old_q['pmm_id'])
        for row in _msg_faction_rows():
            await _push_kvcs(w, vc, b'faction', row)
        pmm_id = _next_match_id(_MM_PMM_ID)
        rank = _rank_from_combat_xp(int(m['combat_xp']))
        ctx = {'writer': w, 'view_cmd': vc, 'gamertag': m['gamertag'],
               'char_name': m['char_name'], 'rank': rank,
               'faction': int(m['faction']),
               'char_id': int(m['character_id']), 'quickplay': False,
               'req_mission_id': req_mission_id,
               'metrics_uid': str(uuid.uuid4()),
               'metrics_session_id': _next_match_id(_MM_METRICS_SESSION_ID),
               'squad_db_id': squad_id}
        _MM_QUEUES[mpid] = {'pmm_id': pmm_id, 'mission_id': 0,
                            'gamemode': gamemode_id, 'ctx': ctx}
        _MATCH_PMM_IDS[mpid] = pmm_id
        await _push_kvcs(w, vc, b'PlayerMatchMaking',
                         _msg_player_matchmaking(
                             pmm_id, int(m['character_id']),
                             int(m['faction']), req_mission_id, _zulu_now(),
                             POOL.waiting_count(gamemode_id) + 1,
                             POOL.waiting_count() + 1))
        entries.append(mm_pool.QueueEntry(
            player_id=mpid, char_id=int(m['character_id']),
            faction=int(m['faction']), gamemode_id=gamemode_id,
            presets=staged_presets, group_key=squad_id))
    try:
        for m in members:
            await STORE.update_squad_member(m['id'], status=3)
        await STORE.set_squad_flag(squad_id, True)
    except Exception as e:
        _log(f'      [DB] squad enqueue status updates failed: {e}')
    from handlers.squad import push_squad_rows_to_all_members
    await push_squad_rows_to_all_members(squad_id)
    _log(f'squad enqueue: squad={squad_id}, players='
         f'{[e.player_id for e in entries]}, gamemode={gamemode_id}')
    for e in entries:
        POOL.enqueue(e)
    await _try_form_all()
    for e in entries:
        if _MM_QUEUES.get(e.player_id, {}).get('mission_id', 0) == 0:
            _arm_ai_fill(e.player_id, gamemode_id)


async def reset_real_squad_to_lobby(squad_db_id, exclude_pids=()):
    """整队回 InLobby（DB 状态 + 向在线成员推整队行 mission=0）。

    战局结束（combat._finish_mission_sessions）/ 放弃（mission abandon）/
    整队取消 / 整队掉线共用。排除 exclude_pids 中的玩家推送（连接已断）。"""
    if STORE is None or not squad_db_id:
        return
    try:
        await STORE.set_squad_flag(int(squad_db_id), False)
        members = await STORE.list_squad_members(int(squad_db_id))
        for m in members:
            await STORE.update_squad_member(m['id'], status=2)
    except Exception as e:
        _log(f'      [DB] reset_real_squad_to_lobby failed: {e}')
        return
    from handlers.squad import _online_member_sessions, push_squad_rows_to_writer
    for s in _online_member_sessions(members):
        if int(s.player_id or 0) in exclude_pids:
            continue
        try:
            await push_squad_rows_to_writer(
                s.writer, s.playerview_cmd, int(squad_db_id), 0,
                viewer_pid=s.player_id, viewer_name=s.username)
        except Exception as e:
            _log(f'      squad reset push to {s.username!r} failed: {e}')


async def _cancel_squad_queue(squad_db_id, exclude_pids=()):
    """整队取消匹配：全队出池、删 PMM 行、回 InLobby（任何成员取消全队退出）。"""
    if STORE is None:
        return
    try:
        members = await STORE.list_squad_members(int(squad_db_id))
    except Exception as e:
        _log(f'      [DB] _cancel_squad_queue lookup failed: {e}')
        return
    for m in members:
        mpid = int(m['player_id'])
        q = _MM_QUEUES.get(mpid)
        if q is None or q.get('mission_id'):
            continue                     # 不在排队 / 已成局不动
        _cleanup_queued_player(mpid)
        POOL.remove(mpid)
        s = FLASH_SESSIONS.get(m['gamertag'] or '')
        if (s is not None and s.playerview_cmd is not None
                and q.get('pmm_id') is not None):
            try:
                await _push_kvcs_delete(s.writer, s.playerview_cmd,
                                        b'PlayerMatchMaking', q['pmm_id'])
            except Exception as e:
                _log(f'      PMM delete push failed (player={mpid}): {e}')
    await reset_real_squad_to_lobby(squad_db_id, exclude_pids)
    _log(f'squad cancel: squad={squad_db_id}, exclude={list(exclude_pids)}')


async def _drop_squad_queue(squad_db_id, gone_pid):
    """整队排队中成员掉线：全队出池、回 InLobby（不推掉线者连接）。"""
    await _cancel_squad_queue(squad_db_id, (int(gone_pid),))
    await _try_form_all()


async def _start_match(match):
    """成局落地：发号、登记会话、拉起 combat server、逐玩家推送进局序列。

    每场战局一个独立 combat server 实例（动态 UDP 端口），多人共享：
    - mission_id 全局唯一（/M、GetMissionDetails、release_match 键）
    - 每名玩家独立 pms_id / metrics ids（game_server 按 pms 查会话）
    - game_connect = 127.0.0.1:{port}（客户端 hng.exe 连此 UDP 打战斗）
    """
    mission_id = _next_match_id(_MM_MISSION_ID)
    match.mission_id = mission_id
    preset = int(match.preset_id)
    gamemode_id = int(match.players[0].gamemode_id) if match.players else 0
    attacker, defender, one_sided = _split_factions(match.players)
    enable_bots = bool(match.ai_fill or one_sided)

    # ★ 防御（非战争模式同局最多两阵营）：combat server 只按攻防两阵营
    #   配出生侧，撮合若带出第三阵营（US/GE/SU 同池），第三阵营玩家进图
    #   无侧 → 判负+观战。超出两阵营的玩家回池开下一局（与端口耗尽
    #   回池同路径）。战争模式（gamemode 10）多方语义不裁剪。
    dropped = _prune_match_factions(match, attacker, defender, gamemode_id)
    if dropped:
        _log(f'match pruned to 2 factions: kept={len(match.players)} '
             f'(atk={attacker}, dfd={defender}), returned='
             f'{[(int(e.player_id), int(e.faction)) for e in dropped]}')
        for e in dropped:
            dpid = int(e.player_id)
            if _MM_QUEUES.get(dpid, {}).get('mission_id', 0) == 0:
                POOL.enqueue(e)
                _arm_ai_fill(dpid, e.gamemode_id)

    preset_row = _load_preset(preset)
    gamemap_id = int(preset_row.get(3, 0) or 0) or 13
    map_entity_id = battlefield_entity_for_gamemap(gamemap_id)
    combat_map = preset_scene(preset)

    port = _alloc_combat_port()
    if port is None:
        # 端口池耗尽：不能丢局——玩家已被 try_form 移出池但 _MM_QUEUES 还在，
        # 回池重排（不立即重试撮合：端口未释放前再撮还会失败；
        # release_match 释放端口后会调 _try_form_all 重新撮合，
        # 超时后 AI 补位计时兜底），避免玩家永久卡在匹配界面。
        _log(f'ERROR: combat port pool exhausted, match returned to pool '
             f'(mission would be {mission_id}, {len(match.players)} players)')
        for e in match.players:
            pid = int(e.player_id)
            if _MM_QUEUES.get(pid, {}).get('mission_id', 0) == 0:
                POOL.enqueue(e)
                _arm_ai_fill(pid, e.gamemode_id)
        return

    # 每名玩家发号登记（发号规则与改造前单人路径一致：PMS f17/f18/f9 与
    # PlayerSettings f22/f23/f25 同源；solo squad 行 id 复用）
    # 整队成员复用 DB 小队 id（PMS f16 与小队行同源，战斗结束按它重置整队）
    sessions = []
    for e in match.players:
        pid = int(e.player_id)
        q = _MM_QUEUES.pop(pid, None)
        _cleanup_queued_player(pid)
        ctx = (q or {}).get('ctx') or {}
        pms_id = _next_match_id(_MM_PMS_ID)
        metrics_uid = ctx.get('metrics_uid') or str(uuid.uuid4())
        metrics_session_id = int(ctx.get('metrics_session_id', 0) or 0) or \
            _next_match_id(_MM_METRICS_SESSION_ID)
        battle_session_id = _next_match_id(_MM_BATTLE_SESSION_ID)
        squad_db_id = int(ctx.get('squad_db_id') or 0)
        squad_id = squad_db_id or _next_match_id(_MM_SQUAD_ID)
        gamertag = ctx.get('gamertag') or 'Player'
        sess_rec = {'pms_id': pms_id, 'player_id': pid,
                    'char_id': int(e.char_id), 'gamertag': gamertag,
                    'faction': int(e.faction),
                    'metrics_uid': metrics_uid,
                    'metrics_session_id': metrics_session_id,
                    'battle_session_id': battle_session_id,
                    'squad_id': squad_id,
                    'squad_db_id': squad_db_id,
                    'squad_slot': 0,
                    'mission_id': mission_id,
                    'quickplay': bool(ctx.get('quickplay', False))}
        PMS_TO_MISSION[pms_id] = mission_id
        PMS_TO_SESSION[pms_id] = sess_rec
        sessions.append({'entry': e, 'ctx': ctx, 'sess': sess_rec,
                         'pms_id': pms_id, 'squad_id': squad_id})

    # ★ 局内小队分组（覆盖上面每人一个 squad_id 的占位发号）：
    #   - 真实小队（ctx.squad_db_id 相同）共用 DB 小队 id，队名取库
    #     （大厅 SetSquadDetails 传入），队长取 DB leader_character_id
    #   - 单人匹配玩家按阵营+兵种自动凑队（每队上限 _MM_SOLO_SQUAD_MAX，
    #     rank 最高者任队长、占 slot 0），队名用兵种对应官方小队模板名
    #     （Infantry/Fighter/Paratrooper/Armor/Recon Squad）——大厅 UI 对
    #     无小队玩家按兵种显示的默认名即此模板标题，局内 SquadResponse
    #     f4 name 同源才能对上
    # game_server SquadResponse 按登记表构建小队定义 → 同队成员进同一局内小队。
    def _register_mission_squad(sq_id, name, leader_char, fac, members,
                                db_id=0, template=1):
        members = sorted(members, key=lambda s: 0 if int(
            s['entry'].char_id) == int(leader_char) else 1)
        rows = []
        for slot, s in enumerate(members):
            s['sess']['squad_id'] = sq_id
            s['sess']['squad_slot'] = slot
            rows.append({'player_id': int(s['entry'].player_id),
                         'char_id': int(s['entry'].char_id),
                         'gamertag': s['sess']['gamertag'],
                         'rank': int((s['ctx'] or {}).get('rank', 0) or 0),
                         'slot': slot})
        _MM_MISSION_SQUADS.setdefault(mission_id, {})[sq_id] = {
            'name': name, 'leader_char_id': int(leader_char),
            'faction': int(fac), 'squad_db_id': int(db_id),
            'template': int(template), 'members': rows}

    real_ids = sorted({s['sess']['squad_db_id'] for s in sessions
                       if s['sess']['squad_db_id']})
    for sid in real_ids:
        members = [s for s in sessions if s['sess']['squad_db_id'] == sid]
        squad_row = None
        try:
            squad_row = await STORE.get_squad(sid)
        except Exception as ex:
            _log(f'      [DB] get_squad for grouping failed ({sid}): {ex}')
        name = (squad_row['name'] if squad_row else None) or 'PlayerSquad'
        member_chars = {int(s['entry'].char_id) for s in members}
        leader_char = int(squad_row['leader_character_id']) \
            if squad_row else 0
        if leader_char not in member_chars:
            leader_char = int(members[0]['entry'].char_id)
        _register_mission_squad(sid, name, leader_char,
                                int(members[0]['entry'].faction), members,
                                db_id=sid)
    solo_by_faction = {}
    for s in sessions:
        if not s['sess']['squad_db_id']:
            solo_by_faction.setdefault(int(s['entry'].faction), []).append(s)
    for fac, grp in sorted(solo_by_faction.items()):
        # 按兵种再分组：同兵种才凑一队（队名=该兵种官方小队模板名，
        # 与大厅 UI 默认名一致；不同兵种大厅默认名不同，混编会再次对不上）
        by_branch = {}
        for s in grp:
            tmpl = squad_template_for_character(
                (s['ctx'] or {}).get('char_template'))
            by_branch.setdefault(int(tmpl), []).append(s)
        for sq_tmpl, sub in sorted(by_branch.items()):
            sub.sort(key=lambda s: (-int((s['ctx'] or {}).get('rank', 0) or 0),
                                    int(s['entry'].player_id)))
            for i in range(0, len(sub), _MM_SOLO_SQUAD_MAX):
                chunk = sub[i:i + _MM_SOLO_SQUAD_MAX]
                leader = chunk[0]
                _register_mission_squad(
                    _next_match_id(_MM_SQUAD_ID),
                    squad_template_name(sq_tmpl),
                    int(leader['entry'].char_id), fac, chunk,
                    template=sq_tmpl)
    if _MM_MISSION_SQUADS.get(mission_id):
        for sq_id, info in _MM_MISSION_SQUADS[mission_id].items():
            _log(f'mission={mission_id} squad={sq_id} name={info["name"]!r} '
                 f'leader_char={info["leader_char_id"]} members='
                 f'{[(m["gamertag"], m["slot"]) for m in info["members"]]}')

    # 真实小队整队置 InBattle（DB 先行；随后 KVCS 整队行读取该状态）
    real_squads = sorted({s['sess']['squad_db_id'] for s in sessions
                          if s['sess']['squad_db_id']})
    for sid in real_squads:
        try:
            for m in await STORE.list_squad_members(sid):
                await STORE.update_squad_member(m['id'], status=4)
        except Exception as ex:
            _log(f'      [DB] squad InBattle updates failed '
                 f'(squad={sid}): {ex}')

    _MM_MISSION_DETAILS[mission_id] = {
        'preset': preset, 'gamemode': gamemode_id,
        'ai_fill': bool(match.ai_fill), 'map_entity_id': map_entity_id,
        'attacker_faction': attacker, 'defender_faction': defender,
        'enable_bots': enable_bots,
        # 成局时刻（monotonic）：等待期中途加入窗口（mm_config.JOIN_WINDOW）
        # 从此起算；release_match 随详情一并清理
        'formed_at': time.monotonic(),
        # 本局天气/时段（成局时随机抽取一次，简报 MissionInfo f12/f13 与
        # 战斗服 BattleFieldSettings f9/f21/f22 同源取值）
        **roll_weather(),
        'players': [{'player_id': int(e.player_id),
                     'gamertag': s['sess']['gamertag'],
                     'char_id': int(e.char_id),
                     'faction': int(e.faction),
                     'pms_id': s['pms_id']} for s in sessions],
    }
    # 每局资源池：f5（匹配界面）/ f14（握手）/ f2（轮询）三处单一事实源，
    # 出生消耗（ReportMissionStatusRequest f2）扣减（与详情同生命周期）
    _init_mission_resources(mission_id, gamemode_id, attacker, defender)
    _log(f'match formed: mission={mission_id}, preset={preset}, map='
         f'{combat_map}, players={len(match.players)}, ai_fill={match.ai_fill}, '
         f'attacker={attacker}, defender={defender}, udp/{port}, '
         f'weather tod={_MM_MISSION_DETAILS[mission_id]["weather_tod"]}/'
         f'cond={_MM_MISSION_DETAILS[mission_id]["weather_condition"]}')

    # 拉起本场专用服（/P 取第一名玩家 pms，仅 boot 参数）
    first_pms = sessions[0]['pms_id'] if sessions else 0
    await _ensure_combat_server(mission_id, port, first_pms, combat_map)
    game_connect = f'{PUBLIC_HOST}:{port}'   # 客户端 hng.exe 连战斗服的地址
    # 战局位置：mapEntityId 对应 battlefield 行坐标（简报战场名与地图标记一致；
    # battlefield_entity_for_gamemap 对无行图恒返回有效行 id，无需回退）
    pos = battlefield_pos(map_entity_id)

    # 逐玩家推送进局序列（单玩家失败不影响他人）
    for s in sessions:
        try:
            await _push_player_into_mission(s['entry'], s['ctx'], s['sess'],
                                            s['pms_id'], mission_id,
                                            game_connect, pos)
        except Exception as ex:
            _log(f'      match push failed (player={s["entry"].player_id}, '
                 f'mission={mission_id}): {ex}')
    await _push_queue_counts()


async def _push_player_into_mission(e, ctx, sess_rec, pms_id, mission_id,
                                    game_connect, pos):
    """单玩家进局 KVCS 序列（成局分发与等待期中途加入共用）。

    PMS Admitted(f14=2，触发客户端拉起 hng.exe) → solo squad InBattle
    → PMM delete（退出匹配中界面）→ BattleInfo set → 5s 后 PMS Armed
    (f14=3)。单玩家失败由调用方捕获（不影响其他玩家）。
    """
    w, vc = ctx.get('writer'), ctx.get('view_cmd')
    if w is None or vc is None:
        _log(f'      [WARN] mission={mission_id}: player={e.player_id} '
             f'has no writer, skip push')
        return
    pid = int(e.player_id)
    char_id = int(e.char_id)
    faction = int(e.faction)
    gamertag = sess_rec['gamertag']
    char_name = ctx.get('char_name') or gamertag
    rank = int(ctx.get('rank', 0) or 0)
    solo = _SOLO_SQUADS.get(pid) or {}
    squad_db_id = int(ctx.get('squad_db_id') or 0)
    # PMS Admitted（f14=2 触发客户端拉起 hng.exe）
    await _push_kvcs(w, vc, b'PlayerMissionSession',
                     _msg_player_mission_session(
                         pms_id, pid, gamertag, char_id, faction,
                         char_name, rank, mission_id, _zulu_now(),
                         game_connect,
                         metrics_uid=sess_rec['metrics_uid'],
                         metrics_session_id=sess_rec['metrics_session_id'],
                         metrics_device_id=METRICS_DEVICE_ID,
                         platform='HnG', connection_state=2,
                         battle_session_id=sess_rec['battle_session_id']))
    _log(f'S->C cmd={vc:<4} KeyValueChangeSet (PlayerMissionSession '
         f'id={pms_id}, player={pid}, mission={mission_id}, '
         f'state=2 Admitted ★launch trigger)')
    # 整队 UPDATE（InBattle）：真实小队推整队行（f17=本局 missionId、
    # 成员 InBattle=4，每名成员各自连接收到同一份）；单人推 solo 行
    if squad_db_id:
        from handlers.squad import push_squad_rows_to_writer
        await push_squad_rows_to_writer(w, vc, squad_db_id, mission_id,
                                        viewer_pid=pid)
    elif solo:
        solo['mission_id'] = mission_id
        solo['entered'] = int(solo.get('entered', 0)) + 1
        await _push_kvcs(w, vc, b'SquadSession',
                         _msg_solo_squad_session(
                             solo['squad_row'], pid,
                             solo['template'], solo['faction'],
                             mission_id, solo['entered']))
        await _push_kvcs(w, vc, b'SquadMemberSession',
                         await _msg_solo_squad_member_session(
                             solo['member_row'], solo['squad_row'], pid,
                             gamertag, char_id, char_name, rank, 4,
                             solo.get('metrics_session_id', 0),
                             solo['faction']))
    # PMM delete（退出匹配中界面）
    pmm_id = _MATCH_PMM_IDS.pop(pid, None)
    if pmm_id is not None:
        await _push_kvcs_delete(w, vc, b'PlayerMatchMaking', pmm_id)
        _log(f'S->C cmd={vc:<4} KeyValueChangeSet '
             f'delete (PlayerMatchMaking id={pmm_id})')
    # BattleInfo 'set'（PMS Armed 后必须推，否则客户端不连 game server）
    await _push_kvcs(w, vc, b'BattleInfo',
                     _msg_battle_info(mission_id, pos[0], pos[1]))
    _log(f'S->C cmd={vc:<4} KeyValueChangeSet (BattleInfo id={mission_id})')
    # ★ private_login.pcapng 复核：Admitted 数秒后再推 f14=3 (Armed)，
    #   KVCS 全走 Flash 连接（game 连接保持静默）。
    await asyncio.sleep(5)
    await _push_kvcs(w, vc, b'PlayerMissionSession',
                     _msg_player_mission_session(
                         pms_id, pid, gamertag, char_id, faction,
                         char_name, rank, mission_id, _zulu_now(),
                         game_connect,
                         metrics_uid=sess_rec['metrics_uid'],
                         metrics_session_id=sess_rec['metrics_session_id'],
                         metrics_device_id=METRICS_DEVICE_ID,
                         platform='HnG', connection_state=3,
                         battle_session_id=sess_rec['battle_session_id']))
    _log(f'S->C cmd={vc:<4} KeyValueChangeSet (PlayerMissionSession '
         f'id={pms_id}, mission={mission_id}, state=3 Armed ★battle arm)')


def _find_joinable_mission(gamemode_id, presets, faction, only_mission=None):
    """找一个等待期内可中途加入的战局，返回 (mission_id, inst) 或 None。

    条件（全部满足）：
    - 战局实例仍在运行（COMBAT_INSTANCES 且 _MM_MISSION_DETAILS 均在）
    - 同 gamemode；玩家选图兼容（未选图=通配，否则须包含该局 preset）
    - 玩家阵营属于该局攻防双方（第三方阵营无出生侧，无法进图）
    - 成局后未超过 mm_config.JOIN_WINDOW 秒（开局等待/倒计时阶段）
    - 该局人数未达该模式 max_players
    only_mission 非空时只考虑该 mission_id（JoinMatchMakingRequest f2
    指定战局加入，战争地图点进行中战斗的路径）。
    """
    if mm_config.JOIN_WINDOW <= 0:
        return None
    now = time.monotonic()
    if only_mission:
        inst = COMBAT_INSTANCES.get(int(only_mission))
        cands = [(int(only_mission), inst)] if inst is not None else []
    else:
        cands = list(COMBAT_INSTANCES.items())
    for mission_id, inst in cands:
        det = _MM_MISSION_DETAILS.get(mission_id)
        if det is None or 'preset' not in det:
            continue
        if int(det.get('gamemode', 0) or 0) != int(gamemode_id):
            continue
        if presets and int(det.get('preset', 0) or 0) not in presets:
            continue
        # ★ 阵营边界（开局两阵营固定，第三方阵营禁入）：det 阵营字段
        #   缺失/非法时不放行——原 .get 默认 3 恰为 SU，字段未就绪会
        #   漏放第三阵营（线上美德局苏军 late join 进局判负+观战）。
        #   玩家 faction 必须严格命中该局攻防两阵营之一。
        atk = int(det.get('attacker_faction', 0) or 0)
        dfd = int(det.get('defender_faction', 0) or 0)
        if atk <= 0 or dfd <= 0:
            continue
        if faction not in (atk, dfd):
            continue
        if now - float(det.get('formed_at', 0) or 0) > mm_config.JOIN_WINDOW:
            continue
        max_p = int(mm_config.gamemode_cfg(gamemode_id).get('max_players', 18))
        if len(det.get('players') or ()) >= max_p:
            continue
        return mission_id, inst
    return None


async def _join_existing_mission(pid, mission_id, e, ctx):
    """等待期中途加入现有战局：登记 PMS 会话 + 推进局 KVCS 序列。

    与 _start_match 单玩家路径同构：发 pms/metrics/battle_session/squad id、
    登记 PMS_TO_MISSION/PMS_TO_SESSION（game_server 按 pms 识别会话）、
    追加进 _MM_MISSION_DETAILS['players']（简报参战方名单/GMD 立即可见），
    客户端 hng.exe 直连该局 combat server UDP 端口。
    """
    q = _MM_QUEUES.pop(pid, None)
    _cleanup_queued_player(pid)
    ctx = (q or {}).get('ctx') or ctx
    det = _MM_MISSION_DETAILS[mission_id]
    inst = COMBAT_INSTANCES[mission_id]
    # ★ 去重：同玩家再次进入同一局（掉线 rejoin / 重复点匹配）先清旧痕
    #   迹——否则旧 PMS 会话、det['players'] 条目、小队 members 会重复
    #   append（幽灵行：earnings/结算会重复计入）。
    pid = int(pid)
    old_pms = [p for p, m in list(PMS_TO_MISSION.items())
               if int(m) == int(mission_id) and
               int((PMS_TO_SESSION.get(p) or {}).get('player_id', 0) or 0) == pid]
    for p in old_pms:
        PMS_TO_MISSION.pop(p, None)
        PMS_TO_SESSION.pop(p, None)
    det['players'] = [p for p in (det.get('players') or [])
                      if int(p.get('player_id', 0) or 0) != pid]
    for sq_id, info in list((_MM_MISSION_SQUADS.get(mission_id) or {}).items()):
        info['members'] = [m for m in (info.get('members') or [])
                           if int(m.get('player_id', 0) or 0) != pid]
        if not info['members']:
            _MM_MISSION_SQUADS[mission_id].pop(sq_id, None)
    pms_id = _next_match_id(_MM_PMS_ID)
    metrics_uid = ctx.get('metrics_uid') or str(uuid.uuid4())
    metrics_session_id = int(ctx.get('metrics_session_id', 0) or 0) or \
        _next_match_id(_MM_METRICS_SESSION_ID)
    sess_rec = {'pms_id': pms_id, 'player_id': pid,
                'char_id': int(e.char_id),
                'gamertag': ctx.get('gamertag') or 'Player',
                'faction': int(e.faction),
                'metrics_uid': metrics_uid,
                'metrics_session_id': metrics_session_id,
                'battle_session_id': _next_match_id(_MM_BATTLE_SESSION_ID),
                'squad_id': 0, 'squad_slot': 0,
                'mission_id': mission_id,
                'quickplay': bool(ctx.get('quickplay', False))}
    # ★ 局内小队归组：单人中途加入优先补进同阵营、同兵种、未满员的单人
    #   凑成队（不动真实小队）；没有可补的队则自建一队（本人任队长），
    #   队名=本人兵种官方小队模板名（与大厅 UI 默认名一致）。
    join_rank = int(ctx.get('rank', 0) or 0)
    join_tmpl = squad_template_for_character(ctx.get('char_template'))
    new_member = {'player_id': pid, 'char_id': int(e.char_id),
                  'gamertag': sess_rec['gamertag'], 'rank': join_rank,
                  'slot': 0}
    picked = None
    for sq_id, info in (_MM_MISSION_SQUADS.get(mission_id) or {}).items():
        if info.get('squad_db_id'):
            continue                      # 真实小队不吸收散人
        if int(info.get('faction', 0) or 0) != int(e.faction):
            continue
        if int(info.get('template', 1) or 1) != int(join_tmpl):
            continue                      # 兵种不同则大厅默认名对不上
        if len(info.get('members') or []) >= _MM_SOLO_SQUAD_MAX:
            continue
        picked = (sq_id, info)
        break
    if picked is not None:
        sq_id, info = picked
        new_member['slot'] = len(info['members'])
        info['members'].append(new_member)
        sess_rec['squad_id'] = sq_id
        sess_rec['squad_slot'] = new_member['slot']
    else:
        sq_id = _next_match_id(_MM_SQUAD_ID)
        _MM_MISSION_SQUADS.setdefault(mission_id, {})[sq_id] = {
            'name': squad_template_name(join_tmpl),
            'leader_char_id': int(e.char_id), 'faction': int(e.faction),
            'squad_db_id': 0, 'template': int(join_tmpl),
            'members': [new_member]}
        sess_rec['squad_id'] = sq_id
    PMS_TO_MISSION[pms_id] = mission_id
    PMS_TO_SESSION[pms_id] = sess_rec
    det.setdefault('players', []).append(
        {'player_id': pid, 'gamertag': sess_rec['gamertag'],
         'char_id': int(e.char_id), 'faction': int(e.faction),
         'pms_id': pms_id})
    age = time.monotonic() - float(det.get('formed_at', 0) or 0)
    _log(f'late join: player {pid} -> mission={mission_id} '
         f'(players={len(det["players"])}, udp/{inst["port"]}, '
         f'age={age:.0f}s, gamemode={det.get("gamemode")}, '
         f'faction={sess_rec["faction"]} vs '
         f'atk={det.get("attacker_faction")}/'
         f'dfd={det.get("defender_faction")})')
    # 该局专用服可能仍在加载地图（握手未完成）：有界等待，超时放行——
    # 客户端 hng.exe 连不上 UDP 会自动重试（与 _start_match 同哲学）
    try:
        await asyncio.wait_for(inst['ready'].wait(), 30)
    except asyncio.TimeoutError:
        _log(f'late join: mission={mission_id} combat server not ready '
             f'after 30s - pushing Admitted anyway (client will retry UDP)')
    game_connect = f'{PUBLIC_HOST}:{inst["port"]}'
    pos = battlefield_pos(int(det.get('map_entity_id') or 0)
                          or MISSION_MAP_ENTITY_ID)
    try:
        await _push_player_into_mission(e, ctx, sess_rec, pms_id, mission_id,
                                        game_connect, pos)
    except Exception as ex:
        _log(f'      late join push failed (player={pid}, mission='
             f'{mission_id}): {ex}')
    await _push_queue_counts()


def _find_player_active_mission(pid):
    """玩家当前存活的原始战局（掉线 rejoin 用）。

    遍历 PMS 会话找 player_id 命中且 mission 仍在 COMBAT_INSTANCES
    存活的记录，返回 mission_id（多条取最大=最新成局）；无 → None。
    drop_player 不清已成局会话（战局照常进行），掉线重连据此找回原局。
    """
    if pid is None:
        return None
    pid = int(pid)
    best = 0
    for pms_id, rec in list(PMS_TO_SESSION.items()):
        if int((rec or {}).get('player_id', 0) or 0) != pid:
            continue
        mid = int(PMS_TO_MISSION.get(pms_id, 0) or 0)
        if mid and mid in COMBAT_INSTANCES and mid > best:
            best = mid
    return best or None


async def _handle_join(sess, writer, cmd_id, proto):
    """JoinMatchMakingRequest：响应 ok + 入匹配池（gamemode 分池）+ 推匹配中状态。

    f5 stagedMissionPresetList（repeated）为玩家勾选的地图 preset（空=不限）；
    f7 gamemodeId 决定进入哪个模式的池。成局条件见 mm_config.GAMEMODES
    （人数凑够 / 超时 AI 补位 / 训练模式单人直开）。
    """
    f = pb_parse_multi(proto)
    char_id = int(_multi_first(f, 1))
    req_mission_id = int(_multi_first(f, 2))
    war_flag = bool(_multi_first(f, 4))
    gamemode_id = int(_multi_first(f, 7))
    quickplay = bool(_multi_first(f, 8))
    staged_presets = tuple(int(v) for v in f.get(5, []) if not
                           isinstance(v, (bytes, bytearray)))
    _log(f'      JoinMatchMaking: charId={char_id}, missionId={req_mission_id}, '
         f'warMissionFlag={war_flag}, gamemodeId={gamemode_id}, '
         f'quickplay={quickplay}, presets={list(staged_presets)}')

    pid = await sess.ensure_player()
    char_info = None
    if pid is not None:
        # ★ charId=0：客户端有小队会话时 EnterMatchmakingCommand 传
        #   int64.zero（官方由服务端按小队成员行取角色）。不解析则
        #   join 被拒 → 真实小队/单人隐式小队的整队匹配永远起不来。
        #   解析顺序：DB 小队成员行（含换角色，SetSquadMemberStatus 已
        #   更新）→ solo 隐式小队注册表 → 无会话则维持 0（拒绝兜底）。
        if not char_id:
            try:
                squad = await STORE.get_squad_for_player(int(pid))
                if squad is not None:
                    for m in await STORE.list_squad_members(squad['id']):
                        if int(m['player_id']) == int(pid):
                            char_id = int(m['character_id'])
                            break
            except Exception as e:
                _log(f'[DB] squad char resolve failed: {e}')
            if not char_id:
                solo = _SOLO_SQUADS.get(int(pid))
                if solo is not None:
                    char_id = int(solo.get('char_id', 0) or 0)
            if char_id:
                _log(f'      JoinMatchMaking: charId=0 resolved from squad '
                     f'session -> char {char_id}')
        try:
            for ch in await STORE.list_characters(pid):
                if int(ch['id']) == int(char_id):
                    char_info = ch
                    break
        except Exception as e:
            _log(f'[DB] list_characters failed: {e}')

    if char_info is None:
        resp = build_response(cmd_id, 'JoinMatchMakingResponse', pb_int32(1, 0))
        writer.write(resp)
        await writer.drain()
        _log(f'S->C cmd={cmd_id:<4} JoinMatchMakingResponse (rejected: char {char_id} not found)')
        return

    faction = int(char_info['faction'])

    resp = build_response(cmd_id, 'JoinMatchMakingResponse', pb_int32(1, 1))
    writer.write(resp)
    await writer.drain()
    _log(f'S->C cmd={cmd_id:<4} JoinMatchMakingResponse (response=1 ok, charId={char_id})')

    view_cmd = sess.playerview_cmd

    # ★ faction 表 3 行（US/GE/SU）必须推送：客户端 FactionTable(xmlTag='faction')
    #   为空时 GetMissionDetailsResponse 处理中 getFromId 返回 null → #1009 卡死进对局。
    #   客户端在 JoinMM ok 后会立即发第一次 GetMissionDetailsRequest，必须先于它推送。
    if view_cmd is not None:
        for row in _msg_faction_rows():
            await _push_kvcs(writer, view_cmd, b'faction', row)
        _log(f'S->C cmd={view_cmd:<4} KeyValueChangeSet (faction ×3 rows) ★FactionTable fill')

    # ★ 整队入池：玩家在真实小队（DB squads）且队内有其他在线 InLobby/Ready
    #   成员 → 全队作为原子单位一起排队（group_key=squad_id 不拆队），
    #   成局后整队同 mission 部署。官方语义：队长激活小队全队进匹配，
    #   这里任何成员点匹配都带全队（角色取各自小队成员行）。
    squad, sq_members = await _squad_eligible_members(pid)
    if squad is not None:
        await _enqueue_squad(squad, sq_members, gamemode_id,
                             staged_presets, req_mission_id)
        return

    # 重复排队：删旧 PMM 行（行 id 即将更换，防客户端表残留）
    old_q = _MM_QUEUES.get(pid)
    if old_q is not None and not old_q.get('mission_id'):
        _cleanup_queued_player(pid)
        POOL.remove(pid)
        if view_cmd is not None:
            try:
                await _push_kvcs_delete(writer, view_cmd, b'PlayerMatchMaking',
                                        old_q['pmm_id'])
            except Exception:
                pass

    # 排队上下文（成局时 _start_match 取用；id 与 PMS/PlayerSettings 同源）
    gamertag = sess.username or 'Player'
    char_name = char_info['name']
    rank = _rank_from_combat_xp(int(char_info.get('combat_xp', 0) or 0))
    template = int(char_info.get('template', 0) or 0)
    pmm_id = _next_match_id(_MM_PMM_ID)
    metrics_uid = str(uuid.uuid4())
    metrics_session_id = _next_match_id(_MM_METRICS_SESSION_ID)
    ctx = {'writer': writer, 'view_cmd': view_cmd, 'gamertag': gamertag,
           'char_name': char_name, 'rank': rank, 'faction': faction,
           'char_id': int(char_id), 'quickplay': quickplay,
           'req_mission_id': req_mission_id,
           'char_template': template,
           'metrics_uid': metrics_uid,
           'metrics_session_id': metrics_session_id}
    _MM_QUEUES[pid] = {'pmm_id': pmm_id, 'mission_id': 0,
                       'gamemode': gamemode_id, 'ctx': ctx}
    _MATCH_PMM_IDS[pid] = pmm_id

    # 1. 推送 PlayerMatchMaking（客户端进入"匹配中"界面；f18/f19 池内实时人数）
    if view_cmd is not None:
        await _push_kvcs(writer, view_cmd, b'PlayerMatchMaking',
                         _msg_player_matchmaking(
                             pmm_id, char_id, faction, req_mission_id,
                             _zulu_now(),
                             POOL.waiting_count(gamemode_id) + 1,
                             POOL.waiting_count() + 1))
        _log(f'S->C cmd={view_cmd:<4} KeyValueChangeSet (PlayerMatchMaking '
             f'id={pmm_id}, gamemode={gamemode_id})')

    # 1b. ★ 单人隐式小队行（官方 solo_kvcs_dump2.txt S2C#4：与 PlayerMatchMaking
    #     同变更集推 SquadSession + SquadMemberSession(Ready)）。缺这两行的后果：
    #     匹配状态机 ActivateSquadCommand 置 squadModel.isActive=true 而
    #     squadSession=null → 小队界面无头像/槽位数据；abandon 后
    #     StatusMediator.onLobbySignalUpdates 读 squadSession.missionId 抛
    #     TypeError #1009 → UI 永卡 "processing"。同一玩家复用行 id（官方行为）。
    solo = _SOLO_SQUADS.get(pid)
    if solo is None:
        solo = {'squad_row': _next_match_id(_MM_SQUAD_ID),
                'member_row': _next_match_id(_MM_SOLO_SMS_ID),
                'mission_id': 0, 'entered': 0}
        _SOLO_SQUADS[pid] = solo
    solo.update({'mission_id': 0, 'faction': faction, 'template': template,
                 'char_id': int(char_id), 'gamertag': gamertag,
                 'char_name': char_name, 'rank': rank,
                 'metrics_session_id': metrics_session_id})
    if view_cmd is not None:
        await _push_kvcs(writer, view_cmd, b'SquadSession',
                         _msg_solo_squad_session(solo['squad_row'], pid, template,
                                                 faction, 0, solo.get('entered', 0)))
        await _push_kvcs(writer, view_cmd, b'SquadMemberSession',
                         await _msg_solo_squad_member_session(
                             solo['member_row'], solo['squad_row'], pid, gamertag,
                             int(char_id), char_name, rank, 3,
                             metrics_session_id, faction))
        _log(f'S->C cmd={view_cmd:<4} KeyValueChangeSet '
             f'(SquadSession id={solo["squad_row"]} + SquadMemberSession '
             f'id={solo["member_row"]} status=3 Ready ★solo squad)')

    # 1c. 等待期中途加入：已有同模式、选图兼容、未满员的战局仍处于开局
    #     等待窗口内（mm_config.JOIN_WINDOW）→ 直接进该局（不另开新局）；
    #     玩家指定了战局（f2 missionId，战争地图点进行中战斗）则只考虑该局。
    #     找不到/超窗/满员/阵营不符 → 正常入池撮合。
    entry = mm_pool.QueueEntry(
        player_id=pid, char_id=int(char_id), faction=faction,
        gamemode_id=gamemode_id, presets=staged_presets)
    # 1c-0. ★ rejoin 只能回原局：玩家在存活战局中掉线重连（drop_player
    #       不清已成局会话）→ 直接回原局，不走 late join 搜索（否则按
    #       创建顺序会撞上别的局）、不另开新局。仅在未显式指定战局
    #       （req_mission_id 空）时生效——玩家指定战局时尊重指定。
    if not req_mission_id:
        rejoin_mid = _find_player_active_mission(pid)
        if rejoin_mid:
            _log(f'rejoin: player {pid} -> original mission={rejoin_mid} '
                 f'(session recovered, skip late-join search)')
            await _join_existing_mission(pid, rejoin_mid, entry, ctx)
            return
    joinable = _find_joinable_mission(gamemode_id, staged_presets, faction,
                                      only_mission=req_mission_id or None)
    if joinable is not None:
        await _join_existing_mission(pid, joinable[0], entry, ctx)
        return

    # 2. 入池 + 撮合（训练模式 min=1 立即成组；凑够 min_players 成组；
    #    不足则 _arm_ai_fill 超时 AI 补位）
    POOL.enqueue(entry)
    await _try_form_all()
    if _MM_QUEUES.get(pid, {}).get('mission_id', 0) == 0:
        # 仍在池中 → 启动/重启该玩家的超时 AI 补位计时
        _arm_ai_fill(pid, gamemode_id)


async def _handle_cancel(sess, writer, cmd_id, proto):
    """CancelJoinMatchMakingRequest：响应 ok + 出池 + delete 匹配行（取消排队）。"""
    resp = build_response(cmd_id, 'CancelJoinMatchMakingResponse', pb_int32(1, 1))
    writer.write(resp)
    await writer.drain()
    _log(f'S->C cmd={cmd_id:<4} CancelJoinMatchMakingResponse (response=1 ok)')

    pid = await sess.ensure_player()
    q = _MM_QUEUES.pop(pid, None) if pid else None
    # ★ 整队取消：任何成员取消 → 全队出池回 InLobby（响应已回 ok）
    squad_db_id = int((q or {}).get('ctx', {}).get('squad_db_id') or 0) \
        if q else 0
    if squad_db_id:
        # 本人的队列条目上面已 pop；其余成员由 _cancel_squad_queue 处理
        POOL.remove(pid)
        _MATCH_PMM_IDS.pop(pid, None)
        t = _AI_FILL_TASKS.pop(pid, None)
        if t is not None:
            t.cancel()
        if q and q.get('pmm_id') is not None and sess.playerview_cmd is not None:
            await _push_kvcs_delete(writer, sess.playerview_cmd,
                                    b'PlayerMatchMaking', q['pmm_id'])
            _log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet '
                 f'delete (PlayerMatchMaking id={q["pmm_id"]})')
        await _cancel_squad_queue(squad_db_id)
        await _try_form_all()
        return
    if pid:
        POOL.remove(pid)                       # 出池（未入池时 no-op）
        _MATCH_PMM_IDS.pop(pid, None)
        t = _AI_FILL_TASKS.pop(pid, None)
        if t is not None:
            t.cancel()
    if q and sess.playerview_cmd is not None:
        await _push_kvcs_delete(writer, sess.playerview_cmd, b'PlayerMatchMaking',
                                q['pmm_id'])
        _log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet '
             f'delete (PlayerMatchMaking id={q["pmm_id"]})')
        # 取消匹配：solo squad 回 InLobby（missionId=0），否则客户端
        # SquadSession.f17 残留战局 id → onLobbySignalUpdates 判 LOCKED_IN_COMBAT
        solo = _SOLO_SQUADS.get(pid)
        if solo is not None:
            solo['mission_id'] = 0
            await _push_kvcs(writer, sess.playerview_cmd, b'SquadSession',
                             _msg_solo_squad_session(
                                 solo['squad_row'], pid, solo['template'],
                                 solo['faction'], 0, solo.get('entered', 0)))
            await _push_kvcs(writer, sess.playerview_cmd, b'SquadMemberSession',
                             await _msg_solo_squad_member_session(
                                 solo['member_row'], solo['squad_row'], pid,
                                 solo['gamertag'], solo['char_id'], solo['char_name'],
                                 solo['rank'], 2, solo.get('metrics_session_id', 0),
                                 solo['faction']))
            _log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet '
                 f'(SquadSession mission=0 + SquadMemberSession status=2 InLobby)')
    # 取消后：成局可能性变化（如 1v1 队友撤了）+ 剩余排队人数刷新
    await _try_form_all()


def _msg_mission_info_multi(mission_id, details, zulu):
    """多人局 MissionInfo proto（GetMissionDetailsResponse f2 / MissionList 共构）。

    与旧 AI 局 _msg_mission_info 的差异：f3=成局 preset、f5=动态 mapEntityId
    （gamemap → battlefield 行）、f9=玩家数（AI 补位 +1 与单人 AI 局实测一致）。
    """
    players = details.get('players') or []
    creator = int(players[0]['player_id']) if players else 0
    pcount = len(players) + (1 if details.get('ai_fill') else 0)
    m = b''
    m += pb_int64(1, int(mission_id))              # missionId
    m += pb_int64(2, 0)                            # battleId
    m += pb_int64(3, int(details.get('preset') or AI_MISSIONPRESET))
    m += pb_int64(5, int(details.get('map_entity_id') or MISSION_MAP_ENTITY_ID))
    m += pb_int64(6, 1)                            # mapEntityTypeId
    m += pb_int32(7, 1)                            # isAutogenerated
    # f8 defenderFactionId：客户端 UI 攻防判定（factionIdToUse ==
    # defenderFactionId → 显示 defend）。★曾误填 creator playerId（巨大
    # id 恒不等于玩家阵营）→ 简报恒显示进攻方，与 BattleFieldSettings f3
    # （正确阵营）不一致 = "UI 防守、进游戏进攻"错位。
    # ★ 无默认值：字段缺失/非法由 _handle_get_mission_details 入口拒绝
    #   （_faction_pair 校验），能走到这里说明字段已验证合法。
    m += pb_int64(8, int(details['defender_faction']))
    m += pb_int32(9, pcount)                       # playerCount
    m += pb_int64(10, MISSION_HOSTING_CENTER_ID)
    m += pb_string(11, zulu)                       # activationTimeStamp
    m += pb_int32(12, int(details.get('weather_tod', 1)))    # weatherTimeOfDay
    m += pb_int32(13, int(details.get('weather_condition', 0)))  # weatherCondition
    return m


def _msg_mission_details_response_multi(mission_id, details, zulu):
    """多人局 GetMissionDetailsResponse（结构沿用 AI 局实测值，参战方动态化）。

    - f3 参战方：真人按 faction 分组列 gamertag；防守方无真人（ai_fill / 单侧）
      时保留无成员名单的 AI 方条目
    - f4/f5 突击队/资源：本局 force_pool 抽取的 AT 名单与资源池（类别为
      库值），突击队模板按【参战阵营】查 commandnodetemplate，
      "我方" = 进攻方（人数最多组），AI 方 = 防守方
    - 出兵点/据点模板按本局 gamemap 查表；f8/f9 战局位置 = mapEntityId 对应
      battlefield 行坐标（简报战场名与地图标记一致）
    """
    players = details.get('players') or []
    # ★ 无默认值：入口（_handle_get_mission_details）已用 _faction_pair
    #   校验过攻防阵营，非法 det 到不了这里；行 id 直接索引（不回退）。
    attacker, defender = _faction_pair(details)
    rfid = FACTION_ROW_IDS[attacker]
    afid = FACTION_ROW_IDS[defender]
    preset = int(details.get('preset') or 0) or AI_MISSIONPRESET
    gamemap_id = int(_load_preset(preset).get(3, 0) or 0) or 13
    aps = _mission_accesspoints(gamemap_id)
    ours_ap, theirs_ap = aps[0], aps[1]
    pos = battlefield_pos(int(details.get('map_entity_id') or 0)
                          or MISSION_MAP_ENTITY_ID)

    by_faction = {}
    for p in players:
        by_faction.setdefault(int(p['faction']), []).append(p['gamertag'])

    m = b''
    m += pb_int32(1, 0)  # 他服私服实测 = 0（非 ResponseCodes.ok=1）
    m += pb_message(2, _msg_mission_info_multi(mission_id, details, zulu))
    # f3 参战方：进攻方（真人名单）+ 防守方（有真人列名单，否则 AI 方无名单）
    atk = pb_int64(1, rfid)
    for tag in by_faction.get(attacker, []):
        atk += pb_message(2, pb_string(1, tag))
    m += pb_message(3, atk)
    dtag = by_faction.get(defender, [])
    dfd = pb_int64(1, afid)
    for tag in dtag:
        dfd += pb_message(2, pb_string(1, tag))
    m += pb_message(3, dfd)
    # f4 突击队：本局 AT 抽取名单跨阵营翻译（无池回退官方锚点 ×2）
    for cmd_node in _assault_team_nodes(attacker, mission_id):
        m += pb_message(4, pb_int64(1, rfid) + pb_int64(2, cmd_node) +
                        pb_int64(3, ours_ap) + pb_int64(4, 0))
    for cmd_node in _assault_team_nodes(defender, mission_id):
        m += pb_message(4, pb_int64(1, afid) + pb_int64(2, cmd_node) +
                        pb_int64(3, theirs_ap) + pb_int64(4, 0))
    # f5 资源：从本局资源池渲染（与战斗内 f2/f14 同源同值，消耗后递减）；
    # 每方每类一条挂该方出兵点（客户端跨点求和 → 显示即池剩余量）
    pool = mission_resource_pool(mission_id)
    atk_counts = _pool_counts(pool, attacker)
    dfd_counts = _pool_counts(pool, defender)
    for res_cat, res_air in force_pool.categories(counts_by_faction=pool):
        m += pb_message(5, pb_int64(1, rfid) + pb_int64(2, res_cat) +
                        pb_int64(3, ours_ap) +
                        pb_int64(4, atk_counts.get(res_cat, 0)) +
                        pb_bool(5, res_air))
    for res_cat, res_air in force_pool.categories(counts_by_faction=pool):
        m += pb_message(5, pb_int64(1, afid) + pb_int64(2, res_cat) +
                        pb_int64(3, theirs_ap) +
                        pb_int64(4, dfd_counts.get(res_cat, 0)) +
                        pb_bool(5, res_air))
    # f6 出兵点归属：首个=进攻方，其余交替（Mill 实测 8=进攻, 15=防守；
    # f2 为 factionTemplateId）
    for i, ap in enumerate(aps):
        m += pb_message(6, pb_int64(1, ap) +
                        pb_int64(2, defender if i % 2 else attacker))
    # f7 据点初始归属：0=中立（他服私服实测 {3:120, 4:0}）
    for cp in _mission_capturepoints(gamemap_id):
        m += pb_message(7, pb_int32(3, cp) + pb_int32(4, 0))
    m += pb_float(8, pos[0])   # posX（battlefield 行坐标）
    m += pb_float(9, pos[1])   # posY
    return m


async def _handle_mission_details(sess, writer, cmd_id, proto):
    """GetMissionDetailsRequest：返回 GetMissionDetailsResponse。

    匹配池成局（details 含 'players'）走多人版；旧结构回退 AI 单人版。"""
    f = pb_parse(proto)
    mid = int(f.get(1, 0))
    details = _MM_MISSION_DETAILS.get(mid)
    if details is None:
        resp = build_response(cmd_id, 'GetMissionDetailsResponse', pb_int32(1, 43))
        writer.write(resp)
        await writer.drain()
        _log(f'S->C cmd={cmd_id:<4} GetMissionDetailsResponse (mission_not_found, '
             f'mission={mid})')
        return
    if 'players' in details:
        # ★ 阵营字段门禁：损坏的 det 直接拒绝（mission_invalid），
        #   不用默认阵营渲染简报（错阵营 f8/f3 → UI 攻防错位 / #1009）。
        if _faction_pair(details) is None:
            resp = build_response(cmd_id, 'GetMissionDetailsResponse',
                                  pb_int32(1, 43))
            writer.write(resp)
            await writer.drain()
            _log(f'S->C cmd={cmd_id:<4} GetMissionDetailsResponse '
                 f'(mission_invalid: faction fields, mission={mid})')
            return
        resp_proto = _msg_mission_details_response_multi(mid, details, _zulu_now())
        mode = 'multi'
    else:
        # 角色建号即写 faction，无默认值必要（无 faction 的角色不存在）
        resp_proto = _msg_mission_details_response(
            mid, details['player_id'], details['gamertag'], _zulu_now(),
            int(details['faction']))
        mode = 'AI mode'
    resp = build_response(cmd_id, 'GetMissionDetailsResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    _log(f'S->C cmd={cmd_id:<4} GetMissionDetailsResponse ({mode}, '
         f'mission={mid}, {len(resp_proto)}B)')


async def handle_request(sess, writer, cmd_id, req_type, proto):
    """realtest_server dispatch 分发入口。"""
    if req_type == 'JoinMatchMakingRequest':
        await _handle_join(sess, writer, cmd_id, proto)
    elif req_type == 'CancelJoinMatchMakingRequest':
        await _handle_cancel(sess, writer, cmd_id, proto)
    elif req_type == 'GetMissionDetailsRequest':
        await _handle_mission_details(sess, writer, cmd_id, proto)
    else:
        return False
    return True
