# HnG 私服（Heroes & Generals 服务器模拟器）

面向《Heroes & Generals》客户端的 Python 协议服务器，提供账号、大厅、装备经济、匹配、战局结算及 RTS 战争逻辑，并调度独立的 Windows 战斗服务器承载实际战斗。

本项目通过 IDA Pro 反编译、客户端 SWF 脚本比对与协议抓包开展逆向开发，早期由 Trae + GLM 协助。

> **逆向工程说明**：本项目仍在持续开发与验证中。对原版协议、客户端行为、数据结构和战斗引擎的理解可能不完整，部分功能采用模拟或近似实现。因此，即使某项功能已有测试或曾正常运行，也可能在不同客户端版本、装备组合、操作顺序、网络条件或多人并发下出现大量未预见的问题，包括卡界面、掉线、战局异常及数据或结算不一致。当前不保证与原版完全一致，也不保证长期稳定运行。下方已知问题仅记录目前发现的情况，并非完整问题清单。

## 当前功能（更新：2026-10-27）

- **认证与会话**：登录、注销、保活、登录排队、封禁检查；Flash 大厅与游戏进程双连接推送；本地 OIDC/JWT 签发与 EOS 模拟后端。
- **角色与装备**：PostgreSQL 持久化玩家、角色、武器、载具及装备；按阵营和兵种发放起始装备；勋带、等级、解锁通知、仓库转移与载具弹药槽位。
- **经济与成长**：credits / warfunds / gold 三币种；武器、载具、配件、弹药、皮肤与礼包购买；每日奖励、兑换券、战争债券、生涯任务和战斗通行证。
- **匹配与组队**：匹配池、地图与兵种筛选、AI 补位、中途加入、多人小队、队长权限、战后小队清理与掉线重返；战斗服实例容量和 UDP 端口池管理。
- **战斗与结算**：自动拉起专用服、BattleFieldSettings 握手、统计上报、经验与战报入库；武器按实际用量维护，载具、升级件及载具弹药按结算包逐实例耐久处理。
- **RTS 战争**：战争地图、阵营、作战部队、部署与行军、会战、增援、补给队列与运输队列。实现及与原玩法的差异见 [战争实现审计](docs/war-vs-implementation-audit.md)。
- **社交**：玩家搜索、好友、邮件推送及 IRC 聊天服务。局内聊天链路仍有待验证，见 [FPS 聊天调查](docs/fps-chat-investigation-2026-10-25.md)。
- **Web**：Vue 入伍页（注册、滑动验证码、外服账号迁移）；独立管理后台提供玩家、装备、战报、邮件、兑换券、战争与服务器配置管理。
- **多机战斗服**：支持远端 agent 拉起、停止及查询实例，在管理后台热配置主机、容量、连接地址与端口池。

近期修复包括小队大厅锁定与生命周期边界、载具逐实例耐久结算、载具变体实例解析、弹药购买及解锁通知。排查记录与验证范围见 [docs/](docs/)。

## 已知缺陷、实际问题与验证边界（2026-10-27）

以下依据当前源码和仓库内最近的调查记录整理，本次文档更新未重新连接线上服务或执行客户端实测。分别标明已观察到的故障、实现限制及仍需验证的事项；历史报告中的“已修复”“已部署”仅适用于其记录的版本与范围。

### 已观察到、尚未完成闭环的问题

