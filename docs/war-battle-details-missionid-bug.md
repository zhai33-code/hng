# 战争成局后对局详情 bug 排查：资源不按 AT 实际数量展示 & 开战按钮失效

> 日期：2026-09-13
> 状态：**已定位根因，未修复**（本文档为排查结论 + 修复方案）
> 现象：
> 1. 战争成局后（AT 相遇形成 battle），对局详情界面的资源不按参战 AT 部队的实际数量展示；
> 2. 战争成局后，玩家点开对局详情，右下角"开战"按钮无法点击（灰掉），或点击后走的却是普通匹配流程。

---

## 一句话结论

两个现象是**同一个服务端 bug 的两种表现**：`mm_handler._handle_mission_details` 的
battle_id 分支（`server/mm_handler.py:714-793`）按 bid 反查到 war record 后构造了详情，
**但从未把 `mid` 更新为 `wrec['id']`**，最终以 `mid=0` 渲染响应 → `MissionInfo.f1
missionId=0`。连锁后果：

- 详情 f4（突击队）/f5（资源）以 mission_id=0 查池 → 查不到 → 全部回退**写死锚点值**（现象 1）；
- 客户端拿 missionId=0 → `warBattleId=0` 且匹配参数全清 → 按钮落入 BASE_STATE
  （绿色普通 To Battle → 点击=普通匹配），或因回退的纯步兵 AT 名单导致兵种匹配失败
  → WAR_BATTLE_UNAVAILABLE 灰掉（现象 2）。

---

## 二、链路证据

### 2.1 客户端请求形态（前提）

战争图点有会战的城市 → `HandleWarBattleCommand.execute()` 恒发
`GetMissionDetailsRequest{f1=missionId=0, f2=battleId}`（battleId = KVCS `battle` 行 f1）。

反编译实锤：`decompiled/swf-ui-logic/scripts/dk/reto/heroesandgenerals/components/war/controller/HandleWarBattleCommand.as`
（`int64.zero` 作 missionId）；测试注释佐证：`server/tests/test_war_battle_details.py:4-6`。

### 2.2 服务端处理（bug 本体）

`server/mm_handler.py` `_handle_mission_details`：

```
719  mid = int(f.get(1, 0))              # = 0（客户端恒发 0）
720  bid = int(f.get(2, 0))              # = battleId
721  details = _MM_MISSION_DETAILS.get(mid)   # get(0) → None
722  if details is None and mid == 0 and bid > 0:
729      wrec = await state.STORE.get_mission_by_battle(bid)   # ★ 返回的 record 含真实 mission_id（'id'）
733      if wrec is not None:
734          details = {..., 'war_pending': True, 'players': []}
           # ★★ wrec['id'] 被丢弃，mid 保持 0
781  resp_proto = _msg_mission_details_response_multi(mid, details, ...)   # mid=0
```

响应里 `MissionInfo.f1 missionId` 由 `mm_handler.py:565` `pb_int64(1, int(mission_id))` 写入 → **恒为 0**。

附注：`store/missions.py:69-80` `get_mission_by_battle` 只取 state∈(0,1) 的最新一条，
返回行**含 `id` 字段**（真实 mission_id），修复所需数据现成。

### 2.3 现象 1 的传导链：f4/f5 回退写死值

响应渲染 `_msg_mission_details_response_multi`（`mm_handler.py:586-711`）：

| 字段 | 代码 | mid=0 时的结果 |
|---|---|---|
| f4 突击队 | `mm_handler.py:653-660` → `_assault_team_nodes(faction, 0)`（`matchmaking.py:83-92`） | `force_pool.assault_team_names(0)` = None → 回退 `('infantry','motorized_infantry','motorized_guard')×2`（`matchmaking.py:80` 写死） |
| f5 资源 | `mm_handler.py:663-675` → `mission_resource_pool(0)`（无 0 号池）→ `_pool_counts(None, fac)`（`matchmaking.py:207-211`）→ `force_pool.counts_for(None, fac)`（`force_pool.py:394-399`） | 回退 `FALLBACK_COUNTS = {1:432, 15:60, 11:24}`（`force_pool.py:108`，官方 First Encounter 抓包锚点：步兵 432 / 地形车 60 / 摩托 24） |

