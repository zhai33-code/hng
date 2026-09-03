"""玩家数据持久化：PostgreSQL（asyncpg 连接池）。

表结构（首次启动自动建表）：
  players    账号（username 唯一）
  characters 角色（id 来自 character_id_seq，从 1 递增）
  weapons    武器实例（id 来自 weapon_id_seq，从 100 递增）

连接串从环境变量 HNG_DATABASE_URL 读取，
默认 postgresql://hng:hng_password@localhost:5432/hng
（对应 docker run -e POSTGRES_USER=hng -e POSTGRES_PASSWORD=hng_password ...）
"""
import json
import os
import re
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import datetime

import asyncpg

DEFAULT_DSN = 'postgresql://hng:hng_password@127.0.0.1:5432/hng'


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

_SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS character_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS weapon_id_seq START 100;
CREATE SEQUENCE IF NOT EXISTS modifier_id_seq START 200;
CREATE SEQUENCE IF NOT EXISTS ammo_id_seq START 300;
CREATE SEQUENCE IF NOT EXISTS vehicle_id_seq START 400;
CREATE SEQUENCE IF NOT EXISTS vehicle_upgrade_id_seq START 500;
CREATE SEQUENCE IF NOT EXISTS vehicle_ammo_id_seq START 600;
CREATE SEQUENCE IF NOT EXISTS uniform_variant_id_seq START 700;
CREATE SEQUENCE IF NOT EXISTS weapon_variant_id_seq START 800;
CREATE SEQUENCE IF NOT EXISTS uniform_part_id_seq START 900;
CREATE SEQUENCE IF NOT EXISTS vehicle_variant_id_seq START 1000;
CREATE SEQUENCE IF NOT EXISTS tracer_effect_id_seq START 1100;
-- 玩家 id 从 1e6 起步：避开战斗服 bot 缺省 playerId（= u16 clientId ≤65535）
-- 的命名空间，防止击杀归属/实体顶替撞号（playerid-bot-collision-2026-08-31.md）
CREATE SEQUENCE IF NOT EXISTS players_id_seq START 1000000;

