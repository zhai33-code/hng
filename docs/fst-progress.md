# .fst 地图格式破译 —— 进度文档

> 更新：2026-09-19  
> 总目标：把 `D:\hng-map\Project` 的 Unity 地图项目编译成游戏可用的 `.fst`。  
> 近期目标：读取并回写 `.fst` 内预置沙袋（SandbagWall）的变换。  
> 方法红线：所有结论必须同时有 `player.dll` 反编译代码和官方数据双向验证，禁止猜测。

## 当前结论

### 1. `.fst` 外层封装（已闭环）

磁盘文件为 `"FST\0" + zlib_stream`。zlib 解压结果为：

```text
[MD5 16B][payload_length ULEB128][payload]
```

- 15/15 个官方 `.fst` 均满足 `payload_length == 实际剩余长度`。
- 15/15 均满足 `MD5(payload) == 前 16B`。
- 当前样本的长度 varint 都占 4B，因此 payload 从解压流 `0x14` 开始。
- 旧文档把 `[0:16]` 当随机值、把 `[16:20]` 当未知字段的说法已经作废。

### 2. `.fst` 不是普通 catalog section 容器（已纠正）

`FST_Save_OptimizedScene(0x1804B55C0)` 调用 `sub_18050AA20(..., mode=2, ..., "FST", 1)`。
基类 `CatalogOutputStreams_Finalize(0x180509D30)` 只在 `mode == 0` 时调用
`Catalog_WriteSections(0x18050E340)`；FST 使用的 `mode == 2` 不写普通 catalog 目录。
最终由 `ZPerObjectFileCatalogOutputStreams_Finalize(0x18050AAD0)` 写专用对象流。

因此旧分析中所谓顶层 “manifest / cluster / 属性块” 实际是对象缓存 payload，
不是 `.fst` 顶层 section。普通 catalog 的读写格式仍然成立，但不能套在 FST 顶层：

```text
[u32 section_end_or_size][u32 entry_count]
entry_count * [CString dir][CString name][u32 cumulative_offset][u32 length]
[payloads...]
```

### 3. ObjectStreamV2 头部（已闭环）

payload 开头：

```text
[schema_digest 16B]
[negative_group_count ULEB128]
[feature_value ULEB128]
group_count * {
    [schema_id ULEB128]
    [record_count ULEB128]
    record_count * [digest_A 16B][value ULEB128][digest_B 16B]
}
```

- `ZStream_ReadVarint(0x180507430)` 是标准无符号 ULEB128，返回值再按 `int32` 判断负数。
- Mill：87 组、feature=33、51,255 条记录，组表结束于解压流 `0x19D306`。
- TankFactory：92 组、feature=33、115,400 条记录，结束于 `0x3A2298`。
- Skirmish：86 组、feature=33、30,917 条记录，结束于 `0x0F9440`。
- 旧分析把记录内的 `"SVC\0"` 当成头部字符串；实际上它位于第一个 16B digest 字段内。

### 4. RS03 schema（已闭环）

`_schemas/000417b244e3b07175a81ba446daace8.bin` 的格式：

```text
CString "RS03"
u32 name_count
name_count * CString
重复 class，空 CString 结束：
    CString class_name
    CString display_name
    u8 flags
    if flags & 1: u32 custom_value
    else: 重复 [i32 type][u8 flags][i32 name_index][i32 count]，i32(-32) 结束
```

文件可无剩余字节地解析出 751 个类。关键 ID：

| schema | ID | 已确认字段 |
|---|---:|---|
| `ZSceneNode` | 434 | parent、type、name、property sets、children、base |
| `ZPropertySet` | 438 | name、type、data block、base |
| `ZLocalKinematics` | 730 | 16 个 float32 + base |
| `ZKinematics` | 731 | `ZLocalKinematics local` + base |

`ZLocalKinematics` 的 16 个 float32 顺序：

