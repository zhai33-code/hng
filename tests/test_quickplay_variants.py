"""快速匹配 11/12/14 的角色范围与 PMM 候选信息回归。"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import matchmaking
import mm_handler
from protocol import pb_parse_multi


CHARS = [
    {'id': 1, 'faction': 1, 'template': 101, 'name': 'Infantry'},
    {'id': 2, 'faction': 1, 'template': 102, 'name': 'Tanker'},
    {'id': 3, 'faction': 2, 'template': 103, 'name': 'German'},
    {'id': 4, 'faction': 1, 'template': 999, 'name': 'General'},
]


def test_candidate_scopes():
    old_general = mm_handler.is_general_character
    mm_handler.is_general_character = lambda template: int(template) == 999
    try:
        assert [c['id'] for c in mm_handler._quickplay_character_candidates(
            CHARS, CHARS[0], 11)] == [1, 2]
        assert [c['id'] for c in mm_handler._quickplay_character_candidates(
            CHARS, CHARS[0], 12)] == [1]
        assert [c['id'] for c in mm_handler._quickplay_character_candidates(
            CHARS, CHARS[0], 14)] == [1, 2, 3]
        assert [c['id'] for c in mm_handler._quickplay_character_candidates(
            CHARS, CHARS[3], 11)] == [1, 2]
    finally:
        mm_handler.is_general_character = old_general


def test_picker_prefers_fastest_compatible_character():
    class Pool:
        @staticmethod
        def waiting_counts():
            return {2: 1, 3: 5, 4: 0, 5: 0, 6: 0}

    async def vehicle_cats(_pid, _char_id):
        return ()

    old = (mm_handler.POOL, mm_handler.character_resource_category,
           mm_handler._char_vehicle_cats, mm_handler._find_joinable_mission,
           mm_handler.is_general_character)
    mm_handler.POOL = Pool()
    mm_handler.character_resource_category = lambda template: {
        101: 1, 102: 5, 103: 1, 999: 4,
    }[int(template)]
    mm_handler._char_vehicle_cats = vehicle_cats
    mm_handler._find_joinable_mission = lambda *_a, **_k: None
    mm_handler.is_general_character = lambda template: int(template) == 999
    try:
        picked11, info11 = asyncio.run(mm_handler._pick_quickplay_character(
            9, CHARS, CHARS[0], 11))
        assert picked11['id'] == 2       # 坦克池更忙，可更快成局
        assert [i['faction'] for i in info11] == [1, 1]

        picked12, info12 = asyncio.run(mm_handler._pick_quickplay_character(
            9, CHARS, CHARS[0], 12))
        assert picked12['id'] == 1       # 当前士兵按钮绝不换人
        assert len(info12) == 1

        mm_handler._find_joinable_mission = (
            lambda *_a, **kw: (700, {}) if int(_a[2]) == 2 else None)
        picked14, _ = asyncio.run(mm_handler._pick_quickplay_character(
            9, CHARS, CHARS[0], 14))
        assert picked14['id'] == 3       # 全阵营模式优先可立即加入的角色
    finally:
        (mm_handler.POOL, mm_handler.character_resource_category,
         mm_handler._char_vehicle_cats, mm_handler._find_joinable_mission,
         mm_handler.is_general_character) = old


def test_player_matchmaking_contains_quickplay_info():
    msg = matchmaking._msg_player_matchmaking(
        1, 2, 1, 0, '20260930120000Z', quickplay_info=(
            {'faction': 1, 'template': 101, 'presets': (10, 12),
             'vehicle_cats': (5,)},
            {'faction': 2, 'template': 103, 'presets': (22,)},
        ))
    outer = pb_parse_multi(msg)
    rows = [pb_parse_multi(raw) for raw in outer[24]]
    assert len(rows) == 2
    assert rows[0][1] == [1] and rows[0][2] == [101]
    assert rows[0][3] == [10, 12] and rows[0][4] == [5]
    assert rows[1][1] == [2] and rows[1][2] == [103]
