# -*- coding: utf-8 -*-
"""小队/军团域（squad）：小队创建/邀请/状态 + 军团（Company）管理。

消息 schema（decompiled/proto-files/HnG_States）：
- CreateSquadRequest {characterId=1} → CreateSquadResponse {response=1}
- ModifySquadRequest {squadSessionId=1, removeSquadSupportSlot=2,
  addSquadSupportSlotTemplateId=3} → ModifySquadResponse {response=1}
- RemoveSquadRequest {dummy=1} → RemoveSquadResponse {response=1}
- RemoveSquadMemberRequest {squadMemberSessionId=1}
  → RemoveSquadMemberResponse {response=1}
- SquadInvitePlayerRequest {ownerCharacterId=1, friendPlayerId=2,
  matchMakeWarMissionFlag=4, matchMakeStagedMissionPresetList=5,
  matchMakeSquadVehicleFilterArmyResourceTemplateIds=6}
  → SquadInvitePlayerResponse {result=1}
- SquadRespondToInviteRequest {squadMemberSessionId=1, hasAccepted=2,
  characterId=3} → SquadRespondToInviteResponse {result=1}
- SetSquadDetailsRequest {characterId=1, squadName=2, pricingId=3,
  discountId=4, saveSessionState=5, paymentCurrency=6}
  → SetSquadDetailsResponse {response=1}
- SetSquadMatchMakingFlagRequest {squadSessionId=1,
  matchMakeWarMissionFlag=3, matchMakeStagedMissionPresetList=4,
  matchMakeSquadVehicleFilterArmyResourceTemplateIds=5}
  → SetSquadMatchMakingFlagResponse {response=1}
- SetSquadMemberStatusRequest {squadSlotId=1, memberCharacterId=2,
  squadMemberSessionStatus=3} → SetSquadMemberStatusResponse {response=1}
- UpdateSquadResourceRequest {squadSessionId=1,
  resourcePurchaseList=2 repeated SquadResourcePurchaseSet{
  armyResourceCategoryId=1, resourcePricingId=2}, paymentCurrency=3}
  → UpdateSquadResourceResponse {response=1}
- CreateCompanyRequest {nameLong=1, nameShort=2, description=3}
  → CreateCompanyResponse {result=1}
- InviteToCompanyRequest {playerToInvite=1} → InviteToCompanyResponse {result=1}
- KickFromCompanyRequest {playerToKick=1} → KickFromCompanyResponse {result=1}
- LeaveCompanyRequest {} → LeaveCompanyResponse {result=1}
- RespondToCompanyInvitationRequest {invitationId=1, accept=2}
  → RespondToCompanyInvitationResponse {result=1}

客户端 UI 流程（swf-ui-logic squad 域）：
- SquadModel 监听 KVCS 表 SquadSession / SquadMemberSession（xmlTag 首字母大写）
  的 changeSignal —— 小队 UI 的全部数据源，创建/变更后必须推送。
- SquadService.clearSlot → RemoveSquadMemberRequest（squadMemberSessionId）；
  非队长离队也走该请求（带手动解散标记）。
- SquadService.deactivate：队长发 RemoveSquadRequest，成员发
  RemoveSquadMemberRequest。
- setSquadMemberStatus(squadSlotId, memberCharacterId, status)：
  squadSlotId = SquadMemberSession.id（成员行 id）；
  memberCharacterId 非 0 = 换角色（SquadChangeCharacterMediator），
  0 = 仅改状态（SetSquadPlayerStateCommand：InLobby/Ready）。
- 所有响应非 ok 会 sendFeedbackMessage 弹错误提示，ok=1 即静默通过。
- SquadMemberSessionStatus 枚举：InvitationPending=1 InLobby=2 Ready=3
  InCombat=4（squad_members.status 同值）。

ResponseCodes（squad/company 域）：
- player_already_in_squad=157 squad_slot_not_found=158 player_not_owner=159
- squad_not_found=161 squad_size_too_small=162 squad_slot_in_use=163
- squad_not_owned_by_player=164 player_not_found=64
- company_insufficientPrivileges=170 company_memberNotFound=171
- company_playerNotFound=172 company_cannotKickSelf=173
- company_playerAlreadyInCompany=174 company_playerHasInvitationPending=175
- company_companyIsFull=176 company_companyNameAlreadyExists=177
- company_companyLongNameTooLong=178 company_companyLongNameTooShort=179
- company_companyLongNameInvalid=180 company_companyShortNameTooLong=181
- company_companyShortNameTooShort=182 company_companyShortNameInvalid=183
- company_companyDescriptionTooLong=184 company_invitationNotFound=185
- company_companyNoLongerExists=186
"""

import struct

from protocol import (
    build_response,
    pb_bool,
    pb_int64,
    pb_int32,
    pb_parse,
    pb_parse_multi,
    pb_string,
    )

from state import FLASH_SESSIONS, PLAYER_SELECTED_CHAR, STORE, log

from messages import _push_kvcs
from matchmaking import _SOLO_SQUADS, _push_kvcs_delete
from wardb import _rank_from_combat_xp, squad_template_for_character

RC_OK = 1
RC_PLAYER_NOT_FOUND = 64
RC_CHARACTER_NOT_OWNED = 17
RC_PLAYER_ALREADY_IN_SQUAD = 157
RC_SQUAD_SLOT_NOT_FOUND = 158
RC_SQUAD_NOT_FOUND = 161
RC_SQUAD_NOT_OWNED = 164

# 官方小队人数上限（客户端 SquadModel.MAX_CHARACTERS_PER_SQUAD=4 硬编码，
# 反编译 swf-frontend/scripts/.../squad/model/SquadModel.as:108）。
# 客户端 InviteFriendToContextCommand 已用 squadModel.isFull 本地拦截
# "unable_to_invite_squad_full"，服务端此上限为兜底（回 158 slot not found）。
MAX_SQUAD_MEMBERS = 4

RC_CO_PRIVILEGES = 170
RC_CO_MEMBER_NOT_FOUND = 171
RC_CO_PLAYER_NOT_FOUND = 172
RC_CO_CANNOT_KICK_SELF = 173
RC_CO_ALREADY_IN_COMPANY = 174
RC_CO_INVITE_PENDING = 175
RC_CO_NAME_EXISTS = 177
RC_CO_LONG_TOO_LONG = 178
RC_CO_LONG_TOO_SHORT = 179
RC_CO_SHORT_TOO_LONG = 181
RC_CO_SHORT_TOO_SHORT = 182
RC_CO_INVITE_NOT_FOUND = 185
RC_CO_NO_LONGER_EXISTS = 186

# 名称长度约束（CreateCompanyRequest 校验，与官服一致的宽松阈值）
_CO_LONG_MIN, _CO_LONG_MAX = 3, 40
_CO_SHORT_MIN, _CO_SHORT_MAX = 2, 6
_CO_DESC_MAX = 200

ST_INVITE_PENDING = 1
ST_IN_LOBBY = 2
ST_READY = 3
ST_IN_COMBAT = 4


def _str(v):
    if isinstance(v, bytes):
        return v.decode('utf-8', 'replace')
    return str(v)


# ------------------------------------------------------------- KVCS 推送

