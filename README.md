# HnG 私服（Heroes & Generals 服务器模拟器）

> 本项目由 **Trae + GLM** 协助逆向分析与开发。
>
> - IDE：Trae（https://trae.cn）
> - AI 模型：GLM
> - 逆向工作流：IDA Pro 反编译 + 客户端 SWF 反编译脚本比对 + Python 协议模拟

---

## 一、这是什么

《Heroes & Generals》客户端的私服服务器模拟器，包含两大组件：

1. **hng-server/**（本仓库，Python）：协议服务器
   - TCP 游戏服务器（端口 15110）：登录、角色/武器/载具、商店、任务、匹配、战局推送
   - HTTP 服务器（端口 80）：客户端静态资源、账号注册页 `http://127.0.0.1/register`
   - OIDC/JWT 本地签发（eos_oidc 密钥对，供战斗服 EOS token）
   - Ping 响应器（端口 20201，TCP+UDP）
   - 匹配成功后自动拉起战斗服务器（combat-server）
2. **combat-server/**（不在本仓库，C++ 引擎，hng.exe /D 专用服）：承载实际战局
   - UDP 5678 承载战斗流量，回连 warserver（15110）完成 BattleFieldSettings 握手
   - player.dll 已打二进制补丁（EAC 服务端校验禁用），`player.dll.bak_*` 为补丁前备份
   - `start_server.bat` 为手动调试入口（常规测试无需运行，匹配后自动拉起）

## 二、功能进度（更新：2026-08-25）

### 已完成 ✅

**认证与会话**
- 登录/注销/keepalive 全链路（logout 响应回显防请求队列卡死）
- OIDC/JWT 本地签发（iss/aud/sub/exp 与客户端校验完全对齐）+ EOS 假后端
- 双连接拓扑支持：Flash UI 连接 + hng.exe 游戏连接（FLASH_SESSIONS 注册表跨连接推送）

**角色系统**
- PostgreSQL 持久化角色（players/characters/weapons/vehicles + 8 张装备表）
- 新兵初始装备发放（starting_gear.py：按国家×兵种配置，步兵武器/侦察瞄具/坦克兵坦克/飞行员侦察机）
- tutorialsCompleted 持久化（修复新手免费招募模式卡死）

**经济与商店（全部原子事务）**
- 三币种（credits/warfunds/gold）+ 42 用例事务测试（并发扣款/防双领/回滚/幂等）
- 武器/载具/皮肤变体/配件/弹药购买，捆绑包、勋带等级购买
- 每日登录奖励、兑换券、战争债券、军令奖励领取、战斗通行证

**解锁系统（2026-08-25 重构完成）**
- unlockSequence 统一白名单：可购买物品类 uid 不随默认序列授予，只走起始装备/勋带 XP/购买三条正路
- 修复武器、载具、战斗徽章的解锁位图多重泄漏（新号零徽章、PPD/Bazooka 正确锁定）

**匹配与战局**
- 完整匹配流程：GetMissionList → JoinMission → 拉起专用战斗服 → BattleFieldSettings 握手 → PMS 推送
- 离开战局/退出登录全链路（PlayerExitBatch 跨连接推送 + AbandonMission + PMS DELETE）
- 单人隐式小队（SquadSession/SquadMemberSession KVCS 全时序，含解散）
- 战局结算入库 + 战报界面读取（battle_reports 表）
- 开局倒计时覆盖（专用服启动参数 controllers.overrideStartCountdown）

**社交与生涯**
- 玩家搜索/好友响应、军团邀请响应、军团离开
- CareerOrder 46 种大厅动作进度 + 奖励领取 + BattlePass 四赛季延期可用

**协议层**
- 全部未知请求返回 HandlerErrorResponse（防客户端请求队列阻塞）
- KVCS 表推送路由（客户端 DataModelProxy 按表名自动注册）
- 协议回归测试 33 项 + 真库测试 57 项 + 事务测试 42 项全部通过

### 已知限制 / 待办 ⏳
- 战局热身时间：missionpreset 客户端文件修改未生效，已改道专用服启动参数覆盖（待实测）
- BattlePass 赛季为延期模拟（模板行 2099），非真实赛季轮换
- 会员体系（Veteran）相关请求恒返回 ok，无实际会员逻辑

---

## 三、环境要求

| 项 | 要求 |
|---|---|
| 操作系统 | Windows（监听 80 端口需管理员权限） |
| Python | 3.11+（仅 hng-server 需要） |
| 数据库 | PostgreSQL 13+（本机 Docker 或 WSL Docker 均可） |
| 客户端 | 完整的 HnG 客户端目录（含 WarDatabase、images 等，见"部署"） |

## 四、部署方式

**git clone 本仓库到客户端根目录**（与客户端的 WarDatabase、images 同级）：

```powershell
cd d:\HnG-test
git clone https://gitee.com/zcmk123/hng-re.git hng-server
```

```
d:\HnG-test\               ← 客户端根目录（已存在的游戏文件）
├── WarDatabase\           ← 客户端自带
├── images\                ← 客户端自带
├── HeroesAndGeneralsDesktop.exe   ← 客户端启动器
├── hng-server\            ← git clone 得到（本仓库，Python 服务器）
└── combat-server\         ← 战斗服务器（不在本仓库，需另行部署；含全部运行数据 _packed 等）
```

服务器启动时**自动探测**客户端根目录与 combat-server 位置（探测特征：WarDatabase + images），
无需改代码。若克隆在其他位置，用环境变量指定：

| 环境变量 | 作用 | 示例 |
|---|---|---|
| `HNG_GAME_DIR` | 客户端根目录 | `D:\MyHnG` |
| `HNG_COMBAT_SERVER_DIR` | 战斗服务器目录 | `D:\MyHnG\combat-server` |
| `HNG_DATABASE_URL` | PostgreSQL 连接串 | `postgresql://hng:hng_password@192.168.1.5:5432/hng` |

## 五、首次部署步骤

### 1. 安装 Python 依赖（仅 hng-server）

```powershell
cd hng-server
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

### 2. 准备 PostgreSQL

- 默认连接串：`postgresql://hng:hng_password@127.0.0.1:5432/hng`
- 本机/WSL Docker：无需配置，启动时自动探测 WSL Docker 网段
- 远程数据库：设 `HNG_DATABASE_URL`（显式指定时不做任何替换）
- 建库建用户：

```sql
CREATE USER hng WITH PASSWORD 'hng_password';
CREATE DATABASE hng OWNER hng;
```

表结构首次启动时自动创建/迁移，无需手工建表。

### 3. hosts 与杀软（手动，一次性，管理员）

编辑 `C:\Windows\System32\drivers\etc\hosts`：

```
127.0.0.1 game.hng-group.com
127.0.0.1 api.epicgames.dev
127.0.0.1 fs1.hng-group.com
127.0.0.1 fs2.hng-group.com
127.0.0.1 fs3.hng-group.com
127.0.0.1 fs4.hng-group.com
127.0.0.1 fs5.hng-group.com
127.0.0.1 fs6.hng-group.com
```

并把 hosts 文件加入 **Windows Defender 排除项**（否则条目会被当 DNS 劫持清除）。

## 六、日常使用

### 启动

```powershell
# 方式一：脚本（推荐，管理员 PowerShell）
cd hng-server
powershell -ExecutionPolicy Bypass -File start_server.ps1

# 方式二：一键服务器+客户端（管理员）
powershell -ExecutionPolicy Bypass -File run_realtest.ps1

# 方式三：直接跑（需管理员，否则 80 端口绑定失败）
.venv\Scripts\python realtest_server.py
```

启动成功的标志（控制台 / hng-server\realtest_game.log）：

```
[oidc] configured (audience=xyza7891uO05dmOamk2giEDkkLHD6Taj)
[DB] PostgreSQL ready: <ip>:5432/hng
TCP game server listening on 0.0.0.0:15110  (demo password: demodemo123)
[HTTP] listening on 0.0.0.0:80  (serving <客户端根目录>)
```

进入战斗后日志会出现：

```
launching combat server: /D 5678 /S session-... /M T01M08_Mill /W 127.0.0.1:15110 ...
combat server #N handshake ok (pid=...)
```

### 更新与重启（改代码后必须重启才生效）

```powershell
cd hng-server
git pull                                              # 拉取最新代码
powershell -ExecutionPolicy Bypass -File admin_restart_server.ps1
```

### 停止

关掉服务器控制台窗口，或 `taskkill /F /PID <监听 15110 的进程>`（提权启动时需管理员）。

## 七、测试账号与验证

| 账号 | 密码 | 说明 |
|---|---|---|
| demo | demodemo123 | 内置演示账号 |

- 新账号：游戏内注册页 `http://127.0.0.1/register`，或命令行（见下）
- 自测：

```powershell
powershell -ExecutionPolicy Bypass -File run_selftest.ps1     # 登录+订阅自测
.venv\Scripts\python tests\test_regression.py    # 协议回归（33 项）
.venv\Scripts\python tests\test_realdb.py        # 数据库读写（57 项）
.venv\Scripts\python tests\test_transactions.py  # 数据库事务（42 项）
.venv\Scripts\python tests\test_e2e.py           # 端到端（需服务器运行中）
.venv\Scripts\python tests\test_autorepair.py    # 自动维修三态（需服务器运行中）
```

## 八、常用工具（tools/）

```powershell
.venv\Scripts\python tools\add_user.py <用户名> <密码>       # 手动添加账号
.venv\Scripts\python tools\set_password.py <用户名> <新密码>  # 改密码
```

## 九、常见问题

| 现象 | 处理 |
|---|---|
| 启动报 80 端口绑定失败 | 用管理员身份运行（start_server.ps1 需管理员 PowerShell） |
| 启动报 PostgreSQL 连接失败 | 确认 Docker 容器在跑；远程库检查 `HNG_DATABASE_URL` |
| 客户端连不上 game.hng-group.com | 检查 hosts 条目是否还在（Defender 排除项是否配置） |
| 启动警告「未能自动探测客户端根目录」 | 仓库未克隆在客户端根目录下；设置环境变量 `HNG_GAME_DIR` 指向客户端根目录（或在 hng-server\.env 里写 `HNG_GAME_DIR=...`） |
| 改了代码没生效 | 必须 `admin_restart_server.ps1` 重启 |
| 登录提示密码错误 | demo 密码是 `demodemo123`；其他账号用 set_password.py 重置 |
| 战局卡加载/校验失败 | WarDatabase 被改动后需重启服务器（校验清单是启动时生成的） |
| 战斗服务器没拉起 | 看 realtest_game.log 的 `launching combat server` 行；单独调试用 combat-server\start_server.bat |

## 十、日志

- Python 服务器：`hng-server\realtest_game.log`（全部协议收发记录）
- 战斗服务器：`combat-server\combat-server-N.log`（每局一个，N 递增）+ `hng_boot.log`
