"""战斗服多机部署（combat_hosts / combat_instances / matchmaking 集成点）单测。

纯逻辑 + 假 agent（patch combat_instances._agent_request），无需服务器/DB：

A. combat_hosts 配置层
 1. fallback_host = env 单机行为（name/agent/max/端口池）
 2. _parse_hosts 校验：合法解析（public_addr/warserver 缺省回退）/无效条目
    跳过 / 空列表 / 重名 / 端口池非法 → None
 3. load_hosts 热配置：有效行 → 多机；无行/解析失败/STORE None → 单机回退
 4. pick_host：剩余容量最大者优先；容量满/端口池尽/agent 冷却 → 跳过；
    全满 → None；max=0（不限）恒可选
 5. alloc_port：各机端口池独立（同端口号可分属不同机）
 6. host_matches_ip：127.0.0.1=本机 spawn 条目；addr/public_addr/agent
    主机名匹配；不匹配 → False
 7. agent 冷却：mark_host_failed 后选机跳过，到期自愈
B. combat_instances 实例层
 8. reserve_combat_instance 占坑占端口（host_running 含预留）
 9. claim_pending_combat_instance：全局 FIFO（无 IP 旧路径）/ 按来源机
    FIFO（跨机不错配）/ 该机无待认领 → None / 未知 IP → 全局回退
10. _ensure_combat_server agent 路径：launch ok（agent_pid + 待认领登记，
    握手超时保留坑位）/ launch 失败（回收 + 冷却 + 重派排队局）
11. _on_combat_conn_closed：agent 确认存活 → 保留；agent 确认死亡/不可达
    → 回收；本机 proc 存活 → 保留、已退出 → 回收
12. _stop_instance 远端走 agent /stop；release_match 清理 + 消费排队局
C. matchmaking 集成点
13. _game_connect_for：多机 public_addr:port / 无 host 桩回退 PUBLIC_HOST
14. _arm_pending_drain：全冷却排队后冷却到期自动重派（防滞留）
D. combat_agent 鉴权与 /stop 收紧（跨公网明文 HTTP 威胁模型）
15. HMAC 签名往返 / 错密钥 / 时间戳超窗 / body 篡改 / path 不符 → 拒
16. nonce 一次性：同 nonce 二次请求（重放）→ 拒
17. 真实 HTTP（ThreadingHTTPServer 本地起 agent）：合法签名 200，
    无签名/错密钥/重放 → 403；签名失败限速：连续失败 IP 封锁
18. warserver _agent_request 签名头构造对得上真 agent（端到端头格式）
19. handle_stop：未登记 pid → 400（不能杀整机任意进程）；登记 pid →
    taskkill + 移除
"""
import asyncio
import http.client
import json
import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import combat_agent
import combat_hosts
import combat_instances
import matchmaking
import mm_config
import state
from state import PUBLIC_HOST


RESULTS = []


def check(label, ok, detail=''):
    RESULTS.append((label, ok))
    print(f'[{"PASS" if ok else "FAIL"}] {label}{("  " + detail) if detail else ""}')


class FakeStore:
    """get_config 桩：load_hosts 只读这一个方法。"""

    def __init__(self, row=None):
        self.row = row

    async def get_config(self, key):
        assert key == 'combat_hosts'
        return self.row


class StubMatch:
    """排队局桩：只需 mission_id/players 属性（_drain 日志 + 分发用）。"""
    mission_id = 0
    players = []
    ai_fill = False


def host_a():
    return {'name': 'A', 'addr': '10.0.0.1', 'public_addr': '203.0.113.1',
            'max': 4, 'port_base': 5700, 'port_max': 5703,
            'agent': None, 'warserver': None}


def host_b():
    return {'name': 'B', 'addr': '10.0.0.2', 'public_addr': '203.0.113.2',
            'max': 8, 'port_base': 5700, 'port_max': 5707,
            'agent': 'http://10.0.0.2:15116', 'warserver': '10.0.0.9:15110'}


def seed_inst(mid, host, port, ready=True):
    """登记运行中实例（reserve_combat_instance 同构，含 host 键）。"""
    inst = combat_instances.reserve_combat_instance(mid, host, port)
    if ready:
        inst['ready'].set()
    return inst


