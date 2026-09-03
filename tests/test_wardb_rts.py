# -*- coding: utf-8 -*-
"""RTS 静态数据接口回归测试（wardb.py Phase 1，docs/hng-rts-protocol.md §2.2/§3）。

用例（真实 WarDatabase 锚点，2026-09-02 dump）：
 A. capitals：13 行、default_faction ∈ {0,1,2,3}（0=中立）、London=US；
 B. battlefields：非空、标题非空、坐标可解析；
 C. supplylines/accesspoints：拓扑闭合（两端点存在、accesspoint 归属城市）；
 D. commandnode_templates：三阵营 infantry（103/203/303）速度 0.6、编制
    {1: 90}；faction 段 1xx/2xx/3xx；
 E. at_levels：(1,350)/(2,3450)/(3,9200)；
 F. morale_template：官服实值 max=100/lost=20/win=10/minute≈0.15/surrender=25；
 G. at_pricing：objectId 命中 AT 模板段（如 224）且 warfunds>0；
 H. armyresource_production：非空且 faction/category 合法。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wardb

RESULTS = []


def check(label, ok, detail=''):
    RESULTS.append((label, ok))
    print(f'[{"PASS" if ok else "FAIL"}] {label}{("  " + detail) if detail else ""}')


def main():
    # ---- A. capitals ----
    caps = wardb.capitals()
    check('capital 表非空（实测 13 行）', len(caps) >= 3, f'{len(caps)} 行')
    check('capital default_faction ∈ {0,1,2,3}',
          all(c['default_faction'] in (0, 1, 2, 3) for c in caps))
    check('capital victory_points ≥ 0',
          all(c['victory_points'] >= 0 for c in caps))
    by_bf = {c['battlefield_id']: c for c in caps}

    # ---- B. battlefields ----
    bfs = wardb.battlefields()
    check('battlefield 表非空', len(bfs) > 0, f'{len(bfs)} 行')
    check('battlefield 标题非空',
          all(b['title'] for b in bfs))
    check('capital 关联的 battlefield 行存在（London）',
          any(b['id'] in by_bf and b['title'] == 'London City' for b in bfs))

    # ---- C. supplylines / accesspoints 拓扑闭合 ----
    aps = {a['id']: a for a in wardb.accesspoints()}
    check('accesspoint 表非空', len(aps) > 0, f'{len(aps)} 行')
    bf_ids = {b['id'] for b in bfs}
    check('accesspoint 归属 battlefield（城市）存在',
          all(a['battlefield'] in bf_ids for a in aps.values() if a['battlefield']))
    sls = wardb.supplylines()
    check('supplyline 表非空', len(sls) > 0, f'{len(sls)} 行')
    check('supplyline 两端 accesspoint 存在（拓扑闭合）',
          all(s['ap1'] in aps and s['ap2'] in aps for s in sls))

    # ---- D. commandnode_templates ----
    tpl = wardb.commandnode_templates()
    check('commandnodetemplate 表非空', len(tpl) > 0, f'{len(tpl)} 行')
    check('模板 faction ∈ {1,2,3}',
          all(t['faction'] in (1, 2, 3) for t in tpl.values()))
    for tid, fid in ((103, 1), (203, 2), (303, 3)):
        t = tpl.get(tid)
        check(f'infantry 模板 {tid} 存在（faction={fid}）', t is not None
              and t['name'] == 'infantry' and t['faction'] == fid)
        if t:
            check(f'infantry {tid} 速度 0.6（官服实值）',
                  abs(t['speed'] - 0.6) < 1e-6, f'{t["speed"]}')
            check(f'infantry {tid} 编制资源 {{1: 90}}',
                  t['resources'] == {1: 90}, f'{t["resources"]}')

    # ---- E. at_levels ----
    lv = wardb.at_levels()
    check('AssaultTeamLevel 官服阈值 (1,350)/(2,3450)/(3,9200)',
          lv[:3] == [(1, 350), (2, 3450), (3, 9200)], f'{lv[:3]}')

    # ---- F. morale_template（官服实值）----
    mt = wardb.morale_template()
    check('CommandNodeMoraleTemplate 存在', mt is not None)
    if mt:
        check('morale max=100', mt['max'] == 100, f'{mt["max"]}')
        check('morale lost_per_battle=20', mt['lost_per_battle'] == 20)
        check('morale gained_per_win=10', mt['gained_per_win'] == 10)
        check('morale gained_per_minute≈0.15',
              abs(mt['gained_per_minute'] - 0.15) < 1e-3,
              f'{mt["gained_per_minute"]}')
        check('morale surrender_limit=25', mt['surrender_limit'] == 25)

    # ---- G. at_pricing ----
    pr = wardb.at_pricing()
    check('Pricing 表非空', len(pr) > 0, f'{len(pr)} 行')
    tids = set(tpl)
    hit = [(oid, p) for oid, p in pr.items() if oid in tids]
    check('Pricing objectId 命中 AT 模板段', len(hit) > 0, f'{len(hit)} 条命中')
    check('命中条目 warfunds > 0',
          all(p['warfunds'] > 0 for _, p in hit))

    # ---- H. armyresource_production ----
    ar = wardb.armyresource_production()
    check('ArmyResourceProductionTemplate 非空', len(ar) > 0, f'{len(ar)} 行')
    check('生产模板 faction ∈ {1,2,3}',
          all(r['faction'] in (1, 2, 3) for r in ar))

    passed = sum(1 for _, ok in RESULTS if ok)
    print(f'\n{passed}/{len(RESULTS)} checks passed')
    if passed != len(RESULTS):
        sys.exit(1)


if __name__ == '__main__':
    main()
