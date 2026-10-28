"""Native PlayerBattleResult f10/f11/f12 regression: bill actual item usage."""
import asyncio
from copy import deepcopy
import struct

import pytest

from test_equipment_wear import (
    _build_loadout, _condition_status, _earnings_proto, _msg, _varint,
    _MockSess, _MockWriter, _usage_cleanup,
)
from handlers import combat
import statistics_server
import wardb


@pytest.fixture
def loadout(monkeypatch):
    st, p = _build_loadout()
    # Two equipped vehicles with the SAME variant/weapon/ammo template.
    st.vehicles.append(dict(st.vehicles[0], id=403, equipped_in_slot=2))
    st.vehicle_ammo.append(dict(st.vehicle_ammo[0], id=603, vehicle_id=403))
    upgrade_max = wardb.vehicle_upgrade_wear_params(
        st.vehicle_upgrades[0]['upgrade_template_id'])[0]
    for row in st.vehicle_upgrades:
        row['condition'] = upgrade_max
    st.vehicle_upgrades.append(dict(st.vehicle_upgrades[0], id=503, vehicle_id=403))
    monkeypatch.setattr(combat, 'STORE', st)
    _usage_cleanup()
    yield st, p
    _usage_cleanup()


def _reports(raw, pid=1):
    return [(pid, combat._split_fields(raw))]


def _apply(raw, key=(123456789, 100003), pid=1):
    return asyncio.run(combat._apply_vehicle_condition_reports(_reports(raw, pid), key))


def test_foot_spawns_do_not_wear_or_repair_any_vehicle(loadout):
    st, p = loadout
    for rows in (st.vehicles, st.vehicle_upgrades, st.vehicle_ammo):
        for row in rows:
            row['auto_repair'] = 1
            row['condition'] /= 2  # Existing damage must not cause a charge either.
    st.balances[(1, 'credits')] = 10**9
    before = deepcopy((st.vehicles, st.vehicle_upgrades, st.vehicle_ammo))
    spent, rows = asyncio.run(combat._apply_equipment_wear(
        {(1, 11): 900}, {11: {'shots': {}, 'spawns': 10}}))
    assert (st.vehicles, st.vehicle_upgrades, st.vehicle_ammo) == before
    assert spent == rows == {}
    assert st.spent == []


def test_only_reported_vehicle_upgrade_and_ammo_are_charged(loadout):
    st, p = loadout
    u0 = st.vehicle_upgrades[0]['condition']
    raw = (_condition_status(10, 401, p['vmax'], p['vmax'] - 2)
           + _condition_status(11, 501, u0, u0 - 2)
           + _condition_status(12, 601, p['vamax'], p['vamax'] - 7))
    spent, rows = _apply(raw)
    assert st.vehicles[0]['condition'] == p['vmax'] - 2
    assert st.vehicle_upgrades[0]['condition'] == u0 - 2
    assert st.vehicle_ammo[0]['condition'] == p['vamax'] - 7
    assert st.vehicles[1]['condition'] == st.vehicles[2]['condition'] == p['vmax']
    assert st.vehicle_upgrades[1]['condition'] == st.vehicle_upgrades[2]['condition'] == u0
    assert st.vehicle_ammo[1]['condition'] == p['vamax']
    assert spent == {}
    assert {(kind, row['id']) for kind, row, _ in rows[1]} == {
        (b'Vehicle', 401), (b'VehicleUpgrade', 501), (b'VehicleAmmo', 601)}


def test_shared_weapon_template_does_not_charge_unused_vehicle_ammo(loadout):
    st, p = loadout
    asyncio.run(combat._apply_equipment_wear(
        {(1, 11): 900}, p['usage']))
    assert all(r['condition'] == p['vamax'] for r in st.vehicle_ammo)
    _apply(_condition_status(12, 601, p['vamax'], p['vamax'] - 5))
    assert st.vehicle_ammo[0]['condition'] == p['vamax'] - 5
    assert st.vehicle_ammo[1]['condition'] == p['vamax']