```text
posx posy posz
quatx quaty quatz quatw
sclx scly sclz
nposx nposy nposz
nrotx nroty nrotz
```

### 5. 内联对象缓存（已闭环）

组表之后，缓存条目是 `[length ULEB128][payload]`；`0xFFFFFFFF` 为哨兵。
对有效条目计算标准 MD5，可与组记录的 `digest_A` 对上。

Mill 实测：

- 9238 个可验证的内联 payload；
- 293 个哨兵；
- 连续通用缓存扫描在解压流 `0x883866` 停止；这不是最终引用流边界，后面仍有专用缓存；
- schema 434 有 1999 个唯一 SceneNode payload；
- schema 438 有 1189 个唯一 PropertySet payload；
- `Kinematics/kine` 的唯一 payload digest 为 `80fdd166e5ba87b7be18604b33b4ea15`。

SceneNode payload 中已能可靠列出 SandbagWall 名称。

### 6. `ZKinematics` 变换本体（已闭环）

Mill 的 schema 731 共有 2365 条记录。已在解压流中找到 2365/2365 个对应变换：

- 每个本体为 64B，即上述 16 个 little-endian float32；
- `MD5(64B 本体) == digest_A`，2365/2365 全部通过；
- 每个本体前面的 `0x40` 是 64B payload 的 ULEB128 长度，不是基类引用；
- schema 731 是最后一个缓存组；最后一个 64B 本体结束后，最终对象引用流从 `0x8BEE10` 开始；
- Mill 首个本体在 `0x899594`；
- `digest_B == MD5(LE32(731) + digest_A)`，2365/2365 全部通过。

这与 `ObjStreamV2_WriteData(0x18050EDE0)` 对 `-18` 类型直接写 4B float、
以及 `sub_18050E610` 对 `schema_id + payload digest + dependency digests` 求 MD5 的代码一致。

### 7. Kinematics PropertySet → Transform（已闭环）

schema 438 中 `Kinematics/kine` payload 出现 2365 个上下文记录，`value == 2`。
对每个 schema 731 记录计算：

```text
MD5(LE32(438) + kinematics_property_digest + transform_digest_B)
```

所得值与 schema 438 的 `digest_B` 逐一对应，2365/2365 成功。
因此 PropertySet 到变换的映射已闭环，不依赖位置或浮点特征猜测。

同一验证已跑完 15/15 个官方 `.fst`：共定位 68,599 个 schema 731 变换，
并完成 68,599/68,599 个 Kinematics PropertySet 映射；没有样本例外。

### 8. 对象引用图与 SceneNode → Transform（已闭环）

`sub_180517620` 确认引用为 ULEB128：0 表示 null，非零值是扁平组记录表的一基索引。
Mill 最终引用流共有 172,099 个值、35 个根；按首次出现展开、重复 `digest_B` 只占一个值的 DFS
规则可无剩余地覆盖 51,255 条上下文记录。`ZSceneNode` 的直接边数与
`1 parent + property-set count + child count` 完全一致。

当前工具已闭环：

```text
SandbagWall 名称
  -> SceneNode 上下文 digest
  -> Kinematics PropertySet 上下文 digest
  -> ZKinematics digest_A
  -> 64B 变换及精确文件偏移
```

Mill 的 28,401 条 SceneNode 上下文记录都能绑定到实际变换。对单个已有实例的保守模板回写也已实现：

- 原位替换 64B `ZKinematics`；
- 重算 schema 731 `digest_A/digest_B`；
- 重算对应 Kinematics PropertySet 的 schema 438 `digest_B`；
- 保持记录索引和 DFS 引用稳定；
- 重算外层 payload MD5 并重新 zlib 压缩。

回归测试会实际移动一个 Mill 沙袋，写出临时 `.fst`，再从头解析并确认同一 SceneNode 已绑定到新坐标。
这证明“已有实例的变换模板编译”已形成字节级闭环；尚未宣称游戏运行时验收。增删实例见第 9 节。

