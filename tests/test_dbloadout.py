# -*- coding: utf-8 -*-
"""f12 装备 DB 化自测（test_dbloadout.py）—— 2026-08-20 全动态版。

回放路径（pvt_player_settings.bin 打底 / 装备模板克隆 / _patch_player_settings）
已从 game_server.py 移除，本测试验证：
  1. _msg_character_entry loadout 路径：武器/载具/徽章/军衔/军服/杂项全 DB 构建，
     wire 类型正确（耐久字段 fixed32）；
  2. 未装备武器自动补最低空闲槽位；
  3. 无 DB 装备角色（Alexei/Hans）→ 空装备条目（f3/f4 整体省略），
     不再有任何回放兜底；
  4. _msg_player_settings 整帧全动态构建可解析、DB 武器出现在 f12、f999 token 注入；
  5. 勋带进度 Ribbon 构造（等级/下级阈值）；
  6. loadout=None（DB 聚合失败）→ 空装备，绝不克隆。

2026-08-21 模块化：消息构建从 game_server 迁至 warserver_messages，
军衔推导迁至 wardb，pb 助手迁至 protocol —— 本测试同步换用新导入。
"""
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from protocol import pb_field_payload  # noqa: E402
from wardb import _rank_from_combat_xp  # noqa: E402
from warserver_messages import (  # noqa: E402
    _msg_character_entry,
    _msg_player_settings,
    _msg_ribbon_progress,
)


def rd_varint(buf, off):
    val = shift = 0
    while True:
        b = buf[off]
        off += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, off
        shift += 7


def parse(data):
    """[(field, wiretype, value)]，wt5 解为 float。"""
    out = []
    i, n = 0, len(data)
    while i < n:
        key, i = rd_varint(data, i)
        fn, wt = key >> 3, key & 7
        if wt == 0:
            v, i = rd_varint(data, i)
        elif wt == 2:
            ln, i = rd_varint(data, i)
            v = data[i:i + ln]
            i += ln
        elif wt == 5:
            v = struct.unpack('<f', data[i:i + 4])[0]
            i += 4
        elif wt == 1:
            v = data[i:i + 8]
            i += 8
        else:
            raise ValueError(wt)
        out.append((fn, wt, v))
    return out


def fields(data):
    """{field: [value,...]}（repeated 保留列表）。"""
    d = {}
    for fn, wt, v in parse(data):
        d.setdefault(fn, []).append(v)
    return d


