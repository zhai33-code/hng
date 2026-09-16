# 计划：新建 Vue3 前端项目（HnG 风格注册页 + 游戏后台管理）

## 一、总结

在当前 Python 服务仓库（`hng-re/hng-re/`，部署名 `hng-server`）内新建 `web/` 目录，容纳**全部 Web 相关代码**：

```
web/
├── (Vite 前端项目，双入口 MPA)
└── server/    # 管理后端（FastAPI，独立进程，独立端口）
```

**架构与端口（用户确认）**：
- **注册页（公网面）**：80 端口，由现有 `web_server.py`（游戏服进程内）下发——仅暴露注册一个功能；
- **管理后台**：独立进程（`web/server/`，FastAPI + uvicorn），独立端口（默认 `127.0.0.1:8080`，`HNG_ADMIN_BIND`/`HNG_ADMIN_PORT` 可配）——公网默认不可达，远程管理走 SSH 隧道；
- **游戏服进程（realtest_server.py）不动**：管理端重启不影响在线玩家，爆炸半径隔离。

前端为同一 Vite 项目**双入口 MPA**：`register.html`（注册应用，80 端口下发）与 `admin.html`（管理应用，管理端口下发），物理分离。

## 二、现状分析（Phase 1 探索结论）

- 注册页现状：[web_server.py](file:///Volumes/ZHITAI/Users/zhouchenminkun/Desktop/dev-projects/hng-re/hng-re/web_server.py) 内嵌 `_REGISTER_TMPL`/`_REGISTER_RESULT_TMPL` HTML 模板，`GET/POST /register` 表单直出，POST 经 `run_coroutine_threadsafe(STORE.create_player)` 投递到主 loop 建号。
- web_server 是 `ThreadingHTTPServer`（0.0.0.0:80，独立线程），同时承担游戏客户端硬依赖端点，**这些不能动**：
  - `GET /`：hngsync ping 探测（延迟 60ms 回纯文本 `OK`，不能变成 index.html）
  - `/settings.js`、`/hng_unique_v173293`、`/wardb_unique`、`/WarDatabase/<tbl>`、`/flashres_unique`、本地化 XML、GAME_DIR 静态回退
- `settings.js` 的 `account_url = http://{PUBLIC_HOST}/account`（客户端"账号"链接入口）。
- 建号入口 `STORE.create_player(username, password)`：`ON CONFLICT DO NOTHING`，已存在返回 None（[player_store.py:699](file:///Volumes/ZHITAI/Users/zhouchenminkun/Desktop/dev-projects/hng-re/hng-re/player_store.py#L699)）。
- **官方视觉素材可直接复用**：客户端 GAME_DIR 已带 `images/Soldier/`（士兵立绘 150+）、`images/Campaign/`（战场地图图）、`images/Membership/`（jpg 宣传图）、`images/Rank/`（军衔），且 80 端口静态回退已可直接访问（如 `/images/Soldier/1.png`）——注册页无需额外图片资产。
- 现有安全约束（必须完整保留）：64KB Content-Length 上限、用户名 `2-24` 位 `[A-Za-z0-9_\-.]`、`reto.`/`[admin]` 保留前缀拒绝（记日志、对外固定话术）、密码 4-64 位、DB 错误细节不外泄、静态敏感扩展名 404、日志走 `state.log()`（[HH:MM:SS] 前缀 + 按天滚动）。
- 部署形态：Windows 云服务器 `git pull` + `admin_restart_server.ps1` 重启，**服务器无 Node 环境** → 构建产物 `web/dist` 提交入库。
- 数据库：PostgreSQL（players/characters/battle_reports/mission_records 等表，asyncpg 连接池）。

## 三、新增内容

### 1. 前端项目 `hng-re/hng-re/web/`（Vite MPA 双入口）

```
web/
├── package.json            # vue3 / element-plus / 自动按需导入插件
├── vite.config.ts           # MPA：rollupOptions.input = { register, admin }
│                            #   dev 代理 /api -> http://127.0.0.1:80；/admin/api -> http://127.0.0.1:8080
├── tsconfig.json / tsconfig.app.json / tsconfig.node.json
├── register.html            # 注册应用入口
├── admin.html               # 管理应用入口
├── public/                  # 从客户端 GAME_DIR 复制的官方素材（用户要求：直接复制过去用）
│   └── icons/{Weapons,Vehicles,Soldier,Rank,Ribbons}/...   # 随 build 拷入 dist，双入口共用
├── .gitignore               # node_modules/；dist/、public/ 不忽略（提交入库）
├── auto-imports.d.ts / components.d.ts   # unplugin 生成（提交）
└── src/
    ├── register/
    │   ├── main.ts / App.vue / api.ts
    │   └── RegisterForm.vue
    ├── admin/
    │   ├── main.ts / App.vue          # 布局框架（侧边栏 + 顶栏）
    │   ├── api.ts                      # 统一 fetch 封装（带凭证、401 跳登录）
    │   ├── router.ts                   # /login /dashboard /players /players/:id ...
    │   └── views/                      # Login / Dashboard / Players / PlayerDetail / ... (见 §2)
    └── styles/hng-theme.scss           # HnG 军事风主题变量（双入口共享）
```

- 依赖：`vue`、`vue-router`（admin 应用，hash 模式）、`pinia`（admin 会话/用户状态管理）、`element-plus`、`unplugin-auto-import`、`unplugin-vue-components`。
- **请求库（明确）**：不引入 axios/ky，统一用 **原生 fetch 封装**（`src/admin/api.ts` 与 `src/register/api.ts` 各自轻封装：JSON 序列化、超时 AbortController、统一错误结构；admin 封装附带 credentials + 写操作 `X-Requested-With` 头）——浏览器原生、零依赖、类型友好。
- `web/dist` 提交入库；服务器 `git pull` + 重启即生效。

#### 1.1 注册页 HnG 官方风格设计（用户要求）

参照 HnG 官网/游戏 UI 的二战军事风（深色战场氛围 + 阵营士兵立绘 + 卡其/橄榄色 + 军事标题字）：

- **背景**：深色（近黑的棕橄榄 `#1a1c16` 系）+ 细噪点/斜纹纹理（纯 CSS，无图片依赖），底部暗角渐变；
- **主视觉**：右侧士兵立绘——**从客户端 `GAME_DIR/images/Soldier/` 复制**选定立绘到 `web/public/`（随构建进 dist，注册页与管理端共用同源素材，不依赖 80 端口路径），CSS 遮罩渐变融入背景；移动端隐藏立绘只留纹理；
- **卡片**：左侧注册卡片，卡其/帆布质感（`#c8b98a` 边框 + 半透明深底），带轻微"军用公文"斜切角样式；
- **标题**：大写 + 宽字距（stencil 感：`font-family` 用系统黑体 + `letter-spacing`），主色 `#e8d48b`（HnG 金）；
- **按钮**：HnG 金/橄榄绿实心（`#8a7a2e`→hover 提亮），斜切角，全大写文字；
- **文案**：标题「Heroes & Generals 私服」，副题「加入战斗 · 注册账号即可登录游戏客户端」（呼应官方 "Join the war" 语调）；
- **Element Plus 定制**：通过 CSS 变量覆盖（`--el-color-primary` 等对齐上述色板），输入框暗色化；
- 不引入外部字体/CDN（服务器在境内、客户端可能离线场景），全部系统字体。

### 2. 管理后台功能清单（本期实现范围）

#### 本期实现 ✅

| 功能 | 说明 | 数据来源 |
|---|---|---|
| **管理员登录/鉴权** | 见 §3 安全设计；登出、会话过期 | admin_users 表 |
| **仪表盘** | 总玩家数、今日新增注册、总角色数、累计战局数、最近注册列表 | players/characters/mission_records |
| **玩家列表** | 分页/按用户名搜索/按注册时间排序 | players 表 |
| **玩家详情（账号级）** | 账号信息（id/用户名/注册时间）、三货币 credits/warfunds/gold、**角色列表**（每角色：阵营/兵种/军衔(combat_xp 经验等级)/创建时间）、最近战报 | players/characters/battle_reports |
| **角色详情（装备明细，用户要求）** | 角色基础信息 + **持有武器清单**（武器名+图标+变体、已装备配件 weapon_modifiers（名称+图标）、弹药 weapon_ammo（类型+剩余数量 condition））、**持有载具清单**（变体名+图标+已装备升级件 vehicle_upgrades）、战斗徽章槽位（combat_badge_0/1）、勋带进度（character_ribbon_xp 各勋带等级） | weapons/weapon_modifiers/weapon_ammo/vehicles/vehicle_upgrades/character_ribbon_xp + §3 名称映射 |
| **玩家操作** | **重置密码**；**发放/扣减货币**（原子事务+审计）；**封禁/解封** | players 表 + admin_audit_log |
| **战报查看** | battle_reports 分页列表 + 详情 | battle_reports 表 |
| **服务器状态** | 游戏服端口健康检查（80/15110 TCP 探测）、数据库连接状态、uptime | TCP 探测/DB |
| **审计日志** | 全部管理操作留痕（谁/何时/做了什么/目标/IP），可查看 | admin_audit_log 表 |
| **注册统计** | 按日注册量（近 30 天，表格+简单条形图） | players 表 |

#### 本期不做、架构预留 ⏳

- **强制踢下线/在线玩家列表**：在线会话在游戏进程内存（FLASH_SESSIONS），跨进程需游戏服开内部状态接口——预留 `/admin/api/server/*` 路由，后续在 `realtest_server.py` 加内部只读端点对接；
- **封禁实时生效**：本期封禁写 DB 标志位，游戏登录处增加检查属游戏服改动（一并列入后续小改动）；
- **角色数据编辑（解锁/装备）**：表结构复杂，后续做；
- **WarDatabase/兵力池配置管理**：后续做（force_pool.py 已模块化）。

### 3. 管理后端 `web/server/`（FastAPI 独立进程）——安全与鉴权设计（用户要求重点）

```
web/server/
├── main.py          # FastAPI app + uvicorn 入口（读 .env：HNG_DATABASE_URL/HNG_ADMIN_*）
├── config.py        # .env 加载（复用 state.py 的极简解析逻辑，不 import state——避免其日志/目录副作用跨进程）
├── db.py            # 独立 asyncpg 连接池（同一 PostgreSQL，独立进程持有）
├── auth.py          # 登录/会话/CSRF/限速
├── audit.py         # 审计日志写入
├── wardb_meta.py    # 装备名称/图标映射（见下）
└── routers/         # players / characters(装备明细) / dashboard / battles / status / stats
```

#### 装备名称与图标映射（角色装备明细的显示基础）

- DB 表存的是 template_id（如 weapons.template_id、vehicles.vehicle_variant_template_id），名称/图标在客户端 WarDatabase 模板表里（如 weaponTemplate 字段 4=name，模板表带 image 字段）；
- `wardb_meta.py`：启动时解析 WarDirectory 模板表（weaponTemplate/weaponModTemplate/weaponAmmoTemplate/vehicleTemplate/vehiclesVariantTemplate 等），构建 `template_id → {name, image}` 映射并缓存；表文件解析需要 Chunk/protobuf-wire 解码器——**从 protocol.py 复制精简版解码器**（保持"管理端不 import 游戏服代码"原则，~百行级）；
- GAME_DIR 探测逻辑同 state.py（HNG_GAME_DIR 环境变量 → 父级目录探测），精简复制进 config.py；
- 解析失败（WarDatabase 缺失/格式异常）→ 降级显示 template_id 原始值，不阻断管理端；
- 前端图标：`web/public/icons/` 下素材文件名即模板 image id（如 `Weapons/41.png`），API 返回 image id，前端拼路径 `icons/Weapons/{image}.png`；缺失图显示占位。

#### 图片素材策略（用户要求：客户端里找，复制过去用）

- 实施 copy 脚本（一次性，不入库自动执行）：从 `GAME_DIR/images/` 复制以下目录到 `web/public/icons/`：
  - `Weapons/`（武器图标，管理端角色装备明细）+ `Vehicles/`（载具图标）
  - `Rank/`（军衔，玩家角色军衔显示）+ `Ribbons/`（勋带，角色勋带进度）
  - `Soldier/` 中选定的一张注册页立绘 → `web/public/soldier.png`
- 复制后素材随 git 提交（`public/` 不忽略），服务器 pull 即有，不依赖服务器上的客户端目录（前端展示层完全自包含）；
- 80 端口 `web_server.py` 下发 `dist/` 时同时覆盖 `public` 拷贝产物（vite build 会把 public/ 原样并入 dist 根，无需额外处理）。

#### 安全设计

**鉴权**

**管理员账号生成与生命周期（完整机制）**

- **存储**：`admin_users` 表（管理端启动时 `CREATE TABLE IF NOT EXISTS` 自建）：
  ```sql
  CREATE TABLE IF NOT EXISTS admin_users (
      id            BIGSERIAL PRIMARY KEY,
      username      TEXT UNIQUE NOT NULL,
      password_hash TEXT NOT NULL,          -- scrypt$N$r$p$salt$hash
      created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
      last_login_at TIMESTAMPTZ
  );
  ```
- **创建方式：首次启动 CLI 交互式初始化（不预留任何默认账号）**
  - 服务器上首次运行 `python web/server/main.py`，检测到 `admin_users` 为空 → 进程进入交互式引导（终端提示输入用户名 + 两次密码，密码不回显）→ scrypt 哈希入库 → 打印“管理员创建成功”后正常启动监听；
  - 同时提供非交互模式供脚本/无终端场景：`python web/server/main.py --init-admin <用户名> --password-stdin`（密码从 stdin 读，避免进命令行历史）；
  - 用户名规则：2-32 位字母/数字/`_-.`；密码规则：≥ 10 位且不含空格（管理端是最高权限面，比玩家注册的 4 位门槛严格）。
- **已有管理员的后续维护**（同样只在服务器终端操作，不暴露 Web 注册通道）：
  - 新增管理员：`python web/server/main.py --add-admin <用户名> --password-stdin`（需在服务器上操作，天然只有机主能做）；
  - 改密：`python web/server/main.py --set-password <用户名> --password-stdin`（admin_users 增加 `credentials_updated_at` 列；改密时更新为当前时间，JWT 校验时拒绝签发时间早于它的 token → 该管理员旧会话全部失效）；
  - Web 端**永不提供**管理员注册/自助找回入口（管理员账号只能经服务器 CLI 管理——这是权限面收敛的关键设计）。
- **JWT 签发密钥**：`HNG_ADMIN_SECRET` 环境变量；未设置时首次启动自动生成随机密钥并写入 `web/server/.admin_secret`（gitignore，不入库）——重启后会话保持有效，删文件即全体登出（应急开关）。
- 表为空且使用了非交互模式启动（如服务托管场景）→ 拒绝启动并提示先初始化，杜绝“无管理员裸奔”。
- **会话**：登录成功签发 PyJWT（已是现有依赖）短时效 token（2 小时），HttpOnly + SameSite=Strict Cookie；不落服务端会话表（无状态）；签发密钥持久化在 `.admin_secret` → 管理端进程重启不踢人，改密 / 删除密钥文件才使全体会话失效；
- **CSRF**：SameSite=Strict + 所有写操作要求自定义头 `X-Requested-With: XMLHttpRequest`（fetch 才能携带，表单跨站无法伪造）双保险；
- **登录限速**：按 IP 内存计数（15 分钟窗口 5 次失败 → 锁 15 分钟），全局连续失败退避；失败原因不区分（统一"用户名或密码错误"）。

**传输与网络**
- 默认绑定 `127.0.0.1:8080`（公网不可达）；需要公网时 `HNG_ADMIN_BIND=0.0.0.0` + 云防火墙白名单仅放行管理员 IP（README 注明）；
- 若公网开放（BIND 非 127.0.0.1）：启动时打印醒目警告。

**应用层**
- FastAPI 中间件：安全响应头（`X-Content-Type-Options`/`X-Frame-Options: DENY`/`Referrer-Policy`）、请求体大小上限（1MB）、统一 JSON 错误（不泄异常细节，细节进日志）；
- 所有查询参数化（asyncpg 原生参数绑定，无拼接）；管理写操作走事务 + 审计同事务落库；
- **审计**：`admin_audit_log(id, admin_id, action, target, detail, ip, created_at)`，登录成功/失败、密码重置、货币变更（含变动前后值）、封禁操作全部记录；
- 健壮性：DB 不可用时管理端返回 503 并在日志标注，不影响游戏服；
- 日志：独立文件 `web/server/admin.log`（同 [HH:MM:SS] 前缀 + 按天滚动，复刻 state.py 的滚动逻辑，不与其共写一个文件避免跨进程写冲突）。

**数据库权限（建议项，写入 README）**
- 生产环境可为管理端单独建 DB 用户，仅授予 `players/characters/battle_reports/mission_records/admin_*` 的 SELECT/UPDATE，不授 DROP/CREATE。

### 4. 80 端口后端改动 `hng-re/hng-re/web_server.py`（唯一改动的游戏服文件）

a) **新增 `POST /api/register`（JSON）**：校验逻辑从 `_handle_register_post` 原样迁移（正则、保留前缀 BLOCKED 日志、长度、两次一致、`STORE.pool` 判空、64KB 上限、DB 错误固定话术）；仍经 `run_coroutine_threadsafe(STORE.create_player, _MAIN_LOOP)`。

b) **接管注册 SPA 静态下发**：`GET /assets/<f>` → `web/dist/assets/`（realpath 前缀校验防穿越）；`GET /register`、`/account` → `web/dist/register.html`；**`/admin`、`admin.html` 一律 404**（80 端口绝不暴露管理入口）；dist 缺失时降级回旧表单页。

