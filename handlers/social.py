# -*- coding: utf-8 -*-
"""社交域（social）：好友 / 屏蔽 / 玩家搜索 / 举报 / 聊天频道 / 邮件 / 通知。

消息 schema（decompiled/proto-files/HnG_States，注意 response 字段号不统一）：
- AddToFriendsListRequest {playerId=2} → AddToFriendsListResponse {response=2}
- RemoveFromFriendsListRequest {playerId=2, addToIgnoreList=3}
  → RemoveFromFriendsListResponse {response=2}
- SearchPlayersRequest {searchKeyword=2, searchByEmail=3}
  → SearchPlayersResponse {response=2, gamerTagsFound=3 repeated}
- SearchPlayerDetailRequest {playerGamerTag=1}
  → SearchPlayerDetailResponse {response=1, playerId=2, gamertag=3,
    factiontemplateid=4, warName=5}
- QueryGamertagRequest {playerId=1} → QueryGamertagResponse {gamertag=1}
- IgnorePlayerChangeRequest {ignoredPlayerGamerTag=1, ignoreAction=2(0加1删)}
  → IgnorePlayerChangeResponse {response=1}（Ok=0 AlreadyIgnored=1
    NotCurrentlyIgnored=2 Fail=3）
- AddPlayerReportRequest {reportedplayer=5, category=6, actiongamesession=7,
  chatlogs=8, reporter=9} → AddPlayerReportResponse {response=1}
- PlayerVerificationStatusRequest {dummy=1}
  → PlayerVerificationStatusResponse {msg=1}
- GetChatChannelsSubscribedRequest {dummy=1}
  → GetChatChannelsSubscribedResponse {channelsSubscribed=1
    repeated ChatChannelSubscribed{id=1, channelName=2, displayName=3, channelTab=4}}
- add_chatchannel_request {createNewChannel=1(u32), channelId=2, channelName=3,
  channelTab=4} → add_chatchannel_response {response=1}
- create_chatchannel_request {channelName=1, hidden=2, passwordRequired=3,
  password=4, invitationOnly=5, channelTab=6} → create_chatchannel_response {response=1}
- remove_chatchannel_request {channelId=1} → remove_chatchannel_response {response=1}
- EditChatChannelRequest {channelId=1, password=2, isPasswordRequired=3,
  newPassword=4, newChannelName=5, isInvitationOnly=6, isHidden=7}
  → EditChatChannelResponse {response=1}
- GetMailMessagesRequest {messageIds=1 repeated}
  → GetMailMessagesResponse {messageThreads=1 repeated MessageThread{
    threadId=1, messages=2 repeated MailMessage{id=1, threadMessageId=2,
    replyToMessageId=3, fromPlayerId=4, subject=5, text=6, timestamp=7,
    expiration=8}, recipients=3 repeated MessageThreadRecipient{
    playerId=1, gamertag=2}}}
- SendMailMessageRequest {replyToMessageId=1, subject=2, text=3,
  recipientGamertags=4 repeated} → SendMailMessageResponse {response=1}
- SetMessageRecipientsReadRequest {messageRecipientIds=1 repeated, isRead=2}
  → SetMessageRecipientsReadResponse {response=1}
- SetMessageRecipientsDeletedRequest {messageRecipientIds=1 repeated, isDeleted=2}
  → SetMessageRecipientsDeletedResponse {response=1}
- SetNotificationsReadRequest {notificationIds=1 repeated, readStatus=2}
  → SetNotificationsReadResponse {response=1}

客户端 UI 流程（U0 反编译 swf-ui-logic）：
- FriendService.addFriend：先 SearchPlayerDetailRequest 查 id（response==ok
  才继续），再 AddToFriendsListRequest（ok=请求已发出 / invite_accepted=254
  直接成为好友 / invite_pending=253）。
- SearchPlayersResponse：response==players_found(67) 才展开 gamerTagsFound，
  否则按 players_not_found(68) 显示错误。
- 好友列表 UI 数据源是 KVCS 表 friendinfo（FriendsListTable.xmlTag）与
  ignoredplayerwithname（IgnoredPlayerTable.xmlTag），订阅 playerview 时推送，
  增删好友后重推刷新。
- ChatMediator：GetChatChannelsSubscribedResponse 空列表 → 隐藏频道 UI（安全）。
- ResponseCodes：ok=1 player_not_found=64 players_found=67 players_not_found=68
  already_friend=3 invite_pending=253 invite_accepted=254。
"""