即：**即使 war 局已通过 `_start_match` 按真实 mission_id 建好了 AT 池**
（`matchmaking.py:1415-1417` 调 `_init_war_mission_resources` → `force_pool.init_mission_war`，
池 = 双方参战 AT 编制之和，`matchmaking.py:154-199`），城市点击链也查不到（key=0 ≠ 真实 mission_id），
玩家看到的资源与参战 AT 完全无关。

（对照：战略层 AT 自身资源是正确的——KVCS `armyresource` 行 f4 用 `at['resources']` 实际值，
`war_campaign/warmap_view.py:158-175`。本 bug 只影响"对局详情/简报"层的资源展示。）

### 2.4 现象 2 的传导链：按钮灰掉 / 跳普通匹配

客户端消费 missionId=0（均在反编译 swf-ui-logic）：

1. `HandleWarBattleCommand.checkIfMissionCanBeJoined`：
   `setWarBattleSignal.dispatch(info.missionId)` → **warBattleId 的唯一来源**。
2. `SetWarBattleCommand`：`warBattleId=0` 时同时清空 `quickplaySelected`、
   `selectedGamemode`、`warBattlesSelected`、`selectedMissionPresets`。
3. `StatusMediator.onWarBattleFiltersValidated`（729-776 行）：
   - `warBattleId.Equals(int64.zero)` → **BASE_STATE**（绿色普通 To Battle 按钮）→
     点击发 `JoinMatchMakingRequest{f2=0, f4=false, f7=0}` → 服务端 `_handle_join`
     不进 war 路由 → 落普通撮合池。**这是"点击跳到正常匹配"的直接原因**（war 路由本身
     `mm_handler.py:397-446` 逻辑正常，问题纯在客户端拿不到正确 missionId）。
   - 若 f4 突击队名单里命中不了玩家兵种（回退名单只有步兵系，坦克兵/侦察兵在
     `CommandnodeTemplateCharacterTemplateRelationTable` 查不到对应）→
     `warBattleResourceExistsForSquad=false` → **WAR_BATTLE_UNAVAILABLE** →
     `handleInteractionButton` BigGrey + `enabled=false`（灰掉）。
4. 辅因（非服务端 bug）：客户端硬门槛 `playerLevel < 10` 强制禁用 JOIN_WAR_BATTLE
   按钮（带等级徽章 tooltip）——等级不足的测试号会稳定复现"灰掉"。

### 2.5 服务端 war 局资源池本身（数据源是好的）

- 建池：`matchmaking.py:1415-1417`（`_start_match` war 分支）→
  `_init_war_mission_resources(mission_id, battle_id, atk, dfd)`（`matchmaking.py:154-199`）：
  AT 名单 = `war_battles.factions_json`，编制 = AT `template_id` →
  `force_pool.composition_for_template(tid)` 模板编制原值。
- 详情/握手/轮询三处单一事实源：f5（`mm_handler.py:661-675`）、
  f14（`game_server.py:527-536`）、f2（`warserver_messages.py:555-612`）。
- 战略层：`armyresource` KVCS 行用 AT 实际值（`warmap_view.py:158-175`）。

即：**池的数据源与建池逻辑基本正确，坏在详情请求的 mid 路由上**。

---

## 三、附带发现（次要问题）

1. **池建立时机偏晚**：AT 成局（`war_campaign/tick.py` `_check_battle_formation` 建
   mission_record，state=0）与真人局成局（`mm_pool.try_form_war`：123-148 行，按
   battle_id 分桶、桶内攻守阵营各 ≥1 真人才成局）之间存在窗口期。此期间即使 mid 正确，
   f5 仍是 FALLBACK_COUNTS——官服语义下战局资源在 AT 成局那一刻就该可见。
