# -*- coding: utf-8 -*-
"""载具手动维修价格回归测试（纯单元，无需数据库）。"""
import asyncio

from protocol import pb_int32, pb_int64
import handlers.career as career
import handlers.vehicles as vehicles
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

    async def ensure_player(self):
        return 7


class _MaintenanceStore:
    def __init__(self):
        # variant 1 对应的基础载具满耐久为 60。
        self.vehicle = {
            'id': 101, 'vehicle_variant_template_id': 1,
            'character_id': 70, 'equipped_in_slot': 1, 'variant_id': 0,
            'auto_repair': 0, 'condition': 45.0,
        }
        self.upgrades = [{
            'id': 201, 'upgrade_template_id': 1, 'vehicle_id': 101,
            'equipped_slot_id': 1, 'character_id': 70, 'condition': 50.0,
        }]
        self.ammo = [{
            'id': 301, 'ammo_template_id': 1, 'vehicle_id': 101,
            'equipped_storage_id': 1, 'slot_index': 1,
            'character_id': 70, 'condition': 1250.0,
        }]

    async def get_vehicle(self, _pid, vehicle_id):
        return dict(self.vehicle) if vehicle_id == self.vehicle['id'] else None

    async def list_vehicle_upgrades(self, _pid):
        return [dict(x) for x in self.upgrades]

    async def list_vehicle_ammo(self, _pid):
        return [dict(x) for x in self.ammo]

    async def update_vehicle_condition(self, _pid, _vehicle_id, value):
        self.vehicle['condition'] = value
        return True

    async def update_vehicle_upgrade_condition(self, _pid, _upgrade_id, value):
        self.upgrades[0]['condition'] = value
        return True

    async def update_vehicle_ammo_condition(self, _pid, _ammo_id, value):
        self.ammo[0]['condition'] = value
        return True


def test_manual_vehicle_maintenance_uses_client_damage_fraction(monkeypatch):
    """载具本体、升级件和弹药均按缺损比例扣款并修到模板上限。"""
    store = _MaintenanceStore()
    calls = []

    async def charge(_sess, pricing_id, currency, conn=None, fraction=1.0,
                     **_kwargs):
        calls.append((pricing_id, currency, fraction))
        return 1, True

    async def noop(*_args, **_kwargs):
        pass

    monkeypatch.setattr(vehicles, 'STORE', store)
    monkeypatch.setattr(vehicles, '_charge_player', charge)
    monkeypatch.setattr(vehicles, '_push_player_refresh', noop)
    monkeypatch.setattr(career, 'complete_order_and_push', noop)

    async def run():
        sess = _Session()
        writer = _Writer()
        await vehicles.handle_repairvehiclerequest(
            sess, writer, 1, 'RepairVehicleRequest',
            pb_int64(2, 101) + pb_int32(3, 60) +
            pb_int64(4, 4001) + pb_int32(7, 1))
        await vehicles.handle_repairvehicleupgraderequest(
            sess, writer, 2, 'RepairVehicleUpgradeRequest',
            pb_int64(1, 201) + pb_int32(2, 100) +
            pb_int64(3, 4002) + pb_int32(5, 1))
        await vehicles.handle_repairvehicleammorequest(
            sess, writer, 3, 'RepairVehicleAmmoRequest',
            pb_int64(1, 301) + pb_int32(2, 5000) +
            pb_int64(3, 4003) + pb_int32(5, 3))

    asyncio.run(run())

    assert [round(x[2], 6) for x in calls] == [0.25, 0.5, 0.75]
    assert store.vehicle['condition'] == wardb.vehicle_full_condition(1) == 60.0
    assert store.upgrades[0]['condition'] == \
        wardb.vehicle_upgrade_wear_params(1)[0] == 100.0
    assert store.ammo[0]['condition'] == wardb.ammo_wear_params(1)[0] == 5000.0


def test_missing_vehicle_items_are_not_charged(monkeypatch):
    """不存在或不属于玩家的实例必须在扣款前被拒绝。"""
    store = _MaintenanceStore()
    calls = []

    async def charge(*args, **kwargs):
        calls.append((args, kwargs))
        return 1, True

    monkeypatch.setattr(vehicles, 'STORE', store)
    monkeypatch.setattr(vehicles, '_charge_player', charge)

    async def run():
        sess = _Session()
        writer = _Writer()
        await vehicles.handle_repairvehiclerequest(
            sess, writer, 1, 'RepairVehicleRequest', pb_int64(2, 999))
        await vehicles.handle_repairvehicleupgraderequest(
            sess, writer, 2, 'RepairVehicleUpgradeRequest', pb_int64(1, 999))
        await vehicles.handle_repairvehicleammorequest(
            sess, writer, 3, 'RepairVehicleAmmoRequest', pb_int64(1, 999))

    asyncio.run(run())
    assert calls == []
