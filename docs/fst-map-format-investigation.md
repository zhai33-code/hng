# .fst 地图文件格式调查（沙袋工事编辑）

> **2026-09-18 更正提示：** 本文保留了调查过程，§3、§4、§5、§7.4、§7.7、§7.9
> 中把对象缓存 payload 误判为顶层 manifest/cluster/catalog section 的内容均已作废。
> 当前权威结论、精确偏移和验证工具以 [fst-progress.md](fst-progress.md) 为准。
> 已确认 FST 使用 catalog `mode=2` 专用对象流，不走普通 `Catalog_WriteSections`；
> 解压流是 `[16B MD5][ULEB128 payload长度][ObjectStreamV2 payload]`。

> 调查日期：2026-09-10
> 目标：直接编辑 .fst 场景文件，移动/增删地图上预置的沙袋工事
> 状态：**未完成**——已摸清文件宏观布局与节点/簇表格式，卡在"变换矩阵的具体序列化编码"一步

## 1. 任务背景

需求：调整 T01M08_Mill 等地图上预置的沙袋（SandbagWall）位置。

先期验证过的死路：

| 路径 | 结果 |
|------|------|
| scenesetup XML `<defencestructures>` | 无效——只影响动态生成的工事，不作用于 .fst 内预置的沙袋 |
| warserver `ResponseBattleFieldSettings f11` | 无效——客户端渲染预置沙袋读的是本地 .fst 数据，不是网络消息 |

结论：必须直接改 .fst 内沙袋实例的变换矩阵（位置/旋转/缩放）。

## 2. .fst 容器格式（已确认）

- 文件头 4 字节 magic `"FST\0"`（0x46535400）
- 之后整个是 **zlib 流，无签名、无 CRC 校验**——解压→改→重压即可被游戏加载（已验证往返可行）
- T01M08_Mill.fst 解压后 9,638,603 字节（~9.6MB）

修改通道已经打通：`tools/gamemap_patch.py`（KVCS 补丁，用于 gamemap f4 改 `file:<name>` 走外部 XML）+ `tools/start_custom_map.ps1`（离线快验），与本调查互补——XML 链路能改天气/底图，但改不了沙袋。

## 3. 解压后宏观布局（已确认）

以 T01M08_Mill.fst 为例（偏移为解压后流内偏移）：

```
+0000000 .. +07c1f00   ~8.0 MB  二进制大块（疑为网格/顶点/索引数据，含 RTRE 块）
+07c1f00 .. +07d0edd     61 KB  节点 manifest 流：1594 条记录
   （小间隙）
+07d191d .. +07dba72     36 KB  簇表（cluster table）：1189 条记录
+07dba72 .. EOF         ~1.4 MB  二进制块——疑似属性数据（含变换），编码格式未破译
```

RTRE 标记（0x52545245）散布在大二进制块内（如 +0x8209b1），**实测 RTRE 块存的是 AABB 包围盒（min/max），不是变换矩阵**——早期误判点。

## 4. 节点 manifest 记录格式（已确认）

流起点 +0x7c1f00，1594 条，每条：

```
[u8 size][u8 klen][key bytes][0x00][u8 vlen][val bytes][0x00][u32 a][u32 b]
```

- `key` = 节点类型：`#model`(800)、`polymsh`(554)、`null`(198)、`crvlist`(14)、`pointcloud`(11)、`light`(10)、`TransfoGroup`(3) 等——**这是 XSI/Softimage 场景术语**，引擎场景格式源自 XSI
- `val` = 节点名（如 `SandbagWallA_RoundingA_lowA_Instance2`）
- `a` ∈ 1..4（疑为层级深度或属性簇数）
- `b` ∈ 0..213（疑为父节点索引或簇引用）

节点是**乱序扁平表**，不是按树序排列。

### 沙袋节点（29 条 val 含 Sandbag）

