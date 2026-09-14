# -*- coding: utf-8 -*-
"""战争图城市点击 → 战局详情链路回归（2026-09-08）。

客户端有会战城市点击 → GetMissionDetailsRequest{f1=0, f2=battleId}
（HandleWarBattleCommand 恒发 missionId=0）。此前服务端只读 f1 →
恒查不到 → 回 43 → MissionDetailsWindow 秒关（用户报"点不开城市详情"）。

测试注入 state.STORE 验证 f2 分支（参照 test_battlepass.py 注入模式）。
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import state
import mm_handler
from protocol import pb_int32, pb_parse, build_response, pb_message


class FakeWriter:
    def __init__(self):
        self.buf = b''

    def write(self, data):
        self.buf += data

    async def drain(self):
        pass


class FakeSess:
    def __init__(self):
        self.player_id = 42


class FakeBattleStore:
    """get_mission_by_battle / get_mission_record 的最小 mock。"""

    def __init__(self, by_battle=None, by_mid=None, battles=None, nodes=None):
        self.by_battle = by_battle or {}
        self.by_mid = by_mid or {}
        self.battles = battles or {}
        self.nodes = nodes or {}

    async def get_mission_by_battle(self, battle_id, states=(0, 1)):
        return self.by_battle.get(int(battle_id))

    async def get_mission_record(self, mid):
        return self.by_mid.get(int(mid))

    async def get_war_battle(self, battle_id):
        return self.battles.get(int(battle_id))

    async def get_command_nodes_by_ids(self, ids):
        return [self.nodes[int(i)] for i in ids if int(i) in self.nodes]


async def _call(proto):
    """调 _handle_mission_details 返回 (writer, 响应解析结果)。"""
    w = FakeWriter()
    sess = FakeSess()
    await mm_handler._handle_mission_details(sess, w, 900, proto)
    # build_response 的响应体在 frame 里；pb 解析 write 的字节提取 proto
    # （响应 = frame 头 + GetMissionDetailsResponse proto；直接用
    # mm_handler 的回退路径验证：43 = resp proto 第一字段）
    return w


def _is_not_found(writer):
    """响应是否 mission_not_found(43)：pb_int32(1, 43) 编码 = b'\\x08\\x2b'。

    （frame 头干扰 varint 扫描，改用字节序列判定。）"""
    return b'\x08\x2b' in writer.buf


async def main():
    ok = 0
    fail = 0

    def check(name, cond, detail=''):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f'  PASS {name}')
        else:
            fail += 1
            print(f'  FAIL {name} {detail}')

    rec = {'id': 4242, 'name': 'war battle', 'preset': 3, 'state': 1,
           'attacker_faction': 1, 'defender_faction': 2,
           'owner_player_id': 42, 'map_entity_id': 20, 'battle_id': 77}

    # T1: f1=0 + f2=battleId → 按 battle 反查 record → 成功响应（≠43）
    orig_store = state.STORE
    wb = {'id': 77, 'war_id': 1, 'battle_type': 1, 'battlefield_id': 20,
          'started_at': None,
          'factions': [{'faction': 1, 'at_id': 901}, {'faction': 2, 'at_id': 902}]}
    at1 = {'id': 901, 'template_id': 104, 'state': 0, 'faction': 1,
           'owner_player_id': 42, 'name': 'Inf A',
           'resources': {1: 90}}
    at2 = {'id': 902, 'template_id': 204, 'state': 0, 'faction': 2,
           'owner_player_id': 43, 'name': 'Inf B',
           'resources': {1: 120}}
    state.STORE = FakeBattleStore(
        by_battle={77: rec}, battles={77: wb}, nodes={901: at1, 902: at2})
    # 拦截渲染捕获 (mission_id, details)
    calls = []
    orig_render = mm_handler._msg_mission_details_response_multi

    def _cap(mid_arg, det_arg, zulu_arg):
        calls.append((mid_arg, det_arg))
        return orig_render(mid_arg, det_arg, zulu_arg)

    mm_handler._msg_mission_details_response_multi = _cap
    try:
        w = await _call(pb_int32(1, 0) + pb_int32(2, 77))
        check('T1 城市点击链（f1=0,f2=battleId）非 mission_not_found',
              not _is_not_found(w))
        check('T1b missionId 回填 == record.id（修复 1：非 0）',
              calls and calls[0][0] == 4242,
              f'calls={[(m, d.get("war_pending")) for m, d in calls]}')
        det = calls[0][1] if calls else {}
        check('T1c f5 资源 = roster 实际存量（非 FALLBACK 432/60/24）',
              det.get('war_counts') == {1: {1: 90}, 2: {1: 120}},
              f'counts={det.get("war_counts")}')
        check('T1d f4 名单 = 真实 AT（非写死步兵名单）',
              det.get('war_at_rows') == {1: [at1], 2: [at2]},
              f'ats={det.get("war_at_rows")}')
        check('T1e entity = battle 行口径（城战 bf/1）',
              det.get('entity_id') == 20 and det.get('entity_type') == 1,
              f'ent={det.get("entity_id")}/{det.get("entity_type")}')

        # T1f: 成局后复用 live det（真实 players 名单，mid 路由生效）
        live_det = {'battle_id': 77, 'preset': 3, 'players':
                    [{'player_id': 42, 'gamertag': 'p1', 'faction': 1}],
                    'attacker_faction': 1, 'defender_faction': 2,
                    'map_entity_id': 20}
        mm_handler._MM_MISSION_DETAILS[4242] = live_det
        calls.clear()
        w = await _call(pb_int32(1, 0) + pb_int32(2, 77))
        check('T1f 成局后复用 live det + mid 路由',
              calls and calls[0][0] == 4242 and calls[0][1] is live_det,
              f'calls={[(m, d is live_det) for m, d in calls]}')
        del mm_handler._MM_MISSION_DETAILS[4242]
    finally:
        mm_handler._msg_mission_details_response_multi = orig_render
        state.STORE = orig_store

    # T4: 野外遭遇局（battle_type=3）→ MissionInfo f5=sl id / f6=3（§三.5 B1）
    from handlers.mission import _msg_mission_info_from_record
    from war_campaign.common import battle_map_entity
    wb_f = {'id': 78, 'battle_type': 3, 'battlefield_id': 555}
    ent = battle_map_entity(wb_f)
    rec_f = {'id': 5000, 'preset': 10, 'state': 0,
             'attacker_faction': 1, 'defender_faction': 2,
             'map_entity_id': 20, 'battle_id': 78}
    parsed = pb_parse(_msg_mission_info_from_record(rec_f, 'z', entity=ent))
    check('T4 野外局 f5=sl id / f6=3（客户端拼"城市名-字母"）',
          parsed[5] == 555 and parsed[6] == 3,
          f'f5={parsed.get(5)} f6={parsed.get(6)}')
    # 城战 / 无 entity 回退 record.map_entity_id / 1
    parsed2 = pb_parse(_msg_mission_info_from_record(rec_f, 'z'))
    check('T4b 无 entity 回退 record map_entity_id/1',
          parsed2[5] == 20 and parsed2[6] == 1)

    # T5: _at_composition 池口径 = AT 实际存量优先（修复 3）
    from matchmaking import _at_composition
    import force_pool
    comp = _at_composition({'template_id': 104, 'resources': {1: 33, 4: 10}})
    check('T5 池编制随 AT 实际存量（非模板满编）',
          comp == {1: 33, 4: 10}, f'comp={comp}')
    comp2 = _at_composition({'template_id': 104, 'resources': {}})
    full = force_pool.composition_for_template(104)
    check('T5b resources 缺省回退模板满编', comp2 == full,
          f'comp2={comp2} full={full}')

    # T2: f1=0 + f2=0 + 无匹配 → 43（维持原语义）
    state.STORE = FakeBattleStore()
    try:
        w = await _call(pb_int32(1, 0))
        check('T2 空请求维持 43', _is_not_found(w))
    finally:
        state.STORE = orig_store

    # T3: f1=0 + f2=battleId 但 battle 无 record → 43
    state.STORE = FakeBattleStore()
    try:
        w = await _call(pb_int32(1, 0) + pb_int32(2, 999))
        check('T3 battle 无关联 record → 43', _is_not_found(w))
    finally:
        state.STORE = orig_store

    print(f'\n==== war_battle_details 回归：{ok}/{ok + fail} PASS ====')
    if fail:
        sys.exit(1)


if __name__ == '__main__':
    asyncio.run(main())