### 9. 克隆实例插入（已闭环）

`insert_cloned_instance()` 可把一个已绑定的 SceneNode（如沙袋）克隆为新实例，
产出完整可解析的 `.fst` 字节；回归测试 `test_insert_cloned_instance_roundtrip` 实际
克隆一个 Mill 沙袋后从头解析，确认 transforms=2366、绑定数 +1、克隆变换精确生效、
模板不受影响。

克隆的核心机制（引用流为按首现展开的 DFS，重复 `digest_B` 占 1 槽）：

1. **子树 re-emit**：对模板子树按原流语义重新生成克隆序列。三个替换槽
   （scene-node / Kinematics PropertySet / 64B 变换）携带新 `digest_B`，
   在克隆中仍是首现、按各自新 span 展开；其余槽的首现都在插入点之前，
   全部坍缩为 1 槽 REPEAT（模板 span 27 的沙袋坍缩为 9 槽）。
2. **新记录 digest_B**（与第 6/7 节公式一致）：

   ```text
   transform_dB = MD5(LE32(731) + transform_dA)
   kine_dB      = MD5(LE32(438) + kine_dA + transform_dB)
   node_dB      = MD5(LE32(434) + node_dA + kine_dB)
   ```

3. **表内追加与索引重映射**：新记录追加到 434/438/731 各组尾部，中间组插入使
   后续所有组记录的扁平索引 +1，全流槽值必须过一遍 `index_map`。
4. **跨插入点祖先加长**：克隆插在模板子树之后，凡首现展开区间跨越插入点的
   记录（直至场景根）`value` 一律 `+len(clone)`；区间规则直接推导全部新 span，
   再做一次全流 DFS 结构校验。
5. 缓存条目按新记录顺序重放：两个新 payload（node/body）插入，kine 复用模板
   的 PropertySet payload（同 `(schema, digest_A)` 键去重）；最后重算外层 MD5
   并 zlib 压缩。

该闭环证明“增实例”不需要运行 counter——克隆的全部子依赖都是组表已有记录，
`digest_B` 直接由公式推导。

### 10. ObjectStreamV2 写端三阶段去重与 counter（反编译闭环）

`CatalogOutputStreams` Finalize（`sub_1805108A0`）分两阶段写引用流：

- **Phase 1（`sub_18050E610`）**：按对象**身份**（qword 指针）去重遍历依赖，
  维护 `*(state+428)` 访问计数器；环依赖时向 MD5 喂占位符 `LE32(k)+12 零字节`，
  `k` 即该对象的 Phase 1 访问序号。Phase 1 结束后 counter 在 Phase 2 前被清零。
- **Phase 2（`sub_18050E9E0`）**：按 `digest_B` **内容**去重。dA 树命中已有记录则
  直接引用；payload 树命中则建记录（节点存 pre-increment counter 再自增）并递归；
  dA 树查不到（ghost，仅 Phase 1 访问过、无独立记录）时写 `{-32,0,0}` 空槽即
  null 引用，**不建记录、不耗 Phase 2 counter**。
- **组表**再按 `(schema, digest_A)` 去重。

由此解释官方数据中的两个现象：

- 内容相同、身份不同的子树在 Phase 1 各耗 counter，Phase 2 折叠成同一记录
  （引用流中 span=1），导致 counter 差值随流位置递增（实测 48 条单环样本
  k−V1 差 1856→18021 单调增长）——counter 无法从引用流反推，只能由写入顺序决定。
- null 槽对 `digest_B` 的贡献内容依赖（正常为 `MD5(LE32(s_X)+16 零字节)`，
  但可被更早的同身份不同 ref 的 payload 命中遮蔽），300/300 单 null 记录的
  三族字面形式暴力（16 零、`LE32(s)+12 零` s≤131072、`LE32(k)+12 零` k≤60000）
  全部失败，证明无固定字面形式。从零编译时只要写入顺序自控即可精确模拟；
  克隆插入（第 9 节）则完全绕开该问题。

