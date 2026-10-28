# 坦克多槽位炮弹调查（2026-10-25）

> 实施状态：已于 2026-10-25 完成本地服务端修复与回归测试。下文“未修改”等说明为前期调查时的记录；最终行为及验证结果见文末。

## 结论

“弹药仓最多 3 槽”不等于“必须装备 3 槽”。玩家反馈的初始只装备一份默认炮弹，与官方单槽载弹规则和现有客户端空槽处理相符。之前反编译读取到的实例数量阈值本身正确，但将勋带页的购买状态条件推广为默认发放、装备或战斗有效性约束，属于解释过度。

这里的“一份”指 VehicleAmmo 实例/一种装备配置，不是战斗中只有一发炮弹。主炮与同轴机枪也属于不同弹药仓；不能把“主炮一份”解释为整辆坦克所有武器总共仅一行弹药。

## 官方规则（直接证据）

2022-01-25 官方 Steam 公告 Switch Faster and Punch Harder 明确列出：

| 已装备槽数 | 按装备顺序分配载弹量 |
| --- | --- |
| 1 | 100% |
| 2 | 60% / 40% |
| 3 | 50% / 30% / 20% |

来源：https://store.steampowered.com/news/posts/?appids=227940&enddate=1644830270&feed=steam_community_announcements
对应公告位于页面的 25 Jan, 2022 条目，发布者 Reto.Umbra。

这直接排除了“只有占满 3 槽才可正常使用”的理解。官方这篇公告未规定购买新车时数据库具体创建几行，因此“新车每仓只送一份”的精确发货语义仍应与原始新购车快照或实际回放核对；不能声称公告已证明原服务器建号实现。

## 反编译证据

以下路径相对 D:/HnG-test/work/hng-server-re/。

1. decompiled/swf-frontend/scripts/dk/reto/heroesandgenerals/view/window/progression/ProgressionOverviewWindowMediator.as:1061-1093
   - eWeaponAmmoMapping 的载具分支统计同角色、同载具、同 ammoTemplateId 的 VehicleAmmo 数量。
   - 数量 >= getVehicleWeaponSlotsForWeaponTemplate(...) 才 setAlreadyOwnedState()。
   - 不要求这些实例已装备：这里没有按 equippedAmmoSlotIndex 或 equippedVehicleAmmoStorageId 过滤。
   - 因此该条件是“该弹种的购买按钮何时显示已拥有”，不是“载具能否出战”或“初始送多少”。只有一份时仍显示可购买，本身不证明服务端数据错误。

2. decompiled/swf-frontend/scripts/dk/reto/heroesandgenerals/util/VehicleUtilities.as:472-491
   - 上述名字中的 WeaponSlots 实际返回 VehicleAmmoStorage.storageSlots；3 是容量上限相关的购买状态阈值。

3. decompiled/swf-frontend/scripts/dk/reto/heroesandgenerals/view/window/vehicles/VehicleModificationMediator.as:1153-1203
   - 先按 storageSlots 构造所有空槽，ammoTemplateId、ammoId 初始为 0。
   - 再以实际 VehicleAmmo 的仓号与槽号填入已有弹药；无匹配实例的槽继续为空。
   - :349-373 支持 doEquip=false 的卸装请求。

4. decompiled/swf-frontend/scripts/dk/reto/heroesandgenerals/view/window/vehicles/VehicleAmmoStorageItemRenderer.as:155-174
   - ammoTemplateId=0 有明确空槽渲染分支。

5. decompiled/swf-frontend/scripts/dk/reto/heroesandgenerals/view/window/vehicles/VehicleModificationMediator.as:991-1012
   - 车库弹药列表只要存在一份同模板实例，就不再额外加入该弹种的待购买占位项。
   - 这与勋带页“达到仓槽数才显示已拥有”使用不同的 UI 判定，再次说明不能把某个页面状态当成全局规则。

## 当前服务端为何出现 3 份

- server/handlers/vehicles_purchase.py:54-93 grant_default_vehicle_ammo：每个默认仓循环 1..storageSlots，创建并装备一份默认弹药到每槽。其 docstring 明确写的是与“一次购买发满一仓”策略一致。这是本地服务端实现策略，不是反编译出来的原始发放代码。
- 此公共函数由新车购买、新兵初始载具、礼包发货共用，修正时应覆盖全部调用入口。
- server/player_service.py:800-859：推送角色载具数据时，将每个未占用槽自动补默认弹药，优先复用库存，再新建。因此只修改新车赠送还不够，后续加载依然会补满，玩家主动留空也无法持久保留。
- server/handlers/vehicles_purchase.py:480 附近：弹药购买一次创建 vehicle_ammo_purchase_count(...) 份，仅扣一次款。
- server/wardb.py:457-486：注释明确说明数量按仓槽数发满是为了保证勋带页显示已拥有。这一动机不足以证明原版一次购买应发 3 份。
- server/warserver_messages.py:476-491：当前战斗下发仅枚举实际已装备行，没有在这里要求凑满 storageSlots；spawnClipCount 则独立从仓表 clipCount 取值。不能把实例份数、槽数、战斗发数混为一谈。