@pytest.mark.parametrize('auto_repair', [1, 2])
def test_used_vehicle_repairs_once_unused_vehicle_never_charged(loadout, auto_repair):
    st, p = loadout
    currency = 'credits' if auto_repair == 1 else 'gold'
    for row in st.vehicles:
        row['auto_repair'] = auto_repair
    st.balances[(1, currency)] = 10**9
    raw = _condition_status(10, 401, p['vmax'], p['vmax'] - 3)
    spent, rows = _apply(raw)
    base = wardb.repair_base_price('VehicleMaxConditionRepair',
        wardb.vehicle_parent_template_id(p['vvid']), currency)
    expected = max(1, int(base * 3 / p['vmax']))
    assert spent == {1: {currency: expected}}
    assert st.spent == [(1, currency, expected)]
    assert st.vehicles[0]['condition'] == st.vehicles[2]['condition'] == p['vmax']
    assert _apply(raw) == ({}, {})
    assert st.spent == [(1, currency, expected)]


def test_insufficient_repair_funds_preserves_actual_wear(loadout):
    st, p = loadout
    st.vehicles[0]['auto_repair'] = 2
    _apply(_condition_status(10, 401, p['vmax'], p['vmax'] - 3))
    assert st.vehicles[0]['condition'] == p['vmax'] - 3
    assert st.spent == []


def test_report_uses_item_id_after_inventory_move_and_keeps_external_repair(loadout):
    st, p = loadout
    # It was used in battle, then moved into depot and repaired before settlement.
    st.vehicles[0]['equipped_in_slot'] = 0
    _apply(_condition_status(10, 401, p['vmax'] - 10, p['vmax'] - 12))
    assert st.vehicles[0]['condition'] == p['vmax'] - 2


def test_other_players_and_unknown_ids_cannot_be_charged(loadout):
    st, p = loadout
    st.vehicles.append(dict(st.vehicles[0], id=999, player_id=2))
    st.vehicle_upgrades.append(dict(st.vehicle_upgrades[0], id=998, vehicle_id=999))
    before = deepcopy((st.vehicles, st.vehicle_upgrades))
    _apply(_condition_status(10, 999, p['vmax'], 0)
           + _condition_status(10, 123456, p['vmax'], 0)
           + _condition_status(11, 998, 1, 0))
    assert (st.vehicles, st.vehicle_upgrades) == before
    assert st.spent == []


@pytest.mark.parametrize('initial,final', [
    (float('nan'), 0), (float('inf'), 0), (25, float('nan')),
    (25, float('inf')), (-1, 0), (0, 0), (1, 2), (10**6, 0),
])
def test_invalid_report_never_wears_or_bills(loadout, initial, final):
    st, p = loadout
    before = deepcopy(st.vehicles)
    assert _apply(_condition_status(10, 401, initial, final)) == ({}, {})
    assert st.vehicles == before
    assert st.spent == []


def test_invalid_row_does_not_hide_valid_following_rows(loadout):
    st, p = loadout
    raw = _msg(10, b'\x0a\x01x') + _condition_status(10, 401, p['vmax'], p['vmax']-1)
    _apply(raw)
    assert st.vehicles[0]['condition'] == p['vmax']-1


def test_zero_condition_default_and_negative_native_upgrade_are_clamped(loadout):
    st, p = loadout
    # Native protobuf serializer may omit default-valued f2=0.
    raw = _msg(10, _varint(1, 401) + b'\x1d' + struct.pack('<f', p['vmax']))
    u0 = st.vehicle_upgrades[0]['condition']
    raw += _condition_status(11, 501, u0, -1)
    _apply(raw)
    assert st.vehicles[0]['condition'] == 0
    assert st.vehicle_upgrades[0]['condition'] == 0


@pytest.mark.parametrize('conflicting', [False, True])
def test_duplicate_rows_are_not_added_together(loadout, conflicting):
    st, p = loadout
    raw = _condition_status(10, 401, p['vmax'], p['vmax'] - 2)
    raw += _condition_status(10, 401, p['vmax'], p['vmax'] - (3 if conflicting else 2))
    _apply(raw)
    assert st.vehicles[0]['condition'] == p['vmax'] - (0 if conflicting else 2)


