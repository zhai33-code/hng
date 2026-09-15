# -*- coding: utf-8 -*-
"""_push_kvcs_batch 批量推送消息结构契约单测（纯单元级，无需服务器/DB）。

契约依据 docs/kvcs-batch-push.md §1-§2（客户端 updateTablesFromChangeset
逐 action × 逐表块 × 只取首行；先应用全部行、再按行应用顺序 FIFO 派发信号）：

  1. 整批必须是**单条** KeyValueChangeSet 消息（跨行原子语义）
  2. delete action 块必须在 set action 块**前**（del 信号先派发——moving
     标志清除先于 updatedWarInstance 重钉，见文档 §2 卡城外根因）
  3. 同表多行必须以**重复表块**表达，每表块恰好 1 个行块（客户端只取
     subChunks[0]，塞 subs 会丢行）
  4. delete 行内容为 8 字节 little-endian int64 行 id（非 proto）
  5. 批内相对顺序保持：dels 内部、sets 内部各自按事件顺序
  6. main chunk data = 4 字节小端 cmd_id

覆盖 _push_kvcs_batch；_push_kvcs / _push_kvcs_delete 单行助手顺带做
结构对拍（单行消息必须等价于批量消息的单 action 单表块形态）。
"""
import asyncio
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 定位客户端根目录（wardb 静态数据加载需要；同 test_booster_table_push）
for _cand in (os.environ.get('HNG_GAME_DIR'),
              os.path.join(os.path.dirname(os.path.dirname(
                  os.path.dirname(os.path.abspath(__file__)))), 'HnG')):
    if _cand and os.path.isdir(os.path.join(_cand, 'WarDatabase')):
        os.environ['HNG_GAME_DIR'] = _cand
        break

from protocol import Chunk, pb_int64          # noqa: E402
from messages import (                        # noqa: E402
    _push_kvcs, _push_kvcs_delete, _push_kvcs_batch,
)

PASS = FAIL = 0


def check(label, ok, detail=''):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f'[PASS] {label}{("  " + detail) if detail else ""}')
    else:
        FAIL += 1
        print(f'[FAIL] {label}{("  " + detail) if detail else ""}')


class _FakeWriter:
    def __init__(self):
        self.frames = []

    def write(self, data):
        self.frames.append(data)

    async def drain(self):
        pass


def _run_batch(events, cmd=4242):
    w = _FakeWriter()
    asyncio.run(_push_kvcs_batch(w, cmd, events))
    return w.frames


def _parse_frame(buf):
    """单帧 → (cmd_id, [(action, [(table, row_bytes), ...]), ...])"""
    root, _ = Chunk.parse(buf, 0)
    cmd = struct.unpack('<i', root.data)[0]
    title = root.subs[0]
    assert title.as_str() == 'KeyValueChangeSet'
    actions = [(act.as_str(),
                [(tbl.as_str(), tbl.subs[0].data) for tbl in act.subs])
               for act in title.subs]
    return cmd, actions


# ------------------------------------------------------------ 用例

def test_dels_first_ordering():
    print('== 1. delete 块必须在 set 块前（与事件输入顺序无关） ==')
    # 事件故意 set/del 交错输入：helper 必须重排为 dels-first
    events = [
        ('set', b'TableA', pb_int64(1, 1)),
        ('del', b'TableB', 100),
        ('set', b'TableC', pb_int64(1, 2)),
        ('del', b'TableD', 200),
    ]
    frames = _run_batch(events)
    check('整批是单条消息', len(frames) == 1, f'frames={len(frames)}')
    cmd, actions = _parse_frame(frames[0])
    check('cmd_id 小端写入 main chunk', cmd == 4242, f'cmd={cmd}')
    names = [a for a, _ in actions]
    check('action 块序 = [delete, set]', names == ['delete', 'set'],
          f'actions={names}')
    dels = dict(actions)['delete']
    sets = dict(actions)['set']
    check('dels 内部保持事件相对顺序',
          [t for t, _ in dels] == ['TableB', 'TableD'],
          f'{[t for t, _ in dels]}')
    check('sets 内部保持事件相对顺序',
          [t for t, _ in sets] == ['TableA', 'TableC'],
          f'{[t for t, _ in sets]}')


