# -*- coding: utf-8 -*-
"""载具及其升级/弹药/军服/头饰/外观。
拆分自 player_store.py（纯移动重构，方法体逐字保留）。
"""
class EquipmentVehiclesMixin:
    """载具及其升级/弹药/军服/头饰/外观。"""

    # ---- vehicles ----

    async def list_vehicles(self, player_id, character_id=None):
        """返回 [{id, vehicle_variant_template_id, character_id, equipped_in_slot, variant_id,
        condition, auto_repair}]，按 id 顺序。character_id 非空时只返回该角色名下的载具。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, vehicle_variant_template_id, character_id, equipped_in_slot, '
                'variant_id, condition, auto_repair '
                'FROM vehicles WHERE player_id = $1 '
                'AND ($2::bigint IS NULL OR character_id = $2) ORDER BY id',
                player_id, character_id)
            return [dict(r) for r in rows]

    async def get_vehicle(self, player_id, vehicle_id):
        """按 id 查询单个载具，未找到返回 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, vehicle_variant_template_id, character_id, equipped_in_slot, '
                'variant_id, condition, auto_repair '
                'FROM vehicles WHERE id = $1 AND player_id = $2', vehicle_id, player_id)
            return dict(row) if row else None

    async def create_vehicle(self, player_id, vehicle_variant_template_id,
                             character_id, condition=None):
        """创建载具实例，返回含新 id 的 dict。
        condition 缺省时按模板满耐久初始化（vehicletemplate f35 maxCondition，
        经变体 → 基础模板解析）——旧实现不写 condition 吃 DB 默认 100.0，
        地面载具（max 25~75）超满、飞机（max 200/220）半损，购买即异常。"""
        if condition is None:
            import wardb
            condition = wardb.vehicle_full_condition(vehicle_variant_template_id)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO vehicles (player_id, vehicle_variant_template_id, '
                'character_id, condition) VALUES ($1, $2, $3, $4) '
                'RETURNING id, vehicle_variant_template_id, character_id, equipped_in_slot, '
                'variant_id, condition',
                player_id, vehicle_variant_template_id, character_id, float(condition))
            return dict(row)

    async def delete_vehicle_ammo_rows(self, player_id, ammo_ids):
        """Delete partially-created ammo rows when a multi-row purchase fails."""
        ids = [int(v) for v in ammo_ids]
        if not ids:
            return
        async with self.pool.acquire() as conn:
            await conn.execute(
                'DELETE FROM vehicle_ammo WHERE player_id = $1 '
                'AND id = ANY($2::bigint[])', int(player_id), ids)

    async def set_vehicle_equipment(self, player_id, vehicle_id,
                                    character_id, equip_in_slot):
        """装备载具到角色槽位（equip_in_slot>0）或卸下（0）。返回受影响载具列表
        [{id, vehicle_variant_template_id, character_id, equipped_in_slot, variant_id}]（含被挤掉槽位的）。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # 目标载具校验归属
                row = await conn.fetchrow(
                    'SELECT id, vehicle_variant_template_id, character_id, equipped_in_slot, variant_id, condition '
                    'FROM vehicles WHERE id = $1 AND player_id = $2',
                    vehicle_id, player_id)
                if row is None:
                    return []
                affected = [dict(row)]
                if int(equip_in_slot) > 0:
                    # 挤掉同角色同槽位的其他载具（一个槽位只能一辆）
                    prev = await conn.fetch(
                        'SELECT id, vehicle_variant_template_id, character_id, equipped_in_slot, variant_id, condition '
                        'FROM vehicles WHERE player_id = $1 AND character_id = $2 '
                        'AND equipped_in_slot = $3 AND id <> $4',
                        player_id, character_id, int(equip_in_slot), vehicle_id)
                    for p in prev:
                        await conn.execute(
                            'UPDATE vehicles SET equipped_in_slot = 0 '
                            'WHERE id = $1', p['id'])
                        # asyncpg Record 不可变，须先转 dict 副本再改写，
                        # 否则 TypeError 导致整个事务回滚（挤槽位静默失败）
                        pd = dict(p)
                        pd['equipped_in_slot'] = 0
                        affected.append(pd)
                await conn.execute(
                    'UPDATE vehicles SET character_id = $1, equipped_in_slot = $2 '
                    'WHERE id = $3', character_id, int(equip_in_slot), vehicle_id)
            # 返回最新状态
            ids = [a['id'] for a in affected]
            rows = await conn.fetch(
                'SELECT id, vehicle_variant_template_id, character_id, equipped_in_slot, variant_id, condition '
                'FROM vehicles WHERE id = ANY($1::bigint[]) ORDER BY id', ids)
            return [dict(r) for r in rows]

    # ---- vehicle variants（载具皮肤）----

    async def list_vehicle_variants(self, player_id):
        """返回 [{id, vehicle_id, character_id, vehicle_variant_template_id}]，按 id 顺序。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, vehicle_id, character_id, vehicle_variant_template_id '
                'FROM vehicle_variants WHERE player_id = $1 ORDER BY id', player_id)
            return [dict(r) for r in rows]

    async def create_vehicle_variant(self, player_id, vehicle_id, character_id,
                                     vehicle_variant_template_id):
        """创建载具皮肤变体实例（购买），返回含新 id 的 dict。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO vehicle_variants '
                '(player_id, vehicle_id, character_id, vehicle_variant_template_id) '
                'VALUES ($1, $2, $3, $4) '
                'RETURNING id, vehicle_id, character_id, vehicle_variant_template_id',
                player_id, vehicle_id, character_id, vehicle_variant_template_id)
            return dict(row)

    async def set_vehicle_variant(self, player_id, vehicle_id, variant_id):
        """装备/卸下载具皮肤（variant_id=0 卸下）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE vehicles SET variant_id = $1 '
                'WHERE id = $2 AND player_id = $3', variant_id, vehicle_id, player_id)

    async def bind_vehicle_variant(self, player_id, variant_id, vehicle_id,
                                   character_id):
        """绑定载具皮肤仓库行到载具（SetVehicleVariantRequest 装备仓库皮肤）。
        行必须属于该玩家且 vehicle_id=0（仓库/角色行）；绑定后 character_id
        跟随载具，客户端 isVehicleVariantOwned 要求 characterId==选中角色
        （与 bind_weapon_variant 同款语义）。返回更新后行或 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'UPDATE vehicle_variants SET vehicle_id = $3, character_id = $4 '
                'WHERE id = $1 AND player_id = $2 AND vehicle_id = 0 '
                'RETURNING id, vehicle_id, character_id, vehicle_variant_template_id',
                int(variant_id), int(player_id), int(vehicle_id), int(character_id))
            return dict(row) if row else None

    # ---- vehicle upgrades（载具升级/补给箱）----

    async def list_vehicle_upgrades(self, player_id):
        """返回 [{id, upgrade_template_id, vehicle_id, character_id, equipped_slot_id,
        condition, auto_repair}]。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, upgrade_template_id, vehicle_id, character_id, equipped_slot_id, '
                'condition, auto_repair '
                'FROM vehicle_upgrades WHERE player_id = $1 ORDER BY id', player_id)
            return [dict(r) for r in rows]

    async def create_vehicle_upgrade(self, player_id, upgrade_template_id,
                                     vehicle_id, character_id, condition):
        """创建载具升级实例（购买），返回含新 id 的 dict。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO vehicle_upgrades (player_id, upgrade_template_id, vehicle_id, '
                'character_id, equipped_slot_id, condition) VALUES ($1, $2, $3, $4, 0, $5) '
                'RETURNING id, upgrade_template_id, vehicle_id, character_id, equipped_slot_id, condition',
                player_id, upgrade_template_id, vehicle_id, character_id, condition)
            return dict(row)

    async def set_vehicle_upgrade_equipment(self, player_id, upgrade_id, slot_id,
                                            do_equip, vehicle_id=0):
        """装备（do_equip=true）或卸下（false）载具升级。装备时先清空同载具同槽位
        的其他升级（互斥：一个槽位只能装一件）。装备账户级库存件（vehicle_id=0，
        通行证/军令奖励发放）或跨载具转移时，先把该行绑定到 vehicle_id 对应载具
        （vehicle_id + character_id 一并更新——否则战斗配装按 vehicle_id 分组
        取不到该件，伪装网进战斗无外观）。返回受影响升级行列表。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    'SELECT id, upgrade_template_id, vehicle_id, character_id, equipped_slot_id, condition '
                    'FROM vehicle_upgrades WHERE id = $1 AND player_id = $2',
                    upgrade_id, player_id)
                if row is None:
                    return []
                affected = [dict(row)]
                if do_equip:
                    if vehicle_id and int(vehicle_id) != int(row['vehicle_id'] or 0):
                        # 账户级库存件（vehicle_id=0）/跨载具转移：绑定目标载具
                        vrow = await conn.fetchrow(
                            'SELECT character_id FROM vehicles '
                            'WHERE id = $1 AND player_id = $2', vehicle_id, player_id)
                        if vrow is None:
                            return []  # 目标载具不属于该玩家 → 拒绝
                        await conn.execute(
                            'UPDATE vehicle_upgrades SET vehicle_id = $1, character_id = $2 '
                            'WHERE id = $3', vehicle_id, vrow['character_id'], upgrade_id)
                        affected[0]['vehicle_id'] = vehicle_id
                        affected[0]['character_id'] = vrow['character_id']
                    # 先记录同载具同槽位被挤掉的升级，再清槽（互斥：一个槽位只能装一件）
                    # （车辆级件用绑定后的 vehicle_id，兼容未传 vehicle_id 的旧调用）
                    clear_vehicle = int(vehicle_id) if vehicle_id else int(row['vehicle_id'] or 0)
                    cleared = await conn.fetch(
                        'SELECT id, upgrade_template_id, vehicle_id, character_id, equipped_slot_id, condition '
                        'FROM vehicle_upgrades WHERE player_id = $1 AND vehicle_id = $2 '
                        'AND equipped_slot_id = $3 AND id != $4',
                        player_id, clear_vehicle, slot_id, upgrade_id)
                    await conn.execute(
                        'UPDATE vehicle_upgrades SET equipped_slot_id = 0 '
                        'WHERE player_id = $1 AND vehicle_id = $2 '
                        'AND equipped_slot_id = $3 AND id != $4',
                        player_id, clear_vehicle, slot_id, upgrade_id)
                    for c in cleared:
                        cd = dict(c)          # Record 只读，转 dict 再改
                        cd['equipped_slot_id'] = 0
                        affected.append(cd)
                new_slot = slot_id if do_equip else 0
                await conn.execute(
                    'UPDATE vehicle_upgrades SET equipped_slot_id = $1 '
                    'WHERE id = $2', new_slot, upgrade_id)
                affected[0]['equipped_slot_id'] = new_slot
            return affected

    async def update_vehicle_upgrade_condition(self, player_id, upgrade_id, condition):
        """修复载具升级：更新 condition（耐久）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE vehicle_upgrades SET condition = $1 '
                'WHERE id = $2 AND player_id = $3', condition, upgrade_id, player_id)

    # ---- vehicle ammo（载具弹药）----

    async def list_vehicle_ammo(self, player_id):
        """返回 [{id, ammo_template_id, vehicle_id, character_id, equipped_storage_id,
        slot_index, condition, spawn_clip_count, auto_repair}]，按 id 顺序。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, ammo_template_id, vehicle_id, character_id, equipped_storage_id, '
                'slot_index, condition, spawn_clip_count, auto_repair '
                'FROM vehicle_ammo WHERE player_id = $1 ORDER BY id', player_id)
            return [dict(r) for r in rows]

    async def create_vehicle_ammo(self, player_id, ammo_template_id, vehicle_id,
                                  character_id, condition):
        """创建载具弹药实例（购买），返回含新 id 的 dict。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO vehicle_ammo (player_id, ammo_template_id, vehicle_id, character_id, '
                'equipped_storage_id, slot_index, condition) '
                'VALUES ($1, $2, $3, $4, 0, 0, $5) '
                'RETURNING id, ammo_template_id, vehicle_id, character_id, equipped_storage_id, '
                'slot_index, condition, spawn_clip_count',
                player_id, ammo_template_id, vehicle_id, character_id, condition)
            return dict(row)

    async def set_vehicle_ammo_equipment(self, player_id, ammo_id, storage_id,
                                         slot_index, do_equip):
        """装备（do_equip=true）或卸下（false）载具弹药。装备时先清空同载具同仓同槽位
        的其他弹药（互斥：一个弹药仓槽位只能装一件）。返回受影响弹药行列表。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    'SELECT id, ammo_template_id, vehicle_id, character_id, equipped_storage_id, '
                    'slot_index, condition '
                    'FROM vehicle_ammo WHERE id = $1 AND player_id = $2',
                    ammo_id, player_id)
                if row is None:
                    return []
                affected = [dict(row)]
                if do_equip:
                    # 同载具同仓同槽位被挤掉的弹药先清槽（互斥）
                    cleared = await conn.fetch(
                        'SELECT id, ammo_template_id, vehicle_id, character_id, equipped_storage_id, '
                        'slot_index, condition '
                        'FROM vehicle_ammo WHERE player_id = $1 AND vehicle_id = $2 '
                        'AND equipped_storage_id = $3 AND slot_index = $4 AND id != $5',
                        player_id, row['vehicle_id'], storage_id, slot_index, ammo_id)
                    await conn.execute(
                        'UPDATE vehicle_ammo SET equipped_storage_id = 0, slot_index = 0 '
                        'WHERE player_id = $1 AND vehicle_id = $2 '
                        'AND equipped_storage_id = $3 AND slot_index = $4 AND id != $5',
                        player_id, row['vehicle_id'], storage_id, slot_index, ammo_id)
                    for c in cleared:
                        c['equipped_storage_id'] = 0
                        c['slot_index'] = 0
                        affected.append(dict(c))
                new_storage = storage_id if do_equip else 0
                new_slot = slot_index if do_equip else 0
                await conn.execute(
                    'UPDATE vehicle_ammo SET equipped_storage_id = $1, slot_index = $2 '
                    'WHERE id = $3', new_storage, new_slot, ammo_id)
                affected[0]['equipped_storage_id'] = new_storage
                affected[0]['slot_index'] = new_slot
            return affected

    async def update_vehicle_ammo_condition(self, player_id, ammo_id, condition):
        """修复载具弹药：更新 condition（耐久）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE vehicle_ammo SET condition = $1 '
                'WHERE id = $2 AND player_id = $3', condition, ammo_id, player_id)

    # ---- uniform variants（军服迷彩变体）----

    async def list_uniform_variants(self, player_id):
        """返回 [{id, uniform_variant_template_id, character_id}]，按 id 顺序。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, uniform_variant_template_id, character_id '
                'FROM uniform_variants WHERE player_id = $1 ORDER BY id', player_id)
            return [dict(r) for r in rows]

    async def create_uniform_variant(self, player_id, uniform_variant_template_id, character_id):
        """创建军服迷彩变体实例（购买），返回含新 id 的 dict。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO uniform_variants (player_id, uniform_variant_template_id, character_id) '
                'VALUES ($1, $2, $3) '
                'RETURNING id, uniform_variant_template_id, character_id',
                player_id, uniform_variant_template_id, character_id)
            return dict(row)

    # ---- uniform parts（头盔/军装部件）----

    async def list_uniform_parts(self, player_id):
        """返回 [{id, character_id, uniform_part_template_id, uniform_part_type}]，按 id 顺序。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, character_id, uniform_part_template_id, uniform_part_type '
                'FROM uniform_parts WHERE player_id = $1 ORDER BY id', player_id)
            return [dict(r) for r in rows]

    async def create_uniform_part(self, player_id, character_id,
                                  uniform_part_template_id, uniform_part_type):
        """创建头盔/军装部件实例（购买），返回含新 id 的 dict。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO uniform_parts '
                '(player_id, character_id, uniform_part_template_id, uniform_part_type) '
                'VALUES ($1, $2, $3, $4) '
                'RETURNING id, character_id, uniform_part_template_id, uniform_part_type',
                player_id, character_id, uniform_part_template_id, uniform_part_type)
            return dict(row)

    async def set_character_headgear(self, player_id, character_id, headgear_id):
        """装备/卸下角色头盔（headgear_id=0 卸下）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE characters SET headgear_id = $1 '
                'WHERE id = $2 AND player_id = $3', headgear_id, character_id, player_id)

    async def get_character_appearance(self, player_id, character_id):
        """角色装备的迷彩/头盔模板 id（SquadMemberSession f8/f19 小队头像用）。

        characters.uniform_variant_id / headgear_id 存的是实例 id，这里换算成
        uniform_variants / uniform_parts 表的模板 id；未装备（0 或实例缺失）
        返回 0，客户端按默认军服渲染。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT (SELECT uniform_variant_template_id FROM uniform_variants '
                'WHERE id = c.uniform_variant_id) AS uvt, '
                '(SELECT uniform_part_template_id FROM uniform_parts '
                'WHERE id = c.headgear_id) AS upt '
                'FROM characters c WHERE c.id = $1 AND c.player_id = $2',
                int(character_id), int(player_id))
        if row is None:
            return (0, 0)
        return (int(row['uvt'] or 0), int(row['upt'] or 0))