def _msg_squad_session(squad, members, mission_id=0, entered=0,
                       viewer_faction=None):
    """SquadSession proto（22 字段，f11 缺失；repeated 空省略）。

    viewer_faction：客户端把 SquadSession.f4 当作"本队阵营"渲染——
    MatchmakingView 阵营图标、SquadPlayerSlotItemRenderer 段位徽章、
    默认迷彩、StatusMediator 进战确认框全部绑 squadModel.
    factionTemplateId（=SquadSession.f4，官方语义=全队同阵营）。私服
    允许混阵营小队 → 按接收者自己的角色阵营逐连接下发 f4（每个客户端
    只收到自己连接的 KVCS 流，行 id 相同、字段值可按观察者区分），
    否则全队显示建队者阵营（德军玩家也显示苏军）。"""
    leader = next((m for m in members
                   if m['character_id'] == squad['leader_character_id']),
                  members[0] if members else None)
    m = pb_int64(1, squad['id'])                     # id（=squadSessionId）
    m += pb_int64(2, leader['player_id'] if leader else 0)  # ownerPlayerId
    m += pb_int64(3, leader['template'] if leader else 0)   # characterTemplateId
    m += pb_int64(4, int(viewer_faction) if viewer_faction
                  else squad['faction'])             # factionTemplateId（按观察者）
    m += pb_int64(5, 0)                              # supportSlotTemplateId1
    m += pb_int64(6, 0)                              # supportSlotTemplateId2
    m += pb_int64(7, 0)                              # supportSlotTemplateId3
    m += pb_int32(8, 0)                              # totalCustomizationPoints
    m += pb_int32(9, 0)                              # usedCustomizationPoints
    m += pb_string(10, squad['name'])                # name
    m += pb_bool(12, squad['matchmaking_flag'])      # matchMakeWarMissionFlag
    m += pb_int64(17, int(mission_id or 0))          # missionId
    m += pb_int64(18, squad['id'])                   # squadId
    m += pb_int32(19, int(entered or 0))             # actionGamesEntered
    m += pb_bool(20, False)                          # isLockedForMembers
    m += pb_bool(21, False)                          # isLockedForCustomization
    m += pb_bool(22, False)                          # forceReady
    return m


async def _member_appearance(member):
    """成员角色装备的迷彩/头盔模板 id（SquadMemberSession f8/f19）。

    小队头像按这两个模板渲染士兵外观；0 = 未装备 → 默认军服。"""
    try:
        return await STORE.get_character_appearance(
            int(member['player_id']), int(member['character_id']))
    except Exception as e:
        log(f'      [DB] member appearance lookup failed: {e}')
        return (0, 0)


def _msg_squad_member_session(member, squad_id, uvt=0, upt=0):
    """SquadMemberSession proto（20 字段，repeated 空省略）。

    uvt/upt：角色装备的迷彩/头盔模板 id（_member_appearance 解析）。"""
    m = pb_int64(1, member['id'])                    # id（成员行 id）
    m += pb_int64(2, squad_id)                       # squadSessionId
    m += pb_int64(3, 0)                              # squadPlayerSlotTemplateId
    m += pb_int64(4, member['player_id'])            # playerId
    m += pb_string(5, member['gamertag'])            # gamerTag
    m += pb_int64(6, member['faction'])              # playerFactionTemplateId
    m += pb_int64(7, member['character_id'])         # characterId
    m += pb_int64(8, uvt)                            # uniformVariantTemplateId
    m += pb_int64(9, 0)                              # weaponVariantTemplateId
    m += pb_int32(10, _rank_from_combat_xp(member['combat_xp']))  # characterRank
    m += pb_int64(11, 0)                             # shopMembershipItemId
    m += pb_int32(12, member['status'])              # squadMemberSessionStatus
    m += pb_string(14, member['char_name'])          # characterName
    m += pb_int64(15, 0)                             # playerSessionId
    m += pb_int32(16, 0)                             # playerTier
    m += pb_int32(17, 0)                             # squadCustomizationPoints
    m += pb_bool(18, member['status'] == ST_INVITE_PENDING)  # wasInvited
    m += pb_int64(19, upt)                           # uniformPartTemplateId
    m += pb_int64(20, member['faction'])             # characterFactionTemplateId
    return m


async def push_squad_rows_to_writer(writer, view_cmd, squad_id, mission_id=0,
                                     viewer_pid=None, viewer_name=None):
    """整队 KVCS 行推给指定连接（底层；状态读 DB 实时值）。

    matchmaking 进战局序列复用：真实小队成员各自的连接收到同一份
    SquadSession(f17=missionId) + 全体 SquadMemberSession。
    viewer_pid/viewer_name：接收者身份 → SquadSession.f4 按其角色阵营
    下发（混阵营小队显示，见 _msg_squad_session）；都不传则用小队默认。"""
    if STORE is None or view_cmd is None or not squad_id:
        return
    try:
        squad = await STORE.get_squad(squad_id)
        if squad is None:
            return
        members = await STORE.list_squad_members(squad_id)
    except Exception as e:
        log(f'      [DB] push_squad_rows_to_writer failed: {e}')
        return
    viewer_faction = None
    if viewer_pid is not None or viewer_name:
        for m in members:
            if (viewer_pid is not None
                    and int(m['player_id']) == int(viewer_pid)) \
                    or (viewer_name
                        and (m['gamertag'] or '') == viewer_name):
                viewer_faction = int(m['faction'])
                break
    await _push_kvcs(writer, view_cmd, b'SquadSession',
                     _msg_squad_session(squad, members, mission_id,
                                        viewer_faction=viewer_faction))
    for mem in members:
        uvt, upt = await _member_appearance(mem)
        await _push_kvcs(writer, view_cmd, b'SquadMemberSession',
                         _msg_squad_member_session(mem, squad_id, uvt, upt))
    log(f'S->C cmd={view_cmd:<4} KeyValueChangeSet '
        f'(SquadSession id={squad_id} + {len(members)} members, '
        f'mission={mission_id or 0})')


async def push_squad_rows(sess, writer, squad_id, mission_id=0):
    """推送小队全部 KVCS 行（SquadSession + 全体 SquadMemberSession）。"""
    if sess.playerview_cmd is None:
        return
    await push_squad_rows_to_writer(writer, sess.playerview_cmd, squad_id,
                                    mission_id,
                                    viewer_pid=getattr(sess, 'player_id',
                                                        None),
                                    viewer_name=getattr(sess, 'username',
                                                         None))


async def push_squad_member_row(sess, writer, member_row_id):
    """推送单个成员行（按成员行 id）。"""
    if STORE is None or sess.playerview_cmd is None:
        return
    try:
        mem_row = await STORE.get_squad_member(member_row_id)
        if mem_row is None:
            return
        squad = await STORE.get_squad(mem_row['squad_id'])
        members = await STORE.list_squad_members(mem_row['squad_id'])
    except Exception as e:
        log(f'      [DB] push_squad_member_row failed: {e}')
        return
    mem = next((x for x in members if x['id'] == member_row_id), None)
    if mem is None:
        return
    uvt, upt = await _member_appearance(mem)
    await _push_kvcs(writer, sess.playerview_cmd, b'SquadMemberSession',
                     _msg_squad_member_session(mem, mem_row['squad_id'],
                                               uvt, upt))
    log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet '
        f'(SquadMemberSession id={member_row_id})')


async def delete_squad_member_row(sess, writer, member_row_id):
    """KVCS 删除成员行。"""
    if sess.playerview_cmd is None:
        return
    await _push_kvcs_delete(writer, sess.playerview_cmd, b'SquadMemberSession',
                            member_row_id)
    log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet '
        f'(SquadMemberSession delete id={member_row_id})')


def _online_member_sessions(members):
    """成员列表 → 在线 Flash 会话列表（去重；仅含已订阅 playerview 的连接）。"""
    seen, out = set(), []
    for m in members:
        s = FLASH_SESSIONS.get(m['gamertag'] or '')
        if s is None or s.playerview_cmd is None:
            continue
        key = id(s)
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