def test_repeated_table_chunks():
    print('== 2. 同表多行 = 重复表块，每表块恰好 1 行（客户端只取首行） ==')
    events = [
        ('set', b'armyresource', pb_int64(1, 11)),
        ('set', b'armyresource', pb_int64(1, 12)),
        ('set', b'armyresource', pb_int64(1, 13)),
        ('del', b'armyresource', 21),
        ('del', b'armyresource', 22),
    ]
    cmd, actions = _parse_frame(_run_batch(events)[0])
    sets = dict(actions)['set']
    check('set 块内同表 3 行 → 3 个表块',
          len(sets) == 3 and all(t == 'armyresource' for t, _ in sets),
          f'set tables={[t for t, _ in sets]}')
    dels = dict(actions)['delete']
    check('delete 块内同表 2 行 → 2 个表块',
          len(dels) == 2, f'del tables={[t for t, _ in dels]}')

    # 直接解析底层结构：每表块 subs 数必须恰为 1（>1 客户端会丢行）
    root, _ = Chunk.parse(_run_batch(events)[0], 0)
    for act in root.subs[0].subs:
        for tbl in act.subs:
            check(f'表块 {tbl.as_str()} 恰好 1 个行块',
                  len(tbl.subs) == 1, f'subs={len(tbl.subs)}')


def test_delete_row_encoding():
    print('== 3. delete 行内容 = 8 字节 little-endian int64（非 proto） ==')
    rid = 0x0102030405060708
    events = [('del', b'TableX', rid)]
    cmd, actions = _parse_frame(_run_batch(events)[0])
    row = dict(actions)['delete'][0][1]
    check('delete 行恰 8 字节', len(row) == 8, f'len={len(row)}')
    check('小端 int64 还原行 id',
          struct.unpack('<q', row)[0] == rid,
          f'got={struct.unpack("<q", row)[0]:#x} want={rid:#x}')
    # 大 id（>2^32）不截断
    rid2 = 5_000_000_001
    events = [('del', b'TableX', rid2)]
    row = _parse_frame(_run_batch(events)[0])[1][0][1][0][1]
    check('大 id 不截断', struct.unpack('<q', row)[0] == rid2,
          f'got={struct.unpack("<q", row)[0]}')


def test_client_view_roundtrip():
    print('== 4. 与客户端解析同构的还原：逐 action × 逐表 × 取首行 ==')
    events = [
        ('del', b'transport_commandnode_segment', 555),
        ('del', b'air_transport', 666),
        ('set', b'CommandNodeWarInstance', pb_int64(1, 42)),
        ('set', b'armyresource', pb_int64(1, 42001)),
        ('set', b'armyresource', pb_int64(1, 42002)),
    ]
    cmd, actions = _parse_frame(_run_batch(events)[0])
    # 客户端视角：行应用顺序 = del 块全部行 → set 块全部行
    applied = [(a, t) for a, tbls in actions for t, _ in tbls]
    want = [('delete', 'transport_commandnode_segment'),
            ('delete', 'air_transport'),
            ('set', 'CommandNodeWarInstance'),
            ('set', 'armyresource'),
            ('set', 'armyresource')]
    check('行应用顺序 = del 全部 → set 全部（信号派发顺序）',
          applied == want, f'{applied}')


def test_single_kind_and_empty():
    print('== 5. 边界：全 set / 全 del / 空批 ==')
    cmd, actions = _parse_frame(
        _run_batch([('set', b'T', pb_int64(1, 1)),
                    ('set', b'T', pb_int64(1, 2))])[0])
    check('全 set → 仅 1 个 set 块', [a for a, _ in actions] == ['set'])
    cmd, actions = _parse_frame(
        _run_batch([('del', b'T', 1), ('del', b'T', 2)])[0])
    check('全 del → 仅 1 个 delete 块', [a for a, _ in actions] == ['delete'])
    frames = _run_batch([])
    check('空批仍单帧且无 action 块',
          len(frames) == 1 and _parse_frame(frames[0])[1] == [],
          f'frames={len(frames)}')


