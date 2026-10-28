# -*- coding: utf-8 -*-
"""连接生命周期 / 事务 / 序列重同步 等基础设施（原 PlayerStore 类体）。
拆分自 player_store.py（纯移动重构，方法体逐字保留）。
"""
import os
import re
import subprocess
import sys
import asyncpg
from contextlib import asynccontextmanager
from .schema import _SCHEMA

DEFAULT_DSN = 'postgresql://hng:hng_password@127.0.0.1:5432/hng'

# 每日首战 +50% XP 资格判定（布尔 SQL 表达式，SELECT/WHERE 两用）：
# 资格可用 ⇔ last_daily_battle_at 早于最近一次 05:00 UTC 重置点
# （客户端 DailyBonusModel.utcDailyResetHour=5 同窗口，见 swf 反编译；
# 此前 CURRENT_DATE 按服务器本地午夜，与客户端窗口错位）。
# timezone('utc', ...) 把无时区 timestamp 按 UTC 钉成 timestamptz——
# 直接比较会被会话 TimeZone 重新解释，服务器非 UTC 时区时重置点漂移。
_DAILY_BONUS_AVAILABLE_SQL = (
    "COALESCE(last_daily_battle_at < timezone('utc', "
    "date_trunc('day', now() AT TIME ZONE 'UTC' - interval '5 hours') "
    "+ interval '5 hours'), TRUE)")


def _rowcount(status):
    """asyncpg execute() 状态串（如 'DELETE 1'/'UPDATE 3'）→ 受影响行数。"""
    try:
        return int(str(status).split()[-1])
    except (ValueError, IndexError):
        return 0


def _wsl_docker_ip():
    """Windows + Docker Desktop(WSL2) 下 localhost 转发对长连接不稳定，
    直连 WSL VM IP 更可靠。返回 eth0 IPv4 或 None。"""
    try:
        out = subprocess.run(
            ['wsl', '-d', 'docker-desktop', 'ip', '-4', 'addr', 'show', 'eth0'],
            capture_output=True, text=True, timeout=10,
            encoding='utf-8', errors='replace').stdout
        m = re.search(r'inet (\d+\.\d+\.\d+\.\d+)', out)
        return m.group(1) if m else None
    except Exception:
        return None