| 问题 | 实际影响与当前状态 | 证据 / 下一步 |
| --- | --- | --- |
| 解锁窗口的客户端空引用异常 | 10 月 25 日客户端报告出现 `Error #1009`，涉及价格标签鼠标移出回调，以及窗口关闭后的解锁动画延迟回调；可能表现为解锁界面卡住。已有对应异常记录，但尚未实机确认具体空对象，也未包含客户端修复 | [解锁卡顿调查](docs/unlock-lag-zhai33-2026-10-26.md)、[第二轮日志调查](docs/online-log-audit-round2-2026-10-26.md)。需验证快速连续解锁、弹出奖励及关闭窗口后的回调生命周期 |
| 部分客户端启动卡在 HnG Sync | 两份启动报告显示本机同步服务连接持续失败，最终提示“连接 HnG Sync 出现问题”。日志不能区分同步进程未运行、退出、平台端口差异或本机拦截；不能直接归因于游戏服性能 | [第二轮日志调查](docs/online-log-audit-round2-2026-10-26.md)。需在受影响玩家机器检查 hngsync 进程、50009 / 50010 等对应平台端口与本机日志 |
| 线上局内全队 / 小队聊天尚未验收 | 原 TCP 6667 公网不可达问题已通过切换 16667 处理，公网 IRC 欢迎消息与监听已验证；这不足以证明游戏 HUD 频道初始化及全队、小队收发完整恢复 | [FPS 聊天调查](docs/fps-chat-investigation-2026-10-25.md)。需重新登录后进行双客户端局内收发，结合 JOIN / PART / numeric 日志确认 |

### 当前实现限制及运行风险

- **结算缺少跨重启的完整幂等保证**：载具逐实例耐久结算使用最多 4096 项的进程内去重缓存，统计用量去重同样依赖内存。进程重启、缓存淘汰后的旧报告重放，以及中断时的部分结算，不在现有保证范围内，可能造成重复或不完整处理。见 [载具结算实现边界](docs/vehicle-condition-settlement-fix-2026-10-26.md)。
- **维护扣损依赖上报完整性**：缺少或无效的逐实例耐久不进行估算扣损；武器用量上报缺失时也有跳过处理的路径。因此没有报错不等于每局维护数据完整。见 [装备维护模型](docs/equipment-wear-billing.md)。
- **游戏服重启不能无缝恢复活局**：匹配、实例登记等状态依赖内存。启动会清理旧任务并重建部分 RTS 会战记录，但远端 agent 已拉起的进程可能继续存活，不能据此认为原战局连接、席位及结算已完整恢复。重启后需核查远端 `/status` 与残留进程。见 [多机生命周期说明](docs/combat-hosts.md)。
- **数据库失败后仍可能监听端口**：游戏服初始化数据库异常时会记录“持久化已禁用”并继续启动。仅检查 HTTP 或 TCP 可达会漏掉这一故障，需同时确认数据库就绪日志与实际读写。
- **RTS 部分数值和损耗为近似模型**：当前真人会战的 AT 损耗按胜负比例处理，尚非原版逐兵、逐资源实际消耗；自动判定的门槛及时间参数包含社区资料推导值，需继续校准。胜利条件第二分支及阈值判定已在当前代码实现，不能照搬旧审计文档将其列为“未实现”。参见 `mm_config.py`、`war_campaign/automission.py` 与 `war_campaign/lifecycle.py`。
- **通行证未实现真实赛季轮换**：当前使用历史赛季数据，并将选定赛季延长到 2099 年，以维持界面与奖励流程；不能等同于原版实时赛季服务。见 `handlers/career.py`。
- **EOS / EAC 与原版环境存在差异**：当前默认 `MM_EOS_TOKEN=0`，避免补丁版 DLL 触发官方文件哈希校验。手动开启后可能遇到“未知的文件版本”及退出 10011；本地 OIDC / EOS 模拟不代表原版平台认证与反作弊服务完整可用。见 `mm_config.py`。
- **地图编辑工具仍属研究阶段**：已有 FST 解析、变换回写与模板克隆验证，但不能从零完整生成 mesh、材质、碰撞和 terrain；删除及重挂父节点等操作也有未完成部分，不能视为可直接生产任意可玩地图的完整工具链。见 [FST 进度](docs/fst-progress.md)。

### 已修复问题的验收缺口

载具入库、小队误锁定、弹药槽位与逐实例耐久已有本地修复和针对性回归，但各项部署与实机验证范围不同：载具入库有 [10 月 26 日发布记录](docs/deployment-2026-10-26.md)；[小队误锁定](docs/squad-lobby-lock-fix-2026-10-26.md)、[弹药槽位](docs/tank-ammo-slots-investigation-2026-10-25.md) 和 [耐久结算](docs/vehicle-condition-settlement-fix-2026-10-26.md) 的记录仍包含未部署或未做完整实机 / 真库验证的边界。不能仅凭本地代码或单元测试通过断言线上问题已全部消失。

