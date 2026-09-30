# -*- coding: utf-8 -*-
"""军团（Company）/徽章（Insignia）域：从 handlers/squad.py 拆出（纯移动）。

消息 schema（decompiled/proto-files/HnG_States）：
- CreateCompanyRequest {nameLong=1, nameShort=2, description=3}
  → CreateCompanyResponse {result=1}
- InviteToCompanyRequest {playerToInvite=1} → InviteToCompanyResponse {result=1}
- KickFromCompanyRequest {playerToKick=1} → KickFromCompanyResponse {result=1}
- LeaveCompanyRequest {} → LeaveCompanyResponse {result=1}
- RespondToCompanyInvitationRequest {invitationId=1, accept=2}
  → RespondToCompanyInvitationResponse {result=1}
- CreateInsigniaRequest {repeated InsigniaPart parts=1;
  repeated int64 pricingIds=2; int32 paymentCurrency=3}
  → CreateInsigniaResponse {status=1}
- RemoveInsigniaRequest {int64 insigniaId=1} → RemoveInsigniaResponse {status=1}

ResponseCodes（company 域）：
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
    pb_int32,
    pb_parse,
    pb_parse_multi,
    )

from state import STORE, log

from .squad import RC_OK, RC_PLAYER_NOT_FOUND, _str

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
    for index, pid_price in enumerate(pricing_ids):
        part = parts[index] if index < len(parts) else None
        if part is None:
            ok, cost = False, 0
        else:
            cost, ok = await _charge_player(
                sess, pid_price, currency,
                expected_object_id=part.get('partId'))
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
    'CreateCompanyRequest': handle_create_company,
    'InviteToCompanyRequest': handle_invite_to_company,
    'KickFromCompanyRequest': handle_kick_from_company,
    'LeaveCompanyRequest': handle_leave_company,
    'RespondToCompanyInvitationRequest': handle_respond_company_invitation,
    'CreateInsigniaRequest': handle_create_insignia,
    'RemoveInsigniaRequest': handle_remove_insignia,
}
