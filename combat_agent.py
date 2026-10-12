"""战斗服 agent（远端 Windows 机常驻进程）。

多机部署（combat_hosts 配置 agent=http://ip:port 条目）时由 warserver 经
HTTP 调用，在本机拉起/停止/查询 combat server（hng.exe /D）实例；启动参数
与 warserver 本机 spawn（combat_instances._ensure_combat_server）完全同构。
零第三方依赖（Python 3.11 stdlib），可直接 PyInstaller 打包成 exe 部署。

用法：
  python combat_agent.py --port 15116 --token <与 warserver 同值> \
      --combat-dir D:\combat-server

鉴权（HMAC 请求签名；跨公网明文 HTTP 下密钥不上链路）：
  warserver 每个请求带 X-Agent-Timestamp / X-Agent-Nonce / X-Agent-Signature
  三头，签名 = HMAC-SHA256(secret, "ts\\nnonce\\nMETHOD\\npath\\nsha256(body)")。
  agent 校验三关：时间戳 ±SIGNATURE_WINDOW_SECONDS、签名恒时比较、
  nonce 一次性（防重放）。签名失败的来源 IP 滚动限速（连续
  AUTH_FAIL_LIMIT 次 / AUTH_FAIL_WINDOW 秒 → 封锁至失败记录老化）。
  --token 不设 = 完全开放（仅限本机调试，启动时大字警告）。

协议：
  GET  /status            -> {"instances": [{"pid","port","mission_id","started_at"}]}
  POST /launch            -> {"pid": N}    body: port/pms_id/mission_id/map/
                             warserver/statserver_host/seq/countdown
  POST /stop              -> {"ok": true}  body: pid（仅允许本 agent 登记过的
                             实例——密钥泄漏时爆炸半径限战斗进程，杀不了整机）

网络要求（部署手册见 docs/combat-host-deploy.md）：
  - 出站可达 warserver TCP 15110（握手）/ 15115（统计）
  - 入站开放 agent TCP 端口（默认 15116，防火墙限 warserver 源 IP 更佳）
  - 入站开放 combat UDP 端口池（客户端直连打战斗）
  - 与 warserver 时钟大致同步（NTP；签名窗口 ±90s，VPS 默认满足）

CPU 资源分配（2026-09-09，"人数越多客户端帧数越低"的服务端半边治理）：
launch 时按本机核数与 MM_MAX_COMBAT_SERVERS（agent 机自己的环境变量，
建议与 warserver 侧 host max 同值）推导每实例亲核掩码窗口
（SetProcessAffinityMask）+ /R server.packthreads（实体状态打包并行度，
引擎默认 0=串行是多人局 SendPump 饱和根因，见
docs/hng-combat-protocol.md §6.8.4/§B ZOption_Server）；响应附
affinity_mask/packthreads 字段。MM_COMBAT_CORES（核数探测覆盖）/
MM_PACKTHREADS（统一打包线程数，-1=推导）MM_AFFINITY_MASKS（显式掩码
表，逗号分隔十六进制）可覆盖，与 warserver 侧 mm_config 同名。
"""
import argparse
import ctypes
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 15116
DEFAULT_BIND = '0.0.0.0'
# 与 combat_instances.COMBAT_START_COUNTDOWN_OVERRIDE 同源：请求体可覆盖
STATSERVER_PORT = 15115

# 签名时间窗（秒）：两端时钟偏差超过即拒
SIGNATURE_WINDOW_SECONDS = 90
# nonce 一次性缓存的老化周期（覆盖窗口边界请求的重放余量）
NONCE_TTL_SECONDS = SIGNATURE_WINDOW_SECONDS * 2
# 签名失败限速：AUTH_FAIL_WINDOW 秒内失败 AUTH_FAIL_LIMIT 次 → 封锁该 IP
AUTH_FAIL_LIMIT = 10
AUTH_FAIL_WINDOW = 60
# 请求体上限（launch body 很小，防恶意超大 Content-Length 撑内存）
MAX_BODY_BYTES = 65536

