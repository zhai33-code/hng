# 社区项目（hagcp）采纳计划：zipchunk / 协议审计 / 一致性测试 / 数据校准

> 来源分析：E:\devProject\Heroes-and-Generals-community-project（下称 hagcp，MIT，TS monorepo）
> **2026-09-17 更新**：P2 调研完成（范围收窄，基础好于预期）、P3 已落地完成（含抓出 missionresource f1 wire type 真 bug）、P4 已完成、P5 调研后关闭（客户端死代码）。详见各节。
> 本文件为 `.trae/documents/community-project-adoption-plan.md` 的落位副本（完成记录以此为准同步维护）。

---

## 一、背景

hagcp 是"能听懂官方服务器的客户端 + 数据采集器"：780 消息的 .proto 字典（经真实官方服务器在线验证）、KeyValueChangeSet 解析器 + 60 类实体注册表、281 项 Response 错误码枚举、79 类官方服务器静态数据快照（JSON）。我们的私服（`work/hng-server-re/server`，Python）在业务层远超它，但协议层是手写 protobuf（字段号靠 SPBC dump + 抓包比对），且**未实现 zipchunk 压缩**——而客户端原生支持（已实锤）。

本计划把可采纳项排成 5 个独立可落地的工作项（P1–P5），按优先级排序。

## 二、现状锚点（Phase 1 已探明）

