# 战争服模块瘦身重构计划

> 2026-09-02 调研成文。目标：在不改变任何线上行为的前提下，拆解服务端巨型模块，降低导航/修改/评审成本。
> 参照先例：web 管理端 routers→services 拆分（commit 8e5a0a1）。

## 一、现状诊断

服务端共约 27k 行 Python（根目录 14.6k + handlers 12.4k），问题集中在：

| 模块                       | 行数   | 问题                                                                                                                                                                                                           |
| ------------------------ | ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `player_store.py`        | 3912 | 单一 `PlayerStore` 类承载 10+ 职责域：账号、角色、载具/迷彩、武器/弹药、战报统计、战斗通行证、邮件、兑换码/经济道具、小队/军团。所有方法挂一个类上，事务模板代码大量重复                                                                                                             |
| `matchmaking.py`         | 2556 | 至少 5 个正交职责：① 匹配池/撮合 ② 战斗服进程生命周期（spawn/端口/健康检查/回收）③ 天气/资源池委托 ④ AI 补位任务 ⑤ 请求处理入口。另有 `COMBAT_INSTANCES`/`PENDING_MATCHES`/`_MM_MISSION_SQUADS`/`_MM_MISSION_DETAILS` 等模块级全局 dict，是 game\_server/handlers 的隐式共享点 |
| `handlers/squad.py`      | 1513 | 单文件多消息族混杂                                                                                                                                                                                                    |
| `handlers/combat.py`     | 1490 | 同上                                                                                                                                                                                                           |
| `handlers/characters.py` | 1376 | 同上                                                                                                                                                                                                           |
| `handlers/weapons.py`    | 1365 | 同上                                                                                                                                                                                                           |
| `handlers/vehicles.py`   | 1059 | 同上                                                                                                                                                                                                           |
| `wardb.py`               | 1474 | 静态表访问与派生映射逻辑混合                                                                                                                                                                                               |

**有利条件**：

- 39 个测试文件、无任何 monkeypatch（grep 实锤 0 处），测试直接注入状态调用函数 → 拆分不破坏 patch 目标；

- 已有清晰分层先例：`protocol.py`（帧/wire）→ `messages.py`/`warserver_messages.py`（构建）→ `handlers/` + `matchmaking` + `game_server`（处理）→ `realtest_server.py`（dispatch 编排）；

- 每个文件头部有职责注释，边界认知已存在。

**风险点**：

- matchmaking ↔ game\_server 互查全局状态（`PMS_TO_SESSION`、`_session_by_player`），拆分不当会引入循环 import；

- macOS 环境 `test_mm_latejoin.py` BattleInfo/Armed 2 例必失败（无 WarDatabase），属环境问题，基线须先记录。

## 二、重构原则

1. **纯移动，不改行为**：只搬代码，不改逻辑；任何行为变更单独成 commit。
2. **不留兼容层**：所有调用方（handlers、game\_server、web 端、tests）都在本仓库内，拆分后直接全局修改 import 到新路径，不保留 `player_store.py` 薄壳或 re-export——避免长期背着双重路径。
3. **小步提交**：每阶段独立 commit，全量 pytest 通过 + `realtest_server.py` 冒烟启动（`MM_SIMULATE_COMBAT=1`）后才进下一步。

## 三、分阶段计划

### 阶段 0：基线固化（半天）

- 本机全量 pytest，记录基线结果（预期除 2 例环境失败外全绿）；

- 冒烟启动 `realtest_server.py` 确认可运行；

- 生成当前 import 依赖图存档（`pydeps` 或 grep 汇总），作为拆分后对比依据。

### 阶段 1：player\_store.py → store/ 包（收益最大，优先做）

采用 **mixin 方案**：`PlayerStore` 类对外签名不变，但 import 路径直接切到 `from store import STORE, PlayerStore`，调用方一次性全局改 import（不保留 `player_store.py` 薄壳）。

```
store/
  __init__.py        # 组装 PlayerStore(Base, Players, Characters, Equipment, ...)
  base.py            # 连接池、事务上下文、序列同步、条件归一化（原 L729-889）
  players.py         # 账号域（L889-997）
  characters.py      # 角色域（L997-1135）
  equipment.py       # 载具/迷彩/升级件/弹药 + 武器/配件（L1137-1690，可再拆 vehicles/weapons 两文件）
  battle_reports.py  # 战报/统计/生涯（L1691-1855）
  battle_pass.py     # 通行证（L1909-2010）
  mail.py            # 邮件（L2168-2240）
  economy.py         # 每日奖励/兑换码/债券/会员（L2393-2529）
  social.py          # 小队/军团/徽章（L2783-3109）
```