def fresh_globals():
    """清模块级全局（实例表/待认领/排队局/冷却/热配置快照），返回备份。"""
    backup = (dict(combat_instances.COMBAT_INSTANCES),
              list(combat_instances._PENDING_HANDSHAKE),
              list(combat_instances.PENDING_MATCHES),
              dict(combat_hosts._agent_fail_until),
              combat_hosts._last_hosts)
    combat_instances.COMBAT_INSTANCES.clear()
    combat_instances._PENDING_HANDSHAKE.clear()
    combat_instances.PENDING_MATCHES.clear()
    combat_hosts._agent_fail_until.clear()
    return backup


def restore_globals(backup):
    insts, pend, queued, cool, last_hosts = backup
    combat_instances.COMBAT_INSTANCES.clear()
    combat_instances.COMBAT_INSTANCES.update(insts)
    combat_instances._PENDING_HANDSHAKE[:] = pend
    combat_instances.PENDING_MATCHES[:] = queued
    combat_hosts._agent_fail_until.clear()
    combat_hosts._agent_fail_until.update(cool)
    combat_hosts._last_hosts = last_hosts


async def test_parse_and_fallback():
    # ---- 1. fallback_host = env 单机行为 ----
    fb = combat_hosts.fallback_host()
    check('fallback_host 名称 local / 非远端', fb['name'] == 'local'
          and fb['agent'] is None, str(fb))
    check('fallback_host 容量/端口池来自 env',
          fb['max'] == mm_config.MAX_CONCURRENT_COMBAT_SERVERS
          and fb['port_base'] == mm_config.COMBAT_PORT_BASE
          and fb['port_max'] == mm_config.COMBAT_PORT_MAX)
    check('fallback_host public_addr = PUBLIC_HOST',
          fb['public_addr'] == PUBLIC_HOST)

    # ---- 2. _parse_hosts ----
    row = {'hosts': [
        {'name': 'A', 'addr': '10.0.0.1', 'max': 4,
         'port_base': 5700, 'port_max': 5703},
        {'name': 'B', 'addr': '10.0.0.2', 'max': 8,
         'port_base': 5700, 'port_max': 5707, 'agent': 'http://10.0.0.2:15116',
         'public_addr': '203.0.113.2', 'warserver': '10.0.0.9:15110'},
        # 无效条目（容量越界）→ 跳过，不影响其余
        {'name': 'bad', 'addr': '10.0.0.3', 'max': 99,
         'port_base': 5800, 'port_max': 5810},
    ]}
    hosts = combat_hosts._parse_hosts(row)
    check('_parse_hosts 合法两机解析（无效条目跳过）',
          hosts is not None and [h['name'] for h in hosts] == ['A', 'B'])
    check('_parse_hosts public_addr 缺省 = addr',
          hosts[0]['public_addr'] == '10.0.0.1')
    check('_parse_hosts agent/warserver 透传',
          hosts[1]['agent'] == 'http://10.0.0.2:15116'
          and hosts[1]['warserver'] == '10.0.0.9:15110')
    check('_parse_hosts 非法：空列表 → None',
          combat_hosts._parse_hosts({'hosts': []}) is None)
    check('_parse_hosts 非法：非 dict → None',
          combat_hosts._parse_hosts(None) is None)
    check('_parse_hosts 非法：重名 → 整体回退 None',
          combat_hosts._parse_hosts({'hosts': [
              {'name': 'A', 'addr': '1.1.1.1', 'max': 1,
               'port_base': 5700, 'port_max': 5701},
              {'name': 'A', 'addr': '2.2.2.2', 'max': 1,
               'port_base': 5710, 'port_max': 5711}]}) is None)
    check('_parse_hosts 非法：端口池 < 容量 → None',
          combat_hosts._parse_hosts({'hosts': [
              {'name': 'A', 'addr': '1.1.1.1', 'max': 4,
               'port_base': 5700, 'port_max': 5701}]}) is None)

    # ---- 3. load_hosts ----
    old_store = state.STORE
    state.STORE = FakeStore(row)
    try:
        got = await combat_hosts.load_hosts()
        check('load_hosts 热配置生效', len(got) == 2
              and got[0]['name'] == 'A')
        check('load_hosts 刷新 last_hosts 快照',
              [h['name'] for h in combat_hosts.last_hosts()] == ['A', 'B'])
        state.STORE = FakeStore(None)
        got = await combat_hosts.load_hosts()
        check('load_hosts 无配置行 → 单机回退',
              len(got) == 1 and got[0]['name'] == 'local')
        state.STORE = FakeStore({'hosts': 'garbage'})
        got = await combat_hosts.load_hosts()
        check('load_hosts 解析失败 → 单机回退',
              len(got) == 1 and got[0]['name'] == 'local')
    finally:
        state.STORE = None if old_store is None else old_store
    state.STORE = None
    got = await combat_hosts.load_hosts()
    check('load_hosts STORE 未就绪 → 单机回退',
          len(got) == 1 and got[0]['name'] == 'local')


