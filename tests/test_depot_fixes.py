# -*- coding: utf-8 -*-
"""2026-08-27 depot 遗留问题修复回归测试（test_depot_ammo.py 后续）。

问题 1（严重）：send_upgrades_to_depot 语义实现错——客户端 ItemInfoOverlayMediator
  showSendUpgradesToDepotButtonIfNeeded 发的 itemType 是【容器类型】(1=weapon
  2=vehicle)、objectId 是【容器实例 id】（"卸下该武器/载具上挂的升级件"），
  旧实现按升级件自身类型查表 → UPDATE no-op，按钮永远无效。
问题 2（严重）：武器退仓零 KVCS 重推（La-7 事故同类残留路径）——只回 response
  不推 Weapon/character 行，UI 不走 → 玩家连点重发；本会话 unlockBits 不刷新。
问题 3（中等）：DepotNewItem 通知链路死代码——depot_items 只写不读，入库误标
  viewed=TRUE，登录不推 DepotNewItem 行 → 仓库红点永不出现。
问题 4（低）：余额不足 response 码不分货币（一律 56，金币不足提示错误货币）。
问题 5（低）：depot_items 无唯一约束 → 行无限膨胀 + UPDATE 跨 tab 误标。

验证（DB 级，经 PlayerStore 直连）：
  1. send_upgrades_to_depot(weapon 容器) → weapon_modifiers.weapon_id/character_id 清 0
  2. send_upgrades_to_depot(vehicle 容器) → 升级卸下 + 皮肤复位 + 变体实例归仓
  3. send_item_to_depot → 配件/皮肤/弹药 character_id 跟随（武器与载具）
  4. set_inventory_item_character → 挂靠行 character_id 跟随指派
  5. mark_depot_item_new/viewed 幂等往返 + 唯一索引 + list_depot_new_items
验证（源码级，无需运行服务器）：
  6. 余额不足 response 码按货币映射（56/153/57）
  7. 退仓/指派/卸升级件 handler 重推 Weapon/挂靠行/character/DepotNewItem
  8. 登录回放推 DepotNewItem 行
"""
import asyncio
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0


def mark(label, good, extra=''):
    global PASS, FAIL
    PASS += bool(good)
    FAIL += (not good)
    print(f'  {"PASS" if good else "FAIL"}  {label}{extra}')


async def cleanup(st, pid):
    """清掉本测试玩家的可测行（保留玩家行本身）。"""
    async with st.pool.acquire() as conn:
        for tbl in ('weapon_ammo', 'weapon_modifiers', 'weapon_variants',
                    'vehicle_ammo', 'vehicle_upgrades', 'vehicle_variants'):
            await conn.execute(f'DELETE FROM {tbl} WHERE player_id = $1', int(pid))
        await conn.execute("DELETE FROM weapons WHERE player_id = $1 "
                           "AND name = 'U10DepotGun'", int(pid))
        await conn.execute('DELETE FROM vehicles WHERE player_id = $1', int(pid))
        await conn.execute('DELETE FROM depot_items WHERE player_id = $1', int(pid))