模板 6 种：
- `SandbagWallA_RoundingA_lowA`（圆弧矮墙）
- `SandbagWallA_3m_lowA` / `SandbagWallA_3m_lowA1`（3m 矮）
- `SandbagWallA_3m_highA` / `SandbagWallA_3m_highB`（3m 高）
- `SandbagWallA_4m_lowA`（4m 矮）

实例约 20 个（`_Instance1..11` 变体命名），全部 `key=#model`，大多 `a=1, b=5` 或 `b=1`。

## 5. 簇表格式（已确认）

起点 +0x7d191d，1189 条，每条：

```
[u8 size][u8 l1][name][0x00][u8 l2][type][0x00]
```

类型分布：

| type | 条数 | 说明 |
|------|------|------|
| MultiCollision | 655 | 碰撞 |
| Reto_Collision | 490 | 碰撞 |
| polymsh / cube | 各 2 | 网格 |
| **kine** | **1** | **Kinematics——变换矩阵属性，本调查核心目标** |
| 其余 ~40 种各 1 | | Reto_Instance、Reto_AccessPoint、Reto_CapturePoint、camera、light、ZPHeightfieldData 等 |

关键条目：`(Kinematics, kine)` 位于 +0x7d5904。

## 6. IDA 逆向结果（player.dll）

### .fst 加载链路

- "FST\0" magic 0x46535400 在 player.dll `0x1807A66C0`（资源类型表内）
- `sub_1804B55C0` = resourcev2 资源类型分发器
- `sub_1804535A0` = 场景序列化/反序列化
- 源码路径（字符串泄漏）：`D:\RETO\code\engine\resources\propertysets\_private\kinematics.cpp`

### ZKinematics 类（sub_180081060 = 构造+注册）

注册名：type=`kine`，display=`Kinematics`（经 sub_1804A4210 注册进属性系统）。

**内存布局**（注意这是运行时对象，不是 .fst 线上格式）：

| 偏移 | 内容 |
|------|------|
| +0 | vtable |
| +32 | scale（3 float，默认 1.0） |
| +80 | position（3 float，默认含异或混淆初值） |
| +96 | rotation |
| +112 | quaternion（默认 0,0,0,1） |
| +128 | 标志字节 |

构造函数里有 `_mm_xor_ps` 对位置/旋转做异或——**运行时对象有混淆，序列化格式待破译**。

### ZKinematics vtable

vtable dump 进行到一半被中断（`??_7ZKinematics@@6B@`），序列化/反序列化虚函数槽位未完成定位。`ida_tool.py` 本次新增了 `findimm` 子命令（搜立即数常量，如 0x52545245="RTRE"）。

## 7. 属性块（原"卡点二进制块"）——已破译（2026-09-17）

原 +0x7dba72 后的"乱值块"实为**属性块 section**，其框架与字符串池编码已完全破译
（数据侧 15/15 官方 .fst 验证 + 代码侧 player.dll 反编译双向确认）。

### 7.1 Section 框架（已确认）

```
[u16 nrec][u16 ver=1][u32 S][字符串池 S 字节][\0 终止空串][记录区 ...]
```

- `S` = 池字节数，**不含**末尾终止空串；15/15 文件实测 used == S+1（含终止符）精确吻合
- 池终止：读到空串即结束（代码语义，非长度驱动）

### 7.2 字符串池条目与编码（已确认）

```
entryK: [enc str]\0 [LEB128 varint RID]     k = 0,1,2,... 逐条 +1（无跳号）
编码:   enc[j]  = bitrev8( (plain[j] - k) & 0xFF )
解码:   plain[j] = bitrev8[ enc[j] ] + k    （bitrev8 自逆）
```

- bitrev8 = 8 位字节位反转；**player.dll 内无静态查找表**——由 SSE 掩码常量
  （xmmword_1807A6960/970/980/990/9A0/9B0 + 18079B080 + 18075F1B0）运行时构建 256B 表
