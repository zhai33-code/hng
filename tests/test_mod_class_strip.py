# -*- coding: utf-8 -*-
"""2026-09-04 兵种专属配件误随武器指派修复回归测试。

事故：侦察兵带 ZF-39 镜的 98k 入仓库，指派步兵后镜子跟着走（步兵不该
获得侦察镜）。根因 set_inventory_item_character 无差别带走全部挂靠配件。
修复设计：入库不拆挂靠（客户端 isModded + "卸下升级件"按钮依赖 weapon_id
关联，同兵种往返也应保留配件）；出库指派时按目标兵种过滤——配件的
level>0 解锁需求 uid 中无一条适用于目标角色（faction 相符或 0 且
characterType 相符或 0，客户端 getUnlocksRequiredForItem 同口径）→
剥离回仓库（detach_weapon_modifiers，weapon_id/character_id 清 0）。

验证：
  1. _mod_usable_by_class 判定表（真实 wardb 数据：K98/M1903/FG42/StG44，
     含正向/反向/双兵种/无门槛/无映射）
  2. handle_setinventoryitemtocharacterrequest 集成（stub STORE/sess）：
     带侦察镜 98k 指派步兵 → 侦察镜剥离回仓库且重推 WeaponModifier 行、
     双兵种镜随行、别的武器上的配件不动；指派侦察兵 → 镜子随行不剥
  3. PlayerStore.detach_weapon_modifiers（DB 级：指定 id 清 0、其余不动、
     空列表/非法 id 安全、跨玩家不可卸）
  4. 源码级：handler 剥离+重推链路、import 完整（2026-09-04 复发事故：
     _weapon_template_id import 丢失 → NameError 被吞 → 剥离静默失效）
"""
import asyncio
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0


def mark(label, good, extra=''):
    global PASS, FAIL
    PASS += bool(good)
    FAIL += (not good)
    print(f'  {"PASS" if good else "FAIL"}  {label}{extra}')


# ---------- 2. handler 集成（stub，无需 DB/网络） ----------

class _BufWriter:
    def __init__(self):
        self.buf = bytearray()

    def write(self, b):
        self.buf += b

    async def drain(self):
        pass


class _Sess:
    def __init__(self, pid, weapons):
        self.player_id = pid
        self.weapons = weapons          # _weapon_template_id 会话缓存
        self.soldier_view_cmd = 77

    async def ensure_player(self):
        return self.player_id


class _StubStore:
    """最小 STORE 桩：只实现指派 handler 用到的方法。"""

    def __init__(self, chars, mods):
        self.chars = chars              # [{id, faction, template}]
        self.mods = mods                # [{id, template_id, weapon_id, character_id, ...}]
        self.detached = None
        self.assigned = None

    async def get_item_character(self, pid, tid, iid):
        return 0

    async def set_inventory_item_character(self, pid, itype, iid, cid):
        # 仿真真实现：武器上挂靠配件的 character_id 无差别跟随（事故根因）
        self.assigned = (itype, iid, cid)
        for m in self.mods:
            if int(m['weapon_id']) == int(iid):
                m['character_id'] = int(cid)
        return True

    async def list_characters(self, pid):
        return [dict(c) for c in self.chars]

    async def list_weapon_modifiers(self, pid):
        return [dict(m) for m in self.mods]

    async def detach_weapon_modifiers(self, pid, ids):
        self.detached = list(ids)
        idset = {int(i) for i in ids}
        for m in self.mods:
            if int(m['id']) in idset:
                m['weapon_id'] = 0
                m['character_id'] = 0
        return len(idset)


async def _run_assign(hc, store, sess, itype, iid, cid):
    """调用指派 handler，屏蔽重推辅助（本测试只验剥离语义）。"""
    from protocol import pb_int64, pb_string
    writer = _BufWriter()
    proto = pb_string(1, itype) + pb_int64(2, iid) + pb_int64(3, cid)
    saved = (hc.STORE, hc._repush_item_row, hc._repush_character_by_id)

    async def _noop(*a, **k):
        pass

    hc.STORE = store
    hc._repush_item_row = _noop
    hc._repush_character_by_id = _noop
    try:
        await hc.handle_setinventoryitemtocharacterrequest(
            sess, writer, 46, 'SetInventoryItemToCharacterRequest', proto)
    finally:
        hc.STORE, hc._repush_item_row, hc._repush_character_by_id = saved
    return writer


