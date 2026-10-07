# 教程进度持久化与战斗内教程兼容方案

状态：待实现（当前使用兼容模式）  
记录日期：2026-10-07

## 1. 背景与当前结论

当前版本若在战斗 `PlayerSettings.f28 triggeredTutorialTemplateIds` 中下发空列表，
客户端进入部署地图后会连续弹出战斗教程。2026-10-07 的复现中，客户端依次上报
`TutorialRequest`，教程 ID 为 `1`、`8`、`18`，随后 `hng.exe` 在
`player.dll+0x6f1f21` 发生 `0xc0000005` 访问冲突。

地图坐标逻辑回退为 `(0, 0)` 后仍可复现，因此没有证据表明地图坐标是此次崩溃
原因。教程弹窗后的请求/响应链路仍是首要嫌疑，但尚未确认客户端期待的响应类型。

为保持可玩性，当前暂时对齐已验证可用的 `bf65521`：当调用方没有显式提供教程
进度时，战斗 `PlayerSettings.f28` 下发以下 28 个“已触发”教程 ID：

```text
1, 2, 3, 4, 6, 7, ..., 29
```

注意：字段语义是“已触发”，不是“已完成”。当前兼容值也不会批量修改数据库。

## 2. 必须分开的三套状态

### 2.1 账号新手流程完成态

- 数据源：`players.tutorials_completed`
- 下发位置：大厅 `HnG_States.player.f32 tutorialsCompleted`
- 写入请求：`SetTutorialCompletedRequest`
- 当前用途：决定首次阵营选择、免费士兵招募、训练匹配、聊天和商店 UI 等全局新手流程
- 作用域：账号级

该值是整个新手流程的完成开关，不是单个教程模板的状态。

### 2.2 大厅提示的已触发集合

- 数据源：`player_tutorials_triggered`
- 下发位置：`Tutorial` KVCS 行的 `f3 triggeredTutorialTemplateIds`
- 写入请求：`MarkTutorialTriggeredRequest`
- 作用域：账号级

这一层用于大厅、战报、生涯军令等 Flash UI 提示。它不应直接表示战斗内教程已经完成。

### 2.3 战斗内教程的已触发集合

- 当前数据表：`character_tutorials`
- 下发位置：战斗 `PlayerSettings.f28 triggeredTutorialTemplateIds`
- 上报请求：战斗连接上的 `TutorialRequest`
- 建议作用域：暂按角色级处理，最终以官服抓包/客户端逆向结果为准

当前代码只写 `character_tutorials`，没有读取并传给 `PlayerSettings.f28` 的完整链路。
此外，`TutorialRequest` 只有 PMS ID 和教程 ID，现有证据不足以把它解释为“完成”。

## 3. 当前实现中的缺口

1. `game_server.py` 构建 `PlayerSettings` 时没有读取战斗教程进度，也没有传入
   `tutorial_ids`。
2. `warserver_messages.py` 过去把缺省 `None` 转为空列表，使老客户端认为所有战斗
   教程均未触发。
3. `handlers/combat.py::handle_tutorial_request` 当前调用
   `mark_character_tutorial(..., completed=True)`，把“收到教程事件”直接当成“已完成”，
   语义证据不足。
4. `TutorialRequest` 当前返回通用 `HandlerErrorResponse`。尚未证明这是客户端期待的
   响应；本次崩溃发生在该请求链路之后。
5. `store/schema.py` 中“完成态走 PlayerSettings f24”的旧注释不正确：战斗
   `PlayerSettings.f24` 是 `firstActionGame`；全局教程完成态在大厅 player 的 f32。
6. `character_tutorials.completed` 目前没有可靠的完成事件来源，也没有读路径。

## 4. 建议的最终数据流

### 4.1 登录与大厅订阅

1. 从 `players.tutorials_completed` 构建大厅 player f32。
2. 从 `player_tutorials_triggered` 构建 `Tutorial` KVCS。
3. `MarkTutorialTriggeredRequest` 幂等写入账号级触发集合，并立即重推该 KVCS 行。
4. `SetTutorialCompletedRequest` 只更新全局完成态并重推 player 行。

### 4.2 玩家进入战斗

1. 通过 PMS 精确定位玩家和本次参战角色。
2. 查询当前角色的战斗教程触发集合，例如新增：

   ```python
   list_character_tutorials_triggered(character_id)
   ```

3. 将结果显式传入 `_msg_player_settings(..., tutorial_ids=ids)`。
4. `None` 只代表调用方没有提供可靠数据，可按配置进入兼容路径；显式空列表才表示
   新角色尚未触发任何战斗教程。

