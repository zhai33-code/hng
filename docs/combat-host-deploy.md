# 新增战斗服主机部署手册（runbook）

把一台新的 Windows 机器加入战斗服集群（多机部署）的完整操作步骤。
架构与选机规则说明见 [combat-hosts.md](combat-hosts.md)，本文只讲"怎么装"。

## 0. 你需要准备什么

| 项 | 要求 |
| --- | --- |
| 远端机 | Windows 10/11 x64，内存 ≥ 8 GB（每个实例约 1-2 GB），**无需**管理员权限、PostgreSQL、服务器仓库 |
| 网络 | 远端机出站可达游戏服 15110/15115；入站可开放 agent 端口 + UDP 端口池（见 §3） |
| 文件 | 从游戏服机器拷贝 combat-server 目录（约 2.7 GB）+ `combat_agent.py` |
| 游戏服侧 | 管理后台权限（添加主机条目） |

**端口与流向总表**（"游戏服"指跑 realtest_server.py 的机器）：

| 方向 | 端口 | 协议 | 用途 |
| --- | --- | --- | --- |
| 远端机 → 游戏服 | 15110 | TCP | 战斗服回连握手（BattleFieldSettings）+ 战局通信 |
| 远端机 → 游戏服 | 15115 | TCP | 勋带/统计上报（GetTotals，随 warserver 地址自动同机） |
| 游戏服 → 远端机 | 15116（默认） | TCP | agent 拉起/停止/查询（仅游戏服需要可达） |
| 玩家 → 远端机 | 端口池（如 5678-5699） | **UDP** | 战斗数据（**公网可达**，客户端直连） |

## 1. 远端机：放置 combat-server 目录

从游戏服机器把 `combat-server` 目录整个拷过去（以本仓库布局为例是
`D:\HnG-test\combat-server`，约 2.7 GB，含 `hng.exe`、`player.dll`、
`_packed` 地图、`WarDatabase`）。

```powershell
# 在游戏服机器上执行（示例：经管理共享拷到 B 机；也可压缩包/移动硬盘）
robocopy D:\HnG-test\combat-server \\B机\C$\hng\combat-server /E /XF *.log *.old /R:1 /W:1
```

拷完核对两件事（**版本必须与游戏服机器一致**，否则客户端连不上/行为异常）：

```powershell
# 远端机上：版本号（当前 173293）
Get-Content C:\hng\combat-server\version.txt
# player.dll 哈希（须与游戏服机器一致；补丁版承载下雪等补丁逻辑）
(Get-FileHash C:\hng\combat-server\player.dll -Algorithm SHA256).Hash
```

## 2. 远端机：安装并启动 agent

`combat_agent.py` 在仓库 `server\` 下，纯 Python 3.11 标准库、零第三方依赖。

```powershell
# 目录布局建议
# C:\hng\
# ├── combat-server\     ← §1 拷来的
# └── combat_agent.py    ← 从仓库 server\combat_agent.py 拷来