CREATE TABLE IF NOT EXISTS players (
    id         BIGINT PRIMARY KEY DEFAULT nextval('players_id_seq'),
    username   TEXT UNIQUE NOT NULL,
    password   TEXT NOT NULL,
    credits    BIGINT NOT NULL DEFAULT 100000,
    warfunds   BIGINT NOT NULL DEFAULT 100000,
    gold       BIGINT NOT NULL DEFAULT 100000,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 迁移：旧库补充 password / 货币列
ALTER TABLE players ADD COLUMN IF NOT EXISTS password TEXT NOT NULL;
-- 撤销历史版本的固定默认密码（防新建账号自动获得已知密码）
ALTER TABLE players ALTER COLUMN password DROP DEFAULT;
ALTER TABLE players ADD COLUMN IF NOT EXISTS credits  BIGINT NOT NULL DEFAULT 100000;
ALTER TABLE players ADD COLUMN IF NOT EXISTS warfunds BIGINT NOT NULL DEFAULT 100000;
ALTER TABLE players ADD COLUMN IF NOT EXISTS gold     BIGINT NOT NULL DEFAULT 100000;
-- 软删除标记（管理后台写入；登录侧 get_password 排除已删除账号 → 拒登）
ALTER TABLE players ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
-- 玩家等级奖励已发到的等级（进展 tab：PlayerLevelUnlock 每级 credits/gold/
-- warfunds 奖励只发一次；默认 1 = 1 级起步无奖励，存量玩家下次检测时补发）
ALTER TABLE players ADD COLUMN IF NOT EXISTS level_granted INT NOT NULL DEFAULT 1;
-- 存量库兜底：players_id_seq 仍处撞号区（<1e6，如 BIGSERIAL 时代建的库）时
-- 抬到 1e6（不低于已用 max id+1）；已迁移过的库不受影响
DO $$
DECLARE cur bigint;
BEGIN
  IF EXISTS (SELECT 1 FROM pg_sequences
             WHERE schemaname='public' AND sequencename='players_id_seq') THEN
    SELECT COALESCE(last_value, 0) INTO cur FROM players_id_seq;
    IF cur < 1000000 THEN
      PERFORM setval('players_id_seq', GREATEST(
          1000000, (SELECT COALESCE(MAX(id), 0) + 1 FROM players)), false);
    END IF;
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS characters (
    id         BIGINT PRIMARY KEY DEFAULT nextval('character_id_seq'),
    player_id  BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    faction    BIGINT NOT NULL,
    template   BIGINT NOT NULL,
    combat_badge_0 BIGINT NOT NULL DEFAULT 0,
    combat_badge_1 BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 迁移：旧库补充战斗徽章槽位列（set_combatbadge_request 持久化）
ALTER TABLE characters ADD COLUMN IF NOT EXISTS combat_badge_0 BIGINT NOT NULL DEFAULT 0;
ALTER TABLE characters ADD COLUMN IF NOT EXISTS combat_badge_1 BIGINT NOT NULL DEFAULT 0;
-- 角色战斗经验（combat XP，军衔单源：军衔等级 = combat_xp 经 ExperienceLevels 阈值推导）
ALTER TABLE characters ADD COLUMN IF NOT EXISTS combat_xp BIGINT NOT NULL DEFAULT 0;

-- 勋带经验（勋带单源：等级 = total_xp 经 ribbonlevel 阈值推导；战后结算与购买勋带等级都写这里）
CREATE TABLE IF NOT EXISTS character_ribbon_xp (
    character_id       BIGINT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    ribbon_template_id BIGINT NOT NULL,
    total_xp           DOUBLE PRECISION NOT NULL DEFAULT 0,
    PRIMARY KEY (character_id, ribbon_template_id)
);

-- 勋带购买解锁的额外 uid（progressionUnlock.id，AddRibbonLevelRequest 按人物持久化；
-- 取代旧 characters.extra_unlock_uids 逗号串。"extra" = 默认解锁序列之外购买的解锁）
CREATE TABLE IF NOT EXISTS character_extra_unlocks (
    character_id BIGINT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    unlock_uid   BIGINT NOT NULL,
    PRIMARY KEY (character_id, unlock_uid)
);

CREATE TABLE IF NOT EXISTS weapons (
    id                  BIGINT PRIMARY KEY DEFAULT nextval('weapon_id_seq'),
    player_id           BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    name                TEXT NOT NULL,
    template_id         BIGINT NOT NULL,
    character_id        BIGINT NOT NULL DEFAULT 0,
    container_id        BIGINT NOT NULL DEFAULT 0,
    container_slot      INT NOT NULL DEFAULT 0,
    variant_template_id BIGINT NOT NULL DEFAULT 0,
    variant_id          BIGINT NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS weapon_modifiers (
    id           BIGINT PRIMARY KEY DEFAULT nextval('modifier_id_seq'),
    player_id    BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    template_id  BIGINT NOT NULL,
    character_id BIGINT NOT NULL DEFAULT 0,
    weapon_id    BIGINT NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS weapon_ammo (
    id               BIGINT PRIMARY KEY DEFAULT nextval('ammo_id_seq'),
    player_id        BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    ammo_template_id BIGINT NOT NULL,
    character_id     BIGINT NOT NULL DEFAULT 0,
    weapon_id        BIGINT NOT NULL DEFAULT 0,
    slot_index       INT NOT NULL DEFAULT 0,
    condition        DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 迁移：旧库补充 condition 列
ALTER TABLE weapon_ammo ADD COLUMN IF NOT EXISTS condition DOUBLE PRECISION NOT NULL DEFAULT 0;

-- 玩家载具实例（CreateVehicleVariantRequest 购买；equipped_in_slot>0 = 已装备到角色槽位）
CREATE TABLE IF NOT EXISTS vehicles (
    id                          BIGINT PRIMARY KEY DEFAULT nextval('vehicle_id_seq'),
    player_id                   BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    vehicle_variant_template_id BIGINT NOT NULL,
    character_id                BIGINT NOT NULL DEFAULT 0,
    equipped_in_slot            INT NOT NULL DEFAULT 0,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 玩家载具升级/补给箱实例（CreateVehicleUpgradeRequest 购买，如 Ammo/Medic supply crate、
-- 车载机枪等；equipped_slot_id>0 = 已装到载具对应槽位 VehicleUpgradeSlot.id）
CREATE TABLE IF NOT EXISTS vehicle_upgrades (
    id                  BIGINT PRIMARY KEY DEFAULT nextval('vehicle_upgrade_id_seq'),
    player_id           BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    upgrade_template_id BIGINT NOT NULL,
    vehicle_id          BIGINT NOT NULL DEFAULT 0,
    character_id        BIGINT NOT NULL DEFAULT 0,
    equipped_slot_id    BIGINT NOT NULL DEFAULT 0,
    condition           DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 玩家载具弹药实例（CreateVehicleAmmoRequest 购买；equipped_storage_id>0 且 slot_index>0 =
-- 已装到载具对应弹药仓 VehicleAmmoStorage 槽位，同仓同槽互斥）
CREATE TABLE IF NOT EXISTS vehicle_ammo (
    id                  BIGINT PRIMARY KEY DEFAULT nextval('vehicle_ammo_id_seq'),
    player_id           BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    ammo_template_id    BIGINT NOT NULL,
    vehicle_id          BIGINT NOT NULL DEFAULT 0,
    character_id        BIGINT NOT NULL DEFAULT 0,
    equipped_storage_id BIGINT NOT NULL DEFAULT 0,
    slot_index          INT NOT NULL DEFAULT 0,
    condition           DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 玩家军服迷彩变体实例（CreateUniformVariantRequest 购买；角色 proto 字段 17 uniformVariantId
-- 指向当前装备的变体实例 id）
CREATE TABLE IF NOT EXISTS uniform_variants (
    id                          BIGINT PRIMARY KEY DEFAULT nextval('uniform_variant_id_seq'),
    player_id                   BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    uniform_variant_template_id BIGINT NOT NULL,
    character_id                BIGINT NOT NULL DEFAULT 0,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 玩家武器皮肤变体实例（CreateWeaponVariantRequest 购买；weapon_id 指向已有武器实例）
CREATE TABLE IF NOT EXISTS weapon_variants (
    id                         BIGINT PRIMARY KEY DEFAULT nextval('weapon_variant_id_seq'),
    player_id                  BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    weapon_id                  BIGINT NOT NULL DEFAULT 0,
    character_id               BIGINT NOT NULL DEFAULT 0,
    weapon_variant_template_id BIGINT NOT NULL,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 玩家载具皮肤变体实例（CreateVehicleVariantRequest 购买，vehicleId 指向已有载具实例；
-- vehicle.equippedVehicleVariantId 字段9 指向当前装备的皮肤实例 id）
CREATE TABLE IF NOT EXISTS vehicle_variants (
    id                         BIGINT PRIMARY KEY DEFAULT nextval('vehicle_variant_id_seq'),
    player_id                  BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    vehicle_id                 BIGINT NOT NULL DEFAULT 0,
    character_id               BIGINT NOT NULL DEFAULT 0,
    vehicle_variant_template_id BIGINT NOT NULL,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 迁移：旧库补充载具装备的皮肤变体 id 列（SetVehicleVariantRequest 装备后持久化）
ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS variant_id BIGINT NOT NULL DEFAULT 0;

-- 迁移：旧库补充角色装备的迷彩变体 id 列（SetUniformVariantRequest 装备后持久化）
ALTER TABLE characters ADD COLUMN IF NOT EXISTS uniform_variant_id BIGINT NOT NULL DEFAULT 0;
ALTER TABLE weapons ADD COLUMN IF NOT EXISTS variant_id BIGINT NOT NULL DEFAULT 0;

-- ===== warserver PlayerSettings f12 装备数值列（2026-08-20 回放动态化 → DB 化）=====
-- 对应 player.dll charactersetting 子消息（warserver-battlefield-settings.md §7.2）：
--   WeaponSetting      f7 spawnClips / f9 condition(fixed32 0-1) / f11 name
--   VehicleSetting     f3 condition(fixed32 点数)
--   WeaponModifierSetting f3 condition / VehicleAmmoSetting f6 spawnClipCount(fixed32)
-- 出生弹夹数不再单独存列（旧 weapons.spawn_clips 已废弃）：
--   weapons.clip_count 持久化客户端三态选择（weapon KVCS f14：
--   负=最少 / 0=默认 / 正=最大），spawnClips 由
--   wardb.spawn_clips_for(clip_count, template_id) 推导
--   （T = WarDatabase weapontemplate f55 clipCount 单源）
ALTER TABLE weapons ADD COLUMN IF NOT EXISTS condition DOUBLE PRECISION NOT NULL DEFAULT 1.0;
ALTER TABLE weapons ADD COLUMN IF NOT EXISTS clip_count INT NOT NULL DEFAULT 0;
ALTER TABLE weapon_modifiers ADD COLUMN IF NOT EXISTS condition DOUBLE PRECISION NOT NULL DEFAULT 1.0;
ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS condition DOUBLE PRECISION NOT NULL DEFAULT 100.0;
-- 载具弹药出生弹夹数：官方恒等于 WarDatabase VehicleAmmoStorage.f2 clipCount
-- （回放锚点 T-38=21 / Yak-9D=1 / R-Z=1），下发时由
-- wardb.vehicle_ammo_spawn_clips(equipped_storage_id) 推导，本列仅存档不参与下发
ALTER TABLE vehicle_ammo ADD COLUMN IF NOT EXISTS spawn_clip_count INT NOT NULL DEFAULT 1;
ALTER TABLE characters ADD COLUMN IF NOT EXISTS squad_points INT NOT NULL DEFAULT 0;
ALTER TABLE characters ADD COLUMN IF NOT EXISTS receive_daily_bonus BOOLEAN NOT NULL DEFAULT TRUE;
-- 每日首战 +50% XP：真实资格源（结算 consume_daily_bonus 消费时写入）。
-- receive_daily_bonus 列保留兼容旧库，SELECT 侧改由本列惰性推导
-- （COALESCE(last_daily_battle_at::date <> CURRENT_DATE, TRUE)，跨天自动回 TRUE）
ALTER TABLE characters ADD COLUMN IF NOT EXISTS last_daily_battle_at TIMESTAMPTZ;

-- 玩家头盔/军装部件实例（PurchaseUniformPartRequest 购买；uniform_part_type 用 EUniformPartType
-- 枚举：eHead=0, eHelmet=1, eJacket=2...；character.headgear_id 指向当前装备的部件实例 id）
CREATE TABLE IF NOT EXISTS uniform_parts (
    id                       BIGINT PRIMARY KEY DEFAULT nextval('uniform_part_id_seq'),
    player_id                BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    character_id             BIGINT NOT NULL DEFAULT 0,
    uniform_part_template_id BIGINT NOT NULL,
    uniform_part_type        INT NOT NULL DEFAULT 0,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 迁移：旧库补充角色装备的头盔实例 id 列（SetUniformPartRequest 装备后持久化）
ALTER TABLE characters ADD COLUMN IF NOT EXISTS headgear_id BIGINT NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_characters_player ON characters(player_id);
CREATE INDEX IF NOT EXISTS idx_weapons_player ON weapons(player_id);
CREATE INDEX IF NOT EXISTS idx_modifiers_player ON weapon_modifiers(player_id);
CREATE INDEX IF NOT EXISTS idx_ammo_player ON weapon_ammo(player_id);
CREATE INDEX IF NOT EXISTS idx_vehicles_player ON vehicles(player_id);
CREATE INDEX IF NOT EXISTS idx_vehicle_upgrades_player ON vehicle_upgrades(player_id);
CREATE INDEX IF NOT EXISTS idx_vehicle_ammo_player ON vehicle_ammo(player_id);
CREATE INDEX IF NOT EXISTS idx_uniform_variants_player ON uniform_variants(player_id);
CREATE INDEX IF NOT EXISTS idx_weapon_variants_player ON weapon_variants(player_id);
CREATE INDEX IF NOT EXISTS idx_uniform_parts_player ON uniform_parts(player_id);
CREATE INDEX IF NOT EXISTS idx_vehicle_variants_player ON vehicle_variants(player_id);

-- 玩家曳光弹效果实例（通行证奖励 target='depot' 发放；SetTracerEffectRequest 装备）。
-- 官方模型 = KVCS 'TracerEffect' 行 {tracerEffectTemplateId, playerId, characterId,
-- weaponId, isEquipped}：characterId=0 即仓库在库（客户端 TracerEffectUtilities
-- .getTracerEffectsInDepot），装备=行绑定 weaponId + isEquipped 置位。
CREATE TABLE IF NOT EXISTS tracer_effects (
    id                 BIGINT PRIMARY KEY DEFAULT nextval('tracer_effect_id_seq'),
    player_id          BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    tracer_template_id BIGINT NOT NULL,
    character_id       BIGINT NOT NULL DEFAULT 0,
    weapon_id          BIGINT NOT NULL DEFAULT 0,
    is_equipped        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tracer_effects_player ON tracer_effects(player_id);

-- ===== SWF 前端未响应请求全面补全（2026-08-21 计划 U1）=====
-- 全部 CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS，启动自动迁移，不动旧数据

-- 战报（AnnounceBattleFieldEarningsRequest 写入，GetBattleReportRequest 读取）
CREATE TABLE IF NOT EXISTS battle_reports (
    id                  SERIAL PRIMARY KEY,
    mission_session_id  BIGINT UNIQUE NOT NULL,
    mission_id          BIGINT NOT NULL,
    mission_preset_id  BIGINT NOT NULL,
    winner_faction_id   BIGINT NOT NULL,
    defender_faction_id BIGINT NOT NULL,
    mission_time_seconds INT NOT NULL DEFAULT 0,
    battle_start_time   INT NOT NULL DEFAULT 0,
    capturepoints_json  JSONB NOT NULL DEFAULT '[]',
    factions_json       JSONB NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE battle_reports ADD COLUMN IF NOT EXISTS
    capturepoints_json JSONB NOT NULL DEFAULT '[]';
ALTER TABLE battle_reports ADD COLUMN IF NOT EXISTS
    factions_json JSONB NOT NULL DEFAULT '{}';
CREATE TABLE IF NOT EXISTS battle_report_players (
    id          SERIAL PRIMARY KEY,
    report_id   INT NOT NULL REFERENCES battle_reports(id) ON DELETE CASCADE,
    player_id   BIGINT NOT NULL,
    gamertag    TEXT NOT NULL DEFAULT '',
    squad_name  TEXT NOT NULL DEFAULT '',
    score INT NOT NULL DEFAULT 0, captures INT NOT NULL DEFAULT 0,
    kills  INT NOT NULL DEFAULT 0, deaths  INT NOT NULL DEFAULT 0,
    is_bot      BOOLEAN NOT NULL DEFAULT FALSE,
    characters_json JSONB NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_breport_players_pid ON battle_report_players(player_id);

-- 生涯军令（CareerOrderRequest / CareerOrderActionGameRequest / Claim）
CREATE TABLE IF NOT EXISTS career_orders (
    player_id        BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    career_order_id  BIGINT NOT NULL,
    status           INT NOT NULL DEFAULT 0,
    progress         INT NOT NULL DEFAULT 0,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (player_id, career_order_id)
);

-- 战斗通行证
CREATE TABLE IF NOT EXISTS battle_pass_progress (
    player_id  BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    season_id  BIGINT NOT NULL,
    xp         INT NOT NULL DEFAULT 0,
    purchased  BOOLEAN NOT NULL DEFAULT FALSE,
    claimed_tiers JSONB NOT NULL DEFAULT '[]',
    PRIMARY KEY (player_id, season_id)
);

-- 好友 / 屏蔽
CREATE TABLE IF NOT EXISTS friends (
    player_id BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    friend_id BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    status    INT NOT NULL DEFAULT 0,
    PRIMARY KEY (player_id, friend_id)
);
CREATE TABLE IF NOT EXISTS ignored_players (
    player_id BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    ignored_player_id BIGINT NOT NULL,
    PRIMARY KEY (player_id, ignored_player_id)
);

-- 邮件
CREATE TABLE IF NOT EXISTS mail_messages (
    id          SERIAL PRIMARY KEY,
    sender_id   BIGINT NOT NULL DEFAULT 0,
    recipient_id BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    subject     TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL DEFAULT '',
    is_read     BOOLEAN NOT NULL DEFAULT FALSE,
    deleted     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_mail_recipient ON mail_messages(recipient_id);

-- 聊天频道
CREATE TABLE IF NOT EXISTS chat_channels (
    id       SERIAL PRIMARY KEY,
    name     TEXT UNIQUE NOT NULL,
    owner_id BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS chat_channel_members (
    channel_id INT NOT NULL REFERENCES chat_channels(id) ON DELETE CASCADE,
    player_id  BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    PRIMARY KEY (channel_id, player_id)
);

-- 经济道具：加成 / 门票 / 战争债券 / 兑换券包
CREATE TABLE IF NOT EXISTS player_boosters (
    id SERIAL PRIMARY KEY, player_id BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    booster_template_id BIGINT NOT NULL,
    activated_at TIMESTAMPTZ, expires_at TIMESTAMPTZ,
    is_ribbon BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE TABLE IF NOT EXISTS player_tickets (
    id SERIAL PRIMARY KEY, player_id BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    ticket_template_id BIGINT NOT NULL, expires_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS player_warbonds (
    player_id BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    warbond_item_id BIGINT NOT NULL, count INT NOT NULL DEFAULT 0,
    PRIMARY KEY (player_id, warbond_item_id)
);
CREATE TABLE IF NOT EXISTS voucher_packs (
    id SERIAL PRIMARY KEY, player_id BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    pack_name TEXT NOT NULL, items_json JSONB NOT NULL DEFAULT '[]',
    redeemed BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- 兑换记录（谁在何时兑换；管理端 db.py 迁移同步追加，两侧幂等）
ALTER TABLE voucher_packs ADD COLUMN IF NOT EXISTS redeemed_by BIGINT NOT NULL DEFAULT 0;
ALTER TABLE voucher_packs ADD COLUMN IF NOT EXISTS redeemed_at TIMESTAMPTZ;
ALTER TABLE voucher_packs ADD COLUMN IF NOT EXISTS note TEXT NOT NULL DEFAULT '';
-- 兑换码体系 v2（docs/voucher-redemption-design.md）：时效 + 限量/无限次 + 道具
-- player_id 是购买流程遗留，管理端生成不传 → 改可空
ALTER TABLE voucher_packs ALTER COLUMN player_id DROP NOT NULL;
ALTER TABLE voucher_packs ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
-- 1=一码一用（存量语义）；N=限量 N 次；0=无限次（每人仍限一次，靠流水 UNIQUE）
ALTER TABLE voucher_packs ADD COLUMN IF NOT EXISTS max_redemptions INT NOT NULL DEFAULT 1;
-- 兑换流水：UNIQUE(码, 玩家) = 每人限一次；status 1=pending(待选士兵) 2=granted 3=耗尽跳过
CREATE TABLE IF NOT EXISTS voucher_redemptions (
    id SERIAL PRIMARY KEY,
    voucher_pack_id INT NOT NULL REFERENCES voucher_packs(id) ON DELETE CASCADE,
    player_id BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    character_id BIGINT NOT NULL DEFAULT 0,
    status SMALLINT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    granted_at TIMESTAMPTZ,
    UNIQUE (voucher_pack_id, player_id)
);

-- 角色统计（AddGeneralStats/AddCharacterStatisticsRequest 写，RequestXPs/Sum/Session 读）
CREATE TABLE IF NOT EXISTS character_stats (
    character_id BIGINT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    stat_key     TEXT NOT NULL,
    value        DOUBLE PRECISION NOT NULL DEFAULT 0,
    PRIMARY KEY (character_id, stat_key)
);

-- 迁移：wire stat 59 补映射（statistics_server.WIRE_STAT_NAMES 原缺 59，存量
-- 以 x59/<ctx1>/<ctx2> 死键落库、回读端永远跳过）。聚并可能的多 ctx 变体后
-- 改写为标准键 'Unknown59'（幂等：迁移后无 x59 行，INSERT..SELECT 空集 no-op）。
INSERT INTO character_stats (character_id, stat_key, value)
    SELECT character_id, 'Unknown59', SUM(value) FROM character_stats
    WHERE stat_key LIKE 'x59/%' GROUP BY character_id
ON CONFLICT (character_id, stat_key)
    DO UPDATE SET value = character_stats.value + EXCLUDED.value;
DELETE FROM character_stats WHERE stat_key LIKE 'x59/%';

-- 教程触发（MarkTutorialTriggeredRequest / SetTutorialCompletedRequest 持久化）
CREATE TABLE IF NOT EXISTS character_tutorials (
    character_id BIGINT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    tutorial_id  BIGINT NOT NULL,
    triggered    BOOLEAN NOT NULL DEFAULT TRUE,
    completed    BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (character_id, tutorial_id)
);

-- 仓库（SendItemToDepot / SendUpgradesToDepot / SetDepotItemViewed）
CREATE TABLE IF NOT EXISTS depot_items (
    id SERIAL PRIMARY KEY, player_id BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    item_type TEXT NOT NULL,
    item_id   BIGINT NOT NULL,
    viewed    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 迁移：depot_items 去重 + 唯一约束（历史无约束导致 SetDepotItemViewed 每次
-- INSERT 一行、ON CONFLICT DO NOTHING 永不触发 → 表无限膨胀且 UPDATE 跨 tab 误标）
DELETE FROM depot_items a USING depot_items b
WHERE a.player_id = b.player_id AND a.item_type = b.item_type
  AND a.item_id = b.item_id AND a.id > b.id;
CREATE UNIQUE INDEX IF NOT EXISTS uq_depot_items
    ON depot_items(player_id, item_type, item_id);

-- 小队 / 军团
CREATE TABLE IF NOT EXISTS squads (
    id SERIAL PRIMARY KEY, name TEXT NOT NULL, faction BIGINT NOT NULL DEFAULT 0,
    leader_character_id BIGINT NOT NULL DEFAULT 0,
    matchmaking_flag BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE TABLE IF NOT EXISTS squad_members (
    squad_id INT NOT NULL REFERENCES squads(id) ON DELETE CASCADE,
    character_id BIGINT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    status INT NOT NULL DEFAULT 0,
    PRIMARY KEY (squad_id, character_id)
);
CREATE TABLE IF NOT EXISTS player_selected_char (
    player_id BIGINT PRIMARY KEY REFERENCES players(id) ON DELETE CASCADE,
    character_id BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS companies (
    id SERIAL PRIMARY KEY, name TEXT NOT NULL, leader_player_id BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS company_members (
    company_id INT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    player_id  BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    role INT NOT NULL DEFAULT 0,
    PRIMARY KEY (company_id, player_id)
);

-- 战争地图（指挥节点/补给队列）
CREATE TABLE IF NOT EXISTS command_nodes (
    id SERIAL PRIMARY KEY, war_id BIGINT NOT NULL DEFAULT 1,
    template_id BIGINT NOT NULL, owner_player_id BIGINT NOT NULL DEFAULT 0,
    faction BIGINT NOT NULL DEFAULT 0, name TEXT NOT NULL DEFAULT '',
    equipment_json JSONB NOT NULL DEFAULT '{}', upgrade_level INT NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS supply_queue (
    player_id BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    position  INT NOT NULL,
    entry_json JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (player_id, position)
);

-- 任务记录（CreateMission/EndMission/AbandonMission 状态）
CREATE TABLE IF NOT EXISTS mission_records (
    id SERIAL PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
    preset BIGINT NOT NULL DEFAULT 0, state INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ===== 字段调整（迁移）=====
-- players：语言/Discord/Steam 绑定、每日奖励时间戳、新手首金标记
ALTER TABLE players ADD COLUMN IF NOT EXISTS language TEXT NOT NULL DEFAULT 'en';
ALTER TABLE players ADD COLUMN IF NOT EXISTS discord_id TEXT NOT NULL DEFAULT '';
ALTER TABLE players ADD COLUMN IF NOT EXISTS steam_id   TEXT NOT NULL DEFAULT '';
ALTER TABLE players ADD COLUMN IF NOT EXISTS last_daily_reward_at TIMESTAMPTZ;
ALTER TABLE players ADD COLUMN IF NOT EXISTS first_gold_offer_shown BOOLEAN NOT NULL DEFAULT FALSE;
-- 每日签到（PlayerOfferMetrics KVCS 行源）：已领次数 / 连续登录天数 /
-- 最近登录日期（连续性判定）/ 首局日期（player f19 firstActionGameDate）
ALTER TABLE players ADD COLUMN IF NOT EXISTS daily_reward_redeemed_count INT NOT NULL DEFAULT 0;
ALTER TABLE players ADD COLUMN IF NOT EXISTS consecutive_day_count INT NOT NULL DEFAULT 0;
ALTER TABLE players ADD COLUMN IF NOT EXISTS last_login_date DATE;
ALTER TABLE players ADD COLUMN IF NOT EXISTS first_action_game_date DATE;

-- characters：改名/换模板/激活状态/tier/软删
ALTER TABLE characters ADD COLUMN IF NOT EXISTS active_status INT NOT NULL DEFAULT 1;
ALTER TABLE characters ADD COLUMN IF NOT EXISTS tier INT NOT NULL DEFAULT 0;
ALTER TABLE characters ADD COLUMN IF NOT EXISTS deleted BOOLEAN NOT NULL DEFAULT FALSE;

-- 装备自动修理开关（三态：0=关 1=金币 2=金条；与客户端 autoRepair int 一致）
ALTER TABLE weapons          ADD COLUMN IF NOT EXISTS auto_repair SMALLINT NOT NULL DEFAULT 0;
ALTER TABLE weapon_modifiers ADD COLUMN IF NOT EXISTS auto_repair SMALLINT NOT NULL DEFAULT 0;
ALTER TABLE weapon_ammo      ADD COLUMN IF NOT EXISTS auto_repair SMALLINT NOT NULL DEFAULT 0;
ALTER TABLE vehicles         ADD COLUMN IF NOT EXISTS auto_repair SMALLINT NOT NULL DEFAULT 0;
ALTER TABLE vehicle_upgrades ADD COLUMN IF NOT EXISTS auto_repair SMALLINT NOT NULL DEFAULT 0;
ALTER TABLE vehicle_ammo     ADD COLUMN IF NOT EXISTS auto_repair SMALLINT NOT NULL DEFAULT 0;
-- 旧库 BOOLEAN 列迁移为 SMALLINT（True→1 False→0；先 DROP DEFAULT 否则类型转换报错）
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['weapons','weapon_modifiers','weapon_ammo',
                             'vehicles','vehicle_upgrades','vehicle_ammo'] LOOP
        IF EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = t AND column_name = 'auto_repair'
                     AND data_type = 'boolean') THEN
            EXECUTE format('ALTER TABLE %I ALTER COLUMN auto_repair DROP DEFAULT', t);
            EXECUTE format('ALTER TABLE %I ALTER COLUMN auto_repair TYPE SMALLINT '
                           'USING CASE WHEN auto_repair THEN 1 ELSE 0 END', t);
            EXECUTE format('ALTER TABLE %I ALTER COLUMN auto_repair SET DEFAULT 0', t);
        END IF;
    END LOOP;
END $$;

-- battle_pass_progress：激活态（购买后需手动激活才计 isActive）
ALTER TABLE battle_pass_progress ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT FALSE;

-- battle_pass_progress：解锁时间（Zulu 'YYYYMMDDHHMMSSZ'）。客户端 canBeActivated
-- 要求 isRunning（activeTo 未过期）或 unlockDateTime 非空——已结束赛季购买后
-- 只有该字段非空才会显示"激活"按钮；回填历史已购行
ALTER TABLE battle_pass_progress ADD COLUMN IF NOT EXISTS unlock_at TEXT NOT NULL DEFAULT '';
UPDATE battle_pass_progress SET unlock_at = '20990101000000Z'
WHERE purchased AND unlock_at = '';

-- ===== U4 迁移（任务/社交域）=====
-- 举报（AddPlayerReportRequest 持久化）
CREATE TABLE IF NOT EXISTS player_reports (
    id SERIAL PRIMARY KEY,
    reporter_id BIGINT NOT NULL,
    reported_player_id BIGINT NOT NULL,
    category INT NOT NULL DEFAULT 0,
    action_game_session BIGINT NOT NULL DEFAULT 0,
    chatlogs TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 邮件线程化（GetMailMessagesResponse 的 MessageThread 分组）
ALTER TABLE mail_messages ADD COLUMN IF NOT EXISTS thread_id BIGINT NOT NULL DEFAULT 0;
ALTER TABLE mail_messages ADD COLUMN IF NOT EXISTS reply_to BIGINT NOT NULL DEFAULT 0;

-- 聊天频道属性（create/edit_chatchannel_request）
ALTER TABLE chat_channels ADD COLUMN IF NOT EXISTS display_name TEXT NOT NULL DEFAULT '';
ALTER TABLE chat_channels ADD COLUMN IF NOT EXISTS channel_tab TEXT NOT NULL DEFAULT 'general';
ALTER TABLE chat_channels ADD COLUMN IF NOT EXISTS password TEXT NOT NULL DEFAULT '';
ALTER TABLE chat_channels ADD COLUMN IF NOT EXISTS hidden BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE chat_channels ADD COLUMN IF NOT EXISTS invitation_only BOOLEAN NOT NULL DEFAULT FALSE;

-- 任务记录扩展（CreateMission/EndMission/AbandonMission 状态机）
ALTER TABLE mission_records ADD COLUMN IF NOT EXISTS attacker_faction BIGINT NOT NULL DEFAULT 0;
ALTER TABLE mission_records ADD COLUMN IF NOT EXISTS defender_faction BIGINT NOT NULL DEFAULT 0;
ALTER TABLE mission_records ADD COLUMN IF NOT EXISTS owner_player_id BIGINT NOT NULL DEFAULT 0;
ALTER TABLE mission_records ADD COLUMN IF NOT EXISTS map_entity_id BIGINT NOT NULL DEFAULT 0;
ALTER TABLE mission_records ADD COLUMN IF NOT EXISTS battle_id BIGINT NOT NULL DEFAULT 0;

-- ===== U5 迁移（经济/小队/军团域）=====
-- 会员（AddMembershipRequest 购买，PlayerMembershipItem 源）
CREATE TABLE IF NOT EXISTS player_memberships (
    id SERIAL PRIMARY KEY,
    player_id BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    membership_template_id BIGINT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ
);
-- 小队成员会话行 id（KVCS delete 需 8 字节行 id，proto SquadMemberSession.id）
ALTER TABLE squad_members ADD COLUMN IF NOT EXISTS id BIGSERIAL;
-- 军团属性（CreateCompanyRequest 的短名/描述）
ALTER TABLE companies ADD COLUMN IF NOT EXISTS name_short TEXT NOT NULL DEFAULT '';
ALTER TABLE companies ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT '';
-- 军团邀请（InviteToCompanyRequest / RespondToCompanyInvitationRequest）
CREATE TABLE IF NOT EXISTS company_invitations (
    id SERIAL PRIMARY KEY,
    company_id INT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    player_id BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    invited_by BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (company_id, player_id)
);
-- 军团徽章（CreateInsigniaRequest / RemoveInsigniaRequest；
-- parts 存 InsigniaPart 列表 JSON：[{partType,partId,fieldId,scale,offset}]）
CREATE TABLE IF NOT EXISTS player_insignias (
    id BIGSERIAL PRIMARY KEY,
    player_id BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    parts JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ===== U7 迁移（角色/装备维护域）=====
-- 角色软删（DeleteCharacterRequest，character KVCS f14 markedDeleted）
-- 与休假状态（SetCharacterActiveStatusRequest，character KVCS f24 isOnLeave）
ALTER TABLE characters ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE characters ADD COLUMN IF NOT EXISTS is_on_leave BOOLEAN NOT NULL DEFAULT FALSE;
-- 换头前的旧模板（ChangeCharacterTemplateRequest，character KVCS f19 previousCharacterTemplateId）
ALTER TABLE characters ADD COLUMN IF NOT EXISTS previous_template BIGINT NOT NULL DEFAULT 0;
-- 换头（SwapHeadRequest，character KVCS f22 headId / f23 eHeadSwapType）
ALTER TABLE characters ADD COLUMN IF NOT EXISTS head_id BIGINT NOT NULL DEFAULT 0;
-- 武器曳光弹效果（SetTracerEffectRequest，weapons 行级持久化）
ALTER TABLE weapons ADD COLUMN IF NOT EXISTS tracer_effect_id BIGINT NOT NULL DEFAULT 0;
-- 玩家语言（SetPlayerLanguageRequest）
ALTER TABLE players ADD COLUMN IF NOT EXISTS language_code TEXT NOT NULL DEFAULT 'en';
-- 已触发教程（MarkTutorialTriggeredRequest；完成态走 PlayerSettings f24 tutorialsCompleted）
CREATE TABLE IF NOT EXISTS player_tutorials_triggered (
    player_id BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    tutorial_template_id BIGINT NOT NULL,
    triggered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (player_id, tutorial_template_id)
);
-- 教程完成态（SetTutorialCompletedRequest；player KVCS f32 tutorialsCompleted）。
-- 客户端 ==0 时 SoldierShop 处于"新手免费招募"模式：等级选择组/价格按钮隐藏、
-- 关闭按钮跳阵营选择页；==1 才是正常招募界面（有兵种等级选择）。
-- 历史账号因该字段从未设置而永远卡在 0：有角色的玩家视为已完成教程。
ALTER TABLE players ADD COLUMN IF NOT EXISTS tutorials_completed INT NOT NULL DEFAULT 0;
UPDATE players SET tutorials_completed = 1
 WHERE tutorials_completed = 0
   AND id IN (SELECT DISTINCT player_id FROM characters);

-- 加成行归属（勋带/通用）：RibbonBoosterTemplate 与 BoosterTemplate 模板 id
-- 有重叠（通用表 1-5 是勋带表的子集），不能按模板 id 区分，购买时按请求
-- 类型记录。推送 KVCS 时决定进 RibbonBooster 还是 Booster 表，分类错误
-- 会导致勋带加成无法激活（客户端按表查模板）。
ALTER TABLE player_boosters ADD COLUMN IF NOT EXISTS is_ribbon BOOLEAN NOT NULL DEFAULT FALSE;
-- 历史行回填：模板不在通用表（1-5）内的一定是勋带加成
UPDATE player_boosters SET is_ribbon = TRUE
 WHERE is_ribbon = FALSE AND booster_template_id NOT IN (1,2,3,4,5);

-- ===== U8 迁移（RTS 战争域，docs/hng-rts-protocol.md Phase 2）=====
-- 战争实例（war_campaign.py 状态机：开战→running→结算→sequel 续战）
CREATE TABLE IF NOT EXISTS wars (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    begintime TIMESTAMPTZ NOT NULL DEFAULT now(),
    endtime TIMESTAMPTZ,
    winning_faction_id BIGINT NOT NULL DEFAULT 0,   -- 0=进行中；1/2/3
    points_needed_for_victory INT NOT NULL DEFAULT 15,
    sequel_war_id BIGINT NOT NULL DEFAULT 0,
    mapid BIGINT NOT NULL DEFAULT 1,
    state INT NOT NULL DEFAULT 1,                  -- 1=running 2=ended
    config_json JSONB NOT NULL DEFAULT '{}'        -- timescale/morale 等配置快照
);

-- 城市归属（battlefield 行 id = 城市；owner 1/2/3，0=中立）
CREATE TABLE IF NOT EXISTS war_cities (
    war_id BIGINT NOT NULL REFERENCES wars(id) ON DELETE CASCADE,
    battlefield_id BIGINT NOT NULL,
    owner_faction BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (war_id, battlefield_id)
);

-- 每场战争的阵营选择（官服 join_war_request 落库）：FactionSelectionWindow
-- 选定后存 (player, war, faction)；新战争（sequel 新 war_id）无行 → player
-- f11=0 → 客户端重弹选阵营窗（"每场战争重新选阵营"官服语义）
CREATE TABLE IF NOT EXISTS player_war_factions (
    player_id  BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    war_id     BIGINT NOT NULL REFERENCES wars(id) ON DELETE CASCADE,
    faction    INT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (player_id, war_id)
);


-- AT 实例：扩展既有 command_nodes（原表零使用，纯 schema 占位）
-- state：0=首都待命 1=移动中 2=会战中 3=已解散；morale/maxMorale 按
-- CommandNodeMoraleTemplate 官服实值（100）由 war_campaign 层维护
ALTER TABLE command_nodes ADD COLUMN IF NOT EXISTS abbreviation TEXT NOT NULL DEFAULT '';
ALTER TABLE command_nodes ADD COLUMN IF NOT EXISTS rank INT NOT NULL DEFAULT 0;
ALTER TABLE command_nodes ADD COLUMN IF NOT EXISTS xp BIGINT NOT NULL DEFAULT 0;
ALTER TABLE command_nodes ADD COLUMN IF NOT EXISTS resources JSONB NOT NULL DEFAULT '{}';
ALTER TABLE command_nodes ADD COLUMN IF NOT EXISTS morale INT NOT NULL DEFAULT 100;
ALTER TABLE command_nodes ADD COLUMN IF NOT EXISTS max_morale INT NOT NULL DEFAULT 100;
ALTER TABLE command_nodes ADD COLUMN IF NOT EXISTS state INT NOT NULL DEFAULT 0;
ALTER TABLE command_nodes ADD COLUMN IF NOT EXISTS map_entity_id BIGINT NOT NULL DEFAULT 0;
-- map_entity_type：0=按旧规则推导（id≠0 视为 battlefield）；1/2/3 显式实体类型
--（3=SUPPLYLINE 在途，transport_commandnode_segment 驱动动画）
ALTER TABLE command_nodes ADD COLUMN IF NOT EXISTS map_entity_type INT NOT NULL DEFAULT 0;
ALTER TABLE command_nodes ADD COLUMN IF NOT EXISTS general_character_id BIGINT NOT NULL DEFAULT 0;
ALTER TABLE command_nodes ADD COLUMN IF NOT EXISTS is_deployed BOOLEAN NOT NULL DEFAULT FALSE;
-- 官服 suppliedOnCreation / hasGotten*ForFree：买壳为空编，首次增援免费补齐
ALTER TABLE command_nodes ADD COLUMN IF NOT EXISTS has_gotten_soldier_free BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE command_nodes ADD COLUMN IF NOT EXISTS has_gotten_vehicle_free BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE command_nodes ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
-- AT 战绩（GetAssaultTeamStatsRecord f5/f6；XP 口径 battles×5+battlesWon×25）
ALTER TABLE command_nodes ADD COLUMN IF NOT EXISTS battles INT NOT NULL DEFAULT 0;
ALTER TABLE command_nodes ADD COLUMN IF NOT EXISTS battles_won INT NOT NULL DEFAULT 0;
-- AT 战绩刷新时间戳（CommandNodeDefinition f10 statUpdateTimeStamp：rank/xp/
-- battles 变化时刷新 → 客户端 statsHaveChanged 重拉 GetAssaultTeamStats）
ALTER TABLE command_nodes ADD COLUMN IF NOT EXISTS stats_updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
-- 补给队（Reserve）父链：template_id=faction*100+4 时指向被补给的 AT
ALTER TABLE command_nodes ADD COLUMN IF NOT EXISTS parent_command_node_id BIGINT NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_command_nodes_war ON command_nodes(war_id);

-- 旧库修正：rank 默认曾是 1（0 XP 新 AT 可直接升级的根因），存量行按
-- xp 归零重算（0 XP → rank 0；有 xp 的行由结算 rank_from_xp 口径重推）
UPDATE command_nodes SET rank = 0 WHERE xp = 0 AND rank > 0;

-- AT 移动（transport_commandnode 路径；path_json=段列表
-- [{sl_id,from_ap,to_ap,from_bf,to_bf,seconds}]）
CREATE TABLE IF NOT EXISTS commandnode_movements (
    id BIGSERIAL PRIMARY KEY,
    commandnode_id BIGINT NOT NULL REFERENCES command_nodes(id) ON DELETE CASCADE,
    war_id BIGINT NOT NULL DEFAULT 0,
    path_json JSONB NOT NULL DEFAULT '[]',
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    arrive_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    state INT NOT NULL DEFAULT 0,                   -- 0=moving 1=arrived 2=cancelled
    dest_ap BIGINT NOT NULL DEFAULT 0               -- 目标出兵点（0=城市本体）
);
ALTER TABLE commandnode_movements ADD COLUMN IF NOT EXISTS dest_ap BIGINT NOT NULL DEFAULT 0;

-- 会战登记（M1 桩：敌对相遇登记+地图 battle 标记；M2 接战斗服）
CREATE TABLE IF NOT EXISTS war_battles (
    id BIGSERIAL PRIMARY KEY,
    war_id BIGINT NOT NULL,
    battlefield_id BIGINT NOT NULL,
    factions_json JSONB NOT NULL DEFAULT '[]',      -- [{at_id, faction}]
    state INT NOT NULL DEFAULT 0,                   -- 0=active 1=ended
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at TIMESTAMPTZ,
    winning_faction INT NOT NULL DEFAULT 0
);
ALTER TABLE wars ADD COLUMN IF NOT EXISTS production_ms BIGINT NOT NULL DEFAULT 0;

-- warfunds 流水（余额仍在 players.warfunds）
CREATE TABLE IF NOT EXISTS war_funds_ledger (
    id BIGSERIAL PRIMARY KEY,
    player_id BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    amount BIGINT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    balance_after BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_war_funds_player ON war_funds_ledger(player_id);

-- 阵营资源库存（M3 生产 tick：按 ArmyResourceProductionTemplate 初始值
-- 开战初始化，之后周期累加；增援/部署从这里扣）
CREATE TABLE IF NOT EXISTS war_faction_resources (
    war_id BIGINT NOT NULL,
    faction INT NOT NULL,                          -- 1/2/3
    category_id INT NOT NULL,                      -- armyresourcecategory id
    count BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (war_id, faction, category_id)
);

-- 阵营补给队列（M3 #3/#5：增援/部署库存不足入队；queue_number 单调递增，
-- 推进到队头且库存足够时资源从阵营库存转移到 AT）
CREATE TABLE IF NOT EXISTS war_supply_queue (
    id BIGSERIAL PRIMARY KEY,
    war_id BIGINT NOT NULL,
    faction INT NOT NULL,
    command_node_id BIGINT NOT NULL,
    category_id INT NOT NULL,
    count INT NOT NULL DEFAULT 0,
    queue_number BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""



class PlayerStore:
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

    # ---- players ----

    async def get_player(self, username):
        """按用户名查 player_id；账号不存在返回 None（不自动建号）。"""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                'SELECT id FROM players WHERE username = $1', username)

    async def create_player(self, username, password):
        """手动建号（add_user.py 使用）。已存在返回 None，否则返回 player_id。"""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                'INSERT INTO players (username, password) VALUES ($1, $2) '
                'ON CONFLICT (username) DO NOTHING RETURNING id',
                username, password)

    async def get_password(self, username):
        """返回账号密码；账号不存在或已软删除（管理后台 deleted_at）返回 None。"""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                'SELECT password FROM players '
                'WHERE username = $1 AND deleted_at IS NULL', username)

    async def get_extra_unlock_uids(self, player_id, character_id):
        """返回该角色勋带购买的额外解锁 uid 集合（character_extra_unlocks 行表）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT u.unlock_uid FROM character_extra_unlocks u '
                'JOIN characters c ON c.id = u.character_id '
                'WHERE c.id = $1 AND c.player_id = $2',
                character_id, player_id)
        return {int(r['unlock_uid']) for r in rows}

    async def add_extra_unlock_uids(self, player_id, character_id, uids):
        """追加角色勋带购买的额外解锁 uid（progressionUnlock.id），幂等。"""
        if not uids:
            return
        async with self.pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO character_extra_unlocks (character_id, unlock_uid) '
                'SELECT c.id, x FROM characters c, unnest($3::bigint[]) AS x '
                'WHERE c.id = $1 AND c.player_id = $2 '
                'ON CONFLICT DO NOTHING',
                character_id, player_id, [int(u) for u in uids])

    async def get_combat_xp(self, player_id, character_id):
        """返回角色战斗经验（characters.combat_xp），未找到返回 0。"""
        async with self.pool.acquire() as conn:
            val = await conn.fetchval(
                'SELECT combat_xp FROM characters '
                'WHERE id = $1 AND player_id = $2', character_id, player_id)
        return int(val or 0)

    async def add_combat_xp(self, player_id, character_id, amount):
        """给角色增加战斗经验并返回新值。"""
        async with self.pool.acquire() as conn:
            return int(await conn.fetchval(
                'UPDATE characters SET combat_xp = combat_xp + $1 '
                'WHERE id = $2 AND player_id = $3 RETURNING combat_xp',
                int(amount), character_id, player_id))

    async def get_currency(self, player_id):
        """返回 {credits, warfunds, gold}；账号不存在返回默认值。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT credits, warfunds, gold FROM players WHERE id = $1', player_id)
        if row is None:
            return {'credits': 100000, 'warfunds': 100000, 'gold': 100000}
        return {'credits': row['credits'], 'warfunds': row['warfunds'],
                'gold': row['gold']}

    async def set_currency(self, player_id, credits=None, warfunds=None, gold=None):
        """更新账号货币（只更新非 None 字段）。"""
        sets, args = [], []
        if credits is not None:
            args.append(credits); sets.append('credits = $%d' % len(args))
        if warfunds is not None:
            args.append(warfunds); sets.append('warfunds = $%d' % len(args))
        if gold is not None:
            args.append(gold); sets.append('gold = $%d' % len(args))
        if not sets:
            return
        args.append(player_id)
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE players SET ' + ', '.join(sets) +
                ' WHERE id = $%d' % len(args), *args)

    async def set_password(self, username, password):
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE players SET password = $1 WHERE username = $2',
                password, username)

    async def get_tutorials_completed(self, player_id):
        """返回教程完成态（0=未完成 1=已完成；player KVCS f32）。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT tutorials_completed FROM players WHERE id = $1', player_id)
        return int(row['tutorials_completed']) if row else 0

    async def set_tutorials_completed(self, player_id, value):
        """持久化教程完成态（SetTutorialCompletedRequest）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE players SET tutorials_completed = $1 WHERE id = $2',
                int(value), int(player_id))

    async def get_level_granted(self, player_id):
        """玩家等级奖励已发到的等级（进展 tab PlayerLevelUnlock 奖励防重发）。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT level_granted FROM players WHERE id = $1', player_id)
        return int(row['level_granted']) if row else 1

    async def set_level_granted(self, player_id, level):
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE players SET level_granted = $1 WHERE id = $2',
                int(level), int(player_id))

    # ---- characters ----

    async def list_characters(self, player_id):
        """返回 [{id, name, faction, template, combat_xp, combat_badge_0, combat_badge_1,
        uniform_variant_id, headgear_id, squad_points, receive_daily_bonus, is_on_leave,
        previous_template, head_id}]，按创建顺序；软删角色（is_deleted）不返回。
        军衔等级由 combat_xp 经 ExperienceLevels 阈值推导（单源）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, name, faction, template, combat_xp, combat_badge_0, combat_badge_1, '
                'uniform_variant_id, headgear_id, squad_points, '
                'COALESCE(last_daily_battle_at::date <> CURRENT_DATE, TRUE) '
                'AS receive_daily_bonus, '
                'is_on_leave, previous_template, head_id '
                'FROM characters WHERE player_id = $1 AND NOT is_deleted '
                'ORDER BY id', player_id)
            return [dict(r) for r in rows]

    async def get_character(self, player_id, character_id):
        """按 id 查询单个角色（软删角色视为不存在），未找到返回 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, name, faction, template, combat_xp, combat_badge_0, combat_badge_1, '
                'uniform_variant_id, headgear_id, squad_points, '
                'COALESCE(last_daily_battle_at::date <> CURRENT_DATE, TRUE) '
                'AS receive_daily_bonus, '
                'is_on_leave, previous_template, head_id '
                'FROM characters WHERE id = $1 AND player_id = $2 AND NOT is_deleted',
                character_id, player_id)
            return dict(row) if row else None

    async def consume_daily_bonus(self, player_id, character_id):
        """每日首战 +50% XP 资格消费：当天未用则记账并返回 True。

        单条 UPDATE 原子判定（WHERE 带资格条件 + RETURNING），当天已
        消费（last_daily_battle_at 为今天）返回 False 不重复记账；
        跨天后 SELECT 侧推导自动回 TRUE，无需定时任务。
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'UPDATE characters SET last_daily_battle_at = now() '
                'WHERE id = $1 AND player_id = $2 AND NOT is_deleted '
                'AND COALESCE(last_daily_battle_at::date <> CURRENT_DATE, TRUE) '
                'RETURNING id',
                int(character_id), int(player_id))
            return row is not None

    async def create_character(self, player_id, name, faction, template, combat_xp=0,
                               uniform_variant_template_id=0):
        """创建角色（军衔单源：起始军衔由调用方换算为初始 combat_xp）。

        uniform_variant_template_id 非零时同步创建默认军服实例并装备
        （(兵种,阵营) 基础迷彩，由调用方经 wardb.default_uniform_variant 解析；
        官方服角色生来带基础迷彩，缺失则 PlayerSettings f9 为空、角色不显示）。
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    'INSERT INTO characters (player_id, name, faction, template, combat_xp) '
                    'VALUES ($1, $2, $3, $4, $5) '
                    'RETURNING id, name, faction, template, combat_xp, '
                    'combat_badge_0, combat_badge_1, uniform_variant_id, headgear_id, '
                    'squad_points, receive_daily_bonus',
                    player_id, name, faction, template, combat_xp)
                if uniform_variant_template_id:
                    uv = await conn.fetchrow(
                        'INSERT INTO uniform_variants '
                        '(player_id, uniform_variant_template_id, character_id) '
                        'VALUES ($1, $2, $3) RETURNING id',
                        player_id, uniform_variant_template_id, row['id'])
                    row = await conn.fetchrow(
                        'UPDATE characters SET uniform_variant_id = $1 WHERE id = $2 '
                        'RETURNING id, name, faction, template, combat_xp, '
                        'combat_badge_0, combat_badge_1, uniform_variant_id, headgear_id, '
                        'squad_points, receive_daily_bonus',
                        uv['id'], row['id'])
                return dict(row)

    # ---- character ribbon XP（勋带单源）----

    async def get_ribbon_xp(self, player_id, character_id):
        """返回该角色 {ribbonTemplateId: total_xp}（勋带累计经验，等级由阈值推导）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT r.ribbon_template_id, r.total_xp '
                'FROM character_ribbon_xp r JOIN characters c ON c.id = r.character_id '
                'WHERE c.id = $1 AND c.player_id = $2',
                character_id, player_id)
            return {int(r['ribbon_template_id']): float(r['total_xp']) for r in rows}

    async def get_ribbon_xp_by_char(self, character_id):
        """按角色 id 直接取勋带 XP（统计服务器用：客户端 GetTotals 只报 charId）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT ribbon_template_id, total_xp FROM character_ribbon_xp '
                'WHERE character_id = $1',
                character_id)
            return {int(r['ribbon_template_id']): float(r['total_xp']) for r in rows}

    async def set_ribbon_xp_floor(self, player_id, character_id,
                                  ribbon_template_id, min_xp):
        """购买勋带等级 = 把该勋带 XP 补到门槛（total_xp = max(total_xp, min_xp)）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO character_ribbon_xp (character_id, ribbon_template_id, total_xp) '
                'SELECT id, $2, $3 FROM characters WHERE id = $1 AND player_id = $4 '
                'ON CONFLICT (character_id, ribbon_template_id) '
                'DO UPDATE SET total_xp = GREATEST(character_ribbon_xp.total_xp, EXCLUDED.total_xp)',
                character_id, ribbon_template_id, min_xp, player_id)

    async def add_ribbon_xp(self, player_id, character_id,
                            ribbon_template_id, amount):
        """战后结算：给勋带增加经验（total_xp += amount），返回新值。"""
        async with self.pool.acquire() as conn:
            val = await conn.fetchval(
                'INSERT INTO character_ribbon_xp (character_id, ribbon_template_id, total_xp) '
                'SELECT id, $2, $3 FROM characters WHERE id = $1 AND player_id = $4 '
                'ON CONFLICT (character_id, ribbon_template_id) '
                'DO UPDATE SET total_xp = character_ribbon_xp.total_xp + EXCLUDED.total_xp '
                'RETURNING character_ribbon_xp.total_xp',
                character_id, ribbon_template_id, amount, player_id)
            return float(val or 0)

    async def set_combat_badge(self, player_id, character_id, slot, badge_id):
        """设置角色战斗徽章槽（slot=0/1）；badge_id=0 表示卸下。

        官方语义是"移动"而非"复制"：客户端把已装备徽章拖到另一槽时只发
        set 新槽一条请求、不发卸下旧槽（SoldierOverviewMediator.
        badgesList_dragDropHandler:960-973），故装备非 0 徽章时若另一槽
        持有同一徽章需同步清 0，否则两槽同 id → UI 显示"复制"。
        """
        if int(slot) == 0:
            column, other = 'combat_badge_0', 'combat_badge_1'
        else:
            column, other = 'combat_badge_1', 'combat_badge_0'
        badge_id = int(badge_id)
        async with self.pool.acquire() as conn:
            if badge_id:
                await conn.execute(
                    'UPDATE characters SET {0} = $1, '
                    '{1} = CASE WHEN {1} = $1 THEN 0 ELSE {1} END '
                    'WHERE id = $2 AND player_id = $3'.format(column, other),
                    badge_id, int(character_id), int(player_id))
            else:
                await conn.execute(
                    'UPDATE characters SET {0} = 0 '
                    'WHERE id = $1 AND player_id = $2'.format(column),
                    int(character_id), int(player_id))

    async def set_uniform_variant(self, player_id, character_id, variant_id):
        """装备/卸下角色军服迷彩变体（variant_id=0 卸下），返回该角色 uniform_variant_id。"""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                'UPDATE characters SET uniform_variant_id = $1 '
                'WHERE id = $2 AND player_id = $3 RETURNING uniform_variant_id',
                int(variant_id), int(character_id), int(player_id))

    # ---- vehicles ----

    async def list_vehicles(self, player_id, character_id=None):
        """返回 [{id, vehicle_variant_template_id, character_id, equipped_in_slot, variant_id,
        condition, auto_repair}]，按 id 顺序。character_id 非空时只返回该角色名下的载具。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, vehicle_variant_template_id, character_id, equipped_in_slot, '
                'variant_id, condition, auto_repair '
                'FROM vehicles WHERE player_id = $1 '
                'AND ($2::bigint IS NULL OR character_id = $2) ORDER BY id',
                player_id, character_id)
            return [dict(r) for r in rows]

    async def get_vehicle(self, player_id, vehicle_id):
        """按 id 查询单个载具，未找到返回 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, vehicle_variant_template_id, character_id, equipped_in_slot, '
                'variant_id, condition, auto_repair '
                'FROM vehicles WHERE id = $1 AND player_id = $2', vehicle_id, player_id)
            return dict(row) if row else None

    async def create_vehicle(self, player_id, vehicle_variant_template_id,
                             character_id, condition=None):
        """创建载具实例，返回含新 id 的 dict。
        condition 缺省时按模板满耐久初始化（vehicletemplate f35 maxCondition，
        经变体 → 基础模板解析）——旧实现不写 condition 吃 DB 默认 100.0，
        地面载具（max 25~75）超满、飞机（max 200/220）半损，购买即异常。"""
        if condition is None:
            import wardb
            condition = wardb.vehicle_full_condition(vehicle_variant_template_id)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO vehicles (player_id, vehicle_variant_template_id, '
                'character_id, condition) VALUES ($1, $2, $3, $4) '
                'RETURNING id, vehicle_variant_template_id, character_id, equipped_in_slot, '
                'variant_id, condition',
                player_id, vehicle_variant_template_id, character_id, float(condition))
            return dict(row)

    async def set_vehicle_equipment(self, player_id, vehicle_id,
                                    character_id, equip_in_slot):
        """装备载具到角色槽位（equip_in_slot>0）或卸下（0）。返回受影响载具列表
        [{id, vehicle_variant_template_id, character_id, equipped_in_slot, variant_id}]（含被挤掉槽位的）。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # 目标载具校验归属
                row = await conn.fetchrow(
                    'SELECT id, vehicle_variant_template_id, character_id, equipped_in_slot, variant_id, condition '
                    'FROM vehicles WHERE id = $1 AND player_id = $2',
                    vehicle_id, player_id)
                if row is None:
                    return []
                affected = [dict(row)]
                if int(equip_in_slot) > 0:
                    # 挤掉同角色同槽位的其他载具（一个槽位只能一辆）
                    prev = await conn.fetch(
                        'SELECT id, vehicle_variant_template_id, character_id, equipped_in_slot, variant_id, condition '
                        'FROM vehicles WHERE player_id = $1 AND character_id = $2 '
                        'AND equipped_in_slot = $3 AND id <> $4',
                        player_id, character_id, int(equip_in_slot), vehicle_id)
                    for p in prev:
                        await conn.execute(
                            'UPDATE vehicles SET equipped_in_slot = 0 '
                            'WHERE id = $1', p['id'])
                        # asyncpg Record 不可变，须先转 dict 副本再改写，
                        # 否则 TypeError 导致整个事务回滚（挤槽位静默失败）
                        pd = dict(p)
                        pd['equipped_in_slot'] = 0
                        affected.append(pd)
                await conn.execute(
                    'UPDATE vehicles SET character_id = $1, equipped_in_slot = $2 '
                    'WHERE id = $3', character_id, int(equip_in_slot), vehicle_id)
            # 返回最新状态
            ids = [a['id'] for a in affected]
            rows = await conn.fetch(
                'SELECT id, vehicle_variant_template_id, character_id, equipped_in_slot, variant_id, condition '
                'FROM vehicles WHERE id = ANY($1::bigint[]) ORDER BY id', ids)
            return [dict(r) for r in rows]

    # ---- vehicle variants（载具皮肤）----

    async def list_vehicle_variants(self, player_id):
        """返回 [{id, vehicle_id, character_id, vehicle_variant_template_id}]，按 id 顺序。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, vehicle_id, character_id, vehicle_variant_template_id '
                'FROM vehicle_variants WHERE player_id = $1 ORDER BY id', player_id)
            return [dict(r) for r in rows]

    async def create_vehicle_variant(self, player_id, vehicle_id, character_id,
                                     vehicle_variant_template_id):
        """创建载具皮肤变体实例（购买），返回含新 id 的 dict。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO vehicle_variants '
                '(player_id, vehicle_id, character_id, vehicle_variant_template_id) '
                'VALUES ($1, $2, $3, $4) '
                'RETURNING id, vehicle_id, character_id, vehicle_variant_template_id',
                player_id, vehicle_id, character_id, vehicle_variant_template_id)
            return dict(row)

    async def set_vehicle_variant(self, player_id, vehicle_id, variant_id):
        """装备/卸下载具皮肤（variant_id=0 卸下）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE vehicles SET variant_id = $1 '
                'WHERE id = $2 AND player_id = $3', variant_id, vehicle_id, player_id)

    # ---- vehicle upgrades（载具升级/补给箱）----

    async def list_vehicle_upgrades(self, player_id):
        """返回 [{id, upgrade_template_id, vehicle_id, character_id, equipped_slot_id,
        condition, auto_repair}]。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, upgrade_template_id, vehicle_id, character_id, equipped_slot_id, '
                'condition, auto_repair '
                'FROM vehicle_upgrades WHERE player_id = $1 ORDER BY id', player_id)
            return [dict(r) for r in rows]

    async def create_vehicle_upgrade(self, player_id, upgrade_template_id,
                                     vehicle_id, character_id, condition):
        """创建载具升级实例（购买），返回含新 id 的 dict。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO vehicle_upgrades (player_id, upgrade_template_id, vehicle_id, '
                'character_id, equipped_slot_id, condition) VALUES ($1, $2, $3, $4, 0, $5) '
                'RETURNING id, upgrade_template_id, vehicle_id, character_id, equipped_slot_id, condition',
                player_id, upgrade_template_id, vehicle_id, character_id, condition)
            return dict(row)

    async def set_vehicle_upgrade_equipment(self, player_id, upgrade_id, slot_id, do_equip):
        """装备（do_equip=true）或卸下（false）载具升级。装备时先清空同载具同槽位
        的其他升级（互斥：一个槽位只能装一件）。返回受影响升级行列表。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    'SELECT id, upgrade_template_id, vehicle_id, character_id, equipped_slot_id, condition '
                    'FROM vehicle_upgrades WHERE id = $1 AND player_id = $2',
                    upgrade_id, player_id)
                if row is None:
                    return []
                affected = [dict(row)]
                if do_equip:
                    # 先记录同载具同槽位被挤掉的升级，再清槽（互斥：一个槽位只能装一件）
                    cleared = await conn.fetch(
                        'SELECT id, upgrade_template_id, vehicle_id, character_id, equipped_slot_id, condition '
                        'FROM vehicle_upgrades WHERE player_id = $1 AND vehicle_id = $2 '
                        'AND equipped_slot_id = $3 AND id != $4',
                        player_id, row['vehicle_id'], slot_id, upgrade_id)
                    await conn.execute(
                        'UPDATE vehicle_upgrades SET equipped_slot_id = 0 '
                        'WHERE player_id = $1 AND vehicle_id = $2 '
                        'AND equipped_slot_id = $3 AND id != $4',
                        player_id, row['vehicle_id'], slot_id, upgrade_id)
                    for c in cleared:
                        c['equipped_slot_id'] = 0
                        affected.append(dict(c))
                new_slot = slot_id if do_equip else 0
                await conn.execute(
                    'UPDATE vehicle_upgrades SET equipped_slot_id = $1 '
                    'WHERE id = $2', new_slot, upgrade_id)
                affected[0]['equipped_slot_id'] = new_slot
            return affected

    async def update_vehicle_upgrade_condition(self, player_id, upgrade_id, condition):
        """修复载具升级：更新 condition（耐久）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE vehicle_upgrades SET condition = $1 '
                'WHERE id = $2 AND player_id = $3', condition, upgrade_id, player_id)

    # ---- vehicle ammo（载具弹药）----

    async def list_vehicle_ammo(self, player_id):
        """返回 [{id, ammo_template_id, vehicle_id, character_id, equipped_storage_id,
        slot_index, condition, spawn_clip_count, auto_repair}]，按 id 顺序。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, ammo_template_id, vehicle_id, character_id, equipped_storage_id, '
                'slot_index, condition, spawn_clip_count, auto_repair '
                'FROM vehicle_ammo WHERE player_id = $1 ORDER BY id', player_id)
            return [dict(r) for r in rows]

    async def create_vehicle_ammo(self, player_id, ammo_template_id, vehicle_id,
                                  character_id, condition):
        """创建载具弹药实例（购买），返回含新 id 的 dict。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO vehicle_ammo (player_id, ammo_template_id, vehicle_id, character_id, '
                'equipped_storage_id, slot_index, condition) '
                'VALUES ($1, $2, $3, $4, 0, 0, $5) '
                'RETURNING id, ammo_template_id, vehicle_id, character_id, equipped_storage_id, '
                'slot_index, condition, spawn_clip_count',
                player_id, ammo_template_id, vehicle_id, character_id, condition)
            return dict(row)

    async def set_vehicle_ammo_equipment(self, player_id, ammo_id, storage_id,
                                         slot_index, do_equip):
        """装备（do_equip=true）或卸下（false）载具弹药。装备时先清空同载具同仓同槽位
        的其他弹药（互斥：一个弹药仓槽位只能装一件）。返回受影响弹药行列表。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    'SELECT id, ammo_template_id, vehicle_id, character_id, equipped_storage_id, '
                    'slot_index, condition '
                    'FROM vehicle_ammo WHERE id = $1 AND player_id = $2',
                    ammo_id, player_id)
                if row is None:
                    return []
                affected = [dict(row)]
                if do_equip:
                    # 同载具同仓同槽位被挤掉的弹药先清槽（互斥）
                    cleared = await conn.fetch(
                        'SELECT id, ammo_template_id, vehicle_id, character_id, equipped_storage_id, '
                        'slot_index, condition '
                        'FROM vehicle_ammo WHERE player_id = $1 AND vehicle_id = $2 '
                        'AND equipped_storage_id = $3 AND slot_index = $4 AND id != $5',
                        player_id, row['vehicle_id'], storage_id, slot_index, ammo_id)
                    await conn.execute(
                        'UPDATE vehicle_ammo SET equipped_storage_id = 0, slot_index = 0 '
                        'WHERE player_id = $1 AND vehicle_id = $2 '
                        'AND equipped_storage_id = $3 AND slot_index = $4 AND id != $5',
                        player_id, row['vehicle_id'], storage_id, slot_index, ammo_id)
                    for c in cleared:
                        c['equipped_storage_id'] = 0
                        c['slot_index'] = 0
                        affected.append(dict(c))
                new_storage = storage_id if do_equip else 0
                new_slot = slot_index if do_equip else 0
                await conn.execute(
                    'UPDATE vehicle_ammo SET equipped_storage_id = $1, slot_index = $2 '
                    'WHERE id = $3', new_storage, new_slot, ammo_id)
                affected[0]['equipped_storage_id'] = new_storage
                affected[0]['slot_index'] = new_slot
            return affected

    async def update_vehicle_ammo_condition(self, player_id, ammo_id, condition):
        """修复载具弹药：更新 condition（耐久）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE vehicle_ammo SET condition = $1 '
                'WHERE id = $2 AND player_id = $3', condition, ammo_id, player_id)

    # ---- uniform variants（军服迷彩变体）----

    async def list_uniform_variants(self, player_id):
        """返回 [{id, uniform_variant_template_id, character_id}]，按 id 顺序。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, uniform_variant_template_id, character_id '
                'FROM uniform_variants WHERE player_id = $1 ORDER BY id', player_id)
            return [dict(r) for r in rows]

    async def create_uniform_variant(self, player_id, uniform_variant_template_id, character_id):
        """创建军服迷彩变体实例（购买），返回含新 id 的 dict。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO uniform_variants (player_id, uniform_variant_template_id, character_id) '
                'VALUES ($1, $2, $3) '
                'RETURNING id, uniform_variant_template_id, character_id',
                player_id, uniform_variant_template_id, character_id)
            return dict(row)

    # ---- uniform parts（头盔/军装部件）----

    async def list_uniform_parts(self, player_id):
        """返回 [{id, character_id, uniform_part_template_id, uniform_part_type}]，按 id 顺序。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, character_id, uniform_part_template_id, uniform_part_type '
                'FROM uniform_parts WHERE player_id = $1 ORDER BY id', player_id)
            return [dict(r) for r in rows]

    async def create_uniform_part(self, player_id, character_id,
                                  uniform_part_template_id, uniform_part_type):
        """创建头盔/军装部件实例（购买），返回含新 id 的 dict。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO uniform_parts '
                '(player_id, character_id, uniform_part_template_id, uniform_part_type) '
                'VALUES ($1, $2, $3, $4) '
                'RETURNING id, character_id, uniform_part_template_id, uniform_part_type',
                player_id, character_id, uniform_part_template_id, uniform_part_type)
            return dict(row)

    async def set_character_headgear(self, player_id, character_id, headgear_id):
        """装备/卸下角色头盔（headgear_id=0 卸下）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE characters SET headgear_id = $1 '
                'WHERE id = $2 AND player_id = $3', headgear_id, character_id, player_id)

    async def get_character_appearance(self, player_id, character_id):
        """角色装备的迷彩/头盔模板 id（SquadMemberSession f8/f19 小队头像用）。

        characters.uniform_variant_id / headgear_id 存的是实例 id，这里换算成
        uniform_variants / uniform_parts 表的模板 id；未装备（0 或实例缺失）
        返回 0，客户端按默认军服渲染。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT (SELECT uniform_variant_template_id FROM uniform_variants '
                'WHERE id = c.uniform_variant_id) AS uvt, '
                '(SELECT uniform_part_template_id FROM uniform_parts '
                'WHERE id = c.headgear_id) AS upt '
                'FROM characters c WHERE c.id = $1 AND c.player_id = $2',
                int(character_id), int(player_id))
        if row is None:
            return (0, 0)
        return (int(row['uvt'] or 0), int(row['upt'] or 0))

    # ---- weapon variants（武器皮肤变体）----

    async def list_weapon_variants(self, player_id):
        """返回 [{id, weapon_id, character_id, weapon_variant_template_id}]，按 id 顺序。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, weapon_id, character_id, weapon_variant_template_id '
                'FROM weapon_variants WHERE player_id = $1 ORDER BY id', player_id)
            return [dict(r) for r in rows]

    async def create_weapon_variant(self, player_id, weapon_id, character_id,
                                    weapon_variant_template_id):
        """创建武器皮肤变体实例（购买），返回含新 id 的 dict。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO weapon_variants '
                '(player_id, weapon_id, character_id, weapon_variant_template_id) '
                'VALUES ($1, $2, $3, $4) '
                'RETURNING id, weapon_id, character_id, weapon_variant_template_id',
                player_id, weapon_id, character_id, weapon_variant_template_id)
            return dict(row)

    async def bind_weapon_variant(self, player_id, variant_id, weapon_id, character_id):
        """绑定武器皮肤 depot 行到武器（SetWeaponVariantRequest 装备仓库皮肤）。
        行必须属于该玩家且 weapon_id=0（仓库行）；绑定后 character_id 跟随武器，
        客户端 isWeaponVariantOwned 要求 characterId==选中角色。返回更新后行或 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'UPDATE weapon_variants SET weapon_id = $3, character_id = $4 '
                'WHERE id = $1 AND player_id = $2 AND weapon_id = 0 '
                'RETURNING id, weapon_id, character_id, weapon_variant_template_id',
                int(variant_id), int(player_id), int(weapon_id), int(character_id))
            return dict(row) if row else None

    # ---- tracer effects（曳光弹）----

    async def list_tracer_effects(self, player_id):
        """返回 [{id, tracer_template_id, character_id, weapon_id, is_equipped}]，按 id 顺序。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, tracer_template_id, character_id, weapon_id, is_equipped '
                'FROM tracer_effects WHERE player_id = $1 ORDER BY id', player_id)
            return [dict(r) for r in rows]

    async def create_tracer_effect(self, player_id, tracer_template_id,
                                   character_id=0, weapon_id=0):
        """创建曳光弹效果实例（通行证奖励发放），返回含新 id 的 dict。
        官方发放 target='depot'：character_id=0 的仓库行，装备时才绑定武器。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO tracer_effects '
                '(player_id, tracer_template_id, character_id, weapon_id) '
                'VALUES ($1, $2, $3, $4) '
                'RETURNING id, tracer_template_id, character_id, weapon_id, is_equipped',
                int(player_id), int(tracer_template_id),
                int(character_id), int(weapon_id))
            return dict(row)

    async def get_tracer_effect(self, player_id, tracer_id):
        """按 id 查询单个曳光弹实例，未找到返回 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, tracer_template_id, character_id, weapon_id, is_equipped '
                'FROM tracer_effects WHERE id = $1 AND player_id = $2',
                int(tracer_id), int(player_id))
            return dict(row) if row else None

    async def get_weapon(self, player_id, weapon_id):
        """按 id 查询单个武器，未找到返回 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, name, template_id, character_id, container_id, container_slot, '
                'variant_template_id, variant_id, condition, clip_count, '
                'auto_repair, tracer_effect_id '
                'FROM weapons WHERE id = $1 AND player_id = $2', weapon_id, player_id)
            return dict(row) if row else None

    async def delete_weapon(self, player_id, weapon_id):
        """删除武器实例及关联弹药/配件/皮肤（清理历史 bug 产生的 template_id=0 假武器）。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    'DELETE FROM weapon_ammo WHERE weapon_id = $1 AND player_id = $2',
                    weapon_id, player_id)
                await conn.execute(
                    'DELETE FROM weapon_modifiers WHERE weapon_id = $1 AND player_id = $2',
                    weapon_id, player_id)
                await conn.execute(
                    'DELETE FROM weapon_variants WHERE weapon_id = $1 AND player_id = $2',
                    weapon_id, player_id)
                await conn.execute(
                    'DELETE FROM weapons WHERE id = $1 AND player_id = $2', weapon_id, player_id)

    # ---- weapons ----

    async def list_weapons(self, player_id, character_id=None):
        """返回 [{id, name, template_id, character_id, container_id, container_slot,
        variant_template_id, variant_id, condition, clip_count, auto_repair}]，按 id 顺序。
        character_id 非空时只返回该角色名下的武器（character_id=0 的仓库物品
        不属于任何角色，不返回）——解锁位图等按角色语义的调用必须传此参数。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, name, template_id, character_id, container_id, container_slot, '
                'variant_template_id, variant_id, condition, clip_count, auto_repair '
                'FROM weapons WHERE player_id = $1 '
                'AND ($2::bigint IS NULL OR character_id = $2) ORDER BY id',
                player_id, character_id)
            return [dict(r) for r in rows]

    async def set_weapon_clip_count(self, player_id, weapon_id, clip_count):
        """持久化武器弹夹三态选择（SetWeaponClipCountRequest；
        负=最少 / 0=默认 / 正=最大）。返回更新后的行，未找到返回 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'UPDATE weapons SET clip_count = $3 '
                'WHERE id = $1 AND player_id = $2 '
                'RETURNING id, name, template_id, character_id, container_id, '
                'container_slot, variant_template_id, variant_id, condition, clip_count',
                weapon_id, player_id, clip_count)
            return dict(row) if row else None

    async def create_weapon(self, player_id, name, template_id, character_id,
                            variant_template_id=0):
        """创建武器实例，返回含新 id 的 dict。clip_count 默认 0（默认弹夹档）。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO weapons (player_id, name, template_id, character_id, '
                'variant_template_id) VALUES ($1, $2, $3, $4, $5) '
                'RETURNING id, name, template_id, character_id, container_id, '
                'container_slot, variant_template_id, variant_id, condition, clip_count',
                player_id, name, template_id, character_id, variant_template_id)
            return dict(row)

    async def set_weapon_variant(self, player_id, weapon_id, variant_id):
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE weapons SET variant_id = $1 '
                'WHERE id = $2 AND player_id = $3', variant_id, weapon_id, player_id)

    async def update_weapon_container(self, player_id, weapon_id,
                                      container_id, container_slot):
        """装备（container_id=角色id, slot=1-4）或卸下（0, 0）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE weapons SET container_id = $1, container_slot = $2 '
                'WHERE id = $3 AND player_id = $4',
                container_id, container_slot, weapon_id, player_id)

    async def set_weapon_equipment(self, player_id, weapon_id,
                                   container_id, container_slot):
        """装备武器到角色槽位（container_slot>0）或卸下（0, 0）。返回受影响武器
        列表（含被挤掉同槽位的其他武器，与 set_vehicle_equipment 同构）。

        客户端替换槽位武器时只发 set_equipmentselection（新武器），不发
        remove_equipmentselection（旧武器）——服务端必须把同容器同槽位的
        旧武器挤回军械库（container 归零）并推送其 KVCS，否则旧武器既不在
        军械库列表（container_slot 仍非 0）也不在槽位显示，且装备点数
        按新旧两把重复计算。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    'SELECT id, name, template_id, character_id, container_id, '
                    'container_slot, variant_template_id, variant_id, condition, '
                    'clip_count, auto_repair FROM weapons '
                    'WHERE id = $1 AND player_id = $2',
                    weapon_id, player_id)
                if row is None:
                    return []
                affected = [dict(row)]
                if int(container_slot) > 0:
                    # 挤掉同容器同槽位的其他武器（一个槽位只能一把）
                    prev = await conn.fetch(
                        'SELECT id, name, template_id, character_id, container_id, '
                        'container_slot, variant_template_id, variant_id, condition, '
                        'clip_count, auto_repair FROM weapons '
                        'WHERE player_id = $1 AND container_id = $2 '
                        'AND container_slot = $3 AND id <> $4',
                        player_id, container_id, int(container_slot), weapon_id)
                    for p in prev:
                        await conn.execute(
                            'UPDATE weapons SET container_id = 0, container_slot = 0 '
                            'WHERE id = $1', p['id'])
                        # asyncpg Record 不可变，须先转 dict 副本再改写
                        pd = dict(p)
                        pd['container_id'] = 0
                        pd['container_slot'] = 0
                        affected.append(pd)
                await conn.execute(
                    'UPDATE weapons SET container_id = $1, container_slot = $2 '
                    'WHERE id = $3 AND player_id = $4',
                    container_id, container_slot, weapon_id, player_id)
            # 返回最新状态（affected[0] 是更新前的旧值，须回读）
            ids = [a['id'] for a in affected]
            rows = await conn.fetch(
                'SELECT id, name, template_id, character_id, container_id, '
                'container_slot, variant_template_id, variant_id, condition, '
                'clip_count, auto_repair FROM weapons '
                'WHERE id = ANY($1::bigint[]) ORDER BY id', ids)
            return [dict(r) for r in rows]

    # ---- weapon modifiers ----

    async def list_weapon_modifiers(self, player_id, character_id=None):
        """返回 [{id, template_id, character_id, weapon_id, condition, auto_repair}]，按 id 顺序。
        character_id 非空时只返回该角色名下的配件。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, template_id, character_id, weapon_id, condition, auto_repair '
                'FROM weapon_modifiers WHERE player_id = $1 '
                'AND ($2::bigint IS NULL OR character_id = $2) ORDER BY id',
                player_id, character_id)
            return [dict(r) for r in rows]

    async def create_weapon_modifier(self, player_id, template_id, character_id, weapon_id):
        """创建武器配件实例，返回含新 id 的 dict。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO weapon_modifiers (player_id, template_id, character_id, weapon_id) '
                'VALUES ($1, $2, $3, $4) '
                'RETURNING id, template_id, character_id, weapon_id, condition',
                player_id, template_id, character_id, weapon_id)
            return dict(row)

    async def clear_weapon_modifier_category(self, player_id, weapon_id,
                                             template_ids, exclude_modifier_id):
        """卸下指定武器上与 exclude 同槽位（分类）的其他配件（互斥装备）。

        分类不在表内：调用方按 WarDatabase WeaponModifierTemplate.f28
        （weaponModCategoryId）算出同槽位的模板 id 列表传入。返回受影响行数。"""
        if not template_ids:
            return 0
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE weapon_modifiers SET weapon_id = 0 '
                'WHERE player_id = $1 AND weapon_id = $2 AND id != $3 '
                'AND template_id = ANY($4::bigint[])',
                int(player_id), int(weapon_id), int(exclude_modifier_id),
                [int(t) for t in template_ids])
            return _rowcount(n)

    # ---- weapon ammo ----

    async def list_weapon_ammo(self, player_id, character_id=None):
        """返回 [{id, ammo_template_id, character_id, weapon_id, slot_index, condition,
        auto_repair}]，按 id 顺序。character_id 非空时只返回该角色名下的弹药。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, ammo_template_id, character_id, weapon_id, slot_index, condition, '
                'auto_repair FROM weapon_ammo WHERE player_id = $1 '
                'AND ($2::bigint IS NULL OR character_id = $2) ORDER BY id',
                player_id, character_id)
            return [dict(r) for r in rows]

    async def create_weapon_ammo(self, player_id, ammo_template_id, character_id,
                                 weapon_id, slot_index=0, condition=0):
        """创建弹药实例（购买/默认弹药），返回含新 id 的 dict。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO weapon_ammo (player_id, ammo_template_id, character_id, weapon_id, slot_index, condition) '
                'VALUES ($1, $2, $3, $4, $5, $6) '
                'RETURNING id, ammo_template_id, character_id, weapon_id, slot_index, condition',
                player_id, ammo_template_id, character_id, weapon_id, slot_index, condition)
            return dict(row)

    async def update_weapon_ammo_slot(self, player_id, ammo_id, slot_index):
        """装备（slot>0）或卸下（slot=0）弹药。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE weapon_ammo SET slot_index = $1 '
                'WHERE id = $2 AND player_id = $3', slot_index, ammo_id, player_id)

    async def clear_weapon_ammo_slots(self, player_id, weapon_id, exclude_ammo_id):
        """清空指定武器上除 exclude_ammo_id 外所有弹药的槽位（互斥装备）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE weapon_ammo SET slot_index = 0 '
                'WHERE player_id = $1 AND weapon_id = $2 AND id != $3',
                player_id, weapon_id, exclude_ammo_id)

    async def update_weapon_ammo_condition(self, player_id, ammo_id, condition):
        """修复弹药：更新 condition（耐久）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE weapon_ammo SET condition = $1 '
                'WHERE id = $2 AND player_id = $3', condition, ammo_id, player_id)

    # ---- 战报 / 统计 / 生涯（U2 战斗协议，2026-08-21）----

    async def add_currency(self, player_id, credits=0, warfunds=0, gold=0):
        """结算加钱：credits/warfunds/gold 增量累加，返回新余额 dict。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'UPDATE players SET credits = credits + $2, warfunds = warfunds + $3, '
                'gold = gold + $4 WHERE id = $1 '
                'RETURNING credits, warfunds, gold',
                player_id, int(credits), int(warfunds), int(gold))
        if row is None:
            return None
        return {'credits': row['credits'], 'warfunds': row['warfunds'],
                'gold': row['gold']}

    # 货币列白名单（spend_currency 拼接列名前校验，防注入）
    _CURRENCY_KEYS = ('credits', 'warfunds', 'gold')

    async def spend_currency(self, player_id, currency_key, cost, conn=None):
        """原子扣款：事务内 FOR UPDATE 锁行，余额足够才扣减（增量扣，杜绝丢更新）。

        返回 (ok, 剩余余额)：余额不足/账号不存在 → (False, 当前余额)。
        传入 conn 时并入外部事务（外部事务内自动降级为 savepoint）。
        """
        key = str(currency_key).lower()
        if key not in self._CURRENCY_KEYS:
            return False, 0
        async with self._conn(conn) as c:
            async with c.transaction():
                balance = await c.fetchval(
                    f'SELECT {key} FROM players WHERE id = $1 FOR UPDATE',
                    int(player_id))
                if balance is None:
                    return False, 0
                if int(balance) < int(cost):
                    return False, int(balance)
                new_val = await c.fetchval(
                    f'UPDATE players SET {key} = {key} - $2 WHERE id = $1 '
                    f'RETURNING {key}', int(player_id), int(cost))
        return True, int(new_val)

    async def save_battle_report(self, report, players):
        """战报入库（battle_reports + battle_report_players）。

        report: {mission_session_id, mission_id, mission_preset_id,
                 winner_faction_id, defender_faction_id,
                 mission_time_seconds, battle_start_time,
                 capturepoints: [{id, owner}]（结算时据点终态，可缺省）,
                 factions_extra: {faction: {ats: [{name, tmpl}],
                                   destroyed: {cat: n}, captured: {cat: n}}}
        players: [{player_id, gamertag, squad_name, score, captures, kills,
                   deaths, is_bot, characters: [dict 任意结构存 JSONB]}]
        按 mission_session_id 幂等：同一局重复结算先删旧 players 再重建。
        players 为空时（解析失败/乱字节输入）DO NOTHING——绝不覆盖已有
        战报的表头与玩家行。
        返回 report_id；DB 异常向上抛由调用方记日志。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                conflict = ('ON CONFLICT (mission_session_id) DO UPDATE SET '
                            'mission_id = EXCLUDED.mission_id, '
                            'mission_preset_id = EXCLUDED.mission_preset_id, '
                            'winner_faction_id = EXCLUDED.winner_faction_id, '
                            'defender_faction_id = EXCLUDED.defender_faction_id, '
                            'mission_time_seconds = EXCLUDED.mission_time_seconds, '
                            'battle_start_time = EXCLUDED.battle_start_time, '
                            'capturepoints_json = EXCLUDED.capturepoints_json, '
                            'factions_json = EXCLUDED.factions_json, '
                            'created_at = now() ') if players else \
                           'ON CONFLICT (mission_session_id) DO NOTHING '
                rid = await conn.fetchval(
                    'INSERT INTO battle_reports (mission_session_id, mission_id, '
                    'mission_preset_id, winner_faction_id, defender_faction_id, '
                    'mission_time_seconds, battle_start_time, capturepoints_json, '
                    'factions_json) '
                    'VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb) '
                    + conflict +
                    'RETURNING id',
                    int(report['mission_session_id']), int(report.get('mission_id', 0)),
                    int(report.get('mission_preset_id', 0)),
                    int(report.get('winner_faction_id', 0)),
                    int(report.get('defender_faction_id', 0)),
                    int(report.get('mission_time_seconds', 0)),
                    int(report.get('battle_start_time', 0)),
                    json.dumps(report.get('capturepoints') or [],
                               ensure_ascii=False),
                    json.dumps(report.get('factions_extra') or {},
                               ensure_ascii=False))
                if rid is None:
                    # DO NOTHING 命中冲突：回查已存战报 id（供通知引用）
                    rid = await conn.fetchval(
                        'SELECT id FROM battle_reports WHERE mission_session_id = $1',
                        int(report['mission_session_id']))
                # 幂等重建：新结算带玩家才删旧重建——空玩家（解析失败/测试
                # 乱字节）命中旧行时直接跳过，绝不擦掉已有战报数据
                if players:
                    await conn.execute(
                        'DELETE FROM battle_report_players WHERE report_id = $1',
                        rid)
                    await conn.executemany(
                        'INSERT INTO battle_report_players '
                        '(report_id, player_id, gamertag, squad_name, score, captures, '
                        'kills, deaths, is_bot, characters_json) '
                        'VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)',
                        [(rid, int(p.get('player_id', 0)), p.get('gamertag', ''),
                          p.get('squad_name', ''), int(p.get('score', 0)),
                          int(p.get('captures', 0)), int(p.get('kills', 0)),
                          int(p.get('deaths', 0)), bool(p.get('is_bot', False)),
                          json.dumps(p.get('characters', []), ensure_ascii=False))
                         for p in players])
        return rid

    async def get_battle_report(self, report_id):
        """读取战报：返回 (report_row, players_rows) 或 None。"""
        async with self.pool.acquire() as conn:
            rep = await conn.fetchrow(
                'SELECT id, mission_session_id, mission_id, mission_preset_id, '
                'winner_faction_id, defender_faction_id, mission_time_seconds, '
                'battle_start_time, capturepoints_json, factions_json, '
                'created_at '
                'FROM battle_reports WHERE id = $1',
                report_id)
            if rep is None:
                return None
            players = await conn.fetch(
                'SELECT player_id, gamertag, squad_name, score, captures, kills, '
                'deaths, is_bot, characters_json FROM battle_report_players '
                'WHERE report_id = $1 ORDER BY score DESC', report_id)
        return rep, players

    async def list_recent_report_ids(self, player_id, limit=10):
        """玩家最近参战的战报 id 列表（新→旧）。

        排除 bot 行：bot playerId 可能与真实账号撞号（bot 3 ≠ 玩家 3），
        不排除会让真人凭空多出没参加过的人机局战报。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT r.id FROM battle_reports r '
                'JOIN battle_report_players p ON p.report_id = r.id '
                'WHERE p.player_id = $1 AND NOT p.is_bot '
                'ORDER BY r.created_at DESC, r.id DESC LIMIT $2',
                player_id, int(limit))
        return [int(r['id']) for r in rows]

    async def add_stat(self, character_id, stat_key, delta):
        """角色统计增量累加（character_stats，无行则建）。返回新值。"""
        async with self.pool.acquire() as conn:
            val = await conn.fetchval(
                'INSERT INTO character_stats (character_id, stat_key, value) '
                'VALUES ($1, $2, $3) '
                'ON CONFLICT (character_id, stat_key) '
                'DO UPDATE SET value = character_stats.value + EXCLUDED.value '
                'RETURNING value',
                character_id, stat_key, float(delta))
        return float(val or 0)

    async def set_stat(self, character_id, stat_key, value):
        """角色统计绝对写入（Last 类指标）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO character_stats (character_id, stat_key, value) '
                'VALUES ($1, $2, $3) '
                'ON CONFLICT (character_id, stat_key) '
                'DO UPDATE SET value = EXCLUDED.value',
                character_id, stat_key, float(value))

    async def get_stats(self, character_id):
        """返回该角色 {stat_key: value}。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT stat_key, value FROM character_stats WHERE character_id = $1',
                character_id)
        return {r['stat_key']: float(r['value']) for r in rows}

    async def upsert_career_order_progress(self, player_id, career_order_id, delta=1):
        """生涯军令进度累加（CareerOrderActionGameRequest 上报一次执行）。

        progress += delta；progress>=1 即 status=1（已完成可领取，领取逻辑 U3）。
        返回 (progress, status)。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO career_orders (player_id, career_order_id, progress) '
                'VALUES ($1, $2, $3) '
                'ON CONFLICT (player_id, career_order_id) DO UPDATE SET '
                'progress = career_orders.progress + EXCLUDED.progress, '
                'updated_at = now() '
                'RETURNING progress, status',
                player_id, int(career_order_id), int(delta))
        progress = int(row['progress'])
        status = int(row['status'])
        if progress >= 1 and status == 0:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    'UPDATE career_orders SET status = 1, updated_at = now() '
                    'WHERE player_id = $1 AND career_order_id = $2',
                    player_id, int(career_order_id))
            status = 1
        return progress, status

    async def list_career_orders(self, player_id):
        """玩家全部生涯军令进度：{career_order_id: (progress, status)}。

        status：0=进行中 1=已完成待领取 2=已领取。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT career_order_id, progress, status FROM career_orders '
                'WHERE player_id = $1', player_id)
        return {int(r['career_order_id']): (int(r['progress']), int(r['status']))
                for r in rows}

    async def claim_career_order(self, player_id, career_order_id):
        """领取生涯军令奖励：status 1→2。返回 (ok, progress)。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'UPDATE career_orders SET status = 2, updated_at = now() '
                'WHERE player_id = $1 AND career_order_id = $2 AND status = 1 '
                'RETURNING progress',
                player_id, int(career_order_id))
            if row is None:
                # fallback 查询必须在连接归还前执行（连接已回池会被其他协程借用）
                prog = await conn.fetchval(
                    'SELECT progress FROM career_orders WHERE player_id = $1 '
                    'AND career_order_id = $2', player_id, int(career_order_id))
                return False, int(prog or 0)
            return True, int(row['progress'])

    # ------------------------------------------------------------- battle pass

    async def get_battle_pass(self, player_id, season_id):
        """读取通行证进度行：{score, purchased, active, claimed, unlock} 或 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT xp, purchased, is_active, claimed_tiers, unlock_at '
                'FROM battle_pass_progress WHERE player_id = $1 AND season_id = $2',
                player_id, int(season_id))
        if row is None:
            return None
        return {
            'score': int(row['xp']),
            'purchased': bool(row['purchased']),
            'active': bool(row['is_active']),
            'claimed': json.loads(row['claimed_tiers'] or '[]'),
            'unlock': row['unlock_at'] or '',
        }

    async def add_battle_pass_score(self, player_id, season_id, delta):
        """通行证分数累加（结算时调用）。返回新分数。"""
        async with self.pool.acquire() as conn:
            val = await conn.fetchval(
                'INSERT INTO battle_pass_progress (player_id, season_id, xp) '
                'VALUES ($1, $2, $3) '
                'ON CONFLICT (player_id, season_id) '
                'DO UPDATE SET xp = battle_pass_progress.xp + EXCLUDED.xp '
                'RETURNING xp',
                player_id, int(season_id), int(delta))
        return int(val or 0)

    async def set_battle_pass_score(self, player_id, season_id, score):
        """通行证分数设值（买层级完成：提升到 requiredScore，只升不降）。

        xp = GREATEST(xp, score)，返回新分数。"""
        async with self.pool.acquire() as conn:
            val = await conn.fetchval(
                'INSERT INTO battle_pass_progress (player_id, season_id, xp) '
                'VALUES ($1, $2, $3) '
                'ON CONFLICT (player_id, season_id) '
                'DO UPDATE SET xp = GREATEST(battle_pass_progress.xp, '
                'EXCLUDED.xp) '
                'RETURNING xp',
                player_id, int(season_id), int(score))
        return int(val or 0)

    async def purchase_battle_pass(self, player_id, season_id, gold_cost=0):
        """购买通行证：扣金币 + purchased=true。返回 (ok, 剩余gold)。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                gold = await conn.fetchval(
                    'SELECT gold FROM players WHERE id = $1 FOR UPDATE', player_id)
                if gold is None:
                    return False, 0
                if int(gold) < int(gold_cost):
                    return False, int(gold)
                gold = await conn.fetchval(
                    'UPDATE players SET gold = gold - $2 WHERE id = $1 '
                    'RETURNING gold', player_id, int(gold_cost))
                await conn.execute(
                    'INSERT INTO battle_pass_progress '
                    '    (player_id, season_id, purchased, unlock_at) '
                    'VALUES ($1, $2, TRUE, $3) '
                    'ON CONFLICT (player_id, season_id) '
                    'DO UPDATE SET purchased = TRUE, unlock_at = $3',
                    player_id, int(season_id),
                    datetime.utcnow().strftime('%Y%m%d%H%M%SZ'))
        return True, int(gold)

    async def activate_battle_pass(self, player_id, season_id):
        """激活已购买的通行证（is_active=true，需先 purchased）。

        互斥激活：同一玩家同时只有一个激活赛季（客户端 activeOrNewest
        先取 isActive 行；多个同时为真时行为不确定），激活成功后把
        其他赛季的 is_active 清掉。返回是否激活成功。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    'INSERT INTO battle_pass_progress (player_id, season_id) '
                    'VALUES ($1, $2) '
                    'ON CONFLICT (player_id, season_id) DO UPDATE SET is_active = TRUE '
                    'WHERE battle_pass_progress.purchased '
                    'RETURNING is_active',
                    player_id, int(season_id))
                ok = bool(row['is_active']) if row else False
                if ok:
                    await conn.execute(
                        'UPDATE battle_pass_progress SET is_active = FALSE '
                        'WHERE player_id = $1 AND season_id <> $2',
                        player_id, int(season_id))
        return ok

    async def get_active_battle_pass_season(self, player_id):
        """玩家当前激活的通行证赛季 id（无激活行返回 None）。

        结算涨分路由用：激活哪个赛季就涨哪个（客户端"激活"切换语义），
        未激活任何赛季时由调用方回落到当前赛季。"""
        async with self.pool.acquire() as conn:
            val = await conn.fetchval(
                'SELECT season_id FROM battle_pass_progress '
                'WHERE player_id = $1 AND is_active AND purchased '
                'ORDER BY season_id DESC LIMIT 1', player_id)
        return int(val) if val is not None else None

    async def claim_battle_pass_reward(self, player_id, season_id, template_id):
        """领取通行证奖励模板：写 claimed_tiers（幂等，需已购买）。

        返回 (首次领取, 全部已领列表)。
        FOR UPDATE 锁行必须在事务内才有效（否则行锁随语句自动提交即释放）。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    'SELECT claimed_tiers FROM battle_pass_progress '
                    'WHERE player_id = $1 AND season_id = $2 AND purchased '
                    'FOR UPDATE', player_id, int(season_id))
                if row is None:
                    return False, []
                claimed = json.loads(row['claimed_tiers'] or '[]')
                first = int(template_id) not in claimed
                if first:
                    claimed.append(int(template_id))
                    await conn.execute(
                        'UPDATE battle_pass_progress SET claimed_tiers = $3::jsonb '
                        'WHERE player_id = $1 AND season_id = $2',
                        player_id, int(season_id), json.dumps(claimed))
        return first, claimed

    # ------------------------------------------------------------- 好友 / 屏蔽

    async def get_player_by_gamertag(self, gamertag):
        """按 gamertag（players.username）查玩家：{id, username} 或 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, username FROM players WHERE lower(username) = lower($1)',
                str(gamertag))
        if row is None:
            return None
        return {'id': int(row['id']), 'username': row['username']}

    async def get_username(self, player_id):
        """按 players.id 反查 username，无则返回 None。"""
        async with self.pool.acquire() as conn:
            val = await conn.fetchval(
                'SELECT username FROM players WHERE id = $1', int(player_id))
        return val

    async def get_player_faction(self, player_id):
        """玩家阵营：本场战争显式选择（player_war_factions）优先 → 名下 AT
        faction → 最新角色（AT/购买链路用；player f11 用
        get_player_war_faction 严格口径，未选择发 0）。

        战争模式阵营以 AT 阵营为准——玩家用其他阵营士兵打普通模式
        （characters 表 faction 变化）不应导致战争模式阵营漂移。
        2026-09-03 官服口径：本场战争的显式选择（player_war_factions，
        join_war_request 落库）最优先；未选择回落 AT/角色派生。"""
        pid = int(player_id)
        async with self.pool.acquire() as conn:
            war_id = await conn.fetchval(
                'SELECT id FROM wars WHERE state = 1 '
                'ORDER BY id DESC LIMIT 1')
            if war_id is not None:
                sel = await conn.fetchval(
                    'SELECT faction FROM player_war_factions '
                    'WHERE player_id = $1 AND war_id = $2', pid, int(war_id))
                if sel is not None:
                    return int(sel)
            val = await conn.fetchval(
                'SELECT faction FROM command_nodes '
                'WHERE owner_player_id = $1 AND state < 3 '
                'ORDER BY id DESC LIMIT 1', pid)
            if val is None:
                val = await conn.fetchval(
                    'SELECT faction FROM characters WHERE player_id = $1 '
                    'AND deleted = FALSE ORDER BY id DESC LIMIT 1', pid)
        return int(val or 0)

    async def get_player_war_faction(self, player_id, war_id):
        """本场战争的阵营选择（官服 join_war_request 落库行）。

        未选择返回 None（调用方据此把 player f11 发 0，客户端弹
        FactionSelectionWindow / 首页 CTA）。"""
        async with self.pool.acquire() as conn:
            val = await conn.fetchval(
                'SELECT faction FROM player_war_factions '
                'WHERE player_id = $1 AND war_id = $2',
                int(player_id), int(war_id))
        return int(val) if val is not None else None

    async def set_player_war_faction(self, player_id, war_id, faction):
        """记录/更新本场战争的阵营选择（UPSERT；同 war 重选覆盖）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO player_war_factions (player_id, war_id, faction) '
                'VALUES ($1, $2, $3) '
                'ON CONFLICT (player_id, war_id) DO UPDATE SET faction = $3',
                int(player_id), int(war_id), int(faction))
        return True

    async def add_friend(self, player_id, friend_id):
        """添加好友（单向请求行 status=0；对方也已请求则双向转正 status=1）。

        返回 'ok'（请求已发出）/ 'accepted'（直接成为好友）/ 'already' /
        'self'（不能加自己）。"""
        if int(player_id) == int(friend_id):
            return 'self'
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT 1 FROM friends WHERE player_id = $1 AND friend_id = $2 '
                'AND status = 1', int(player_id), int(friend_id))
            if row is not None:
                return 'already'
            reverse = await conn.fetchrow(
                'SELECT 1 FROM friends WHERE player_id = $2 AND friend_id = $1 '
                'AND status = 0', int(player_id), int(friend_id))
            # 无反向请求 → status=0（重复邀请幂等，不单方面转正）；
            # 有反向请求 → 本行直接置 1，与下方反向行 UPDATE 保持对称
            await conn.execute(
                'INSERT INTO friends (player_id, friend_id, status) '
                'VALUES ($1, $2, $3) ON CONFLICT (player_id, friend_id) '
                'DO UPDATE SET status = EXCLUDED.status',
                int(player_id), int(friend_id), 1 if reverse is not None else 0)
            if reverse is not None:
                await conn.execute(
                    'UPDATE friends SET status = 1 '
                    'WHERE player_id = $2 AND friend_id = $1',
                    int(player_id), int(friend_id))
                return 'accepted'
        return 'ok'

    async def remove_friend(self, player_id, friend_id):
        """删除好友（双向）。返回是否有行被删。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'DELETE FROM friends WHERE (player_id = $1 AND friend_id = $2) '
                'OR (player_id = $2 AND friend_id = $1)',
                int(player_id), int(friend_id))
        return _rowcount(n) > 0

    async def list_friend_rows(self, player_id):
        """好友行（friendinfo KVCS 推送源）：

        双向查询（我发起的 + 收到的），按对方玩家聚合：
        - 任一方向 status=1 → otherPlayerAlsoFriend=true（正式好友）
        - 存在 player_id=我的行 → friendshipInitiatedByMe=true
        收到的待处理邀请：initiated_by_me=false + also_friend=false，
        客户端据此显示在待确认列表。
        返回 [{id, gamertag, faction, also_friend, initiated_by_me}]。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT p.id AS pid, p.username, '
                '       (SELECT faction FROM characters c '
                '        WHERE c.player_id = p.id ORDER BY c.id DESC LIMIT 1) '
                '   AS faction, '
                '       BOOL_OR(f.status = 1) AS confirmed, '
                '       BOOL_OR(f.player_id = $1) AS i_initiated '
                'FROM friends f '
                'JOIN players p ON p.id = '
                '     CASE WHEN f.player_id = $1 THEN f.friend_id '
                '          ELSE f.player_id END '
                'WHERE f.player_id = $1 OR f.friend_id = $1 '
                'GROUP BY p.id, p.username', int(player_id))
        return [{
            'id': int(r['pid']),
            'gamertag': r['username'],
            'faction': int(r['faction'] or 0),
            'also_friend': bool(r['confirmed']),
            'initiated_by_me': bool(r['i_initiated']),
        } for r in rows]

    async def search_players(self, keyword, limit=20, exclude_id=None):
        """按用户名模糊搜索（排除自己）：[username]。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT username FROM players '
                'WHERE username ILIKE $1 AND id <> $2 '
                'ORDER BY username LIMIT $3',
                f'%{str(keyword)}%', int(exclude_id or 0), int(limit))
        return [r['username'] for r in rows]

    async def ignore_player(self, player_id, gamertag):
        """屏蔽玩家。返回 0=Ok 1=AlreadyIgnored 3=Fail（玩家不存在）。"""
        target = await self.get_player_by_gamertag(gamertag)
        if target is None:
            return 3
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT 1 FROM ignored_players WHERE player_id = $1 '
                'AND ignored_player_id = $2', int(player_id), target['id'])
            if row is not None:
                return 1
            await conn.execute(
                'INSERT INTO ignored_players (player_id, ignored_player_id) '
                'VALUES ($1, $2) ON CONFLICT DO NOTHING',
                int(player_id), target['id'])
        return 0

    async def unignore_player(self, player_id, gamertag):
        """取消屏蔽。返回 0=Ok 2=NotCurrentlyIgnored 3=Fail。"""
        target = await self.get_player_by_gamertag(gamertag)
        if target is None:
            return 3
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'DELETE FROM ignored_players WHERE player_id = $1 '
                'AND ignored_player_id = $2', int(player_id), target['id'])
        return 0 if _rowcount(n) > 0 else 2

    async def list_ignored_rows(self, player_id):
        """屏蔽名单（ignoredplayerwithname KVCS 推送源）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT ip.ignored_player_id, p.username '
                'FROM ignored_players ip JOIN players p '
                '  ON p.id = ip.ignored_player_id '
                'WHERE ip.player_id = $1', int(player_id))
        return [{'ignored_id': int(r['ignored_player_id']),
                 'ignoredname': r['username']} for r in rows]

    async def save_player_report(self, reporter_id, reported_player_id, category,
                                 action_game_session=0, chatlogs=''):
        """保存玩家举报（AddPlayerReportRequest）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO player_reports (reporter_id, reported_player_id, '
                'category, action_game_session, chatlogs) '
                'VALUES ($1, $2, $3, $4, $5)',
                int(reporter_id), int(reported_player_id), int(category),
                int(action_game_session), str(chatlogs or ''))

    # ------------------------------------------------------------- 邮件

    async def send_mail(self, sender_id, recipient_ids, subject, body, reply_to=0):
        """发送邮件（每个收件人一行）。返回 [message_id]。"""
        ids = []
        async with self.pool.acquire() as conn:
            for rid in recipient_ids:
                mid = await conn.fetchval(
                    'INSERT INTO mail_messages (sender_id, recipient_id, subject, '
                    'body, reply_to) VALUES ($1, $2, $3, $4, $5) RETURNING id',
                    int(sender_id or 0), int(rid), str(subject or ''),
                    str(body or ''), int(reply_to or 0))
                # 新话题：thread_id 指向首条消息；回复：继承原线程
                if int(reply_to or 0) == 0:
                    await conn.execute(
                        'UPDATE mail_messages SET thread_id = $1 WHERE id = $1', mid)
                else:
                    await conn.execute(
                        'UPDATE mail_messages SET thread_id = '
                        'COALESCE(NULLIF((SELECT thread_id FROM mail_messages '
                        'WHERE id = $2), 0), $2) WHERE id = $1',
                        mid, int(reply_to))
                ids.append(int(mid))
        return ids

    async def get_mail_threads(self, player_id, message_ids=None, limit=20):
        """收件箱线程组装（MessageThread KVCS 源）。

        返回 [{thread_id, messages: [{id, thread_id, reply_to, sender, subject,
        body, created_at, sender_name}], recipients: [player_id]}]。"""
        async with self.pool.acquire() as conn:
            if message_ids:
                rows = await conn.fetch(
                    'SELECT m.*, p.username AS sender_name FROM mail_messages m '
                    'LEFT JOIN players p ON p.id = m.sender_id '
                    'WHERE m.recipient_id = $1 AND m.id = ANY($2::bigint[]) '
                    'ORDER BY m.created_at DESC LIMIT $3',
                    int(player_id), [int(x) for x in message_ids], int(limit))
            else:
                rows = await conn.fetch(
                    'SELECT m.*, p.username AS sender_name FROM mail_messages m '
                    'LEFT JOIN players p ON p.id = m.sender_id '
                    'WHERE m.recipient_id = $1 AND NOT m.deleted '
                    'ORDER BY m.created_at DESC LIMIT $2',
                    int(player_id), int(limit))
        threads = {}
        for r in rows:
            tid = int(r['thread_id'] or r['id'])
            t = threads.setdefault(tid, {'thread_id': tid, 'messages': []})
            t['messages'].append({
                'id': int(r['id']),
                'thread_id': tid,
                'reply_to': int(r['reply_to'] or 0),
                'sender': int(r['sender_id'] or 0),
                'sender_name': r['sender_name'] or '',
                'subject': r['subject'],
                'body': r['body'],
                'created_at': r['created_at'],
                'is_read': bool(r['is_read']),
            })
        return list(threads.values())

    async def set_mail_flag(self, player_id, message_ids, column, value):
        """设置邮件标记（is_read / deleted），仅限本人邮件。返回更新行数。"""
        assert column in ('is_read', 'deleted')
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                f'UPDATE mail_messages SET {column} = $3 '
                'WHERE recipient_id = $1 AND id = ANY($2::bigint[])',
                int(player_id), [int(x) for x in message_ids], bool(value))
        return _rowcount(n)

    async def list_mail_recipient_rows(self, player_id, limit=200):
        """收件箱行（客户端 MessageRecipient KVCS 表的数据源）。

        本库一封邮件一行（send_mail 按收件人各插一行），因此
        MessageRecipient.id = messageId = mail_messages.id；
        客户端读写/删除标记请求回写的也是这组 id（set_mail_flag）。"""
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                'SELECT id, is_read, deleted FROM mail_messages '
                'WHERE recipient_id = $1 ORDER BY id DESC LIMIT $2',
                int(player_id), int(limit))

    async def get_mail_rows(self, player_id, message_ids):
        """按 id 取本人邮件行（标记回推用），返回与 list_mail_recipient_rows
        同构的行（id/is_read/deleted）。"""
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                'SELECT id, is_read, deleted FROM mail_messages '
                'WHERE recipient_id = $1 AND id = ANY($2::bigint[]) '
                'ORDER BY id',
                int(player_id), [int(x) for x in message_ids])

    # ------------------------------------------------------------- 聊天频道

    async def create_channel(self, name, owner_id, tab='general', password='',
                             hidden=False, invitation_only=False):
        """创建频道并让创建者加入。返回 (channel_id, ok)。"""
        async with self.pool.acquire() as conn:
            try:
                cid = await conn.fetchval(
                    'INSERT INTO chat_channels (name, owner_id, display_name, '
                    'channel_tab, password, hidden, invitation_only) '
                    "VALUES ($1, $2, '', $3, $4, $5, $6) RETURNING id",
                    str(name), int(owner_id or 0), str(tab or 'general'),
                    str(password or ''), bool(hidden), bool(invitation_only))
            except Exception:
                return 0, False              # name UNIQUE 冲突 → 已存在
            await conn.execute(
                'INSERT INTO chat_channel_members (channel_id, player_id) '
                'VALUES ($1, $2) ON CONFLICT DO NOTHING', cid, int(owner_id))
        return int(cid), True

    async def join_channel(self, player_id, channel_id=None, channel_name=None):
        """加入频道。返回 (ok, code)：code 0=ok 9=channel_already_added
        12=channel_not_found。"""
        async with self.pool.acquire() as conn:
            if channel_id is None:
                row = await conn.fetchrow(
                    'SELECT id FROM chat_channels WHERE lower(name) = lower($1)',
                    str(channel_name or ''))
                if row is None:
                    return False, 12
                channel_id = row['id']
            else:
                row = await conn.fetchrow(
                    'SELECT id FROM chat_channels WHERE id = $1', int(channel_id))
                if row is None:
                    return False, 12
            dup = await conn.fetchrow(
                'SELECT 1 FROM chat_channel_members WHERE channel_id = $1 '
                'AND player_id = $2', int(channel_id), int(player_id))
            if dup is not None:
                return False, 9
            await conn.execute(
                'INSERT INTO chat_channel_members (channel_id, player_id) '
                'VALUES ($1, $2) ON CONFLICT DO NOTHING',
                int(channel_id), int(player_id))
        return True, 0

    async def leave_channel(self, player_id, channel_id):
        """退出频道。返回是否退成。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'DELETE FROM chat_channel_members WHERE channel_id = $1 '
                'AND player_id = $2', int(channel_id), int(player_id))
        return _rowcount(n) > 0

    async def update_channel(self, channel_id, owner_id=None, new_name=None,
                             password=None, invitation_only=None, hidden=None):
        """编辑频道（改名/密码/邀请制/隐藏），owner 校验可选。返回是否更新。"""
        sets, args = [], [int(channel_id)]
        if new_name is not None:
            args.append(str(new_name))
            sets.append(f'name = ${len(args)}')
        if password is not None:
            args.append(str(password))
            sets.append(f'password = ${len(args)}')
        if invitation_only is not None:
            args.append(bool(invitation_only))
            sets.append(f'invitation_only = ${len(args)}')
        if hidden is not None:
            args.append(bool(hidden))
            sets.append(f'hidden = ${len(args)}')
        if not sets:
            return False
        cond = ''
        if owner_id is not None:
            args.append(int(owner_id))
            cond = f' AND owner_id = ${len(args)}'
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                f'UPDATE chat_channels SET {", ".join(sets)} '
                f'WHERE id = $1{cond}', *args)
        return _rowcount(n) > 0

    async def list_subscribed_channels(self, player_id):
        """玩家已订阅频道（GetChatChannelsSubscribedResponse 源）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT c.id, c.name, c.display_name, c.channel_tab '
                'FROM chat_channel_members m JOIN chat_channels c '
                '  ON c.id = m.channel_id WHERE m.player_id = $1 '
                'ORDER BY c.id', int(player_id))
        return [{'id': int(r['id']), 'name': r['name'],
                 'display': r['display_name'] or r['name'],
                 'tab': r['channel_tab']} for r in rows]

    # ------------------------------------------------------------- 任务记录

    async def create_mission_record(self, preset, attacker_faction=0,
                                    defender_faction=0, owner_player_id=0,
                                    name='', map_entity_id=0, battle_id=0):
        """CreateMissionRequest / war 会战成局落库（state=0 组队中）。
        battle_id>0 = war 会战局（M2）。返回 mission_id。"""
        async with self.pool.acquire() as conn:
            mid = await conn.fetchval(
                'INSERT INTO mission_records (name, preset, state, '
                'attacker_faction, defender_faction, owner_player_id, '
                'map_entity_id, battle_id) VALUES ($1, $2, 0, $3, $4, $5, '
                '$6, $7) RETURNING id',
                str(name or ''), int(preset or 0), int(attacker_faction or 0),
                int(defender_faction or 0), int(owner_player_id or 0),
                int(map_entity_id or 0), int(battle_id or 0))
        return int(mid)

    async def set_mission_state(self, mission_id, state):
        """更新任务状态：0 组队中 1 运行 2 结束 3 放弃。返回是否更新。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE mission_records SET state = $2 WHERE id = $1',
                int(mission_id), int(state))
        return _rowcount(n) > 0

    async def close_stale_missions(self):
        """服务器启动清理：所有 state IN (0,1) 的任务置为 state=2（结束）。

        重启后内存态（COMBAT_INSTANCES / 匹配池）已丢失，DB 里的组队中/
        运行中记录不可能再推进——不清掉会被 GetMissionList 当活局展示
        （玩家点进去是空壳）。返回清理行数。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE mission_records SET state = 2 WHERE state IN (0, 1)')
        return _rowcount(n)

    async def list_missions_by_state(self, states, limit=50):
        """按状态查任务记录。"""
        if not states:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, name, preset, state, attacker_faction, '
                'defender_faction, owner_player_id, map_entity_id, battle_id '
                'FROM mission_records WHERE state = ANY($1::int[]) '
                'ORDER BY id DESC LIMIT $2', [int(s) for s in states],
                int(limit))
        return [dict(r) for r in rows]

    async def get_mission_record(self, mission_id):
        """单条任务记录或 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, name, preset, state, attacker_faction, '
                'defender_faction, owner_player_id, map_entity_id, battle_id '
                'FROM mission_records WHERE id = $1', int(mission_id))
        return dict(row) if row else None

    async def get_mission_by_battle(self, battle_id, states=(0, 1)):
        """按 war battle 查活任务记录（M2：撮合/automission 判定）。

        返回最新一条或 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, name, preset, state, attacker_faction, '
                'defender_faction, owner_player_id, map_entity_id, battle_id '
                'FROM mission_records WHERE battle_id = $1 '
                'AND state = ANY($2::int[]) ORDER BY id DESC LIMIT 1',
                int(battle_id), [int(s) for s in states])
        return dict(row) if row else None

    # ------------------------------------------------------------- 经济道具（U5）

    async def claim_daily_reward(self, player_id):
        """每日签到领取（RedeemDailyLoginRewardRequest）。

        当天首次领取 → daily_reward_redeemed_count+1、last_daily_reward_at=now()
        返回 (True, day, redeemed_count)；当天已领 → (True, 0, n) 幂等不发；
        账号不存在 → (False, 0, 0)。day 即客户端 dailyRewards.xml 的奖励日
        （连续计数，handler 侧做 365 天循环折算）。发放内容（货币/加成/会员）
        由 handler 按当日 day 执行——标记与计数在同一事务内（FOR UPDATE
        锁行防并发双领），发放失败不回滚（已计入的 day 只少发一次奖励，
        不会重复发）。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    'SELECT last_daily_reward_at::date AS d, '
                    'daily_reward_redeemed_count AS n FROM players '
                    'WHERE id = $1 FOR UPDATE', int(player_id))
                if row is None:
                    return False, 0, 0
                if row['d'] is not None:
                    today = await conn.fetchval('SELECT now()::date')
                    if row['d'] == today:
                        return True, 0, int(row['n'])
                n = int(row['n']) + 1
                await conn.execute(
                    'UPDATE players SET last_daily_reward_at = now(), '
                    'daily_reward_redeemed_count = $2 WHERE id = $1',
                    int(player_id), n)
        return True, n, n

    async def touch_daily_login(self, player_id):
        """登录即记连续天数（subscribeplayerview 推签到行前调用）：
        同日幂等；昨日已登录 → +1；断签 → 重置 1。返回连续天数。"""
        async with self.pool.acquire() as conn:
            return int(await conn.fetchval(
                'UPDATE players SET consecutive_day_count = CASE '
                'WHEN last_login_date = CURRENT_DATE '
                '  THEN consecutive_day_count '
                'WHEN last_login_date = CURRENT_DATE - 1 '
                '  THEN consecutive_day_count + 1 '
                'ELSE 1 END, last_login_date = CURRENT_DATE '
                'WHERE id = $1 RETURNING consecutive_day_count',
                int(player_id)) or 1)

    async def get_daily_metrics(self, player_id):
        """PlayerOfferMetrics KVCS 行源（签到窗口数据）。

        available=今天可领（last_daily_reward_at 非今天）；
        fagd=firstActionGameDate（YYYYMMDD，无首局回落注册日）。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT daily_reward_redeemed_count AS n, '
                'consecutive_day_count AS c, '
                "COALESCE(last_daily_reward_at::date <> now()::date, "
                "TRUE) AS available, "
                "to_char(COALESCE(first_action_game_date, created_at::date), "
                "'YYYYMMDD') AS fagd "
                'FROM players WHERE id = $1', int(player_id))
        return dict(row) if row else None

    async def mark_first_action_game(self, player_id):
        """首局日期（player f19 firstActionGameDate）：结算时调用，幂等只写一次。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE players SET first_action_game_date = '
                'COALESCE(first_action_game_date, now()::date) '
                'WHERE id = $1', int(player_id))

    async def redeem_voucher(self, player_id, key):
        """兑换码 v2（RedeemVoucherRequest）：校验 + 记 pending 流水，不立即发放。

        统一走弹窗流程（docs/voucher-redemption-design.md）：输码成功只记
        pending，客户端自动发 QueryVoucherPacksRequest{0} → 264 弹士兵选择 →
        玩家选定后 Query{charId} 才真正发货（voucher_claim）。
        返回 code：1 ok（含 pending 幂等重入）/ 165 invalid_key /
        168 already_used（本玩家已兑或总量耗尽）/ 280 expired。

        FOR UPDATE 锁包行防并发；已用次数 = granted 流水数 + legacy redeemed
        （旧流程直接置位无流水，兼容存量一码一用语义）。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    'SELECT id, redeemed, expires_at, max_redemptions '
                    'FROM voucher_packs WHERE lower(pack_name) = lower($1) '
                    'FOR UPDATE', str(key or ''))
                if row is None:
                    return 165
                if row['expires_at'] is not None:
                    # 过期时间在锁行后取库端 now() 判定，避免本地时钟偏差
                    expired = await conn.fetchval(
                        'SELECT $1 <= now()', row['expires_at'])
                    if expired:
                        return 280
                granted_n = int(await conn.fetchval(
                    'SELECT count(*) FROM voucher_redemptions '
                    'WHERE voucher_pack_id = $1 AND status = 2', row['id']))
                used = granted_n + (1 if row['redeemed'] else 0)
                max_n = int(row['max_redemptions'] or 0)
                mine = await conn.fetchrow(
                    'SELECT status FROM voucher_redemptions '
                    'WHERE voucher_pack_id = $1 AND player_id = $2',
                    row['id'], int(player_id))
                if mine is not None:
                    # 本玩家已有流水：granted=已兑过 168；pending=幂等重入 1
                    return 168 if mine['status'] == 2 else 1
                if max_n > 0 and used >= max_n:
                    return 168
                await conn.execute(
                    'INSERT INTO voucher_redemptions (voucher_pack_id, player_id) '
                    'VALUES ($1, $2)', row['id'], int(player_id))
                return 1

    async def voucher_pending_packs(self, player_id):
        """待选士兵的兑换包（QueryVoucherPacksRequest characterId=0 → 264）。

        返回 [{pack_name, note, items}]，items 为 items_json 解析结果。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT v.pack_name, v.note, v.items_json '
                'FROM voucher_redemptions r '
                'JOIN voucher_packs v ON v.id = r.voucher_pack_id '
                'WHERE r.player_id = $1 AND r.status = 1 ORDER BY r.id',
                int(player_id))
        packs = []
        for r in rows:
            try:
                items = json.loads(r['items_json'] or '[]')
            except Exception:
                items = []
            packs.append({'pack_name': r['pack_name'], 'note': r['note'],
                          'items': items})
        return packs

    async def voucher_claim(self, player_id, character_id):
        """选士兵后确认发货（QueryVoucherPacksRequest characterId>0）。

        pending → granted（记录所选角色）；限量码在锁行后重检总量，
        已耗尽的 pending 行置 status=3 跳过（宁缺毋错，不重复发放）。
        返回本次确认发放的包列表 [{pack_name, note, items}]，
        实际物品发放由调用方（shop.py _deliver_voucher_items）执行——
        先标记后发放：发放中途失败不会导致重试时二次发放。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                pend = await conn.fetch(
                    'SELECT r.id AS rid, v.id AS vid, v.pack_name, v.note, '
                    'v.items_json, v.max_redemptions '
                    'FROM voucher_redemptions r '
                    'JOIN voucher_packs v ON v.id = r.voucher_pack_id '
                    'WHERE r.player_id = $1 AND r.status = 1 '
                    'ORDER BY r.id FOR UPDATE OF r, v', int(player_id))
                packs = []
                for r in pend:
                    # 锁住包行后重检：并发 claim 同一限量码时，后到者在此
                    # 读到先到者已提交的 granted 计数（READ COMMITTED 每条
                    # 语句取新快照）
                    used = int(await conn.fetchval(
                        'SELECT count(*) FROM voucher_redemptions '
                        'WHERE voucher_pack_id = $1 AND status = 2',
                        r['vid']))
                    max_n = int(r['max_redemptions'] or 0)
                    if max_n > 0 and used >= max_n:
                        # 输码与发货之间被其他玩家抢先兑完（限量码竞态）
                        await conn.execute(
                            'UPDATE voucher_redemptions SET status = 3 '
                            'WHERE id = $1', r['rid'])
                        continue
                    await conn.execute(
                        'UPDATE voucher_redemptions SET status = 2, '
                        'character_id = $2, granted_at = now() WHERE id = $1',
                        r['rid'], int(character_id or 0))
                    try:
                        items = json.loads(r['items_json'] or '[]')
                    except Exception:
                        items = []
                    packs.append({'pack_name': r['pack_name'],
                                  'note': r['note'], 'items': items})
                return packs

    async def add_warbond(self, player_id, template_id, conn=None):
        """购买战争债券（AddWarBondRequest）。返回债券条目 id。
        传入 conn 时并入外部事务（与扣款同生共死）。"""
        async with self._conn(conn) as c:
            wid = await c.fetchval(
                'INSERT INTO player_warbonds (player_id, warbond_item_id, count) '
                'VALUES ($1, $2, 1) ON CONFLICT (player_id, warbond_item_id) '
                'DO UPDATE SET count = player_warbonds.count + 1 '
                'RETURNING warbond_item_id', int(player_id), int(template_id))
        return int(wid)

    async def list_warbonds(self, player_id):
        """玩家战争债券（UpdateWarBondsResponse 源）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT warbond_item_id, count FROM player_warbonds '
                'WHERE player_id = $1 ORDER BY warbond_item_id', int(player_id))
        return [{'template_id': int(r['warbond_item_id']),
                 'count': int(r['count'])} for r in rows]

    async def set_first_gold_offer_shown(self, player_id):
        """标记首金优惠已展示（AddFirstGoldOfferRequest）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE players SET first_gold_offer_shown = TRUE WHERE id = $1',
                int(player_id))

    async def add_membership(self, player_id, template_id, duration_days=30,
                             conn=None):
        """购买会员（AddMembershipRequest）。返回插入行 {id, expires_at}。
        传入 conn 时并入外部事务（与扣款同生共死）。

        官方排队语义（客户端 PlayerModel：isCurrentMembership = expireDate
        非空，getCurrentMembership 只认第一行判 isPremium，最终到期 =
        当前行到期 + Σ排队行 durationDays）：已有进行中的会员
        （expires_at > now()）→ 插排队行 expires_at=NULL，前一份到期后由
        sweep_memberships 激活；没有 → 立即起跑。EXISTS 判定并入同一条
        INSERT...SELECT，避免"先查后插"窗口。"""
        async with self._conn(conn) as c:
            row = await c.fetchrow(
                'INSERT INTO player_memberships (player_id, '
                'membership_template_id, started_at, expires_at) '
                'SELECT $1, $2, now(), CASE WHEN EXISTS ('
                '  SELECT 1 FROM player_memberships WHERE player_id = $1 '
                '  AND expires_at IS NOT NULL AND expires_at > now()) '
                'THEN NULL ELSE now() + make_interval(days => $3) END '
                'RETURNING id, expires_at',
                int(player_id), int(template_id), int(duration_days))
        return {'id': int(row['id']), 'expires_at': row['expires_at']}

    async def sweep_memberships(self, player_id, days_by_template=None,
                                default_days=30):
        """会员行清扫+激活（登录回放/购买后推送前调用）。

        1) 删已过期行——客户端 getCurrentMembership 取表中第一行（不校验
           是否已过期），残留过期行会把老兵判定永久卡成 false；
        2) 无进行中行时激活最老排队行（expires_at=NULL → now() ~
           now()+days，天数按 days_by_template[membership_template_id]）。
        返回 (删除行数, 激活行 {id, template_id, days} 或 None)。"""
        if days_by_template is None:
            days_by_template = {}
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                tag = await conn.execute(
                    'DELETE FROM player_memberships WHERE player_id = $1 '
                    'AND expires_at IS NOT NULL AND expires_at <= now()',
                    int(player_id))
                deleted = int(tag.split()[-1]) if tag else 0
                row = await conn.fetchrow(
                    'SELECT m.id, m.membership_template_id '
                    'FROM player_memberships m WHERE m.player_id = $1 '
                    'AND m.expires_at IS NULL AND NOT EXISTS ('
                    '  SELECT 1 FROM player_memberships r '
                    '  WHERE r.player_id = $1 AND r.expires_at IS NOT NULL '
                    '  AND r.expires_at > now()) '
                    'ORDER BY m.id LIMIT 1', int(player_id))
                if row is None:
                    return deleted, None
                days = int(days_by_template.get(
                    int(row['membership_template_id']), default_days))
                await conn.execute(
                    'UPDATE player_memberships SET started_at = now(), '
                    'expires_at = now() + make_interval(days => $2) '
                    'WHERE id = $1', int(row['id']), days)
                return deleted, {'id': int(row['id']),
                                 'template_id': int(row['membership_template_id']),
                                 'days': days}

    async def get_active_membership(self, player_id):
        """未过期的最新会员行或 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, membership_template_id AS template_id, '
                'started_at, expires_at '
                'FROM player_memberships WHERE player_id = $1 '
                'AND (expires_at IS NULL OR expires_at > now()) '
                'ORDER BY id DESC LIMIT 1', int(player_id))
        return dict(row) if row else None

    async def add_booster(self, player_id, template_id, duration_minutes=240,
                          activate_now=False, conn=None, is_ribbon=False):
        """购买加成（AddBooster/AddVariousBoosterRequest）。返回 booster 行 id。
        传入 conn 时并入外部事务（与扣款同生共死）。
        is_ribbon：勋带加成（AddBoosterRequest）为 True，通用加成
        （AddVariousBoosterRequest）为 False —— 两张模板表 id 有重叠，
        推送 KVCS 时靠该列决定进 RibbonBooster 还是 Booster 表。"""
        async with self._conn(conn) as c:
            if activate_now:
                bid = await c.fetchval(
                    'INSERT INTO player_boosters (player_id, booster_template_id, '
                    'activated_at, expires_at, is_ribbon) VALUES ($1, $2, now(), '
                    'now() + make_interval(mins => $3), $4) RETURNING id',
                    int(player_id), int(template_id), int(duration_minutes),
                    bool(is_ribbon))
            else:
                bid = await c.fetchval(
                    'INSERT INTO player_boosters (player_id, booster_template_id, '
                    'is_ribbon) VALUES ($1, $2, $3) RETURNING id',
                    int(player_id), int(template_id), bool(is_ribbon))
        return int(bid)

    async def activate_booster(self, player_id, booster_row_id, duration_minutes=240):
        """激活已购加成（ActivateBooster/ActivateVariousBoosterRequest）。
        返回 ResponseCodes：1 ok / 208 booster_not_found / 209 already_activated。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, activated_at FROM player_boosters '
                'WHERE id = $1 AND player_id = $2',
                int(booster_row_id), int(player_id))
            if row is None:
                return 208
            if row['activated_at'] is not None:
                return 209
            await conn.execute(
                'UPDATE player_boosters SET activated_at = now(), '
                'expires_at = now() + make_interval(mins => $2) WHERE id = $1',
                int(booster_row_id), int(duration_minutes))
        return 1

    async def add_ticket(self, player_id, template_id, is_timed=False,
                         duration_hours=72, conn=None):
        """购买门票（AddTicketRequest）。返回 ticket 行 id。
        传入 conn 时并入外部事务（与扣款同生共死）。"""
        async with self._conn(conn) as c:
            if is_timed:
                tid = await c.fetchval(
                    'INSERT INTO player_tickets (player_id, ticket_template_id, '
                    'expires_at) VALUES ($1, $2, '
                    'now() + make_interval(hours => $3)) RETURNING id',
                    int(player_id), int(template_id), int(duration_hours))
            else:
                tid = await c.fetchval(
                    'INSERT INTO player_tickets (player_id, ticket_template_id) '
                    'VALUES ($1, $2) RETURNING id',
                    int(player_id), int(template_id))
        return int(tid)

    async def activate_ticket(self, player_id, ticket_row_id, duration_hours=72):
        """激活限时门票（ActivateTimedTicketRequest）。
        返回 1 ok / 208 not found / 209 already activated。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, expires_at FROM player_tickets '
                'WHERE id = $1 AND player_id = $2',
                int(ticket_row_id), int(player_id))
            if row is None:
                return 208
            if row['expires_at'] is not None:
                return 209
            await conn.execute(
                'UPDATE player_tickets SET expires_at = '
                'now() + make_interval(hours => $2) WHERE id = $1',
                int(ticket_row_id), int(duration_hours))
        return 1

    async def list_boosters(self, player_id):
        """玩家加成行（Booster/RibbonBooster KVCS 源）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, booster_template_id, activated_at, expires_at, '
                'is_ribbon FROM player_boosters WHERE player_id = $1 '
                'ORDER BY id',
                int(player_id))
        return [{'id': int(r['id']),
                 'template_id': int(r['booster_template_id']),
                 'activated_at': r['activated_at'],
                 'expires_at': r['expires_at'],
                 'is_ribbon': bool(r['is_ribbon'])} for r in rows]

    async def list_active_boosters(self, player_id, ribbon_only=False):
        """激活中的加成行（activated_at 非空且未过期；未激活/已过期不发）。

        战斗 PlayerSettings charactersetting f5 ribbonBoosters 的数据源；
        ribbon_only=True 只取勋带加成（is_ribbon）。"""
        sql = ('SELECT id, booster_template_id, is_ribbon FROM player_boosters '
               'WHERE player_id = $1 AND activated_at IS NOT NULL '
               'AND expires_at > now()')
        if ribbon_only:
            sql += ' AND is_ribbon'
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql + ' ORDER BY id', int(player_id))
        return [{'id': int(r['id']),
                 'template_id': int(r['booster_template_id']),
                 'is_ribbon': bool(r['is_ribbon'])} for r in rows]

    async def list_tickets(self, player_id, timed_only=False):
        """玩家门票行（Ticket/TicketTimed KVCS 源）。"""
        if timed_only:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    'SELECT id, ticket_template_id, expires_at FROM player_tickets '
                    'WHERE player_id = $1 AND expires_at IS NOT NULL ORDER BY id',
                    int(player_id))
        else:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    'SELECT id, ticket_template_id, expires_at FROM player_tickets '
                    'WHERE player_id = $1 ORDER BY id', int(player_id))
        return [{'id': int(r['id']),
                 'template_id': int(r['ticket_template_id']),
                 'expires_at': r['expires_at']} for r in rows]

    async def list_memberships(self, player_id):
        """玩家会员行（PlayerMembershipItem KVCS 源）。

        进行中行（expires_at 非空，过期行已由 sweep_memberships 删除）
        在前、排队行（expires_at IS NULL）在后——客户端 getCurrentMembership
        取表中第一行判 isPremium，行序即推送序，必须 running 在最前。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, membership_template_id, started_at, expires_at '
                'FROM player_memberships WHERE player_id = $1 '
                'ORDER BY (expires_at IS NULL), id',
                int(player_id))
        return [{'id': int(r['id']),
                 'template_id': int(r['membership_template_id']),
                 'started_at': r['started_at'],
                 'expires_at': r['expires_at']} for r in rows]

    async def get_username(self, player_id):
        """按 id 查用户名，不存在返回 None。"""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                'SELECT username FROM players WHERE id = $1', int(player_id))

    async def get_latest_character(self, player_id):
        """玩家最新角色或 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, name, faction, template, combat_xp FROM characters '
                'WHERE player_id = $1 ORDER BY id DESC LIMIT 1', int(player_id))
        return dict(row) if row else None

    # ------------------------------------------------------------- 小队（U5）

    async def create_squad(self, char_id, faction, name, matchmaking_flag=False):
        """创建小队 + 队长成员会话（CreateSquadRequest）。返回 squad_id。"""
        async with self.pool.acquire() as conn:
            sid = await conn.fetchval(
                'INSERT INTO squads (name, faction, leader_character_id, '
                'matchmaking_flag) VALUES ($1, $2, $3, $4) RETURNING id',
                str(name or ''), int(faction or 0), int(char_id),
                bool(matchmaking_flag))
            await conn.execute(
                'INSERT INTO squad_members (squad_id, character_id, status) '
                'VALUES ($1, $2, 2)', int(sid), int(char_id))  # 2=InLobby
        return int(sid)

    async def update_squad_leader(self, squad_id, leader_char_id, faction):
        """队长换兵种/换角色（官服语义：小队类型跟随队长）。
        更新 squads.leader_character_id + faction。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE squads SET leader_character_id = $2, faction = $3 '
                'WHERE id = $1', int(squad_id), int(leader_char_id),
                int(faction))

    async def get_squad(self, squad_id):
        """小队行或 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, name, faction, leader_character_id, matchmaking_flag '
                'FROM squads WHERE id = $1', int(squad_id))
        return dict(row) if row else None

    async def get_squad_for_character(self, char_id):
        """角色所在小队或 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT s.id, s.name, s.faction, s.leader_character_id, '
                's.matchmaking_flag FROM squads s '
                'JOIN squad_members m ON m.squad_id = s.id '
                'WHERE m.character_id = $1', int(char_id))
        return dict(row) if row else None

    async def get_squad_for_player(self, player_id):
        """玩家（任一角色）所在小队或 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT s.id, s.name, s.faction, s.leader_character_id, '
                's.matchmaking_flag FROM squads s '
                'JOIN squad_members m ON m.squad_id = s.id '
                'JOIN characters c ON c.id = m.character_id '
                'WHERE c.player_id = $1', int(player_id))
        return dict(row) if row else None

    async def list_squad_members(self, squad_id):
        """小队成员（SquadMemberSession KVCS 源）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT m.id AS member_id, m.character_id, m.status, '
                'p.id AS player_id, p.username, c.template, c.combat_xp, '
                'c.name AS char_name, c.faction '
                'FROM squad_members m '
                'JOIN characters c ON c.id = m.character_id '
                'JOIN players p ON p.id = c.player_id '
                'WHERE m.squad_id = $1 ORDER BY m.id', int(squad_id))
        return [{'id': int(r['member_id']),
                 'character_id': int(r['character_id']),
                 'status': int(r['status']),
                 'player_id': int(r['player_id']),
                 'gamertag': r['username'],
                 'template': int(r['template']),
                 'combat_xp': int(r['combat_xp']),
                 'char_name': r['char_name'],
                 'faction': int(r['faction'])} for r in rows]

    async def set_player_selected_char(self, player_id, character_id):
        """持久化"当前选中士兵"（官服组队规则需要被邀者的选中角色）。

        客户端无独立选角消息，服务器从携带 characterId 的请求侧面感知
        （JoinMatchMaking / 邀请 / 接受邀请 / GetMissionList），写内存
        注册表 state.PLAYER_SELECTED_CHAR 时同步落库，重启后回放。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO player_selected_char (player_id, character_id, '
                'updated_at) VALUES ($1, $2, now()) '
                'ON CONFLICT (player_id) DO UPDATE SET '
                'character_id = $2, updated_at = now()',
                int(player_id), int(character_id))

    async def get_player_selected_char(self, player_id):
        """玩家持久化的选中士兵 id；无记录返回 0。"""
        async with self.pool.acquire() as conn:
            cid = await conn.fetchval(
                'SELECT character_id FROM player_selected_char '
                'WHERE player_id = $1', int(player_id))
        return int(cid) if cid else 0

    async def load_all_selected_chars(self):
        """全量回放 player_id → character_id（服务器启动时灌内存注册表）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT player_id, character_id FROM player_selected_char')
        return {int(r['player_id']): int(r['character_id']) for r in rows}

    async def add_squad_member(self, squad_id, char_id, status=1):
        """添加成员会话行（1=InvitationPending 2=InLobby 3=Ready 4=InCombat）。
        返回成员行 id。"""
        async with self.pool.acquire() as conn:
            mid = await conn.fetchval(
                'INSERT INTO squad_members (squad_id, character_id, status) '
                'VALUES ($1, $2, $3) ON CONFLICT (squad_id, character_id) '
                'DO UPDATE SET status = $3 RETURNING id',
                int(squad_id), int(char_id), int(status))
        return int(mid)

    async def get_squad_member(self, member_row_id):
        """按成员行 id 查（RemoveSquadMember/SquadRespondToInvite）。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, squad_id, character_id, status '
                'FROM squad_members WHERE id = $1', int(member_row_id))
        return dict(row) if row else None

    async def get_squad_member_by_char(self, squad_id, char_id):
        """按 (squad_id, character_id) 查（SetSquadMemberStatus）。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, squad_id, character_id, status '
                'FROM squad_members WHERE squad_id = $1 AND character_id = $2',
                int(squad_id), int(char_id))
        return dict(row) if row else None

    async def update_squad_member(self, member_row_id, character_id=None,
                                  status=None):
        """更新成员会话（接受邀请换角色/改状态）。返回是否更新。"""
        sets, args = [], [int(member_row_id)]
        if character_id is not None:
            args.append(int(character_id))
            sets.append(f'character_id = ${len(args)}')
        if status is not None:
            args.append(int(status))
            sets.append(f'status = ${len(args)}')
        if not sets:
            return False
        try:
            async with self.pool.acquire() as conn:
                n = await conn.execute(
                    f'UPDATE squad_members SET {", ".join(sets)} '
                    'WHERE id = $1', *args)
        except Exception:
            # character_id 换成同队已有角色 → PK 冲突，退化为仅改状态
            if status is None:
                return False
            async with self.pool.acquire() as conn:
                n = await conn.execute(
                    'UPDATE squad_members SET status = $2 WHERE id = $1',
                    int(member_row_id), int(status))
        return _rowcount(n) > 0

    async def remove_squad_member(self, member_row_id):
        """删除成员会话行。返回是否删除。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'DELETE FROM squad_members WHERE id = $1', int(member_row_id))
        return _rowcount(n) > 0

    async def dissolve_squad(self, squad_id):
        """解散小队（级联删成员行）。返回是否删除。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'DELETE FROM squads WHERE id = $1', int(squad_id))
        return _rowcount(n) > 0

    async def rename_squad(self, squad_id, name):
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE squads SET name = $2 WHERE id = $1',
                int(squad_id), str(name))
        return _rowcount(n) > 0

    async def set_squad_flag(self, squad_id, flag):
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE squads SET matchmaking_flag = $2 WHERE id = $1',
                int(squad_id), bool(flag))
        return _rowcount(n) > 0

    # ------------------------------------------------------------- 军团（U5）

    async def create_company(self, name_long, name_short, leader_player_id,
                             description=''):
        """创建军团（CreateCompanyRequest），创建者 role=2（队长）。返回 company_id。"""
        async with self.pool.acquire() as conn:
            cid = await conn.fetchval(
                'INSERT INTO companies (name, name_short, leader_player_id, '
                'description) VALUES ($1, $2, $3, $4) RETURNING id',
                str(name_long), str(name_short), int(leader_player_id),
                str(description or ''))
            await conn.execute(
                'INSERT INTO company_members (company_id, player_id, role) '
                'VALUES ($1, $2, 2)', int(cid), int(leader_player_id))
        return int(cid)

    async def company_name_exists(self, name_long):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT 1 FROM companies WHERE lower(name) = lower($1)',
                str(name_long))
        return row is not None

    async def get_company_for_player(self, player_id):
        """玩家所在军团 {id, name, leader_player_id, role} 或 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT c.id, c.name, c.leader_player_id, m.role '
                'FROM companies c JOIN company_members m ON m.company_id = c.id '
                'WHERE m.player_id = $1', int(player_id))
        return dict(row) if row else None

    async def add_company_member(self, company_id, player_id, role=0):
        async with self.pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO company_members (company_id, player_id, role) '
                'VALUES ($1, $2, $3) ON CONFLICT (company_id, player_id) '
                'DO NOTHING', int(company_id), int(player_id), int(role))

    async def remove_company_member(self, company_id, player_id):
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'DELETE FROM company_members WHERE company_id = $1 '
                'AND player_id = $2', int(company_id), int(player_id))
        return _rowcount(n) > 0

    async def invite_to_company(self, company_id, player_id, invited_by):
        """军团邀请。返回 (invitation_id, code)：1 ok / 174 already in company
        / 175 invitation pending。"""
        async with self.pool.acquire() as conn:
            member = await conn.fetchrow(
                'SELECT 1 FROM company_members WHERE company_id = $1 '
                'AND player_id = $2', int(company_id), int(player_id))
            if member is not None:
                return 0, 174
            pend = await conn.fetchrow(
                'SELECT id FROM company_invitations WHERE company_id = $1 '
                'AND player_id = $2', int(company_id), int(player_id))
            if pend is not None:
                return 0, 175
            iid = await conn.fetchval(
                'INSERT INTO company_invitations (company_id, player_id, '
                'invited_by) VALUES ($1, $2, $3) RETURNING id',
                int(company_id), int(player_id), int(invited_by))
        return int(iid), 1

    async def respond_company_invitation(self, invitation_id, accept):
        """接受/拒绝军团邀请。返回 (code, company_id)：
        1 ok / 185 invitation_not_found / 174 already in company。

        删邀请与入团在同一事务内（FOR UPDATE 锁邀请行防并发双兑）。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    'SELECT id, company_id, player_id FROM company_invitations '
                    'WHERE id = $1 FOR UPDATE', int(invitation_id))
                if row is None:
                    return 185, 0
                await conn.execute(
                    'DELETE FROM company_invitations WHERE id = $1', int(invitation_id))
                if accept:
                    in_co = await conn.fetchrow(
                        'SELECT 1 FROM company_members WHERE player_id = $1',
                        int(row['player_id']))
                    if in_co is not None:
                        return 174, 0
                    await conn.execute(
                        'INSERT INTO company_members (company_id, player_id, role) '
                        'VALUES ($1, $2, 0) ON CONFLICT DO NOTHING',
                        int(row['company_id']), int(row['player_id']))
        return 1, int(row['company_id'])

    async def leave_company(self, player_id):
        """退出军团；队长退出且仍有成员 → 移交队长，无成员 → 解散。
        返回 (code, company_id)：1 ok / 186 no company。

        退团与队长移交/解散在同一事务内（锁军团行防交错）。"""
        co = await self.get_company_for_player(player_id)
        if co is None:
            return 186, 0
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # 锁军团行：并发退团/解散时串行化移交与解散判定
                locked = await conn.fetchval(
                    'SELECT id FROM companies WHERE id = $1 FOR UPDATE',
                    int(co['id']))
                if locked is None:
                    return 186, 0
                await conn.execute(
                    'DELETE FROM company_members WHERE company_id = $1 '
                    'AND player_id = $2', int(co['id']), int(player_id))
                if int(co['leader_player_id']) == int(player_id):
                    nxt = await conn.fetchrow(
                        'SELECT player_id FROM company_members WHERE company_id = $1 '
                        'ORDER BY role DESC, player_id LIMIT 1', int(co['id']))
                    if nxt is None:
                        await conn.execute(
                            'DELETE FROM companies WHERE id = $1', int(co['id']))
                    else:
                        await conn.execute(
                            'UPDATE companies SET leader_player_id = $2 WHERE id = $1',
                            int(co['id']), int(nxt['player_id']))
        return 1, int(co['id'])

    async def create_insignia(self, player_id, parts):
        """军团徽章创建（CreateInsigniaRequest）。parts 为 InsigniaPart
        dict 列表（partType/partId/fieldId/scale/offset），存 JSONB。
        返回新行 id。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO player_insignias (player_id, parts) '
                'VALUES ($1, $2::jsonb) RETURNING id',
                int(player_id), json.dumps(parts or []))
            return int(row['id'])

    async def remove_insignia(self, player_id, insignia_id):
        """军团徽章删除（RemoveInsigniaRequest）。返回是否删除。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'DELETE FROM player_insignias WHERE id = $1 AND player_id = $2',
                int(insignia_id), int(player_id))
        return _rowcount(n) > 0

    # -------------------------------------------------- 角色/装备维护（U7）

    async def rename_character(self, player_id, character_id, new_name):
        """角色改名（RenameCharacterRequest）。返回是否更新。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE characters SET name = $3 WHERE id = $1 AND player_id = $2 '
                'AND NOT is_deleted',
                int(character_id), int(player_id), str(new_name or ''))
        return _rowcount(n) > 0

    async def soft_delete_character(self, player_id, character_id):
        """角色软删（DeleteCharacterRequest，character KVCS f14 markedDeleted）。
        最后一个可用角色不允许删除。返回 (ok, active_count)。

        软删与装备退仓（10+ 张表）在同一事务内；锁玩家行防并发删光全部角色。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # 锁玩家行：并发删除两个角色时串行化 count 检查，防止删到 0 个
                locked = await conn.fetchval(
                    'SELECT id FROM players WHERE id = $1 FOR UPDATE', int(player_id))
                if locked is None:
                    return False, 0
                active = await conn.fetchval(
                    'SELECT count(*) FROM characters WHERE player_id = $1 '
                    'AND NOT is_deleted', int(player_id))
                if active is None or int(active) <= 1:
                    return False, int(active or 0)
                n = await conn.execute(
                    'UPDATE characters SET is_deleted = TRUE WHERE id = $1 '
                    'AND player_id = $2 AND NOT is_deleted',
                    int(character_id), int(player_id))
                if _rowcount(n) == 0:
                    return False, int(active)
                # 角色装备全部退回仓库（character_id=0）
                await self._unequip_all_for_character(conn, int(character_id))
        return True, int(active) - 1

    async def _unequip_all_for_character(self, conn, character_id):
        """角色软删时把其名下装备行 character_id 清 0（退回仓库）。"""
        for tbl in ('weapons', 'vehicles', 'weapon_modifiers', 'weapon_ammo',
                    'weapon_variants', 'vehicle_upgrades', 'vehicle_ammo',
                    'vehicle_variants', 'uniform_variants', 'uniform_parts'):
            await conn.execute(
                f'UPDATE {tbl} SET character_id = 0 WHERE character_id = $1',
                int(character_id))
        await conn.execute(
            'UPDATE weapons SET container_id = 0, container_slot = 0 '
            'WHERE character_id = 0', )

    async def set_character_on_leave(self, player_id, character_id, on_leave):
        """角色休假状态（SetCharacterActiveStatusRequest，f24 isOnLeave）。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE characters SET is_on_leave = $3 WHERE id = $1 '
                'AND player_id = $2 AND NOT is_deleted',
                int(character_id), int(player_id), bool(on_leave))
        return _rowcount(n) > 0

    async def count_active_characters(self, player_id):
        """未删除角色数（SetCharacterActiveStatusResponse.soldierMax 同源口径）。"""
        async with self.pool.acquire() as conn:
            return int(await conn.fetchval(
                'SELECT count(*) FROM characters WHERE player_id = $1 '
                'AND NOT is_deleted', int(player_id)) or 0)

    async def change_character_template(self, player_id, character_id, new_template):
        """兵种切换（ChangeCharacterTemplateRequest）：template 换新、
        旧值写 previous_template（character KVCS f19）。返回旧模板或 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'UPDATE characters SET previous_template = template, template = $3 '
                'WHERE id = $1 AND player_id = $2 AND NOT is_deleted '
                'RETURNING previous_template',
                int(character_id), int(player_id), int(new_template))
        return int(row['previous_template']) if row else None

    async def swap_character_head(self, player_id, character_id, head_template_id):
        """换头（SwapHeadRequest，f22 headId；0 = 换回默认头）。返回是否更新。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE characters SET head_id = $3 WHERE id = $1 AND player_id = $2 '
                'AND NOT is_deleted',
                int(character_id), int(player_id), int(head_template_id))
        return _rowcount(n) > 0

    async def rename_weapon(self, player_id, weapon_id, new_name):
        """武器改名（RenameWeaponRequest）。返回是否更新。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE weapons SET name = $3 WHERE id = $1 AND player_id = $2',
                int(weapon_id), int(player_id), str(new_name or ''))
        return _rowcount(n) > 0

    async def set_weapon_tracer_effect(self, player_id, weapon_id, tracer_effect_id,
                                       character_id=0):
        """武器曳光弹效果（SetTracerEffectRequest，官方行模型）。
        tracer_effect_id=0：卸下该武器全部曳光弹行；非 0：绑定该行到武器
        （character_id 跟随武器）并置 is_equipped，同时清该武器其他行的装备位。
        weapons.tracer_effect_id 反规范化列同步维护（战斗配装兜底）。
        返回受影响行 [{id, tracer_template_id, character_id, weapon_id, is_equipped}]。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                affected = []
                if int(tracer_effect_id) == 0:
                    rows = await conn.fetch(
                        'UPDATE tracer_effects SET is_equipped = FALSE '
                        'WHERE weapon_id = $1 AND player_id = $2 AND is_equipped '
                        'RETURNING id, tracer_template_id, character_id, weapon_id, is_equipped',
                        int(weapon_id), int(player_id))
                    affected = [dict(r) for r in rows]
                    await conn.execute(
                        'UPDATE weapons SET tracer_effect_id = 0 '
                        'WHERE id = $1 AND player_id = $2',
                        int(weapon_id), int(player_id))
                    return affected
                # 目标行必须属于该玩家
                row = await conn.fetchrow(
                    'SELECT id, tracer_template_id, character_id, weapon_id, is_equipped '
                    'FROM tracer_effects WHERE id = $1 AND player_id = $2',
                    int(tracer_effect_id), int(player_id))
                if row is None:
                    return None
                # 清同武器其他行的装备位（武器同一时间只有一个曳光弹）
                others = await conn.fetch(
                    'UPDATE tracer_effects SET is_equipped = FALSE '
                    'WHERE weapon_id = $1 AND player_id = $2 AND is_equipped AND id <> $3 '
                    'RETURNING id, tracer_template_id, character_id, weapon_id, is_equipped',
                    int(weapon_id), int(player_id), int(tracer_effect_id))
                affected = [dict(r) for r in others]
                # 绑定目标行：weapon_id + character_id 跟随武器
                bound = await conn.fetchrow(
                    'UPDATE tracer_effects SET weapon_id = $3, character_id = $4, '
                    'is_equipped = TRUE WHERE id = $1 AND player_id = $2 '
                    'RETURNING id, tracer_template_id, character_id, weapon_id, is_equipped',
                    int(tracer_effect_id), int(player_id),
                    int(weapon_id), int(character_id))
                if bound is not None:
                    affected.append(dict(bound))
                await conn.execute(
                    'UPDATE weapons SET tracer_effect_id = $3 '
                    'WHERE id = $1 AND player_id = $2',
                    int(weapon_id), int(player_id), int(tracer_effect_id))
        return affected

    async def update_weapon_condition(self, player_id, weapon_id, condition):
        """修复武器耐久（RepairWeaponRequest）。返回是否更新。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE weapons SET condition = $3 WHERE id = $1 AND player_id = $2',
                int(weapon_id), int(player_id), float(condition))
        return _rowcount(n) > 0

    async def update_weapon_modifier_condition(self, player_id, modifier_id,
                                                condition):
        """修复武器配件耐久（RepairWeaponModifierRequest）。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE weapon_modifiers SET condition = $3 WHERE id = $1 '
                'AND player_id = $2',
                int(modifier_id), int(player_id), float(condition))
        return _rowcount(n) > 0

    async def update_vehicle_condition(self, player_id, vehicle_id, condition):
        """修复载具耐久（RepairVehicleRequest）。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE vehicles SET condition = $3 WHERE id = $1 AND player_id = $2',
                int(vehicle_id), int(player_id), float(condition))
        return _rowcount(n) > 0

    # itemType 字符串（SetInventoryItemToCharacterRequest f1）→ 表名
    _ITEM_TABLES = {
        'weapon': 'weapons', 'vehicle': 'vehicles',
        'uniformvariant': 'uniform_variants', 'uniformpart': 'uniform_parts',
        'weaponmodifier': 'weapon_modifiers', 'weaponammo': 'weapon_ammo',
        'weaponvariant': 'weapon_variants', 'vehicleammo': 'vehicle_ammo',
        'vehicleupgrade': 'vehicle_upgrades', 'vehiclevariant': 'vehicle_variants',
    }
    # ItemType 枚举（SendItemToDepotRequest/SendUpgradesToDepotRequest f1 int）→ 表名
    _ITEM_TYPE_ID_TABLES = {
        1: 'weapons', 2: 'vehicles', 3: 'uniform_parts', 4: 'uniform_variants',
        5: 'weapon_modifiers', 6: 'weapon_ammo', 7: 'weapon_variants',
        8: 'vehicle_upgrades', 9: 'vehicle_ammo', 10: 'vehicle_variants',
    }
    # 挂靠件（配件/皮肤/弹药/载具升级与弹药）指派角色时需同步清 0 的外键列。
    # 官方容器归属不变式：客户端 ModifyWeaponMediator 只认
    # characterId==角色 && (weaponId==本枪 || weaponId==0)，指派后仍挂在
    # 仓库枪上的配件在角色改枪窗口里永远不可见（幽灵配件脏数据）。
    _ITEM_DETACH_SETS = {
        'weapon_modifiers': ', weapon_id = 0',
        'weapon_variants': ', weapon_id = 0',
        'weapon_ammo': ', weapon_id = 0',
        'vehicle_upgrades': ', vehicle_id = 0, equipped_slot_id = 0',
        'vehicle_variants': ', vehicle_id = 0',
        'vehicle_ammo': ', vehicle_id = 0, equipped_storage_id = 0',
    }

    async def set_inventory_item_character(self, player_id, item_type, item_id,
                                           character_id):
        """装备/仓库物品指派到角色（SetInventoryItemToCharacterRequest，
        itemType 为小写字符串 'weapon'/'vehicle'/...）。返回是否更新。

        武器指派时配件/皮肤随行（character_id 跟随武器，否则 _char_unlock_bits_for
        的配件 mapping uid 挂在旧角色名下 → 新角色配件显示锁定 + "Ribbon required"，
        与 2026-08-27 弹药跟随同根因）；载具指派时升级/弹药/皮肤同理随行。"""
        tbl = self._ITEM_TABLES.get(str(item_type or '').strip().lower())
        if tbl is None:
            return False
        async with self.pool.acquire() as conn:
            if tbl == 'weapons':
                n = await conn.execute(
                    f'UPDATE {tbl} SET character_id = $3 WHERE id = $1 '
                    'AND player_id = $2',
                    int(item_id), int(player_id), int(character_id))
                # 弹药随武器指派（slot_index 保留：默认弹药 slot 1 自动恢复装备）
                await conn.execute(
                    'UPDATE weapon_ammo SET character_id = $3 WHERE weapon_id = $1 '
                    'AND player_id = $2',
                    int(item_id), int(player_id), int(character_id))
                # 配件/皮肤随武器指派（weapon_id 挂靠关系保留）
                await conn.execute(
                    'UPDATE weapon_modifiers SET character_id = $3 '
                    'WHERE weapon_id = $1 AND player_id = $2',
                    int(item_id), int(player_id), int(character_id))
                await conn.execute(
                    'UPDATE weapon_variants SET character_id = $3 '
                    'WHERE weapon_id = $1 AND player_id = $2',
                    int(item_id), int(player_id), int(character_id))
            elif tbl == 'vehicles':
                n = await conn.execute(
                    f'UPDATE {tbl} SET character_id = $3 WHERE id = $1 '
                    'AND player_id = $2',
                    int(item_id), int(player_id), int(character_id))
                # 升级/弹药/皮肤随载具指派（vehicle_id 挂靠关系与槽位保留）
                await conn.execute(
                    'UPDATE vehicle_upgrades SET character_id = $3 '
                    'WHERE vehicle_id = $1 AND player_id = $2',
                    int(item_id), int(player_id), int(character_id))
                await conn.execute(
                    'UPDATE vehicle_ammo SET character_id = $3 '
                    'WHERE vehicle_id = $1 AND player_id = $2',
                    int(item_id), int(player_id), int(character_id))
                await conn.execute(
                    'UPDATE vehicle_variants SET character_id = $3 '
                    'WHERE vehicle_id = $1 AND player_id = $2',
                    int(item_id), int(player_id), int(character_id))
            else:
                # 挂靠件指派角色 → 同步脱离原容器（weapon_id/vehicle_id 清 0），
                # 详见 _ITEM_DETACH_SETS 注释（官方容器归属不变式）
                detach = self._ITEM_DETACH_SETS.get(tbl, '')
                n = await conn.execute(
                    f'UPDATE {tbl} SET character_id = $3{detach} '
                    'WHERE id = $1 AND player_id = $2',
                    int(item_id), int(player_id), int(character_id))
                # 皮肤实例脱离武器后，引用它的武器行同步复位默认皮肤
                #（对齐 send_upgrades_to_depot 载具分支 variant_id 复位语义）
                if tbl == 'weapon_variants':
                    await conn.execute(
                        'UPDATE weapons SET variant_id = 0 WHERE variant_id = $1 '
                        'AND player_id = $2',
                        int(item_id), int(player_id))
        return _rowcount(n) > 0

    async def get_item_character(self, player_id, item_type, object_id):
        """查询物品当前所属角色 id（SendItemToDepot 幂等预检用，
        itemType 为 ItemType 枚举 int）。物品不存在返回 None，已在仓库返回 0。"""
        tbl = self._ITEM_TYPE_ID_TABLES.get(int(item_type or 0))
        if tbl is None:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f'SELECT character_id FROM {tbl} WHERE id = $1 AND player_id = $2',
                int(object_id), int(player_id))
            return row['character_id'] if row else None

    async def send_item_to_depot(self, player_id, item_type, object_id):
        """物品送回仓库（SendItemToDepotRequest，itemType 为 ItemType 枚举 int）：
        character_id 清 0；武器同时卸下容器槽，载具同时卸下装备槽
        （否则装备中的载具入库会留下"仓库里却占着角色槽位"的脏数据）。返回是否更新。"""
        tbl = self._ITEM_TYPE_ID_TABLES.get(int(item_type or 0))
        if tbl is None:
            return False
        async with self.pool.acquire() as conn:
            if tbl == 'weapons':
                n = await conn.execute(
                    'UPDATE weapons SET character_id = 0, container_id = 0, '
                    'container_slot = 0 WHERE id = $1 AND player_id = $2',
                    int(object_id), int(player_id))
                # 弹药随武器退仓：不清理会留下"武器在仓库、弹药仍挂在角色名下
                # 且装备中"的脏数据 → unlockBits 弹药回查查不到武器（不在角色
                # 名下），默认弹药 uid 加不上 → 客户端显示锁定 + "Ribbon
                # required"（2026-08-27 weapon 147/ammo 346 实锤）。slot_index
                # 保留，重新指派角色时默认弹药自动恢复装备槽。
                await conn.execute(
                    'UPDATE weapon_ammo SET character_id = 0 WHERE weapon_id = $1 '
                    'AND player_id = $2',
                    int(object_id), int(player_id))
                # 配件/皮肤随武器退仓（weapon_id 挂靠关系保留：客户端 isModded
                # 靠 WeaponModifierTable 按 weapon_id 索引，清 0 会显示"无配件"，
                # 且卸配件的 SendUpgradesToDepot 按钮也不再出现）
                await conn.execute(
                    'UPDATE weapon_modifiers SET character_id = 0 '
                    'WHERE weapon_id = $1 AND player_id = $2',
                    int(object_id), int(player_id))
                await conn.execute(
                    'UPDATE weapon_variants SET character_id = 0 '
                    'WHERE weapon_id = $1 AND player_id = $2',
                    int(object_id), int(player_id))
            elif tbl == 'vehicles':
                n = await conn.execute(
                    'UPDATE vehicles SET character_id = 0, equipped_in_slot = 0 '
                    'WHERE id = $1 AND player_id = $2',
                    int(object_id), int(player_id))
                # 升级/弹药/皮肤随载具退仓（vehicle_id 挂靠关系与槽位保留，
                # 语义同武器配件：客户端按 vehicle_id 判定 hasUpgradeEquipped）
                await conn.execute(
                    'UPDATE vehicle_upgrades SET character_id = 0 '
                    'WHERE vehicle_id = $1 AND player_id = $2',
                    int(object_id), int(player_id))
                await conn.execute(
                    'UPDATE vehicle_ammo SET character_id = 0 '
                    'WHERE vehicle_id = $1 AND player_id = $2',
                    int(object_id), int(player_id))
                await conn.execute(
                    'UPDATE vehicle_variants SET character_id = 0 '
                    'WHERE vehicle_id = $1 AND player_id = $2',
                    int(object_id), int(player_id))
            else:
                n = await conn.execute(
                    f'UPDATE {tbl} SET character_id = 0 WHERE id = $1 '
                    'AND player_id = $2',
                    int(object_id), int(player_id))
        return _rowcount(n) > 0

    async def send_upgrades_to_depot(self, player_id, item_type, object_id):
        """卸下容器上挂的升级件（SendUpgradesToDepotRequest）。

        客户端 ItemInfoOverlayMediator.showSendUpgradesToDepotButtonIfNeeded：
        物品已在仓库时，武器 isModded / 载具 hasUpgradeEquipped 或非默认皮肤
        → 显示"卸下升级件"按钮，请求的 itemType 是【容器类型】（1=weapon
        2=vehicle）、objectId 是【容器实例 id】：
        - 武器：weapon_modifiers.weapon_id 清 0（配件回仓库，character_id 同步清 0）；
        - 载具：vehicle_upgrades 卸下（vehicle_id/equipped_slot_id 清 0）、
          vehicles.variant_id 复位默认皮肤、vehicle_variants.character_id 清 0。
        返回受影响行数（0 = 容器上没有可卸下的升级件/皮肤）。"""
        if int(item_type or 0) == 1:
            async with self.pool.acquire() as conn:
                n = await conn.execute(
                    'UPDATE weapon_modifiers SET weapon_id = 0, character_id = 0 '
                    'WHERE weapon_id = $1 AND player_id = $2',
                    int(object_id), int(player_id))
            return _rowcount(n)
        if int(item_type or 0) == 2:
            async with self.pool.acquire() as conn:
                n = await conn.execute(
                    'UPDATE vehicle_upgrades SET vehicle_id = 0, '
                    'equipped_slot_id = 0, character_id = 0 '
                    'WHERE vehicle_id = $1 AND player_id = $2',
                    int(object_id), int(player_id))
                m = await conn.execute(
                    'UPDATE vehicles SET variant_id = 0 '
                    'WHERE id = $1 AND player_id = $2 AND variant_id != 0',
                    int(object_id), int(player_id))
                await conn.execute(
                    'UPDATE vehicle_variants SET character_id = 0 '
                    'WHERE vehicle_id = $1 AND player_id = $2',
                    int(object_id), int(player_id))
            return _rowcount(n) + _rowcount(m)
        return 0

    async def mark_depot_item_viewed(self, player_id, tab_name, item_id):
        """仓库物品已读（SetDepotItemViewedRequest）。
        UPDATE 带 item_type 条件（唯一索引 uq_depot_items 按
        (player_id, item_type, item_id)，不带会跨 tab 误标）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO depot_items (player_id, item_type, item_id, viewed) '
                'VALUES ($1, $2, $3, TRUE) '
                'ON CONFLICT (player_id, item_type, item_id) DO NOTHING',
                int(player_id), str(tab_name or ''), int(item_id))
            await conn.execute(
                'UPDATE depot_items SET viewed = TRUE WHERE player_id = $1 '
                'AND item_type = $2 AND item_id = $3',
                int(player_id), str(tab_name or ''), int(item_id))
        return True

    async def mark_depot_item_new(self, player_id, tab_name, item_id):
        """物品入仓标记"新"（SendItemToDepot 后调用；客户端 ItemInfoOverlay
        setInfo 对 isNewItem 的物品发 SetDepotItemViewed 消点）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO depot_items (player_id, item_type, item_id, viewed) '
                'VALUES ($1, $2, $3, FALSE) '
                'ON CONFLICT (player_id, item_type, item_id) '
                'DO UPDATE SET viewed = FALSE',
                int(player_id), str(tab_name or ''), int(item_id))
        return True

    async def list_depot_new_items(self, player_id):
        """未读仓库物品 → {tab_name: [item_id,...]}（登录回放 DepotNewItem 行用）。
        物品行已被物理删除（如武器实例清理）时对应条目自动失效，客户端
        checkIsItemNew 查不到物品即无红点，无需服务端清理。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT item_type, item_id FROM depot_items '
                'WHERE player_id = $1 AND viewed = FALSE ORDER BY id',
                int(player_id))
        out = {}
        for r in rows:
            out.setdefault(str(r['item_type']), []).append(int(r['item_id']))
        return out

    async def mark_tutorial_triggered(self, player_id, tutorial_template_id):
        """教程已触发（MarkTutorialTriggeredRequest，幂等）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO player_tutorials_triggered '
                '(player_id, tutorial_template_id) VALUES ($1, $2) '
                'ON CONFLICT DO NOTHING',
                int(player_id), int(tutorial_template_id))
        return True

    async def list_tutorials_triggered(self, player_id):
        """已触发教程模板 id 列表（Tutorial KVCS 行数据源）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT tutorial_template_id FROM player_tutorials_triggered '
                'WHERE player_id = $1', int(player_id))
        return sorted(int(r['tutorial_template_id']) for r in rows)

    async def mark_character_tutorial(self, character_id, tutorial_id,
                                      completed=True):
        """战斗内教程上报（combat TutorialRequest）→ character_tutorials。

        幂等：重复上报只补 completed（triggered 首报即置位）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO character_tutorials '
                '(character_id, tutorial_id, triggered, completed) '
                'VALUES ($1, $2, TRUE, $3) '
                'ON CONFLICT (character_id, tutorial_id) '
                'DO UPDATE SET completed = character_tutorials.completed OR $3',
                int(character_id), int(tutorial_id), bool(completed))
        return True

    async def set_player_language(self, player_id, language_code):
        """玩家语言（SetPlayerLanguageRequest）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE players SET language_code = $2 WHERE id = $1',
                int(player_id), str(language_code or 'en')[:8])
        return True

    async def set_player_discord_id(self, player_id, discord_id):
        """绑定 Discord id（SetPlayerDiscordIdRequest）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE players SET discord_id = $2 WHERE id = $1',
                int(player_id), str(discord_id or ''))
        return True

    async def set_player_steam_id(self, player_id, steam_id):
        """绑定 Steam id（SetPlayerSteamIdRequest）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE players SET steam_id = $2 WHERE id = $1',
                int(player_id), str(steam_id or ''))
        return True

    async def set_equipment_auto_repair(self, player_id, item_type, item_id,
                                        enabled):
        """装备自动维修开关（Set*AutoRepairRequest 族）。
        item_type 为 ItemType 枚举 int（1-10）；enabled 为三态 int
        （0=关 1=金币 2=金条）。返回是否更新。"""
        tbl = self._ITEM_TYPE_ID_TABLES.get(int(item_type or 0))
        if tbl is None:
            return False
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                f'UPDATE {tbl} SET auto_repair = $3 WHERE id = $1 AND player_id = $2',
                int(item_id), int(player_id), int(enabled or 0))
        return _rowcount(n) > 0

    async def set_weapon_modifier_weapon(self, player_id, modifier_id, weapon_id,
                                         character_id=None):
        """配件装备/卸下（SetWeaponModifierRequest）：
        装备 → weapon_id=武器实例 + character_id 同步为该武器归属角色；
        卸下（weapon_id=0）→ weapon_id=0。返回是否更新。"""
        async with self.pool.acquire() as conn:
            if weapon_id:
                n = await conn.execute(
                    'UPDATE weapon_modifiers SET weapon_id = $3 '
                    'WHERE id = $1 AND player_id = $2',
                    int(modifier_id), int(player_id), int(weapon_id))
                if character_id is not None:
                    await conn.execute(
                        'UPDATE weapon_modifiers SET character_id = $3 '
                        'WHERE id = $1 AND player_id = $2',
                        int(modifier_id), int(player_id), int(character_id))
            else:
                n = await conn.execute(
                    'UPDATE weapon_modifiers SET weapon_id = 0 '
                    'WHERE id = $1 AND player_id = $2',
                    int(modifier_id), int(player_id))
        return _rowcount(n) > 0

    async def get_character_any(self, player_id, character_id):
        """按 id 取单个角色行（含软删角色，DeleteCharacterRequest 后重推
        markedDeleted=1 用）。返回 dict 或 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, name, faction, template, combat_xp, combat_badge_0, '
                'combat_badge_1, uniform_variant_id, headgear_id, is_deleted, '
                'is_on_leave, previous_template, head_id '
                'FROM characters WHERE id = $1 AND player_id = $2',
                int(character_id), int(player_id))
        return dict(row) if row else None

    async def reset_player_progress(self, player_id):
        """重置玩家进度（ResetPlayerRequest）：删角色（连带装备表清空该玩家行）、
        勋带经验、统计、教程触发记录；保留账号与货币。返回删除的角色数。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                deleted = int(await conn.fetchval(
                    'SELECT count(*) FROM characters WHERE player_id = $1 '
                    'AND NOT is_deleted', int(player_id)) or 0)
                await conn.execute(
                    'DELETE FROM characters WHERE player_id = $1', int(player_id))
                for tbl in ('weapons', 'vehicles', 'weapon_modifiers',
                            'weapon_ammo', 'weapon_variants', 'vehicle_upgrades',
                            'vehicle_ammo', 'vehicle_variants', 'uniform_variants',
                            'uniform_parts', 'depot_items',
                            'player_tutorials_triggered'):
                    await conn.execute(
                        f'DELETE FROM {tbl} WHERE player_id = $1', int(player_id))
                await conn.execute(
                    'DELETE FROM squad_members WHERE character_id NOT IN '
                    '(SELECT id FROM characters)')
        return deleted

    # ================================================================
    # RTS 战争域（docs/hng-rts-protocol.md Phase 2；war_campaign.py 使用）
    # ================================================================

    _WAR_COLUMNS = ('id, name, begintime, endtime, winning_faction_id, '
                    'points_needed_for_victory, sequel_war_id, mapid, state, '
                    'config_json, production_ms')

    @staticmethod
    def _war_row(row):
        if row is None:
            return None
        d = dict(row)
        cfg = d.pop('config_json')
        if isinstance(cfg, (str, bytes)):
            # asyncpg 无 codec 时 JSONB 返回 str（2026-09-03 VPS 实锤：
            # build_war_proto 拿到 str 直接 AttributeError，登录卡死）
            try:
                cfg = json.loads(cfg) if cfg else {}
            except ValueError:
                cfg = {}
        d['config'] = cfg or {}
        return d

    async def war_count(self):
        """历史战争总数（wars 只增不删，count 即战争轮次；_next_war_name 用）。"""
        async with self.pool.acquire() as conn:
            return int(await conn.fetchval('SELECT count(*) FROM wars'))

    async def create_war(self, name, points_needed_for_victory=15, mapid=1,
                         config=None):
        """开一场新战争（war_campaign.ensure_war）。返回 war 行 dict。"""
        import json as _json
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO wars (name, points_needed_for_victory, mapid, '
                'config_json) VALUES ($1, $2, $3, $4) RETURNING ' + self._WAR_COLUMNS,
                str(name or ''), int(points_needed_for_victory or 15),
                int(mapid or 1), _json.dumps(config or {}))
        return self._war_row(row)

    async def get_war(self, war_id):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT ' + self._WAR_COLUMNS + ' FROM wars WHERE id = $1',
                int(war_id))
        return self._war_row(row)

    async def get_active_war(self):
        """当前 running 的战争（state=1）；无则 None。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT ' + self._WAR_COLUMNS +
                ' FROM wars WHERE state = 1 ORDER BY id DESC LIMIT 1')
        return self._war_row(row)

    async def list_wars(self, limit=20):
        """战争目录列表（新→旧，war_campaign.build_war_catalogue 用）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT ' + self._WAR_COLUMNS +
                ' FROM wars ORDER BY id DESC LIMIT $1', int(limit))
        return [self._war_row(r) for r in rows]

    async def end_war(self, war_id, winning_faction_id, sequel_war_id=0):
        """战争结算（state=2 + 胜方 + 续战 id）。返回是否更新。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE wars SET state = 2, endtime = now(), '
                'winning_faction_id = $2, sequel_war_id = $3 WHERE id = $1 '
                'AND state = 1',
                int(war_id), int(winning_faction_id or 0),
                int(sequel_war_id or 0))
        return _rowcount(n) > 0

    async def update_war_sequel(self, war_id, sequel_war_id):
        """回填续战 id（war_campaign.check_victory 续战后调用）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE wars SET sequel_war_id = $2 WHERE id = $1',
                int(war_id), int(sequel_war_id or 0))

    # ---- war_cities ----

    async def init_war_cities(self, war_id, owner_map):
        """批量写入城市归属 {battlefield_id: owner_faction}（开战时一次性）。"""
        if not owner_map:
            return
        async with self.pool.acquire() as conn:
            await conn.executemany(
                'INSERT INTO war_cities (war_id, battlefield_id, owner_faction) '
                'VALUES ($1, $2, $3) ON CONFLICT (war_id, battlefield_id) '
                'DO UPDATE SET owner_faction = EXCLUDED.owner_faction',
                [(int(war_id), int(bf), int(owner))
                 for bf, owner in owner_map.items()])

    async def get_war_cities(self, war_id):
        """[(battlefield_id, owner_faction), ...]。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT battlefield_id, owner_faction FROM war_cities '
                'WHERE war_id = $1 ORDER BY battlefield_id', int(war_id))
        return [(r['battlefield_id'], r['owner_faction']) for r in rows]

    async def set_city_owner(self, war_id, battlefield_id, owner_faction):
        async with self.pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO war_cities (war_id, battlefield_id, owner_faction) '
                'VALUES ($1, $2, $3) ON CONFLICT (war_id, battlefield_id) '
                'DO UPDATE SET owner_faction = $3',
                int(war_id), int(battlefield_id), int(owner_faction))

    async def count_war_city_owners(self, war_id, battlefield_ids=None):
        """各阵营城市占有数 {faction: count}（首都名单由 wardb.battlefields
        提供静态判定，war_campaign 层负责交集过滤）。"""
        wanted = (None if battlefield_ids is None
                  else set(int(b) for b in battlefield_ids))
        counts = {}
        for bf, owner in await self.get_war_cities(war_id):
            if owner > 0 and (wanted is None or bf in wanted):
                counts[owner] = counts.get(owner, 0) + 1
        return counts

    # ---- command_nodes（AT 实例）----

    async def create_command_node(self, war_id, template_id, owner_player_id,
                                  faction, name, resources=None,
                                  abbreviation='', rank=0,
                                  parent_command_node_id=0):
        """购买 AT → 新实例（state=0 首都待命）。返回 AT 行 dict。"""
        import json as _json
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO command_nodes (war_id, template_id, '
                'owner_player_id, faction, name, abbreviation, rank, '
                'resources, parent_command_node_id) '
                'VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) '
                'RETURNING *',
                int(war_id), int(template_id), int(owner_player_id),
                int(faction), str(name or ''), str(abbreviation or ''),
                int(rank or 0), _json.dumps(resources or {}),
                int(parent_command_node_id or 0))
        return dict(row) if row else None

    async def get_command_node(self, node_id):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT * FROM command_nodes WHERE id = $1', int(node_id))
        return dict(row) if row else None

    async def list_player_command_nodes(self, player_id, war_id=None):
        """玩家名下 AT 列表（war_id=None 取全部战争）。"""
        q = ('SELECT * FROM command_nodes WHERE owner_player_id = $1 '
             'AND state < 3')
        args = [int(player_id)]
        if war_id is not None:
            q += ' AND war_id = $2'
            args.append(int(war_id))
        q += ' ORDER BY id'
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q, *args)
        return [dict(r) for r in rows]

    async def list_war_command_nodes(self, war_id):
        """本战争全部存活 AT（含各玩家），warmapview 快照用。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT * FROM command_nodes WHERE war_id = $1 AND state < 3 '
                'ORDER BY id', int(war_id))
        return [dict(r) for r in rows]

    async def get_command_nodes_by_ids(self, node_ids):
        """按 id 集合查 AT（M2 会战兵力聚合：war_battles.factions_json）。"""
        ids = [int(i) for i in (node_ids or []) if int(i)]
        if not ids:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT * FROM command_nodes WHERE id = ANY($1::bigint[]) '
                'ORDER BY id', ids)
        return [dict(r) for r in rows]

    async def update_command_node(self, node_id, **fields):
        """AT 字段更新（name/rank/xp/battles/battles_won/resources/morale/
        state/map_entity_id/map_entity_type/general_character_id/is_deployed/
        abbreviation）。rank/xp/battles/battles_won 任一变更时自动刷新
        stats_updated_at（Definition f10 → 客户端重拉 GetAssaultTeamStats）。
        返回是否更新。"""
        allowed = {'name', 'abbreviation', 'rank', 'xp', 'battles',
                   'battles_won', 'resources', 'morale',
                   'max_morale', 'state', 'map_entity_id', 'map_entity_type',
                   'general_character_id', 'is_deployed', 'upgrade_level',
                   'owner_player_id', 'faction', 'template_id',
                   'has_gotten_soldier_free', 'has_gotten_vehicle_free',
                   'parent_command_node_id'}
        import json as _json
        sets, args = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == 'resources' and not isinstance(v, str):
                v = _json.dumps(v or {})
            args.append(v)
            sets.append(f'{k} = ${len(args)}')
        if not sets:
            return False
        if {'rank', 'xp', 'battles', 'battles_won'} & fields.keys():
            sets.append('stats_updated_at = now()')
        args.append(int(node_id))
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                f'UPDATE command_nodes SET {", ".join(sets)} '
                f'WHERE id = ${len(args)}', *args)
        return _rowcount(n) > 0

    async def delete_command_node(self, node_id):
        """解散 AT（state=3 标记，保留历史行）。返回是否更新。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE command_nodes SET state = 3 WHERE id = $1 AND state < 3',
                int(node_id))
        return _rowcount(n) > 0

    # ---- commandnode_movements ----

    async def create_movement(self, commandnode_id, war_id, path,
                              started_at=None, arrive_at=None, dest_ap=0):
        """AT 移动登记（path=段列表；dest_ap=目标出兵点，0=城市本体）。"""
        import json as _json
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO commandnode_movements (commandnode_id, war_id, '
                'path_json, started_at, arrive_at, dest_ap) '
                'VALUES ($1, $2, $3, $4, $5, $6) '
                'RETURNING id, commandnode_id, war_id, path_json, started_at, '
                'arrive_at, state, dest_ap',
                int(commandnode_id), int(war_id), _json.dumps(list(path or [])),
                started_at, arrive_at, int(dest_ap or 0))
        if row:
            d = dict(row)
            import json as _j
            d['path'] = _j.loads(d.pop('path_json') or '[]')
            return d
        return None

    async def get_active_movement(self, commandnode_id):
        """AT 当前进行中的移动（state=0）；无则 None。"""
        import json as _j
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, commandnode_id, war_id, path_json, started_at, '
                'arrive_at, state, dest_ap FROM commandnode_movements '
                'WHERE commandnode_id = $1 AND state = 0 '
                'ORDER BY id DESC LIMIT 1', int(commandnode_id))
        if not row:
            return None
        d = dict(row)
        d['path'] = _j.loads(d.pop('path_json') or '[]')
        return d

    async def list_active_movements(self, war_id):
        """本战争全部进行中移动（增量推进器轮询用）。"""
        import json as _j
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, commandnode_id, war_id, path_json, started_at, '
                'arrive_at, state, dest_ap FROM commandnode_movements '
                'WHERE war_id = $1 AND state = 0 ORDER BY id', int(war_id))
        out = []
        for r in rows:
            d = dict(r)
            d['path'] = _j.loads(d.pop('path_json') or '[]')
            out.append(d)
        return out

    async def finish_movement(self, movement_id, state=1):
        """移动结束（1=arrived 2=cancelled）。返回是否更新。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE commandnode_movements SET state = $2 '
                'WHERE id = $1 AND state = 0', int(movement_id), int(state))
        return _rowcount(n) > 0

    # ---- war_battles（M1 桩）----

    async def create_war_battle(self, war_id, battlefield_id, factions):
        """敌对 AT 相遇 → 会战登记（factions=[{at_id, faction}]）。返回行。"""
        import json as _json
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'INSERT INTO war_battles (war_id, battlefield_id, factions_json) '
                'VALUES ($1, $2, $3) RETURNING id, war_id, battlefield_id, '
                'factions_json, state, winning_faction, started_at, ended_at',
                int(war_id), int(battlefield_id),
                _json.dumps(list(factions or [])))
        if not row:
            return None
        d = dict(row)
        d['factions'] = _json.loads(d.pop('factions_json') or '[]')
        return d

    async def list_active_war_battles(self, war_id):
        import json as _j
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, war_id, battlefield_id, factions_json, state, '
                'winning_faction, started_at, ended_at FROM war_battles '
                'WHERE war_id = $1 AND state = 0 ORDER BY id', int(war_id))
        out = []
        for r in rows:
            d = dict(r)
            d['factions'] = _j.loads(d.pop('factions_json') or '[]')
            out.append(d)
        return out

    async def get_war_battle(self, battle_id):
        """单查会战（含已结束）；无则 None。"""
        import json as _j
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, war_id, battlefield_id, factions_json, state, '
                'winning_faction, started_at, ended_at FROM war_battles '
                'WHERE id = $1', int(battle_id))
        if not row:
            return None
        d = dict(row)
        d['factions'] = _j.loads(d.pop('factions_json') or '[]')
        return d

    async def end_war_battle(self, battle_id, winning_faction=0):
        """会战结束（state=1，M2：写入胜方阵营）。"""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                'UPDATE war_battles SET state = 1, ended_at = now(), '
                'winning_faction = $2 WHERE id = $1 AND state = 0',
                int(battle_id), int(winning_faction))
        return _rowcount(n) > 0

    # ---- warfunds 流水 ----

    async def add_war_funds(self, player_id, amount, reason=''):
        """战争资金变动（事务：余额 + 流水）。amount 可负；玩家不存在时
        事务回滚并抛 ValueError。返回新余额。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                balance = await conn.fetchval(
                    'UPDATE players SET warfunds = warfunds + $2 '
                    'WHERE id = $1 RETURNING warfunds',
                    int(player_id), int(amount))
                if balance is None:
                    raise ValueError(f'player {player_id} not found')
                await conn.execute(
                    'INSERT INTO war_funds_ledger (player_id, amount, reason, '
                    'balance_after) VALUES ($1, $2, $3, $4)',
                    int(player_id), int(amount), str(reason or ''),
                    int(balance))
        return balance

    # ---- war_faction_resources（M3 阵营资源库存）----

    async def init_war_faction_resources(self, war_id, initial):
        """幂等初始化阵营库存。initial={faction: {category_id: count}}；
        已有行不覆盖（ON CONFLICT DO NOTHING）。"""
        rows = []
        for fid, cats in (initial or {}).items():
            for cat, cnt in cats.items():
                if cnt:
                    rows.append((int(war_id), int(fid), int(cat), int(cnt)))
        if not rows:
            return
        async with self.pool.acquire() as conn:
            await conn.executemany(
                'INSERT INTO war_faction_resources '
                '(war_id, faction, category_id, count) '
                'VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING', rows)

    async def list_war_faction_resources(self, war_id, faction=None):
        """阵营库存行。faction=None 时全阵营。"""
        sql = ('SELECT war_id, faction, category_id, count '
               'FROM war_faction_resources WHERE war_id = $1')
        args = [int(war_id)]
        if faction is not None:
            sql += ' AND faction = $2'
            args.append(int(faction))
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql + ' ORDER BY faction, category_id', *args)
        return [dict(r) for r in rows]

    async def adjust_war_faction_resources(self, war_id, faction, deltas):
        """按类别批量增减库存（delta 可负，clamp 到 0）。返回更新后行。"""
        if not deltas:
            return []
        sets = sorted((int(cat), int(d)) for cat, d in deltas.items() if d)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                for cat, d in sets:
                    await conn.execute(
                        'INSERT INTO war_faction_resources '
                        '(war_id, faction, category_id, count) '
                        'VALUES ($1, $2, $3, GREATEST($4, 0)) '
                        'ON CONFLICT (war_id, faction, category_id) DO UPDATE '
                        'SET count = GREATEST(war_faction_resources.count + $4, 0)',
                        int(war_id), int(faction), cat, d)
            rows = await conn.fetch(
                'SELECT war_id, faction, category_id, count '
                'FROM war_faction_resources WHERE war_id = $1 AND faction = $2 '
                'ORDER BY category_id', int(war_id), int(faction))
        return [dict(r) for r in rows]

    async def update_war_production(self, war_id, production_ms):
        """推进生产游标（M3 tick）。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE wars SET production_ms = $2 WHERE id = $1',
                int(war_id), int(production_ms))

    # ---- war_supply_queue（M3-2 阵营资源增援队列）----

    async def enqueue_war_supply(self, war_id, faction, command_node_id,
                                 items):
        """AT 增援缺口入队（FIFO）：items={category_id: count}，逐行写
        queue_number = 阵营内 COALESCE(MAX,0)+1 单调分配。返回入队行数。"""
        rows = [(int(war_id), int(faction), int(command_node_id),
                 int(cat), int(cnt)) for cat, cnt in (items or {}).items()
                if int(cnt) > 0]
        if not rows:
            return 0
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                for wid, fac, cnid, cat, cnt in rows:
                    await conn.execute(
                        'INSERT INTO war_supply_queue '
                        '(war_id, faction, command_node_id, category_id, '
                        'count, queue_number) VALUES '
                        '($1, $2, $3, $4, $5, COALESCE('
                        '(SELECT MAX(queue_number) FROM war_supply_queue '
                        ' WHERE war_id = $1 AND faction = $2), 0) + 1)',
                        wid, fac, cnid, cat, cnt)
        return len(rows)

    async def list_war_supply_queue(self, war_id, faction=None,
                                    command_node_id=None):
        """队列行（FIFO 序：faction, queue_number）。可按阵营/AT 过滤。"""
        sql = ('SELECT id, war_id, faction, command_node_id, category_id, '
               'count, queue_number, created_at '
               'FROM war_supply_queue WHERE war_id = $1')
        args = [int(war_id)]
        if faction is not None:
            sql += ' AND faction = $2'
            args.append(int(faction))
        if command_node_id is not None:
            sql += f' AND command_node_id = ${len(args) + 1}'
            args.append(int(command_node_id))
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql + ' ORDER BY faction, queue_number',
                                    *args)
        return [dict(r) for r in rows]

    async def dequeue_war_supply(self, row_ids):
        """按行 id 批量出队（交付/AT 歼灭解散清理）。返回删除行数。"""
        ids = [int(i) for i in (row_ids or [])]
        if not ids:
            return 0
        async with self.pool.acquire() as conn:
            res = await conn.execute(
                'DELETE FROM war_supply_queue WHERE id = ANY($1::bigint[])',
                ids)
        try:
            return int(res.split()[-1])
        except (ValueError, IndexError):
            return len(ids)