async def push_squad_rows_to_all_members(squad_id, mission_id=0):
    """向全体在线成员的 Flash 连接推送整队 KVCS 行（跨会话同步）。

    邀请接受/踢人/改名/排队/进战局等任何整队数据变化后调用，
    否则其他成员要重登才能看到变化。"""
    if STORE is None or not squad_id:
        return
    try:
        squad = await STORE.get_squad(squad_id)
        if squad is None:
            return
        members = await STORE.list_squad_members(squad_id)
    except Exception as e:
        log(f'      [DB] push_squad_rows_to_all_members failed: {e}')
        return
    for s in _online_member_sessions(members):
        try:
            await push_squad_rows(s, s.writer, squad_id, mission_id)
        except Exception as e:
            log(f'      [squad] push to {s.username!r} failed: {e}')


async def push_squad_deletes_to_sessions(sessions, member_ids, squad_id):
    """解散小队：向给定在线会话推 SquadSession + 全体成员行 DELETE。

    sessions 必须在 dissolve_squad 落库前收集（解散后查库为空）。"""
    for s in sessions:
        if s.playerview_cmd is None:
            continue
        try:
            for mid in member_ids:
                await _push_kvcs_delete(s.writer, s.playerview_cmd,
                                        b'SquadMemberSession', mid)
            await _push_kvcs_delete(s.writer, s.playerview_cmd,
                                    b'SquadSession', squad_id)
            log(f'S->C cmd={s.playerview_cmd:<4} KeyValueChangeSet '
                f'(SquadSession delete id={squad_id} + {len(member_ids)} '
                f'members ★cross-session)')
        except Exception as e:
            log(f'      [squad] delete push to {s.username!r} failed: {e}')


async def delete_squad_member_row_everywhere(member_row_id, squad_id):
    """成员行 DELETE 推给该队全体在线成员（踢人/离队/拒绝邀请后）。"""
    if STORE is None or not squad_id:
        return
    try:
        members = await STORE.list_squad_members(squad_id)
    except Exception as e:
        log(f'      [DB] delete_squad_member_row_everywhere failed: {e}')
        return
    for s in _online_member_sessions(members):
        if s.playerview_cmd is None:
            continue
        try:
            await _push_kvcs_delete(s.writer, s.playerview_cmd,
                                    b'SquadMemberSession', member_row_id)
        except Exception as e:
            log(f'      [squad] delete push to {s.username!r} failed: {e}')


# ------------------------------------------------------------- 小队

async def handle_create_squad(sess, writer, cmd_id, req_type, proto):
    """CreateSquadRequest {characterId=1} → CreateSquadResponse{response=1}。

    创建 squads 行 + 队长成员会话（InLobby），推 SquadSession/SquadMemberSession
    KVCS 行（客户端 SquadModel 数据源）。"""
    char_id = 0
    try:
        f = pb_parse(proto)
        char_id = int(f.get(1, 0) or 0)
    except Exception as e:
        log(f'      [squad] CreateSquad parse error: {e}')

    pid = await sess.ensure_player()
    code = RC_OK
    squad_id = 0
    if pid is None or STORE is None or not char_id:
        code = RC_PLAYER_NOT_FOUND
    else:
        try:
            char = await STORE.get_character(int(pid), char_id)
            if char is None:
                code = RC_CHARACTER_NOT_OWNED        # character_not_owned_by_player
            else:
                existing = await STORE.get_squad_for_character(char_id)
                if existing is not None:
                    code = RC_PLAYER_ALREADY_IN_SQUAD
                    squad_id = existing['id']
                else:
                    name = f"Squad of {sess.username or 'Player'}"
                    squad_id = await STORE.create_squad(
                        char_id, char['faction'], name)
                    log(f'      [DB] create_squad id={squad_id} '
                        f'leader_char={char_id} faction={char["faction"]}')
        except Exception as e:
            log(f'      [DB] create_squad failed: {e}')
            code = RC_PLAYER_NOT_FOUND

    if code == RC_OK and squad_id:
        await push_squad_rows(sess, writer, squad_id)

    resp = build_response(cmd_id, 'CreateSquadResponse', pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} CreateSquadResponse '
        f'(char={char_id}, response={code}, squad={squad_id})')


async def handle_modify_squad(sess, writer, cmd_id, req_type, proto):
    """ModifySquadRequest {squadSessionId=1, removeSquadSupportSlot=2,
    addSquadSupportSlotTemplateId=3} → ModifySquadResponse{response=1}。

    支援槽位增删：私服无槽位模板系统，验证小队归属后直接 ok。"""
    squad_session_id = 0
    try:
        f = pb_parse(proto)
        squad_session_id = int(f.get(1, 0) or 0)
    except Exception as e:
        log(f'      [squad] ModifySquad parse error: {e}')

    pid = await sess.ensure_player()
    code = RC_OK
    if pid is not None and STORE is not None and squad_session_id:
        try:
            squad = await STORE.get_squad(squad_session_id)
            if squad is None:
                code = RC_SQUAD_NOT_FOUND
        except Exception as e:
            log(f'      [DB] modify_squad lookup failed: {e}')
            code = RC_SQUAD_NOT_FOUND

    resp = build_response(cmd_id, 'ModifySquadResponse', pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} ModifySquadResponse '
        f'(squad={squad_session_id}, response={code})')


async def handle_remove_squad(sess, writer, cmd_id, req_type, proto):
    """RemoveSquadRequest {dummy=1} → RemoveSquadResponse{response=1}。

    队长解散小队：删 KVCS 行（SquadSession + 全体 SquadMemberSession）。
    非队长调离队接口（RemoveSquadMemberRequest），此处校验 ownership。
    ★ 单人匹配的隐式小队（matchmaking._SOLO_SQUADS，squadId=0）不在 DB：
      SquadService.deactivate 对 isOwnedByPlayer 也发本请求，须先查注册表，
      否则 get_squad_for_player 找不到 → 161 squad_not_found 弹"解散失败"。"""
    pid = await sess.ensure_player()

    # ① 单人隐式小队：KVCS 删成员行 + 小队行，清注册表（下次 join 重建）
    if pid is not None:
        solo = _SOLO_SQUADS.pop(int(pid), None)
        if solo is not None:
            if sess.playerview_cmd is not None:
                await _push_kvcs_delete(writer, sess.playerview_cmd,
                                        b'SquadMemberSession',
                                        solo['member_row'])
                await _push_kvcs_delete(writer, sess.playerview_cmd,
                                        b'SquadSession', solo['squad_row'])
                log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet '
                    f'(SquadSession delete id={solo["squad_row"]} + '
                    f'SquadMemberSession delete id={solo["member_row"]} '
                    f'★solo squad disband)')
            resp = build_response(cmd_id, 'RemoveSquadResponse',
                                  pb_int32(1, RC_OK))
            writer.write(resp)
            await writer.drain()
            log(f'S->C cmd={cmd_id:<4} RemoveSquadResponse '
                f'(solo squad={solo["squad_row"]}, response=1)')
            return

    # ② DB 真小队（CreateSquadRequest 建立）
    code = RC_OK
    member_ids = []
    squad_id = 0
    live_sessions = []
    if pid is None or STORE is None:
        code = RC_PLAYER_NOT_FOUND
    else:
        try:
            squad = await STORE.get_squad_for_player(int(pid))
            if squad is None:
                code = RC_SQUAD_NOT_FOUND
            else:
                squad_id = squad['id']
                members = await STORE.list_squad_members(squad_id)
                member_ids = [m['id'] for m in members]
                # 解散后查库为空 → 先收集在线会话再 dissolve
                live_sessions = _online_member_sessions(members)
                await STORE.dissolve_squad(squad_id)
                log(f'      [DB] dissolve_squad id={squad_id} '
                    f'({len(member_ids)} members)')
        except Exception as e:
            log(f'      [DB] dissolve_squad failed: {e}')
            code = RC_SQUAD_NOT_FOUND

    if code == RC_OK and sess.playerview_cmd is not None:
        # 解散行推给全体在线成员（含解散者本人的连接）
        await push_squad_deletes_to_sessions(live_sessions, member_ids,
                                             squad_id)
        if sess not in live_sessions:
            for mid in member_ids:
                await _push_kvcs_delete(writer, sess.playerview_cmd,
                                        b'SquadMemberSession', mid)
            if squad_id:
                await _push_kvcs_delete(writer, sess.playerview_cmd,
                                        b'SquadSession', squad_id)
        log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet '
            f'(SquadSession delete id={squad_id} + {len(member_ids)} members)')

    resp = build_response(cmd_id, 'RemoveSquadResponse', pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} RemoveSquadResponse '
        f'(squad={squad_id}, response={code})')


