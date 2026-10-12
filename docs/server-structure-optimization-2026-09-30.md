# 服务端项目结构优化建议（2026-09-30）

## 1. 文档目的

本文只分析和规划当前 Python 游戏服务端目录，不涉及：

- 外层客户端文件、战斗服文件、SWF、DLL、IDA 数据库和逆向工作目录；
- `web/frontend/` 前端工程及其静态素材；
- 协议行为、数据库业务规则和游戏数值的功能性修改。

本轮只落文档，不修改任何 `.py` 文件。目标是在保持现有协议行为和部署方式的前提下，逐步降低服务端模块体积、循环依赖和隐式全局状态带来的维护成本。

## 2. 当前结构概览

当前服务端已经完成过一轮有价值的领域拆分：

- 请求处理已由单一入口拆入 `handlers/`；
- 数据访问已拆入 `store/`；
- 战争逻辑已拆入 `war_campaign/`；
- 匹配逻辑已有 `mm_config.py`、`mm_pool.py`、`mm_state.py`、`mm_weather.py` 等辅助模块；
- `realtest_server.py` 已主要承担进程启动、连接生命周期和顶层分派。

目前的主要问题不是“完全没有分层”，而是拆分后仍存在若干超大模块、跨模块私有符号引用、函数内延迟导入和全局可变状态。因此下一阶段适合做渐进式边界收敛，不适合一次性重写或整体搬迁目录。

## 3. 主要结构问题

### 3.1 服务端模块仍平铺在仓库根目录

根目录同时放置入口、协议层、运行时状态、匹配、战斗实例、静态数据库解析和玩家服务，例如：

- `realtest_server.py`
- `game_server.py`
- `protocol.py`
- `state.py`
- `messages.py`
- `player_service.py`
- `matchmaking.py`
- `combat_instances.py`
- `wardb.py`

这些文件属于不同职责，但目录层级没有表达依赖方向。代码只能依赖文件名约定和开发者记忆判断模块边界。

此外，`realtest_server.py` 通过修改 `sys.path` 保证根目录模块可导入，说明当前服务端还不是一个标准 Python package。短期内这不影响运行，但会增加测试、IDE、类型检查和后续多入口部署的复杂度。

### 3.2 少数大文件重新成为聚合点

当前优先关注的文件包括：

| 文件 | 当前职责混合情况 | 建议优先级 |
|---|---|---:|
| `matchmaking.py` | 消息构建、排队撮合、任务状态、重连和战斗服编排 | P1 |
| `handlers/war_at.py` | AT 移动、撤退、补给、运输等多条业务链 | P2 |
| `handlers/combat.py` | 战报解析、装备磨损、奖励、战争结算和战局收尾 | P1 |
| `wardb.py` | 多类 WarDatabase 解析、索引构建和查询接口 | P0 |
| `war_campaign/tick.py` | 战争 tick、移动推进和战斗结算 | P2 |
| `mm_handler.py` | 匹配请求解析与加入流程编排 | P2 |

大文件本身不是问题；真正的问题是其中包含多个变化原因。协议字段变化、结算规则变化、状态生命周期变化会同时触碰同一个文件，导致评审和回归范围不断扩大。

### 3.3 模块边界被私有符号穿透

多个模块会直接导入其他模块以下划线开头的常量、字典或函数，例如：

- 消息层读取 `wardb.py` 的内部索引；
- 战斗实例层读取 `mm_state.py` 的内部状态表；
- handler 之间直接调用内部辅助函数；
- 业务 handler 直接使用 `messages.py`、`player_service.py` 的私有实现函数。

这意味着文件虽然拆开，接口仍未稳定。被导入模块无法安全调整内部实现，也难以在单元测试中替换依赖。

建议逐步建立公开接口，不要求一次性移除所有下划线符号：

```python
# 不再向外暴露内部字典
def get_mission_details(mission_id: int) -> MissionDetails | None:
    ...

def ribbon_level_from_xp(ribbon_id: int, xp: int) -> int:
    ...
```

公开接口建立后，再按调用链逐步迁移旧引用。

### 3.4 `state.py` 承担过多隐式依赖

`STORE`、会话注册表、当前玩家上下文、目录和日志等运行时对象集中在 `state.py`。其优点是启动简单、兼容当前单进程模型；缺点是 handler 的真实依赖不会出现在函数签名中。

本轮不建议直接删除 `state.py`，也不建议一次性引入大型依赖注入框架。可以先增加轻量运行时上下文：

```python
@dataclass
class AppContext:
    store: PlayerStore
    sessions: SessionRegistry
    combat_instances: CombatInstanceRegistry
    wardb: WarDatabase
```