def test_pick_and_ports():
    # ---- 4. pick_host ----
    a, b = host_a(), host_b()
    hosts = [a, b]
    check('pick_host 空载取剩余容量最大者（B 8 > A 4）',
          combat_hosts.pick_host(hosts, {})['name'] == 'B')
    for i in range(8):                 # B 满（8 实例，端口池 5700-5707）
        seed_inst(910000 + i, b, 5700 + i)
    check('pick_host B 满 → A',
          combat_hosts.pick_host(hosts)['name'] == 'A')
    for i in range(4):                 # A 也满
        seed_inst(910100 + i, a, 5700 + i)
    check('pick_host 全满 → None',
          combat_hosts.pick_host(hosts) is None)
    combat_instances.COMBAT_INSTANCES.pop(910100, None)   # A 腾 1 坑
    check('pick_host A 腾坑后可再选',
          (combat_hosts.pick_host(hosts) or {}).get('name') == 'A')
    combat_hosts.mark_host_failed('A')
    check('pick_host agent 冷却中的主机被跳过',
          combat_hosts.pick_host(hosts) is None)
    combat_hosts._agent_fail_until.clear()
    # 端口池耗尽：容量不限（max=0）但端口池占满的机 → 被跳过；另一机容量满
    combat_instances.COMBAT_INSTANCES.clear()
    wide = dict(host_a(), name='W', max=0, port_base=5700, port_max=5703)
    full = dict(host_b(), name='F', max=1, port_base=5800, port_max=5810)
    seed_inst(910210, full, 5800)               # F 容量满（1/1）
    for i in range(4):                          # W 端口池 4 端口占满
        seed_inst(910200 + i, wide, 5700 + i)
    check('pick_host 端口池耗尽（容量不限但池满）→ 跳过该机',
          combat_hosts.pick_host([wide, full]) is None)
    combat_instances.COMBAT_INSTANCES.clear()
    unlimited = dict(host_a(), name='U', max=0, port_base=6000,
                     port_max=6002)
    check('pick_host max=0（不限）恒可选',
          combat_hosts.pick_host([unlimited, a], {})['name'] == 'U')

    # ---- 5. alloc_port 各机独立 ----
    combat_instances.COMBAT_INSTANCES.clear()
    seed_inst(910300, a, 5700)
    seed_inst(910301, b, 5700)
    check('alloc_port 各机端口池独立（同端口可分属不同机）',
          combat_hosts.alloc_port(a, combat_instances.COMBAT_INSTANCES) == 5701
          and combat_hosts.alloc_port(b, combat_instances.COMBAT_INSTANCES) == 5701)
    tiny = dict(host_b(), name='T', port_base=5700, port_max=5701)
    seed_inst(910310, tiny, 5700)
    seed_inst(910311, tiny, 5701)
    check('alloc_port 本机池耗尽 → None',
          combat_hosts.alloc_port(tiny, combat_instances.COMBAT_INSTANCES) is None)

    # ---- 6. host_matches_ip ----
    check('host_matches_ip 127.0.0.1 → 本机 spawn 条目（agent=None）',
          combat_hosts.host_matches_ip(a, '127.0.0.1') is True
          and combat_hosts.host_matches_ip(b, '127.0.0.1') is False)
    check('host_matches_ip addr 匹配',
          combat_hosts.host_matches_ip(b, '10.0.0.2'))
    check('host_matches_ip public_addr 匹配',
          combat_hosts.host_matches_ip(b, '203.0.113.2'))
    check('host_matches_ip agent URL 主机名（带端口）匹配',
          combat_hosts.host_matches_ip(b, '10.0.0.2'))
    check('host_matches_ip 不相关 IP → False',
          combat_hosts.host_matches_ip(a, '10.0.0.2') is False)
    check('host_matches_ip 空 IP → False',
          combat_hosts.host_matches_ip(a, '') is False)

    # ---- 7. agent 冷却到期自愈 ----
    old_cd = combat_hosts.AGENT_COOLDOWN_SECONDS
    combat_hosts.AGENT_COOLDOWN_SECONDS = 0.05
    try:
        combat_hosts.mark_host_failed('A')
        check('mark_host_failed 后 in_cooldown',
              combat_hosts.in_cooldown(a))
        time.sleep(0.08)
        check('冷却到期自愈', not combat_hosts.in_cooldown(a))
    finally:
        combat_hosts.AGENT_COOLDOWN_SECONDS = old_cd
        combat_hosts._agent_fail_until.clear()


