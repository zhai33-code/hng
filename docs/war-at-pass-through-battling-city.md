# 战争行军 bug 排查：地面 AT 途经交战城市不停下、直接穿城而过

> 日期：2026-09-13
> 状态：**已定位根因，未修复**
> 现象：地面部队（AT）移动过程中，如果行军路径经过已交战（有进行中会战）的城市，
> 不会停下来加入战斗/被拦截，会直接穿过交战城市继续前往目的地。

---

## 一句话结论

`advance_movements` 的**换段分支**（`war_campaign/tick.py:225-243`，AT 途经中间城市的
唯一检查点）只调用了 `_capture_city_events`（占城检查，城市有会战时主动放弃占领返回
空），**没有调用 `_check_battle_formation`（会战/增援检查）**。会战检查只存在于
**最终到达分支**（`tick.py:212-220`）。叠加 `find_supplyline_path` 的 BFS 寻路
（`war_campaign/common.py:190-207`）完全不筛途经城市（不看归属/驻军/会战），于是：

**途经交战城市 → 占城放弃（无事件）→ 无会战检查 → AT 换到下一段继续走 = 穿城而过。**

---

## 二、链路证据

### 2.1 行军推进的两个检查点（`tick.py` `advance_movements`）

| 分支 | 位置 | 做了什么 | 缺什么 |
|---|---|---|---|
| 到达（终段结束） | `tick.py:102-221` | 落位终点 → `_capture_city_events`（217）→ **`_check_battle_formation`**（219-220：敌驻军成局 / 参战阵营增援入战 state=2） | —（终点行为正确） |
| 换段（途经中间城市） | `tick.py:225-243` | AT map_entity 同步为新段补给线 → **只 `_capture_city_events(segs[idx]['from_bf'])`**（242-243，途经占城） | **没有 `_check_battle_formation`**：途经交战城市不入战增援、不停；途经敌驻军城市也不成战拦截 |

换段时机：`current_segment_index` 推进后 `at.map_entity_id != cur_sl` 的那个 tick，
此时 `segs[idx]['from_bf']` = 上一段终点 = **途经城市**（AT 刚从该城出发上路的瞬间）。

### 2.2 占城检查的守卫放大了问题（`tick.py:434-459` `_capture_city_events`）

`force=False` 时：目标城市有进行中会战（battle_type=城战且 battlefield_id 命中，
449-455 行）或敌对 AT 驻扎（456+ 行）→ **返回 []（放弃占领）**。
即途经交战城市/敌驻城市时占城环节静默 no-op，AT 若无会战检查就无任何拦截。

### 2.3 寻路不筛途经点（`common.py:190-207` `find_supplyline_path`）

纯 BFS 最短路径（静态补给线邻接表 `supplyline_adjacency`，163-187 行缓存），
**不按城市归属/驻军/会战过滤**——路径可以自由穿过敌方控制城市、敌驻军城市、
交战中的城市。途经城市动态变化（行军中途被打起来）时也无重规划。

### 2.4 会战检查本身是对的，只是没被途经路径复用（`tick.py:252-327`）

`_check_battle_formation(store, war_id, at, dest_bf)` 已实现官方语义：
- 同城已有进行中会战且 AT 阵营参战 → `append_war_battle_faction` + `state=2`
  增援入战（284-294 行）；
- 城内有敌驻军 → `create_war_battle` 成局（297-306 行）；
- 第三阵营（会战双方之外）→ 不入战正常驻扎（295 行，两方制契约）。

### 2.5 测试盲区

`tests/test_war_movement.py`：
- H1（839-853 行）只测**途经占城**（无敌情城市）；
- H4（873-884 行）敌驻军成局、I1b（937-946 行）参战阵营增援入战——两者的
  `start_movement` 目标**都是会战城本身**（到达场景）；
- **没有"目的地在更远处、途经交战/敌驻城市"的用例**，故本 bug 未被拦截。

---

## 三、官方语义对照

- war1/war2 + `server/.trae/documents/war-gameplay-fix-plan.md` §2：城市攻防成局
  判据是"AT **到达**敌占城且城内有敌 AT"；参战阵营 AT 到场即增援入战。
  该"到场"语义对路径中转城市同样成立——官方行军 AT 抵达路径任一节点都会做敌情
  检查，不存在"穿过"交战城市。
- 两方制契约（fix-plan 附·三国图考证）：第三阵营路过他人会战 → 不入战正常
  通过/驻扎——该口径现状即正确，修复时需保留。

---

## 四、修复方案（未实施）

在换段分支（`tick.py:225-243`）复用到达分支的"敌情检查"序列，途经城市 =
`segs[idx]['from_bf']`：

1. **途经交战城市且我方阵营参战** → 中止行军（`finish_movement`）+ 落位途经城市
   （`map_entity_id=途经城, type=BATTLEFIELD, state=2`）+ `_check_battle_formation`
   增援分支（`append_war_battle_faction`）+ 推事件（del segment/destination 行 +
   set instance 行，推序沿用到达分支的 dels-first 契约，见 `tick.py:157-174` 注释）。
2. **途经敌驻军城市（无会战）** → 同上落位 + `_check_battle_formation` 成局分支
   （新会战，攻方=途经方）。
3. **第三阵营会战（我方不参战）** → 保持现状正常通过（两方制契约）。
4. **无敌情** → 现状（途经占城 + 继续行军）不变。

实现建议：把到达分支 212-220 行的"占城 + 成局检查"抽成公共协程，两个分支共用；
入战时注意 movement 生命周期选**中止落位**（同到达分支），而非野外会战的
"冻结 + 结算顺延恢复"（那是挂在补给线上的会战，城市会战必须把 AT 钉在城市实体上
才能被结算正确释放）。

### 回归补强（`tests/test_war_movement.py`）

- 途经交战城市（己方参战）→ AT state=2、进 factions 名单、movement 结束、
  不再出现在途行；
- 途经敌驻军城市（无会战）→ 新 battle 成局、AT state=2 落位该城；
- 途经第三方会战城市（我方不参战）→ 正常通过、不进 factions；
- 途经无敌情城市 → 现状占城继续走（防回归）。

---

## 五、关键文件索引

| 侧 | 文件 | 关键位置 |
|---|---|---|
| 服务端（bug 本体） | `server/war_campaign/tick.py` | 换段分支 225-243（缺会战检查）、到达分支 102-221（对照正确）、`_check_battle_formation` 252-327、`_capture_city_events` 434-459（会战守卫 449-455） |
| 服务端（寻路） | `server/war_campaign/common.py` | `find_supplyline_path` 190-207（BFS 不筛途经点）、`supplyline_adjacency` 163-187 |
| 服务端（移动规划） | `server/war_campaign/at_movement.py` | `start_movement` 219+（path=find_supplyline_path 直用）、`reroute` 266+（改道同样不筛） |
| 测试 | `server/tests/test_war_movement.py` | H1 途经占城 839-853、H4 到达成战 873-884、I1b 到达增援 937-946（均无途经敌情用例） |
| 文档 | `server/.trae/documents/war-gameplay-fix-plan.md` | §2 交战类型判定（成局判据=到达敌占城+敌 AT） |
