# -*- coding: utf-8 -*-
"""数据库 schema DDL（建表/索引/迁移），PlayerStore.start() 应用。
拆分自 player_store.py（纯移动，字符串逐字保留）。
"""

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
-- 账号封禁（管理端写入；登录侧 login2 拦截回 91 永久 / 92 限时，见
-- handlers/auth.py）。ban_expires 空 = 永久；到期由 get_ban_info 惰性解封。
ALTER TABLE players ADD COLUMN IF NOT EXISTS banned BOOL NOT NULL DEFAULT FALSE;
ALTER TABLE players ADD COLUMN IF NOT EXISTS ban_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE players ADD COLUMN IF NOT EXISTS ban_expires TIMESTAMPTZ;
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
-- （_DAILY_BONUS_AVAILABLE_SQL：早于最近 05:00 UTC 重置点即 TRUE，
-- 与客户端 DailyBonusModel 窗口一致，跨点自动回 TRUE）
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
-- 交战类型建模（war-gameplay-fix-plan §2）：battle_type 存官方 missiontype
-- （1=assault 城战 / 3=skirmish 补给线·步侦装 / 5=encounter 补给线·纯步兵）；
-- position = 补给线会战位置（0-1，ap1→ap2 方向）；factioncount/excludedFactionId
-- 由 factions_json 渲染时派生（battle_msgs.msg_battle_row，恒一致）
ALTER TABLE war_battles ADD COLUMN IF NOT EXISTS battle_type INT NOT NULL DEFAULT 1;
ALTER TABLE war_battles ADD COLUMN IF NOT EXISTS position FLOAT NOT NULL DEFAULT 0;
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

-- ===== 外服账号迁移（foreign_import.py，web /api/import）=====
-- 同一外服账号只许导入一次：UNIQUE 约束在 DB 层兜底（并发防重）。
-- foreign_player_id = 外服 player KVCS 行 f1（雪花 int64，外服侧稳定身份，
-- 外服改名也不影响去重）；foreign_username 仅作展示/审计。
CREATE TABLE IF NOT EXISTS foreign_imports (
    id                BIGSERIAL PRIMARY KEY,
    foreign_server    TEXT NOT NULL,          -- 'host:port'（多外服源扩展位）
    foreign_player_id BIGINT NOT NULL,
    foreign_username  TEXT NOT NULL,
    local_player_id   BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    dump_path         TEXT NOT NULL DEFAULT '',   -- 采集 dump 目录（审计/复查）
    characters        INT  NOT NULL DEFAULT 0,    -- 导入统计速览
    weapons           INT  NOT NULL DEFAULT 0,
    vehicles          INT  NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (foreign_server, foreign_player_id)
);
CREATE INDEX IF NOT EXISTS idx_foreign_imports_local ON foreign_imports(local_player_id);

-- ===== 动态配置（web 管理端可写，游戏服运行时读取）=====
-- key 为配置项标识（如 'weather'）；value 为该项 JSON 配置。
-- 优先级：有行 > 代码内置默认（无行时环境变量兜底照旧生效）。
-- 成局等低频路径直接读库（保存即生效）；未来热路径项迁入时再在
-- get_config 内部加 TTL 缓存 + pg_notify 失效，接口不变。
CREATE TABLE IF NOT EXISTS server_config (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by TEXT NOT NULL DEFAULT ''
);

-- ===== 登录事件审计（IP / 机器码）=====
-- 客户端每次启动登录前发 QueryBannedMachineRequest，携带 hngsync.exe 本机
-- 生成的 machineIdentifier（36 字符类 GUID）；IP 取 TCP peer。成功与失败
-- （97/98/91/92）都记录——撞库尝试与小号排查的依据。
-- player_id 无外键：管理后台删号后审计记录保留（机器码连坐排查依据）。
CREATE TABLE IF NOT EXISTS login_events (
    id             BIGSERIAL PRIMARY KEY,
    player_id      BIGINT,
    username       TEXT NOT NULL DEFAULT '',
    ip             TEXT,
    machine_id     TEXT NOT NULL DEFAULT '',
    machine_id_old TEXT NOT NULL DEFAULT '',
    result         INT  NOT NULL,                 -- 97=成功 98=密码错 91/92=封禁拦截
    source         TEXT NOT NULL DEFAULT 'game',  -- game / register / import
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_login_events_player  ON login_events(player_id);
CREATE INDEX IF NOT EXISTS idx_login_events_ip      ON login_events(ip);
CREATE INDEX IF NOT EXISTS idx_login_events_machine ON login_events(machine_id);

-- ===== 机器码封禁（管理端"连带封禁"写入）=====
-- 客户端启动时 QueryBannedMachineRequest 上报 machineIdentifier，命中则回
-- isBanned=true，客户端直接弹封禁提示并中止同步（连登录界面都不出现）；
-- login2 再查一次作纵深防御。banned_player_id 供解封时清理本玩家登记的机器。
CREATE TABLE IF NOT EXISTS machine_bans (
    machine_id       TEXT PRIMARY KEY,
    reason           TEXT NOT NULL DEFAULT '',
    banned_player_id BIGINT NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_machine_bans_player ON machine_bans(banned_player_id);

-- ===== 玩家永久解锁（player f37 位图源）=====
-- 非 PlayerLevelUnlock 来源的永久解锁（通行证奖励 progressionUnlock，
-- 如赛季1满级面部迷彩 2387-2395 eHeadSwap）。客户端 hasPlayerLevelUnlock
-- 按 progressionUnlock.id 直接索引玩家 f37 位图，而等级推导只覆盖
-- PlayerLevelUnlock 表 id 1-40——这些 id 不登记则外观窗口永远未解锁。
CREATE TABLE IF NOT EXISTS player_unlocks (
    player_id BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    unlock_id BIGINT NOT NULL,
    PRIMARY KEY (player_id, unlock_id)
);
"""



