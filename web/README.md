# HnG 私服 Web 前端 + 管理后端

Vite + Vue 3 + TypeScript + Pinia + Element Plus + SCSS（pnpm 管理，依赖锁 `~` 次版本）。

## 目录结构

```
web/
  frontend/                     # 前端工程（Vite + Vue3 + TS + Pinia + Element Plus + SCSS）
    join.html / admin.html       # MPA 双入口
    src/join/                    # 入伍 SPA（history 路由：/join 注册 + /join/veteran 外服迁移；部署到游戏服 80 端口，hng-re/web_server.py 托管 dist）
    src/admin/                    # 管理后台 SPA（hash 路由；FastAPI 同域托管，API_BASE=/api）
    src/styles/                   # HnG 主题（SCSS 变量 + Element Plus 变量覆盖）
    public/icons/                 # 客户端素材（Rank 军衔 / Vehicles 载具 / Ribbons 勋带 / CombatBadges 徽章）
    dist/                         # 构建产物（不入库；部署时手动上传到服务器对应目录）
  server/                       # 管理后端（FastAPI + asyncpg，独立进程，仅共享 PostgreSQL）
  start_admin.ps1               # Windows 启动脚本
```

## 开发

```powershell
cd web/frontend
pnpm install
pnpm dev          # /join.html、/admin.html；/api -> 127.0.0.1:80，/admin/api -> 127.0.0.1:8080
pnpm build        # vue-tsc 类型检查 + vite build -> dist/
```

## 管理后端启动（web/ 目录）

```powershell
# 依赖：pip install -r ../requirements.txt（fastapi/uvicorn/asyncpg/PyJWT）
# 首次：创建管理员（生成随机密码，仅显示一次）
python -m server.main create-admin <用户名>
# 追加管理员 / 改密（改密使该账号全部会话失效）
python -m server.main add-admin <用户名> <密码>
python -m server.main set-password <用户名> <新密码>

# 启动服务（默认 127.0.0.1:8080，仅回环；或直接运行 start_admin.ps1）
python -m server.main
```

配置见仓库根 `.env`（`HNG_ADMIN_BIND` / `HNG_ADMIN_PORT` / `HNG_ADMIN_SECRET`，
示例见 `.env.example`）。远程管理走 SSH 隧道，不建议对公网开放。

## 安全设计

- 管理员密码 scrypt 哈希（`hashlib.scrypt`，格式 `scrypt$N$r$p$salt$hash`）
- 会话：PyJWT HS256 + HttpOnly SameSite=Strict Cookie，2 小时时效；改密踢全部会话
- 登录限速：按 IP 15 分钟 5 次失败锁 15 分钟
- CSRF：写操作要求 `X-Requested-With: XMLHttpRequest` 自定义头
- 审计：全部写操作入库 `admin_audit_log`（管理员/操作/目标/变动值/IP）
- 响应头：`X-Content-Type-Options` / `X-Frame-Options: DENY` / `Referrer-Policy`
- 注册 API（游戏服侧）：用户名黑名单（reto./[admin] 前缀）、Content-Length 64KB 上限、错误信息不外泄

## 功能

- 注册页：HnG 官方风格，2-24 位用户名 / 4-64 位密码校验
- 管理后台：仪表盘（注册趋势/统计）、玩家管理（搜索/详情/重置密码/调整货币/封禁）、
  角色装备明细（武器+配件+弹药、载具+升级件、勋带、军衔徽章）、战报、服务器健康检查、审计日志
- 装备名称/军衔等级：静态数据表（`server/templates_data.py`，自客户端 WarDatabase
  提取固化），管理端不依赖客户端文件；查表未命中降级为 `#id` 显示。模板数据升级时
  重新提取并更新该文件即可
