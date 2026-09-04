# -*- coding: utf-8 -*-
"""2026-08-27 武器入库弹药跟随 + 购买后 character KVCS 重推回归测试。

事故 1（永久锁定，weapon 147/ammo 346 实锤）：武器送仓库只清 weapons 行，
  weapon_ammo 仍挂原角色且 slot 1 装备中 → _char_unlock_bits_for 弹药回查
  查不到武器（不在角色名下）→ 默认弹药 uid 加不上 → 客户端
  isOwned && !isUnlocked → 勾选框锁死 + "Ribbon required"，重登也不恢复。
事故 2（会话内锁定，MG13/1903 Pocket 实锤）：购买武器/配件/弹药后不重推
  character KVCS，客户端 unlockBits 是登录时的旧位图 → 本会话内刚买的
  默认弹药显示锁定 + "Ribbon required"，重登才恢复。

验证（DB 级，经 PlayerStore 直连）：
  1. send_item_to_depot(weapon) → weapon_ammo.character_id 跟随清 0（slot 保留）
  2. set_inventory_item_character(weapon) → weapon_ammo.character_id 跟随指派
  3. 全库不变量：ammo.character_id == weapon.character_id（脏数据扫描为 0）
验证（源码级，无需运行服务器）：
  4. weapons.py 三个购买 handler 均调用 _repush_character_unlocks
  5. characters.py 武器指派 handler 重推 character（unlockBits 刷新）
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


async def main():
    import player_store

    st = player_store.PlayerStore()
    await st.start()

    # ---------- DB 级 ----------
    print('== 1/2. 武器退仓/指派 → 弹药跟随（weapon_ammo.character_id）==')
    pid = await st.get_player('u9depot_a') or await st.create_player('u9depot_a', 'pw')
    # 干净起点：清掉历史测试行
    async with st.pool.acquire() as conn:
        await conn.execute('DELETE FROM weapon_ammo WHERE player_id = $1', int(pid))
        await conn.execute("DELETE FROM weapons WHERE player_id = $1 "
                           "AND name = 'U9DepotGun'", int(pid))
    ch = await st.get_latest_character(pid)
    if ch is None:
        await st.create_character(pid, 'U9DepotSoldier', 1, 0)
        ch = await st.get_latest_character(pid)
    cid = int(ch['id'])
    print(f'  测试账号 pid={pid} char={cid}')

    # 武器（M1 Garand template 40）+ 默认弹药（14，slot 1 装备中）= 事故现场
    w = await st.create_weapon(pid, 'U9DepotGun', 40, cid, 0)
    await st.update_weapon_container(pid, w['id'], cid, 1)
    a = await st.create_weapon_ammo(pid, 14, cid, w['id'], 1, 100.0)
    wid, aid = int(w['id']), int(a['id'])

    # 1) 送仓库：武器 + 弹药 character_id 都清 0，弹药 slot 保留
    updated = await st.send_item_to_depot(pid, 1, wid)   # ItemType 1 = weapons
    async with st.pool.acquire() as conn:
        wrow = await conn.fetchrow('SELECT character_id, container_id, container_slot '
                                   'FROM weapons WHERE id = $1', wid)
        arow = await conn.fetchrow('SELECT character_id, slot_index '
                                   'FROM weapon_ammo WHERE id = $1', aid)
    mark('退仓：weapons 行清 0（含容器槽）',
         updated and wrow['character_id'] == 0 and wrow['container_id'] == 0
         and wrow['container_slot'] == 0,
         f" w=({wrow['character_id']},{wrow['container_id']},{wrow['container_slot']})")
    mark('退仓：weapon_ammo.character_id 跟随清 0（slot 保留）',
         arow['character_id'] == 0 and arow['slot_index'] == 1,
         f" a=({arow['character_id']},{arow['slot_index']})")

    # 2) 重新指派到角色：武器 + 弹药 character_id 都指回，slot 保留（自动恢复装备）
    updated = await st.set_inventory_item_character(pid, 'weapon', wid, cid)
    async with st.pool.acquire() as conn:
        wrow = await conn.fetchrow('SELECT character_id FROM weapons WHERE id = $1', wid)
        arow = await conn.fetchrow('SELECT character_id, slot_index '
                                   'FROM weapon_ammo WHERE id = $1', aid)
    mark('指派：weapons.character_id 指回角色',
         updated and wrow['character_id'] == cid, f" w={wrow['character_id']}")
    mark('指派：weapon_ammo.character_id 跟随指回（slot 保留）',
         arow['character_id'] == cid and arow['slot_index'] == 1,
         f" a=({arow['character_id']},{arow['slot_index']})")

    print('== 3. 全库脏数据扫描（ammo.character_id != weapon.character_id）==')
    async with st.pool.acquire() as conn:
        n = await conn.fetchval(
            'SELECT count(*) FROM weapon_ammo a JOIN weapons w '
            'ON w.id = a.weapon_id AND w.player_id = a.player_id '
            'WHERE a.character_id IS DISTINCT FROM w.character_id')
    mark('全库武器/弹药归属一致（脏行 = 0）', n == 0, f' 脏行={n}')

    # ---------- 源码级 ----------
    print('== 4/5. 购买/指派 handler 重推 character（源码级）==')
    import handlers.weapons as hw
    import handlers.characters as hc

    for fname in ('handle_createweaponvariantrequest',
                 'handle_createweaponmodifierrequest',
                 'handle_createweaponammorequest'):
        src = inspect.getsource(getattr(hw, fname))
        mark(f'weapons.{fname} 调用 _repush_character_unlocks',
             '_repush_character_unlocks(' in src)

    src = inspect.getsource(hc.handle_setinventoryitemtocharacterrequest)
    mark('characters.SetInventoryItemToCharacter 武器指派后重推 character',
         "_repush_character_by_id" in src and 'weapon' in src)

    # 收尾清理
    async with st.pool.acquire() as conn:
        await conn.execute('DELETE FROM weapon_ammo WHERE player_id = $1', int(pid))
        await conn.execute("DELETE FROM weapons WHERE player_id = $1 "
                           "AND name = 'U9DepotGun'", int(pid))
    print('  [cleanup] u9depot 测试行已清理')
    await st.close()
    print(f'\n{"ALL PASS" if FAIL == 0 else f"{FAIL} FAILED"} (ok={PASS})')
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
