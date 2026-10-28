# 伞兵据点点不开简报 / 反复闪退城市信息图 — 排查结论

> 2026-09-16 排查记录。玩家反馈：**有伞兵部队的战争据点无法点击进入简报，反复闪退"城市信息图"（弹窗显示"未有交战情况"）**。
> 本文件记录客户端机制实锤链 + 服务端缺口定位 + 待捞的生产证据。

## 一、客户端机制（AS3 全部实锤）

### 1. 点击城市的分流逻辑

`WarMapViewMediator.warMapMouseUp`（WarMapViewMediator.as:503-517）：

- `BattleTable.getBattleFromMapEntityId(城市id)` **有 battle 行** → `warShowBattleSignal.dispatch` → 开会战简报（MissionDetailsWindow）；
- **无 battle 行** → `new BattlefieldDetailsView()` 弹"城市信息图"。

⟹ **玩家看到城市信息图 ⟺ 客户端 BattleTable 里没有该城的 battle 行**（与服务端是否真有会战无关，以客户端表为准）。

### 2. "城市信息图闪退"的来源

`BattlefieldDetailsMediator.onBattleUpdate`（BattlefieldDetailsMediator.as:174-190）：城市信息图打开期间，若该城 battle 行 **ADD** 到达 → 记录 `battleToOpenInfoAbout` + `view.Close()`，onRemove 时自动 `warShowBattleSignal.dispatch` 跳简报。

⟹ battle 行在窗口打开期间出现 = 窗口被自动关掉（观感=闪退）。

### 3. "点不开简报"的来源

- 简报数据链：`warShowBattleSignal` → `MissionDetailsMediator.getBattleDetails` → `HandleWarBattleCommand` → `GetMissionDetailsRequest{f1=0, f2=battleId}`。
- 响应 43（mission_not_found）→ `clearWarBattleSignal` + `warBattleNotFoundSignal` → `MissionDetailsMediator.onWarBattleNotFound` → `closeView()` **窗口秒关**（HandleWarBattleCommand.as:113-118、MissionDetailsMediator.as:220-223）。
- 简报窗口打开期间挂 **1 秒定时器** `MISSION_DETAILS_UPDATE` 反复重拉详情（MissionDetailsMediator.as:195-204）→ 每次 43 都会再关一次。

### 4. 崩溃面（已排除/待证）

- `BattlefieldDetailsMediator.setResources`（BattlefieldDetailsMediator.as:111-118）：`getAssaultTeamObject(warInstanceId)` 返回值**无 null 检查**直接 `.resourceOneArmyResourceCategoryId` —— 若 AT 在 `AssaultTeamLocationMapping` 里但未注册进 `AssaultTeamModel`（`_nonPlayerAssaultTeams` / `_currentPlayerAssaultTeams`）→ #1009 开图即崩。注意 `AssaultTeamObject.resourceOne/TwoArmyResourceCategoryId` 构造即初始化 `int64.zero`（AssaultTeamObject.as:76/86），对象本身字段不会 null，**只有对象缺失才崩**。
- AT 注册：非本玩家 AT 在 CommandNodeDefinition **ADD** 时无条件注册（AssaultTeamModel.as:620-628）；本玩家 AT 有 `isInWar` 门控（AssaultTeamModel.as:682-684，false 直接 return 不注册）。
- relation 表已 dump 实锤三阵营齐全（char-relation 60 行含伞兵 108/111/112/114/208/211/308/311，ar-relation 含伞兵 {4,10} 行）——**排除"伞兵模板缺 relation 行致客户端 null 迭代"**。

## 二、症状组合成立的服务端条件

三个症状同时出现的唯一自洽解释：**该城 battle 在客户端反复消失→重建（churn），且/或 battle 存在但简报查询恒 43**。

- battle 行 del 后点击 → 城市信息图（"未有交战情况"）✓
- 城市信息图打开期间 battle 行 ADD → 自动关窗跳简报（"闪退"）✓
- 简报 43 → 秒关（"点不开"）✓

## 三、服务端缺口（按嫌疑排序）

### 缺口 1：tie 无胜方（winner=0）结算不撤退 → 立即再成局循环 ★独立实锤逻辑漏洞

- `settle_war_battle` ③b 败方撤退整段被 `and winner` 门控（tick.py:1177）：`winner=0` 时**攻守双方全部原地释放（state=0）留在同城**。
- D 修复（tick.py:1279-1291）随即以非城主 AT 为基准再成局 → battle 行 del/set 循环。
- 伞兵攻城 tie 分支（automission.py:187-195 `return 0, []`）**伞兵不落地不撤退**，空中原型（cat4+cat10）悬在敌城反复入战。
- 触发条件：§7/§4 双方满足门槛 → tie → WAR_AUTO_TIE_HOURS 后兵力持平判 0；或 _LEGACY → `_power_winner` 双方资源相等/皆空。

