# -*- coding: utf-8 -*-
"""战斗/运输消息行构建（battle row、transport segment/destination、air transport）。

原 war_campaign.py 阶段 3.5 拆分（docs/warserver-refactor-plan.md §3.5）：
符号逐字搬运，行为不变。"""

import datetime
import time
from protocol import (
    pb_bool,
    pb_int32,
    pb_int64,
    pb_sfixed64,
    pb_string,
    pb_uint32,
    pb_float,
    )
from .common import (
    _fmt_ts,
    MAP_ENTITY_BATTLEFIELD,
    MAP_ENTITY_SUPPLYLINE,
    BATTLE_TYPE_ASSAULT,
    battle_prep_seconds,
    _ts_ms,
    )


def msg_transport_segment_row(at, war_id, mv, seg):
    """transport_commandnode_segment 行（f6/f7 为毫秒数字符串，
    客户端 Number(begintime) 直接与 serverTimeAsNumber 比较）。"""
    m = pb_int64(1, mv['id'])                          # id（movement id）
    m += pb_int64(2, at['id'])                         # commandNodeWarInstanceId
    m += pb_int64(3, seg['sl_id'])                     # supplylineid
    m += pb_int64(4, seg['from_ap'])                  # fromAccesspoint
    m += pb_int64(5, seg['to_ap'])                     # toAccesspoint
    m += pb_string(6, str(int(seg['begin_ms'])))       # begintime（ms STR）
    m += pb_string(7, str(int(seg['end_ms'])))         # endtime（ms STR）
    m += pb_int64(8, war_id)                           # warid
    return m


def msg_transport_destination_row(at, war_id, mv, segs, now_ms=None,
                                  retreat=False):
    """transport_commandnode_destination 行（终落地 + 总时长估算）。
    retreat：撤退行军旗标（f7，客户端按撤退方向渲染箭头/动画）。"""
    if segs:
        dest_bf = segs[-1]['to_bf']
        end_ms = segs[-1]['end_ms']
        cached = ','.join(str(s['to_bf']) for s in segs)
    else:
        dest_bf, end_ms, cached = 0, 0, ''
    start_ms = _ts_ms(mv.get('started_at'))
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    total = max(0.0, (end_ms - start_ms) / 1000.0)
    m = pb_int64(1, mv['id'])                          # id
    m += pb_int64(2, at['id'])                         # commandNodeWarInstanceId
    m += pb_int64(3, dest_bf)                          # battlefieldid
    m += pb_int64(4, int(mv.get('dest_ap') or 0))      # accesspointid
    m += pb_string(5, cached)                          # cachedpath
    m += pb_int64(6, war_id)                            # warid
    m += pb_bool(7, retreat)                           # retreatFlag
    m += pb_float(8, total)                             # timeEstimate（秒）
    m += pb_float(9, 0.0)                              # beginPosition
    return m


def msg_air_transport_row(at, war_id, mv, seg):
    """air_transport 行（§3.14）：f3/f4 from + f5/f6 to（type1=battlefield）
    + f7/f8 起止毫秒。空运在途动画由本行驱动（instance 行保持出发城市）。"""
    m = pb_int64(1, mv['id'])                          # id（movement id）
    m += pb_int64(2, at['id'])                         # commandNodeWarInstanceId
    m += pb_int64(3, seg['from_bf'])                   # fromentityid
    m += pb_int32(4, MAP_ENTITY_BATTLEFIELD)           # fromentitytype
    m += pb_int64(5, seg['to_bf'])                     # toentityid
    m += pb_int32(6, MAP_ENTITY_BATTLEFIELD)           # toentitytype
    m += pb_int64(7, int(seg['begin_ms']))             # begintime（ms）
    m += pb_int64(8, int(seg['end_ms']))               # endtime（ms）
    m += pb_int64(9, war_id)                           # warid
    return m


def msg_battle_row(wb):
    """battle 行（会战；f1 id 为 S64，f11 position 仅补给线会战使用）。

    交战类型建模（war-gameplay-fix-plan §2）：battle_type（官方
    missiontype）驱动 mapEntityTypeId（城战=BATTLEFIELD、补给线会战=
    SUPPLYLINE 且 mapEntityId=补给线 id）+ position（补给线 0-1）+
    activationTimeStamp（= 成局 + 准备期，automission 超时起算点）。
    factioncount（f9）如实计数；excludedFactionId（f10）= 未参战阵营
    ——两方局填 1/2/3 中唯一缺席方（官方契约：被排除阵营不得参战）；
    三方局 = 0。"""
    btype = int(wb.get('battle_type') or BATTLE_TYPE_ASSAULT)
    factions = wb.get('factions') or []
    present = {int(f.get('faction') or 0) for f in factions
               if int(f.get('faction') or 0)}
    n_factions = len(present)
    excluded = 0
    if n_factions == 2 and present <= {1, 2, 3}:
        excluded = ({1, 2, 3} - present).pop()
    started = wb.get('started_at')
    activation = started
    if isinstance(started, datetime.datetime):
        activation = (started
                      + datetime.timedelta(seconds=battle_prep_seconds(btype)))
    ent_type = (MAP_ENTITY_BATTLEFIELD if btype == BATTLE_TYPE_ASSAULT
                else MAP_ENTITY_SUPPLYLINE)
    m = pb_sfixed64(1, wb['id'])                       # id
    m += pb_int64(2, wb['war_id'])                     # warid
    m += pb_int64(3, wb['battlefield_id'])             # mapEntityId（补给线会战=sl id）
    m += pb_int64(4, ent_type)                         # mapEntityTypeId
    m += pb_string(5, _fmt_ts(started))                # start
    m += pb_uint32(9, n_factions)                      # factioncount
    m += pb_int64(10, excluded)                        # excludedFactionId
    m += pb_float(11, float(wb.get('position') or 0.0))  # position
    m += pb_string(12, _fmt_ts(activation))            # activationTimeStamp
    return m
