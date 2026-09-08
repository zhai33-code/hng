# HnG 缴获武器（Captured Weapon）功能调研报告

> 调研日期：2026-09-08
> 数据来源：客户端反编译（swf_unpacked/as3_desktop_full）+ WarDatabase 静态表 + hng-re 服务端源码
> 结论前置：**缴获武器不是战斗掉落物，而是商店商品——敌对阵营武器的 "Used"（二手）变体，由 `WeaponVariantTemplate.weaponVariantType == 2` 标识。官方对"枪械"类缴获购买线做了数据级下架（45 把枪跨阵营行 available=0），只保留爆破物/近战类（23 把）；勋带列表默认隐藏但 XP 一入账即现身。私服核心链路已支持。web 管理端动态开关可行：服务端闸门走现成 server_config 体系保存即生效；客户端呈现层需动态改 WarDatabase 表下发（哈希三通道同源，重启客户端生效）。**

---

## 1. 机制本质

- 玩家在战场上捡起敌人掉落的武器杀敌（引擎行为，不持久化）
- 击杀统计按实际武器 id 上报 → 升该武器的 Specialist 专家勋带（ribbonCategory=7）
- 勋带到门槛级后，武器商店解锁购买该敌军武器的 **Used 版**（独立折扣价）
- 永久拥有，可装备给己方（异阵营）士兵

### 1.1 数据形态（WarDatabase 实值）

| 表 | 关键数据 |
|---|---|
| `WeaponVariantTemplate` | 438 个变体，其中 **90 个 weaponVariantType=2（Used）**，覆盖 Gewehr 43、MP 40、Luger P8、StG 44 等；Used 变体独占 `textureIndex == 1`；全部 `available=1` |
| `Pricing` | Used 变体有独立价格行（如 Gewehr 43 Used pricingId=4936 = 280 credits，与本体同价；Bazooka M1A1 Used = 10 credits） |
| `progressionUnlock` | 跨阵营行：faction=我方、unlockType=eRibbon(1)、metaName=武器 Specialist 勋带 id、metaLevel=门槛级 |
| `ribbontemplate` | 42 个 Specialist 勋带全部 available=1；`characterClassRequirement1/2/3`（f7/f8/f9）按武器所属阵营独占 |

变体类型枚举（本地化 `weapontemplate/weaponvarianttype/*Title`）：1=Factory 出厂、**2=Used 二手/缴获**、3=Dark Walnut、4=Beech 等纯皮肤。

UI 仓库标签文本：`weaponvarianttemplate/114Title` = "Captured"（中文 XML 误译"已占领"，实为"缴获"）。

---

## 2. 客户端完整闭环（反编译实锤）

```
战斗中捡起敌军掉落武器杀敌（引擎行为，不持久）
   ↓ 击杀统计按实际武器 id 上报（statistics type-6 武器段）
升该武器的 Specialist 勋带（ribbonCategory=7）
   ↓ 勋带到 9 级（progressionUnlock 跨阵营行 metaLevel）
武器商店解锁购买该敌军武器的 Used 版（折扣价）
   ↓ CreateWeaponVariantRequest（weaponVariantTemplateId=Used 变体 id）
永久拥有，可装备给异阵营（己方）士兵
```

### 2.1 四个关键判定点

**① 解锁门槛（勋带路径图标）**：[ProgressionOverviewWindowMediator.as:795-813](../swf_unpacked/as3_desktop_full/scripts/dk/reto/heroesandgenerals/view/window/progression/ProgressionOverviewWindowMediator.as)

```actionscript
if(_loc4_.available)              // 闸1
   if(_loc4_.unlockable)          // 闸2
      if(_loc4_.unlockMetaLevel != 0)   // 闸3：level 0 不上路径
```

**② 商店陈列过滤**：[UnlockProgressionUtilities.removeCapturedLockedWeapons:512](../swf_unpacked/as3_desktop_full/scripts/dk/reto/heroesandgenerals/util/UnlockProgressionUtilities.as)——异阵营武器仅当 `isCrossFactionWeaponUnlocked`（Specialist 行已解锁）才出现在商店。

