# -*- coding: utf-8 -*-
"""小队域（squad）：小队创建/邀请/状态/成员管理。
（军团 Company/徽章 Insignia 已拆至 handlers/company.py；
 战斗服 SquadRequest 定义查询见 handlers/squad_match.py）

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

ResponseCodes（squad 域）：
- player_already_in_squad=157 squad_slot_not_found=158 player_not_owner=159
- squad_not_found=161 squad_size_too_small=162 squad_slot_in_use=163
- squad_not_owned_by_player=164 player_not_found=64
"""

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

from player_service import (
    SQUAD_NAME_MAX_LENGTH,
    _charge_player,
    _official_name_error,
    )

from messages import _push_kvcs
from matchmaking import (
    _cancel_squad_queue,
    _find_player_active_mission,
    _msg_auto_squad_session,
    _push_kvcs_delete,
    _try_form_all,
)
from mm_state import (
    _AI_FILL_TASKS,
    _MM_MISSION_SQUADS,
    _MM_QUEUES,
    _MATCH_PMM_IDS,
    _SOLO_SQUADS,
    POOL,
)
from wardb import _rank_from_combat_xp, squad_template_for_character

RC_OK = 1
RC_PLAYER_NOT_FOUND = 64
RC_CHARACTER_NOT_OWNED = 17
RC_PLAYER_ALREADY_IN_SQUAD = 157
RC_SQUAD_SLOT_NOT_FOUND = 158
RC_PLAYER_NOT_OWNER = 159
RC_SQUAD_NOT_FOUND = 161
RC_SQUAD_NOT_OWNED = 164
RC_SQUAD_IN_LOCKED_STATE = 261

# 官方小队人数上限（客户端 SquadModel.MAX_CHARACTERS_PER_SQUAD=4 硬编码，
# 反编译 swf-frontend/scripts/.../squad/model/SquadModel.as:108）。
# 客户端 InviteFriendToContextCommand 已用 squadModel.isFull 本地拦截
# "unable_to_invite_squad_full"，服务端此上限为兜底（回 158 slot not found）。
MAX_SQUAD_MEMBERS = 4

ST_INVITE_PENDING = 1
ST_IN_LOBBY = 2
ST_READY = 3
ST_IN_COMBAT = 4


def _str(v):
    if isinstance(v, bytes):
        return v.decode('utf-8', 'replace')
    return str(v)


def _leader_member(squad, members):
    """Resolve the authoritative leader row; never grant ownership by fallback."""
    if squad is None:
        return None
    leader_char = int(squad.get('leader_character_id', 0) or 0)
    return next((m for m in members or ()
                 if int(m.get('character_id', 0) or 0) == leader_char), None)


def _player_is_leader(squad, members, player_id):
    leader = _leader_member(squad, members)
    return leader is not None and int(leader['player_id']) == int(player_id or 0)


def _squad_is_locked(squad, members):
    """Member/customization mutations are forbidden while queued or in combat."""
    # matchmaking_flag is SquadSession.f12 (matchMakeWarMissionFlag), a
    # client-selected mode filter. Selecting war missions in the lobby must
    # not block accepting invitations or changing InLobby/Ready status.
    for member in members or ():
        if int(member.get('status', 0) or 0) == ST_INVITE_PENDING:
            continue  # An invitee's independent solo queue is not this squad.
        if int(member.get('status', 0) or 0) == ST_IN_COMBAT:
            return True
        # Only the authoritative queue registry establishes matchmaking locks.
        if int(member.get('player_id', 0) or 0) in _MM_QUEUES:
            return True
    return False


def _player_in_matchmaking(player_id):
    return int(player_id or 0) in _MM_QUEUES


def _squad_view_mission_id(members, player_id):
    """Keep an in-combat viewer's mission when another member returns to lobby."""
    member = next((m for m in members if int(m['player_id'])
                   == int(player_id or 0)), None)
    if member is None or int(member.get('status', 0) or 0) != ST_IN_COMBAT:
        return 0
    queue = _MM_QUEUES.get(int(player_id)) or {}
    return int(queue.get('mission_id') or
               _find_player_active_mission(player_id) or 0)


def _auto_squad_leader(info):
    members = list((info or {}).get('members') or [])
    leader_char = int((info or {}).get('leader_char_id', 0) or 0)
    if leader_char:
        leader = next((m for m in members
                       if int(m.get('char_id', 0) or 0) == leader_char), None)
        if leader is not None:
            return leader
    return members[0] if members else None


async def _repair_auto_squad_leader(mission_id, squad_id, info):
    """Elect and publish a replacement when an auto-squad leader leaves."""
    members = list((info or {}).get('members') or [])
    if not members:
        return
    leader_char = int((info or {}).get('leader_char_id', 0) or 0)
    if any(int(m.get('char_id', 0) or 0) == leader_char for m in members):
        return
    info['leader_char_id'] = int(members[0].get('char_id', 0) or 0)
    # Remaining clients must also see the new owner.  A member-only DELETE
    # leaves their cached SquadSession.ownerPlayerId pointing at the leaver.
    row = _msg_auto_squad_session(squad_id, info, mission_id)
    for member in members:
        flash = FLASH_SESSIONS.get(member.get('gamertag') or '')
        if flash is None or flash.playerview_cmd is None:
            continue
        try:
            await _push_kvcs(flash.writer, flash.playerview_cmd,
                             b'SquadSession', row)
        except Exception as e:
            # A dead peer must not prevent the roster mutation or the caller's
            # response; that peer will receive the repaired row on reconnect.
            log(f'      [squad] auto leader sync to '
                f'{member.get("gamertag")!r} failed: {e}')