def test_single_row_helpers_shape():
    print('== 6. 单行助手与批量形态一致（单 action 单表块） ==')
    w = _FakeWriter()
    asyncio.run(_push_kvcs(w, 77, b'TableS', pb_int64(1, 9)))
    cmd, actions = _parse_frame(w.frames[0])
    check('_push_kvcs → cmd + [set] 单表块',
          cmd == 77 and [a for a, _ in actions] == ['set']
          and len(actions[0][1]) == 1
          and actions[0][1][0][0] == 'TableS')
    w = _FakeWriter()
    asyncio.run(_push_kvcs_delete(w, 78, b'TableD', 0x1122334455667788))
    cmd, actions = _parse_frame(w.frames[0])
    row = actions[0][1][0][1]
    check('_push_kvcs_delete → cmd + [delete] 8 字节 LE id',
          cmd == 78 and [a for a, _ in actions] == ['delete']
          and struct.unpack('<q', row)[0] == 0x1122334455667788)


def test_preserve_order_mode():
    print('== 7. preserve_order=True：只合并相邻同 kind，交错序列保序 ==')

    async def _run(events, cmd=4242):
        w = _FakeWriter()
        await _push_kvcs_batch(w, cmd, events, preserve_order=True)
        return w.frames

    # 到达推序原型：del segment → set instance → del destination
    # （客户端 AT 列表 status 重算时机依赖此交错序，docs §2.1）
    events = [
        ('del', b'transport_commandnode_segment', 555),
        ('set', b'CommandNodeWarInstance', pb_int64(1, 42)),
        ('del', b'transport_commandnode_destination', 555),
    ]
    frames = asyncio.run(_run(events))
    check('保序批仍是单条消息', len(frames) == 1, f'frames={len(frames)}')
    cmd, actions = _parse_frame(frames[0])
    names = [a for a, _ in actions]
    check('action 块序 = [delete, set, delete]（不被重排）',
          names == ['delete', 'set', 'delete'], f'actions={names}')
    applied = [(a, t) for a, tbls in actions for t, _ in tbls]
    want = [('delete', 'transport_commandnode_segment'),
            ('set', 'CommandNodeWarInstance'),
            ('delete', 'transport_commandnode_destination')]
    check('行应用/派发顺序 = 事件顺序', applied == want, f'{applied}')

    # 相邻同 kind 仍合并为一块
    events2 = [
        ('del', b'TableB', 1),
        ('del', b'TableD', 2),
        ('set', b'TableA', pb_int64(1, 1)),
        ('set', b'TableC', pb_int64(1, 2)),
    ]
    cmd, actions = _parse_frame(asyncio.run(_run(events2))[0])
    check('相邻同 kind 合并：块序 [delete, set] 且各含 2 表块',
          [a for a, _ in actions] == ['delete', 'set']
          and len(actions[0][1]) == 2 and len(actions[1][1]) == 2,
          f'{[(a, len(t)) for a, t in actions]}')

    # 默认模式不受影响（dels-first 重排保持）
    events3 = [
        ('set', b'TableA', pb_int64(1, 1)),
        ('del', b'TableB', 100),
    ]
    cmd, actions = _parse_frame(_run_batch(events3)[0])
    check('默认模式仍 dels-first',
          [a for a, _ in actions] == ['delete', 'set'],
          f'{[a for a, _ in actions]}')


def main():
    test_dels_first_ordering()
    test_repeated_table_chunks()
    test_delete_row_encoding()
    test_client_view_roundtrip()
    test_single_kind_and_empty()
    test_single_row_helpers_shape()
    test_preserve_order_mode()
    print(f'\n结果: PASS={PASS} FAIL={FAIL}')
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