### 4.3 战斗内教程事件

`TutorialRequest` 的推荐处理顺序：

1. 解析并严格校验 `playerMissionSessionId` 和教程 ID。
2. PMS 必须属于当前连接的玩家，角色必须属于该玩家且正在本局参战。
3. 教程 ID 应在有效模板集合中，避免任意 ID 污染数据库。
4. 在协议语义未进一步证实前，只幂等写 `triggered=TRUE`，不写
   `completed=TRUE`。
5. 更新本局内存集合，防止同一连接重复处理。
6. 使用抓包或逆向确认过的响应方式；不能把通用 `HandlerErrorResponse` 当成已验证协议。

只有发现独立、明确的完成事件或字段后，才能设置 `completed=TRUE`。如果客户端协议
根本没有逐项完成态，应删除或停止使用该列，避免制造虚假状态。

## 5. 兼容模式与真实进度模式

建议增加配置项，例如：

```text
HNG_TUTORIAL_PROGRESS_MODE=compat|tracked
```

- `compat`：缺少显式数据时下发 `bf65521` 的 28 项默认集合；这是当前安全默认值。
- `tracked`：严格读取数据库；新角色允许空集合并真正展示教程。只有确认
  `TutorialRequest` 的正确响应且完成端到端测试后才能启用。

即使实现真实进度，也应保留 `compat` 作为回滚开关。

## 6. 数据迁移建议

切换到真实进度前，应避免所有存量账号重新弹教程：

1. 存量账号/角色预置 `bf65521` 的 28 项触发集合，或设置明确的
   `tutorial_progress_migrated` 标记并在读取时使用兼容集合。
2. 新建的专用测试账号不预置，以验证完整教程流程。
3. 不要用 `players.tutorials_completed=1` 代替战斗 f28；两者控制的客户端模块不同。
4. 不要把账号级 `player_tutorials_triggered` 与角色级战斗教程表直接互相覆盖。
5. 迁移脚本必须幂等，并能统计迁移账号数、角色数和教程记录数。

## 7. 在启用真实教程前必须确认的问题

1. `TutorialRequest` 是单向通知，还是需要专用响应？若需要，响应消息名和字段是什么？
2. 请求表示“弹出”“确认”“完成”中的哪一种状态？
3. 战斗教程进度是账号级还是角色级？
4. `PlayerSettings.f28` 是否允许真正的空列表？客户端崩溃是否由错误响应、缺少其他
   配套状态，或教程本身的资源/流程缺失造成？
5. 教程 ID 5 为什么被 `bf65521` 跳过，它是否是废弃模板或由其他流程控制？
6. 新手训练局是否需要特定 mission preset、地图、资源或机器人配置才能安全完成教程？

优先通过官服/已知正常私服抓包回答 1–3；若无法抓包，再对 `player.dll` 中
`TutorialRequest` 的注册描述符和回调进行逆向。

## 8. 验收用例

### 兼容模式

- 存量玩家进入普通局不弹战斗教程。
- 进入部署地图、选择出生点、关闭 UI 均不崩溃。
- 重连同一战局时 f28 内容保持一致。
- 大厅 `Tutorial` KVCS 和 `players.tutorials_completed` 行为不受影响。

### 真实进度模式

- 全新测试角色首次进入时只出现预期的第一项教程。
- 点击确认后客户端不崩，服务端收到并正确响应 `TutorialRequest`。
- 退出重进后已触发教程不再弹出。
- 未触发的下一项仍能按客户端条件正常出现。
- 同账号不同角色的行为符合最终确认的作用域。
- 非法 PMS、他人角色和非法教程 ID 均被拒绝且不写库。
- 服务端重启后进度仍然存在。
- 全部教程走完后，全局 `tutorials_completed` 只由正确的全局完成请求更新。

## 9. 当前临时实现位置

- 战斗 f28 兼容默认值：`warserver_messages.py::_msg_player_settings`
- 战斗教程请求：`handlers/combat.py::handle_tutorial_request`
- 战斗教程存储：`store/maintenance.py::mark_character_tutorial`
- 大厅触发集合：`handlers/characters.py::push_tutorial_row`
- 大厅完成态：`handlers/characters.py::handle_settutorialcompletedrequest`
- 数据表定义：`store/schema.py`

在上述“必须确认的问题”解决前，不应删除 f28 的兼容默认列表或默认启用
`tracked` 模式。