c) **删除**旧注册模板与 `POST /register` 表单处理（降级兜底的精简 form 保留）。

d) **不动**：`GET /` ping 探测、`/settings.js`、`/hng_unique_*`、`/wardb_unique`、`/WarDatabase/*`、`/flashres_unique`、本地化 XML、GAME_DIR 静态回退与 `_deny_static`、日志格式。

### 5. 部署集成

- `requirements.txt`：追加 `fastapi`、`uvicorn`（版本钉死）；
- `start_server.ps1` / `admin_restart_server.ps1`：拉起两个进程（realtest_server.py + `web/server/main.py`）；重启脚本停止逻辑覆盖 8080 端口进程；
- `.env.example`：`HNG_ADMIN_BIND` / `HNG_ADMIN_PORT` / `HNG_ADMIN_SECRET`（注释说明：不设则自动生成并落 `web/server/.admin_secret`）；
- `hng-re/.gitignore`：`web/node_modules/`、`web/server/admin.log`、`web/server/.admin_secret`（dist/public 不忽略）；
- `README.md`：前端开发/构建说明、管理后台使用说明（**首次启动交互式创建管理员**、`--init-admin/--add-admin/--set-password` CLI、SSH 隧道访问）。

## 四、假设与决策

1. 管理后端独立进程放 `web/server/`（用户确认）；游戏服进程不动，仅 `web_server.py` 增注册 API 与 SPA 下发。
2. 管理端口默认 `127.0.0.1:8080`；远程管理走 SSH 隧道（`ssh -L 8080:127.0.0.1:8080`）。
3. 管理后端不 import 游戏服代码（避免 state.py 日志/GAME_DIR 副作用跨进程耦合），SQL 查询独立编写；表结构由游戏服建表逻辑拥有，管理端只读表结构（admin_* 表由管理端自建）。
4. 注册页玩家密码仍按现状明文存储（与现有登录逻辑一致）；**管理员密码 scrypt 哈希**。玩家密码哈希化列为后续独立任务。
5. 封禁本期仅写 DB 标志位，游戏登录侧检查为后续小改动（在计划中标明）。
6. `web/dist`、`web/public`（复制的客户端素材）提交入库，服务器无 Node；开发机 macOS Node 22 已验证。
7. HnG 风格素材**从客户端 GAME_DIR/images/ 复制**到 `web/public/`（Weapons/Vehicles/Rank/Ribbons 全量 + Soldier 选定立绘，用户确认"直接复制过去用"）；素材随 git 提交，前端自包含、不依赖运行时客户端目录。
8. 装备名称/图标映射由管理端启动时解析 WarDatabase 模板表获得（解码器从 protocol.py 精简复制，不 import 游戏服代码）；解析失败降级显示 template_id。
9. 旧 `GET /account` 此前 404，SPA 返回注册页与 settings.js 的 `account_url` 对齐。

