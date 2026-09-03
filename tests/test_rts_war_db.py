# -*- coding: utf-8 -*-
"""RTS 战争域持久层回归（player_store.py U8 迁移 + war 域方法）。

覆盖：
  A. 迁移存在性（wars/war_cities/commandnode_movements/war_battles/
     war_funds_ledger 表 + command_nodes 新列）
  B. war 生命周期：create_war → get_active_war → end_war（state=2）
  C. 城市归属：init_war_cities 批量 / set_city_owner / count_war_city_owners
     （首都过滤）
  D. AT 实例：create/get/update/list/delete_command_node（resources JSON
     往返、morale 默认 100、state=3 解散过滤）
  E. 移动：create_movement（path JSON 往返）/ get_active_movement /
     list_active_movements / finish_movement
  F. 会战桩：create_war_battle / list_active_war_battles / end_war_battle
  G. warfunds：add_war_funds（余额+流水事务）/ 不存在玩家 ValueError 回滚

环境：需要本地 PostgreSQL（默认 DSN hng:hng_password@127.0.0.1:5432/hng）。
不可达时整脚本 SKIP（exit 0），不算失败——与 test_mm_latejoin 的环境豁免
同策略；数据自清理（war 级联删除 + 临时玩家硬删）。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RESULTS = []


def check(label, ok, detail=''):
    RESULTS.append((label, ok))
    print(f'[{"PASS" if ok else "FAIL"}] {label}{("  " + detail) if detail else ""}')


async def main():
    import player_store

    st = player_store.PlayerStore()
    try:
        await st.start()
    except Exception as e:
        print(f'[SKIP] PostgreSQL 不可达（{e}），RTS DB 回归跳过')
        return 0

    wid = pid = 0
    try:
        # ---- A. 迁移存在性 ----
        async with st.pool.acquire() as conn:
            for tbl in ('wars', 'war_cities', 'commandnode_movements',
                        'war_battles', 'war_funds_ledger'):
                n = await conn.fetchval(
                    'SELECT count(*) FROM information_schema.tables '
                    'WHERE table_name = $1', tbl)
                check(f'表 {tbl} 存在', n == 1)
            for col in ('abbreviation', 'rank', 'xp', 'resources', 'morale',
                        'max_morale', 'state', 'map_entity_id',
                        'general_character_id', 'is_deployed', 'created_at'):
                n = await conn.fetchval(
                    'SELECT count(*) FROM information_schema.columns '
                    'WHERE table_name = $1 AND column_name = $2',
                    'command_nodes', col)
                check(f'command_nodes.{col} 存在', n == 1)

        # ---- B. war 生命周期 ----
        w = await st.create_war('rts-db-regression', points_needed_for_victory=15,
                                config={'strategytimescale': 1.0})
        wid = w['id']
        check('create_war 返回行', wid > 0 and w['state'] == 1,
              f'id={wid} state={w["state"]}')
        check('create_war config 快照', w['config'] == {'strategytimescale': 1.0})
        act = await st.get_active_war()
        check('get_active_war 命中本局', act and act['id'] == wid)
        check('get_war 按 id', (await st.get_war(wid))['id'] == wid)

        # ---- C. 城市归属 ----
        await st.init_war_cities(wid, {101: 1, 102: 2, 103: 0})
        await st.set_city_owner(wid, 103, 3)
        cities = dict(await st.get_war_cities(wid))
        check('init_war_cities 批量写', cities.get(101) == 1 and cities.get(102) == 2)
        check('set_city_owner 覆盖中立', cities.get(103) == 3)
        all_counts = await st.count_war_city_owners(wid)
        check('count_war_city_owners 全量', all_counts == {1: 1, 2: 1, 3: 1})
        cap_counts = await st.count_war_city_owners(wid, [101, 102])
        check('count_war_city_owners 首都过滤', cap_counts == {1: 1, 2: 1})

        # ---- D. AT 实例 ----
        at = await st.create_command_node(wid, 501, 7, 2, 'Test AT',
                                          resources={'inf': 40})
        aid = at['id']
        row = await st.get_command_node(aid)
        check('create_command_node 默认值',
              row['morale'] == 100 and row['state'] == 0
              and row['rank'] == 1 and row['is_deployed'] is False)
        ok = await st.update_command_node(aid, state=1, morale=80,
                                          resources={'inf': 35}, rank=2)
        row = await st.get_command_node(aid)
        check('update_command_node 字段生效',
              ok and row['state'] == 1 and row['morale'] == 80
              and row['resources'] == {'inf': 35} and row['rank'] == 2,
              f'state={row["state"]} morale={row["morale"]}')
        check('list_player_command_nodes 圈定战争',
              len(await st.list_player_command_nodes(7, wid)) == 1)
        check('list_war_command_nodes 含本 AT',
              any(r['id'] == aid for r in await st.list_war_command_nodes(wid)))

        # ---- E. 移动 ----
        mv = await st.create_movement(aid, wid, [101, 105, 108])
        check('create_movement path 往返', mv['path'] == [101, 105, 108])
        am = await st.get_active_movement(aid)
        check('get_active_movement 命中', am and am['id'] == mv['id'])
        check('list_active_movements 命中',
              len(await st.list_active_movements(wid)) == 1)
        check('finish_movement 结束',
              await st.finish_movement(mv['id'], 1)
              and await st.get_active_movement(aid) is None)
        check('结束后 list_active_movements 为空',
              await st.list_active_movements(wid) == [])

        # ---- F. 会战桩 ----
        wb = await st.create_war_battle(wid, 105, [{'at_id': aid, 'faction': 2}])
        check('create_war_battle factions 往返',
              wb and wb['factions'] == [{'at_id': aid, 'faction': 2}])
        check('list_active_war_battles 命中',
              len(await st.list_active_war_battles(wid)) == 1)
        check('end_war_battle 结束', await st.end_war_battle(wb['id'])
              and await st.list_active_war_battles(wid) == [])

        # ---- G. warfunds ----
        pid = await st.create_player('rts_db_smoke', 'x')
        if pid is None:  # 上次异常残留：按用户名找回
            async with st.pool.acquire() as conn:
                pid = await conn.fetchval(
                    'SELECT id FROM players WHERE username = $1',
                    'rts_db_smoke')
        bal0 = (await st.get_currencies(pid))['warfunds']
        bal1 = await st.add_war_funds(pid, 5000, 'rts-db-test')
        check('add_war_funds 加钱', bal1 == bal0 + 5000)
        bal2 = await st.add_war_funds(pid, -1000, 'rts-db-test')
        check('add_war_funds 扣钱', bal2 == bal1 - 1000)
        async with st.pool.acquire() as conn:
            n = await conn.fetchval(
                'SELECT count(*) FROM war_funds_ledger WHERE player_id = $1',
                pid)
            check('warfunds 流水 2 行', n == 2, f'n={n}')
        try:
            await st.add_war_funds(999999999, 100, 'no-such-player')
            check('add_war_funds 不存在玩家抛错', False)
        except ValueError:
            check('add_war_funds 不存在玩家抛错', True)

        # ---- B2. 收尾：解散 + 结束战争 ----
        check('delete_command_node 解散', await st.delete_command_node(aid))
        check('解散后 list 过滤',
              not any(r['id'] == aid
                      for r in await st.list_war_command_nodes(wid)))
        check('end_war 结算', await st.end_war(wid, 1)
              and (await st.get_war(wid))['state'] == 2)
    finally:
        # 清理：war 级联（war_cities/commandnode_movements 均挂外键），
        # command_nodes/war_battles 无 war 外键需手删
        if st.pool:
            async with st.pool.acquire() as conn:
                if wid:
                    await conn.execute('DELETE FROM war_battles WHERE war_id = $1', wid)
                    await conn.execute('DELETE FROM command_nodes WHERE war_id = $1', wid)
                    await conn.execute('DELETE FROM wars WHERE id = $1', wid)
                if pid:
                    await conn.execute('DELETE FROM war_funds_ledger WHERE player_id = $1', pid)
                    await conn.execute('DELETE FROM players WHERE id = $1', pid)
            await st.pool.close()

    fails = [l for l, ok in RESULTS if not ok]
    print(f'\n{"="*50}\nRTS DB 回归：{len(RESULTS)-len(fails)}/{len(RESULTS)} PASS')
    if fails:
        for l in fails:
            print(f'  FAIL: {l}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