async def release_auto_squad_memberships(mission_id, player_ids):
    """Detach departing players before restoring their lobby squad views.

    Only temporary squads in this mission are affected. Mutate every roster
    before yielding, so a lobby request cannot be captured by an old squad.
    Persistent squads keep their membership across battles.
    """
    mission_id = int(mission_id)
    player_ids = {int(pid) for pid in player_ids}
    squads = _MM_MISSION_SQUADS.get(mission_id)
    if not squads or not player_ids:
        return
    updates = []
    for squad_id, info in list(squads.items()):
        if int(info.get('squad_db_id', 0) or 0):
            continue
        members = list(info.get('members') or [])
        departed = [m for m in members
                    if int(m.get('player_id', 0) or 0) in player_ids]
        if not departed:
            continue
        remaining = [m for m in members
                     if int(m.get('player_id', 0) or 0) not in player_ids]
        info['members'] = remaining
        if not remaining:
            squads.pop(squad_id, None)
            continue
        leader_char = int(info.get('leader_char_id', 0) or 0)
        leader_changed = not any(int(m.get('char_id', 0) or 0) == leader_char
                                 for m in remaining)
        if leader_changed:
            info['leader_char_id'] = int(remaining[0].get('char_id', 0) or 0)
        updates.append((squad_id, info, departed, remaining, leader_changed))
    if not squads:
        _MM_MISSION_SQUADS.pop(mission_id, None)
    for squad_id, info, departed, remaining, leader_changed in updates:
        row = (_msg_auto_squad_session(squad_id, info, mission_id)
               if leader_changed else None)
        for member in remaining:
            flash = FLASH_SESSIONS.get(member.get('gamertag') or '')
            if flash is None or flash.playerview_cmd is None:
                continue
            try:
                for removed in departed:
                    await _push_kvcs_delete(
                        flash.writer, flash.playerview_cmd,
                        b'SquadMemberSession', int(removed['row_id']))
                if row is not None:
                    await _push_kvcs(flash.writer, flash.playerview_cmd,
                                     b'SquadSession', row)
            except Exception as e:
                log(f'      [squad] departure sync to '
                    f'{member.get("gamertag")!r} failed: {e}')


async def _push_member_delete_to_session(session, member_row_id):
    """Push a member DELETE to a captured session, including a just-kicked user."""
    if session is None or session.playerview_cmd is None:
        return
    try:
        await _push_kvcs_delete(session.writer, session.playerview_cmd,
                                b'SquadMemberSession', member_row_id)
    except Exception as e:
        log(f'      [squad] target delete push failed: {e}')


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
    locked = _squad_is_locked(squad, members)
    m += pb_bool(20, locked)                         # isLockedForMembers
    m += pb_bool(21, locked)                         # isLockedForCustomization
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


def _msg_squad_member_session(member, squad_id, uvt=0, upt=0, slot=0):
    """SquadMemberSession proto（20 字段，repeated 空省略）。

    uvt/upt：角色装备的迷彩/头盔模板 id（_member_appearance 解析）。
    slot：成员槽位（0 基）→ f3 = slot+1。★f3 必须是 SquadPlayerSlotTemplate
    表有效行 id（1-4）：客户端 SquadModel.activate() 对每条成员行
    getFromId(f3) 后直接取 .slotNumber，0 查无此行返回 null → Error #1009
    （进小队/士兵界面即抛，卡小队界面且匹配按钮不可用，重登复现——登录
    重推同样带 0）。solo 行官方=1（slot0→1），散人凑队=slot+1 同规律。"""
    m = pb_int64(1, member['id'])                    # id（成员行 id）
    m += pb_int64(2, squad_id)                       # squadSessionId
    m += pb_int64(3, int(slot or 0) + 1)             # squadPlayerSlotTemplateId
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
    # 槽位分配：队长置顶占 slot0（f3=1），其余按行序。list_squad_members
    # ORDER BY m.id 本就队长先插（建队先插队长行），排序仅兜底防御。
    members = sorted(members, key=lambda m: 0 if squad['leader_character_id']
                     and int(m['character_id']) == int(
                         squad['leader_character_id']) else 1)
    await _push_kvcs(writer, view_cmd, b'SquadSession',
                     _msg_squad_session(squad, members, mission_id,
                                        viewer_faction=viewer_faction))
    for slot, mem in enumerate(members):
        uvt, upt = await _member_appearance(mem)
        await _push_kvcs(writer, view_cmd, b'SquadMemberSession',
                         _msg_squad_member_session(mem, squad_id, uvt, upt,
                                                   slot=slot))
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
    # 槽位与整队推送同一套规则（队长置顶占 slot0，其余按行序）
    members = sorted(members, key=lambda x: 0 if squad['leader_character_id']
                     and int(x['character_id']) == int(
                         squad['leader_character_id']) else 1)
    slot = members.index(mem)
    uvt, upt = await _member_appearance(mem)
    await _push_kvcs(writer, sess.playerview_cmd, b'SquadMemberSession',
                     _msg_squad_member_session(mem, mem_row['squad_id'],
                                               uvt, upt, slot=slot))
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


async def push_squad_rows_to_all_members(squad_id, mission_id=None):
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
            viewer_mission = mission_id
            if viewer_mission is None:
                viewer_pid = getattr(s, 'player_id', 0) or next(
                    (m['player_id'] for m in members
                     if m.get('gamertag') == getattr(s, 'username', None)), 0)
                viewer_mission = _squad_view_mission_id(members, viewer_pid)
            await push_squad_rows(s, s.writer, squad_id, viewer_mission)
        except Exception as e:
            log(f'      [squad] push to {s.username!r} failed: {e}')