2. **满编口径偏差**：war 池取 AT **模板满编制**（`composition_for_template`），而 AT 实际
   存量在 `command_nodes.resources`（战斗结算按损耗扣减 `tick.py` settle_war_battle、
   撤退损 25% `handlers/war_at.py:628-631`）。打残/缺编的 AT 再成局，对局资源池仍按满编计，
   损耗不传导到下一场。
3. **测试盲区**：`tests/test_war_battle_details.py`（2026-09-08 回归）只断言响应非 43
   （查 `b'\x08\x2b'`），未断言 missionId == record id，故此 bug 未被拦截。

---

## 三.5、战局所属城市名称核查（2026-09-13 追加）

> 客户端名字解析链（反编译实锤，服务端**不下发任何名字字段**，客户端完全本地查表）：
> - type=1（battlefield）：`BattlefieldTable.getFromId(mapEntityId).bftitle` → 城市名
>   （`decompiled/swf-frontend/.../warmap/util/WarmapPositionUtilities.as:166-185`）。
> - type=3（supplyline）：`MissionUtilities.getMissionCityName()`
>   （`decompiled/swf-frontend/.../util/MissionUtilities.as:37-65`）拼
>   **"城市名 - 方向字母"**：城市 = battlePosition<0.5 取 ap1 端、≥0.5 取 ap2 端的
>   battlefield；字母 = `accesspointtemplate.abbr`（静态表 f5，非方位计算）。
> - 简报标题 / 任务列表城市列 / 游戏内 HUD 均走 **MissionInfo f5/f6 → 本地表**；
>   `missionpreset`(f3) 只决定 gamemap/missiontype（模式图标/小地图），不是城市名来源。
> - 战争图 battle 标记：城战 `BattlefieldLayer.onBattleUpdates` 按城市坐标画；
>   野外战 `SupplylineTable.updateBattles` 把 battle 行 f11 position 写入
>   supplyline 行，`SupplylineView` 沿线插值画标记——**标记与名字走 battle 行，不走 MissionInfo**。

### 城市会战（assault，城内成局）——链路正确 ✅

| 环节 | 代码 | 结论 |
|---|---|---|
| record.map_entity_id | `tick.py:318` = `dest_bf`（真实城市 battlefield 表 id，来自补给线段端点） | ✅ |
| battle 行 f3/f4 | `battle_msgs.py:107-108` = dest_bf / 1 | ✅ 客户端按 bftitle 显名 + 城市坐标画标记 |
| MissionInfo f5/f6 | `mm_handler.py:568-569`、`handlers/mission.py:99-100` | ✅ 简报/任务列表显正确城市名 |
| 简报 f8/f9 坐标 | `mm_handler.py:626` `battlefield_pos(map_entity_id)` | ✅ 与城市位置一致 |

### 野外遭遇（encounter/skirmish，补给线成局）——简报城市名有问题 ❌

战争图上的 battle 标记本身正确（battle 行 f3=sl id、f4=3、f11=position，
客户端沿线插值画标记 ✅）。**错的是简报/任务列表的"所属城市名"链路**：

| # | 问题 | 位置 | 说明 |
|---|---|---|---|
| B1 | 简报城市名 = **group[0] 行进方向的起点城市**（裸城市名） | `tick.py:405-419`（`near_bf = segs[idx].from_bf` → record.map_entity_id）；`mission.py:100`、`mm_handler.py:569` f6 恒=1 | 官方口径应为 f5=supplyline id、f6=3，客户端自动按 battlePosition 拼"城市名-字母"。现状三个后果：a) 名字缺"-字母"后缀；b) 两 AT 相向而行时，group[0] 起点城市可能与遭遇点（position 均值）所在半段**相反 → 城市名直接选错**；c) 简报 f8/f9 = 起点城市坐标（`mm_handler.py:626`），简报战场标记与战争图补给线中段标记**错位** |
| B2 | `near_bf=0` 时 MissionInfo f5 回退 `MISSION_MAP_ENTITY_ID`（112741325 = **Benoit Village**） | `mm_handler.py:568`、`mission.py:99` | mv 查不到/段索引无效的竞态下，简报显示完全不相干的城市名（概率低但存在） |
| B3 | `_start_match` 简报落点同源 | `matchmaking.py:1228-1231` | war 局 map_entity_id = record.map_entity_id → B1/B2 原样传导进成局后的简报 |