import time

from protocol import (
    build_response,
    pb_bool,
    pb_int64,
    pb_int32,
    pb_message,
    pb_parse,
    pb_parse_multi,
    pb_string,
    pb_uint32,
    )
from state import FLASH_SESSIONS, STORE, log

from messages import _push_kvcs
from matchmaking import FACTION_ROW_IDS

# ResponseCodes（HnG_States.ResponseCodes 枚举）
RC_OK = 1
RC_ALREADY_FRIEND = 3
RC_PLAYER_NOT_FOUND = 64
RC_PLAYERS_FOUND = 67
RC_PLAYERS_NOT_FOUND = 68
RC_INVITE_PENDING = 253
RC_INVITE_ACCEPTED = 254

# IgnorePlayerChangeResponseCode
IGN_OK = 0
IGN_ALREADY = 1
IGN_NOT_CURRENTLY = 2
IGN_FAIL = 3

_WAR_NAME = 'Demo War'


def _str(v):
    if isinstance(v, bytes):
        return v.decode('utf-8', 'replace')
    return str(v)


# ------------------------------------------------------------- KVCS 推送

async def push_friend_rows(sess, writer, pid):
    """推送好友列表 KVCS（friendinfo 表，FriendsListTable.xmlTag）。

    otherPlayerAlsoFriend=true → 正式好友；false → 待确认（pendingList）。
    onlinestatus：0 离线 1 在线（按 FLASH_SESSIONS 注册表实时判断）。"""
    if STORE is None or sess.playerview_cmd is None:
        return
    try:
        rows = await STORE.list_friend_rows(pid)
    except Exception as e:
        log(f'      [DB] list_friend_rows failed: {e}')
        return
    for r in rows:
        online = FLASH_SESSIONS.get(r['gamertag']) is not None
        m = pb_int64(1, r['id'])                     # id
        m += pb_string(2, r['gamertag'])             # gamertag
        m += pb_uint32(3, 1 if online else 0)        # onlinestatus
        m += pb_int64(4, r['faction'])               # factionTemplateId
        m += pb_int64(5, FACTION_ROW_IDS.get(r['faction'], 0))  # factionId（行 id）
        m += pb_int64(6, 0)                          # missionId
        m += pb_int64(7, 0)                          # missionFactionId
        # warId 必须与 war catalogue / player.war 的 id=1 一致：
        # 客户端邀请好友（小队/战斗）前本地校验 friend.warId == currentWar.id，
        # 不一致直接提示"不在同一服务器"（此前用 matchmaking._WAR_ID 随机大数）
        m += pb_int64(8, 1)                          # warId
        m += pb_string(9, _WAR_NAME)                 # warName
        m += pb_bool(10, r['also_friend'])           # otherPlayerAlsoFriend
        m += pb_bool(11, r['initiated_by_me'])       # friendshipInitiatedByMe
        m += pb_int64(12, 0)                         # squadSessionId
        m += pb_int64(13, 0)                         # mapEntityId
        m += pb_int64(14, 0)                         # mapEntityTypeId
        await _push_kvcs(writer, sess.playerview_cmd, b'friendinfo', m)
    if rows:
        log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet '
            f'(friendinfo x{len(rows)}, player={pid})')


async def push_rows_to_player_if_online(pid):
    """好友数据变化时实时通知该玩家（在 Flash 会话在线才推；离线下次登录
    订阅 playerview 时自然拉到最新列表，无需重登的另一半场景就靠这里）。"""
    if STORE is None:
        return
    try:
        name = await STORE.get_username(int(pid))
    except Exception as e:
        log(f'      [DB] get_username failed: {e}')
        return
    other = FLASH_SESSIONS.get(name or '')
    if other is not None and other.playerview_cmd is not None:
        await push_friend_rows(other, other.writer, int(pid))


async def notify_friends_of(pid):
    """我的上/下线通知：向所有在线好友重推其好友列表，刷新我的 onlinestatus。

    上线：在 FLASH_SESSIONS 注册之后调用（此时我已算在线）；
    下线：注销之后调用（此时我已算离线）。"""
    if STORE is None:
        return
    try:
        rows = await STORE.list_friend_rows(int(pid))
    except Exception as e:
        log(f'      [DB] list_friend_rows failed: {e}')
        return
    for r in rows:
        other = FLASH_SESSIONS.get(r['gamertag'])
        if other is None or other.playerview_cmd is None:
            continue
        try:
            await push_friend_rows(other, other.writer, r['id'])
        except Exception as e:
            log(f'      [social] notify friend {r["id"]} failed: {e}')


