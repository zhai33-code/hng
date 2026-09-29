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


def current_morale(at, cfg=None, now=None):
    """官方 CommandnodeSchema.getMorale 口径的当前士气（物化）：
    DB morale 是 morale_updated_at 时刻的快照，当前值 =
    快照 + floor(gainedPerMinute × 已过分钟)，夹在 [min, max_morale]。
    服务器一切读取/判定/扣减士气前统一走此函数，与客户端显示保持一致；
    变动后由调用方写回新快照并重置 morale_updated_at。"""
    cfg = cfg or morale_defaults()
    max_m = int(at.get('max_morale') or cfg['max'])
    base = int(at.get('morale') or 0)
    ts = at.get('morale_updated_at') or at.get('created_at')
    regen = 0
    if ts:
        try:
            ts = _as_dt(ts)
            now = now or datetime.datetime.now(datetime.timezone.utc)
            if now.tzinfo is None:       # naive（FakeStore）按 UTC
                now = now.replace(tzinfo=datetime.timezone.utc)
            minutes = (now - ts).total_seconds() / 60.0
            if minutes > 0:
                regen = int(float(cfg['gained_per_minute']) * minutes)
        except (TypeError, ValueError, AttributeError):
            regen = 0
    return max(int(cfg['min']), min(max_m, base + regen))


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
        'queueReinforcements': mm_config.WAR_QUEUE_REINFORCEMENTS,
        'firstResourceFree': mm_config.WAR_FIRST_RESOURCE_FREE,
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


def battle_map_entity(wb):
    """battle 行 → (mapEntityId, mapEntityTypeId)（MissionInfo f5/f6 用，
    与 battle 行 f3/f4 同源：城战=城市 battlefield id/1、野外遭遇=
    补给线 id/3）。客户端按 f6 分流显名：type1=BattlefieldTable.bftitle
    城市名，type3=getMissionCityName 拼"城市名-方向字母"（修复野外遭遇
    局简报城市名取段起点城市的问题，docs/war-battle-details-missionid-
    bug.md §三.5 B1/B2）。行缺失回退 (0, 0)（调用方回退 record 旧值）。"""
    btype = int((wb or {}).get('battle_type') or BATTLE_TYPE_ASSAULT)
    ent_type = (MAP_ENTITY_BATTLEFIELD if btype == BATTLE_TYPE_ASSAULT
                else MAP_ENTITY_SUPPLYLINE)
    try:
        ent_id = int((wb or {}).get('battlefield_id') or 0)
    except (TypeError, ValueError):
        ent_id = 0
    return ent_id, ent_type


def battle_activation_ts(wb):
    """battle 行 → activationTimeStamp Zulu 串（= started_at + 准备期，
    与 battle 行 f12 同源同值）——war 会战局激活时刻的单一事实源。

    MissionInfo f11（GetMissionList / GetMissionDetails）复用：客户端
    MissionDetailsMediator 的"已激活时长" = serverTime − f11。此前 war 局
    复用 _zulu_activated()（固定回溯 1 小时，普通即时匹配局防 serverTime
    同步延迟的设计）→ 会战刚成立即显示"战斗已启动于 1 小时"（2026-09-11
    玩家反馈）。war 局发真实激活时刻：未过准备期 → 客户端显示集结倒计时
    （battle_ready_in，官方观感）；已过 → 真实激活时长。行缺失/时间缺失
    回退 ''（调用方回退旧 zulu）。"""
    started = (wb or {}).get('started_at')
    if not started:
        return ''
    activation = started
    if isinstance(started, datetime.datetime):
        activation = (started + datetime.timedelta(
            seconds=battle_prep_seconds((wb or {}).get('battle_type'))))
    return _fmt_ts(activation)


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
