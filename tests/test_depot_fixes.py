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
问题 6（严重，2026-08-27 官方对齐）：挂靠件单独指派角色不脱离容器——
  weapon_modifiers/weapon_variants/vehicle_upgrades 等指派后 weapon_id/
  vehicle_id 保留 → 违反官方不变式（客户端只认 characterId==角色 &&
  weaponId==本枪||0），配件成为"幽灵"（改枪窗口/仓库页签均不可见）；
  且指派/退仓只重推 weapon/vehicle 两类行，其余 8 类 itemType 无 KVCS
  重推 → UI 不收敛连点重发。

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
                    'vehicle_ammo', 'vehicle_upgrades', 'vehicle_variants',
                    'tracer_effects'):
            await conn.execute(f'DELETE FROM {tbl} WHERE player_id = $1', int(pid))
        await conn.execute("DELETE FROM weapons WHERE player_id = $1 "
                           "AND name = 'U10DepotGun'", int(pid))
        await conn.execute('DELETE FROM vehicles WHERE player_id = $1', int(pid))
        await conn.execute('DELETE FROM depot_items WHERE player_id = $1', int(pid))


async def main():
    import store

    st = store.PlayerStore()
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

    print('== 4b. 挂靠件单独指派角色（脱离容器，官方不变式）==')
    # 现场A：仓库枪 + 挂在枪上的配件（character_id=0, weapon_id=仓库枪）
    w2 = await st.create_weapon(pid, 'U10DepotGun', 40, 0, 0)
    w2id = int(w2['id'])
    m2 = await st.create_weapon_modifier(pid, 3, 0, w2id)
    m2id = int(m2['id'])
    ok = await st.set_inventory_item_character(pid, 'weaponmodifier', m2id, cid)
    async with st.pool.acquire() as conn:
        m2row = await conn.fetchrow('SELECT weapon_id, character_id '
                                    'FROM weapon_modifiers WHERE id = $1', m2id)
    mark('配件指派：character_id=角色 且 weapon_id 清 0（脱离仓库枪；'
         '旧实现保留 weapon_id → 幽灵配件：角色改枪窗口不可见、'
         '仓库配件页签也不可见）',
         ok and m2row['character_id'] == cid and m2row['weapon_id'] == 0,
         f' m=({m2row["weapon_id"]},{m2row["character_id"]})')

    # 现场B：仓库载具 + 挂槽位的升级件
    v2 = await st.create_vehicle(pid, 205, 0)
    v2id = int(v2['id'])
    vu2 = await st.create_vehicle_upgrade(pid, 10, v2id, 0, 100.0)
    vu2id = int(vu2['id'])
    async with st.pool.acquire() as conn:
        await conn.execute('UPDATE vehicle_upgrades SET equipped_slot_id = 2 '
                           'WHERE id = $1', vu2id)
    ok = await st.set_inventory_item_character(pid, 'vehicleupgrade', vu2id, cid)
    async with st.pool.acquire() as conn:
        vu2row = await conn.fetchrow('SELECT vehicle_id, equipped_slot_id, '
                                     'character_id FROM vehicle_upgrades '
                                     'WHERE id = $1', vu2id)
    mark('载具升级指派：vehicle_id/slot 清 0 且 character_id=角色',
         ok and vu2row['vehicle_id'] == 0 and vu2row['equipped_slot_id'] == 0
         and vu2row['character_id'] == cid,
         f' vu=({vu2row["vehicle_id"]},{vu2row["equipped_slot_id"]},'
         f'{vu2row["character_id"]})')

    # 现场C：仓库枪 + 皮肤实例（weapons.variant_id 引用该实例）
    wv2 = await st.create_weapon_variant(pid, w2id, 0, 40)
    wv2id = int(wv2['id'])
    async with st.pool.acquire() as conn:
        await conn.execute('UPDATE weapons SET variant_id = $1 WHERE id = $2',
                           wv2id, w2id)
    ok = await st.set_inventory_item_character(pid, 'weaponvariant', wv2id, cid)
    async with st.pool.acquire() as conn:
        wv2row = await conn.fetchrow('SELECT weapon_id, character_id '
                                     'FROM weapon_variants WHERE id = $1', wv2id)
        w2row = await conn.fetchrow('SELECT variant_id FROM weapons '
                                    'WHERE id = $1', w2id)
    mark('皮肤指派：weapon_id 清 0 且引用它的武器 variant_id 复位默认',
         ok and wv2row['weapon_id'] == 0 and wv2row['character_id'] == cid
         and w2row['variant_id'] == 0,
         f' wv=({wv2row["weapon_id"]},{wv2row["character_id"]}) '
         f'w.variant_id={w2row["variant_id"]}')

    # 现场D：制服（无挂靠关系，纯归属切换）
    uv2 = await st.create_uniform_variant(pid, 40, 0)
    uv2id = int(uv2['id'])
    ok = await st.set_inventory_item_character(pid, 'uniformvariant', uv2id, cid)
    async with st.pool.acquire() as conn:
        uv2row = await conn.fetchrow('SELECT character_id FROM uniform_variants '
                                     'WHERE id = $1', uv2id)
    mark('制服指派：character_id=角色（无容器外键，不受 detach 影响）',
         ok and uv2row['character_id'] == cid,
         f' uv={uv2row["character_id"]}')

    # 现场E：曳光弹（客户端 itemType='tracereffect' 字符串 / 退仓枚举 42）
    # 2026-09-04 事故：itemType 不在 _ITEM_TABLES → 指派直接 response=0，
    # 仓库拖曳光弹到士兵无效（itemId=1100/charId=25 实锤）
    tr = await st.create_tracer_effect(pid, 1)     # 通行证发放=仓库在库
    trid = int(tr['id'])
    ok = await st.set_inventory_item_character(pid, 'tracereffect', trid, cid)
    async with st.pool.acquire() as conn:
        trrow = await conn.fetchrow('SELECT character_id, weapon_id, is_equipped '
                                    'FROM tracer_effects WHERE id = $1', trid)
    mark('曳光弹指派：character_id=角色（旧实现 itemType 未映射 → '
         'response=0 拖拽无效）',
         ok and trrow['character_id'] == cid,
         f' tr=({trrow["character_id"]},{trrow["weapon_id"]},'
         f'{trrow["is_equipped"]})')

    # 装备到武器（武器引用该行）后再指派角色 → 指派应解除武器绑定
    await st.set_weapon_tracer_effect(pid, w2id, trid, cid)
    ok2 = await st.set_inventory_item_character(pid, 'tracereffect', trid, cid)
    async with st.pool.acquire() as conn:
        trrow = await conn.fetchrow('SELECT character_id, weapon_id, is_equipped '
                                    'FROM tracer_effects WHERE id = $1', trid)
        w2trow = await conn.fetchrow('SELECT tracer_effect_id FROM weapons '
                                     'WHERE id = $1', w2id)
    mark('曳光弹装备中指派：解除武器绑定（weapon_id=0/is_equipped=FALSE）'
         '且 weapons.tracer_effect_id 复位',
         ok2 and trrow['character_id'] == cid and trrow['weapon_id'] == 0
         and not trrow['is_equipped'] and w2trow['tracer_effect_id'] == 0,
         f' tr=({trrow["character_id"]},{trrow["weapon_id"]},'
         f'{trrow["is_equipped"]}) w.tracer={w2trow["tracer_effect_id"]}')

    # 装备到武器后退仓（客户端 SendItemToDepotRequest itemType=42）
    await st.set_weapon_tracer_effect(pid, w2id, trid, cid)
    ok3 = await st.send_item_to_depot(pid, 42, trid)
    async with st.pool.acquire() as conn:
        trrow = await conn.fetchrow('SELECT character_id, weapon_id, is_equipped '
                                    'FROM tracer_effects WHERE id = $1', trid)
        w2trow = await conn.fetchrow('SELECT tracer_effect_id FROM weapons '
                                     'WHERE id = $1', w2id)
    mark('曳光弹退仓（itemType=42）：归仓+卸装+武器引用复位'
         '（旧实现枚举未映射 → response=0）',
         ok3 and trrow['character_id'] == 0 and trrow['weapon_id'] == 0
         and not trrow['is_equipped'] and w2trow['tracer_effect_id'] == 0,
         f' tr=({trrow["character_id"]},{trrow["weapon_id"]},'
         f'{trrow["is_equipped"]}) w.tracer={w2trow["tracer_effect_id"]}')

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
    mark('退仓：全 itemType 通用重推 _repush_item_row（La-7 同类事故扩展）',
         '_repush_item_row' in src)
    mark('退仓：任意类型退仓后重推原角色行（unlockBits 刷新，旧版仅 1/5/6）',
         '_repush_character_by_id' in src)
    mark('退仓：入仓标记 mark_depot_item_new（非 viewed，红点链路）',
         'mark_depot_item_new' in src and 'mark_depot_item_viewed' not in src)
    mark('退仓：重推 DepotNewItem 行（红点即时点亮）',
         '_repush_depot_new_item' in src)

    src = inspect.getsource(hc.handle_sendupgradestodepotrequest)
    mark('卸升级件：成功后重推（WeaponModifier 行 / _repush_vehicle_row）',
         'WeaponModifier' in src and '_repush_vehicle_row' in src)

    src = inspect.getsource(hc.handle_setinventoryitemtocharacterrequest)
    mark('指派：全 itemType 通用重推 _repush_item_row（旧版仅 weapon/vehicle）',
         '_repush_item_row' in src)
    mark('指派：捕获 prev_char 重推原角色行（unlockBits 摘 uid）',
         'prev_char' in src and '_repush_character_by_id' in src)

    # _repush_item_row 覆盖全部 10 个 ItemType 表 + 字符串映射齐全
    src = inspect.getsource(hc._repush_item_row)
    mark('重推器：覆盖 UniformPart/UniformVariant/WeaponModifier/WeaponAmmo/'
         'WeaponVariant/VehicleUpgrade/VehicleAmmo/VehicleVariant 全部 8 类单行',
         all(t in src for t in ('UniformPart', 'UniformVariant',
                                'WeaponModifier', 'WeaponAmmo', 'WeaponVariant',
                                'VehicleUpgrade', 'VehicleAmmo',
                                'VehicleVariant'))
         and '_repush_weapon_rows' in src and '_repush_vehicle_row' in src)
    mark('重推器：_DEPOT_STR_TYPE 字符串映射含全部 10 类 + tracereffect=42'
         '（客户端 buildSendItemToSoldierRequest case 42 → "tracereffect"）',
         len(hc._DEPOT_STR_TYPE) == 11
         and set(hc._DEPOT_STR_TYPE.values()) == set(range(1, 11)) | {42}
         and hc._DEPOT_STR_TYPE.get('tracereffect') == 42)
    src = inspect.getsource(hc._repush_item_row)
    mark('重推器：tid=42 推 TracerEffect 行（指派/退仓后 UI 收敛）',
         "tid == 42" in src and 'TracerEffect' in src)

    # 持久层（store.maintenance）：挂靠件指派脱离容器（官方容器归属不变式；
    # 字典定义在类属性上，取类级源码校验。2026-09 拆分后为 MaintenanceMixin）
    src = inspect.getsource(store.maintenance.MaintenanceMixin)
    mark('store：挂靠件指派同步清 weapon_id/vehicle_id（_ITEM_DETACH_SETS）',
         '_ITEM_DETACH_SETS' in src and 'weapon_id = 0' in src
         and 'vehicle_id = 0' in src)

    src = inspect.getsource(hc.handle_setdepotitemviewedrequest)
    mark('已读：重推 DepotNewItem 行（红点即时消点）',
         '_repush_depot_new_item' in src)

    print('== 8. 登录回放推 DepotNewItem + schema 唯一约束（源码级）==')
    import player_service
    src = inspect.getsource(player_service._push_stored_soldier_data)
    mark('player_service: 登录回放推 DepotNewItem KVCS 行',
         'DepotNewItem' in src and '_msg_depot_new_item' in src)

    schema = store._SCHEMA
    mark('schema: depot_items 唯一索引 uq_depot_items(player,item_type,item_id)',
         'uq_depot_items' in schema)

    # 收尾清理
    await cleanup(st, pid)
    print('  [cleanup] u10depot 测试行已清理')
    await st.close()
    print(f'\n{"ALL PASS" if FAIL == 0 else f"{FAIL} FAILED"} (ok={PASS})')
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