def test_reserve_and_claim():
    # ---- 8. reserve_combat_instance ----
    combat_instances.COMBAT_INSTANCES.clear()
    a = host_a()
    inst = combat_instances.reserve_combat_instance(920001, a, 5700)
    check('reserve 登记实例 + host 透传',
          combat_instances.COMBAT_INSTANCES[920001] is inst
          and inst['host']['name'] == 'A' and inst['port'] == 5700)
    check('host_running 含已预留未拉起实例',
          combat_hosts.host_running(a, combat_instances.COMBAT_INSTANCES) == 1)
    check('_alloc_combat_port(host) 走该机端口池',
          combat_instances._alloc_combat_port(a) == 5701)
    check('_alloc_combat_port() 旧全局口径仍可用（兼容）',
          isinstance(combat_instances._alloc_combat_port(), int))

    # ---- 9. claim_pending_combat_instance ----
    combat_instances.COMBAT_INSTANCES.clear()
    combat_instances._PENDING_HANDSHAKE.clear()
    a, b = host_a(), host_b()
    combat_hosts._last_hosts = [a, b]   # 模拟热配置已加载（同步认领读快照）
    ia = seed_inst(920101, a, 5700, ready=False)   # A 机未握手
    ib = seed_inst(920102, b, 5700, ready=False)   # B 机未握手
    combat_instances._PENDING_HANDSHAKE[:] = [920101, 920102]
    check('claim 全局 FIFO（无 IP 旧路径）取最早',
          combat_instances.claim_pending_combat_instance() == 920101)
    combat_instances._PENDING_HANDSHAKE[:] = [920101, 920102]
    check('claim 按来源机（B 的 IP → B 的实例，不错配 A）',
          combat_instances.claim_pending_combat_instance('10.0.0.2') == 920102)
    combat_instances._PENDING_HANDSHAKE[:] = [920101]
    check('claim 该机无待认领 → None（绝不认领他机）',
          combat_instances.claim_pending_combat_instance('10.0.0.2') is None)
    combat_instances._PENDING_HANDSHAKE[:] = [920101]
    check('claim 127.0.0.1 视为本机 spawn 条目',
          combat_instances.claim_pending_combat_instance('127.0.0.1') == 920101)
    combat_instances._PENDING_HANDSHAKE[:] = [920101]
    check('claim 未知来源 IP → 全局 FIFO 回退（兼容旧分离部署）',
          combat_instances.claim_pending_combat_instance('99.9.9.9') == 920101)
    combat_instances._PENDING_HANDSHAKE[:] = [920101]
    ia['ready'].set()
    check('claim 跳过已 ready 的陈旧条目',
          combat_instances.claim_pending_combat_instance('10.0.0.1') is None
          and not combat_instances._PENDING_HANDSHAKE)


