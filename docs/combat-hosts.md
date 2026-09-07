# 战斗服多机部署与容量配置

combat server（每局战局的独立 `hng.exe /D` 进程）支持分布在多台机器上运行，
各机独立容量上限与 UDP 端口池；主机表在管理后台热配置（server_config 表
`combat_hosts` 行），保存即生效，无需重启游戏服。

单机部署（默认）不需要任何配置：游戏服按环境变量回退
（`MM_MAX_COMBAT_SERVERS` / `MM_COMBAT_PORT_BASE` / `MM_COMBAT_PORT_MAX` /
`HNG_COMBAT_WARSERVER`），与多机改造前行为完全一致。

## 架构

```
客户端 hng.exe ──UDP 直连──> combat server (hng.exe /D, 任意一台主机)
                                │  TCP 回连握手 (BattleFieldSettings)
                                ▼
玩家 hng.exe ──TCP──> warserver (游戏服 :15110)  ──HTTP──> combat_agent (远端机)
    匹配成局时按各机剩余容量选机、分配端口
```

- **warserver**（realtest_server.py）：成局时从主机表选机，本机条目直接
  spawn 进程；远端条目经 HTTP 调用该机的 agent 拉起。
- **combat_agent.py**：远端 Windows 机上的常驻进程（Python 3.11 stdlib，
  零第三方依赖，可 PyInstaller 打包成 exe 当服务跑），提供
  `POST /launch`、`POST /stop`、`GET /status`；鉴权为 HMAC 请求签名
  （时间戳/nonce/签名三头，密钥不上链路 + 防重放 + 失败限速，详见
  combat-host-deploy.md §4）。
- **握手认领**：combat server 启动后回连 warserver 15110 端口做
  BattleFieldSettings 握手，warserver 按来源 IP 匹配主机表认领实例
  （127.0.0.1 视为本机 spawn 条目）；未匹配的来源回退全局 FIFO 认领。

## 字段说明（管理后台「服务器配置 → 战斗服主机」）

| 字段 | 说明 |
| --- | --- |
| 名称 | 主机标识（实例归属键，重名校验失败会整体回退单机） |
| 内网地址 | 该机在 warserver 网络中的地址（握手来源 IP 匹配用；远端机不能填 127.0.0.1） |
| 公网地址 | 客户端 game_connect 连接地址（缺省 = 内网地址；客户端直连需填公网 IP） |
| 最大并发 | 本机同时运行的 combat server 数上限，0 = 不限（端口池自然封顶） |
| UDP 端口池 | 本机独立端口段（base..max），各机互不挤占，可用同号段 |
| Agent | 空 = 本机直接 spawn；`http://ip:port` = 远端 combat_agent 地址 |
| warserver | 该机 combat server 回连 warserver 的地址（NAT/跨网段时填公网入口；空 = 游戏服 `HNG_COMBAT_WARSERVER`） |

示例：A 机最多 4 个实例、B 机最多 8 个：

```json
{
  "hosts": [
    {"name": "A机", "addr": "10.0.0.2", "public_addr": "203.0.113.11",
     "max": 4, "port_base": 5678, "port_max": 5699,
     "agent": "http://10.0.0.2:15116", "warserver": ""},
    {"name": "B机", "addr": "10.0.0.3", "public_addr": "203.0.113.12",
     "max": 8, "port_base": 5678, "port_max": 5707,
     "agent": "http://10.0.0.3:15116", "warserver": ""}
  ]
}
```

## 选机与排队

成局时按顺序过滤候选机：容量有余 + 端口池有空闲 + 非 agent 冷却，
取**剩余绝对容量最大**者（并列取配置顺序）。全部不满足时该局进
`PENDING_MATCHES` 排队，有空位后自动重派。

agent 拉起/停止失败会记 60 秒冷却（期间选机跳过该机，防一台死机拖慢
每次成局），管理后台仪表盘「战斗服主机」表中显示「冷却中（拉起失败）」。

## 实例生命周期与异常回收

- 战局结束 / 全员掉线 → warserver 停止实例（本机 kill / 远端 agent
  `POST /stop`），槽位立即释放。
- combat server 进程崩溃 / 断电重启等孤儿槽位：
  - **握手前**（预留 120 秒未握手）：预留超时自动回收；
  - **握手后**：warserver 侧 TCP 断连钩子按实例的 combat 连接数清零回收，
    并由 watchdog 兜底（进程不存在 = 释放）。
- 游戏服重启：COMBAT_INSTANCES 内存表清空；远端机上的进程由 agent 自行
  保留（可 `GET /status` 查看，必要时手工 `taskkill` 或重启 agent 所在机）。

## 远端机部署步骤

完整操作手册（含目录拷贝/防火墙命令/密钥生成与签名鉴权/验证与排查）见
[combat-host-deploy.md](combat-host-deploy.md)。概要：

1. 拷贝 combat-server 目录到远端机（版本与 player.dll 须与游戏服一致）。
2. 远端机启动 `combat_agent.py --port 15116 --token <与游戏服同值>
   --combat-dir <目录>`，防火墙放行 agent TCP 端口 + combat UDP 端口池。
3. 游戏服 `.env` 设 `HNG_AGENT_TOKEN`（改后须重启游戏服）。
4. 管理后台「服务器配置 → 战斗服主机」添加该机条目并保存（热配置）。
   **注意**：远端条目的 warserver 字段必填（留空回退 127.0.0.1 会让远端
   机连它自己）。
5. 仪表盘确认 Agent「正常」，打一局验证（日志 `launched via agent` →
   `handshake ok` → `READY`）。

## 运维速查

- 当前各机运行数：管理后台仪表盘「战斗服主机」表，或
  `GET http://<游戏服>/online.json` 的 `combat_hosts` 字段。
- 远端机实际进程：带签名探测 `GET /status`（一行命令见
  combat-host-deploy.md §4）。
- 主机表配置错误（重名 / 端口池非法）：游戏服日志
  `[combat-hosts]` 前缀提示，解析失败整体回退单机 env 行为，不影响成局。
- 排队日志：`match queued (combat hosts busy: A机:4/4, B机:8/8, ...)`，
  各机 `running/max`（冷却中带 `(cooldown)` 标记）。

## 相关代码

| 文件 | 职责 |
| --- | --- |
| `server/combat_hosts.py` | 主机表解析 / 选机 / 端口分配 / 冷却（纯数据层，无 IO 依赖） |
| `server/combat_instances.py` | 实例生命周期：预留 / spawn / agent HTTP 拉起 / 停止 / 断连回收 / 握手认领 |
| `server/combat_agent.py` | 远端机常驻 agent（launch / stop / status） |
| `server/matchmaking.py` | 成局时选机占坑、game_connect 生成（`_game_connect_for`） |
| `web/server/services/config_svc.py` | 管理后台 combat_hosts 配置校验与读写 |
| `web/frontend/src/admin/views/config/CombatHostsPanel.vue` | 管理后台主机表编辑面板 |
| `tests/test_combat_hosts.py` | 62 项回归（解析 / 选机 / 预留认领 / agent 拉起失败恢复 / 排队重派） |