_lock = threading.Lock()          # _instances（launch/stop/status）
_auth_lock = threading.Lock()     # _nonce_seen / _auth_failures
_instances = {}     # pid -> {'proc': Popen, 'port', 'mission_id', 'started_at'}
_nonce_seen = {}    # nonce -> 校验时刻（time.time()，TTL 懒清理）
_auth_failures = {}  # ip -> [失败时刻列表]（滚动窗口）
TOKEN = ''


def sign_request(method, path, body, secret, timestamp=None, nonce=None):
    """warserver 侧：为请求构造签名三头（密钥只进 HMAC，不出现在头里）。"""
    ts = str(int(time.time() if timestamp is None else timestamp))
    nonce = nonce or secrets.token_hex(16)
    body_hash = hashlib.sha256(body or b'').hexdigest()
    msg = '\n'.join((ts, nonce, method, path, body_hash)).encode()
    sig = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return {'X-Agent-Timestamp': ts, 'X-Agent-Nonce': nonce,
            'X-Agent-Signature': sig}


def verify_request(method, path, body, headers, secret, now=None):
    """agent 侧：三关校验（时间窗 → 恒时签名比较 → nonce 一次性）。

    headers 兼容 http.client 风格 dict（.get 大小写不敏感由调用方保证，
    BaseHTTPRequestHandler.headers 是 email.Message 天然不敏感）。
    通过即消费 nonce（同 nonce 二次调用必拒 = 重放）。
    """
    ts = str(headers.get('X-Agent-Timestamp', '') or '')
    nonce = str(headers.get('X-Agent-Nonce', '') or '')
    sig = str(headers.get('X-Agent-Signature', '') or '')
    if not ts.isdigit() or not 8 <= len(nonce) <= 128 or not sig:
        return False
    now = time.time() if now is None else now
    if abs(now - int(ts)) > SIGNATURE_WINDOW_SECONDS:
        return False
    body_hash = hashlib.sha256(body or b'').hexdigest()
    msg = '\n'.join((ts, nonce, method, path, body_hash)).encode()
    expect = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expect, sig):
        return False
    with _auth_lock:
        seen_at = _nonce_seen.get(nonce)
        if seen_at is not None and now - seen_at < NONCE_TTL_SECONDS:
            return False                      # 重放
        # TTL 懒清理 + 登记本 nonce（仅签名有效请求可进入：伪造垃圾刷不进表）
        for n in [n for n, t in _nonce_seen.items()
                  if now - t >= NONCE_TTL_SECONDS]:
            _nonce_seen.pop(n, None)
        _nonce_seen[nonce] = now
    return True


def _ip_blocked(ip, now=None):
    """滚动封锁判定：窗口内失败数达上限（记录随窗口老化自动解封）。"""
    now = time.time() if now is None else now
    with _auth_lock:
        fails = [t for t in _auth_failures.get(ip, [])
                 if now - t < AUTH_FAIL_WINDOW]
        _auth_failures[ip] = fails
        return len(fails) >= AUTH_FAIL_LIMIT


def _record_auth_failure(ip, now=None):
    now = time.time() if now is None else now
    with _auth_lock:
        _auth_failures.setdefault(ip, []).append(now)
        if len(_auth_failures) > 4096:      # 防 IP 伪造撑表
            for k in [k for k, ts in _auth_failures.items()
                      if all(now - t >= AUTH_FAIL_WINDOW for t in ts)]:
                del _auth_failures[k]


def _detect_combat_dir(explicit=None):
    """combat-server 目录探测（与 combat_instances._detect_combat_dir 同序）：
    显式参数 > 环境变量 HNG_COMBAT_SERVER_DIR > 脚本旁探测。"""
    for cand in (explicit,
                 os.environ.get('HNG_COMBAT_SERVER_DIR'),
                 os.path.dirname(os.path.abspath(__file__)),
                 os.path.join(
                     os.path.dirname(os.path.abspath(__file__)),
                     'combat-server')):
        if cand and os.path.isfile(os.path.join(cand, 'hng.exe')):
            return cand
    return None


