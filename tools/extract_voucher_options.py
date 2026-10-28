# -*- coding: utf-8 -*-
"""生成兑换码道具下拉选项（前端静态数据）。

用法（在 hng-re/ 下）：
    GAME_DIR=../HnG .venv/bin/python tools/extract_voucher_options.py

产出 web/frontend/src/admin/data/voucherOptions.ts：
WeaponVariantTemplate(武器皮肤 438) / VehicleVariantTemplate(载具涂装 564，
拼基础载具名) / UniformPartTemplate(头部件 149) / RibbonBoosterTemplate(勋带
加成 310，拼勋带名+倍率+时长) / ShopMembershipItems(会员 19，days<=0 剔除)。

前端直接 import（纯静态、无接口），数据升级时重跑本脚本 + pnpm build。
服务端发货时长用 wardb.py BOOSTER_MINUTES_BY_ID（运行时从 WarDatabase 读）。
"""
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'web', 'server'))

from wardb import _parse_wardb_table  # noqa: E402
from templates_data import RIBBONS, VEHICLES  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), '..', 'web', 'frontend',
                   'src', 'admin', 'data', 'voucherOptions.ts')


def ts_str(s):
    return "'" + s.replace('\\', '\\\\').replace("'", "\\'") + "'"


def opts_block(d):
    # el-select-v2 选项默认字段为 { label, value }，缺 value 无法选中
    return '[\n' + ',\n'.join(
        f"  {{ value: {k}, label: {ts_str(v)} }}" for k, v in sorted(d.items())) + ',\n]'


def main():
    # 武器皮肤：WeaponVariantTemplate { id=1, name=2, weaponTemplateId=3 }
    wv = {int(r[1]): f"{r[2].decode('utf-8', 'replace')} (#{int(r[1])})"
          for r in _parse_wardb_table('WeaponVariantTemplate') if r.get(1)}

    # 头部件：UniformPartTemplate { id=1, name=3 }
    up = {int(r[1]): f"{r[3].decode('utf-8', 'replace')} (#{int(r[1])})"
          for r in _parse_wardb_table('UniformPartTemplate') if r.get(1)}

    # 勋带加成：RibbonBoosterTemplate { id=1, ribbonId=2, minutes=3, multiplier=4(fixed32) }
    rb = {}
    for r in _parse_wardb_table('RibbonBoosterTemplate'):
        bid, rid = int(r[1]), int(r[2])
        hours = int(r.get(3, 0)) / 60
        mult = struct.unpack('<f', r[4])[0] if isinstance(r.get(4), bytes) else 1.0
        ribbon = RIBBONS.get(rid, f'勋带 #{rid}')
        rb[bid] = f'{ribbon} ×{mult:g}（{hours:g} 小时）(#{bid})'

    # 会员商品：ShopMembershipItems { id=1, identifier=2, durationDays=3 }
    ms = {}
    for r in _parse_wardb_table('ShopMembershipItems'):
        mid, days = int(r[1]), int(r.get(3, 0))
        if days <= 0:      # durationDays=0（赠送位）发了也立即过期，不提供
            continue
        name = r[2].decode('utf-8', 'replace')
        if 'not found' in name:   # 本地化键缺失的条目
            name = '老兵会员'
        ms[mid] = f'{name} {days} 天 (#{mid})'

    # 载具变体：VehicleVariantTemplate { id=1, name=2, parent=3 } → 拼基础载具名
    vv = {}
    for r in _parse_wardb_table('VehicleVariantTemplate'):
        vid = int(r.get(1, 0))
        if not vid:
            continue
        name = r.get(2, b'').decode('utf-8', 'replace')
        parent = int(r.get(3, 0) or 0)
        base = VEHICLES.get(parent)
        vv[vid] = (f'{name} — {base} (#{vid})' if base else f'{name} (#{vid})')

    ts = '''// eslint-disable
// 兑换码道具下拉选项：自 HnG 客户端 WarDatabase 提取固化（2026-09-02）。
// 重新提取：cd hng-re && GAME_DIR=../HnG .venv/bin/python tools/extract_voucher_options.py
// 服务端发货时长按模板（wardb.py BOOSTER_MINUTES_BY_ID），与本表独立维护。

export interface VoucherOption { value: number; label: string }

export const VOUCHER_OPTIONS: Record<string, VoucherOption[]> = {
  weaponvarianttemplate: %s,
  vehiclevarianttemplate: %s,
  uniformparttemplate: %s,
  ribbonboostertemplate: %s,
  shopmembershipitems: %s,
}
''' % (opts_block(wv), opts_block(vv), opts_block(up), opts_block(rb), opts_block(ms))

    with open(OUT, 'w', encoding='utf-8') as f:
        f.write(ts)
    print(f'written {OUT}: wv={len(wv)} vv={len(vv)} up={len(up)} '
          f'rb={len(rb)} ms={len(ms)}')


if __name__ == '__main__':
    main()
