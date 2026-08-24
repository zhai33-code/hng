# -*- coding: utf-8 -*-
"""长尾 ack 域：平台交易族（Steam/Epic/Discord/Hype/MailRU/兑换券）+
Debug*/AdminTool* 内部工具族 + memorytest/ProtobufChecksum32。

表驱动映射：请求名 → (响应名, 载荷构造器)，统一分发回最小有效响应，
防止 sendCommand 请求队列卡死。正常玩家客户端不触发本域请求
（平台交易仅在接 Steam/Epic/Discord 支付时出现；Debug/AdminTool 为
RETO 内部运维工具）；实测出现时全部安全 ack。
字段号以 decompiled/proto-files/HnG_States/*.proto 为准；
repeated 零条目完全省略（项目协议规则）。
"""


from protocol import (
    build_response,
    pb_bool,
    pb_int32,
    pb_int64,
    pb_parse,
    pb_string,
    pb_uint32,
    )

from state import log


# ---- 载荷构造器（f=解析后的请求字段 dict，proto=原始字节）----

def _b_ok(f, proto):
    """response=1 (ok)。"""
    return pb_int32(1, 1)


def _b_steam_init(f, proto):
    """SteamInitializeTransactionResponse：response=1 ok、steamResponseCode=2、
    orderId=3 回显请求 orderId。"""
    return pb_int32(1, 1) + pb_int32(2, 1) + pb_int64(3, f.get(1, 0))


def _b_steam_finalize(f, proto):
    """SteamFinalizeTransactionResponse：response=1 ok、steamResponseCode=2。"""
    return pb_int32(1, 1) + pb_int32(2, 1)


def _b_epic_token(f, proto):
    """EpicGetAccessTokenResponse：response=1 ok、accessToken=2 回显 exchangeToken。"""
    token = f.get(1, b'')
    if not isinstance(token, bytes):
        token = str(token).encode('utf-8', 'replace')
    return pb_int32(1, 1) + pb_string(2, token)


def _b_epic_finalize(f, proto):
    """EpicFinalizeTransactionResponse：response=1 ok、epicResponseCode=2。"""
    return pb_int32(1, 1) + pb_int32(2, 1)


def _b_query_bundles(f, proto):
    """Steam/EpicQueryBundlesResponse：response=1 ok、steamResponseCode=2、
    infos=3 省略（无 bundle）、validFactionTemplateIds=4、
    validCharacterTemplateIds=5（逗号分隔模板 id，客户端按串解析过滤）。"""
    return (pb_int32(1, 1) + pb_int32(2, 1)
            + pb_string(4, '1,2,3') + pb_string(5, '1,2,3,4,5,6,7'))


def _b_query_vouchers(f, proto):
    """QueryVoucherPacksResponse：response=1 ok、infos=2 省略、
    validFactionTemplateIds=3、validCharacterTemplateIds=4。"""
    return (pb_int32(1, 1)
            + pb_string(3, '1,2,3') + pb_string(4, '1,2,3,4,5,6,7'))


def _b_epic_entitlements(f, proto):
    """EpicQueryEntitlementsResponse：response=1 ok、epicResponseCode=2。"""
    return pb_int32(1, 1) + pb_int32(2, 1)


def _b_discord_finalize(f, proto):
    """DiscordFinalizeTransactionResponse：response=1 ok、discordResponseCode=2。"""
    return pb_int32(1, 1) + pb_int32(2, 1)


def _b_discord_entitlements(f, proto):
    """DiscordQueryEntitlementsResponse：response=1 ok、discordResponseCode=2、
    nitroPerksAreReady=3（无 Nitro 权益可领）。"""
    return pb_int32(1, 1) + pb_int32(2, 1) + pb_bool(3, False)


def _b_hype_finalize(f, proto):
    """HypeFinalizeTransactionResponse：response=1 (string)、transactionId=2
    回显 providerTransactionId、responseCode=3。"""
    tid = f.get(1, b'')
    if not isinstance(tid, bytes):
        tid = str(tid).encode('utf-8', 'replace')
    return pb_string(1, 'ok') + pb_string(2, tid) + pb_int32(3, 1)


def _b_hype_reverse(f, proto):
    """HypeReverseTransactionResponse：response=1 (string)、responseCode=2。"""
    return pb_string(1, 'ok') + pb_int32(2, 1)


