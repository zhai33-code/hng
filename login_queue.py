# -*- coding: utf-8 -*-
"""登录排队（LoginQueueUpdate 推送）。

客户端契约（FrontScreenMediator.queueCallback + ServiceHandler.receiveChunk
的 "LoginQueueUpdate" 分支）：
- StartLogin 只发一次；mayProceed=false 时客户端仅显示排队位置并继续等待，
  服务器必须用【原 cmdId】持续推送 LoginQueueUpdate（originalId==cmdId 时
  客户端重置该 pending 请求的超时计时）。
- 收到 mayProceed=true 才 delete _requestList[originalId] 解锁请求队列，
  继续真正的 login2 流程。

本模块实现：
- 在线容量 HNG_MAX_ONLINE（默认 100；<=0 表示不限容量，永远立即放行，
  行为与无排队时完全一致）。
- 在线数 = len(FLASH_SESSIONS)（已登录 Flash 连接）+ len(PENDING)
  （已放行、尚未 subscribeplayerview 的会话）——防止登录中途名额超发。
- 事件驱动（断线出队 / 登录完成释放）+ 1 秒周期 pump 兜底，FIFO 放行。
"""

import asyncio

from protocol import build_response, pb_int32
from state import FLASH_SESSIONS, _env_int, log

MAX_ONLINE = _env_int('HNG_MAX_ONLINE', 100)

# 排队中的条目列表：[(sess, writer, cmd_id)]，FIFO。
QUEUE = []
# 已放行、尚未 subscribeplayerview 注册的会话（占用名额，防超发）。
PENDING = set()

_pump_task = None


def _online_count():
    return len(FLASH_SESSIONS) + len(PENDING)


async def _send_update(writer, cmd_id, position, may_proceed):
    proto = (pb_int32(1, position) +
             pb_int32(2, 1 if may_proceed else 0) +
             pb_int32(3, cmd_id) +           # originalId = 原 StartLogin cmdId
             pb_int32(4, 1))                 # result = 1 (ResponseCodes.ok)
    writer.write(build_response(cmd_id, 'LoginQueueUpdate', proto))
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} LoginQueueUpdate '
        f'(mayProceed={may_proceed}, position={position})')


def _drop_from_queue(sess):
    """从 QUEUE 删除会话条目；返回是否有变动。"""
    for i, (s, _w, _c) in enumerate(QUEUE):
        if s is sess:
            QUEUE.pop(i)
            return True
    return False


async def enter(sess, writer, cmd_id):
    """StartLogin 入口：容量未满立即放行（返回 None），否则入队（返回位置）。

    连接已断（推送失败）时直接放弃，不占排队/名额。
    """
    if MAX_ONLINE <= 0 or _online_count() < MAX_ONLINE:
        try:
            await _send_update(writer, cmd_id, 0, True)
        except Exception:
            return None
        PENDING.add(sess)
        return None
    # 同一连接重复 StartLogin：更新 cmd_id 即可（正常流程每连接只发一次）
    for i, (s, w, _c) in enumerate(QUEUE):
        if s is sess:
            QUEUE[i] = (s, w, cmd_id)
            try:
                await _send_update(writer, cmd_id, i + 1, False)
            except Exception:
                _drop_from_queue(sess)
                return None
            return i + 1
    QUEUE.append((sess, writer, cmd_id))
    pos = len(QUEUE)
    log(f'[queue] server full ({_online_count()}/{MAX_ONLINE}), '
        f'{getattr(sess, "username", None)!r} queued at position {pos}')
    try:
        await _send_update(writer, cmd_id, pos, False)
    except Exception:
        _drop_from_queue(sess)
        return None
    return pos


async def pump():
    """放行队首（有空位时）+ 刷新剩余排队位置（有放行才需要刷）。

    幂等，可在任意事件回调后调用；发送失败（连接已断）直接出队。
    """
    admitted = 0
    while QUEUE and (MAX_ONLINE <= 0 or _online_count() < MAX_ONLINE):
        sess, writer, cmd_id = QUEUE.pop(0)
        PENDING.add(sess)
        try:
            await _send_update(writer, cmd_id, 0, True)
        except Exception:
            PENDING.discard(sess)       # 连接已断：名额立即回收
        else:
            admitted += 1
    if admitted:
        for i, (sess, writer, cmd_id) in enumerate(QUEUE):
            try:
                await _send_update(writer, cmd_id, i + 1, False)
            except Exception:
                _drop_from_queue(sess)


def on_logged_in(sess):
    """subscribeplayerview 注册进 FLASH_SESSIONS 后调用：
    PENDING 名额转由 FLASH_SESSIONS 计数（总在线数不变）。"""
    if sess in PENDING:
        PENDING.discard(sess)
        return True
    return False


def on_disconnect(sess):
    """连接断开时调用：出队 / 释放 PENDING 名额；返回是否有变动
    （有变动时调用方应跑一次 pump 放行后续排队者）。"""
    changed = _drop_from_queue(sess)
    if sess in PENDING:
        PENDING.discard(sess)
        changed = True
    if changed:
        log(f'[queue] session dropped ({getattr(sess, "username", None)!r}), '
            f'{len(QUEUE)} still queued')
    return changed


async def _pump_loop():
    while True:
        await asyncio.sleep(1)
        try:
            await pump()
        except Exception as e:
            log(f'[queue] pump failed: {type(e).__name__}: {e}')


def start():
    """启动周期 pump 任务（容量不限时无需启动）。"""
    global _pump_task
    if MAX_ONLINE > 0 and _pump_task is None:
        _pump_task = asyncio.ensure_future(_pump_loop())
        log(f'[queue] login queue enabled, capacity={MAX_ONLINE}')
    elif MAX_ONLINE <= 0:
        log('[queue] login queue disabled (HNG_MAX_ONLINE<=0)')