## 建议修正方向（本次未修改业务代码和玩家数据）

1. 初始默认配置：每个需要默认弹药的仓创建/装备一份；主炮和同轴等不同武器仓分别处理，共享仓去重。
2. 移除“所有空槽都补默认弹药”的加载副作用。旧账号兜底需区分真正缺少初始配置与玩家主动卸装/留空，不能见空就补。
3. 允许 1、2、3 个有效装备槽持久保存；槽位顺序影响载弹比例。
4. 单独核查弹药购买的原始发货语义。在没有原版快照前，不应仅为了让勋带页变为已拥有就一次发 3 份。一次购买一份符合逐份计数的解释，但目前是推断，不是已取得原服务器证据的事实。
5. 既有玩家多余实例需单独迁移设计：自动赠送与真实购买实例没有可靠来源标识时，不宜直接按同模板重复行删除。

## 验证边界

本次完成官方公告与现有 AS3 反编译、当前 Python 服务端的静态交叉核查；未启动游戏实测，也未新运行 player.dll 原生反编译。100% / 60-40% / 50-30-20% 为官方规则证据，本次未独立验证当前二进制内的取整和稀疏槽处理。若实施修复，应实测新车初始单槽、重登留空、双槽和三槽的载弹分配，以及同轴武器弹药独立性。


## 追加：勋带解锁完整链路核查

### A. 勋带树进度和购买权限是两路输入

- frontend 的 ProgressionOverviewWindowMediator.as:780-829 从 CharacterTable.getCharacterStats(...).attributes 中取勋带经验，填充可见条目。
- frontend/scripts/layout/HnGGUIProgressionOverviewDisplay/HnGGUIProgressionBranch.as:471-489 用 level <= currentLevel 设置等级 ownedByCharacter 和条目 outlineColor。这里只体现勋带等级进度，不统计弹药数量。
- 真正选中物品后的购买权限来自另一条路径：ProgressionOverviewWindowMediator.as:980 调用 UnlockProgressionUtilities.hasCharacterUnlockedItem(...)。

### B. 真正解锁来源为 character.unlockBits

- proto-as3-classes/scripts/HnG_States/character.as:81 定义 field 21 为 repeated bool unlockBits。
- frontend/.../model/schema/CharacterSchema.as:103-134 遍历 unlockBits，下标 i 为 progressionUnlock.id，将置位的条目组成已解锁列表。它不会在这个函数里按勋带 XP 推导解锁，也不统计 VehicleAmmo。
- frontend/.../model/table/ProgressionUnlockTable.as:62-110 按 available、unlockable、itemType、itemId、阵营等取相关需求。
- frontend/.../util/UnlockProgressionUtilities.as:74-104：没有相关需求直接 true；存在需求时，只要角色已解锁列表命中任一相关需求 ID，且角色类型相符或需求角色类型为 0，即返回 true（炮弹分支不涉及指挥点的特例）。这里是任一命中，而不是所有需求都要满足。
- 炮弹勋带条目的 itemId 指 weaponAmmoMapping.id，不是 ammoTemplateId；最终发放 VehicleAmmo 的模板则是 mapping.ammoTemplateId。发错种类的 ID 会出现权限与库存不一致。

### C. 对应载具和已拥有判定

- frontend/.../util/UnlockProgressionUtilities.as:560-639：炮弹需要匹配父级物品。经 weaponAmmoMapping.weaponTemplateId -> 驾驶员主武器槽，找到角色所持且基础模板匹配的载具实例。
- frontend/.../view/window/progression/ProgressionOverviewWindowMediator.as:1061-1093：先确认已解锁，随后统计同角色、同 ammoTemplateId、同匹配 vehicleId 的 VehicleAmmo。数量 >= storageSlots 才显示已拥有，装备状态不参与。
- :1212-1216：有价格时，购买区域 enabled = isItemUnlocked && ownsRequiredItem；锁图标条件为 !isItemUnlocked && priceComponent.visible。未拥有载具也能导致购买禁用，不能只凭按钮灰色判断缺解锁位。
- :1251-1264：setAlreadyOwnedState 隐藏价格，显示 alreadyOwned 文案；不是给角色设置解锁权限。

### D. 刷新路径

