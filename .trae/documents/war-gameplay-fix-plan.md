# 战争玩法规则修复计划（war1/war2 ↔ 实现 7 项对齐）

> 日期：2026-09-08
> **状态：7 项全部完成**（第 1/5/6 项 → lifecycle/tick/war_at；第 2 项 → battle_type/
> position/factioncount 建模 + 野外遭遇成局；第 3/4/7/8 项 → `war_campaign/automission.py`
> 阈值/tie 判定，production.automission_tick 转发；回归 test_war_movement 100/100 含
> K1-K9、test_war_campaign 71/71、test_war_at_supply 90/90 全绿）。
> 依据：docs/war-vs-implementation-audit.md 三大核心出入的修复清单。
> 用户确认的决策：
>   - 单点/三点图 = 按该 battle 实际 preset → gamemap → 据点(CapturepointTemplate) 数判定
>   - 三种交战类型 = 按**原游戏设计**（城市归属 + 进攻方部队属性）判定
>   - 系统判定胜负 = XP + warfunds 全跳过
>   - **生成战局的逻辑（交战类型判定）修改完后需找用户确认**，再继续后续项

文中服从硬约束：所有新数值从 mm_config 可配（环境变量覆盖）；automission 为正交到
`war_campaign/automission.py` 的新模块，避免继续膨胀 `production.py`/`tick.py`。

---

## 上下文 / 动机

war1/war2 文档描述了官方将军/战争模式的详细规则。当前实现把「无人加入自动结算」退化为
「AT 剩余资源总量比较 + %损耗」的统一 automission，与文档的整数兵力阈值 / 多种交战类型 /
撤退惩罚 / 包围全歼相去甚远。本计划将这 7 项对齐到可配置、可测试的实现。

---

## 关键依赖（复用现有代码）

- 兵力 = AT `resources` 中士兵族类别之和；`_SOLDIER_CATS = {1,2,4,5,6}`（`war_campaign/common.py`）
- 单点/三点判定：`preset → gamemap`（`wardb.preset_scene`）→ `wardb.capturepoint_templates(gamemap)` 据点数
- 城市归属：`store.get_war_cities(war_id)` → dict；`at_current_battlefield(at)`（`war_campaign/at_movement.py`）
- 包围判定：`wardb.supplyline_adjacency()` 邻接表（bf → 邻居 bf），`common.find_supplyline_path`
- 士气/编削回写：`settle_war_battle`（`war_campaign/tick.py`），`update_command_node`
- 伞兵判定：`is_paradrop_unit`（`war_campaign/at_ops.py`），伞兵类 `_PLANE_CATS` 中的运输机族

---

## 第 1 项：胜利条件第二支（一方失光主城 → 比较剩下两方）

**文件**：`war_campaign/lifecycle.py` → `check_victory()`
**改动**：现有 alone「某方首都数 ≥ needed」。新增：统计三阵营首都数，
若某阵营 `counts[f]==0`（已失全部主城），则比较剩余两方首都数，**多者胜**；
平分时平局（无胜方，续 war）。
若出现两支同时成立（极端），优先「≥needed」胜。
**配置**：无新增（复用 `effective_points_needed`）。

## 第 2 项：交战类型建模（对齐官方 8 种 missiontype，2026-09-08 反编译复核修正）

### 官方模型（客户端实锤，见「原版核对结论」节）

- 官方 missiontype：AREA_RECONNAISSANCE=0 / **ASSAULT=1**（城战）/ DESTRUCTION=2 /
  **SKIRMISH=3**（补给线·步/侦/装）/ SUPPLY=4 / **ENCOUNTER=5**（补给线·纯步兵）/
  TANK_ENCOUNTER=6 / PILOT_ENCOUNTER=7 / RECON_ENCOUNTER=8（客户端无消费点）。
- **无 paradrop 类型**：伞兵攻城在类型上就是 Assault；war2.md 的 288 伞兵是
  automission 判定规则（攻方 AT 为伞兵族时的自动结算规则），不是战斗类型。
- `battle` 行契约：`mapEntityTypeId`（1=城市/2=出兵点/3=补给线）+ `position`（float，
  补给线上的位置）+ `factioncount`（f9，参战阵营数如实计数）+
  `excludedFactionId`（两方局=被排除第三方；三方局=0，2026-09-08 三国图考证
  后分层口径：协议层如实填）+ `activationTimeStamp`（激活时刻，准备期应体现在此）。
  当前 `battle_msgs.py` 硬编码 BATTLEFIELD/position=0/excluded=0——本项一并修正。

