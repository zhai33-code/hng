# -*- coding: utf-8 -*-
"""武器/武器变体/曳光/配枪/弹药/修改器。
拆分自 player_store.py（纯移动重构，方法体逐字保留）。
"""
from .base import _rowcount

class EquipmentWeaponsMixin:
    """武器/武器变体/曳光/配枪/弹药/修改器。"""

    # ---- weapon variants（武器皮肤变体）----

    async def list_weapon_variants(self, player_id):
        """返回 [{id, weapon_id, character_id, weapon_variant_template_id}]，按 id 顺序。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, weapon_id, character_id, weapon_variant_template_id '
                'FROM weapon_variants WHERE player_id = $1 ORDER BY id', player_id)
            return [dict(r) for r in rows]

    async def create_weapon_variant(self, player_id, weapon_id, character_id,
                                    weapon_variant_template_id):
        """创建武器皮肤变体实例（购买），返回含新 id 的 dict。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO weapon_variants '
                '(player_id, weapon_id, character_id, weapon_variant_template_id) '
                'VALUES ($1, $2, $3, $4) '
                'RETURNING id, weapon_id, character_id, weapon_variant_template_id',
                player_id, weapon_id, character_id, weapon_variant_template_id)
            return dict(row)

    async def bind_weapon_variant(self, player_id, variant_id, weapon_id, character_id):
        """绑定武器皮肤 depot 行到武器（SetWeaponVariantRequest 装备仓库皮肤）。
        行必须属于该玩家且 weapon_id=0（仓库行）；绑定后 character_id 跟随武器，
        客户端 isWeaponVariantOwned 要求 characterId==选中角色。返回更新后行或 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'UPDATE weapon_variants SET weapon_id = $3, character_id = $4 '
                'WHERE id = $1 AND player_id = $2 AND weapon_id = 0 '
                'RETURNING id, weapon_id, character_id, weapon_variant_template_id',
                int(variant_id), int(player_id), int(weapon_id), int(character_id))
            return dict(row) if row else None

    # ---- tracer effects（曳光弹）----

    async def list_tracer_effects(self, player_id):
        """返回 [{id, tracer_template_id, character_id, weapon_id, is_equipped}]，按 id 顺序。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, tracer_template_id, character_id, weapon_id, is_equipped '
                'FROM tracer_effects WHERE player_id = $1 ORDER BY id', player_id)
            return [dict(r) for r in rows]

    async def create_tracer_effect(self, player_id, tracer_template_id,
                                   character_id=0, weapon_id=0):
        """创建曳光弹效果实例（通行证奖励发放），返回含新 id 的 dict。
        官方发放 target='depot'：character_id=0 的仓库行，装备时才绑定武器。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO tracer_effects '
                '(player_id, tracer_template_id, character_id, weapon_id) '
                'VALUES ($1, $2, $3, $4) '
                'RETURNING id, tracer_template_id, character_id, weapon_id, is_equipped',
                int(player_id), int(tracer_template_id),
                int(character_id), int(weapon_id))
            return dict(row)

    async def get_tracer_effect(self, player_id, tracer_id):
        """按 id 查询单个曳光弹实例，未找到返回 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, tracer_template_id, character_id, weapon_id, is_equipped '
                'FROM tracer_effects WHERE id = $1 AND player_id = $2',
                int(tracer_id), int(player_id))
            return dict(row) if row else None

    async def get_weapon(self, player_id, weapon_id):
        """按 id 查询单个武器，未找到返回 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, name, template_id, character_id, container_id, container_slot, '
                'variant_template_id, variant_id, condition, clip_count, '
                'auto_repair, tracer_effect_id '
                'FROM weapons WHERE id = $1 AND player_id = $2', weapon_id, player_id)
            return dict(row) if row else None

    async def delete_weapon(self, player_id, weapon_id):
        """删除武器实例及关联弹药/配件/皮肤（清理历史 bug 产生的 template_id=0 假武器）。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    'DELETE FROM weapon_ammo WHERE weapon_id = $1 AND player_id = $2',
                    weapon_id, player_id)
                await conn.execute(
                    'DELETE FROM weapon_modifiers WHERE weapon_id = $1 AND player_id = $2',
                    weapon_id, player_id)
                await conn.execute(
                    'DELETE FROM weapon_variants WHERE weapon_id = $1 AND player_id = $2',
                    weapon_id, player_id)
                await conn.execute(
                    'DELETE FROM weapons WHERE id = $1 AND player_id = $2', weapon_id, player_id)

    # ---- weapons ----

    async def list_weapons(self, player_id, character_id=None):
        """返回 [{id, name, template_id, character_id, container_id, container_slot,
        variant_template_id, variant_id, condition, clip_count, auto_repair}]，按 id 顺序。
        character_id 非空时只返回该角色名下的武器（character_id=0 的仓库物品
        不属于任何角色，不返回）——解锁位图等按角色语义的调用必须传此参数。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, name, template_id, character_id, container_id, container_slot, '
                'variant_template_id, variant_id, condition, clip_count, auto_repair '
                'FROM weapons WHERE player_id = $1 '
                'AND ($2::bigint IS NULL OR character_id = $2) ORDER BY id',
                player_id, character_id)
            return [dict(r) for r in rows]

    async def set_weapon_clip_count(self, player_id, weapon_id, clip_count):
        """持久化武器弹夹三态选择（SetWeaponClipCountRequest；
        负=最少 / 0=默认 / 正=最大）。返回更新后的行，未找到返回 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'UPDATE weapons SET clip_count = $3 '
                'WHERE id = $1 AND player_id = $2 '
                'RETURNING id, name, template_id, character_id, container_id, '
                'container_slot, variant_template_id, variant_id, condition, clip_count',
                weapon_id, player_id, clip_count)
            return dict(row) if row else None

    async def create_weapon(self, player_id, name, template_id, character_id,
                            variant_template_id=0):
        """创建武器实例，返回含新 id 的 dict。clip_count 默认 0（默认弹夹档）。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO weapons (player_id, name, template_id, character_id, '
                'variant_template_id) VALUES ($1, $2, $3, $4, $5) '
                'RETURNING id, name, template_id, character_id, container_id, '
                'container_slot, variant_template_id, variant_id, condition, clip_count',
                player_id, name, template_id, character_id, variant_template_id)
            return dict(row)

    async def set_weapon_variant(self, player_id, weapon_id, variant_id):
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE weapons SET variant_id = $1 '
                'WHERE id = $2 AND player_id = $3', variant_id, weapon_id, player_id)

    async def update_weapon_container(self, player_id, weapon_id,
                                      container_id, container_slot):
        """装备（container_id=角色id, slot=1-4）或卸下（0, 0）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE weapons SET container_id = $1, container_slot = $2 '
                'WHERE id = $3 AND player_id = $4',
                container_id, container_slot, weapon_id, player_id)

    async def set_weapon_equipment(self, player_id, weapon_id,
                                   container_id, container_slot):
        """装备武器到角色槽位（container_slot>0）或卸下（0, 0）。返回受影响武器
        列表（含被挤掉同槽位的其他武器，与 set_vehicle_equipment 同构）。

        客户端替换槽位武器时只发 set_equipmentselection（新武器），不发
        remove_equipmentselection（旧武器）——服务端必须把同容器同槽位的
        旧武器挤回军械库（container 归零）并推送其 KVCS，否则旧武器既不在
        军械库列表（container_slot 仍非 0）也不在槽位显示，且装备点数
        按新旧两把重复计算。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                if int(container_slot) > 0:
                    owns_character = await conn.fetchval(
                        'SELECT EXISTS(SELECT 1 FROM characters WHERE id = $1 '
                        'AND player_id = $2 AND NOT is_deleted)',
                        int(container_id), int(player_id))
                    if not owns_character:
                        return []
                row = await conn.fetchrow(
                    'SELECT id, name, template_id, character_id, container_id, '
                    'container_slot, variant_template_id, variant_id, condition, '
                    'clip_count, auto_repair FROM weapons '
                    'WHERE id = $1 AND player_id = $2',
                    weapon_id, player_id)
                if row is None:
                    return []
                affected = [dict(row)]
                if int(container_slot) > 0:
                    # 挤掉同容器同槽位的其他武器（一个槽位只能一把）
                    prev = await conn.fetch(
                        'SELECT id, name, template_id, character_id, container_id, '
                        'container_slot, variant_template_id, variant_id, condition, '
                        'clip_count, auto_repair FROM weapons '
                        'WHERE player_id = $1 AND container_id = $2 '
                        'AND container_slot = $3 AND id <> $4',
                        player_id, container_id, int(container_slot), weapon_id)
                    for p in prev:
                        await conn.execute(
                            'UPDATE weapons SET container_id = 0, container_slot = 0 '
                            'WHERE id = $1', p['id'])
                        # asyncpg Record 不可变，须先转 dict 副本再改写
                        pd = dict(p)
                        pd['container_id'] = 0
                        pd['container_slot'] = 0
                        affected.append(pd)
                await conn.execute(
                    'UPDATE weapons SET container_id = $1, container_slot = $2 '
                    'WHERE id = $3 AND player_id = $4',
                    container_id, container_slot, weapon_id, player_id)
            # 返回最新状态（affected[0] 是更新前的旧值，须回读）
            ids = [a['id'] for a in affected]
            rows = await conn.fetch(
                'SELECT id, name, template_id, character_id, container_id, '
                'container_slot, variant_template_id, variant_id, condition, '
                'clip_count, auto_repair FROM weapons '
                'WHERE id = ANY($1::bigint[]) ORDER BY id', ids)
            return [dict(r) for r in rows]

    # ---- weapon modifiers ----

    async def list_weapon_modifiers(self, player_id, character_id=None):
        """返回 [{id, template_id, character_id, weapon_id, condition, auto_repair}]，按 id 顺序。
        character_id 非空时只返回该角色名下的配件。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, template_id, character_id, weapon_id, condition, auto_repair '
                'FROM weapon_modifiers WHERE player_id = $1 '
                'AND ($2::bigint IS NULL OR character_id = $2) ORDER BY id',
                player_id, character_id)
            return [dict(r) for r in rows]

    async def create_weapon_modifier(self, player_id, template_id, character_id, weapon_id):
        """创建武器配件实例，返回含新 id 的 dict。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO weapon_modifiers (player_id, template_id, character_id, weapon_id) '
                'VALUES ($1, $2, $3, $4) '
                'RETURNING id, template_id, character_id, weapon_id, condition',
                player_id, template_id, character_id, weapon_id)
            return dict(row)

    async def clear_weapon_modifier_category(self, player_id, weapon_id,
                                             template_ids, exclude_modifier_id):
        """卸下指定武器上与 exclude 同槽位（分类）的其他配件（互斥装备）。

        分类不在表内：调用方按 WarDatabase WeaponModifierTemplate.f28
        （weaponModCategoryId）算出同槽位的模板 id 列表传入。返回受影响行数。"""
        if not template_ids:
            return 0
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE weapon_modifiers SET weapon_id = 0 '
                'WHERE player_id = $1 AND weapon_id = $2 AND id != $3 '
                'AND template_id = ANY($4::bigint[])',
                int(player_id), int(weapon_id), int(exclude_modifier_id),
                [int(t) for t in template_ids])
            return _rowcount(n)

    # ---- weapon ammo ----

    async def list_weapon_ammo(self, player_id, character_id=None):
        """返回 [{id, ammo_template_id, character_id, weapon_id, slot_index, condition,
        auto_repair}]，按 id 顺序。character_id 非空时只返回该角色名下的弹药。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, ammo_template_id, character_id, weapon_id, slot_index, condition, '
                'auto_repair FROM weapon_ammo WHERE player_id = $1 '
                'AND ($2::bigint IS NULL OR character_id = $2) ORDER BY id',
                player_id, character_id)
            return [dict(r) for r in rows]

    async def create_weapon_ammo(self, player_id, ammo_template_id, character_id,
                                 weapon_id, slot_index=0, condition=0):
        """创建弹药实例（购买/默认弹药），返回含新 id 的 dict。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO weapon_ammo (player_id, ammo_template_id, character_id, weapon_id, slot_index, condition) '
                'VALUES ($1, $2, $3, $4, $5, $6) '
                'RETURNING id, ammo_template_id, character_id, weapon_id, slot_index, condition',
                player_id, ammo_template_id, character_id, weapon_id, slot_index, condition)
            return dict(row)

    async def update_weapon_ammo_slot(self, player_id, ammo_id, slot_index):
        """装备（slot>0）或卸下（slot=0）弹药。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE weapon_ammo SET slot_index = $1 '
                'WHERE id = $2 AND player_id = $3', slot_index, ammo_id, player_id)

    async def clear_weapon_ammo_slots(self, player_id, weapon_id, exclude_ammo_id):
        """清空指定武器上除 exclude_ammo_id 外所有弹药的槽位（互斥装备）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE weapon_ammo SET slot_index = 0 '
                'WHERE player_id = $1 AND weapon_id = $2 AND id != $3',
                player_id, weapon_id, exclude_ammo_id)

    async def update_weapon_ammo_condition(self, player_id, ammo_id, condition):
        """修复弹药：更新 condition（耐久）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE weapon_ammo SET condition = $1 '
                'WHERE id = $2 AND player_id = $3', condition, ammo_id, player_id)