async def test_ensure_agent():
    """_ensure_combat_server agent 路径（假 agent：patch _agent_request）。"""
    b = host_b()
    calls = []

    async def fake_agent(host, method, path, body=None):
        calls.append((method, path, body))
        if path == '/launch':
            return {'pid': 4321}
        if path == '/status':
            return {'instances': [{'pid': 4321}]}
        return {'ok': True}

    real = combat_instances._agent_request
    combat_instances._agent_request = fake_agent
    try:
        # ---- launch ok：登记 agent_pid + 待认领；握手超时保留坑位 ----
        combat_instances.COMBAT_INSTANCES.clear()
        combat_instances._PENDING_HANDSHAKE.clear()
        inst = combat_instances.reserve_combat_instance(930001, b, 5700)
        ok = await combat_instances._ensure_combat_server(930001, 777,
                                                          'map/skirmish', 0.05)
        check('_ensure agent launch ok（假 agent 回 pid）', ok is False)  # 超时放行
        check('_ensure 登记 agent_pid + 待认领队列',
              inst['agent_pid'] == 4321
              and 930001 in combat_instances._PENDING_HANDSHAKE)
        check('_ensure 握手超时不回收坑位（等迟到的握手）',
              930001 in combat_instances.COMBAT_INSTANCES)

        # ---- launch 失败：回收 + 冷却 + 重派排队局 ----
        combat_instances.COMBAT_INSTANCES.clear()
        combat_instances._PENDING_HANDSHAKE.clear()
        combat_hosts._agent_fail_until.clear()

        async def fail_agent(host, method, path, body=None):
            raise RuntimeError('agent down')

        combat_instances._agent_request = fail_agent
        inst = combat_instances.reserve_combat_instance(930002, b, 5700)
        ok = await combat_instances._ensure_combat_server(930002, 778,
                                                          'map/skirmish')
        check('_ensure agent launch 失败 → False', ok is False)
        check('_ensure launch 失败回收实例登记',
              930002 not in combat_instances.COMBAT_INSTANCES)
        check('_ensure launch 失败主机进冷却',
              combat_hosts.in_cooldown(b))

        # ---- _on_combat_conn_closed：存活保留 / 死亡回收 ----
        combat_instances._agent_request = fake_agent
        inst = combat_instances.reserve_combat_instance(930003, b, 5701)
        inst['agent_pid'] = 4321
        await combat_instances._on_combat_conn_closed(930003)
        check('conn-closed agent 确认 pid 存活 → 保留坑位',
              930003 in combat_instances.COMBAT_INSTANCES)

        async def dead_agent(host, method, path, body=None):
            if path == '/status':
                return {'instances': []}
            raise RuntimeError('unreachable')

        combat_instances._agent_request = dead_agent
        await combat_instances._on_combat_conn_closed(930003)
        await asyncio.sleep(0.05)      # _reclaim 内部 _spawn 让出事件循环
        check('conn-closed agent 确认 pid 已死 → 回收坑位',
              930003 not in combat_instances.COMBAT_INSTANCES)

        # agent 整机不可达 → 按死亡回收（坑位永不泄漏）
        inst = combat_instances.reserve_combat_instance(930004, b, 5702)
        inst['agent_pid'] = 4321

        async def unreachable_agent(host, method, path, body=None):
            raise ConnectionError('no route')

        combat_instances._agent_request = unreachable_agent
        await combat_instances._on_combat_conn_closed(930004)
        await asyncio.sleep(0.05)
        check('conn-closed agent 不可达 → 回收坑位（防泄漏）',
              930004 not in combat_instances.COMBAT_INSTANCES)

        # ---- 本机实例：proc 存活保留 / 已退出回收 ----
        a = host_a()

        class FakeProc:
            returncode = None

        inst = combat_instances.reserve_combat_instance(930005, a, 5700)
        inst['proc'] = FakeProc()
        await combat_instances._on_combat_conn_closed(930005)
        check('conn-closed 本机 proc 存活 → 保留坑位',
              930005 in combat_instances.COMBAT_INSTANCES)
        inst['proc'].returncode = -1
        await combat_instances._on_combat_conn_closed(930005)
        await asyncio.sleep(0.05)
        check('conn-closed 本机 proc 已退出 → 回收坑位',
              930005 not in combat_instances.COMBAT_INSTANCES)

        # ---- _stop_instance 远端走 agent /stop ----
        combat_instances._agent_request = fake_agent
        calls.clear()
        inst = combat_instances.reserve_combat_instance(930006, b, 5703)
        inst['agent_pid'] = 5555
        await combat_instances._stop_instance(inst)
        check('_stop_instance 远端实例经 agent /stop（带 pid）',
              calls == [('POST', '/stop', {'pid': 5555})], str(calls))
    finally:
        combat_instances._agent_request = real
        combat_hosts._agent_fail_until.clear()


