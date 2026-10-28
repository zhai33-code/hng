# -*- coding: utf-8 -*-
"""武器维护价格与配件归属缓存回归测试（纯单元，无需数据库）。"""
import asyncio

from protocol import pb_int32, pb_int64
import handlers.career as career
import handlers.weapons as weapons
import player_service
import wardb


class _Writer:
    def __init__(self):
        self.buf = bytearray()

    def write(self, data):
        self.buf.extend(data)

    async def drain(self):
        pass


class _Session:
    soldier_view_cmd = None
    playerview_cmd = None

    def __init__(self):
        self.weapons = {}

    async def ensure_player(self):
        return 7


class _MaintenanceStore:
    def __init__(self):
        self.weapon = {
            'id': 101, 'name': 'Thompson', 'template_id': 32,
            'character_id': 70, 'container_id': 70, 'container_slot': 1,
            'variant_id': 0, 'clip_count': 0, 'auto_repair': 0,
            'condition': 0.75,
        }
        self.ammo = [{
            'id': 201, 'ammo_template_id': 46, 'character_id': 70,
            'weapon_id': 101, 'slot_index': 1, 'auto_repair': 0,
            'condition': 750.0,
        }]
        self.mods = [{
            'id': 301, 'template_id': 32, 'character_id': 70,
            'weapon_id': 101, 'auto_repair': 0, 'condition': 0.5,
        }]

    async def get_weapon(self, _pid, wid):
        return dict(self.weapon) if wid == self.weapon['id'] else None

    async def list_weapon_ammo(self, _pid):
        return [dict(x) for x in self.ammo]

    async def list_weapon_modifiers(self, _pid):
        return [dict(x) for x in self.mods]

    async def update_weapon_condition(self, _pid, _wid, value):
        self.weapon['condition'] = value
        return True

    async def update_weapon_ammo_condition(self, _pid, _aid, value):
        self.ammo[0]['condition'] = value
        return True

    async def update_weapon_modifier_condition(self, _pid, _mid, value):
        self.mods[0]['condition'] = value
        return True


def test_manual_maintenance_uses_client_damage_fraction(monkeypatch):
    """三种手动维修都必须按客户端显示的缺损比例扣款并修到模板上限。"""
    store = _MaintenanceStore()
    calls = []

    async def charge(_sess, pricing_id, currency, conn=None, fraction=1.0,
                     **_kwargs):
        calls.append((pricing_id, currency, fraction))
        return 1, True

    async def noop(*_args, **_kwargs):
        pass

    monkeypatch.setattr(weapons, 'STORE', store)
    monkeypatch.setattr(weapons, '_charge_player', charge)
    monkeypatch.setattr(weapons, '_push_player_refresh', noop)
    monkeypatch.setattr(career, 'complete_order_and_push', noop)

    async def run():
        sess = _Session()
        writer = _Writer()
        await weapons.handle_repairweaponrequest(
            sess, writer, 1, 'RepairWeaponRequest',
            pb_int64(2, 101) + pb_int32(3, 1) +
            pb_int64(4, 4033) + pb_int32(6, 1))
        await weapons.handle_repairweaponammorequest(
            sess, writer, 2, 'RepairWeaponAmmoRequest',
            pb_int64(1, 201) + pb_int32(2, 1000) +
            pb_int64(3, 5656) + pb_int32(5, 3))
        await weapons.handle_repairweaponmodifierrequest(
            sess, writer, 3, 'RepairWeaponModifierRequest',
            pb_int64(1, 301) + pb_int32(2, 1) +
            pb_int64(3, 5538) + pb_int32(5, 1))

    asyncio.run(run())

    assert [round(x[2], 6) for x in calls] == [0.25, 0.25, 0.5]
    assert store.weapon['condition'] == wardb.weapon_wear_params(32)[0] == 1.0
    assert store.ammo[0]['condition'] == wardb.ammo_wear_params(46)[0] == 1000.0
    assert store.mods[0]['condition'] == \
        wardb.weapon_modifier_wear_params(32)[0] == 1.0


def test_weapon_owner_lookup_prefers_database_over_stale_session(monkeypatch):
    """转移武器后的旧会话缓存不能再误拒合法的配件购买/挂接。"""
    class Store:
        async def get_weapon(self, _pid, _wid):
            return {'id': 101, 'template_id': 32, 'character_id': 70}

    sess = _Session()
    sess.weapons[101] = {'id': 101, 'template_id': 999, 'character_id': 66}
    monkeypatch.setattr(player_service, 'STORE', Store())

    async def run():
        owner = await player_service._weapon_owner_id(sess, 101)
        template = await player_service._weapon_template_id(sess, 101)
        return owner, template

    assert asyncio.run(run()) == (70, 32)
    assert sess.weapons[101]['character_id'] == 70
    assert sess.weapons[101]['template_id'] == 32


def test_thompson_repair_pricing_keeps_gold_currency():
    """截图对应的汤普森及默认弹药必须同时具有独立的 Credits/Gold 价格。"""
    weapon_price = wardb._REPAIR_PRICING[
        ('WeaponMaxConditionRepair', 32)]
    ammo_price = wardb._REPAIR_PRICING[
        ('AmmoMaxConditionRepair', 46)]

    assert weapon_price['credits'] == 9481
    assert weapon_price['gold'] == 114
    assert ammo_price['credits'] == 340
    assert ammo_price['gold'] == 4
