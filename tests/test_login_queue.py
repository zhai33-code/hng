# -*- coding: utf-8 -*-
"""登录排队（login_queue）单元测试——无需起服务器 / 连 DB。

覆盖：
- 容量未满立即放行 / 满员入队（FIFO，位置从 1 开始）
- 在线数 = FLASH_SESSIONS + PENDING（防登录中途名额超发）
- 断线释放名额 → pump 放行队首并刷新剩余位置
- 排队中断线：出队、后续位置前移
- 登录完成（subscribeplayerview）：PENDING 名额转 FLASH_SESSIONS 计数
- 推送帧格式：cmdId / originalId / mayProceed / result 契约
  （客户端 ServiceHandler 只认 originalId==cmdId 的帧）
- 推送目标连接已断：条目出队、名额回收
- HNG_MAX_ONLINE<=0：不限容量，行为与旧版一致
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol import Chunk, pb_parse

PASS = FAIL = 0


def check(name, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  PASS  {name}')
    else:
        FAIL += 1
        print(f'  FAIL  {name}  {detail}')


class FakeWriter:
    def __init__(self, broken=False, fail_after=None):
        self.sent = b''
        self.broken = broken
        self.fail_after = fail_after   # 前 N 次成功，之后模拟断开
        self._n = 0

    def write(self, data):
        if self.fail_after is not None:
            self._n += 1
            if self._n > self.fail_after:
                raise ConnectionResetError('broken pipe')
        if self.broken:
            raise ConnectionResetError('broken pipe')
        self.sent += data

    async def drain(self):
        if self.broken:
            raise ConnectionResetError('broken pipe')
        if self.fail_after is not None and self._n > self.fail_after:
            raise ConnectionResetError('broken pipe')


class FakeSess:
    def __init__(self, name):
        self.username = name


def parse_updates(writer):
    """解析 writer 发出的全部帧 → [(cmd_id, title, {proto 字段})]。"""
    out = []
    buf = writer.sent
    off = 0
    while True:
        chunk, nxt = Chunk.parse(buf, off)
        if chunk is None:
            break
        off = nxt
        title = chunk.subs[0].as_str()
        proto = chunk.subs[0].subs[0].data if chunk.subs[0].subs else b''
        out.append((chunk.as_int(), title, pb_parse(proto)))
    return out


async def main():
    import login_queue
    from state import FLASH_SESSIONS

    def reset(cap):
        login_queue.QUEUE.clear()
        login_queue.PENDING.clear()
        FLASH_SESSIONS.clear()
        login_queue.MAX_ONLINE = cap

    print('== 1. 容量未满：立即放行（行为同旧版）==')
    reset(10)
    w1 = FakeWriter()
    pos = await login_queue.enter(FakeSess('u1'), w1, 101)
    upd = parse_updates(w1)
    check('立即放行返回 None', pos is None)
    check('回 mayProceed=true 帧', upd and upd[0][2][2] == 1,
          str(upd))
    check('cmdId/originalId 回显 101', upd[0][0] == 101 and upd[0][2][3] == 101)
    check('position=0 result=ok', upd[0][2][1] == 0 and upd[0][2][4] == 1)
    check('放行会话进入 PENDING', len(login_queue.PENDING) == 1)

    print('== 2. 登录完成：PENDING 名额转 FLASH_SESSIONS 计数 ==')
    s1 = list(login_queue.PENDING)[0]
    FLASH_SESSIONS['u1'] = s1
    login_queue.on_logged_in(s1)
    check('on_logged_in 释放 PENDING', len(login_queue.PENDING) == 0)
    check('在线数仍按 FLASH_SESSIONS 计', login_queue._online_count() == 1)

    print('== 3. 满员入队（FIFO 位置）==')
    # 占满剩余容量（10-1=9）：再放 9 个
    sessions = []
    for i in range(2, 11):
        s = FakeSess(f'u{i}')
        w = FakeWriter()
        sessions.append((s, w))
        await login_queue.enter(s, w, 100 + i)
        FLASH_SESSIONS[f'u{i}'] = s
        login_queue.on_logged_in(s)
    check('在线数到容量 10', login_queue._online_count() == 10,
          f'count={login_queue._online_count()}')
    q1, qw1 = FakeSess('q1'), FakeWriter()
    q2, qw2 = FakeSess('q2'), FakeWriter()
    q3, qw3 = FakeSess('q3'), FakeWriter()
    check('q1 排队位置 1', await login_queue.enter(q1, qw1, 201) == 1)
    check('q2 排队位置 2', await login_queue.enter(q2, qw2, 202) == 2)
    check('q3 排队位置 3', await login_queue.enter(q3, qw3, 203) == 3)
    u = parse_updates(qw3)
    check('排队帧 mayProceed=false', u and u[0][2][2] == 0, str(u))
    check('排队帧 originalId=203', u[0][2][3] == 203)
    check('在线数不随排队虚增', login_queue._online_count() == 10)

    print('== 4. 在线玩家下线：释放名额 → pump 放行队首 ==')
    del FLASH_SESSIONS['u2']
    login_queue.on_disconnect(sessions[0][0])
    await login_queue.pump()
    u1 = parse_updates(qw1)
    check('队首 q1 被放行', u1[-1][2][2] == 1 and u1[-1][2][3] == 201,
          str(u1[-1]))
    check('q1 转入 PENDING', q1 in login_queue.PENDING)
    u2, u3 = parse_updates(qw2), parse_updates(qw3)
    check('q2 收到位置刷新=1', u2[-1][2][1] == 1 and u2[-1][2][2] == 0,
          str(u2[-1]))
    check('q3 收到位置刷新=2', u3[-1][2][1] == 2 and u3[-1][2][2] == 0,
          str(u3[-1]))

    print('== 5. 放行会话断线（未注册）：名额回收 ==')
    login_queue.on_disconnect(q1)
    check('q1 出 PENDING', q1 not in login_queue.PENDING)
    await login_queue.pump()
    u2 = parse_updates(qw2)
    check('q2 顶上被放行', u2[-1][2][2] == 1 and u2[-1][2][3] == 202,
          str(u2[-1]))

    print('== 6. 排队中会话断线：出队 + 后续前移 ==')
    login_queue.on_disconnect(q3)
    check('q3 出队', len(login_queue.QUEUE) == 0)

    print('== 7. 推送目标连接已断：条目丢弃、名额不泄漏 ==')
    # 7a. StartLogin 时连接就断了：不入队、不占名额
    q_dead, qw_dead = FakeSess('q_dead'), FakeWriter(broken=True)
    pos = await login_queue.enter(q_dead, qw_dead, 204)
    check('断连会话不入队', pos is None and len(login_queue.QUEUE) == 0)
    check('断连会话不占 PENDING', q_dead not in login_queue.PENDING)
    # 7b. 排队期间断开：放行推送失败 → 条目丢弃、名额回收
    q4, qw4 = FakeSess('q4'), FakeWriter(fail_after=1)  # 入队帧成功，放行帧失败
    await login_queue.enter(q4, qw4, 204)
    check('q4 入队位置 1', len(login_queue.QUEUE) == 1)
    login_queue.on_disconnect(sessions[1][0])   # u3 下线释放名额
    del FLASH_SESSIONS['u3']
    await login_queue.pump()                    # 放行 q4 → 写失败 → 回收
    check('q4 写失败后被丢弃', len(login_queue.QUEUE) == 0)
    check('q4 名额已回收', q4 not in login_queue.PENDING)

    print('== 8. 同一连接重复 StartLogin：更新 cmdId 不重复入队 ==')
    q5, qw5 = FakeSess('q5'), FakeWriter()
    login_queue.MAX_ONLINE = login_queue._online_count()  # 先占满
    pos_first = await login_queue.enter(q5, qw5, 205)
    check('q5 排队位置 1', pos_first == 1)
    pos = await login_queue.enter(q5, qw5, 206)
    check('重复 StartLogin 返回原位置', pos == 1, f'pos={pos}')
    u = parse_updates(qw5)
    check('重复帧带新 originalId=206', u[-1][2][3] == 206, str(u[-1]))
    check('队列不重复', len(login_queue.QUEUE) == 1)

    print('== 9. HNG_MAX_ONLINE<=0：不限容量，永不排队 ==')
    reset(0)
    ws = []
    ok = True
    for i in range(15):
        w = FakeWriter()
        ws.append(w)
        p = await login_queue.enter(FakeSess(f'n{i}'), w, 300 + i)
        if p is not None:
            check(f'不限容量第 {i} 个不该排队', False)
            ok = False
            break
    if ok:
        check('15 个连接全部立即放行', all(
            parse_updates(w)[0][2][2] == 1 for w in ws))

    print(f'\n{"=" * 40}\nRESULT: PASS={PASS} FAIL={FAIL}')
    sys.exit(1 if FAIL else 0)


if __name__ == '__main__':
    asyncio.run(main())