### 修正后的类型判定规则（战局生成逻辑，完成后找用户确认）

- **城市攻防战 `assault`（type 1）**：AT 到达敌占城（owner ∈ 1/2/3 且非己方）且城内
  有敌 AT → 成局。攻方=到达方，守方=城 owner（`MissionInfo.defenderFactionId`）。
  城防/城攻是同一场城战的两侧视角，不单独成类型。
- **野外遭遇战（补给线）**：两敌对 AT 移动中同一补给线段（`state==1 &&
  map_entity_type==SUPPLYLINE && map_entity_id 相同` 且不同阵营）→ 成局：
  - 双方均为步兵族 AT → `encounter`（type 5）
  - 任一方含侦察/装甲族 → `skirmish`（type 3）
  - map_entity_id=该补给线 id，battle 行 `position`=双方 AT 位置折算（0-1），
    命名 `Encounter/Capture & Hold near <城市-缩写>`（客户端按 supplyline 自动命名，无需服务端发文案）
- **伞兵**：不设类型。攻城成局时若攻方 AT 为伞兵族（`is_paradrop_unit`），
  仅在 automission 套用伞兵判定规则（第 7 项）。

**文件**：
- `store/schema.py`：`war_battles` 加 `battle_type INT DEFAULT 1`（存官方 missiontype：
  1=assault/3=skirmish/5=encounter）+ `position FLOAT DEFAULT 0` +
  `excluded_faction INT DEFAULT 0` + `faction_count INT DEFAULT 2`（三国图分层口径：
  第三阵营敌对 AT 到场加入既有会战时递增）
- `store/war.py`：`create_war_battle` 增参；get/list SELECT 带新列
- `war_campaign/battle_msgs.py` → `msg_battle_row`：按 wb 行填 `mapEntityTypeId`
  （城战=BATTLEFIELD，补给线=SUPPLYLINE）、`position`、`factioncount`（= 参战
  阵营数）、`excludedFactionId`（两方局=1/2/3 中非参战两方；三方局=0）、
  `activationTimeStamp`（= started_at + 准备期，见下）
- `war_campaign/at_movement.py`：新增 `moving_ats_by_supplyline(war_id)` → {sl_id: [AT…]}
- `war_campaign/tick.py` → `advance_movements()`/`_check_battle_formation()`：
  1. 到达敌占城 → assault 成局（现有逻辑 + 类型标记）
  2. 移动扫描同段敌对 → encounter/skirmish 成局（新增，AT 双方 state 置 2 会战中，
     移动 segment 冻结）
- 准备期配置：`WAR_BATTLE_PREP_FIELD`（野外，默认 300s）/ `WAR_BATTLE_PREP_ASSAULT`
  （城攻，默认 330s），写入 battle 行 `activationTimeStamp`，automission 超时以此起算。

## 第 3 项：准备期无玩家按 144（单点）/216（三点）步兵判胜负

**文件**：`war_campaign/automission.py`（新建，承接 `production.automission_tick` 改造）
**改动**（适用 encounter/skirmish 补给线会战）：
- 按 `mission record` 的 `preset` → `wardb.preset_scene(preset)` → gamemap → 据点数，
  据点 ≥ 2 判「三点」用 216，否则「单点」用 144。
  配置：`WAR_AUTO_TP_CP`（三点判据点门槛，默认 2）、`WAR_AUTO_SP_MIN`（144）、`WAR_AUTO_TP_MIN`（216）。
- 超时 = battle 行 `activationTimeStamp`（成局 + `WAR_BATTLE_PREP_FIELD` 300s）后无人加入。
  兵力 = `_SOLDIER_CATS` 求和。单向不足 → 该方败北，损 `WAR_AUTO_LOSS`（默认 0.25）编制
  + 扣 `WAR_MORALE_LOST_PER_BATTLE`。
- 双方都满足 → 等第 8 项 tie 超时再比兵力少者败（同一结算入口）。
- ⚠️ 数值（144/216/时间窗）为 war1.md 社区口径，客户端无证据，全部走 mm_config 可配。

## 第 4 项：城攻需进攻方 216 步兵，5 分 30 秒判定

- `automission.py` 中 `battle_type==1/assault`（非伞兵攻方）：超时
  `WAR_AUTO_ATTACK_TIMEOUT`（默认 330s，= `WAR_BATTLE_PREP_ASSAULT`），
  进攻方 `_SOLDIER_CATS` 步兵 < `WAR_AUTO_TP_MIN`(216) → 攻方败北（损 25% + 20 士气）；
  足够 → 攻方胜（城易主）。共用第 3 项的结算入口。