async def handle_remove_squad_member(sess, writer, cmd_id, req_type, proto):
    """RemoveSquadMemberRequest {squadMemberSessionId=1}
    → RemoveSquadMemberResponse{response=1}。

    队长踢人 / 成员自行离队（SquadService.clearSlot / deactivate）。"""
    member_row = 0
    try:
        f = pb_parse(proto)
        member_row = int(f.get(1, 0) or 0)
    except Exception as e:
        log(f'      [squad] RemoveSquadMember parse error: {e}')

    pid = await sess.ensure_player()
    code = RC_OK
    removed_squad_id = 0
    if pid is None or STORE is None or not member_row:
        code = RC_SQUAD_SLOT_NOT_FOUND
    else:
        try:
            mem = await STORE.get_squad_member(member_row)
            removed_squad_id = int(mem['squad_id']) if mem else 0
            removed = await STORE.remove_squad_member(member_row)
            if not removed:
                code = RC_SQUAD_SLOT_NOT_FOUND
                removed_squad_id = 0
            else:
                log(f'      [DB] remove_squad_member row={member_row}')
        except Exception as e:
            log(f'      [DB] remove_squad_member failed: {e}')
            code = RC_SQUAD_SLOT_NOT_FOUND

    if code == RC_OK:
        await delete_squad_member_row(sess, writer, member_row)
        # 队长/其他成员实时看到成员移除；被移除者若在线也收到
        await delete_squad_member_row_everywhere(member_row, removed_squad_id)

    resp = build_response(cmd_id, 'RemoveSquadMemberResponse',
                          pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} RemoveSquadMemberResponse '
        f'(member={member_row}, response={code})')


async def _squad_join_spec(squad):
    """既有小队的入队约束 (faction, 兵种模板 id)：取队长成员行；
    小队/成员缺失返回 None。"""
    if squad is None or STORE is None:
        return None
    try:
        members = await STORE.list_squad_members(int(squad['id']))
    except Exception as e:
        log(f'      [DB] squad_join_spec lookup failed: {e}')
        return None
    leader = next((m for m in members
                   if int(m['character_id']) ==
                   int(squad['leader_character_id'])),
                  members[0] if members else None)
    if leader is None:
        return None
    return int(leader['faction']), \
        squad_template_for_character(leader['template'])


async def _resolve_join_char(player_id, faction, squad_tmpl,
                             selected_id=None):
    """官服入队角色解析（A 邀请 B，A=faction+兵种 squad_tmpl）：
    - B 当前选中士兵（selected_id / PLAYER_SELECTED_CHAR / DB 持久化回放）
      同阵营同兵种 → 直接入队（不换角）
    - 否则取 B 该阵营该兵种第一个角色（创建顺序）
    - B 无该阵营该兵种角色 → (None, False) 入队失败
    返回 (char_row or None, direct)。"""
    try:
        chars = await STORE.list_characters(int(player_id))
    except Exception as e:
        log(f'      [DB] list_characters failed: {e}')
        return None, False
    if selected_id is None:
        selected_id = int(PLAYER_SELECTED_CHAR.get(int(player_id), 0) or 0)
        if not selected_id:
            try:
                selected_id = int(
                    await STORE.get_player_selected_char(int(player_id)) or 0)
            except Exception as e:
                log(f'      [DB] get_player_selected_char failed: {e}')
    else:
        selected_id = int(selected_id or 0)

    def _ok(c):
        return (int(c['faction']) == int(faction) and
                squad_template_for_character(c['template']) ==
                int(squad_tmpl))

    for c in chars:                       # 选中角色匹配 → 直接入队
        if int(c['id']) == selected_id and _ok(c):
            return c, True
    for c in chars:                       # 同阵营同兵种第一个
        if _ok(c):
            return c, False
    return None, False


async def _leader_switch_spec(squad, members, slot_id, new_leader_char,
                              status):
    """队长换角色（官服语义：小队类型跟随队长）。

    - squads.leader_character_id / faction 更新为新角色
    - 队长成员行换 character_id
    - 其余成员按新约束（新角色阵营+兵种）重新解析：
      有匹配角色 → 自动换角（update character_id）；
      无匹配 → 请离（删成员行，KVCS delete 推其在线连接）
    最后由调用方 push_squad_rows_to_all_members 广播新约束行。
    返回请离的 member row id 列表。"""
    if STORE is None or squad is None:
        return []
    squad_id = int(squad['id'])
    new_spec = (int(new_leader_char['faction']),
                squad_template_for_character(new_leader_char['template']))
    kicked = []
    try:
        await STORE.update_squad_leader(
            squad_id, int(new_leader_char['id']), new_spec[0])
        await STORE.update_squad_member(
            slot_id, character_id=int(new_leader_char['id']),
            status=int(status) if status else None)
    except Exception as e:
        log(f'      [DB] leader switch failed: {e}')
        return []
    log(f'      [DB] leader switch: squad={squad_id} -> char='
        f'{new_leader_char["id"]} faction={new_spec[0]} '
        f'squad_tmpl={new_spec[1]}')
    for m in members:
        if int(m['id']) == int(slot_id):
            continue                     # 队长行已处理
        try:
            resolved, _d = await _resolve_join_char(
                int(m['player_id']), new_spec[0], new_spec[1],
                selected_id=int(m['character_id']))
        except Exception as e:
            log(f'      [DB] member re-resolve failed: {e}')
            resolved, _d = None, False
        if resolved is not None:
            if int(resolved['id']) != int(m['character_id']):
                try:
                    await STORE.update_squad_member(
                        int(m['id']), character_id=int(resolved['id']))
                    log(f'      [DB] member auto-switch row={m["id"]} '
                        f'char={m["character_id"]} -> {resolved["id"]} '
                        f'(follow leader spec)')
                except Exception as e:
                    log(f'      [DB] member auto-switch failed: {e}')
        else:
            # 无匹配角色 → 请离（删行；KVCS delete 广播在下方）
            try:
                await STORE.remove_squad_member(int(m['id']))
                kicked.append(int(m['id']))
                log(f'      [DB] member kicked row={m["id"]} '
                    f'pid={m["player_id"]} (no char for new spec '
                    f'faction={new_spec[0]} squad_tmpl={new_spec[1]})')
            except Exception as e:
                log(f'      [DB] member kick failed: {e}')
    # 请离者行删除推其本人在线连接（其他成员走整队行广播）
    for row_id in kicked:
        try:
            await delete_squad_member_row_everywhere(row_id, squad_id)
        except Exception as e:
            log(f'      [squad] kick KVCS push failed: {e}')
    return kicked