async def push_ignored_rows(sess, writer, pid):
    """推送屏蔽名单 KVCS（ignoredplayerwithname 表，IgnoredPlayerTable.xmlTag）。"""
    if STORE is None or sess.playerview_cmd is None:
        return
    try:
        rows = await STORE.list_ignored_rows(pid)
    except Exception as e:
        log(f'      [DB] list_ignored_rows failed: {e}')
        return
    for i, r in enumerate(rows, 1):
        m = pb_int64(1, i)                           # id（名单序号）
        m += pb_int64(2, pid)                        # ignorerid
        m += pb_int64(3, r['ignored_id'])            # ignoredid
        m += pb_string(4, r['ignoredname'])          # ignoredname
        await _push_kvcs(writer, sess.playerview_cmd,
                         b'ignoredplayerwithname', m)
    if rows:
        log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet '
            f'(ignoredplayerwithname x{len(rows)}, player={pid})')


# ------------------------------------------------------------- 好友

async def handle_search_players(sess, writer, cmd_id, req_type, proto):
    """SearchPlayersRequest {searchKeyword=2} → SearchPlayersResponse{response=2}。"""
    keyword = ''
    try:
        f = pb_parse(proto)
        keyword = _str(f.get(2, '') or '')
    except Exception as e:
        log(f'      [social] SearchPlayers parse error: {e}')

    tags = []
    if keyword:
        pid = await sess.ensure_player()
        if pid is not None and STORE is not None:
            try:
                tags = await STORE.search_players(keyword, exclude_id=pid)
            except Exception as e:
                log(f'      [DB] search_players failed: {e}')

    resp_proto = pb_int32(2, RC_PLAYERS_FOUND if tags else RC_PLAYERS_NOT_FOUND)
    for t in tags:
        resp_proto += pb_string(3, t)

    resp = build_response(cmd_id, 'SearchPlayersResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SearchPlayersResponse '
        f'(keyword={keyword!r}, found={len(tags)})')


async def handle_search_player_detail(sess, writer, cmd_id, req_type, proto):
    """SearchPlayerDetailRequest {playerGamerTag=1} → SearchPlayerDetailResponse。

    FriendService.addFriend 第一步：response==ok 才会继续发 AddToFriendsList。"""
    gamertag = ''
    try:
        f = pb_parse(proto)
        gamertag = _str(f.get(1, '') or '')
    except Exception as e:
        log(f'      [social] SearchPlayerDetail parse error: {e}')

    target, faction = None, 0
    if gamertag and STORE is not None:
        try:
            target = await STORE.get_player_by_gamertag(gamertag)
            if target is not None:
                faction = await STORE.get_player_faction(target['id'])
        except Exception as e:
            log(f'      [DB] get_player_by_gamertag failed: {e}')

    if target is not None:
        resp_proto = pb_int32(1, RC_OK)
        resp_proto += pb_int64(2, target['id'])
        resp_proto += pb_string(3, target['username'])
        resp_proto += pb_int64(4, faction)
        resp_proto += pb_string(5, _WAR_NAME)
    else:
        # 未找到：response=64；required 字段仍需给出（客户端先判 response）
        resp_proto = pb_int32(1, RC_PLAYER_NOT_FOUND)
        resp_proto += pb_int64(2, 0)
        resp_proto += pb_string(3, '')
        resp_proto += pb_int64(4, 0)
        resp_proto += pb_string(5, '')

    resp = build_response(cmd_id, 'SearchPlayerDetailResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SearchPlayerDetailResponse '
        f'(gamertag={gamertag!r}, '
        f'{"found id=" + str(target["id"]) if target else "not found"})')


