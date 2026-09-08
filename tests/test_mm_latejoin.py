"""等待期中途加入（late join）单测：_find_joinable_mission 纯逻辑 +
_join_existing_mission 会话登记/推送序列（假 writer，无需服务器/DB 写）。

场景（对应 matchmaking.py 1c 步骤）：
 1. 窗口内同模式选图兼容 → 可加入
 2. 不同 gamemode / 选图无交集 / 第三方阵营 / 超窗 / 满员 / 实例已释放 → 不可
 3. 未选图（通配）→ 可加入
 4. formed_at 缺失（旧结构战局）→ 视为超窗不可加入
 5. JOIN_WINDOW=0 → 功能关闭
 6. only_mission（f2 指定战局：战争图点战斗 / 好友列表"跟随进入"）
    → 只匹配该局，且绕过 gamemode/preset/JOIN_WINDOW 过滤（阵营/满员仍挡）
 7. _join_existing_mission：登记 PMS_TO_MISSION/PMS_TO_SESSION、追加
    details['players']、推出 PMS Admitted/Armed + PMM delete + BattleInfo
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import matchmaking
import mm_handler
import state
import combat_instances
import mm_state
import mm_factions
import mm_config
import mm_pool


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


def seed_mission(mid, *, gamemode=5, preset=10, attacker=3, defender=2,
                 players=1, age=0.0, formed=True):
    """登记一个（伪）运行中战局；返回 (inst, det)。"""
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
    if formed:
        det['formed_at'] = time.monotonic() - age
    combat_instances.COMBAT_INSTANCES[mid] = inst
    mm_state._MM_MISSION_DETAILS[mid] = det
    return inst, det


def main():
    old_window = mm_config.JOIN_WINDOW
    old_gm5 = mm_config.GAMEMODES[5]
    mm_config.JOIN_WINDOW = 90
    try:
        # ---- 1. 窗口内同模式兼容 ----
        seed_mission(900001, gamemode=5, preset=10, players=1, age=5)
        r = matchmaking._find_joinable_mission(5, (10,), 3)
        check('窗口内同模式同图 → 可加入', r is not None and r[0] == 900001)
        check('返回的是运行实例', r is not None and r[1] is
              combat_instances.COMBAT_INSTANCES[900001])

        # ---- 2. 各不兼容情形（only_mission 不再隔离超窗/满员——改为
        #      临时摘除池内其它可加入局做全局断言；指定局绕窗是新语义，见 5） ----
        check('不同 gamemode → 不可加入',
              matchmaking._find_joinable_mission(3, (10,), 3) is None)
        check('选图无交集 → 不可加入',
              matchmaking._find_joinable_mission(5, (26, 27), 3) is None)
        check('第三方阵营 → 不可加入',
              matchmaking._find_joinable_mission(5, (10,), 1) is None)
        seed_mission(900002, gamemode=5, preset=10, players=1, age=200)
        _pool_backup = {mid: combat_instances.COMBAT_INSTANCES.pop(mid)
                        for mid in (900001,)}
        check('超窗（age=200s > 90s）→ 不可加入',
              matchmaking._find_joinable_mission(5, (10,), 3) is None)
        combat_instances.COMBAT_INSTANCES.update(_pool_backup)
        seed_mission(900003, gamemode=5, preset=10, players=1, age=5)
        mm_config.GAMEMODES[5] = dict(old_gm5, max_players=2)
        seed_mission(900004, gamemode=5, preset=10, players=2, age=5)
        check('满员（players=2 >= max=2）→ 不可加入',
              matchmaking._find_joinable_mission(5, (10,), 3,
                                                  only_mission=900004) is None)
        check('实例已释放（不在 COMBAT_INSTANCES）→ 不可加入',
              matchmaking._find_joinable_mission(
                  5, (10,), 3, only_mission=999999) is None)
        seed_mission(900005, gamemode=5, preset=10, players=1, formed=False)
        _pool_backup = {mid: combat_instances.COMBAT_INSTANCES.pop(mid)
                        for mid in (900001, 900003)}
        check('formed_at 缺失（旧结构）→ 不可加入',
              matchmaking._find_joinable_mission(5, (10,), 3) is None)
        combat_instances.COMBAT_INSTANCES.update(_pool_backup)

        # ---- 3. 通配 / 防守方阵营 / 多实例择 earliest ----
        # 池内现有 900001(可加入) 900002(超窗) 900003(可加入) 900004(满员)
        #   900005(无 formed_at) → 全局搜索应跳过后四者命中 900001
        r = matchmaking._find_joinable_mission(5, (10,), 3)
        check('多实例：跳过超窗/满员/无 formed_at，命中最早可加入局',
              r is not None and r[0] == 900001)
        r = matchmaking._find_joinable_mission(5, (), 3)
        check('未选图通配 → 可加入', r is not None and r[0] == 900001)
        r = matchmaking._find_joinable_mission(5, (), 2)
        check('防守方阵营（defender=2）→ 可加入',
              r is not None and r[0] == 900001)

        # ---- 4. JOIN_WINDOW=0 → 关闭 ----
        mm_config.JOIN_WINDOW = 0
        check('JOIN_WINDOW=0 → 功能关闭',
              matchmaking._find_joinable_mission(5, (10,), 3) is None)
        mm_config.JOIN_WINDOW = 90

        # ---- 5. only_mission 指定战局 ----
        r = matchmaking._find_joinable_mission(5, (10,), 3, only_mission=900003)
        check('only_mission 命中指定局', r is not None and r[0] == 900003)
        r = matchmaking._find_joinable_mission(5, (10,), 3, only_mission=900004)
        check('only_mission 指向满员局 → 不可加入', r is None)
        r = matchmaking._find_joinable_mission(5, (26,), 3, only_mission=900001)
        check('only_mission 绕过选图过滤（好友跟随 UI 选图可不同）',
              r is not None and r[0] == 900001)
        r = matchmaking._find_joinable_mission(3, (10,), 3, only_mission=900001)
        check('only_mission 绕过 gamemode 过滤（同上）',
              r is not None and r[0] == 900001)
        seed_mission(900007, gamemode=5, preset=10, players=1, age=200)
        r = matchmaking._find_joinable_mission(5, (10,), 3, only_mission=900007)
        check('only_mission 绕过 JOIN_WINDOW（好友跟随随时可进）',
              r is not None and r[0] == 900007)

        # ---- 6. _join_existing_mission 全流程（假 writer） ----
        w = FakeWriter()
        pid, char_id = 777, 888
        ctx = {'writer': w, 'view_cmd': 100, 'gamertag': 'LateJoiner',
               'char_name': 'LateJoiner', 'rank': 3, 'metrics_uid': 'uid-x',
               'metrics_session_id': 424242}
        mm_state._MM_QUEUES[pid] = {'pmm_id': 555001, 'mission_id': 0,
                                       'gamemode': 5, 'ctx': ctx}
        mm_state._MATCH_PMM_IDS[pid] = 555001
        entry = mm_pool.QueueEntry(player_id=pid, char_id=char_id, faction=3,
                                   gamemode_id=5, presets=(10,))
        det = mm_state._MM_MISSION_DETAILS[900001]
        n_before = len(det['players'])
        pms_before = set(mm_state.PMS_TO_SESSION)
        asyncio.run(matchmaking._join_existing_mission(pid, 900001, entry,
                                                        ctx))
        new_pms = set(mm_state.PMS_TO_SESSION) - pms_before
        check('中途加入：登记了新 pms 会话', len(new_pms) == 1)
        if new_pms:
            pms_id = new_pms.pop()
            check('中途加入：pms 映射到目标战局',
                  mm_state.PMS_TO_MISSION.get(pms_id) == 900001)
            rec = mm_state.PMS_TO_SESSION[pms_id]
            check('中途加入：会话字段正确',
                  rec['player_id'] == pid and rec['char_id'] == char_id
                  and rec['faction'] == 3
                  and rec['metrics_session_id'] == 424242)
        check('中途加入：details.players 追加 1 人',
              len(det['players']) == n_before + 1 and
              det['players'][-1]['player_id'] == pid)
        check('中途加入：排队上下文已清理',
              pid not in mm_state._MM_QUEUES and
              pid not in mm_state._MATCH_PMM_IDS)
        # KVCS 序列：PMS(Admitted+Armed) + PMM delete + BattleInfo
        data = w.data
        check('推送序列：PlayerMissionSession ×2（Admitted+Armed）',
              data.count(b'PlayerMissionSession') == 2)
        check('推送序列：PMM delete 已推',
              b'delete' in data and b'PlayerMatchMaking' in data)
        check('推送序列：BattleInfo 已推', b'BattleInfo' in data)

        # ---- 7. 阵营字段缺失 → 不放行（加固：原 .get 默认 3=SU 漏放） ----
        seed_mission(900006, gamemode=5, preset=10, players=1, age=5,
                     attacker=1, defender=2)
        det6 = mm_state._MM_MISSION_DETAILS[900006]
        det6['attacker_faction'] = 0
        check('attacker_faction 为 0 → 不放行（不回退默认 3）',
              matchmaking._find_joinable_mission(5, (10,), 3,
                                                  only_mission=900006) is None)
        det6['attacker_faction'] = 1
        det6.pop('defender_faction', None)
        check('defender_faction 字段缺失 → 不放行',
              matchmaking._find_joinable_mission(5, (10,), 2,
                                                  only_mission=900006) is None)
        det6['defender_faction'] = 2
        check('字段恢复：SU（第三方）仍不可加入（美德局挡苏军）',
              matchmaking._find_joinable_mission(5, (10,), 3,
                                                  only_mission=900006) is None)
        check('字段恢复：GE（defender）可加入',
              matchmaking._find_joinable_mission(5, (10,), 2,
                                                  only_mission=900006) is not None)

        # ---- 8. rejoin：_find_player_active_mission 只找存活原局 ----
        rpid = 999
        pms_a, pms_b = 600001, 600002
        mm_state.PMS_TO_SESSION[pms_a] = {
            'pms_id': pms_a, 'player_id': rpid, 'char_id': 42, 'faction': 3}
        mm_state.PMS_TO_MISSION[pms_a] = 900003
        check('rejoin：有存活原局 → 命中该局',
              matchmaking._find_player_active_mission(rpid) == 900003)
        mm_state.PMS_TO_SESSION[pms_b] = {
            'pms_id': pms_b, 'player_id': rpid, 'char_id': 42, 'faction': 3}
        mm_state.PMS_TO_MISSION[pms_b] = 900001
        check('rejoin：多条会话 → 取最新成局（最大 mission_id）',
              matchmaking._find_player_active_mission(rpid) == 900003)
        mm_state.PMS_TO_MISSION[pms_a] = 999999
        mm_state.PMS_TO_MISSION[pms_b] = 999999
        check('rejoin：原局均已释放 → None（走正常匹配）',
              matchmaking._find_player_active_mission(rpid) is None)
        check('rejoin：无会话玩家 → None',
              matchmaking._find_player_active_mission(123456) is None)

        # ---- 9. _join_existing_mission 去重（rejoin 不残留旧痕迹） ----
        mm_state.PMS_TO_SESSION[pms_a] = {
            'pms_id': pms_a, 'player_id': rpid, 'char_id': 42, 'faction': 3}
        mm_state.PMS_TO_MISSION[pms_a] = 900003
        det3 = mm_state._MM_MISSION_DETAILS[900003]
        det3['players'].append({'player_id': rpid, 'gamertag': 'Rejoiner',
                                'char_id': 42, 'faction': 3,
                                'pms_id': pms_a})
        mm_state._MM_MISSION_SQUADS.setdefault(900003, {})[777001] = {
            'name': 'Infantry Squad', 'leader_char_id': 42, 'faction': 3,
            'squad_db_id': 0, 'template': 1,
            'members': [{'player_id': rpid, 'char_id': 42,
                         'gamertag': 'Rejoiner', 'rank': 1, 'slot': 0}]}
        w9 = FakeWriter()
        ctx9 = {'writer': w9, 'view_cmd': 100, 'gamertag': 'Rejoiner',
                'char_name': 'Rejoiner', 'rank': 1, 'metrics_uid': 'uid-r',
                'metrics_session_id': 434343}
        entry9 = mm_pool.QueueEntry(player_id=rpid, char_id=42, faction=3,
                                    gamemode_id=5, presets=(10,))
        mm_state._MM_QUEUES[rpid] = {'pmm_id': 555009, 'mission_id': 0,
                                        'gamemode': 5, 'ctx': ctx9}
        asyncio.run(matchmaking._join_existing_mission(rpid, 900003, entry9,
                                                        ctx9))
        pms_of_rpid = [p for p, r in mm_state.PMS_TO_SESSION.items()
                       if r.get('player_id') == rpid and
                       mm_state.PMS_TO_MISSION.get(p) == 900003]
        check('去重：同玩家同局 PMS 会话仅 1 条（旧会话已清）',
              len(pms_of_rpid) == 1 and pms_a not in pms_of_rpid)
        check('去重：det.players 该玩家仅 1 条',
              sum(1 for p in det3['players']
                  if p['player_id'] == rpid) == 1)
        squads3 = mm_state._MM_MISSION_SQUADS.get(900003) or {}
        check('去重：小队 members 该玩家仅 1 条',
              sum(1 for s in squads3.values() for m in s['members']
                  if m['player_id'] == rpid) == 1)
        check('去重：原空小队 777001 已移除（重选/重建小队）',
              777001 not in squads3)

        # ---- 10. _prune_match_factions：非战争模式同局限两阵营 ----
        m3 = mm_pool.Match(players=[
            mm_pool.QueueEntry(player_id=1, char_id=11, faction=1,
                               gamemode_id=5, presets=(10,)),
            mm_pool.QueueEntry(player_id=2, char_id=22, faction=2,
                               gamemode_id=5, presets=(10,)),
            mm_pool.QueueEntry(player_id=3, char_id=33, faction=3,
                               gamemode_id=5, presets=(10,))],
            preset_id=10)
        dropped = matchmaking._prune_match_factions(m3, 1, 2, 5)
        check('撮合裁剪：三国同局 → 第三阵营被裁（美德局裁苏军）',
              [e.player_id for e in dropped] == [3] and
              [e.player_id for e in m3.players] == [1, 2])
        m2 = mm_pool.Match(players=[
            mm_pool.QueueEntry(player_id=1, char_id=11, faction=1,
                               gamemode_id=5, presets=(10,)),
            mm_pool.QueueEntry(player_id=2, char_id=22, faction=2,
                               gamemode_id=5, presets=(10,))],
            preset_id=10)
        check('撮合裁剪：两阵营 → 不裁',
              matchmaking._prune_match_factions(m2, 1, 2, 5) == [])
        m3w = mm_pool.Match(players=[
            mm_pool.QueueEntry(player_id=1, char_id=11, faction=1,
                               gamemode_id=10, presets=(10,)),
            mm_pool.QueueEntry(player_id=3, char_id=33, faction=3,
                               gamemode_id=10, presets=(10,))],
            preset_id=10)
        check('撮合裁剪：战争模式（gamemode 10）不裁',
              matchmaking._prune_match_factions(m3w, 1, 3, 10) == [])

        # ---- 11. _faction_pair / _rival_faction（去默认值加固） ----
        check('_faction_pair：合法攻防 → (atk, dfd)',
              mm_factions._faction_pair({'attacker_faction': 1,
                                         'defender_faction': 2}) == (1, 2))
        check('_faction_pair：字段缺失 → None',
              mm_factions._faction_pair({'attacker_faction': 1}) is None and
              mm_factions._faction_pair({}) is None)
        check('_faction_pair：非法值/同侧 → None',
              mm_factions._faction_pair({'attacker_faction': 0,
                                         'defender_faction': 2}) is None and
              mm_factions._faction_pair({'attacker_faction': 2,
                                         'defender_faction': 2}) is None)
        rivals = {f: mm_factions._rival_faction(f) for f in (1, 2, 3)}
        check('_rival_faction：AI 方 ∈ 另外两阵营（等概率随机）',
              rivals[2] in (1, 3) and rivals[1] in (2, 3) and
              rivals[3] in (1, 2))
        # AI 局简报 f8 = 实际 AI 阵营（成局掷一次，简报透传该值）
        info_ge = matchmaking._msg_mission_info(1, 2, '2026-01-01T00:00:00Z',
                                                 rivals[2])
        info_us = matchmaking._msg_mission_info(1, 2, '2026-01-01T00:00:00Z',
                                                 rivals[1])
        # f8 = field 8 varint（field1-7 均小于 8 字节，0x40+值 粗定位）
        check('AI 局简报 f8：GE 玩家 → AI 方=掷出阵营',
              bytes((0x40, rivals[2])) in info_ge)
        check('AI 局简报 f8：US 玩家 → AI 方=掷出阵营',
              bytes((0x40, rivals[1])) in info_us)

        # ---- 9b. 真实小队 rejoin 归队（InBattle 掉线重连回原小队） ----
        # A(pid=888) 与 R(pid=777) 同在 DB 小队 555（局内登记行 888001，
        # R 掉线重连：去重移出 members 后必须放回原小队，而不是自建散人队
        seed_mission(900007, gamemode=5, preset=10, players=2)
        det7 = mm_state._MM_MISSION_DETAILS[900007]
        rpid2 = 777
        pms_r = 600777
        mm_state.PMS_TO_SESSION[pms_r] = {
            'pms_id': pms_r, 'player_id': rpid2, 'char_id': 42,
            'gamertag': 'Rejoiner2', 'faction': 3, 'squad_db_id': 555,
            'squad_id': 888001, 'squad_slot': 1, 'mission_id': 900007}
        mm_state.PMS_TO_MISSION[pms_r] = 900007
        det7['players'].append({'player_id': rpid2, 'gamertag': 'Rejoiner2',
                                'char_id': 42, 'faction': 3, 'pms_id': pms_r})
        mm_state._MM_MISSION_SQUADS.setdefault(900007, {})[888001] = {
            'name': 'RealSquad', 'leader_char_id': 488, 'faction': 3,
            'squad_db_id': 555, 'template': 1,
            'members': [{'player_id': 888, 'char_id': 488,
                         'gamertag': 'StayMate', 'rank': 3, 'slot': 0}]}
        w9b = FakeWriter()
        ctx9b = {'writer': w9b, 'view_cmd': 101, 'gamertag': 'Rejoiner2',
                 'char_name': 'Rejoiner2', 'rank': 2, 'faction': 3,
                 'char_template': 1, 'metrics_uid': 'uid-r2',
                 'metrics_session_id': 434344}
        entry9b = mm_pool.QueueEntry(player_id=rpid2, char_id=42, faction=3,
                                     gamemode_id=5, presets=(10,))
        asyncio.run(matchmaking._join_existing_mission(rpid2, 900007, entry9b,
                                                       ctx9b))
        new_sess9b = [r for r in mm_state.PMS_TO_SESSION.values()
                      if r.get('player_id') == rpid2 and
                      mm_state.PMS_TO_MISSION.get(r.get('pms_id')) == 900007]
        sq9b = mm_state._MM_MISSION_SQUADS[900007][888001]
        check('小队 rejoin 归队：放回原小队行（squad_id 888001）',
              len(new_sess9b) == 1 and
              new_sess9b[0].get('squad_id') == 888001 and
              new_sess9b[0].get('squad_db_id') == 555,
              f'sess={new_sess9b[0] if new_sess9b else None}')
        check('小队 rejoin 归队：槽位接续队友（slot=1）',
              new_sess9b and new_sess9b[0].get('squad_slot') == 1)
        check('小队 rejoin 归队：members 含队友+归队者各一次',
              [m['player_id'] for m in sq9b['members']] == [888, 777])
        check('小队 rejoin 归队：未自建散人队',
              len(mm_state._MM_MISSION_SQUADS[900007]) == 1)
        # 原小队行不在本局（被裁/解散）→ 降级散人凑队，squad_db_id 清零
        mm_state._MM_MISSION_SQUADS[900007].pop(888001, None)
        asyncio.run(matchmaking._join_existing_mission(rpid2, 900007, entry9b,
                                                       ctx9b))
        sess9c = [r for r in mm_state.PMS_TO_SESSION.values()
                  if r.get('player_id') == rpid2 and
                  mm_state.PMS_TO_MISSION.get(r.get('pms_id')) == 900007]
        check('小队行缺失 → 降级散人队且 squad_db_id=0',
              len(sess9c) == 1 and sess9c[0].get('squad_db_id') == 0)
        # 散人自建队默认名按本局 (阵营, 兵种) 从 1 编号（faction=3, tmpl=1）
        auto9c = list(mm_state._MM_MISSION_SQUADS[900007].values())
        check('散人自建队默认名 → "Infantry Squad 1"（按阵营编号）',
              len(auto9c) == 1 and auto9c[0]['name'] == 'Infantry Squad 1',
              f'name={auto9c[0]["name"]!r}' if auto9c else 'no squad row')

        # ---- 12. _handle_join 小队分支路由：InBattle 掉线重连回原局 ----
        # 修复前：_squad_eligible_members 过滤 InBattle → (squad, []) →
        # _enqueue_squad 静默 return（不掉 rejoin、不入池、无状态推送）。
        from protocol import pb_varint

        calls = {'rejoin': [], 'enqueue': []}

        async def fake_eligible(p):
            return ({'id': 555, 'name': 'RealSquad'}, [])

        def fake_active(p):
            return 900007

        async def fake_rejoin(p, mid, e, c):
            calls['rejoin'].append((p, mid, c))

        async def fake_enqueue(squad, members, gm, presets, req_mid):
            calls['enqueue'].append((squad, members))

        old_eligible = mm_handler._squad_eligible_members
        old_active = mm_handler._find_player_active_mission
        old_rejoin = mm_handler._join_existing_mission
        old_enqueue = mm_handler._enqueue_squad
        old_store = state.STORE

        class Store12:
            async def list_characters(self, p):
                return [{'id': 42, 'faction': 3, 'name': 'Rejoiner2',
                         'combat_xp': 0, 'template': 1}]

        class Sess12:
            player_id = rpid2
            username = 'Rejoiner2'
            playerview_cmd = None          # 跳过 KVCS 推送

            async def ensure_player(self):
                return rpid2

        mm_handler._squad_eligible_members = fake_eligible
        mm_handler._find_player_active_mission = fake_active
        mm_handler._join_existing_mission = fake_rejoin
        mm_handler._enqueue_squad = fake_enqueue
        state.STORE = Store12()
        try:
            proto12 = pb_varint(1, 42)      # f1 charId（f7 gamemode 缺省=0）
            w12 = FakeWriter()
            asyncio.run(mm_handler._handle_join(
                Sess12(), w12, 30, proto12))
            check('小队 InBattle 重连：路由到 rejoin 回原局（900007）',
                  calls['rejoin'] and calls['rejoin'][0][:2] == (rpid2, 900007)
                  and not calls['enqueue'],
                  f'rejoin={calls["rejoin"][:1]} enqueue={calls["enqueue"]}')
            check('小队 InBattle 重连：rejoin ctx 携带本队 squad_db_id',
                  calls['rejoin'] and
                  calls['rejoin'][0][2].get('squad_db_id') == 555)
            # 无存活原局（局已结束/会话清理）→ 维持旧行为：仅整队入池路径
            calls['rejoin'].clear()
            mm_handler._find_player_active_mission = lambda p: None
            w12b = FakeWriter()
            asyncio.run(mm_handler._handle_join(
                Sess12(), w12b, 30, proto12))
            check('无存活原局 → 不 rejoin，空成员入池（原行为）',
                  not calls['rejoin'] and
                  calls['enqueue'] and calls['enqueue'][0][1] == [])
        finally:
            mm_handler._squad_eligible_members = old_eligible
            mm_handler._find_player_active_mission = old_active
            mm_handler._join_existing_mission = old_rejoin
            mm_handler._enqueue_squad = old_enqueue
            state.STORE = old_store

        # ---- 13. 将军不参战（官方设计：General 只在大地图指挥）----
        # 修复前：general（actionid=7 与伞兵同）不挡 → 混进战斗当伞兵。
        from wardb import is_general_character

        check('wardb：general 模板识别（id=7 命中，其余兵种/未知否）',
              is_general_character(7) and not is_general_character(1)
              and not is_general_character(4) and not is_general_character(0))

        gpid = 999
        calls13 = {'rejoin': [], 'enqueue': []}

        class Store13:
            async def list_characters(self, p):
                return [{'id': 501, 'faction': 3, 'name': 'GenChar',
                         'combat_xp': 0, 'template': 7}]

        class Sess13:
            player_id = gpid
            username = 'GeneralUser'
            playerview_cmd = None

            async def ensure_player(self):
                return gpid

        async def fake_rejoin13(p, mid, e, c):
            calls13['rejoin'].append(p)

        async def fake_enqueue13(squad, members, gm, presets, req_mid):
            calls13['enqueue'].append([m['player_id'] for m in members])

        async def fake_eligible13(p):
            return ({'id': 556}, [])       # 即便可达小队路径也应先被拒

        old13 = (state.STORE, mm_handler._join_existing_mission,
                 mm_handler._enqueue_squad, mm_handler._squad_eligible_members)
        state.STORE = Store13()
        mm_handler._join_existing_mission = fake_rejoin13
        mm_handler._enqueue_squad = fake_enqueue13
        mm_handler._squad_eligible_members = fake_eligible13
        try:
            w13 = FakeWriter()
            asyncio.run(mm_handler._handle_join(
                Sess13(), w13, 30, pb_varint(1, 501)))
            check('将军 join → 拒绝（JoinMatchMakingResponse f1=0）',
                  w13.data.endswith(b'\x08\x00'),
                  f'data={w13.data!r}')
            check('将军 join → 不入池不回局不进小队路径',
                  not calls13['rejoin'] and not calls13['enqueue']
                  and gpid not in mm_state._MM_QUEUES
                  and gpid not in state.PLAYER_SELECTED_CHAR,
                  f'calls={calls13}')
        finally:
            state.STORE = old13[0]
            mm_handler._join_existing_mission = old13[1]
            mm_handler._enqueue_squad = old13[2]
            mm_handler._squad_eligible_members = old13[3]
        state.PLAYER_SELECTED_CHAR.pop(gpid, None)

        # 小队成员含将军 → 整队入池时被过滤（成员行无 template，走 DB 反查）
        from state import FLASH_SESSIONS

        class Flash13:
            playerview_cmd = 101

        class Store13b:
            async def get_squad_for_player(self, p):
                return {'id': 556, 'name': 'MixedSquad'}

            async def list_squad_members(self, sid):
                return [
                    {'player_id': 990, 'member_id': 9901, 'id': 9901,
                     'gamertag': 'GenMate', 'char_name': 'GenChar',
                     'combat_xp': 0, 'faction': 3, 'character_id': 501,
                     'status': 2},
                    {'player_id': 991, 'member_id': 9911, 'id': 9911,
                     'gamertag': 'RifleMate', 'char_name': 'RifleChar',
                     'combat_xp': 0, 'faction': 3, 'character_id': 502,
                     'status': 2},
                ]

            async def list_characters(self, p):
                # 990=将军成员（char 501/tmpl 7）991=步兵队长（char 502/tmpl 1）
                if int(p) == 990:
                    return [{'id': 501, 'faction': 3, 'name': 'GenChar',
                             'combat_xp': 0, 'template': 7}]
                return [{'id': 502, 'faction': 3, 'name': 'RifleChar',
                         'combat_xp': 0, 'template': 1}]

        FLASH_SESSIONS['GenMate'] = Flash13()
        FLASH_SESSIONS['RifleMate'] = Flash13()
        old13b = state.STORE
        state.STORE = Store13b()
        try:
            _, elig13 = asyncio.run(matchmaking._squad_eligible_members(991))
            check('小队整队入池：将军成员被过滤、步兵保留',
                  [m['player_id'] for m in (elig13 or [])] == [991],
                  f'eligible={[m["player_id"] for m in (elig13 or [])]}')
        finally:
            state.STORE = old13b
            FLASH_SESSIONS.pop('GenMate', None)
            FLASH_SESSIONS.pop('RifleMate', None)

        # ---- 14. 重登 rejoin：push_relogin_rejoin 登录订阅重推存活局 ----
        # 客户端整端崩溃重开后 playerMissionSessionId=null；登录订阅时
        # 服务端必须重推存活 PMS 行（同 pms id，state=2 Admitted）+
        # BattleInfo，客户端 StatusMediator 才显示"恢复战斗"按钮并弹
        # 出战局详情（REJOIN 弹窗唯一自动显示路径 = Admitted 分支启动的
        # MISSION_END_TIMER 每秒 tick；state=3 Entered 该分支什么都不做
        # → 只有按钮没有详情页。登录瞬间不会自动拉起 hng.exe：自动拉起
        # 条件 gameClientDataDownloaded 在登录开始被 ClearAndConnectCommand
        # 重置 false，PMS 行到达时必然仍为 false）。无存活战局则不推。
        rpid3 = 555
        pms_c = 600100
        seed_mission(900008, gamemode=5, preset=10, players=1)
        mm_state.PMS_TO_SESSION[pms_c] = {
            'pms_id': pms_c, 'player_id': rpid3, 'char_id': 42,
            'gamertag': 'CrashTest', 'faction': 3,
            'battle_session_id': 778899, 'metrics_uid': 'uid-crash',
            'metrics_session_id': 123456}
        mm_state.PMS_TO_MISSION[pms_c] = 900008
        w14 = FakeWriter()
        ok14 = asyncio.run(matchmaking.push_relogin_rejoin(w14, 105, rpid3))
        data14 = w14.data
        check('重登 rejoin：有存活战局 → 推送（返回 True）', ok14 is True)
        check('重登 rejoin：推 PlayerMissionSession + BattleInfo 各一次',
              data14.count(b'PlayerMissionSession') == 1 and
              data14.count(b'BattleInfo') == 1)
        check('重登 rejoin：同 pms id 重推（f1=600100，hng.exe /P 精确回会话）',
              pb_varint(1, pms_c) in data14)
        check('重登 rejoin：connectionState=2 Admitted（战局详情弹窗源）',
              b'\x70\x02' in data14 and b'\x70\x03' not in data14)
        check('重登 rejoin：gameConnect 指向本局 combat 端口',
              f':{5600 + 900008 % 100}'.encode() in data14)
        # 凑队小队重推（四修）：临时凑的小队只在内存（_MM_MISSION_SQUADS/
        # _SOLO_SQUADS），重登须随 PMS 行一并重推，否则小队面板不恢复
        mm_state.PMS_TO_SESSION[pms_c]['squad_id'] = 777700
        mm_state._MM_MISSION_SQUADS.setdefault(900008, {})[777700] = {
            'name': 'Infantry Squad 1', 'leader_char_id': 42, 'faction': 3,
            'squad_db_id': 0, 'template': 1,
            'members': [
                {'player_id': rpid3, 'char_id': 42, 'gamertag': 'CrashTest',
                 'rank': 0, 'slot': 0, 'row_id': 800001},
                {'player_id': 556, 'char_id': 43, 'gamertag': 'SquadMate',
                 'rank': 0, 'slot': 1, 'row_id': 800002}]}
        mm_state._SOLO_SQUADS[rpid3] = {
            'squad_row': 700001, 'member_row': 700002, 'entered': 2,
            'faction': 3, 'template': 1, 'metrics_session_id': 123456}
        w14s = FakeWriter()
        check('重登 rejoin：凑队小队 → SquadSession + 全体成员行',
              asyncio.run(matchmaking.push_relogin_rejoin(
                  w14s, 105, rpid3)) is True and
              w14s.data.count(b'SquadSession') == 1 and
              w14s.data.count(b'SquadMemberSession') == 2 and
              w14s.data.count(b'PlayerMissionSession') == 1)
        # 单人隐式小队重推：solo 行（status=4 InBattle）
        mm_state._MM_MISSION_SQUADS[900008][777700]['members'] = [
            {'player_id': rpid3, 'char_id': 42, 'gamertag': 'CrashTest',
             'rank': 0, 'slot': 0, 'row_id': 800001}]
        w14t = FakeWriter()
        check('重登 rejoin：单人 → solo SquadSession + 1 成员行',
              asyncio.run(matchmaking.push_relogin_rejoin(
                  w14t, 105, rpid3)) is True and
              w14t.data.count(b'SquadSession') == 1 and
              w14t.data.count(b'SquadMemberSession') == 1)
        w14n = FakeWriter()
        check('重登 rejoin：无存活战局 → 不推送（正常登录）',
              asyncio.run(matchmaking.push_relogin_rejoin(
                  w14n, 105, 424242)) is False and w14n.data == b'')
        combat_instances.COMBAT_INSTANCES.pop(900008, None)   # 模拟战局已结束
        w14b = FakeWriter()
        check('重登 rejoin：战局已释放 → 不推送',
              asyncio.run(matchmaking.push_relogin_rejoin(
                  w14b, 105, rpid3)) is False and w14b.data == b'')
        mm_state.PMS_TO_SESSION.pop(pms_c, None)
        mm_state.PMS_TO_MISSION.pop(pms_c, None)
        mm_state._MM_QUEUES.pop(rpid3, None)
        mm_state._SOLO_SQUADS.pop(rpid3, None)

        # ---- 15. 指定战局加入校验（好友跟随：_specified_mission_join_error）----
        # 修复前：JoinMM 一律回 ok，指定局阵营不符/不存在/满员时静默落入
        # 普通撮合 → 玩家被排进另一场新局（好友跟随失效根因）。
        class Store15:
            def __init__(self, war_recs=None, fail_get=False):
                self.war_recs = war_recs or {}
                self.fail_get = fail_get

            async def get_mission_record(self, mid):
                if self.fail_get:
                    raise RuntimeError('db down')
                return self.war_recs.get(int(mid))

            async def list_characters(self, p):
                return [{'id': 42, 'faction': 3, 'name': 'Follower',
                         'combat_xp': 0, 'template': 1}]

        old_store15 = state.STORE
        state.STORE = Store15()
        try:
            seed_mission(900101, gamemode=5, preset=10, attacker=3,
                         defender=2, players=1)
            seed_mission(900102, gamemode=5, preset=10, attacker=3,
                         defender=2, players=2)   # max_players=2 → 满
            seed_mission(900103, gamemode=5, preset=10, attacker=1,
                         defender=2, players=1)
            check('指定局校验：阵营命中攻方 → None',
                  asyncio.run(mm_handler._specified_mission_join_error(
                      900101, 3)) is None)
            check('指定局校验：阵营命中守方 → None',
                  asyncio.run(mm_handler._specified_mission_join_error(
                      900101, 2)) is None)
            check('指定局校验：第三方阵营 → 62（player_faction_invalid）',
                  asyncio.run(mm_handler._specified_mission_join_error(
                      900103, 3)) == 62)
            check('指定局校验：局不存在 → 43（mission_not_found）',
                  asyncio.run(mm_handler._specified_mission_join_error(
                      999999, 1)) == 43)
            check('指定局校验：满员 → 227（mission_faction_full）',
                  asyncio.run(mm_handler._specified_mission_join_error(
                      900102, 3)) == 227)
            # war 会战 record（battle_id>0）
            state.STORE = Store15({900201: {'battle_id': 7, 'state': 2,
                                            'attacker_faction': 1,
                                            'defender_faction': 2}})
            check('war 局校验：已结算 state=2 → 43',
                  asyncio.run(mm_handler._specified_mission_join_error(
                      900201, 1)) == 43)
            state.STORE = Store15({900201: {'battle_id': 7, 'state': 3,
                                            'attacker_faction': 1,
                                            'defender_faction': 2}})
            check('war 局校验：已结算 state=3 → 43',
                  asyncio.run(mm_handler._specified_mission_join_error(
                      900201, 1)) == 43)
            state.STORE = Store15({900201: {'battle_id': 7, 'state': 1,
                                            'attacker_faction': 1,
                                            'defender_faction': 2}})
            check('war 局校验：state=1 无存活会话 → 43',
                  asyncio.run(mm_handler._specified_mission_join_error(
                      900201, 1)) == 43)
            check('war 局校验：阵营非交战双方 → 62',
                  asyncio.run(mm_handler._specified_mission_join_error(
                      900201, 3)) == 62)
            seed_mission(900201, gamemode=10, preset=10, attacker=1,
                         defender=2, players=1)
            check('war 局校验：state=1 有存活会话 → None（late join）',
                  asyncio.run(mm_handler._specified_mission_join_error(
                      900201, 1)) is None)
            combat_instances.COMBAT_INSTANCES.pop(900201, None)
            mm_state._MM_MISSION_DETAILS.pop(900201, None)
            state.STORE = Store15({900202: {'battle_id': 8, 'state': 0,
                                            'attacker_faction': 1,
                                            'defender_faction': 2}})
            check('war 局校验：state=0 待开局 → None（war 桶排队）',
                  asyncio.run(mm_handler._specified_mission_join_error(
                      900202, 1)) is None)
            state.STORE = Store15(fail_get=True)
            check('DB 异常 → 回落内存口径（不存在 → 43）',
                  asyncio.run(mm_handler._specified_mission_join_error(
                      999999, 1)) == 43)
        finally:
            state.STORE = old_store15

        # ---- 16. _handle_join 指定战局：错误码响应 + 不落入普通撮合 ----
        calls16 = {'join': [], 'enqueue': []}

        async def fake_eligible16(p):
            return (None, None)            # solo 路径

        async def fake_join16(p, mid, e, c):
            calls16['join'].append((p, mid))

        async def fake_enqueue16(squad, members, gm, presets, req_mid):
            calls16['enqueue'].append(True)

        class Sess16:
            player_id = 321
            username = 'Follower'
            playerview_cmd = None          # 跳过 KVCS 推送

            async def ensure_player(self):
                return 321

        old16 = (state.STORE, mm_handler._squad_eligible_members,
                 mm_handler._join_existing_mission, mm_handler._enqueue_squad,
                 mm_handler._find_joinable_mission)
        state.STORE = Store15()             # 无 war record → 普通内存局
        mm_handler._squad_eligible_members = fake_eligible16
        mm_handler._join_existing_mission = fake_join16
        mm_handler._enqueue_squad = fake_enqueue16
        try:
            # 16a. 阵营不符（SU 士兵进美德局 900103）→ 回 62，不进局不入池
            proto16 = pb_varint(1, 42) + pb_varint(2, 900103)
            w16 = FakeWriter()
            asyncio.run(mm_handler._handle_join(Sess16(), w16, 30, proto16))
            check('阵营不符：JoinMM 回 62（错误码非 ok）',
                  w16.data.endswith(b'\x08\x3e'),
                  f'data_tail={w16.data[-4:]!r}')
            check('阵营不符：不进局不入池',
                  not calls16['join'] and 321 not in mm_state._MM_QUEUES
                  and mm_state.POOL.get(321) is None,
                  f'join={calls16["join"]} pool={mm_state.POOL.get(321)}')
            # 16b. 阵营命中（900101 攻方 3）→ 回 ok + late join 指定局
            proto16b = pb_varint(1, 42) + pb_varint(2, 900101)
            w16b = FakeWriter()
            asyncio.run(mm_handler._handle_join(Sess16(), w16b, 30, proto16b))
            check('阵营命中：JoinMM 回 ok（f1=1）',
                  w16b.data.endswith(b'\x08\x01'))
            check('阵营命中：late join 进指定局（900101）',
                  calls16['join'] and calls16['join'][0] == (321, 900101),
                  f'join={calls16["join"]}')
            check('阵营命中：不入普通池',
                  mm_state.POOL.get(321) is None)
            mm_state._MM_QUEUES.pop(321, None)
            mm_state._MATCH_PMM_IDS.pop(321, None)
            mm_state._SOLO_SQUADS.pop(321, None)
            # 16c. 校验通过但撮合瞬间失效（竞态）→ 绝不回退普通撮合
            calls16['join'].clear()
            mm_handler._find_joinable_mission = lambda *a, **k: None
            proto16c = pb_varint(1, 42) + pb_varint(2, 900101)
            w16c = FakeWriter()
            asyncio.run(mm_handler._handle_join(Sess16(), w16c, 30, proto16c))
            check('竞态失效：不进局不入普通池（保持匹配中由玩家取消）',
                  not calls16['join'] and mm_state.POOL.get(321) is None,
                  f'join={calls16["join"]} pool={mm_state.POOL.get(321)}')
        finally:
            state.STORE = old16[0]
            mm_handler._squad_eligible_members = old16[1]
            mm_handler._join_existing_mission = old16[2]
            mm_handler._enqueue_squad = old16[3]
            mm_handler._find_joinable_mission = old16[4]
            mm_state._MM_QUEUES.pop(321, None)
            mm_state._MATCH_PMM_IDS.pop(321, None)
            mm_state._SOLO_SQUADS.pop(321, None)
            state.PLAYER_SELECTED_CHAR.pop(321, None)

        # ---- 17. 快速匹配模式解析（11/12/14 虚拟号 → 具体 staging 模式） ----
        # 17a. _find_joinable_mission(None,...) 跨模式搜局
        _snap17_inst = dict(combat_instances.COMBAT_INSTANCES)
        _snap17_det = dict(mm_state._MM_MISSION_DETAILS)
        combat_instances.COMBAT_INSTANCES.clear()
        mm_state._MM_MISSION_DETAILS.clear()
        try:
            seed_mission(900171, gamemode=5, preset=10, players=1, age=5)
            r = matchmaking._find_joinable_mission(None, (), 3)
            check('quickplay 跨模式搜局：具体 staging 局可加入',
                  r is not None and r[0] == 900171)
            seed_mission(900172, gamemode=1, preset=9, players=1, age=5)
            combat_instances.COMBAT_INSTANCES.pop(900171)
            mm_state._MM_MISSION_DETAILS.pop(900171)
            check('quickplay 跨模式搜局：训练局(1)不进',
                  matchmaking._find_joinable_mission(None, (), 3) is None)
            combat_instances.COMBAT_INSTANCES.pop(900172)
            mm_state._MM_MISSION_DETAILS.pop(900172)
            seed_mission(900173, gamemode=5, preset=27, players=1, age=5)
            mm_state._MM_MISSION_DETAILS[900173]['battle_id'] = 55
            check('quickplay 跨模式搜局：war 会战局(battle_id)不进',
                  matchmaking._find_joinable_mission(None, (), 3) is None)
            combat_instances.COMBAT_INSTANCES.pop(900173)
            mm_state._MM_MISSION_DETAILS.pop(900173)
            seed_mission(900174, gamemode=11, preset=27, players=1, age=5)
            check('quickplay 跨模式搜局：旧虚拟号局(11)不进',
                  matchmaking._find_joinable_mission(None, (), 3) is None)
            combat_instances.COMBAT_INSTANCES.pop(900174)
            mm_state._MM_MISSION_DETAILS.pop(900174)
            seed_mission(900175, gamemode=8, preset=36, players=1, age=5)
            r = matchmaking._find_joinable_mission(None, (36,), 3)
            check('quickplay 跨模式搜局：士兵 tile 局(8)可加入',
                  r is not None and r[0] == 900175)
            combat_instances.COMBAT_INSTANCES.pop(900175)
            mm_state._MM_MISSION_DETAILS.pop(900175)
            seed_mission(900176, gamemode=5, preset=10, players=1, age=5,
                         attacker=1, defender=2)
            check('quickplay 跨模式搜局：阵营过滤照常生效',
                  matchmaking._find_joinable_mission(None, (), 3) is None)
            combat_instances.COMBAT_INSTANCES.pop(900176)
            mm_state._MM_MISSION_DETAILS.pop(900176)
        finally:
            combat_instances.COMBAT_INSTANCES.clear()
            combat_instances.COMBAT_INSTANCES.update(_snap17_inst)
            mm_state._MM_MISSION_DETAILS.clear()
            mm_state._MM_MISSION_DETAILS.update(_snap17_det)

        # 17b. _resolve_quickplay_gamemode：并入等待人数最多的池
        _pool17b = {m: list(lst) for m, lst in mm_state.POOL._by_mode.items()}
        _poolp17b = dict(mm_state.POOL._by_player)
        mm_state.POOL._by_mode.clear()
        mm_state.POOL._by_player.clear()
        try:
            m0 = matchmaking._resolve_quickplay_gamemode()
            check('quickplay 解析：全空池 → 通用模式随机',
                  m0 in mm_config.QUICKPLAY_POOL_MODES, f'got {m0}')
            for pid17, gm17 in ((501, 5), (502, 5), (503, 6)):
                mm_state.POOL.enqueue(mm_pool.QueueEntry(
                    player_id=pid17, char_id=600 + pid17, faction=3,
                    gamemode_id=gm17))
            check('quickplay 解析：并入等待人数最多的池（5×2 > 6×1）',
                  matchmaking._resolve_quickplay_gamemode() == 5)
            mm_state.POOL.enqueue(mm_pool.QueueEntry(
                player_id=504, char_id=604, faction=2, gamemode_id=4))
            mm_state.POOL.enqueue(mm_pool.QueueEntry(
                player_id=505, char_id=605, faction=3, gamemode_id=7))
            check('quickplay 解析：士兵 tile 池(7)不主动并入、仍取最大者',
                  matchmaking._resolve_quickplay_gamemode() == 5)
        finally:
            for pid17 in (501, 502, 503, 504, 505):
                mm_state.POOL.remove(pid17)
            mm_state.POOL._by_mode.clear()
            mm_state.POOL._by_player.clear()
            mm_state.POOL._by_mode.update(_pool17b)
            mm_state.POOL._by_player.update(_poolp17b)

        # 17c. _handle_join 全链路：quickplay 请求入池前解析为具体模式
        calls17 = {'search': [], 'fill': []}

        async def fake_eligible17(p):
            return (None, None)               # solo 路径

        async def fake_try17():
            return None

        def fake_search17(gm, presets, faction, only_mission=None,
                          vehicle_cats=()):
            calls17['search'].append(gm)
            return None

        def fake_fill17(pid, gm):              # 真身是同步函数（内部 spawn）
            calls17['fill'].append(gm)

        class Sess17:
            player_id = 321
            username = 'Quickplayer'
            playerview_cmd = None              # 跳过 KVCS 推送

            async def ensure_player(self):
                return 321

        from protocol import pb_varint
        old17 = (state.STORE, mm_handler._squad_eligible_members,
                 mm_handler._find_joinable_mission, mm_handler._try_form_all,
                 mm_handler._arm_ai_fill)
        state.STORE = Store15()
        mm_handler._squad_eligible_members = fake_eligible17
        mm_handler._find_joinable_mission = fake_search17
        mm_handler._try_form_all = fake_try17
        mm_handler._arm_ai_fill = fake_fill17
        _pool17c = {m: list(lst) for m, lst in mm_state.POOL._by_mode.items()}
        _poolp17c = dict(mm_state.POOL._by_player)
        mm_state.POOL._by_mode.clear()
        mm_state.POOL._by_player.clear()
        try:
            def _cleanup17c():
                mm_state.POOL.remove(321)
                mm_state._MM_QUEUES.pop(321, None)
                mm_state._MATCH_PMM_IDS.pop(321, None)
                mm_state._SOLO_SQUADS.pop(321, None)
                calls17['search'].clear()
                calls17['fill'].clear()

            # 11（按阵营）
            proto17 = pb_varint(1, 42) + pb_varint(7, 11) + pb_varint(8, 1)
            w17 = FakeWriter()
            asyncio.run(mm_handler._handle_join(Sess17(), w17, 30, proto17))
            e17 = mm_state.POOL.get(321)
            check('quickplay(11)入池：已解析为具体通用模式',
                  e17 is not None and e17.gamemode_id in
                  mm_config.QUICKPLAY_POOL_MODES,
                  f'gamemode={e17.gamemode_id if e17 else None}')
            check('quickplay(11)入池：1c 跨模式搜局（gamemode=None）',
                  calls17['search'] == [None], f'search={calls17["search"]}')
            check('quickplay(11)入池：_MM_QUEUES/ai_fill 用解析后模式',
                  mm_state._MM_QUEUES.get(321, {}).get('gamemode') ==
                  (e17.gamemode_id if e17 else None)
                  and calls17['fill'] == [e17.gamemode_id if e17 else None],
                  f'q={mm_state._MM_QUEUES.get(321, {}).get("gamemode")}, '
                  f'fill={calls17["fill"]}')
            _cleanup17c()
            # 12（按士兵）/ 14（全模式）变体同样解析
            for variant in (12, 14):
                proto17v = pb_varint(1, 42) + pb_varint(7, variant) \
                    + pb_varint(8, 1)
                asyncio.run(mm_handler._handle_join(
                    Sess17(), FakeWriter(), 30, proto17v))
                e17v = mm_state.POOL.get(321)
                check(f'quickplay({variant})变体：同样解析为具体模式',
                      e17v is not None and e17v.gamemode_id in
                      mm_config.QUICKPLAY_POOL_MODES,
                      f'gamemode={e17v.gamemode_id if e17v else None}')
                _cleanup17c()
            # 普通具体模式不受影响：保持 5 入池、1c 按模式搜局
            proto17n = pb_varint(1, 42) + pb_varint(7, 5)
            asyncio.run(mm_handler._handle_join(
                Sess17(), FakeWriter(), 30, proto17n))
            e17n = mm_state.POOL.get(321)
            check('普通模式(5)：不解析、1c 按模式搜局',
                  e17n is not None and e17n.gamemode_id == 5
                  and calls17['search'] == [5],
                  f'gm={e17n.gamemode_id if e17n else None}, '
                  f'search={calls17["search"]}')
        finally:
            mm_state.POOL.remove(321)
            mm_state._MM_QUEUES.pop(321, None)
            mm_state._MATCH_PMM_IDS.pop(321, None)
            mm_state._SOLO_SQUADS.pop(321, None)
            mm_state.POOL._by_mode.clear()
            mm_state.POOL._by_player.clear()
            mm_state.POOL._by_mode.update(_pool17c)
            mm_state.POOL._by_player.update(_poolp17c)
            state.STORE = old17[0]
            mm_handler._squad_eligible_members = old17[1]
            mm_handler._find_joinable_mission = old17[2]
            mm_handler._try_form_all = old17[3]
            mm_handler._arm_ai_fill = old17[4]
            state.PLAYER_SELECTED_CHAR.pop(321, None)

        # ---- 清理（模块级全局，防污染其它测试） ----
        for mid in (900001, 900002, 900003, 900004, 900005, 900006, 900007,
                    900008, 900101, 900102, 900103):
            combat_instances.COMBAT_INSTANCES.pop(mid, None)
            mm_state._MM_MISSION_DETAILS.pop(mid, None)
            mm_state._MM_MISSION_SQUADS.pop(mid, None)
        for pms_id in list(mm_state.PMS_TO_SESSION):
            if mm_state.PMS_TO_MISSION.get(pms_id) in (
                    900001, 900002, 900003, 900004, 900005, 900006, 900007,
                    999999):
                mm_state.PMS_TO_MISSION.pop(pms_id, None)
                mm_state.PMS_TO_SESSION.pop(pms_id, None)
        mm_state.PMS_TO_SESSION.pop(600777, None)
        mm_state.PMS_TO_MISSION.pop(600777, None)
        mm_state._MM_QUEUES.pop(pid, None)
        mm_state._MM_QUEUES.pop(rpid, None)
        mm_state._MM_QUEUES.pop(rpid2, None)
        mm_state._MATCH_PMM_IDS.pop(pid, None)
        mm_state._MATCH_PMM_IDS.pop(rpid, None)
        mm_state._MATCH_PMM_IDS.pop(rpid2, None)
        mm_state._SOLO_SQUADS.pop(pid, None)
        mm_state._SOLO_SQUADS.pop(rpid, None)
        mm_state._SOLO_SQUADS.pop(rpid2, None)
    finally:
        mm_config.JOIN_WINDOW = old_window
        mm_config.GAMEMODES[5] = old_gm5

    failed = [l for l, ok in RESULTS if not ok]
    print(f'\nMM_LATEJOIN: {"ALL PASS" if not failed else f"{len(failed)} FAILED: {failed}"} '
          f'({len(RESULTS) - len(failed)}/{len(RESULTS)} passed)')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())