新拆出的服务函数显式接收 `AppContext` 或其中的具体依赖；旧 handler 继续使用兼容层。这样可以渐进迁移，不改变现有启动方式。

### 3.5 消息构建、业务规则和副作用未完全分离

若一个 handler 同时执行以下步骤，函数会很快膨胀：

1. 解析 protobuf；
2. 查询或修改数据库；
3. 修改匹配/战局内存状态；
4. 构建响应消息；
5. 向其他会话广播；
6. 记录日志和触发生涯进度。

建议拆成三层，但不必为每个请求机械地创建三个文件：

```text
handler        负责协议输入输出、鉴权和错误映射
service        负责业务流程和事务边界
message/view   负责 protobuf/KVCS 构建
```

优先在最长、回归风险最高的请求链中使用该模式，简单 ACK handler 保持现状即可。

### 3.6 测试目录缺少执行边界

现有 `tests/` 同时包含：

- 可独立运行的逻辑测试；
- 真实 PostgreSQL 测试；
- 要求服务端已启动的端到端测试；
- 要求 Windows、客户端或战斗服环境的测试；
- 运维检查和一次性诊断脚本。

这会让“运行测试”缺少明确含义。建议先通过 pytest marker 分类，而不是立即大规模移动文件：

```text
unit            无外部服务，可快速执行
db              需要测试数据库
e2e             需要服务端进程
combat          需要战斗服
windows         仅 Windows 可运行
```

分类稳定后，再考虑将其物理移动到 `tests/unit/`、`tests/integration/` 和 `tests/e2e/`。

### 3.7 工程配置分散

当前 Python 依赖主要由 `requirements.txt` 管理，测试入口和开发工具配置没有统一承载文件。建议增加 `pyproject.toml`，统一管理：

- Python 版本声明；
- pytest 配置和 marker；
- Ruff/格式检查规则；
- 可选的类型检查配置；
- 服务端包元数据和命令行入口。

运行依赖、管理工具依赖和开发测试依赖应分组。传递依赖不必作为直接依赖长期手工维护，除非离线部署流程确实要求完整锁定；若有此要求，应额外生成锁定文件，而不是混合“直接依赖声明”和“离线依赖快照”。

### 3.8 数据库 schema 与业务存储代码耦合

`store/schema.py` 同时承担建表/演进定义。随着功能继续增加，单文件 schema 会越来越难确认变更顺序，也难以回答某个部署实例已执行到哪个版本。

建议保留当前自动建库体验，但把 schema 演进改为有序迁移：

```text
store/migrations/
├── 0001_initial.sql
├── 0002_battle_reports.sql
├── 0003_login_events.sql
└── ...
```

数据库中保存当前 schema version；服务端启动时按顺序执行未应用迁移。迁移文件一旦发布只追加、不修改，避免不同部署环境发生漂移。

## 4. 建议的目标结构

这是方向性结构，不建议一次性把全部文件搬完：

```text
src/hng_server/
├── app/
│   ├── main.py                 # 进程启动和生命周期
│   └── context.py              # AppContext 组装
├── core/
│   ├── config.py
│   ├── logging.py
│   └── protocol/               # Chunk/protobuf wire 基础能力
├── transport/
│   ├── game_tcp.py             # 15110 连接与顶层分派
│   ├── ping.py
│   └── irc.py
├── players/
│   ├── handlers/
│   ├── service.py
│   └── messages.py
├── matchmaking/
│   ├── handlers.py
│   ├── service.py
│   ├── pool.py
│   ├── rejoin.py
│   └── messages.py
├── combat/
│   ├── handlers.py
│   ├── instances.py
│   ├── settlement.py
│   ├── wear.py
│   └── messages.py
├── war/
│   ├── campaign/
│   ├── assault_teams/
│   └── messages.py
├── wardb/
│   ├── loader.py
│   ├── models.py
│   ├── progression.py
│   ├── equipment.py
│   └── battlefield.py
└── store/
    ├── migrations/
    └── repositories/
```

该结构强调业务域内聚，而不是简单地按“handlers/services/models”建立三个巨型公共目录。

## 5. 分阶段落地计划

### 阶段 0：建立约束和基线

目标：确保后续重构只改变结构，不改变协议行为。

1. 增加 `pyproject.toml`，先只配置 pytest 和项目元数据；
2. 为测试标注运行环境，形成快速测试集和完整回归集；
3. 记录当前关键入口、端口、后台任务和启动顺序；
4. 固化关键协议响应与 KVCS 推送快照；
5. 约定新代码不得新增跨模块私有符号引用。

验收标准：现有启动命令仍可用，既有测试结果不发生非预期变化。

### 阶段 1：低风险机械拆分

