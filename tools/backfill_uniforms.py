# -*- coding: utf-8 -*-
"""一次性回填：给 uniform_variant_id=0 的角色补 (兵种,阵营) 基础迷彩实例。

背景（2026-08-21）：create_character 历史上不分配默认军服，导致全动态
PlayerSettings f9 variantTemplateId 缺失、部署界面角色不显示。
服务器已有运行时兜底（wardb.default_uniform_variant），本脚本把数据补进库：
外观界面"已拥有迷彩"将包含基础款，且新角色由 create_character 自动分配。

幂等：只处理 uniform_variant_id=0 的角色，可重复执行。
用法：python tools\backfill_uniforms.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # server/

import player_store  # noqa: E402
from wardb import default_uniform_variant  # noqa: E402


async def main():
    st = player_store.PlayerStore()
    await st.start()
    async with st.pool.acquire() as conn:
        rows = await conn.fetch(
            'SELECT id, player_id, name, faction, template '
            'FROM characters WHERE uniform_variant_id = 0 ORDER BY id')
        print(f'待回填角色: {len(rows)} 个')
        fixed = skipped = 0
        async with conn.transaction():
            for r in rows:
                uv_tid = default_uniform_variant(r['template'], r['faction'])
                if not uv_tid:
                    print(f"  SKIP  {r['name']}(id={r['id']}): "
                          f"(template={r['template']}, faction={r['faction']}) 无默认变体")
                    skipped += 1
                    continue
                uv = await conn.fetchrow(
                    'INSERT INTO uniform_variants '
                    '(player_id, uniform_variant_template_id, character_id) '
                    'VALUES ($1, $2, $3) RETURNING id',
                    r['player_id'], uv_tid, r['id'])
                await conn.execute(
                    'UPDATE characters SET uniform_variant_id = $1 WHERE id = $2',
                    uv['id'], r['id'])
                fixed += 1
                print(f"  OK    {r['name']}(id={r['id']}): "
                      f"军服实例 {uv['id']} -> 模板 {uv_tid}")

        total_chars = await conn.fetchval('SELECT count(*) FROM characters')
        assigned = await conn.fetchval(
            'SELECT count(*) FROM characters WHERE uniform_variant_id > 0')
        total_uv = await conn.fetchval('SELECT count(*) FROM uniform_variants')
        print(f'\n回填 {fixed} 个，跳过 {skipped} 个；'
              f'已装备军服角色 {assigned}/{total_chars}，uniform_variants 共 {total_uv} 行')
    await st.close()


asyncio.run(main())