def _prune_dead():
    """懒清理已退出实例（Popen.returncode 轮询）。"""
    for pid in [p for p, it in _instances.items()
                if it['proc'].poll() is not None]:
        _instances.pop(pid, None)


def handle_status():
    with _lock:
        _prune_dead()
        return {'instances': [
            {'pid': pid, 'port': it['port'],
             'mission_id': it['mission_id'],
             'started_at': it['started_at']}
            for pid, it in sorted(_instances.items())]}


# ---------------------------------------------------------------- CPU 资源槽
# 战斗服性能治理（2026-09-09）：专用服每 tick 快照全部实体 × 每客户端打包
# 下发（O(实例数×客户端数)），server.packthreads 引擎默认 0=串行 → 多人局
# SendPump 饱和 → 全员 tick 落后 → 客户端 tickrate 自适应被拖慢（= 玩家报
# "人数越多帧数越低"的服务端半边根因）；主模拟循环单线程需独占一核，多实例
# 互相挤核会丢 60Hz。以最大并发战斗服数 B 为主控推导每实例资源槽（combat_
# instances 从本模块 import plan_combat_resources，warserver 本机 spawn 与
# agent 拉起两侧同构）。

def detect_cores(explicit=0):
    """本机逻辑核数（资源槽推导输入；agent 单文件部署故自带探测）。

    优先级：显式参数 > MM_COMBAT_CORES env > Windows
    NUMBER_OF_PROCESSORS（反映宿主机实际分配）> os.cpu_count()。
    容器/云切片探测失真时用 MM_COMBAT_CORES 钉死。
    """
    if explicit and explicit > 0:
        return int(explicit)
    v = os.environ.get('MM_COMBAT_CORES')
    if v and v.isdigit() and int(v) > 0:
        return int(v)
    v = os.environ.get('NUMBER_OF_PROCESSORS')
    if v and v.isdigit() and int(v) > 0:
        return int(v)
    return os.cpu_count() or 0


def plan_combat_resources(cores, max_servers, packthreads=-1, masks=''):
    """由最大并发战斗服数 B 推导每实例 CPU 资源槽（亲核掩码 + packthreads）。

    公式（连续核段窗口，互不重叠）：
      W_i = N // B，前 N%B 个窗口各 +1 核
      P_i = W_i - 1（窗口内留 1 核给 60Hz 主模拟循环，余核给打包线程）
      掩码 = 窗口核段位掩码；实例按 seq % len(slots) 轮转取槽
    B > N 过订阅 → 钳到 N（每实例至少 1 核、P=0 退回串行），调用方负责告警。
    packthreads >= 0 显式统一覆盖推导值；masks（逗号分隔十六进制掩码表）
    显式覆盖窗口推导，P_i = popcount(mask) - 1。
    返回 [{'mask': int, 'packthreads': int}, ...]；cores/B 无效 → []。
    """
    if masks:
        out = []
        for tok in str(masks).split(','):
            tok = tok.strip()
            if not tok:
                continue
            try:
                m = int(tok, 0)
            except ValueError:
                continue
            if m > 0:
                p = packthreads if packthreads >= 0 \
                    else max(0, bin(m).count('1') - 1)
                out.append({'mask': m, 'packthreads': max(0, p)})
        return out
    if not cores or not max_servers or cores < 1 or max_servers < 1:
        return []
    n, b = int(cores), int(max_servers)
    if b > n:
        b = n                       # 过订阅钳位：每实例至少 1 核
    base, rem = divmod(n, b)
    out, start = [], 0
    for i in range(b):
        w = base + (1 if i < rem else 0)
        p = packthreads if packthreads >= 0 else max(0, w - 1)
        out.append({'mask': ((1 << w) - 1) << start, 'packthreads': max(0, p)})
        start += w
    return out


