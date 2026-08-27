# -*- coding: utf-8 -*-
"""临时复现：直接匹配 A(德军2)+B(苏军3) → GMD f3 分组解码。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import matchmaking

# macOS 无 WarDatabase：stub 掉出兵点/坐标查询（不影响 f3 阵营分组验证）
matchmaking._mission_accesspoints = lambda gamemap_id: [8, 15, 21, 22]
matchmaking.battlefield_pos = lambda mid: (120.0, 240.0)


def parse_fields(data):
    out = {}

    def read_varint(i):
        v = s = 0
        while True:
            b = data[i]
            i += 1
            v |= (b & 0x7F) << s
            s += 7
            if not (b & 0x80):
                return v, i

    i = 0
    while i < len(data):
        key, i = read_varint(i)
        fn, wt = key >> 3, key & 7
        if wt == 0:
            v, i = read_varint(i)
            out.setdefault(fn, []).append(v)
        elif wt == 2:
            ln, i = read_varint(i)
            out.setdefault(fn, []).append(data[i:i + ln])
            i += ln
        else:
            break
    return out


def main():
    mission = 488464
    matchmaking._MM_MISSION_DETAILS[mission] = {
        'preset': 29, 'gamemode': 4, 'ai_fill': False,
        'map_entity_id': 1,
        'attacker_faction': 2, 'defender_faction': 3,
        'enable_bots': False, 'formed_at': 0,
        'weather_tod': 2, 'weather_condition': 0, 'timeofday': 12.0,
        'players': [
            {'player_id': 2, 'gamertag': 'zhai33', 'char_id': 31,
             'faction': 2, 'pms_id': 9001},
            {'player_id': 11, 'gamertag': 'summercatOVO', 'char_id': 49,
             'faction': 3, 'pms_id': 9002},
        ],
    }
    try:
        matchmaking._init_mission_resources(mission, 4, 2, 3)
    except Exception as ex:
        print('init resources failed:', ex)

    resp = matchmaking._msg_mission_details_response_multi(
        mission, matchmaking._MM_MISSION_DETAILS[mission], '20260827120000Z')

    f = parse_fields(resp)
    print('response code f1 =', f.get(1))
    info = parse_fields(f[2][0])
    print('MissionInfo f8 defenderFactionId =', info.get(8))
    row_to_tmpl = {rid: tmpl for tmpl, rid in
                   matchmaking.FACTION_ROW_IDS.items()}
    print('faction rows (tmpl -> row id):')
    for tmpl, rid in matchmaking.FACTION_ROW_IDS.items():
        print('  tmpl {} -> row {}'.format(tmpl, rid))

    ok = True
    entries = [parse_fields(e) for e in f.get(3, [])]
    print('f3 factions:')
    tmpls = []
    all_players = []
    for ef in entries:
        fid = ef.get(1, [0])[0]
        players = [parse_fields(p).get(1, [b''])[0].decode()
                  for p in ef.get(2, [])]
        tmpl = row_to_tmpl.get(fid)
        tmpls.append(tmpl)
        all_players += players
        print('  factionId={} -> tmpl={} players={}'.format(fid, tmpl, players))
    if sorted(t for t in tmpls if t) != [2, 3]:
        print('FAIL: 两个 f3 组不是 GE/SU:', tmpls)
        ok = False
    if sorted(all_players) != ['summercatOVO', 'zhai33']:
        print('FAIL: 名单不对:', all_players)
        ok = False
    for ef in entries:
        fid = ef.get(1, [0])[0]
        players = [parse_fields(p).get(1, [b''])[0].decode()
                   for p in ef.get(2, [])]
        if row_to_tmpl.get(fid) == 2 and 'zhai33' not in players:
            print('FAIL: 德军组没有 A:', players)
            ok = False
        if row_to_tmpl.get(fid) == 3 and 'summercatOVO' not in players:
            print('FAIL: 苏军组没有 B:', players)
            ok = False
    print('RESULT:', 'PASS' if ok else 'FAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