**③ 变体列表过滤**：[WeaponUtilities.getVariantsForEquipmentSortedArrayCollection:626-648](../swf_unpacked/as3_desktop_full/scripts/dk/reto/heroesandgenerals/util/WeaponUtilities.as)

```actionscript
if(!(param2.Equals(param3) && _loc6_.textureIndex == 1))          // 同阵营：排除 Used
   if(!(param2.Equals(param3) == false && _loc6_.textureIndex != 1 && param2.isZero == false))
                                                                    // 异阵营：只显示 Used
```

同阵营武器只显示皮肤变体；**异阵营武器只显示 Used 变体（textureIndex==1）**。

**④ 装备阵营规则**：[setLockStateExeptionsBasedOnCapturedStatus:38-65](../swf_unpacked/as3_desktop_full/scripts/dk/reto/heroesandgenerals/util/ObjectActionExecuter/depotObjectActions/setLockStateExeptionsBasedOnCapturedStatus.as)

- Used 武器（`weaponVariantType==2`）：只能给**异阵营**士兵（同阵营时 `isUnlocked=canBeUnlocked=false`）
- 普通武器：只能给同阵营士兵（异阵营时锁死）
- 判定入口：仓库对象 `EquippableItemObject.isCaptured`（DepotItemRenderer 显示 Captured 标签）

### 2.2 购买请求链路

[UnlockWeaponWindowMediator.purchaseClickHandler:224-252](../swf_unpacked/as3_desktop_full/scripts/dk/reto/heroesandgenerals/view/window/progression/UnlockWeaponWindowMediator.as) / [WeaponShopWindowMediator:692](../swf_unpacked/as3_desktop_full/scripts/dk/reto/heroesandgenerals/view/window/weapons/WeaponShopWindowMediator.as)：

```
CreateWeaponVariantRequest {
  characterId=1; weaponId=2; weaponTemplateId=3; weaponName=4;
  weaponPricingId=5; weaponVariantTemplateId=6; weaponDiscountId=7;
  variantPricingId=8; variantDiscountId=9; paymentCurrency=10 }
```

异阵营购买时武器本体价与皮肤变体价**合并展示**（[setupPrices:620-654](../swf_unpacked/as3_desktop_full/scripts/dk/reto/heroesandgenerals/view/window/weapons/WeaponShopWindowMediator.as)：异阵营时把 Used 变体 pricingId 作为 `_loc4_` 传给 PriceModel）。

### 2.3 辅助规则（本地化文本实锤）

- `CharacterScreen/CombatRibbonAndBadge/desc_upgrades_for_captured_weapons`：缴获武器的改装/弹药需先解锁该武器才能购买
- 将军晋升警告：缴获武器 ONLY 转移到 Equipment Depot，且仅对捕获来源同阵营士兵可用（该描述与 setLockState 实现有出入，以代码为准：Used 给异阵营）
- StoreBundle 礼包 79 个中 **0 个**含 Used 变体——只能从武器商店/勋带弹窗购买

---

## 3. 开关体系（三道数据闸，无运行时开关）

**ZOption/协议层开关：不存在**。groups_true.json 全参数组无缴获相关项。

### 3.1 `WeaponVariantTemplate.available`（f5）— 变体级

90 个 Used 变体全部 available=1（全开），此层不构成实际限制。

### 3.2 `progressionUnlock.available`（f10）/ `unlockable`（f11）— 跨阵营解锁行级（**真正生效**）

| 类别 | 武器数 | 状态 | 例子 |
|---|---|---|---|
| 爆破物/近战/反坦克 | 23 把 | ✅ available=1 + unlockable=1 | Bazooka M1A1、Panzerschreck RPzB 54、PTRD 1941、RPG-43、S-Mine、Tellermine 42、工兵铲、军刀 |
| 枪械（步枪/冲锋枪/手枪/机枪） | 45 把 | ❌ available=0 + unlockable=0 | K98、MP40、Gewehr 43、M1 Garand、Mosin-Nagant、PPSh-41、Thompson、1911、C96 |