def resource_slot(seq):
    """本机第 seq 个实例的资源槽 → (亲核掩码, packthreads)。

    读本机环境推导（agent 机与 warserver 机各自算自己的核）：B 取
    MM_MAX_COMBAT_SERVERS（默认 4），MM_PACKTHREADS / MM_AFFINITY_MASKS
    可覆盖。无槽表（核数未知 / B=0 不限）→ (0, 0) = 不亲核、不加 /R
    （引擎默认串行打包）。
    """
    v = os.environ.get('MM_MAX_COMBAT_SERVERS')
    b = int(v) if v and v.lstrip('-').isdigit() else 4
    v = os.environ.get('MM_PACKTHREADS')
    pt = int(v) if v and v.lstrip('-').isdigit() else -1
    cores = detect_cores()
    if cores and b > cores:
        print(f'[agent] WARNING: MM_MAX_COMBAT_SERVERS={b} > 本机核数 '
              f'{cores}（过订阅）：亲核钳为每实例 1 核、串行打包，'
              f'多人局会掉帧', flush=True)
    slots = plan_combat_resources(cores, b, pt,
                                  os.environ.get('MM_AFFINITY_MASKS', ''))
    if not slots:
        return 0, 0
    s = slots[seq % len(slots)]
    return s['mask'], s['packthreads']


def apply_affinity(pid, mask):
    """Windows 进程亲核（kernel32.SetProcessAffinityMask）。

    失败返回 False（仅告警不影响开局——亲核是优化不是正确性）。
    非 Windows / 空掩码直接 False。OpenProcess 权限
    PROCESS_SET_INFORMATION|PROCESS_QUERY_INFORMATION = 0x0200|0x0400。
    """
    if sys.platform != 'win32' or not mask or mask <= 0:
        return False
    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x0200 | 0x0400, False, int(pid))
        if not h:
            return False
        try:
            return bool(k32.SetProcessAffinityMask(h, int(mask)))
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


# Windows 优先级 class（winbase.h；映射子集，禁止 Realtime——会饿死
# 系统进程/驱动，游戏服绝对不能用）。主模拟循环单线程 → 与 PG/frp/
# 多实例同 Normal 级抢核会丢 60Hz，High 保证同核竞争必赢。
PRIORITY_CLASSES = {'normal': 0x20, 'above': 0x8000,
                    'abovenormal': 0x8000, 'high': 0x80}


def apply_priority(pid, priority):
    """Windows 进程优先级（kernel32.SetPriorityClass）。

    priority 接受 PRIORITY_CLASSES 键（不区分大小写）或直接 class 值；
    失败/未知值/非 Windows → False（仅告警不影响开局）。OpenProcess 权限
    同 apply_affinity。
    """
    if sys.platform != 'win32' or not priority:
        return False
    if isinstance(priority, str):
        priority = PRIORITY_CLASSES.get(priority.strip().lower(), 0)
    if priority not in PRIORITY_CLASSES.values():
        return False
    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x0200 | 0x0400, False, int(pid))
        if not h:
            return False
        try:
            return bool(k32.SetPriorityClass(h, int(priority)))
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


def combat_priority(explicit=None):
    """战斗服进程优先级（win32）：normal/above(high 同覆盖口)。

    显式参数 > MM_COMBAT_PRIORITY env > 默认 high（与亲核同哲学：调度
    优化，非正确性）。返回值经 apply_priority 校验（未知值回退 high）。
    """
    v = explicit if explicit is not None \
        else os.environ.get('MM_COMBAT_PRIORITY', 'high')
    if isinstance(v, str):
        v = v.strip().lower()
    if v in (None, '', 0):
        return None
    if isinstance(v, str) and v not in PRIORITY_CLASSES \
            or isinstance(v, int) and v not in PRIORITY_CLASSES.values():
        print(f'[agent] WARNING: MM_COMBAT_PRIORITY='
              f'{os.environ.get("MM_COMBAT_PRIORITY")!r} 未知，回退 high',
              flush=True)
        v = 'high'
    return v


