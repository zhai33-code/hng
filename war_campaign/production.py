# -*- coding: utf-8 -*-
"""阵营资源生产推进（600s/run 游标）与增援队列 tick。

原 war_campaign.py 阶段 3.5 拆分（docs/warserver-refactor-plan.md §3.5）。
automission_tick 主体已迁移 war_campaign/automission.py（war-gameplay-fix-
plan §3/§4/§7/§8 阈值/tie 判定），本模块保留符号转发。"""

import time
import mm_config
import wardb
from state import log
from .automission import automission_tick  # noqa: F401（转发，对外符号不变）
from .lifecycle import (
    ensure_war,
    )
from .warmap_view import (
    hq_stock_rows,
    )
from .supply import (
    supply_windows,
    )


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
