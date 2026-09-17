# -*- coding: utf-8 -*-
"""zipchunk 压缩推送回归（2026-09-17 P1，纯单元级，无需服务器/DB）。

契约依据（客户端反编译 + 计划 P1 四条决策）：
  1. 大帧（≥ ZIP_THRESHOLD）allow_zip=True → 整帧 gzip 装进 zipchunk
     proto（netsysmessages.zipchunk：required bytes data=1）再走标准
     response 布局；外层标题 "zipchunk"、外层 cmd_id=0（客户端丢弃）
  2. gunzip 后逐字节 == 不压缩路径写出的明文帧（客户端 Chunk.as:94-103：
     uncompressToByteArray → parseData 只解析第一棵 Chunk 树 → 单 zipchunk
     只装 1 个内层完整帧）
  3. 小帧（< 阈值）不压缩（gzip 头/尾开销不划算）
  4. allow_zip=False（combat 连接）永不压缩
  5. ZIP_THRESHOLD=0 = 一键回滚开关（全明文）
  6. 压缩后反而更大的帧回退明文
"""
import asyncio
import gzip
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol import Chunk, pb_int64, pb_parse      # noqa: E402
from messages import _push_kvcs_batch, ZIP_THRESHOLD  # noqa: E402
import messages                                      # noqa: E402

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


def _run(events, **kw):
    w = _FakeWriter()
    asyncio.run(_push_kvcs_batch(w, 4242, events, **kw))
    assert len(w.frames) == 1
    return w.frames[0]


def _big_events(n=40):
    """构造 main 帧远超 ZIP_THRESHOLD 的批量事件（每行 ~120B proto）。"""
    return [('set', b'battlefieldstatus',
             pb_int64(1, 10_000 + i) + pb_int64(2, i + 1) + b'\x01' * 100)
            for i in range(n)]


def _main():
    events = _big_events()

    # 前置：该批量明文帧确实超阈值
    plain = _run(events, allow_zip=False)
    check(f'前置：批量明文帧 ≥ ZIP_THRESHOLD（{len(plain)}B ≥ {ZIP_THRESHOLD}B）',
          len(plain) >= ZIP_THRESHOLD, f'{len(plain)}B')

    # T1 大帧 allow_zip=True → zipchunk 包装：外层标题/cmd_id=0/proto f1=gzip
    z = _run(events, allow_zip=True)
    root, _ = Chunk.parse(z, 0)
    title = root.subs[0]
    proto = title.subs[0].data
    gz = pb_parse(proto)[1]
    check('T1 大帧走 zipchunk（外层标题 zipchunk、cmd_id=0、f1 为 gzip 流）',
          title.as_str() == 'zipchunk'
          and root.as_int() == 0
          and isinstance(gz, (bytes, bytearray))
          and gz[:2] == b'\x1f\x8b',
          f'title={title.as_str()!r} cmd={root.as_int()} '
          f'magic={bytes(gz[:2]).hex()}')

    # T2 gunzip 后逐字节 == 明文帧（单 zipchunk 只装 1 个完整内层帧）
    inner = gzip.decompress(bytes(gz))
    check('T2 gunzip 还原内层完整帧（与明文路径逐字节一致）',
          inner == plain,
          f'{len(inner)}B vs {len(plain)}B')

    # T2b 内层帧本身仍是合法 KVCS（cmd/表行数对得上）
    iroot, _ = Chunk.parse(inner, 0)
    sets = iroot.subs[0].subs[0]
    check('T2b 内层帧 KVCS 结构（cmd=4242、set 40 行 battlefieldstatus）',
          iroot.as_int() == 4242
          and iroot.subs[0].as_str() == 'KeyValueChangeSet'
          and sets.as_str() == 'set'
          and len(sets.subs) == len(events)
          and all(t.as_str() == 'battlefieldstatus'
                  for t in sets.subs))

    # T3 小帧（< 阈值）allow_zip=True → 不压缩（仍是明文 KVCS 帧）
    small = _run([('set', b'armyresource', pb_int64(1, 7))],
                 allow_zip=True)
    sroot, _ = Chunk.parse(small, 0)
    check('T3 小帧不压缩（标题 KeyValueChangeSet、原 cmd 保留）',
          sroot.subs[0].as_str() == 'KeyValueChangeSet'
          and sroot.as_int() == 4242)

    # T4 allow_zip=False + 大帧 → 永不压缩（combat 连接口径）
    forced = _run(events, allow_zip=False)
    froot, _ = Chunk.parse(forced, 0)
    check('T4 allow_zip=False 大帧不压缩', forced == plain
          and froot.subs[0].as_str() == 'KeyValueChangeSet')

    # T5 ZIP_THRESHOLD=0 → 一键回滚：全明文（恢复原值）
    orig_thr = messages.ZIP_THRESHOLD
    try:
        messages.ZIP_THRESHOLD = 0
        off = _run(events, allow_zip=True)
        oroot, _ = Chunk.parse(off, 0)
        check('T5 ZIP_THRESHOLD=0 全明文（一键回滚开关）',
              off == plain and oroot.subs[0].as_str() == 'KeyValueChangeSet')
    finally:
        messages.ZIP_THRESHOLD = orig_thr

    # T6 压缩无收益 → 回退明文：阈值压到 1 迫使单行小帧也尝试 gzip，
    # gzip 头/deflate 开销必然超过帧本身 → 走明文回退分支（确定性）
    orig_thr = messages.ZIP_THRESHOLD
    try:
        messages.ZIP_THRESHOLD = 1
        tiny_plain = _run([('set', b'armyresource', pb_int64(1, 7))],
                          allow_zip=False)
        tiny_out = _run([('set', b'armyresource', pb_int64(1, 7))],
                        allow_zip=True)
        troot, _ = Chunk.parse(tiny_out, 0)
        check('T6 压缩无收益回退明文（gzip 后更大时保持 KVCS 明文帧）',
              tiny_out == tiny_plain
              and troot.subs[0].as_str() == 'KeyValueChangeSet',
              f'{len(tiny_out)}B')
    finally:
        messages.ZIP_THRESHOLD = orig_thr

    print(f'\n==== zipchunk 回归：{PASS}/{PASS + FAIL} '
          f'{"PASS" if FAIL == 0 else "FAIL"} ====')
    return 0 if FAIL == 0 else 1


if __name__ == '__main__':
    sys.exit(_main())