def _b_mailru_init(f, proto):
    """MailRUInitializeTransactionResponse：response=1 ok、mailRUResponseCode=2、
    mailRUErrorMessage=3 空、url=4 空（无支付跳转）。"""
    return pb_int32(1, 1) + pb_int32(2, 1) + pb_string(3, '') + pb_string(4, '')


def _b_mailru_finalize(f, proto):
    """MailRUFinalizeTransactionResponse：response=1 (string)。"""
    return pb_string(1, 'ok')


def _b_echo_id(f, proto):
    """DebugTestSubscribe/DebugTestStartResponse：id=1 回显请求 id。"""
    return pb_int64(1, f.get(1, 0))


def _b_success(f, proto):
    """success=1 (bool true)：Debug 战场操作族。"""
    return pb_bool(1, True)


def _b_result_ok(f, proto):
    """result=1 (ok)：DebugSetPlayerTier/AdminTool result 族。"""
    return pb_int32(1, 1)


def _b_war_active(f, proto):
    """DebugGetWarResponse：isActive=1 true。"""
    return pb_bool(1, True)


def _b_empty(f, proto):
    """全部字段为 repeated 且零条目（DebugGetCharacters/DebugGetAts）：
    空 payload（零条目完全省略）。"""
    return b''


def _b_admintool_update(f, proto):
    """AdminToolUpdateResponse：result=1 ok、updateResult=2、createdId=3。"""
    return pb_int32(1, 1) + pb_int32(2, 1) + pb_int64(3, 0)


def _b_admintool_matchmaker(f, proto):
    """AdminToolTestMatchmakerResponse：result=1 ok、log=2、missions=3 省略。"""
    return pb_int32(1, 1) + pb_string(2, '')


def _b_admintool_statquery(f, proto):
    """AdminToolStatQueryResponse：result=1 ok、characterId=2 回显、
    characterStats=3 省略。"""
    return pb_int32(1, 1) + pb_int64(2, f.get(1, 0))


def _b_admintool_perform(f, proto):
    """AdminToolPerformActionResponse：result=1 ok、performActionResult=2。"""
    return pb_int32(1, 1) + pb_string(2, '')


def _b_admintool_sql(f, proto):
    """AdminToolExecuteSqlResponse：result=1 ok、executeResult=2。"""
    return pb_int32(1, 1) + pb_string(2, '')


def _b_memorytest(f, proto):
    """memorytestresponse：Bytes=1 原样回显（内存测试 echo）。"""
    data = f.get(1, b'')
    if not isinstance(data, bytes):
        data = b''
    return pb_string(1, data)


def _b_checksum32(f, proto):
    """ProtobufChecksum32Response：responseCode=1 (0=checksum_ok)、
    checksum=2、debugInfo=3 省略。checksum 回 0：
    本服务器 proto 构建与客户端类描述符无 crc 对照表，仅 ack。"""
    return pb_int32(1, 0) + pb_uint32(2, 0)


# ---- 表驱动映射：请求名 → (响应名, 载荷构造器) ----

