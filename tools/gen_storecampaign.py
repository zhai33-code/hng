"""临时：生成 WarDatabase/StoreCampaign 文件（与 StoreBundle 完全同构的 wardb 格式）"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 2026-08-21 归档至 tools/
import protocol as m
from messages import _msg_store_campaign
from wardb import _load_wardb_kvcs

pairs = [
    m.Chunk(b'StoreCampaign', [m.Chunk(_msg_store_campaign(1, 1))]),  # eDiscount
    m.Chunk(b'StoreCampaign', [m.Chunk(_msg_store_campaign(2, 2))]),  # eCampaign
]
action = m.Chunk(b'set', pairs)
title = m.Chunk(b'KeyValueChangeSet', [action])
data = title.write(b'')

path = r'd:\HnG-test\WarDatabase\StoreCampaign'
with open(path, 'wb') as f:
    f.write(data)

# 验证 round-trip：_load_wardb_kvcs 能解析
ch = _load_wardb_kvcs('StoreCampaign')
print('written:', len(data), 'B ->', path)
print('parse:', ch.data, '| set->pairs:', len(ch.subs[0].subs))
for p in ch.subs[0].subs:
    print('   ', p.data, m.pb_parse(p.subs[0].data))