- **RID = LEB128 varint**（通常 5 字节）。早期观测的"[4B 头]+[1B t]"是误读：
  那 1B 是 varint 末字节，不是独立字段
- RID 全局稳定：68 条路径跨 15 文件 0 冲突；但**不是** crc32/adler32/fnv1a/djb2/sdbm/jenkins
  （全部实测无命中），算法疑似在 RETO 打包工具侧，player.dll 只读不算
- 加载时资源查找按**路径字符串**做红黑树比较（sub_180068B70 插入，比较子 = strcmp），
  RID 仅是节点附带数据（节点+40），不参与查找 → 自建 .fst 时 RID 可暂填 0 试 load

### 7.3 代码侧锚点（player.dll，已 rename 进 IDB）

| 地址 | 名称 | 作用 |
|------|------|------|
| 0x1804B3CB0 | `FST_Pool_Read` | 池读取循环：ReadCString→空串终止→bitrev 解码→ReadVarint(RID)→插入 a1+90 红黑树 |
| 0x1804B55C0 | `FST_Load_DecodeRegister` | .fst 装载主流程（FST magic 分发、296B/条资源数组、解码+注册、UniqueFiles 写出） |
| 0x180507430 | `ZStream_ReadVarint` | LEB128 读 |
| 0x180507250 | `ZStream_WriteVarint` | LEB128 写 |
| 0x180507520 | `ZStream_ReadCString` | \0 终止串读 |
| 0x180507320 | `ZStream_WriteCString` | \0 终止串写 |

- "FST\0" magic 引用点：0x1807A66C0（数据），代码引用在 FST_Load_DecodeRegister 内
- 源码路径泄漏：`D:\RETO\code\engine\player\resourcev2\_private\resourcemanager.cpp:1346`
- 旁证：sub_180525270（崩溃报告写出器）也用同一 SSE bitrev 表（byte_181E2DC60）——
  该表是引擎通用混淆原语，不止 .fst 在用

### 7.4 记录区（下一步目标）

池之后紧跟记录区（nrec 条；T02M03 nrec=698 池仅 5 串）。
T01 系列记录区开头观测：单位四元数 + 平移浮点重复模式 → 疑似实例变换数据，
与 kine 属性的关联（记录 ↔ manifest 节点 ↔ kine 簇）是下一步反编译目标。

验证脚本：`tmp/fst_codec5.py`（15 文件池全量解析 + RID 一致性）、`tmp/fst_rid1.py`（RID 哈希排除）

### 7.5 加载链路与场景节点反序列化（2026-09-18 新增）

**加载调用链**（Resource_LoadByName 0x1804B3260 反编译确认）：

```
Resource_LoadByName(name)
  └─ 路径含 .fst → FST_File_Read(0x1804B3CB0)   ← 只读 pak.bin 字符串池 + 建资源注册表
       （打开 pak.bin、跑 256B SSE bitrev 表、循环 ReadCString/ReadVarint，池读完即返回）
  └─ 其余资源 → sub_1804B9E80(v101) 判断存在性
       → sub_180517960(v99, a1+304, v100, v55)  ← 真正打开/装载资源（a1+304 = 文件系统接口）
       → sub_1805195F0(v99) 取句柄；返回 v58-8 给上层
```

- **FST_File_Read 不读记录区**——它只建 "路径→RID" 红黑树（a1+90）。记录区的读取在
  后续资源真正被实例化时才发生（按需反序列化）。
- ZStream_ReadVarint(0x180507430) 全局调用方仅 6 个：FST_File_Read、sub_1805093B0、
  sub_180517960、0x18071EBD0、0x1807ADDB8、0x181E79A14——**记录区不走 varint 读**，
  说明记录区是定长/按 schema 字段读，不是 varint 流。

**ZSceneNode 反序列化链路**（scenenode.cpp 系）：

