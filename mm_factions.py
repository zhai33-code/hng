"""faction / 统帅部 / 资源 / 队列 proto 行构建（mm_factions）。

faction 表行 id 约定（★必须 == factiontemplateid 1/2/3，推导见下方注释）、
统帅部 CommandNode 行、FactionResourceProduction/Consumption 与
FactionResourceQueue 行构建。从 matchmaking.py 拆出
（2026-09-06 重构，纯移动行为不变）。
"""
import random
import time

from protocol import pb_float, pb_int32, pb_int64, pb_string
from wardb import (army_resource_categories, armyresource_production,
                   commandnode_template)
from mm_state import _WAR_ID, _log


# ===== 他服私服 AI 战局实测值（2026-08-18 抓包：双人局 ai_battle_15130.pcapng / 单人局 match_to_game.pcapng）=====
# 单人局（match_to_game.pcapng, 2026-08-18 21:23, DoubleBird 单人 vs AI 苏德）：
#   GetMissionDetailsResponse 758B 完整 dump 见 solo_gmd_dump.txt / solo_gmd_response.bin
#   我方 6 队（303×2/320×2/316×2）+ AI 6 队（203×2/220×2/216×2），资源 20 条
#   资源总量：步兵 90×4+36×2=432、汽车 30×2=60、摩托 12×2=24（与游戏内一致）

# faction 表行 id：★必须 == factiontemplateid（1=US/2=GE/3=SU），不能随机大数。
# 反编译实证（客户端三个调用点传不同 id 空间给同一比较参数，同时成立唯有此解）：
#   ① MissionDetailsMediator:259 setPlayersInMatch(…, factionTemplateId)→ 与
#      faction.factiontemplateid 比较（模板 id 空间）
#   ② MissionInformationWindowMediator:206 传 PMS f5 → 既要 getFromId(f5) 可查
#      （行 id 空间）又要 Equals(factiontemplateid) 成立（模板 id 空间）
#   ③ MissionsOverviewWindowMediator:255 传 getFactionIdFromTemplateId()（行 id）
#   ④ MissionUtilities.updateFactionCounts: currentPlayer.factionid(player 行 f11
#      =1/2/3) 与 faction 行 id Equals → 行 id 必须 ∈ {1,2,3}
# 随机大数时所有"我方/敌方"判定永不相等：friendCount 永不命中、enemyCount 累加
# 双方全部玩家（跨阵营俩人大厅全算"敌方"=同阵营观感）、简报我方不高亮、
# setBasicInformation 恒判进攻方（2026-09-01 玩家反馈实锤）。
# 客户端 FactionTable(xmlTag='faction') 只有收到这些 set 才有数据；行 id 与
# GMD/PMS/社交列表引用同源即可（getFromId 有效性不受数值大小影响）。
FACTION_ROW_IDS = {tmpl: tmpl for tmpl in (1, 2, 3)}

# Assault 简报守方阵营 logo 锚点：Mill A 点（全库无据点挂靠 AP）——客户端
# setupMissionAssaultTeamOnMap 次选路径把 logo 画在地图中心（O 点区域，
# BattlefieldUtilities.as L99-105）；f4 仅驱动大厅 logo 定位，战斗侧
# combat.py f5 home_ap 仍锚真实守方线出兵点（引擎判负/部署必需）
ASSAULT_DEFENDER_LOGO_AP = 8
FACTION_SUPREME_COMMANDS = {tmpl: random.getrandbits(63) for tmpl in (1, 2, 3)}
# 与上面配对的 CommandNodeDefinition 行 id（客户端 CommandnodeTable 由
# CommandNodeDefinition + CommandNodeWarInstance 两行 join 而成）
FACTION_SUPREME_COMMAND_DEFS = {tmpl: random.getrandbits(63) for tmpl in (1, 2, 3)}


def _rival_faction(faction):
    """单阵营局的 AI 对手阵营：另外两阵营等概率随机抽一。

    原先该规则在 _split_factions(L921) 与 AI 局简报(L850) 各写一份
    `2 if x != 2 else 3`——改一漏一即错位，收敛到这一处。
    ★ 随机值只在成局时由 _split_factions 掷一次并写入
    _MM_MISSION_DETAILS（attacker_faction/defender_faction），简报 f8/
    战斗服 BattleFieldSettings/资源池/late-join 全读存储值，不再重掷
    （避免简报与实际战局阵营错位）。
    """
    f = int(faction)
    return random.choice([x for x in (1, 2, 3) if x != f])