### 缺口 2：D 修复只扫 `wb.factions`，第三阵营残留 AT 永不再成局

- 战斗期间到场的第三阵营 AT 被两方制排除（`_check_battle_formation` tick.py:461 注释路径），不在 factions 名单；
- 结算后 D 修复仅遍历 `wb.get('factions')` → 第三阵营 AT（含落在第三阵营城的伞兵）留在城里**永远不再触发成局**；
- 稳态症状：城上长期挂伞兵图标但永远没有会战 → 点击只能开"未有交战"的城市信息图。

### 缺口 3：再成局时 `create_mission_record` 失败仅记日志 → 简报恒 43

- tick.py:484-494 try/except 吞掉异常（`[war] create war mission record failed`）；
- battle 行照推，但 `get_mission_by_battle(bid, states=(0,1))` 永远查不到 → `_handle_mission_details` 回 43（mm_handler.py:897-903）→ 客户端秒关。

### 已排除

- 简报渲染链本身：前轮已验证 war_pending det / `_war_pending_roster` 对伞兵模板正常返回；
- 成局链路对伞兵无特判：`_check_battle_formation` 必定推 battle set + 建 mission record；
- relation 表缺行假说（见一.4）。

## 四、伞兵相关机制备查（本次梳理确认）

- 空降敌对城市：有敌情 → **保持空中原型入战**（cat4 伞兵+cat10 运输机成对，结算时才拆分）；无敌情 → `land_paradrop_split` 落地变体 + 运输机拆成玩家名下独立 reserve AT 飞回原机场（tick.py:215-240、at_ops.py:155-303）。
- 空敌城到达：`_capture_city_events` 直接易主（tick.py:246）→ 无会战属正常。
- 败方伞兵：`automission._paras_land` 先行拆分落地（automission.py:92-111）→ `settle_war_battle` ③b 撤退（state=0 + 在 dest_bf + 非胜方 → `start_retreat_movement`，无航路兜底瞬移 tick.py:1212-1220）。**正常闭环，唯一例外是缺口 1 的 winner=0 门控**。
- 拆分产物 planes-AT 是 reserve 模板 → `_check_battle_formation` enemies 过滤 `not is_reserve`（tick.py:433）不参与成局。

## 五、待捞的生产证据（定案用）

VPS `C:/Users/Administrator/Desktop/HngServer/server/logs/realtest_game.log` 报障时段：

```
"battle .* formed" / "battle .* settled" / "create war mission record failed"
"paradrop" / "以空中原型入战" / "GetMissionDetailsResponse (mission_not_found"
```

外加：**具体城市名、伞兵所属阵营、报障大致时间**。

判读：
- 日志里**同城 battle id 反复 +1**（formed→settled→formed）→ 缺口 1（成局-结算循环）实锤；
- 伞兵到达后**无 formed 记录** → 缺口 2（第三阵营/无再战）方向；
- 有 formed 但伴随 `create war mission record failed` 或大量 43 → 缺口 3。

## 六、修复预案（✅ 2026-09-16 已全部实施，待 VPS 实测）

- [x] 缺口 1：`settle_war_battle` ③b 改为 `winner=0` 时**攻方（非城主）AT 也撤退**（守方=城主留守；城主未知 0 时 tie 全撤，宁撤不循环）；`automission` tie 持平分支（原 `return 0, []`）落伞兵。留守集合条件式：`winner>0` 时 stay={winner}（与原行为完全一致）。
- [x] 缺口 2：D 修复候选从 `wb.factions` 扩为**全城 state=0 非城主已部署非 reserve AT**（与 `_check_battle_formation` enemies 判据同源：撤退中 state=1 的 AT 天然排除）。
- [x] 缺口 3：`create_mission_record` 失败时连 battle 一起回滚（城战 `_check_battle_formation`：关会战 + 双方回 state=0；野外 `_check_field_encounters` 同源：关会战 + 恢复行军 state=1），battle 行不推送，下次到达/D 修复重试。

回归：tests/test_war_movement.py 161/161（新增 K10 tie 持平落地+撤退+无 churn、M1 tie 城战撤退/留守/无再成局、M2 第三阵营 SU 再成局 {1,3}+GE 撤退不回归、M3 城战回滚、M4 野外回滚）；全量回归 war_campaign 82 / wardb_rts 33 / war_at_supply 137 / at_stats_rank 43 / mm_pool 55 / stats_type6 44 / settlement 13 / mm_latejoin 106 / player_ids / snow_config 全绿。

## 七、生产日志定案：19:30 卡死 = 他人 reserve AT 资源行致客户端 #1009（✅ 2026-09-16 已修复）

