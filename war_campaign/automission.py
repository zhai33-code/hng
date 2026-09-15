# -*- coding: utf-8 -*-
"""automission 阈值/tie 判定（war-gameplay-fix-plan §3/§4/§7/§8）。

承接原 production.automission_tick 主体改造：war battle 无人加入（mission
record state==0 / 缺失）超时后不再统一按「AT 剩余资源总量比较」结算，改为
war1/war2.md 社区口径（客户端零证据，数值全部 mm_config 可配）：

  §3 野外（encounter/skirmish）：准备期 WAR_BATTLE_PREP_FIELD（默认 300s）
      后无人加入 → 按本局 preset 据点数选门槛（≥WAR_AUTO_TP_CP 个据点 =
      三点图 WAR_AUTO_TP_MIN 216，否则单点图 WAR_AUTO_SP_MIN 144），步兵
      （_SOLDIER_CATS 编制合计）单向不足者败北；
  §4 城攻（assault，非伞兵攻方）：超时 WAR_AUTO_ATTACK_TIMEOUT（默认
      330s）攻方步兵 < 216 → 攻方败北；守方 < 216 → 攻方胜（城易主）；
      双方满足 → tie（§8）；
  §7 伞兵攻城（assault 且攻方 AT 为伞兵族）：超时 WAR_AUTO_PARADROP_
      TIMEOUT（默认 330s）攻方伞兵（cat4）< WAR_AUTO_PARA_MIN(288) →
      攻方败北 + 伞兵落地变步兵（convert_to *_on_ground 变体）；守方
      步兵 < 216 → 攻方胜占城；
  §8 tie（双方门槛均满足）：等 WAR_AUTO_TIE_HOURS（默认 6h）后比兵力
      少者败（持平无胜方收局）。

败方损耗 WAR_AUTO_LOSS（默认 0.25）编制 + 扣 WAR_MORALE_LOST_PER_BATTLE
士气；系统判定结算 is_auto=True：无 XP/rank/battles 计数与 warfunds
（war1.md「系统判定胜负不提供经验」，settle_war_battle is_auto 分支）。
真人局（mission state==1 且战斗会话存活）不触碰；会话已消亡的中途退场局
与无攻守上下文的局保留旧「资源总量比较」长超时（WAR_AUTOMISSION_NORMAL）
兜底。
"""

import time

import mm_config
import wardb
from state import log

from .common import (
    _SOLDIER_CATS,
    _as_resources,
    _ts_ms,
    BATTLE_TYPE_ASSAULT,
    )
from .lifecycle import (
    ensure_war,
    )
from .at_ops import (
    _tpl_of,
    is_paradrop_unit,
    land_paradrop_split,
    )


# armyresourcecategory cat 4 = paratroopers（§7 伞兵数口径）
PARATROOPER_CAT = 4


# 无攻守上下文/会话消亡 → 交回旧资源量比较兜底
_LEGACY = object()


def _troop_count(ats, cats):
    """编制合计：AT resources 中指定类别族数量之和（当前编制，含损耗）。"""
    total = 0
    for at in ats or ():
        for cat, v in _as_resources(at.get('resources')).items():
            try:
                if int(cat) in cats and isinstance(v, (int, float)) and v > 0:
                    total += int(v)
            except (TypeError, ValueError):
                continue
    return total


def _is_para_family(at):
    """伞兵族判定（§7，含落地变体）：actionid 12/13 或模板名 *_on_ground
    （空降到达即转落地变体，actionid 归属无从核实时按名兜底）。"""
    if is_paradrop_unit(at):
        return True
    return str(_tpl_of(at).get('name') or '').endswith('_on_ground')


def _preset_cp_count(preset_id):
    """本局 preset 据点数（§3 单点/三点门槛判据）：preset → gamemap →
    CapturepointTemplate 行数。查表失败回退 0（= 单点门槛，宁早判不挂死）。"""
    try:
        gamemap = int(wardb._load_preset(int(preset_id or 0)).get(3, 0) or 0)
        if not gamemap:
            return 0
        return len(wardb._load_capturepoint_templates(gamemap))
    except Exception:
        return 0


