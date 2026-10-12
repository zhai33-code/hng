# -*- coding: utf-8 -*-
"""战斗服 CPU 资源分配（亲核掩码 + server.packthreads）回归测试（2026-09-09）。

背景："人数越多客户端帧数越低"的服务端半边根因 = server.packthreads 引擎
默认 0（每客户端实体状态打包串行 → SendPump 饱和 → 全员 tick 落后 → 客户端
tickrate 自适应被拖慢，docs/hng-combat-protocol.md §6.4/§6.8.4）+ 多实例主
模拟循环（单线程）无核隔离互相挤核。治理：以 B=MM_MAX_COMBAT_SERVERS 为主
控推导每实例亲核掩码窗口（W=N//B，余数前置分配）与 packthreads（P=W-1），
实例按 seq 轮转取槽；warserver 本机 spawn（combat_instances）与远端 agent
（combat_agent，读自己环境的同名变量）两侧同构。

覆盖：
 1. plan_combat_resources 窗口推导（均分 / 余数前置分配 / B=1 全核）
 2. 过订阅钳位（B > N → 每实例 1 核、P=0 串行）
 3. 显式覆盖（packthreads 统一值 / masks 掩码表 P=popcount-1）
 4. 非法输入回退（cores=0 / B=0 / 坏掩码 → []）
 5. detect_cores 优先级（显式 > MM_COMBAT_CORES > NUMBER_OF_PROCESSORS）
 6. resource_slot seq 轮转 + env 覆盖（agent 侧，本机环境隔离）
 7. 启动参数拼装：packthreads>0 → /R server.packthreads=<P>（完整注册名，
    前面必须紧跟 /R）；=0 → 不拼（引擎默认即串行）
 8. 亲核仅 win32 生效（非 Windows apply_affinity 恒 False）
 9. mm_config 旋钮解析（MM_PACKTHREADS / MM_AFFINITY_MASKS / MM_COMBAT_CORES）
"""
import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import combat_agent          # noqa: E402
import combat_instances      # noqa: E402
import mm_config             # noqa: E402


# ---------- 1-4. 推导纯函数 ----------
def test_plan_windows_even():
    """8 核 / B=4：四个 2 核连续窗口，P=1（留 1 核给主模拟循环）。"""
    slots = combat_agent.plan_combat_resources(8, 4)
    assert [s['mask'] for s in slots] == [0x03, 0x0C, 0x30, 0xC0]
    assert all(s['packthreads'] == 1 for s in slots)


def test_plan_windows_remainder():
    """8 核 / B=3：余数 2 前置分配 → 窗口 3,3,2，P=2,2,1。"""
    slots = combat_agent.plan_combat_resources(8, 3)
    assert [s['mask'] for s in slots] == [0x07, 0x38, 0xC0]
    assert [s['packthreads'] for s in slots] == [2, 2, 1]


def test_plan_single_instance():
    """B=1：全核一个窗口，P=N-1。"""
    assert combat_agent.plan_combat_resources(8, 1) == \
        [{'mask': 0xFF, 'packthreads': 7}]


def test_plan_oversubscribe_clamp():
    """B=6 > 4 核：钳为 4 个 1 核窗口，P=0（退回引擎默认串行）。"""
    slots = combat_agent.plan_combat_resources(4, 6)
    assert [s['mask'] for s in slots] == [0x1, 0x2, 0x4, 0x8]
    assert all(s['packthreads'] == 0 for s in slots)


def test_plan_packthreads_override():
    """packthreads>=0 统一覆盖推导值（W-1 失效）。"""
    slots = combat_agent.plan_combat_resources(8, 4, packthreads=3)
    assert [s['mask'] for s in slots] == [0x03, 0x0C, 0x30, 0xC0]
    assert all(s['packthreads'] == 3 for s in slots)


def test_plan_masks_override():
    """显式掩码表：P = popcount(mask)-1；packthreads 覆盖优先于 popcount。"""
    slots = combat_agent.plan_combat_resources(8, 2, masks='0xF0, 0x0F')
    assert [s['mask'] for s in slots] == [0xF0, 0x0F]
    assert all(s['packthreads'] == 3 for s in slots)   # popcount=4 → 3
    slots = combat_agent.plan_combat_resources(8, 2, packthreads=1,
                                               masks='0xF0,0x0F')
    assert all(s['packthreads'] == 1 for s in slots)


def test_plan_invalid_inputs():
    """cores/B 无效或掩码全坏 → []（不亲核、不加 /R）。"""
    assert combat_agent.plan_combat_resources(0, 4) == []
    assert combat_agent.plan_combat_resources(8, 0) == []
    assert combat_agent.plan_combat_resources(-1, 4) == []
    assert combat_agent.plan_combat_resources(8, 4, masks='zz, 0, ') == []