async def test_release_and_pending():
    """release_match：清理 + agent 停止 + 消费排队局（PENDING_MATCHES）。"""
    b = host_b()

    async def fake_agent(host, method, path, body=None):
        return {'ok': True}

    real = combat_instances._agent_request
    real_dispatch = matchmaking._dispatch_match
    dispatched = []
    matchmaking._dispatch_match = lambda m: dispatched.append(m)
    combat_instances._agent_request = fake_agent
    try:
        inst = combat_instances.reserve_combat_instance(940001, b, 5700)
        inst['agent_pid'] = 4321
        stub = StubMatch()
        combat_instances.PENDING_MATCHES.append(stub)
        combat_instances.release_match(940001)
        await asyncio.sleep(0.05)
        check('release_match 清实例登记',
              940001 not in combat_instances.COMBAT_INSTANCES)
        check('release_match 消费排队局（重派）',
              combat_instances.PENDING_MATCHES == []
              and dispatched == [stub])
    finally:
        combat_instances._agent_request = real
        matchmaking._dispatch_match = real_dispatch
        combat_instances.PENDING_MATCHES.clear()


def test_game_connect():
    # ---- 13. _game_connect_for ----
    b = host_b()
    inst = {'port': 5701, 'host': b}
    check('_game_connect_for 多机 → public_addr:port',
          matchmaking._game_connect_for(inst) == '203.0.113.2:5701')
    check('_game_connect_for 无 host 测试桩 → PUBLIC_HOST:port',
          matchmaking._game_connect_for({'port': 5701}) == f'{PUBLIC_HOST}:5701')
    check('_game_connect_for None → PUBLIC_HOST:0',
          matchmaking._game_connect_for(None) == f'{PUBLIC_HOST}:0')
    fb = combat_hosts.fallback_host()
    check('_game_connect_for 单机回退 = PUBLIC_HOST',
          matchmaking._game_connect_for({'port': 5678, 'host': fb})
          == f'{PUBLIC_HOST}:5678')


async def test_pending_drain_retry():
    # ---- 14. _arm_pending_drain：全冷却排队 → 到期自动重派 ----
    a, b = host_a(), host_b()
    combat_hosts.mark_host_failed('A')
    combat_hosts.mark_host_failed('B')
    old_cd = combat_hosts.AGENT_COOLDOWN_SECONDS
    combat_hosts.AGENT_COOLDOWN_SECONDS = 0.05
    drained = []
    real_drain = combat_instances._drain_pending_matches
    real_dispatch = matchmaking._dispatch_match
    dispatched = []

    def spy_drain():
        drained.append(True)
        real_drain()

    combat_instances._drain_pending_matches = spy_drain
    matchmaking._dispatch_match = lambda m: dispatched.append(m)
    try:
        stub = StubMatch()
        combat_instances.PENDING_MATCHES.append(stub)
        matchmaking._arm_pending_drain([a, b])
        # _later 睡 AGENT_COOLDOWN_SECONDS + 1（安全边距），等过线再断言
        await asyncio.sleep(1.2)
        check('冷却到期后自动重派排队局（防滞留）',
              bool(drained) and combat_instances.PENDING_MATCHES == []
              and dispatched == [stub])
        # 无冷却场景：不安排（spy 不应再被触发）
        drained.clear()
        dispatched.clear()
        combat_hosts._agent_fail_until.clear()
        matchmaking._arm_pending_drain([a, b])
        await asyncio.sleep(0.08)
        check('无冷却主机时不安排重派任务', not drained and not dispatched)
    finally:
        combat_instances._drain_pending_matches = real_drain
        matchmaking._dispatch_match = real_dispatch
        combat_hosts.AGENT_COOLDOWN_SECONDS = old_cd
        combat_hosts._agent_fail_until.clear()
        combat_instances.PENDING_MATCHES.clear()