**23 把可购清单**（owner=所属阵营，可购=能买的阵营，等级=Specialist 需求级）：

| 武器 | owner | 可购阵营 | 等级 |
|---|---|---|---|
| Combat Knife 1219C2 | US | GE/SU | 9 |
| Seitengewehr 98 | GE | US/SU | 11 |
| M-1910 Spade | US | GE/SU | 2,4 |
| Schanzzeug Spade | GE | US/SU | 2,3 |
| PMK-40 | SU | US/GE | 11 |
| TM-35 | SU | US/GE | 3 |
| NR-40 'Scouts knife' | SU | US/GE | 10 |
| Vulcan Stalin | SU | US/GE | 2,4 |
| PTRD 1941 | SU | US | 8 |
| RPG-43 | SU | US/GE | 6 |
| Bazooka M1A1 | US | GE/SU | 4,9 |
| Panzerschreck RPzB 54 | GE | US/SU | 12 |
| Panzerwurfmine Lang | GE | US/SU | 6 |
| S-Mine | GE | US/SU | 11 |
| M2A3 | US | GE/SU | 11 |
| Tellermine 42 | GE | US/SU | 3 |
| M1A1 | US | GE/SU | 3 |
| Hafthohlladung H3 | GE | US/SU | 9 |
| S.T. Grenade No.74 | US | GE/SU | 6 |
| Bazooka M9A1 | US | GE/SU | 10 |
| GranatBuchse 39 | GE | US/SU | 10 |
| Ampulomet-41 | SU | US/GE | 10 |
| M18 Recoilless Rifle | US | GE/SU | 12 |

规律：`available=0` 全部是跨阵营行（本阵营行全开）——官服后期明确下架了枪械的缴获购买线。

客户端消费点：
- [canCharacterUseItem:105-134](../swf_unpacked/as3_desktop_full/scripts/dk/reto/heroesandgenerals/util/UnlockProgressionUtilities.as) 的 `useable` 判定跳过 available=false → 武器商店价格按钮禁用
- 勋带路径三道闸（见 2.1①）
- 注意：**位图授予与 hasCharacterUnlockedItem 都不看 available**，UnlockWeaponWindowMediator 勋带弹窗购买路径也不查 useable

### 3.3 `ribbontemplate.characterClassRequirement1/2/3`（f7/f8/f9）— 勋带列表显隐级

Specialist 勋带的位掩码按武器所属阵营独占（如 Gewehr 43 Specialist：req1(US)=0、req2(GE)=5、req3(SU)=0）。

士兵勋带列表显示条件（[ProgressionModel.setupProgressionObjects:174-210](../swf_unpacked/as3_desktop_full/scripts/dk/reto/heroesandgenerals/components/progression/model/ProgressionModel.as)，三重 OR）：

```actionscript
CheckRibbonRequirements(bit, char, ribbon)   // 阵营位掩码匹配
|| ribbon.progression > 0                     // 已有进度
|| ribbon.level > 0                           // 已有等级
```

**结论：美军士兵默认看不到 Gewehr 43 Specialist（掩码 0），但一旦用缴获的 G43 杀敌攒了 XP（progression>0），勋带立刻现身。** 这是官服"自证式"设计——三道闸各管一层：

| 层 | 闸 | 效果 |
|---|---|---|
| 勋带列表 | characterClassRequirement 位掩码 | 默认隐藏，XP 入账即现身 |
| 勋带路径购买行 | available=0（41 把枪） | 点亮了也买不了 |
| 变体/商店 | Used 变体全开 | 入口被上面两道闸锁死 |

---

## 4. 服务端现状（hng-re）

### 4.1 已实现 ✅