`sub_18050E610` 没有真实外部调用者（`callers` 列出的 `0x180854F50/0x180854F88/
0x181E79E88` 均为 `.rdata`/`.pdata` 的 RUNTIME_FUNCTION 异常表项），仅被 Finalize
Phase 1 与自身递归调用。

## 11. 克隆 FST 战斗服崩溃排查（进行中，当前最高优先级）

**症状**：战斗服加载克隆版 `T01M08_Mill.fst` 后 player.dll 崩溃；C1 崩溃点为
`mov [rcx+28h],rsi`（rcx=NULL，scenenode 构建期疑似 NULL child）。官方原版正常。

### 11.1 二分定位（已闭环）

零修改 round-trip（仅重算外层 MD5 并重新 zlib 打包、内容字节不变）→ 战斗服正常加载；
克隆版（+3 条记录）→ 稳定崩溃。重建管线排除，问题锁定在**克隆语义本身**。

### 11.2 digest_B 多条共享假设被推翻（实证）

`tmp/dup_digest_b.py` 实测 Mill：51,255 条记录对应 51,255 个**互不相同**的 digest_B，
零重复。dB ↔ 扁平索引是双射，"按 dB 判重"与"按槽判重"两种模型等价。
（早前 find_value_records.py 报"模板首现位置为 REPEAT"是 walker 自身状态错位伪影。）

### 11.3 引用流读取语义权威确证（反编译闭环）

- `sub_1805195F0`（ZObjectInputStreamV2 vtable slot 16，ReadObjectReference）核心即
  `sub_180517620`；记录项布局 `flag(+0)/class(+4)/group(+8)/span(+16)/dA(+20)/object(+40)`。
- **首现**（flag=1）：清 flag、span 置 0，经 `sub_180517210` 按 schema 成员表递归反序列化，
  返回新对象。
- **REPEAT**（flag=0）：若 span>0，**从流中再读 (span-1) 个索引并只清它们的 flag/span
  （不构建对象）**，然后清自身 span，返回共享对象。
- 游戏端对象同一性 = `(schema_id, digest_A)`；值记录（dA[0:8]==0 且 dA[8:12]==LE32(1)）
  的槽对象为 NULL。
- `sub_180514090` 仅从 payload 读标量成员、`sub_18051C050` 是成员深拷贝，均不消费引用流。

### 11.4 引用流读取层排除（模拟闭环）

`tmp/sim_game_reader.py` 精确模拟上述语义（含 (schema,dA) 对象共享与值记录 NULL），
对官方/克隆/C1 三个文件比对：槽位消费完全一致
（official values=172099 consumed=172097；克隆 172108/172106），
均为 33 根、1 条未物化记录（官方本就存在）、0 双重物化、33 个值记录 NULL、7550 个 index-0。
**崩溃不在槽位消费层。**

### 11.5 现行嫌疑：对象身份共享层（diff 定位）

`tmp/diff_game_graph.py` 按游戏语义（身份=(schema,dA)、值记录→None、边=引用流 DFS 子引用）diff：

- 游戏按 (schema,dA) 共享对象：官方 51,255 条记录 → 仅 **12,307 个身份**，
  2,772 个身份有多条记录（twin）。
- 克隆仅新增 2 个身份（新 434 节点 @记录 36359、新 731 transform @51258），0 删除，
  但有 **2 个共享身份的边集被改变**：
  1. `schema 434 dA f13d9698..`（记录 25803/25804 共享同一游戏对象）：边 215→216。
     克隆子树拼接点 `values[template_pos+template_span:...]`（fst_inspect.py:934）
     落在**共享身份记录的展开区间内**。
  2. `schema 438 dA 80fdd166..`（Kinematics property-set，全部 2365 个场景节点共享一个
     对象）：克隆 property 记录**复用了模板 kine_digest_a**（fst_inspect.py:818
     `kine_digest_a = property_record.digest_a`），成为第 2366 个 twin，
     其边指向新 731 transform。
