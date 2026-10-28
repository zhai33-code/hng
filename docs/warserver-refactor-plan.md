# 战争服模块瘦身重构计划

> 2026-09-02 调研成文；**2026-09-06 重调研全面修订**（原文档行数/行号区间已过时：计划成文后 4 天内目标文件继续膨胀 15-25% 且未启动任何拆分；本次修订刷新全部数据、补入 war_campaign.py 与 player_store 8 个遗漏域、修正阶段 2 失效前提、运行环境改为 Windows 口径）。
> 目标：在不改变任何线上行为的前提下，拆解服务端巨型模块，降低导航/修改/评审成本。
> 参照先例：web 管理端 routers→services 拆分（commit 8e5a0a1）；force_pool.py 从 matchmaking 拆出（2026-08-26）。
> **文中行号以 2026-09-06 工作区快照（commit 7e741bc）为准，执行时以符号名为准。**
> **2026-09-06 执行完毕**：阶段 0-3.5 全部落地并逐阶段回归零回归（见下"执行记录"）；阶段 4 未执行。

## 执行记录（2026-09-06 收尾更新）

| 阶段 | 内容 | commit | 阶段回归 |
| --- | --- | --- | --- |
| 0 | 基线固化（50 项结果 + 环境修正记录入 baseline 文档） | `90a65fb`（tag `pre-refactor`） | — |
| 1 | player_store.py(4557) → store/ 包（19 文件，BaseStore + 17 域 mixin） | `d682b1e` | 与基线一致 |
| 2 | matchmaking.py(3138) → combat_instances/mm_state/mm_factions/mm_weather/mm_handler + 撮合核心 | `6d99ca2` | 与基线一致 |
| 3a | combat.py(1902) → combat + combat_join | `c29259e` | 与基线一致 |
| 3b | squad.py(1632) → squad + squad_match + company | `52fd46f` | 与基线一致 |
| 3c | characters/weapons/vehicles → 各拆 `*_purchase.py` | `47ec785` | 与基线一致 |
| 3.5 | war_campaign.py(2603) → war_campaign/ 包（12 文件，153 符号 re-export） | `1b10fb5` | 与基线一致 |

- **收尾全量复跑**（HEAD=1b10fb5）：50 项中 5 项与基线"既有失败"完全一致，另 test_admin_adjust_score 因 SSH 隧道（127.0.0.1:15432）未开环境性失败——**零回归**，明细见 baseline 文档"重构后复跑结果"。
- **阶段 3 其余文件**（economy/social/shop/career，第三节条件句）：评估后**不拆**——四文件消息族各自内聚、边界清晰，拆分收益低于引入新文件的导航成本；后续膨胀再按同法处理。
- **阶段 4（可选）**：未执行，wardb/player_service/web_server 维持现状。

## 〇、环境说明（09-06 修订新增）

