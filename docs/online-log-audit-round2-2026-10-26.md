# 第二轮线上日志排查

只读检查最新client_logs.txt和当前game日志；未修改或重启线上服务。

本地快照：../tmp/audit-round2-20261026/。当前game日志由PowerShell读取（tar读活动文件遇到共享访问限制），最多取末25000行。快照内未发现HANDLER-ERR、CRASHED、failed、UNEXPECTED或新的定价/免费资格拒绝候选；仅说明本次采集窗口，不代表功能均被玩家实际触发。

## 新确认：解锁动画回调也存在空引用异常

client_logs.txt的2026-10-25 22:16:12报告：

```
TypeError: Error #1009
ProgressionOverviewWindowMediator/unlockSelected()
TweenMax/render()
```

这是与21:17价格标签rollOut不同的调用路径。该段操作角色687；22:16:00和22:16:09两次AddRibbonLevelRequest分别79ms、151ms完成，后续统计查询84ms、87ms；22:16:12.158打开UnlockCombatBadgeWindow，12.217关闭ProgressionOverViewWindow，12.264抛出异常。

当前反编译ProgressionOverviewWindowMediator.as:673安排TweenMax.delayedCall(0.4, unlockSelected)，onRemove:252–275移除信号并clear子视图，但没有取消该延迟回调；unlockSelected虽检查view非空，后续直接访问子控件、角色及表行。日志支持窗口生命周期/回调竞态这一具体排查方向，尚未实机复现确认究竟哪个对象为空，不应宣告根因已完全闭环。

同函数在10月25日03:03的报告中也有Error #1009，说明不是只出现过一次。不能将这两次自动归到zhai33账号。

## 新确认：两次启动流程卡在本机HnG Sync连接

22:27:54和22:37:06两份type=1报告均显示LocalcallsService连接50010端口失败，重试计数从184到200，最后FrontScreen提示“连接HnG Sync出现问题”。

反编译LocalCallsService加载127.0.0.1:50009的跨域策略，并通过m_SyncClient.Connect连接默认50010端口（Steam/Discord/Epic有不同端口分支）；结合HnGSyncClient错误和FrontScreen提示，这是玩家本机同步/启动服务链路。它与服务器15110游戏协议和16667 IRC聊天连接不同。日志证明连接失败，但不能区分hngsync未运行、监听/发行平台端口不一致、进程退出或本机拦截。不能仅凭错误#2031断定防火墙原因。

该问题可表现为下载/启动阶段卡住。下一步需检查对应玩家本机进程、监听端口和hngsync日志；仅凭线上服务端数据不能直接修复玩家本机。

## 优先处理方向

优先在客户端解锁窗口关闭时取消延迟回调，并检查价格标签事件与销毁控件的交互，再复现快速连续解锁、弹出勋章奖励和关闭窗口场景。服务端此前部署不包含这两处客户端处理。HnG Sync启动失败应单独检查玩家本机，避免与全服性能混为一谈。