async def main():
    import player_store

    st = player_store.PlayerStore()
    await st.start()

    # ---------- DB 级 ----------
    print('== 1. SendUpgradesToDepot 容器语义（武器：卸配件）==')
    pid = await st.get_player('u10depot_a') or await st.create_player('u10depot_a', 'pw')
    await cleanup(st, pid)
    ch = await st.get_latest_character(pid)
    if ch is None:
        await st.create_character(pid, 'U10DepotSoldier', 1, 0)
        ch = await st.get_latest_character(pid)
    cid = int(ch['id'])
    print(f'  测试账号 pid={pid} char={cid}')

    # 武器（已退仓 character_id=0）+ 挂靠配件 = 客户端 isModded 按钮现场
    w = await st.create_weapon(pid, 'U10DepotGun', 40, 0, 0)
    m = await st.create_weapon_modifier(pid, 3, 0, w['id'])
    wid, mid = int(w['id']), int(m['id'])
    n = await st.send_upgrades_to_depot(pid, 1, wid)   # itemType=1 = 武器容器
    async with st.pool.acquire() as conn:
        mrow = await conn.fetchrow('SELECT weapon_id, character_id '
                                   'FROM weapon_modifiers WHERE id = $1', mid)
    mark('卸配件：返回受影响行数 ≥ 1（旧实现 rowcount=0 → response=0 按钮无效）',
         n >= 1, f' n={n}')
    mark('卸配件：weapon_modifiers.weapon_id/character_id 清 0（回仓库）',
         mrow['weapon_id'] == 0 and mrow['character_id'] == 0,
         f' m=({mrow["weapon_id"]},{mrow["character_id"]})')
    n2 = await st.send_upgrades_to_depot(pid, 1, wid)
    mark('卸配件幂等：重复请求返回 0（无可卸配件）', n2 == 0, f' n2={n2}')

    print('== 2. SendUpgradesToDepot 容器语义（载具：卸升级 + 皮肤复位）==')
    v = await st.create_vehicle(pid, 205, 0)
    up = await st.create_vehicle_upgrade(pid, 10, v['id'], 0, 100.0)
    # 升级装到槽位 2（equipped_slot_id=2）
    async with st.pool.acquire() as conn:
        await conn.execute('UPDATE vehicle_upgrades SET equipped_slot_id = 2 '
                           'WHERE id = $1', up['id'])
    vv = await st.create_vehicle_variant(pid, v['id'], 0, 205)
    async with st.pool.acquire() as conn:
        await conn.execute('UPDATE vehicles SET variant_id = $1 WHERE id = $2',
                           vv['id'], v['id'])
    vid, upid, vvid = int(v['id']), int(up['id']), int(vv['id'])
    n = await st.send_upgrades_to_depot(pid, 2, vid)   # itemType=2 = 载具容器
    async with st.pool.acquire() as conn:
        uprow = await conn.fetchrow('SELECT vehicle_id, equipped_slot_id, character_id '
                                    'FROM vehicle_upgrades WHERE id = $1', upid)
        vrow = await conn.fetchrow('SELECT variant_id FROM vehicles WHERE id = $1', vid)
        vvrow = await conn.fetchrow('SELECT character_id FROM vehicle_variants '
                                    'WHERE id = $1', vvid)
    mark('卸升级：返回受影响行数 ≥ 1', n >= 1, f' n={n}')
    mark('卸升级：vehicle_upgrades 卸下（vehicle_id/slot/character_id 清 0）',
         uprow['vehicle_id'] == 0 and uprow['equipped_slot_id'] == 0
         and uprow['character_id'] == 0,
         f' up=({uprow["vehicle_id"]},{uprow["equipped_slot_id"]},{uprow["character_id"]})')
    mark('卸升级：vehicles.variant_id 复位默认皮肤',
         vrow['variant_id'] == 0, f' variant_id={vrow["variant_id"]}')
    mark('卸升级：vehicle_variants.character_id 归仓',
         vvrow['character_id'] == 0, f' vv={vvrow["character_id"]}')

    print('== 3. SendItemToDepot 挂靠行跟随（配件/皮肤/弹药）==')
    # 重建现场：武器+配件+弹药 在角色名下（配件在第 1 节已卸下，先挂回武器）
    async with st.pool.acquire() as conn:
        await conn.execute('UPDATE weapon_modifiers SET weapon_id = $1, '
                           'character_id = 0 WHERE id = $2', wid, mid)
    await st.set_inventory_item_character(pid, 'weapon', wid, cid)
    wv = await st.create_weapon_variant(pid, wid, cid, 40)
    wvid = int(wv['id'])
    a = await st.create_weapon_ammo(pid, 14, cid, wid, 1, 100.0)
    aid = int(a['id'])
    await st.send_item_to_depot(pid, 1, wid)
    async with st.pool.acquire() as conn:
        mrow = await conn.fetchrow('SELECT character_id FROM weapon_modifiers '
                                   'WHERE id = $1', mid)
        wvrow = await conn.fetchrow('SELECT character_id FROM weapon_variants '
                                    'WHERE id = $1', wvid)
        arow = await conn.fetchrow('SELECT character_id FROM weapon_ammo '
                                   'WHERE id = $1', aid)
    mark('武器退仓：配件/皮肤/弹药 character_id 跟随清 0',
         mrow['character_id'] == 0 and wvrow['character_id'] == 0
         and arow['character_id'] == 0,
         f' m={mrow["character_id"]} wv={wvrow["character_id"]} a={arow["character_id"]}')

    # 载具现场：载具+升级+弹药 在角色名下
    await st.set_inventory_item_character(pid, 'vehicle', vid, cid)
    va = await st.create_vehicle_ammo(pid, 22, vid, cid, 100.0)
    vaid = int(va['id'])
    # 升级重新挂回载具（槽 1）
    async with st.pool.acquire() as conn:
        await conn.execute('UPDATE vehicle_upgrades SET vehicle_id = $1, '
                           'equipped_slot_id = 1, character_id = $2 WHERE id = $3',
                           vid, cid, upid)
    await st.send_item_to_depot(pid, 2, vid)
    async with st.pool.acquire() as conn:
        uprow = await conn.fetchrow('SELECT character_id FROM vehicle_upgrades '
                                    'WHERE id = $1', upid)
        varow = await conn.fetchrow('SELECT character_id FROM vehicle_ammo '
                                    'WHERE id = $1', vaid)
    mark('载具退仓：升级/弹药 character_id 跟随清 0',
         uprow['character_id'] == 0 and varow['character_id'] == 0,
         f' up={uprow["character_id"]} va={varow["character_id"]}')

    print('== 4. SetInventoryItemToCharacter 挂靠行跟随 ==')
    await st.set_inventory_item_character(pid, 'weapon', wid, cid)
    async with st.pool.acquire() as conn:
        mrow = await conn.fetchrow('SELECT character_id FROM weapon_modifiers '
                                   'WHERE id = $1', mid)
        wvrow = await conn.fetchrow('SELECT character_id FROM weapon_variants '
                                    'WHERE id = $1', wvid)
        arow = await conn.fetchrow('SELECT character_id FROM weapon_ammo '
                                   'WHERE id = $1', aid)
    mark('武器指派：配件/皮肤/弹药 character_id 跟随指回角色',
         mrow['character_id'] == cid and wvrow['character_id'] == cid
         and arow['character_id'] == cid,
         f' m={mrow["character_id"]} wv={wvrow["character_id"]} a={arow["character_id"]}')
    await st.set_inventory_item_character(pid, 'vehicle', vid, cid)
    async with st.pool.acquire() as conn:
        uprow = await conn.fetchrow('SELECT character_id FROM vehicle_upgrades '
                                    'WHERE id = $1', upid)
        varow = await conn.fetchrow('SELECT character_id FROM vehicle_ammo '
                                    'WHERE id = $1', vaid)
    mark('载具指派：升级/弹药 character_id 跟随指回角色',
         uprow['character_id'] == cid and varow['character_id'] == cid,
         f' up={uprow["character_id"]} va={varow["character_id"]}')

    print('== 5. depot_items 新物品标记（红点链路）==')
    await st.mark_depot_item_new(pid, 'weapons', wid)
    await st.mark_depot_item_new(pid, 'weapons', wid)      # 幂等：不重复插行
    await st.mark_depot_item_new(pid, 'vehicles', vid)
    async with st.pool.acquire() as conn:
        rows = await conn.fetch('SELECT item_type, item_id, viewed '
                                'FROM depot_items WHERE player_id = $1 '
                                'ORDER BY item_type', int(pid))
    mark('入仓标记"新"：viewed=FALSE 且无重复行（唯一索引）',
         len(rows) == 2 and all(not r['viewed'] for r in rows),
         f' rows={[(r["item_type"], r["item_id"], r["viewed"]) for r in rows]}')
    new_items = await st.list_depot_new_items(pid)
    mark('list_depot_new_items：按 tab 分组返回未读',
         new_items == {'weapons': [wid], 'vehicles': [vid]},
         f' new={new_items}')
    await st.mark_depot_item_viewed(pid, 'weapons', wid)
    async with st.pool.acquire() as conn:
        cnt = await conn.fetchval('SELECT count(*) FROM depot_items '
                                  'WHERE player_id = $1 AND item_id = $2',
                                  int(pid), wid)
    mark('已读标记：viewed=TRUE 且行数仍为 1（UPDATE 带 item_type）',
         cnt == 1, f' count={cnt}')
    new_items = await st.list_depot_new_items(pid)
    mark('已读后：list_depot_new_items 不再包含该物品',
         new_items == {'vehicles': [vid]}, f' new={new_items}')

    # ---------- 源码级 ----------
    print('== 6. 余额不足 response 码按货币映射（源码级）==')
    import handlers.characters as hc

    src = inspect.getsource(hc)
    mark('characters: _NOT_ENOUGH_RESP 映射 56/153/57（credits/warfunds/gold）',
         '_NOT_ENOUGH_RESP' in src
         and '56' in src and '153' in src and '57' in src)

    print('== 7. depot handler 重推链路（源码级）==')
    src = inspect.getsource(hc.handle_senditemtodepotrequest)
    mark('退仓：武器分支重推 _repush_weapon_rows（La-7 同类事故武器版）',
         '_repush_weapon_rows' in src)
    mark('退仓：武器/弹药/配件后重推 character（unlockBits 刷新）',
         '_repush_character_by_id' in src)
    mark('退仓：入仓标记 mark_depot_item_new（非 viewed，红点链路）',
         'mark_depot_item_new' in src and 'mark_depot_item_viewed' not in src)
    mark('退仓：重推 DepotNewItem 行（红点即时点亮）',
         '_repush_depot_new_item' in src)

    src = inspect.getsource(hc.handle_sendupgradestodepotrequest)
    mark('卸升级件：成功后重推（WeaponModifier 行 / _repush_vehicle_row）',
         'WeaponModifier' in src and '_repush_vehicle_row' in src)

    src = inspect.getsource(hc.handle_setinventoryitemtocharacterrequest)
    mark('指派：武器分支重推 _repush_weapon_rows',
         '_repush_weapon_rows' in src)

    src = inspect.getsource(hc.handle_setdepotitemviewedrequest)
    mark('已读：重推 DepotNewItem 行（红点即时消点）',
         '_repush_depot_new_item' in src)

    print('== 8. 登录回放推 DepotNewItem + schema 唯一约束（源码级）==')
    import player_service
    src = inspect.getsource(player_service._push_stored_soldier_data)
    mark('player_service: 登录回放推 DepotNewItem KVCS 行',
         'DepotNewItem' in src and '_msg_depot_new_item' in src)

    schema = player_store._SCHEMA
    mark('schema: depot_items 唯一索引 uq_depot_items(player,item_type,item_id)',
         'uq_depot_items' in schema)

    # 收尾清理
    await cleanup(st, pid)
    print('  [cleanup] u10depot 测试行已清理')
    await st.close()
    print(f'\n{"ALL PASS" if FAIL == 0 else f"{FAIL} FAILED"} (ok={PASS})')
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
