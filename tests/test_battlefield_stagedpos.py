"""ResponseBattleFieldSettings f29/f30 stagedPos 回归测试（2026-08-28 缩放错位）。

事故：stagedPosX/stagedPosY 恒硬编码 0.0 → 战斗内 SpawnMenu 的
IngameEuropeMapOverlay 欧洲地图缩放动画定位到左上角，与大厅地图位置不一致。

修复后语义：f29/f30 = 本局 preset → gamemap → battlefield 行坐标（与
简报 GetMissionDetailsResponse f8/f9、大厅地图标记同源同值）；遭遇战图
（Mill 等无 battlefield 行）回退 Benoit Village 锚点（112741325）。

验证：
1) 回退路径：preset=10 (Mill=13) → f29/f30 = battlefield_pos(锚点) ≠ 0
2) 战役图路径：preset 26/27/28/29/1345335 → f29/f30 = 对应 battlefield 行
   坐标，且各图互不相同（证明按 preset 取本局坐标，非固定值）
3) 与简报同源：AI 局 _msg_mission_details_response f8/f9（回退锚点）与
   同 preset 的 BattleFieldSettings f29/f30 一致
4) 兜底：wardb 不可用（battlefield_pos → None）时消息仍可构建（0.0 不崩）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol import pb_field_payload, pb_parse_loose, pb_split_top
import matchmaking
import warserver_messages
from wardb import battlefield_entity_for_gamemap, battlefield_pos


ok = fail = 0


def check(desc, cond, detail=''):
    global ok, fail
    if cond:
        ok += 1
        print(f'  PASS {desc}')
    else:
        fail += 1
        print(f'  FAIL {desc} {detail}')


def _staged_pos(preset_id):
    """构建 BattleFieldSettings 并解出 (f29, f30)。"""
    m = warserver_messages._msg_response_battlefield_settings(preset_id)
    f = pb_parse_loose(m)
    return f.get(29), f.get(30)


def _gmd_pos(mission_id=300001):
    """AI 局简报 GetMissionDetailsResponse 的 (f8, f9)。"""
    det = {'player_id': 1, 'gamertag': 'tester', 'faction': 3}
    m = matchmaking._msg_mission_details_response(
        mission_id, det['player_id'], det['gamertag'], 'zulu',
        player_faction=det['faction'])
    f = pb_parse_loose(m)
    return f.get(8), f.get(9)


print('== 1) 回退路径：遭遇战 preset=10 (Mill=13，无 battlefield 行)')
anchor = battlefield_pos(112741325)      # Benoit Village 锚点
sx, sy = _staged_pos(10)
if anchor is None:
    print('  SKIP 本机 WarDatabase 不可用（battlefield_pos → None），真实值断言跳过')
else:
    check('f29/f30 = Benoit Village 锚点坐标',
          abs(sx - anchor[0]) < 0.01 and abs(sy - anchor[1]) < 0.01,
          f'got=({sx},{sy}) want={anchor}')
check('f29/f30 非 0（不再缩到左上角）',
      sx != 0 and sy != 0, f'got=({sx},{sy})')

print('== 2) 战役图路径：按 preset 取本局 battlefield 行坐标')
# (preset_id, gamemap)：Airfield / Tank Factory / Forward Airfield /
# Mountain Village / Medium French Village
CAMPAIGN_PRESETS = [(26, 204), (27, 203), (28, 205), (29, 201), (1345335, 70439)]
seen = set()
for pid, gm in CAMPAIGN_PRESETS:
    want = battlefield_pos(battlefield_entity_for_gamemap(gm))
    got = _staged_pos(pid)
    if want is None:
        print(f'  SKIP preset={pid} gamemap={gm} 无坐标（wardb 缺行）')
        continue
    check(f'preset={pid} (gamemap={gm}) f29/f30 = battlefield 行坐标',
          abs(got[0] - want[0]) < 0.01 and abs(got[1] - want[1]) < 0.01,
          f'got={got} want={want}')
    seen.add(got)
check('各战役图坐标互不相同（按本局取值，非固定值）',
      len(seen) == len(CAMPAIGN_PRESETS), f'distinct={len(seen)}')

print('== 3) 与简报 GetMissionDetailsResponse f8/f9 同源（大厅/战斗一致）')
gmd = _gmd_pos()
sx10, sy10 = _staged_pos(10)
if anchor is None or gmd[0] is None:
    print('  SKIP 本机 WarDatabase 不可用，同源断言跳过')
else:
    check('GMD f8/f9 与 BattleFieldSettings f29/f30 同值（Mill 回退锚点）',
          abs(gmd[0] - sx10) < 0.01 and abs(gmd[1] - sy10) < 0.01,
          f'gmd={gmd} bfs=({sx10},{sy10})')

print('== 4) 兜底：wardb 不可用时（battlefield_pos → None）不崩、输出 0.0')
orig = warserver_messages.battlefield_pos
warserver_messages.battlefield_pos = lambda eid: None
try:
    bx, by = _staged_pos(10)
    check('None → (0.0, 0.0)，消息构建不抛异常',
          bx == 0.0 and by == 0.0, f'got=({bx},{by})')
finally:
    warserver_messages.battlefield_pos = orig

print('== 5) 出兵点攻防归属：大厅简报 f6 ↔ 战斗 layout / f12 同源（不互换）')
# 2026-08-28 事故：_accesspoint_layout 交替起始=defender，大厅 f6 起始=attacker
# → 大厅概览图攻防图标位置与进战局后实际出生侧互换。修复后三方必须同规则。
from warserver_messages import _accesspoint_layout

_det = {'preset': 10, 'attacker_faction': 3, 'defender_faction': 2,
        'players': [{'player_id': 1, 'gamertag': 'atk1', 'faction': 3}],
        'map_entity_id': 112741325}
_mm = matchmaking._msg_mission_details_response_multi(990001, _det, 'zulu')
def _repeated_map(raw, fn, kf, vf):
    """顶层拆 repeated 子消息 → {kf 字段值: vf 字段值}（pb_parse_loose 单值
    覆盖语义收集不了 repeated，须 pb_split_top 保序拆块）。"""
    out = {}
    for _fn, block in pb_split_top(raw):
        if _fn != fn:
            continue
        _ef = pb_parse_loose(pb_field_payload(block))
        out[int(_ef.get(kf))] = int(_ef.get(vf))
    return out


_f6 = _repeated_map(_mm, 6, 1, 2)                   # {模板: factionTemplateId}
_layout = _accesspoint_layout(10, 2, 3)
check('layout 首点=进攻方、次点=防守方（交替起始=attacker）',
      _layout and _layout[0][1] == 3 and
      (len(_layout) < 2 or _layout[1][1] == 2),
      f'layout={_layout}')
check('大厅 f6 与战斗 layout 逐点同 owner',
      _f6 and len(_f6) == len(_layout) and
      all(_f6.get(int(t)) == int(o) for t, o in _layout),
      f'f6={_f6} layout={_layout}')
_bf = warserver_messages._msg_response_battlefield_settings(
    10, 2, 3, True, False, 1, 0, None, counts_by_faction=None)
_f12 = _repeated_map(_bf, 12, 3, 1)                 # f3=模板 f1=owner
check('BattleFieldSettings f12 accesspoints owner 与 layout 同值',
      _f12 and len(_f12) == len(_layout) and
      all(_f12.get(int(t)) == int(o) for t, o in _layout),
      f'f12={_f12} layout={_layout}')

print('== 5b) Assault 图（Airfield preset 26）：末点=防守方 其余=进攻方 + f7 O点=防守方')
# assault-map-spawn-investigation.md §6.1/§6.2：Encounter 交替；Assault 出兵点
# 归进攻方、★末点留防守方（引擎判负条件：0 出兵点 → "has lost their
# accesspoints" 秒判负 + AI 小队无处部署，0831 实测）；f7 O 点=防守方。
from wardb import (_load_objective_capturepoints, _load_preset as _lp,
                   _missiontype_for_gamemap)
if _missiontype_for_gamemap(int(_lp(26).get(3, 0) or 0)) != 1:
    print('  SKIP 本机 WarDatabase 无 Airfield 行（preset 26 回退 Encounter），Assault 分支跳过')
else:
    _lay26 = _accesspoint_layout(26, 2, 3)
    check('Airfield layout 末点=防守方(2) 其余=进攻方(3)',
          _lay26 and len(_lay26) >= 3 and
          all(o == (2 if i == len(_lay26) - 1 else 3)
              for i, (_, o) in enumerate(_lay26)),
          f'layout={_lay26}')
    _det26 = {'preset': 26, 'attacker_faction': 3, 'defender_faction': 2,
              'players': [{'player_id': 1, 'gamertag': 'atk1', 'faction': 3}],
              'map_entity_id': 112741325}
    _mm26 = matchmaking._msg_mission_details_response_multi(990002, _det26, 'zulu')
    _f6a = _repeated_map(_mm26, 6, 1, 2)
    check('大厅 f6 Assault 末点=防守方 其余=进攻方 且与 layout 同源',
          _f6a and len(_f6a) == len(_lay26) and
          all(_f6a.get(int(t)) == int(o) for t, o in _lay26),
          f'f6={_f6a} layout={_lay26}')
    _bf26 = warserver_messages._msg_response_battlefield_settings(
        26, 2, 3, True, False, 1, 0, None, counts_by_faction=None)
    _f12a = _repeated_map(_bf26, 12, 3, 1)
    check('BattleFieldSettings f12 Assault 与 layout 同源（末点=防守方）',
          _f12a and len(_f12a) == len(_lay26) and
          all(_f12a.get(int(t)) == int(o) for t, o in _lay26),
          f'f12={_f12a} layout={_lay26}')
    _objs = set(_load_objective_capturepoints(int(_lp(26).get(3, 0) or 0)))
    check('Airfield O 点识别非空（CapturepointTemplate f4 objectiveId）',
          bool(_objs), f'objs={_objs}')
    _f7a = _repeated_map(_mm26, 7, 3, 4)             # f3=模板 f4=owner
    check('大厅 f7 O点=防守方(2)、其余=0 中立',
          _f7a and all(v == (2 if int(k) in _objs else 0)
                       for k, v in _f7a.items()),
          f'f7={_f7a} objs={_objs}')

print(f'\nresult: ok={ok} fail={fail}')
sys.exit(1 if fail else 0)
