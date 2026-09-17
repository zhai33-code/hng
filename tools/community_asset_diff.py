# -*- coding: utf-8 -*-
"""community_asset_diff.py —— hagcp 官方数据快照 vs 本服 WarDatabase 数值 diff
（P4 数据校准工具，2026-09-17 计划项；一次性证据收集，只读不落库）。

数据源：
  - 本服：WARDDB_DIR（state.py 自动探测，combat-server 副本优先——与
    wardb.py/HTTP 下发同源，即客户端实际使用的最终版表）；
  - hagcp：packages/hagcp-assets/json（社区抓取的官方快照；与最终版客户端
    WarDatabase 存在时代差，diff 结果只作参考依据，不自动落库）。

字段映射依据：hagcp HnG_States.proto 的表 message 定义（field number 与
我们 pb_parse 的 dict key 同源——客户端 WarDatabase 文件就是这些 proto
的 KVCS 序列化）。

本服值解码注意（protocol.pb_parse 契约）：
  - wiretype 5（float32）返回原始 4 字节 → 工具按字段声明类型 'f' 解包；
  - wiretype 2（string）返回 bytes → 类型 's' decode；
  - 负数 varint 按无符号读出（-1 → 2^64-1）→ 类型 'i' 做 64/32 位补码
    折返后与 hagcp json 的 -1 等值比较。

比对表（计划 P4 四类）：
  commandnodetemplate  speed/attackradius/transportradius/size/level/
                       convertTo/parenttemplateid/victoryWarfundBonus
  armyresourcecategory avgSize/speed/maxDedicatedResources/
                       captureTransferMultiplierWar/Staged（automission
                       加权结算的直接改进依据）
  capital              battlefieldId/defaultFactionTemplateId/victoryPoints
                       （表级聚合：总胜利点数对照战争胜利条件）
  Pricing              type/objectId/costGold/costWarfunds/costCredits（抽查）

用法：
  python tools/community_asset_diff.py [--out docs/xxx.md]
  环境变量 HAGCP_ASSETS 指向 hagcp json 目录（默认 E 盘社区项目路径）。
"""
import argparse
import json
import os
import struct
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))          # server/

from protocol import Chunk, pb_parse                # noqa: E402
from state import WARDDB_DIR                        # noqa: E402

HAGCP_ASSETS = os.environ.get(
    'HAGCP_ASSETS',
    r'E:\devProject\Heroes-and-Generals-community-project'
    r'\packages\hagcp-assets\json')

# 字段配置：(proto f#, hagcp json 字段名, 类型 f=float32/i=int/s=string)
TABLES = [
    {
        'table': 'commandnodetemplate',
        'title': 'commandnodetemplate（AT 模板：速度/交战半径/升级树/战争资金加成）',
        'fields': [(4, 'speed', 'f'), (6, 'attackradius', 'i'),
                   (7, 'transportradius', 'i'), (9, 'size', 'i'),
                   (10, 'level', 'i'), (11, 'convertTo', 'i'),
                   (12, 'parenttemplateid', 'i'),
                   (13, 'victoryWarfundBonus', 'i')],
    },
    {
        'table': 'armyresourcecategory',
        'title': 'armyresourcecategory（资源类别：avgSize/速度/捕获转移系数——'
                 'automission 加权结算的候选依据）',
        'fields': [(4, 'avgSize', 'f'), (6, 'speed', 'f'),
                   (7, 'maxDedicatedResources', 'i'),
                   (8, 'captureTransferMultiplierWar', 'f'),
                   (9, 'captureTransferMultiplierStaged', 'f')],
        'aggregate': [('avgSize', 4, 'avgSize', 'f'),
                      ('captureTransferMultiplierWar', 8,
                       'captureTransferMultiplierWar', 'f'),
                      ('captureTransferMultiplierStaged', 9,
                       'captureTransferMultiplierStaged', 'f')],
    },
    {
        'table': 'capital',
        'title': 'capital（首都：胜利点数——对照战争胜利条件）',
        'fields': [(2, 'battlefieldId', 'i'),
                   (3, 'defaultFactionTemplateId', 'i'),
                   (4, 'victoryPoints', 'i')],
        'aggregate': [('victoryPoints 总和', 4, 'victoryPoints', 'i')],
    },
    {
        'table': 'Pricing',
        'title': 'Pricing（经济数值抽查：三币种价格）',
        'fields': [(2, 'type', 's'), (3, 'objectId', 'i'),
                   (4, 'costGold', 'f'), (5, 'costWarfunds', 'f'),
                   (6, 'costCredits', 'f')],
    },
]