async def test_refresh_hosts_soon():
    """/online.json 触发的防抖刷新：重启后无人成局时快照也能反映库配置。"""
    row = {'hosts': [
        {'name': 'A', 'addr': '10.0.0.1', 'max': 4,
         'port_base': 5700, 'port_max': 5703},
    ]}
    old_store = state.STORE
    old_loop = state.MAIN_LOOP
    state.MAIN_LOOP = asyncio.get_running_loop()
    state.STORE = FakeStore(row)
    try:
        # 模拟刚重启：_last_hosts=None → 快照显示 env 单机回退
        combat_hosts._last_hosts = None
        check('刷新前快照 = env 单机回退',
              combat_hosts.snapshot_hosts()[0]['name'] == 'local'
              and combat_hosts.snapshot_hosts()[0]['max']
              == mm_config.MAX_CONCURRENT_COMBAT_SERVERS)
        # HTTP 线程语义：refresh_hosts_soon 投递主 loop 后立即返回
        combat_hosts.refresh_hosts_soon()
        await asyncio.sleep(0.05)
        snap = combat_hosts.snapshot_hosts()
        check('refresh_hosts_soon 刷新缓存 → 快照反映库配置',
              snap[0]['name'] == 'A' and snap[0]['max'] == 4)
        check('防抖窗口内不重复投递',
              combat_hosts._refresh_pending is False)
        # 30s 窗口内再调 → 不查库（用 row 篡改探测：若又查了会拿到新值）
        state.STORE = FakeStore({'hosts': 'garbage'})
        combat_hosts.refresh_hosts_soon()
        await asyncio.sleep(0.05)
        check('防抖窗口内二次调用不查库',
              combat_hosts.snapshot_hosts()[0]['name'] == 'A')
        # 窗口过期后 → 重新查库（garbage → 单机回退）
        combat_hosts._last_refresh_at -= (combat_hosts
                                          .SNAPSHOT_REFRESH_SECONDS + 1)
        combat_hosts.refresh_hosts_soon()
        await asyncio.sleep(0.05)
        check('窗口过期后重新查库（解析失败 → 单机回退）',
              combat_hosts.snapshot_hosts()[0]['name'] == 'local')
    finally:
        state.STORE = None if old_store is None else old_store
        state.MAIN_LOOP = old_loop