当前也没有本次更新对应的全量测试通过结论。测试混用独立脚本与 pytest，存在导入时执行、环境与账号依赖；直接执行 `pytest tests` 曾被脚本中的 `sys.exit` 中断。应使用 `tests/run_all.py` 并逐项核对结果，记录数据库、客户端版本和运行配置后，再判断覆盖范围。

## 组件与端口

本仓库包含 Python 服务、Web 前端与管理后端。完整客户端资源、`hng.exe` 和适配的 `player.dll` 需要另行准备。

| 组件 | 默认监听 | 用途 |
| --- | --- | --- |
| `realtest_server.py` | TCP 15110 | 大厅协议、游戏进程连接、战斗服握手 |
| `web_server.py`（随游戏服启动） | TCP 80 | Settings.js、WarDatabase、客户端资源、注册与迁移 API |
| `statistics_server.py`（随游戏服启动） | TCP 15115 | 战斗统计、勋带与用量上报 |
| `irc_server.py`（随游戏服启动） | TCP 16667 | IRC 聊天 |
| Ping 响应器（随游戏服启动） | TCP + UDP 20201 | HostingCenter 连通与延迟探测；TCP 绑定失败时可降级为 UDP |
| `combat-server/hng.exe /D`（外部程序） | UDP 5678–5699（默认池） | 每个战局独立进程、独立 UDP 端口 |
| `web/server/`（独立进程） | TCP 127.0.0.1:8080 | 管理后台与管理 API |
| `combat_agent.py`（可选，远端部署） | TCP 15116 | 远端战斗服进程管理 |

## 环境要求

- **实际战斗部署**：Windows，完整且版本匹配的客户端与专用战斗服资源。
- **Python**：3.11+；当前锁定依赖见 [requirements.txt](requirements.txt)，安装时以各依赖支持的 Python 版本为准。
- **数据库**：PostgreSQL；本机、Docker / WSL Docker 或远程实例均可。
- **前端构建**：Node.js 与 pnpm，版本配置见 [package.json](web/frontend/package.json)。部署已有构建产物时，服务器无需安装 Node.js。

## 首次部署

### 1. 放置代码与游戏资源

推荐目录布局（仓库目录名可自定，下文统一使用 `hng-server`）：

```text
D:\HnG-test\
├── WarDatabase\
├── images\
├── HeroesAndGeneralsDesktop.exe
├── hng-server\                 # 本仓库
│   ├── realtest_server.py
│   ├── .env
│   └── web\
└── combat-server\              # 另行准备，含 hng.exe、player.dll、_packed 等
```

```powershell
cd D:\HnG-test
git clone https://gitee.com/zcmk123/hng-re.git hng-server
cd hng-server
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

服务器会向上探测包含 `WarDatabase` 与 `images` 的资源目录，也支持与 `combat-server` 同层部署。非标准布局在 `.env` 中显式设置路径：

```ini
HNG_GAME_DIR=D:\HnG-test
HNG_COMBAT_SERVER_DIR=D:\HnG-test\combat-server
HNG_DATABASE_URL=postgresql://hng:hng_password@127.0.0.1:5432/hng
# 外网开服时填写客户端可达的公网 IP 或主机名
HNG_PUBLIC_HOST=127.0.0.1
```

进程启动时加载仓库根目录 `.env`，shell 中已设置的同名环境变量优先。完整配置及说明见 [.env.example](.env.example)。

### 2. 准备数据库

默认连接串为 `postgresql://hng:hng_password@127.0.0.1:5432/hng`。先创建用户与数据库：

```sql
CREATE USER hng WITH PASSWORD 'hng_password';
CREATE DATABASE hng OWNER hng;
```

