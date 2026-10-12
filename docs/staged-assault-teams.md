# 普通局系统突击队到达提示

2026-10-10：普通匹配局在 `ActiveMissionRequestEventListResponse.f5`
下发与资源池抽取名单同源的系统 AT。每阵营按真实
`commandnodetemplate` 模板生成，重复抽中的同型 AT 各有独立 ID。

- `ownerPlayerId=0`，`ownerGamertag=""`。客户端到达提示函数
  `sub_1802991A0` 在名称为空时直接使用硬编码 `High Command`。
- ID 由 mission、阵营、名单序号生成，连续轮询与资源消耗后保持不变。
- 攻方 AT 按已有资源分线挂载；守方和未分线 AT 使用本方出兵锚点。
- 只生成展示记录，资源数量仍取已有资源池；不额外增加兵力。
- 战争局继续下发真实 AT 实例、属主和模板；缺失资源池不生成系统 AT。

实现：`force_pool.staged_assault_teams`、
`handlers.combat.handle_active_mission_event_list`。

验证：`tests/test_staged_assault_teams.py` 4 项通过，包含真实 handler
协议输出、模板/分线资源一致、轮询/消耗稳定、战争属主和缺失数据边界。
`tests/test_war_movement.py` 174/174 通过。
额外 `test_hardcode_fallbacks` 有一项既存断言不一致：要求 player f26
为空，但 `protocol.msg_player` 当前固定使用 `metrics-demo`。

尚需客户端实测：新开普通局，检查开局 AT 到达提示、High Command
名称及类型是否正确，等待后续轮询确认没有重复到达提示。