class BaseStore:
    def __init__(self, dsn=None):
        # 用户显式指定（参数或 HNG_DATABASE_URL）时不做 WSL 替换，尊重远程配置
        self._explicit = dsn is not None or 'HNG_DATABASE_URL' in os.environ
        self.dsn = dsn or os.environ.get('HNG_DATABASE_URL', DEFAULT_DSN)
        self.pool = None
        self.wsl_ip = None    # 探测到的 WSL VM IP（Windows + Docker Desktop 时）

    async def start(self):
        """创建连接池并自动建表。

        Windows + Docker Desktop：localhost 端口转发对长连接不稳定（vpnkit/hostproxy bug，
        新版 Docker 已换 wslrelay 修复但要求 Win10 19044+），因此默认直连 WSL VM IP。
        用户显式指定 HNG_DATABASE_URL（远程部署）时不替换。
        """
        if sys.platform == 'win32' and not self._explicit:
            ip = _wsl_docker_ip()
            if ip:
                self.dsn = re.sub(r'@(127\.0\.0\.1|localhost):', f'@{ip}:', self.dsn)
                self.wsl_ip = ip
            # 探测不到（无 Docker / 原生 PostgreSQL）则保持 127.0.0.1 原样
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=5)
        async with self.pool.acquire() as conn:
            await conn.execute(_SCHEMA)
            await self._resync_small_sequences(conn)
            await self._normalize_conditions(conn)

    async def _normalize_conditions(self, conn):
        """存量装备 condition 归一化（幂等迁移，修复"刚买就有损耗"的脏数据）。

        历史创建路径从不写 condition，全部吃 DB 列默认值：vehicles 默认 100.0，
        而各模板 maxCondition 为 25~220 点 —— 地面载具（max 25~75）超满
        （维修费算出负数→客户端钳成 1 块钱）、飞机（max 200/220）一买即半损。
        磨损系统上线前的载具从未被扣减过，condition==100.0 的行必为该默认值
        残留，直接重置为模板满耐久；其余表只把超过模板上限的值钳到上限。"""
        import wardb
        fixed = 0
        rows = await conn.fetch(
            'SELECT id, vehicle_variant_template_id, condition FROM vehicles')
        for r in rows:
            full = wardb.vehicle_full_condition(r['vehicle_variant_template_id'])
            cond = float(r['condition'] or 0)
            if (cond == 100.0 and full != 100.0) or cond > full:
                await conn.execute(
                    'UPDATE vehicles SET condition = $1 WHERE id = $2',
                    full, r['id'])
                fixed += 1
        # weapons/weapon_modifiers 上限 1.0（默认值即满），弹药/载具升级
        # 创建路径一直显式写满耐久，这里只兜底钳制异常超上限的行
        for tbl, tcol, params_fn in (
            ('weapons', 'template_id', wardb.weapon_wear_params),
            ('weapon_modifiers', 'template_id', wardb.weapon_modifier_wear_params),
            ('weapon_ammo', 'ammo_template_id', wardb.ammo_wear_params),
            ('vehicle_upgrades', 'upgrade_template_id',
             wardb.vehicle_upgrade_wear_params),
            ('vehicle_ammo', 'ammo_template_id', wardb.ammo_wear_params),
        ):
            rows = await conn.fetch(
                f'SELECT id, {tcol} AS tid, condition FROM {tbl}')
            for r in rows:
                params = params_fn(r['tid'])
                if not params or params[0] <= 0:
                    continue
                if float(r['condition'] or 0) > params[0]:
                    await conn.execute(
                        f'UPDATE {tbl} SET condition = $1 WHERE id = $2',
                        params[0], r['id'])
                    fixed += 1
        if fixed:
            from state import log
            log(f'[store] normalized out-of-range condition on {fixed} item row(s)')

    # (表名, 序列名, 玩家 ID 过滤列 -- 只对大 pid 行保留; None=全表小 id)
    _SMALL_ID_SEQ_PAIRS = [
        ('characters',              'character_id_seq',              None),
        ('weapons',                 'weapon_id_seq',                 None),
        ('vehicles',                'vehicle_id_seq',                None),
        ('weapon_ammo',             'ammo_id_seq',                   None),
        ('vehicle_ammo',            'vehicle_ammo_id_seq',           None),
        ('weapon_modifiers',        'modifier_id_seq',               None),
        ('uniform_variants',        'uniform_variant_id_seq',        None),
        ('uniform_parts',           'uniform_part_id_seq',           None),
        ('ribbons',                 'ribbon_id_seq',                 'character_id'),
        ('battle_reports',          'battle_reports_id_seq',        None),
        ('battle_report_players',   'battle_report_players_id_seq', None),
        ('career_orders',           'career_order_id_seq',          None),
        ('mission_records',         'mission_records_id_seq',       None),
        ('squads',                  'squads_id_seq',                None),
        ('squad_members',           'squad_members_id_seq',        None),
        ('companies',               'companies_id_seq',             None),
        ('company_invitations',     'company_invitations_id_seq',   None),
        ('command_nodes',           'command_nodes_id_seq',         None),
        ('chat_channels',           'chat_channels_id_seq',         None),
        ('depot_items',             'depot_items_id_seq',           None),
        ('mail_messages',           'mail_messages_id_seq',         None),
        ('player_boosters',         'player_boosters_id_seq',      None),
        ('player_insignias',        'player_insignias_id_seq',     None),
        ('player_memberships',      'player_memberships_id_seq',   None),
        ('player_reports',          'player_reports_id_seq',       None),
        ('player_tickets',          'player_tickets_id_seq',       None),
        ('vehicle_upgrades',        'vehicle_upgrade_id_seq',      None),
        ('weapon_variants',         'weapon_variant_id_seq',       None),
        ('vehicle_variants',        'vehicle_variant_id_seq',      None),
        ('voucher_packs',           'voucher_packs_id_seq',        None),
    ]
    _SMALL_ID_THRESHOLD = 1_000_000  # player_id 等大 id 超过此值，不受序列约束

    async def _resync_small_sequences(self, conn):
        """启动时把所有小 id 序列重置为 MAX(id)+1，规避"直接删库再建号"
        导致 nextval 返回已占用 id → 主键冲突 → create_character 回滚 →
        starting gear 不发放（武器全错 / 军服不推 → SoldierScreen 1009）。

        只处理小 id (<1e6) 的行：players/玩家相关大 pid 列不碰，保留
        create_user 指定大 id 的语义。序列不存在（空库 / 老 schema）
        时静默跳过，sequence nextval 会正确从 1 起步。"""
        for tbl, seq, id_filter_col in self._SMALL_ID_SEQ_PAIRS:
            try:
                where = '' if id_filter_col is None else (
                    f'WHERE {id_filter_col} < {self._SMALL_ID_THRESHOLD}')
                new_val = await conn.fetchval(f'''
                    SELECT GREATEST(
                        COALESCE((SELECT MAX(id) FROM {tbl} {where}), 0) + 1,
                        nextval('{seq}'))
                ''')
            except Exception:
                # 表或序列不存在（schema 老/空库），跳过
                continue
            # nextval 已经推进过了，再 setval 到目标值 (is_called=false)
            try:
                await conn.execute(
                    f"SELECT setval('{seq}', $1, false)", int(new_val))
            except Exception:
                pass

    async def close(self):
        if self.pool:
            await self.pool.close()
            self.pool = None

    @asynccontextmanager
    async def _conn(self, conn=None):
        """复用外部连接（事务内多步操作）或从池内取一条。"""
        if conn is not None:
            yield conn
        else:
            async with self.pool.acquire() as c:
                yield c

    @asynccontextmanager
    async def transaction(self):
        """跨多条 store 操作的事务上下文。

        用法：async with STORE.transaction() as conn:
                  await STORE.xxx(..., conn=conn)   # 支持传 conn 的方法
        块内任一步抛异常则整体回滚；正常退出统一提交。
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                yield conn