- git 仓库根 = `server/`（main 分支，与 origin 同步）→ 原"小步提交、随时回滚"原则可直接执行；
- Windows venv：`.venv\Scripts\python.exe`（Python 3.11.15，已装 asyncpg/PyJWT/cryptography 等运行时依赖，**未装 pytest**）；
- 测试全部为独立脚本模式：`tests\` 下 50 个 `test_*.py` 均可直接 `.venv\Scripts\python.exe tests\test_x.py` 运行（36 个带 `if __name__ == "__main__"`，14 个在文件底部 `asyncio.run(main())` 自执行，如 test_auth.py L134、test_matchmaking.py L281）；
- 其中集成类测试需要 PostgreSQL 和/或运行中的 realtest_server（test_e2e / test_regression / test_realdb / test_matchmaking / test_autorepair 等），纯离线测试占多数——阶段 0 须先分好类；
- 原文档"macOS 环境 test_mm_latejoin 2 例必失败"的说法作废，Windows 基线须重新实测确认。

## 一、现状诊断（09-06 复核数据）

服务端根目录约 20.4k 行 + handlers 约 16.5k 行（19 文件）。问题集中在：

| 模块 | 行数 | 问题 |
| --- | --- | --- |
| `player_store.py` | 4557 | 单一 `PlayerStore` 类 238 个方法、10+ 职责域；模块级 `_SCHEMA` 占 780 行（17%）。09-02 后新增战争域（L4077-4557，38 方法）等 |
| `matchmaking.py` | 3138 | 8 项正交职责（见下）；模块级全局 dict/发号器 20+ 个，是 game_server/handlers/war_campaign/web_server 的隐式共享点 |
| `war_campaign.py` | 2603 | **原文档漏列**。文件头自述三职责（状态机/war 行 29 字段/catalogue 聚合）之外还有 6 块 RTS 引擎（warmap 快照、AT 管理、AT 移动、补给队、tick+会战结算、automission/生产/增援队列） |
| `wardb.py` | 1684 | 急切构建的派生索引（import 即全量解析）与懒加载缓存表混合，7 类内容一文件 |
| `handlers/combat.py` | 1902 | 单文件多消息族混杂 |
| `handlers/squad.py` | 1632 | 同上（小队 CRUD + SquadRequest/入队 + 军团 Company 三族） |
| `handlers/characters.py` | 1556 | 同上 |
| `handlers/weapons.py` | 1442 | 同上 |
| `handlers/war_at.py` | 1349 | 同上（AT 生命周期 11 类请求） |
| `handlers/vehicles.py` | 1063 | 同上 |
| `handlers/social.py` | 1020 | 同上（好友/聊天/战争阵营） |
| `handlers/economy.py` | 1002 | 同上（每日奖励/兑换码/会员/加成/票券） |
| `handlers/shop.py` | 902 | 同上（商店回放表） |
| `handlers/career.py` | 963 | 同上 |
| `game_server.py` | 687 | 单函数 `handle_request`（L66 起）内联 6 类请求处理；单向重度依赖 matchmaking 的全局 dict 与**私有发号器**（L131/135/137/166/170/180 直接调 `_next_match_id` + `_MM_SQUAD_ID/_MM_METRICS_SESSION_ID/_MM_BATTLE_SESSION_ID`） |

**matchmaking.py 的 8 项职责**（09-02 为 5 项，新增 ③'⑤'⑥'⑦'）：

1. 匹配池编排（池数据结构+成组算法已在 `mm_pool.py`，编排/簿记仍在 matchmaking）；
2. 战斗服进程生命周期（spawn/端口池/健康检查/握手认领/回收）；
3. **war 会战成局（M2）**：battle_id 路由、`try_form_war`、`_init_war_mission_resources`；
4. 天气/资源池委托（force_pool 包装函数）+ AI 补位任务；
5. **重登 rejoin**（`push_relogin_rejoin`，09-06 新增）；
6. **单人隐式小队/自动凑队**（`_SOLO_SQUADS`、`_msg_auto_squad_*` 一族）；
7. **faction 表/统帅部/资源/队列行构建**（`_msg_faction_rows` 等 4 个，被 views/war_campaign 复用）；
8. 请求处理入口（Join/Cancel/GetMissionDetails 三类，realtest_server L163-166 路由）。

**matchmaking 模块级全局状态清单**（拆分收敛的对象，行号为现值）：

| 类别 | 符号（行号） | 外部引用 |
| --- | --- | --- |
| 战斗服实例 | `COMBAT_INSTANCES`(103)、`PENDING_MATCHES`(104)、`COMBAT_READY`(107)、`_PENDING_HANDSHAKE`(110) | war_campaign L2258（延迟）、tests |
| 战局登记 | `_MM_MISSION_SQUADS`(872)、`_MM_MISSION_DETAILS`(903)、`_MM_QUEUES`(905)、`_SOLO_SQUADS`(907)、`POOL`(911) | game_server、handlers/{squad,mission,combat,social}、web_server（惰性） |
| 会话映射 | `PMS_TO_MISSION`(1084)、`PMS_TO_SESSION`(1085) | game_server（十余处）、handlers/{mission,combat}、web_server |
| 发号器（单元素 list） | `_MM_MISSION_ID`(853)、`_MM_PMM_ID`(854)、`_MM_PMS_ID`(855)、`_MM_SOLO_SMS_ID`(857)、`_MM_METRICS_SESSION_ID`(862)、`_MM_BATTLE_SESSION_ID`(863)、`_MM_SQUAD_ID`(864)、`_MM_SQUAD_MEMBER_ROW`(865) | game_server L131/135/137/166/170/180 直接取用 |
| 仅内部 | `_MATCH_PMM_IDS`(906)、`_AI_FILL_TASKS`(912) | 无外部引用，可留在撮合核心 |

**有利条件**：

- 50 个测试文件、0 处 monkeypatch（grep 实锤），测试直接注入状态调用函数 → 拆分不破坏 patch 目标；唯一例外见"风险点 5"；
- 已有清晰分层先例：`protocol.py`（帧/wire）→ `messages.py`/`warserver_messages.py`（构建）→ `handlers/` + `matchmaking` + `game_server`（处理）→ `realtest_server.py`（dispatch 编排）；
- 已有两个成功拆分先例：force_pool.py（08-26，从 matchmaking 拆出资源池）、web/server routers→services（8e5a0a1）；
- 每个文件头部有职责注释，边界认知已存在。

**风险点**（09-06 重查，带行号）：

1. **matchmaking ↔ handlers 环**：handlers 侧顶层 import（mission L50、views L39、squad L89、combat L30、social L79-80、battlereport L35）；matchmaking 侧 5 处函数内延迟 import 反向压制（L185 `handlers.combat`、L1690/1717/2103 `handlers.squad`、L2159 `handlers.social`）；
2. **matchmaking ↔ war_campaign 环**：matchmaking L50 顶层 `import war_campaign`；反向 5 处延迟（war_campaign L623/2199/2248/2342/2414）；
3. **game_server → matchmaking 单向重度耦合**：顶层 import + 直读 `PMS_TO_SESSION`/`PMS_TO_MISSION`/`_MM_MISSION_SQUADS`/`_MM_MISSION_DETAILS` + **调用私有发号器**（game_server.py L131/135/137/166/170/180）——拆 mm_state 时必须同步改这些调用点；
4. **matchmaking 双重 STORE**：L36 `from state import STORE` 后被 L405-406 `STORE = None` + `configure()`(L410) 注入遮蔽——matchmaking 与 game_server 用的 STORE 实例不同源，拆分时须统一为单一路径；
5. **测试 patch 目标耦合**：`test_at_stats_rank.py` L116/253 直接 patch `player_service._PRICING_MAP` 与 `player_service.STORE`（`_charge_player` 读的是 player_service 命名空间的模块全局）——若移动 `_charge_player` 须同步改测试 patch 目标；
6. war_campaign → messages/state 的运行时反向依赖（`_push_kvcs`/`FLASH_SESSIONS`，L1977/2343/2415/2578 等多处延迟）——拆 war_campaign 包时这些边保留延迟或经统一 broadcast 模块中转。

## 二、重构原则

1. **纯移动，不改行为**：只搬代码，不改逻辑；任何行为变更单独成 commit。
2. **不留兼容层**：所有调用方（handlers、game_server、web 端、tests）都在本仓库内，拆分后直接全局修改 import 到新路径，不保留 `player_store.py` 薄壳或 re-export——避免长期背着双重路径。
3. **小步提交**：每阶段独立 commit（git 仓库根=server/），全量离线测试通过 + `realtest_server.py` 冒烟启动（`MM_SIMULATE_COMBAT=1`）后才进下一步。
4. **顺手修的缺陷独立提交**（09-06 新增）：拆分中发现的明确缺陷（`get_username` 双重定义 L2260/L3138、11 处 `import json as _json` 冗余、`add/spend_currency` 与 `get/set_currency` 分居两处、L1896 分节注释错位等）可顺手修复，但须与纯移动分开 commit 并在提交信息中注明。

## 三、分阶段计划

### 阶段 0：基线固化

- 测试分类清点：50 个 `test_*.py` 分为"离线可跑"与"需 DB/运行中服务器"（test_e2e/test_regression/test_realdb/test_matchmaking/test_autorepair 等集成类）；
- 用 `.venv\Scripts\python.exe` 逐个跑离线测试，结果（PASS/FAIL/跳过原因）记录到 `docs/baseline-2026-09.md` 作为此后各阶段回归对照基线；
- 冒烟启动确认可运行：PowerShell 下 `$env:MM_SIMULATE_COMBAT='1'; $env:HNG_GAME_DIR='..'; .venv\Scripts\python.exe realtest_server.py`；
- 集成类测试在本地 PostgreSQL 可用的前提下尽量也跑一遍入基线；
- `git tag pre-refactor` 固化回滚点。

### 阶段 1：player_store.py → store/ 包（收益最大，优先做）

采用 **mixin 方案**：`PlayerStore` 类对外签名不变，import 路径直接切到 `from store import STORE, PlayerStore`，调用方一次性全局改 import（grep 实证引用方：handlers 全部、game_server、player_service、matchmaking、state、web 端、tools、tests 中 `from player_store import`）。

**09-06 修正后的域表**（原表区间全部失效，且漏 8 个域；行号为现值）：

```
store/
  __init__.py        # 组装 PlayerStore(Base, Players, Characters, ...)，导出 STORE/PlayerStore/DEFAULT_DSN
  schema.py          # _SCHEMA 建表 DDL（L57-835，约 780 行，原表漏列）
  base.py            # 连接池/事务/序列同步：__init__/start/_normalize_conditions/
                     #   _resync_small_sequences/close/_conn/transaction（L836-995）
                     # 顺手项：L871 import wardb、L905 from state import log 可上提到模块顶部
  players.py         # 账号域（L996-1116）+ get_latest_character（L3144-3151）
  characters.py      # 角色域（L1117-1277，含勋带 XP/军衔徽章/军服变体入口）
  equipment_vehicles.py  # 载具/变体/升级件/弹药 + 军服变体/部件（L1278-1609）
  equipment_weapons.py   # 武器/变体/配件/示踪/弹药（L1610-1895）
  economy.py         # 货币 add/spend（L1898-1936）+ 经济道具全族（L2711-3137，427 行：
                     #   每日登录/兑换码/战争债券/会员/加成/票券）+ 归并 get/set_currency
                     #   （L1057-1083）——原表只列 136 行已严重失真
  battle_reports.py  # 战报/统计/生涯订单（L1937-2120）
  battle_pass.py     # 通行证（L2121-2247）
  identity.py        # 玩家身份/战争阵营（L2250-2318，含 09-03 新增 get/set_player_war_faction）
                     # 顺手项：get_username 双定义（L2260 被 L3138 覆盖）在此修掉
  social_graph.py    # 好友/屏蔽/举报（L2319-2451，原表漏列）
  mail.py            # 邮件（L2452-2545）
  chat.py            # 聊天频道（L2546-2640，原表漏列）
  missions.py        # 任务记录（L2641-2712，原表漏列）
  squads.py          # 小队（L3154-3337）
  companies.py       # 军团+徽章（L3340-3480）
  maintenance.py     # 角色/装备维护+仓库 depot+教程+账号设置+重置（L3483-4076，原表漏列）
  war_store.py       # 战争域：wars/war_cities/command_nodes/movements/war_battles/
                     #   warfunds/war_faction_resources/war_supply_queue（L4077-4557，
                     #   38 方法 481 行，09-02 后新增的重灾区，原表漏列）