async def handle_add_to_friends_list(sess, writer, cmd_id, req_type, proto):
    """AddToFriendsListRequest {playerId=2} → AddToFriendsListResponse{response=2}。"""
    friend_id = 0
    try:
        f = pb_parse(proto)
        friend_id = int(f.get(2, 0) or 0)
    except Exception as e:
        log(f'      [social] AddToFriendsList parse error: {e}')

    pid = await sess.ensure_player()
    code = RC_OK
    if pid is not None and STORE is not None and friend_id:
        try:
            result = await STORE.add_friend(int(pid), friend_id)
            code = {'ok': RC_OK, 'accepted': RC_INVITE_ACCEPTED,
                    'already': RC_ALREADY_FRIEND,
                    'self': RC_ALREADY_FRIEND}[result]
            log(f'      [DB] add_friend {pid}->{friend_id}: {result}')
            # 实时通知对方：'ok'=对方收到待确认请求 / 'accepted'=对方转正好友，
            # 不推则对方要重登才能看到变化
            if result in ('ok', 'accepted'):
                await push_rows_to_player_if_online(friend_id)
            await push_friend_rows(sess, writer, int(pid))
        except Exception as e:
            log(f'      [DB] add_friend failed: {e}')

    resp = build_response(cmd_id, 'AddToFriendsListResponse', pb_int32(2, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} AddToFriendsListResponse (response={code})')


async def handle_remove_from_friends_list(sess, writer, cmd_id, req_type, proto):
    """RemoveFromFriendsListRequest {playerId=2, addToIgnoreList=3}
    → RemoveFromFriendsListResponse{response=2}。"""
    friend_id, add_ignore = 0, False
    try:
        f = pb_parse(proto)
        friend_id = int(f.get(2, 0) or 0)
        add_ignore = bool(f.get(3, 0))
    except Exception as e:
        log(f'      [social] RemoveFromFriendsList parse error: {e}')

    pid = await sess.ensure_player()
    code = RC_OK
    if pid is not None and STORE is not None and friend_id:
        try:
            await STORE.remove_friend(int(pid), friend_id)
            if add_ignore:
                # 顺带屏蔽：好友 id 反查 gamertag 再入屏蔽名单
                async with STORE.pool.acquire() as conn:
                    tag = await conn.fetchval(
                        'SELECT username FROM players WHERE id = $1', friend_id)
                if tag:
                    await STORE.ignore_player(int(pid), tag)
                    await push_ignored_rows(sess, writer, int(pid))
            log(f'      [DB] remove_friend {pid}-x-{friend_id} '
                f'(also_ignore={add_ignore})')
            # 对方也要实时掉好友行（双向删除），否则其列表残留到我方重登
            await push_rows_to_player_if_online(friend_id)
            await push_friend_rows(sess, writer, int(pid))
        except Exception as e:
            log(f'      [DB] remove_friend failed: {e}')

    resp = build_response(cmd_id, 'RemoveFromFriendsListResponse',
                          pb_int32(2, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} RemoveFromFriendsListResponse (response={code})')


async def handle_query_gamertag(sess, writer, cmd_id, req_type, proto):
    """QueryGamertagRequest {playerId=1} → QueryGamertagResponse{gamertag=1}。"""
    target_id = 0
    try:
        f = pb_parse(proto)
        target_id = int(f.get(1, 0) or 0)
    except Exception as e:
        log(f'      [social] QueryGamertag parse error: {e}')

    gamertag = ''
    if target_id and STORE is not None:
        try:
            async with STORE.pool.acquire() as conn:
                val = await conn.fetchval(
                    'SELECT username FROM players WHERE id = $1', target_id)
            gamertag = val or ''
        except Exception as e:
            log(f'      [DB] query gamertag failed: {e}')

    resp = build_response(cmd_id, 'QueryGamertagResponse', pb_string(1, gamertag))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} QueryGamertagResponse '
        f'(playerId={target_id}, gamertag={gamertag!r})')


async def handle_ignore_player_change(sess, writer, cmd_id, req_type, proto):
    """IgnorePlayerChangeRequest {ignoredPlayerGamerTag=1, ignoreAction=2}
    → IgnorePlayerChangeResponse{response=1}。"""
    gamertag, action = '', 0
    try:
        f = pb_parse(proto)
        gamertag = _str(f.get(1, '') or '')
        action = int(f.get(2, 0) or 0)
    except Exception as e:
        log(f'      [social] IgnorePlayerChange parse error: {e}')

    pid = await sess.ensure_player()
    code = IGN_FAIL
    if pid is not None and STORE is not None and gamertag:
        try:
            if action == 0:                        # IgnorePlayerAction.Add
                code = await STORE.ignore_player(int(pid), gamertag)
            else:                                  # Remove
                code = await STORE.unignore_player(int(pid), gamertag)
            await push_ignored_rows(sess, writer, int(pid))
        except Exception as e:
            log(f'      [DB] ignore/unignore failed: {e}')
            code = IGN_FAIL

    resp = build_response(cmd_id, 'IgnorePlayerChangeResponse',
                          pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} IgnorePlayerChangeResponse '
        f'(action={action}, gamertag={gamertag!r}, code={code})')


