# -*- coding: utf-8 -*-
"""角色与装备的改名/软删/换型/仓库收纳/脱离修改器/教程标记等维护操作。
拆分自 player_store.py（纯移动重构，方法体逐字保留）。
"""
from .base import _rowcount

class MaintenanceMixin:
    """角色与装备的改名/软删/换型/仓库收纳/脱离修改器/教程标记等维护操作。"""

    # -------------------------------------------------- 角色/装备维护（U7）

    async def rename_character(self, player_id, character_id, new_name):
        """角色改名（RenameCharacterRequest）。返回是否更新。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE characters SET name = $3 WHERE id = $1 AND player_id = $2 '
                'AND NOT is_deleted',
                int(character_id), int(player_id), str(new_name or ''))
        return _rowcount(n) > 0

    async def soft_delete_character(self, player_id, character_id):
        """角色软删（DeleteCharacterRequest，character KVCS f14 markedDeleted）。
        最后一个可用角色不允许删除。返回 (ok, active_count)。

        软删与装备退仓（10+ 张表）在同一事务内；锁玩家行防并发删光全部角色。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # 锁玩家行：并发删除两个角色时串行化 count 检查，防止删到 0 个
                locked = await conn.fetchval(
                    'SELECT id FROM players WHERE id = $1 FOR UPDATE', int(player_id))
                if locked is None:
                    return False, 0
                active = await conn.fetchval(
                    'SELECT count(*) FROM characters WHERE player_id = $1 '
                    'AND NOT is_deleted', int(player_id))
                if active is None or int(active) <= 1:
                    return False, int(active or 0)
                n = await conn.execute(
                    'UPDATE characters SET is_deleted = TRUE WHERE id = $1 '
                    'AND player_id = $2 AND NOT is_deleted',
                    int(character_id), int(player_id))
                if _rowcount(n) == 0:
                    return False, int(active)
                # 角色装备全部退回仓库（character_id=0）
                await self._unequip_all_for_character(conn, int(character_id))
        return True, int(active) - 1

    async def _unequip_all_for_character(self, conn, character_id):
        """角色软删时把其名下装备行 character_id 清 0（退回仓库）。"""
        for tbl in ('weapons', 'vehicles', 'weapon_modifiers', 'weapon_ammo',
                    'weapon_variants', 'vehicle_upgrades', 'vehicle_ammo',
                    'vehicle_variants', 'uniform_variants', 'uniform_parts'):
            await conn.execute(
                f'UPDATE {tbl} SET character_id = 0 WHERE character_id = $1',
                int(character_id))
        await conn.execute(
            'UPDATE weapons SET container_id = 0, container_slot = 0 '
            'WHERE character_id = 0', )

    async def set_character_on_leave(self, player_id, character_id, on_leave):
        """角色休假状态（SetCharacterActiveStatusRequest，f24 isOnLeave）。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE characters SET is_on_leave = $3 WHERE id = $1 '
                'AND player_id = $2 AND NOT is_deleted',
                int(character_id), int(player_id), bool(on_leave))
        return _rowcount(n) > 0

    async def count_active_characters(self, player_id):
        """未删除角色数（SetCharacterActiveStatusResponse.soldierMax 同源口径）。"""
        async with self.pool.acquire() as conn:
            return int(await conn.fetchval(
                'SELECT count(*) FROM characters WHERE player_id = $1 '
                'AND NOT is_deleted', int(player_id)) or 0)

    async def change_character_template(self, player_id, character_id, new_template):
        """兵种切换（ChangeCharacterTemplateRequest）：template 换新、
        旧值写 previous_template（character KVCS f19）。返回旧模板或 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'UPDATE characters SET previous_template = template, template = $3 '
                'WHERE id = $1 AND player_id = $2 AND NOT is_deleted '
                'RETURNING previous_template',
                int(character_id), int(player_id), int(new_template))
        return int(row['previous_template']) if row else None

    async def swap_character_head(self, player_id, character_id, head_template_id):
        """换头（SwapHeadRequest，f22 headId；0 = 换回默认头）。返回是否更新。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE characters SET head_id = $3 WHERE id = $1 AND player_id = $2 '
                'AND NOT is_deleted',
                int(character_id), int(player_id), int(head_template_id))
        return _rowcount(n) > 0

    async def rename_weapon(self, player_id, weapon_id, new_name):
        """武器改名（RenameWeaponRequest）。返回是否更新。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE weapons SET name = $3 WHERE id = $1 AND player_id = $2',
                int(weapon_id), int(player_id), str(new_name or ''))
        return _rowcount(n) > 0

    async def set_weapon_tracer_effect(self, player_id, weapon_id, tracer_effect_id,
                                       character_id=0):
        """武器曳光弹效果（SetTracerEffectRequest，官方行模型）。
        tracer_effect_id=0：卸下该武器全部曳光弹行；非 0：绑定该行到武器
        （character_id 跟随武器）并置 is_equipped，同时清该武器其他行的装备位。
        weapons.tracer_effect_id 反规范化列同步维护（战斗配装兜底）。
        返回受影响行 [{id, tracer_template_id, character_id, weapon_id, is_equipped}]。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                affected = []
                if int(tracer_effect_id) == 0:
                    rows = await conn.fetch(
                        'UPDATE tracer_effects SET is_equipped = FALSE '
                        'WHERE weapon_id = $1 AND player_id = $2 AND is_equipped '
                        'RETURNING id, tracer_template_id, character_id, weapon_id, is_equipped',
                        int(weapon_id), int(player_id))
                    affected = [dict(r) for r in rows]
                    await conn.execute(
                        'UPDATE weapons SET tracer_effect_id = 0 '
                        'WHERE id = $1 AND player_id = $2',
                        int(weapon_id), int(player_id))
                    return affected
                # 目标行必须属于该玩家
                row = await conn.fetchrow(
                    'SELECT id, tracer_template_id, character_id, weapon_id, is_equipped '
                    'FROM tracer_effects WHERE id = $1 AND player_id = $2',
                    int(tracer_effect_id), int(player_id))
                if row is None:
                    return None
                # 清同武器其他行的装备位（武器同一时间只有一个曳光弹）
                others = await conn.fetch(
                    'UPDATE tracer_effects SET is_equipped = FALSE '
                    'WHERE weapon_id = $1 AND player_id = $2 AND is_equipped AND id <> $3 '
                    'RETURNING id, tracer_template_id, character_id, weapon_id, is_equipped',
                    int(weapon_id), int(player_id), int(tracer_effect_id))
                affected = [dict(r) for r in others]
                # 绑定目标行：weapon_id + character_id 跟随武器
                bound = await conn.fetchrow(
                    'UPDATE tracer_effects SET weapon_id = $3, character_id = $4, '
                    'is_equipped = TRUE WHERE id = $1 AND player_id = $2 '
                    'RETURNING id, tracer_template_id, character_id, weapon_id, is_equipped',
                    int(tracer_effect_id), int(player_id),
                    int(weapon_id), int(character_id))
                if bound is not None:
                    affected.append(dict(bound))
                await conn.execute(
                    'UPDATE weapons SET tracer_effect_id = $3 '
                    'WHERE id = $1 AND player_id = $2',
                    int(weapon_id), int(player_id), int(tracer_effect_id))
        return affected

    async def update_weapon_condition(self, player_id, weapon_id, condition):
        """修复武器耐久（RepairWeaponRequest）。返回是否更新。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE weapons SET condition = $3 WHERE id = $1 AND player_id = $2',
                int(weapon_id), int(player_id), float(condition))
        return _rowcount(n) > 0

    async def update_weapon_modifier_condition(self, player_id, modifier_id,
                                                condition):
        """修复武器配件耐久（RepairWeaponModifierRequest）。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE weapon_modifiers SET condition = $3 WHERE id = $1 '
                'AND player_id = $2',
                int(modifier_id), int(player_id), float(condition))
        return _rowcount(n) > 0

    async def update_vehicle_condition(self, player_id, vehicle_id, condition):
        """修复载具耐久（RepairVehicleRequest）。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE vehicles SET condition = $3 WHERE id = $1 AND player_id = $2',
                int(vehicle_id), int(player_id), float(condition))
        return _rowcount(n) > 0

    # itemType 字符串（SetInventoryItemToCharacterRequest f1）→ 表名。
    # 'tracereffect'=42：客户端 ad-hoc 值（buildSendItemToSoldierRequest
    # case 42），指派 = tracer_effects.character_id 置角色 id
    _ITEM_TABLES = {
        'weapon': 'weapons', 'vehicle': 'vehicles',
        'uniformvariant': 'uniform_variants', 'uniformpart': 'uniform_parts',
        'weaponmodifier': 'weapon_modifiers', 'weaponammo': 'weapon_ammo',
        'weaponvariant': 'weapon_variants', 'vehicleammo': 'vehicle_ammo',
        'vehicleupgrade': 'vehicle_upgrades', 'vehiclevariant': 'vehicle_variants',
        'tracereffect': 'tracer_effects',
    }
    # ItemType 枚举（SendItemToDepotRequest/SendUpgradesToDepotRequest f1 int）→ 表名
    _ITEM_TYPE_ID_TABLES = {
        1: 'weapons', 2: 'vehicles', 3: 'uniform_parts', 4: 'uniform_variants',
        5: 'weapon_modifiers', 6: 'weapon_ammo', 7: 'weapon_variants',
        8: 'vehicle_upgrades', 9: 'vehicle_ammo', 10: 'vehicle_variants',
        42: 'tracer_effects',
    }
    _ITEM_TYPE_TEMPLATE_COLUMNS = {
        1: 'template_id', 2: 'vehicle_variant_template_id',
        3: 'uniform_part_template_id', 4: 'uniform_variant_template_id',
        5: 'template_id', 6: 'ammo_template_id',
        7: 'weapon_variant_template_id', 8: 'upgrade_template_id',
        9: 'ammo_template_id', 10: 'vehicle_variant_template_id',
        42: 'tracer_template_id',
    }
    # 挂靠件（配件/皮肤/弹药/载具升级与弹药）指派角色时需同步清 0 的外键列。
    # 官方容器归属不变式：客户端 ModifyWeaponMediator 只认
    # characterId==角色 && (weaponId==本枪 || weaponId==0)，指派后仍挂在
    # 仓库枪上的配件在角色改枪窗口里永远不可见（幽灵配件脏数据）。
    _ITEM_DETACH_SETS = {
        'weapon_modifiers': ', weapon_id = 0',
        'weapon_variants': ', weapon_id = 0',
        'weapon_ammo': ', weapon_id = 0',
        'vehicle_upgrades': ', vehicle_id = 0, equipped_slot_id = 0',
        'vehicle_variants': ', vehicle_id = 0',
        'vehicle_ammo': ', vehicle_id = 0, equipped_storage_id = 0',
    }

    async def set_inventory_item_character(self, player_id, item_type, item_id,
                                           character_id):
        """装备/仓库物品指派到角色（SetInventoryItemToCharacterRequest，
        itemType 为小写字符串 'weapon'/'vehicle'/...）。返回是否更新。

        武器/载具指派时挂靠件（弹药/皮肤/配件/升级）均不随行
        （2026-09-13 改造：入库即全部解绑，可与武器/载具分别发回士兵；
        此处对历史脏数据兜底脱离挂靠，防幽灵挂靠件）。"""
        tbl = self._ITEM_TABLES.get(str(item_type or '').strip().lower())
        if tbl is None:
            return False
        async with self.pool.acquire() as conn:
            if int(character_id or 0) > 0:
                owns_character = await conn.fetchval(
                    'SELECT EXISTS(SELECT 1 FROM characters WHERE id = $1 '
                    'AND player_id = $2 AND NOT is_deleted)',
                    int(character_id), int(player_id))
                if not owns_character:
                    return False
            if tbl == 'weapons':
                n = await conn.execute(
                    f'UPDATE {tbl} SET character_id = $3 WHERE id = $1 '
                    'AND player_id = $2',
                    int(item_id), int(player_id), int(character_id))
                # 弹药不随武器指派（2026-09-13 改造：入库即解绑，可与武器
                # 分别发回士兵；此处对历史脏数据兜底脱离挂靠——否则指派后
                # 挂在新角色名下武器上的弹药，其 uid 计入 unlockBits 推导）
                await conn.execute(
                    'UPDATE weapon_ammo SET weapon_id = 0 '
                    'WHERE weapon_id = $1 AND player_id = $2',
                    int(item_id), int(player_id))
                # 配件不随武器指派（同改造语义；兜底脱离挂靠防幽灵配件）
                await conn.execute(
                    'UPDATE weapon_modifiers SET weapon_id = 0 '
                    'WHERE weapon_id = $1 AND player_id = $2',
                    int(item_id), int(player_id))
                # 皮肤不随武器指派（同改造语义；兜底脱离挂靠）+
                # weapons.variant_id 反向引用复位（武器不得指向留仓皮肤）
                await conn.execute(
                    'UPDATE weapon_variants SET weapon_id = 0 '
                    'WHERE weapon_id = $1 AND player_id = $2',
                    int(item_id), int(player_id))
                await conn.execute(
                    'UPDATE weapons SET variant_id = 0 WHERE id = $1 '
                    'AND player_id = $2',
                    int(item_id), int(player_id))
            elif tbl == 'vehicles':
                n = await conn.execute(
                    f'UPDATE {tbl} SET character_id = $3 WHERE id = $1 '
                    'AND player_id = $2',
                    int(item_id), int(player_id), int(character_id))
                # 升级不随载具指派（2026-09-13 改造：入库即解绑，可与载具
                # 分别发回士兵；兜底脱离挂靠防幽灵升级件）
                await conn.execute(
                    'UPDATE vehicle_upgrades SET vehicle_id = 0 '
                    'WHERE vehicle_id = $1 AND player_id = $2',
                    int(item_id), int(player_id))
                # 弹药不随载具指派（同改造语义；兜底脱离挂靠，防幽灵弹药）
                await conn.execute(
                    'UPDATE vehicle_ammo SET vehicle_id = 0, '
                    'equipped_storage_id = 0 WHERE vehicle_id = $1 '
                    'AND player_id = $2',
                    int(item_id), int(player_id))
                # 皮肤不随载具指派（同改造语义；兜底脱离挂靠）+
                # vehicles.variant_id 反向引用复位（载具不得指向留仓皮肤）
                await conn.execute(
                    'UPDATE vehicle_variants SET vehicle_id = 0 '
                    'WHERE vehicle_id = $1 AND player_id = $2',
                    int(item_id), int(player_id))
                await conn.execute(
                    'UPDATE vehicles SET variant_id = 0 WHERE id = $1 '
                    'AND player_id = $2',
                    int(item_id), int(player_id))
            elif tbl == 'tracer_effects':
                # 曳光弹指派角色：character_id 置目标角色；若此前装备在某
                # 武器上需解除绑定（武器不得引用别的角色名下的曳光弹行，
                # 官方容器归属不变式），weapons.tracer_effect_id 脏引用同步
                # 复位（对齐 weapon_variants 指派时复位 weapons.variant_id）
                n = await conn.execute(
                    'UPDATE tracer_effects SET character_id = $3, weapon_id = 0, '
                    'is_equipped = FALSE WHERE id = $1 AND player_id = $2',
                    int(item_id), int(player_id), int(character_id))
                await conn.execute(
                    'UPDATE weapons SET tracer_effect_id = 0 '
                    'WHERE tracer_effect_id = $1 AND player_id = $2',
                    int(item_id), int(player_id))
            else:
                # 挂靠件指派角色 → 同步脱离原容器（weapon_id/vehicle_id 清 0），
                # 详见 _ITEM_DETACH_SETS 注释（官方容器归属不变式）
                detach = self._ITEM_DETACH_SETS.get(tbl, '')
                n = await conn.execute(
                    f'UPDATE {tbl} SET character_id = $3{detach} '
                    'WHERE id = $1 AND player_id = $2',
                    int(item_id), int(player_id), int(character_id))
                # 皮肤实例脱离武器后，引用它的武器行同步复位默认皮肤
                #（对齐 send_upgrades_to_depot 载具分支 variant_id 复位语义）
                if tbl == 'weapon_variants':
                    await conn.execute(
                        'UPDATE weapons SET variant_id = 0 WHERE variant_id = $1 '
                        'AND player_id = $2',
                        int(item_id), int(player_id))
        return _rowcount(n) > 0

    async def get_item_character(self, player_id, item_type, object_id):
        """查询物品当前所属角色 id（SendItemToDepot 幂等预检用，
        itemType 为 ItemType 枚举 int）。物品不存在返回 None，已在仓库返回 0。"""
        tbl = self._ITEM_TYPE_ID_TABLES.get(int(item_type or 0))
        if tbl is None:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f'SELECT character_id FROM {tbl} WHERE id = $1 AND player_id = $2',
                int(object_id), int(player_id))
            return row['character_id'] if row else None

    async def get_item_template(self, player_id, item_type, object_id):
        """Return the owned item's pricing object id, or None."""
        kind = int(item_type or 0)
        tbl = self._ITEM_TYPE_ID_TABLES.get(kind)
        column = self._ITEM_TYPE_TEMPLATE_COLUMNS.get(kind)
        if tbl is None or column is None:
            return None
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                f'SELECT {column} FROM {tbl} WHERE id = $1 AND player_id = $2',
                int(object_id), int(player_id))

    async def send_item_to_depot(self, player_id, item_type, object_id):
        """物品送回仓库（SendItemToDepotRequest，itemType 为 ItemType 枚举 int）：
        character_id 清 0；武器同时卸下容器槽，载具同时卸下装备槽
        （否则装备中的载具入库会留下"仓库里却占着角色槽位"的脏数据）。返回是否更新。"""
        tbl = self._ITEM_TYPE_ID_TABLES.get(int(item_type or 0))
        if tbl is None:
            return False
        async with self.pool.acquire() as conn:
            if tbl == 'weapons':
                n = await conn.execute(
                    'UPDATE weapons SET character_id = 0, container_id = 0, '
                    'container_slot = 0 WHERE id = $1 AND player_id = $2',
                    int(object_id), int(player_id))
                # 弹药随武器退仓并解绑（weapon_id 一并清 0 → 独立仓库弹药，
                # 可与武器分别发回士兵，2026-09-13 改造；slot_index 保留，
                # 重新挂靠后恢复默认弹药装备槽；unlockBits 按 character_id
                # 推导，解绑后不挂任何角色名下）
                await conn.execute(
                    'UPDATE weapon_ammo SET character_id = 0, weapon_id = 0 '
                    'WHERE weapon_id = $1 AND player_id = $2',
                    int(object_id), int(player_id))
                # 配件随武器退仓并解绑（weapon_id 一并清 0 → 独立仓库配件，
                # 可与武器分别发回士兵；2026-09-13 改造：此前只清 character_id
                # 保留挂靠，出库指派时配件被武器无差别带走。兵种限制不因此
                # 丢失——挂接时点 setweaponmodifierrequest 按 _mod_usable_by_class
                # 校验）
                await conn.execute(
                    'UPDATE weapon_modifiers SET character_id = 0, weapon_id = 0 '
                    'WHERE weapon_id = $1 AND player_id = $2',
                    int(object_id), int(player_id))
                # 皮肤随武器退仓并解绑（weapon_id 清 0 → 独立仓库皮肤）+
                # weapons.variant_id 反向引用复位（否则武器行指向已独立
                # 皮肤，客户端显示不一致）
                await conn.execute(
                    'UPDATE weapon_variants SET character_id = 0, weapon_id = 0 '
                    'WHERE weapon_id = $1 AND player_id = $2',
                    int(object_id), int(player_id))
                await conn.execute(
                    'UPDATE weapons SET variant_id = 0 WHERE id = $1 '
                    'AND player_id = $2',
                    int(object_id), int(player_id))
            elif tbl == 'vehicles':
                n = await conn.execute(
                    'UPDATE vehicles SET character_id = 0, equipped_in_slot = 0 '
                    'WHERE id = $1 AND player_id = $2',
                    int(object_id), int(player_id))
                # 升级随载具退仓并解绑（vehicle_id 一并清 0 → 独立仓库
                # 升级件，可与载具分别发回士兵，2026-09-13 改造；
                # equipped_slot_id 保留，重新挂靠后恢复槽位）
                await conn.execute(
                    'UPDATE vehicle_upgrades SET character_id = 0, vehicle_id = 0 '
                    'WHERE vehicle_id = $1 AND player_id = $2',
                    int(object_id), int(player_id))
                # 弹药随载具退仓并解绑（vehicle_id/equipped_storage_id 一并
                # 清 0 → 独立仓库弹药，可与载具分别发回士兵，对齐武器配件
                # 2026-09-13 改造；slot_index 保留，重新指派后可恢复装备位）
                await conn.execute(
                    'UPDATE vehicle_ammo SET character_id = 0, vehicle_id = 0, '
                    'equipped_storage_id = 0 WHERE vehicle_id = $1 '
                    'AND player_id = $2',
                    int(object_id), int(player_id))
                # 皮肤随载具退仓并解绑（vehicle_id 清 0 → 独立仓库皮肤）+
                # vehicles.variant_id 反向引用复位（否则载具行指向已独立
                # 皮肤，客户端显示不一致）
                await conn.execute(
                    'UPDATE vehicle_variants SET character_id = 0, vehicle_id = 0 '
                    'WHERE vehicle_id = $1 AND player_id = $2',
                    int(object_id), int(player_id))
                await conn.execute(
                    'UPDATE vehicles SET variant_id = 0 WHERE id = $1 '
                    'AND player_id = $2',
                    int(object_id), int(player_id))
            elif tbl == 'tracer_effects':
                # 曳光弹退仓：解除武器绑定与装备位，weapons.tracer_effect_id
                # 脏引用同步复位（否则战斗配装仍读到已回仓库的曳光弹）
                wref = await conn.fetchval(
                    'SELECT weapon_id FROM tracer_effects '
                    'WHERE id = $1 AND player_id = $2',
                    int(object_id), int(player_id))
                n = await conn.execute(
                    'UPDATE tracer_effects SET character_id = 0, weapon_id = 0, '
                    'is_equipped = FALSE WHERE id = $1 AND player_id = $2',
                    int(object_id), int(player_id))
                if wref:
                    await conn.execute(
                        'UPDATE weapons SET tracer_effect_id = 0 '
                        'WHERE id = $1 AND player_id = $2 AND tracer_effect_id = $3',
                        int(wref), int(player_id), int(object_id))
            else:
                n = await conn.execute(
                    f'UPDATE {tbl} SET character_id = 0 WHERE id = $1 '
                    'AND player_id = $2',
                    int(object_id), int(player_id))
        return _rowcount(n) > 0

    async def send_upgrades_to_depot(self, player_id, item_type, object_id):
        """卸下容器上挂的升级件（SendUpgradesToDepotRequest）。

        客户端 ItemInfoOverlayMediator.showSendUpgradesToDepotButtonIfNeeded：
        物品已在仓库时，武器 isModded / 载具 hasUpgradeEquipped 或非默认皮肤
        → 显示"卸下升级件"按钮，请求的 itemType 是【容器类型】（1=weapon
        2=vehicle）、objectId 是【容器实例 id】：
        - 武器：weapon_modifiers.weapon_id 清 0（配件回仓库，character_id 同步清 0）；
        - 载具：vehicle_upgrades 卸下（vehicle_id/equipped_slot_id 清 0）、
          vehicles.variant_id 复位默认皮肤、vehicle_variants.character_id 清 0。
        返回受影响行数（0 = 容器上没有可卸下的升级件/皮肤）。"""
        if int(item_type or 0) == 1:
            async with self.pool.acquire() as conn:
                n = await conn.execute(
                    'UPDATE weapon_modifiers SET weapon_id = 0, character_id = 0 '
                    'WHERE weapon_id = $1 AND player_id = $2',
                    int(object_id), int(player_id))
            return _rowcount(n)
        if int(item_type or 0) == 2:
            async with self.pool.acquire() as conn:
                n = await conn.execute(
                    'UPDATE vehicle_upgrades SET vehicle_id = 0, '
                    'equipped_slot_id = 0, character_id = 0 '
                    'WHERE vehicle_id = $1 AND player_id = $2',
                    int(object_id), int(player_id))
                m = await conn.execute(
                    'UPDATE vehicles SET variant_id = 0 '
                    'WHERE id = $1 AND player_id = $2 AND variant_id != 0',
                    int(object_id), int(player_id))
                await conn.execute(
                    'UPDATE vehicle_variants SET character_id = 0 '
                    'WHERE vehicle_id = $1 AND player_id = $2',
                    int(object_id), int(player_id))
            return _rowcount(n) + _rowcount(m)
        return 0

    async def detach_weapon_modifiers(self, player_id, modifier_ids):
        """按实例 id 批量卸下武器配件（指派武器到不相容兵种时剥离留仓库）：
        weapon_id/character_id 清 0 → 仓库独立配件（客户端 WeaponDepotView
        getDepotMods 按 characterId.isZero 收录）。返回受影响行数。"""
        ids = [int(i) for i in (modifier_ids or []) if int(i) > 0]
        if not ids:
            return 0
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE weapon_modifiers SET weapon_id = 0, character_id = 0 '
                'WHERE id = ANY($1::bigint[]) AND player_id = $2',
                ids, int(player_id))
        return _rowcount(n)

    async def mark_depot_item_viewed(self, player_id, tab_name, item_id):
        """仓库物品已读（SetDepotItemViewedRequest）。
        UPDATE 带 item_type 条件（唯一索引 uq_depot_items 按
        (player_id, item_type, item_id)，不带会跨 tab 误标）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO depot_items (player_id, item_type, item_id, viewed) '
                'VALUES ($1, $2, $3, TRUE) '
                'ON CONFLICT (player_id, item_type, item_id) DO NOTHING',
                int(player_id), str(tab_name or ''), int(item_id))
            await conn.execute(
                'UPDATE depot_items SET viewed = TRUE WHERE player_id = $1 '
                'AND item_type = $2 AND item_id = $3',
                int(player_id), str(tab_name or ''), int(item_id))
        return True

    async def mark_depot_item_new(self, player_id, tab_name, item_id):
        """物品入仓标记"新"（SendItemToDepot 后调用；客户端 ItemInfoOverlay
        setInfo 对 isNewItem 的物品发 SetDepotItemViewed 消点）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO depot_items (player_id, item_type, item_id, viewed) '
                'VALUES ($1, $2, $3, FALSE) '
                'ON CONFLICT (player_id, item_type, item_id) '
                'DO UPDATE SET viewed = FALSE',
                int(player_id), str(tab_name or ''), int(item_id))
        return True

    async def list_depot_new_items(self, player_id):
        """未读仓库物品 → {tab_name: [item_id,...]}（登录回放 DepotNewItem 行用）。
        物品行已被物理删除（如武器实例清理）时对应条目自动失效，客户端
        checkIsItemNew 查不到物品即无红点，无需服务端清理。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT item_type, item_id FROM depot_items '
                'WHERE player_id = $1 AND viewed = FALSE ORDER BY id',
                int(player_id))
        out = {}
        for r in rows:
            out.setdefault(str(r['item_type']), []).append(int(r['item_id']))
        return out

    async def mark_tutorial_triggered(self, player_id, tutorial_template_id):
        """教程已触发（MarkTutorialTriggeredRequest，幂等）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO player_tutorials_triggered '
                '(player_id, tutorial_template_id) VALUES ($1, $2) '
                'ON CONFLICT DO NOTHING',
                int(player_id), int(tutorial_template_id))
        return True

    async def list_tutorials_triggered(self, player_id):
        """已触发教程模板 id 列表（Tutorial KVCS 行数据源）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT tutorial_template_id FROM player_tutorials_triggered '
                'WHERE player_id = $1', int(player_id))
        return sorted(int(r['tutorial_template_id']) for r in rows)

    async def mark_character_tutorial(self, character_id, tutorial_id,
                                      completed=True):
        """战斗内教程上报（combat TutorialRequest）→ character_tutorials。

        幂等：重复上报只补 completed（triggered 首报即置位）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO character_tutorials '
                '(character_id, tutorial_id, triggered, completed) '
                'VALUES ($1, $2, TRUE, $3) '
                'ON CONFLICT (character_id, tutorial_id) '
                'DO UPDATE SET completed = character_tutorials.completed OR $3',
                int(character_id), int(tutorial_id), bool(completed))
        return True

    async def set_player_language(self, player_id, language_code):
        """玩家语言（SetPlayerLanguageRequest）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE players SET language_code = $2 WHERE id = $1',
                int(player_id), str(language_code or 'en')[:8])
        return True

    async def set_player_discord_id(self, player_id, discord_id):
        """绑定 Discord id（SetPlayerDiscordIdRequest）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE players SET discord_id = $2 WHERE id = $1',
                int(player_id), str(discord_id or ''))
        return True

    async def set_player_steam_id(self, player_id, steam_id):
        """绑定 Steam id（SetPlayerSteamIdRequest）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE players SET steam_id = $2 WHERE id = $1',
                int(player_id), str(steam_id or ''))
        return True

    async def set_equipment_auto_repair(self, player_id, item_type, item_id,
                                        enabled):
        """装备自动维修开关（Set*AutoRepairRequest 族）。
        item_type 为 ItemType 枚举 int（1-10）；enabled 为三态 int
        （0=关 1=金币 2=金条）。返回是否更新。"""
        tbl = self._ITEM_TYPE_ID_TABLES.get(int(item_type or 0))
        if tbl is None:
            return False
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                f'UPDATE {tbl} SET auto_repair = $3 WHERE id = $1 AND player_id = $2',
                int(item_id), int(player_id), int(enabled or 0))
        return _rowcount(n) > 0

    async def set_weapon_modifier_weapon(self, player_id, modifier_id, weapon_id,
                                         character_id=None):
        """配件装备/卸下（SetWeaponModifierRequest）：
        装备 → weapon_id=武器实例 + character_id 同步为该武器归属角色；
        卸下（weapon_id=0）→ weapon_id=0。返回是否更新。"""
        async with self.pool.acquire() as conn:
            if weapon_id:
                n = await conn.execute(
                    'UPDATE weapon_modifiers SET weapon_id = $3 '
                    'WHERE id = $1 AND player_id = $2',
                    int(modifier_id), int(player_id), int(weapon_id))
                if character_id is not None:
                    await conn.execute(
                        'UPDATE weapon_modifiers SET character_id = $3 '
                        'WHERE id = $1 AND player_id = $2',
                        int(modifier_id), int(player_id), int(character_id))
            else:
                n = await conn.execute(
                    'UPDATE weapon_modifiers SET weapon_id = 0 '
                    'WHERE id = $1 AND player_id = $2',
                    int(modifier_id), int(player_id))
        return _rowcount(n) > 0

    async def get_character_any(self, player_id, character_id):
        """按 id 取单个角色行（含软删角色，DeleteCharacterRequest 后重推
        markedDeleted=1 用）。返回 dict 或 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, name, faction, template, combat_xp, combat_badge_0, '
                'combat_badge_1, uniform_variant_id, headgear_id, is_deleted, '
                'is_on_leave, previous_template, head_id '
                'FROM characters WHERE id = $1 AND player_id = $2',
                int(character_id), int(player_id))
        return dict(row) if row else None

    async def reset_player_progress(self, player_id):
        """重置玩家进度（ResetPlayerRequest）：删角色（连带装备表清空该玩家行）、
        勋带经验、统计、教程触发记录；保留账号与货币。返回删除的角色数。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                deleted = int(await conn.fetchval(
                    'SELECT count(*) FROM characters WHERE player_id = $1 '
                    'AND NOT is_deleted', int(player_id)) or 0)
                await conn.execute(
                    'DELETE FROM characters WHERE player_id = $1', int(player_id))
                for tbl in ('weapons', 'vehicles', 'weapon_modifiers',
                            'weapon_ammo', 'weapon_variants', 'vehicle_upgrades',
                            'vehicle_ammo', 'vehicle_variants', 'uniform_variants',
                            'uniform_parts', 'depot_items',
                            'player_tutorials_triggered'):
                    await conn.execute(
                        f'DELETE FROM {tbl} WHERE player_id = $1', int(player_id))
                await conn.execute(
                    'DELETE FROM squad_members WHERE character_id NOT IN '
                    '(SELECT id FROM characters)')
        return deleted
