# -*- coding: utf-8 -*-
"""真实 DB 验证：新表结构迁移 + 新查询 + _load_db_loadout 端到端。

1. PlayerStore.start() 应用 _SCHEMA（含新列 ALTER TABLE ... IF NOT EXISTS）
2. information_schema 校验 7 个新列存在
3. 逐表跑新 SELECT（list_weapons 等含新列的查询）
4. player_service._load_db_loadout 端到端聚合

2026-08-21 模块化：_load_db_loadout 从 game_server 迁至 player_service；
DSN 不再硬编码 WSL IP（PlayerStore 自动探测 docker-desktop eth0）。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NEW_COLS = [
    ('weapons', 'condition'), ('weapons', 'clip_count'),
    ('weapon_modifiers', 'condition'), ('vehicles', 'condition'),
    ('vehicle_ammo', 'spawn_clip_count'),
    ('characters', 'squad_points'), ('characters', 'receive_daily_bonus'),
]

# 2026-08-21 迁移验证：char_id → character_id rename + unlock 行表化
RENAME_TABLES = ['weapons', 'weapon_modifiers', 'weapon_ammo', 'vehicles',
                 'vehicle_upgrades', 'vehicle_ammo', 'uniform_variants',
                 'weapon_variants', 'vehicle_variants']


async def main():
    import player_store
    import player_service
    import state
    st = player_store.PlayerStore()          # 默认 DSN → 自动探测 WSL docker IP
    await st.start()
    print('== 1. 迁移应用（start() OK）==')
    ok = fail = 0

    async with st.pool.acquire() as conn:
        print('== 2. 新列校验 ==')
        for tbl, col in NEW_COLS:
            has = await conn.fetchval(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = $1 AND column_name = $2", tbl, col)
            mark = 'PASS' if has else 'FAIL'
            ok += has; fail += (not has)
            print(f'  {mark}  {tbl}.{col}')
        # 默认值抽查
        wd = await conn.fetchval(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name='weapons' AND column_name='condition'")
        print(f"  weapons.condition default = {wd}")

    async with st.pool.acquire() as conn:
        print('== 2b. 2026-08-21 迁移验证（rename + unlock 行表）==')
        for tbl in RENAME_TABLES:
            has_new = await conn.fetchval(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = $1 AND column_name = 'character_id'", tbl)
            has_old = await conn.fetchval(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = $1 AND column_name = 'char_id'", tbl)
            good = has_new and not has_old
            ok += good; fail += (not good)
            print(f'  {"PASS" if good else "FAIL"}  {tbl}.character_id'
                  f'（旧列残留: {bool(has_old)}）')
        has_unlocks = await conn.fetchval(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'character_extra_unlocks' AND column_name = 'unlock_uid'")
        ok += has_unlocks; fail += (not has_unlocks)
        print(f'  {"PASS" if has_unlocks else "FAIL"}  character_extra_unlocks 行表存在')
        legacy_col = await conn.fetchval(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'characters' AND column_name = 'extra_unlock_uids'")
        good = not legacy_col
        ok += good; fail += (not good)
        print(f'  {"PASS" if good else "FAIL"}  characters.extra_unlock_uids 已删除')
        unlock_rows = await conn.fetchval(
            'SELECT count(*) FROM character_extra_unlocks')
        print(f'  character_extra_unlocks 迁入数据 = {unlock_rows} 行')

    print('== 3. 新 SELECT 查询（含新列）==')
    pid = await st.get_player('DoubleBird')
    print(f'  DoubleBird player_id = {pid}')
    if pid is None:
        print('  FAIL: DoubleBird 账号不存在')
        await st.close(); return
    for label, rows in [
        ('list_weapons', await st.list_weapons(pid)),
        ('list_weapon_ammo', await st.list_weapon_ammo(pid)),
        ('list_weapon_modifiers', await st.list_weapon_modifiers(pid)),
        ('list_vehicles', await st.list_vehicles(pid)),
        ('list_vehicle_upgrades', await st.list_vehicle_upgrades(pid)),
        ('list_vehicle_ammo', await st.list_vehicle_ammo(pid)),
        ('list_uniform_variants', await st.list_uniform_variants(pid)),
        ('list_uniform_parts', await st.list_uniform_parts(pid)),
        ('list_characters', await st.list_characters(pid)),
    ]:
        good = all(('condition' not in r.keys() or r['condition'] is not None)
                   and ('clip_count' not in r.keys() or r['clip_count'] is not None)
                   for r in rows)
        mark = 'PASS' if good else 'FAIL'
        ok += good; fail += (not good)
        extra = ''
        if rows and label == 'list_weapons':
            w = rows[0]
            extra = f"  首条 condition={w['condition']} clip_count={w['clip_count']}"
        if rows and label == 'list_vehicle_ammo':
            a = rows[0]
            extra = f"  首条 spawn_clip_count={a['spawn_clip_count']}"
        if rows and label == 'list_characters':
            c = rows[0]
            extra = f"  首条 squad_points={c['squad_points']} daily_bonus={c['receive_daily_bonus']}"
        print(f'  {mark}  {label}: {len(rows)} 行{extra}')

    print('== 3b. 默认军服不变量（每角色 uniform_variant_id > 0，f9 数据源）==')
    for ch in await st.list_characters(pid):
        good = int(ch.get('uniform_variant_id', 0) or 0) > 0
        ok += good; fail += (not good)
        print(f'  {"PASS" if good else "FAIL"}  {ch["name"]}(id={ch["id"]}): '
              f'uniform_variant_id={ch["uniform_variant_id"]}')

    print('== 3c. unlock 行表读路径（get_extra_unlock_uids）==')
    for ch in await st.list_characters(pid):
        cid = int(ch['id'])
        try:
            uids = await st.get_extra_unlock_uids(pid, cid)
            ok += 1
            print(f'  PASS  {ch["name"]}(id={cid}): {len(uids)} 个解锁 uid'
                  f'{sorted(uids)[:8] if uids else ""}')
        except Exception as e:
            fail += 1
            print(f'  FAIL  {ch["name"]}(id={cid}): {e}')

    print('== 4. _load_db_loadout 端到端 ==')
    # from state import STORE 是值绑定 → state 与 player_service 两处都要换
    state.STORE = st
    player_service.STORE = st
    lo = await player_service._load_db_loadout(pid)
    if lo is None:
        print('  FAIL: loadout 为 None')
        fail += 1
    else:
        chars = set(lo['weapons']) | set(lo['vehicles']) | set(lo['u_parts'])
        print(f"  PASS  聚合成功：weapons={sum(len(v) for v in lo['weapons'].values())} "
              f"vehicles={sum(len(v) for v in lo['vehicles'].values())} "
              f"w_ammo={sum(len(v) for v in lo['w_ammo'].values())} "
              f"v_ammo={sum(len(v) for v in lo['v_ammo'].values())} "
              f"uv_tmpl={len(lo['uv_tmpl'])} u_parts={sum(len(v) for v in lo['u_parts'].values())}")
        print(f'        涉及角色 char_id: {sorted(chars)}')
        ok += 1

    # ================= U9 增补：新表读写用例 =================
    # 战报存取 / 统计累加 / 生涯进度 / 好友 / 邮件 / 经济道具
    import time as _time
    print('== 5. U9 新表读写（战报/统计/生涯/好友/邮件/经济）==')
    ta = await st.get_player('u9db_a') or await st.create_player('u9db_a', 'pw')
    tb = await st.get_player('u9db_b') or await st.create_player('u9db_b', 'pw')
    ca = await st.get_latest_character(ta)
    if ca is None:
        await st.create_character(ta, 'U9DbSoldier', 1, 0)
        ca = await st.get_latest_character(ta)
    print(f'  测试账号 a={ta} b={tb} char={ca["id"]}')

    def mark(label, good, extra=''):
        nonlocal ok, fail
        ok += bool(good); fail += (not good)
        print(f'  {"PASS" if good else "FAIL"}  {label}{extra}')

    # 清理旧测试数据
    async with st.pool.acquire() as conn:
        await conn.execute('DELETE FROM friends WHERE player_id IN ($1,$2) '
                           'AND friend_id IN ($1,$2)', int(ta), int(tb))
        await conn.execute('DELETE FROM mail_messages WHERE sender_id IN ($1,$2) '
                           'AND recipient_id IN ($1,$2)', int(ta), int(tb))
        await conn.execute('DELETE FROM player_boosters WHERE player_id = $1', int(ta))
        await conn.execute('DELETE FROM player_tickets WHERE player_id = $1', int(ta))
        await conn.execute('DELETE FROM character_stats WHERE character_id = $1',
                           int(ca['id']))
        await conn.execute('DELETE FROM career_orders WHERE player_id = $1', int(ta))

    # 5.1 战报存取（入库 → 读回 → 幂等重写 → 最近列表 → 级联清理）
    msid = int(_time.time() * 1000) & 0x7FFFFFFFFFFFFFFF
    rep = {'mission_session_id': msid, 'mission_id': 11, 'mission_preset_id': 3,
           'winner_faction_id': 1, 'defender_faction_id': 2,
           'mission_time_seconds': 620, 'battle_start_time': 1700000000}
    plist = [{'player_id': int(ta), 'gamertag': 'U9DbA', 'squad_name': 'SQ',
              'score': 1200, 'captures': 3, 'kills': 7, 'deaths': 2, 'is_bot': False,
              'characters': [{'template': 1}]},
             {'player_id': 0, 'gamertag': 'BotIvan', 'squad_name': '', 'score': 300,
              'captures': 0, 'kills': 1, 'deaths': 5, 'is_bot': True, 'characters': []}]
    rid = await st.save_battle_report(rep, plist)
    r2, ps = await st.get_battle_report(rid)
    good = (int(r2['mission_session_id']) == msid and len(ps) == 2
            and ps[0]['gamertag'] == 'U9DbA' and ps[0]['score'] == 1200)
    mark('战报入库+读回', good, f' report_id={rid} players={len(ps)}')
    # 幂等：同 msid 重写 players 不重复
    rid2 = await st.save_battle_report(rep, plist[:1])
    _, ps2 = await st.get_battle_report(rid2)
    mark('战报幂等重写', rid2 == rid and len(ps2) == 1, f' players={len(ps2)}')
    ids = await st.list_recent_report_ids(ta, 5)
    mark('最近战报列表', rid in ids, f' {len(ids)} 条')
    async with st.pool.acquire() as conn:
        await conn.execute('DELETE FROM battle_reports WHERE id = $1', rid)
    gone = await st.get_battle_report(rid)
    mark('战报级联清理', gone is None)

    # 5.2 统计累加（add_stat 增量 + set_stat 绝对 + get_stats）
    v1 = await st.add_stat(ca['id'], 'kills', 5)
    v2 = await st.add_stat(ca['id'], 'kills', 3)
    await st.set_stat(ca['id'], 'longest_kill_distance', 152.5)
    stats = await st.get_stats(ca['id'])
    mark('统计累加/绝对写', v1 == 5 and v2 == 8
         and abs(stats.get('kills', 0) - 8) < 1e-9
         and abs(stats.get('longest_kill_distance', 0) - 152.5) < 1e-9,
         f" kills={stats.get('kills')}")

    # 5.3 生涯进度（upsert 累加 → 完成态）
    p1, s1 = await st.upsert_career_order_progress(ta, 999901, 1)
    p2, s2 = await st.upsert_career_order_progress(ta, 999901, 1)
    mark('生涯军令进度', p1 == 1 and s1 == 1 and p2 == 2 and s2 == 1,
         f' progress={p2} status={s2}')

    # 5.4 好友（a→b 请求 → b→a 转正 → 列表 → 删除）
    r = await st.add_friend(ta, tb)
    rows = await st.list_friend_rows(ta)
    r2 = await st.add_friend(tb, ta)
    rows2 = await st.list_friend_rows(ta)
    good = (r == 'ok' and r2 == 'accepted' and len(rows2) == 1
            and rows2[0]['id'] == int(tb) and rows2[0]['also_friend'])
    mark('好友请求→转正→列表', good)
    deleted = await st.remove_friend(ta, tb)
    rows3 = await st.list_friend_rows(ta)
    mark('好友删除(双向)', deleted and not rows3)

    # 5.5 邮件（发送 → 线程组装 → 回复继承线程）
    (m1,) = await st.send_mail(ta, [tb], 'U9 subject', 'U9 body')
    (m2,) = await st.send_mail(tb, [ta], 'Re: U9 subject', 'U9 reply', reply_to=m1)
    threads_b = await st.get_mail_threads(tb)
    t_b = next((t for t in threads_b if t['thread_id'] == m1), None)
    threads_a = await st.get_mail_threads(ta)
    t_a = next((t for t in threads_a if t['thread_id'] == m1), None)
    good = (t_b is not None and len(t_b['messages']) == 1
            and t_b['messages'][0]['subject'] == 'U9 subject'
            and t_a is not None and len(t_a['messages']) == 1
            and t_a['messages'][0]['thread_id'] == m1)
    mark('邮件发送+线程组装', good, f' m1={m1} m2={m2}')

    # 5.6 经济道具（加成：购→列→激活→重复激活209；门票同构）
    bid = await st.add_booster(ta, 2001, activate_now=False)
    rows = await st.list_boosters(ta)
    unact = next((b for b in rows if b['id'] == bid), None)
    code = await st.activate_booster(ta, bid)
    rows = await st.list_boosters(ta)
    act = next((b for b in rows if b['id'] == bid), None)
    code2 = await st.activate_booster(ta, bid)
    mark('加成 购/列/激活/防重', unact is not None and unact['expires_at'] is None
         and code == 1 and act is not None and act['expires_at'] is not None
         and code2 == 209)
    tid = await st.add_ticket(ta, 3001, is_timed=False)
    trows = await st.list_tickets(ta)
    trow = next((t for t in trows if t['id'] == tid), None)
    tcode = await st.activate_ticket(ta, tid)
    trows = await st.list_tickets(ta, timed_only=True)
    mark('门票 购/列/限时激活', trow is not None and trow['expires_at'] is None
         and tcode == 1 and any(t['id'] == tid for t in trows))

    # 收尾清理（测试账号保留，行数据清除）
    async with st.pool.acquire() as conn:
        await conn.execute('DELETE FROM mail_messages WHERE sender_id IN ($1,$2) '
                           'AND recipient_id IN ($1,$2)', int(ta), int(tb))
        await conn.execute('DELETE FROM player_boosters WHERE player_id = $1', int(ta))
        await conn.execute('DELETE FROM player_tickets WHERE player_id = $1', int(ta))
        await conn.execute('DELETE FROM character_stats WHERE character_id = $1',
                           int(ca['id']))
        await conn.execute('DELETE FROM career_orders WHERE player_id = $1', int(ta))
    print('  [cleanup] U9 测试行已清理')

    await st.close()
    print(f'\n{"ALL PASS" if fail == 0 else f"{fail} FAILED"} (ok={ok})')


asyncio.run(main())
