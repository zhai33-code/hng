"""普通局系统 AT 与资源池/分线/轮询一致，战争局保留真实属主。"""
import asyncio
import os
import sys
from collections import Counter
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import force_pool
import warserver_messages
from handlers import combat
from protocol import pb_int64, pb_parse, pb_parse_multi
from wardb import commandnode_template


@pytest.fixture
def mission(monkeypatch):
    mid = 987001
    names = ['infantry', 'infantry', 'fighter_recon']
    monkeypatch.setattr(force_pool, 'draw_at_names',
                        lambda *_args: (list(names), 'assault_battle'))
    assert all(force_pool._AT_COMP.get(n) for n in names)
    force_pool.init_mission(mid, 6, 1, 2)
    monkeypatch.setitem(combat.mm_state._MM_MISSION_DETAILS, mid,
                        {'preset': 10, 'attacker_faction': 1,
                         'defender_faction': 2})
    layout = [(8, 1), (15, 1), (1234567890, 2)]
    monkeypatch.setattr(combat, '_accesspoint_layout',
                        lambda *_args, **_kwargs: layout)
    monkeypatch.setattr(warserver_messages, '_accesspoint_layout',
                        lambda *_args, **_kwargs: layout)
    # 解码真实 handler 输出，只省略外层传输帧。
    monkeypatch.setattr(combat, 'build_response',
                        lambda _cmd, _type, proto: proto)
    monkeypatch.setattr(combat, 'STORE', SimpleNamespace())
    yield mid, names
    force_pool.release(mid)
    force_pool.release(mid + 1)


def response(mid, session=None):
    frames = []

    async def drain():
        pass

    writer = SimpleNamespace(write=frames.append, drain=drain)
    asyncio.run(combat.handle_active_mission_event_list(
        session or SimpleNamespace(), writer, 17, '', pb_int64(1, mid)))
    assert len(frames) == 1
    return pb_parse_multi(frames[0])


def test_system_ats_match_resources_and_poll_without_new_ids(mission):
    mid, names = mission
    session = SimpleNamespace(game_mission=mid)
    first = response(0, session)  # 游戏进程用连接上的 mission 定位
    ats = [pb_parse(row) for row in first[5]]
    assert len(ats) == 2 * len(names)
    assert len({a[1] for a in ats}) == len(ats)
    for fac in (1, 2):
        side = [a for a in ats if a[4] == fac]
        assert Counter(a[5] for a in side) == Counter(
            commandnode_template(fac, n) for n in names)
        assert all(a[2] == 0 and a[3] == b'' for a in side)
        assert all(a[8] == 0 and a[14] == 1 for a in side)
        assert all(a[15] in ({8, 15} if fac == 1 else {1234567890})
                   for a in side)
    resources = [pb_parse(row) for row in first[2]]
    for fac in (1, 2):
        for ap in ({8, 15} if fac == 1 else {1234567890}):
            expected = Counter()
            for a in ats:
                if a[4] == fac and a[15] == ap:
                    name = next(n for n in names
                                if commandnode_template(fac, n) == a[5])
                    expected.update(force_pool._AT_COMP[name])
            actual = {r[3]: r[5] for r in resources
                      if r[1] == fac and r[2] == ap}
            assert actual == dict(expected)
    infantry_ap = next(a[15] for a in ats
                       if a[4] == 1 and a[5] == commandnode_template(1, 'infantry'))
    cat = next(iter(force_pool._AT_COMP['infantry']))
    force_pool.consume(mid, 1, cat, 1, ap=infantry_ap)
    second = response(mid, session)
    assert second[5] == first[5]  # 属主、类型、ID、出兵线均稳定
    before = sum(r[5] for r in resources if r[1] == 1 and r[3] == cat)
    after = sum(r[5] for r in map(pb_parse, second[2])
                if r[1] == 1 and r[3] == cat)
    assert after == before - 1


def test_system_ids_are_unique_across_missions_and_release(mission):
    mid, _ = mission
    force_pool.init_mission(mid + 1, 6, 1, 2)
    first = force_pool.staged_assault_teams(mid)
    other = force_pool.staged_assault_teams(mid + 1)
    ids = lambda teams: {a['id'] for rows in teams.values() for a in rows}
    assert ids(first).isdisjoint(ids(other))
    force_pool.release(mid)
    assert force_pool.staged_assault_teams(mid) is None


def test_war_keeps_real_at_ids_templates_and_owner(mission, monkeypatch):
    mid, _ = mission
    force_pool.init_mission_war(
        mid, 1, 2, {1: [{1: 36}], 2: []},
        {1: ['infantry'], 2: []},
        {1: [{'id': 73, 'name': 'My AT', 'owner_player_id': 42,
              'template_id': commandnode_template(1, 'infantry'),
              'ap_tmpl': 15}], 2: []})

    async def fetch(_sql, ids):
        assert ids == [42]
        return [{'id': 42, 'username': 'General42'}]

    monkeypatch.setattr(combat, 'STORE',
                        SimpleNamespace(pool=SimpleNamespace(fetch=fetch)))
    assert force_pool.staged_assault_teams(mid) is None
    ats = [pb_parse(row) for row in response(mid)[5]]
    assert len(ats) == 1
    assert ats[0][1] == 73
    assert ats[0][2] == 42 and ats[0][3] == b'General42'
    assert ats[0][5] == commandnode_template(1, 'infantry')
    assert ats[0][15] == 15


def test_missing_pool_and_unknown_template_do_not_fabricate_ats(mission, monkeypatch):
    mid, _ = mission
    monkeypatch.setattr(force_pool, 'commandnode_template', lambda *_args: 0)
    assert 5 not in response(mid)
    force_pool.release(mid)
    assert 5 not in response(mid)