async def handle_squad_invite_player(sess, writer, cmd_id, req_type, proto):
    """SquadInvitePlayerRequest {ownerCharacterId=1, friendPlayerId=2}
    → SquadInvitePlayerResponse{result=1}。

    官服入队规则解析被邀者角色（同阵营+同兵种，见 _resolve_join_char），
    status=InvitationPending 入小队，推成员行；无匹配角色 → 17 拒绝。"""
    owner_char_id, friend_player_id = 0, 0
    try:
        f = pb_parse(proto)
        owner_char_id = int(f.get(1, 0) or 0)
        friend_player_id = int(f.get(2, 0) or 0)
    except Exception as e:
        log(f'      [squad] SquadInvitePlayer parse error: {e}')

    pid = await sess.ensure_player()
    code = RC_OK
    member_row = 0
    created_squad_id = 0
    squad = None
    if pid is None or STORE is None or not friend_player_id:
        code = RC_PLAYER_NOT_FOUND
    else:
        try:
            squad = await STORE.get_squad_for_player(int(pid))
            # 记录邀请者当前选中士兵（ownerCharacterId 即选中角色，落库）
            if owner_char_id:
                own = await STORE.get_character(int(pid), owner_char_id)
                if own is not None:
                    PLAYER_SELECTED_CHAR[int(pid)] = int(own['id'])
                    try:
                        await STORE.set_player_selected_char(
                            int(pid), int(own['id']))
                    except Exception as e:
                        log(f'      [DB] set_player_selected_char '
                            f'failed: {e}')
            # 被邀者已在别的真小队 → 157 player_already_in_squad
            friend_squad = await STORE.get_squad_for_player(
                int(friend_player_id))
            if friend_squad is not None and \
                    (squad is None or
                     int(friend_squad['id']) != int(squad['id'])):
                code = RC_PLAYER_ALREADY_IN_SQUAD

            # ★ 满员校验（官方 MAX_CHARACTERS_PER_SQUAD=4）：
            #   既有真队已满（pending + 正式成员）→ 158 拒绝
            if code == RC_OK and squad is not None:
                try:
                    members = await STORE.list_squad_members(
                        int(squad['id']))
                    if len(members) >= MAX_SQUAD_MEMBERS:
                        code = RC_SQUAD_SLOT_NOT_FOUND
                        log(f'      [squad] invite rejected: squad='
                            f'{squad["id"]} full ({len(members)}/'
                            f'{MAX_SQUAD_MEMBERS})')
                except Exception as e:
                    log(f'      [DB] squad capacity lookup failed: {e}')

            # ★ 官服入队规则：入队约束 = A（邀请者）选中角色的阵营+兵种；
            #   B 当前选中士兵同阵营同兵种 → 直接入队；否则取 B 该阵营
            #   该兵种第一个角色；B 无对应角色 → 入队失败（17），
            #   隐式建队也不发生（否则建出必炸的混阵营队）
            anchor_char = None
            if code == RC_OK:
                if squad is not None:
                    spec = await _squad_join_spec(squad)
                else:
                    anchor_char = None
                    if owner_char_id:
                        anchor_char = await STORE.get_character(
                            int(pid), owner_char_id)
                    if anchor_char is None:
                        anchor_char = await STORE.get_latest_character(
                            int(pid))
                    spec = (int(anchor_char['faction']),
                            squad_template_for_character(
                                anchor_char['template'])) \
                        if anchor_char is not None else None
                friend_char, direct = (None, False)
                if spec is not None:
                    friend_char, direct = await _resolve_join_char(
                        int(friend_player_id), spec[0], spec[1])
                if friend_char is None:
                    code = RC_CHARACTER_NOT_OWNED
                    log(f'      [squad] invite rejected: friend='
                        f'{friend_player_id} has no char for faction='
                        f'{spec[0] if spec else "?"} squad_tmpl='
                        f'{spec[1] if spec else "?"}')

            if code == RC_OK:
                if squad is None:
                    # ★ 客户端从不发 CreateSquadRequest（SquadService.inviteFriend
                    #   直接发本请求，桌面/网页两版 swf 均无建队调用）：官服由
                    #   服务端在首次邀请时隐式建队。用锚点角色（选中角色，兜底
                    #   最新角色）建队 —— 与上面解析 B 用的是同一约束。
                    # 清残留 solo 隐式小队（上一局 squadId=0 行仍在客户端
                    # SquadSession 表），否则新真队行入表后两行并存冲突
                    solo = _SOLO_SQUADS.pop(int(pid), None)
                    if solo is not None and sess.playerview_cmd is not None:
                        await _push_kvcs_delete(
                            writer, sess.playerview_cmd,
                            b'SquadMemberSession', solo['member_row'])
                        await _push_kvcs_delete(
                            writer, sess.playerview_cmd,
                            b'SquadSession', solo['squad_row'])
                        log(f'S->C cmd={sess.playerview_cmd:<4} '
                            f'KeyValueChangeSet (solo squad delete '
                            f'id={solo["squad_row"]} ★before implicit create)')
                    name = f"Squad of {sess.username or 'Player'}"
                    created_squad_id = await STORE.create_squad(
                        anchor_char['id'], anchor_char['faction'], name)
                    squad = await STORE.get_squad(created_squad_id)
                    log(f'      [DB] implicit create_squad id='
                        f'{created_squad_id} leader_char={anchor_char["id"]} '
                        f'faction={anchor_char["faction"]} (on invite)')
                member_row = await STORE.add_squad_member(
                    squad['id'], friend_char['id'], status=ST_INVITE_PENDING)
                log(f'      [DB] squad_invite squad={squad["id"]} '
                    f'friend_char={friend_char["id"]} '
                    f'{"(selected)" if direct else "(first matching)"} '
                    f'row={member_row}')
        except Exception as e:
            log(f'      [DB] squad_invite failed: {e}')
            code = RC_PLAYER_NOT_FOUND

    if code == RC_OK and member_row:
        if created_squad_id:
            # 隐式建队：队长此前无队 → 整队行（SquadSession + 队长 InLobby 行
            # + 被邀请者 pending 行）一起推，客户端小队 UI 才会激活
            await push_squad_rows(sess, writer, created_squad_id)
        else:
            await push_squad_member_row(sess, writer, member_row)
        # 被邀请方实时收邀请（整队行含其 pending 成员行 → 弹邀请提示）；
        # 不推则对方要重登才能看到邀请
        if STORE is not None:
            invited_tag = await STORE.get_username(int(friend_player_id))
            invited_sess = FLASH_SESSIONS.get(invited_tag or '')
            if invited_sess is not None and invited_sess.playerview_cmd is not None:
                try:
                    await push_squad_rows(invited_sess, invited_sess.writer,
                                          squad['id'])
                except Exception as e:
                    log(f'      [squad] invite push to {invited_tag!r} '
                        f'failed: {e}')

    resp = build_response(cmd_id, 'SquadInvitePlayerResponse',
                          pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SquadInvitePlayerResponse '
        f'(friend={friend_player_id}, result={code})')