# ---------- 5. 核数探测优先级 ----------
def test_detect_cores_priority(monkeypatch):
    monkeypatch.delenv('MM_COMBAT_CORES', raising=False)
    monkeypatch.setenv('NUMBER_OF_PROCESSORS', '16')
    assert combat_agent.detect_cores() == 16
    monkeypatch.setenv('MM_COMBAT_CORES', '4')
    assert combat_agent.detect_cores() == 4          # env 覆盖 > OS 探测
    assert combat_agent.detect_cores(2) == 2         # 显式参数最高
    monkeypatch.setenv('MM_COMBAT_CORES', '0')
    assert combat_agent.detect_cores() == 16         # 0 = 不覆盖
    monkeypatch.setenv('MM_COMBAT_CORES', 'abc')
    assert combat_agent.detect_cores() == 16         # 非数字 = 不覆盖


# ---------- 6. agent 侧 resource_slot（seq 轮转 + env 覆盖） ----------
def test_resource_slot_rotation(monkeypatch):
    monkeypatch.setenv('MM_COMBAT_CORES', '8')
    monkeypatch.setenv('MM_MAX_COMBAT_SERVERS', '4')
    monkeypatch.delenv('MM_PACKTHREADS', raising=False)
    monkeypatch.delenv('MM_AFFINITY_MASKS', raising=False)
    # warserver 的 seq 是 6 位随机起始 + 递增，取模轮转即可错开窗口
    assert combat_agent.resource_slot(100000) == (0x03, 1)   # 100000 % 4 = 0
    assert combat_agent.resource_slot(100001) == (0x0C, 1)
    assert combat_agent.resource_slot(100003) == (0xC0, 1)
    assert combat_agent.resource_slot(100004) == (0x03, 1)   # 轮转回绕


def test_resource_slot_env_overrides(monkeypatch):
    monkeypatch.setenv('MM_COMBAT_CORES', '8')
    monkeypatch.setenv('MM_MAX_COMBAT_SERVERS', '2')
    monkeypatch.setenv('MM_PACKTHREADS', '3')
    assert combat_agent.resource_slot(7) == (0xF0, 3)
    monkeypatch.setenv('MM_AFFINITY_MASKS', '0x0F, 0xF0')
    monkeypatch.delenv('MM_PACKTHREADS', raising=False)
    assert combat_agent.resource_slot(1) == (0xF0, 3)   # popcount-1


def test_resource_slot_no_plan(monkeypatch):
    """B=0（不限）→ 无槽表 → (0, 0)：不亲核、不加 /R（引擎默认串行）。"""
    monkeypatch.setenv('MM_COMBAT_CORES', '8')
    monkeypatch.setenv('MM_MAX_COMBAT_SERVERS', '0')
    assert combat_agent.resource_slot(1) == (0, 0)


# ---------- 7. 启动参数拼装（两侧同构断言） ----------
def _has_pack_arg(args, val):
    tok = f'server.packthreads={val}'
    i = args.index(tok)
    assert i > 0 and args[i - 1] == '/R', args     # 完整注册名 + /R 前缀


def _has_log_arg(args, seq):
    filename = os.path.join('logs', f'combat-server-{seq}.log')
    i = args.index(filename)
    assert i > 0 and args[i - 1] == '/L', args
    assert not any(a.startswith('boot.logfile=') for a in args)


def test_build_launch_args_packthreads():
    args = combat_agent.build_launch_args(
        'D:\\cs', 5678, 488464, 9001, 123, '10.0.0.9:15110',
        '10.0.0.9', 'Town', None, 2)
    _has_pack_arg(args, 2)
    _has_log_arg(args, 123)
    assert 'boot.disable_eac=1' in args and '/D' in args
    args0 = combat_agent.build_launch_args(
        'D:\\cs', 5678, 488464, 9001, 123, '10.0.0.9:15110',
        '10.0.0.9', 'Town', None, 0)
    assert not any(a.startswith('server.packthreads') for a in args0)
    # countdown 照旧拼装
    argsc = combat_agent.build_launch_args(
        'D:\\cs', 5678, 488464, 9001, 123, 'w', 's', 'Town', 10, 2)
    assert 'controllers.overrideStartCountdown=10' in argsc


