# -*- coding: utf-8 -*-
"""共享常量与纯工具：CommandNodeMorale 默认模板、单位类别集合、地图实体类型、时间戳/资源格式化、war 配置读取，以及 battlefield 地理工具（坐标缓存、supplyline 邻接表、寻路）。

原 war_campaign.py 阶段 3.5 拆分（docs/warserver-refactor-plan.md §3.5）：
符号逐字搬运，行为不变。"""

import datetime
import json
import mm_config
import wardb


# CommandNodeMoraleTemplate 官服实值（tests/test_wardb_rts.py §F 校验）
DEFAULT_MORALE = {
    'min': 0,
    'max': 100,
    'lost_per_battle': 20,
    'gained_per_win': 10,
    'gained_per_minute': 0.15,
    'surrender_limit': 25,
}


# armyresourcecategory 归类（force_pool._CAT_ORDER 同源）：
# 士兵族（infantryCount）/ 坦克族（tankCount）/ 地面载具族（vehicleCount）/
# 飞机族（planeCount）——war_catalogue 计数展示用（M1 近似值，可校准）。
# cat 5 = 坦克组乘员（人员，light_armor AT 编制 {5:20, 12:16} 实锤），归士兵族。
_SOLDIER_CATS = {1, 2, 4, 5, 6}


_TANK_CATS = {7, 8, 12, 17, 18}


_GROUND_VEHICLE_CATS = {9, 11, 15, 16}


_PLANE_CATS = {3, 10, 20, 21}


def morale_defaults():
    """战争 morale 参数：wardb.morale_template() 优先，缺表回退官服实值。"""
    mt = wardb.morale_template()
    if mt:
        return {k: mt.get(k, DEFAULT_MORALE[k]) for k in DEFAULT_MORALE}
    return dict(DEFAULT_MORALE)


def _fmt_ts(dt):
    """DB TIMESTAMPTZ → 协议 STR 时间戳（紧凑 Zulu 'YYYYMMDDHHMMSSZ'，UTC）。

    客户端 TimeUtilities.convertZuluTimeToMilliseconds 按位 substr(0,4)/(4,2)/
    ... 截取组 'YYYY/MM/DD HH:mm:ss GMT-0000' 再 Date.parse；ISO 分隔符串
    会解析成 NaN（uint(NaN)=0 → 0 士气 bug）。"""
    if not dt:
        return ''
    if isinstance(dt, str):
        return dt
    if dt.tzinfo is None:            # naive（FakeStore/无时区来源）按 UTC
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc).strftime('%Y%m%d%H%M%SZ')