async def handle_squad_respond_to_invite(sess, writer, cmd_id, req_type, proto):
    """SquadRespondToInviteRequest {squadMemberSessionId=1, hasAccepted=2,
    characterId=3} → SquadRespondToInviteResponse{result=1}。

    接受：按小队（队长角色）阵营+兵种复核入队角色（f3 选中匹配优先，
    官服入队规则同 invite）并置 InLobby；拒绝：删成员行。"""
    member_row, has_accepted, char_id = 0, False, 0
    try:
        f = pb_parse(proto)
        member_row = int(f.get(1, 0) or 0)
        has_accepted = bool(f.get(2, 0))
        char_id = int(f.get(3, 0) or 0)
    except Exception as e:
        log(f'      [squad] SquadRespondToInvite parse error: {e}')

    pid = await sess.ensure_player()
    code = RC_OK
    declined_squad_id = 0
    if pid is None or STORE is None or not member_row:
        code = RC_SQUAD_SLOT_NOT_FOUND
    else:
        try:
            mem = await STORE.get_squad_member(member_row)
            if mem is None:
                code = RC_SQUAD_SLOT_NOT_FOUND
            elif has_accepted:
                # 记录被邀者当前选中士兵（f3 characterId，落库）
                if char_id:
                    PLAYER_SELECTED_CHAR[int(pid)] = int(char_id)
                    try:
                        await STORE.set_player_selected_char(
                            int(pid), int(char_id))
                    except Exception as e:
                        log(f'      [DB] set_player_selected_char '
                            f'failed: {e}')
                # ★ 官服入队规则同 invite：接受时按小队（队长角色）约束
                #   复核入队角色 —— f3 选中角色匹配则直接用，否则用邀请时
                #   存入成员行的解析角色/首个匹配角色；无匹配 → 17 拒绝
                squad = await STORE.get_squad(int(mem['squad_id']))
                spec = await _squad_join_spec(squad) if squad else None
                resolved, _direct = (None, False)
                if spec is not None:
                    resolved, _direct = await _resolve_join_char(
                        int(pid), spec[0], spec[1],
                        selected_id=(char_id or
                                     int(mem['character_id'] or 0)))
                if resolved is None:
                    code = RC_CHARACTER_NOT_OWNED
                    log(f'      [squad] accept rejected: pid={pid} has no '
                        f'char for faction={spec[0] if spec else "?"} '
                        f'squad_tmpl={spec[1] if spec else "?"}')
                else:
                    await STORE.update_squad_member(
                        member_row,
                        character_id=resolved['id'],
                        status=ST_IN_LOBBY)
                    log(f'      [DB] squad_invite accepted row={member_row} '
                        f'char={resolved["id"]} '
                        f'(requested={char_id or mem["character_id"]})')
            else:
                declined_squad_id = int(mem['squad_id'])
                await STORE.remove_squad_member(member_row)
                log(f'      [DB] squad_invite declined row={member_row}')
        except Exception as e:
            log(f'      [DB] squad_respond failed: {e}')
            code = RC_SQUAD_SLOT_NOT_FOUND

    if code == RC_OK:
        if has_accepted:
            await push_squad_member_row(sess, writer, member_row)
            mem_row = await STORE.get_squad_member(member_row) \
                if STORE is not None else None
            if mem_row is not None:
                await push_squad_rows(sess, writer, mem_row['squad_id'])
                # 队长等在线成员实时看到新成员入队
                await push_squad_rows_to_all_members(mem_row['squad_id'])
        else:
            await delete_squad_member_row(sess, writer, member_row)
            # 队长的待确认列表实时移除该行
            await delete_squad_member_row_everywhere(member_row,
                                                     declined_squad_id)

    resp = build_response(cmd_id, 'SquadRespondToInviteResponse',
                          pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SquadRespondToInviteResponse '
        f'(member={member_row}, accepted={has_accepted}, result={code})')


async def handle_set_squad_details(sess, writer, cmd_id, req_type, proto):
    """SetSquadDetailsRequest {characterId=1, squadName=2, ...}
    → SetSquadDetailsResponse{response=1}。

    小队改名（squadName 非空时；官服改名收费，私服免费直接改）。"""
    char_id, squad_name = 0, ''
    try:
        f = pb_parse(proto)
        char_id = int(f.get(1, 0) or 0)
        squad_name = _str(f.get(2, '') or '')
    except Exception as e:
        log(f'      [squad] SetSquadDetails parse error: {e}')

    pid = await sess.ensure_player()
    code = RC_OK
    squad_id = 0
    if pid is None or STORE is None:
        code = RC_SQUAD_NOT_FOUND
    else:
        try:
            squad = await STORE.get_squad_for_player(int(pid))
            if squad is None:
                code = RC_SQUAD_NOT_FOUND
            else:
                squad_id = squad['id']
                if squad_name:
                    await STORE.rename_squad(squad_id, squad_name)
                    log(f'      [DB] rename_squad id={squad_id} '
                        f'name={squad_name!r}')
        except Exception as e:
            log(f'      [DB] set_squad_details failed: {e}')
            code = RC_SQUAD_NOT_FOUND

    if code == RC_OK and squad_name and squad_id:
        await push_squad_rows(sess, writer, squad_id)
        # 改名同步给全体在线成员
        await push_squad_rows_to_all_members(squad_id)

    resp = build_response(cmd_id, 'SetSquadDetailsResponse', pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetSquadDetailsResponse '
        f'(name={squad_name!r}, response={code})')


async def handle_set_squad_matchmaking_flag(sess, writer, cmd_id, req_type,
                                            proto):
    """SetSquadMatchMakingFlagRequest {squadSessionId=1,
    matchMakeWarMissionFlag=3} → SetSquadMatchMakingFlagResponse{response=1}。"""
    squad_session_id, flag = 0, False
    try:
        f = pb_parse(proto)
        squad_session_id = int(f.get(1, 0) or 0)
        flag = bool(f.get(3, 0))
    except Exception as e:
        log(f'      [squad] SetSquadMatchMakingFlag parse error: {e}')

    code = RC_OK
    if STORE is None or not squad_session_id:
        code = RC_SQUAD_NOT_FOUND
    else:
        try:
            updated = await STORE.set_squad_flag(squad_session_id, flag)
            if not updated:
                code = RC_SQUAD_NOT_FOUND
            else:
                log(f'      [DB] set_squad_flag id={squad_session_id} '
                    f'flag={flag}')
        except Exception as e:
            log(f'      [DB] set_squad_flag failed: {e}')
            code = RC_SQUAD_NOT_FOUND

    if code == RC_OK:
        await push_squad_rows(sess, writer, squad_session_id)
        # 匹配开关同步给全体在线成员（小队界面状态一致）
        await push_squad_rows_to_all_members(squad_session_id)

    resp = build_response(cmd_id, 'SetSquadMatchMakingFlagResponse',
                          pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetSquadMatchMakingFlagResponse '
        f'(squad={squad_session_id}, flag={flag}, response={code})')


async def handle_set_squad_member_status(sess, writer, cmd_id, req_type,
                                         proto):
    """SetSquadMemberStatusRequest {squadSlotId=1, memberCharacterId=2,
    squadMemberSessionStatus=3} → SetSquadMemberStatusResponse{response=1}。

    squadSlotId = SquadMemberSession.id；memberCharacterId 非 0 时换角色。"""
    slot_id, member_char_id, status = 0, 0, 0
    try:
        f = pb_parse(proto)
        slot_id = int(f.get(1, 0) or 0)
        member_char_id = int(f.get(2, 0) or 0)
        status = int(f.get(3, 0) or 0)
    except Exception as e:
        log(f'      [squad] SetSquadMemberStatus parse error: {e}')

    code = RC_OK
    if STORE is None or not slot_id:
        code = RC_SQUAD_SLOT_NOT_FOUND
    else:
        try:
            mem = await STORE.get_squad_member(slot_id)
            if mem is None:
                code = RC_SQUAD_SLOT_NOT_FOUND
            elif member_char_id:
                # ★ 官服换角色语义（两个分支）：
                #   队长行换角色 → 小队类型跟随（leader_character_id +
                #     faction 更新），全体成员按新约束重新解析：有匹配
                #     角色的自动换角，无匹配的请离（删行 + KVCS 推全员）
                #   成员行换角色 → 必须同阵营+同兵种（否则 SquadModel 崩溃）
                spec = None
                me_pid = 0
                is_leader_row = False
                members = []
                squad = None
                try:
                    members = await STORE.list_squad_members(
                        int(mem['squad_id']))
                    squad = await STORE.get_squad(int(mem['squad_id']))
                    leader = next(
                        (m for m in members if squad is not None and
                         int(m['character_id']) ==
                         int(squad['leader_character_id'])),
                        members[0] if members else None)
                    if leader is not None:
                        spec = (int(leader['faction']),
                                squad_template_for_character(
                                    leader['template']))
                    me = next((m for m in members
                               if int(m['id']) == int(slot_id)), None)
                    if me is not None:
                        me_pid = int(me['player_id'])
                        is_leader_row = (leader is not None and
                                         int(me['id']) ==
                                         int(leader['id']))
                except Exception as e:
                    log(f'      [DB] squad member lookup failed: {e}')

                own_char = None
                if me_pid:
                    try:
                        own_char = await STORE.get_character(
                            me_pid, member_char_id)
                    except Exception as e:
                        log(f'      [DB] get_character failed: {e}')
                if own_char is None:
                    code = RC_CHARACTER_NOT_OWNED
                elif is_leader_row:
                    # 队长换角色：小队类型跟随 + 成员重新校验请离
                    await _leader_switch_spec(
                        squad, members, slot_id, own_char, status)
                else:
                    switch_ok = False
                    if spec is not None:
                        resolved, _d = await _resolve_join_char(
                            me_pid, spec[0], spec[1],
                            selected_id=member_char_id)
                        switch_ok = (resolved is not None and
                                     int(resolved['id']) ==
                                     int(member_char_id))
                    if not switch_ok:
                        code = RC_CHARACTER_NOT_OWNED
                        log(f'      [squad] switch rejected: char '
                            f'{member_char_id} not squad-compatible '
                            f'(faction={spec[0] if spec else "?"} '
                            f'squad_tmpl={spec[1] if spec else "?"})')
                    else:
                        await STORE.update_squad_member(
                            slot_id, character_id=member_char_id,
                            status=status if status else None)
                        log(f'      [DB] set_squad_member_status '
                            f'row={slot_id} char={member_char_id} '
                            f'status={status}')
            else:
                await STORE.update_squad_member(
                    slot_id,
                    character_id=None,
                    status=status if status else None)
                log(f'      [DB] set_squad_member_status row={slot_id} '
                    f'char={mem["character_id"]} '
                    f'status={status or mem["status"]}')
        except Exception as e:
            log(f'      [DB] set_squad_member_status failed: {e}')
            code = RC_SQUAD_SLOT_NOT_FOUND

    if code == RC_OK:
        await push_squad_member_row(sess, writer, slot_id)
        # 成员状态变化（Ready/换角色）同步给全体在线成员
        if STORE is not None:
            mem = await STORE.get_squad_member(slot_id)
            if mem is not None:
                await push_squad_rows_to_all_members(int(mem['squad_id']))

    resp = build_response(cmd_id, 'SetSquadMemberStatusResponse',
                          pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetSquadMemberStatusResponse '
        f'(slot={slot_id}, char={member_char_id}, status={status}, '
        f'response={code})')


