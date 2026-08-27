# -*- coding: utf-8 -*-
"""模板元数据查表：template_id -> 名称 映射。

数据固化在 templates_data.py（自客户端 WarDatabase 提取），
管理端不再依赖客户端文件（无需 GAME_DIR）。
模板数据升级时重新提取并更新 templates_data.py 即可。
"""
from .templates_data import (
    AMMO,
    BADGES,
    CHARACTERS,
    FACTIONS,
    MODIFIERS,
    RANK_THRESHOLDS,
    RIBBONS,
    VEHICLES,
    VEHICLE_VARIANTS,
    WEAPONS,
    )


def load():
    """兼容旧调用：数据已固化，无需运行时加载。"""


def rank_from_xp(xp: int) -> int:
    """军衔等级 = combat_xp 经阈值推导（rank 0 起）。"""
    rank = 0
    for t in RANK_THRESHOLDS:
        if xp >= t:
            rank += 1
        else:
            break
    return rank


def weapon_name(template_id: int) -> str:
    return WEAPONS.get(template_id, f'#{template_id}')


def vehicle_info(variant_template_id: int) -> dict:
    v = VEHICLE_VARIANTS.get(variant_template_id)
    if not v:
        return {'name': f'#{variant_template_id}', 'parent': 0,
                'parent_name': ''}
    return {'name': v['name'],
            'parent': v['parent'],
            'parent_name': VEHICLES.get(v['parent'], '')}


def modifier_name(template_id: int) -> str:
    return MODIFIERS.get(template_id, f'#{template_id}')


def ammo_name(template_id: int) -> str:
    return AMMO.get(template_id, f'#{template_id}')


def ribbon_name(template_id: int) -> str:
    return RIBBONS.get(template_id, f'#{template_id}')


def character_name(template_id: int) -> str:
    return CHARACTERS.get(template_id, f'#{template_id}')


def faction_name(faction_id: int) -> str:
    return FACTIONS.get(faction_id, str(faction_id))


def badge_name(template_id: int) -> str:
    return BADGES.get(template_id, f'#{template_id}')