# ------------------------------------------------------------- 举报 / 校验

async def handle_add_player_report(sess, writer, cmd_id, req_type, proto):
    """AddPlayerReportRequest → AddPlayerReportResponse{response=1}（落库）。"""
    reported, category, session_id, chatlogs, reporter = 0, 0, 0, '', 0
    try:
        f = pb_parse(proto)
        reported = int(f.get(5, 0) or 0)
        category = int(f.get(6, 0) or 0)
        session_id = int(f.get(7, 0) or 0)
        chatlogs = _str(f.get(8, '') or '')
        reporter = int(f.get(9, 0) or 0)
    except Exception as e:
        log(f'      [social] AddPlayerReport parse error: {e}')

    pid = await sess.ensure_player()
    if pid is not None and STORE is not None and reported:
        try:
            await STORE.save_player_report(reporter or int(pid), reported,
                                           category, session_id, chatlogs)
        except Exception as e:
            log(f'      [DB] save_player_report failed: {e}')

    resp = build_response(cmd_id, 'AddPlayerReportResponse', pb_int32(1, RC_OK))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} AddPlayerReportResponse '
        f'(reported={reported}, category={category})')


async def handle_player_verification_status(sess, writer, cmd_id, req_type, proto):
    """PlayerVerificationStatusRequest → PlayerVerificationStatusResponse{msg=1}。"""
    resp = build_response(cmd_id, 'PlayerVerificationStatusResponse',
                          pb_int32(1, RC_OK))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} PlayerVerificationStatusResponse (msg=1)')


# ------------------------------------------------------------- 聊天频道

async def handle_get_chat_channels_subscribed(sess, writer, cmd_id, req_type,
                                              proto):
    """GetChatChannelsSubscribedRequest → GetChatChannelsSubscribedResponse。

    ChatMediator：空列表 → 隐藏频道 UI（安全省略 repeated）。"""
    channels = []
    pid = await sess.ensure_player()
    if pid is not None and STORE is not None:
        try:
            channels = await STORE.list_subscribed_channels(int(pid))
        except Exception as e:
            log(f'      [DB] list_subscribed_channels failed: {e}')

    resp_proto = b''
    for c in channels:
        m = pb_int64(1, c['id'])
        m += pb_string(2, c['name'])
        m += pb_string(3, c['display'])
        m += pb_string(4, c['tab'])
        resp_proto += pb_message(1, m)

    resp = build_response(cmd_id, 'GetChatChannelsSubscribedResponse',
                          resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} GetChatChannelsSubscribedResponse '
        f'({len(channels)} channels)')


async def handle_add_chatchannel(sess, writer, cmd_id, req_type, proto):
    """add_chatchannel_request {createNewChannel=1, channelId=2, channelName=3,
    channelTab=4} → add_chatchannel_response{response=1}。

    createNewChannel=1 → 以 channelName 建新频道；=0 → 加入 channelId。"""
    create_new, channel_id, channel_name, tab = 0, 0, '', ''
    try:
        f = pb_parse(proto)
        create_new = int(f.get(1, 0) or 0)
        channel_id = int(f.get(2, 0) or 0)
        channel_name = _str(f.get(3, '') or '')
        tab = _str(f.get(4, '') or '')
    except Exception as e:
        log(f'      [social] add_chatchannel parse error: {e}')

    pid = await sess.ensure_player()
    code = RC_OK
    if pid is not None and STORE is not None:
        try:
            if create_new:
                cid, ok = await STORE.create_channel(
                    channel_name or 'channel', int(pid),
                    tab=tab or 'general')
                code = RC_OK if ok else 10       # channel_creation_failed
            else:
                ok, err = await STORE.join_channel(int(pid),
                                                   channel_id=channel_id,
                                                   channel_name=channel_name)
                code = err if not ok else RC_OK   # 9 already / 12 not found
            log(f'      [DB] add_chatchannel: createNew={create_new} '
                f'id={channel_id} name={channel_name!r} code={code}')
        except Exception as e:
            log(f'      [DB] add_chatchannel failed: {e}')
            code = 11                             # channel_membership_failed

    resp = build_response(cmd_id, 'add_chatchannel_response',
                          pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} add_chatchannel_response (response={code})')