注：record 的 `name` 字段（'war battle {id} battlefield {bf}'）不下发客户端，纯内部
日志用途，无 UI 影响。

---

## 四、修复方案（未实施）

### 修复 1（核心）：bid 分支补 mid 路由

`mm_handler.py:722-741`，查到 `wrec` 后：

```python
mid = int(wrec['id'])                      # ★ 真实 mission_id
live = _MM_MISSION_DETAILS.get(mid)        # 局已开打：复用运行中详情（真实 players 名单）
details = live if live is not None else {  # 否则落 war_pending 假详情（现有构造）
    'battle_id': int(bid), 'preset': ..., 'war_pending': True, 'players': []}
```

效果：`MissionInfo.f1` = 真实 missionId → 客户端 warBattleId 正确 → 按钮进
JOIN_WAR_BATTLE、点击走 war 路由（现象 2 消除）；f4/f5 按真实池渲染（成局后现象 1 消除）。

### 修复 2（窗口期）：battle 成局即建池

`war_campaign/tick.py` `_check_battle_formation` / `_check_field_encounters` 在
`create_mission_record` 后同步调 `_init_war_mission_resources(mid, battle_id, atk, dfd)`
（或改为详情请求时惰性建池 + 已有池跳过），使 AT 成局起资源即按实际编制可见，
不等真人排队成局。注意 `_start_match` 处需保证不重复建池/覆盖已扣减的池。

### 修复 3（口径，可选）：池随 AT 实际存量

`_init_war_mission_resources`（`matchmaking.py:178-183`）编制改用 AT 当前
`command_nodes.resources` 实际值（模板满编仅作缺省），损耗传导到下一场对局。

### 修复 4（城市名，§三.5 B1/B2/B3）：野外遭遇局 MissionInfo 改走 supplyline 口径

1. record 侧：`_check_field_encounters`（`tick.py:414-420`）`map_entity_id` 改存
   **补给线 id**（sl_id 现成），并落一列 `map_entity_type=3`（城战=1，缺省 1 兼容旧行）；
   或 MissionInfo 渲染时按 `battle_id → war_battles.battle_field/battle_type` 反查。
2. 渲染侧：`mm_handler.py:568-569` 与 `handlers/mission.py:99-100` 的 f5/f6 改按
   record 的实体类型动态填（城战：city id / 1；野外：sl id / 3）——客户端
   `getMissionCityName` 即自动按 battle 行 f11 position 拼"城市名-方向字母"。
3. B2 兜底：near_bf/sl 查不到时 f5 回退**补给线 id**（f6=3，客户端沿线中点 + 端点
   abbr），不再回退 Benoit Village（112741325）。
4. 简报 f8/f9（`mm_handler.py:626`）：type=3 时客户端标题不消费坐标（仅
   isAutogenerated 路径用 posX/posY），可保持现状或按 position 对两端城市坐标插值。

### 回归补强

`tests/test_war_battle_details.py` 增断言：

- 响应 `MissionInfo.f1 missionId == mission_record.id`（而非仅"非 43"）；
- 成局后（池已建）f5 各类别数量 == force_pool 池值，而非 `{1:432, 15:60, 11:24}`；
- f4 突击队名单 == 池内 AT 名单（非 `ASSAULT_TEAM_NODE_NAMES×2`）；
- 野外遭遇局 `MissionInfo.f6 == 3` 且 `f5 == 补给线 id`（修复 4 后）。