```

- 各 mixin 内部只通过 `self`（Base 提供的 pool/txn）协作，禁止互相 import；
- 战争域 11 处 `import json as _json/_j` 冗余延迟 import 顺手清理（顶层已有 json）；
- 一个 commit 完成移动 + 调用方全量切换，`player_store.py` 删除。

### 阶段 2：matchmaking.py 职责分离

```
combat_instances.py  # 战斗服生命周期：_detect_combat_dir/_spawn/claim_pending_combat_instance/
                     #   notify_combat_ready/_combat_alive/_watch_proc_exit/_alloc_combat_port/
                     #   _stop_instance/release_match/_drain_pending_matches/_wait_udp_bound/
                     #   _ensure_combat_server（L65-401）
                     # 携带 COMBAT_INSTANCES/PENDING_MATCHES/COMBAT_READY/_PENDING_HANDSHAKE/_combat_seq
mm_state.py          # 显式 registry：_MM_MISSION_SQUADS/_MM_MISSION_DETAILS/_MM_QUEUES/
                     #   _SOLO_SQUADS/POOL + PMS_TO_MISSION/PMS_TO_SESSION + 8 个 _MM_* 发号器
                     # （含 _next_match_id 助手）。game_server/handlers/war_campaign 统一改从
                     #   这里 import，消除对 matchmaking 的单向重依赖与环
