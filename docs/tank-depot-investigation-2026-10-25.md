# 坦克无法入库排查

最新状态：修复已于2026-10-26 00:23（香港时间）部署。发布与服务健康检查见deployment-2026-10-26.md；下文未部署说明为历史记录。

2026-10-25，仅排查。通过 SSH 只读查询线上日志及源码；未改业务代码、未部署、未重启或修改线上数据。

## 结论

确实存在服务端缺陷，可导致坦克及其他载具无法送入账户仓库。已找到线上 M26 Pershing 失败记录，并用真实 handler 和真实 WarDatabase 定价在离线隔离进程复现。

## 线上证据

远端目录：C:/Users/Administrator/Desktop/HngServer/server。

logs/realtest_game.log：

- 221046–221048，22:24:07：itemType=2、objectId=1653、pricingId=4520、tab=v17、isFree=False；紧接 rejected pricing 4520: target/type mismatch 和 response=56 not_enough、cost=0。
- 22:24:14、22:24:37、22:24:42、22:27:32 同一载具重复失败。
- WarDatabase Pricing 4520 的 type=vehicletemplate、object_id=28；vehicletemplate 28=M26 Pershing。其 credits=291000、gold=3380。
- 14073 等，01:57:00–01:57:27：objectId=1605、tab=v20、pricingId=0、isFree=True 重试四次。
- 125277，19:09:49：objectId=1587、tab=v17、pricingId=0、isFree=True。

此外 10-24 日志有大量免费入库重试，以及付费载具入库的定价不匹配；影响也包括摩托车、卡车、飞机，非坦克独有。

完整摘录保存在 ../tmp/investigate_tank_depot_remote.log，包含少量相邻日志，仅供本地排查。

## 两处根因

1. handlers/characters.py:780–791 把 itemType=2 的定价类型限定为 VehicleVariantTemplate。真实整车定价使用 vehicletemplate，校验必然失败；返回码却映射到余额不足 56/57，容易误导玩家。整车定价对象还应按车辆变体的 parentVehicleTemplateId 校验，不能仅替换类型字符串就宣告修复。
2. 同文件 774–778 无条件拒绝已归属物品的 pricingId<=0，未处理合法免费入库。客户端 ItemInfoOverlayMediator.as:1003–1027 在 isFree 时明确不发送 pricingId。服务端解析后为 0，返回 response=0，且该早退无响应日志；客户端只处理成功及余额不足，因此可能表现为点击无效果。

线上源码已只读确认有相同逻辑。sd_is_free 目前只用于打印日志。

## 离线复现

脚本：../tmp/reproduce_tank_depot.py。

执行：hng-server-re/.venv/Scripts/python.exe -X utf8 server/tmp/reproduce_tank_depot.py（cwd=hng-server-re）。

调用真实 handle_senditemtodepotrequest 和真实 _charge_player；仅 STORE 替换为记录调用的内存对象，不连接数据库。

- 真实定价4520、物品模板28、余额扣款桩设为充足：response=56，spend_currency 和 send_item_to_depot 均未调用。
- 免费入库、pricingId=0：response=0，同样未调用扣款和移动操作。
- 两项断言通过，退出码0。

没有客户端实机操作；免费请求当次是否都具备合法免费资格尚未逐件查询数据库。不能直接信任客户端 isFree 来豁免付费。

## 修复方向

整车按 parent vehicletemplate 校验价格；皮肤仍使用 VehicleVariantTemplate。免费资格应根据服务端物品、定价、角色解锁及客户端规则计算，再决定是否需要 pricingId 与扣款。补充付费/免费、真实变体与父模板不同、伪造免费/价格、已入库重试等回归验证；定价错误与余额不足应分开返回并记录。

## 本地修复与回归

已修改 handlers/characters.py 和 wardb.py；未部署或重启线上服务。

- 整车使用已知 VehicleVariantTemplate 的父 vehicletemplate 定价；未知变体拒绝。皮肤继续按自身 VehicleVariantTemplate 定价。
- 免费载具入库由服务端查询本人载具及所属角色，再核对兵种、阵营、available/unlockable 静态规则；与客户端 canCharacterUnlockItem(checkUnlockable=true) 一致，该规则不依赖当前勋带 XP。不能兼容的本人载具可免费退仓；没有定价的兼容载具仅未装备时可免费退仓。未知模板、静态数据缺失或查询失败均不授予免费资格。
- 不信任 isFree 标记；伪造免费请求返回239。不存在或无法读取的物品不扣款、不进入移动步骤。价格不匹配单独返回252并记录日志；余额不足仍按币种返回56/153/57。
- 保留成功后 Vehicle、挂靠弹药/升级/皮肤、角色和仓库新物品通知推送；保留重复入库不再收费及持久化失败退款。

新增 tests/test_vehicle_depot_transfer.py，含真实 handler、真实扣款逻辑、真实 KVCS 序列化和 WarDatabase 定价验证。覆盖付费/免费、M26历史价格4520、其他坦克及飞机、父模板与变体不同、同编号、未知变体、皮肤独立定价、余额不足、伪造价格/免费、越权实例、查询异常、入库重试、退款、Vehicle归属及槽位字段、挂靠件解绑推送。

完整离线回归：

```
cd server
../.venv/Scripts/python.exe -X utf8 -m pytest tests/test_vehicle_depot_transfer.py tests/test_non_rts_security_regressions.py tests/test_vehicle_purchase_pricing.py tests/test_vehicle_ammo_slots.py tests/test_vehicle_maintenance_regression.py -q
89 passed
```

修改的Python文件通过py_compile。tmp/check_depot_negative_control.py在独立进程内将价格类型恢复错误并关闭免费资格计算，4项成功场景全部失败，证明新增测试能检测这两处逻辑被禁用。未修改磁盘业务文件。此前 tmp/reproduce_tank_depot.py 的输出是修复前历史证据；修复后验证使用新增pytest文件。

验证边界：无线上数据库写入或客户端实机操作；持久化接口在新测试中使用内存替身，未执行需连接数据库的旧test_depot_fixes.py/test_depot_ammo.py。
