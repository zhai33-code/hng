# 载具逐实例耐久结算修复

状态：本地已实现并测试；未部署、未重启线上、未修改线上数据。

## 结果

实际使用并产生损耗的车辆正常扣耐久及自动维修费；其他装备中的车辆不扣。
车体、升级件和载具弹药全部按实例ID读取战斗服结算状态，不再以角色出生数
或武器模板射击量推算。普通武器/配件/武器弹药保留原type6实际发数结算。

## 协议及native证据

最初只检查type6统计，遗漏了PlayerBattleResult的物品状态字段。
本次直接检查原始player.dll的描述符与战斗服结算生成函数，确认已有可靠数据，
无需暂停正常载具扣损，也无需新增战斗服上报协议。

原始文件：D:/HnG-test/player.dll，配套分析库player.dll.i64。
分析输出：../tmp/ida_vehicle_usage.txt；字段表读取脚本：
../tmp/dump_vehicle_status_schema.py。

| PlayerBattleResult字段 | 内容 | native字段表地址 |
| --- | --- | --- |
| 10 | vehicles / BattleFieldWinnerVehicleStatus | 0x180917E00 |
| 11 | vehicleUpgrades / BattleFieldWinnerVehicleUpgradeStatus | 0x180917E40 |
| 12 | vehicleAmmos / BattleFieldWinnerVehicleAmmoStatus | 0x180917E80 |

三个子消息均为f1 int64实例ID、f2 float condition、f3 float initialCondition。
车体子消息字段表始于0x180917640，升级件始于0x180917700，弹药始于0x1809177C0。

native结算生成函数sub_1802BC2A0：

- 车体在对象+20处存结束耐久、+24处存初始耐久，二者不同才加入结果列表。
- 输出PlayerBattleResult对象+88/+96的列表（对应f10），填入实例ID及
  `max(结束耐久,0)`、初始耐久。
- 升级件输出+104/+112的列表（f11）；载具弹药输出+120/+128（f12）。
- native已根据实际使用计算磨损；大厅不应再次乘角色出生数。

此证据来自native实现与描述符，非把VehicleLost/VehicleTimeSpent误当部署次数。
普通线上日志仅打印结算包开头32字节，无法从旧汇总日志还原完整逐车报告。

## 实现及边界

handlers/combat.py新增_apply_vehicle_condition_reports，结算handler读取并处理
f10/f11/f12。物品需仍归该玩家所有，升级/弹药父车辆也需归该玩家所有；以
实例身份核验，不依赖结算时装备槽（战后可能已移动或卸下）。

按本局损耗差值扣当前耐久，避免将当前数据库直接覆盖为过期战斗快照。
随后复用原自动维修价格/币种/余额不足逻辑，合并维修通知和KVCS刷新行。
不会再把同一武器模板发数扣到多辆车的弹药上。

不接受NaN、无穷、负初始值、结束值大于初始值、超过模板上限的初始耐久。
native升级件可能上传负结束耐久，按0钳底；结束耐久缺省0按protobuf缺省处理。
相同重复行只算一次，冲突重复行跳过；bot与未知玩家继续沿用结算名单过滤。
缺状态不估算，但正常上报的已使用载具照常计损。

完整结算标识+玩家ID的4096项进程内缓存防止近期重复/并发报告二次扣损，
与现有type6的进程内去重边界一致。它不是跨重启的持久化账本；异常中断期间
的部分结算与跨重启重放不在本次幂等保证内。

## 验证

新增tests/test_vehicle_condition_reports.py，覆盖步行出生、多辆同模板载具只用
一辆、逐升级/弹药实例、正常扣损、Credits/Gold自动维修、余额不足、物品移动、
归属校验、异常/损坏数据、重复行、并发/重复结算、新战局继续扣损、type6缺失、
bot与真人账号ID碰撞。原装备磨损和结算测试同步改用真实协议字段。

真实数据库测试仍为既有事务回滚路径，新增了逐车状态实际扣损断言；本次回归
不连接数据库，因此不能据此宣称数据库集成或线上真实战局验证通过。

最终结果：装备磨损、逐车状态及结算解析合计62项pytest通过，1项真实数据库
集成测试明确排除；type6统计脚本按其原生async runner运行，44/44检查通过。
git diff --check通过。最初将type6脚本直接交pytest曾因缺async插件报3项
收集执行方式错误，随后改用仓库脚本自己的runner完成全部44项检查。
