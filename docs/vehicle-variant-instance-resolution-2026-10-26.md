# 载具迷彩实例选择失败：2026-10-26

状态：本地修复和回归测试完成，未发布或重启线上服务。线上只读查日志及库存，无玩家数据变更。

## 已确认根因

客户端 VehicleUtilities.updateVehicleVariantObject 的已拥有分支调用 getVariantId(variantAvailable)。该方法只按 vehicleVariantTemplateId 返回账号内第一条实例，没有限定 vehicleId 或 characterId。isVehicleVariantOwned 却按当前角色和载具判断已拥有，导致当前车已购买同款皮肤，装备请求仍提交另一辆车的实例。

服务端 SetVehicleVariantRequest 按实例归属校验，正确拒绝另一辆车的已绑定实例。其他皮肤没有账号内多份实例时，不触发这个冲突。

反编译证据：../decompiled/swf-frontend/scripts/dk/reto/heroesandgenerals/util/VehicleUtilities.as（相对本项目需再上一级）；updateVehicleVariantObject、getVariantId、isVehicleVariantOwned。运行时请求和数据库记录支持该路径。

## 线上证据

来源：logs/realtest_game.log 当前日期文件（2026-10-26），快照保存在 ../tmp/r75-online-variants.log。

共60条 SetVehicleVariantResponse rejected，涉及3组：

| 请求载具 | 客户端提交的皮肤实例 | 实际绑定车辆 | 目标车辆已拥有的同款实例 | 皮肤模板 |
|---|---|---|---|---|
| 1748 | 1266 | 1699 | 1272 | 441 R75(Ambush DB) |
| 1754 | 1266 | 1699 | 1276 | 441 R75(Ambush DB) |
| 1757 | 1278 | 1756 | 1279 | 412 Kübelwagen Typ 82 (Grey and Brown) |

这不是仅R75的特例。日志中R75自14:22起被拒，库贝尔桶车15:27–15:28也连续被拒。

库存以数据库只读事务核对。上述记录均属于同一玩家，R75车辆型号模板440与皮肤441同parent=99。库贝尔车辆模板2与皮肤412同parent=2。

## 修复边界

handlers/vehicles.py：保留原有直接装备和仓库皮肤绑定路径。当客户端提交的实例属于当前玩家但绑定另一辆车时，仅在当前载具确实已有同模板皮肤、且皮肤与车辆同父模板的情况下，解析成当前车自己的实例。记录 requested/resolved/vehicle/template，持久化及Vehicle KVCS推送均使用解析后的实例。

不转移另一辆车的皮肤，不给未购买者授予皮肤，不修改静态数据或现有库存。适用于全部载具迷彩。

## 验证

- 新增tests/test_vehicle_variant_instance_resolution.py：复现两辆R75及库贝尔失败请求；确认推送和持久化使用本车实例、另一车皮肤保持原样；验证缺少购买、其他涂装、其他车辆、缺少请求实例、缺少目标车辆、不同父模板的拒绝边界；直接装备与卸下正常。
- pytest新测试和test_battlepass_skin_unlock.py：18 passed。
- 载具维修、购买定价和入库测试：45 passed。
- 原有test_battlepass_skin_unlock.py独立运行：PASS=49 FAIL=0。

尚未进行线上发布后的玩家实机验证。