mm_factions.py       # faction/统帅部/资源/队列行构建（L469-632：_rival_faction/_faction_pair/
                     #   _msg_faction_rows/_msg_supreme_command_rows/_msg_faction_resource_rows/
                     #   _msg_faction_queue_rows + FACTION_ROW_IDS/FACTION_SUPREME_COMMAND*）
mm_weather.py        # WEATHER_PRESETS/SNOW_MASK + roll_weather + snow_f22_condition（L643-686）
                     # ★修正：原文档写的 snow_r_args 已不存在，现为 snow_f22_condition+SNOW_MASK
mm_handler.py        # 请求入口：_handle_join/_handle_cancel/_msg_mission_info_multi/
                     #   _msg_mission_details_response_multi/_handle_mission_details/handle_request
                     #   （L2545-3138）
matchmaking.py       # 保留撮合核心：_try_form_all/_dispatch_match/_prune_match_factions/
                     #   AI 补位/late join 判定/成局落地 _start_match/_push_player_into_mission/
                     #   小队路径/rejoin；资源池薄包装（L694-835）可评估并入 force_pool 或保留
```

- 关键：`mm_state.py` 解决"全局 dict 散落 + 双向 import"——game_server 不再 `import matchmaking` 拿状态与发号器（同步修改 game_server.py L131/135/137/166/170/180），handlers/war_campaign 的状态引用也统一切换；
- **统一双重 STORE**：去掉 matchmaking L405-406 的 `STORE = None` 遮蔽，全部走 `state.STORE` 单一路径（`configure()` 仅保留日志注入或整体废除，视引用情况定）；
- 拆分后不留 re-export：调用方与测试中 `matchmaking._MM_MISSION_SQUADS`、`matchmaking.COMBAT_INSTANCES` 等引用直接改为新路径，一个 commit 全量切换。

### 阶段 3：handlers/ 巨型文件内部拆分

`handlers/__init__.py` 的 dispatch 表不变，纯文件内移动（行数为 09-06 值）：

- `combat.py`(1902) → `combat.py`（局内状态/结算）+ `combat_join.py`（late join/rejoin/成局加入）；
- `squad.py`(1632) → `squad.py`（小队 CRUD/成员）+ `squad_match.py`（SquadRequest/入队/阵营注册，08-27/08-28 修复重灾区，独立成文件便于回归）+ `company.py`（军团 Company/徽章族）；
- `characters.py`(1556) / `weapons.py`(1442) / `vehicles.py`(1063) → 各拆出 `*_purchase.py`（购买/解锁校验子域）；
- `economy.py`(1002) / `social.py`(1020) / `shop.py`(902) / `career.py`(963)：观察体感，超千行且消息族混杂时同法拆分；
- 每拆一个文件跑一次对应测试（见第六节映射表）。

### 阶段 3.5：war_campaign.py 拆分（09-06 新增，原文档漏列）

2603 行、文件头自述三职责之外实为 10 块，拆 `war_campaign/` 包：

```
war_campaign/
  __init__.py       # 对外 re-export 原顶层符号（war_tick/ensure_war/build_war_proto 等被
                    #   matchmaking/handlers/realtest_server 引用的名字），内部各模块协作
  common.py         # _fmt_ts/_as_resources/_war_config/DEFAULT_MORALE/分类常量（L36-139 公共段）
  lifecycle.py      # 状态机：morale_defaults/capital_battlefield_ids/effective_points_needed/
                    #   initial_city_owners/_next_war_name/ensure_war（L36-179）+ check_victory
                    #   （L382-416，注意与 catalogue 段交错，拆分时并回）
  war_row.py        # war 行 29 必填字段：build_war_proto（L181-223）+ war_kvcs_proto（L226-232）
  catalogue.py      # catalogue 聚合：_list_wars/_faction_stats/_msg_catalogue*/build_*_protos
                    #   （L235-377）
  warmap_view.py    # warmap 快照行构建：MAP_ENTITY_*/msg_battlefieldstatus_row/
                    #   msg_commandnode_*/build_warmap_snapshot（L419-705）
  at_ops.py         # AT 管理业务：RC_* 常量/validate_create_at/template_*/underdog/upgrade_allowed/
                    #   resolve_deploy_battlefield（L708-981）
  at_movement.py    # AT 移动引擎：supplyline_adjacency/find_supplyline_path/at_speed/
                    #   start_movement/start_air_transport/check_radius_move 等（L982-1395）
  supply.py         # 补给队（L1397-1678）+ 增援队列 supply_queue_*（L2369-2596）
  battle_msgs.py    # 移动/战斗消息行：msg_transport_*/msg_air_transport_row/msg_battle_row
                    #   （L1679-1745）
  tick.py           # tick 引擎 advance_movements/war_tick/_capture_city_events/
                    #   _sweep_ended_war_leftovers（L1747-2036）+ 会战结算 settle_war_battle
                    #   （L2038-2227）
  production.py     # automission_tick/production_tick（L2229-2367）
