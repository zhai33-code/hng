"""Round-trip test: Chunk codec vs real WarDatabase cache files (ground truth)."""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 2026-08-21 归档至 tests/
from hngserver import Chunk
from state import WARDDB_DIR

base = WARDDB_DIR
ok = fail = 0
for fn in sorted(os.listdir(base)):
    p = os.path.join(base, fn)
    raw = open(p, 'rb').read()
    c, nxt = Chunk.parse(raw, 0)
    if c is None:
        print(f'{fn:<40} PARSE-FAIL')
        fail += 1
        continue
    out = c.write(b'')
    match = out == raw
    title = c.subs[0].as_str() if c.subs else '?'
    n_frames = 0
    if c.subs and len(c.subs) > 1:
        body = c.subs[1]
        n_frames = len(body.subs)
    if match:
        ok += 1
    else:
        fail += 1
        print(f'{fn:<40} title={title:<20} frames={n_frames:<4} reserialize {"OK" if match else "MISMATCH"} ({len(out)} vs {len(raw)})')
print(f'\nround-trip ok={ok} fail={fail}')

# show structure of one file
raw = open(os.path.join(base, 'factiontemplate'), 'rb').read()
c, _ = Chunk.parse(raw, 0)
print('\nfactiontemplate structure:')
print('  msg title:', c.subs[0].as_str(), '| direct subs:', len(c.subs))
for frame in c.subs[1:3]:
    op = frame.subs[0].as_str()
    kv = frame.subs[1]
    key = kv.subs[0].as_str()
    payload = kv.subs[1].subs[0].data if kv.subs[1].subs else b''
    print(f'  [{op}] {key} -> {payload.hex()}')