- sub_1800755B0 = ZSceneNodeInstance 构造/挂接（引用 "ZSceneNodeInstance" RTTI 名 +
  "Instance '%s' is missing master, must be a bug in scenenodepacker"）：
  - 内部调 sub_18006F870(a1) —— 这是**场景节点属性构建主函数**：
    - 按属性 ID 查属性（vtable+80 偏移的 FindById，参数为立即数 246/666/672/1335/1340/1339/607）
    - 1335/1340/607/1339 等 ID 对应碰撞/实例/几何属性集
    - 大量 vtable 调用：+560 读四元组、+552 写回（归一化 rsqrt → **四元数归一化**），
      坐实"记录区存四元数旋转"假设
    - 末尾 sub_180080130 把节点插入 v1+80 红黑树（按节点名排序）
- sub_180070800 = 递归**属性写入器**（按 schema 类型分发，switch 按字段类型 ±26..-17
  写 byte/word/dword/float/数组）——这是"属性值 → 对象内存"的通用路径，
  反推即可知记录区字段编码。

下一步：跟 sub_180517960 → sub_1805195F0 返回句柄后的消费方，定位"nrec 条记录"循环。

### 7.6 写出侧（.fst 打包器）——最短路径（2026-09-18 新增）

**重大更正**：原 IDB 名 FST_Load_DecodeRegister(0x1804B55C0) 实为
**FST_Save_OptimizedScene**（写出侧，player.dll 内置完整打包器）。写出器即格式规范。

**FST_Save_OptimizedScene (0x1804B55C0)** 阶段（ZSecureString 标签）：
1. "Invalidate unresolved links" —— 遍历资源表清未加载链接（警告字符串 resourcemanager.cpp）
2. "Optimize scene graph" —— 若文件名含 "crr" 则 sub_18006F160 + sub_180076920
3. 生成三份输出路径：`<base>.fst` / `<base>.resources.txt` / `<base>.extra`（先 .tmp 再 rename，
   sub_1804D83C0 完成）；容器路径 sub_18050AA20(v355, dir, "bin", "FST", 1)
4. "Level" / "Shared"（路径含 Shared_Resources 时）+ sub_18050FDA0 建 schema 流
5. **写 FST 字符串池**：对每条资源 296B 条目 `{len, char[len], pad, obj*, RID@+272}`，
   `enc[j] = bitrev8[plain[j]] - i`（i=条目序号）后 WriteCString + WriteVarint(RID@条目+272)
   —— 与读取侧 codec 严格互逆，双重实锤
6. "Crc32"、"UniqueFiles" 阶段；resources.txt = 全资源路径列表（"%s\n" 逐行）

**ObjStreamV2_WriteSchema (0x18050FDA0)** —— RS03 `_schemas` bin 写出器：
```
WriteCString("RS03")
ZStream_WriteU32(类名数)          ← 0x180507220 = WriteU32(vtable+32, &v, 4)
for 每类: WriteCString(类名)
for 每个 RTTI 类: WriteCString(类名); WriteCString(显示名@schema96B+40)
  1B flag = (有v37)|2|4(命中ZSceneNode缓存类) …
  for 每属性: WriteU32(0分隔); 1B = ((flag&0x20000000)?1:0)+((flag>>29)&2)+((count<0)?4:0)
             WriteU32(0); [指针型字段: 递归 switch(8*type>>3) case -26..-19 sub_180079350]
             WriteU32(0)
WriteCString("")  ← 空串终止
末尾 sub_1804B9860 算 16B 哈希 → 作 "_schemas/<hash>.bin" 文件名写入容器
```

**ObjStreamV2_WriteObject (0x180511050)** —— 单对象写出：先查缓存（64B key 树，
sub_18051B520 哈希），命中直接引用；未命中序列化，**>4096B 走 gzip**（sub_1805234E0,
标签"gzip"）。

**sub_18050EDE0（"oos.WriteData"）= 字段数据写出核心**（递归）：
- 逐属性：`Value = PropField_ReadValue(属性)`（0x180079350，数组长度来源）
- **定长类型直接 memcpy**（WriteData vtable+96, len = 元素宽 × Value）：
  `-27/-26/-25`→1B、`-24/-23`→2B、`-22/-21`→4B、`-20/-19`→8B、`-18`→4B、`-17`→8B、
  `-30`→指针数组逐元素 WriteU32