| 环节 | 位置 | 说明 |
|---|---|---|
| 购买处理 | `handlers/weapons_purchase.py:73` handle_createweaponvariantrequest | 新武器带变体：create_weapon(variant_template_id) + create_weapon_variant + set_weapon_variant，KVCS 推送齐全；纯皮肤购买分支（weaponId>0 且 templateId=0）不创建新武器 |
| 双价格原子扣款 | `handlers/weapons_purchase.py:136` `_charge_player_multi([weapon_pricing, variant_pricing])` | 余额不足整体不扣 |
| 解锁校验 | `player_service.py:356` _unlock_need_for_char | 阵营过滤后异阵营 Specialist 行按勋带等级放行；`_REQ_UNLOCK_BY_ITEM` 剔除 level=0 专属勋带条目 |
| 数据表 | `store/schema.py:107` | weapons.variant_template_id / weapon_variants 表 |
| 勋带 XP 链路 | combat.py 结算 → set_ribbon_xp_floor → character_ribbon_xp | 引擎按实际武器上报，跨阵营击杀照样入账 |

### 4.2 缺口 ⚠️（按风险排序）

1. **装备/购买无服务端阵营 gate**：客户端锁 UI 但协议层可伪造——① 买异阵营普通（Factory）武器不带变体：对方阵营解锁行被过滤 → need 为空 → **放行**；② Used 武器装备给同阵营士兵、普通武器给异阵营士兵，服务端装备链路均不拦截
2. **Used 武器无二手磨损**：官服二手货应初始 condition < 1.0，当前 create_weapon 落列默认值/兜底 1.0（全新）
3. （待 VPS 验证）战斗 spawn 武器皮肤 f10 链路（b44f8ee）是否正确渲染 Used 变体 textureIndex=1 外观

### 4.3 私服比官服宽松的部分

官服关闭的 45 把枪，私服上勋带弹窗路径（UnlockWeaponWindowMediator，不查 useable/available）+ 服务端校验（不滤 available）**照样能买**。武器商店按钮虽禁用，但勋带 9 级弹出的解锁购买窗可走通。

---

## 5. web 管理端动态开关方案

### 5.1 现成基础设施（零新增架构）

| 组件 | 现状 |
|---|---|
| 配置存储 | `server_config` 表（store/schema.py:816）：key/value JSONB + 保存人审计；注释明确"web 管理端可写，游戏服运行时读取，低频路径直接读库，保存即生效" |
| 管理端后端 | web/server/services/config_svc.py 的 `CONFIG_REGISTRY` 模式：新增配置项 = 注册 label + pydantic 校验模型，路由层不动 |
| 管理端前端 | ConfigView 已有 tab 结构（"战局天气"/"战斗服主机"），加一个 tab |
| 游戏服消费先例 | mm_weather.py / combat_hosts.py 均为成局/部署时读库——购买与解锁位计算同属低频路径，照搬 |

开关形态建议三态：`official`（默认，按官服 45 关/23 开）/ `all_on`（全放开）/ `all_off`（全关）。

### 5.2 两层能力边界

| 层 | 挂点 | 生效方式 | 边界 |
|---|---|---|---|
| ① 服务端购买闸门 | weapons_purchase.py:112 购买校验 + player_service.py:164 `_char_unlock_bits_for` 解锁位授予，按开关过滤 `_REQ_UNLOCK_BY_ITEM` 跨阵营行 | 保存即生效 | 关=彻底关死（勋带弹窗路径也过不了服务端校验）；开=服务端放行，但 45 把枪客户端 UI 购买按钮仍灰（本地表 available=0），仅 23 把爆破/近战有完整 UI 入口 |
| ② 客户端呈现 | `/WarDatabase/progressionUnlock` 下发字节动态重编码（§5.3） | 玩家重启客户端后生效 | 放开 45 把枪的商店按钮/勋带路径图标 |

### 5.3 数据流：改表链路（客户端呈现层）

**改的是 `HnG/WarDatabase/progressionUnlock` 表文件内容**（KVCS 二进制，159KB），服务端其余环节是哑管道。

现状数据流（双通道，全部从同一磁盘文件出发）：

