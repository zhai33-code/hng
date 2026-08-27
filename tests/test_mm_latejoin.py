"""等待期中途加入（late join）单测：_find_joinable_mission 纯逻辑 +
_join_existing_mission 会话登记/推送序列（假 writer，无需服务器/DB 写）。

场景（对应 matchmaking.py 1c 步骤）：
 1. 窗口内同模式选图兼容 → 可加入
 2. 不同 gamemode / 选图无交集 / 第三方阵营 / 超窗 / 满员 / 实例已释放 → 不可
 3. 未选图（通配）→ 可加入
 4. formed_at 缺失（旧结构战局）→ 视为超窗不可加入
 5. JOIN_WINDOW=0 → 功能关闭
 6. only_mission（f2 指定战局）→ 只匹配该局
 7. _join_existing_mission：登记 PMS_TO_MISSION/PMS_TO_SESSION、追加
    details['players']、推出 PMS Admitted/Armed + PMM delete + BattleInfo
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import matchmaking
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
           'map_entity_id': matchmaking.MISSION_MAP_ENTITY_ID,
           'attacker_faction': attacker, 'defender_faction': defender,
           'enable_bots': True,
           'weather_tod': 2, 'weather_condition': 0, 'timeofday': None,
           'players': [{'player_id': 1000 + i, 'gamertag': f'P{i}',
                        'char_id': 2000 + i, 'faction': attacker,
                        'pms_id': 3000 + i} for i in range(players)]}
    if formed:
        det['formed_at'] = time.monotonic() - age
    matchmaking.COMBAT_INSTANCES[mid] = inst
    matchmaking._MM_MISSION_DETAILS[mid] = det
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
              matchmaking.COMBAT_INSTANCES[900001])

        # ---- 2. 各不兼容情形（only_mission 隔离，防被其它可加入局命中） ----
        check('不同 gamemode → 不可加入',
              matchmaking._find_joinable_mission(3, (10,), 3) is None)
        check('选图无交集 → 不可加入',
              matchmaking._find_joinable_mission(5, (26, 27), 3) is None)
        check('第三方阵营 → 不可加入',
              matchmaking._find_joinable_mission(5, (10,), 1) is None)
        seed_mission(900002, gamemode=5, preset=10, players=1, age=200)
        check('超窗（age=200s > 90s）→ 不可加入',
              matchmaking._find_joinable_mission(5, (10,), 3,
                                                  only_mission=900002) is None)
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
        check('formed_at 缺失（旧结构）→ 不可加入',
              matchmaking._find_joinable_mission(5, (10,), 3,
                                                  only_mission=900005) is None)

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
        check('only_mission 但选图不含该局 preset → 不可加入', r is None)

        # ---- 6. _join_existing_mission 全流程（假 writer） ----
        w = FakeWriter()
        pid, char_id = 777, 888
        ctx = {'writer': w, 'view_cmd': 100, 'gamertag': 'LateJoiner',
               'char_name': 'LateJoiner', 'rank': 3, 'metrics_uid': 'uid-x',
               'metrics_session_id': 424242}
        matchmaking._MM_QUEUES[pid] = {'pmm_id': 555001, 'mission_id': 0,
                                       'gamemode': 5, 'ctx': ctx}
        matchmaking._MATCH_PMM_IDS[pid] = 555001
        entry = mm_pool.QueueEntry(player_id=pid, char_id=char_id, faction=3,
                                   gamemode_id=5, presets=(10,))
        det = matchmaking._MM_MISSION_DETAILS[900001]
        n_before = len(det['players'])
        pms_before = set(matchmaking.PMS_TO_SESSION)
        asyncio.run(matchmaking._join_existing_mission(pid, 900001, entry,
                                                        ctx))
        new_pms = set(matchmaking.PMS_TO_SESSION) - pms_before
        check('中途加入：登记了新 pms 会话', len(new_pms) == 1)
        if new_pms:
            pms_id = new_pms.pop()
            check('中途加入：pms 映射到目标战局',
                  matchmaking.PMS_TO_MISSION.get(pms_id) == 900001)
            rec = matchmaking.PMS_TO_SESSION[pms_id]
            check('中途加入：会话字段正确',
                  rec['player_id'] == pid and rec['char_id'] == char_id
                  and rec['faction'] == 3
                  and rec['metrics_session_id'] == 424242)
        check('中途加入：details.players 追加 1 人',
              len(det['players']) == n_before + 1 and
              det['players'][-1]['player_id'] == pid)
        check('中途加入：排队上下文已清理',
              pid not in matchmaking._MM_QUEUES and
              pid not in matchmaking._MATCH_PMM_IDS)
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
        det6 = matchmaking._MM_MISSION_DETAILS[900006]
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
        matchmaking.PMS_TO_SESSION[pms_a] = {
            'pms_id': pms_a, 'player_id': rpid, 'char_id': 42, 'faction': 3}
        matchmaking.PMS_TO_MISSION[pms_a] = 900003
        check('rejoin：有存活原局 → 命中该局',
              matchmaking._find_player_active_mission(rpid) == 900003)
        matchmaking.PMS_TO_SESSION[pms_b] = {
            'pms_id': pms_b, 'player_id': rpid, 'char_id': 42, 'faction': 3}
        matchmaking.PMS_TO_MISSION[pms_b] = 900001
        check('rejoin：多条会话 → 取最新成局（最大 mission_id）',
              matchmaking._find_player_active_mission(rpid) == 900003)
        matchmaking.PMS_TO_MISSION[pms_a] = 999999
        matchmaking.PMS_TO_MISSION[pms_b] = 999999
        check('rejoin：原局均已释放 → None（走正常匹配）',
              matchmaking._find_player_active_mission(rpid) is None)
        check('rejoin：无会话玩家 → None',
              matchmaking._find_player_active_mission(123456) is None)

        # ---- 9. _join_existing_mission 去重（rejoin 不残留旧痕迹） ----
        matchmaking.PMS_TO_SESSION[pms_a] = {
            'pms_id': pms_a, 'player_id': rpid, 'char_id': 42, 'faction': 3}
        matchmaking.PMS_TO_MISSION[pms_a] = 900003
        det3 = matchmaking._MM_MISSION_DETAILS[900003]
        det3['players'].append({'player_id': rpid, 'gamertag': 'Rejoiner',
                                'char_id': 42, 'faction': 3,
                                'pms_id': pms_a})
        matchmaking._MM_MISSION_SQUADS.setdefault(900003, {})[777001] = {
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
        matchmaking._MM_QUEUES[rpid] = {'pmm_id': 555009, 'mission_id': 0,
                                        'gamemode': 5, 'ctx': ctx9}
        asyncio.run(matchmaking._join_existing_mission(rpid, 900003, entry9,
                                                        ctx9))
        pms_of_rpid = [p for p, r in matchmaking.PMS_TO_SESSION.items()
                       if r.get('player_id') == rpid and
                       matchmaking.PMS_TO_MISSION.get(p) == 900003]
        check('去重：同玩家同局 PMS 会话仅 1 条（旧会话已清）',
              len(pms_of_rpid) == 1 and pms_a not in pms_of_rpid)
        check('去重：det.players 该玩家仅 1 条',
              sum(1 for p in det3['players']
                  if p['player_id'] == rpid) == 1)
        squads3 = matchmaking._MM_MISSION_SQUADS.get(900003) or {}
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
              matchmaking._faction_pair({'attacker_faction': 1,
                                         'defender_faction': 2}) == (1, 2))
        check('_faction_pair：字段缺失 → None',
              matchmaking._faction_pair({'attacker_faction': 1}) is None and
              matchmaking._faction_pair({}) is None)
        check('_faction_pair：非法值/同侧 → None',
              matchmaking._faction_pair({'attacker_faction': 0,
                                         'defender_faction': 2}) is None and
              matchmaking._faction_pair({'attacker_faction': 2,
                                         'defender_faction': 2}) is None)
        check('_rival_faction：GE→SU、US/SU→GE（两处硬编码合一）',
              matchmaking._rival_faction(2) == 3 and
              matchmaking._rival_faction(1) == 2 and
              matchmaking._rival_faction(3) == 2)
        # AI 局简报 f8 = 实际 AI 阵营（原恒 2，GE 玩家错位）
        info_ge = matchmaking._msg_mission_info(1, 2, '2026-01-01T00:00:00Z',
                                                 matchmaking._rival_faction(2))
        info_us = matchmaking._msg_mission_info(1, 2, '2026-01-01T00:00:00Z',
                                                 matchmaking._rival_faction(1))
        # f8 = field 8 varint（field1-7 均小于 8 字节，0x40 03 = f8:3 粗定位）
        check('AI 局简报 f8：GE 玩家 → AI 方 SU(3)（原恒 GE 错位）',
              b'\x40\x03' in info_ge)
        check('AI 局简报 f8：US 玩家 → AI 方 GE(2)（原行为不变）',
              b'\x40\x02' in info_us)

        # ---- 9b. 真实小队 rejoin 归队（InBattle 掉线重连回原小队） ----
        # A(pid=888) 与 R(pid=777) 同在 DB 小队 555（局内登记行 888001，
        # R 掉线重连：去重移出 members 后必须放回原小队，而不是自建散人队
        seed_mission(900007, gamemode=5, preset=10, players=2)
        det7 = matchmaking._MM_MISSION_DETAILS[900007]
        rpid2 = 777
        pms_r = 600777
        matchmaking.PMS_TO_SESSION[pms_r] = {
            'pms_id': pms_r, 'player_id': rpid2, 'char_id': 42,
            'gamertag': 'Rejoiner2', 'faction': 3, 'squad_db_id': 555,
            'squad_id': 888001, 'squad_slot': 1, 'mission_id': 900007}
        matchmaking.PMS_TO_MISSION[pms_r] = 900007
        det7['players'].append({'player_id': rpid2, 'gamertag': 'Rejoiner2',
                                'char_id': 42, 'faction': 3, 'pms_id': pms_r})
        matchmaking._MM_MISSION_SQUADS.setdefault(900007, {})[888001] = {
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
        new_sess9b = [r for r in matchmaking.PMS_TO_SESSION.values()
                      if r.get('player_id') == rpid2 and
                      matchmaking.PMS_TO_MISSION.get(r.get('pms_id')) == 900007]
        sq9b = matchmaking._MM_MISSION_SQUADS[900007][888001]
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
              len(matchmaking._MM_MISSION_SQUADS[900007]) == 1)
        # 原小队行不在本局（被裁/解散）→ 降级散人凑队，squad_db_id 清零
        matchmaking._MM_MISSION_SQUADS[900007].pop(888001, None)
        asyncio.run(matchmaking._join_existing_mission(rpid2, 900007, entry9b,
                                                       ctx9b))
        sess9c = [r for r in matchmaking.PMS_TO_SESSION.values()
                  if r.get('player_id') == rpid2 and
                  matchmaking.PMS_TO_MISSION.get(r.get('pms_id')) == 900007]
        check('小队行缺失 → 降级散人队且 squad_db_id=0',
              len(sess9c) == 1 and sess9c[0].get('squad_db_id') == 0)

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

        old_eligible = matchmaking._squad_eligible_members
        old_active = matchmaking._find_player_active_mission
        old_rejoin = matchmaking._join_existing_mission
        old_enqueue = matchmaking._enqueue_squad
        old_store = matchmaking.STORE

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

        matchmaking._squad_eligible_members = fake_eligible
        matchmaking._find_player_active_mission = fake_active
        matchmaking._join_existing_mission = fake_rejoin
        matchmaking._enqueue_squad = fake_enqueue
        matchmaking.STORE = Store12()
        try:
            proto12 = pb_varint(1, 42)      # f1 charId（f7 gamemode 缺省=0）
            w12 = FakeWriter()
            asyncio.run(matchmaking._handle_join(
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
            matchmaking._find_player_active_mission = lambda p: None
            w12b = FakeWriter()
            asyncio.run(matchmaking._handle_join(
                Sess12(), w12b, 30, proto12))
            check('无存活原局 → 不 rejoin，空成员入池（原行为）',
                  not calls['rejoin'] and
                  calls['enqueue'] and calls['enqueue'][0][1] == [])
        finally:
            matchmaking._squad_eligible_members = old_eligible
            matchmaking._find_player_active_mission = old_active
            matchmaking._join_existing_mission = old_rejoin
            matchmaking._enqueue_squad = old_enqueue
            matchmaking.STORE = old_store

        # ---- 清理（模块级全局，防污染其它测试） ----
        for mid in (900001, 900002, 900003, 900004, 900005, 900006, 900007):
            matchmaking.COMBAT_INSTANCES.pop(mid, None)
            matchmaking._MM_MISSION_DETAILS.pop(mid, None)
            matchmaking._MM_MISSION_SQUADS.pop(mid, None)
        for pms_id in list(matchmaking.PMS_TO_SESSION):
            if matchmaking.PMS_TO_MISSION.get(pms_id) in (
                    900001, 900002, 900003, 900004, 900005, 900006, 900007,
                    999999):
                matchmaking.PMS_TO_MISSION.pop(pms_id, None)
                matchmaking.PMS_TO_SESSION.pop(pms_id, None)
        matchmaking.PMS_TO_SESSION.pop(600777, None)
        matchmaking.PMS_TO_MISSION.pop(600777, None)
        matchmaking._MM_QUEUES.pop(pid, None)
        matchmaking._MM_QUEUES.pop(rpid, None)
        matchmaking._MM_QUEUES.pop(rpid2, None)
        matchmaking._MATCH_PMM_IDS.pop(pid, None)
        matchmaking._MATCH_PMM_IDS.pop(rpid, None)
        matchmaking._MATCH_PMM_IDS.pop(rpid2, None)
        matchmaking._SOLO_SQUADS.pop(pid, None)
        matchmaking._SOLO_SQUADS.pop(rpid, None)
        matchmaking._SOLO_SQUADS.pop(rpid2, None)
    finally:
        mm_config.JOIN_WINDOW = old_window
        mm_config.GAMEMODES[5] = old_gm5

    failed = [l for l, ok in RESULTS if not ok]
    print(f'\nMM_LATEJOIN: {"ALL PASS" if not failed else f"{len(failed)} FAILED: {failed}"} '
          f'({len(RESULTS) - len(failed)}/{len(RESULTS)} passed)')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())