# 启动（--token 与游戏服 .env 的 HNG_AGENT_TOKEN 同值，见 §4）
python C:\hng\combat_agent.py --port 15116 --token <TOKEN> --combat-dir C:\hng\combat-server
```

控制台应输出：

```
[agent] combat dir: C:\hng\combat-server
[agent] listening on 0.0.0.0:15116 (hmac auth: on)
```

**免装 Python 的部署方式**（可选）：在有 Python 的机器上
`pip install pyinstaller && pyinstaller --onefile combat_agent.py`，把
`dist\combat_agent.exe` 拷到远端机，命令行参数完全相同。

**开机自启（推荐）**：nssm 注册为服务，或计划任务（登录时触发）：

```powershell
# nssm 示例
nssm install HngCombatAgent "C:\hng\combat_agent.exe" "--port 15116 --token <TOKEN> --combat-dir C:\hng\combat-server"
nssm start HngCombatAgent
```

**远端机防火墙**（管理员 PowerShell 一次性执行）：

```powershell
# agent 端口（建议限定来源为游戏服 IP）
netsh advfirewall firewall add rule name="HnG Agent" dir=in action=allow protocol=TCP localport=15116
# UDP 端口池（公网开放，范围 = 你打算在主机表里填的 port_base..port_max）
netsh advfirewall firewall add rule name="HnG Combat UDP" dir=in action=allow protocol=UDP localport=5678-5699
```

## 3. 游戏服机器：放行出站、确认连通

游戏服须能访问远端机 agent（15116）与被动接受远端机回连（15110/15115，
本就已监听）。验证：

```powershell
# 游戏服机器上
Test-NetConnection <远端机IP> -Port 15116     # TcpTestSucceeded : True
Test-NetConnection <远端机IP> -Port 15116 | Select RemotePort
# agent 状态探测（设了密钥要走签名，不能裸 curl；见 §4 的探测命令）
#   预期：{"instances": []}
```

## 4. 游戏服：设置共享密钥（改 .env 须重启）

先在任意一台机器上生成强随机密钥（**别用自编短语**，跨公网会被爆破）：

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

游戏服 `.env` 增加（或确认）：

```ini
HNG_AGENT_TOKEN=<生成的密钥>
```

远端机 agent 启动参数 `--token` 用**同一个值**（§2 的命令）。

> **鉴权机制（跨公网明文 HTTP 安全）**：warserver 每个请求带
> `X-Agent-Timestamp / X-Agent-Nonce / X-Agent-Signature` 三头，签名 =
> HMAC-SHA256(密钥, ts+nonce+METHOD+path+sha256(body))。密钥**只进签名、
> 不上链路**——抓包抓不到凭证；nonce 一次性 + 时间戳 ±90s——抓到的完整
> 请求也无法重放；签名失败按 IP 限速（60s 内 10 次 → 滚动封锁）。
> `/stop` 只允许杀 agent 自己拉起的进程（密钥泄漏也仅波及战斗进程）。
>
> **时钟要求**：两端偏差须在 ±90s 内（VPS 默认 NTP 同步即满足）。
>
> **注意**：`HNG_AGENT_TOKEN` 在进程启动时读取（combat_instances.py 模块级），
> 修改后必须重启游戏服才生效（80 端口绑定需管理员，走
> `admin_restart_server.ps1`）。规则：两边同值；游戏服留空则远端
> agent 也应留空（= 开放，仅限可信内网调试）。

手工探测 agent 状态（带签名的一行命令）：

```powershell
python -c "import sys; sys.path.insert(0, r'C:\hng'); import http.client, combat_agent; h=combat_agent.sign_request('GET','/status',b'',r'<TOKEN>'); c=http.client.HTTPConnection('<远端机IP>',15116); c.request('GET','/status',headers=h); print(c.getresponse().read().decode())"
```

## 5. 管理后台：添加主机条目（热配置，保存即生效）

打开 `http://127.0.0.1:8080` → 服务器配置 → **战斗服主机** → 添加主机：

| 字段 | 填什么 | 示例 |
| --- | --- | --- |
| 名称 | 标识（仅显示/日志用） | `B机` |
| 内网地址 | 游戏服看到的远端机 IP（握手认领按此匹配，**远端机不能填 127.0.0.1**） | `10.0.0.3` |
| 公网地址 | 客户端连接地址（game_connect 用；有公网 IP 必填，缺省=内网地址） | `203.0.113.12` |
| 最大并发 | 该机实例上限（0=不限，端口池封顶） | `8` |
| UDP 端口池 | 该机独立端口段（= §2 防火墙放行的范围；各机可用同号段） | `5678 - 5699` |
| Agent | 远端 agent 地址 | `http://10.0.0.3:15116` |
| warserver | 远端机回连游戏服的地址（**远端条目必填**，见下方注意） | `10.0.0.2:15110` |

> **warserver 字段陷阱**：留空时回退游戏服 env `HNG_COMBAT_WARSERVER`
> （默认 `127.0.0.1:15110`）——远端机会去连**它自己**，握手永远超时。
> 远端条目必须显式填游戏服在远端机视角的可达地址（内网 IP 或公网入口）。
> 统计服地址自动取该字段的 host 部分（15115 与 warserver 同机），不用单独配。

保存。仪表盘（首页）底部「战斗服主机」表应出现新机器，Agent 列显示
「正常」。

