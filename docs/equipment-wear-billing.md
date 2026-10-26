# 装备维护损耗：官服按发计费语义（2026-09-09）

2026-10-26修正：载具、载具升级和载具弹药改为读取结算包逐实例耐久状态。
角色出生数不能当作载具部署次数。完整协议证据及测试见
[逐实例结算修复](vehicle-condition-settlement-fix-2026-10-26.md)。

## 背景：估算算法多扣事故（2026-09-08）

玩家 1000093 反馈"带了长装甲投掷手雷没用也被扣维护费"。排查
`remote-log/realtest_game.log.2026-09-08.log` 发现系统性多扣：旧
`_apply_equipment_wear` 按 `timeActive` **估算**射击发数
（`_est_shots = max(100, min(2000, 时长秒 × 0.75))`），对一发未开的玩家
也按估算值扣武器/配件/弹药磨损。该玩家三局被按 60/33/38 发回购手雷
弹药（弹药 wearPerShot=1.0 → condition 即剩余发数），实际全天才扔 3
发，弹药部分被多扣约 44 倍，收益不足以覆盖扣费。

根因：官服语义是**按实际消耗计费**（打了多少发扣多少发、出生多少次
扣多少次），估算值对 0 发玩家必然虚增。客户端
`ItemMaintenance`/`PriceObject.calculateFractionPrice` 与 WarDatabase
模板 `wearPerShot`/`wearPerSpawn` 全部按此模型设计。

## 官方磨损模型（客户端逆向 + WarDatabase 模板）

| 物品类型 | maxCondition 量纲 | 磨损率 | 计费单位 |
|---|---|---|---|
| 武器 / 配件 | 1.0（0~1 比例） | wearPerShot（8e-6~3.13e-4 / 1.93e-4~1.984e-3） | 每发 |
| 载具 / 载具升级 | 点数 25~220 | wearPerSpawn（全表 1.0） | 每次出生 |
| 武器弹药 / 载具弹药 | 剩余发数 | wearPerShot（1.0 / 0.5 / 0） | 每发 |

- 全表 `wearPerBattle = 0`（不存在"按局"磨损）。
- `maxCondition = 0` 的武器模板（匕首/手雷/望远镜等 120 个）磨损率全
  0，不可维护——带在身上不产生任何费用（1000093 事故的直接诉求）。
- PlayerSpawns 是**角色级标量**统计（stat 12），不能区分步行及具体载具，
  不得用于载具磨损。载具系列以PlayerBattleResult f10/f11/f12逐实例状态为准。

## 实现链路

### 1. type6 用量累积（statistics_server.py）

战斗服周期上报的 type6 统计新增累积入口：

- `process_type6_body(body, peer)`：type6 帧处理统一入口（解压 → 解析 →
  **先累积用量 → 再落库**）。顺序铁律：`_accumulate_battle_usage` 是纯
  内存同步操作，必须在 `_persist_type6`（逐条 `await STORE.add_stat`，
  反复让出事件循环）之前执行——否则结算（AnnounceBattleFieldEarnings）
  可在落库间隙插队，`pop_battle_usage` 时用量还未入账，整局磨损被误判
  "无 type6 数据"跳过（2026-09-09 01:40 mission=356460 四角色全漏扣
  的根因）。
- `_accumulate_battle_usage(session_id, chars)`：type6 处理时逐帧调用。
  - `mission_id = session_id % 1_000_000`（session 尾数即 missionId，
    实测 730433917100003 → 100003；尾数 0 忽略）。
  - stat 519（WeaponShotsFired）按武器模板累积发数；stat 12
    （PlayerSpawns）累积出生数。
  - **同帧去重**：载具武器发数会以裸模板与 `(载具模板<<16|模板)` 两种
    ctx1 各报一次且值相同（char 177 实测 w88=30 与 w3801176=30 并存），
    按低 16 位武器模板取 max 防双计；帧间是增量语义，跨帧累加。
  - 缓存 `_BATTLE_USAGE` 最多 128 局（FIFO 淘汰，`_BATTLE_USAGE_KEYS`
    保插入序），防泄漏。
- `pop_battle_usage(mission_id)`：结算时取走该局用量
  `{charId: {'shots': {武器模板: 发数}, 'spawns': 出生数}}`，
  **取走即删**——重复结算/迟到补报不会对同一局二次计损；取走过的局
  记入 `_BATTLE_USAGE_DONE`（FIFO 淘汰）。
- `wait_battle_usage(mission_id, timeout=3.0)`：结算等待该局用量到达
  （0.1s 轮询），到达即 pop 返回，确保"消耗数据到达才计费"。已结算过
  的局（重复结算）立即返回 None；超时未到返回 None——宁可不扣不多扣。

### 2. 归属链帮助函数（wardb.py）

