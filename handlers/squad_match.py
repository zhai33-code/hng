# -*- coding: utf-8 -*-
"""SquadRequest（战斗服 squad 定义查询）处理：从 game_server.handle_request
拆出（纯移动，08-27/08-28 修复重灾区，独立成文件便于回归）。

专用服（hng.exe /D）在玩家加入后请求 squad 信息：
  "Request squad nCmdId: X missionSessionId: Y, squadId: Z"
  "ResponseSquadCallback SquadId: 0 team: T squadTemplateId: ST playerSlots: N supportSlots: M Name: Nm"
若返回 HandlerErrorResponse（空 squad）→：
  #error3 Invalid factionTemplateId 0 for faction 0
  #error3 Invalid squad team received

SquadResponse proto:
  f1 = squadId (int64, runtime id)
  f2 = ActionGameSquadDefinition
      f1 = missionSquadId (=team 号, runtime)
      f2 = squadTemplateId (WarDatabase/SquadTemplate 行 id)
      f3 = factionTemplateId (1=US 2=GE 3=SU)
      f4 = name (string)
      f5 = ActionGameSquadSlotDefinition playerSlots (repeated)
      f6 = ActionGameSquadSlotDefinition supportSlots (repeated)
      f8 = ownerCharacterId
      f9 = squadleaderCharacterId
"""

import mm_state
from protocol import (
    build_response,
    pb_int32,
    pb_int64,
    pb_message,
    pb_parse_loose,
    pb_string,
    )
from state import log
from wardb import SQUAD_TEMPLATE_SLOTS