## 第 5 项：系统判定胜负不提供经验（XP + warfunds 全跳过）

**文件**：`war_campaign/tick.py` → `settle_war_battle()`
**改动**：加形参 `is_auto=False`。为 True 时：
- 跳过 `battles/battles_won/xp/rank` 计算与回写（保持原值）
- 跳过 `add_war_funds`（每局 + 胜方加成）
- 仍执行编制损耗、士气回写、城市易主、会战终态、check_victory
- automission 结算调用处传 `is_auto=True`；FPS 真人结算传 `False`。

## 第 6 项：撤退惩罚 + 随机撤退方向 + 包围全歼

**文件**：`handlers/war_at.py` → `handle_retreat_command_node_request`
**改动**：
- AT 处于交战状态（`state==2`/会战中）时撤出：
  - 损 `WAR_RETREAT_LOSS`（默认 0.25）编制
  - 扣 `WAR_MORALE_LOST_PER_BATTLE`(20) 士气
  - 无经验无战币（本就无此发放路径，确认即可）
- 撤退落点：沿随机友方路线回退到最近友方城市（`find_supplyline_path` 到最近友方邻城）；
  无友方路径 → 回己方首都（现有行为兜底）。
- **包围判定**：AT 所在城市的所有补给线邻居均为敌占城（无敌方空城/中立/己方）→ 无法撤退，
  `RetreatCommandNodeResponse` 返回失败码（复用 `RC_COMMANDNODE_WAR_INSTANCE_NOT_FOUND` 或以 103 类拒绝），
  不执行撤退；战败(下一回合 automission 失败)时全歼（编制扣 0，`state=3`）。
  配置：`WAR_RETREAT_LOSS`、`WAR_ENCIRCLE_CHECK`（默认 1 开启包围判断）。

## 第 7 项：伞兵攻城 automission 判定（288 伞兵 / 守方 <216 则胜 / 双方满足 6 小时后少者败）

**文件**：`automission.py`
**改动**（`battle_type==1/assault` 且**攻方 AT 为伞兵族**时套用，非独立类型）：
- 超时 `WAR_AUTO_PARADROP_TIMEOUT`（默认 330s）
- 进攻方伞兵数 < `WAR_AUTO_PARA_MIN`(288) → 攻方败北，伞兵落地变步兵
  （复用现有 `convert_to` `*_on_ground` 变体），损 25% + 20 士气
- 进攻伞兵 ≥288 且防守步兵 < `WAR_AUTO_TP_MIN`(216) → 伞兵胜占城
- 双方都满足 → tie 折算：等第 8 项 tie 超时，兵力少者败
- 伞兵数 = 攻方 AT 伞兵编制合计（cat4 `paratroopers`，按模板编制数计）。

## 第 8 项（tie 共用）：双方兵力都满足 → 6 小时后比兵力少者败

- automission 结算拆两阶段：超时先判兵力门槛；皆满足则标记 `tie_pending`，再等
  `WAR_AUTO_TIE_HOURS`（默认 6h）后比兵力少者败。
- `war_battles` 可复用 `started_at` + 加一列 `tie_timeout_at TIMESTAMPTZ`（ALTER，可选；或内存/ replay 少备选）。为轻量，**复用 started_at + battle_type**，用 tie 判定只读 DB 无需新列（首次门槛满足时记录，tick 内算已过时长）。若无新列则确定性依赖 started_at，避免破坏既有同名流程。

---

## 实施顺序（含确认门）

1. **第 1 项**胜利条件、**第 5 项**系统判定不发放 → 独立小改，先做
2. **第 6 项**撤退惩罚/包围 → 独立 handler 改，次做
3. **第 2 项**成局 battle_type + 战局生成逻辑 → **完成后找用户确认**（用户要求）
4. 支 3/4/7 新建 `automission.py` + tie 判定（第 8 项）→ 最后，基于类型读阈值
5. 全量回归

## 关键文件清单

- `war_campaign/lifecycle.py`（check_victory）
- `war_campaign/tick.py`（_check_battle_formation、settle_war_battle is_auto）
- `war_campaign/automission.py`（**新建**：阈值/tie 判定，替换 production.automission_tick 主体）
- `war_campaign/production.py`（automission_tick 改为转发到新模块）
- `handlers/war_at.py`（撤退惩罚/包围）
- `store/schema.py`（war_battles.battle_type/faction_count 迁移）
- `store/war.py`（create_war_battle/get/list 带 battle_type/faction_count）
- `mm_config.py`（新增 WAR_AUTO_* / WAR_BATTLE_PREP_FIELD / WAR_RETREAT_LOSS 等）
- `wardb.py`（如有需要，暴露据点数 helper；复用 preset_scene/capturepoint_templates）
- 测试：`tests/test_war_campaign.py`（automission 类型/阈值）、`tests/test_war_movement.py`、
  `tests/test_war_at_supply.py`（撤退）