async def push_squad_deletes_to_sessions(sessions, member_ids, squad_id):
    """解散小队：向给定在线会话推 SquadSession + 全体成员行 DELETE。

    sessions 必须在 dissolve_squad 落库前收集（解散后查库为空）。
    ★SquadSession 行先删（2026-09-09）：客户端 onSquadSessionChange DELETE
    按 _squadSession 匹配触发 resetSquad() 全量清理；成员行先删会把
    _squadSession 置空 → SquadSession DELETE 变 no-op → isOwnedByPlayer
    残留 → 重登玩家切屏卡 READY_UP"小队未准备好"。"""
    for s in sessions:
        if s.playerview_cmd is None:
            continue
        try:
            await _push_kvcs_delete(s.writer, s.playerview_cmd,
                                    b'SquadSession', squad_id)
            for mid in member_ids:
                await _push_kvcs_delete(s.writer, s.playerview_cmd,
                                        b'SquadMemberSession', mid)
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
    elif _player_in_matchmaking(pid):
        code = RC_SQUAD_IN_LOCKED_STATE
    else:
        try:
            char = await STORE.get_character(int(pid), char_id)
            if char is None:
                code = RC_CHARACTER_NOT_OWNED        # character_not_owned_by_player
            elif not str(sess.username or '').strip():
                code = RC_PLAYER_NOT_FOUND
            else:
                # 一个账号只能处于一个真小队中；不能通过换另一个角色重复建队。
                existing = await STORE.get_squad_for_player(int(pid))
                if existing is not None:
                    code = RC_PLAYER_ALREADY_IN_SQUAD
                    squad_id = existing['id']
                else:
                    name = f"Squad of {str(sess.username).strip()}"
                    squad_id = await STORE.create_squad(
                        char_id, char['faction'], name)
                    log(f'      [DB] create_squad id={squad_id} '
                        f'leader_char={char_id} faction={char["faction"]}')
        except Exception as e:
            log(f'      [DB] create_squad failed: {e}')
            code = RC_PLAYER_NOT_FOUND

    if code == RC_OK and squad_id:
        # A completed match can leave the reusable solo row registered.  Once
        # a real squad exists it must be removed before publishing the DB row,
        # otherwise the client can retain two current SquadSession records.
        stale_solo = _SOLO_SQUADS.pop(int(pid), None)
        if stale_solo is not None and sess.playerview_cmd is not None:
            await _push_kvcs_delete(writer, sess.playerview_cmd,
                                    b'SquadSession',
                                    stale_solo['squad_row'])
            await _push_kvcs_delete(writer, sess.playerview_cmd,
                                    b'SquadMemberSession',
                                    stale_solo['member_row'])
        await push_squad_rows(sess, writer, squad_id)
        # 生涯军令 27 eCreateSquad（客户端无上报点）+ 推行实时刷新
        from handlers.career import complete_order_and_push
        await complete_order_and_push(sess, writer, pid, 27)

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
    if pid is None or STORE is None or not squad_session_id:
        code = RC_SQUAD_NOT_FOUND
    else:
        try:
            squad = await STORE.get_squad(squad_session_id)
            if squad is None:
                code = RC_SQUAD_NOT_FOUND
            else:
                members = await STORE.list_squad_members(squad_session_id)
                if not _player_is_leader(squad, members, pid):
                    code = RC_SQUAD_NOT_OWNED
                elif _squad_is_locked(squad, members):
                    code = RC_SQUAD_IN_LOCKED_STATE
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

    队长解散小队：删 KVCS 行（★SquadSession 先删 + 全体 SquadMemberSession，
    顺序见 push_squad_deletes_to_sessions 注释）；排队中先整队撤队
    （PMM delete 先推）。非队长调离队接口（RemoveSquadMemberRequest）。
    ★ 单人匹配的隐式小队（matchmaking._SOLO_SQUADS，squadId=0）不在 DB：
      SquadService.deactivate 对 isOwnedByPlayer 也发本请求，须先查注册表，
      否则 get_squad_for_player 找不到 → 161 squad_not_found 弹"解散失败"。"""
    pid = await sess.ensure_player()

    # ⓪ 匹配凑队（auto squad）：野队合组的临时小队行只在内存
    #    （_MM_MISSION_SQUADS），不在 DB 也不在 solo 注册表——两者都查不到
    #    时旧代码直接 161 "squad not found"，且客户端上的凑队行永远删不掉
    #    （2026-09-02 "解散后没有退出小队/再次解散 squad not found" 根因）。
    #    ★必须排在 solo 分支前：合并进凑队后 solo 注册表条目刻意保留
    #    （战斗结束恢复 solo 视图用），先走 solo 分支会误删早已不存在的
    #    旧 solo 行并消费掉该条目。
    if pid is not None:
        for _mid in list(_MM_MISSION_SQUADS.keys()):
            for _sq_id, _info in list(_MM_MISSION_SQUADS[_mid].items()):
                if int(_info.get('squad_db_id', 0) or 0):
                    continue          # DB 真小队走下方 ② 分支
                me = next((m for m in _info.get('members') or []
                           if int(m.get('player_id', 0) or 0) == int(pid)),
                          None)
                if me is None:
                    continue
                leader = _auto_squad_leader(_info)
                if leader is None or int(leader.get('player_id', 0) or 0) != \
                        int(pid):
                    resp = build_response(cmd_id, 'RemoveSquadResponse',
                                          pb_int32(1, RC_SQUAD_NOT_OWNED))
                    writer.write(resp)
                    await writer.drain()
                    log(f'S->C cmd={cmd_id:<4} RemoveSquadResponse '
                        f'(auto squad={_sq_id}, response='
                        f'{RC_SQUAD_NOT_OWNED} non-leader)')
                    return
                my_row = int(me['row_id'])
                all_rows = [int(m['row_id']) for m in _info['members']]
                # 删本人连接上的凑队行：★SquadSession 行必须先删——客户端
                #   onSquadSessionChange DELETE 分支按 _squadSession 匹配才
                #   触发 resetSquad() 全量清理（isOwnedByPlayer/isSquadLeader
                #   等一并清）；成员行先删会把 _squadSession 置空，SquadSession
                #   DELETE 变 no-op → isOwnedByPlayer 残留 true，重登过
                #   （wasThroughLoginProcessAndHasSessions=true）的玩家切屏
                #   screenChange→activate() 无条件置 isActive=true → 卡
                #   READY_UP_STATE"小队未准备好"（2026-09-09 解散卡住根因）
                if sess.playerview_cmd is not None:
                    await _push_kvcs_delete(writer, sess.playerview_cmd,
                                            b'SquadSession', int(_sq_id))
                    for rid in all_rows:
                        await _push_kvcs_delete(writer, sess.playerview_cmd,
                                                b'SquadMemberSession', rid)
                    log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet '
                        f'delete (auto squad id={_sq_id} + '
                        f'{len(all_rows)} member rows ★auto squad disband)')
                # 其他在线成员：只删本人在队内的成员行（面板移除该玩家）
                for m in _info['members']:
                    if int(m.get('player_id', 0) or 0) == int(pid):
                        continue
                    flash = FLASH_SESSIONS.get(m.get('gamertag') or '')
                    if flash is None or flash.playerview_cmd is None:
                        continue
                    await _push_kvcs_delete(flash.writer,
                                            flash.playerview_cmd,
                                            b'SquadMemberSession', my_row)
                _info['members'] = [m for m in _info['members']
                                    if int(m.get('player_id', 0) or 0)
                                    != int(pid)]
                if not _info['members']:    # 空壳凑队随手清（战局结束前）
                    _MM_MISSION_SQUADS[_mid].pop(_sq_id, None)
                    if not _MM_MISSION_SQUADS[_mid]:
                        _MM_MISSION_SQUADS.pop(_mid, None)
                else:
                    await _repair_auto_squad_leader(
                        _mid, _sq_id, _info)
                resp = build_response(cmd_id, 'RemoveSquadResponse',
                                      pb_int32(1, RC_OK))
                writer.write(resp)
                await writer.drain()
                log(f'S->C cmd={cmd_id:<4} RemoveSquadResponse '
                    f'(auto squad={_sq_id}, response=1)')
                return

    # ① 单人隐式小队：解散 = 退出匹配（若在队，小队是撮合单位）+ 删 KVCS
    #    行（★SquadSession 先删）+ 清注册表（下次 join 重建）。
    #    ★2026-09-09 两版教训：
    #    - 首版"成员行先删"：客户端 handleClearingSlot 置空 _squadSession →
    #      SquadSession DELETE 变 no-op，resetSquad 被跳过 → isOwnedByPlayer
    #      残留 true → 重登玩家（wasThroughLoginProcessAndHasSessions=true）
    #      切屏 screenChange→activate() 无条件置 isActive=true → 卡
    #      READY_UP_STATE"小队未准备好"（按钮禁用）。
    #    - 二版"不删行改重推 idle solo 行"：小队永不消失，解散无效果。
    #    终版：SquadSession 先删（resetSquad 全量清理）+ 排队中一并撤队
    #    （PMM delete 先推——否则客户端 ActivateSquadCommand 激活态遇上
    #    squadSession=null，onLobbySignalUpdates 读 missionId 抛
    #    TypeError #1009 → UI 永卡 processing）。
    if pid is not None:
        # ① 前置防劫持（2026-09-09）：solo 注册表条目在玩家加入真小队后
        #    不会被清理（战斗结束恢复 solo 视图的设计使然）。带残留条目
        #    解散真小队时若直接消费旧条目 → 删旧 solo 行 + 回 OK，真小队
        #    未删 → "小队没法解散"。玩家在 DB 真小队中 → 跳过本分支走 ②。
        in_real_squad = False
        if STORE is not None:
            try:
                in_real_squad = (await STORE.get_squad_for_player(
                    int(pid))) is not None
            except Exception as e:
                log(f'      [DB] solo disband pre-check failed: {e}')
        solo = None if in_real_squad else _SOLO_SQUADS.pop(int(pid), None)
        if solo is not None:
            if sess.playerview_cmd is not None:
                q = _MM_QUEUES.pop(int(pid), None)
                if q is not None:
                    POOL.remove(int(pid))
                    _MATCH_PMM_IDS.pop(int(pid), None)
                    t = _AI_FILL_TASKS.pop(int(pid), None)
                    if t is not None:
                        t.cancel()
                    if q.get('pmm_id') is not None:
                        await _push_kvcs_delete(
                            writer, sess.playerview_cmd,
                            b'PlayerMatchMaking', q['pmm_id'])
                        log(f'S->C cmd={sess.playerview_cmd:<4} '
                            f'KeyValueChangeSet delete (PlayerMatchMaking '
                            f'id={q["pmm_id"]} ★disband cancels queue)')
                    await _try_form_all()
                await _push_kvcs_delete(writer, sess.playerview_cmd,
                                        b'SquadSession', solo['squad_row'])
                await _push_kvcs_delete(writer, sess.playerview_cmd,
                                        b'SquadMemberSession',
                                        solo['member_row'])
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
                if not _player_is_leader(squad, members, pid):
                    code = RC_SQUAD_NOT_OWNED
                else:
                    member_ids = [m['id'] for m in members]
                    # 解散后查库为空 → 先收集在线会话再 dissolve
                    live_sessions = _online_member_sessions(members)
                    # ★解散前整队撤出匹配队列：排队中的真小队解散不清队会留
                    #   幽灵 entry（group_key 指向已删的 squad_id，成局后向不
                    #   存在的小队推局）。必须在 dissolve 前调。
                    await _cancel_squad_queue(squad_id, push_rows=False)
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
            await _push_kvcs_delete(writer, sess.playerview_cmd,
                                    b'SquadSession', squad_id)
            for mid in member_ids:
                await _push_kvcs_delete(writer, sess.playerview_cmd,
                                        b'SquadMemberSession', mid)
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

    # ⓪ 匹配凑队（auto squad）成员行：只在内存（_MM_MISSION_SQUADS），
    #    不在 DB——SquadService.deactivate 成员离队 / clearSlot 队长踢人
    #    都带凑队成员行 id，走 DB 必查空 → 158 弹错且行删不掉（与
    #    handle_remove_squad 凑队分支同族的补漏，2026-09-02）。
    #    权限：请求者 = 该成员本人（离队）或同队成员（队长踢人）。
    if pid is not None and member_row:
        for _mid in list(_MM_MISSION_SQUADS.keys()):
            for _sq_id, _info in list(
                    (_MM_MISSION_SQUADS.get(_mid) or {}).items()):
                if int(_info.get('squad_db_id', 0) or 0):
                    continue          # DB 真小队走下方原分支
                me = next((m for m in _info.get('members') or []
                           if int(m.get('row_id', 0) or 0) == member_row),
                          None)
                if me is None:
                    continue
                requester = next(
                    (m for m in _info.get('members') or []
                     if int(m.get('player_id', 0) or 0) == int(pid)), None)
                leader = _auto_squad_leader(_info)
                if requester is None:
                    break              # 请求者不在该凑队 → 交回原分支判错
                if (int(me.get('player_id', 0) or 0) != int(pid)
                        and (leader is None or
                             int(leader.get('player_id', 0) or 0) != int(pid))):
                    resp = build_response(
                        cmd_id, 'RemoveSquadMemberResponse',
                        pb_int32(1, RC_PLAYER_NOT_OWNER))
                    writer.write(resp)
                    await writer.drain()
                    return
                # 删除该成员行：请求者本人 + 其他在线成员连接都推
                if sess.playerview_cmd is not None:
                    await _push_kvcs_delete(writer, sess.playerview_cmd,
                                            b'SquadMemberSession', member_row)
                for m in _info.get('members') or []:
                    if int(m.get('player_id', 0) or 0) == int(pid):
                        continue
                    flash = FLASH_SESSIONS.get(m.get('gamertag') or '')
                    if flash is None or flash.playerview_cmd is None:
                        continue
                    await _push_kvcs_delete(flash.writer,
                                            flash.playerview_cmd,
                                            b'SquadMemberSession', member_row)
                _info['members'] = [m for m in _info.get('members') or []
                                    if int(m.get('row_id', 0) or 0)
                                    != member_row]
                if not _info['members']:    # 空壳凑队随手清（战局结束前）
                    _MM_MISSION_SQUADS[_mid].pop(_sq_id, None)
                    if not _MM_MISSION_SQUADS[_mid]:
                        _MM_MISSION_SQUADS.pop(_mid, None)
                else:
                    await _repair_auto_squad_leader(
                        _mid, _sq_id, _info)
                resp = build_response(cmd_id, 'RemoveSquadMemberResponse',
                                      pb_int32(1, RC_OK))
                writer.write(resp)
                await writer.drain()
                log(f'S->C cmd={cmd_id:<4} RemoveSquadMemberResponse '
                    f'(member={member_row}, response=1 '
                    f'★auto squad id={_sq_id})')
                return

    target_session = None
    if pid is None or STORE is None or not member_row:
        code = RC_SQUAD_SLOT_NOT_FOUND
    else:
        try:
            mem = await STORE.get_squad_member(member_row)
            if mem is None:
                code = RC_SQUAD_SLOT_NOT_FOUND
            else:
                removed_squad_id = int(mem['squad_id'])
                squad = await STORE.get_squad(removed_squad_id)
                members = await STORE.list_squad_members(removed_squad_id)
                target = next((m for m in members
                               if int(m['id']) == int(member_row)), None)
                if squad is None or target is None:
                    code = RC_SQUAD_SLOT_NOT_FOUND
                elif _squad_is_locked(squad, members):
                    code = RC_SQUAD_IN_LOCKED_STATE
                elif (int(target['player_id']) != int(pid)
                      and not _player_is_leader(squad, members, pid)):
                    code = RC_PLAYER_NOT_OWNER
                elif int(target['character_id']) == int(
                        squad.get('leader_character_id', 0) or 0):
                    # 队长必须走 RemoveSquadRequest 解散，不能留下无主/空队。
                    code = RC_SQUAD_NOT_OWNED
                else:
                    target_session = FLASH_SESSIONS.get(
                        target.get('gamertag') or '')
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
        if target_session is not None and target_session is not sess:
            await _push_member_delete_to_session(target_session, member_row)
        # Slots are assigned from roster order. Compact all surviving rows
        # before a later invite reuses the old last slot.
        await push_squad_rows_to_all_members(removed_squad_id)

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
    replacements = []
    kicked_sessions = {}
    for m in members:
        if int(m['id']) == int(slot_id):
            continue
        try:
            resolved, _d = await _resolve_join_char(
                int(m['player_id']), new_spec[0], new_spec[1],
                selected_id=int(m['character_id']))
        except Exception as e:
            log(f'      [DB] member re-resolve failed: {e}')
            resolved, _d = None, False
        if resolved is not None:
            if int(resolved['id']) != int(m['character_id']):
                replacements.append((int(m['id']), int(resolved['id'])))
        else:
            kicked.append(int(m['id']))
            kicked_sessions[int(m['id'])] = FLASH_SESSIONS.get(
                m.get('gamertag') or '')

    try:
        if hasattr(STORE, 'reconfigure_squad_members'):
            ok = await STORE.reconfigure_squad_members(
                squad_id, slot_id, int(new_leader_char['id']), new_spec[0],
                int(status) if status else 0, replacements, kicked)
            if not ok:
                raise RuntimeError('squad disappeared during leader switch')
        else:  # lightweight regression fakes
            await STORE.update_squad_leader(
                squad_id, int(new_leader_char['id']), new_spec[0])
            await STORE.update_squad_member(
                slot_id, character_id=int(new_leader_char['id']),
                status=int(status) if status else None)
            for member_id, resolved_id in replacements:
                await STORE.update_squad_member(
                    member_id, character_id=resolved_id)
            for member_id in kicked:
                await STORE.remove_squad_member(member_id)
    except Exception as e:
        log(f'      [DB] leader switch failed: {e}')
        raise

    log(f'      [DB] leader switch: squad={squad_id} -> char='
        f'{new_leader_char["id"]} faction={new_spec[0]} '
        f'squad_tmpl={new_spec[1]}')
    for member_id, resolved_id in replacements:
        log(f'      [DB] member auto-switch row={member_id} '
            f'-> char={resolved_id} (follow leader spec)')
    for m in members:
        if int(m['id']) in kicked:
            log(f'      [DB] member kicked row={m["id"]} '
                f'pid={m["player_id"]} (no char for new spec '
                f'faction={new_spec[0]} squad_tmpl={new_spec[1]})')

    # 请离者已不在 DB roster，必须使用删除前捕获的会话定向推送。
    for row_id in kicked:
        try:
            await delete_squad_member_row_everywhere(row_id, squad_id)
            await _push_member_delete_to_session(
                kicked_sessions.get(row_id), row_id)
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
    elif int(friend_player_id) == int(pid):
        code = RC_PLAYER_ALREADY_IN_SQUAD
    elif (_player_in_matchmaking(pid)
          or _player_in_matchmaking(friend_player_id)):
        code = RC_SQUAD_IN_LOCKED_STATE
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
            if squad is not None:
                members = await STORE.list_squad_members(int(squad['id']))
                if not _player_is_leader(squad, members, pid):
                    code = RC_PLAYER_NOT_OWNER
                elif _squad_is_locked(squad, members):
                    code = RC_SQUAD_IN_LOCKED_STATE

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

            # 被邀者已在任意真小队（包括当前队/pending）→ 157。
            # 容量错误保持客户端原有优先级；非满员时重复邀请绝不把正式
            # 成员 UPSERT 回 pending，也不允许同一玩家用另一角色占第二槽。
            if code == RC_OK:
                friend_squad = await STORE.get_squad_for_player(
                    int(friend_player_id))
                if friend_squad is not None:
                    code = RC_PLAYER_ALREADY_IN_SQUAD

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
                    username = str(sess.username or '').strip()
                    if not username:
                        code = RC_PLAYER_NOT_FOUND
                        raise ValueError('authenticated squad owner has no username')
                    # ★ 客户端从不发 CreateSquadRequest（SquadService.inviteFriend
                    #   直接发本请求，桌面/网页两版 swf 均无建队调用）：官服由
                    #   服务端在首次邀请时隐式建队。用锚点角色（选中角色，兜底
                    #   最新角色）建队 —— 与上面解析 B 用的是同一约束。
                    name = f"Squad of {username}"
                    if hasattr(STORE, 'create_squad_with_invite'):
                        created_squad_id, member_row = \
                            await STORE.create_squad_with_invite(
                                anchor_char['id'], anchor_char['faction'],
                                name, friend_char['id'], ST_INVITE_PENDING)
                    else:  # lightweight regression fakes
                        created_squad_id = await STORE.create_squad(
                            anchor_char['id'], anchor_char['faction'], name)
                    squad = await STORE.get_squad(created_squad_id)
                    log(f'      [DB] implicit create_squad id='
                        f'{created_squad_id} leader_char={anchor_char["id"]} '
                        f'faction={anchor_char["faction"]} (on invite)')
                if not member_row:
                    if hasattr(STORE, 'add_squad_member_checked'):
                        member_row, add_result = \
                            await STORE.add_squad_member_checked(
                                squad['id'], friend_char['id'],
                                status=ST_INVITE_PENDING,
                                max_members=MAX_SQUAD_MEMBERS)
                        if add_result == 'already':
                            code = RC_PLAYER_ALREADY_IN_SQUAD
                        elif add_result == 'full':
                            code = RC_SQUAD_SLOT_NOT_FOUND
                    else:  # lightweight regression fakes
                        member_row = await STORE.add_squad_member(
                            squad['id'], friend_char['id'],
                            status=ST_INVITE_PENDING)
                if code != RC_OK:
                    member_row = 0
                    raise ValueError(f'invite rejected during atomic add: {code}')
                log(f'      [DB] squad_invite squad={squad["id"]} '
                    f'friend_char={friend_char["id"]} '
                    f'{"(selected)" if direct else "(first matching)"} '
                    f'row={member_row}')
        except Exception as e:
            log(f'      [DB] squad_invite failed: {e}')
            if code == RC_OK:
                code = RC_PLAYER_NOT_FOUND

    if code == RC_OK and member_row:
        if created_squad_id:
            # Only discard the reusable solo row after the atomic DB create
            # succeeded.  A transient DB failure must not destroy the user's
            # current solo state.
            solo = _SOLO_SQUADS.pop(int(pid), None)
            if solo is not None and sess.playerview_cmd is not None:
                await _push_kvcs_delete(
                    writer, sess.playerview_cmd,
                    b'SquadSession', solo['squad_row'])
                await _push_kvcs_delete(
                    writer, sess.playerview_cmd,
                    b'SquadMemberSession', solo['member_row'])
                log(f'S->C cmd={sess.playerview_cmd:<4} '
                    f'KeyValueChangeSet (solo squad delete '
                    f'id={solo["squad_row"]} ★after implicit create)')
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
        # 生涯军令 26 eInviteFriend（客户端无上报点，映射到小队邀请）
        # + 27 eCreateSquad（首次邀请隐式建队）+ 推行实时刷新
        from handlers.career import complete_order_and_push
        await complete_order_and_push(sess, writer, pid, 26)
        if created_squad_id:
            await complete_order_and_push(sess, writer, pid, 27)

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
            else:
                squad = await STORE.get_squad(int(mem['squad_id']))
                members = await STORE.list_squad_members(int(mem['squad_id']))
                target = next((m for m in members
                               if int(m['id']) == int(member_row)), None)
                if (squad is None or target is None
                        or int(target['player_id']) != int(pid)
                        or int(mem.get('status', 0) or 0) !=
                        ST_INVITE_PENDING):
                    code = RC_SQUAD_SLOT_NOT_FOUND
                elif has_accepted and (
                        _squad_is_locked(squad, members)
                        or _player_in_matchmaking(pid)):
                    code = RC_SQUAD_IN_LOCKED_STATE

            if code == RC_OK and has_accepted:
                # ★ 官服入队规则同 invite：接受时按小队（队长角色）约束
                #   复核入队角色 —— f3 选中角色匹配则直接用，否则用邀请时
                #   存入成员行的解析角色/首个匹配角色；无匹配 → 17 拒绝
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
                    if hasattr(STORE, 'accept_squad_invite'):
                        updated = bool(await STORE.accept_squad_invite(
                            member_row, int(pid), int(resolved['id'])))
                    else:
                        updated = await STORE.update_squad_member(
                            member_row,
                            character_id=resolved['id'],
                            status=ST_IN_LOBBY)
                    if not updated:
                        code = RC_SQUAD_SLOT_NOT_FOUND
                    else:
                        PLAYER_SELECTED_CHAR[int(pid)] = int(resolved['id'])
                        try:
                            await STORE.set_player_selected_char(
                                int(pid), int(resolved['id']))
                        except Exception as e:
                            log(f'      [DB] set_player_selected_char '
                                f'failed: {e}')
                        log(f'      [DB] squad_invite accepted row={member_row} '
                            f'char={resolved["id"]} '
                            f'(requested={char_id or mem["character_id"]})')
            elif code == RC_OK:
                if hasattr(STORE, 'decline_squad_invite'):
                    declined_squad_id = await STORE.decline_squad_invite(
                        member_row, int(pid))
                    if not declined_squad_id:
                        code = RC_SQUAD_SLOT_NOT_FOUND
                else:
                    declined_squad_id = int(mem['squad_id'])
                    if not await STORE.remove_squad_member(member_row):
                        code = RC_SQUAD_SLOT_NOT_FOUND
                if code == RC_OK:
                    log(f'      [DB] squad_invite declined row={member_row}')
        except Exception as e:
            log(f'      [DB] squad_respond failed: {e}')
            code = RC_SQUAD_SLOT_NOT_FOUND

    if code == RC_OK:
        if has_accepted:
            # ★清残留 solo 隐式小队：接受邀请入真小队后，旧 solo 行会留在
            #   客户端 SquadSession 表、_SOLO_SQUADS 残留条目会劫持后续
            #   解散请求（① 分支消费旧条目 → 真小队解散不生效）。先删
            #   solo 两行（SquadSession 先删，resetSquad 前置条件）再推
            #   真小队行 → 客户端干净切换到真小队视图。
            stale_solo = _SOLO_SQUADS.pop(int(pid), None)
            if stale_solo is not None and sess.playerview_cmd is not None:
                await _push_kvcs_delete(writer, sess.playerview_cmd,
                                        b'SquadSession',
                                        stale_solo['squad_row'])
                await _push_kvcs_delete(writer, sess.playerview_cmd,
                                        b'SquadMemberSession',
                                        stale_solo['member_row'])
                log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet '
                    f'(SquadSession delete id={stale_solo["squad_row"]} + '
                    f'SquadMemberSession delete '
                    f'id={stale_solo["member_row"]} ★stale solo on accept)')
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
            await push_squad_rows_to_all_members(declined_squad_id)

    resp = build_response(cmd_id, 'SquadRespondToInviteResponse',
                          pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SquadRespondToInviteResponse '
        f'(member={member_row}, accepted={has_accepted}, result={code})')


async def handle_set_squad_details(sess, writer, cmd_id, req_type, proto):
    """SetSquadDetailsRequest {characterId=1, squadName=2, pricingId=3, discountId=4,
    saveSessionState=5, paymentCurrency=6} → SetSquadDetailsResponse{response=1}。

    官方语义（客户端 SquadService.renameSquad 契约）：
    - 名字 trim 后 3..15（SQUAD_NAME_LENGTH）；空 → no_name(53)，
      非法 → invalid_name(36)
    - 仅队长（leader_character_id == 请求 characterId）可改名，否则 squad_not_owned(164)
    - 按 pricingId + paymentCurrency 收费（squad_rename 10315 = 50金/8400银），
      余额不足 → not_enough_credits(56)
    - 改名后 SquadSession 行同步给全体在线成员
    - 玩家无小队（未 CreateSquadRequest 建队/已解散）→ squad_not_found(161)"""
    char_id, squad_name, pricing_id, currency = 0, '', 0, 0
    try:
        f = pb_parse(proto)
        char_id = int(f.get(1, 0) or 0)
        squad_name = _str(f.get(2, '') or '')
        pricing_id = int(f.get(3, 0) or 0)
        currency = int(f.get(6, 0) or 0)
    except Exception as e:
        log(f'      [squad] SetSquadDetails parse error: {e}')

    squad_name = squad_name.strip()

    pid = await sess.ensure_player()
    code = RC_OK
    squad_id = 0
    if pid is None or STORE is None:
        code = RC_SQUAD_NOT_FOUND
    else:
        name_err = _official_name_error(squad_name, max_length=SQUAD_NAME_MAX_LENGTH)
        if name_err:
            code = name_err
        else:
            try:
                squad = await STORE.get_squad_for_player(int(pid))
                if squad is None and char_id:
                    squad = await STORE.get_squad_for_character(char_id)
                if squad is None:
                    code = RC_SQUAD_NOT_FOUND
                elif int(squad.get('leader_character_id', 0) or 0) != char_id:
                    code = RC_SQUAD_NOT_OWNED
                else:
                    members = (await STORE.list_squad_members(int(squad['id']))
                               if hasattr(STORE, 'list_squad_members') else [])
                    if members and not _player_is_leader(squad, members, pid):
                        code = RC_SQUAD_NOT_OWNED
                    elif _squad_is_locked(squad, members):
                        code = RC_SQUAD_IN_LOCKED_STATE
                    else:
                        cost, ok = await _charge_player(
                            sess, pricing_id, currency,
                            expected_types='squad_rename')
                        if not ok:
                            code = 56  # not_enough_credits
                        else:
                            squad_id = squad['id']
                            try:
                                await STORE.rename_squad(squad_id, squad_name)
                            except Exception:
                                from player_service import _refund_player_charge
                                await _refund_player_charge(
                                    sess, currency, cost)
                                raise
                            log(f'      [DB] rename_squad id={squad_id} '
                                f'name={squad_name!r} cost={cost}')
            except Exception as e:
                log(f'      [DB] set_squad_details failed: {e}')
                code = RC_SQUAD_NOT_FOUND

    if code == RC_OK and squad_id:
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

    pid = await sess.ensure_player()
    code = RC_OK
    # 内存隐式小队（solo / 匹配凑队）：squadSessionId 是内存行 id（超
    # int32），走 DB set_squad_flag 必炸（"value out of int32 range"）
    # 且回 161 会触发客户端 sendFeedbackMessage 错误弹窗（2026-09-02
    # 实测每次凑队成局客户端连发两条 161）。战争模式标记对隐式小队
    # 无持久化意义 → 静默 OK。
    memory_owner_ok = False
    in_memory = False
    for owner_pid, solo in _SOLO_SQUADS.items():
        if int(solo.get('squad_row', 0) or 0) == squad_session_id:
            in_memory = True
            memory_owner_ok = int(owner_pid) == int(pid or 0)
            break
    if not in_memory:
        for _mid in list(_MM_MISSION_SQUADS.keys()):
            info = _MM_MISSION_SQUADS.get(_mid, {}).get(squad_session_id)
            if info is not None:
                in_memory = True
                leader = _auto_squad_leader(info)
                memory_owner_ok = (leader is not None and int(
                    leader.get('player_id', 0) or 0) == int(pid or 0))
                break
    if in_memory:
        if not memory_owner_ok:
            code = RC_PLAYER_NOT_OWNER
        else:
            log(f'      [squad] SetSquadMatchMakingFlag: in-memory squad '
                f'{squad_session_id} → no-op OK')
    elif pid is None or STORE is None or not squad_session_id:
        code = RC_SQUAD_NOT_FOUND
    else:
        try:
            squad = await STORE.get_squad(squad_session_id)
            members = await STORE.list_squad_members(squad_session_id) \
                if squad is not None else []
            if squad is None:
                code = RC_SQUAD_NOT_FOUND
            elif not _player_is_leader(squad, members, pid):
                code = RC_SQUAD_NOT_OWNED
            else:
                updated = await STORE.set_squad_flag(squad_session_id, flag)
                if not updated:
                    code = RC_SQUAD_NOT_FOUND
                else:
                    log(f'      [DB] set_squad_flag id={squad_session_id} '
                        f'flag={flag}')
        except Exception as e:
            log(f'      [DB] set_squad_flag failed: {e}')
            code = RC_SQUAD_NOT_FOUND

    if code == RC_OK and not in_memory:
        # Only persisted squads have DB rows to synchronize. Solo/auto squads
        # have already been acknowledged above; querying their 63-bit row IDs
        # here overflows the int32 squads primary key.
        # ★绝不回推整队行集（2026-09-09 闪烁风暴根因）：客户端每条
        #   SquadMemberSession SET → onSquadMemberSessionChange:586 →
        #   evaluatePlayerFactions:1061 → squadMatchMakingFilterUpdateSignal
        #   → 队长（isOwnedByPlayer 且队内 >1 人）自动重发本请求
        #   （SquadMatchMakingFilterUpdateCommand:42-47）。全量回推 =
        #   每个请求给队长喂 2 遍整队行集（含全部成员行 SET）→ 死循环
        #   （VPS 实测 12:20-12:25 一小时档 4100 请求，队员 UI 每秒重绘
        #   "一直在闪"；解散后循环仍在途请求全部 161）。官服语义：写操作
        #   只回 Response，行集推送只给其他成员且只推值变化的行——这里
        #   仅 SquadSession（f12 flag）变化 → 只推 SquadSession 单行、
        #   不含成员行（SquadSession SET 不进 evaluatePlayerFactions 链，
        #   且 Command 有 isOwnedByPlayer 条件，非队长不会回发请求）。
        #   发起者本地状态在发请求前已是目标值，无需推。
        if STORE is not None and squad_session_id:
            try:
                squad = await STORE.get_squad(squad_session_id)
                members = await STORE.list_squad_members(squad_session_id) \
                    if squad is not None else []
            except Exception as e:
                log(f'      [DB] set flag sync push failed: {e}')
                squad, members = None, []
            if squad is not None:
                for s in _online_member_sessions(members):
                    if s is sess or s.playerview_cmd is None:
                        continue          # 发起者不推
                    viewer_faction = None
                    for m in members:
                        if int(m['player_id']) == int(
                                getattr(s, 'player_id', 0) or 0):
                            viewer_faction = int(m['faction'])
                            break
                    try:
                        await _push_kvcs(
                            s.writer, s.playerview_cmd, b'SquadSession',
                            _msg_squad_session(squad, members,
                                               mission_id=_squad_view_mission_id(
                                                   members, getattr(s, 'player_id', 0)),
                                               viewer_faction=viewer_faction))
                        log(f'S->C cmd={s.playerview_cmd:<4} KeyValueChangeSet '
                            f'(SquadSession id={squad_session_id} flag sync '
                            f'★single row, no member rows)')
                    except Exception as e:
                        log(f'      [squad] flag sync push to '
                            f'{s.username!r} failed: {e}')

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

    pid = await sess.ensure_player()
    code = RC_OK
    mem = squad = me = None
    members = []
    if pid is None or STORE is None or not slot_id:
        code = RC_SQUAD_SLOT_NOT_FOUND
    elif status not in (0, ST_IN_LOBBY, ST_READY):
        # pending / in-combat are server-owned transitions.
        code = RC_SQUAD_SLOT_NOT_FOUND
    else:
        try:
            mem = await STORE.get_squad_member(slot_id)
            if mem is None:
                code = RC_SQUAD_SLOT_NOT_FOUND
            else:
                squad = await STORE.get_squad(int(mem['squad_id']))
                members = await STORE.list_squad_members(int(mem['squad_id']))
                me = next((m for m in members
                           if int(m['id']) == int(slot_id)), None)
                if (squad is None or me is None
                        or int(me['player_id']) != int(pid)):
                    code = RC_PLAYER_NOT_OWNER
                elif int(me.get('status', 0) or 0) == ST_INVITE_PENDING:
                    # Only SquadRespondToInvite may establish membership.
                    code = RC_SQUAD_SLOT_NOT_FOUND
                elif _squad_is_locked(squad, members):
                    code = RC_SQUAD_IN_LOCKED_STATE

            if code == RC_OK and member_char_id:
                leader = _leader_member(squad, members)
                spec = ((int(leader['faction']),
                         squad_template_for_character(leader['template']))
                        if leader is not None else None)
                own_char = await STORE.get_character(int(pid), member_char_id)
                if own_char is None:
                    code = RC_CHARACTER_NOT_OWNED
                elif leader is not None and int(me['id']) == int(leader['id']):
                    await _leader_switch_spec(
                        squad, members, slot_id, own_char, status)
                else:
                    resolved, _direct = (None, False)
                    if spec is not None:
                        resolved, _direct = await _resolve_join_char(
                            int(pid), spec[0], spec[1],
                            selected_id=member_char_id)
                    if resolved is None or int(resolved['id']) != member_char_id:
                        code = RC_CHARACTER_NOT_OWNED
                    else:
                        updated = await STORE.update_squad_member(
                            slot_id, character_id=member_char_id,
                            status=status if status else None)
                        if not updated:
                            code = RC_SQUAD_SLOT_NOT_FOUND
            elif code == RC_OK:
                if not status:
                    code = RC_SQUAD_SLOT_NOT_FOUND
                else:
                    updated = await STORE.update_squad_member(
                        slot_id, status=status)
                    if not updated:
                        code = RC_SQUAD_SLOT_NOT_FOUND
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

    pid = await sess.ensure_player()
    code = RC_OK
    if pid is None or STORE is None or not squad_session_id:
        code = RC_SQUAD_NOT_FOUND
    else:
        try:
            squad = await STORE.get_squad(squad_session_id)
            if squad is None:
                code = RC_SQUAD_NOT_FOUND
            else:
                members = await STORE.list_squad_members(squad_session_id)
                if not _player_is_leader(squad, members, pid):
                    code = RC_SQUAD_NOT_OWNED
                elif _squad_is_locked(squad, members):
                    code = RC_SQUAD_IN_LOCKED_STATE
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
}
