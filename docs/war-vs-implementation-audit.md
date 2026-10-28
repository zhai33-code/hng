# 战争玩法文档（war1/war2） ↔ 当前实现 核对审计

> 日期：2026-09-08
> 用途：对照 `docs/war1.md`、`docs/war2.md`（H&G 将军玩法视频文字总结）逐条核对
> 当前 RTS 实现（`war_campaign/`、`handlers/war_at.py`、`force_pool.py`、`wardb.py`、
> `mm_config.py`、`handlers/progression.py`）的出入，作为补齐实施清单。
>
> 状态标记：
> - ✅ 已对齐：代码有实锤，与文档一致
> - ⚠️ 有出入：实现与文档描述不一致
> - 🔲 未见实现 / 待数据库考证（macOS 无 WarDatabase，数值需 VPS 核实）

---

## 一、解锁 / 初始化（war1）

| 文档断言 | 状态 | 证据 / 出入 |
|---|---|---|
| 账号 10 级解锁战争模式并获赠 2 万战争资金 | 🔲 | 等级奖励走 `handlers/progression.py` + `PlayerLevelUnlock` 表（`wardb.py _build_level_unlocks`），数值取决于 WarDatabase，macOS 无表无法核实 20000/rank10 是否命中 |
| 士兵 12 级才可携带作战部队 | 🔲 | 表驱动，待 DB 考证 |
| 选择阵营后本战争结束前无法更改 | ⚠️ | 有阵营持 lock 语义，但未见「战争结束前不可更换」的持久化约束；`faction_capital_bfs`/`_faction_capital_bf` 仅按 faction 查首都，无换阵禁止 |
| 劣势阵营额外经验加成（游戏内外生效） | ✅ | `at_ops.underdog_multipliers()` + `WAR_UNDERDOG_*`；faction 行 f6/f7 下发 + 结算放大 |

## 二、部队携带 / 将军（war1）

| 文档断言 | 状态 | 证据 / 出入 |
|---|---|---|
| 将军无视兵种任意采购（当前口径=指挥点） | ⚠️ | 实现为 `commandPointsUnlocked > 0` 而非 isGeneral（`validate_create_at`）。文档说「将军可任意买、普通兵种仅限对应兵种」，**实现未按兵种限制购买** |
| 将军三种获取途径（满级晋升/商店购买/签到 364 天） | 🔲 | `handlers/career.py` 有 is_general_character 判定，但「签到 364 天赠将军」数据链未见 |

## 三、部队类型与发展树（war1）

| 文档断言 | 状态 | 证据 / 出入 |
|---|---|---|
| 五类部队初始/升级编制（步兵 36/90…、坦克 16…、飞机 16/24…、伞兵 36+9…） | ⚠️ | 编制走 `commandnodetemplatearmyresourcetemplaterelation` 表（`wardb.py`），数值完全由 WarDatabase 决定，需 VPS 表核对；`force_pool` 兜底 `{1:90}` 等仅 training；升级链 = parent_template_id 挂靠 + motorized_guard 特例（`upgrade_allowed`，2026-09-08 修正：convertTo 非升级字段，见第九节） |
| 显示名称可随意、短称用于排序（汉字>字母>数字） | 🔲 | 客户端本地排序，非服务端逻辑 |

## 四、战争地图（war1）

| 文档断言 | 状态 | 证据 / 出入 |
|---|---|---|
| 主城（五角星）可部署，小城市只能驻扎 | ✅ | `capital_battlefield_ids()` / `initial_city_owners()`（`lifecycle.py`） |
| 红绿紫=苏美德、灰=中立、经过即占领 | ✅ | `_capture_city_events` 途经占城（`tick.py`） |
| 灰色区域和海面行军速度大幅降低 | ✅ | `WAR_SEA_DISTANCE/SF` + `WAR_NEUTRAL_SPEED_FACTOR`（`at_movement.segment_seconds`） |
| 胜利=夺 15 主城；或一方失全部主城后比较剩下两方主城数 | ⚠️ | `check_victory` **只实现「占领首都数 ≥ needed」一支**；`WAR_POINTS_VICTORY` 默认 **9**（文档 15）；「一方丢光主城→比较剩下两方」整支未实现 |
| 部署条件：主城非交战 + 周边有己方非交战城 | ⚠️→✅ | **2026-09-08 修正**：原实现把 war1「就近原则」误读为部署门槛（周边无己方非交战城 → 221 拒单）——官服开局仅首都 owned、周边全中立，若为硬条件开局无法部署任何 AT，且客户端主城候选仅按阵营拥有筛选无周边判定。已改为「就近原则=出生点分布池」：主城非交战即可部署，落位 random.choice(主城 ∪ 周边非交战己方城)，周边无己方城时落主城（at_ops.resolve_deploy_battlefield，回归 test_war_at_supply D2d/D2e 改断言） |
| 排队取号 / 一键部署按短称，富余放前 | 🔲 | 客户端排队，非服务端 |

