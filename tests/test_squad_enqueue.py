# -*- coding: utf-8 -*-
"""_enqueue_squad 整队入池回归测试（2026-09-09 组队卡匹配事故）。

背景：matchmaking.py 引用 character_resource_category（QueueEntry.char_cat）
却未从 wardb 导入 → _enqueue_squad 第一个成员在 1011 行推完 PMM 行后、
1021 行构建 QueueEntry 时抛 NameError → 异常上抛 dispatch：
  * 队长收到 PMM 行（UI 显示匹配中）
  * 后续队员的 PMM 行、POOL.enqueue、set_squad_flag、成局撮合全部未执行
    → 队员不锁定匹配状态、整队永不成局（卡匹配阶段）
现有测试全部 mock 掉 _enqueue_squad 或走空成员 no-op 路径，未覆盖函数体，
故该 NameError 漏网。本测试【直接执行真实函数体】（不 mock 本体）：

  1. 两在线成员整队入池不抛异常（NameError 回归）
  2/3. 队长/队员各自收到 PlayerMatchMaking KVCS 行（队员锁定匹配状态）
  4. 双方 _MM_QUEUES 排队上下文建立（mission_id=0、squad_db_id 正确）
  5. 双方入池且 group_key 相同（整队原子）、char_cat 按模板正确解析
     （template 1=步兵→1、4=伞兵→4，即触发事故的那次调用）
  6. DB 状态推进：成员 status=3 ×2 + set_squad_flag(squad, True)
  7. 整队 SquadSession 行重推（push_squad_rows_to_all_members）
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import matchmaking
import mm_pool
import war_campaign
import handlers.squad as squad_mod
from state import FLASH_SESSIONS
import state
from mm_state import _MM_QUEUES, _MATCH_PMM_IDS, POOL

RESULTS = []

SQUAD_ID = 86001
LEAD_PID, MATE_PID = 86002, 86003
LEAD_CHAR, MATE_CHAR = 86004, 86005       # template 1=步兵 / 4=伞兵


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


class FakeFlash:
    def __init__(self):
        self.writer = FakeWriter()
        self.playerview_cmd = 55


class FakeStore:
    def __init__(self, war_record=None):
        self.member_updates = []   # (member_id, status)
        self.flag_updates = []     # (squad_id, flag)
        self.war_record = war_record

    async def update_squad_member(self, mid, status=None, **kw):
        self.member_updates.append((int(mid), status))

    async def set_squad_flag(self, sid, flag):
        self.flag_updates.append((int(sid), bool(flag)))

    async def list_vehicles(self, pid, char_id):
        return []

    async def get_mission_record(self, mid):
        return self.war_record


def main():
    lead, mate = FakeFlash(), FakeFlash()
    squad = {'id': SQUAD_ID, 'name': 'TestSquad', 'faction': 1}
    members = [
        {'id': 86011, 'member_id': 86011, 'player_id': LEAD_PID,
         'gamertag': 'SquadLead', 'char_name': 'LeadChar', 'combat_xp': 0,
         'faction': 1, 'character_id': LEAD_CHAR, 'template': 1, 'status': 2},
        {'id': 86012, 'member_id': 86012, 'player_id': MATE_PID,
         'gamertag': 'SquadMate', 'char_name': 'MateChar', 'combat_xp': 0,
         'faction': 1, 'character_id': MATE_CHAR, 'template': 4, 'status': 2},
    ]
    store = FakeStore()

    async def fake_underdog(s):
        return None

    async def fake_try_form_all():
        return None

    def fake_arm_ai_fill(pid, gamemode_id):
        return None

    pushed_squads = []

    async def fake_push_rows(sid):
        pushed_squads.append(int(sid))

    olds = (state.STORE,
            war_campaign.current_underdog_mults,
            matchmaking._try_form_all,
            matchmaking._arm_ai_fill,
            squad_mod.push_squad_rows_to_all_members)
    for pid in (LEAD_PID, MATE_PID):
        _MM_QUEUES.pop(pid, None)
        _MATCH_PMM_IDS.pop(pid, None)
        POOL.remove(pid)
    FLASH_SESSIONS['SquadLead'] = lead
    FLASH_SESSIONS['SquadMate'] = mate
    state.STORE = store
    war_campaign.current_underdog_mults = fake_underdog
    matchmaking._try_form_all = fake_try_form_all
    matchmaking._arm_ai_fill = fake_arm_ai_fill
    squad_mod.push_squad_rows_to_all_members = fake_push_rows

    err = None
    try:
        try:
            asyncio.run(matchmaking._enqueue_squad(
                squad, members, 6, (), 0))
        except Exception as e:
            err = e

        check('1. 整队入池不抛异常（NameError 回归：修复前队长 PMM 行后即崩）',
              err is None, f'err={type(err).__name__}: {err}' if err else '')

        if err is None:
            check('2. 队长收到 PlayerMatchMaking 行',
                  b'PlayerMatchMaking' in lead.writer.data)
            check('3. 队员收到 PlayerMatchMaking 行（客户端锁定匹配状态）',
                  b'PlayerMatchMaking' in mate.writer.data)

            q_lead = _MM_QUEUES.get(LEAD_PID) or {}
            q_mate = _MM_QUEUES.get(MATE_PID) or {}
            check('4. 双方排队上下文建立（mission_id=0、squad_db_id 正确）',
                  q_lead.get('mission_id') == 0 and q_mate.get('mission_id') == 0
                  and (q_lead.get('ctx') or {}).get('squad_db_id') == SQUAD_ID
                  and (q_mate.get('ctx') or {}).get('squad_db_id') == SQUAD_ID)

            e_lead, e_mate = POOL.get(LEAD_PID), POOL.get(MATE_PID)
            check('5. 双方入池整队原子（group_key 同队）+ char_cat 正确解析',
                  e_lead is not None and e_mate is not None
                  and e_lead.group_key == e_mate.group_key == SQUAD_ID
                  and e_lead.char_cat == 1 and e_mate.char_cat == 4,
                  f'lead.cat={getattr(e_lead, "char_cat", None)} '
                  f'mate.cat={getattr(e_mate, "char_cat", None)}')

            check('6. DB 状态推进（成员 status=3 ×2 + set_squad_flag True）',
                  sorted(store.member_updates) == [(86011, 3), (86012, 3)]
                  and store.flag_updates == [(SQUAD_ID, True)],
                  f'members={store.member_updates} flag={store.flag_updates}')

            check('7. 整队 SquadSession 行重推（push_squad_rows_to_all_members）',
                  pushed_squads == [SQUAD_ID])
    finally:
        state.STORE = olds[0]
        war_campaign.current_underdog_mults = olds[1]
        matchmaking._try_form_all = olds[2]
        matchmaking._arm_ai_fill = olds[3]
        squad_mod.push_squad_rows_to_all_members = olds[4]
        FLASH_SESSIONS.pop('SquadLead', None)
        FLASH_SESSIONS.pop('SquadMate', None)
        for pid in (LEAD_PID, MATE_PID):
            _MM_QUEUES.pop(pid, None)
            _MATCH_PMM_IDS.pop(pid, None)
            POOL.remove(pid)

    failed = [l for l, ok in RESULTS if not ok]
    print(f'\nSQUAD_ENQUEUE: '
          f'{"ALL PASS" if not failed else f"{len(failed)} FAILED: {failed}"} '
          f'({len(RESULTS) - len(failed)}/{len(RESULTS)} passed)')
    if failed:
        return 1

    # ---- 小队排 war 会战路由（2026-09-14：排队成员进会战详情名单）----
    # 指定战局为 war record（battle_id>0, state=0）→ 整队改排 gamemode 10
    # war 桶（battle_id 分桶）：此前 entries 无 battle_id 落普通撮合
    # （war 桶永不撮合）且详情名单看不到排队成员。
    n0 = len(RESULTS)
    lead2, mate2, su = FakeFlash(), FakeFlash(), FakeFlash()
    members2 = [
        {'id': 86021, 'player_id': LEAD_PID, 'gamertag': 'SquadLead',
         'char_name': 'LeadChar', 'combat_xp': 0, 'faction': 1,
         'character_id': LEAD_CHAR, 'template': 1, 'status': 2},
        {'id': 86022, 'player_id': MATE_PID, 'gamertag': 'SquadMate',
         'char_name': 'MateChar', 'combat_xp': 0, 'faction': 1,
         'character_id': MATE_CHAR, 'template': 4, 'status': 2},
    ]
    wrec = {'id': 4242, 'state': 0, 'battle_id': 77,
            'attacker_faction': 1, 'defender_faction': 2}
    store2 = FakeStore(war_record=wrec)

    # 场景 A：整队阵营在攻守双方 → war 桶
    # （重挂 mock：第一块的 finally 已还原；否则真实 _try_form_all 会把
    #   刚入池的条目直接撮合成局抽走，单测断言不到排队态）
    olds2 = (state.STORE,
             war_campaign.current_underdog_mults,
             matchmaking._try_form_all,
             matchmaking._arm_ai_fill,
             squad_mod.push_squad_rows_to_all_members)
    war_campaign.current_underdog_mults = fake_underdog
    matchmaking._try_form_all = fake_try_form_all
    matchmaking._arm_ai_fill = fake_arm_ai_fill
    squad_mod.push_squad_rows_to_all_members = fake_push_rows
    FLASH_SESSIONS['SquadLead'] = lead2
    FLASH_SESSIONS['SquadMate'] = mate2
    state.STORE = store2
    try:
        asyncio.run(matchmaking._enqueue_squad(
            squad, members2, 10, (), 4242))
        e_lead, e_mate = POOL.get(LEAD_PID), POOL.get(MATE_PID)
        check('W1. 整队入 war 桶（gamemode=10 + battle_id 分桶）',
              e_lead is not None and e_mate is not None
              and e_lead.gamemode_id == 10 and e_lead.battle_id == 77
              and e_mate.battle_id == 77,
              f'lead={e_lead and (e_lead.gamemode_id, e_lead.battle_id)} '
              f'mate={e_mate and (e_mate.gamemode_id, e_mate.battle_id)}')
        q_lead = _MM_QUEUES.get(LEAD_PID) or {}
        check('W2. 排队上下文 gamemode=10（取消/计数路径一致）',
              q_lead.get('gamemode') == 10,
              f'gamemode={q_lead.get("gamemode")}')
        # 详情名单口径（mm_handler._war_queue_players 消费端）
        import mm_handler
        players = mm_handler._war_queue_players(77)
        tags = {p['gamertag']: p['faction'] for p in players}
        check('W3. 排队成员进会战详情名单（war_bucket 圈定）',
              tags == {'SquadLead': 1, 'SquadMate': 1}, f'tags={tags}')
        # 场景 B：第三方阵营（SU=3）不在攻守双方 → 整队不入池
        # （先清 A 场景残留，断言 gate 拒绝后确实没有新入池）
        POOL.remove(MATE_PID)
        _MM_QUEUES.pop(MATE_PID, None)
        members3 = [
            {'id': 86031, 'player_id': MATE_PID, 'gamertag': 'SquadMate',
             'char_name': 'MateChar', 'combat_xp': 0, 'faction': 3,
             'character_id': MATE_CHAR, 'template': 4, 'status': 2},
        ]
        FLASH_SESSIONS['SquadMate'] = su
        asyncio.run(matchmaking._enqueue_squad(
            squad, members3, 10, (), 4242))
        check('W4. 阵营不在攻守双方 → 不入任何池',
              POOL.get(MATE_PID) is None and POOL.get(LEAD_PID) is not None,
              f'mate={POOL.get(MATE_PID)}')
    finally:
        state.STORE = olds2[0]
        war_campaign.current_underdog_mults = olds2[1]
        matchmaking._try_form_all = olds2[2]
        matchmaking._arm_ai_fill = olds2[3]
        squad_mod.push_squad_rows_to_all_members = olds2[4]
        FLASH_SESSIONS.pop('SquadLead', None)
        FLASH_SESSIONS.pop('SquadMate', None)
        for pid in (LEAD_PID, MATE_PID):
            _MM_QUEUES.pop(pid, None)
            _MATCH_PMM_IDS.pop(pid, None)
            POOL.remove(pid)

    failed = [l for l, ok in RESULTS[n0:] if not ok]
    print(f'SQUAD_WAR_ROUTE: '
          f'{"ALL PASS" if not failed else f"{len(failed)} FAILED: {failed}"} '
          f'({len(RESULTS) - n0} checks)')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
