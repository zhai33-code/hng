"""两人局 SquadResponse faction 回归测试（2026-08-27 线上 bug）。

背景：ZServer 的 SquadRequest 里 missionSessionId 是它自己按局生成的 id
（730433917*10^6+mission，兼作 stats session id），不是玩家的
pms_id/battle_session_id → 两人局精确匹配落空、回退"最新会话"=
后入队玩家 → 先入队玩家（如德军）的小队拿到后者的 faction（苏军）→
战斗服两个小队全进同一 team → "#error3 squad setup invalid"、
部署界面起不来、无小队名。

用例：
 1. 两人局（A=德军 faction 2 先入队，B=苏军 faction 3 后入队），
    ZServer 风格 missionSessionId（命不中任何玩家会话）分别请求
    A/B 的小队 → 各自 SquadResponse 的 team/factionTemplate 必须是
    2/3（修复前 A 会拿到 3）
 2. 单人局回退路径仍正确（唯一会话）
 3. 精确命中 pms_id 的旧路径不回归
 4. RequestReadCharacterStats 两人局聚合：按 char_id 反查所属 player
    查勋带（修复前只取最后一条会话 → A 的角色查无勋带）
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import game_server
import matchmaking
from protocol import Chunk, pb_message, pb_varint
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


def _parse_varints(data):
    """protobuf 顶层字段 → {field_no: value}（0 varint / 2 message 收集 /
    5 fixed32 跳过——PlayerSettings f6 是 float，不能像旧版那样 break）。"""
    out = {}
    i = 0
    n = len(data)

    def _read_varint(pos):
        v = shift = 0
        while True:
            b = data[pos]
            pos += 1
            v |= (b & 0x7F) << shift
            shift += 7
            if not (b & 0x80):
                return v, pos

    while i < n:
        key, i = _read_varint(i)
        fn, wt = key >> 3, key & 7
        if wt == 0:
            out[fn], i = _read_varint(i)
        elif wt == 1:
            i += 8
        elif wt == 2:
            ln, i = _read_varint(i)
            out.setdefault('_messages', {})[fn] = data[i:i + ln]
            i += ln
        elif wt == 5:
            i += 4
        else:
            break
    return out


async def _squad_request(mission_session_id, squad_id, sess=None):
    """发一条 SquadRequest，返回解析后的 SquadResponse 字段。
    sess 可带 combat_mission（战斗服连接场景）。"""
    proto = pb_varint(1, mission_session_id) + pb_varint(2, squad_id)
    w = FakeWriter()
    await game_server.handle_request(sess, w, 6, 'SquadRequest', proto)
    main, _ = Chunk.parse(w.data, 0)
    title = main.subs[0]
    resp = _parse_varints(title.subs[0].data)
    squad_def = _parse_varints(resp['_messages'][2])
    return {
        'squad_id': resp.get(1),
        'team': squad_def.get(1),
        'squad_template': squad_def.get(2),
        'faction_template': squad_def.get(3),
        'name': squad_def.get('_messages', {}).get(4, b'').decode(),
    }


def _seed_two_player_match():
    """两人局：A=德军(2) 先入队，B=苏军(3) 后入队；同 mission 两个 solo 小队。"""
    matchmaking.PMS_TO_SESSION.clear()
    matchmaking.PMS_TO_MISSION.clear()
    matchmaking._MM_MISSION_SQUADS.clear()
    mission = 488464
    sess_a = {'pms_id': 903048748842679864, 'player_id': 2,
              'char_id': 31, 'gamertag': 'zhai33', 'faction': 2,
              'metrics_uid': 'u-a', 'metrics_session_id': 1,
              'battle_session_id': 1189987113029180681,
              'squad_id': 3721435839200754145, 'squad_db_id': 0,
              'squad_slot': 0, 'mission_id': mission, 'quickplay': True}
    sess_b = {'pms_id': 903048748842679865, 'player_id': 11,
              'char_id': 49, 'gamertag': 'summercatOVO', 'faction': 3,
              'metrics_uid': 'u-b', 'metrics_session_id': 2,
              'battle_session_id': 1189987113029180682,
              'squad_id': 3721435839200754146, 'squad_db_id': 0,
              'squad_slot': 0, 'mission_id': mission, 'quickplay': True}
    matchmaking.PMS_TO_SESSION[sess_a['pms_id']] = sess_a
    matchmaking.PMS_TO_SESSION[sess_b['pms_id']] = sess_b
    matchmaking.PMS_TO_MISSION[sess_a['pms_id']] = mission
    matchmaking.PMS_TO_MISSION[sess_b['pms_id']] = mission
    matchmaking._MM_MISSION_SQUADS[mission] = {
        3721435839200754145: {
            'name': 'Infantry Squad', 'leader_char_id': 31, 'faction': 2,
            'squad_db_id': 0, 'template': 1,
            'members': [{'player_id': 2, 'char_id': 31, 'gamertag': 'zhai33',
                         'rank': 5, 'slot': 0}]},
        3721435839200754146: {
            'name': 'Infantry Squad', 'leader_char_id': 49, 'faction': 3,
            'squad_db_id': 0, 'template': 1,
            'members': [{'player_id': 11, 'char_id': 49,
                         'gamertag': 'summercatOVO', 'rank': 5, 'slot': 0}]},
    }
    return mission, sess_a, sess_b


async def _stats_request(char_ids):
    """发一条 RequestReadCharacterStats，返回 {charId: ribbonId}。"""
    proto = b''.join(pb_varint(1, c) for c in char_ids)
    w = FakeWriter()
    await game_server.handle_request(None, w, 7,
                                     'RequestReadCharacterStats', proto)
    main, _ = Chunk.parse(w.data, 0)
    body = main.subs[0].subs[0].data
    entries = []
    i = 0
    while i < len(body):
        v = shift = 0
        while True:
            b = body[i]; i += 1
            v |= (b & 0x7F) << shift; shift += 7
            if not (b & 0x80):
                break
        fn, wt = v >> 3, v & 7
        ln = 0; shift = 0
        while True:
            b = body[i]; i += 1
            ln |= (b & 0x7F) << shift; shift += 7
            if not (b & 0x80):
                break
        if fn == 1 and wt == 2:
            ent = body[i:i + ln]
            ev = _parse_varints(ent)
            rid = ev.get('_messages', {}).get(3)
            rid_no = _parse_varints(rid).get(1) if rid else None
            entries.append((ev.get(1), rid_no))
        i += ln
    return dict(entries)


class FakeStore:
    """两人局假 DB：A(pid=2) 角色 31/32，B(pid=11) 角色 49。"""
    CHARACTERS = {2: [{'id': 31, 'combat_xp': 0},
                      {'id': 32, 'combat_xp': 0}],
                  11: [{'id': 49, 'combat_xp': 0}]}
    RIBBONS = {(2, 31): {13: 12000.0},   # A 的角色 31 勋带 13
               (11, 49): {79: 3000.0}}    # B 的角色 49 勋带 79

    async def list_characters(self, pid):
        return self.CHARACTERS[pid]

    async def get_ribbon_xp(self, pid, cid):
        return self.RIBBONS.get((pid, cid), {})


async def run():
    # --- 用例 1：两人局 ZServer 风格 missionSessionId（730433917*10^6+mission）---
    mission, sess_a, sess_b = _seed_two_player_match()
    zserver_sid = 730433917 * 10**6 + mission     # 命不中任何玩家会话
    resp_a = await _squad_request(zserver_sid, sess_a['squad_id'])
    check('两人局先入队(德军) factionTemplate=2',
          resp_a['faction_template'] == 2 and resp_a['team'] == 2,
          f'got team={resp_a["team"]} factionTemplate='
          f'{resp_a["faction_template"]} name={resp_a["name"]!r}')
    check('两人局先入队(德军) squadId 回显',
          resp_a['squad_id'] == sess_a['squad_id'])
    resp_b = await _squad_request(zserver_sid, sess_b['squad_id'])
    check('两人局后入队(苏军) factionTemplate=3',
          resp_b['faction_template'] == 3 and resp_b['team'] == 3,
          f'got team={resp_b["team"]} factionTemplate='
          f'{resp_b["faction_template"]}')

    # --- 用例 2：单人局回退（唯一会话）不回归 ---
    matchmaking.PMS_TO_SESSION.clear()
    matchmaking.PMS_TO_SESSION[sess_a['pms_id']] = sess_a
    resp_solo = await _squad_request(zserver_sid, sess_a['squad_id'])
    check('单人局回退 faction 正确',
          resp_solo['faction_template'] == 2 and resp_solo['team'] == 2)

    # --- 用例 3：精确命中 pms_id 旧路径不回归 ---
    resp_exact = await _squad_request(sess_b['pms_id'], sess_b['squad_id'])
    check('精确命中 pms_id 路径 faction 正确',
          resp_exact['faction_template'] == 3 and resp_exact['team'] == 3)

    # --- 用例 4：RequestReadCharacterStats 两人局聚合 ---
    # 修复前只取最后一条会话的角色库：A(先入队)的角色查无此 id。
    matchmaking.PMS_TO_SESSION.clear()
    matchmaking.PMS_TO_SESSION[sess_a['pms_id']] = sess_a
    matchmaking.PMS_TO_SESSION[sess_b['pms_id']] = sess_b
    old_store = game_server.STORE
    game_server.STORE = FakeStore()
    try:
        got = await _stats_request([31, 49])
        check('两人局 stats：A(先入队)角色 31 勋带 13 正确归属',
              got.get(31) == 13, f'entries={got}')
        check('两人局 stats：B(后入队)角色 49 勋带 79 正确归属',
              got.get(49) == 79, f'entries={got}')
    finally:
        game_server.STORE = old_store

    # --- 用例 5：跨局并行 SquadRequest 兜底圈定本局 ---
    # 战斗服 M1 的连接（combat_mission=488464），f1=ZServer 自产 id（未命中）、
    # f2=0（未命中）→ 修复前全局"最新会话"= M2 后进玩家 → faction 错；
    # 修复后圈定 M1 → faction 用 M1 玩家的登记小队。
    # （M1 只留一个会话：f2 未命中时局内无法区分具体玩家，验证点在跨局。）
    from types import SimpleNamespace as NS
    mission, sess_a, sess_b = _seed_two_player_match()
    m2 = 488465
    sess_c = dict(sess_b, pms_id=903048748842679900,
                  battle_session_id=1189987113029180799,
                  squad_id=3721435839200754200,
                  mission_id=m2, player_id=33, char_id=77)
    matchmaking.PMS_TO_SESSION.pop(sess_b['pms_id'])
    matchmaking.PMS_TO_MISSION.pop(sess_b['pms_id'], None)
    matchmaking.PMS_TO_SESSION[sess_c['pms_id']] = sess_c
    matchmaking.PMS_TO_MISSION[sess_c['pms_id']] = m2
    resp_scope = await _squad_request(
        730433917 * 10**6 + m2, 0, sess=NS(combat_mission=mission))
    check('跨局 SquadRequest 兜底圈定本局（faction=M1 德军 2）',
          resp_scope['faction_template'] == 2 and resp_scope['team'] == 2,
          f'got team={resp_scope["team"]} factionTemplate='
          f'{resp_scope["faction_template"]}')
    # M1 会话全清（结算后残留请求）：宁缺毋错 → 单槽默认而非串到 M2
    matchmaking.PMS_TO_SESSION.pop(sess_a['pms_id'])
    resp_empty = await _squad_request(
        730433917 * 10**6 + m2, 0, sess=NS(combat_mission=mission))
    check('M1 会话清空后不串到 M2（faction=默认 3 且 squad 非成员队）',
          resp_empty['faction_template'] == 3
          and resp_empty['squad_id'] not in (sess_b['squad_id'],
                                             sess_c['squad_id']),
          f'got faction={resp_empty["faction_template"]} '
          f'squad={resp_empty["squad_id"]}')

    # --- 用例 6：GetPlayerSettings 跨局兜底圈定本局 ---
    # stale pms（rejoin/结算清理后 ZServer 仍持有）+ 战斗服 M1 连接 →
    # 修复前全局最新会话 = M2 玩家 → 身份/角色错配（"进游戏人变成 bot"路径）
    matchmaking.PMS_TO_SESSION.clear()
    matchmaking.PMS_TO_SESSION[sess_a['pms_id']] = sess_a
    matchmaking.PMS_TO_SESSION[sess_c['pms_id']] = sess_c
    matchmaking.PMS_TO_MISSION.clear()
    matchmaking.PMS_TO_MISSION[sess_a['pms_id']] = mission
    matchmaking.PMS_TO_MISSION[sess_c['pms_id']] = m2
    old_store = game_server.STORE
    game_server.STORE = None
    try:
        w = FakeWriter()
        stale_pms = 987654321
        await game_server.handle_request(
            NS(combat_mission=mission), w, 8, 'GetPlayerSettings',
            pb_varint(1, stale_pms))
        main, _ = Chunk.parse(w.data, 0)
        ps = _parse_varints(main.subs[0].subs[0].data)
        check('GetPlayerSettings stale pms 圈定本局（M1 玩家 2/角色 31）',
              ps.get(1) == 2 and ps.get(15) == 31,
              f'got player={ps.get(1)} char={ps.get(15)} '
              f'faction={ps.get(7)}')
        check('GetPlayerSettings 圈定本局阵营（M1 德军 2）',
              ps.get(7) == 2, f'got faction={ps.get(7)}')
        # 无 combat_mission（旧路径）→ 保持全局最新会话行为
        w2 = FakeWriter()
        await game_server.handle_request(None, w2, 9, 'GetPlayerSettings',
                                         pb_varint(1, stale_pms))
        main2, _ = Chunk.parse(w2.data, 0)
        ps2 = _parse_varints(main2.subs[0].subs[0].data)
        check('无 combat_mission 时保持旧回退（最新会话 M2 玩家 33）',
              ps2.get(1) == 33, f'got player={ps2.get(1)}')
    finally:
        game_server.STORE = old_store

    # --- 用例 7：击杀归属 bot 撞号过滤（ReportMissionStatus f3） ---
    # M1 roster 只有 pid=2；killer pid=3 是 bot（M2 里恰好有同号真实玩家）→
    # 修复前全局反查把 bot 击杀记到 M2 玩家角色上。
    from handlers import combat as combat_handlers
    matchmaking.PMS_TO_SESSION.clear()
    matchmaking.PMS_TO_SESSION[sess_a['pms_id']] = sess_a   # M1: pid=2, char=31
    matchmaking.PMS_TO_SESSION[sess_c['pms_id']] = sess_c   # M2: pid=33...
    # 构造 pid=3 的他局会话（撞号源）
    sess_d = dict(sess_b, pms_id=903048748842679910,
                  battle_session_id=1189987113029180798,
                  squad_id=3721435839200754201,
                  mission_id=488466, player_id=3, char_id=99)
    matchmaking.PMS_TO_SESSION[sess_d['pms_id']] = sess_d
    recorded = []

    class StatStore:
        async def add_stat(self, cid, key, val):
            recorded.append((cid, key, val))

    old_cstore = combat_handlers.STORE
    combat_handlers.STORE = StatStore()
    try:
        kill_rec_bot = pb_varint(1, 3) + pb_message(5, pb_varint(3, 2))
        kill_rec_real = pb_varint(1, 2) + pb_message(5, pb_varint(3, 1))
        proto = pb_message(3, kill_rec_bot) + pb_message(3, kill_rec_real)
        await combat_handlers.handle_report_mission_status(
            NS(combat_mission=mission), FakeWriter(), 10,
            'ReportMissionStatusRequest', proto)
        check('bot(pid=3 撞他局玩家号)击杀不入账，本局(pid=2)正常入账',
              recorded == [(31, 'kills', 1)],
              f'got recorded={recorded}')
    finally:
        combat_handlers.STORE = old_cstore

    # --- 用例 8：SquadResponse 发模板全量槽（late join 根因回归） ---
    # 战斗服对同一 squadId 只拉一次定义并缓存：按成员数发槽会让
    # late join 补队的成员分到不存在的槽 → "specified squad character
    # slot not found"（2026-09-02 hng_boot.log）。必须按
    # SquadPlayerSlotTemplate 表发模板全量槽。
    async def _squad_slots(mission_session_id, squad_id):
        proto = pb_varint(1, mission_session_id) + pb_varint(2, squad_id)
        w = FakeWriter()
        await game_server.handle_request(None, w, 6, 'SquadRequest', proto)
        main, _ = Chunk.parse(w.data, 0)
        resp = _parse_varints(main.subs[0].subs[0].data)
        squad_def = resp['_messages'][2]
        # 逐字段扫 f2 定义消息里的 repeated f5 playerSlots
        slots = []
        i = 0
        data = squad_def

        def _rv(pos):
            v = shift = 0
            while True:
                b = data[pos]
                pos += 1
                v |= (b & 0x7F) << shift
                shift += 7
                if not (b & 0x80):
                    return v, pos

        while i < len(data):
            key, i = _rv(i)
            fn, wt = key >> 3, key & 7
            if wt != 2:
                if wt == 0:
                    _, i = _rv(i)
                elif wt == 5:
                    i += 4
                elif wt == 1:
                    i += 8
                continue
            ln, i = _rv(i)
            body = data[i:i + ln]
            i += ln
            if fn == 5:
                f = _parse_varints(body)
                slots.append((f.get(1), f.get(2)))
        return resp.get(1), _parse_varints(squad_def).get(2), slots

    mission, sess_a, sess_b = _seed_two_player_match()
    sid, tmpl, slots = await _squad_slots(sess_a['pms_id'],
                                          sess_a['squad_id'])
    check('步兵小队(1 成员)发模板全量 4 槽（late join slot 1-3 可命中）',
          tmpl == 1 and slots == [(1, 0), (2, 1), (3, 2), (4, 3)],
          f'got template={tmpl} slots={slots}')
    # 侦察小队（template 5，表 2 槽）
    recon_sq = 3721435839200754999
    matchmaking._MM_MISSION_SQUADS[mission][recon_sq] = {
        'name': 'Recon Squad', 'leader_char_id': 49, 'faction': 3,
        'squad_db_id': 0, 'template': 5,
        'members': [{'player_id': 11, 'char_id': 49, 'gamertag': 'x',
                     'rank': 1, 'slot': 0}]}
    sid, tmpl, slots = await _squad_slots(sess_b['pms_id'], recon_sq)
    check('侦察小队发模板全量 2 槽且 squadTemplateId=5',
          tmpl == 5 and slots == [(21, 0), (22, 1)],
          f'got template={tmpl} slots={slots}')
    # 装甲小队 4 成员超模板 3 槽 → 补第 4 槽（沿用最后一行槽模板）
    armor_sq = 3721435839200754888
    matchmaking._MM_MISSION_SQUADS[mission][armor_sq] = {
        'name': 'Armor Squad', 'leader_char_id': 60, 'faction': 2,
        'squad_db_id': 0, 'template': 4,
        'members': [{'player_id': p, 'char_id': 60 + p, 'gamertag': 'a',
                     'rank': 1, 'slot': p - 1} for p in (1, 2, 3, 4)]}
    sid, tmpl, slots = await _squad_slots(sess_a['pms_id'], armor_sq)
    check('装甲小队 4 成员超模板 3 槽 → 补第 4 槽',
          tmpl == 4 and slots == [(17, 0), (18, 1), (19, 2), (19, 3)],
          f'got template={tmpl} slots={slots}')
    # 无登记表（兜底路径）→ 回归冒烟（响应可解析、字段齐全）
    matchmaking.PMS_TO_SESSION.clear()
    w = FakeWriter()
    await game_server.handle_request(None, w, 6, 'SquadRequest',
                                     pb_varint(1, 1) + pb_varint(2, 0))
    main, _ = Chunk.parse(w.data, 0)
    resp = _parse_varints(main.subs[0].subs[0].data)
    check('无登记表兜底仍回有效 SquadResponse（动态 squadId 非零）',
          int(resp.get(1) or 0) != 0 and '_messages' in resp
          and 2 in resp.get('_messages', {}))

    # --- 清理 ---
    matchmaking.PMS_TO_SESSION.clear()
    matchmaking.PMS_TO_MISSION.clear()
    matchmaking._MM_MISSION_SQUADS.clear()


def main():
    asyncio.run(run())
    ok = all(r[1] for r in RESULTS)
    print('\nRESULT:', 'PASS' if ok else 'FAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
