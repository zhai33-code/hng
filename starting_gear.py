# -*- coding: utf-8 -*-
"""新角色初始赠送配置（独立成文件，调整赠送内容只改这里）。

数据源（2026-08-24 从 WarDatabase 提取，已与用户逐项确认）：
- weapon: ShopCharacterPacks.slot1WeaponTemplateId（兵种初始武器，随建号赠送）
- sight_mod: 侦察兵随枪赠送的瞄具（WeaponModifier 模板；WeaponModMapping
  97→Rifle M1903 / 107→Karabiner 98 Kurz / 117→Mosin-Nagant 1891-30）
- vehicle_variant: progressionUnlock 表 includeItemAsGift=true 的赠送载具变体
  （坦克兵默认坦克 M2A2 / Panzer I / T-38；飞行员默认侦察机 O-52 / Hs 126 / R-Z）

键: (faction, template)   faction: 1=US  2=GE  3=SU
                          template: 1=步兵 2=飞行员 4=伞兵 5=坦克兵 6=侦察 7=将军

发放行为（handlers/characters.py::_grant_starting_gear）：
- weapon           → weapons 实例 + 默认弹药（weaponAmmoMapping defaultAmmo，
                     首个装备槽 1）+ 装备到主武器槽（container_slot=1，
                     战斗配装只下发已装备武器）
- sight_mod        → weapon_modifiers 实例（挂在该武器上，随枪赠送）
- vehicle_variant  → vehicles 实例 + 默认载具弹药（按弹药仓顺延装备）
                     + 装备到载具槽 1（战斗配装只下发已装备载具）
字段缺省 0 / 缺键 = 不赠送（将军无初始装备；飞行员无初始武器但有侦察机）。
"""

FACTION_NAMES = {1: 'US', 2: 'GE', 3: 'SU'}
TEMPLATE_NAMES = {1: '步兵', 2: '飞行员', 4: '伞兵', 5: '坦克兵', 6: '侦察', 7: '将军'}

STARTING_GEAR = {
    # ---- US ----
    (1, 1): {'weapon': 40, 'weapon_name': 'M1 Garand'},
    (1, 2): {'vehicle_variant': 205},            # Curtiss O-52 Owl (Factory)
    (1, 4): {'weapon': 40, 'weapon_name': 'M1 Garand'},
    (1, 5): {'weapon': 5, 'weapon_name': 'M3 Grease Gun',
             'vehicle_variant': 45},             # M2A2 Light Tank
    (1, 6): {'weapon': 46, 'weapon_name': 'Rifle M1903', 'sight_mod': 97},
    # ---- GE ----
    (2, 1): {'weapon': 44, 'weapon_name': 'Gewehr 43'},
    (2, 2): {'vehicle_variant': 204},            # Henschel Hs 126 B-1 (Factory)
    (2, 4): {'weapon': 44, 'weapon_name': 'Gewehr 43'},
    (2, 5): {'weapon': 6, 'weapon_name': 'Maschinenpistole 34',
             'vehicle_variant': 16},             # Panzerkampfwagen I Ausf. A
    (2, 6): {'weapon': 43, 'weapon_name': 'Karabiner 98 Kurz', 'sight_mod': 107},
    # ---- SU ----
    (3, 1): {'weapon': 14, 'weapon_name': 'Tokarev SVT-40'},
    (3, 2): {'vehicle_variant': 206},            # Polikarpov R-Z (Factory)
    (3, 4): {'weapon': 14, 'weapon_name': 'Tokarev SVT-40'},
    (3, 5): {'weapon': 24, 'weapon_name': 'PPD-40',
             'vehicle_variant': 43},             # T-38 Model 1937
    (3, 6): {'weapon': 12, 'weapon_name': 'Mosin-Nagant 1891-30', 'sight_mod': 117},
    # 将军 (*, 7)：无初始装备
}