> 2026-09-15 VPS 日志（`realtest_game.log.2026-09-15`）分析：用户反馈 19:30 左右卡死。

### 1. 日志实锤（客户端时间戳为 UTC，服务器时间 -8h 语境下对应 19:29-19:32 服务器时段）

- 19:29:26：GE 玩家 zhai333 的伞兵 AT 219 空降到达敌城 75221 → `land_paradrop_split`：219 转落地变体 + **拆分出玩家名下 reserve 运输机队 AT 238**（'AT004 - zhai333'，带 cat10 资源行，parent=219）飞回机场 670595102；
- 同秒 battle 204 阈值判负结算（winner=3）——19:29:27-44 的 9 次 `mission_not_found` 是结算与已开简报窗口竞争（本文档 §1.3 已知症状，非卡死本体）；
- 19:31:11：183.197.233.187 重连（SU 玩家 zhai33，pid 1000002，char 250/251）→ 19:31:31 登录批订阅（warmaplightview 全量快照，含 238 的 armyresource 行）；
- **19:31:44 客户端 ClientLogMessage 报未捕获异常**：切 CampaignScreen → `WarMapViewMediator.onRegister` → `AssaultTeamModel.handleReservesMissingParentInfo` → `AssaultTeamResources.addReserveResourcesToParentAssaultTeam` → **Error #1009 TypeError** → 19:32:18 断线（卡死后关闭）；
- 顺带实锤：19 个 GetAssaultTeamStats id 中 3 个 int64 巨值 = HQ 实例 id（`FACTION_SUPREME_COMMANDS` 63 位随机数），handler int32 越界报错但不致命（已加越界守卫静默跳过）。

### 2. 客户端崩溃链（AS3 实锤，不可修，只能服务端避开）

1. 他人 AT 的 CommandNodeWarInstance 行无条件注册进 `_nonPlayerAssaultTeams`（AssaultTeamModel.as:620-628）→ 238 对所有非属主客户端已注册；
2. `updateArmyResources`（:871-924）：armyresource 行到达且所属 AT 已注册 + `locTemplateId==4`（reserve）+ `getPlayerAssaultTeam(parent)` 为 **null**（parent 219 是他人 AT，永不在本人列表）→ 写入 `_reservesThatRequireParentUpdate`（:884，唯一写入点）；
3. 开战争图 → `handleReservesMissingParentInfo`（:266-290）：`_loc3_ = getPlayerAssaultTeam(parent)` **无 null 检查**（:282）直传 `addReserveResourcesToParentAssaultTeam(_loc1_, _loc3_, _loc4_)` → `param2.templateId` #1009（AssaultTeamResources.as:403）。

⟹ **任何非属主客户端收到"他人 reserve AT 的 armyresource 行"后，开战争图必崩**。官方服务器不崩 ⟹ 官方口径必为：他人 reserve AT 的资源行不推给非属主。补充排除：候选"落地变体模板缺客户端静态表"不成立（WarDatabase commandnodetemplate 三阵营均含 paratroopers/pathfinders_on_ground 行，且服务端 `land_paradrop_split` 只写 `cvt in wardb.commandnode_templates()` 的模板）。

### 3. 服务端修复（两层收口 + 顺带）

- [x] **快照层**：`build_warmap_snapshot(store, war, player_id=None)`——reserve AT 的 armyresource 行只推属主（`views.py` 订阅入口传 `sess.ensure_player()`；player_id 未知 → 全隐藏，宁缺毋错）；非 reserve AT 资源行不过滤（客户端对非 reserve 行无延迟字典写入，且敌方 AT 兵力显示依赖它）；
- [x] **广播层**：`broadcast_war_events(events, store=None)`——按会话 `ensure_player()` 过滤 reserve AT 的 armyresource **set 与 del 行**（del 行同样触发延迟字典写入，均过滤）；行归属 AT 解析 `_armyresource_row_at_id`（set 行 proto f1 前导 varint / del 行 id，HQ base|cat 行解析失败自然放行）；全部 13 处调用点传入 store；
- [x] `GetAssaultTeamStatsRequest` handler 对 int32 越界 id（HQ 63 位随机实例 id）直接跳过（不再走异常日志路径）；
- 属主不受影响：本人视角 parent 在 `_currentPlayerAssaultTeams` → 走 ADD 即时汇总（:903-909 有 null 检查）；属主换异阵营角色时 reserve AT 未注册（isInWar false）→ 资源行无消费者，安全。

回归：tests/test_war_movement.py 164/164（新增 N0 行归属解析、N1 快照属主可见性、N2 广播按会话过滤）；全量回归 war_at_supply 137 / war_campaign 82 / wardb_rts 33 / mm_pool 55 / stats_type6 44 / settlement 13 / mm_latejoin 106 / squad_multiplayer PASS / player_ids+snow 20 全绿。
