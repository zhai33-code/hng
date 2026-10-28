# -*- coding: utf-8 -*-
"""war 行消息构建（29 REQUIRED 字段全填，docs/hng-rts-protocol.md §3.1）。

原 war_campaign.py 阶段 3.5 拆分（docs/warserver-refactor-plan.md §3.5）：
符号逐字搬运，行为不变。"""

from protocol import (
    pb_int32,
    pb_int64,
    pb_string,
    pb_uint32,
    pb_float,
    )
import mm_config
from .common import (
    morale_defaults,
    _fmt_ts,
    )
from .lifecycle import (
    effective_points_needed,
    ensure_war,
    )


def build_war_proto(war):
    """构建 HnG_States.war 行 proto（29 REQUIRED 字段全填）。

    war：wars 表行（config_json 已由 store._war_row 转成 config dict）。"""
    morale = (war.get('config') or {}).get('morale') or morale_defaults()
    cfg = war.get('config') or {}
    m = b''
    m += pb_int64(1, war['id'])                                  # id
    m += pb_string(2, war.get('name') or '')                      # name
    m += pb_string(3, _fmt_ts(war.get('begintime')))              # begintime
    m += pb_string(4, _fmt_ts(war.get('endtime')))                # endtime（进行中=''）
    m += pb_string(5, '')                                         # web
    m += pb_string(6, '0')                                        # friendlyfire
    m += pb_int64(7, war.get('winning_faction_id') or 0)          # winningfactionid
    m += pb_int32(8, effective_points_needed(              # pointsNeededForVictory
        war.get('points_needed_for_victory')))
    m += pb_uint32(9, mm_config.WAR_AUTOMISSION_NORMAL)      # automissionnormal
    m += pb_uint32(10, mm_config.WAR_AUTOMISSION_SKIRMISH)   # automissionskirmish
    m += pb_uint32(11, mm_config.WAR_AUTOMISSION_SUPPLY)     # automissionsupply
    m += pb_float(12, float(cfg.get('strategytimescale',
                                    mm_config.WAR_TIMESCALE)))     # strategytimescale
    m += pb_float(13, float(cfg.get('daycycletimescale',
                                    mm_config.WAR_DAYCYCLE_TIMESCALE)))  # daycycletimescale
    m += pb_uint32(14, 0)                                         # disablestrategy
    m += pb_int32(15, int(cfg.get('maxPlayerPerFaction',
                                  mm_config.WAR_MAX_PLAYERS_PER_FACTION)))  # maxPlayerPerFaction
    m += pb_int64(16, war.get('sequel_war_id') or 0)              # sequelwarid
    m += pb_int64(17, war.get('mapid') or 1)                      # mapid
    m += pb_int32(18, 0)                                          # cleanupStatus
    # fow 网格参数（M3 §9.3）：非 0 即激活客户端 FogOfWarLayer，
    # 可见性判定在客户端本地；默认 0=全图可见
    m += pb_int32(19, mm_config.WAR_FOW_GRID_SIZE)                # fowGridSize
    m += pb_int32(20, mm_config.WAR_FOW_GRID_ORIGIN_X)            # fowGridOriginX
    m += pb_int32(21, mm_config.WAR_FOW_GRID_ORIGIN_Y)            # fowGridOriginY
    m += pb_int32(22, int(bool(cfg.get(
        'queueReinforcements', mm_config.WAR_QUEUE_REINFORCEMENTS))))
    m += pb_int32(23, int(bool(cfg.get(
        'firstResourceFree', mm_config.WAR_FIRST_RESOURCE_FREE))))
    m += pb_uint32(24, int(morale['min']))                        # moraleMinValue
    m += pb_uint32(25, int(morale['max']))                        # moraleMaxValue
    m += pb_uint32(26, int(morale['lost_per_battle']))            # moraleLostPerBattle
    m += pb_uint32(27, int(morale['gained_per_win']))             # moraleGainedPerWin
    m += pb_float(28, float(morale['gained_per_minute']))          # moraleGainedPerMinute
    m += pb_uint32(29, int(morale['surrender_limit']))            # moraleSurrenderLimit
    return m


async def war_kvcs_proto(store):
    """当前战争的 war 行 proto；无战争（DB 不可用/未开战）返回 None，
    调用方回退静态 mock（views.handle_subscribe）。"""
    war = await ensure_war(store)
    if war is None:
        return None
    return build_war_proto(war)


# ---- war catalogue 聚合 ----