## 五、士气（war1）

| 文档断言 | 状态 | 证据 / 出入 |
|---|---|---|
| 初始 100，随时间恢复 | ✅ | `gained_per_minute=0.15`（`war_campaign/common.py`） |
| 胜扣 10 / 失败·被击退·撤退扣 20 | ✅ | `WAR_MORALE_LOST_PER_BATTLE=20` / `WAR_MORALE_GAINED_PER_WIN=10`（`mm_config.py`） |
| 士气低于 25 遇敌即降 | ✅ | `surrender_limit=25`（`war_campaign/common.py`） |

## 六、战斗判定（war1）——出入最集中

| 文档断言 | 状态 | 证据 / 出入 |
|---|---|---|
| 三种交战类型（城防/城攻/野外遭遇）+ 野外 5 分钟准备期 | ⚠️ | 实现只有统一「到达敌城→war_battles 成局」（`_check_battle_formation`），**未细分城防/城攻/野外遭遇三种** |
| 准备期无玩家：按 144 人（单点）/216 步兵（三点）判定，不足败北损 25% + 扣 20 士气 | ⚠️ | `automission` 用**剩余资源总量比较 + %损耗**（`WAR_AUTOMISSION_*_LOSS`），**无 144/216 阈值**；超时 3600s 而非 4:30 |
| 城攻需进攻方 216 步兵，5 分 30 秒约判定 | ⚠️ | **无 216 阈值、无比分 5:30 判定** |
| 三国图（5 分钟内三方同图触发） | 🔲 | **未实现**；撮合侧「第三阵营挡」。2026-09-08 考证：官方**确实支持三方同图一局**（Gamepressure 官方时代指南原文 "all three sides of the conflict can take part in a single battle - if that happens, all of them are present on the battlefield"；官网 Campaign 描述成局条件 "two or more hostile Assault Teams" 无上限 2，第三方 AT 到场是**加入既有会战**）——battle 行 `factioncount` (f9, REQUIRED uint32) 即 N 方计数遗迹，两方制不需要此字段。但**三方局战斗内出生侧如何分配无任何资料**（Encounter 图 3 出兵点是否对应三阵营未证实），客户端对 factioncount/excludedFactionId 的 UI 消费点为零。分层结论：协议层可对齐（成局不裁剪第三阵营 AT、factioncount 如实计数、excludedFactionId 按局填），战斗内落位保持「第三阵营玩家 join 挡」直至 VPS 验证（详见附录条目 4） |
| 参战胜利 30 经验/扣 10 士气、失败 5 经验/扣 20 士气 | ✅ | `battles*5 + battlesWon*25`（`tick.py`）≈胜 30/败 5；士气 ±10/20 一致 |
| **系统判定胜负不提供经验** | ⚠️ | `automission` 结算**也发 XP + warfunds**（调 `settle_war_battle`），与「系统判定不给经验」相左 |
| 撤退惩罚：无经验无战币、损 25% 编制 + 扣 20 士气、撤退方向随机沿友方路线、被包围无法撤退→全歼 | ⚠️ | `handle_retreat_command_node_request` **只置 is_deployed=0 回首都，无任何惩罚/无包围判定/无随机撤退方向/无全歼**（`handlers/war_at.py`） |

## 七、经验购买与战争迷雾（war2）

| 文档断言 | 状态 | 证据 / 出入 |
|---|---|---|
| 部队经验购买刷满/买等级 | ✅ | `handle_complete_assault_team_rank_request`、`rank_from_xp` / `next_level_xp` |
| 战争迷雾（只能看己方附近） | ⚠️ | `WAR_FOW_GRID_*=0` **默认关闭=全图可见**，文档迷雾当前未启用 |
| 各兵种视野/观察力/攻击半径/运输半径 | ✅ | 模板 `view_range`/`attackradius`/`transportradius` 下发（`wardb.py`） |

## 八、飞机 / 机场（war2）