def _faction_pair(det):
    """读取战局详情的攻防两阵营，字段缺失/非法时报错并返回 None。

    ★ 与 _find_joinable_mission 同一教训（2026-08-27 美德局漏放苏军）：
    det 阵营字段默认值（3=SU/2=GE）看似无害，实为静默放行错阵营——
    BattleFieldSettings 会按默认阵营配出生侧（判负+观战）、简报 f8 攻防
    错位、FACTION_ROW_IDS.get 回退错行 id（客户端 #1009）。凡读取方
    拿到 None 必须走"拒绝/回退"路径，不允许再用默认值渲染。
    """
    atk = int(det.get('attacker_faction', 0) or 0)
    dfd = int(det.get('defender_faction', 0) or 0)
    if not (atk in (1, 2, 3) and dfd in (1, 2, 3) and atk != dfd):
        _log(f'ERROR: mission details faction fields invalid: '
             f'attacker={atk}, defender={dfd} (det={det.get("preset")})'
             f' - refusing defaults')
        return None
    return atk, dfd


def _msg_faction_rows(mults=None):
    """KVS 'faction' 表 3 行（US/GE/SU，结构按他服 solo_kvcs_dump2 S2C#17 复刻）。

    mults：#1 underdog 倍率 {faction: (xp_mult, wf_mult)}（war_campaign.
    current_underdog_mults），写入 f6/f7 underdogBonus*Multiplier——
    客户端阵营选择窗按 >1 显示"当前弱势阵营加成"；缺省全 1.0。"""
    rows = []
    for tmpl, fid in FACTION_ROW_IDS.items():
        xp_m, wf_m = (mults or {}).get(tmpl, (1.0, 1.0))
        m = pb_int64(1, fid)                                   # id
        m += pb_int64(2, tmpl)                                 # factiontemplateid
        m += pb_int64(4, FACTION_SUPREME_COMMANDS[tmpl])       # supremecommand
        m += pb_int64(5, _WAR_ID)                              # war（本服运行时战争 id）
        m += pb_float(6, float(xp_m))                          # underdogBonusXpMultiplier
        m += pb_float(7, float(wf_m))                          # underdogBonusWarfundsMultiplier
        rows.append(m)
    return rows


def _msg_supreme_command_rows():
    """KVS 'CommandNodeDefinition' + 'CommandNodeWarInstance' 行（三阵营最高司令部）。

    客户端 CommandnodeTable 由 CommandNodeDefinitionTable × CommandNodeWarInstanceTable
    两行按 commandNodeDefinitionId join 而成；战争图"阵营资源总览"窗口
    （FactionResourceOverviewMediator.setupProductionOverview）查
    getFromId(faction.supremecommand) 返回 null 时会 dispatch
    subscribeResourceViewSignal 重订 resourceview 再查——缺这两行数据时
    形成每秒上千次 unsub/sub 的请求死循环（2026-09-01 realtest_game.log
    80 分钟 16 万行事故）。两行都推即可让查找命中、循环终止。
    行结构按反编译 proto-as3-classes/HnG_States 字段号编码。
    """
    defs, instances = [], []
    now_ms = int(time.time() * 1000)
    for tmpl, fid in FACTION_ROW_IDS.items():
        d = pb_int64(1, FACTION_SUPREME_COMMAND_DEFS[tmpl])    # id
        d += pb_string(2, 'Supreme Command')                    # name
        d += pb_string(3, 'HQ')                                 # abbreviation
        d += pb_int64(4, 0)                                     # ownerPlayerId
        d += pb_int64(5, tmpl)                                   # factionTemplateId
        d += pb_int64(6, commandnode_template(tmpl, 'infantry'))  # commandNodeTemplateId
        d += pb_int32(7, 0)                                     # rank
        d += pb_int64(8, 0)                                     # supplyResourceCategoryId
        d += pb_int64(9, 0)                                     # generalCharacterId
        d += pb_int64(10, now_ms)                               # statUpdateTimeStamp
        d += pb_int32(11, 0)                                    # suppliedOnCreation
        defs.append(d)

        w = pb_int64(1, FACTION_SUPREME_COMMANDS[tmpl])         # id（=faction.supremecommand）
        w += pb_int64(2, FACTION_SUPREME_COMMAND_DEFS[tmpl])    # commandNodeDefinitionId
        w += pb_int64(3, 0)                                     # parentCommandNodeInstanceId
        w += pb_int64(4, fid)                                   # factionId
        w += pb_int64(5, 0)                                     # mapEntityId
        w += pb_int64(6, 3)                                     # mapEntityTypeId（3=SUPPLYLINE）
        w += pb_int64(7, _WAR_ID)                               # warId
        w += pb_int32(8, 0)                                     # morale
        w += pb_int64(9, now_ms)                                # lastReset
        w += pb_int32(10, 1)                                    # isDeployed
        w += pb_int32(11, 0)                                    # hasGottenSoldierResourcesForFree
        w += pb_int32(12, 0)                                    # hasGottenVehicleResourcesForFree
        w += pb_int32(13, 100)                                  # maxMorale
        w += pb_float(14, 1.0)                                  # moraleGainModifier
        instances.append(w)
    return defs, instances


