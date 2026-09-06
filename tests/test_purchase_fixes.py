# -*- coding: utf-8 -*-
"""2026-08-26 三项购买/匹配修复的回归测试（纯单元级，无需服务器/DB）。

  1. level=0 解锁条目不是购买门槛（wardb._REQ_UNLOCK_BY_ITEM）
     事故：Yak-9（variant 215）因 uid 1401 "Yak-9 Specialist" level=0 被误拒
  2. SetWeaponClipCountRequest 负数 clipCount 的 32 位补码 varint 解码
     事故：减备弹到最少（-9）被当 4294967287 越界拒掉（weapon 124, T=10）
  3. PlayerMissionSession field 5 characterFactionId 必须是 faction 表行 id
     事故：填了 player_id -> FactionTable.getFromId 返回 null ->
     MissionInformationWindowMediator.setAccessPoints TypeError #1009
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('HNG_GAME_DIR', r'd:\HnG-test')

from protocol import pb_parse                       # noqa: E402
import wardb                                        # noqa: E402
import matchmaking as mm                           # noqa: E402

PASS = FAIL = 0


def check(label, ok, detail=''):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f'[PASS] {label}{("  " + detail) if detail else ""}')
    else:
        FAIL += 1
        print(f'[FAIL] {label}{("  " + detail) if detail else ""}')


def test_level0_not_purchase_gate():
    print('== 1. level=0 解锁条目不是购买门槛 ==')

    # Yak-9（variant 215）：uid 1401（ribbon 68 level=0）必须被剔除，
    # 只保留 uid 369（Dog Fight ribbon 2000 level=7）
    req4 = wardb._REQ_UNLOCK_BY_ITEM.get((215, wardb._E_VEHICLE))
    req15 = wardb._REQ_UNLOCK_BY_ITEM.get((215, wardb._E_VEHICLES_VARIANT))
    check('Yak-9 req4 剔除 level=0 后仅剩勋带等级条目',
          req4 == {369}, f'req4={sorted(req4) if req4 else None}')
    check('Yak-9 req15（uid 1401 level=0）不再是门槛',
          req15 is None, f'req15={sorted(req15) if req15 else None}')
    check('uid 1401 确属 level=0 集合', 1401 in wardb._LEVEL0_UIDS)

    # 不误杀：PPSh-41（template 5）的高等级需求仍保留（苏联条目 1763 ribbon 22 lv9）
    req_ppsh = wardb._REQ_UNLOCK_BY_ITEM.get((5, wardb._E_WEAPON))
    check('PPSh-41 高等级需求未被误杀',
          req_ppsh is not None and 1763 in req_ppsh,
          f'req={sorted(req_ppsh) if req_ppsh else None}')
    # 全量映射不受影响（授予位图仍用 _UNLOCK_BY_ITEM）
    check('全量映射仍含 1401（授予位图不受过滤影响）',
          1401 in wardb._UNLOCK_BY_ITEM.get((215, wardb._E_VEHICLES_VARIANT), set()))


def _decode_clip_count(proto_bytes):
    """复现 handle_setweaponclipcountrequest 的解析+补码转换路径。"""
    f = pb_parse(proto_bytes)
    cc = int(f.get(2, 0))
    if 0x80000000 <= cc <= 0xFFFFFFFF:
        cc -= 0x100000000
    return cc


def test_clipcount_signed_varint():
    print('== 2. clipCount 负数 32 位补码 varint ==')

    # 客户端真实字节（2026-08-26 日志 weapon 124 减备弹到最少）：
    # 087c10f7ffffff0f = weaponId 124, clipCount -9（0xFFFFFFF7）
    cc = _decode_clip_count(bytes.fromhex('087c10f7ffffff0f'))
    check('-9 补码 varint 解码为 -9', cc == -9, f'clipCount={cc}')

    # 范围判定（T=10 -> [-9, 9]）；124 是武器实例 id，
    # template 查 Mosin-Nagant 1891-30（weapontemplate 12, f55 clipcount=10）
    t_max = wardb.weapon_template_clipcount(12)
    limit = max(t_max - 1, 0)
    check('T=10 武器最少备弹 -9 在合法范围内',
          -limit <= cc <= limit, f'T={t_max} range=[-{limit},{limit}]')

    # 正常值不受影响
    check('+9 正常解码', _decode_clip_count(bytes.fromhex('08741009')) == 9)
    check('0 正常解码', _decode_clip_count(bytes.fromhex('08741000')) == 0)

    # 出生弹夹数三态：-9 -> 1 弹匣（最少）
    check('spawn_clips_for(-9, 12) = 1（最少 1 弹匣）',
          wardb.spawn_clips_for(-9, 12) == 1)


def test_pms_character_faction_id():
    print('== 3. PMS field 5 characterFactionId ==')

    player_id = 987654321
    m = mm._msg_player_mission_session(
        1, player_id, 'Tester', 5, 3, 'Ivan', 12, 777, '20260826T',
        '1.2.3.4:1')
    f = pb_parse(m)
    f5 = f.get(5)
    check('f5 = FACTION_ROW_IDS[3]（faction 表行 id）',
          f5 == mm.FACTION_ROW_IDS[3], f'f5={f5}')
    check('f5 != player_id（不再误填玩家 id）', f5 != player_id)
    # 三阵营都正确映射
    for faction in (1, 2, 3):
        m2 = mm._msg_player_mission_session(
            2, player_id, 'T', 6, faction, 'N', 1, 778, '20260826T', '1.2.3.4:1')
        check(f'faction={faction} 映射正确',
              pb_parse(m2).get(5) == mm.FACTION_ROW_IDS[faction])


def test_starting_sight_purchase_waiver():
    """2026-09-05 ZF-39 起始瞄具购买豁免（事故：德军侦察兵 char 12 放仓库
    后重买 ZF-39 被拒——unlockBits 授予认 STARTING_GEAR（客户端显示可购），
    购买校验却只认 uid 37（K98 Specialist 勋带 7 级），两套判定自相矛盾）。"""
    print('== 4. 起始瞄具购买豁免（STARTING_GEAR.sight_mod）==')
    import asyncio
    import handlers.weapons_purchase as hw
    from protocol import Chunk

    class _Sess:
        soldier_view_cmd = None
        playerview_cmd = None
        weapons = {}

        async def ensure_player(self):
            return 1

    class _Store:
        def __init__(self, char, mods):
            self.char, self.mods, self.created = char, mods, []

        async def get_character(self, pid, cid):
            return dict(self.char)

        async def list_weapon_modifiers(self, pid):
            return [dict(m) for m in self.mods]

        async def get_ribbon_xp(self, pid, cid):
            return {}          # 勋带 0 级

        async def create_weapon_modifier(self, pid, tpl, cid, wid):
            self.created.append((tpl, cid, wid))
            return {'id': 999, 'condition': 1.0}

    class _Writer:
        def __init__(self):
            self.buf = b''

        def write(self, data):
            self.buf += data

        async def drain(self):
            pass

    def _varint(v):
        out = bytearray()
        while True:
            b = v & 0x7F
            v >>= 7
            out.append(b | 0x80 if v else b)
            if not v:
                return bytes(out)

    def _req(mod_tpl, char_id, weapon_id):
        # CreateWeaponModifierRequest f2 模板/f3 角色/f4 武器/f5 定价/f7 货币
        return (bytes([0x10]) + _varint(mod_tpl) + bytes([0x18]) + _varint(char_id)
                + bytes([0x20]) + _varint(weapon_id) + bytes([0x28]) + _varint(5770)
                + bytes([0x38]) + _varint(1))

    def _response_value(raw):
        c, _ = Chunk.parse(raw)
        payload = c.subs[0].subs[0].data      # title chunk 的 proto 子块
        return int(pb_parse(payload).get(2, 0))

    async def _run(store, mod_tpl, char_id=12, weapon_id=192):
        hw.STORE = store
        w = _Writer()
        await hw.handle_createweaponmodifierrequest(
            _Sess(), w, 1001, b'', _req(mod_tpl, char_id, weapon_id))
        return _response_value(w.buf), store.created

    # 打桩：mapping/等级要求受控（不依赖 WarDatabase），其余外呼全部短路
    saved = (hw.STORE, hw._weapon_template_id, hw._charge_player,
             hw._repush_character_unlocks, hw._MOD_MAPPING_IDS, hw._MOD_REQ_LEVEL)

    async def _wtpl(sess, wid):
        return 43                              # K98

    async def _charge(sess, pricing_id, currency):
        return 0, True

    async def _noop_republish(sess, writer, cid):
        pass

    recon = {'id': 12, 'faction': 2, 'template': 6}   # 德军侦察兵
    rifle = {'id': 12, 'faction': 1, 'template': 1}   # 美军步兵（对照）
    hw.STORE = None    # 占位，循环内逐用例替换
    hw._weapon_template_id = _wtpl
    hw._charge_player = _charge
    hw._repush_character_unlocks = _noop_republish
    hw._MOD_MAPPING_IDS = {(107, 43): 295, (108, 43): 296}
    hw._MOD_REQ_LEVEL = {295: (31, 7), 296: (31, 7)}  # 均要求 K98 专精 7 级
    try:
        # 4.1 德军侦察兵买起始瞄具 ZF-39(107)：勋带 0 级也放行（与
        #     unlockBits 授予同源），实例落库
        st = _Store(recon, [])
        val, created = asyncio.run(_run(st, 107))
        check('侦察兵重买 ZF-39（勋带 0 级）放行 response=1',
              val == 1 and created == [(107, 12, 192)],
              f'response={val} created={created}')
        # 4.2 豁免不扩大：同角色买非起始瞄具 ZF-39 Fine Cross(108)
        #     仍按勋带门槛拒绝
        st = _Store(recon, [])
        val, created = asyncio.run(_run(st, 108))
        check('非起始瞄具(108)仍被勋带门槛拒绝 response=0',
              val == 0 and not created,
              f'response={val} created={created}')
        # 4.3 豁免仅限起始兵种：美军步兵买 107 不豁免 → 拒绝
        st = _Store(rifle, [])
        val, created = asyncio.run(_run(st, 107))
        check('美军步兵买 ZF-39 不豁免 response=0',
              val == 0 and not created, f'response={val}')
        # 4.4 原行为回归：已拥有同款实例时重复购买放行（不因本次改动破坏）
        st = _Store(recon, [{'character_id': 12, 'weapon_id': 192,
                             'template_id': 108}])
        val, created = asyncio.run(_run(st, 108))
        check('已拥有 108 重复购买仍放行 response=1', val == 1, f'response={val}')
    finally:
        (hw.STORE, hw._weapon_template_id, hw._charge_player,
         hw._repush_character_unlocks, hw._MOD_MAPPING_IDS,
         hw._MOD_REQ_LEVEL) = saved


def main():
    test_level0_not_purchase_gate()
    test_clipcount_signed_varint()
    test_pms_character_faction_id()
    test_starting_sight_purchase_waiver()
    print(f'\n结果: PASS={PASS} FAIL={FAIL}')
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