def _war_days(war):
    """war_catalogue f3 wartime：战争已持续天数（字符串，≥1）。

    客户端 WarIsOverScreenMediator 把 wartime 原样代入结算文案
    "用{3}天的时间占领了欧洲"（WarIsOverScreen/article_neutral 的 {3}），
    WarModel 另取 int(wartime)/daycycletimescale 作 warElapsed——发天数
    两处口径都成立。此前误发 begintime 的 Zulu 时间戳 → 结算页"x天"
    显示成具体日期。结束战争用 endtime；进行中战争用当前时刻；
    不足一天按 1 天（当天开打当天结束也是"用1天"）。"""
    begin = war.get('begintime') if isinstance(war, dict) else None
    if not begin:
        return '1'
    end = war.get('endtime')
    try:
        begin = _as_dt(begin)
        end = _as_dt(end) if end else datetime.datetime.now(
            datetime.timezone.utc)
    except (TypeError, ValueError):
        return '1'
    elapsed = (end - begin).total_seconds()
    if elapsed <= 0:
        return '1'
    return str(max(1, int(-(-elapsed // 86400))))    # ceil


def _as_dt(v):
    """begintime/endtime → aware UTC datetime（naive 按 UTC）。"""
    if isinstance(v, str):
        v = datetime.datetime.fromisoformat(v.replace('Z', '+00:00'))
    if v.tzinfo is None:
        v = v.replace(tzinfo=datetime.timezone.utc)
    return v


def _as_resources(raw):
    """command_nodes.resources（JSONB 或 str 或 None）→ dict。"""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def _war_config(morale=None):
    """新战争 config 快照（wars.config_json）。"""
    morale = morale or morale_defaults()
    return {
        'strategytimescale': mm_config.WAR_TIMESCALE,
        'daycycletimescale': mm_config.WAR_DAYCYCLE_TIMESCALE,
        'maxPlayerPerFaction': mm_config.WAR_MAX_PLAYERS_PER_FACTION,
        'morale': morale,
    }


# mapEntityTypeId 枚举（client MapEntityType）
MAP_ENTITY_BATTLEFIELD = 1


MAP_ENTITY_ACCESSPOINT = 2


MAP_ENTITY_SUPPLYLINE = 3


# 官方 missiontype（war-gameplay-fix-plan §2 反编译复核）：1=ASSAULT 城战 /
# 3=SKIRMISH 补给线·步/侦/装 / 5=ENCOUNTER 补给线·纯步兵；
# 伞兵攻城类型上就是 Assault（288 伞兵是 automission 判定规则，非类型）
BATTLE_TYPE_ASSAULT = 1


BATTLE_TYPE_SKIRMISH = 3


BATTLE_TYPE_ENCOUNTER = 5


def battle_prep_seconds(battle_type):
    """会战准备期秒数（城攻 330 / 野外 300，mm_config 可配）。"""
    return (mm_config.WAR_BATTLE_PREP_ASSAULT
            if int(battle_type or BATTLE_TYPE_ASSAULT) == BATTLE_TYPE_ASSAULT
            else mm_config.WAR_BATTLE_PREP_FIELD)


def _ts_ms(dt):
    """datetime → 毫秒时间戳（无值回 0）。"""
    if not dt:
        return 0
    if isinstance(dt, (int, float)):
        return int(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


_ADJACENCY_CACHE = None


_BF_POS_CACHE = None


def supplyline_adjacency():
    """battlefield 邻接表 {bf: [(supplyline_id, from_ap, to_ap, 对端 bf)]}。

    supplyline 两端 accesspoint 各归属一个 battlefield，构成城市图的边。"""
    global _ADJACENCY_CACHE
    if _ADJACENCY_CACHE is None:
        ap_by_id = {ap['id']: ap for ap in wardb.accesspoints()}
        adj = {}
        for sl in wardb.supplylines():
            a = ap_by_id.get(sl.get('ap1'))
            b = ap_by_id.get(sl.get('ap2'))
            if not a or not b:
                continue
            bf1, bf2 = a.get('battlefield'), b.get('battlefield')
            if not bf1 or not bf2 or bf1 == bf2:
                continue
            adj.setdefault(bf1, []).append(
                (sl['id'], sl['ap1'], sl['ap2'], bf2))
            adj.setdefault(bf2, []).append(
                (sl['id'], sl['ap2'], sl['ap1'], bf1))
        _ADJACENCY_CACHE = adj
    return _ADJACENCY_CACHE


def find_supplyline_path(from_bf, to_bf):
    """BFS 最短补给线路径 → [{sl_id, from_ap, to_ap, from_bf, to_bf}]。

    起终点相同返回 []；不可达返回 None。"""
    from_bf, to_bf = int(from_bf or 0), int(to_bf or 0)
    if from_bf == to_bf:
        return []
    adj = supplyline_adjacency()
    prev = {from_bf: None}          # bf → (来源 bf, 边)
    queue = [from_bf]
    while queue:
        cur = queue.pop(0)
        for edge in adj.get(cur, ()):
            nb = edge[3]
            if nb in prev:
                continue
            prev[nb] = (cur, edge)
            if nb == to_bf:
                path = []
                node = to_bf
                while prev[node] is not None:
                    p, e = prev[node]
                    path.append({'sl_id': e[0], 'from_ap': e[1],
                                 'to_ap': e[2], 'from_bf': p, 'to_bf': node})
                    node = p
                path.reverse()
                return path
            queue.append(nb)
    return None


def _bf_pos():
    global _BF_POS_CACHE
    if _BF_POS_CACHE is None:
        _BF_POS_CACHE = {b['id']: (b['posx'], b['posy'])
                         for b in wardb.battlefields()}
    return _BF_POS_CACHE