def main():
    ok = True

    def check(desc, cond):
        nonlocal ok
        ok = ok and cond
        print(f'  {"PASS" if cond else "FAIL"}  {desc}')

    # ---- 模拟 DoubleBird 库内数据（pid=1：Alexei/Hans/Dmitri）----
    loadout = {
        'weapons': {1: [{'id': 118, 'name': 'PPSh-41', 'template_id': 5,
                         'character_id': 1, 'container_id': 1, 'container_slot': 2,
                         'variant_template_id': 0, 'variant_id': 0,
                         'condition': 1.0, 'clip_count': -4},
                    {'id': 119, 'name': 'Mosin-Nagant', 'template_id': 8,
                     'character_id': 1, 'container_id': 1, 'container_slot': 0,
                     'variant_template_id': 0, 'variant_id': 0,
                     'condition': 1.0, 'clip_count': 0}],
                    3: [{'id': 123, 'name': 'PPD-40', 'template_id': 24,
                         'character_id': 3, 'container_id': 3, 'container_slot': 1,
                         'variant_template_id': 0, 'variant_id': 0,
                         'condition': 1.0, 'clip_count': 1}]},
        'w_ammo': {118: [{'id': 312, 'ammo_template_id': 46, 'weapon_id': 118,
                          'slot_index': 1, 'condition': 1000.0}],
                   123: [{'id': 317, 'ammo_template_id': 92, 'weapon_id': 123,
                          'slot_index': 1, 'condition': 1000.0}]},
        'w_mods': {123: [{'id': 201, 'template_id': 50, 'weapon_id': 123,
                          'condition': 1.0}]},
        'vehicles': {3: [{'id': 409, 'vehicle_variant_template_id': 7,
                          'character_id': 3, 'equipped_in_slot': 1, 'variant_id': 0,
                          'condition': 100.0},
                    {'id': 410, 'vehicle_variant_template_id': 26,
                     'character_id': 3, 'equipped_in_slot': 0, 'variant_id': 0,
                     'condition': 100.0}]},
        'v_ups': {409: [{'id': 501, 'upgrade_template_id': 11, 'vehicle_id': 409,
                         'equipped_slot_id': 1, 'condition': 1.0},
                        {'id': 502, 'upgrade_template_id': 12, 'vehicle_id': 409,
                         'equipped_slot_id': 0, 'condition': 1.0}]},
        'v_ammo': {409: [{'id': 603, 'ammo_template_id': 93, 'vehicle_id': 409,
                          'equipped_storage_id': 71, 'slot_index': 1,
                          'condition': 2000.0, 'spawn_clip_count': 3}]},
        'uv_tmpl': {701: 1},
        'u_parts': {3: [90, 94]},
        'up_tmpl': {902: 90, 903: 94},
    }
    chars = [
        {'id': 1, 'name': 'Alexei', 'faction': 3, 'template': 1, 'combat_xp': 0,
         'combat_badge_0': 0, 'combat_badge_1': 0, 'uniform_variant_id': 0,
         'headgear_id': 0, 'squad_points': 0, 'receive_daily_bonus': True},
        {'id': 2, 'name': 'Hans', 'faction': 2, 'template': 1, 'combat_xp': 250000,
         'combat_badge_0': 0, 'combat_badge_1': 0, 'uniform_variant_id': 0,
         'headgear_id': 0, 'squad_points': 0, 'receive_daily_bonus': True},
        {'id': 3, 'name': 'Dmitri', 'faction': 3, 'template': 1, 'combat_xp': 50000,
         'combat_badge_0': 110, 'combat_badge_1': 0, 'uniform_variant_id': 701,
         'headgear_id': 902, 'squad_points': 8, 'receive_daily_bonus': False},
    ]

    print('== 1. Dmitri（全装备 DB 构建）==')
    entry = _msg_character_entry(chars[2], loadout=loadout)
    e = fields(pb_field_payload(entry))
    check('f1 id=3 / f2 name=Dmitri', e[1][0] == 3 and e[2][0] == b'Dmitri')
    w = fields(e[3][0])
    # PPD-40 模板24 T=2：clip_count=1（最大档）→ 出生弹夹 2T-1=3
    check('武器 f1=123 f2=24 f7 spawnClips=3(clip_count=1最大档) f8 slot=1 f11 name=PPD-40',
          w[1][0] == 123 and w[2][0] == 24 and w[7][0] == 3
          and w[8][0] == 1 and w[11][0] == b'PPD-40')
    am = fields(w[3][0])
    check('弹药 f1=317 f2=92 f3 耐久=1000.0 (fixed32)',
          am[1][0] == 317 and am[2][0] == 92 and abs(am[3][0] - 1000.0) < 1e-3)
    w_raw = parse(e[3][0])
    f9wt = [wtv for fn, wtv, v in w_raw if fn == 9][0]
    f9val = [v for fn, wtv, v in w_raw if fn == 9][0]
    check('武器 f9 耐久 wiretype=5(fixed32) 值=1.0',
          f9wt == 5 and abs(f9val - 1.0) < 1e-6)
    md = fields(w[4][0])
    check('配件 f1=201 f2=50', md[1][0] == 201 and md[2][0] == 50)
    v = fields(e[4][0])
    check('f4 载具条目数=1（未装备的 410 不上阵）', len(e[4]) == 1)
    check('载具 f1=409 f2=7 f4 equippedInSlot=1',
          v[1][0] == 409 and v[2][0] == 7 and v[4][0] == 1)
    v_raw = parse(e[4][0])
    f3wt = [wtv for fn, wtv, _ in v_raw if fn == 3][0]
    check('载具 f3 耐久 wiretype=5 值=100.0',
          f3wt == 5 and abs(v[3][0] - 100.0) < 1e-3)
    vu = fields(v[5][0])
    # f2 是 VehicleUpgradeSlot 表 id（equipped_slot_id）而非升级模板 id
    # （2026-08-31 hng_boot.log 实证：客户端按 f2 查槽位表反解模板，
    # 误发模板 id 会撞同 id 行解析错模板且挂载失败）
    check('载具升级 f5 只含已安装的 501（slot=0 的 502 不下发），'
          'f2=槽位表 id=1',
          len(v[5]) == 1 and vu[1][0] == 501 and vu[2][0] == 1)
    va = fields(v[6][0])
    check('载具弹药 f2仓=71 f3槽=1 f4模板=93 f6出生弹夹=3(VehicleAmmoStorage.f2)',
          va[2][0] == 71 and va[3][0] == 1 and va[4][0] == 93
          and abs(va[6][0] - 3.0) < 1e-6)
    check('f6 战斗徽章=[110]', e[6] == [110])
    rank = _rank_from_combat_xp(50000)
    check(f'f7=兵种actionid(2,rifleman) / f8=军衔({rank})（combat_xp 推导）',
          e[7][0] == 2 and e[8][0] == rank)
    check('f9 军服迷彩模板=1（uniform_variant_id 701 → uv_tmpl）',
          e[9][0] == 1)
    check('f10 军装部件=[90]（只发 headgear_id=902 已装备头盔，'
          '未装备的 94 不下发）', e[10] == [90])
    check('f11 squadPoints=8 / f12 receiveDailyBonus=0',
          e[11][0] == 8 and e[12][0] == 0)

    print('== 2. Alexei（已装备槽2下发，未装备武器省略）==')
    entry = _msg_character_entry(chars[0], loadout=loadout)
    e = fields(pb_field_payload(entry))
    # 库里 2 把武器：118 已装备槽2、119 未装备 → f3 只含 118
    check('f3 武器条目数=1（未装备的 119 不上阵）', len(e[3]) == 1)
    w = fields(e[3][0])
    check('f1=118（已装备武器）/ f8 equipmentSlot=2（真实槽位）',
          w[1][0] == 118 and w[8][0] == 2)
    # PPSh-41 模板5 T=5：clip_count=-4（最少档）→ 出生弹夹 1
    check('f7 spawnClips=1（clip_count=-4最少档）', w[7][0] == 1)
    check('无徽章 → f6 缺省', 6 not in e)
    check('f9 默认军服变体=28（SU 步兵 Red Army，无实例时兜底）', e[9][0] == 28)
    check('f11 squadPoints=0 恒发（官方镜像）', e[11] == [0])
    w_raw = parse(e[3][0])
    check('武器 f5=0 / f6 空消息（官方恒发镜像）',
          any(fn == 5 and v == 0 for fn, _, v in w_raw)
          and any(fn == 6 and v == b'' for fn, _, v in w_raw))

    print('== 3. Hans（无 DB 装备 → 空装备条目，无回放兜底）==')
    entry = _msg_character_entry(chars[1], loadout=loadout)
    e = fields(pb_field_payload(entry))
    check('f1=2 / f2=Hans（条目仍存在）',
          e[1][0] == 2 and e[2][0] == b'Hans')
    check('f3 武器条目数=0（无武器则整体省略）', 3 not in e)
    check('f4 载具条目数=0（无载具则整体省略）', 4 not in e)
    check('f9 默认军服变体=21（GE 步兵 Field Grey 41）', e[9][0] == 21)
    rank_h = _rank_from_combat_xp(250000)
    check(f'f7=兵种actionid(2,rifleman) / f8=军衔({rank_h})（xp 推导仍正常）',
          e[7][0] == 2 and e[8][0] == rank_h)

    print('== 4. 整帧 _msg_player_settings（全动态构建）==')
    full = _msg_player_settings(
        player_id=1, pms_id=77, char_id=3,
        eos_token=b'fake-eos-token-bytes',
        display_name='DoubleBird', metrics_uid='uid-xyz',
        metrics_session_id=42, mission_session_id=43,
        squad_id=111, faction=3, db_chars=chars, loadout=loadout,
        quickplay=True, first_action_game=False)
    f = fields(full)
    check('f1 player_id=1 / f2 gamertag=DoubleBird',
          f[1][0] == 1 and f[2][0] == b'DoubleBird')
    check('f7 faction=3', f[7][0] == 3)
    check('f12 条目数=2（SU 局只发 SU 角色，GE 的 Hans 被过滤）', len(f[12]) == 2)
    a_e = fields(f[12][0])          # Alexei = 第 1 条
    check('f12[0]=Alexei(id=1)（SU 角色保留）',
          a_e[1][0] == 1 and a_e[2][0] == b'Alexei')
    d_e = fields(f[12][1])          # Dmitri = 第 2 条
    check('整帧 Dmitri 武器来自 DB（f2 模板=24）',
          fields(d_e[3][0])[2][0] == 24)
    check('整帧 Dmitri f9=1（DB 军服变体）', d_e[9][0] == 1)
    check('f15 missionCharacterId=3', f[15][0] == 3)
    check('f16 squadId=111', f[16][0] == 111)
    check('f17 squadSlotIndex=0', f[17][0] == 0)
    check('f22 metricsUid=uid-xyz / f23=42 / f25=43',
          f[22][0] == b'uid-xyz' and f[23][0] == 42 and f[25][0] == 43)
    check('f30 isQuickPlay=1', f[30][0] == 1)
    check('f999 eos token 注入', f[999][0] == b'fake-eos-token-bytes')
    check('f1000 expires_in=300', f[1000][0] == 300)
    check('整帧可线性解析（无损坏帧）', len(full) > 300)

    print('== 5. 勋带进度 Ribbon ==')
    rb = _msg_ribbon_progress(5, 184500.0)
    rf = fields(rb)
    check(f'Ribbon f1=5 f2=184500 f3={rf[3][0]} f4={rf[4][0]}（阈值推导）',
          rf[1][0] == 5 and rf[2][0] == 184500 and rf[3][0] >= 0
          and rf[4][0] >= rf[2][0])

    print('== 6. loadout=None（DB 聚合失败 → 空装备，绝不克隆）==')
    entry = _msg_character_entry(chars[0], loadout=None)
    e = fields(pb_field_payload(entry))
    check('f1=1 f2=Alexei（条目仍存在）',
          e[1][0] == 1 and e[2][0] == b'Alexei')
    check('f3/f4 空装备（无回放克隆）', 3 not in e and 4 not in e)
    check('f9 默认军服=28（loadout=None 仍兜底）', e[9][0] == 28)

    print('== 7. 飞行员 f7=actionid(11)（部署兵种识别，回放 Yaroslav 锚点）==')
    pilot = {'id': 126, 'name': 'Yaroslav', 'faction': 3, 'template': 2,
             'combat_xp': 60000, 'combat_badge_0': 321, 'combat_badge_1': 0,
             'uniform_variant_id': 0, 'squad_points': 4,
             'receive_daily_bonus': False}
    pilot_loadout = {
        'weapons': {}, 'w_ammo': {}, 'w_mods': {},
        'vehicles': {126: [
            {'id': 900, 'vehicle_variant_template_id': 206,
             'character_id': 126, 'equipped_in_slot': 1, 'variant_id': 0,
             'condition': 100.0},
            {'id': 901, 'vehicle_variant_template_id': 57,
             'character_id': 126, 'equipped_in_slot': 2, 'variant_id': 0,
             'condition': 100.0}]},
        'v_ups': {}, 'v_ammo': {}, 'uv_tmpl': {}, 'u_parts': {}}
    entry = _msg_character_entry(pilot, loadout=pilot_loadout)
    e = fields(pb_field_payload(entry))
    check('f7=11（fighterpilot actionid → 游戏端空军部署）', e[7][0] == 11)
    check('f8=军衔（combat_xp 推导）', e[8][0] == _rank_from_combat_xp(60000))
    check('f4 载具条目数=2（两架飞机已装备）', len(e[4]) == 2)
    v0 = fields(e[4][0])
    v1 = fields(e[4][1])
    check('载具 vvt=206(R-Z recon)/57(La-7 fighter) 槽位 1/2',
          v0[2][0] == 206 and v0[4][0] == 1
          and v1[2][0] == 57 and v1[4][0] == 2)
    check('f9 默认军服=32（SU 飞行员）', e[9][0] == 32)
    # 坦克兵对照：actionid=10
    tanker = {'id': 130, 'name': 'Arkady', 'faction': 3, 'template': 5,
              'combat_xp': 0, 'combat_badge_0': 0, 'combat_badge_1': 0,
              'uniform_variant_id': 0, 'squad_points': 0,
              'receive_daily_bonus': True}
    entry = _msg_character_entry(tanker, loadout=None)
    e = fields(pb_field_payload(entry))
    check('坦克兵 f7=10（tankcrew actionid）', e[7][0] == 10)

    print('== 8. 武器弹药过滤（slot_index>0 才下发，与官方回放一致）==')
    # 用户改子弹场景：PPD-40(weapon 123) 持有 2 条弹药
    #   - 旧弹药 317(slot=0，已卸下) + 新弹药 318(slot=1，已装备)
    # 官方回放 weapon f3 ammos 子消息无 equippedAmmoSlotIndex 字段，
    # 客户端按"列表中所有 ammo 均视为已装备"处理 → 必须只下发已装备的那一条
    swap_loadout = {
        'weapons': {3: [{'id': 123, 'name': 'PPD-40', 'template_id': 24,
                         'character_id': 3, 'container_id': 3, 'container_slot': 1,
                         'variant_template_id': 0, 'variant_id': 0,
                         'condition': 1.0, 'clip_count': 1}]},
        'w_ammo': {123: [
            {'id': 317, 'ammo_template_id': 92, 'weapon_id': 123,
             'slot_index': 0, 'condition': 1000.0},   # 旧弹药，已卸下
            {'id': 318, 'ammo_template_id': 93, 'weapon_id': 123,
             'slot_index': 1, 'condition': 1000.0}]},  # 新弹药，已装备
        'w_mods': {}, 'vehicles': {}, 'v_ups': {}, 'v_ammo': {},
        'uv_tmpl': {}, 'u_parts': {},
    }
    entry = _msg_character_entry(chars[2], loadout=swap_loadout)
    e = fields(pb_field_payload(entry))
    w = fields(e[3][0])
    check('f3 ammos 只下发已装备的 1 条（slot_index>0 过滤）',
          len(w[3]) == 1)
    am = fields(w[3][0])
    check('下发的是新弹药 id=318 template=93（用户改后已装备的那条）',
          am[1][0] == 318 and am[2][0] == 93)
    # 全部 slot=0 时整体省略（repeated 空字段整体省略）
    noeq_loadout = {
        'weapons': {3: [{'id': 123, 'name': 'PPD-40', 'template_id': 24,
                         'character_id': 3, 'container_id': 3, 'container_slot': 1,
                         'variant_template_id': 0, 'variant_id': 0,
                         'condition': 1.0, 'clip_count': 1}]},
        'w_ammo': {123: [{'id': 317, 'ammo_template_id': 92, 'weapon_id': 123,
                         'slot_index': 0, 'condition': 1000.0}]},
        'w_mods': {}, 'vehicles': {}, 'v_ups': {}, 'v_ammo': {},
        'uv_tmpl': {}, 'u_parts': {},
    }
    entry = _msg_character_entry(chars[2], loadout=noeq_loadout)
    e = fields(pb_field_payload(entry))
    w = fields(e[3][0])
    check('全部弹药 slot=0 → f3 ammos 整体省略（无已装备弹药）',
          3 not in w)

    print('== 9. 载具迷彩下发（VehicleSetting f2 涂装变体模板）==')
    # 车库装备迷彩 → vehicles.variant_id → vehicle_variants 实例（涂装模板）。
    # f2 装备迷彩时发涂装模板（与基础模板同 parent = 同载具不同漆），
    # 皮肤实例缺失/不同载具（脏数据）时回退基础模板（此前恒发基础模板，
    # 导致"大厅装备的载具迷彩入场后不生效"）。测试用假模板 id 注入
    # wardb._VEHICLE_VARIANT_PARENT（warserver_messages 按引用共享同一 dict）。
    from wardb import _VEHICLE_VARIANT_PARENT
    _VEHICLE_VARIANT_PARENT.update({90001: 700, 90002: 700, 90003: 701})
    camo_loadout = {
        'weapons': {}, 'w_ammo': {}, 'w_mods': {},
        'vehicles': {3: [
            {'id': 409, 'vehicle_variant_template_id': 90001,
             'character_id': 3, 'equipped_in_slot': 1, 'variant_id': 777,
             'condition': 100.0},
            {'id': 410, 'vehicle_variant_template_id': 90001,
             'character_id': 3, 'equipped_in_slot': 2, 'variant_id': 778,
             'condition': 100.0},
            {'id': 411, 'vehicle_variant_template_id': 90001,
             'character_id': 3, 'equipped_in_slot': 3, 'variant_id': 0,
             'condition': 100.0}]},
        'v_ups': {}, 'v_ammo': {},
        'vv_tmpl': {777: 90002, 778: 90003},   # 777=同载具迷彩 / 778=异载具脏数据
        'uv_tmpl': {}, 'u_parts': {},
    }
    entry = _msg_character_entry(chars[2], loadout=camo_loadout)
    e = fields(pb_field_payload(entry))
    check('f4 载具条目数=3', len(e[4]) == 3)
    v_ok = fields(e[4][0])      # 409：装备迷彩 90002（同 parent 700）
    check('装备迷彩 → f2=涂装模板 90002（同 parent 替换）', v_ok[2][0] == 90002)
    v_bad = fields(e[4][1])     # 410：迷彩 90003 parent=701 ≠ 基础 700 → 回退
    check('迷彩与基础不同 parent（脏数据）→ f2 回退基础模板 90001',
          v_bad[2][0] == 90001)
    v_none = fields(e[4][2])    # 411：未装备迷彩（variant_id=0）
    check('未装备迷彩 → f2=基础模板 90001', v_none[2][0] == 90001)
    # 旧版 loadout（无 vv_tmpl 键，兼容）不报错且发基础模板
    legacy_loadout = {k: v for k, v in camo_loadout.items() if k != 'vv_tmpl'}
    entry = _msg_character_entry(chars[2], loadout=legacy_loadout)
    e = fields(pb_field_payload(entry))
    check('loadout 无 vv_tmpl 键（旧调用方）→ f2 仍为基础模板',
          fields(e[4][0])[2][0] == 90001)

    print('== 10. 武器皮肤下发（WeaponSetting f10 涂装模板）==')
    # f10 以装备位 weapons.variant_id（→ weapon_variants 实例 → 模板）为准：
    #   单买皮肤后装备（variant_id→实例）生效；有实例行而 variant_id=0 = 已卸下
    #   → 默认涂装；无实例行的旧库数据回退建武器时的反规范化列
    #   variant_template_id（此前恒发该列 → 单买装备的皮肤战斗内不生效）。
    skin_loadout = {
        'weapons': {3: [
            # 单买皮肤已装备：variant_id=801 → 模板 44
            {'id': 130, 'name': 'MP40', 'template_id': 9,
             'character_id': 3, 'container_id': 3, 'container_slot': 1,
             'variant_template_id': 0, 'variant_id': 801,
             'condition': 1.0, 'clip_count': 0},
            # 合买后已卸下：variant_id=0 但存在实例行(模板55) → 默认涂装
            {'id': 131, 'name': 'StG44', 'template_id': 10,
             'character_id': 3, 'container_id': 3, 'container_slot': 2,
             'variant_template_id': 55, 'variant_id': 0,
             'condition': 1.0, 'clip_count': 0},
            # 旧库数据：variant_id 从未持久化且无实例行 → 反规范化列兜底
            {'id': 132, 'name': 'Kar98k', 'template_id': 11,
             'character_id': 3, 'container_id': 3, 'container_slot': 3,
             'variant_template_id': 66, 'variant_id': 0,
             'condition': 1.0, 'clip_count': 0},
            # 脏数据：variant_id 指向不存在的实例 → 默认涂装
            {'id': 133, 'name': 'MG42', 'template_id': 12,
             'character_id': 3, 'container_id': 3, 'container_slot': 4,
             'variant_template_id': 0, 'variant_id': 999,
             'condition': 1.0, 'clip_count': 0}]},
        'w_ammo': {}, 'w_mods': {}, 'vehicles': {}, 'v_ups': {}, 'v_ammo': {},
        'wv_tmpl': {801: 44}, 'wv_owned': {131: [55]},
        'uv_tmpl': {}, 'u_parts': {}, 'up_tmpl': {},
    }
    entry = _msg_character_entry(chars[2], loadout=skin_loadout)
    e = fields(pb_field_payload(entry))
    check('f3 武器条目数=4', len(e[3]) == 4)
    w0, w1, w2, w3 = (fields(x) for x in e[3])
    check('单买皮肤已装备(130) → f10=44（variant_id 801 → wv_tmpl）',
          w0.get(10, [0])[0] == 44)
    check('合买后已卸下(131) → f10 省略（有实例行且 variant_id=0）',
          10 not in w1)
    check('旧库无实例行(132) → f10=66（反规范化列兜底）',
          w2.get(10, [0])[0] == 66)
    check('实例行缺失脏数据(133) → f10 省略（默认涂装）', 10 not in w3)

    print('== 11. 勋带加成下发（charactersetting f5 ribbonBoosters）==')
    # 大厅购买/激活（economy 域 player_boosters：activated_at 非空且未过期）
    # → charactersetting f5 {f1 ribbonTemplateId, f2 multiplier(fixed32)}。
    # 战斗客户端按乘数放大局内勋带经验，结算 f13 endXp 回报放大值，
    # 服务端 set_ribbon_xp_floor 落库（此前 f5 恒缺省 → 加成买了没用）。
    from wardb import _RIBBON_BOOSTERS
    check('wardb RibbonBoosterTemplate 解析出条目（模板表可读）',
          len(_RIBBON_BOOSTERS) > 0)
    rb_loadout = {
        'weapons': {}, 'w_ammo': {}, 'w_mods': {}, 'vehicles': {},
        'v_ups': {}, 'v_ammo': {}, 'uv_tmpl': {}, 'u_parts': {}, 'up_tmpl': {},
        'rboosters': {5: 2.0, 9: 3.0},
    }
    entry = _msg_character_entry(chars[2], loadout=rb_loadout)
    e = fields(pb_field_payload(entry))
    check('f5 勋带加成条目数=2', len(e.get(5, [])) == 2)
    rb0 = fields(e[5][0])
    check('f5[0] {f1 勋带=5, f2 乘数=2.0 (fixed32)}',
          rb0[1][0] == 5 and abs(rb0[2][0] - 2.0) < 1e-6)
    rb_raw = parse(e[5][0])
    f2wt = [wt for fn, wt, _ in rb_raw if fn == 2][0]
    check('f5[0] f2 乘数 wiretype=5(fixed32)', f2wt == 5)
    rb1 = fields(e[5][1])
    check('f5[1] {f1 勋带=9, f2 乘数=3.0}', rb1[1][0] == 9
          and abs(rb1[2][0] - 3.0) < 1e-6)
    # 无加成 → f5 整体省略（repeated 空字段省略）；其余字段不受影响
    entry = _msg_character_entry(chars[2], loadout=skin_loadout)
    e = fields(pb_field_payload(entry))
    check('无激活加成 → f5 整体省略', 5 not in e)

    print()
    print('ALL PASS' if ok else 'SOME CHECKS FAILED')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