- varint 型：按子类型 switch 分发 `vtable+24/+40/+48/+64` 逐元素写（1B/4B/4B/2B 四组）
- **对象型**（flag 0x20000000）：逐元素 ObjStreamV2_WriteObject；否则递归 sub_18050EDE0
- 尾部 "Cache" 阶段：写缓存块（WriteObject 回调 + 长度 + 流复制）

**PropField_ReadValue (0x180079350)**：取数组元素数/字段值——按 type 高位 0x40000000
判指针型，switch(8*type>>3) 取 1B/1B/2B/2B/4B/4B/8B，递归解析多维。

结论：**记录区 = ObjStreamV2 对象流**（schema 头 + 按 schema 顺序的定长/varint 字段流 +
对象引用缓存表），非自由 varint 流。下一步用写出管线逐字节对照 tmp/fst_records2.py dump。

本轮 IDB rename/comment：FST_Save_OptimizedScene、ObjStreamV2_WriteSchema、
ObjStreamV2_WriteObject、ZStream_WriteU32、PropField_ReadValue（均已写回 player.dll.i64）。

### 7.7 .fst 解压流 = catalog 目录容器（2026-09-18 重大更正）

**更正 §3 的宏观认知**：.fst 解压后不是"单一对象流 + 分段"，而是一个 **catalog 目录容器**
（源码 `D:\RETO\code\engine\common\stream\_private\catalogstreams.cpp`），内部以
(目录, 名字) 组织多个子文件。§3 的"节点 manifest 流 / 簇表 / 属性块"都是 catalog 里的
独立条目，之前的偏移划分其实是各条目的边界。

**catalog 内存树节点布局**（280B/节点，读侧构造函数确认）：

| 偏移 | 内容 |
|------|------|
| +16 | 目录名 |
| +296 | 条目名 |
| +564 | 数据长度 u32 |
| +568 | 数据指针 |
| +576 | 缓存流 |

**条目打开（sub_1805093B0，读侧）**：
`[CString 名字][16B MD5][varint 长度][数据]`，MD5 不符抛 `AVMD5MismatchException`；
数据装载进节点 +896 的内存流。

**目录解析（sub_18050D1E0）——catalog 容器线上格式**：

```
循环（按 section 分组）:
  [u32 section_size]                       ← ReadU32（vtable+72）
  每条目:
    [CString 目录 dir][CString 条目 name][u32 数据长度][u32 数据长度2/标志]
    （ v8 累加 = 4 + 每条目(两串长度+2 + 8) ）
  每条目按序跟 [u32长度] 的数据块（ReadData vtable+112）
  校验：累加字节数 == section_size，否则 __debugbreak（catalogstreams.cpp:506）
  之后又读一个 u32（下一 section 的 size 或结束标志 0）
```

条目读出后 sub_18050CD80 插入内存树（按 dir→name）。

**条目 payload 的 gzip2/md5 包裹（sub_18050DED0，写出侧收尾）**：
- flag+17（a1+17）= "md5"：条目内容整体算 16B MD5
- flag+16（a1+16）= "gzip2"：payload = **gzip 包裹 [16B MD5][varint 未压缩大小][原始数据]**
  （先写 MD5 16B 进 gzip 流，再 WriteVarint(未压缩大小)，再写数据）

### 7.8 SchemaCache_Load（0x180511C50）——读侧 schema 加载器

- 对象流资源开头 = **16B 摘要**；sub_1804B7740 读 16B 摘要 → sub_1804B9860 哈希 →
  拼 `"_schemas/<hash>.bin"` 注册依赖 → **文件名 = 摘要的 hex 编码**（15/15 数据实证：
  stream[0x14:0x24] == HASH）