---

## 五、关键文件索引

| 侧 | 文件 | 关键位置 |
|---|---|---|
| 服务端（bug 本体） | `server/mm_handler.py` | `_handle_mission_details` 714-793（mid 丢失点 722-741/781）、`_msg_mission_info_multi` 553-583（f1 missionId 565）、f4/f5 渲染 653-675 |
| 服务端（池） | `server/matchmaking.py` | `_assault_team_nodes` 83-92、`_init_war_mission_resources` 154-199、`_pool_counts` 207-211、`_start_match` war 分支 1144-1230（建池 1415-1417） |
| 服务端（池底座） | `server/force_pool.py` | `FALLBACK_COUNTS` 108、`init_mission_war` 346-385、`pool` 388-391、`counts_for` 394-399 |
| 服务端（成局） | `server/war_campaign/tick.py` | `_check_battle_formation` 250-327、`_check_field_encounters` 330-427（mission_record 落库 313-319/416-422，野外局 near_bf=段起点城市 405-413） |
| 服务端（battle 行） | `server/war_campaign/battle_msgs.py` | `msg_battle_row` 80-114（f3/f4/f11：城战=bf id/1、野外=sl id/3/position） |
| 服务端（任务列表） | `server/handlers/mission.py` | `handle_get_mission_list` 164-229、`_msg_mission_info_from_record` 89-109（f5/f6 填充 99-100） |
| 服务端（实体类型常量） | `server/war_campaign/common.py` | `MAP_ENTITY_BATTLEFIELD=1` 121、`MAP_ENTITY_SUPPLYLINE=3` 127 |
| 服务端（撮合） | `server/mm_pool.py` | `try_form_war` 123-148 |
| 服务端（存储） | `server/store/missions.py` | `create_mission_record` 12-26、`get_mission_by_battle` 69-80（返回含 `id`） |
| 测试 | `server/tests/test_war_battle_details.py` | 回归盲区（只断言非 43） |
| 客户端 | `decompiled/swf-ui-logic/.../war/controller/HandleWarBattleCommand.as` | execute（恒发 f1=0）/ onMissionDetailsResponse / checkIfMissionCanBeJoined |
| 客户端 | `.../actiongame/controller/SetWarBattleCommand.as` | warBattleId 赋值 + 匹配参数清空 |
| 客户端 | `.../actiongame/view/StatusMediator.as` | `onWarBattleFiltersValidated` 729-776、`handleInteractionButton` 1067-1188（BASE_STATE/WAR_BATTLE_UNAVAILABLE/等级门槛） |
| 客户端 | `.../matchmaking/service/MatchmakingService.as` | joinMatchmaking（JoinMatchMakingRequest f1/f2/f4/f5/f7/f8） |
| 客户端（城市名解析） | `decompiled/swf-frontend/.../util/MissionUtilities.as` | `getMissionCityName` 37-65（type=3 拼"城市-abbr"）、`getMissionDetailsTitle` 271-289、`getMissionTitle` 291-317 |
| 客户端（实体查表） | `.../warmap/util/WarmapPositionUtilities.as` | `getBattlefieldForMapEntity` 166-185、`getPositionFromEntity` 116-161（supplyline 沿线插值） |
| 客户端（战争图标记） | `.../warmap/maplayers/BattlefieldLayer.as` / `.../model/table/SupplylineTable.as` / `.../warmap/component/SupplylineView.as` | 城战标记 103-185；野外战 battlePosition 写入 44-62、沿线插值标记 27-64 |
| 客户端（简报标题） | `.../view/window/campaign/components/MissionDetailsComponent.as` | `setBasicInformation` 577-660（标题 = getMissionDetailsTitle；isAutogenerated 走 posX/posY 最近城市） |