async def handle_create_chatchannel(sess, writer, cmd_id, req_type, proto):
    """create_chatchannel_request → create_chatchannel_response{response=1}。"""
    name, hidden, pw_req, password, inv_only, tab = '', False, False, '', False, ''
    try:
        f = pb_parse(proto)
        name = _str(f.get(1, '') or '')
        hidden = bool(f.get(2, 0))
        pw_req = bool(f.get(3, 0))
        password = _str(f.get(4, '') or '')
        inv_only = bool(f.get(5, 0))
        tab = _str(f.get(6, '') or '')
    except Exception as e:
        log(f'      [social] create_chatchannel parse error: {e}')

    pid = await sess.ensure_player()
    code = RC_OK
    if pid is not None and STORE is not None and name:
        try:
            cid, ok = await STORE.create_channel(
                name, int(pid), tab=tab or 'general', password=password,
                hidden=hidden, invitation_only=inv_only)
            code = RC_OK if ok else 10           # channel_creation_failed
            log(f'      [DB] create_chatchannel {name!r}: id={cid} ok={ok}')
        except Exception as e:
            log(f'      [DB] create_chatchannel failed: {e}')
            code = 10
    elif not name:
        code = 36                                  # invalid_name

    resp = build_response(cmd_id, 'create_chatchannel_response',
                          pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} create_chatchannel_response (response={code})')


async def handle_remove_chatchannel(sess, writer, cmd_id, req_type, proto):
    """remove_chatchannel_request {channelId=1} → remove_chatchannel_response。"""
    channel_id = 0
    try:
        f = pb_parse(proto)
        channel_id = int(f.get(1, 0) or 0)
    except Exception as e:
        log(f'      [social] remove_chatchannel parse error: {e}')

    pid = await sess.ensure_player()
    code = RC_OK
    if pid is not None and STORE is not None and channel_id:
        try:
            left = await STORE.leave_channel(int(pid), channel_id)
            code = RC_OK if left else 59          # not_subscribed_to_channel
        except Exception as e:
            log(f'      [DB] leave_channel failed: {e}')
            code = 11

    resp = build_response(cmd_id, 'remove_chatchannel_response',
                          pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} remove_chatchannel_response '
        f'(channel={channel_id}, response={code})')


async def handle_edit_chatchannel(sess, writer, cmd_id, req_type, proto):
    """EditChatChannelRequest {channelId=1, password=2, isPasswordRequired=3,
    newPassword=4, newChannelName=5, isInvitationOnly=6, isHidden=7}
    → EditChatChannelResponse{response=1}。"""
    channel_id = 0
    new_name = password = ''
    invitation_only = hidden = None
    try:
        f = pb_parse(proto)
        channel_id = int(f.get(1, 0) or 0)
        if 5 in f:
            new_name = _str(f.get(5, ''))
        if 4 in f:
            password = _str(f.get(4, ''))
        if 6 in f:
            invitation_only = bool(f.get(6, 0))
        if 7 in f:
            hidden = bool(f.get(7, 0))
    except Exception as e:
        log(f'      [social] EditChatChannel parse error: {e}')

    pid = await sess.ensure_player()
    code = RC_OK
    if pid is not None and STORE is not None and channel_id:
        try:
            updated = await STORE.update_channel(
                channel_id, owner_id=int(pid), new_name=new_name or None,
                password=password or None,
                invitation_only=invitation_only, hidden=hidden)
            code = RC_OK if updated else 12        # channel_not_found
        except Exception as e:
            log(f'      [DB] update_channel failed: {e}')
            code = 10

    resp = build_response(cmd_id, 'EditChatChannelResponse', pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} EditChatChannelResponse '
        f'(channel={channel_id}, response={code})')


# ------------------------------------------------------------- 邮件

def _zulu(dt):
    """datetime → 客户端 Zulu 时间串（YYYYMMDDHHmmssZ）。"""
    if dt is None:
        return ''
    return time.strftime('%Y%m%d%H%M%S', dt.utctimetuple()) + 'Z'


def _msg_mail_message(msg):
    """MailMessage proto。"""
    m = pb_int64(1, msg['id'])
    m += pb_int64(2, msg['thread_id'])             # threadMessageId（线程根）
    m += pb_int64(3, msg['reply_to'])              # replyToMessageId
    m += pb_int64(4, msg['sender'])                # fromPlayerId（0=系统）
    m += pb_string(5, msg['subject'])
    m += pb_string(6, msg['body'])
    m += pb_string(7, _zulu(msg['created_at']))    # timestamp
    m += pb_string(8, '20990101000000Z')           # expiration（长期有效）
    return m


