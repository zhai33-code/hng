# 小队流程第二轮走查（2026-10-26）

范围：邀请、准备、删除成员、整队入池/撤队、个人退场与战局结束返回。
本次修复只落在本地源码，未部署、未重启服务器、未做双客户端实测。

## 确认并修复

1. **未接受邀请可直接准备入队**：SetSquadMemberStatus 原先允许把
   InvitationPending 改为 InLobby/Ready。现在拒绝该请求，仅接受邀请能建立成员资格。
2. **邀请者受被邀请者的单人队列影响**：锁判断原先将 pending 行的队列也计入，
   撤队还会取消该玩家独立的单人匹配。现在 pending 不影响队伍锁定，
   撤队只清理 ctx.squad_db_id 与本队相同的队列。被邀者自身仍在匹配时不能接受邀请。
3. **返回大厅自动接受邀请**：reset_real_squad_to_lobby 原先无条件将全部成员改成
   InLobby。现在保留 InvitationPending。
4. **排队覆盖战争模式选项**：入池写 True、返回写 False，把模式筛选当作排队状态。
   已移除这两处写入；选项由原筛选请求负责，实际锁定依据队列和战斗状态。
5. **中间槽位成员退出后槽位未重排**：删除只推 DELETE，客户端剩余成员保留旧槽位；
   后续邀请可能复用其槽位。现在踢人、离队、拒绝邀请后重推剩余成员的连续槽位。
6. **单人退场重置全队战斗状态**：个人放弃/离线释放原先整队回大厅，离线释放甚至
   不执行持久化恢复。现在 player_ids 限定退出者，其他战斗成员保持 InCombat。
7. **普通同步清掉在战队友的 missionId**：筛选同步和默认全队同步原先发 mission=0。
   现在按接收者的成员状态及实际战局关联下发；主动全队结束仍清零。

修改文件：handlers/squad.py、matchmaking.py、handlers/mission.py。

## 验证

新增 test_squad_lifecycle_boundaries.py（13 项），并在
test_squad_post_battle_disband.py 补个人退场的在线/离线 × 队长/成员四种组合。
新增边界用例在修复前复现了待邀请成员晋升、误锁、误撤队及旧槽位问题。

最终相关 pytest 组合 **70/70 通过**；脚本断言：

- test_squad_authorization.py：23/23
- test_squad_invite_implicit.py：31/31
- test_auto_squad_disband.py：30/30
- test_squad_enqueue.py：7 项普通入池 + 15 项战争路由全部通过

合计 176 项测试/脚本断言通过。使用模拟存储和会话验证行为，不代表真实双端验收。

前一轮战争路由 W6/W6c/W6f 失败来自本机 WAR_SKIP_PREP 配置；测试没有隔离配置，
首个断言提前消费队列后导致后续断言连锁失败。本次测试显式隔离并恢复该配置，
无需修改战争撮合业务代码。