- `vehicle_ammo_weapon_templates(parent_vehicle_tid, ammo_template_id)`：
  载具基础模板上能发射指定弹药的驾驶员主武器模板集合（经
  `VehicleWeaponSlot → weaponAmmoMapping` 推导，与客户端
  `getAvailableVehicleAmmoStorage` 同源）。载具弹药磨损 = 这些武器
  实际发数之和 × 弹药 wearPerShot。集合去重防同模板多槽（双联装机枪）
  双计。未知弹药/载具返回空集合（磨损 0，宁可不扣）。
  **2026-10-26：此归属帮助函数不再用于结算**，同模板武器会被不同载具
  共享，单靠武器模板发数无法定位弹药实例；现直接读取f12耐久变化。
  注意 `weaponAmmoMapping` 元组是 `(映射id, 弹药模板id, isDefault)`，
  锚点：vehicle 58 主武器 88 → 默认弹药模板 139（映射 id 291）。

### 3. 磨损扣减（handlers/combat.py `_apply_equipment_wear`）

结算路径 `handle_announce_battlefield_earnings` 内：
`wait_battle_usage(missionId)`（等待 type6 用量到达，见上）→
`_apply_equipment_wear(char_times, usage)`（type6 帧与结算同秒到达、
分属不同连接，结算先到时短暂等待确保数据到达才计费）。

**charge 语义**：
- 武器系列：usage 有该角色数据 → 按实际发数扣，0发不扣，绝无估算。
- usage 为 None（该局无 type6，统计链路异常）或角色不在其中 → 跳过该
  角色的武器系列磨损并记日志。载具系列仍可按结算包正常扣损。
- timeActive=0（未参战）/ bot 角色整体跳过。
- 仅对已装备物品生效：武器 `container_slot>0`、配件/弹药挂已装备武器
  （弹药还须 `slot_index>0` 在膛）、载具 `equipped_in_slot>0`、升级/
  载具弹药装在已装备载具上（升级 `equipped_slot_id>0`、弹药
  `equipped_storage_id>0`）。仓库物品不磨损。
- 磨损公式：`new = max(0, cur - wearPerShot × 实际发数)`（武器/配件/
  武器弹药）；载具/升级/载具弹药为`max(0, cur - (initialCondition-condition))`，
  精确到物品实例。装备中同模板武器至多一件（弹药槽类别互斥），模板发数即物品
  发数。扣减后钳到 0 不为负。
- 返回 `(spent, worn_rows)`：spent 供 ItemMaintenance 通知；
  worn_rows 逐玩家重推 KVCS 行刷新客户端耐久/弹药 UI（不推需重登才变）。

### 4. 战后自动维修（autoRepair，2026-08-31 已有，口径不变）

`_auto_repair_after_wear`：物品 `autoRepair` 1=Credits / 2=Gold 时按
`max(1, int(基础价 × (max-cur)/max))` 扣费修复到满耐久（与客户端
`PriceObject.calculateFractionPrice` 同式）；定价缺失免费修复；余额
不足保持磨损。磨损基数从估算值改为实际损耗后，维修费同步回归精确。

## 测试（tests/test_equipment_wear.py，22 项）

- A. wardb 磨损静态表（量纲/磨损率分布/满耐久回退）
- B. type6 用量累积：尾数关联、跨帧增量累加、双报去重、FIFO 淘汰、
  取走即删、**入账先于落库**（process_type6_body 顺序铁律）、
  **wait_battle_usage 等待语义**（在途等到/重复结算免等/超时不扣）
- C. 磨损数学与跳过规则：全装备矩阵（未开火武器/不可维护/已损坏/仓库
  物品均不扣）、**0 发 0 出生一分不扣（1000093 事故场景）**、无数据
  跳过、未参战跳过、钳底不为负
- C2. autoRepair 三态（Credits/Gold/余额不足/定价缺失/载具/弹药）
- D. 完整结算链路：type6 + AnnounceBattleFieldEarnings → 按实际用量
  受损、响应先行、重复结算不二次计损
- E. 真实 DB 集成（事务 ROLLBACK）：condition 迁移归一化、create_vehicle
  满耐久、磨损精确扣减

相关回归（本改动触及面）：test_stats_type6 44 项、test_settlement_parse
12 项、test_autorepair、test_battlepass 67 项、test_storebundle_delivery
52 项、test_player_ids、test_mod_class_strip——全部通过。

## 已知边界

- type6 帧晚于结算到达：结算侧 `wait_battle_usage` 最多等 3s（覆盖同秒
  到达但结算先处理的竞态，2026-09-09 mission=356460 事故）；仍不到才按
  "无数据"跳过磨损，不会多扣。
- 载具出生消耗（vehicles.js `spawnCost`）是另一套购买/部署定价，不属于
  condition 磨损体系，本文不覆盖。
