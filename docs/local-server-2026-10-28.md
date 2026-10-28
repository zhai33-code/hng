# 本机私服部署与修复记录（2026-10-28）

本文件记录这一轮把「客户端能进对局」跑通所做的所有改动、踩过的坑和可回滚点。
配置细节见 [tmp/LOCAL-SETUP.md](../tmp/LOCAL-SETUP.md)（本机速查，不入库的路径都在那里）。

---

## 1. 运行架构（本机）

```
客户端  E:\Hng\Hng\Hng\HeroesAndGeneralsDesktop.exe
   │  settings.js / WarDatabase / CAS        ← HTTP 80
   │  大厅协议                               ← TCP 15110
   │  IRC                                    ← TCP 16667
   │  HostingCenter ping                     ← UDP 20201
   ▼
游戏服  E:\HnG-test\realtest_server.py       pid 会变，用 admin_restart_server.ps1 拉起
   │  PostgreSQL                            ← 127.0.0.1:5432（便携版 _setup/pgsql）
   │  战斗统计                               ← TCP 15115
   ▼
战斗服  E:\HnG-test\combat-server\hng.exe /D <port>
        _packed / WarDatabase / texts 用 junction 指回客户端目录
        UDP 5678–5699（每局一个端口）
```

管理后台独立进程：`web/server/main.py` → http://127.0.0.1:8080/admin.html

---

## 2. 三项必须保持的配置（`.env`，不入库）

| 变量 | 值 | 为什么 |
| --- | --- | --- |
| `HNG_PUBLIC_HOST` | `127.0.0.2` | `127.0.0.1:80` 被本机其它程序占用，改 127.0.0.2 避开 |
| `HNG_HC_PING_HOST` | `127.0.0.1` | UDP ping 回包源地址必须与目标一致，否则 Windows 丢弃 |
| `HNG_COMBAT_SERVER_DIR` | `E:\HnG-test\combat-server` | 用客户端目录当战斗服会在 `PACK: SERIAL` 后静默退出 |
| `MM_EOS_TOKEN` | `1` | 战斗服 EAC 路径需要 `eos_openid_token`（PlayerSettings f999） |
| `MM_PACKTHREADS` | `0` | 引擎默认串行打包 |
| `MM_COMBAT_START_COUNTDOWN_OVERRIDE` | `0` | 不覆盖开局倒计时 |

---

## 3. 这一轮修的三个 bug

### 3.1 HostingCenter ping 地址（`handlers/views.py`）

**症状**：客户端弹 `UNABLE TO PING GAME SERVERS`，匹配按钮点了没反应。

**根因**：hngsync 的 ping 用 `connect()` 模型，Windows 会丢弃源地址不匹配的 UDP 回包。
应答器绑 `0.0.0.0:20201`，客户端 ping `127.0.0.2` 时内核回包源恒为 `127.0.0.1` → 被丢弃 → 超时。
实测三行定案：

```
sendto 127.0.0.2   → 收到回包（源 127.0.0.1）
connect 127.0.0.2  → TimeoutError
connect 127.0.0.1  → 收到回包
```

**改法**：ping 地址单独可配 `HNG_HC_PING_HOST`，默认仍是 `PUBLIC_HOST`（线上不设即原行为）。

### 3.2 管理后台启动脚本（`web/start_admin.ps1`）

**症状**：脚本一执行就报 `ParameterArgumentValidationError`，后台起不来。

**根因**：`Start-Process` 不允许 `-WindowStyle Hidden` 与 `-RedirectStandardOutput/-RedirectStandardError` 共存。

**改法**：`-WindowStyle Hidden` → `-NoNewWindow`；补 8080 幂等预检与启动轮询。

### 3.3 战斗服 int3 退出（`combat-server/player.dll`，二进制 patch，不入库）

**症状**：战斗服握手成功、地图加载完成后以 `code=0x80000003`（STATUS_BREAKPOINT）退出，客户端停在 `IN BATTLE` 倒计时。

**根因**：HnG 自带分配器的 `operator delete` 堆头完整性校验失败触发 `int3`。
`Get-WinEvent Application/1000` 五次一致指向 `player.dll` 偏移 `0x4AD46D`。

**Patch**（两处同族，各 3 字节等长）：

| 站点 | 文件偏移 | 原字节 | 新字节 |
| --- | --- | --- | --- |
| site1 | `0x4AC86B/0x4AC86D` | `74 01 CC` | `75 76 90` |
| site2 | `0x4AC6C2/0x4AC6C4` | `74 01 CC` | `75 76 90` |

语义：`je +1` → `jne +0x76`（跳函数结尾），`int3` → `nop`。
校验通过时与 patch 前完全同一条释放路径；校验失败时跳过释放、不碰可疑指针。

- patched sha256：`6ca9adf6836fa2a7…`
- 原版 sha256：`070259a82c8ad1cf…`
- 回滚：`combat-server\player.dll.bak_20261028_011807` 覆盖回去

> 这是把「崩」降级为「跳过释放（泄漏）」。真 bug 是某处越界写坏了堆头，长局若见内存持续上涨需回头查。

---

## 4. 过程中被破坏又恢复的文件

| 文件 | 现象 | 恢复方式 |
| --- | --- | --- |
| `player.dll` | 被 hngsync 同步清掉，战斗服 `LoadLibrary player.dll failed` | 从 `Hng.z01`+`Hng.zip` 分卷按中央目录手工解出（CRC `0xd0d68456`），SHA256 `070259A8…` |
| `_packed\pak.bin` | 被清成 0 字节 | 确认分卷里原始就是 0 字节（运行期生成），改用 `D:\HnG\HnG` 版本后仍秒退 → 证明与 pak 无关 |

结论：`pak.bin` 不是崩溃原因，`player.dll` 缺失才是 `LoadLibrary` 报错的直接原因。

---

## 5. 客户端侧的两个劫持点

1. **SWF 写死 settings 主机**：`HeroesAndGeneralsDesktop.swf` 解压流偏移 `0x9BB159D` 处常量 `64.90.31.56/`，客户端用它取 settings.js，再作为 `CMD_DOWNLOADNAMED` 参数下发给 hngsync（`-u` 地址）。hosts 对 IP 字面量无效，故用环回别名把 `64.90.31.56` 劫到本机（**部署到真服务器前必须先删该别名**）。
2. **hosts 重定向**：`game.hng-group.com`、`fs1~fs6.hng-group.com`、`api.epicgames.dev` → `127.0.0.2`。

---

## 6. 常用命令

```powershell
# 一键起（PG + 游戏服 + 自检）
powershell -ExecutionPolicy Bypass -File E:\HnG-test\tmp\run_all.ps1

# 带管理后台
powershell -ExecutionPolicy Bypass -File E:\HnG-test\tmp\run_all.ps1 -WithAdmin

# 只重启游戏服
powershell -ExecutionPolicy Bypass -File E:\HnG-test\admin_restart_server.ps1

# 客户端
Start-Process E:\Hng\Hng\Hng\HeroesAndGeneralsDesktop.exe -ArgumentList 'launchMode=desktop'
```

---

## 7. 代码托管

| remote | 地址 | 说明 |
| --- | --- | --- |
| `github` | `git@github.com:zhai33-code/hng.git` | 主力，`main` 跟踪它 |
| `origin` | `git@gitee.com:zhai33/hng-re.git` | 备份，另有 `local-baseline` 分支 |

两条独立历史已通过 `f9971cb` 合并提交汇合，`git log --graph` 可对照。
