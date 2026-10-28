# -*- coding: utf-8 -*-
"""玩家数据持久层（PostgreSQL access layer）。

player_store.py 拆分产物：BaseStore 基础设施 + 17 个域 mixin，
在此按 MRO 组装为完整 PlayerStore（对外签名与原 player_store.PlayerStore 一致）。

MRO 约定：EconomyMixin 必须排在 IdentityMixin 之前——get_username 在两个
mixin 中均有定义，原 player_store 类体中 L3138（经济段）定义位于 L2260
（身份段）之后因而生效（后定义覆盖先定义）；MRO 解析方向相反（靠前优先），
故此处 EconomyMixin 必须更靠前以保持原覆盖语义。
循环依赖约定：wardb / state.log 的 import 保持函数内延迟（见各方法体），
上提会造成 store → wardb → state → store 环。
"""
from .base import (BaseStore, DEFAULT_DSN, _rowcount, _wsl_docker_ip,
                   _DAILY_BONUS_AVAILABLE_SQL)  # 后者 noqa: F401 重导出兼容测试
from .schema import _SCHEMA
from .players import PlayersMixin
from .characters import CharactersMixin
from .equipment_vehicles import EquipmentVehiclesMixin
from .equipment_weapons import EquipmentWeaponsMixin
from .battle_reports import BattleReportsMixin
from .battle_pass import BattlePassMixin
from .identity import IdentityMixin
from .social import SocialGraphMixin
from .mail import MailMixin
from .chat import ChatMixin
from .missions import MissionsMixin
from .economy import EconomyMixin
from .squads import SquadsMixin
from .companies import CompaniesMixin
from .maintenance import MaintenanceMixin
from .war import WarStoreMixin
from .foreign_imports import ForeignImportsMixin
from .login_events import LoginEventsMixin
from .server_config import ServerConfigMixin


class PlayerStore(
        BaseStore,
        PlayersMixin,
        CharactersMixin,
        EquipmentVehiclesMixin,
        EquipmentWeaponsMixin,
        BattleReportsMixin,
        BattlePassMixin,
        EconomyMixin,        # 必须在 IdentityMixin 之前（get_username 覆盖语义）
        IdentityMixin,
        SocialGraphMixin,
        MailMixin,
        ChatMixin,
        MissionsMixin,
        SquadsMixin,
        CompaniesMixin,
        MaintenanceMixin,
        WarStoreMixin,
        ForeignImportsMixin,
        LoginEventsMixin,
        ServerConfigMixin,
):
    """组装后的完整 PlayerStore（BaseStore + 18 域 mixin）。"""

    pass

__all__ = ['PlayerStore', '_SCHEMA', 'DEFAULT_DSN',
           '_wsl_docker_ip', '_rowcount']