- .bin 格式（RS01/RS02/RS03）：类名表 + 每类 {类名、显示名、flag 字节、属性表
  （属性 id == −32 终止，48B/条运行时结构）}
- 类型码 0xFFFFFFE6..EF（−26..−17）→ 元素宽 1/1/2/2/4/4/8/8（与 §7.6 写出侧一致）

**对象二级存储（ObjStreamV2_WriteObject 0x180511050）**：
对象序列化后算 16B MD5 →
- **< 4096B**：内联注册（sub_18051C440，{schemaId u32, MD5 16B}）
- **≥ 4096B**：gzip 后写成 `_objects/<hash>` 独立 catalog 条目

### 7.9 解压流字节级布局（fst_layout3.py 实测，2026-09-18）

| 文件 | stream 长度 | [0:16] | [16:20] | varints@0x24 起 | 池 hdr | nrec | 池后记录区起点 | 尾部 |
|------|------|--------|---------|------------------|--------|------|------|------|
| T02M03_TankFactory | 15,903,717 | `28 b7 98 19 ef d7 51 7f db d4 fc 56 d7 fa ae a3` | `d1 d7 ca 07` | −92, 33, 325, 1186, "SVC\0" | 0xB91A93 | 698 | 0xB91BD0 | 3,772,437 |
| T01M08_Mill | 9,638,603 | （各异） | `b7 a5 cc 04` | −87, 33, 325, 962, 1182499, 0, 48, 59 | 0x7DBA72 | 763 | 0x7DBBF0 | 1,398,491 |
| T02M05_Skirmish | 5,909,197 | （各异） | `0a b9 d5 e8` | −86, 33, 325, 685, 83('S'),86('V'),67('C'),0 | 0x4C38EC | 427 | 0x4C399A | 913,715 |

- `[0x14:0x24]` = 16B schema 摘要（全部命中 `_schemas` HASH，实锤）
- `[0:16]` 每文件不同，疑 GUID/MD5；`[16:20]` 每文件不同（疑 varint 大小，未证实）
- 首负 varint −92/−87/−86 = ZObjectInputStreamV2 标志位（触发第二 varint 读）
- 池后记录区 rec[0:0x30]（T02M03）含旋转四元数浮点：`eb 83 04 22 fc ff ff be e6 46 f7 a2 ...`

### 7.10 FST_File_Read（0x1804B3CB0）完整加载链

```
catalog 打开 .fst（sub_18050A5D0 读模式构造；_schemas 特例按 "%s%s/%s.bin" 磁盘回退）
→ sub_180517960 装载对象流（schema cache → varint 头表 → 外层条目表 → 双内存池
   （"MemoryWriteable" schema 标志分流）→ 逐对象反序列化 sub_180514090，
   digest 树 miss 时按 schema 名+哈希开外部资源，错误串 "Error: missing '%s/%s'"）
→ 根对象（+4=size, +8=data）用 bitrev 循环解析成 路径→RID 红黑树（a1+90）
→ 另开 pak.bin
```

### 7.11 待闭环（下一步）

1. 解压流顶层各分区（[0:16]/[16:20]/摘要/prelude/池/记录区/尾部）与 catalog
   目录/条目/对象流的对应关系——用 sub_18050D1E0 的目录格式在 T02M03 候选偏移
   （0x00/0x10/0x14/0x20）实测解析，验证 MD5 与 gzip2 payload
2. sub_180517960 的 92/87/86 次外层循环与实际字节矛盾
3. 对象帧（sub_180514090 读字段）逐字节格式

### 7.12 本轮 IDB rename（2026-09-18 已写回 player.dll.i64）

SchemaCache_Load(0x180511C50)、ZObjectInputStreamV2_LoadStream(0x180517960)、
CatalogEntry_Open(0x1805093B0)、Catalog_OpenByPath(0x180508D60)、
Catalog_ParseSections(0x18050D1E0)、Catalog_Write_Gzip2Md5(0x18050DED0)、
ObjStreamV2_RegisterSchemaDeps(0x1804B7740)。