- 假设链：新槽首现 → 对 (schema,dA) 共享对象**再次反序列化/覆写子指针** →
  破坏"每身份一对象"不变量 → PostLoad/scenenode 构建期 NULL child。

### 11.6 最新线索：unused 对象列表（postloadscene.cpp）

- `sub_1804BA590` = **RegisterUnusedObject**：懒初始化全局 linarray `qword_181DC7570`
  （源码 `postloadscene.cpp:433`）并 append 对象指针。
- `sub_1804BA870` = **PostLoadScene 收尾器**：依次跑 FinalizeTraverse、ZReto_TerrainModifier、
  ZPAmmoInfo、ZPHeightfieldData、ZPrimTerrain、ZReto_StartupWeapons、ZPCapturePoint*、
  ZPSpawnArea、ZAudioPropertyData、ZPCharacterTypeVehicles、ZReto_HealthDepot、
  ZReto_AmmoDepot 等阶段后进入 **"Remove unused"**：
  - 对 `qword_181DC7568` 列表（16B 条目）逐条调 `(obj+8)->vtable+120(arg)`；
  - 对 `qword_181DC7570` 列表逐对象：vtable+408 取名字 → `sub_180080130`（unused 日志）→
    **`*(obj+32)=0`** 清空对象字段；
  - 随后两列表释放、全局清零。
- `sub_1804BA590` 的调用者：`sub_180098BE0`、`0x18084D098`、`0x18084D0A8`、`0x181E76C48`
  （后两个疑为非代码引用）。**对象进入 unused 名单的登记条件尚未查明——这是恢复排查后的第一步。**

### 11.7 待验证的修复方向（未实施）

1. 给克隆 438 property 记录合成独立 digest_A + 新 payload，脱离 2366-twin 共享
   （难点：payload 是 "Kinematics/kine" 属性集，改 dA 需重造 payload 内容）；
2. 或移动克隆子树拼接点，避开共享身份（434 f13d9698）记录的展开区间；
3. 查明 unused 登记条件后再最终定夺。

### 11.8 当前部署状态

| 位置 | 当前文件 |
|---|---|
| `D:\HnG-test\combat-server\_packed\...\T01M08_Mill.fst` | C1 同名克隆版，备份 `.bak_pre_clone` |
| `D:\HnG-test\_packed\...\T01M08_Mill.fst` | 克隆版，备份 `.bak_pre_clone` |

## 当前关键卡点

**第一优先：克隆 FST 战斗服崩溃根因**（详见第 11 节）。字节级克隆插入（第 9 节）产物在
player.dll 端崩溃；引用流读取层已排除，嫌疑集中在 (schema,dA) 身份共享层（11.5）与
unused 对象登记条件（11.6）。修复 `insert_cloned_instance` 前不宜推进批量克隆。

引用流的解码/重编码/克隆插入均已闭环（第 8/9 节），单实例增删已可字节级产出。
Unity 场景与模板 `.fst` 之间仍缺少稳定的实例身份键。仅靠本地坐标或 prefab 显示名不能安全匹配；
`T01M08_ColmarHamlet_173288-QQ.unity` 的抽样坐标也不能直接命中 `T01M08_Mill.fst` 的局部变换。
下一步必须恢复导出场景所用的原始 SceneNode 身份/层级，或在 Unity 中写入持久的 FST record/digest 元数据，
然后才能把 6000+ Unity 实例批量送入已经闭环的模板回写后端。

## Unity → FST 编译链状态

最终交付目标是完整编译 `D:\hng-map\Project`，不是只修改现有沙袋。当前拆为两阶段：

