# -*- coding: utf-8 -*-
"""战争生命周期：初始城市归属分配、ensure_war 自动开战、check_victory 胜负判定与自动续战。

原 war_campaign.py 阶段 3.5 拆分（docs/warserver-refactor-plan.md §3.5）：
符号逐字搬运，行为不变。"""

import mm_config
import wardb
from state import log
from .common import (
    _war_config,
    rts_scope,
    )


def capital_battlefield_ids():
    """首都 battlefield id 集合（胜利点数判据）。"""
    return {c['battlefield_id'] for c in wardb.capitals()
            if c.get('battlefield_id')}


def effective_points_needed(stored=None):
    """胜利所需主城数（war f8 / war_catalogue f4 下发值与 check_victory 阈值）。

    基准 = 落库值（无值回退 MM_WAR_POINTS_VICTORY，官服 15=完整地图
    15 座主城），与 WarDatabase capital 表实际主城数取小：裁剪地图主城
    不足时按实际数——否则客户端按 capital 表数出总数 N，显示"占领 N 座
    城市中的 15 座"（N<15 永不可达，战争无法结束）。无 capital 表
    （空地图）回退基准值。"""
    base = int(stored or 0) or mm_config.WAR_POINTS_VICTORY
    caps = len(capital_battlefield_ids())
    return min(base, caps) if caps > 0 else base


def initial_city_owners():
    """开战初始城市归属 {battlefield_id: faction}。

    官服语义：仅 capital 表登记的城市有初始归属（default_faction），
    其余战场节点中立（0）。"""
    return {c['battlefield_id']: c['default_faction']
            for c in wardb.capitals()
            if c.get('battlefield_id') and c.get('default_faction')}


async def _next_war_name(store):
    """递进战争名：'<base> #N'（N=历史战争轮次，wars 只增不删 count+1）。
    统计不可用（FakeStore/DB 异常）回退 base 无后缀。"""
    base = mm_config.WAR_NAME
    try:
        n = int(await store.war_count()) + 1
        return f'{base} #{n}'
    except Exception:
        return base


async def ensure_war(store):
    """Find or atomically initialize the single running war."""
    if store is None or getattr(store, 'pool', None) is None:
        return None
    try:
        current = await store.get_active_war()
        if current is not None:
            return current
        async with rts_scope(store, 'creation') as bound:
            return await _ensure_war_unlocked(bound)
    except Exception as e:
        log(f'[war] ensure_war failed: {e}')
        return None


async def _ensure_war_unlocked(store):
    war = await store.get_active_war()
    if war is not None:
        return war
    if not mm_config.WAR_AUTO_CREATE:
        return None
    war_name = await _next_war_name(store)
    war = await store.create_war(
        war_name,
        points_needed_for_victory=effective_points_needed(),
        config=_war_config())
    await store.init_war_cities(war['id'], initial_city_owners())
    # 阵营种子库存 = ArmyResourceProductionTemplate warStartValue
    # （§3.12.1：步兵 85 万 / 重坦 225……幂等，已有行不覆盖）
    initial = {}
    for t in wardb.armyresource_production():
        v = int(t.get('war_start_value') or 0)
        if v > 0:
            initial.setdefault(int(t.get('faction') or 0), {})[
                int(t.get('category_id') or 0)] = v
    if initial:
        await store.init_war_faction_resources(war['id'], initial)
    log(f'[war] 新战争开打 id={war["id"]} name={war_name!r} '
        f'points={war.get("points_needed_for_victory")} '
        f'cities={len(initial_city_owners())} '
        f'stock_rows={sum(len(c) for c in initial.values())}')
    return war