## 验证

- 新增 pytest：
  - `test_check_victory_shutdown`：某方首都=0 → 剩两方多者胜（含平分无胜方）
  - `test_automission_field/single_vs_triple`：单点 144 / 三点 216 门槛，不足方损 25% + 20 士气
  - `test_automission_city_attack`：攻方 216 判定，5:30 超时
  - `test_automission_paradrop`：288 伞兵 / <216 守 / 双方满足 tie 少者败
  - `test_system_judge_no_reward`：is_auto=True 时 XP/warfunds 不变
  - `test_retreat_penalty_encircled`：撤退损 25%+20 士气；包围时拒绝撤退
- 跑通全量：`pytest tests/`（war_campaign 61 + war_movement 52 + war_at_supply + mm_pool 32 + stats 44 + settlement 8 + latejoin 58 + player_ids/snow 19）

---

## 附：原版核对结论（war1/war2 文档 vs 原版客户端，2026-09-08 反编译实锤）

### 与原版一致（文档可信）
| war1/war2 断言 | 原版证据 |
|---|---|
| 胜利两支：夺 N 主城 / 一方被灭后比主城 | 官方文案 `Capture {0} of {1} major cities to win` + `Hold most major cities upon faction defeat`；MajorCityModel 从 `war.pointsNeededForVictory` 读动态值（15 非常数，是 war 行字段） |
| 野外遭遇=移动途中（补给线）相遇 | `battle` 行 `mapEntityTypeId=SUPPLYLINE + position(float)`；encounter/skirmish 任务 map entity 为补给线，命名 `Encounter/Capture & Hold near <城市-缩写>`；战斗标记可渲染于城市/出兵点/补给线三实体 |
| 城防/城攻=同一场城战两侧 | 官方仅 ASSAULT=1 一种城战类型 |
| 士气模型（断 morale/恢复/低士气全歼） | `Morale breaks at:`/`Full morale in:`/`was fully defeated due to low morale` |
| 攻守角色（到达方=攻方，城 owner=守方） | `MissionInfo.defenderFactionId` REQUIRED 语义吻合 |

### 文档与原版的出入（文档不准，本计划已按原版修正）
1. **伞兵不是交战类型**：官方 8 种 missiontype 无 paradrop；伞兵攻城=Assault，288 伞兵是 automission 判定规则 → 本计划第 7 项改为规则修饰而非 battle_type。
2. **野外遭遇两子类**：Encounter（纯步兵）vs Skirmish（步/侦/装），war1.md 统称丢了区分 → 第 2 项按 AT 构成细分。
3. **战斗可发生在出兵点（ACCESSPOINT）**，war1.md 未提（本计划暂不实装，battle 行契约已兼容）。
4. **三国图考证（2026-09-08 二次考证，此前结论部分推翻）**：早前判断「战斗恒
   2 阵营制、不实装三方局」**过于绝对**。新证据：Gamepressure 官方时代指南原文
   "all three sides ... can take part in a single battle ... all of them are
   present on the battlefield"；官网 Campaign 成局条件 "two or more hostile
   Assault Teams" 无上限 2；battle 行 `factioncount` (f9) 是 N 方计数遗迹。
   修正口径（分层实装）：
   - **协议层对齐（本计划采纳）**：成局登记不裁剪第三方阵营 AT——第三阵营敌对
     AT 到达交战节点加入既有会战（factioncount 如实计数），两方局
     excludedFactionId=被排除第三方、三方局=0；
   - **战斗内落位不开放（维持现状）**：第三阵营玩家 join 闸保持挡，因三方局
     出生侧分配无任何资料（客户端对 factioncount/excludedFactionId 的 UI
     消费点为零），待 VPS/录像验证后再议。
   详见审计文档附录条目 4。
5. **144/216/288 与 5:00/5:30/6h**：纯服务端规则，客户端零证据 → 全走 mm_config 可配，VPS 观感校准。
6. `activationTimeStamp`（battle 行 REQUIRED）官方语义应为准备期激活时刻 → 第 2 项以此承载准备期。