_ACK_TABLE = {
    # 平台交易族
    'SteamQueryBundlesRequest': ('SteamQueryBundlesResponse', _b_query_bundles),
    'SteamInitializeTransactionRequest': ('SteamInitializeTransactionResponse',
                                          _b_steam_init),
    'SteamFinalizeTransactionRequest': ('SteamFinalizeTransactionResponse',
                                        _b_steam_finalize),
    'EpicGetAccessTokenRequest': ('EpicGetAccessTokenResponse', _b_epic_token),
    'EpicFinalizeTransactionRequest': ('EpicFinalizeTransactionResponse',
                                       _b_epic_finalize),
    'EpicQueryBundlesRequest': ('EpicQueryBundlesResponse', _b_query_bundles),
    'EpicQueryEntitlementsRequest': ('EpicQueryEntitlementsResponse',
                                     _b_epic_entitlements),
    'DiscordInitializeTransactionRequest': ('DiscordInitializeTransactionResponse',
                                            _b_ok),
    'DiscordFinalizeTransactionRequest': ('DiscordFinalizeTransactionResponse',
                                          _b_discord_finalize),
    'DiscordQueryEntitlementsRequest': ('DiscordQueryEntitlementsResponse',
                                        _b_discord_entitlements),
    'HypeFinalizeTransactionRequest': ('HypeFinalizeTransactionResponse',
                                       _b_hype_finalize),
    'HypeReverseTransactionRequest': ('HypeReverseTransactionResponse',
                                      _b_hype_reverse),
    'MailRUInitializeTransactionRequest': ('MailRUInitializeTransactionResponse',
                                           _b_mailru_init),
    'MailRUFinalizeTransactionRequest': ('MailRUFinalizeTransactionResponse',
                                         _b_mailru_finalize),
    'QueryVoucherPacksRequest': ('QueryVoucherPacksResponse', _b_query_vouchers),
    # Debug 族（DebugGetWarCatalogueRequest/DebugAddCharacterRibbonXPRequest
    # 无响应 proto 定义，不注册，走 HandlerErrorResponse 兜底）
    'DebugTestSubscribe': ('DebugTestSubscribeResponse', _b_echo_id),
    'DebugTestStart': ('DebugTestStartResponse', _b_echo_id),
    'DebugStartBattlesRequest': ('DebugStartBattlesResponse', _b_success),
    'DebugSetPlayerTierRequest': ('DebugSetPlayerTierResponse', _b_result_ok),
    'DebugSetPlayerGoldRequest': ('DebugSetPlayerGoldResponse', _b_success),
    'DebugSetMapEntityOwnerRequest': ('DebugSetMapEntityOwnerResponse', _b_success),
    'DebugSetAtMoraleRequest': ('DebugSetAtMoraleResponse', _b_success),
    'DebugResupplyATRequest': ('DebugResupplyATResponse', _b_success),
    'DebugRemoveBattlesRequest': ('DebugRemoveBattlesResponse', _b_success),
    'DebugPlaceATRequest': ('DebugPlaceATResponse', _b_success),
    'DebugGetWarRequest': ('DebugGetWarResponse', _b_war_active),
    'DebugGetCharactersRequest': ('DebugGetCharactersResponse', _b_empty),
    'DebugGetAtsRequest': ('DebugGetAtsResponse', _b_empty),
    'DebugEquipATRequest': ('DebugEquipATResponse', _b_success),
    # AdminTool 族（AdminToolQueryFastRequest 无响应 proto，不注册）
    'AdminToolUpdateRequest': ('AdminToolUpdateResponse', _b_admintool_update),
    'AdminToolTestMatchmakerRequest': ('AdminToolTestMatchmakerResponse',
                                       _b_admintool_matchmaker),
    'AdminToolStatQueryRequest': ('AdminToolStatQueryResponse',
                                  _b_admintool_statquery),
    'AdminToolQueryRequest': ('AdminToolQueryResponse', _b_ok),
    'AdminToolPlayerStatRequest': ('AdminToolPlayerStatResponse', _b_ok),
    'AdminToolModifyRibbonLevelRequest': ('AdminToolModifyRibbonLevelResponse',
                                          _b_result_ok),
    'AdminToolGetDbInfoRequest': ('AdminToolGetDbInfoResponse', _b_ok),
    'AdminToolPerformActionRequest': ('AdminToolPerformActionResponse',
                                      _b_admintool_perform),
    'AdminToolExecuteSqlRequest': ('AdminToolExecuteSqlResponse',
                                   _b_admintool_sql),
    # 网络诊断族
    'memorytestrequest': ('memorytestresponse', _b_memorytest),
    'ProtobufChecksum32Request': ('ProtobufChecksum32Response', _b_checksum32),
}


def _make_ack_handler(req_type):
    resp_name, builder = _ACK_TABLE[req_type]

    async def _ack_handler(sess, writer, cmd_id, _rt, proto):
        try:
            f = pb_parse(proto)
            log(f'      ack {req_type}: fields={f}')
        except Exception as e:
            log(f'      ack {req_type} parse error: {e}')
            f = {}
        payload = builder(f, proto)
        resp = build_response(cmd_id, resp_name, payload)
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} {resp_name} (ack, {len(payload)}B)')

    _ack_handler.__name__ = f'handle_ack_{req_type.lower()}'
    return _ack_handler


HANDLERS = {rt: _make_ack_handler(rt) for rt in _ACK_TABLE}