async def _broadcast_war_rows(store, war_ids):
    """war 行 KVCS 推送到所有 playerview 订阅会话（战争结束/续战通知）。

    客户端 WarModel.onWarUpdates（WarTable.changeSignal）凭 war 行
    UPDATE 驱动全部战争终局链：f7 winningfactionid != 0 → 发
    query_war_catalogue_request{f1=结束war id} → WarIsOver 结算界面
    （"进入下一场战争"按钮读目录 f14 sequelWarId → join_war_request）。
    此前 end_war 只落库不推送 → 战争结束客户端完全无感知（用户报
    "没弹结算界面"）；续战新 war 行 ADD 同推（currentWar 切换）。
    push-and-forget：单会话故障不影响其他订阅者。"""
    from state import FLASH_SESSIONS
    from messages import _push_kvcs
    from .war_row import build_war_proto
    rows = []
    for wid in war_ids:
        try:
            w = await store.get_war(int(wid))
            if w is not None:
                rows.append(build_war_proto(w))
        except Exception as e:
            log(f'[war] get_war({wid}) for broadcast failed: {e}')
    if not rows:
        return
    for sess in list(FLASH_SESSIONS.values()):
        cmd = getattr(sess, 'playerview_cmd', None)
        wtr = getattr(sess, 'writer', None)
        if not cmd or not wtr:
            continue
        for payload in rows:
            try:
                await _push_kvcs(wtr, cmd, b'war', payload)
            except Exception:
                pass


async def check_victory(store, war=None):
    """检查当前战争胜利条件（两支，先判第一支）：

    ① 某阵营占领首都数 ≥ pointsNeededForVictory；
    ② 某阵营首都数 = 0（失光全部主城）→ 比较剩余两方首都数，多者胜
      （war1.md 胜利条件第二支；平分时平局，无胜方战争继续）。
    两支同时成立（极端）优先 ①。

    达成 → end_war（winning_faction）+ MM_WAR_SEQUEL_AUTO 时自动开续战
    （复用 ensure_war）。返回胜方 faction id；未达成返回 None。"""
    if store is None or getattr(store, 'pool', None) is None:
        return None
    try:
        if war is None:
            war = await store.get_active_war()
        if war is None:
            return None
        counts = await store.count_war_city_owners(
            war['id'], capital_battlefield_ids())
        needed = effective_points_needed(war.get('points_needed_for_victory'))
        winner = 0
        for faction in (1, 2, 3):
            if counts.get(faction, 0) >= needed:
                winner = faction
                break
        if not winner:
            # ② 第二支：某方失光全部主城 → 剩余两方首都数多者胜
            eliminated = [f for f in (1, 2, 3) if counts.get(f, 0) == 0]
            if eliminated:
                rest = [f for f in (1, 2, 3) if counts.get(f, 0) > 0]
                if len(rest) == 2:
                    a, b = rest
                    if counts[a] > counts[b]:
                        winner = a
                    elif counts[b] > counts[a]:
                        winner = b
                    # 平分：平局，无胜方战争继续
        if not winner:
            return None
        sequel = 0
        await store.end_war(war['id'], winner)
        if mm_config.WAR_SEQUEL_AUTO:
            nxt = await ensure_war(store)
            if nxt is not None:
                sequel = nxt['id']
                await store.update_war_sequel(war['id'], sequel)
        # ★ 广播 war 行（结束行 f7=胜方 + 续战行 ADD）：客户端凭此弹
        # WarIsOver 结算界面并切换新战争（链路见 _broadcast_war_rows 注释）
        await _broadcast_war_rows(store,
                                   [war['id']] + ([sequel] if sequel else []))
        if winner:
            log(f'[war] 战争 {war["id"]} 结束：阵营 {winner} 胜 '
                f'（首都 {counts}，needed={needed}），sequel={sequel}')
        return winner
    except Exception as e:
        log(f'[war] check_victory failed: {e}')
        return None


# ---- warmapview 快照（Phase 4，docs/hng-rts-protocol.md §4.3 时序）----
# 推送顺序 = WarMapViewMediator.setupWar 图层初始化顺序：
#   battlefieldstatus → supplylinestatus → accesspointstatus →
#   CommandNodeDefinition + CommandNodeWarInstance → CommandNodeMorale →
#   battle → armyresource → transport_commandnode_segment/destination
# ⚠ xmlTag 大小写逐字符匹配（硬约束 §7.4）；f1 id 为 SFIXED64 的表用
#   pb_sfixed64（硬约束 §7.5）；mapEntityTypeId 必须 ∈ {1,2,3}（§7.2）。


def _faction_capital_bf():
    """{faction: capital battlefield_id}（standby AT 落位用）。"""
    out = {}
    for c in wardb.capitals():
        if c.get('default_faction') and c.get('battlefield_id'):
            out.setdefault(c['default_faction'], c['battlefield_id'])
    return out