| 文档断言 | 状态 | 证据 / 出入 |
|---|---|---|
| 飞机随机出现在主城附近机场、机场间移动、作战半径红圈、战后回原机场 | ✅ | `nearest_airfield`/`is_airfield`/`air_commandnode_base` + `check_radius_move` |
| 飞机与驾驶员分开补给（机场不同时等待汇合） | 🔲 | 未见该拆分逻辑 |

## 九、伞兵（war2）

| 文档断言 | 状态 | 证据 / 出入 |
|---|---|---|
| 伞兵部署机场、空投落地变步兵、机飞回 | ✅ | `is_paradrop_unit` + 落地 `convert_to` `*_on_ground` 变体（`tick.py`）；**2026-09-08 修 2 处**：① `upgrade_allowed` 原 `convert_to == 目标` 分支放行「伞兵升级成落地变体」（真实表 f11 是空降⇄落地互转字段 108⇄114/111⇄112，非升级链；客户端 [AssaultTeamUpgradeOptions.as:37](../../swf_unpacked/as3_desktop_full/scripts/dk/reto/heroesandgenerals/util/AssaultTeamUpgradeOptions.as#L37) 显式排除 on_ground、升级选项=parenttemplateid 挂靠）→ 升级后 transportradius 变无限哨兵、`is_radius_unit=False`、airbase 行消失 → **伞兵从此永远步行**（玩家报障根因）；② 落地变体无转回机制（官服 114.f11=108 双向互转；war2.md「伞兵机飞回」）→ 撤退回待命时自动转回原型恢复空降能力（`war_at.py` retreat handler） |
| 5:30 无玩家时进攻方需凑 288 名伞兵判定成功；防守不足 216 则胜；双方满足则 6 小时后少者败 | ⚠️ | **无 288/216 阈值**，automission 统一按资源比结算 |

## 十、加入对局（war2）

| 文档断言 | 状态 | 证据 / 出入 |
|---|---|---|
| 已开战图可加入、兵种需匹配 | ✅ | war battle → mission record + JoinMM 路由（`tick.py`） |
| 人数上限 18:18（三点）/13:13（单点）/12:12（Colmar）| ⚠️ | 战争模式 `max_players` 统一 18（`mm_config.py`），未按单点/三点分人数上限 |
| 冲突地图命名「城市-方向字母」 | 🔲 | 客户端本地，非服务端 |

---

## 结论 / 主要出入清单

需决策优先级的核心出入（均落在 war1 第六组战斗判定与战争地图胜利）：

1. **战斗判定数值**：文档 144/216/288 人阈值 + 4:30/5:30 时间窗 + 25% 编制/20 士气 当前均未实现，统一退化为「资源总量比较 + %损耗」的 automission。
2. **撤退惩罚与包围/全歼**：当前撤退零惩罚，无包围判定、无随机撤退方向。
3. **胜利条件**：只实现「占领 ≥N 首都」一支（默认 9 而非 15）；「一方失光主城后比较剩下两方」未实现。

其余差异：三国图未实现、系统判定也发经验、指挥点与文档「将军/兵种限制」口径差异、战争迷雾当前关闭。
部队编制/等级解锁等数值因 macOS 无 WarDatabase，需 VPS 核实。

### 会战生命周期连带 bug（2026-09-08 三国局核查中发现，待修）

1. **第三阵营 AT 误入战且永久卡死**：`_check_battle_formation` 的 enemies 判定
   `faction != 自己` 不分敌我——第三阵营 AT 到达交战城会被标 state=2 入战，但它
   不在 wb['factions'] 名单 → 结算遍历名单时跳过 → 战斗结束永不释放（state=2
   挡移动）。官方语义：两方局第三阵营 AT 到场应视为无关方正常驻扎。
2. **结算不释放任何幸存 AT**：`settle_war_battle` 对幸存者只回写
   resources/morale/XP/rank，只有士气≤25 投降分支才置 state=0——普通幸存者
   战后永远 state=2，移动被 `RC_COMMANDNODE_IN_BATTLE` 挡死（打过一仗的 AT
   全部不能再移动/推进，VPS 应已可复现「AT 卡在会战中」）。
3. **撤退不重置 state**：`handle_retreat_command_node_request` 只设
   is_deployed=False，state 残留 2 继续拦截后续移动。
4. **结算后无「再成局」**：胜方占城后不检查城里是否还驻有敌对 AT →
   US 胜占城后与 SU AT 同城对峙永久僵持（连环三国局断链）。
   连环三方局的官方时序：美 vs 德 → 战毕美胜占城 → 城内 US+SU 对峙 →
   立即触发 US vs SU 新会战（war1「三国图」的观感来源）。
   修复口径：A（第三阵营不入战）+ B（结算释放幸存者 state=0）+
   C（battle 行 factioncount/excludedFactionId 如实填）+
   D（结算尾部对会战城市再跑一次成局判定）+ retreat 补 state=0。

---

## 附录：war1/war2 文档 vs 原版客户端核对（2026-09-08 反编译实锤）

对 war1/war2 文档本身与原版游戏的出入做二次核对（证据：`swf_unpacked/as3_desktop_full`
反编译源码 + `HnG/texts/HnG_Strategy_en.xml` 官方文案）。

### 文档可信（与原版一致）
- 胜利两支：官方文案 `Capture {0} of {1} major cities to win` +
  `Hold most major cities upon faction defeat`；MajorCityModel.as:250-252 从
  `war.pointsNeededForVictory`（war 行字段）读动态值——15 非常数。
- 野外遭遇=移动途中（补给线）相遇：`battle` 行 schema 带
  `mapEntityId+mapEntityTypeId+position(float)`；MissionUtilities.as:42-56 中
  encounter/skirmish 任务的 map entity 是 SUPPLYLINE（命名 `Encounter near %s` /
  `Capture & Hold near %s`，城市-出兵点缩写）；BattleMarkingView 支持在城市/
  出兵点/补给线三实体渲染战斗标记（补给线标记可点击）。
- 城防/城攻=同一场城战两侧：官方仅 `MISSION_TYPE_ASSAULT=1`。
- 士气模型：`Morale breaks at:` / `Full morale in:` /
  `was fully defeated due to low morale and needs to be re-deployed`。
- 攻守角色：`MissionInfo.defenderFactionId`（REQUIRED, f8）与
  「到达方=攻方、城 owner=守方」吻合。

### 文档与原版出入（文档不准）
1. **伞兵不是交战类型**：官方 missiontype 共 8 种（AREA_RECONNAISSANCE=0 /
   ASSAULT=1 / DESTRUCTION=2 / SKIRMISH=3 / SUPPLY=4 / ENCOUNTER=5 /
   TANK_ENCOUNTER=6 / PILOT_ENCOUNTER=7 / RECON_ENCOUNTER=8），无 paradrop；
   伞兵攻城在类型上就是 Assault，war2 的「288 伞兵」是 automission 判定规则。
2. **野外遭遇两子类**：Encounter=纯步兵（"Only infantry can play"）与
   Skirmish=步/侦/装混编；war1 统称「野外遭遇」丢了这层区分。
   TANK/PILOT/RECON_ENCOUNTER 三个变体客户端无消费点（纯服务端数据）。
3. **战斗可发生在出兵点（ACCESSPOINT）**：BattleMarkingView 三实体均支持。
4. **三国图考证结论（2026-09-08 更新，此前判断部分推翻）**：早前依据
   `battle.excludedFactionId` 字段推断「战斗恒 2 阵营制、三方同图不存在」——
   该推断**过于绝对**。新证据：
   - 官方时代指南（Gamepressure 2016）原文明确 "all three sides of the conflict
     can take part in a single battle - if that happens, all of them are present
     on the battlefield"——三方同图一局在官方存在过；
   - 官网 Campaign 描述成局条件为 "When two or more hostile Assault Teams
     attack each other, a mission is created"——无上限 2，第三方 AT 到场加入
     既有会战；
   - battle 行 `factioncount` (f9, REQUIRED uint32) 是 N 方计数遗迹，两方局
     用 excludedFactionId (f10) 填被排除第三方，两方局与三方局并存、字段按局填。
   **仍然成立的保守面**：最终版客户端对 factioncount/excludedFactionId 的
   UI 消费点为零（全反编译 grep 仅命中 battle.as 自身定义）；三方局的战斗内
   出生侧分配无任何资料。故实施口径：协议层如实填（factioncount=参战阵营数、
   两方局 excludedFactionId=第三方、三方局=0），战斗侧第三阵营玩家 join
   闸保持不动，待 VPS/录像验证出生侧后再议全量开放。
5. **144/216/288 与 5:00/5:30/6h 数值**：纯服务端规则，客户端零证据，无法证实/证伪。
6. `battle.activationTimeStamp`（REQUIRED f12）官方语义应为准备期激活时刻
   （当前实现填 started_at）。