目标：优先移动纯函数和静态数据，减少大文件体积。

1. 按既有计划拆分 `wardb.py`；
2. 从 `matchmaking.py` 提取纯消息构建函数；
3. 提取 handler 共用的字符串、时间和数值解析工具；
4. 为跨模块访问的内部字典增加公开查询函数；
5. 每次只拆一个边界，保留旧模块转发接口。

验收标准：调用方可暂时不改或只改 import；协议字节结果保持一致。

### 阶段 2：拆业务流程

目标：让 handler 只承担协议适配。

1. 从 `handlers/combat.py` 提取装备磨损服务；
2. 提取战报解析和战局结算服务；
3. 从 `matchmaking.py` 提取 late join/rejoin 生命周期；
4. 拆分 `handlers/war_at.py` 中撤退、补给和运输流程；
5. 拆分 `war_campaign/tick.py` 中移动推进与战斗结算。

验收标准：单个 handler 的主要流程能在不构造完整网络连接的情况下测试。

### 阶段 3：收敛运行时状态

目标：减少对 `state.py`、`mm_state.py` 内部数据的直接读写。

1. 引入 `SessionRegistry`；
2. 引入 `CombatInstanceRegistry`；
3. 建立 `AppContext`；
4. 新服务函数显式接收依赖；
5. 保留旧全局变量作为过渡代理，调用方迁完后再删除。

验收标准：核心 service 测试可以使用内存 fake，不依赖进程级全局状态。

### 阶段 4：标准包结构

目标：完成 `src/hng_server/` 迁移。

1. 先迁移无副作用的基础模块；
2. 再迁移领域服务；
3. 最后迁移入口和运行时组装；
4. 提供稳定命令，例如 `python -m hng_server`；
5. 在过渡期保留 `realtest_server.py` 薄包装，避免部署脚本立即失效。

验收标准：不再需要手工修改 `sys.path`，从任意工作目录均能通过标准入口启动。

### 阶段 5：数据库迁移版本化

目标：让数据库结构演进可追踪、可重复。

1. 为当前 schema 建立基线版本；
2. 增加 schema version 表；
3. 将后续 ALTER/CREATE 改为只追加迁移；
4. 增加空库升级和旧版本升级测试；
5. 保留启动时自动迁移体验。

## 6. 优先级清单

### P0：下一轮即可执行

- 拆分 `wardb.py`，沿用已有重构方案；
- 增加 `pyproject.toml` 和 pytest markers；
- 建立“不再新增跨模块私有符号引用”的约束；
- 从 `matchmaking.py` 提取纯消息构建函数；
- 为数据库 schema 建立迁移基线设计。

### P1：结构收益最高

- 拆分 `handlers/combat.py` 的磨损、结算和收尾流程；
- 拆分匹配重连生命周期；
- 为会话和战斗实例建立 Registry；
- 引入轻量 `AppContext`；
- 将大 handler 调整为“协议适配 + service 调用”。

### P2：完成领域化

- 拆 `handlers/war_at.py`；
- 拆 `war_campaign/tick.py`；
- 将根目录模块逐步迁入标准 package；
- 将 schema 变更全面迁入版本化 migrations。

## 7. 不建议的做法

- 不要一次性移动全部模块并同时重命名所有符号；
- 不要在结构重构时顺手调整协议字段或结算规则；
- 不要仅为了缩短文件而创建大量只有一两个函数的模块；
- 不要用一个新的全局 `utils.py` 汇集所有公共逻辑；
- 不要直接删除旧入口、旧导入路径和状态变量，应先提供兼容层；
- 不要把所有 handler、service、model 分别塞进三个跨领域巨型目录。

## 8. 每次重构的验收要求

每个拆分项至少满足：

1. 对外请求类型和响应类型不变；
2. protobuf/KVCS 字节结构不变，或有明确兼容说明；
3. 数据库事务边界不变；
4. 后台任务启动数量和关闭顺序不变；
5. 原模块保留必要的兼容导出；
6. 对应单元、数据库或端到端测试通过；
7. 单次提交只处理一个明确结构边界，方便回退和定位回归。

## 9. 推荐的首个实施批次

首批建议只做以下三项：

1. 增加 `pyproject.toml`，声明 pytest marker 和测试路径；
2. 将 `matchmaking.py` 中不访问全局状态的消息构建函数迁入 `mm_messages.py`，原模块保留转发导出；
3. 为 `wardb.py` 制定并执行第一步拆分：只迁移静态查询和纯计算，不调整模块级初始化顺序。

这三项能够建立工程规范、验证兼容迁移方式，并明显降低两个最大聚合模块的体积，同时避开战斗结算和战争状态机等高风险路径。
