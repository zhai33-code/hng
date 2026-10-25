# 线上日志排查（2026-10-26）

最新状态：本次确认的问题已修复，并于2026-10-26 00:23（香港时间）部署。详见deployment-2026-10-26.md；下文“未部署”均为排查及修复时的历史记录。

只读排查，未修改业务代码、线上文件或数据，未重启服务。

## 范围

通过SSH于香港时间2026-10-26 00:05起采集：

- 10月25日整日 realtest_game.log.2026-10-25.log，242354行。
- 10月26日实时 realtest_game.log 的采集时快照，911行。
- realtest_stderr.log 最后20000行，以及整个累计文件的错误分类、计数和定向上下文。累计文件包含9月历史记录，计数不能视为一天的发生次数。
- 只读核对线上 squad.py、mm_handler.py 部分代码，并对照本地源码。

采集文件保存在 ../tmp/online-audit-2026-10-26/。包含用户日志，仅本地保存。

## 明确存在的新发现：隐式小队成功分支仍查DB，触发int32溢出

10月25日20:56:29两次、21:01:26一次、21:20:19两次，共5次：

```
SetSquadMatchMakingFlag: in-memory squad 7699723661786535486 → no-op OK
[DB] set flag sync push failed: ... value out of int32 range
SetSquadMatchMakingFlagResponse (... response=1)
```

累计stderr同类错误94次（跨日期）。

handlers/squad.py:1512–1538已识别内存solo/自动凑队，主动避开持久化写入；但1561–1582的公共成功后同步分支再次无条件调用 STORE.get_squad(squad_session_id)。63位临时小队行ID被用来查询32位数据库主键，导致错误。线上源码有相同分支。

离线 tmp/reproduce_audit_squad_flag.py 已调用真实handler复现：本人内存solo行识别为no-op OK之后，仍发起一次get_squad查询；错误被捕获，协议response=1。

影响边界：确认不必要的DB查询和异常日志；没有证据显示此处导致解散失败、匹配失败或整队丢失。不能把成功响应直接定性为错误，因为隐式小队本来就约定无持久化意义并返回OK。建议成功后同步仅对持久化小队执行；不应仅因ID较大而放宽归属校验。

## 待核实：统计回读反复过滤gm0数据

10月25日有335条 `[stats] GetTotals ... dropped ... invalid dim entries` 日志，常见键为gm0/GamemodeCPCaptures、gm0/GamemodeAssaultBattles等。

statistics_server.py:488–492明确在战斗服GetTotals回读时过滤gm<=0；messages及既有test_stats_type6.py仍保留gm0并合并到大厅统计。两条读取路径用途不同，不能据此认定过滤一定错误，也不能将335次读取日志解释为335条独立战绩被删除。

需进一步核对战斗统计wire协议及历史写入源，确认gm0是合法汇总还是错误模式维度。当前没有证据证明大厅战绩丢失，本次没有清理任何统计数据。

## 加入战局失败：有现象，未证明误拦

10月25日两个战局共27次JoinMatchMakingResponse=62：916959（24次）、916962（3次）。

- 916959为坦克掌控局，攻方3、防方2；记录中既有阵营1请求，也有阵营2/3请求。
- 916962为步兵突袭局，攻方1、防方2；记录中既有阵营3请求，也有阵营2请求。随后有阵营1和2成功late join。
- mm_handler._specified_mission_join_error对阵营不符和兵种不准入都返回62。现有日志只打印faction，不能单凭“阵营正确仍62”断言bug。

建议后续给拒绝日志补充实际模式、角色兵种、允许兵种及拒绝原因，以区分正确拒绝与误拦。本次未变更校验。

## 已知问题与历史错误不重复当成新故障

- 10月25日仍有4次CreateVehicleVariantResponse=56，对应整车购买定价不匹配；本地已有test_vehicle_purchase_pricing.py及修复，需要结合部署版本确认线上是否生效。
- 付费/免费载具入库失败是本对话已经定位及本地修复的问题，未计为新的发现。
- 累计stderr中有10次GetMissionDetailsRequest字符串.items异常、4次war_entity未初始化导致开局任务崩溃、3次补给队列datetime转int错误，以及46次HQ ID误查AT数据库溢出。这些是历史记录，在此次10月25日完整game日志内没有找到同类业务异常。当前本地代码已存在_as_resources、war_entity非战争分支初始化、HQ大ID过滤等处理；本次未逐项验证它们的线上部署状态。
- 累计日志中的客户端#1009也有9月历史堆栈，不据此认定今天仍会崩溃。
- 10月25日的HnGSyncClient/LocalcallsService连接错误是客户端本地同步服务连接失败记录，不能和IRC聊天端口故障混为一谈。

## 排除或降低优先级的噪声

当天10条handle_tcp UNEXPECTED记录中7条为invalid chunk size、3条为WinError121。部分报文首字节与HTTP/TLS流量相符，属于疑似公网非游戏连接；不足以证明正常玩家协议损坏或服务进程崩溃。

stderr尾部一条HTTP ConnectionResetError来自127.0.0.1的连接，另有IRC连接超时。它们证明连接被重置/超时，不等于服务整体不可用。

大量HandlerErrorResponse日志实际明确标为ReportMissionStatus、PlayerJoined等 acknowledged，不能仅按响应标题将其计为业务失败。

## 优先级

先修复隐式小队成功分支误查DB，并补solo/自动凑队/真实小队及越权回归。然后补齐加入战局拒绝原因诊断；统计gm0语义需要单独做协议验证。当前不建议依据旧stderr记录直接批量改代码或清理数据库。

## 后续修复（2026-10-26）

已完成本地修复，尚未部署：

- handlers/squad.py：公共成功后同步分支加上not in_memory条件。本人solo/自动凑队仍无副作用返回成功，不查DB；非本人/非队长仍被拒绝；真实小队仍校验队长、写入标记并仅向其他成员推SquadSession单行。
- mm_handler.py：指定战局拒绝时额外记录mission、charId、charCategory、faction、实际gamemode、allowedCategories、攻防阵营、人数、war/staged来源、具体reason和返回码。分别标记不存在/实例不可用、已结束、交战阵营字段损坏、请求阵营不符、兵种不符及满员。未改变原准入条件和响应码；合法加入无拒绝日志。
- 新增tests/test_online_log_fixes.py，共23项pytest用例，覆盖上述内存/真实小队授权和同步行为、各拒绝原因、合法准入以及war无存活实例重新排队的既有行为。

验证：新增23项与战后小队8项、退出结算7项，共38项pytest通过。小队权限独立脚本18/18、兵种门禁独立脚本35/35、中途加入独立脚本106/106通过。修改的Python文件通过py_compile。

运行既有匹配脚本时发现测试夹具引用已删除的MISSION_MAP_ENTITY_ID，以及旧Store替身缺少现有装备校验所需查询。仅修复两个相关测试文件：按preset的真实gamemap解析战场锚点，并为无装备路由夹具补充空list_weapons/list_vehicles。保留原有成功与拒绝断言，不放宽业务门禁。

此前tmp/reproduce_audit_squad_flag.py是修复前行为的复现脚本；修复后回归使用test_online_log_fixes.py。此次未修改gm0统计数据或未经确认的匹配逻辑，未操作线上服务。