async def main():
    import handlers.characters as hc

    print('== 1. _mod_usable_by_class 判定表（真实 wardb 数据）==')
    # (mod, weapon, faction, charTemplate, expected, 说明)
    cases = [
        (107, 43, 1, 1, False, 'ZF-39 (German#1) 侦察限定 → 步兵不可用'),
        (107, 43, 1, 6, True,  'ZF-39 → 侦察可用'),
        (106, 43, 1, 1, True,  'ZF-41 (Infantry) 双兵种 → 步兵可用'),
        (106, 43, 1, 6, True,  'ZF-41 (Infantry) 双兵种 → 侦察可用'),
        (122, 43, 1, 1, True,  'ZF-41 (Infantry Marksman) 步兵限定 → 步兵可用'),
        (122, 43, 1, 6, False, 'ZF-41 (Marksman) → 侦察不可用（反向剥离）'),
        (50, 43, 1, 1, True,   'Fine Trigger uid(0,0) 无兵种门槛 → 恒可用'),
        (999999, 43, 1, 1, True, '无映射配件（即买即用）→ 恒可用'),
        (84, 47, 1, 1, False,  'FG42 Chrome Lined 伞兵限定 → 步兵不可用'),
        (84, 47, 1, 4, True,   'FG42 Chrome Lined → 伞兵可用'),
        (106, 49, 1, 5, False, 'StG44 ZF-41 (Infantry) → 坦克兵不可用'),
        (97, 46, 2, 1, False,  'M1903 M84 侦察限定 → US 步兵不可用'),
        (97, 46, 2, 6, True,   'M1903 M84 → US 侦察可用'),
    ]
    for mt, wt, fa, ct, exp, desc in cases:
        got = hc._mod_usable_by_class(mt, wt, fa, ct)
        mark(f'({mt},{wt}) f{fa}/t{ct} → {exp}（{desc}）', got == exp,
             f' got={got}')

    print('== 2. 指派 handler 剥离集成（stub STORE/sess/writer）==')
    # 现场：仓库 K98（实例 9001，模板 43）挂 ZF-39(107) + ZF-41(106)；
    # 另一把枪 9002 挂 ZF-39(107)（不应被波及）
    wid = 9001
    mods = [
        {'id': 501, 'template_id': 107, 'weapon_id': wid, 'character_id': 0,
         'auto_repair': 0, 'condition': 1.0},
        {'id': 502, 'template_id': 106, 'weapon_id': wid, 'character_id': 0,
         'auto_repair': 0, 'condition': 1.0},
        {'id': 503, 'template_id': 107, 'weapon_id': 9002, 'character_id': 55,
         'auto_repair': 0, 'condition': 1.0},
    ]
    chars = [{'id': 88, 'faction': 1, 'template': 1},   # 步兵
             {'id': 99, 'faction': 1, 'template': 6}]   # 侦察
    sess = _Sess(7, {wid: {'template_id': 43}})

    store = _StubStore(chars, mods)
    writer = await _run_assign(hc, store, sess, 'weapon', wid, 88)
    m1, m2, m3 = store.mods
    mark('指派步兵：指派成功（stub 记录 weapon→char 88）',
         store.assigned == ('weapon', wid, 88), f' assigned={store.assigned}')
    mark('指派步兵：侦察镜 ZF-39 剥离回仓库（weapon_id/character_id 清 0）',
         store.detached == [501] and m1['weapon_id'] == 0
         and m1['character_id'] == 0,
         f' detached={store.detached} m1=({m1["weapon_id"]},{m1["character_id"]})')
    mark('指派步兵：双兵种镜 ZF-41 随行（character_id=88，weapon_id 保留）',
         m2['character_id'] == 88 and m2['weapon_id'] == wid,
         f' m2=({m2["weapon_id"]},{m2["character_id"]})')
    mark('指派步兵：其他武器上的配件不受波及',
         m3['character_id'] == 55 and m3['weapon_id'] == 9002,
         f' m3=({m3["weapon_id"]},{m3["character_id"]})')
    mark('指派步兵：剥离行显式重推 WeaponModifier（否则 UI 仍显示挂枪上）',
         bytes(writer.buf).count(b'WeaponModifier') == 1,
         f' pushes={bytes(writer.buf).count(b"WeaponModifier")}')

    # 反向：同枪指派侦察兵 → 镜子随行，不剥离、不重推
    mods2 = [dict(m) for m in
             [{'id': 501, 'template_id': 107, 'weapon_id': wid,
               'character_id': 0, 'auto_repair': 0, 'condition': 1.0}]]
    store2 = _StubStore(chars, mods2)
    writer2 = await _run_assign(hc, store2, sess, 'weapon', wid, 99)
    mark('指派侦察：ZF-39 随行不剥离（同兵种往返保留配件）',
         store2.detached is None and mods2[0]['character_id'] == 99
         and mods2[0]['weapon_id'] == wid,
         f' detached={store2.detached} m=({mods2[0]["weapon_id"]},'
         f'{mods2[0]["character_id"]})')
    mark('指派侦察：无剥离则无 WeaponModifier 补推',
         bytes(writer2.buf).count(b'WeaponModifier') == 0)

    print('== 3. PlayerStore.detach_weapon_modifiers（DB 级）==')
    import store

    st = store.PlayerStore()
    await st.start()

    async def cleanup(_st, _pid):
        async with _st.pool.acquire() as conn:
            await conn.execute('DELETE FROM weapon_modifiers WHERE player_id = $1',
                               int(_pid))
            await conn.execute("DELETE FROM weapons WHERE player_id = $1 "
                               "AND name = 'U12ModGun'", int(_pid))

    pid = await st.get_player('u12modstrip_a') or await st.create_player(
        'u12modstrip_a', 'pw')
    await cleanup(st, pid)
    w = await st.create_weapon(pid, 'U12ModGun', 43, 0, 0)
    mm1 = await st.create_weapon_modifier(pid, 107, 0, int(w['id']))
    mm2 = await st.create_weapon_modifier(pid, 106, 0, int(w['id']))
    n = await st.detach_weapon_modifiers(pid, [mm1['id']])
    async with st.pool.acquire() as conn:
        r1 = await conn.fetchrow('SELECT weapon_id, character_id '
                                 'FROM weapon_modifiers WHERE id = $1',
                                 int(mm1['id']))
        r2 = await conn.fetchrow('SELECT weapon_id, character_id '
                                 'FROM weapon_modifiers WHERE id = $1',
                                 int(mm2['id']))
    mark('detach：返回受影响行数 1', n == 1, f' n={n}')
    mark('detach：指定配件 weapon_id/character_id 清 0',
         r1['weapon_id'] == 0 and r1['character_id'] == 0,
         f' r1=({r1["weapon_id"]},{r1["character_id"]})')
    mark('detach：同枪其他配件不动',
         r2['weapon_id'] == int(w['id']) and r2['character_id'] == 0,
         f' r2=({r2["weapon_id"]},{r2["character_id"]})')
    mark('detach：空列表返回 0（不报错）',
         await st.detach_weapon_modifiers(pid, []) == 0)
    mark('detach：非法 id（0/负数）过滤后返回 0',
         await st.detach_weapon_modifiers(pid, [0, -5]) == 0)
    # 跨玩家安全：别人名下的配件不可被卸
    pid2 = await st.get_player('u12modstrip_b') or await st.create_player(
        'u12modstrip_b', 'pw')
    n2 = await st.detach_weapon_modifiers(pid2, [mm2['id']])
    async with st.pool.acquire() as conn:
        r2b = await conn.fetchrow('SELECT weapon_id, character_id '
                                  'FROM weapon_modifiers WHERE id = $1',
                                  int(mm2['id']))
    mark('detach：player_id 条件生效（跨玩家卸件 no-op）',
         n2 == 0 and r2b['weapon_id'] == int(w['id']),
         f' n2={n2} r2=({r2b["weapon_id"]},{r2b["character_id"]})')

    print('== 4. 源码级回归标记 ==')
    src = inspect.getsource(hc.handle_setinventoryitemtocharacterrequest)
    mark('handler：指派武器时兵种兼容检查 + 剥离（_mod_usable_by_class/'
         'detach_weapon_modifiers）',
         '_mod_usable_by_class' in src and 'detach_weapon_modifiers' in src)
    mark('handler：剥离行显式重推 WeaponModifier（weapon_id 清 0 后'
         '_repush_weapon_rows 不覆盖）',
         'WeaponModifier' in src and 'stripped_mods' in src)
    mark('handler：_weapon_template_id import 在位（2026-09-04 复发事故：'
         'import 丢失 → NameError 被吞 → 剥离静默失效）',
         hasattr(hc, '_weapon_template_id'))
    src = inspect.getsource(store.maintenance.MaintenanceMixin)  # 拆分后驻留 maintenance 域
    mark('store：detach_weapon_modifiers 批量卸件方法存在',
         'async def detach_weapon_modifiers' in src)

    await cleanup(st, pid)
    print('  [cleanup] u12modstrip 测试行已清理')
    await st.close()
    print(f'\n{"ALL PASS" if FAIL == 0 else f"{FAIL} FAILED"} (ok={PASS})')
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