async def handle_update_squad_resource(sess, writer, cmd_id, req_type, proto):
    """UpdateSquadResourceRequest {squadSessionId=1,
    resourcePurchaseList=2 repeated, paymentCurrency=3}
    → UpdateSquadResourceResponse{response=1}。

    小队资源补充购买：私服无资源库存系统，逐条扣款后 ok。"""
    squad_session_id, currency = 0, 0
    purchase_count = 0
    try:
        fields = pb_parse_multi(proto)
        squad_session_id = int(fields.get(1, [0])[0] or 0) \
            if fields.get(1) else 0
        purchase_count = len(fields.get(2, []))
        currency = int(fields.get(3, [0])[0] or 0) if fields.get(3) else 0
    except Exception as e:
        log(f'      [squad] UpdateSquadResource parse error: {e}')

    code = RC_OK
    if STORE is None or not squad_session_id:
        code = RC_SQUAD_NOT_FOUND
    else:
        try:
            squad = await STORE.get_squad(squad_session_id)
            if squad is None:
                code = RC_SQUAD_NOT_FOUND
        except Exception as e:
            log(f'      [DB] update_squad_resource lookup failed: {e}')
            code = RC_SQUAD_NOT_FOUND

    resp = build_response(cmd_id, 'UpdateSquadResourceResponse',
                          pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} UpdateSquadResourceResponse '
        f'(squad={squad_session_id}, purchases={purchase_count}, '
        f'currency={currency}, response={code})')


# ------------------------------------------------------------- 军团（Company）

async def handle_create_company(sess, writer, cmd_id, req_type, proto):
    """CreateCompanyRequest {nameLong=1, nameShort=2, description=3}
    → CreateCompanyResponse{result=1}。

    名称校验（长度/重名）→ companies + company_members(role=2) 入库。"""
    name_long, name_short, description = '', '', ''
    try:
        f = pb_parse(proto)
        name_long = _str(f.get(1, '') or '')
        name_short = _str(f.get(2, '') or '')
        description = _str(f.get(3, '') or '')
    except Exception as e:
        log(f'      [squad] CreateCompany parse error: {e}')

    pid = await sess.ensure_player()
    code = RC_OK
    company_id = 0
    if pid is None or STORE is None:
        code = RC_PLAYER_NOT_FOUND
    else:
        try:
            existing = await STORE.get_company_for_player(int(pid))
            if existing is not None:
                code = RC_CO_ALREADY_IN_COMPANY
            elif not _CO_LONG_MIN <= len(name_long) <= _CO_LONG_MAX:
                code = (RC_CO_LONG_TOO_SHORT if len(name_long) < _CO_LONG_MIN
                        else RC_CO_LONG_TOO_LONG)
            elif not _CO_SHORT_MIN <= len(name_short) <= _CO_SHORT_MAX:
                code = (RC_CO_SHORT_TOO_SHORT if len(name_short) < _CO_SHORT_MIN
                        else RC_CO_SHORT_TOO_LONG)
            elif len(description) > _CO_DESC_MAX:
                code = 184                     # company_companyDescriptionTooLong
            elif await STORE.company_name_exists(name_long):
                code = RC_CO_NAME_EXISTS
            else:
                company_id = await STORE.create_company(
                    name_long, name_short, int(pid), description)
                log(f'      [DB] create_company id={company_id} '
                    f'name={name_long!r} leader={pid}')
        except Exception as e:
            log(f'      [DB] create_company failed: {e}')
            code = RC_PLAYER_NOT_FOUND

    resp = build_response(cmd_id, 'CreateCompanyResponse', pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} CreateCompanyResponse '
        f'(name={name_long!r}/{name_short!r}, result={code}, '
        f'company={company_id})')


async def handle_invite_to_company(sess, writer, cmd_id, req_type, proto):
    """InviteToCompanyRequest {playerToInvite=1}
    → InviteToCompanyResponse{result=1}。"""
    player_to_invite = 0
    try:
        f = pb_parse(proto)
        player_to_invite = int(f.get(1, 0) or 0)
    except Exception as e:
        log(f'      [squad] InviteToCompany parse error: {e}')

    pid = await sess.ensure_player()
    code = RC_OK
    if pid is None or STORE is None or not player_to_invite:
        code = RC_PLAYER_NOT_FOUND
    else:
        try:
            co = await STORE.get_company_for_player(int(pid))
            if co is None:
                code = RC_CO_PRIVILEGES      # 邀请者不在任何军团（无权限）
            elif int(co['leader_player_id']) != int(pid):
                code = RC_CO_PRIVILEGES      # 仅队长可邀请
            else:
                iid, code = await STORE.invite_to_company(
                    co['id'], player_to_invite, int(pid))
                log(f'      [DB] invite_to_company company={co["id"]} '
                    f'player={player_to_invite} code={code}')
        except Exception as e:
            log(f'      [DB] invite_to_company failed: {e}')
            code = RC_PLAYER_NOT_FOUND

    resp = build_response(cmd_id, 'InviteToCompanyResponse', pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} InviteToCompanyResponse '
        f'(player={player_to_invite}, result={code})')


