# -*- coding: utf-8 -*-
"""schema.py 校验：在真实库上 BEGIN; _SCHEMA; ROLLBACK;
DDL 事务可回滚——完整验证每条 SQL 语法/语义，不留任何痕迹。
手动运行：python schema_rollback_check.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from store.schema import _SCHEMA

DSN = 'postgresql://hng:hng_password@172.25.166.165:5432/hng'


async def main():
    import asyncpg
    conn = await asyncpg.connect(DSN)
    # 简单查询协议逐条执行（与 BaseStore.start() conn.execute(_SCHEMA) 同路径）
    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute(_SCHEMA)
        print(f'[1] _SCHEMA 全量执行成功（{len(_SCHEMA)} 字符，含所有 DDL）')
    finally:
        await tx.rollback()
        print('[2] ROLLBACK 完成——未持久化任何变更')
    # 复核：server_config 不应存在（证明回滚干净）
    exists = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_name='server_config')")
    print(f'[3] server_config 表存在性（应 False）: {exists}')
    await conn.close()
    sys.exit(0 if not exists else 1)


if __name__ == '__main__':
    asyncio.run(main())