```
① SWF（游戏内数据表）
   登录 → GET /wardb_unique → 对比本地缓存哈希
        → 有变化 → GET /WarDatabase/progressionUnlock 重新拉表

② hngsync.exe（文件同步，登录前）
   GET /wardb_sync → 逐文件对比本地 sha256/size
        → 不符 → GET /<sha256>-<size>_0（CAS 名）下载本体覆盖本地
```

服务端三份清单全部由 web_server 启动时从文件字节现算（wardb.py:930-980 `_scan_wardb_entries`）：

| 产物 | 消费方 | 格式 |
|---|---|---|
| `WARDB_UNIQUE` | SWF 登录链 | 2 列 `"表名",<sha256>-<size>` |
| `WARDB_SYNC_UNIQUE` | hngsync（desktop_client set） | 3 列 wire 格式，列 2 为完整 CAS 名 |
| `WARDB_CAS_MAP` | hngsync 本体下载回源 | CAS 名 `<sha256>-<size>_0` → 文件路径 |

**关键性质：表字节与三份清单自动同源**。改完表文件重启 web_server，三条通道哈希自动一致（wardb.py:978 注释原话："改动过的表无需手工复制进 cas 目录，重启服务器即可"）。硬约束（不可违反）：四者（表字节/wardb_unique/wardb_sync/CAS）必须同源，错一个 = SHA256 不匹配 → 客户端 restartAir 崩溃循环（已知硬约束）。

改写技术路径：
1. KVCS 重编码：用 wardb.py 现成的 KVCS 解析器读 progressionUnlock → 跨阵营行 f10(available)/f11(unlockable) 翻 true → 重编码（需补一个 KVCS 编码器，解析已有、格式对称可逆）
2. 重启 web_server（或触发热重扫）
3. 玩家下次启动客户端：hngsync 哈希不符自动下载新表 + SWF 重新拉表 → 勋带图标/商店按钮放开

### 5.4 实现路线二选一

| 路线 | 做法 | 优点 | 代价 |
|---|---|---|---|
| A：落盘 + 重扫 | 管理端保存开关 → 按开关重编码落盘（替换 `HnG/WarDatabase/progressionUnlock`）→ 通知 web_server 重扫清单（或重启）。三态字节各自缓存，来回切只是换缓存 | 最省心，`/WarDatabase/` 路由与 CAS 回源零改动 | 磁盘文件非官方原版（留备份可回滚）；切换需重扫/重启 |
| B：路由层内存缓存 | 磁盘文件永远保持官方原版；`/WarDatabase/progressionUnlock`、`/wardb_unique`、`/wardb_sync`、CAS 四路由统一从"按开关重编码的内存字节"出发 | 回滚零成本（official 态=直读文件） | 四个路由都要挂同一份缓存字节，一致性维护点更多 |

逻辑等价，运维偏好选择；磁盘方案（A）更简单。

### 5.5 服务端阵营 gate（防伪造，与开关正交）

weapons_purchase 购买校验 + 装备链路增加两条规则：
- 买异阵营武器（weaponTemplate.factiontemplateid != 角色 faction）必须带 Used 变体（variantTemplate.weaponVariantType==2）
- Used 武器只准装备给异阵营士兵；普通武器只准给同阵营

### 5.6 二手磨损（还原度）

create_weapon 按 variantTemplate.weaponVariantType==2 时初始 condition 取低于 1.0 的值（官服具体数值未考据，可自定如 0.85）。

---

## 6. 待验证（VPS）

1. 捡德军枪杀敌 → 美军士兵 Gewehr 43 Specialist 勋带是否入账并现身（整条链路能否闭环的关键）
2. 德军号看 Tellermine 42（3 级）/ Bazooka M1A1（4/9 级）勋带路径是否显示缴获图标（验证 23 把开放线）
3. 购买 Used 武器后战斗内外观是否为 Used 皮肤（textureIndex=1）
4. 缴获武器跨阵营装备在仓库/配装界面的 Captured 标签与阵营图标显示
5. （开关实施后）动态切换 official → all_on：hngsync 是否正常下载新表（不触发 restartAir 循环）、45 把枪商店按钮/勋带路径图标是否放开、已在线玩家重启客户端后是否生效
