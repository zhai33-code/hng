# 开局倒计时覆盖未生效：2026-10-13

## 结论

`MM_COMBAT_START_COUNTDOWN_OVERRIDE=1` 已传到战斗服命令行，但当前
`combat-server/globals.dll` 将 `controllers.overrideStartCountdown` 注册为
带 `cheat,server` 标签的布尔选项。运行中的专用服 `AllowCheats=0`，因此
赋值被拒绝，覆盖字段保持 0。仅修改 Python 默认值不能解决此问题。

## 全链路证据

1. `mm_config.py` 默认值为 1，`combat_instances.py` 在非零时传入该值；
   本机 `_combat_spawn_args` 和远端 `combat_agent.build_launch_args` 都构造
   `/R controllers.overrideStartCountdown=1`。
2. `combat-server/hng_boot.log` 在 2026-10-13 13:59:30 UTC 的
   session-604345 启动记录中确认该参数为 1；地图加载用时 1.85 秒。
3. 后续新战局的运行时只读检查（hng.exe PID 2672）：
   加载 DLL 路径为 `D:\HnG-test\combat-server\globals.dll`，
   GetOptions 对象的 `+0x2C4` 为 0，`+0x8E2`（AllowCheats）为 0。
4. globals.dll 元数据：descriptor RVA `0x7F940`，字段类型
   `0x1FFFFFE5`（布尔），controllers 内偏移 `0xDC`，根偏移
   `0x1E8+0xDC=0x2C4`；标签表 RVA `0x85160` 包含
   `cheat`、`server` 和 `default=false`。
5. 独立进程加载实际 DLL，调用导出的 `IniFile::Read`，输入
   `[controllers]\noverrideStartCountdown=1`：AllowCheats=0 时字段仍为 0；
   仅在该测试进程中将 AllowCheats 设为 1 后，字段变为 1。
6. player.dll 在 VA `0x1802A2D09` 读取该布尔字段，开启时将截止 tick
   限制为 `min(原截止 tick, 当前 tick+300)`。日志确认 60Hz，约为 5 秒。
   CreateInstance 在 `0x1802A4796` 写入的 7200 是 tick（120 秒），不是秒。

## 已准备的定点修复候选

文件：`D:\HnG-test\combat-server\globals.countdown-fix.dll`。
原运行 DLL 未替换，未修改运行中战局内存。

仅将倒计时 descriptor 的标签表指针从 RVA `0x85160` 改为 `0x85170`，
跳过该选项的 `cheat` 标签，保留 `server` 和 `default=false`；
文件偏移 `0x7E358` 的字节从 `60` 改为 `70`，全文件仅 1 字节变化。
没有全局启用 AllowCheats，也没有修改 player.dll 的倒计时逻辑。

独立原生 DLL 验证结果：

| DLL | AllowCheats | overrideStartCountdown=1 后 | forceSpectate=1 后（对照） |
| --- | --- | --- | --- |
| 原版 | 0 | 0 | 0 |
| 修复候选 | 0 | 1 | 0 |

原文件 SHA-256：
`9f1f4cbd7d23df8defcbb93a9342c28f20651de2a8dd3d46f6e9c30ab3165133`

候选 SHA-256：
`f29b41cf96a32c043fae43cae49dcfccb75f9e9ec8a6609e1de359eeaec6a68f`

这是原生选项赋值验证，尚未部署候选 DLL 进行新战局的端到端计时验证。
部署须在相关战斗服进程退出后备份并替换 `combat-server/globals.dll`，
随后创建新战局验证；客户端 DLL 不需要替换。