def _queue_window(queue_windows, faction, category=None):
    value = (queue_windows or {}).get(faction, (0, 0))
    if not isinstance(value, dict):       # compatibility with old callers/tests
        return value
    if category is None:
        return value.get('overall', (0, 0))
    return value.get('categories', {}).get(category, (0, 0))


def _msg_faction_resource_rows(queue_windows=None, next_production_ms=None):
    """KVS 'FactionResourceProduction' + 'FactionResourceConsumption' 行
    （三阵营 × 全部 armyresourcecategory 分类）：阵营资源总览窗口数据源，
    缺 FactionResourceProduction 行时客户端每分类 dd.error 并留空。

    官服语义（2026-09-02 实锤，docs/hng-rts-protocol.md §3.12.1）：产量/
    间隔/开战库存取 WarDatabase ArmyResourceProductionTemplate（三阵营数值
    相同：步兵 900/85 万、重坦 2/225、战斗机 3/1800……），间隔全表恒 600s。
    此前误用 armyresourcecategory f6（speed）×10 当产量、间隔取 1h，已废弃。
    时间戳为 Zulu 'YYYYMMDDHHmmss'（TimeUtilities.convertZuluTimeToMilliseconds），
    nextRunTimeStamp 沿战争 production_ms 游标推进，不因每次重推而重置。
    queue_windows 同时含阵营总窗口与各资源类别窗口；消耗行 f8/f9 使用
    自己类别的窗口（M3-2，空=0）。"""
    prods, cons = [], []
    templates = {(t['faction'], t['category_id']): t
                 for t in armyresource_production()}
    now = time.time()
    prev_ts = time.strftime('%Y%m%d%H%M%S', time.gmtime(now))
    for tmpl, fid in FACTION_ROW_IDS.items():
        hq = FACTION_SUPREME_COMMANDS[tmpl]
        for cat, _name in army_resource_categories():
            t = templates.get((tmpl, cat))
            interval = int(t['run_every_seconds']) if t else 0
            target = ((float(next_production_ms) / 1000.0 + interval)
                      if next_production_ms else now + interval)
            while interval > 0 and target <= now:
                target += interval
            next_ts = time.strftime('%Y%m%d%H%M%S', time.gmtime(target))
            p = pb_int64(1, fid * 1000 + cat)                # id（faction×分类 唯一）
            p += pb_int64(2, fid)                              # factionId
            p += pb_int64(3, _WAR_ID)                          # warId
            p += pb_int64(4, hq)                               # factionHQCommandNodeWarInstanceId
            p += pb_int64(5, cat)                              # armyResourceCategoryId
            p += pb_int64(6, t['id'] if t else 0)              # armyResourceProductionTemplateId
            p += pb_string(7, next_ts)                         # nextRunTimeStamp
            p += pb_int32(8, t['produce_per_run'] if t else 0)  # nextProductionValue（=模板 produceEveryRun）
            p += pb_int32(9, t['war_start_value'] if t else 0)  # warStartValue
            prods.append(p)

            c = pb_int64(1, fid * 1000 + cat)                # id
            c += pb_int64(2, fid)                              # factionId
            c += pb_int64(3, _WAR_ID)                          # warId
            c += pb_int64(4, hq)                               # factionHQCommandNodeWarInstanceId
            c += pb_int64(5, cat)                              # armyResourceCategoryId
            c += pb_string(6, prev_ts)                         # previousConsumptionTimeStamp
            c += pb_int32(7, 0)                                # previousConsumptionValue
            win = _queue_window(queue_windows, tmpl, cat)
            c += pb_int32(8, win[0])                           # category queue head
            c += pb_int32(9, win[1])                           # category queue tail
            cons.append(c)
    return prods, cons


def _msg_faction_queue_rows(queue_windows):
    """KVS 'FactionResourceQueue' 行（每阵营 1 行，§3.12 schema）：
    f1 id / f2 factionId / f3 warId / f4 supplyQueueHead(I32) /
    f5 supplyQueueTail(I32)。窗口语义：head=MIN(queue_number)、
    tail=MAX+1、空队 (0,0)。queue_windows 取各阵营的 overall 窗口。"""
    rows = []
    for tmpl, fid in FACTION_ROW_IDS.items():
        head, tail = _queue_window(queue_windows, tmpl)
        r = pb_int64(1, fid)                               # id（=factionId）
        r += pb_int64(2, fid)                              # factionId
        r += pb_int64(3, _WAR_ID)                          # warId
        r += pb_int32(4, head)                             # supplyQueueHead
        r += pb_int32(5, tail)                             # supplyQueueTail
        rows.append(r)
    return rows
