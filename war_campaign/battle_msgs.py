# -*- coding: utf-8 -*-
"""战斗/运输消息行构建（battle row、transport segment/destination、air transport）。

原 war_campaign.py 阶段 3.5 拆分（docs/warserver-refactor-plan.md §3.5）：
符号逐字搬运，行为不变。"""

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


def msg_transport_destination_row(at, war_id, mv, segs, now_ms=None):
    """transport_commandnode_destination 行（终落地 + 总时长估算）。"""
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
    m += pb_bool(7, False)                             # retreatFlag
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
    """battle 行（会战；f1 id 为 S64，f11 position 仅补给线会战使用）。"""
    factions = wb.get('factions') or []
    n_factions = len({int(f.get('faction') or 0) for f in factions})
    m = pb_sfixed64(1, wb['id'])                       # id
    m += pb_int64(2, wb['war_id'])                     # warid
    m += pb_int64(3, wb['battlefield_id'])             # mapEntityId
    m += pb_int64(4, MAP_ENTITY_BATTLEFIELD)           # mapEntityTypeId
    m += pb_string(5, _fmt_ts(wb.get('started_at')))   # start
    m += pb_uint32(9, n_factions)                      # factioncount
    m += pb_int64(10, 0)                               # excludedFactionId
    m += pb_float(11, 0.0)                             # position
    m += pb_string(12, _fmt_ts(wb.get('started_at')))  # activationTimeStamp
    return m
