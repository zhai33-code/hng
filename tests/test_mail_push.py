# -*- coding: utf-8 -*-
"""2026-09-02 管理端发邮件玩家收不到修复的回归测试（纯单元级，无服务器/DB）。

事故：管理端 mail_svc 写 mail_messages 成功，但游戏服从未推送
MessageRecipient KVCS 行——客户端邮箱 UI（MessagesWindowMediator）只对
已有 MessageRecipient 行的 messageId 发 GetMailMessagesRequest 拉正文，
主菜单未读角标也来自 MessageRecipientTable → 邮箱恒空、角标恒 0。

修复点：
  1. player_store.list_mail_recipient_rows（id/is_read/deleted 行源）
  2. social.push_mail_recipient_rows / push_new_mail_rows（登录全量 +
     会话内差量，sess.mail_pushed_ids 防重复 ADD）
  3. views.py subscribeplayerview 登录时推送
  4. realtest_server.main 启动 15s 周期轮询，在线玩家实时收新邮件
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _cand in (os.environ.get('HNG_GAME_DIR'),
              os.path.join(os.path.dirname(os.path.dirname(
                  os.path.dirname(os.path.abspath(__file__)))), 'HNG')):
    if _cand and os.path.isdir(os.path.join(_cand, 'WarDatabase')):
        os.environ['HNG_GAME_DIR'] = _cand
        break

from protocol import Chunk, pb_parse          # noqa: E402
from handlers import social                   # noqa: E402

PASS = FAIL = 0


def _s(v):
    return v.decode() if isinstance(v, (bytes, bytearray)) else v


def check(label, ok, detail=''):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f'[PASS] {label}{("  " + detail) if detail else ""}')
    else:
        FAIL += 1
        print(f'[FAIL] {label}{("  " + detail) if detail else ""}')


# ------------------------------------------------------------ 测试桩

class _FakeWriter:
    def __init__(self):
        self.frames = []

    def write(self, data):
        self.frames.append(data)

    async def drain(self):
        pass


class _FakeStore:
    def __init__(self, rows):
        self._rows = rows

    async def list_mail_recipient_rows(self, pid):
        assert pid == 42
        return self._rows

    async def get_player(self, name):
        return 42


class _FakeSess:
    playerview_cmd = 4242
    username = 'tester'

    def __init__(self):
        self.player_id = 42

    async def ensure_player(self):
        return self.player_id


def _parse_kvcs_frames(frames):
    """写入的 KVCS 帧 → [(tableName, pb_fields), ...]"""
    out = []
    for buf in frames:
        root, _ = Chunk.parse(buf, 0)
        title = root.subs[0]
        assert title.as_str() == 'KeyValueChangeSet'
        action = title.subs[0]
        assert action.as_str() == 'set'
        for table in action.subs:
            out.append((table.as_str(), pb_parse(table.subs[0].data)))
    return out


def _run_push(sess, rows):
    writer = _FakeWriter()
    social.STORE = _FakeStore(rows)
    asyncio.run(social.push_new_mail_rows(sess, writer))
    return _parse_kvcs_frames(writer.frames)


# ------------------------------------------------------------ 用例

class _FakeFlagStore:
    """set_mail_flag / get_mail_rows 桩：标记后返回更新行。"""
    updated = {}

    async def set_mail_flag(self, pid, ids, column, value):
        for i in ids:
            row = self.updated.setdefault(i, {'id': i, 'is_read': False,
                                              'deleted': False})
            row[column] = value
        return len(ids)

    async def get_mail_rows(self, pid, ids):
        return [self.updated[i] for i in ids if i in self.updated]


def _parse_kvcs_frames_tolerant(frames):
    """过滤出 KVCS 帧（容忍混入的 response 帧）。"""
    out = []
    for buf in frames:
        try:
            root, _ = Chunk.parse(buf, 0)
            title = root.subs[0]
            if title.as_str() != 'KeyValueChangeSet':
                continue
            action = title.subs[0]
            assert action.as_str() == 'set'
            for table in action.subs:
                out.append((table.as_str(), pb_parse(table.subs[0].data)))
        except Exception:
            continue
    return out


def test_flag_pushback():
    print('== 5. 已读/删除标记回推（未读角标实时清零） ==')
    from protocol import pb_bool, pb_int64
    store = _FakeFlagStore()
    social.STORE = store

    # 已读标记：客户端 markRead 发 MessageRecipient.id（= mail_messages.id）
    writer = _FakeWriter()
    proto = pb_int64(1, 5) + pb_int64(1, 6) + pb_bool(2, True)
    asyncio.run(social.handle_set_message_recipients_read(
        _FakeSess(), writer, 99, 'setmessagerecipientsreadrequest', proto))
    frames = _parse_kvcs_frames_tolerant(writer.frames)
    check('已读标记回推 2 行 MessageRecipient',
          [t for t, _ in frames] == ['MessageRecipient'] * 2,
          f'tables={[t for t, _ in frames]}')
    check('回推行 isRead=True（角标递减依据）',
          all(bool(f.get(4)) is True for _, f in frames),
          f'f4s={[f.get(4) for _, f in frames]}')

    # 删除标记：客户端 onMessageRecipientUpdated 按 isDeleted 移除线程
    writer = _FakeWriter()
    proto = pb_int64(1, 5) + pb_bool(2, True)
    asyncio.run(social.handle_set_message_recipients_deleted(
        _FakeSess(), writer, 99, 'setmessagerecipientsdeletedrequest', proto))
    frames = _parse_kvcs_frames_tolerant(writer.frames)
    check('删除标记回推 1 行 MessageRecipient',
          [t for t, _ in frames] == ['MessageRecipient'],
          f'tables={[t for t, _ in frames]}')
    check('回推行 isDeleted=True（列表移除依据）',
          bool(frames[0][1].get(5)) is True, f'f5={frames[0][1].get(5)}')


def test_row_schema():
    print('== 1. MessageRecipient 行 schema（字段 1-5） ==')
    rows = [{'id': 5, 'is_read': False, 'deleted': False},
            {'id': 6, 'is_read': True, 'deleted': True}]
    frames = _run_push(_FakeSess(), rows)
    check('行推到 MessageRecipient 表（xmlTag）',
          [t for t, _ in frames] == ['MessageRecipient', 'MessageRecipient'],
          f'tables={[t for t, _ in frames]}')
    f = frames[0][1]
    check('f1 id = mail_messages.id', f.get(1) == 5, f'f1={f.get(1)}')
    check('f2 playerId 正确', f.get(2) == 42, f'f2={f.get(2)}')
    check('f3 messageId = 行 id（同 mail_messages.id）', f.get(3) == 5,
          f'f3={f.get(3)}')
    check('f4 isRead', bool(f.get(4)) is False, f'f4={f.get(4)}')
    check('f5 isDeleted', bool(f.get(5)) is False, f'f5={f.get(5)}')
    f2 = frames[1][1]
    check('已读行 f4=True', bool(f2.get(4)) is True, f'f4={f2.get(4)}')
    check('已删行 f5=True', bool(f2.get(5)) is True, f'f5={f2.get(5)}')


def test_incremental_push():
    print('== 2. 差量推送（重复推送会重复 +1 未读角标） ==')
    sess = _FakeSess()
    rows = [{'id': 5, 'is_read': False, 'deleted': False}]
    frames = _run_push(sess, rows)
    check('首次全量推 1 行', len(frames) == 1)
    # 无新邮件：不再发帧
    frames = _run_push(sess, rows)
    check('无新邮件不重复推', frames == [])
    # 新邮件到库（管理端写入）：只推新行
    rows.append({'id': 7, 'is_read': False, 'deleted': False})
    frames = _run_push(sess, rows)
    check('增量只推新行 id=7',
          len(frames) == 1 and frames[0][1].get(1) == 7,
          f'frames={[(t, f.get(1)) for t, f in frames]}')
    # 空收件箱
    frames = _run_push(_FakeSess(), [])
    check('空收件箱不推帧', frames == [])


def test_playerview_gating():
    print('== 3. 未订阅 playerview 不推 ==')
    sess = _FakeSess()
    sess.playerview_cmd = None
    frames = _run_push(sess, [{'id': 5, 'is_read': False, 'deleted': False}])
    check('playerview_cmd=None 直接返回', frames == [])


def test_wiring():
    print('== 4. 登录推送 / 周期轮询接线 ==')
    import inspect
    import handlers.views as views
    src = inspect.getsource(views.handle_subscribe)
    check('views.handle_subscribe 登录时调用 push_new_mail_rows',
          'push_new_mail_rows' in src)
    import realtest_server
    src = inspect.getsource(realtest_server.main)
    check('realtest_server.main 启动 mail listener（LISTEN）',
          'start_mail_listener' in src)
    check('游戏服 LISTEN hng_new_mail（事件驱动）',
          social.MAIL_NOTIFY_CHANNEL == 'hng_new_mail' and
          'add_listener' in inspect.getsource(social._mail_listener_loop))
    src = inspect.getsource(social._on_mail_notify)
    check('NOTIFY 回调按 payload 收件人定向推 / 空兜底全在线会话',
          'push_mail_to_player' in src and 'FLASH_SESSIONS' in src)


def main():
    test_row_schema()
    test_incremental_push()
    test_playerview_gating()
    test_flag_pushback()
    test_wiring()
    print(f'\n结果: PASS={PASS} FAIL={FAIL}')
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