1. **模板编译后端**：以一张官方 `.fst` 的非场景资源对象为基底，用 Unity 场景同步实例、层级和变换，
   完整重算 ObjectStreamV2 引用及摘要。这一阶段用于尽快得到可由游戏加载的 Unity 编辑结果。
2. **独立编译后端**：从 Unity prefab/FBX/材质/碰撞信息生成完整 SceneNode、PropertySet、网格、碰撞和资源依赖，
   不再要求同名官方 `.fst`。这是最终的“从零生成 `.fst`”。

Unity 前端已落地为 [server/tools/hng_unity_scene.py](server/tools/hng_unity_scene.py)：

- 解析 Unity `PrefabInstance`、prefab GUID、父节点、名称和本地 TRS；
- 由 `D:\hng-map\Project\Assets\Script\PrefabSpawn.cs` 的导入代码反证坐标转换；
- Unity → HnG 位置：`(x,y,z) -> (x,z,y)`；
- Unity → HnG 四元数：`(x,y,z,w) -> (-x,-z,-y,w)`；
- Unity → HnG 缩放：`(x,y,z) -> (x,z,y)`；
- `T01M08_ColmarHamlet_173288-QQ.unity` 可导出 6000+ 个实例，沙袋样本位置和四元数已有回归测试。

完整编译仍需闭环的后端对象包括：SceneNode 引用编码、实例/主对象关系、mesh/LOD、材质、碰撞、
terrain/heightfield 以及 `_objects` 外置对象。任何一项都不会用 Unity 字段名进行猜测映射。

## 可复现工具与验证

- 检查器：[server/tools/fst_inspect.py](server/tools/fst_inspect.py)
- 回归测试：[server/tests/test_fst_inspect.py](server/tests/test_fst_inspect.py)
- Unity 场景前端：[server/tools/hng_unity_scene.py](server/tools/hng_unity_scene.py)
- Unity 前端测试：[server/tests/test_hng_unity_scene.py](server/tests/test_hng_unity_scene.py)
- 运行：

```powershell
python server/tools/fst_inspect.py `
  D:\HnG-test\_packed\Environments\Scenes\Gamemaps\T01M08_Mill.fst `
  --schema D:\HnG-test\_packed\_schemas\000417b244e3b07175a81ba446daace8.bin `
  --json

