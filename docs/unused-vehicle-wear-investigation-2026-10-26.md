# 未使用载具掉耐久：线上只读排查（2026-10-26）

后续已找到结算包的逐实例耐久字段，并据此完成本地修复，详见
[载具逐实例耐久结算修复](vehicle-condition-settlement-fix-2026-10-26.md)。
下文“修复方向”为初次排查时的历史建议，暂停正常扣损的方案未采用。

## 结论

线上代码存在把角色出生次数用于所有已装备载具的扣损逻辑，能导致未使用的已装备载具及其已装备升级件掉耐久。只存放仓库且未装备的载具不进入该扣损路径。本次未修改业务代码、未部署、未重启、未改线上数据库。

## 线上证据

香港时间约20:45通过SSH读取线上 `handlers/combat.py`、`statistics_server.py` 以及当前与近两日游戏日志。完整采集保存在 `../tmp/vehicle-wear-20261026/online-source-and-wear.log`，采集脚本为 `../tmp/investigate_vehicle_wear.py`。日志筛选每个文件最多保留最后450条匹配记录，不代表完整历史。

- `statistics_server.py:287–296`：每角色只累积 `shots` 和 `spawns`；`spawns` 来自stat 12 `PlayerSpawns`，没有载具实例或模板维度。
- `handlers/combat.py:873–882`：选取角色 `equipped_in_slot>0` 的全部载具，逐辆执行 `condition - wearPerSpawn * spawns - wearPerBattle`。没有判断该辆载具是否实际部署，步行出生也未排除。
- `handlers/combat.py:898–908`：装在这些载具上的已装备升级件同样使用角色总出生数。
- `handlers/combat.py:950–954`：磨损日志只打印角色、出生数、最多8种武器的射击量、磨损物品总数，不打印载具ID、扣减前后耐久、维修费明细。

线上2026-10-25日志例子：

```text
[02:10:01] [wear] player=1000123 char=671 active=116s spawns=1 shots={} -> 2 item(s) worn/repaired
[04:40:24] [wear] player=1000123 char=644 active=14s spawns=1 shots={} -> 4 item(s) worn/repaired
[21:16:36] [wear] player=1000009 char=42 active=368s spawns=4 shots={} -> 3 item(s) worn/repaired
```

这些记录确认零射击仍发生磨损，但零射击不等于未使用载具，物品总数也不等于载具数量。结合线上源码可以确认错误扣损路径，不能仅凭这些汇总日志认定反馈玩家的具体车辆、每次误扣数或补偿金额。

## 影响及修复方向

- 同角色装备多辆载具时，每辆都按同一个角色出生数计损；只用其中一辆或步行出生也可能扣其他载具。
- 自动维修开启且支付成功时，可能表现为维修费支出，耐久则恢复。
- 修复需要取得按载具实际部署计数的可靠数据，且保留载具归属维度；不能把角色出生次数继续当作载具使用次数。没有可靠载具用量时，应跳过该类扣损而非估算。
- 若反馈是完全未装备、只在仓库的载具也掉耐久，需要反馈账号、车辆与时间段，另查其他写入路径和装备变更；本次证据不支持将这一范围一起定性。
- 补充逐载具日志（mission、player、character、vehicle ID、实际部署数、扣前扣后耐久、维修费）后，才能直接核验单车扣损与补偿。
