# -*- coding: utf-8 -*-
"""一次性回填：通行证迷彩解锁 + 载具皮肤仓库行 + 误发整车清理（2026-09-08）。

背景：修复前 vehiclevarianttemplate 通行证奖励走整车发放（误发整车 +
默认弹药、不发皮肤行），progressionunlock 奖励（面部迷彩 2387-2395）
不登记 player_unlocks（客户端 hasPlayerLevelUnlock 恒 false）。
本脚本对存量玩家按 battle_pass_progress.claimed_tiers 重放补发：

  1. 迷彩：claimed 模板奖励 XML 中的 progressionunlock 条目
     → player_unlocks（ON CONFLICT DO NOTHING）
  2. 皮肤：claimed 模板奖励 XML 中的 vehiclevarianttemplate 条目
     → vehicle_variants 仓库行（vehicle_id=0/character_id=0，按模板计数
     幂等补差额）
  3. 误发整车清理：vehicles 行模板 ∈ 该玩家 claimed 模板的皮肤条目集合
     且未装备（slot=0/char=0）无升级挂载 → 连同挂载的默认弹药删除；
     装备中/有升级的行仅报告（避免破坏玩家在用资产）

先补发后清理（清理失败玩家也不丢资产）。默认 dry-run，--apply 执行。
用法：python tools\\backfill_pass_skins.py [--apply]
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('HNG_GAME_DIR',
                      os.path.abspath(os.path.join(
                          os.path.dirname(os.path.dirname(
                              os.path.abspath(__file__))), '..', '..', '..')))

from xml.etree import ElementTree as ET  # noqa: E402

from wardb import _parse_wardb_table  # noqa: E402
import store  # noqa: E402

APPLY = '--apply' in sys.argv


def _reward_entries():
    """battle_pass_template → {tpl_id: {'camo': [...], 'skin': [...]}}。"""
    out = {}
    for f in _parse_wardb_table('battle_pass_template'):
        xml = f.get(6, b'')
        if isinstance(xml, bytes):
            xml = xml.decode('utf-8', 'replace')
        try:
            root = ET.fromstring(xml)
        except Exception:
            continue
        ent = {'camo': [], 'skin': []}
        for item in root:
            tag = item.tag.lower()
            if tag in ('progressionunlock', 'vehiclevarianttemplate'):
                ent['camo' if tag == 'progressionunlock' else 'skin'].append(
                    int(item.get('id', 0) or 0))
        if ent['camo'] or ent['skin']:
            out[int(f.get(1, 0))] = ent
    return out


async def main():
    rewards = _reward_entries()
    print(f'含迷彩/皮肤奖励的通行证模板: {len(rewards)} 个')

    st = store.PlayerStore()
    await st.start()
    async with st.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT player_id, season_id, claimed_tiers "
            "FROM battle_pass_progress "
            "WHERE claimed_tiers::text NOT IN ('[]', 'null') ORDER BY player_id")
        print(f'有领取记录的玩家行: {len(rows)}')
        n_camo = n_skin = n_bogus = n_ammo = 0

        for r in rows:
            pid = r['player_id']
            try:
                claimed = [int(x) for x in json.loads(r['claimed_tiers'] or '[]')]
            except Exception:
                claimed = []
            camo_ids = sorted({t for c in claimed
                               for t in rewards.get(c, {}).get('camo', [])})
            skin_ids = [t for c in claimed
                        for t in rewards.get(c, {}).get('skin', [])]
            if not camo_ids and not skin_ids:
                continue
            print(f'player={pid} season={r["season_id"]}: '
                  f'迷彩 {len(camo_ids)} 个, 皮肤条目 {len(skin_ids)} 个')

            async with conn.transaction():
                # 1. 迷彩 → player_unlocks
                if camo_ids:
                    if APPLY:
                        ins = await conn.execute(
                            'INSERT INTO player_unlocks (player_id, unlock_id) '
                            "SELECT $1, x FROM unnest($2::bigint[]) AS x "
                            'ON CONFLICT DO NOTHING',
                            pid, camo_ids)
                        print(f'  player_unlocks +{ins.split()[-1]}: {camo_ids}')
                    else:
                        print(f'  [dry] player_unlocks += {camo_ids}')
                    n_camo += len(camo_ids)

                # 2. 皮肤条目 → 仓库行（按模板计数幂等补差额）
                for tid in sorted(set(skin_ids)):
                    have = await conn.fetchval(
                        'SELECT count(*) FROM vehicle_variants '
                        'WHERE player_id = $1 AND vehicle_id = 0 '
                        'AND vehicle_variant_template_id = $2', pid, tid)
                    missing = skin_ids.count(tid) - int(have or 0)
                    if missing > 0:
                        if APPLY:
                            for _ in range(missing):
                                await conn.execute(
                                    'INSERT INTO vehicle_variants '
                                    '(player_id, vehicle_id, character_id, '
                                    ' vehicle_variant_template_id) '
                                    'VALUES ($1, 0, 0, $2)', pid, tid)
                        print(f'  {"OK " if APPLY else "[dry]"} '
                              f'skin depot row tmpl={tid} x{missing} '
                              f'(已有 {have})')
                        n_skin += missing

                # 3. 误发整车清理（模板 ∈ 本玩家皮肤条目集合）
                if skin_ids:
                    ids = ','.join(str(i) for i in sorted(set(skin_ids)))
                    bogus = await conn.fetch(
                        f'SELECT id, vehicle_variant_template_id, '
                        f'equipped_in_slot, character_id FROM vehicles '
                        f'WHERE player_id = $1 '
                        f'AND vehicle_variant_template_id IN ({ids}) '
                        f'ORDER BY id', pid)
                    for v in bogus:
                        ups = await conn.fetchval(
                            'SELECT count(*) FROM vehicle_upgrades '
                            'WHERE vehicle_id = $1', v['id'])
                        if v['equipped_in_slot'] or v['character_id'] or ups:
                            print(f'  SKIP vehicle={v["id"]} tmpl='
                                  f'{v["vehicle_variant_template_id"]} '
                                  f'装备/挂载中（slot={v["equipped_in_slot"]}, '
                                  f'char={v["character_id"]}, upgrades={ups}）'
                                  f'—— 请人工处理')
                            continue
                        if APPLY:
                            await conn.execute(
                                'DELETE FROM vehicle_ammo WHERE vehicle_id = $1',
                                v['id'])
                            await conn.execute(
                                'DELETE FROM vehicles WHERE id = $1', v['id'])
                        print(f'  {"DEL" if APPLY else "[dry]"} vehicle='
                              f'{v["id"]} tmpl='
                              f'{v["vehicle_variant_template_id"]}（含默认弹药）')
                        n_bogus += 1
                        n_ammo += 1  # 每辆挂 1 条默认弹药（发放时逐车配）

        mode = 'APPLY' if APPLY else 'DRY-RUN（加 --apply 执行）'
        print(f'\n{mode}: 迷彩解锁 {n_camo} 条，皮肤仓库行 {n_skin} 个，'
              f'清理误发整车 {n_bogus} 辆')
    await st.close()


asyncio.run(main())