## 6. 验证（打一局）

1. 开一局（训练模式单人成局最快：进游戏 → 训练）。
2. 游戏服日志 `realtest_game.log` 依次出现：

   ```
   combat server #<seq> launched via agent B机 (pid=..., mission=..., udp/5678)
   combat server #<seq> handshake ok (agent pid=..., mission=...)
   combat server #<seq> READY (udp/5678 bound, map load took Ns)
   ```

3. 仪表盘「活跃战局」的「主机」列显示 `B机`；「战斗服主机」表 running 变 1。
4. 客户端正常进入战斗（连接地址 = 条目公网地址:端口）。
5. 战局结束后 running 回落 0；远端机用 §4 的探测命令查看实例清空。

## 7. 常见问题排查

| 现象 | 原因 → 处理 |
| --- | --- |
| 仪表盘 Agent 列「冷却中（拉起失败）」 | 看游戏服日志 `agent launch FAILED`：HTTP 403 = 密钥不一致（或时钟偏差超 ±90s）；连接失败 = agent 没起/防火墙；`combat dir not found` = 远端 --combat-dir 路径错 |
| 签名频繁 403 但密钥确认一致 | 两端时钟偏差超 90s：`w32tm /resync` 对时后重试；agent 控制台 DENY 日志会给出具体原因（签名/时间窗/重放/IP 封锁） |
| `handshake TIMEOUT after 120s` | 远端机连不上游戏服 15110：条目 warserver 字段填错/未填（见 §5 陷阱）；看远端机 `combat-server\hng_boot.log` 是否有 `Connected to WarServer` |
| 客户端进不去战斗（一直转圈） | 公网地址不可达：public_addr 不是公网 IP、路由器未映射、防火墙 UDP 端口池未放行 |
| 战局地图错乱（进了别的图） | 握手认领错配：条目「内网地址」与实际来源 IP 不符（远端机经 NAT 出站时，填 NAT 出口 IP） |
| 局内不涨 XP/勋带 | 远端机连不上统计服 15115：warserver 字段的 host 部分不通（statserver 随该地址） |
| 没有下雪效果 | 远端机 player.dll 不是补丁版（§1 哈希核对） |
| agent 拉起的专用服异常退出 | 看远端机 `combat-server\combat-server-<seq>.log`；怀疑外联 DNS 时可把远端机 hosts 的 `game.hng-group.com` 等指向游戏服 IP（对齐游戏服机器的做法） |

## 8. 日常维护

- **改容量/端口池/增删机器**：管理后台改主机表保存即生效（对下一局成局），不用重启任何进程。
- **游戏服重启**：远端机上的战斗服进程不受 agent 影响；warserver 重启后不再登记旧实例，必要时用 §4 探测命令查看、`taskkill` 清理，或直接重启远端机。
- **升级版本**（客户端/combat-server/player.dll 变更）：两台机的 combat-server 目录必须同步更新，否则新起战局与客户端版本不匹配。
- **agent 升级**：替换 `combat_agent.py` 后重启 agent 进程即可，不影响在跑的战斗服进程（agent 重启丢失实例登记表，在跑实例退出后坑位由 warserver 看门狗回收）。
- **轮换密钥**：两端同值原则；游戏服改 `.env` 后须重启，远端机改 `--token` 后须重启 agent。建议泄露疑似时立即轮换。

## 附：本次部署检查单

```
□ 远端机 combat-server 目录已拷贝，version.txt / player.dll 哈希与游戏服一致
□ 密钥已用 secrets.token_urlsafe(32) 生成（非自编短语）
□ 远端机 agent 已启动，控制台显示 listening + hmac auth: on
□ 远端机防火墙放行 TCP 15116（建议限源=游戏服 IP）+ UDP 端口池
□ 游戏服 .env 已设 HNG_AGENT_TOKEN，且已重启游戏服（如密钥是新加的）
□ 游戏服用 §4 签名探测命令访问 agent /status 返回 {"instances": []}
□ 管理后台已添加主机条目（warserver 字段已填），仪表盘 Agent 显示「正常」
□ 打一局：日志 launched via agent → handshake ok → READY，客户端能进战斗
□ 战局结束 running 回落 0
```
