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
import mm_config

from protocol import (
    build_response,
    pb_int32,
    pb_int64,
    pb_message,
    pb_parse_loose,
    pb_split_top,
    pb_string,
    pb_varint,
    )
from state import STORE, log

from player_service import _load_db_loadout
from wardb import SQUAD_TEMPLATE_SLOTS
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
        mm_sess = matchmaking.PMS_TO_SESSION.get(pms_id)
        pms_exact = mm_sess is not None   # 身份绑定只信任精确命中（见 if mm_sess 内）
        if mm_sess is None:
            # ★ pms 未精确命中（stale pms：rejoin/结算清理后 ZServer 仍持有）
            #   时兜底必须圈定本局：请求来自战斗服连接（握手已记
            #   combat_mission），全局"最新会话"在多局并行时会拿错玩家 →
            #   身份/阵营/角色错配（"进游戏人变成 bot"根因之一：ZServer
            #   建玩家失败 → AI 补位顶替）。
            cm = int(getattr(sess, 'combat_mission', 0) or 0)
            for _s in matchmaking.PMS_TO_SESSION.values():
                if cm and int(_s.get('mission_id', 0) or 0) != cm:
                    continue
                mm_sess = _s          # dict 保序 → 本局最新进局会话
            if mm_sess is None and not cm:
                for _s in matchmaking.PMS_TO_SESSION.values():
                    mm_sess = _s      # 无本局信息（理论不可达）→ 全局最新
                if mm_sess is not None:
                    log(f'      [WARN] GetPlayerSettings: pms={pms_id} miss, '
                        f'fell back to latest session (player='
                        f'{mm_sess.get("player_id")})')
        faction = None
        quickplay = None
        first_action_game = None
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
            # 每局动态 id 与 PMS 推送同源（matchmaking._handle_join 发号入会话），
            # 会话缺失时现场发号 —— 绝不回退他服回放值
            squad_id = int(mm_sess.get('squad_id', 0) or 0) or \
                matchmaking._next_match_id(matchmaking._MM_SQUAD_ID)
            squad_slot = int(mm_sess.get('squad_slot', 0) or 0)
            metrics_uid = mm_sess.get('metrics_uid') or str(uuid.uuid4())
            metrics_session_id = int(mm_sess.get('metrics_session_id', 0) or 0) \
                or matchmaking._next_match_id(matchmaking._MM_METRICS_SESSION_ID)
            mission_session_id = int(mm_sess.get('battle_session_id', 0) or 0) \
                or matchmaking._next_match_id(matchmaking._MM_BATTLE_SESSION_ID)
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
                    if loadout is not None:
                        nw = sum(len(v) for v in loadout['weapons'].values())
                        nv = sum(len(v) for v in loadout['vehicles'].values())
                        log(f'      [DB] loadout: {nw} weapons / {nv} vehicles '
                            f'across {len(loadout["weapons"])} chars')
                except Exception as e:
                    log(f'      [DB] list_characters failed: {e}')
        else:
            # 无任何进局会话（理论不可达：hng.exe 仅在匹配成功后拉起）：
            # player/char 无从查库置零，id 全部现场生成
            char_id = 0
            player_id = 0
            gamertag = 'Player'
            squad_id = matchmaking._next_match_id(matchmaking._MM_SQUAD_ID)
            squad_slot = 0
            metrics_uid = str(uuid.uuid4())
            metrics_session_id = matchmaking._next_match_id(
                matchmaking._MM_METRICS_SESSION_ID)
            mission_session_id = matchmaking._next_match_id(
                matchmaking._MM_BATTLE_SESSION_ID)
            log(f'      [WARN] GetPlayerSettings: no PMS session '
                f'(pms_id={pms_id}), ids generated, player fields zeroed')
        # missionId 必须与客户端启动参数 /M 一致（否则客户端校验失败挂起→20s 超时断开）
        # pms 命中时从映射取；未命中（兜底会话）用该会话的 mission；
        # 都没有才回退全局计数（旧行为）
        mission_id = matchmaking.PMS_TO_MISSION.get(pms_id, 0) or \
            (int(mm_sess.get('mission_id', 0) or 0) if mm_sess else 0) or \
            matchmaking._MM_MISSION_ID[0]
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
        for _s in matchmaking.PMS_TO_SESSION.values():
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
            for _s in matchmaking.PMS_TO_SESSION.values():
                req_pid = _s['player_id']   # dict 保序 → 最新进局会话
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
        #   3) 都未命中 → 回退最新一条会话（EAC 可能用非昵称串查询）；
        #   4) 完全无会话 → found=0（真实 not-found 响应，不用回放 id）。
        uid_pid = None
        if nick:
            for _s in matchmaking.PMS_TO_SESSION.values():
                if _s.get('gamertag') == nick:
                    uid_pid = _s['player_id']
                    break
            if uid_pid is None and STORE is not None:
                try:
                    uid_pid = await STORE.get_player(nick)
                except Exception as e:
                    log(f'[DB] get_player({nick!r}) failed: {e}')
        if uid_pid is None:
            for _s in matchmaking.PMS_TO_SESSION.values():
                uid_pid = _s['player_id']
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
        # 专用服（hng.exe /D）在玩家加入后请求 squad 信息：
        #   "Request squad nCmdId: X missionSessionId: Y, squadId: Z"
        #   "ResponseSquadCallback SquadId: 0 team: T squadTemplateId: ST playerSlots: N supportSlots: M Name: Nm"
        # 若返回 HandlerErrorResponse（空 squad）→：
        #   #error3 Invalid factionTemplateId 0 for faction 0
        #   #error3 Invalid squad team received
        #
        # SquadResponse proto:
        #   f1 = squadId (int64, runtime id)
        #   f2 = ActionGameSquadDefinition
        #       f1 = missionSquadId (=team 号, runtime)
        #       f2 = squadTemplateId (WarDatabase/SquadTemplate 行 id)
        #       f3 = factionTemplateId (1=US 2=GE 3=SU)
        #       f4 = name (string)
        #       f5 = ActionGameSquadSlotDefinition playerSlots (repeated)
        #       f6 = ActionGameSquadSlotDefinition supportSlots (repeated)
        #       f8 = ownerCharacterId
        #       f9 = squadleaderCharacterId
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
            for _s in matchmaking.PMS_TO_SESSION.values():
                if mission_session_id_req in (_s.get('pms_id'),
                                              _s.get('battle_session_id')):
                    mm_sess = _s
                    break
        if mm_sess is None and requested_squad_id:
            for _s in matchmaking.PMS_TO_SESSION.values():
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
            for _s in matchmaking.PMS_TO_SESSION.values():
                if cm and int(_s.get('mission_id', 0) or 0) != cm:
                    continue
                mm_sess = _s          # dict 保序 → 本局最新进局会话
            if mm_sess is None and not cm:
                for _s in matchmaking.PMS_TO_SESSION.values():
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
            matchmaking._next_match_id(matchmaking._MM_SQUAD_ID)

        # ★ 局内小队定义：成局时 matchmaking 按分组登记（真实小队用大厅
        #   传入的队名/队长；单人匹配自动凑队，rank 最高任队长）。
        #   队内全体成员共享 squad_id（PlayerSettings f16 同源），每人占一个
        #   playerSlot（index 与各自 PlayerSettings f17 squadSlotIndex 对齐）。
        #   会话不在登记表（理论不可达，如重启残留）→ 回退单人单槽定义。
        mission_id = int(mm_sess.get('mission_id', 0) or 0) if mm_sess else 0
        sq_info = matchmaking._MM_MISSION_SQUADS.get(
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
        det = matchmaking._MM_MISSION_DETAILS.get(missionid)
        if not det and missionid:
            mid2 = matchmaking.PMS_TO_MISSION.get(missionid)
            if mid2:
                det = matchmaking._MM_MISSION_DETAILS.get(mid2)
        if not det:
            # ★ 实测专用服本请求 proto 为空（无 field1，日志全量可证）：
            #   按启动顺序 FIFO 认领最早未握手实例，取其 preset/地图/阵营，
            #   否则回退默认 preset=10(Mill) → 专用服加载错图 + inst['ready']
            #   永不置位 → 120s 握手超时 → 客户端卡匹配 UI。
            claimed = matchmaking.claim_pending_combat_instance()
            if claimed:
                missionid = claimed
                det = matchmaking._MM_MISSION_DETAILS.get(missionid)
        if det and 'preset' in det:
            # ★ 阵营字段无默认值：_faction_pair 校验（缺失/非法/同侧 →
            #   None + ERROR 日志），不静默用 2/GE、3/SU 默认阵营配出生侧
            #   （曾致错阵营判负+观战）。校验不过按"无详情"走 legacy 回退。
            #   角色建号即写 faction、_start_match 恒写攻防字段，正常路径
            #   恒合法；此处仅拦结构损坏。
            fp = matchmaking._faction_pair(det)
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
            weather_condition = matchmaking.snow_f22_condition(det)
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
            preset_id = MISSION_PRESET_ID
            defender_faction = BF_DEFENDER_FACTION_ID
            attacker_faction = 3
            enable_bots = True
            enable_sa_bots = False
            weather_tod = 1
            weather_condition = 0
            timeofday = None
        # f14 资源从本局资源池渲染（与匹配界面 f5 / 轮询 f2 同源同值）；
        # mission_id = Assault 出兵线随机激活种子（与大厅简报 f6 / 轮询
        # f3/f5 必须同值，wardb.assault_ap_decision 详见）
        resp_proto = _msg_response_battlefield_settings(
            preset_id, defender_faction, attacker_faction, enable_bots,
            enable_sa_bots=enable_sa_bots,
            weather_time_of_day=weather_tod, weather_condition=weather_condition,
            timeofday=timeofday,
            counts_by_faction=matchmaking.mission_resource_pool(missionid),
            mission_id=missionid)
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
            ready_mid = matchmaking.PMS_TO_MISSION.get(missionid, missionid)
        matchmaking.notify_combat_ready(ready_mid or None)
        return True
    return False