| 锚点 | 位置 | 说明 |
|---|---|---|
| Chunk 编解码 | [protocol.py](file:///d:/HnG-test/work/hng-server-re/server/protocol.py#L23-L83) | `Chunk.parse/write`、`build_response(cmd_id, title, proto_bytes)`；zipchunk 编码函数加在此 |
| KVCS 推送 | [messages.py](file:///d:/HnG-test/work/hng-server-re/server/messages.py#L167-L242) | `_push_kvcs` / `_push_kvcs_delete` / `_push_kvcs_batch`（批量契约已有 21 项测试） |
| Pulse 循环 | [realtest_server.py](file:///d:/HnG-test/work/hng-server-re/server/realtest_server.py#L67-L104) | 5s ViewHandlerPulse；`is_combat_server` 连接不推 KVCS（ZServer 堆崩溃教训） |
| 订阅/全量推 | `server/handlers/views.py` | SubscribePlayerView / warmap 全量推送的集成点 |
| 客户端 zipchunk 证据 | [Chunk.as:94-103](file:///d:/HnG-test/work/hng-server-re/decompiled/swf-frontend/scripts/dk/reto/heroesandgenerals/service/Chunk.as#L94-L103) | 收到 `zipchunk` → `GZIPBytesEncoder.uncompressToByteArray` → `parseData(zippedData)` 整树重解析；`swf-login-service` 同款 |
| hagcp proto 字典 | `E:\...\packages\hagcp-network-client\src\protos\` | HnG_States.proto(780 msg)/Common.proto(Response 枚举 281 项)/netsysmessages.proto/statrequests.proto |
| hagcp KVCS 实体表 | `E:\...\src\protolinking\keyValueSet.ts` | KeyValueChangeKey 枚举 60 类 |
| hagcp 数据快照 | `E:\...\packages\hagcp-assets\json\` | 79 类官方数据（commandnodetemplate/armyresourcecategory/Pricing 等） |
| 我们已有的 AS3→proto | `work/hng-server-re/decompiled/proto-files/` | 8 个 .proto（player/war/weapon/.../SR），自产、覆盖面小于 hagcp |

## 三、工作项

### P1 zipchunk 压缩推送（最高优先，小改动确定收益）—— **已完成（2026-09-17）**

> 落地记录：`protocol.build_zipchunk`（单内层完整帧 gzip + zipchunk proto f1，外层 cmd_id=0）；`messages.ZIP_THRESHOLD=1024` + `_push_kvcs_batch(allow_zip=True)`（压缩无收益回退明文，`ZIP_THRESHOLD=0` 一键全关）；views.py warmap 快照由逐行单帧改为**单批量帧**（全 set 顺序保持）+ zip；tick.py 广播批次 zip（combat 会话显式 False）。测试 `test_zipchunk.py` 8/8（外层契约 / gunzip 逐字节对拍 / 小帧不压 / allow_zip=False / 阈值=0 回滚 / 无收益回退），test_kvcs_batch 26/26、war 域全绿（N2 广播桩签名同步补 allow_zip）。run_all 16 项失败均为环境性（DB/服务器未启动，ConnectionRefusedError，抽验证实），与基线判定规则一致。待真机验证压缩比日志与客户端 warmap 渲染。

**做什么**
1. `protocol.py` 新增 `build_zipchunk(inner_frame_bytes, cmd_id=0)`：
   - `gz = gzip.compress(inner_frame_bytes)`（标准 gzip 流，与官方流量一致——hagcp wiresharkDecode 以标准 gzip 解开官方 zipchunk）；
   - zipchunk proto = `pb_string(1, gz)`（bytes 字段，wire type 2，见我们自己的 `decompiled/proto-files/netsysmessages/zipchunk.proto`）；
   - 外层帧 = `build_response(cmd_id, 'zipchunk', proto_bytes)` 布局——客户端读 `subChunks[0]` 为标题 `"zipchunk"`、`subChunks[0].subChunks[0]` 为 proto 体（[Chunk.as:98-99](file:///d:/HnG-test/work/hng-server-re/decompiled/swf-frontend/scripts/dk/reto/heroesandgenerals/service/Chunk.as#L98-L99)），外层 cmd_id 被客户端丢弃，固定 0。
2. `messages.py` 的 `_push_kvcs_batch` 增加阈值开关：组好的 main 帧字节数 ≥ `ZIP_THRESHOLD = 1024` 时改走 zipchunk 包装再 `writer.write`；小帧保持原样（gzip 开销不划算）。阈值定义在 messages.py 模块级常量。
3. 接入点（按收益排序）：
   - `handlers/views.py` warmap 初始全量推（~90 battlefieldstatus + supplyline/accesspoint/commandnode/capital 行，合成一个大批量帧后整体 gzip）；
   - `war_campaign/tick.py` 广播路径中走 `_push_kvcs_batch` 的大事件批次。
   - ViewHandlerPulse 保持不动（帧太小）。

**关键决策（已定）**
- **每个 zipchunk 只装 1 个内层完整帧**：客户端 `parseData(zippedData)` 只解析解压数据的第一棵 Chunk 树（[Chunk.as:102](file:///d:/HnG-test/work/hng-server-re/decompiled/swf-frontend/scripts/dk/reto/heroesandgenerals/service/Chunk.as#L102)），多帧会被丢弃。大批量靠 KVCS 批量消息本身装多行，不靠 zipchunk 装多帧。
- **绝不发给 `is_combat_server` 会话**（hng.exe/ZServer 连接）：与现有 pulse 规则一致，官方 warserver 从不给专用服发 KVCS。
- zip 条件额外要求：会话非 combat、帧 ≥ 阈值，两条件在 `_push_kvcs_batch` 内统一判断（writer 无法反查会话 → 由调用点传入 `allow_zip` 或将判断放 views/tick 调用侧；**采用：`_push_kvcs_batch` 增加参数 `allow_zip=True`，combat 相关调用点显式传 False**）。

**测试**：新增 `tests/test_zipchunk.py`——构造批量事件 → 走 `_push_kvcs_batch(allow_zip=True)` 捕获写出字节 → 自证：外层标题为 `zipchunk`、gunzip 后 `Chunk.parse` 还原出内层 main 帧、行内容与不压缩路径一致；边界：恰低于阈值的帧不压缩、`allow_zip=False` 永不压缩。

**验证**：`tests/run_all.py` 全量回归 + 真机客户端登录订阅 warmap，观察 hng_boot.log 无协议错误、日志输出压缩比（`[kvcs] batch ... zip 9123B→1187B`）。

### P2 协议字典审计：Response 枚举 + KVCS 实体覆盖（调研已完成，范围收窄）

**调研结论（2026-09-17）**
- **错误码基础好于预期，无系统性错码**：我们已用语义化 RC_* 常量且抽查全部对上官方枚举（53=no_name、36=invalid_name、170~176=company 族、161=squad_not_found、158=squad_slot_not_found、97/98/91/92 登录封禁族）。
- **KVCS 覆盖 47/59 直接命中**，加上 [warmap_view.py](file:///d:/HnG-test/work/hng-server-re/server/war_campaign/warmap_view.py#L270-L316) 与 tick.py 的动态表名（battlefieldstatus/supplylinestatus/accesspointstatus/battle/air_*/transport_*）后，真正从未推送的仅 12 表，其中 5 个静态表（capital/supplyline/accesspoint/accesspointtemplate/armyresourcecategory）由 WarDatabase 文件通道覆盖、不算缺口；4 个（StoreDiscount/ShopWarBondItemCounter/PlayerGoldOffer/PlayerPartnerInfo）为折扣/营销功能，有意不做。**剩余待审计疑点 3 个**：

| 疑点表 | 疑点 |
|---|---|
| `Squad` | 我们只推 SquadSession/SquadMemberSession 会话行，持久小队定义行从不推——小队 UI 是否有静默缺失待查 |
| `missionresource` | 战斗任务内资源行，可能影响 MissionDetails 显示 |
| `UnlockNotification` | 解锁弹窗数据源之一，需确认 progression 走的 Notification 是否已覆盖该 UI |

**疑点审计结论（2026-09-17，客户端反编译证据闭环）**

1. **`missionresource` —— 真功能缺口，建议补（高价值）**。证据链：客户端 `AssaultTeamModel.isInBattle` 的**唯一来源**是 MissionResourceTable 行变更（[AssaultTeamModel.as:340-403,627](file:///d:/HnG-test/work/hng-server-re/decompiled/swf-frontend/scripts/dk/reto/heroesandgenerals/components/assaultteams/model/AssaultTeamModel.as#L340-L403)：ADD→true、DELETE/归零→false，含他人 AT）；`isInBattle` 决定战争地图 AT 按钮"撤退"（[MultiMarkerAndAssaultTeamDetailsMediator.as:502-506](file:///d:/HnG-test/work/hng-server-re/decompiled/swf-frontend/scripts/dk/reto/heroesandgenerals/components/war/view/MultiMarkerAndAssaultTeamDetailsMediator.as#L502-L506)）与 AT 列表撤退按钮；撤退窗口打开即查该表、无行**自关闭**（[AssaultTeamRetreatWindowMediator.as:166-189](file:///d:/HnG-test/work/hng-server-re/decompiled/swf-frontend/scripts/dk/reto/heroesandgenerals/view/window/assaultteam/AssaultTeamRetreatWindowMediator.as#L166-L189)）。我们 `handle_retreat_command_node_request`（war_at.py:604，含测试）**已实现却永远没有 UI 入口**；且 war 审计已知的"结算不释放幸存 AT（state=2 挡移动）"卡死问题，玩家也因此无自救入口。
   **补法**：war_battle 成局 AT 入战时推 missionresource 行（10 字段：id/missionid/warid/factionId/commandNodeWarInstanceId/commandNodeTemplateId/accesspointTemplateId/locked/allowedToLobby/spawnInAir——数据全在 war_battles+AT 行）；结算/撤退时推 delete 行或 commandNodeWarInstanceId=0 的 update（触发客户端 handleAssaultTeamLeavingBattle）。行格式见 hagcp HnG_States.proto:2624。
2. **`UnlockNotification` —— UX 缺口，建议补（中价值）**。客户端：UnlockNotificationTable 变更 → `UnlockNotificationModel` 弹 UnlockNotificationWindow（新解锁/勋带升级）+ SoldierOverview 角标 + FirstTimeUserTutorial 读 allEntries；行含 progressionUnlockId（装备/武器/载具解锁）+ ribbonTemplateId/ribbonLevel（勋带升级）。我们现有 `Notification` 表推送只覆盖升级/新模式弹窗（progression.py:119-138），**不覆盖这些**。回执 handler 已存在（[misc.py:449](file:///d:/HnG-test/work/hng-server-re/server/handlers/misc.py#L449) 回 ok）。
   **补法**：解锁生效点（progression/combat 结算处，含 character_extra_unlocks 写入与 character_ribbon_xp 升级判定）推 UnlockNotification 行，id 发号复用 _NOTIF_ROWS 模式；玩家已读（RespondToUnlockNotificationRequest）后推 delete 行。
3. **`Squad` —— 展示层小缺口，暂不补（记录关闭）**。客户端 SquadTable 仅用于小队**名称显示**（SquadModel.onSquadChange→setSquadName、init 时 getFromCharacterId）；SquadSession（我们全量推）已携带同名字段（f10 name、f5-7 支援槽、f12/13 匹配标志与预设列表，见 hagcp HnG_States.proto:4218）。唯一差异是登录后到首个 SquadSession 推送前的名称显示，功能无影响；实测若见小队名显示异常再补（数据在 squads 表全有）。

**剩余工作（落地时）**
1. vendor Response 枚举 281 项（名→值）为参考字典（`server/tools/` 或 tests 资源），对 handlers/ 全量 sweep 一遍显式错误码 + 裸数字；
2. ~~实现 missionresource 推送~~ **已完成（2026-09-17）**：`battle_msgs.msg_missionresource_row`（行 id=AT id，一 AT 一行；f5=AT id、f2=mission_records.id）接入全部 7 处——城战成局/增援、野外成局/增援（新行 ADD）→ `settle_war_battle` 与撤退/解散 handler（行 DELETE 广播，客户端 isInBattle=false）→ `build_warmap_snapshot` 重登录补行（仅 state=2，孤儿会战跳过）。契约依 AssaultTeamModel.as:340-390（入战必新行 ADD，UPDATE 分支无 handler；离战行 DELETE）。测试：test_war_movement.py D/J/I 段 + 快照断言全绿，war 域 8 文件 0 FAIL（含修 FakeStore create_mission_record 契约对齐真实 store 返回 int）。待真机观察撤退按钮；
3. ~~实现 UnlockNotification 推送~~ **已完成（2026-09-17）**：`messages._msg_unlock_notification`（9 字段：id/playerId/characterId/progressionUnlockId/ribbonTemplateId/ribbonLevel/unlockTimestamp(Zulu)/notificationResponse/markedForDelete）+ `player_service.push_ribbon_unlock_notifications`（勋带等级提升区间 `(old_lv, new_lv]` 内每个 progressionUnlock uid 一行，语义="购买/使用资格达成"而非"已拥有"——已拥有武器/载具客户端 checkIfWeaponIsOwned 自清不弹）。生效点：combat 结算（`set_ribbon_xp_floor` 前快照 XP 算等级差，收集后经 `_finish_mission_sessions` 新参 ribbon_upgrades 按玩家推）、shop `AddRibbonLevel`（购买勋带等级）；misc 回执 handler 补推 delete 行清表（防重连重弹）。测试：新增 test_unlock_notification.py 14/14（字段/区间行数/发号唯一/未涨不推/无订阅跳过），回归 test_combat_exit_finish 7 项、purchase/progression/camo、war 域 8 文件、kvcs_batch/zipchunk 全绿。待真机观察解锁弹窗；
4. 产出 `server/docs/protocol-audit-2026-09.md`（枚举字典 + 三表审计结论存档）。

**验证**：审计报告每条结论附双方证据（我们代码行 + 客户端反编译行）；两个补推送项跑相关 handler 测试 + 真机观察（AT 入战后地图出现"撤退"按钮、撤退窗口正常打开；解锁/勋带升级弹窗出现）。

### P3 proto round-trip 一致性测试基建 —— **已完成（2026-09-17）**

**落地记录（2026-09-17）**：新增 `server/tests/conformance/`——`fix_and_compile.py`（vendor 物料生成：机械转换 hagcp 5 个 proto 并 protoc 生成 bindings，生成物 5 个 `_pb2.py` 一并提交，测试环境无需 protoc）+ `test_pb_roundtrip.py`（27 case：war 大地图/battle 行 12 条 + SOLDIER_VIEW 装备行 12 条 + UnlockNotification/DepotNewItem/Ribbon 3 条，三层验证：L1 官方库解析我们的编码无异常、L2 我们 `pb_parse_multi` 解码 → 按官方 DESCRIPTOR 重建 → `ListFields` 逐字段等价、L3 官方编码 → 我们解码与官方值一致）。
- **抓到并修复第一个真 bug**：`battle_msgs.msg_missionresource_row` f1 官方 schema 是 int64（varint），我们误用 `pb_sfixed64`（wire type 1，按 battle 行 f1 的惯例误套）——harness 首轮即捞出，证明字节合规人工比对确有静默错误风险；
- 范围调整：原计划含 ResponseBattleFieldSettings/PlayerSettings，落地时剔除——其 schema 源是 player.dll 逆向描述符（hng.exe 私有协议），hagcp proto 未覆盖，无从对拍；解码侧用 `pb_parse_multi`（repeated-aware）而非原计划的 `pb_parse_loose`；
- `run_all.py` 升级为 `os.walk` 递归收录子目录（`tests/conformance/` 入列）；
- 结果：conformance 27/27 PASS；war 域回归全绿（war_movement 174/174、war_campaign 82/82、war_at_supply 133/133、war_battle_details 20/20，test_war_movement 的 missionresource 行断言同步改为 varint 解码）。

**调研结论（2026-09-17，spike 实测通过）**
- hagcp 的 proto **不能直接编译**（给宽松的 protobufjs 用：`package` 行在 `syntax` 前、声明 proto3 却用 proto2 的 required/optional），但机械转换三步即可过 protoc：① syntax/proto2 行置首并补 package/import 头 ② proto3→proto2 声明 ③ `required`→`optional`（wire 字节完全等价，且绕开 upb "Too many required fields" 上限）。转换后 4 文件全过，生成 267KB bindings（780 消息）。
- `battlefieldstatus` 双向 round-trip 实测：官方库编码 → 我们 `pb_parse` 解码字段全对（含 sfixed64 wire type 1）；我们手写编码 → 官方 `ParseFromString` 字段一致且**字节级相同**；故意错 wire type 能被 harness 检出。
- 字段定义交叉验证：hagcp 的 battlefieldstatus（f1 sfixed64 id / f2 int64 warid / f3 int64 battlefieldid / f4 int64 factionid + optional f6）与我们 [warmap_view.py:40-47](file:///d:/HnG-test/work/hng-server-re/server/war_campaign/warmap_view.py#L40-L47) 完全吻合。
- 环境：项目 venv 已装 grpcio-tools + protobuf（protobuf 7.36.1，dev 依赖，运行时零依赖）；spike 脚本在 `%TEMP%\hagcp_proto_spike\`（fix_and_compile.py / roundtrip_spike.py），落地时直接搬。

**剩余工作（落地时）**
1. vendor hagcp 4 个 .proto 到 `server/tests/conformance/protos/`（附 MIT 出处注释），连同转换脚本 `fix_and_compile.py`（转换后版本一并提交，_pb2.py 生成物提交，测试环境无需 protoc）；
2. 新增 `tests/conformance/test_pb_roundtrip.py`：对我们服务端实际构建的关键消息（首批 ~30 个：login2 族、KVCS 行类型 battlefieldstatus/supplylinestatus/commandnode*/player/character/weapon/vehicle/squad、ResponseBattleFieldSettings、PlayerSettings）做双向比对：
   - 官方 `_pb2` 随机/边界值编码 → 我们 `pb_parse_loose` 解码 → 我们 `_msg_*` 重建 → 字段级等价比对（不强求字节级一致）；
   - 反向：我们 `_msg_*` 的产出 → 官方库 `ParseFromString`（必须无错且字段等值）。
3. 建立消息名 → `_msg_*` 函数的手工映射表（同文件内声明式列表），后续新增消息按表扩展。

**已知注意点**：proto2 语义下 repeated bool 默认非 packed，与 hurlant AS3 库一致（我们的 `pb_packed_bool` 是特例兼容项，映射表中单独标注验证）。

**验证**：`tests/run_all.py` 收录新测试；首轮跑出的不一致逐条定性（测试错 / 我们编码错 / hagcp proto 与最终版客户端有出入），只修"我们编码错"。

### P4 官方数据快照 diff 与参数校准 —— **已完成（2026-09-17）**

**落地记录（2026-09-17）**：新增 `server/tools/community_asset_diff.py`（四表字段映射含类型声明；本服侧按 pb_parse 契约手动解码——wt5 float32 手动 unpack、负数 varint 补码折返、int32 域等价 wrap，不动运行时 protocol.py）。产出 `docs/community-asset-diff-2026-09.md`（含三分类结论）。**关键发现**：
- commandnodetemplate 69/69 行 8 字段全一致；armyresourcecategory 18/18 行 5 字段全一致（含 captureTransferMultiplierWar 5.4 / Staged 1.8 聚合）——**P4-3 automission 加权结算的证据前提成立，可启动**；
- capital 共同仅 4 行（id 体系时代差：本服大哈希 vs hagcp 小序号），共同行 victoryPoints 一致，defaultFactionTemplateId 3 行差异倾向保留本服值；
- Pricing 7132 共同行仅 id 69021000 一行差异（costGold 2850 vs 3），定性快照时代差，保留本服值；
- 全部差异均无一条指向本服数据错误，无需落库改动。

**P4-3 落地记录（2026-09-17，已完成）**：`wardb.army_resource_avg_sizes()`（armyresourcecategory f4 avgSize → {类别: 权重}，wt5 手动 unpack、缓存、查表失败回 {} 退化为旧口径）+ `automission._power_winner` 无人局兜底结算从"各类别资源原始数量直接加总"（= 1 步兵等价 1 运输机）升级为 Σ(数量×avgSize) 加权强度（步兵 1 / 坦克 35-45 / 运输机 55，未知类别缺省 1）。测试：test_war_movement 新增 K11（运输机 2×55=110 > 步兵 100×1=100 攻方胜，旧总量口径会误判守方胜），war_movement 174/174、war 域 10 文件（at_stats_rank 43 / wardb_rts 33 / war_at_supply 133 / war_battle_details 20 / war_campaign 82 / war_campaign_bonus 9 / war_faction_join 17 / war_supply_column 50）全绿；test_rts_war_db 环境性跳过（PostgreSQL 不可达，基线内）。

**做什么**
1. 新增一次性工具 `work/hng-server-re/tools/community_asset_diff.py`（读 `HAGCP_ASSETS` 环境变量指向 hagcp json 目录，默认 `E:\devProject\Heroes-and-Generals-community-project\packages\hagcp-assets\json`），与我们 `wardb.py` 加载的 WarDatabase 表做数值 diff：
   - `commandnodetemplate`：speed/attackradius/transportradius/convertTo 配对/WarfundBonus/升级树 parenttemplateid；
   - `armyresourcecategory`：avgSize/speed/**捕获转移系数**（我们 automission 简化为资源总量比较的直接改进依据）；
   - `capital`：victoryPoints（对照我们胜利条件 N=9 vs 官方值）；
   - `Pricing`/`StoreBundle`/`ShopWarBondItemTemplate`：经济数值抽查。
2. 产出 `server/docs/community-asset-diff-2026-09.md`：差异表 + "采纳/不采纳/需人工判断"三分类（hagcp 快照与最终版客户端 WarDatabase 存在时代差，**只作参考依据，不自动落库**）；
3. 若 diff 证实 armyresourcecategory 系数可用：将 `war_campaign/` automission 结算从"资源总量比较"升级为按类别加权（avgSize×数量）比较——此项为独立代码改动，凭 diff 证据单独提交。

**验证**：diff 脚本可重复执行；automission 改动跑 `test_rts_war_db` / war_campaign 相关 300+ 断言。

### P5 KeyValueSyncCmd 断线差量同步 —— **调研完成：不可行，已关闭**

**调研结论（2026-09-17，有证据关闭）**：KeyValueSyncCmd 四个消息类（GetVersion/GetVersionResponse/RequestSync/RequestSyncResponse）在最终版 Flash 客户端中是**死代码**——三个 SWF（swf-frontend / swf-login-service / swf-rank）全文检索，仅出现在 proto 类注册文件（`decompiled/proto-as3-core/scripts/netsysmessages/KeyValueSyncCmd_*.as` 的 `_protoClasses` 注册表），**业务代码零引用**：客户端永远不会发送 GetVersion，收到 Response 也没有任何 handler 消费。断线重连维持现有 unsubscribe→re-subscribe 全量重推方案。（这与 hagcp 自己也未接线该机制一致。）

无需任何后续动作；本节仅作结论存档。

## 四、执行顺序与交付物

| 顺序 | 工作项 | 改动文件 | 交付物 |
|---|---|---|---|
| 1 | P1 zipchunk | protocol.py / messages.py / handlers/views.py / war_campaign/tick.py + 新测试 | 代码 + test_zipchunk.py + 日志压缩比 |
| 2 | P2 审计 | 零或少量 handler 小修 + 新文档 | docs/protocol-audit-2026-09.md（枚举字典 + 3 疑点结论） |
| 3 | P3 测试基建 | server/tests/conformance/（新目录） | vendored protos + 转换脚本 + roundtrip 测试 |
| 4 | P4 数据 diff | tools/community_asset_diff.py（新）+ 可选 war_campaign 改动 | diff 报告 +（视证据）automission 加权 |
| — | ~~P5 KeyValueSyncCmd~~ | 已关闭（客户端死代码，见第三节） | 结论已存档于本计划 |

各项独立可落地、独立可回滚（P1 的阈值常量置 0 即全量关闭）。

## 五、假设与决策汇总

1. **本次执行的动作仅为落位文档**：将本计划复制到 `work/hng-server-re/server/docs/community-project-adoption-plan.md`（去掉 Plan Mode 头部说明，保留全文）。P1–P4 的实现是后续独立任务，不在本次范围（P2/P3/P5 的调研部分已于 2026-09-17 完成，结论已并入正文）。
2. E 盘 hagcp 路径只读引用；vendor 其 proto 需在文件头注明 MIT 来源（合规）。
3. zipchunk：单内层帧、外层 cmd_id=0、阈值 1024B、combat 连接禁用——四条决策均有客户端反编译证据。
4. hagcp statrequests.proto 的字段编号与我们 statserver wire statId 是两套空间，**statId 59 不在本计划内**（已验证无解）。
5. 所有代码改动以现有测试套件全绿为门槛（`docs/baseline-2026-09.md` 中 5 项既有稳定失败不算回归）。

## 六、验证方式（整体）

1. `python tests/run_all.py`（server/ 下）全量回归；
2. 真机客户端：登录 → 订阅 warmap（P1 生效路径）→ 观察战争地图正常渲染、hng_boot.log 无新增协议错误、服务端日志出现 zip 压缩比记录；
3. 审计/调研类交付物以文档评审为准（每条结论附代码行证据）。