async def _paras_land(store, ats):
    """伞兵落地变步兵（§7 攻方败北）：convert_to *_on_ground 变体
    （已在落地变体的跳过）。运输机与空降到达同语义（官服 Spaatz）：
    拆成玩家名下独立 planes-AT 停原机场（战败伞兵后续损耗只扣落地
    编制 cat4，运输机在后方机场不受本局结算牵连）。返回追加推送
    事件。"""
    events = []
    for at in ats or ():
        if not _is_para_family(at):
            continue
        tpl = _tpl_of(at)
        if str(tpl.get('name') or '').endswith('_on_ground'):
            continue                      # 已是落地变体
        cvt = int(tpl.get('convert_to') or 0)
        if not cvt or cvt not in wardb.commandnode_templates():
            continue
        _at2, evs = await land_paradrop_split(
            store, int(at.get('war_id') or 0), at)
        events.extend(evs)
    return events


async def _judge_threshold(store, wb, mrec, now_s):
    """准备期阈值判定（§3/§4/§7/§8）。

    返回：
      None          未到判定时刻 / tie 窗口内（继续等）
      _LEGACY       无攻守上下文 → 交回旧资源量比较兜底
      (winner, extra_events)  立即结算（extra = 伞兵落地等追加事件）"""
    btype = int(wb.get('battle_type') or BATTLE_TYPE_ASSAULT)
    started = _ts_ms(wb.get('started_at')) / 1000.0
    entries = list(wb.get('factions') or [])
    # 攻守阵营：mission record 为准，缺省回退 factions 首行/首异阵营
    atk_fac = int((mrec or {}).get('attacker_faction') or 0)
    if not atk_fac and entries:
        atk_fac = int(entries[0].get('faction') or 0)
    def_fac = int((mrec or {}).get('defender_faction') or 0)
    if not def_fac and entries:
        def_fac = next((int(e.get('faction') or 0) for e in entries
                        if int(e.get('faction') or 0) != atk_fac), 0)
    if not atk_fac or not def_fac or atk_fac == def_fac:
        return _LEGACY
    # 参战 AT（state==2 在战；撤退/歼灭/释放的不计）
    ats = []
    for e in entries:
        at = await store.get_command_node(int(e.get('at_id') or 0))
        if at and int(at.get('state') or 0) == 2:
            ats.append(at)
    atk_ats = [a for a in ats if int(a.get('faction') or 0) == atk_fac]
    def_ats = [a for a in ats if int(a.get('faction') or 0) == def_fac]
    is_para = (btype == BATTLE_TYPE_ASSAULT
               and any(_is_para_family(a) for a in atk_ats))
    # ---- 准备期超时（§3 野外 300s / §4 城攻 330s / §7 伞兵 330s）----
    # 准备期结束只是战斗开始（activationTimeStamp，客户端切正向计时），
    # 系统判定在开战后 WAR_AUTO_BATTLE_GRACE（war1.md 4分30秒）才执行
    if btype == BATTLE_TYPE_ASSAULT:
        prep = (mm_config.WAR_AUTO_PARADROP_TIMEOUT if is_para
                else mm_config.WAR_AUTO_ATTACK_TIMEOUT)
    else:
        prep = mm_config.WAR_BATTLE_PREP_FIELD
    timeout = (max(1, int(prep))
               + max(0, int(mm_config.WAR_AUTO_BATTLE_GRACE)))
    if now_s - started < timeout:
        return None
    atk_inf = _troop_count(atk_ats, _SOLDIER_CATS)
    def_inf = _troop_count(def_ats, _SOLDIER_CATS)
    if btype == BATTLE_TYPE_ASSAULT:
        if is_para:
            # §7 伞兵攻城：288 攻 / 守 <216 败 / 双方满足 tie
            atk_para = _troop_count(atk_ats, {PARATROOPER_CAT})
            if atk_para < mm_config.WAR_AUTO_PARA_MIN:
                return def_fac, await _paras_land(store, atk_ats)
            if def_inf < mm_config.WAR_AUTO_TP_MIN:
                return atk_fac, []
            # 双方满足 → tie（§8）
        else:
            # §4 城攻：攻方 <216 败北；守方 <216 → 攻方胜（城易主）；
            # 双方满足 → tie（war1.md「双方都满足则等待6小时后比较兵力」，
            # 与 §7 伞兵分支同构——此前漏掉守方检查，守方满编也被判负）
            if atk_inf < mm_config.WAR_AUTO_TP_MIN:
                return def_fac, []
            if def_inf < mm_config.WAR_AUTO_TP_MIN:
                return atk_fac, []
            # 双方满足 → tie（§8）
    else:
        # §3 野外：据点数定门槛（三点 216 / 单点 144），单向不足者败
        thr = (mm_config.WAR_AUTO_TP_MIN
               if _preset_cp_count((mrec or {}).get('preset'))
               >= mm_config.WAR_AUTO_TP_CP
               else mm_config.WAR_AUTO_SP_MIN)
        if atk_inf < thr <= def_inf:
            return def_fac, []
        if def_inf < thr <= atk_inf:
            return atk_fac, []
        # 双方不足 / 双方满足 → tie（§8）
    # ---- tie：WAR_AUTO_TIE_HOURS 后兵力少者败（持平无胜方收局）----
    if now_s - started < timeout + mm_config.WAR_AUTO_TIE_HOURS * 3600.0:
        return None
    if atk_inf > def_inf:
        return atk_fac, []
    if def_inf > atk_inf:
        extra = await _paras_land(store, atk_ats) if is_para else []
        return def_fac, extra
    return 0, []