- :227-249 订阅 VehicleAmmoTable.changeSignal。
- :292-298 仅在 action == TableBase.ADD 时对当前选中项重新 unlockSelected；UPDATE、REMOVE 在此监听器内直接返回。
- :555-584 购买回包成功只恢复按钮并发送 purchaseSignal；不会直接将已拥有设为 true，也不会直接设置 unlockBits。库存需通过服务端表变更推送刷新。
- CharacterModel.as:123-127、184-187、276-283 比较角色 unlockBits，变化时发送 characterUnlocksUpdatedSignal。
- ProgressionOverviewWindowMediator.as:619-674 收到角色统计更新后播放 XP 动画，结束后重新 unlockSelected。故服务端仅更新 XP 但未同步角色解锁位，会出现等级显示达标而选中条目仍锁定；这是由两条输入链路推导出的可发生情形，本次没有玩家会话现场验证。

### E. 当前服务端静态发现的缺口（不能等同于当年故障根因）

server/player_service.py:282-420 的 _char_unlock_bits_for 会从勋带等级生成解锁位，也会为已拥有的步兵武器弹药补回映射解锁位（:401-407）。但该函数未读取 list_vehicle_ammo，也没有对已拥有载具的默认武器弹药进行与步兵相同的补位。拥有载具只补载具/变体的解锁位（:408-414）。因此“已拥有载具炮弹”不会经这个库存兜底路径自动补上炮弹映射权限；是否实际锁定仍取决于对应 mapping 是否另有勋带解锁位或根本无需求。

这解释了为什么需要检查具体角色、具体炮弹 mapping 的 unlockBits 和 XP 推送，而不能把 3 份库存当作权限修复。数量达到 3 的已拥有判定在 !isItemUnlocked 时根本不会执行，单纯发 3 份不会让 true/false 解锁条件发生变化。

本次追加为静态代码核查，未修改客户端、服务端业务或玩家数据。


## 修复实施与验证（2026-10-25）

已完成以下业务修改：

- 买车、招募初始载具、礼包载具共用发放函数：每个独立默认仓仅创建一份，装备槽 1；共享仓去重。
- SOLDIER_VIEW 回放不再创建或自动装备载具弹药；包括全空、稀疏槽、未装备库存、历史多份默认弹，都保留原状态。
- 移除按 storageSlots 倍增弹药发货的 helper；一次购买创建一份未装备实例。
- 角色位图为本人持有载具的默认弹映射补权限；本人该载具上已持有且兼容的弹药也补回映射权限。其他士兵、仓库无绑定行、不兼容模板不参与补位。
- 弹药购买前检查兼容 weaponAmmoMapping 的角色解锁权限（任一符合条件的需求命中即可）；未达标不扣款、不发货。
- 买车默认弹、单独购买弹药的推送顺序包含角色权限刷新，先于新增弹药 ADD。TableBase.setObject 对已有对象执行 copyFromObject，原界面引用可见更新后的字段。
- 保留客户端原有“同角色同车同弹种实例数达到 storageSlots 才显示已拥有”的逻辑；不足时仍可补购，不表示未解锁。未改 SWF。
- 未删除历史多余实例，未改玩家数据库，未重启或部署运行服务。

测试：

- 新增 tests/test_vehicle_ammo_slots.py，共 40 个 pytest 用例：4 个初始发放入口、共享/独立仓、1/2/3 份与装备无关的权限、XP 达标、其他角色/仓库/不兼容行隔离、OR 解锁、阵营/兵种过滤、扣款前拒绝、连续逐份购买、权限与库存推送顺序、余额不足、落库失败退款、重登保留空槽/全部卸装/旧多份库存、战斗仅下发已装备行及 spawnClipCount 不变，以及真实 WarDatabase 的三槽仓发放。
- 相关 8 个测试文件合计 100 passed。
- 独立运行旧式脚本：默认迷彩 PASS=25、购买修复 PASS=23、通行证外观 PASS=49、礼包发货 PASS=52，均 FAIL=0。这些检查与 pytest 用例有重叠，不应简单相加为独立用例总数。
- 礼包测试额外修复：失败检查改为抛 AssertionError（此前仅打印导致 pytest 假通过）；更新角色归属和真实价格测试替身；按 Chunk 正式解析响应，避免把后续 KVCS 字节误认为结果。
- git diff --check 通过。
- 全量 pytest tests 的收集被既有 tests/test_battlefield_stagedpos.py 在导入时 sys.exit(0) 中断；不能声称全量通过。本次相关测试已显式选取并通过。
- 未进行启动客户端后的交互实测；战斗测试验证服务端协议下发，未重新验证原生客户端比例取整。

复现相关测试命令（在 server 目录）：

```text
..\.venv\Scripts\python.exe -m pytest -q tests/test_vehicle_ammo_slots.py tests/test_vehicle_purchase_pricing.py tests/test_vehicle_damage_gate.py tests/test_default_camo_unlock.py tests/test_battlepass_skin_unlock.py tests/test_storebundle_delivery.py tests/test_purchase_fixes.py tests/test_vehicle_maintenance_regression.py
```