## 8. 相关资产

- 原文件备份：`_packed/Environments/Scenes/Gamemaps/T01M08_Mill.fst.bak_20260910`
- 分析脚本（55 个，均在 `tmp/fst_*.py`，核心几个）：
  - `fst_nodes.py` / `fst_nodes2.py`——节点 manifest 解析、沙袋节点定位
  - `fst_kine.py`——簇表解析、kine 条目定位
  - `fst_all_runs.py`——全文件记录流/间隙扫描
  - `fst_sections.py`——记录流 vs 二进制间隙划分
  - `fst_find_matrix.py`——旋转正交矩阵候选扫描
  - `fst_layout.py`——按块统计 float 合法率/可打印率
- IDA 工具：`work/ida_tool.py`（本次新增 `findimm` 子命令）

## 9. 2026-09-18 最终更正与接力入口

> 本节优先级高于 §7.7～§7.11 的历史推测。FST 使用 `mode=2` 的专用
> `ZPerObjectFileCatalogOutputStreams`，不是普通 catalog sections；旧分段解释不要继续沿用。

### 9.1 已确认的最终布局

```text
disk: "FST\0" + zlib
zlib output: [MD5(payload) 16B][payload_size ULEB128][payload]
payload:
  [schema_digest 16B]
  [negative_group_count ULEB128][feature ULEB128]
  groups and contextual records
  cached object payloads
  final DFS reference-index stream
```

Mill 的最终引用流从解压流 `0x8BEE10` 开始，共 172,099 个 ULEB128；
35 个根，覆盖全部 51,255 条上下文记录。`sub_180517620` 证明 0 为 null，
非零值是一基扁平记录索引。记录中间的 `value` 是首次出现时的 DFS 展开跨度；
重复 `digest_B` 只消费一个引用值。

schema 731 的每个变换缓存项是 `[0x40][64B/16 float32]`；`0x40` 是 payload
长度，不是基类引用。最后一个 64B 本体结束处就是最终引用流起点。

### 9.2 已实现的可写闭环

实现位于 `server/tools/fst_inspect.py`：

- `parse_reference_graph()`：解析最终 DFS 引用流；
- `map_kinematics_property_sets()`：schema 438 Kinematics → schema 731；
- `bind_scene_nodes_to_transforms()`：schema 434 SceneNode → PropertySet → Transform；
- `patch_kinematics_transform()`：原位改一个已有实例的 TRS，更新 731/438 摘要、
  外层 MD5 并重新 zlib 压缩。

Mill 已绑定 28,401 条 SceneNode 上下文记录。回归测试真实移动一个沙袋，再完整解析输出，
引用图和节点绑定仍成立。命令：

```powershell
cd D:\HnG-test\work\hng-server-re\server
python -m pytest tests/test_fst_inspect.py tests/test_hng_unity_scene.py -q
```

## 10. 2026-09-19 最新运行时结论

`tools/fst_inspect.py::insert_cloned_instance()` 生成的父子双向链接版本已被战斗服完整加载。
成功产物为 `D:\HnG-test\work\T01M08_Mill.clone-parent-linked.fst`，部署后地图在 1.71 秒内加载完成，
成功创建 battlefield 并初始化 AI，持续运行约 45 秒无异常，随后人工停止测试进程。

关键更正与根因：

- `ZObjectInputStreamV2_LoadStream` 对连续同 digest_A 记录分配不同运行时对象，通过标量复制和成员深拷贝初始化；它们不是同一对象身份。
- schema 434 相同 digest_A 的上下文记录必须连续，否则 loader 的批处理假设会被破坏。
- 一个模型实例拥有 7 个不能共享的 SceneNode：root、`_Coli`、collision mesh、四个 render LOD。
- 仅克隆这 7 个节点仍会在 `player.dll+0x80180` 崩溃，因为 parent/children 是双向序列化关系。
- 新 root 指向 `_ArmyProps` 后，还必须让 `_ArmyProps` 的 children 数组包含新 root，并把缓存 payload 的 child count 从 31 改为 32。
- `0x180080180` 是 `scenenodearray.cpp` 的显式 `__debugbreak()`：PostLoad 删除 unused node 时，在所属 SceneNode 数组中找不到它。父节点计数未同步正好制造了该不一致。