async def handle_get_mail_messages(sess, writer, cmd_id, req_type, proto):
    """GetMailMessagesRequest {messageIds=1 repeated}
    → GetMailMessagesResponse{messageThreads=1}。

    空 messageIds → 拉最近收件箱；repeated 空必须整体省略。"""
    message_ids = []
    try:
        fields = pb_parse_multi(proto)
        message_ids = [int(v) for v in fields.get(1, []) if v]
    except Exception as e:
        log(f'      [social] GetMailMessages parse error: {e}')

    threads = []
    pid = await sess.ensure_player()
    if pid is not None and STORE is not None:
        try:
            threads = await STORE.get_mail_threads(int(pid), message_ids or None)
        except Exception as e:
            log(f'      [DB] get_mail_threads failed: {e}')

    resp_proto = b''
    for t in threads:
        tm = pb_int64(1, t['thread_id'])
        for msg in t['messages']:
            tm += pb_message(2, _msg_mail_message(msg))
        rm = pb_int64(1, int(pid)) + pb_string(2, sess.username or '')
        tm += pb_message(3, rm)
        resp_proto += pb_message(1, tm)

    resp = build_response(cmd_id, 'GetMailMessagesResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} GetMailMessagesResponse '
        f'(requested={len(message_ids)}, threads={len(threads)})')


async def handle_send_mail_message(sess, writer, cmd_id, req_type, proto):
    """SendMailMessageRequest {replyToMessageId=1, subject=2, text=3,
    recipientGamertags=4 repeated} → SendMailMessageResponse{response=1}。"""
    reply_to, subject, text, tags = 0, '', '', []
    try:
        fields = pb_parse_multi(proto)
        reply_to = int(fields.get(1, [0])[0] or 0) if fields.get(1) else 0
        subject = _str(fields.get(2, [''])[0]) if fields.get(2) else ''
        text = _str(fields.get(3, [''])[0]) if fields.get(3) else ''
        tags = [_str(v) for v in fields.get(4, []) if v]
    except Exception as e:
        log(f'      [social] SendMailMessage parse error: {e}')

    pid = await sess.ensure_player()
    sent = 0
    if pid is not None and STORE is not None and tags:
        try:
            rids = []
            for tag in tags:
                target = await STORE.get_player_by_gamertag(tag)
                if target is not None:
                    rids.append(target['id'])
            if rids:
                ids = await STORE.send_mail(int(pid), rids, subject, text,
                                            reply_to)
                sent = len(ids)
        except Exception as e:
            log(f'      [DB] send_mail failed: {e}')

    code = RC_OK if sent else 64                   # player_not_found
    resp = build_response(cmd_id, 'SendMailMessageResponse', pb_int32(1, code))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SendMailMessageResponse '
        f'(recipients={len(tags)}, sent={sent}, response={code})')


async def handle_set_message_recipients_read(sess, writer, cmd_id, req_type,
                                             proto):
    """SetMessageRecipientsReadRequest {messageRecipientIds=1, isRead=2}
    → SetMessageRecipientsReadResponse{response=1}。"""
    ids, is_read = [], True
    try:
        fields = pb_parse_multi(proto)
        ids = [int(v) for v in fields.get(1, []) if v]
        is_read = bool(fields.get(2, [1])[0]) if fields.get(2) else True
    except Exception as e:
        log(f'      [social] SetMessageRecipientsRead parse error: {e}')

    pid = await sess.ensure_player()
    if pid is not None and STORE is not None and ids:
        try:
            n = await STORE.set_mail_flag(int(pid), ids, 'is_read', is_read)
            log(f'      [DB] set_mail_read: {n} rows')
        except Exception as e:
            log(f'      [DB] set_mail_read failed: {e}')

    resp = build_response(cmd_id, 'SetMessageRecipientsReadResponse',
                          pb_int32(1, RC_OK))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetMessageRecipientsReadResponse '
        f'(ids={len(ids)}, isRead={is_read})')