MAX_SAMPLES = 8          # 每字段差异样例上限
EPS = 1e-4               # 数值比较容差（float32 显示精度余量）


def load_ours(table):
    """WarDatabase 表 → {id: {f#: value}}（与 wardb._parse_wardb_table 同款解析）"""
    fpath = os.path.join(WARDDB_DIR, table)
    if not os.path.isfile(fpath):
        return None
    with open(fpath, 'rb') as f:
        root, _ = Chunk.parse(f.read(), 0)
    out = {}
    for t in root.subs[0].subs:
        row = pb_parse(t.subs[0].data)
        out[int(row.get(1, 0) or 0)] = row
    return out


def load_hagcp(table):
    fpath = os.path.join(HAGCP_ASSETS, table + '.json')
    if not os.path.isfile(fpath):
        return None
    with open(fpath, 'r', encoding='utf-8') as f:
        rows = json.load(f)
    return {int(r.get('id', 0) or 0): r for r in rows}


def _ours_val(raw, kind):
    """本服 pb 原始值 → 按声明类型解码后的 python 值。"""
    if raw is None:
        return None
    if kind == 'f':
        if isinstance(raw, (int, float)):
            return float(raw)
        if isinstance(raw, bytes) and len(raw) == 4:
            return struct.unpack('<f', raw)[0]
        return None
    if kind == 'i':
        if isinstance(raw, bytes):          # length-delim 编码的整数（罕见）
            try:
                return int(raw.decode())
            except (UnicodeDecodeError, ValueError):
                return None
        v = int(raw)
        # 负数 varint 按无符号读出：64 位补码折返；int32 域再折 32 位
        if v >= 1 << 63:
            v -= 1 << 64
        return v
    # string
    if isinstance(raw, bytes):
        return raw.decode('utf-8', 'replace')
    return str(raw)


def _theirs_val(raw, kind):
    if raw is None:
        return None
    if kind == 'f':
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    if kind == 'i':
        try:
            return int(raw)
        except (TypeError, ValueError):
            try:
                return int(float(raw))
            except (TypeError, ValueError):
                return None
    return str(raw)


def _i32_wrap(a, b):
    """两侧 int32 域补码等价（attackradius -1 vs 0xFFFFFFFFFFFFFFFF 之类）。"""
    if a is None or b is None:
        return False
    if a == b:
        return True
    if a >= 1 << 31 and b < 0 and (a - (1 << 32)) == b:
        return True
    if b >= 1 << 31 and a < 0 and (b - (1 << 32)) == a:
        return True
    return False


def cmp_val(ours_raw, theirs_raw, kind):
    """按字段类型比较。返回 (相等?, 本服显示值, hagcp 显示值)。"""
    a, b = _ours_val(ours_raw, kind), _theirs_val(theirs_raw, kind)
    if kind == 'f':
        ok = a is not None and b is not None and abs(a - b) <= EPS
        # float32 显示精度：hagcp json 存 double 化的 float32（如
        # 0.6000000238418579 == 0.6f），容差覆盖
        return ok, a, b
    if kind == 'i':
        ok = a is not None and b is not None and (
            a == b or _i32_wrap(a, b))
        return ok, a, b
    return a == b, a, b


def fmt(v):
    if v is None:
        return '∅'
    s = str(v)
    return s if len(s) <= 24 else s[:21] + '...'


def _agg_sum(side_rows, ids, getter):
    tot = 0.0
    n = 0
    for i in ids:
        v = getter(side_rows[i])
        if v is not None:
            tot += float(v)
            n += 1
    return tot, n