def build_launch_args(combat_dir, port, mission_id, pms_id, seq,
                      warserver, statserver_host, combat_map,
                      countdown=None, packthreads=0):
    """hng.exe /D 专用服启动参数（与 combat_instances 本机 spawn 同构）。"""
    args = [os.path.join(combat_dir, 'hng.exe'),
            '/D', str(port),
            '/S', f'session-{mission_id}',
            '/P', str(pms_id),                # boot.playersessionid
            '/PS', f'login-{pms_id}',
            '/M', combat_map,
            '/W', warserver,
            # /R 选项必须用完整注册名（组.选项），与 warserver 本机路径一致
            '/R', 'boot.disable_eac=1',
            # 日志文件使用直接映射 boot.logfile 的专用 /L 开关，避免
            # /R boot.logfile 未生效、最终仍落到 hng_boot.log。
            '/L', os.path.join('logs', f'combat-server-{seq}.log'),
            '/R', f'boot.statserver={statserver_host}',
            '/R', f'boot.statserverport={STATSERVER_PORT}']
    if packthreads > 0:
        # 实体状态打包并行度（ZWorkQueue 工作线程数；引擎默认 0=串行，
        # 推导依据见 plan_combat_resources 注释）
        args += ['/R', f'server.packthreads={int(packthreads)}']
    if countdown is not None:
        args += ['/R', f'controllers.overrideStartCountdown={int(countdown)}']
    return args