async def handle_squad_request(sess, writer, cmd_id, req_type, proto):
    try:
        req_f = pb_parse_loose(proto)
    except Exception:
        req_f = {}
    mission_session_id_req = req_f.get(1, 0)
    requested_squad_id = req_f.get(2, 0)

    # ★ factionTemplateId(f3) 会被 ZServer 直接当 runtime team 号使用(逆向 AddTeam
    #   gamecontroller.cpp 证实)：必须是 BattlefieldFactions 里的 factionId。
    #   玩家 faction 取本次进局会话（BattleFieldSettings f18 已含 1/2/3 全部行，
    #   任意阵营都合法）；无会话回退 SU(3)。
    #   （此前 factions 只发 1/2 两行 → "Adding Team: 3" 越界 → player.dll+0x4AD46D
    #    对象池校验 int3 崩溃，WER 5 次同 offset 0x80000003）
    # ★ f8/f9 owner/squadleader 角色必须用库内 char_id：f12 角色列表已换成
    #   账号库角色（小 id），回放大 id(6360...) 不在列表里 → 客户端按角色查
    #   装备落空 → 部署界面空 + 部署崩溃。
    #   会话匹配：优先按请求 f1 missionSessionId 精确命中（可能是 pms_id 或
    #   battle_session_id——ZServer 从 PlayerSettings f25 / PMS 取值回传）。
    #   ★ 2026-08-27 两人局修复：ZServer 实际回传的却是它自己按局生成的
    #   id（730433917*10^6+mission，兼作 stats session id），两人局必然
    #   命不中任何玩家会话 → 原来回退"最新会话"= 后入队玩家 → 先入队
    #   玩家小队拿到后者的 faction → 战斗服两个小队全进同一 team →
    #   "#error3 squad setup invalid"、部署界面起不来。故再加一层按
    #   请求 f2 squadId（=PlayerSettings f16=成局登记 squad_id）反查；
    #   faction 最终以 _MM_MISSION_SQUADS 登记表为准（见下方 sq_info）。
    mm_sess = None
    if mission_session_id_req:
        for _s in mm_state.PMS_TO_SESSION.values():
            if mission_session_id_req in (_s.get('pms_id'),
                                          _s.get('battle_session_id')):
                mm_sess = _s
                break
    if mm_sess is None and requested_squad_id:
        for _s in mm_state.PMS_TO_SESSION.values():
            if int(requested_squad_id) == int(_s.get('squad_id', 0) or 0):
                mm_sess = _s
                break
    if mm_sess is None:
        # ★ 兜底圈定本局：f1（ZServer 自产 id）/f2（stale f16，如 rejoin
        #   换队后 ZServer 缓存旧值）都未命中时，全局"最新会话"在多局
        #   并行时会拿到别的局的玩家 → mission_id 错 → 登记表查不到 →
        #   单槽默认 + 错误 faction（历史小队阵营错误同款路径）。
        #   请求来自战斗服自己的连接，用握手记录的 combat_mission 圈定。
        cm = int(getattr(sess, 'combat_mission', 0) or 0)
        for _s in mm_state.PMS_TO_SESSION.values():
            if cm and int(_s.get('mission_id', 0) or 0) != cm:
                continue
            mm_sess = _s          # dict 保序 → 本局最新进局会话
        if mm_sess is None and not cm:
            for _s in mm_state.PMS_TO_SESSION.values():
                mm_sess = _s      # 无本局信息（理论不可达）→ 全局最新
    if mm_sess is None:
        log('      [WARN] SquadRequest: no PMS session, char ids zeroed')
    sess_char_id = int(mm_sess['char_id']) if mm_sess else 0
    sess_faction = int(mm_sess.get('faction', 3) or 3) if mm_sess else 3
    team_runtime_faction_id = sess_faction   # ∈ BattleFieldSettings f18 factionId
    squad_template_id = 1                # WarDatabase/SquadTemplate 第一行 id=1
    faction_template_id = sess_faction    # 1=US 2=GE 3=SU（与 f18 行对齐）
    # ★ f1 squadId 必须命中 ZServer squad 树里 PlayerSettings f16 注册的键：
    #   优先回显请求的 squadId（即客户端持有的 f16 值），无则用会话动态
    #   squad_id（与 PlayerSettings f16 同源生成），最后现场发号。
    squad_runtime_id = int(requested_squad_id) if requested_squad_id else (
        int(mm_sess.get('squad_id', 0) or 0) if mm_sess else 0) or \
        mm_state._next_match_id(mm_state._MM_SQUAD_ID)

    # ★ 局内小队定义：成局时 matchmaking 按分组登记（真实小队用大厅
    #   传入的队名/队长；单人匹配自动凑队，rank 最高任队长）。
    #   队内全体成员共享 squad_id（PlayerSettings f16 同源），每人占一个
    #   playerSlot（index 与各自 PlayerSettings f17 squadSlotIndex 对齐）。
    #   会话不在登记表（理论不可达，如重启残留）→ 回退单人单槽定义。
    mission_id = int(mm_sess.get('mission_id', 0) or 0) if mm_sess else 0
    sq_info = mm_state._MM_MISSION_SQUADS.get(
        mission_id, {}).get(squad_runtime_id) if mission_id else None
    if sq_info is not None:
        members = sq_info.get('members') or []
        squad_name = str(sq_info.get('name') or 'PlayerSquad')
        squad_leader_char_id = int(sq_info.get('leader_char_id', 0) or 0) \
            or (int(members[0]['char_id']) if members else sess_char_id)
        owner_char_id = squad_leader_char_id
        # ★ f2 squadTemplateId 取成局登记的兵种小队模板（回退 1）
        squad_template_id = int(sq_info.get('template', 1) or 1)
        # ★ faction 以成局登记表为准（matchmaking 按 entry.faction 分组
        #   写入）：会话回退值在两人局可能是另一名玩家的 faction。
        if int(sq_info.get('faction', 0) or 0):
            team_runtime_faction_id = int(sq_info['faction'])
            faction_template_id = int(sq_info['faction'])
        # ★ f5 playerSlots：发小队模板全量槽（SquadPlayerSlotTemplate
        #   表行：f1 slotTemplateId、f2 index=slotNumber-1）。绝不能按
        #   当前成员数发槽——战斗服对同一 squadId 只在首个成员加入时
        #   拉一次本定义并缓存（PhaseUpdatePlayerInit 反编译证实），
        #   late join 补进小队时分到的 slot 不在首次定义里 →
        #   "#error3 specified squad character slot not found" →
        #   客户端小队名空白/无军衔、无法部署（2026-09-02）。
        slot_rows = list(SQUAD_TEMPLATE_SLOTS.get(squad_template_id)
                         or [(1, 0)])
        # 成员数超模板槽（理论不可达，如超编 DB 小队）→ 沿用最后一行
        # 槽模板补槽，保证每个成员 slot 都能命中
        if len(members) > len(slot_rows):
            last_tid = slot_rows[-1][0]
            slot_rows += [(last_tid, i) for i in
                          range(len(slot_rows), len(members))]
        slots = b''.join(
            pb_message(5, pb_int64(1, int(tid)) + pb_int32(2, int(idx)))
            for tid, idx in slot_rows)
        slot_count = len(slot_rows)
    else:
        squad_name = 'PlayerSquad'
        owner_char_id = sess_char_id
        squad_leader_char_id = sess_char_id
        slots = pb_message(5, pb_int64(1, 1) + pb_int32(2, 0))
        slot_count = 1
    squad_def = b''
    squad_def += pb_int64(1, team_runtime_faction_id)  # f1 missionSquadId (=team)
    squad_def += pb_int64(2, squad_template_id)        # f2 squadTemplateId
    squad_def += pb_int64(3, faction_template_id)      # f3 factionTemplateId (非零!)
    squad_def += pb_string(4, squad_name)              # f4 name（大厅传入/队长名）
    squad_def += slots                                  # f5 playerSlots ×N
    # f6 supportSlots: 空 repeated
    # f7 resources: 空 repeated
    squad_def += pb_int64(8, owner_char_id)            # f8 ownerCharacterId
    squad_def += pb_int64(9, squad_leader_char_id)     # f9 squadleaderCharacterId

    resp_proto = b''
    resp_proto += pb_int64(1, squad_runtime_id)        # f1 squadId
    resp_proto += pb_message(2, squad_def)              # f2 squadDefinition

    resp = build_response(cmd_id, 'SquadResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'GAME S->C cmd={cmd_id:<4} SquadResponse '
        f'(pms_req={mission_session_id_req}, squadId_req={requested_squad_id}, '
        f'team/factionId={team_runtime_faction_id}, '
        f'squadTemplate={squad_template_id}, factionTemplate={faction_template_id}, '
        f'name={squad_name!r}, leader_char={squad_leader_char_id}, '
        f'playerSlots={slot_count}, {len(resp_proto)}B)')
    return True
