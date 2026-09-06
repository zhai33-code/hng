# -*- coding: utf-8 -*-
"""阵营资源生产推进（600s/run 游标）与 automission tick。

原 war_campaign.py 阶段 3.5 拆分（docs/warserver-refactor-plan.md §3.5）：
符号逐字搬运，行为不变。"""

import time
import mm_config
import wardb
from state import log
from .common import (
    _as_resources,
    _ts_ms,
    )
from .lifecycle import (
    ensure_war,
    )
from .warmap_view import (
    hq_stock_rows,
    )
from .supply import (
    supply_windows,
    )


async def automission_tick(store):
    """M2-5 automission（协议 §8.4）：war battle 无人加入超时自动结算。

    每 active battle（state=0）按 mission record 判定：
      - record 缺失 / state==0（撮合未满，无人加入）：started_at 超过
        WAR_AUTOMISSION_NORMAL 秒 → 结算；
      - state==1 且战斗会话已不存在（全员中途退出，release_match 不回写
        record）：同样按超时结算；
      - state==1 且会话存活（COMBAT_INSTANCES 有实例）：live，跳过。
    胜负：双方参战 AT 剩余资源总量多者胜（持平/双方皆空 = 攻方不胜，守方
    守住）。损耗按 WAR_AUTOMISSION_WINNER_LOSS / LOSER_LOSS。
    由 realtest_server war_tick 周期调用；返回无值（事件已广播）。
    """
    from .tick import broadcast_war_events, settle_war_battle
    if store is None or getattr(store, 'pool', None) is None:
        return
    try:
        war = await ensure_war(store)
        if war is None:
            return
        import combat_instances       # 延迟 import（防未来循环依赖）
        now_s = time.time()
        timeout = max(60, mm_config.WAR_AUTOMISSION_NORMAL)
        for wb in await store.list_active_war_battles(war['id']):
            try:
                mrec = await store.get_mission_by_battle(int(wb['id']))
            except Exception:
                mrec = None
            r_state = int((mrec or {}).get('state', 0) or 0)
            if r_state == 1 and mrec is not None:
                if int(mrec['id']) in combat_instances.COMBAT_INSTANCES:
                    continue          # 成局且战斗会话存活
            started = _ts_ms(wb.get('started_at')) / 1000.0
            if started <= 0 or now_s - started < timeout:
                continue
            # ---- 超时：按 AT 剩余资源判胜负 ----
            power = {}
            for entry in (wb.get('factions') or []):
                at = await store.get_command_node(
                    int(entry.get('at_id') or 0))
                if not at or int(at.get('state') or 0) != 2:
                    continue
                fac = int(at.get('faction') or 0)
                power[fac] = power.get(fac, 0) + sum(
                    v for v in _as_resources(at.get('resources')).values()
                    if isinstance(v, (int, float)))
            winner = 0
            if power:
                best = max(power.values())
                tops = [f for f, p in power.items() if p == best]
                if best > 0 and len(tops) == 1:
                    winner = tops[0]
            log(f'[war] automission: battle {wb["id"]} idle '
                f'{int(now_s - started)}s, settling (winner={winner})')
            events = await settle_war_battle(
                store, int((mrec or {}).get('id') or 0), int(wb['id']),
                winner,
                loss_ratios=(mm_config.WAR_AUTOMISSION_WINNER_LOSS,
                             mm_config.WAR_AUTOMISSION_LOSER_LOSS))
            await broadcast_war_events(events)
    except Exception as e:
        log(f'[war] automission_tick failed: {e}')


# ---- M3-1 阵营资源生产（docs/hng-rts-protocol.md §9.1 / §3.12.1）----


_PROD_INTERVAL_MS = 600_000        # ArmyResourceProductionTemplate 全表恒 600s


_PROD_LAST_RUN = 0.0               # WAR_PRODUCTION_MIN_TICK 节流游标


async def production_tick(store):
    """阵营资源生产推进：wars.production_ms 游标按 600s/run 结算 runs，
    把模板产量累加进 war_faction_resources，并对 resourceview 订阅会话
    重推 FactionResourceProduction/Consumption 行 + 最高司令部 armyresource
    行（客户端不做本地生产模拟，不推则资源总览数值停滞）。

    游标语义：0 = 首跑只初始化游标（不补发开战前历史产量）；
    runs>0 时游标推进 cursor + runs*interval。war_tick 5s 周期调用，
    WAR_PRODUCTION_MIN_TICK 节流整函数。"""
    global _PROD_LAST_RUN
    if store is None or getattr(store, 'pool', None) is None:
        return
    now_s = time.time()
    if now_s - _PROD_LAST_RUN < max(5, mm_config.WAR_PRODUCTION_MIN_TICK):
        return
    _PROD_LAST_RUN = now_s
    try:
        war = await ensure_war(store)
        if war is None:
            return
        now_ms = int(now_s * 1000)
        cursor = int(war.get('production_ms') or 0)
        if cursor <= 0:
            await store.update_war_production(war['id'], now_ms)
            return
        runs = int((now_ms - cursor) // _PROD_INTERVAL_MS)
        if runs <= 0:
            return
        deltas = {}
        for t in wardb.armyresource_production():
            per = int(t.get('produce_per_run') or 0)
            if per <= 0:
                continue            # 死行 cat 13 等
            fac = int(t.get('faction') or 0)
            cat = int(t.get('category_id') or 0)
            d = deltas.setdefault(fac, {})
            d[cat] = d.get(cat, 0) + per * runs
        for fac, d in deltas.items():
            await store.adjust_war_faction_resources(war['id'], fac, d)
        await store.update_war_production(
            war['id'], cursor + runs * _PROD_INTERVAL_MS)
        log(f'[war] production: war={war["id"]} runs={runs} '
            f'factions={sorted(deltas)}')
        # ---- 行重推（resourceview 订阅会话，与订阅时同函数同 warId 源）----
        import mm_factions           # 延迟 import（防循环依赖）
        from state import FLASH_SESSIONS
        from messages import _push_kvcs
        windows = await supply_windows(store, war)
        prods, cons = mm_factions._msg_faction_resource_rows(windows)
        queue_rows = mm_factions._msg_faction_queue_rows(windows)
        hq_rows = await hq_stock_rows(store, war)
        for sess in list(FLASH_SESSIONS.values()):
            cmd = getattr(sess, 'resource_view_cmd', None)
            w = getattr(sess, 'writer', None)
            if not cmd or not w:
                continue
            try:
                for row in prods:
                    await _push_kvcs(w, cmd, b'FactionResourceProduction', row)
                for row in hq_rows:
                    await _push_kvcs(w, cmd, b'armyresource', row)
                for row in cons:
                    await _push_kvcs(w, cmd, b'FactionResourceConsumption', row)
                for row in queue_rows:
                    await _push_kvcs(w, cmd, b'FactionResourceQueue', row)
            except Exception:
                pass                # 单会话故障不影响其他订阅者
    except Exception as e:
        log(f'[war] production_tick failed: {e}')


# ---- M3-2 阵营资源增援队列（docs/hng-rts-protocol.md §3.12 / §10.2 #3/#5）----
