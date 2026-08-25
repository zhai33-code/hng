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
                         'condition': 1.0}]},
        'v_ammo': {409: [{'id': 603, 'ammo_template_id': 93, 'vehicle_id': 409,
                          'equipped_storage_id': 71, 'slot_index': 1,
                          'condition': 2000.0, 'spawn_clip_count': 3}]},
        'uv_tmpl': {701: 1},
        'u_parts': {3: [90, 94]},
    }
    chars = [
        {'id': 1, 'name': 'Alexei', 'faction': 3, 'template': 1, 'combat_xp': 0,
         'combat_badge_0': 0, 'combat_badge_1': 0, 'uniform_variant_id': 0,
         'squad_points': 0, 'receive_daily_bonus': True},
        {'id': 2, 'name': 'Hans', 'faction': 2, 'template': 1, 'combat_xp': 250000,
         'combat_badge_0': 0, 'combat_badge_1': 0, 'uniform_variant_id': 0,
         'squad_points': 0, 'receive_daily_bonus': True},
        {'id': 3, 'name': 'Dmitri', 'faction': 3, 'template': 1, 'combat_xp': 50000,
         'combat_badge_0': 110, 'combat_badge_1': 0, 'uniform_variant_id': 701,
         'squad_points': 8, 'receive_daily_bonus': False},
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
    check('载具升级 f1=501 f2=11', vu[1][0] == 501 and vu[2][0] == 11)
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
    check('f10 军装部件=[90, 94]', e[10] == [90, 94])
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

    print()
    print('ALL PASS' if ok else 'SOME CHECKS FAILED')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
