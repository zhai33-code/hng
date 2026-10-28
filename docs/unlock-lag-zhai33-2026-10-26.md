# zhai33 解锁卡住：2026-10-25晚间日志排查

用户提供账号zhai33、约21点。仅检查日志和客户端反编译代码，未改业务代码或线上服务。

## 发现

线上完整client_logs.txt中，服务器接收时间2026-10-25 21:17:48的客户端报告，记录了：

```
TypeError: Error #1009
at layout.HnGGUIProgressionOverviewDisplay::ProgressionLevelSkin/pricetag_rollOutHandler()
at layout.HnGGUIProgressionOverviewDisplay::ProgressionLevelSkin/__pricetagCredits_rollOut()
```

客户端原始时间13:17:47.688，与服务端接收时间相差8小时；按相邻解锁请求对齐，香港时间约21:17:47。该客户端记录显示正在操作角色74；账号zhai33（日志pid=1000002）同期大批角色统计响应中包含74，因此该报告与用户账号和反馈时段吻合。公共ClientLogMessage日志未直接打印username，归属依据是角色和同期操作关联，不是按cmd号跨连接强行归属。

关键证据文件：../tmp/lag-client-20261026.txt:7394起，异常在7444–7448；完整晚间game日志保存在../tmp/online-audit-2026-10-26/realtest_game.log.2026-10-25.log。

## 同段网络请求很快完成

客户端自己报告的耗时：

- 21:17:16：AddRibbonLevelRequest，50ms；随后RequestReadCharacterStats，58ms。
- 21:17:18：AddRibbonLevelRequest，51ms；随后RequestReadCharacterStats，58ms。
- 21:17:19：装备勋章请求46ms，统计查询53ms。
- 21:17:44和21:17:45重新打开解锁窗口。
- 21:17:47.610关闭解锁窗口，21:17:47.688发生价格标签鼠标移出回调异常，相隔约78ms。

这些证据表明上述解锁及统计请求完成较快；异常发生在客户端价格标签移出回调。它是与“解锁界面卡住”高度相关的线索，但单条异常不能证明全部卡顿均由它造成，不能据此排除其他时段的网络/服务端问题。

## 代码对应点

现有反编译文件：hng-server-re/decompiled/swf-frontend/scripts/scripts/layout/HnGGUIProgressionOverviewDisplay/ProgressionLevelSkin.as:203–209。

pricetag_rollOutHandler直接访问levelIcon.ribbonIcon.skin，将其转换为HnGGUIButtonCombatRibbonSkin，随后写currentState，没有空值检查。窗口关闭后鼠标移出事件仍触发、子控件/皮肤已销毁或转换结果为空，是需要复现验证的具体路径；当前日志不能确定这几个对象中究竟哪一个为空。

## 服务端侧证据的边界

- 扫描10月25日全日日志，940次AddRibbonLevelRequest和783次解锁通知已读请求均能找到后续同cmd/同响应类型的回执，未找到秒级明显延迟候选。cmd号跨连接会重复，此统计仅作初筛；账号定点判断优先采用客户端毫秒耗时。
- 晚间大厅持续有心跳与其他业务流量，不支持“整个大厅事件循环长时间停止”的判断。全局心跳连续也不能证明某个玩家连接无延迟。
- 普通game日志只打印ClientLogMessage前2048字符，21:17:48这次异常堆栈被截在后面，先前只扫描game日志漏掉了它。完整client_logs.txt才保留全部堆栈。
- 此前已部署的是服务端入库、小队和匹配诊断修复，未包含该客户端皮肤回调问题。

建议下一步针对关闭解锁窗口时的价格标签rollOut事件复现，并在实际客户端构建中处理控件生命周期和空值，再验证金币/银币标签、快速连续解锁及关闭窗口后的事件。不要将此问题直接当作服务器容量不足处理。