自动回归当前为 `7 passed`。本文后面的旧崩溃假设仅作调查历史保留。

客户端实机随后确认新增沙袋可见，运行时闭环正式验收。为进行原版对比，客户端与服务端当前均已
恢复官方 Mill（MD5 `cc51d18266a56c03b7d85566af786931`）；成功克隆产物仍保存在 work 目录。

### 10.1 Unity 序列化场景坐标实证

用 Unity 场景全部 12 个 `FenceSandbagA_BLowStraight_4m` 对齐 FST 的 12 个逻辑
`SandbagWallA_4m_lowA`，12/12 唯一匹配，得到：位置 `(10x,10y,-10z)`、四元数
`(-x,-y,z,w)`、缩放不变。最大位置误差 `<0.008`，最大四元数误差 `<2.2e-5`。
旧文档引用 `PrefabSpawn.cs` 得出的轴交换公式作废。

`hng_unity_fst_link.py` 已生成 `D:\HnG-test\work\T01M08_ColmarHamlet.fst-link.json`，
用 Unity `instance_file_id` 保存稳定身份。6220 个实例中，Bush/Grass/Tree 分别占
2992/2518/317，它们走优化植被或外置对象路径；主 FST 普通模型链接必须与这些资源分开实现。

`hng_unity_fst_compile.py` 已实现基于冻结身份清单的差异计划。官方基线场景得到严格零差异，
输出为 `D:\HnG-test\work\T01M08_ColmarHamlet.fst-plan.json`。该阶段只分类并审计变更，尚未把批量
update/add 写入 FST；delete/reparent 会被列为阻塞项，避免破坏双向 SceneNode 层级。

当时结果：`4 passed`；最新结果见紧随其后的第 10 节（`6 passed` 且战斗服验收成功）。

### 9.3 摘要传播的重要限制

以下公式已全量成立：

```text
schema731_B = MD5(LE32(731) + transform_digest_A)
schema438_B = MD5(LE32(438) + property_digest_A + schema731_B)
```

但不能把所有序列化引用都当作 `sub_18050E610` 的哈希依赖。Mill 全记录用
`MD5(schema_id + digest_A + 全部直接引用 digest_B)` 验证时，仅 16,177 条命中、
35,078 条不命中。schema 434 中只有 7/28,401 命中；schema 438 有
5,748/5,750 命中；schema 731 为 2,365/2,365。

因此当前保守变换回写只更新已证明的 731 与对应 438 摘要，保持 SceneNode 上下文键、
记录序号和引用索引不变。不要擅自对 SceneNode 做“全子引用摘要传播”。

### 9.4 当前真正卡点

Unity 前端 `server/tools/hng_unity_scene.py` 可解析
`D:\hng-map\Project` 的 PrefabInstance，并完成 Unity→HnG 坐标转换；Mill 对应场景
可导出 6,220 个实例。但 Unity 对象没有持久保存 FST record index / `digest_B`，而显示名和
局部坐标都不足以唯一、安全地关联模板 FST。抽样 Unity 沙袋坐标也不能直接命中
`T01M08_Mill.fst` 的变换缓存。

下一位模型应优先恢复原导出器的实例身份来源，或给 Unity 场景补写稳定的 FST 身份元数据，
再做批量同步。不要先按最近坐标或同名顺序匹配；那会静默改错对象。新增/删除实例、mesh、LOD、
材质、碰撞、terrain/heightfield 与 `_objects` 外置资源仍未实现，游戏运行时也尚未验收。
