# -*- coding: utf-8 -*-
"""游戏进程(warserver)协议模块：hng.exe/player.dll(ZEngine C++) 连 actionhost 后的游戏握手。
协议与 Flash 客户端相同：Chunk 帧(cmdId + 类型名 + protobuf 载荷)。
请求：GetPlayerSettings / SetPlayerSettings / GetPlayerUidByNickRequest / RequestBattleFieldSettings ...
响应：PlayerSettings / SquadResponse / ResponseBattleFieldSettings / GetPlayerUidByNickResponse ...

本模块为纯分发层（请求解析 → 会话/DB 取数 → 调消息构建 → 回帧）：
- 消息构建：warserver_messages.py（PlayerSettings/CharacterEntry/Ribbon/BattleFieldSettings）
- 装备 DB 聚合：player_service._load_db_loadout
- WarDatabase 静态表：wardb.py（preset/accesspoint/clipcount/军衔阈值）
- 宽松 protobuf 请求解析：protocol.pb_parse_loose（原 hng_parse）

schema 来源：player.dll protobuf 描述符 @ RVA 0x7b3f78（逆向确认，40 字段）。
注意：描述符 schema 与字段名表(0x7b2e58)完全不同 —— 名表是 HTTP/Flash 客户端协议字段，
描述符定义的是 hng.exe/player.dll(ZEngine C++ 游戏进程)实际使用的 protobuf schema。
GetPlayerSettings 请求 field1=playerMissionSessionId(300001) 属于请求消息的 schema。
"""
import uuid

# OpenID provider 集成：warserver 本地签发 id_token，Epic 后端用 JWKS 公钥验证
from eos import oidc_issuer
# matchmaking 的运行时状态（PMS 会话 / 动态 id 发号 / COMBAT_READY）
import matchmaking
import combat_instances
import mm_state
import mm_factions
import mm_weather
import mm_config
from handlers.squad_match import handle_squad_request

from protocol import (
    build_response,
    pb_int64,
    pb_message,
    pb_parse_loose,
    pb_split_top,
    pb_string,
    pb_varint,
    )
from state import STORE, log

from player_service import _load_db_loadout
from warserver_messages import (
    BF_DEFENDER_FACTION_ID,
    MISSION_PRESET_ID,
    _load_preset,
    _msg_player_settings,
    _msg_response_battlefield_settings,
    _msg_ribbon_progress,
    )

# ---------------------------------------------------------------------------
# CharacterStats 字段映射表 / 维度分组 / payload 构建（_CS_SCALAR_KEYS /
# _WEAPON_FIELDS / _VEHICLE_FIELDS / _GAMEMODE_FIELDS / _group_dim_stats /
# character_stats_payload）：
# 2026-09-01 下沉到 messages.py 与 Flash 菜单端（handlers/characters.py
# RequestReadCharacterStats → 统计界面）共用，此处仅导入。
# ---------------------------------------------------------------------------
from messages import (                     # noqa: E402（放在使用处附近便于对照）
    _CS_SCALAR_KEYS,
    _GAMEMODE_FIELDS,
    _VEHICLE_FIELDS,
    _WEAPON_FIELDS,
    _group_dim_stats,
    character_stats_payload,
)


async def _handler_error(writer, cmd_id, reason):
    """协议级拒绝：缺少可靠身份/战局数据时不猜测其他会话。"""
    writer.write(build_response(cmd_id, 'HandlerErrorResponse'))
    await writer.drain()
    log(f'GAME S->C cmd={cmd_id:<4} HandlerErrorResponse ({reason})')
    return True


