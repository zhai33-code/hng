"""战斗服主机（combat hosts）配置与选机调度（多机部署）。

server_config 'combat_hosts' 行（管理后台「服务器配置→战斗服主机」热配置，
保存即生效无需重启）登记各机容量/端口池/agent 地址；未配置（无行/解析失败/
STORE 未就绪）回退单机 env 行为（MM_MAX_COMCURRENT / MM_COMBAT_PORT_* /
HNG_COMBAT_WARSERVER），与拆分前逐字节等价。

字段语义（单条 host）：
  name          主机名（实例归属键 + 日志/面板显示）
  addr          该机在 warserver 网络中的地址（握手来源 IP 匹配用）
  public_addr   客户端 game_connect 用地址（缺省 = addr）
  max           本机最大并发实例数（0 = 不限，端口池自然封顶）
  port_base/max 本机独立 UDP 端口池
  agent         None = 本机直接 spawn；http://ip:port = 远端 combat_agent
  warserver     该机 combat server 回连地址（None = HNG_COMBAT_WARSERVER）

本模块只做纯数据 + 同步调度（pick_host / alloc_port / host_matches_ip），
不 import combat_instances / matchmaking（实例表由调用方传入），避免环。
"""
import asyncio
import time

import mm_config
import state
from mm_state import _log

# agent 拉起失败后的选机冷却：期间跳过该机（防一台死机拖慢每次成局）
AGENT_COOLDOWN_SECONDS = 60

# /online.json 触发的缓存刷新防抖（管理后台 30s 轮询；过短会在无人成局
# 时变成定时查库，失去低频语义）
SNAPSHOT_REFRESH_SECONDS = 30

_agent_fail_until = {}    # host name -> 冷却截止（time.monotonic）
_last_hosts = None        # load_hosts 最近一次结果（HTTP 线程同步快照用）
_last_refresh_at = 0.0    # 上次快照触发刷新时刻（monotonic）
_refresh_pending = False


def fallback_host():
    """单机回退配置：与多机改造前的 env 行为完全一致。"""
    return {
        'name': 'local',
        'addr': '127.0.0.1',
        'public_addr': state.PUBLIC_HOST,
        'max': int(mm_config.MAX_CONCURRENT_COMBAT_SERVERS or 0),
        'port_base': int(mm_config.COMBAT_PORT_BASE),
        'port_max': int(mm_config.COMBAT_PORT_MAX),
        'agent': None,
        'warserver': None,
    }


def _parse_hosts(row):
    """解析并校验 server_config 值（宽容策略：跳过无效条目并记日志；
    全部无效/结构不对返回 None → 调用方回退单机）。"""
    if not isinstance(row, dict):
        return None
    raw = row.get('hosts')
    if not isinstance(raw, list) or not raw:
        return None
    out = []
    for i, e in enumerate(raw):
        try:
            if not isinstance(e, dict):
                raise ValueError('条目不是对象')
            h = {
                'name': str(e.get('name') or '').strip(),
                'addr': str(e.get('addr') or '').strip(),
                'public_addr': str(e.get('public_addr') or
                                   e.get('addr') or '').strip(),
                'max': int(e.get('max') or 0),
                'port_base': int(e.get('port_base') or 0),
                'port_max': int(e.get('port_max') or 0),
                'agent': (str(e['agent']).strip() or None)
                         if e.get('agent') else None,
                'warserver': (str(e['warserver']).strip() or None)
                             if e.get('warserver') else None,
            }
            if not h['name'] or not h['addr']:
                raise ValueError('name/addr 不能为空')
            if not 0 <= h['max'] <= 64:
                raise ValueError(f'max={h["max"]} 越界（0..64）')
            if not 1024 <= h['port_base'] <= h['port_max'] <= 65535:
                raise ValueError(f'端口池 {h["port_base"]}-{h["port_max"]} 非法')
            ports = h['port_max'] - h['port_base'] + 1
            if h['max'] and ports < h['max']:
                raise ValueError(f'端口池({ports}) < 容量({h["max"]})')
            if h['agent'] and not h['agent'].startswith('http://'):
                raise ValueError('agent 须为 http:// URL')
            out.append(h)
        except Exception as ex:
            _log(f'[combat-hosts] 跳过无效主机条目 #{i}: {ex}')
    names = [h['name'] for h in out]
    if len(names) != len(set(names)):
        _log('[combat-hosts] 配置含重名主机，整体回退单机')
        return None
    return out or None


async def load_hosts():
    """读热配置（成局等低频路径直读库，保存即生效）；失败回退单机。

    同时刷新 _last_hosts 缓存（/online.json 同步快照用）。
    """
    global _last_hosts
    hosts = None
    if state.STORE is not None:
        try:
            hosts = _parse_hosts(await state.STORE.get_config('combat_hosts'))
        except Exception as ex:
            _log(f'[combat-hosts] 配置读取失败，回退单机: '
                 f'{type(ex).__name__}: {ex}')
    if not hosts:
        hosts = [fallback_host()]
    _last_hosts = hosts
    return hosts


