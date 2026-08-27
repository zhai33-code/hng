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
    RIBBON_KIND,
    RIBBON_KIND_FALLBACK,
    RIBBON_SPECIALIST_OVERLAY,
    RIBBON_PIP_GROUP,
    RIBBON_PIP_STYLES,
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


def ribbon_kind(template_id: int) -> int:
    """勋带类别 id：客户端 images/Ribbons/{kind}.png（Specialist 勋带官方用类别图渲染）。"""
    return RIBBON_KIND.get(template_id, 0)


def ribbon_icon(template_id: int) -> str:
    """最终图标路径（复刻官方 HnGGUICombatRibbonIcon.shouldUseCategoryImage）：
    kind 7-11（Specialist 类）一律用语义类别图；其余用专属数字图。"""
    kind = RIBBON_KIND.get(template_id)
    if kind in RIBBON_KIND_FALLBACK:
        return f'/icons/Ribbons/{RIBBON_KIND_FALLBACK[kind]}.png'
    return f'/icons/Ribbons/{template_id}.png'


_OVERLAY_DIR = {'W': 'Weapons', 'V': 'Vehicles'}


def ribbon_overlay(template_id: int) -> str:
    """Specialist 勋带叠加的官方 pip 图路径（256x128 灰度武器/载具图，
    tools/export_ribbon_pips.py 从 SWF 内嵌位图导出，按勋带 id 命名）。
    官方按 PipTransform 缩放定位绘制（见 ribbon_overlay_style），非整幅叠加。"""
    if template_id in RIBBON_SPECIALIST_OVERLAY:
        return f'/icons/RibbonPips/{template_id}.png'
    return ''


def ribbon_overlay_fallback(template_id: int) -> str:
    """pip 图缺失时的回退：武器/载具全尺寸图标路径（缩放叠加）。"""
    ov = RIBBON_SPECIALIST_OVERLAY.get(template_id)
    if not ov:
        return ''
    return f'/icons/{_OVERLAY_DIR[ov[0]]}/{ov[1]}.png'


def ribbon_overlay_style(template_id: int) -> dict:
    """pip 摆放参数（官方 PipTransforms）：scale 为显示高度占底图高度比例，
    x/y 为中心点相对位置。前端：height=scale*100%，left/top=x/y*100%，
    translate(-50%,-50%) 居中，等比缩放复刻官方 updatePipImage()。"""
    ov = RIBBON_SPECIALIST_OVERLAY.get(template_id)
    if not ov:
        return {}
    if ov[0] == 'V':
        group = 'vehicle'
    elif template_id in RIBBON_PIP_GROUP['pistol']:
        group = 'pistol'
    elif template_id in RIBBON_PIP_GROUP['rifle']:
        group = 'rifle'
    else:
        group = 'smg'
    return RIBBON_PIP_STYLES[group]


def character_name(template_id: int) -> str:
    return CHARACTERS.get(template_id, f'#{template_id}')


def faction_name(faction_id: int) -> str:
    return FACTIONS.get(faction_id, str(faction_id))


def badge_name(template_id: int) -> str:
    return BADGES.get(template_id, f'#{template_id}')