游戏服启动时自动创建或迁移表结构；管理后台首次启动会创建其管理表。本机 / WSL Docker 连接支持自动探测，显式设置 `HNG_DATABASE_URL` 时按指定地址连接。

确认日志出现 `[DB] PostgreSQL ready`。数据库不可用时游戏服可能继续监听端口，但持久化已禁用，不能仅凭端口监听判断部署成功。

### 3. 配置客户端连接与网络

本机联调时，在客户端机器的 `C:\Windows\System32\drivers\etc\hosts` 中配置：

```text
127.0.0.1 game.hng-group.com
127.0.0.1 api.epicgames.dev
127.0.0.1 fs1.hng-group.com
127.0.0.1 fs2.hng-group.com
127.0.0.1 fs3.hng-group.com
127.0.0.1 fs4.hng-group.com
127.0.0.1 fs5.hng-group.com
127.0.0.1 fs6.hng-group.com
```

外网部署时，以上地址改为相应服务入口，并设置 `HNG_PUBLIC_HOST`。根据上方端口表配置防火墙与 NAT，客户端必须能直接访问战斗服的 UDP 端口池。远端战斗服还需回连游戏服 TCP 15110 / 15115。

### 4. 构建入伍页与管理前端

```powershell
cd web\frontend
pnpm install
pnpm build
cd ..\..
```

构建包含 TypeScript 检查，输出到 `web/frontend/dist/`。产物不入库，部署时需单独复制或使用 `deploy.sh --web` 发布；缺少产物会导致 `/join` 返回 404，管理后台仅提供 API。

### 5. 启动游戏服

