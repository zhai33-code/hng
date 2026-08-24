import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 2026-08-21 归档至 tools/
from hngserver import Chunk

raw = open(r'd:\HnG-test\WarDatabase\factiontemplate', 'rb').read()
c, _ = Chunk.parse(raw, 0)


def dump(ch, depth=0):
    d = ch.data
    s = d.decode('utf-8', 'replace') if all(32 <= b < 127 for b in d[:60]) else f'<{len(d)}B bin>'
    print('  ' * depth + f'[{len(d)}B] {s[:60]}' + (f'  subs={len(ch.subs)}' if ch.subs else ''))
    for s2 in ch.subs:
        dump(s2, depth + 1)


dump(c)
