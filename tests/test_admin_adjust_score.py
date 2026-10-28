# -*- coding: utf-8 -*-
"""管理端账号经验调整（web/server players_svc.adjust_score / player_detail）。

直连线上库（SSH 隧道 127.0.0.1:15432，与 test_realdb.py 同款模式），
用临时玩家+角色走全场景后清理：

1. player_detail：totalScore 汇总（PlayerScore 键优先、legacy score 回落不叠加）
2. adjust_score 正数：计入贡献最高角色（沿用其生效键）
3. 超额扣减拒绝（扣减后经验不能为负）
4. 单角色全额扣减：其他角色与 legacy 键不动
5. 跨角色扣减：贡献高者扣到 0 后继续扣次高者
6. 再加回：计入当前贡献最高角色的生效键
7. 无角色玩家 / 不存在玩家 / 0 变动 拒绝
8. wardb_meta.player_level_from_score 阈值边界（与游戏服 wardb 同款）

运行：python tests/test_admin_adjust_score.py（需隧道在线）
"""
import asyncio
import os
import sys

_WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'web')
sys.path.insert(0, _WEB_DIR)

import asyncpg

from server.services import players_svc
from server import wardb_meta

DSN = 'postgresql://hng:hng_password@127.0.0.1:15432/hng'

checks = []


def check(name, cond, extra=''):
    checks.append((name, cond))
    print(('PASS' if cond else 'FAIL'), name, extra)


async def _stat(pool, cid, key):
    async with pool.acquire() as conn:
        return await conn.fetchval(
            'SELECT value FROM character_stats '
            'WHERE character_id = $1 AND stat_key = $2', cid, key)


async def main():
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    async with pool.acquire() as conn:
        pid = await conn.fetchval(
            'INSERT INTO players (username, password) '
            "VALUES ('__score_test__', 'x') RETURNING id")
        ca = await conn.fetchval(
            'INSERT INTO characters (player_id, name, faction, template) '
            "VALUES ($1, 't_a', 1, 1) RETURNING id", pid)
        cb = await conn.fetchval(
            'INSERT INTO characters (player_id, name, faction, template) '
            "VALUES ($1, 't_b', 2, 1) RETURNING id", pid)
        # A：legacy score=1000；B：PlayerScore=500 + score=999（PlayerScore 优先）
        await conn.execute(
            "INSERT INTO character_stats VALUES ($1, 'score', 1000)", ca)
        await conn.execute(
            "INSERT INTO character_stats VALUES ($1, 'score', 999)", cb)
        await conn.execute(
            "INSERT INTO character_stats VALUES ($1, 'PlayerScore', 500)", cb)
    pid2 = None
    try:
        # 1. detail：PlayerScore 优先不叠加
        d = await players_svc.player_detail(pool, pid)
        check('detail total_score=1500', d['player']['total_score'] == 1500,
              f"got {d['player']['total_score']}")
        check('detail level=3', d['player']['level'] == 3,
              f"got {d['player']['level']}")

        # 2. +10000 → 计入贡献最高的 A（沿用其 score 键）
        r = await players_svc.adjust_score(pool, pid, 10000)
        check('add -> 11500', r['total_score'] == 11500,
              f"got {r['total_score']}")
        check('add level=10', r['level'] == 10, f"got {r['level']}")
        check('A.score +10000 -> 11000',
              abs(await _stat(pool, ca, 'score') - 11000) < 1e-6)

        # 3. 超额扣减拒绝
        try:
            await players_svc.adjust_score(pool, pid, -12000)
            check('over-drain rejected', False)
        except ValueError as e:
            check('over-drain rejected', '不能为负' in str(e), str(e))

        # 4. -11000 全部从 A 扣（B 不动，legacy 键不复活）
        r = await players_svc.adjust_score(pool, pid, -11000)
        check('drain -> 500', r['total_score'] == 500,
              f"got {r['total_score']}")
        check('A drained to 0', abs(await _stat(pool, ca, 'score')) < 1e-6)
        check('B.PlayerScore untouched 500',
              abs(await _stat(pool, cb, 'PlayerScore') - 500) < 1e-6)
        check('B legacy score=999（仍被 PlayerScore 覆盖）',
              abs(await _stat(pool, cb, 'score') - 999) < 1e-6)

        # 5. 跨角色扣减：B(500)+加 2200 → 扣 2600 → B=100
        r = await players_svc.adjust_score(pool, pid, 2200)
        r = await players_svc.adjust_score(pool, pid, -2600)
        check('cross drain -> 100', r['total_score'] == 100,
              f"got {r['total_score']}")
        check('B.PlayerScore -> 100',
              abs(await _stat(pool, cb, 'PlayerScore') - 100) < 1e-6)

        # 6. 再加 2000 → 计入当前贡献最高的 B（PlayerScore 键）
        r = await players_svc.adjust_score(pool, pid, 2000)
        check('re-add -> 2100', r['total_score'] == 2100,
              f"got {r['total_score']}")
        check('B.PlayerScore -> 2100',
              abs(await _stat(pool, cb, 'PlayerScore') - 2100) < 1e-6)
        d = await players_svc.player_detail(pool, pid)
        check('detail after ops = 2100', d['player']['total_score'] == 2100,
              f"got {d['player']['total_score']}")
        check('level(2100)=4', d['player']['level'] == 4,
              f"got {d['player']['level']}")

        # 7. 无角色玩家 / 不存在玩家 / 0 变动
        async with pool.acquire() as conn:
            pid2 = await conn.fetchval(
                'INSERT INTO players (username, password) '
                "VALUES ('__score_test2__', 'x') RETURNING id")
        try:
            await players_svc.adjust_score(pool, pid2, 10)
            check('no-char player rejected', False)
        except ValueError as e:
            check('no-char player rejected', '暂无角色' in str(e), str(e))
        try:
            await players_svc.adjust_score(pool, 999999999, 10)
            check('missing player rejected', False)
        except LookupError:
            check('missing player rejected', True)
        try:
            await players_svc.adjust_score(pool, pid, 0)
            check('zero delta rejected', False)
        except ValueError:
            check('zero delta rejected', True)

        # 8. 阈值边界（与游戏服 wardb.player_level_from_score 同款）
        lv = [wardb_meta.player_level_from_score(s)
              for s in (0, 249, 250, 18999, 19000, 37499, 37500, 10 ** 9)]
        check('threshold boundaries', lv == [1, 1, 2, 10, 11, 11, 12, 12], str(lv))
    finally:
        async with pool.acquire() as conn:
            if pid2:
                await conn.execute('DELETE FROM players WHERE id = $1', pid2)
            await conn.execute('DELETE FROM players WHERE id = $1', pid)
        await pool.close()
    fails = [n for n, ok in checks if not ok]
    print(f'\n{len(checks) - len(fails)}/{len(checks)} passed')
    if fails:
        print('FAILED:', fails)
        sys.exit(1)


if __name__ == '__main__':
    asyncio.run(main())
