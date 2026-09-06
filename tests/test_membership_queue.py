# -*- coding: utf-8 -*-
"""老兵会员（AddMembership）排队激活语义验证（2026-09-01 修复回归）。

官方语义（客户端 PlayerModel/PlayerUtilities 反编译）：
- 同一时刻只有一份会员在跑（PlayerMembershipItem.expireDate 非空），
  其余为排队行（expireDate=""，isCurrentMembership=false）；
- getCurrentMembership 取表中第一行判 isPremium（不校验是否过期）→
  残留过期行会把老兵判定永久卡死，推送前必须清扫；
- 最终到期 = 当前行到期 + Σ排队行 durationDays（模板表）。

服务器侧对应实现（player_store / handlers/economy）：
- add_membership：无进行中行 → 立即起跑；有 → 插排队行 expires_at=NULL
- sweep_memberships：删已过期行；无进行中行时按模板天数激活最老排队行
- list_memberships：进行中行排最前（行序即 KVCS 推送序）

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


# 测试自造模板 id → durationDays 映射（membership_template_id 无外键约束，
# 不依赖 wardb 表内容）
DAYS = {9001: 30, 9002: 90}


async def main():
    import store
    st = store.PlayerStore()
    await st.start()
    print('== 0. 建测试账号 ==')
    uname = f'test_mship_{int(time.time())}'
    pid = await st.create_player(uname, 'mship-test')
    check('create_player 建号', pid is not None)
    if pid is None:
        await st.close()
        sys.exit(1)

    async def expire_all_running():
        """把该玩家所有进行中行（expires_at 非空）置为 1 小时前过期。"""
        async with st.pool.acquire() as c:
            await c.execute(
                "UPDATE player_memberships SET expires_at = now() - interval '1 hour' "
                'WHERE player_id = $1 AND expires_at IS NOT NULL', pid)

    async def count_rows():
        return len(await st.list_memberships(pid))

    try:
        # ------------------------------------------------------------- 1
        print('== 1. 首份购买立即起跑 ==')
        r1 = await st.add_membership(pid, 9001, duration_days=DAYS[9001])
        check('返回 {id, expires_at} 且 expires_at 非空（起跑）',
              r1['expires_at'] is not None, f'r1={r1}')
        rows = await st.list_memberships(pid)
        check('行序：唯一行在最前且非排队',
              len(rows) == 1 and rows[0]['id'] == r1['id']
              and rows[0]['expires_at'] is not None)

        # ------------------------------------------------------------- 2
        print('== 2. 已有进行中会员 → 后续购买排队（expires_at=NULL）==')
        r2 = await st.add_membership(pid, 9002, duration_days=DAYS[9002])
        r3 = await st.add_membership(pid, 9001, duration_days=DAYS[9001])
        check('第二/三份均排队（expires_at=NULL）',
              r2['expires_at'] is None and r3['expires_at'] is None,
              f'r2={r2} r3={r3}')
        rows = await st.list_memberships(pid)
        check('行序：running 在前、排队按购买顺序（r1,r2,r3）',
              [x['id'] for x in rows] == [r1['id'], r2['id'], r3['id']]
              and rows[0]['expires_at'] is not None
              and rows[1]['expires_at'] is None
              and rows[2]['expires_at'] is None,
              f'rows={[(x["id"], x["expires_at"] is not None) for x in rows]}')

        # ------------------------------------------------------------- 3
        print('== 3. sweep：running 存在时不激活排队行 ==')
        d, a = await st.sweep_memberships(pid, DAYS, 30)
        check('无过期可删、无排队被激活', d == 0 and a is None, f'd={d} a={a}')
        check('行数不变（3 行）', await count_rows() == 3)

        # ------------------------------------------------------------- 4
        print('== 4. 到期切换：删过期行 + 按模板天数激活最老排队行 ==')
        await expire_all_running()  # 仅 r1 过期（r2/r3 为 NULL 不受影响）
        d, a = await st.sweep_memberships(pid, DAYS, 30)
        check('删除 1 行过期 + 激活最老排队行 r2（模板 9002 → 90 天）',
              d == 1 and a and a['id'] == r2['id'] and a['days'] == 90,
              f'd={d} a={a}')
        rows = await st.list_memberships(pid)
        check('行序：r2 running 在前、r3 排队在后',
              [x['id'] for x in rows] == [r2['id'], r3['id']]
              and rows[0]['expires_at'] is not None
              and rows[1]['expires_at'] is None,
              f'rows={[(x["id"], x["expires_at"] is not None) for x in rows]}')

        # ------------------------------------------------------------- 5
        print('== 5. 逐份消化至清空；复购立即起跑 ==')
        await expire_all_running()  # r2 过期
        d, a = await st.sweep_memberships(pid, DAYS, 30)
        check('r2 过期 → 激活最后一份 r3（30 天）',
              d == 1 and a and a['id'] == r3['id'] and a['days'] == 30,
              f'd={d} a={a}')
        rows = await st.list_memberships(pid)
        check('仅剩 r3 running', len(rows) == 1 and rows[0]['id'] == r3['id']
              and rows[0]['expires_at'] is not None)

        await expire_all_running()  # r3 过期
        d, a = await st.sweep_memberships(pid, DAYS, 30)
        check('最后一份过期 → 删除且无可激活（表清空，无残留卡死客户端判定）',
              d == 1 and a is None and await count_rows() == 0,
              f'd={d} a={a} rows={await count_rows()}')

        r4 = await st.add_membership(pid, 9001, duration_days=DAYS[9001])
        check('清空后复购立即起跑（跨月复购场景）',
              r4['expires_at'] is not None, f'r4={r4}')

        # ------------------------------------------------------------- 6
        print('== 6. 购买并入外部事务：中途失败整体回滚 ==')
        try:
            async with st.transaction() as tx:
                await st.add_membership(pid, 9002, duration_days=90, conn=tx)
                raise Boom()
        except Boom:
            pass
        check('事务内购买后异常 → 行回滚（与扣款同生共死）',
              await count_rows() == 1, f'rows={await count_rows()}')
    finally:
        # ------------------------------------------------------------- cleanup
        print('== 9. 清理测试数据 ==')
        async with st.pool.acquire() as c:
            n = await c.execute('DELETE FROM players WHERE username = $1', uname)
        print(f'  已删除测试账号（含级联会员行）rowcount={n}')
        await st.close()

    print(f'\n结果: PASS={PASS} FAIL={FAIL}')
    sys.exit(1 if FAIL else 0)


if __name__ == '__main__':
    asyncio.run(main())
