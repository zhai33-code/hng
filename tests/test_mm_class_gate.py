# -*- coding: utf-8 -*-
"""兵种准入矩阵回归测试（2026-09-13：官方 GamemodesMediator.as:572-626 的
UNAVAILABLE_REASON_WRONG_CHARACTER 表服务端镜像）。

需求背景：坦克掌控(3)此前步兵/飞行员也能排进（require_cats 保底还会为
其注入兵种资源），侦查掌控(8)同理。严格官方行为 = 只有坦克兵能进坦克
掌控、只有侦察兵能进侦查掌控，全矩阵见表 A。

六个执法点（全部覆盖）：
 A. mm_config.MODE_CHAR_CATS 与客户端禁入表逐格一致
 B. mm_handler._handle_join 直排门禁（玩家手动点 tile 排队 → 拒绝）
 C. matchmaking._find_joinable_mission 统一门禁（late join / quickplay
    跨模式搜局 / 显式指定战局，按该局自身 gamemode 判定）
 D. _resolve_quickplay_gamemode(char_cat) 候选过滤（坦克兵不被随机进
    步兵/坦克外模式）
 E. _resolve_mapfilter_gamemode(presets, char_cat) 勾选分类过滤
 F. _specified_mission_join_error 兵种不符 → 62（好友跟随不绕过）
 G. _enqueue_squad 小队成员过滤（混编队排坦克掌控局剔非坦克兵）

士兵资源类别（wardb.character_resource_category）：
 1=步兵 2=飞行员 4=伞兵 5=坦克兵 6=侦察兵；0=未知模板（不拦）
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HERE = os.path.dirname(os.path.abspath(__file__))

# WarDatabase 目录自动探测（地图筛选解析 _load_preset 需要）
for _cand in (os.environ.get('HNG_GAME_DIR'),
              os.path.abspath(os.path.join(_HERE, '..', '..', 'HnG'))):
    if _cand and os.path.isdir(os.path.join(_cand, 'WarDatabase')):
        os.environ['HNG_GAME_DIR'] = _cand
        break

import state                                           # noqa: E402
import mm_config                                       # noqa: E402
import mm_state                                        # noqa: E402
import mm_pool                                         # noqa: E402
import matchmaking                                     # noqa: E402
import mm_handler                                      # noqa: E402
import force_pool                                      # noqa: E402
import combat_instances                                # noqa: E402
from protocol import pb_varint                         # noqa: E402


RESULTS = []


def check(label, ok, detail=''):
    RESULTS.append((label, ok))
    print(f'[{"PASS" if ok else "FAIL"}] {label}{("  " + detail) if detail else ""}')


class FakeWriter:
    def __init__(self):
        self.data = b''

    def write(self, data):
        self.data += data

    async def drain(self):
        pass


def seed_mission(mid, *, gamemode, preset, attacker=1, defender=2,
                 players=1, age=5.0):
    """登记一个（伪）运行中战局（结构同 test_mm_latejoin.seed_mission）。"""
    inst = {'mission_id': mid, 'port': 5600 + mid % 100, 'proc': None,
            'ready': asyncio.Event(), 'seq': 1, 'ttd': False, 'udp': None}
    inst['ready'].set()
    det = {'preset': preset, 'gamemode': gamemode, 'ai_fill': False,
           'map_entity_id': mm_state.MISSION_MAP_ENTITY_ID,
           'attacker_faction': attacker, 'defender_faction': defender,
           'enable_bots': True,
           'weather_tod': 2, 'weather_condition': 0, 'timeofday': None,
           'players': [{'player_id': 1000 + i, 'gamertag': f'P{i}',
                        'char_id': 2000 + i, 'faction': attacker,
                        'pms_id': 3000 + i} for i in range(players)]}
    det['formed_at'] = time.monotonic() - age
    combat_instances.COMBAT_INSTANCES[mid] = inst
    mm_state._MM_MISSION_DETAILS[mid] = det
    return inst, det


def _allowed_set(mode):
    """矩阵允许集（None=不限 → 全部五类）。"""
    cats = mm_config.mode_char_cats(mode)
    return set(cats) if cats else {1, 2, 4, 5, 6}


def main():
    old_window = mm_config.JOIN_WINDOW
    mm_config.JOIN_WINDOW = 90
    _snap_inst = dict(combat_instances.COMBAT_INSTANCES)
    _snap_det = dict(mm_state._MM_MISSION_DETAILS)
    _snap_pool = {m: list(lst) for m, lst in mm_state.POOL._by_mode.items()}
    _snap_ppool = dict(mm_state.POOL._by_player)
    _snap_flash = dict(state.FLASH_SESSIONS)
    _snap_store = state.STORE
    combat_instances.COMBAT_INSTANCES.clear()
    mm_state._MM_MISSION_DETAILS.clear()
    pools = []
    try:
        # ---- A. 矩阵与客户端 GamemodesMediator 禁入表逐格一致 ----
        # 客户端推导的允许集（isTanker 禁 1/2/5/7/8、isPilot 禁
        # 1/2/3/4/5/8、isParatrooper 禁 1/2/3/4/7/8、isRecon 禁
        # 1/2/3/7、else 步兵禁 3/7/8；模式 6 Assault 全兵种）。
        _client = {1: {1}, 2: {1}, 3: {5}, 4: {1, 5, 6},
                   5: {1, 4, 6}, 6: {1, 2, 4, 5, 6}, 7: {2}, 8: {6}}
        bad_a = [m for m, exp in _client.items() if _allowed_set(m) != exp]
        check('A1 MODE_CHAR_CATS 与官方客户端禁入表逐格一致（模式 1-8）',
              not bad_a, f'bad={bad_a}')
        check('A2 不限模式（6/10/虚拟号/0）→ None',
              all(mm_config.mode_char_cats(m) is None for m in (6, 10, 11, 13, 0)))

        # ---- B. _handle_join 直排门禁 ----
        calls_b = {'fill': []}

        class StoreB:
            def __init__(self, template):
                self.template = template

            async def list_characters(self, p):
                return [{'id': 42, 'faction': 3, 'name': 'Gtester',
                         'combat_xp': 0, 'template': self.template}]

            async def list_vehicles(self, p, character_id=None):
                return []

            async def list_weapons(self, p, character_id=None):
                return []

            async def get_mission_record(self, mid):
                return None

            async def set_player_selected_char(self, p, c):
                pass

        async def fake_eligible_b(p):
            return (None, None)               # solo 路径

        async def fake_try_b():
            return None

        def fake_fill_b(pid, gm):
            calls_b['fill'].append(gm)

        class SessB:
            player_id = 321
            username = 'Gateguy'
            playerview_cmd = None

            async def ensure_player(self):
                return 321

        old_b = (mm_handler._squad_eligible_members,
                 mm_handler._find_joinable_mission,
                 mm_handler._try_form_all, mm_handler._arm_ai_fill)
        mm_handler._squad_eligible_members = fake_eligible_b
        mm_handler._find_joinable_mission = lambda *a, **k: None
        mm_handler._try_form_all = fake_try_b
        mm_handler._arm_ai_fill = fake_fill_b

        def _cleanup_b():
            mm_state.POOL.remove(321)
            mm_state._MM_QUEUES.pop(321, None)
            mm_state._MATCH_PMM_IDS.pop(321, None)
            mm_state._SOLO_SQUADS.pop(321, None)
            calls_b['fill'].clear()

        def _join_b(template, gamemode):
            state.STORE = StoreB(template)
            proto = pb_varint(1, 42) + pb_varint(7, gamemode)
            return asyncio.run(mm_handler._handle_join(
                SessB(), FakeWriter(), 30, proto))

        try:
            _join_b(1, 3)
            check('B1 步兵直排坦克掌控(3) → 拒绝（不入池不补位）',
                  mm_state.POOL.get(321) is None
                  and mm_state._MM_QUEUES.get(321) is None
                  and calls_b['fill'] == [])
            _join_b(5, 3)
            e = mm_state.POOL.get(321)
            check('B2 坦克兵直排坦克掌控(3) → 放行入池',
                  e is not None and e.gamemode_id == 3)
            _cleanup_b()
            _join_b(1, 8)
            check('B3 步兵直排侦查掌控(8) → 拒绝',
                  mm_state.POOL.get(321) is None
                  and mm_state._MM_QUEUES.get(321) is None)
            _join_b(6, 8)
            e = mm_state.POOL.get(321)
            check('B4 侦察兵直排侦查掌控(8) → 放行入池',
                  e is not None and e.gamemode_id == 8)
            _cleanup_b()
            _join_b(99999, 3)
            e = mm_state.POOL.get(321)
            check('B5 未知模板(cat 0)排 3 → 不拦（防误伤）',
                  e is not None and e.gamemode_id == 3)
            _cleanup_b()
            _join_b(1, 4)
            e = mm_state.POOL.get(321)
            check('B6 步兵排 C&H Battle(4，多兵种) → 放行',
                  e is not None and e.gamemode_id == 4)
            _cleanup_b()
        finally:
            mm_handler._squad_eligible_members, \
                mm_handler._find_joinable_mission, \
                mm_handler._try_form_all, mm_handler._arm_ai_fill = old_b
            _cleanup_b()

        # ---- C. _find_joinable_mission 统一门禁 ----
        seed_mission(900301, gamemode=3, preset=12)
        force_pool.init_mission(900301, 3, 1, 2)
        pools.append(900301)
        seed_mission(900302, gamemode=8, preset=36)
        force_pool.init_mission(900302, 8, 1, 2)
        pools.append(900302)
        check('C1 步兵 late join 坦克掌控局 → 拦（矩阵优先于池覆盖）',
              matchmaking._find_joinable_mission(
                  3, (12,), 1, char_cat=1) is None)
        r = matchmaking._find_joinable_mission(3, (12,), 1, char_cat=5)
        check('C2 坦克兵 late join 坦克掌控局 → 放行',
              r is not None and r[0] == 900301)
        r = matchmaking._find_joinable_mission(None, (), 1, char_cat=6)
        check('C3 quickplay 跨模式搜局：侦察兵可进侦查掌控局',
              r is not None and r[0] == 900302)
        check('C4 quickplay 跨模式搜局：步兵不落坦克/侦查局',
              matchmaking._find_joinable_mission(None, (), 1, char_cat=1)
              is None)
        check('C5 显式指定战局(only_mission)：步兵跟坦克局 → 拦',
              matchmaking._find_joinable_mission(
                  3, (), 1, only_mission=900301, char_cat=1) is None)
        r = matchmaking._find_joinable_mission(
            3, (), 1, only_mission=900301, char_cat=5)
        check('C6 显式指定战局：坦克兵跟坦克局 → 放行',
              r is not None and r[0] == 900301)
        check('C7 cat0 不受门禁影响（旧调用方）',
              matchmaking._find_joinable_mission(
                  3, (12,), 1, char_cat=0) is not None)

        # ---- D. _resolve_quickplay_gamemode 候选过滤 ----
        mm_state.POOL._by_mode.clear()
        mm_state.POOL._by_player.clear()
        try:
            for pid_d, gm_d in ((501, 3), (502, 3)):
                mm_state.POOL.enqueue(mm_pool.QueueEntry(
                    player_id=pid_d, char_id=600 + pid_d, faction=3,
                    gamemode_id=gm_d))
            check('D1 池中最忙为模式 3：坦克兵 quickplay 并入 3',
                  matchmaking._resolve_quickplay_gamemode(5) == 3)
            m_d = matchmaking._resolve_quickplay_gamemode(1)
            check('D2 步兵 quickplay：3 被剔出候选（余 2/4/5/6）',
                  m_d in (2, 4, 5, 6), f'got {m_d}')
            m_d = matchmaking._resolve_quickplay_gamemode(6)
            check('D3 侦察兵候选 = 4/5/6',
                  m_d in (4, 5, 6), f'got {m_d}')
            m_d = matchmaking._resolve_quickplay_gamemode(2)
            check('D4 飞行员候选 = 仅 6（Assault 不限）',
                  m_d == 6, f'got {m_d}')
            m_d = matchmaking._resolve_quickplay_gamemode(0)
            check('D5 cat0 不过滤（全候选随机）',
                  m_d in mm_config.QUICKPLAY_POOL_MODES, f'got {m_d}')
        finally:
            for pid_d in (501, 502):
                mm_state.POOL.remove(pid_d)

        # ---- E. _resolve_mapfilter_gamemode 兵种过滤 ----
        # preset → cat 锚点（test_mm_mapfilter A8）：10→1 训练 / 12→3 坦克 /
        # 22→4 全兵种 / 26→5 突击 / 36→8 侦察 / 373394539→2 C&H 步兵
        check('E1 步兵勾训练图 → 模式 1（不受影响）',
              matchmaking._resolve_mapfilter_gamemode((10,), 1) == (1, (10,)))
        check('E2 坦克兵勾训练图 → 不解析（训练仅步兵，回 DEFAULT 兜底）',
              matchmaking._resolve_mapfilter_gamemode((10,), 5)
              == (None, (10,)))
        check('E3 坦克兵勾坦克图 → 模式 3',
              matchmaking._resolve_mapfilter_gamemode((12,), 5) == (3, (12,)))
        check('E4 步兵勾坦克图 → 不解析（无可用分类）',
              matchmaking._resolve_mapfilter_gamemode((12,), 1)
              == (None, (12,)))
        check('E5 坦克兵勾训练+坦克 → 模式 3（训练被剔+收窄）',
              matchmaking._resolve_mapfilter_gamemode((10, 12), 5)
              == (3, (12,)))
        check('E6 步兵勾训练+坦克 → 模式 1（坦克图被剔剩训练）',
              matchmaking._resolve_mapfilter_gamemode((10, 12), 1)
              == (1, (10,)))
        check('E7 侦察兵勾侦查图 → 模式 8；步兵 → 不解析',
              matchmaking._resolve_mapfilter_gamemode((36,), 6) == (8, (36,))
              and matchmaking._resolve_mapfilter_gamemode((36,), 1)
              == (None, (36,)))
        check('E8 伞兵勾突击图 → 模式 5（矩阵允许 {1,4,6}）',
              matchmaking._resolve_mapfilter_gamemode((26,), 4) == (5, (26,)))
        check('E9 伞兵勾 C&H 步兵图 → 不解析（矩阵 {1} 剔 4）',
              matchmaking._resolve_mapfilter_gamemode((373394539,), 4)
              == (None, (373394539,)))

        # ---- F. _specified_mission_join_error 兵种 62 ----
        class StoreF:
            async def get_mission_record(self, mid):
                return None                # 走内存 det 分支

        state.STORE = StoreF()
        check('F1 好友跟随：步兵跟坦克掌控局 → 62',
              asyncio.run(mm_handler._specified_mission_join_error(
                  900301, 1, 1)) == 62)
        check('F2 好友跟随：坦克兵跟坦克掌控局 → None',
              asyncio.run(mm_handler._specified_mission_join_error(
                  900301, 1, 5)) is None)
        check('F3 好友跟随：cat0 不拦',
              asyncio.run(mm_handler._specified_mission_join_error(
                  900301, 1, 0)) is None)
        check('F4 侦察兵跟侦查掌控局 → None；步兵 → 62',
              asyncio.run(mm_handler._specified_mission_join_error(
                  900302, 1, 6)) is None
              and asyncio.run(mm_handler._specified_mission_join_error(
                  900302, 1, 1)) == 62)

        # ---- G. _enqueue_squad 成员过滤 ----
        import handlers.squad

        class FakeFlash:
            def __init__(self):
                self.writer = FakeWriter()
                self.playerview_cmd = 99

        class StoreG:
            async def update_squad_member(self, row_id, **kw):
                pass

            async def set_squad_flag(self, sid, flag):
                pass

        async def fake_rows_g(sid):
            return None

        async def fake_try_g():
            return None

        def fake_fill_g(pid, gm):
            pass

        old_g = (matchmaking._try_form_all, matchmaking._arm_ai_fill,
                 handlers.squad.push_squad_rows_to_all_members)
        matchmaking._try_form_all = fake_try_g
        matchmaking._arm_ai_fill = fake_fill_g
        handlers.squad.push_squad_rows_to_all_members = fake_rows_g
        state.STORE = StoreG()
        state.FLASH_SESSIONS.clear()
        try:
            _sq = {'id': 77}
            _mem = lambda pid, gt, tpl, cid: {
                'id': 9000 + pid, 'player_id': pid, 'gamertag': gt,
                'faction': 3, 'character_id': cid, 'char_name': gt,
                'combat_xp': 0, 'template': tpl}
            # G1 全员被剔：步兵+飞行员排 3 → 整队不入池（且不触
            # FLASH_SESSIONS——未登记也能通过即证明先过滤后取会话）
            asyncio.run(matchmaking._enqueue_squad(
                _sq, [_mem(601, 'Inf601', 1, 6011),
                      _mem(602, 'Pilot602', 2, 6021)], 3, (), 0))
            check('G1 混编队(无坦克兵)排坦克掌控 → 整队不入池',
                  mm_state.POOL.get(601) is None
                  and mm_state.POOL.get(602) is None)
            # G2 部分保留：步兵+坦克兵排 3 → 仅坦克兵入池
            state.FLASH_SESSIONS['Tank612'] = FakeFlash()
            asyncio.run(matchmaking._enqueue_squad(
                _sq, [_mem(611, 'Inf611', 1, 6111),
                      _mem(612, 'Tank612', 5, 6121)], 3, (), 0))
            e_g = mm_state.POOL.get(612)
            check('G2 混编队排坦克掌控 → 仅坦克兵入池（步兵被剔）',
                  e_g is not None and e_g.gamemode_id == 3
                  and e_g.char_cat == 5
                  and mm_state.POOL.get(611) is None)
            for pid_g in (611, 612):
                mm_state.POOL.remove(pid_g)
                mm_state._MM_QUEUES.pop(pid_g, None)
                mm_state._MATCH_PMM_IDS.pop(pid_g, None)
        finally:
            matchmaking._try_form_all, matchmaking._arm_ai_fill, \
                handlers.squad.push_squad_rows_to_all_members = old_g
    finally:
        mm_config.JOIN_WINDOW = old_window
        state.STORE = _snap_store
        for pid_g in (501, 502, 601, 602, 611, 612, 321):
            mm_state.POOL.remove(pid_g)
            mm_state._MM_QUEUES.pop(pid_g, None)
            mm_state._MATCH_PMM_IDS.pop(pid_g, None)
        for mid in pools:
            force_pool.release(mid)
        combat_instances.COMBAT_INSTANCES.clear()
        combat_instances.COMBAT_INSTANCES.update(_snap_inst)
        mm_state._MM_MISSION_DETAILS.clear()
        mm_state._MM_MISSION_DETAILS.update(_snap_det)
        mm_state.POOL._by_mode.clear()
        mm_state.POOL._by_mode.update(_snap_pool)
        mm_state.POOL._by_player.clear()
        mm_state.POOL._by_player.update(_snap_ppool)
        state.FLASH_SESSIONS.clear()
        state.FLASH_SESSIONS.update(_snap_flash)

    failed = [l for l, ok in RESULTS if not ok]
    print(f'\nMM_CLASS_GATE: '
          f'{"ALL PASS" if not failed else f"{len(failed)} FAILED: {failed}"} '
          f'({len(RESULTS) - len(failed)}/{len(RESULTS)} passed)')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