def test_concurrent_retransmit_bills_only_once_and_next_battle_bills_again(loadout):
    st, p = loadout
    raw = _condition_status(10, 401, p['vmax'], p['vmax'] - 2)
    async def race():
        await asyncio.gather(*[
            combat._apply_vehicle_condition_reports(_reports(raw), (1, 100003))
            for _ in range(3)])
    asyncio.run(race())
    assert st.vehicles[0]['condition'] == p['vmax'] - 2
    _apply(raw, key=(2, 100003))
    assert st.vehicles[0]['condition'] == p['vmax'] - 4


def test_vehicle_settlement_does_not_require_type6_or_role_births(loadout, monkeypatch):
    st, p = loadout
    async def no_usage(*args, **kwargs):
        return None
    monkeypatch.setattr(statistics_server, 'wait_battle_usage', no_usage)
    proto = _earnings_proto(vehicle_statuses=
        _condition_status(10, 401, p['vmax'], p['vmax'] - 2))
    writer = _MockWriter()
    asyncio.run(combat.handle_announce_battlefield_earnings(
        _MockSess(), writer, 1, 'AnnounceBattleFieldEarningsRequest', proto))
    assert b'AnnounceBattleFieldEarningsResponse' in writer.sent
    assert st.vehicles[0]['condition'] == p['vmax'] - 2
    assert st.vehicles[2]['condition'] == p['vmax']
    assert st.weapons[0]['condition'] == 1


def test_bot_collision_report_does_not_wear_real_players_vehicle(loadout, monkeypatch):
    st, p = loadout
    async def no_usage(*args, **kwargs):
        return None
    monkeypatch.setattr(statistics_server, 'wait_battle_usage', no_usage)
    statuses = _condition_status(10, 401, p['vmax'], p['vmax'] - 2)
    proto = _earnings_proto(vehicle_statuses=statuses)
    # The battle-report bot uses ID=1, colliding with a real account ID.
    bot = _varint(1, 1) + _varint(10, 1) + _msg(11, _varint(1, 11))
    report = _varint(1, 730433917100099) + _msg(6, _varint(1, 2) + _msg(2, bot))
    fields = combat._split_fields(proto)
    proto = b''.join(_msg(f, report if f == 4 else v) if wt == 2
                     else _varint(f, v) for f, wt, v in fields)
    asyncio.run(combat.handle_announce_battlefield_earnings(
        _MockSess(), _MockWriter(), 1, 'AnnounceBattleFieldEarningsRequest', proto))
    assert st.vehicles[0]['condition'] == p['vmax']
    assert st.spent == []


def test_settlement_forwards_repair_cost_and_item_rows_to_client_refresh(loadout, monkeypatch):
    st, p = loadout
    st.vehicles[0]['auto_repair'] = 1
    st.balances[(1, 'credits')] = 10**9
    forwarded = []
    async def no_usage(*args, **kwargs):
        return None
    async def finish(mid, rid, chars, spent, rows, ribbons):
        forwarded.append((spent, rows))
    monkeypatch.setattr(statistics_server, 'wait_battle_usage', no_usage)
    monkeypatch.setattr(combat, '_finish_mission_sessions', finish)
    proto = _earnings_proto(vehicle_statuses=
        _condition_status(10, 401, p['vmax'], p['vmax'] - 2)
        + _condition_status(12, 601, p['vamax'], p['vamax'] - 3))
    for cmd in (1, 2):
        asyncio.run(combat.handle_announce_battlefield_earnings(
            _MockSess(), _MockWriter(), cmd, 'AnnounceBattleFieldEarningsRequest', proto))
    base = wardb.repair_base_price('VehicleMaxConditionRepair',
        wardb.vehicle_parent_template_id(p['vvid']), 'credits')
    assert forwarded[0][0] == {1: {'credits': max(1, int(base * 2 / p['vmax']))}}
    assert [(kind, row['id'], cond) for kind, row, cond in forwarded[0][1][1]] == [
        (b'VehicleAmmo', 601, p['vamax'] - 3)]
    assert forwarded[1] == ({}, {})