def last_hosts():
    """最近一次 load_hosts 结果（未加载过 = 单机回退）；供同步上下文
    （握手认领 / HTTP 快照线程）使用，不触发 IO。"""
    return _last_hosts or [fallback_host()]


def refresh_hosts_soon():
    """经主 loop 异步刷新 _last_hosts（/online.json 拉取时调用）。

    缓存平时只在成局时刷新（load_hosts），刚重启又无人成局的窗口期
    面板会显示 env 回退值而非库里已保存的配置——管理后台每 30s 轮询
    /online.json，借此触发防抖刷新，保存配置后 ≤30s 面板即反映库值。
    """
    global _last_refresh_at, _refresh_pending
    now = time.monotonic()
    if _refresh_pending or now - _last_refresh_at < SNAPSHOT_REFRESH_SECONDS:
        return
    _refresh_pending = True

    async def _do_refresh():
        global _last_refresh_at, _refresh_pending
        try:
            await load_hosts()
        finally:
            _last_refresh_at = time.monotonic()
            _refresh_pending = False

    loop = state.MAIN_LOOP
    if loop is None or loop.is_closed():
        _refresh_pending = False
        return
    asyncio.run_coroutine_threadsafe(_do_refresh(), loop)


def host_running(host, instances):
    """该机当前运行实例数（含已预留未拉起的——预留即占坑）。"""
    name = host.get('name')
    return sum(1 for i in instances.values()
               if (i.get('host') or {}).get('name') == name)


def alloc_port(host, instances):
    """从该机端口池分配空闲 UDP 端口（只避开同机实例已占用端口）。"""
    used = {i['port'] for i in instances.values()
            if (i.get('host') or {}).get('name') == host.get('name')}
    for p in range(int(host['port_base']), int(host['port_max']) + 1):
        if p not in used:
            return p
    return None


def mark_host_failed(name):
    """agent 拉起/停止失败：记冷却（选机临时跳过该机）。"""
    _agent_fail_until[str(name)] = time.monotonic() + AGENT_COOLDOWN_SECONDS


def in_cooldown(host):
    return _agent_fail_until.get(host.get('name'), 0) > time.monotonic()


def pick_host(hosts, instances=None):
    """选机：过滤（容量有余 + 端口池有余 + 非 agent 冷却），
    取剩余绝对容量最大者（并列取配置顺序）。

    instances 缺省取 combat_instances.COMBAT_INSTANCES（延迟 import 防环）。
    全满 → None（调用方进 PENDING_MATCHES 排队）。
    """
    if instances is None:
        from combat_instances import COMBAT_INSTANCES as instances
    best, best_free = None, -1
    for h in hosts:
        if in_cooldown(h):
            continue
        running = host_running(h, instances)
        maxn = int(h.get('max') or 0)
        if maxn and running >= maxn:
            continue
        if alloc_port(h, instances) is None:
            continue
        free = (maxn - running) if maxn else 999
        if free > best_free:
            best, best_free = h, free
    return best


def host_matches_ip(host, ip):
    """握手来源 IP 是否属于该机：127.0.0.1 视为本机 spawn 条目
    （agent=None）；其余按 addr / public_addr / agent URL 主机名匹配。"""
    if not ip:
        return False
    if ip in ('127.0.0.1', '::1'):
        return not host.get('agent')
    cands = {host.get('addr'), host.get('public_addr')}
    agent = host.get('agent') or ''
    if agent.startswith('http://'):
        cands.add(agent[7:].split('/', 1)[0].split(':')[0])
    return ip in {c for c in cands if c}


def describe_hosts(hosts, instances=None):
    """队列日志用各机状态串（name running/max，冷却加标记）。"""
    if instances is None:
        from combat_instances import COMBAT_INSTANCES as instances
    try:
        return ', '.join(
            f'{h["name"]}:{host_running(h, instances)}'
            f'/{h.get("max") or "∞"}{"(cooldown)" if in_cooldown(h) else ""}'
            for h in hosts)
    except Exception:
        return '?'


def snapshot_hosts(instances=None):
    """各机实时运行数快照（web_server /online.json 用；HTTP 线程同步读，
    用 _last_hosts 缓存；instances 迭代撞上增删按空处理）。"""
    if instances is None:
        from combat_instances import COMBAT_INSTANCES as instances
    try:
        items = list(instances.values())
    except RuntimeError:
        items = []
    out = []
    for h in last_hosts():
        running = sum(1 for i in items
                      if (i.get('host') or {}).get('name') == h.get('name'))
        out.append({'name': h.get('name'), 'addr': h.get('addr'),
                    'public_addr': h.get('public_addr'),
                    'max': h.get('max'), 'running': running,
                    'agent': h.get('agent'), 'agent_up': not in_cooldown(h)})
    return out