def test_combat_spawn_args_packthreads():
    """warserver 本机 spawn 与 agent 拉起参数同构（含 packthreads 段）。"""
    args = combat_instances._combat_spawn_args(
        5678, 488464, 9001, 'Town', 123, '10.0.0.9:15110',
        '10.0.0.9', 2, None)
    _has_pack_arg(args, 2)
    _has_log_arg(args, 123)
    args0 = combat_instances._combat_spawn_args(
        5678, 488464, 9001, 'Town', 123, '10.0.0.9:15110',
        '10.0.0.9', 0, None)
    assert not any(a.startswith('server.packthreads') for a in args0)
    # countdown=None 不拼（与旧逻辑一致；_ensure 传 COMBAT_START_COUNTDOWN_
    # OVERRIDE 时拼完整注册名）
    argsc = combat_instances._combat_spawn_args(
        5678, 488464, 9001, 'Town', 123, 'w', 's', 1, 10)
    assert 'controllers.overrideStartCountdown=10' in argsc


def test_combat_resource_slots_imported():
    """combat_instances import 期槽表结构合法（掩码正数、packthreads>=0）。"""
    slots = combat_instances.COMBAT_RESOURCE_SLOTS
    assert isinstance(slots, list)
    for s in slots:
        assert s['mask'] > 0 and s['packthreads'] >= 0, s


# ---------- 8. 亲核守卫 ----------
def test_apply_affinity_nonwin32():
    """非 Windows / 空掩码恒 False（不抛异常、不碰进程）。"""
    if sys.platform == 'win32':
        return
    assert combat_agent.apply_affinity(4711, 0xF0) is False
    assert combat_agent.apply_affinity(4711, 0) is False
    assert combat_agent.apply_affinity(4711, -1) is False


# ---------- 8b. 进程优先级 ----------
def test_priority_parse_and_guard():
    """优先级解析：合法键映射 class 值；未知值回退 high；Realtime 不在表。"""
    assert combat_agent.PRIORITY_CLASSES['high'] == 0x80
    assert combat_agent.PRIORITY_CLASSES['abovenormal'] == 0x8000
    assert 0x100 not in combat_agent.PRIORITY_CLASSES.values()  # Realtime 禁
    assert combat_agent.combat_priority() == 'high'          # 无 env 默认
    assert combat_agent.combat_priority('above') == 'above'
    assert combat_agent.combat_priority('') is None          # 空 = 不设置
    assert combat_agent.combat_priority(0) is None
    assert combat_agent.combat_priority(0x8000) == 0x8000    # 直接 class 值
    # 未知字符串回退 high（告警不炸）
    assert combat_agent.combat_priority('realtime') == 'high'
    assert combat_agent.combat_priority(0x99999) == 'high'


def test_apply_priority_nonwin32():
    """非 Windows / 未知 class 恒 False（不抛异常、不碰进程）。"""
    if sys.platform == 'win32':
        return
    assert combat_agent.apply_priority(4711, 'high') is False
    assert combat_agent.apply_priority(4711, 0x100) is False
    assert combat_agent.apply_priority(4711, 0) is False
    assert combat_agent.apply_priority(4711, None) is False


def test_priority_env_override(monkeypatch):
    monkeypatch.setenv('MM_COMBAT_PRIORITY', 'above')
    assert combat_agent.combat_priority() == 'above'
    monkeypatch.setenv('MM_COMBAT_PRIORITY', 'normal')
    assert combat_agent.combat_priority() == 'normal'
    monkeypatch.setenv('MM_COMBAT_PRIORITY', 'bogus')
    assert combat_agent.combat_priority() == 'high'     # 未知回退
    monkeypatch.setenv('MM_COMBAT_PRIORITY', '')
    assert combat_agent.combat_priority() is None       # 空 = 不设置
    monkeypatch.delenv('MM_COMBAT_PRIORITY', raising=False)


# ---------- 9. mm_config 旋钮解析 ----------
def test_mm_config_knobs(monkeypatch):
    old = {k: os.environ.get(k) for k in
           ('MM_COMBAT_CORES', 'MM_PACKTHREADS', 'MM_AFFINITY_MASKS')}
    try:
        os.environ['MM_COMBAT_CORES'] = '12'
        os.environ['MM_PACKTHREADS'] = '3'
        os.environ['MM_AFFINITY_MASKS'] = '0xF0, 0x0F'
        importlib.reload(mm_config)
        assert mm_config.COMBAT_CORES == 12
        assert mm_config.COMBAT_PACKTHREADS == 3
        assert mm_config.COMBAT_AFFINITY_MASKS == '0xF0, 0x0F'
        # 推导联动：12 核 / B=默认 4 / P 覆盖 3 → 掩码 3,3,3,3 核窗口
        slots = combat_agent.plan_combat_resources(
            mm_config.COMBAT_CORES, mm_config.MAX_CONCURRENT_COMBAT_SERVERS,
            mm_config.COMBAT_PACKTHREADS, '')
        assert [s['mask'] for s in slots] == [0x7, 0x38, 0x1C0, 0xE00]
        assert all(s['packthreads'] == 3 for s in slots)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(mm_config)
    assert mm_config.COMBAT_PACKTHREADS == -1     # 默认 = 推导
