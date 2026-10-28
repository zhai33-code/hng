# -*- coding: utf-8 -*-
"""RequestReadCharacterStats 官服对齐语义（2026-09-06）：
1. 空 proto（不带 f1 characterids）→ 空 body（官服行为，修正采集器掩盖 bug）
2. 请求 id ∩ 本人角色 → 只回交集，按请求顺序
3. 请求别人的角色 id → 交集为空 → 空 body（防越权）
回归保护：改动前实现忽略请求参数全量回发。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol import Chunk, pb_varint
from handlers import characters

PID = 4242
MY_CHARS = [101, 102, 103]


class FakeWriter:
    def __init__(self):
        self.data = b''

    def write(self, data):
        self.data += data

    async def drain(self):
        pass


class FakeSess:
    async def ensure_player(self):
        return PID


class FakeStore:
    async def list_characters(self, pid):
        return [{'id': cid, 'name': 'c%d' % cid} for cid in MY_CHARS]

    async def list_weapons(self, pid):
        return []

    async def get_ribbon_xp(self, pid, cid):
        return {}

    async def get_combat_xp(self, pid, cid):
        return 0

    async def get_stats(self, cid):
        return {}


def _entry_char_ids(body):
    """解析 ResponseReadCharacterStats body → entry 的 f1 characterId 列表。"""
    ids = []
    i = 0
    while i < len(body):
        tag, i = _varint(body, i)
        fn, wt = tag >> 3, tag & 7
        ln, i = _varint(body, i) if wt == 2 else (0, i)
        if fn == 1 and wt == 2:
            sub = body[i:i + ln]
            j = 0
            while j < len(sub):
                t, j = _varint(sub, j)
                if (t >> 3) == 1:
                    v, j = _varint(sub, j)
                    ids.append(v)
                else:
                    break
        i += ln
    return ids


def _varint(buf, i):
    v = shift = 0
    while True:
        b = buf[i]
        i += 1
        v |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            return v, i


async def _request(proto):
    w = FakeWriter()
    await characters.handle_requestreadcharacterstats(
        FakeSess(), w, 21, 'RequestReadCharacterStats', proto)
    main, _ = Chunk.parse(w.data, 0)
    return main.subs[0].subs[0].data


def test_filter_semantics():
    old_store = characters.STORE
    characters.STORE = FakeStore()
    try:
        async def run():
            # 1. 空 proto → 空 body
            body = await _request(b'')
            assert body == b'', 'empty proto must yield empty body'

            # 2. 只带 f2（无 f1）→ 空 body
            body = await _request(pb_varint(2, 0))
            assert body == b'', 'no characterids must yield empty body'

            # 3. 本人角色子集（乱序）→ 按请求顺序回交集
            body = await _request(pb_varint(1, 103) + pb_varint(1, 101))
            assert _entry_char_ids(body) == [103, 101], 'own chars must follow request order'

            # 4. 混入他人角色 id → 只回本人的部分
            body = await _request(pb_varint(1, 999) + pb_varint(1, 102))
            assert _entry_char_ids(body) == [102], 'foreign ids must be filtered out'

            # 5. 全部是他人的 → 空 body
            body = await _request(pb_varint(1, 777) + pb_varint(1, 888))
            assert body == b'', 'all-foreign request must yield empty body'

            print('[PASS] all 5 filter semantics')
        asyncio.run(run())
    finally:
        characters.STORE = old_store


if __name__ == '__main__':
    test_filter_semantics()
    print('OK')