- 调用方（handlers/\*.py、game\_server.py、player\_service.py、web 端、tests）grep 全量改 `from player_store import` → `from store import`，一个 commit 完成；

- 各 mixin 内部只通过 `self`（Base 提供的 pool/txn）协作，禁止互相 import；

- 事务模板可顺手抽 `async with self.txn():` 助手（base.py 内，属基础设施不属行为变更）。

### 阶段 2：matchmaking.py 职责分离

```
combat_instances.py   # 战斗服生命周期：claim/spawn/watch/alive/release、端口分配、
                      # COMBAT_INSTANCES/PENDING_MATCHES/COMBAT_READY 及 _ensure_combat_server
mm_state.py           # _MM_MISSION_SQUADS/_MM_MISSION_DETAILS/_MATCH_PMM_IDS 等全局 dict
                      # 收敛为显式 registry（模块级单例），game_server/handlers 统一从这里 import
mm_weather.py         # roll_weather/snow_r_args（~100 行，也可并入 mm_config）
mm_handler.py         # _handle_join/_handle_cancel/_handle_mission_details/handle_request
matchmaking.py        # 保留撮合核心：_try_form_all/_dispatch_match/_prune_match_factions/
                      # AI 补位/late join 判定
```

- 关键：`mm_state.py` 解决"全局 dict 散落 + 双向 import"——game\_server 不再 `import matchmaking` 拿状态，改 import mm\_state，消除循环依赖根源；

- 拆分后同样不留 re-export：调用方与测试中 `matchmaking._MM_MISSION_SQUADS`、`matchmaking.COMBAT_INSTANCES` 等引用直接改为 `from mm_state import ...` / `from combat_instances import ...`，一个 commit 全量切换。

### 阶段 3：handlers/ 巨型文件内部拆分

`handlers/__init__.py` 的 dispatch 表不变，纯文件内移动：

- `squad.py`(1513) → `squad.py`（小队 CRUD/成员）+ `squad_match.py`（SquadRequest/入队/阵营注册，这块恰是 08-27/08-28 修复重灾区，独立成文件便于回归）；

- `combat.py`(1490) → `combat.py`（局内状态/结算）+ `combat_join.py`（late join/rejoin/成局加入）；

- `characters.py`/`weapons.py`/`vehicles.py` → 各拆出 `*_purchase.py` 或 `*_equip.py`（购买/装备/解锁校验子域）；

- 每拆一个文件跑一次对应测试（test\_squad\_multiplayer / test\_mm\_latejoin / test\_purchase\_fixes / test\_dbloadout）。

### 阶段 4（可选，视前三阶段体感决定）

- `wardb.py`：拆 `wardb/`（`tables.py` 静态 CSV/XML 加载 + `maps.py` 派生映射 + `general.py` 等）；

- `player_service.py`(740)：观察阶段 1 落地后是否自然归入 store/services 层；

- `web_server.py`(807)：静态文件服务与注册逻辑已可再分，但优先级最低（独立进程、风险小收益也小）。

## 四、每阶段验收清单

- [ ] 全量 pytest 与阶段 0 基线一致（仅允许已知 2 例环境失败）；

- [ ] `HNG_GAME_DIR=../HnG MM_SIMULATE_COMBAT=1 .venv/bin/python realtest_server.py` 冒烟启动正常；

- [ ] `grep -rn "player_store\|import matchmaking"` 确认旧模块引用已清零（旧文件直接删除，不留薄壳）；

- [ ] 新旧模块 import 依赖图无循环（`python -c "import realtest_server"` 无警告）；

- [ ] 独立 commit，信息注明"纯移动无行为变更"，便于 revert。

## 五、明确不做的事

- 不改任何协议消息构建逻辑（warserver\_messages/messages 保持现状）；

- 不动 `hngserver.py`（已明确仅 codec 自测，等废弃不投资）；

- 不引入 DI 框架/插件系统等过度设计——保持"模块 + 函数"的朴素结构；

- 不与 VPS 待实测事项（出兵点方向、O 点假说分辨）同时进行，避免混淆回归来源。