def handle_launch(combat_dir, body):
    port = int(body.get('port') or 0)
    pms_id = int(body.get('pms_id') or 0)
    mission_id = int(body.get('mission_id') or 0)
    seq = int(body.get('seq') or 0)
    warserver = str(body.get('warserver') or '127.0.0.1:15110')
    statserver_host = str(body.get('statserver_host') or '127.0.0.1')
    combat_map = str(body.get('map') or '')
    countdown = body.get('countdown')
    if not (1024 <= port <= 65535) or not pms_id or not combat_map:
        raise ValueError('port/pms_id/map 缺失或非法')
    with _lock:
        _prune_dead()
        if any(it['port'] == port for it in _instances.values()):
            raise ValueError(f'port {port} already in use')
        # 本机资源槽：亲核掩码 + 实体打包并行度（见 resource_slot 注释）
        affinity_mask, packthreads = resource_slot(seq)
        args = build_launch_args(combat_dir, port, mission_id, pms_id, seq,
                                 warserver, statserver_host, combat_map,
                                 countdown, packthreads)
        os.makedirs(os.path.join(combat_dir, 'logs'), exist_ok=True)
        proc = subprocess.Popen(
            args, cwd=combat_dir,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # 进程优先级（默认 high；见 combat_priority 注释）
        prio = combat_priority()
        if prio and not apply_priority(proc.pid, prio):
            print(f'[agent] WARNING: set priority {prio!r} failed '
                  f'for pid {proc.pid}', flush=True)
        if affinity_mask:
            if apply_affinity(proc.pid, affinity_mask):
                print(f'[agent] pid {proc.pid} pinned to cpu mask '
                      f'{affinity_mask:#x} (packthreads={packthreads})',
                      flush=True)
            else:
                print(f'[agent] WARNING: set affinity {affinity_mask:#x} '
                      f'failed for pid {proc.pid}', flush=True)
        _instances[proc.pid] = {'proc': proc, 'port': port,
                                'mission_id': mission_id,
                                'started_at': int(time.time())}
        return {'pid': proc.pid, 'cmd': ' '.join(args[1:]),
                'affinity_mask': affinity_mask, 'packthreads': packthreads}


def handle_stop(body):
    pid = int(body.get('pid') or 0)
    with _lock:
        it = _instances.pop(pid, None)
    if it is None:
        # 只杀本 agent 登记过的实例：密钥泄漏时爆炸半径限战斗进程，
        # 不能借 /stop 杀整机任意进程
        raise ValueError(f'pid {pid} not managed by this agent')
    if it['proc'].poll() is None:
        subprocess.run(['taskkill', '/T', '/F', '/PID', str(pid)],
                       capture_output=True)
    return {'ok': True}


class Handler(BaseHTTPRequestHandler):
    combat_dir = None

    def _reply(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _check_auth(self, method, body):
        """返回 None = 放行；否则拒绝原因（仅记日志，响应统一 403 不泄露细节）。"""
        ip = self.client_address[0]
        if _ip_blocked(ip):
            return f'ip {ip} blocked (auth failures)'
        if not TOKEN:
            return None                     # 未设密钥 = 开放（仅本机调试）
        if verify_request(method, self.path, body, self.headers, TOKEN):
            return None
        _record_auth_failure(ip)
        return 'bad signature/timestamp/nonce'

    def do_GET(self):
        if self.path != '/status':
            return self._reply(404, {'error': 'not found'})
        why = self._check_auth('GET', b'')
        if why:
            print(f'[agent] DENY GET {self.path} from '
                  f'{self.client_address[0]}: {why}', flush=True)
            return self._reply(403, {'error': 'forbidden'})
        try:
            return self._reply(200, handle_status())
        except Exception as e:
            return self._reply(500, {'error': str(e)})

    def do_POST(self):
        if self.path not in ('/launch', '/stop'):
            return self._reply(404, {'error': 'not found'})
        try:
            n = int(self.headers.get('Content-Length') or 0)
            if not 0 <= n <= MAX_BODY_BYTES:
                raise ValueError(f'content-length {n} out of range')
            body = self.rfile.read(n) if n else b''
            payload = json.loads(body or b'{}')
        except Exception as e:
            return self._reply(400, {'error': f'bad request: {e}'})
        # body 参与签名 → 先读后验
        why = self._check_auth('POST', body)
        if why:
            print(f'[agent] DENY POST {self.path} from '
                  f'{self.client_address[0]}: {why}', flush=True)
            return self._reply(403, {'error': 'forbidden'})
        try:
            if self.path == '/launch':
                if not self.combat_dir:
                    raise ValueError('combat dir not found '
                                     '(--combat-dir / HNG_COMBAT_SERVER_DIR)')
                return self._reply(200, handle_launch(self.combat_dir, payload))
            return self._reply(200, handle_stop(payload))
        except ValueError as e:
            return self._reply(400, {'error': str(e)})
        except Exception as e:
            return self._reply(500, {'error': f'{type(e).__name__}: {e}'})

    def log_message(self, fmt, *args):
        print(f'[agent] {self.address_string()} {fmt % args}', flush=True)


def main():
    global TOKEN
    ap = argparse.ArgumentParser(description='HnG combat server agent')
    ap.add_argument('--bind', default=DEFAULT_BIND)
    ap.add_argument('--port', type=int, default=DEFAULT_PORT)
    ap.add_argument('--token', default=os.environ.get('HNG_AGENT_TOKEN', ''),
                    help='HMAC 共享密钥（与 warserver HNG_AGENT_TOKEN 同值，'
                         '建议 secrets.token_urlsafe(32) 生成）')
    ap.add_argument('--combat-dir', default=None,
                    help='combat-server 目录（含 hng.exe）；缺省自动探测')
    args = ap.parse_args()
    TOKEN = args.token
    if TOKEN and len(TOKEN) < 16:
        print('[agent] WARNING: 密钥长度 < 16，跨公网部署易被爆破，'
              '建议 secrets.token_urlsafe(32) 重新生成', flush=True)
    Handler.combat_dir = _detect_combat_dir(args.combat_dir)
    print(f'[agent] combat dir: {Handler.combat_dir or "<NOT FOUND>"}', flush=True)
    if not Handler.combat_dir:
        print('[agent] ERROR: 未找到含 hng.exe 的目录，请用 --combat-dir 指定',
              flush=True)
        raise SystemExit(2)
    print(f'[agent] listening on {args.bind}:{args.port} '
          f'(hmac auth: {"on" if TOKEN else "OFF - OPEN MODE!"})', flush=True)
    ThreadingHTTPServer((args.bind, args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
