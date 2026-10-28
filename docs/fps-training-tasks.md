# FPS 训练任务默认完成（2026-10-13）

用户要求 FPS 左上角任务列表默认全部做完，不自动发奖励。

该面板与大厅生涯军令、教程弹窗是不同数据链路：

- `PlayerSettings.f28 achievementsUnlocked`：FPS 的 AchievementTemplate 任务。
- `PlayerSettings.f37 careerOrderStatus`：生涯军令。
- `PlayerSettings.f38 triggeredTutorialTemplateIds`：已触发教程弹窗。

证据：

1. `work/wardump_out/01_desc_all.txt` 的 PlayerSettings 描述符确认 f28 为重复
   8 字节数值，字段名 achievementsUnlocked。
2. 反编译 `_packed/_Out/Flash/Ingame_Hud/TopLeft.swf`，面板由
   SetAchievementOverview、SetAchievementTask、SetAchievementProgress 驱动。
3. 解压 `_packed/_Out/gamedb.dat`（DAT 标识后的 zlib 数据）后，
   AchievementTemplate 数据位于解压后约 0xc8003 至 0xc8a2c，模板 ID 1–31。
   其中 1/10/15/24 分别是 Combat Training 1、Tactical Training 1、
   Combat Training 2、Tactical Training 2，其余包含移动、射击、装填、占点等任务。
4. 22:54:25 的实测日志已下发生涯军令全部完成，FPS 面板仍然显示，
   因此此前仅修改 f37 的方案无效；现已撤回这组改动。

实现：默认通过 f28 下发 1–31，声明 FPS 任务已解锁；f37 恢复原默认空串，
f38 保持教程兼容触发集合。没有修改数据库或奖励领取逻辑。
新日志包含 `field28(achievementsUnlocked)=31`。

协议构建测试通过，面板是否隐藏仍需重启服务端并重新进局确认。
升级客户端 gamedb 时应重新核对任务 ID；不要把 TutorialTemplate 或
CareerOrderTemplate 的 ID 当成 AchievementTemplate 的 ID。
