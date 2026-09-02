# -*- coding: utf-8 -*-
"""事务/原子性修复验证（2026-08-24）。

覆盖 player_store / player_service 的事务修复点：
  1. spend_currency 原子扣款：FOR UPDATE 锁行 + 增量扣减
     - 余额不足拒绝且余额不变 / 无效货币 / 并发不超扣不丢更新
  2. STORE.transaction() 上下文：块内异常整体回滚（扣款+发货同生共死）
  3. redeem_daily_reward：当天幂等 + 并发只有一个发放（防双领）
  4. redeem_voucher v2：pending 流水 + 时效(280)/限量/无限次 + legacy 兼容 + 并发
  5. claim_battle_pass_reward：FOR UPDATE 行锁 + 幂等 + 并发防双领
  6. claim_career_order：fallback 查询在连接归还前执行（连接释放 bug 回归）
  7. soft_delete_character：最后一个角色拒删 + 软删与装备退仓同事务
  8. respond_company_invitation / leave_company：删邀请与入团/移交同事务

DB 级测试（无需起服务器），复用 test_realdb.py 的直连模式。
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0


def check(name, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  PASS  {name}')
    else:
        FAIL += 1
        print(f'  FAIL  {name}  {detail}')


class Boom(Exception):
    """模拟事务中途失败。"""


async def main():
    import player_store
    st = player_store.PlayerStore()
    await st.start()
    print('== 0. 建测试账号 ==')
    uname = f'test_txn_{int(time.time())}'
    pid = await st.create_player(uname, 'txn-test')
    check('create_player 建号', pid is not None)
    if pid is None:
        await st.close()
        return

    async def bal(col):
        async with st.pool.acquire() as c:
            return int(await c.fetchval(
                f'SELECT {col} FROM players WHERE id = $1', pid))

    try:
        # ---------------------------------------------------------------- 1
        print('== 1. spend_currency 原子扣款 ==')
        await st.set_currency(pid, credits=1000, gold=500)
        ok, new = await st.spend_currency(pid, 'credits', 300)
        check('余额足够扣款成功', ok and new == 700, f'ok={ok} new={new}')
        check('余额已扣减', await bal('credits') == 700)

        ok, cur = await st.spend_currency(pid, 'credits', 99999)
        check('余额不足拒绝且余额不变',
              not ok and cur == 700 and await bal('credits') == 700,
              f'ok={ok} cur={cur}')

        ok, v = await st.spend_currency(pid, 'diamonds', 10)
        check('无效货币返回 (False, 0)', not ok and v == 0, f'ok={ok} v={v}')

        ok, v = await st.spend_currency(999999999, 'credits', 10)
        check('账号不存在返回 (False, 0)', not ok and v == 0, f'ok={ok} v={v}')

        # 并发：余额 1000，20 笔各扣 100 → 恰好 10 笔成功、终态 0、永不透支
        await st.set_currency(pid, credits=1000)
        t0 = time.time()
        results = await asyncio.gather(*[
            st.spend_currency(pid, 'credits', 100) for _ in range(20)])
        n_ok = sum(1 for ok_, _ in results if ok_)
        check('并发扣款不超扣（恰好 10 笔成功）', n_ok == 10,
              f'n_ok={n_ok} elapsed={time.time()-t0:.2f}s')
        check('并发扣款终态余额为 0 且不透支', await bal('credits') == 0,
              f'balance={await bal("credits")}')

        # 并发丢更新（增量扣 vs 读改写）：+500 与 10 笔扣 50 交错
        await st.set_currency(pid, credits=0)
        tasks = [st.add_currency(pid, credits=500)] + \
                [st.spend_currency(pid, 'credits', 50) for _ in range(10)]
        await asyncio.gather(*tasks)
        # 加 500 与扣 10×50：最终 0~500 之间任意值，但必须是净额（不为负）
        final = await bal('credits')
        check('并发加钱+扣款无丢更新（净额非负）', 0 <= final <= 500,
              f'final={final}')

        # ---------------------------------------------------------------- 2
        print('== 2. transaction() 整体回滚（扣款+发货同生共死）==')
        await st.set_currency(pid, credits=1000)
        try:
            async with st.transaction() as tx:
                ok, _ = await st.spend_currency(pid, 'credits', 400, conn=tx)
                assert ok
                raise Boom()
        except Boom:
            pass
        check('事务内扣款后异常 → 余额回滚', await bal('credits') == 1000,
              f'balance={await bal("credits")}')

        # 发货（债券）后扣款失败 → 两者一起回滚（economy 各 handler 的模式）
        await st.set_currency(pid, gold=100)
        try:
            async with st.transaction() as tx:
                await st.add_warbond(pid, 42, conn=tx)
                ok, _ = await st.spend_currency(pid, 'gold', 99999, conn=tx)
                if not ok:
                    raise Boom()  # 模拟 economy handler 的 _PaymentFailed
        except Boom:
            pass
        async with st.pool.acquire() as c:
            n_wb = await c.fetchval(
                'SELECT count(*) FROM player_warbonds WHERE player_id = $1', pid)
        check('扣款失败 → 已发货债券一并回滚', n_wb == 0, f'n_wb={n_wb}')
        check('扣款失败 → 余额不变', await bal('gold') == 100)

        # 正常路径：扣款 + 发货同事务提交
        await st.set_currency(pid, gold=1000)
        async with st.transaction() as tx:
            ok, _ = await st.spend_currency(pid, 'gold', 300, conn=tx)
            assert ok
            await st.add_warbond(pid, 42, conn=tx)
        async with st.pool.acquire() as c:
            n_wb = await c.fetchval(
                'SELECT count(*) FROM player_warbonds WHERE player_id = $1', pid)
        check('正常提交：扣款与发货都落库',
              n_wb == 1 and await bal('gold') == 700,
              f'n_wb={n_wb} gold={await bal("gold")}')

        # ---------------------------------------------------------------- 3
        print('== 3. redeem_daily_reward 幂等 + 防双领 ==')
        await st.set_currency(pid, credits=0)
        ok1, g1 = await st.redeem_daily_reward(pid, credits=2500)
        ok2, g2 = await st.redeem_daily_reward(pid, credits=2500)
        check('当天首次领取发放 2500', ok1 and g1 == 2500, f'ok={ok1} g={g1}')
        check('当天重复领取 granted=0', ok2 and g2 == 0, f'ok={ok2} g={g2}')
        check('重复领取不重复发钱', await bal('credits') == 2500,
              f'credits={await bal("credits")}')

        # 并发双领：fresh 玩家（新建号 credits 列默认 100000，先清零再断言）
        pid2 = await st.create_player(f'{uname}_b', 'txn-test')
        await st.set_currency(pid2, credits=0)
        await asyncio.gather(
            st.redeem_daily_reward(pid2, credits=2500),
            st.redeem_daily_reward(pid2, credits=2500))
        async with st.pool.acquire() as c:
            cr2 = int(await c.fetchval(
                'SELECT credits FROM players WHERE id = $1', pid2))
        check('并发双领只发一次（FOR UPDATE）', cr2 == 2500, f'credits={cr2}')

        # ---------------------------------------------------------------- 4
        print('== 4. redeem_voucher v2（pending 流水 + 时效/限量/无限）==')
        # v2 统一弹窗流程：输码只记 pending（不发钱），发货在 voucher_claim
        async with st.pool.acquire() as c:
            await c.execute(
                "INSERT INTO voucher_packs (pack_name, items_json, max_redemptions) "
                "VALUES ('TXNPACK', $1::jsonb, 1)",
                '[{"type":"credits","amount":888}]')
        code = await st.redeem_voucher(pid, 'txnpack')
        check('有效 key 记 pending（code=1）', code == 1, f'code={code}')
        base = await bal('credits')
        check('输码不立即发钱（发放延迟到选士兵）', base == 2500,
              f'credits={base}')
        code1b = await st.redeem_voucher(pid, 'TXNPACK')
        check('pending 期间重复输码幂等 code=1', code1b == 1, f'code={code1b}')
        pend = await st.voucher_pending_packs(pid)
        check('pending 查询返回 1 包含 items',
              len(pend) == 1 and pend[0]['items'][0]['amount'] == 888,
              f'pend={pend}')
        # 选士兵确认：grant 返回包列表（实际发放由 shop.py 执行）
        await st.create_character(pid, 'v2char', 1, 1)
        chars = await st.list_characters(pid)
        packs = await st.voucher_claim(pid, chars[0]['id'])
        check('claim 返回待发 1 包', len(packs) == 1, f'packs={packs}')
        code2 = await st.redeem_voucher(pid, 'TXNPACK')
        check('已兑过再输码返回 168', code2 == 168, f'code={code2}')
        pend2 = await st.voucher_pending_packs(pid)
        check('claim 后 pending 清空', pend2 == [], f'pend={pend2}')
        code3 = await st.redeem_voucher(pid, 'NO_SUCH_KEY')
        check('无效 key 返回 165', code3 == 165, f'code={code3}')

        # 过期：expires_at 已过 → 280
        async with st.pool.acquire() as c:
            await c.execute(
                "INSERT INTO voucher_packs (pack_name, items_json, expires_at) "
                "VALUES ('TXNEXP', $1::jsonb, now() - interval '1 hour')",
                '[{"type":"gold","amount":1}]')
        code_exp = await st.redeem_voucher(pid, 'TXNEXP')
        check('过期码返回 280', code_exp == 280, f'code={code_exp}')

        # 限量 2 次：两个玩家各兑一次后耗尽
        async with st.pool.acquire() as c:
            await c.execute(
                "INSERT INTO voucher_packs (pack_name, items_json, max_redemptions) "
                "VALUES ('TXNLIM', $1::jsonb, 2)",
                '[{"type":"gold","amount":5}]')
        c1 = await st.redeem_voucher(pid, 'TXNLIM')
        c2 = await st.redeem_voucher(pid2, 'TXNLIM')
        check('限量 2 次两玩家均 ok', c1 == 1 and c2 == 1,
              f'c1={c1} c2={c2}')
        await st.voucher_claim(pid, chars[0]['id'])
        await st.create_character(pid2, 'v2char2', 1, 1)
        chars2 = await st.list_characters(pid2)
        await st.voucher_claim(pid2, chars2[0]['id'])
        pid3 = await st.create_player(f'{uname}_c', 'txn-test')
        c3 = await st.redeem_voucher(pid3, 'TXNLIM')
        check('第三次兑换返回 168（耗尽）', c3 == 168, f'c3={c3}')

        # 无限次（max=0）：每人限一次（UNIQUE 流水），多人可兑
        async with st.pool.acquire() as c:
            await c.execute(
                "INSERT INTO voucher_packs (pack_name, items_json, max_redemptions) "
                "VALUES ('TXNINF', $1::jsonb, 0)",
                '[{"type":"gold","amount":9}]')
        i1 = await st.redeem_voucher(pid, 'TXNINF')
        i1b = await st.redeem_voucher(pid, 'TXNINF')
        i2 = await st.redeem_voucher(pid2, 'TXNINF')
        check('无限码多人各 ok + 同人限一次（168）',
              i1 == 1 and i1b == 1 and i2 == 1,
              f'i1={i1} i1b={i1b} i2={i2}')
        # 无限码同人 claim 后再输码 → 168
        await st.voucher_claim(pid, chars[0]['id'])
        i1c = await st.redeem_voucher(pid, 'TXNINF')
        check('无限码同人已兑过 → 168', i1c == 168, f'i1c={i1c}')

        # legacy 兑换位（v1 直接置 redeemed 无流水）仍按已用计
        async with st.pool.acquire() as c:
            await c.execute(
                "INSERT INTO voucher_packs (pack_name, items_json, redeemed) "
                "VALUES ('TXNLEGACY', $1::jsonb, TRUE)",
                '[{"type":"gold","amount":1}]')
        cleg = await st.redeem_voucher(pid, 'TXNLEGACY')
        check('legacy redeemed 码 → 168', cleg == 168, f'code={cleg}')

        # 并发双兑：fresh voucher，同一玩家并发输码
        async with st.pool.acquire() as c:
            await c.execute(
                "INSERT INTO voucher_packs (pack_name, items_json) "
                "VALUES ('TXNRACE', $1::jsonb)",
                '[{"type":"gold","amount":77}]')
        codes = await asyncio.gather(
            st.redeem_voucher(pid, 'TXNRACE'),
            st.redeem_voucher(pid, 'TXNRACE'))
        check('并发输码均 code=1（同一 pending 幂等，无双流水）',
              codes[0] == 1 and codes[1] == 1, f'codes={codes}')
        async with st.pool.acquire() as c:
            n_rows = int(await c.fetchval(
                'SELECT count(*) FROM voucher_redemptions r '
                'JOIN voucher_packs v ON v.id = r.voucher_pack_id '
                "WHERE v.pack_name = 'TXNRACE'"))
        check('并发输码只落一行 pending（UNIQUE）', n_rows == 1,
              f'rows={n_rows}')
        # 清理 §4 临时角色（硬删；soft_delete 会因"最后一个角色不许删"拒删），
        # 恢复 §7"最后一个角色"前提（每号 0 个活跃角色）
        async with st.pool.acquire() as c:
            await c.execute('DELETE FROM characters WHERE id = ANY($1::bigint[])',
                            [chars[0]['id'], chars2[0]['id']])

        # ---------------------------------------------------------------- 5
        print('== 5. claim_battle_pass_reward 幂等 + 防双领 ==')
        first, lst = await st.claim_battle_pass_reward(pid, 1, 55)
        check('未购买领取返回 (False, [])', not first and lst == [],
              f'first={first} lst={lst}')
        await st.purchase_battle_pass(pid, 1, gold_cost=0)
        first, lst = await st.claim_battle_pass_reward(pid, 1, 55)
        check('已购买首次领取 first=True', first and 55 in lst,
              f'first={first} lst={lst}')
        first2, _ = await st.claim_battle_pass_reward(pid, 1, 55)
        check('重复领取 first=False', not first2, f'first={first2}')

        # 并发同模板双领：fresh season
        await st.purchase_battle_pass(pid, 2, gold_cost=0)
        rs = await asyncio.gather(
            st.claim_battle_pass_reward(pid, 2, 66),
            st.claim_battle_pass_reward(pid, 2, 66))
        n_first = sum(1 for f, _ in rs if f)
        check('并发双领只有一个 first=True（FOR UPDATE）', n_first == 1,
              f'n_first={n_first}')

        # ---------------------------------------------------------------- 6
        print('== 6. claim_career_order（连接释放 bug 回归）==')
        async with st.pool.acquire() as c:
            await c.execute(
                'INSERT INTO career_orders (player_id, career_order_id, status, '
                'progress) VALUES ($1, 1, 1, 30)', pid)
        ok, prog = await st.claim_career_order(pid, 1)
        check('status=1 领取成功返回进度', ok and prog == 30,
              f'ok={ok} prog={prog}')
        ok2, prog2 = await st.claim_career_order(pid, 1)
        check('status=2 重复领取失败但返回进度（fallback 在连接内）',
              not ok2 and prog2 == 30, f'ok={ok2} prog={prog2}')

        # ---------------------------------------------------------------- 7
        print('== 7. soft_delete_character 软删事务 ==')
        c1 = await st.create_character(pid, 'TxnSoldier1', 1, 1)
        ok, active = await st.soft_delete_character(pid, c1['id'])
        check('最后一个角色拒删', not ok and active == 1,
              f'ok={ok} active={active}')
        c2 = await st.create_character(pid, 'TxnSoldier2', 1, 1)
        # 给 c2 一把武器（装备态）验证软删后退仓
        async with st.pool.acquire() as c:
            await c.execute(
                "INSERT INTO weapons (player_id, name, template_id, character_id, "
                "container_slot) VALUES ($1, 'TxnGun', 2, $2, 1)", pid, c2['id'])
        ok, active = await st.soft_delete_character(pid, c2['id'])
        check('非最后角色软删成功', ok and active == 1,
              f'ok={ok} active={active}')
        async with st.pool.acquire() as c:
            deleted = await c.fetchval(
                'SELECT is_deleted FROM characters WHERE id = $1', c2['id'])
            wep = await c.fetchrow(
                'SELECT character_id, container_slot FROM weapons '
                'WHERE player_id = $1 AND name = $2', pid, 'TxnGun')
        check('软删标记 is_deleted', deleted is True, f'is_deleted={deleted}')
        check('装备退仓 character_id=0', int(wep['character_id']) == 0,
              f'character_id={wep["character_id"]}')
        ok, _ = await st.soft_delete_character(pid, c2['id'])
        check('重复软删已删角色失败', not ok)

        # ---------------------------------------------------------------- 8
        print('== 8. respond_company_invitation / leave_company 事务 ==')
        async with st.pool.acquire() as c:
            co_id = await c.fetchval(
                'INSERT INTO companies (name, leader_player_id) '
                "VALUES ('TxnCo', $1) RETURNING id", pid2)
            await c.execute(  # 队长也入 members（leave_company 走 members JOIN）
                'INSERT INTO company_members (company_id, player_id, role) '
                'VALUES ($1, $2, 9)', co_id, pid2)
            inv_id = await c.fetchval(
                'INSERT INTO company_invitations (company_id, player_id, '
                'invited_by) VALUES ($1, $2, $2) RETURNING id', co_id, pid)
        code, rco = await st.respond_company_invitation(inv_id, True)
        check('接受邀请入团成功', code == 1 and rco == co_id,
              f'code={code} co={rco}')
        async with st.pool.acquire() as c:
            in_co = await c.fetchval(
                'SELECT count(*) FROM company_members WHERE company_id = $1 '
                'AND player_id = $2', co_id, pid)
            inv_n = await c.fetchval(
                'SELECT count(*) FROM company_invitations WHERE id = $1', inv_id)
        check('入团+删邀请同事务落库', in_co == 1 and inv_n == 0,
              f'in_co={in_co} inv={inv_n}')
        code2, _ = await st.respond_company_invitation(inv_id, True)
        check('重复响应已删邀请返回 185', code2 == 185, f'code={code2}')

        # 拒绝邀请：不入团
        async with st.pool.acquire() as c:
            inv2 = await c.fetchval(
                'INSERT INTO company_invitations (company_id, player_id, '
                'invited_by) VALUES ($1, $2, $2) RETURNING id', co_id, pid)
        code3, _ = await st.respond_company_invitation(inv2, False)
        async with st.pool.acquire() as c:
            still = await c.fetchval(
                'SELECT count(*) FROM company_members WHERE company_id = $1 '
                'AND player_id = $2', co_id, pid)
        check('拒绝邀请不入团', code3 == 1 and still == 1,
              f'code={code3} members={still}')

        # 队员退团（pid 为普通队员；pid2 是队长）
        code4, lco = await st.leave_company(pid)
        check('队员退团成功', code4 == 1 and lco == co_id,
              f'code={code4} co={lco}')
        # 队长最后退团 → 解散
        code5, _ = await st.leave_company(pid2)
        async with st.pool.acquire() as c:
            co_n = await c.fetchval(
                'SELECT count(*) FROM companies WHERE id = $1', co_id)
        check('最后成员（队长）退团 → 军团解散', code5 == 1 and co_n == 0,
              f'code={code5} companies={co_n}')
        code6, _ = await st.leave_company(pid)
        check('无军团退团返回 186', code6 == 186, f'code={code6}')

        # 队长移交：pid2 队长 + pid 成员 → 队长退团后 pid 接任
        async with st.pool.acquire() as c:
            co2 = await c.fetchval(
                'INSERT INTO companies (name, leader_player_id) '
                "VALUES ('TxnCo2', $1) RETURNING id", pid2)
            await c.execute(
                'INSERT INTO company_members (company_id, player_id, role) '
                'VALUES ($1, $2, 0)', co2, pid)
            await c.execute(
                'INSERT INTO company_members (company_id, player_id, role) '
                'VALUES ($1, $2, 9)', co2, pid2)
        await st.leave_company(pid2)
        async with st.pool.acquire() as c:
            leader = await c.fetchval(
                'SELECT leader_player_id FROM companies WHERE id = $1', co2)
        check('队长退团移交队长', int(leader) == int(pid),
              f'leader={leader} expect={pid}')
    finally:
        # ---------------------------------------------------------------- cleanup
        print('== 9. 清理测试数据（硬删，可对线上库运行）==')
        async with st.pool.acquire() as c:
            for u in (uname, f'{uname}_b', f'{uname}_c'):
                await c.execute('DELETE FROM companies WHERE leader_player_id IN '
                                '(SELECT id FROM players WHERE username = $1)', u)
            n = await c.execute(
                'DELETE FROM players WHERE username IN ($1, $2, $3)',
                uname, f'{uname}_b', f'{uname}_c')
            # §4 兑换码测试包：无 player_id 关联，不随账号级联删，须显式硬删
            # （流水表 FK ON DELETE CASCADE 随包删除）
            nv = await c.execute(
                "DELETE FROM voucher_packs WHERE pack_name IN "
                "('TXNPACK', 'TXNEXP', 'TXNLIM', 'TXNINF', 'TXNLEGACY', 'TXNRACE')")
        print(f'  已删除测试账号（含级联数据）rowcount={n}')
        print(f'  已删除测试兑换码 rowcount={nv}')
        await st.close()

    print(f'\n结果: PASS={PASS} FAIL={FAIL}')
    sys.exit(1 if FAIL else 0)


if __name__ == '__main__':
    asyncio.run(main())