在管理员 PowerShell 中，从仓库根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File start_server.ps1
```

也可直接运行 `.venv\Scripts\python.exe realtest_server.py`；`run_realtest.ps1` 用于服务器与客户端联调。

确认数据库连接成功及 HTTP 80、TCP 15110 正常监听，然后打开：

- 注册：[http://127.0.0.1/join](http://127.0.0.1/join)
- 外服账号迁移：[http://127.0.0.1/join/veteran](http://127.0.0.1/join/veteran)
- 在线状态：[http://127.0.0.1/online.json](http://127.0.0.1/online.json)

自行注册游戏账号；部分历史测试依赖 `demo / demodemo123` 等预置数据，新数据库需按相应测试要求准备。

### 6. 启动管理后台（可选）

在仓库根目录执行（与游戏服共用数据库，使用独立进程）：

```powershell
# 首次创建管理员，随机密码仅输出一次
.venv\Scripts\python.exe web\server\main.py create-admin <用户名>
.venv\Scripts\python.exe web\server\main.py
```

打开 [http://127.0.0.1:8080](http://127.0.0.1:8080)。默认仅监听回环地址，远程访问可使用 SSH 隧道。管理员账号与游戏账号分别管理。

后台监听地址、端口、JWT 密钥由 `HNG_ADMIN_BIND`、`HNG_ADMIN_PORT`、`HNG_ADMIN_SECRET` 控制。更多开发、账号管理及鉴权说明见 [web/README.md](web/README.md)。

## 日常维护与多机部署

修改 Python 代码或 `.env` 后重启游戏服：

```powershell
cd D:\HnG-test\hng-server
git pull
powershell -ExecutionPolicy Bypass -File admin_restart_server.ps1
```

前端变更需要重新构建并同步 `dist`；管理后端变更需要单独重启，可使用 `web/restart_admin.ps1`。本地到 Windows VPS 的发布脚本见 [deploy.sh](deploy.sh)，使用前设置自己的 `HNG_SSH_TARGET`、`HNG_SSH_PORT` 和 `HNG_REMOTE_DIR`。

多机部署通过管理后台「服务器配置 → 战斗服主机」配置，保存后对新战局生效。远端机器运行 `combat_agent.py`，游戏服设置与 agent 相同的 `HNG_AGENT_TOKEN`；远端主机的 warserver 地址应填写其可达的游戏服地址。

- [多机架构、容量和端口配置](docs/combat-hosts.md)
- [新增战斗服主机部署手册](docs/combat-host-deploy.md)

## 开发与验证

主要代码职责：

| 路径 | 职责 |
| --- | --- |
| `protocol.py` / `messages.py` / `warserver_messages.py` | 协议编解码、消息构建与 KVCS 推送 |
| `state.py` / `wardb.py` / `player_service.py` | 配置、会话、静态模板与玩家数据服务 |
| `handlers/` / `store/` | 各业务域请求处理与 PostgreSQL 存储 |
| `matchmaking.py` / `mm_*.py` | 匹配、队列、成局与战局状态 |
| `combat_instances.py` / `combat_hosts.py` / `combat_agent.py` | 战斗服实例、主机容量与远端管理 |
| `war_campaign/` / `force_pool.py` | RTS 战争、部队与补给资源 |
| `web_server.py` / `register_captcha.py` / `foreign_import.py` | 游戏 HTTP 服务、验证码与外服迁移 |
| `web/frontend/` / `web/server/` | Vue 前端与 FastAPI 管理后台 |
| `tools/` / `tests/` / `docs/` | 运维与逆向工具、测试、调查记录 |

测试同时包含 pytest 模块与独立脚本，统一入口会选择相应运行方式：

```powershell
.venv\Scripts\python.exe -m pip install pytest
# 按文件名子串筛选
.venv\Scripts\python.exe tests\run_all.py vehicle_condition
# 全量执行（先阅读相关测试的环境与账号要求）
.venv\Scripts\python.exe tests\run_all.py
# 登录与订阅联调（需运行中的游戏服）
powershell -ExecutionPolicy Bypass -File run_selftest.ps1
```

数据库与端到端测试依赖可用的 PostgreSQL、客户端资源、测试账号或运行中的游戏服，部分测试会写入数据，应使用独立测试环境。`MM_SIMULATE_COMBAT=1` 可用于不拉起 `hng.exe` 的协议调试，不能替代真实战斗验证。历史验证结果见 [重构基线](docs/baseline-2026-09.md)，不代表当前环境已全量通过。

常用账号工具（仓库根目录执行）：

```powershell
.venv\Scripts\python.exe tools\add_user.py <用户名> <密码>
.venv\Scripts\python.exe tools\set_password.py <用户名> <新密码>
```

## 排查与日志

| 现象 | 检查项 |
| --- | --- |
| HTTP 80 绑定失败 | 检查端口占用，按现有 Windows 启动脚本要求使用管理员终端 |
| PostgreSQL 连接失败 | 检查数据库进程、连接串、网络和数据库权限 |
| 客户端无法连接 | 检查客户端 hosts、`HNG_PUBLIC_HOST`、防火墙与 NAT |
| 未能自动探测客户端目录 | 设置 `HNG_GAME_DIR`，确认目录包含 `WarDatabase` 和 `images` |
| `/join` 返回 404 | 在 `web/frontend` 构建并部署 `dist` |
| 公网 80 端口访问不到管理后台 | 后台是独立服务，默认地址为 `127.0.0.1:8080` |
| 战局卡加载或校验失败 | 核对客户端与战斗服版本、WarDatabase 来源及文件；修改资源后重启游戏服 |
| 战斗服没有拉起或无法进入 | 检查实例容量、战斗服路径、agent、握手日志及公网 UDP 端口池 |
| 远端战斗服不涨经验或勋带 | 检查远端到游戏服 TCP 15115 的连通性 |

- 游戏服协议日志：`logs/realtest_game.log`，按天轮转，`LOG_KEEP_DAYS` 默认 3 天。
- 启动脚本的错误输出：`logs/realtest_stderr.log`。
- 每局战斗服日志：对应主机 `combat-server/logs/combat-server-<seq>.log`。

战斗引擎和客户端行为仍需实机验证，RTS、迁移及地图工具的实现边界见各专题文档。地图 `.fst` 解析与 Unity 转换属于独立研究工具，进度见 [fst-progress.md](docs/fst-progress.md)。