```

- **循环依赖边界**：war_campaign 内 5 处 `import matchmaking`（L623/2199/2248/2342/2414）与 `messages._push_kvcs`/`state.FLASH_SESSIONS` 反向依赖——阶段 2 的 mm_state 落地后优先经它中转，否则各子模块保留函数内延迟 import（现状语义不变）；
- 调用方（matchmaking、handlers/{war_at,combat,views,auth}、player_service L413 延迟、realtest_server、6 个 test_war_*）import 路径改为 `war_campaign` 包名不变（`__init__.py` re-export 属包内组装，不算兼容层）。
- **实际落地差异**（依符号真实引用关系微调，纯移动原则不变）：
  - 地理工具群（`_bf_pos`/`_BF_POS_CACHE`/`supplyline_adjacency`/`_ADJACENCY_CACHE`/`find_supplyline_path`）从 at_movement 下沉 `common.py`（warmap_view/supply/tick 共用）；
  - `_faction_capital_bf` 归 `lifecycle.py`（与 capital_battlefield_ids 同域）；单位分类群（`is_reserve`/`is_aircraft`/`airfield_bfs`/`nearest_airfield` 等 + `_AIRFIELD_CACHE`）归 `at_ops.py`，at_movement 仅保留移动引擎；
  - 包内新引入的函数内延迟 import 实际 4 处：warmap_view→at_movement、supply→tick、tick→production、production→tick（tick↔production 互环只能延迟压制）；对 matchmaking/messages/state 的既有延迟依赖全部原样保留；
  - `__init__.py` re-export 全部 153 个顶层符号并配 `__all__`，调用方 import 零改动；tests 的 monkeypatch 同步改打定义子模块（lifecycle/tick/common/at_ops）——from-import 快照不回流子模块，patch 必须打定义处（详见 `1b10fb5` 的 tests diff）。

### 阶段 4（可选，视前几阶段体感决定）

- `wardb.py`(1684) 拆 `wardb/`，按 7 类内容：`tables.py`（_parse_wardb_table + 急切 _build_* 派生索引 L68-849）/ `queries.py`（纯查询函数 L314-1040 散布）/ `manifest.py`（目录扫描/CAS/唯一串 L856-994，含 import 时副作用）/ `combat_static.py`（BattleFieldSettings 静态数据 L1041-1317）/ `match_maps.py`（preset/gamemap/battlefield 链 L1298-1416）/ `rts.py`（RTS 表接口 L1418-1684）——注意 import 时急切执行的副作用顺序不能变；
- `player_service.py`(779)：观察阶段 1 落地后是否自然归入 store/services 层；若移动 `_charge_player`/`_PRICING_MAP`，**必须同步改 test_at_stats_rank.py 的 patch 目标**；
- `web_server.py`(1121)：独立进程、风险小收益也小，优先级最低。

## 四、每阶段验收清单

> 2026-09-06 收尾：阶段 0-3.5 各阶段均已按本清单逐项验收通过，执行明细见文首"执行记录"。

- [ ] 全量离线测试与阶段 0 基线逐项一致（对照 `docs/baseline-2026-09.md`；集成类测试在 DB 可用时同跑）；
- [ ] 冒烟启动正常：`$env:MM_SIMULATE_COMBAT='1'; .venv\Scripts\python.exe realtest_server.py`（PowerShell 语法）；
- [ ] 旧模块引用清零：阶段 1 后 `grep -rn "player_store"`；阶段 2 后 `grep -rn "import matchmaking"`（应只剩 mm_handler/撮合核心自身与 tests 中合法状态引用）；阶段 3.5 后 `grep -rn "from war_campaign import"` 调用方无需改（包名不变）但内部无 `war_campaign.py` 残留；
- [ ] `.venv\Scripts\python.exe -c "import realtest_server"` 无警告无循环 import 报错；
- [ ] 独立 commit，信息注明"纯移动无行为变更"（顺手修缺陷的独立 commit 除外），便于 revert。

## 五、明确不做的事

- 不改任何协议消息构建逻辑（warserver_messages/messages 的 wire 输出保持现状）；
- 不动 `hngserver.py`（仅 codec 自测，等废弃不投资）；
- 不引入 DI 框架/插件系统等过度设计——保持"模块 + 函数"的朴素结构；
- 不改变任何 DB schema（`_SCHEMA` 原样搬到 store/schema.py）与消息 wire 格式；
- 不与 VPS 待实测事项（出兵点方向、O 点假说分辨）同时进行，避免混淆回归来源。

## 六、各阶段回归测试映射（09-06 新增）

| 阶段 | 主回归测试（tests\ 下直跑） |
| --- | --- |
| 1 store/ 包 | test_transactions、test_battlepass、test_daily_rewards、test_storebundle_delivery、test_membership_queue、test_booster_table_push、test_mail_push、test_depot_fixes、test_depot_ammo、test_autorepair、test_career_orders、test_admin_adjust_score、test_rename_validation、test_default_camo_unlock、test_equipment_wear、test_mod_class_strip、（DB 可用时）test_realdb、test_regression、test_e2e |
| 2 matchmaking 拆分 | test_matchmaking、test_mm_pool、test_mm_latejoin、test_mm_multiplayer、test_mm_latejoin_e2e（需服务器）、test_auto_squad_disband、test_battlefield_stagedpos、test_snow_config、test_player_ids |
| 3 handlers 拆分 | test_squad_multiplayer、test_squad_invite_implicit、test_purchase_fixes、test_at_stats_rank、test_stats_type6、test_progression_level、test_player_report、test_combat_exit_finish、test_settlement_parse、test_request_coverage |
| 3.5 war_campaign 拆分 | test_war_campaign、test_war_at_supply、test_war_supply_column、test_war_movement、test_war_faction_join、test_rts_war_db、test_wardb_rts |
| 4 wardb（可选） | test_wardb_rts、test_rts_war_db、test_dbloadout、test_codec、test_battlefield_stagedpos |