async def handle_set_message_recipients_deleted(sess, writer, cmd_id, req_type,
                                                proto):
    """SetMessageRecipientsDeletedRequest {messageRecipientIds=1, isDeleted=2}
    → SetMessageRecipientsDeletedResponse{response=1}。"""
    ids, is_deleted = [], True
    try:
        fields = pb_parse_multi(proto)
        ids = [int(v) for v in fields.get(1, []) if v]
        is_deleted = bool(fields.get(2, [1])[0]) if fields.get(2) else True
    except Exception as e:
        log(f'      [social] SetMessageRecipientsDeleted parse error: {e}')

    pid = await sess.ensure_player()
    if pid is not None and STORE is not None and ids:
        try:
            n = await STORE.set_mail_flag(int(pid), ids, 'deleted', is_deleted)
            log(f'      [DB] set_mail_deleted: {n} rows')
        except Exception as e:
            log(f'      [DB] set_mail_deleted failed: {e}')

    resp = build_response(cmd_id, 'SetMessageRecipientsDeletedResponse',
                          pb_int32(1, RC_OK))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetMessageRecipientsDeletedResponse '
        f'(ids={len(ids)}, isDeleted={is_deleted})')


async def handle_set_notifications_read(sess, writer, cmd_id, req_type, proto):
    """SetNotificationsReadRequest {notificationIds=1, readStatus=2}
    → SetNotificationsReadResponse{response=1} + Notification 行回推。

    客户端发完请求只在等服务器 KVCS：NotificationModel.refreshNotificationCounter
    由 NotificationTable.changeSignal（即 Notification 行更新）触发，本地
    请求路径不刷角标。服务器不回推 readStatus 更新行 → 右上角未读数字
    （newNotificationCount）看完战报也不清零。"""
    ids, read_status = [], 1
    try:
        fields = pb_parse_multi(proto)
        ids = [int(v) for v in fields.get(1, []) if v]
        read_status = int(fields.get(2, [1])[0]) if fields.get(2) else 1
    except Exception as e:
        log(f'      [social] SetNotificationsRead parse error: {e}')

    # 回推更新行（combat._NOTIF_ROWS 登记了本会话推送过的通知）
    if ids and sess.playerview_cmd is not None:
        from .combat import _NOTIF_ROWS
        pid = await sess.ensure_player()
        for nid in ids:
            row = _NOTIF_ROWS.get(int(nid))
            if row is None or pid is None or int(row[0]) != int(pid):
                continue
            m = pb_int64(1, int(nid))                    # id
            m += pb_int64(2, int(row[0]))                # playerId
            m += pb_int32(3, int(row[1]))                # type
            m += pb_int64(4, int(row[2]))                # typeId
            m += pb_int32(5, int(read_status))           # readStatus（回显）
            m += pb_string(6, row[3])                    # timestamp
            m += pb_string(7, row[4])                    # expires
            await _push_kvcs(writer, sess.playerview_cmd, b'Notification', m)
            log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet '
                f'(Notification id={nid}, readStatus={read_status})')

    resp = build_response(cmd_id, 'SetNotificationsReadResponse',
                          pb_int32(1, RC_OK))
    writer.write(resp)
    await writer.drain()
    log(f'S->C cmd={cmd_id:<4} SetNotificationsReadResponse '
        f'(ids={len(ids)}, readStatus={read_status})')


HANDLERS = {
    'SearchPlayersRequest': handle_search_players,
    'SearchPlayerDetailRequest': handle_search_player_detail,
    'AddToFriendsListRequest': handle_add_to_friends_list,
    'RemoveFromFriendsListRequest': handle_remove_from_friends_list,
    'QueryGamertagRequest': handle_query_gamertag,
    'IgnorePlayerChangeRequest': handle_ignore_player_change,
    'AddPlayerReportRequest': handle_add_player_report,
    'PlayerVerificationStatusRequest': handle_player_verification_status,
    'GetChatChannelsSubscribedRequest': handle_get_chat_channels_subscribed,
    'add_chatchannel_request': handle_add_chatchannel,
    'create_chatchannel_request': handle_create_chatchannel,
    'remove_chatchannel_request': handle_remove_chatchannel,
    'EditChatChannelRequest': handle_edit_chatchannel,
    'GetMailMessagesRequest': handle_get_mail_messages,
    'SendMailMessageRequest': handle_send_mail_message,
    'SetMessageRecipientsReadRequest': handle_set_message_recipients_read,
    'SetMessageRecipientsDeletedRequest': handle_set_message_recipients_deleted,
    'SetNotificationsReadRequest': handle_set_notifications_read,
}
