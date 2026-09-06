# 重构基线（2026-09-06）

> 阶段 0 产物。重构（阶段 1-3.5）完成后全量复跑，结果必须与本基线**逐项一致**；
> 任何差异即为重构引入的回归。

## 运行环境

- venv：`..\..\.venv\Scripts\python.exe`（仓库根，非 server\ 下）
- 必须 `$env:PYTHONIOENCODING='utf-8'`（否则 GBK 编码崩溃）
- DB：Docker Desktop `hng-postgres`，PlayerStore 自动探测 WSL IP（172.25.164.25:5432）
- e2e 类（7 项）：需先起服务器
  `$env:MM_SIMULATE_COMBAT='1'; python realtest_server.py`
- git tag：`pre-refactor`

## 基线前环境修正（一次性，已执行）

当前 DB 是较新实例，缺少开发期存量账号，已按下表修复：

| 动作 | 原因 |
|---|---|
| 删除残留账号 `tester`（pid=1000004，无子数据） | test_auth 历次运行的遗留，导致 "just added" 用例必挂 |
| 新建 `demo`/demodemo123 + 角色 DemoSoldier(faction=1, id=104) | test_auth/e2e/autorepair/matchmaking/mm_* 的存量账号约定 |
| demo 名下武器 | 由 test_e2e 首跑自动创建（PPSh-41），test_autorepair 依赖 |

`DoubleBird`（test_realdb 尾部查询）未建——该用例仅 informational，主体全过。

## 测试结果（50 项）

### 通过（42 项）

test_admin_adjust_score 20/20、test_at_stats_rank 38/38、
test_auto_squad_disband 13/13、test_battlefield_stagedpos ok=22、
test_battlepass 67/67、test_booster_table_push 22/22、test_career_orders 41/41、
test_codec ok、test_combat_exit_finish 5/5、test_daily_rewards 78/78、
test_dbloadout ALL PASS、test_default_camo_unlock 25/25、test_depot_ammo 9/9、
test_depot_fixes 37/37、test_equipment_wear 15/15、test_login_queue 31/31、
test_mail_push 21/21、test_membership_queue 14/14、test_mm_latejoin 67/67、
test_mm_pool 32/32、test_mod_class_strip 30/30、test_player_ids（静默通过）、
test_player_report 4/4、test_progression_level 23/23、test_purchase_fixes 19/19、
test_register_captcha（静默通过）、test_rename_validation 71/71、
test_request_coverage PASS、test_settlement_parse 12/12、
test_snow_config（静默通过）、test_squad_invite_implicit 31/31、
test_squad_multiplayer PASS、test_stats_type6 44/44、
test_storebundle_delivery 52/52、test_transactions 51/51、
test_war_at_supply 82/82、test_war_campaign 68/68、test_war_faction_join 17/17、
test_war_movement 68/68、test_war_supply_column 44/44、test_wardb_rts 33/33、
test_codec ok

e2e 类通过（5 项）：

| 测试 | 结果 | 备注 |
|---|---|---|
| test_auth | ALL PASS | 5/5（tester 清理后） |
| test_autorepair | ALL PASS | 需 demo 名下有武器 |
| test_e2e | 主体 PASS | 协议流程全过；尾部 `docker exec` DB dump 辅助输出在当前环境返回 None（脚本环境问题，非主体） |
| test_regression | ALL PASS 39/39 | |
| test_mm_multiplayer | 28/29 | 稳定挂 1 项 |

### 既有失败（6 项，重构前后必须保持一致）

| 测试 | 结果 | 失败项 | 判定 |
|---|---|---|---|
| test_matchmaking | 11/13 | 'Cancel 响应 ok'、'Cancel 后 delete 匹配行' | 稳定复现；Cancel 时无响应推送，疑与池内状态/AI 补位逻辑相关，历史行为 |
| test_mm_latejoin_e2e | 18/26 | 'A 单人不足 min 不成局'、双方 'PMS Admitted=2'、双方 'PMM delete'、'GMD playerCount=2'、'GMD 参战方含双方 gamertag'、'AI 补位：超时单人成局' 共 8 项 | 稳定复现；与 MM_SIMULATE_COMBAT 无关（两种启动方式结果相同）；疑 AI 补位/min_players 行为与测试预期历史性偏差 |
| test_mm_multiplayer | 28/29 | 'AI 补位：超时单人成局' | 同上 |
| test_realdb | 主体 PASS | 尾部 `FAIL: DoubleBird 账号不存在`（exit 0） | 环境性：DB 缺开发期存量账号 DoubleBird |
| test_rts_war_db | FAIL (exit 1) | 'create_command_node 默认值'、'update_command_node 字段生效'、create_movement 抛 NotNullViolation（commandnode_movements.started_at NOT NULL 但插入未提供） | 代码与 schema 不同步的既有失败 |
| test_e2e | 尾部 TypeError | `docker exec ... weapons` stdout=None | 脚本环境问题；协议主体 L174 已 PASS |

### 判定规则

- 上表"通过"列：重构后必须全部保持通过
- "既有失败"列：重构后失败项**集合与数量完全一致**（含 test_mm_latejoin_e2e 的 8 项明细）
- 重构期间不修这些历史失败（如需修复，重构完成后独立 commit 并同步更新本表）

## 复跑命令

```powershell
cd D:\HnG-test\work\hng-server-re\server
$env:PYTHONIOENCODING='utf-8'
# 单元类（无需服务器）
foreach ($f in Get-ChildItem tests\test_*.py) { & ..\.venv\Scripts\python.exe $f.FullName }
# e2e 类（先起服务器，另一终端）
$env:MM_SIMULATE_COMBAT='1'; & ..\.venv\Scripts\python.exe realtest_server.py
```