def diff_table(cfg):
    """单表 diff → markdown 段。返回 (section, 字段差异总行次)。"""
    name = cfg['table']
    ours, theirs = load_ours(name), load_hagcp(name)
    lines = [f"## {cfg['title']}", '']
    if ours is None or theirs is None:
        lines.append(f"**无法比对**：{'本服' if ours is None else 'hagcp'}"
                     f"侧缺表（ours={'有' if ours else '无'} "
                     f"hagcp={'有' if theirs else '无'}）")
        return '\n'.join(lines + ['']), -1
    common = sorted(set(ours) & set(theirs))
    only_ours = sorted(set(ours) - set(theirs))
    only_theirs = sorted(set(theirs) - set(ours))
    lines.append(f"- 行数：本服 **{len(ours)}** / hagcp **{len(theirs)}** / "
                 f"共同 id **{len(common)}**")
    if only_ours:
        lines.append(f"- 仅本服有 {len(only_ours)} 行："
                     f"`{fmt(only_ours[:MAX_SAMPLES])}"
                     f"{'…' if len(only_ours) > MAX_SAMPLES else ''}`")
    if only_theirs:
        lines.append(f"- 仅 hagcp 有 {len(only_theirs)} 行："
                     f"`{fmt(only_theirs[:MAX_SAMPLES])}"
                     f"{'…' if len(only_theirs) > MAX_SAMPLES else ''}`")
    if cfg.get('aggregate'):
        lines.append('- 表级聚合（共同 id 口径，仅计非空行）：')
        lines.append('  | 字段 | 本服(和/行数) | hagcp(和/行数) | 一致 |')
        lines.append('  |---|---|---|---|')
        for label, fno, jname, kind in cfg['aggregate']:
            a, an = _agg_sum(ours, common,
                             lambda r: _ours_val(r.get(fno), kind))
            b, bn = _agg_sum(theirs, common,
                             lambda r: _theirs_val(r.get(jname), kind))
            ok = abs(a - b) <= EPS * max(1, an)
            lines.append(f'  | {label} | {a:g}/{an} | {b:g}/{bn} | '
                         f'{"✓" if ok else "✗"} |')
    # 字段级差异
    total = 0
    field_diffs = []
    for fno, jname, kind in cfg['fields']:
        diffs = []
        for i in common:
            equal, ov, tv = cmp_val(ours[i].get(fno), theirs[i].get(jname),
                                    kind)
            if not equal:
                diffs.append((i, ov, tv))
        if diffs:
            total += len(diffs)
            field_diffs.append((fno, jname, diffs))
    if not field_diffs:
        lines.append(f'- 字段级：**{len(cfg["fields"])} 个字段全部一致**（共同 '
                     f'{len(common)} 行）')
    else:
        lines.append(f'- 字段级：{len(field_diffs)}/{len(cfg["fields"])} 个字段'
                     f'存在差异（共 {total} 行次）')
        lines.append('  | 字段(f#) | 差异数 | 样例 id: 本服 vs hagcp |')
        lines.append('  |---|---|---|')
        for fno, jname, diffs in field_diffs:
            samples = '；'.join(
                f'{i}: {fmt(o)} vs {fmt(t)}'
                for i, o, t in diffs[:MAX_SAMPLES]) + \
                ('…' if len(diffs) > MAX_SAMPLES else '')
            lines.append(f'  | {jname}(f{fno}) | {len(diffs)} | {samples} |')
    return '\n'.join(lines + ['']), total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', help='报告输出路径（默认只打印 stdout）')
    args = ap.parse_args()
    print(f'本服 WarDatabase: {WARDDB_DIR}')
    print(f'hagcp assets    : {HAGCP_ASSETS}\n')
    head = [
        '# hagcp 官方数据快照 vs 本服 WarDatabase diff 报告', '',
        f'- 本服表目录：`{WARDDB_DIR}`（与 wardb.py/HTTP 下发同源）',
        f'- hagcp 快照：`{HAGCP_ASSETS}`（社区抓取，与最终版客户端存在'
        f'时代差——**结果只作参考依据，不自动落库**）',
        '- 字段映射：hagcp `HnG_States.proto` 表 message 定义', '',
    ]
    sections, grand = [], 0
    for cfg in TABLES:
        sec, total = diff_table(cfg)
        sections.append(sec)
        if total > 0:
            grand += total
        print(sec)
    tail = ['', '---', '',
            f'汇总：四表字段级差异共 **{grand} 行次**（float32 容差 '
            f'{EPS:g}、int32 补码折返口径）；"仅一侧有"的行差异多为快照'
            '时代差（版本演进增删条目），数值差异按'
            ' "采纳/不采纳/需人工判断" 三分类见报告正文结论。', '']
    report = '\n'.join(head + sections + tail)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, 'w', encoding='utf-8') as f:
            f.write(report)
        print(f'\n[written] {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
