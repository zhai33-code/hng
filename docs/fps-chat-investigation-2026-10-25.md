# FPS 全队 / 小队频道不可用：排查记录

状态：已定位线上 IRC TCP 6667 公网不可达；按用户要求切换 16667 并上线。
未修改游戏二进制。游戏内收发仍需玩家重新登录验证。

## 线上结论与修复

- 用户确认本地聊天正常，线上大厅提示 `IRC socket IOError: Error #2031`。
- SSH 核查线上 `0.0.0.0:6667` 正常监听，本机连接能收到 IRC NOTICE；
  下发地址为 `64.90.31.56:6667`。Windows 防火墙三个配置均关闭。
- 从本地访问同一服务器的 22、80、15110、8080 均成功，6667 持续超时。
  证据定位到公网连接路径，不能仅凭超时认定是哪一层网络规则拦截。
- 用户放行 TCP 16667 后，临时端口转发验证收到 IRC 欢迎消息。
- 按用户明确要求，线上 `irc_server.py` 的监听端口和 `web_server.py`
  的下发地址改为 16667；只替换对应字符串，未覆盖线上其他代码。
  线上备份后缀：`.bak-20261025-234402`。
- 已重启大厅服务，确认 Python 进程直接监听 `0.0.0.0:16667`，
  `/settings.js` 返回 `ircserver="64.90.31.56:16667";`，公网收到 NOTICE。
- 临时 16667→6667 portproxy 已删除；pktmon 诊断已停止并清理本次过滤器。
- 本地相同端口变更已同步，`python tests/test_irc_server.py` 7 组全部通过。

以下是此前在尚未得到“本地正常、仅线上失败”信息时的静态分析记录。

## 已确认

- `server/logs/realtest_game.log` 中 2026-10-25 的 FPS IRC 连接注册成功，
  USER 形如 `c8395162525689480254`，与大厅连接的 USER `1` 不同。
- 旧日志没有记录 JOIN 请求及 404 等错误，不能据此推断客户端没有发送 JOIN。
- 大厅 `GetChatChannelsSubscribedResponse (0 channels)` 表示没有自定义订阅，
  不能据此断定缺失 FPS 的全队、小队频道。
- `irc_server._cmd_join` 已支持 `JOIN :#a,#b`，并自动创建频道；
  不需要预先把所有战斗频道写入数据库。
- 工作目录 `D:/HnG-test/player.dll`、已有 `player.dll.i64` 与安装目录
  `D:/Program Files/HnG/player.dll` 不是同一版本。不得混用函数地址。
  当前没有运行中的游戏，实际复现使用的 DLL 路径仍需运行时核实。

## IDA 9.1 分析

通过本机 `D:/Program Files/IDA Professional 9.1/idat.exe` 和 Hex-Rays 分析。
现有 IDB 用副本打开；安装目录 DLL 也复制后建立新库。
后续批处理使用项目现有 `work/ida_tool.py`。

安装目录 DLL 的分析副本：
`server/tmp/fps_current_player.dll.i64`。以下函数名已保存到该分析库：

| VA | 名称 | 行为 |
| --- | --- | --- |
| 0x1802D4660 | irc_Fps_EnableTeamChannel | 设置 +1993，记录 team ID 到 +1995，清除已同步标志 +1989 |
| 0x1802D4680 | irc_Fps_EnableSquadChannel | 设置 +1994，记录 squad ID 到 +2000，清除 +1989 |
| 0x1802D5DF0 | irc_Fps_UpdateChannels | 根据开关、team / squad 实体创建本地频道 |
| 0x1802D47D0 | irc_Fps_IsChannelAvailable | 对 type 5 / 6 检查开关及当前 team / squad ID |

全队、小队频道是否可用，依赖客户端生成的频道列表及当前身份；
“IRC 已连接”本身不足以证明频道初始化正确。
启用函数的上游调用路径、HUD 提示原文及实际失败位置尚未闭环。

## 本次代码变更与验证

`server/irc_server.py` 新增以下诊断信息：

- JOIN / PART：昵称、USER、频道名。
- 401 / 403 / 404 / 405 / 421 / 442 / 451 / 461：错误码及协议错误原因。
- 不记录 PASS、认证密钥及聊天正文。

从 server 目录执行 `python tests/test_irc_server.py`，7 组测试全部通过。
此结果验证已有协议路径没有因日志改动而回归，不代表游戏内故障已经修复。

## 下一次复现

启动包含本次日志修改的服务端，进一局后分别尝试全队与小队发送，记录提示原文。
结合 native USER 的 JOIN / numeric 日志判断：

1. 未发 JOIN：检查客户端频道开关、实体列表、注册完成 / 同步门控。
2. 发 JOIN 后收到 403 / 405：检查频道名、批量格式及频道上限。
3. PRIVMSG 收到 404：对比发送目标与已经加入的频道。
4. JOIN / 发送均成功但 HUD 报无频道：检查本地 type 5 / 6 列表与当前 team / squad ID。

复现后再确定修复位置，避免靠创建无关大厅频道或硬编码频道名掩盖问题。
