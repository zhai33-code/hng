"""排查车载武器局内不实装：对照 vehicles / vehicle_upgrades / vehicle_ammo 装备状态"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from player_store import PlayerStore


async def main():
    s = PlayerStore()
    await s.start()
    try:
        async with s.pool.acquire() as conn:
            players = await conn.fetch('SELECT id, username FROM players ORDER BY id')
            for p in players:
                pid = p['id']
                print(f"=== player {pid} {p['username']} ===")
                for row in await conn.fetch(
                        'SELECT id, vehicle_variant_template_id, character_id, '
                        'equipped_in_slot FROM vehicles WHERE player_id = $1 '
                        'ORDER BY id', pid):
                    print(f"  vehicle id={row['id']} tpl={row['vehicle_variant_template_id']} "
                          f"char={row['character_id']} equipped_in_slot={row['equipped_in_slot']}")
                for row in await conn.fetch(
                        'SELECT id, upgrade_template_id, vehicle_id, character_id, '
                        'equipped_slot_id, condition FROM vehicle_upgrades '
                        'WHERE player_id = $1 ORDER BY id', pid):
                    print(f"  v_up id={row['id']} tpl={row['upgrade_template_id']} "
                          f"vehicle_id={row['vehicle_id']} char={row['character_id']} "
                          f"equipped_slot={row['equipped_slot_id']} cond={row['condition']}")
                for row in await conn.fetch(
                        'SELECT id, vehicle_id, ammo_template_id, equipped_storage_id, '
                        'slot_index, condition FROM vehicle_ammo '
                        'WHERE player_id = $1 ORDER BY id', pid):
                    print(f"  v_ammo id={row['id']} vehicle_id={row['vehicle_id']} "
                          f"tpl={row['ammo_template_id']} storage={row['equipped_storage_id']} "
                          f"slot={row['slot_index']} cond={row['condition']}")
    finally:
        await s.close()


asyncio.run(main())