async def handle_request(sess, writer, cmd_id, req_type, proto):
    """游戏进程请求分发。sess：会话对象（当前未用，保留签名与 matchmaking 一致）"""
    if req_type == 'GetPlayerSettings':
        # 解析请求：field1 = playerMissionSessionId（int64）
        #          field2 = requesterType（0/缺省=游戏路径, 1=EAC 路径）
        pms_id = 0
        requester_type = 0
        try:
            fields = pb_parse_loose(proto)
            pms_id = fields.get(1, 0)
            requester_type = int(fields.get(2, 0) or 0)
        except Exception:
            pass
        if not pms_id:
            pms_id = 0

        db_chars = None
        loadout = None
        # ★ 账号库动态组装：pms_id → 本次进局会话（player/char），查 characters 表。
        #   客户端部署界面与 RequestReadCharacterStats 用的都是这些库内小 id
        #   （历史回放里的他服角色是 6360... 大 id，不在库内 → 列表全空没角色可选）。
        #   f1 player_id / f2 gamertag 同步用本服账号库值 —— JWT sub 必须同源
        #   （客户端校验 f1 == sub）。
        mm_sess = mm_state.PMS_TO_SESSION.get(pms_id)
        pms_exact = mm_sess is not None   # 身份绑定只信任精确命中（见 if mm_sess 内）
        if mm_sess is None:
            return await _handler_error(
                writer, cmd_id, f'unknown playerMissionSessionId={pms_id}')
        faction = None
        quickplay = None
        first_action_game = None
        membership_benefit_id = 0
        if mm_sess:
            char_id = int(mm_sess['char_id'])
            player_id = int(mm_sess['player_id'])
            gamertag = mm_sess['gamertag']
            faction = int(mm_sess.get('faction', 3) or 3)
            # ★ 2026-09-06：把玩家身份绑定到本游戏连接（hng.exe 单连接单玩家）。
            #   局内举报（AddPlayerReportRequest）等社交类消息由 hng.exe 经本连接
            #   直发（player.dll warcomm 注册表实证），此前不绑定 →
            #   ensure_player() 查 'demo' 落空 → 举报被静默丢弃（后台 0 记录根因）。
            #   仅 pms 精确命中时绑定：stale-pms 兜底会话可能是别的玩家，宁缺勿错。
            if pms_exact:
                try:
                    sess.player_id = player_id
                    sess.username = str(gamertag)
                except AttributeError:
                    pass
                # ★ 玩家游戏连接打标（战斗服 ZServer 连接也会带精确 pms 查
                #   GetPlayerSettings 做 InitPlayer，必须排除）：连接断开 =
                #   hng.exe 离开战局 → realtest_server 触发 rejoin 掉线保护
                #   （on_game_client_closed：推 PMS Admitted 起 300s 倒计时）。
                if not getattr(sess, 'is_combat_server', False):
                    try:
                        sess.is_game_client = True
                    except AttributeError:
                        pass
                    # hng.exe 携本会话重进（rejoin 按钮拉起）：撤销掉线保护
                    # deadline（连接再断会由 on_game_client_closed 重新起算）
                    matchmaking.clear_rejoin_deadline(int(player_id))
            # 每局动态 id 与 PMS 推送同源（matchmaking._handle_join 发号入会话），
            # 会话缺失时现场发号 —— 绝不回退他服回放值
            squad_id = int(mm_sess.get('squad_id', 0) or 0) or \
                mm_state._next_match_id(mm_state._MM_SQUAD_ID)
            squad_slot = int(mm_sess.get('squad_slot', 0) or 0)
            metrics_uid = mm_sess.get('metrics_uid') or str(uuid.uuid4())
            metrics_session_id = int(mm_sess.get('metrics_session_id', 0) or 0) \
                or mm_state._next_match_id(mm_state._MM_METRICS_SESSION_ID)
            mission_session_id = int(mm_sess.get('battle_session_id', 0) or 0) \
                or mm_state._next_match_id(mm_state._MM_BATTLE_SESSION_ID)
            if mm_sess.get('quickplay') is not None:
                quickplay = bool(mm_sess['quickplay'])
            if STORE is not None:
                try:
                    db_chars = await STORE.list_characters(int(mm_sess['player_id']))
                    log(f'      [DB] list_characters pid={mm_sess["player_id"]} '
                        f'-> {len(db_chars)} chars, joined char={char_id}')
                    # f24 firstActionGame：全部角色 combat_xp==0 → 首场对局
                    if db_chars:
                        first_action_game = all(
                            int(ch.get('combat_xp', 0) or 0) == 0
                            for ch in db_chars)
                    # ★ f12 装备 DB 化：聚合武器/弹药/配件/载具/军服表
                    #   （失败 = 空装备条目，不回退回放）
                    loadout = await _load_db_loadout(int(mm_sess['player_id']))
                    membership = await STORE.get_active_membership(
                        int(mm_sess['player_id']))
                    # MembershipBenefitTemplate #2 是原版老兵会员收益模板；
                    # 仅在数据库确有未过期会员时下发。
                    membership_benefit_id = 2 if membership else 0
                    if loadout is not None:
                        nw = sum(len(v) for v in loadout['weapons'].values())
                        nv = sum(len(v) for v in loadout['vehicles'].values())
                        log(f'      [DB] loadout: {nw} weapons / {nv} vehicles '
                            f'across {len(loadout["weapons"])} chars')
                except Exception as e:
                    log(f'      [DB] list_characters failed: {e}')
        # missionId 必须与客户端启动参数 /M 一致（否则客户端校验失败挂起→20s 超时断开）
        # pms 命中时从映射取；未命中（兜底会话）用该会话的 mission；
        # 都没有才回退全局计数（旧行为）
        mission_id = mm_state.PMS_TO_MISSION.get(pms_id, 0) or \
            int(mm_sess.get('mission_id', 0) or 0)
        if not mission_id:
            return await _handler_error(
                writer, cmd_id, f'PMS {pms_id} has no mission binding')
        # ★ 记在连接上：玩家游戏进程的 ActiveMissionRequestEventList 轮询
        #   实测不带 mission_id（f1=0，与战斗服同款），combat.py 用它定位
        #   本局资源池/布局（否则回退 Mill 模板 → 资源数字错局）。
        try:
            sess.game_mission = int(mission_id)
        except AttributeError:
            pass

        username = gamertag
        eos_token = ''
        if mm_config.EOS_TOKEN_ENABLED:
            try:
                eos_token = oidc_issuer.make_id_token(username, subject=str(player_id))
                log(f'      [oidc] issued id_token for {username!r} sub={player_id} ({len(eos_token)}B)')
            except Exception as e:
                log(f'      [oidc] token issue FAILED: {e}')
                eos_token = ''
        # ★★ 2026-08-20 定论（14:26 成功运行 vs 16:05 崩溃 dump 对比 +
        #    ResponseSquadCallback sub_1802C4150 反编译证实）：
        #    requesterType=1（EAC 路径）与 type=0 必须回同一份完整 PlayerSettings。
        #    完整处理时（"Player Joined"）会把 f16 squadId 注册进 gamecontroller
        #    的 squad 树(+0x20298)；随后 SquadResponse 的 f1 在该树查找命中 →
        #    走 CreateSquadInstance。若回最小消息（缺 f16）→ 树里无节点 →
        #    sub_1802C4150 的树遍历 i=NULL，AddOrGetTeam 成功后解引用 i[4] →
        #    空指针崩溃（0x2c46c0, dump 实证 Rdi=0）。
        # ★ 2026-08-20 起回放路径全部移除：不再以 pvt_player_settings.bin
        #    字节打底替换，requesterType 0/1 一律走 warserver_messages.
        #    _msg_player_settings 全动态构建（f12 = 库内角色+装备，
        #    库里没有就发空装备条目）。
        resp_proto = _msg_player_settings(
            player_id=player_id,
            pms_id=pms_id,
            char_id=char_id,
            eos_token=eos_token,
            display_name=username,
            metrics_uid=metrics_uid,
            metrics_session_id=metrics_session_id,
            mission_session_id=mission_session_id,
            squad_id=squad_id,
            squad_slot=squad_slot,
            faction=faction if faction is not None else 3,
            db_chars=db_chars,
            loadout=loadout,
            quickplay=quickplay,
            first_action_game=first_action_game,
            membership_benefit_id=membership_benefit_id,
        )
        resp = build_response(cmd_id, 'PlayerSettings', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'GAME S->C cmd={cmd_id:<4} PlayerSettings (pms={pms_id}, player={player_id}, '
            f'char={char_id}, {len(resp_proto)}B, requesterType={requester_type})')

        # ★ private_login.pcapng 复核（2026-08-19 全帧解析）：
        # 真实 warserver 的 game 连接只回 PlayerSettings 后【完全静默】——
        # PMS(f14=2/3)、ViewHandlerPulse、BattleInfo、faction 全部走 Flash 客户端长连接。
        # 之前在 game 连接推 KVCS 帧属于协议违约（真实流程从不这么做），
        # 游戏收到后状态机卡死 → 不连 actionhost UDP → ~20s 断开。
        # PMS f14=3(Armed) 由 matchmaking._matchmade 在 Flash 连接上延迟推送。
        return True

    if req_type == 'RequestReadCharacterStats':
        # ★ 2026-08-20：客户端 hng.exe 部署界面 + ZServer InitPlayer 都会请求
        #   角色统计（"Requesting stats for character: %lld" statistics.cpp
        #   sub_180381A20；实测日志 211 次请求全被 HandlerErrorResponse 吞掉）。
        #   无响应 → "#error3 _GetTotalsFromStatisticsServerNotify as == 0,
        #   ErrorCode: 2" → 部署界面无角色可选、TAB 积分板无数据。
        # schema（decompiled/proto-files/statrequests/）：
        #   RequestReadCharacterStats  { f1 repeated uint64 characterids,
        #                               f2 bool ignoreSessionId }
        #   ResponseReadCharacterStats { f1 repeated entry
        #       { f1 uint64 characterId, f2 CharacterStats 必填,
        #         f3 repeated Ribbon（空必须省略）} }
        #   CharacterStats：f1-f35+f37-f48 required int32 + f56 required string key
        #   （proto2 required → 全部显式发 0/空串，spbc 解析器按字段号取值）。
        char_ids = []
        try:
            for fn, chunk in pb_split_top(proto):
                if fn == 1 and (chunk[0] & 7) == 0:      # f1 varint
                    v = vsh = k = 0
                    while True:
                        b = chunk[1 + k]; k += 1
                        v |= (b & 0x7F) << vsh; vsh += 7
                        if not (b & 0x80):
                            break
                    char_ids.append(v)
        except Exception:
            pass
        # ★ 2026-08-20 DB 化：f35 CharacterXp ← characters.combat_xp（军衔进度条），
        #   f3 Ribbon 勋带进度 ← character_ribbon_xp（等级经 ribbonlevel 阈值推导）。
        # ★ 2026-08-27 全量回读：f1-f48 标量 + f50/f51/f53 载具/武器/模式数组
        #   ← character_stats（type-6 统计上报经 statistics_server 落库，
        #   见 _CS_SCALAR_KEYS / _group_dim_stats；legacy 结算键作回落）。
        # ★ 2026-08-27 两人局修复：请求里的 characterids 属于"发起请求的那个
        #   玩家"，但该请求无玩家标识可回查（ZServer 按角色直发），原来只取
        #   最后一条会话的角色库 → 两人局先入队玩家的角色查无此 id（xp=0、
        #   无勋带）。改为聚合所有进局会话玩家的角色，按 char_id 反查所属
        #   player 查勋带。
        chars_by_id = {}
        pid_by_char = {}
        stats_pid = None
        stats_cid = 0
        for _s in mm_state.PMS_TO_SESSION.values():
            stats_pid = _s['player_id']   # dict 保序 → 最后一条 = 最新进局会话
            stats_cid = int(_s.get('char_id', 0) or 0)
            if STORE is None:
                continue
            try:
                for ch in await STORE.list_characters(int(_s['player_id'])):
                    chars_by_id[int(ch['id'])] = ch
                    pid_by_char[int(ch['id'])] = int(_s['player_id'])
            except Exception as e:
                log(f'[DB] stats list_characters failed: {e}')
        resp_proto = b''
        ribbon_count = 0
        n_vehicles = n_weapons = n_gamemodes = 0
        for cid in char_ids or ([stats_cid] if stats_cid else []):
            ch = chars_by_id.get(int(cid))
            xp = int(ch.get('combat_xp', 0) or 0) if ch else 0
            # ★ 2026-08-27 DB 化回读：type-6 统计上报落库的键
            #   （WIRE 名 + w/v/gm 维度前缀）→ f1-f48 标量 + f50/f51/f53 数组
            cstats = {}
            if STORE is not None:
                try:
                    cstats = await STORE.get_stats(int(cid)) or {}
                except Exception as e:
                    log(f'[DB] stats get_stats failed (char={cid}): {e}')
            # ★ 2026-09-01 统计构建下沉 messages.character_stats_payload 与
            #   Flash 菜单端共用（f53 并合并为 gameMode=0 —— 客户端只接受 0）
            cs = character_stats_payload(cstats, xp)
            n_vehicles += len(_group_dim_stats(cstats, 'v'))
            n_weapons += len(_group_dim_stats(cstats, 'w'))
            n_gamemodes += min(len(_group_dim_stats(cstats, 'gm')), 1)
            entry = pb_int64(1, cid)
            entry += pb_message(2, cs)
            if ch and STORE is not None:
                try:
                    ribbon_pid = pid_by_char.get(int(cid), stats_pid)
                    if ribbon_pid is None:
                        raise LookupError('char not owned by any live session')
                    xps = await STORE.get_ribbon_xp(int(ribbon_pid), int(cid))
                    for rid, total in sorted(xps.items()):
                        if total > 0:
                            entry += pb_message(3, _msg_ribbon_progress(rid, total))
                            ribbon_count += 1
                except Exception as e:
                    log(f'[DB] stats ribbon_xp failed: {e}')
            resp_proto += pb_message(1, entry)
        resp = build_response(cmd_id, 'ResponseReadCharacterStats', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'GAME S->C cmd={cmd_id:<4} ResponseReadCharacterStats '
            f'({len(char_ids)} chars: {char_ids[:8]}, {ribbon_count} ribbons, '
            f'{n_weapons} weapons, {n_vehicles} vehicles, {n_gamemodes} gamemodes)')
        return True

    if req_type == 'RequestEarnings':
        # §7.4：ZServer 请求玩家累计收益（doc §4 登录序列第 [5] 步）。
        #   ResponseEarnings { f1 player_id, f2 silver_earned, f3 gold_earned,
        #                       f4 silver_used, f5 gold_used }
        # DB 化：silver=credits / gold=gold（players 表）；earned/used 历史
        # 拆分无累计数据源（战斗结算写入端未建），earned=当前余额、used=0。
        req_pid = 0
        try:
            fields = pb_parse_loose(proto)
            req_pid = int(fields.get(1, 0) or 0)
        except Exception:
            pass
        if not req_pid:
            req_pid = int(getattr(sess, 'player_id', 0) or 0)
        if not req_pid:
            return await _handler_error(writer, cmd_id,
                                        'RequestEarnings missing playerId')
        credits = gold = 0
        if req_pid and STORE is not None:
            try:
                cur = await STORE.get_currency(int(req_pid))
                credits = int(cur['credits'])
                gold = int(cur['gold'])
            except Exception as e:
                log(f'[DB] get_currency failed: {e}')
        resp_proto = pb_int64(1, int(req_pid or 0))
        resp_proto += pb_int64(2, credits)            # silver_earned
        resp_proto += pb_int64(3, gold)               # gold_earned
        resp_proto += pb_int64(4, 0)                  # silver_used（无累计源）
        resp_proto += pb_int64(5, 0)                  # gold_used（无累计源）
        resp = build_response(cmd_id, 'ResponseEarnings', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'GAME S->C cmd={cmd_id:<4} ResponseEarnings '
            f'(pid={req_pid}, silver={credits}, gold={gold})')
        return True

    if req_type == 'GetPlayerUidByNickRequest':
        # EAC 辅助查询：warserver 查昵称 → player_uid。
        # ★★ 2026-08-20 恢复真实响应：14:26 成功运行完整走通
        #    "RESPONSE GetPlayerUidByNick found=1" → "GetPlayerUidByNick OK"
        #    → "Player Joined"——真实响应是 EAC 把 ClientId 映射到 playeruid、
        #    玩家正式入局的必要环节（16:05 改 HandlerErrorResponse 后
        #    Player Joined 不再出现）。
        # ★★ 2026-08-20 TTD 时间旅行实锤崩溃根因（work/ttd_culprit.txt）：
        #    ZServer OnGetPlayerUidByNickResult 对 playeruid 做
        #    ZStringBuffer<16,1> 临时拷贝，字符串 >15 字节走堆路径，
        #    释放时用引擎对象池 free() 去 free spbc ringbuf(LFH) 内存，
        #    [ptr-8] 读到 LFH 头 0xe0 ≠ 1 → player+0x4AD46D int3 必崩。
        #    （"跨分配器 free 系误判"的旧备注才是误判——KVCS 修复只是
        #    改变了 LFH 块回收路径，暂缓了崩溃。）
        #    修复：uid 字符串截到 15 字符内（SSO 内联），其余字段不动。
        nick = ''
        try:
            f = pb_parse_loose(proto)
            _n = f.get(1, b'')
            if isinstance(_n, bytes):
                nick = _n.decode('utf-8', 'replace')
            else:
                nick = str(_n)
        except Exception:
            nick = ''
        # ★ 15 字符仍崩（hng.exe.2604.dmp 同点 int3）——ZStringBuffer<16,1>
        #   的 16B 缓冲含簿记字节，SSO 上限可能是 14 甚至更小。
        #   降到 10 字符（任何阈值下都在 SSO 内），若仍崩则非长度问题，
        #   需 TTD 轨迹直接读崩溃块内容定性。
        # ★ uid 用账号库 player_id（与 PlayerSettings f1 / JWT sub 同源）：
        #   1) 按请求昵称精确匹配进局会话（多人服各玩家查各自的 uid）；
        #   2) 昵称即登录名 → 查 players 表（未进局的账号也能查到）；
        #   3) 都未命中 → found=0；绝不把别的最新会话冒充查询结果。
        uid_pid = None
        if nick:
            for _s in mm_state.PMS_TO_SESSION.values():
                if _s.get('gamertag') == nick:
                    uid_pid = _s['player_id']
                    break
            if uid_pid is None and STORE is not None:
                try:
                    uid_pid = await STORE.get_player(nick)
                except Exception as e:
                    log(f'[DB] get_player({nick!r}) failed: {e}')
        found = 1 if uid_pid is not None else 0
        uid_str = str(uid_pid)[:10] if uid_pid is not None else ''  # ≤10 字符 SSO 安全区
        resp_proto = pb_string(1, uid_str)   # f1 = playeruid
        resp_proto += pb_varint(2, found)    # f2 = found
        resp = build_response(cmd_id, 'GetPlayerUidByNickResponse', resp_proto)
        writer.write(resp)
        await writer.drain()
        log(f'GAME S->C cmd={cmd_id:<4} GetPlayerUidByNickResponse '
            f'(nick={nick!r}, found={found}, uid={uid_str})')
        return True

    if req_type == 'SquadRequest':
        # 战斗服 squad 定义查询（08-27/08-28 修复重灾区）→ handlers/squad_match.py
        return await handle_squad_request(
            sess, writer, cmd_id, req_type, proto)

    if req_type == 'RequestBattleFieldSettings':
        # 专用服（hng.exe /D）启动请求：field1=missionid（/P 启动参数）。
        # ★ 标记该连接为 ZServer wardatacomm：realtest_server 的 ViewHandlerPulse
        #   心跳必须跳过它（官方 warserver 从不向专用服推 KVCS，ZServer 收到
        #   'KeyValueChangeSet' 走 Unhandled 路径，该路径疑似堆损坏 →
        #   0x4AD46D (ZMemoryTracker::Free 校验) / 0x269E2 崩溃）。
        try:
            sess.is_combat_server = True
        except AttributeError:
            pass
        # 响应按 cmd_id 回显匹配（WarDataComm+800 红黑树查请求 ID），
        # 10s 超时 → "#error3 Couldn't GetBattleFieldSettings()" → 专用服退出。
        missionid = 0
        try:
            missionid = int(pb_parse_loose(proto).get(1, 0) or 0)
        except Exception:
            pass
        # 匹配池成局：按 missionid 取成局 preset / 攻防 faction（多局并行时
        # 每场战局地图/布局各不相同）；查不到（旧路径/异常）回退全局默认。
        # 防御：若 field1 实测为 pms_id（协议文档 §B.7 之外的备选情形），
        # 经 PMS_TO_MISSION 反查 mission_id 再取详情。
        det = mm_state._MM_MISSION_DETAILS.get(missionid)
        if not det and missionid:
            mid2 = mm_state.PMS_TO_MISSION.get(missionid)
            if mid2:
                det = mm_state._MM_MISSION_DETAILS.get(mid2)
        if not det:
            # ★ 实测专用服本请求 proto 为空（无 field1，日志全量可证）：
            #   按启动顺序 FIFO 认领最早未握手实例，取其 preset/地图/阵营，
            #   否则回退默认 preset=10(Mill) → 专用服加载错图 + inst['ready']
            #   永不置位 → 120s 握手超时 → 客户端卡匹配 UI。
            #   多机部署：按来源 IP 只在该机（combat_hosts addr/public_addr/
            #   agent 主机名匹配）的待认领实例中 FIFO——不同机实例握手到达
            #   顺序 ≠ 启动顺序，全局 FIFO 会错配地图。
            claimed = combat_instances.claim_pending_combat_instance(
                getattr(sess, 'remote_ip', None))
            if claimed:
                missionid = claimed
                det = mm_state._MM_MISSION_DETAILS.get(missionid)
        if det and 'preset' in det:
            # ★ 阵营字段无默认值：_faction_pair 校验（缺失/非法/同侧 →
            #   None + ERROR 日志），不静默用 2/GE、3/SU 默认阵营配出生侧
            #   （曾致错阵营判负+观战）。校验不过按"无详情"走 legacy 回退。
            #   角色建号即写 faction、_start_match 恒写攻防字段，正常路径
            #   恒合法；此处仅拦结构损坏。
            fp = mm_factions._faction_pair(det)
            if fp is None:
                det = None
        if det and 'preset' in det:
            preset_id = int(det['preset'])
            attacker_faction, defender_faction = fp
            enable_bots = bool(det.get('enable_bots', True))
            enable_sa_bots = bool(det.get('enable_sa_bots', False))
            # 本局天气/时段（成局时随机抽取，与简报 MissionInfo f12/f13 同源）
            weather_tod = int(det.get('weather_tod', 1))
            # f22 = 天气列 | 雪掩码（雪局 0x30 高位，snow-unified-patch.md §3：
            # 补丁版 dll 于 CreateInstance 拆开；details 的 weather_condition
            # 保持 0..2，简报 f13 同源不受污染）
            weather_condition = mm_weather.snow_f22_condition(det)
            timeofday = det.get('timeofday')
            # ★ 记在连接上：战斗服后续 ActiveMissionRequestEventList 轮询
            #   请求同样不带 mission_id（实测 mission=0），combat.py 用它
            #   定位本局 preset/布局，否则回退 Mill 模板 8/15 →
            #   "New Resource Invalid accesspoint"（图上无此通道点）。
            try:
                sess.combat_mission = int(missionid)
            except AttributeError:
                pass
        else:
            return await _handler_error(
                writer, cmd_id,
                f'no battlefield details for mission={missionid}')
        # ---- war 会战局战役标识（官方机制，build_battle_report_spbc 实锤）：
        # f17 isCampaignMission 是战斗服战役加成总开关（+50% XP/勋带、+25%
        # 银币、warfundsEarned 基础值）；f16 战争天数；f24 野外遭遇局补给线
        # id；f18 faction 行 f3/f4 带当前战争 underdog 倍率（战斗服结算
        # xpUnderdogBonus / 战资 underdogBonus 数据源）。
        is_campaign = False
        days_since_war = 0
        supply_line_id = 0
        underdog_mults = None
        if det and int(det.get('battle_id') or 0):
            is_campaign = True
            if int(det.get('entity_type') or 0) == 3:  # MAP_ENTITY_SUPPLYLINE
                supply_line_id = int(det.get('entity_id') or 0)
            try:
                from war_campaign import current_underdog_mults
                underdog_mults = await current_underdog_mults(STORE)
            except Exception as e:
                log(f'[war] underdog mults for battle settings failed: {e}')
            try:
                war = await STORE.get_active_war()
                bt = (war or {}).get('begintime')
                if bt:
                    from datetime import datetime, timezone
                    if bt.tzinfo is None:
                        bt = bt.replace(tzinfo=timezone.utc)
                    days_since_war = max(
                        0, (datetime.now(timezone.utc) - bt).days)
            except Exception as e:
                log(f'[war] days since war started failed: {e}')
        # f14 资源从本局资源池渲染（与匹配界面 f5 / 轮询 f2 同源同值）；
        # mission_id = Assault 出兵线随机激活种子（与大厅简报 f6 / 轮询
        # f3/f5 必须同值，wardb.assault_ap_decision 详见）
        resp_proto = _msg_response_battlefield_settings(
            preset_id, defender_faction, attacker_faction, enable_bots,
            enable_sa_bots=enable_sa_bots,
            weather_time_of_day=weather_tod, weather_condition=weather_condition,
            timeofday=timeofday,
            counts_by_faction=matchmaking.mission_resource_pool(missionid),
            mission_id=missionid,
            is_campaign=is_campaign,
            days_since_war_started=days_since_war,
            supply_line_id=supply_line_id,
            underdog_mults=underdog_mults)
        if is_campaign:
            log(f'[war] campaign battle settings mission={missionid} '
                f'days={days_since_war} supplyline={supply_line_id} '
                f'underdog={underdog_mults}')
        resp = build_response(cmd_id, 'ResponseBattleFieldSettings', resp_proto)
        writer.write(resp)
        await writer.drain()
        preset = _load_preset(preset_id)
        _name = preset.get(5, b'')
        if isinstance(_name, bytes):
            _name = _name.decode('utf-8', 'replace')
        log(f'GAME S->C cmd={cmd_id:<4} ResponseBattleFieldSettings '
            f'(mission={missionid}, preset={int(preset.get(1, 0))}, '
            f'map gamemap={int(preset.get(3, 0))}, {_name!r}, '
            f'bots={enable_bots}, sa_bots={enable_sa_bots}, weather tod={weather_tod}/'
            f'cond={weather_condition} @{timeofday}h, {len(resp_proto)}B)')
        # 通知 matchmaking：该 missionid 的专用服握手完成，可推 Admitted
        ready_mid = missionid
        if not (det and 'preset' in det) and missionid:
            ready_mid = mm_state.PMS_TO_MISSION.get(missionid, missionid)
        combat_instances.notify_combat_ready(ready_mid or None)
        return True
    return False