async def _power_winner(store, wb):
    """旧口径胜负：双方参战 AT 剩余资源总量多者胜（持平/皆空 = 无胜方，
    守方守住）。"""
    power = {}
    for entry in (wb.get('factions') or []):
        at = await store.get_command_node(int(entry.get('at_id') or 0))
        if not at or int(at.get('state') or 0) != 2:
            continue
        fac = int(at.get('faction') or 0)
        power[fac] = power.get(fac, 0) + sum(
            v for v in _as_resources(at.get('resources')).values()
            if isinstance(v, (int, float)))
    if power:
        best = max(power.values())
        tops = [f for f, p in power.items() if p == best]
        if best > 0 and len(tops) == 1:
            return tops[0]
    return 0


async def automission_tick(store):
    """war battle 无人加入超时自动结算（阈值/tie 判定，见模块 docstring）。
    由 realtest_server war_tick 周期调用；返回无值（事件已广播）。"""
    from .tick import broadcast_war_events, settle_war_battle
    if store is None or getattr(store, 'pool', None) is None:
        return
    try:
        war = await ensure_war(store)
        if war is None:
            return
        import combat_instances       # 延迟 import（防未来循环依赖）
        now_s = time.time()
        for wb in await store.list_active_war_battles(war['id']):
            try:
                mrec = await store.get_mission_by_battle(int(wb['id']))
            except Exception:
                mrec = None
            r_state = int((mrec or {}).get('state', 0) or 0)
            if (r_state == 1 and mrec is not None
                    and int(mrec['id']) in combat_instances.COMBAT_INSTANCES):
                continue      # 真人局战斗会话存活
            started = _ts_ms(wb.get('started_at')) / 1000.0
            if started <= 0:
                continue
            if r_state == 1 and mrec is not None:
                # 会话已消亡的中途退场局：旧口径长超时兜底
                if now_s - started < max(60, mm_config.WAR_AUTOMISSION_NORMAL):
                    continue
                winner = await _power_winner(store, wb)
                log(f'[war] automission: battle {wb["id"]} abandoned '
                    f'{int(now_s - started)}s, settling (winner={winner})')
                events = await settle_war_battle(
                    store, int(mrec.get('id') or 0), int(wb['id']), winner,
                    loss_ratios=(mm_config.WAR_AUTOMISSION_WINNER_LOSS,
                                 mm_config.WAR_AUTOMISSION_LOSER_LOSS),
                    is_auto=True)
                await broadcast_war_events(events)
                continue
            # ---- 准备期阈值判定（§3/§4/§7/§8）----
            judged = await _judge_threshold(store, wb, mrec, now_s)
            if judged is None:
                continue
            if judged is _LEGACY:
                if now_s - started < max(60, mm_config.WAR_AUTOMISSION_NORMAL):
                    continue
                winner = await _power_winner(store, wb)
                extra = []
            else:
                winner, extra = judged
            log(f'[war] automission: battle {wb["id"]} threshold-judged '
                f'idle {int(now_s - started)}s (winner={winner or "none"})')
            events = await settle_war_battle(
                store, int((mrec or {}).get('id') or 0), int(wb['id']),
                winner,
                loss_ratios=(0.0, mm_config.WAR_AUTO_LOSS),
                is_auto=True)      # 系统判定不提供经验（war1.md）
            await broadcast_war_events(list(extra) + list(events))
    except Exception as e:
        log(f'[war] automission_tick failed: {e}')