## 五、实施顺序

1. `web/` Vite 脚手架（MPA 双入口 + Element Plus 按需导入 + HnG 主题样式）+ 素材复制（GAME_DIR/images → web/public/）；
2. 注册页应用（HnG 风格视觉 + 表单校验 + API 调用）；管理前端骨架（登录页 + 布局 + 路由）；
3. `web_server.py`：`POST /api/register` + `/assets/*` + `register.html` 下发 + `/admin` 404 + 删旧模板；
4. `web/server/`：config/db/auth/audit/wardb_meta（名称映射）+ routers（登录、仪表盘、玩家列表、玩家详情、**角色装备明细**、战报、状态、统计）；
5. 管理前端对接 API（各 view 实装，含角色装备明细的武器/配件/弹药/载具展示与图标）；
6. 部署集成：requirements / ps1 脚本 / .env.example / .gitignore / README；
7. `npm run build` + 全面验证 + git commit（综合描述，commit 前询问）。

## 六、验证步骤

- 前端：`npm run build` 零 TS 报错，产出 `dist/register.html` + `dist/admin.html`；注册页视觉符合 HnG 风格（深色战场 + 士兵立绘 + 卡其卡片）。
- 注册 API：正常建号 `ok:true`；重复用户名/`reto.` 前缀/非法用户名/短密码/坏 JSON/超长 body → 对应错误且无内部细节外泄；新账号可登录游戏客户端。
- 游戏端点回归不受影响：`GET /`（60ms 延迟 OK）、`/settings.js`、`/wardb_unique`、`/WarDatabase/factiontemplate`、本地化 XML。
- 端口隔离：80 上 `/admin`、`/admin.html` 404；8080 上管理页可访问；外机访问 8080 不通（默认回环绑定）。
- 管理端鉴权：未登录 API 401；错误密码 5 次锁 15 分钟；登录后可查玩家/重置密码/发货币（审计表有记录）；非写操作无 `X-Requested-With` 头被拒（CSRF 防护生效）。
- **管理员账号生成**：首次启动 `admin_users` 为空 → 交互式引导创建（密码不回显，哈希入库）；`--init-admin <用户名> --password-stdin` 非交互模式同效；已初始化后再次 `--init-admin` 被拒绝；`--set-password` 改密后旧 JWT 失效（401）。
- **玩家信息查看**：玩家详情显示账号三货币与角色列表（阵营/兵种/军衔图标）；点进角色显示装备明细——武器（名称+图标+变体）、已装备配件（weapon_modifiers）、弹药类型与剩余（weapon_ammo.condition）、载具与升级件（vehicles/vehicle_upgrades）、勋带进度、徽章槽位；template_id 均正确解析为名称（WarDatabase 映射生效），图标从 `icons/` 正常加载。
- 既有 Python 测试不回归：`tests/test_regression.py` 等。
