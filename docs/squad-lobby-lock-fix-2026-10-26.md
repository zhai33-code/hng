# 组队后误锁定（2026-10-26）

## 根因与修复

`handlers/squad.py::_squad_is_locked` 将数据库 `matchmaking_flag=True`
当成正在排队。该字段实际编码为 `SquadSession.f12`
（`matchMakeWarMissionFlag`），是战争模式筛选项。客户端
`SquadModel.as:1360,1375` 将它读写为 `warMissionsSelected`，并非排队状态。

大厅选中战争模式后，服务端错误下发 f20/f21 两个锁定字段；队员的
`SetSquadMemberStatusRequest` 返回 261，无法准备，邀请接受等操作也会被拦截。

锁定判断现仅依据成员实际存在于 `_MM_QUEUES` 或成员状态为 `InCombat`。
战争模式选项为 True、单独 Ready、撤队后遗留 True 均不会再误锁定。
排队和战斗期间的成员保护、身份检查仍然保留。

## 验证

- 新增测试在修复前复现：两个锁定字段错误为 True，准备/取消准备返回 261。
- `test_squad_authorization.py`：23/23 通过。
- `test_squad_invite_implicit.py`：31/31 通过。
- `test_squad_multiplayer.py`：通过。
- 从 server 目录运行 `python -m pytest tests/test_online_log_fixes.py -q`：23 通过。
- `test_squad_enqueue.py`：整队入池 7/7 通过；战争撮合 W6/W6c/W6f 失败。
  单独加载部署快照中修复前的锁判断函数复跑，得到完全相同的三项失败，
  确认与本次锁判断变更无关。

本次仅修改本地源码及测试，尚未部署或重启线上服务器，也未进行双客户端实测。

后续补充：第二轮走查确认上述三项战争撮合测试失败是 WAR_SKIP_PREP 环境配置
未隔离导致；已修复测试隔离，现均通过。详见 squad-lifecycle-audit-2026-10-26.md。
