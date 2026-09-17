# hagcp 官方数据快照 vs 本服 WarDatabase diff 报告

- 本服表目录：`D:\HnG-test\combat-server\WarDatabase`（与 wardb.py/HTTP 下发同源）
- hagcp 快照：`E:\devProject\Heroes-and-Generals-community-project\packages\hagcp-assets\json`（社区抓取，与最终版客户端存在时代差——**结果只作参考依据，不自动落库**）
- 字段映射：hagcp `HnG_States.proto` 表 message 定义

## commandnodetemplate（AT 模板：速度/交战半径/升级树/战争资金加成）

- 行数：本服 **69** / hagcp **69** / 共同 id **69**
- 字段级：**8 个字段全部一致**（共同 69 行）

## armyresourcecategory（资源类别：avgSize/速度/捕获转移系数——automission 加权结算的候选依据）

- 行数：本服 **18** / hagcp **18** / 共同 id **18**
- 表级聚合（共同 id 口径，仅计非空行）：
  | 字段 | 本服(和/行数) | hagcp(和/行数) | 一致 |
  |---|---|---|---|
  | avgSize | 480/18 | 480/18 | ✓ |
  | captureTransferMultiplierWar | 5.4/18 | 5.4/18 | ✓ |
  | captureTransferMultiplierStaged | 1.8/18 | 1.8/18 | ✓ |
- 字段级：**5 个字段全部一致**（共同 18 行）

## capital（首都：胜利点数——对照战争胜利条件）

- 行数：本服 **13** / hagcp **23** / 共同 id **4**
- 仅本服有 9 行：`[44104485, 67670344, ...…`
- 仅 hagcp 有 19 行：`[3, 4, 5, 7, 8, 9, 10...…`
- 表级聚合（共同 id 口径，仅计非空行）：
  | 字段 | 本服(和/行数) | hagcp(和/行数) | 一致 |
  |---|---|---|---|
  | victoryPoints 总和 | 4/4 | 4/4 | ✓ |
- 字段级：1/3 个字段存在差异（共 3 行次）
  | 字段(f#) | 差异数 | 样例 id: 本服 vs hagcp |
  |---|---|---|
  | defaultFactionTemplateId(f3) | 3 | 1: 1 vs 0；6: 2 vs 0；14: 3 vs 2 |

## Pricing（经济数值抽查：三币种价格）

- 行数：本服 **7151** / hagcp **7132** / 共同 id **7132**
- 仅本服有 19 行：`[68100045, 68100046, ...…`
- 字段级：2/5 个字段存在差异（共 2 行次）
  | 字段(f#) | 差异数 | 样例 id: 本服 vs hagcp |
  |---|---|---|
  | costGold(f4) | 1 | 69021000: 2850.0 vs 3.0 |
  | costCredits(f6) | 1 | 69021000: 0.0 vs 9.0 |


---

## 结论与三分类

> 前提：hagcp 快照是社区在正式服运营期抓取的，与本服 WarDatabase（最终版客户端配套）存在时代差。**结论只作参考依据，不自动落库。**

### 采纳（作为参考依据）

- **commandnodetemplate**：69/69 行、8 字段全部一致——本服 AT 模板数值与官方终版吻合，无需任何动作；后续调 AT 数值时 hagcp 快照可直接作对照基准。
- **armyresourcecategory**：18/18 行、5 字段全部一致，含**捕获转移系数**（captureTransferMultiplierWar 聚合 5.4、Staged 聚合 1.8）——**automission 从"资源总量比较"升级为按类别加权（avgSize×数量）结算的证据前提成立**，P4-3 可启动。

### 不采纳（快照时代差，保留本服数据）

- **capital 行集**：共同 id 仅 4 行。本服 id 为大号哈希、hagcp 为小序号（3/4/5/7…），两侧行集分属不同时代的内容版本；强行对齐没有意义。共同行 victoryPoints 总和一致，说明数值口径本身无漂移。
- **Pricing 仅本服有的 19 行**（68100045+）：终版新增条目，快照未覆盖，保留本服值。

### 需人工判断

- **capital defaultFactionTemplateId 3 行差异**（id 1: 1 vs 0；id 6: 2 vs 0；id 14: 3 vs 2）：本服值语义更明确（1/2/3 = US/GE/SU 阵营模板），hagcp 的 0 像旧版占位——倾向保留本服值，暂不动作。
- **Pricing id 69021000**：本服 costGold=2850 / costCredits=0 vs hagcp costGold=3 / costCredits=9。本服 2850 金是典型高阶物品定价，hagcp 的 3 金+9 信贷更像早期/占位数据——定性为快照时代差，倾向保留本服值；若后续真机发现该物品定价异常再单独复核。

### 汇总

四表字段级差异共 **5 行次**（float32 容差 0.0001、int32 补码折返口径），全部落在上表"需人工判断"与时代差解释内；**无一条指向本服数据错误**。