async def test_agent_auth_and_stop():
    """D 组：combat_agent HMAC 鉴权（签名/防重放/限速）与 /stop 收紧。"""
    SECRET = 'unit-test-secret-0123456789abcdef'

    # ---- 15. 签名验证单元 ----
    hdrs = combat_agent.sign_request('GET', '/status', b'', SECRET)
    check('签名往返校验通过',
          combat_agent.verify_request('GET', '/status', b'', hdrs, SECRET))
    check('密钥不符 → 拒绝',
          not combat_agent.verify_request('GET', '/status', b'',
                                          hdrs, 'wrong-secret'))
    stale = combat_agent.sign_request(
        'GET', '/status', b'', SECRET,
        timestamp=int(time.time()) - combat_agent.SIGNATURE_WINDOW_SECONDS - 10)
    check('时间戳超窗 → 拒绝',
          not combat_agent.verify_request('GET', '/status', b'', stale, SECRET))
    body = b'{"pid": 5555}'
    hdrs_post = combat_agent.sign_request('POST', '/stop', body, SECRET)
    check('body 篡改 → 拒绝',
          not combat_agent.verify_request('POST', '/stop', b'{"pid": 1}',
                                          hdrs_post, SECRET))
    check('path 不符 → 拒绝（/stop 签名不能用于 /launch）',
          not combat_agent.verify_request('POST', '/launch', body,
                                          hdrs_post, SECRET))
    check('缺头 → 拒绝',
          not combat_agent.verify_request('GET', '/status', b'', {}, SECRET))

    # ---- 16. nonce 一次性（防重放）----
    combat_agent._nonce_seen.clear()
    replay = combat_agent.sign_request('GET', '/status', b'', SECRET,
                                       nonce='replay-probe-0001')
    first = combat_agent.verify_request('GET', '/status', b'', replay, SECRET)
    second = combat_agent.verify_request('GET', '/status', b'', replay, SECRET)
    check('nonce 首次过 / 同 nonce 重放拒', first and not second)

    # ---- 17. 真实 HTTP：本地起 agent 线程 ----
    old_token = combat_agent.TOKEN
    old_dir = combat_agent.Handler.combat_dir
    combat_agent.TOKEN = SECRET
    combat_agent.Handler.combat_dir = 'stub-dir'
    combat_agent._auth_failures.clear()
    combat_agent._nonce_seen.clear()
    srv = ThreadingHTTPServer(('127.0.0.1', 0), combat_agent.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def raw_request(method, path, headers, body=b''):
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    try:
        ok_hdrs = combat_agent.sign_request('GET', '/status', b'', SECRET)
        status, data = raw_request('GET', '/status', ok_hdrs)
        check('真 agent 合法签名 /status → 200 + 空 instances',
              status == 200 and json.loads(data) == {'instances': []})

        status, _ = raw_request('GET', '/status', {})
        check('真 agent 无签名 → 403', status == 403)

        bad_hdrs = combat_agent.sign_request('GET', '/status', b'',
                                             'attacker-secret')
        status, _ = raw_request('GET', '/status', bad_hdrs)
        check('真 agent 错密钥签名 → 403', status == 403)

        # 重放：同三头原样再发（HTTP 层防重放，非仅单元层）
        replay_hdrs = combat_agent.sign_request('GET', '/status', b'',
                                                SECRET, nonce='http-replay-01')
        s1, _ = raw_request('GET', '/status', replay_hdrs)
        s2, _ = raw_request('GET', '/status', replay_hdrs)
        check('真 agent 抓包重放完整请求 → 403', s1 == 200 and s2 == 403)

        # 限速：同 IP 连续失败达上限 → 封锁（合法签名也被拒）
        for _ in range(combat_agent.AUTH_FAIL_LIMIT):
            raw_request('GET', '/status', bad_hdrs)
        fresh = combat_agent.sign_request('GET', '/status', b'', SECRET)
        status, _ = raw_request('GET', '/status', fresh)
        check(f'连续 {combat_agent.AUTH_FAIL_LIMIT} 次签名失败 → IP 封锁',
              status == 403)
        combat_agent._auth_failures.clear()      # 解封，供后续用例

        # ---- 18. warserver _agent_request 签名头端到端 ----
        old_tok = combat_instances.AGENT_TOKEN
        combat_instances.AGENT_TOKEN = SECRET
        try:
            host = {'agent': f'http://127.0.0.1:{port}', 'name': 'T'}
            data = await combat_instances._agent_request(host, 'GET', '/status')
            check('_agent_request 签名头构造对得上真 agent',
                  data == {'instances': []})
        finally:
            combat_instances.AGENT_TOKEN = old_tok

        # ---- 19. handle_stop 收紧 ----
        try:
            combat_agent.handle_stop({'pid': 4711})
            check('handle_stop 未登记 pid → 拒绝', False)
        except ValueError:
            check('handle_stop 未登记 pid → 拒绝', True)

        class FakeProc:
            def poll(self):
                return None

        killed = []
        real_run = combat_agent.subprocess.run
        combat_agent.subprocess.run = lambda *a, **k: killed.append(a) or None
        try:
            combat_agent._instances.clear()
            combat_agent._instances[4712] = {
                'proc': FakeProc(), 'port': 5700, 'mission_id': 1,
                'started_at': 0}
            r = combat_agent.handle_stop({'pid': 4712})
            check('handle_stop 登记过的 pid → taskkill + 移除',
                  r == {'ok': True} and 4712 not in combat_agent._instances
                  and killed and '4712' in ' '.join(map(str, killed[0])))
        finally:
            combat_agent.subprocess.run = real_run
            combat_agent._instances.clear()
    finally:
        srv.shutdown()
        srv.server_close()
        combat_agent.TOKEN = old_token
        combat_agent.Handler.combat_dir = old_dir
        combat_agent._auth_failures.clear()
        combat_agent._nonce_seen.clear()


async def main():
    backup = fresh_globals()
    try:
        await test_parse_and_fallback()
        test_pick_and_ports()
        test_reserve_and_claim()
        await test_ensure_agent()
        await test_release_and_pending()
        test_game_connect()
        await test_pending_drain_retry()
        await test_refresh_hosts_soon()
        await test_agent_auth_and_stop()
    finally:
        restore_globals(backup)

    failed = [l for l, ok in RESULTS if not ok]
    print(f'\nCOMBAT_HOSTS: {"ALL PASS" if not failed else f"{len(failed)} FAILED: {failed}"} '
          f'({len(RESULTS) - len(failed)}/{len(RESULTS)})')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
