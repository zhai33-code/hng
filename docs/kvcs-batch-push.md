# KVCS 批量推送机制与批量候补审计

日期：2026-09-11。配套代码：`messages.py _push_kvcs / _push_kvcs_delete / _push_kvcs_batch`。

## 1. 协议与客户端处理契约（批量推送的正确性基础）

### 1.1 消息结构

单行 set：`main(cmd_id) → KeyValueChangeSet → set → <table> → <row proto>`
单行 delete：`main(cmd_id) → KeyValueChangeSet → delete → <table> → 8 字节 LE 行 id`
批量：`main(cmd_id) → KeyValueChangeSet → delete → 表块×N（同表多行=重复表块） + set → 表块×M`

客户端解析（`DataModelProxy.updateTablesFromChangeset`）：逐 action 块 × 逐表块 × **只取首行**（`subChunks[0]`）——同表多行必须以重复表块表达，不能塞在一个表块的 subs 里。

### 1.2 两阶段处理（关键）

客户端收到一个 changeset 后分两步：

1. **应用全部行**（`updateTablesFromChangeset`）：按块序逐行写 schema 表；应用时同步跑 `preUpdatedSignal`（handleMapEntityChanges 算差异标志，标志存活在 schema 上）。
2. **FIFO 派发信号**（`publishPendingTableUpdates`）：按**行应用顺序**派发表更新信号，20ms 时间分片。

推论：

- **信号派发顺序 = changeset 内行顺序**（delete 块在前 → del 信号先派发）。
- 派发时读到的是**整批应用后的最终表状态**（跨行可见）。
- 但信号处理器查的 **schema/组件级标志（如 moving）由前序信号设置**——批内行序就是派发时序，顺序错误会漏状态迁移。

## 2. dels-first 契约（为什么 delete 块必须在 set 块前）

`_push_kvcs_batch` 强制把 delete action 块放前、set action 块放后。原因（以战争地图到达推送为典型案例）：

- AT 到达推送 = `del transport_commandnode_segment` + `del destination/air_transport` + `set CommandNodeWarInstance`。
- `moving` 标志（schema 级 `commandnodeStoppedMoving` + 组件级 `CampaignUnitBase.moving`）**只在 segment 删除信号派发时清除**。
- `updatedWarInstance` 重钉进城要求 `hasChangedMapEntityOrType && !moving`——若 instance 信号先派发（set 块在前），moving 仍 true → 重钉跳过 → 单位卡在动画终点（城外出兵点偏移位）。2026-09-11 步兵小队卡城外的根因。
- 批内 del 重钉（如 `groundAssaultTeamMoveEndedOnSupplyLine` 读 `mapEntityTypeId`）读到的是批后活态：type 已=1，不会误走补给线分支（`WarmapPositionUtilities.getPositionFromEntity` L137-138 对 `accessPointStatus` 无空检查，中立端点 + 线上有战斗即 NPE → `publishPendingTableUpdates` while 循环中断 → kvcs 管线冻结、全部后续信号死——到达后名字不显示、重登才恢复的根因）。

**适用条件**：全 set / 全 del / 原本就"del 先 set 后"的序列，批内派发顺序与逐条推送一致（且更原子）；set-del **交错**的序列直接套用会被重排变序，不能用（除非 helper 加保序参数）。

## 3. 现有批量入口

- `war_campaign/tick.py broadcast_war_events`：war 域全部事件（到达/换段/撤退/占城/成战/结算…）已走 `_push_kvcs_batch`，推给全部 warmapview 订阅会话。
- 其余模块仍是单行推送（见 §4 审计）。

## 4. 批量候补审计（2026-09-11，未实施）

### A. 可安全批（同 cmd、全 set 或全 del、行间独立、派发顺序不变）

| 位置 | 内容 | 备注 |
|---|---|---|
| `handlers/war_at.py` `_push_at_rows`（L102-115） | Definition+WarInstance+Morale+armyresource×N | 全 set；补给队多资源行时收益明显；部署/购买/补编/撤退多处调用 |
| `handlers/war_at.py` `_push_at_delete`（L156-170） | 解散 del 行（instance/definition/morale/armyresource×N/airbase） | 全 del，批内保序 |
| `handlers/combat.py` 战后磨损物品行循环（~L1747-1752） | weapon/WeaponModifier/WeaponAmmo/Vehicle… 行 | 全 set 到 soldier_view_cmd，行数=武器+载具+弹药数，**收益最高** |
| `handlers/combat.py` 维修通知（~L1718-1728） | NotificationData×N + Notification | 全 set；set 内保序天然满足"Data 先于 Notification"（渲染器 set data 时一次性读表） |
| `mm_handler.py` faction×3 行循环（~L243-244） | 同表多行 set | 独立行 |
| `matchmaking.py` faction 循环 + PlayerMatchMaking（~L995-1011） | 全 set | 独立行 |

### B. 可批但需谨慎（顺序调优过 / set-del 交错 / 客户端派发依赖）

| 位置 | 风险点 |
|---|---|
| `handlers/combat.py` 战斗结束解散 auto squad/solo（~L1673-1697） | 注释明确"SquadSession 先删防切屏卡死"，实测调优顺序，行少收益低——**建议不动** |
| `matchmaking.py` `_reset_auto_squad_rows`（~L1495-1534） | set/del 交错（member dels → sets → PMM del → sets）；helper 的 dels-first 重排会改变 PMM del 相对位置。若批需保序模式 |
| `matchmaking.py` `_push_auto_squad_rows`（~L375-406） | 全 set 保序可批，但"session 先成员后"是调优结果（成员行 ADD 触发切换），需小队流程回归 |
| `matchmaking.py` 成局推送（~L2102-2153：PMS+BattleInfo+每成员小队行） | 全 set 保序可批，但进局关键路径、客户端状态机敏感，最后做 |
| `handlers/war_at.py` `_push_at_deploy_rows`（~L145-153） | del→set 混合，重排后顺序恰与现序一致；但批内 DELETE 信号派发时 set 行已应用（同 id 先重加再删再由 set 信号加回，净效果应相同）——需实测"部署第二个 AT 图标堆叠"场景验证（该场景历史 bug：UPDATE 分支无堆叠检查，见函数注释） |
| `player_service.py` / `handlers/weapons.py` / `weapons_purchase.py` 购买维修改装流 | weapon+modifier+ammo 2-3 行到 soldier_view_cmd + 1 行 player 到 playerview_cmd（跨 cmd 只能部分批），收益 1-2 帧价值低；弹药多行循环（player_service ~L578/L665）收益稍高 |

### C. 不值得动

所有单行推送（player 货币行、ViewHandlerPulse、各响应后单行 set 等）——批量无意义。

## 5. 实施顺序建议

1. A 类四项（优先 combat 磨损行循环）；
2. `_push_at_rows` / `_push_at_delete` 批量化后跑 `test_war_at_supply.py` + `test_war_supply_column.py` 回归；
3. B 类按需逐项评估，先加 helper 保序参数再动交错序列。

回归基线（2026-09-11）：test_war_movement 118 / test_war_supply_column 44 / test_war_campaign 82 / test_war_at_supply 133，全部 PASS。
