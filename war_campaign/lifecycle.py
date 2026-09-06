# -*- coding: utf-8 -*-
"""战争生命周期：初始城市归属分配、ensure_war 自动开战、check_victory 胜负判定与自动续战。

原 war_campaign.py 阶段 3.5 拆分（docs/warserver-refactor-plan.md §3.5）：
符号逐字搬运，行为不变。"""

import mm_config
import wardb
from state import log
from .common import (
    _war_config,
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
    """取当前 running 战争；无且 MM_WAR_AUTO_CREATE=1 时自动开战。

    开战流程：create_war（递进名 + config 快照）→ init_war_cities（capital
    表初始归属）。返回 war dict 或 None（store 不可用 / 不自动开战）。"""
    if store is None or getattr(store, 'pool', None) is None:
        return None
    try:
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
    except Exception as e:
        log(f'[war] ensure_war failed: {e}')
        return None


async def check_victory(store, war=None):
    """检查当前战争胜利条件：某阵营占领首都数 ≥ pointsNeededForVictory。

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
            return None
        sequel = 0
        await store.end_war(war['id'], winner)
        if mm_config.WAR_SEQUEL_AUTO:
            nxt = await ensure_war(store)
            if nxt is not None:
                sequel = nxt['id']
                await store.update_war_sequel(war['id'], sequel)
        log(f'[war] 战争 {war["id"]} 结束：阵营 {winner} 占领 '
            f'{counts.get(winner, 0)} 首都 ≥ {needed}，sequel={sequel}')
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