python -m pytest server/tests/test_fst_inspect.py -q
```

## 2026-09-19 运行时突破（最新接力入口）

模板克隆后端已经首次产出由战斗服完整加载的修改版 `.fst`：

- 输出：`D:\HnG-test\work\T01M08_Mill.clone-parent-linked.fst`
- 成功产物：`D:\HnG-test\work\T01M08_Mill.clone-parent-linked.fst`
- 大小：3,493,400 bytes
- MD5：`e626e03b0ca220dfe9f4d59f9089462a`（输出文件与 combat-server 部署文件一致）
- 模板：官方备份 `T01M08_Mill.fst.bak_pre_clone` 的 record 8449，`SandbagWallA_4m_lowA1`
- 新实例名：`sandbag_clone_parent_linked`
- 新位置：`(-682.8263549804688, 61.25, 156.93588256835938)`
- 运行结果：地图 1.71 秒加载完成，成功创建 battlefield、初始化 AI，之后持续输出玩家计数；观察约 45 秒无崩溃，测试进程随后人工停止。
- 自动测试：`7 passed`。

用户随后把成功产物同时部署到客户端与 combat-server，并已在客户端实机找到新增沙袋，
因此“新增实例可见”也已验收。为做原版对比，客户端和 combat-server 当前都已恢复官方原版，
两端 `T01M08_Mill.fst` MD5 均为 `cc51d18266a56c03b7d85566af786931`。成功克隆版仍保留在
上述 work 路径，服务端另有 `T01M08_Mill.fst.bak_parent_linked_20260919`。

真正的崩溃根因不是早先假设的 `(schema_id, digest_A)` 共享对象。`ZObjectInputStreamV2_LoadStream`
会为同 digest_A 的连续上下文记录分配不同对象，再复制标量并深拷贝指针成员；相同 digest_A 记录只要求在组内连续。

失败版只克隆根节点时，共享的子 SceneNode 仍指向旧根。完整克隆 7 个实例自有 SceneNode 后仍在
`player.dll+0x80180` 触发 `__debugbreak()`，因为层级关系只更新了一侧：克隆根指向 `_ArmyProps`，
但 `_ArmyProps` 缓存 payload 的 child count 仍为 31。最终修复同时完成：

1. 克隆 root、`_Coli`、collision mesh 与四个 LOD，共 7 个 SceneNode；
2. 克隆 root Kinematics PropertySet 与 transform，复用其他资源对象；
3. 把克隆子树插入父节点 DFS 区间并传播所有祖先 span；
4. 将 `_ArmyProps` 的 child count 从 31 改为 32，重算其 digest_A/cache payload；
5. 保持同 digest_A 的记录块连续，重排一基 flat indices 并重建引用流、缓存、wrapper MD5 和 zlib。

第 11.3–11.7 节保留的是排查历史，其中“同 `(schema,dA)` 共享同一运行时对象”和“当前仍崩溃”
均已被本节取代。下一优先级是把 Unity 实例稳定映射到模板 SceneNode，然后做批量新增/修改/删除；
从零生成 mesh、材质、碰撞、terrain 仍未闭环。

复现生成命令（在 `server` 目录运行）：

```powershell
python tools/fst_inspect.py D:\HnG-test\_packed\Environments\Scenes\Gamemaps\T01M08_Mill.fst.bak_pre_clone --schema D:\HnG-test\_packed\_schemas\000417b244e3b07175a81ba446daace8.bin --clone-record-index 8449 --clone-name sandbag_clone_parent_linked --clone-position -682.8263549804688 61.25 156.93588256835938 --output D:\HnG-test\work\T01M08_Mill.clone-parent-linked.fst
```

## 2026-09-19 Unity → FST 身份链接突破（最新）

旧前端的 Unity→HnG 轴交换公式已被官方 Mill 数据推翻。对 Unity 中全部 12 个
`FenceSandbagA_BLowStraight_4m` 与 FST 中 12 个逻辑 `SandbagWallA_4m_lowA` 实例做点云匹配后，
得到并全量验证序列化场景真正使用的公式：

```text
position:   Unity (x,y,z)   -> FST (10*x, 10*y, -10*z)
quaternion: Unity (x,y,z,w) -> FST (-x, -y, z, w)
scale:      Unity (x,y,z)   -> FST (x,y,z)
```

12/12 沙袋均唯一命中正确逻辑实例；每个逻辑实例在官方 FST 中对应两个上下文 SceneNode record。
位置最大误差 `<0.008` FST 单位，四元数最大误差 `<2.2e-5`。`PrefabSpawn.cs` 是旧的一次性运行时
辅助脚本，不能代表当前 `.unity` 文件的实际坐标编码。

新增工具 `server/tools/hng_unity_fst_link.py` 会在未编辑的基线场景上，将稳定的 Unity
`instance_file_id` 固化到 FST record 候选，并把 unmatched/ambiguous 明确保留，绝不猜测绑定。
当前审计清单：`D:\HnG-test\work\T01M08_ColmarHamlet.fst-link.json`。

Mill 首轮结果：

- Unity PrefabInstance：6220；FST `#model` bindings：4975；
- `matched=36`、`ambiguous=20`、`unmatched=6164`；
- 低覆盖率并非坐标公式失败：Unity 父容器统计为 `Bush=2992`、`Grass=2518`、`Tree=317`、
  `O1=264`、`Misc=68`。前三类属于优化植被/外置对象路径，不是主 Mill FST 中普通模型实例；