async def handle_kick_from_company(sess, writer, cmd_id, req_type, proto):
    """KickFromCompanyRequest {playerToKick=1}
    → KickFromCompanyResponse{result=1}。"""
    player_to_kick = 0
    try:
        f = pb_parse(proto)
        player_to_kick = int(f.get(1, 0) or 0)
    except Exception as e:
        log(f'      [squad] KickFromCompany parse error: {e}')

    pid = await sess.ensure_player()
    code = RC_OK
    if pid is None or STORE is None or not player_to_kick:
        code = RC_PLAYER_NOT_FOUND
    else:
        try:
            if int(pid) == player_to_kick:
                code = RC_CO_CANNOT_KICK_SELF
            else:
                co = await STORE.get_company_for_player(int(pid))
                if co is None:
                    code = RC_CO_MEMBER_NOT_FOUND
                elif int(co['leader_player_id']) != int(pid):
                    code = RC_CO_PRIVILEGES   # 仅队长可踢人
                else:
                    removed = await STORE.remove_company_member(
                        co['id'], player_to_kick)
                    code = RC_OK if removed else RC_CO_MEMBER_NOT_FOUND
                    log(f'      [DB] kick_from_company company={co["id"]} '
                        f'player={player_to_kick} ok={removed}')
        except Exception as e:
            log(f'      [DB] kick_from_company failed: {e}')
            code = RC_PLAYER_NOT_FOUND

    resp = build_response(cmd_id, 'KickFromCompanyResponse', pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} KickFromCompanyResponse '
        f'(player={player_to_kick}, result={code})')


async def handle_leave_company(sess, writer, cmd_id, req_type, proto):
    """LeaveCompanyRequest {} → LeaveCompanyResponse{result=1}。"""
    pid = await sess.ensure_player()
    code, company_id = 186, 0
    if pid is not None and STORE is not None:
        try:
            code, company_id = await STORE.leave_company(int(pid))
            log(f'      [DB] leave_company player={pid} code={code} '
                f'company={company_id}')
        except Exception as e:
            log(f'      [DB] leave_company failed: {e}')

    resp = build_response(cmd_id, 'LeaveCompanyResponse', pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} LeaveCompanyResponse (result={code})')


async def handle_respond_company_invitation(sess, writer, cmd_id, req_type,
                                            proto):
    """RespondToCompanyInvitationRequest {invitationId=1, accept=2}
    → RespondToCompanyInvitationResponse{result=1}。"""
    invitation_id, accept = 0, False
    try:
        f = pb_parse(proto)
        invitation_id = int(f.get(1, 0) or 0)
        accept = bool(f.get(2, 0))
    except Exception as e:
        log(f'      [squad] RespondToCompanyInvitation parse error: {e}')

    code = RC_OK
    if STORE is None or not invitation_id:
        code = RC_CO_INVITE_NOT_FOUND
    else:
        try:
            code, company_id = await STORE.respond_company_invitation(
                invitation_id, accept)
            log(f'      [DB] respond_company_invitation invite='
                f'{invitation_id} accept={accept} code={code}')
        except Exception as e:
            log(f'      [DB] respond_company_invitation failed: {e}')
            code = RC_CO_INVITE_NOT_FOUND

    resp = build_response(cmd_id, 'RespondToCompanyInvitationResponse',
                          pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} RespondToCompanyInvitationResponse '
        f'(invite={invitation_id}, accept={accept}, result={code})')


def _parse_insignia_part(raw):
    """InsigniaPart { string partType=1; int64 partId=2; string fieldId=3;
    float scale=4; float offset=5; } → dict（float 为 wiretype 5 原始 4 字节）。"""
    f = pb_parse(raw) if raw else {}
    scale = f.get(4, b'')
    offset = f.get(5, b'')
    return {
        'partType': _str(f.get(1, b'') or b''),
        'partId': int(f.get(2, 0) or 0),
        'fieldId': _str(f.get(3, b'') or b''),
        'scale': struct.unpack('<f', scale)[0]
        if isinstance(scale, bytes) and len(scale) == 4 else 0.0,
        'offset': struct.unpack('<f', offset)[0]
        if isinstance(offset, bytes) and len(offset) == 4 else 0.0,
    }


async def handle_create_insignia(sess, writer, cmd_id, req_type, proto):
    """CreateInsigniaRequest {repeated InsigniaPart parts=1;
    repeated int64 pricingIds=2; int32 paymentCurrency=3}
    → CreateInsigniaResponse{status=1}。

    部件逐项扣款（pricingIds 与 parts 对应）→ player_insignias 入库。"""
    parts = []
    pricing_ids = []
    currency = 0
    try:
        f = pb_parse_multi(proto)
        parts = [_parse_insignia_part(raw) for raw in f.get(1, [])]
        pricing_ids = [int(p) for p in f.get(2, [])]
        currency = int(f.get(3, [0])[0])
        log(f'      [squad] CreateInsignia: {len(parts)} parts, '
            f'pricingIds={pricing_ids}, currency={currency}')
    except Exception as e:
        log(f'      [squad] CreateInsignia parse error: {e}')

    # 逐项扣款（部件购买费）；任一不足 → not_enough_credits
    from player_service import _charge_player
    for pid_price in pricing_ids:
        cost, ok = await _charge_player(sess, pid_price, currency)
        if not ok:
            resp = build_response(cmd_id, 'CreateInsigniaResponse',
                                  pb_int32(1, 56))
            writer.write(resp)
            await writer.drain()
            log(f'S->C cmd={cmd_id:<4} CreateInsigniaResponse '
                f'(status=56 not_enough) cost={cost}')
            return

    pid = await sess.ensure_player()
    status = RC_OK
    if pid is not None and STORE is not None and parts:
        try:
            insignia_id = await STORE.create_insignia(int(pid), parts)
            log(f'      [DB] create_insignia id={insignia_id} '
                f'player={pid} parts={len(parts)}')
        except Exception as e:
            log(f'      [DB] create_insignia failed: {e}')
            status = 64  # player_not_found（入库异常兜底）
    else:
        status = 64

    resp = build_response(cmd_id, 'CreateInsigniaResponse', pb_int32(1, status))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} CreateInsigniaResponse (status={status})')


async def handle_remove_insignia(sess, writer, cmd_id, req_type, proto):
    """RemoveInsigniaRequest {int64 insigniaId=1}
    → RemoveInsigniaResponse{status=1}。"""
    insignia_id = 0
    try:
        f = pb_parse(proto)
        insignia_id = int(f.get(1, 0) or 0)
    except Exception as e:
        log(f'      [squad] RemoveInsignia parse error: {e}')

    status = RC_OK
    pid = await sess.ensure_player()
    if pid is not None and STORE is not None and insignia_id:
        try:
            removed = await STORE.remove_insignia(int(pid), insignia_id)
            log(f'      [DB] remove_insignia id={insignia_id} '
                f'player={pid} removed={removed}')
        except Exception as e:
            log(f'      [DB] remove_insignia failed: {e}')

    resp = build_response(cmd_id, 'RemoveInsigniaResponse', pb_int32(1, status))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} RemoveInsigniaResponse (status={status}, '
        f'id={insignia_id})')


HANDLERS = {
    'CreateSquadRequest': handle_create_squad,
    'ModifySquadRequest': handle_modify_squad,
    'RemoveSquadRequest': handle_remove_squad,
    'RemoveSquadMemberRequest': handle_remove_squad_member,
    'SquadInvitePlayerRequest': handle_squad_invite_player,
    'SquadRespondToInviteRequest': handle_squad_respond_to_invite,
    'SetSquadDetailsRequest': handle_set_squad_details,
    'SetSquadMatchMakingFlagRequest': handle_set_squad_matchmaking_flag,
    'SetSquadMemberStatusRequest': handle_set_squad_member_status,
    'UpdateSquadResourceRequest': handle_update_squad_resource,
    'CreateCompanyRequest': handle_create_company,
    'InviteToCompanyRequest': handle_invite_to_company,
    'KickFromCompanyRequest': handle_kick_from_company,
    'LeaveCompanyRequest': handle_leave_company,
    'RespondToCompanyInvitationRequest': handle_respond_company_invitation,
    'CreateInsigniaRequest': handle_create_insignia,
    'RemoveInsigniaRequest': handle_remove_insignia,
}
