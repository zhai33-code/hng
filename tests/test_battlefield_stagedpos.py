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

from protocol import pb_parse_loose
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

print(f'\nresult: ok={ok} fail={fail}')
sys.exit(1 if fail else 0)