- O1 中 12 个目标沙袋全部正确链接。其他 O1 对象还需建立 prefab GUID → FST 模型根的别名规则，
  以消除同位置的 LOD/子模型歧义；植被三类必须另行解析 `_objects`/实例化资源格式。

新增 `scene_node_world_transforms()` 可沿 FST parent 边递归合成本地 TRS；
`hng_unity_scene.py` 现在还输出父容器名称。下一步应基于链接清单实现编译器：

1. 现有对象用 `instance_file_id -> digest_B/record` 更新两个上下文记录；
2. 新对象按 prefab GUID 选择已验证模板，调用已通过实机验收的完整子树克隆；
3. 删除对象尚未实现，必须同步父节点 children、缓存及祖先 span，不能直接删记录；
4. 主 FST 编译与 Bush/Grass/Tree 外置实例编译必须分开处理。

新增 `server/tools/hng_unity_fst_compile.py` 作为编译前的安全变更计划器。它读取冻结的链接清单，
按 `instance_file_id` 比较当前 Unity 场景，分别输出：可直接更新、可用已验证 GUID 模板新增、删除、
重挂父节点、无模板新增及其他不支持项。基线 Mill 已生成
`D:\HnG-test\work\T01M08_ColmarHamlet.fst-plan.json`，五类计数全部为 0，证明当前场景与冻结身份清单
严格一致。下一步是把 plan 中的 `updates` 接到成对上下文 transform 回写，把 `added` 接到完整子树克隆；
删除与 reparent 必须先实现通用父 children 改写，当前计划器会明确阻止它们静默执行。

当前结果：`7 passed`；覆盖 15/15 官方 FST 外层、完整 RS03、Mill 对象缓存、
2365 个变换、2365 个 PropertySet 映射、完整引用图、一次实际变换回写后的重新解析，
以及一次完整克隆实例插入（含重新解析与绑定校验）。

### 接力时的工作区状态

以下 7 个文件目前位于 `server` Git 工作树中且尚未提交（`git status --short` 显示 `??`）：

- `tools/fst_inspect.py`
- `tools/hng_unity_fst_compile.py`
- `tools/hng_unity_fst_link.py`
- `tools/hng_unity_scene.py`
- `tests/test_fst_inspect.py`
- `tests/test_hng_unity_fst_link.py`
- `tests/test_hng_unity_scene.py`

不要清理或覆盖这些文件。测试生成的修改版 FST 只存在于 pytest 临时目录；目前没有把实验文件覆盖到
`D:\HnG-test\_packed` 或游戏目录。`patch_kinematics_transform()` 返回完整 FST 字节，下一步批量编译器
应在明确输出路径写新文件，并保留官方模板不动。

## IDB 已沉淀命名

- `FST_Save_OptimizedScene` — `0x1804B55C0`
- `CatalogOutputStreams_Finalize` — `0x180509D30`
- `ZPerObjectFileCatalogOutputStreams_Finalize` — `0x18050AAD0`
- `Catalog_WriteSections` — `0x18050E340`
- `SchemaCache_Load` — `0x180511C50`
- `ObjStreamV2_WriteData` — `0x18050EDE0`
- `ZObjectInputStreamV2_LoadStream` — `0x180517960`
- `ZObjectInputStreamV2_ReadObjectReference` — `0x1805195F0`（vtable slot 16）
- `ResolveObjectReference` — `0x180517620`（首现/REPEAT 的 flag/span 权威语义，见 11.3）
- `DeserializeObjectMembers` — `0x180517210`（按 schema 成员表递归反序列化）
- `PostLoad_RegisterUnusedObject` — `0x1804BA590`（全局列表 `qword_181DC7570`，见 11.6）
- `PostLoadScene_Finalize` — `0x1804BA870`（"Remove unused" 消费两列表，见 11.6）

详细反编译证据和历史调查见 [fst-map-format-investigation.md](fst-map-format-investigation.md)。
