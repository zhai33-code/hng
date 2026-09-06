"""匹配请求入口（mm_handler）。

JoinMatchMaking / CancelJoinMatchMaking / GetMissionDetails 三个请求的
处理与响应构建，由 realtest_server.py dispatch 分发调用
（handle_request 返回 False 表示未处理，交由后续 handler）。
从 matchmaking.py 拆出（2026-09-06 重构，纯移动行为不变）。
"""
import uuid

from protocol import (build_response, pb_parse, pb_parse_multi, pb_bool,
                      pb_float, pb_int32, pb_int64, pb_message, pb_string)
from state import PLAYER_SELECTED_CHAR
import state
from wardb import (
    _capturepoint_ap_map, _load_objective_capturepoints, _load_preset,
    _missiontype_for_gamemap, _rank_from_combat_xp, assault_ap_decision,
    battlefield_pos, is_general_character,
)
import mm_pool
import force_pool
import war_campaign

from matchmaking import (
    AI_MISSIONPRESET, _arm_ai_fill, _assault_team_nodes, _cancel_squad_queue,
    _char_vehicle_cats, _cleanup_queued_player, _enqueue_squad,
    _find_joinable_mission, _find_player_active_mission,
    _join_existing_mission, _mission_accesspoints, _mission_capturepoints,
    _msg_mission_details_response, _msg_player_matchmaking,
    _msg_solo_squad_member_session, _msg_solo_squad_session, _multi_first,
    _pool_counts, _push_kvcs, _push_kvcs_delete, _revive_own_membership,
    _squad_eligible_members, _try_form_all, _zulu_activated, _zulu_now,
    mission_resource_pool,
)
from mm_state import (
    MISSION_HOSTING_CENTER_ID, MISSION_MAP_ENTITY_ID,
    POOL, _AI_FILL_TASKS, _MATCH_PMM_IDS, _MM_METRICS_SESSION_ID,
    _MM_MISSION_DETAILS, _MM_PMM_ID, _MM_QUEUES, _MM_SQUAD_ID,
    _MM_SOLO_SMS_ID, _SOLO_SQUADS, _log, _next_match_id,
)
from combat_instances import COMBAT_INSTANCES
from mm_factions import (
    ASSAULT_DEFENDER_LOGO_AP, FACTION_ROW_IDS, _faction_pair,
    _msg_faction_rows,
)


async def _handle_join(sess, writer, cmd_id, proto):
    """JoinMatchMakingRequest：响应 ok + 入匹配池（gamemode 分池）+ 推匹配中状态。

    f5 stagedMissionPresetList（repeated）为玩家勾选的地图 preset（空=不限）；
    f7 gamemodeId 决定进入哪个模式的池。成局条件见 mm_config.GAMEMODES
    （人数凑够 / 超时 AI 补位 / 训练模式单人直开）。
    """
    f = pb_parse_multi(proto)
    char_id = int(_multi_first(f, 1))
    req_mission_id = int(_multi_first(f, 2))
    war_flag = bool(_multi_first(f, 4))
    gamemode_id = int(_multi_first(f, 7))
    quickplay = bool(_multi_first(f, 8))
    staged_presets = tuple(int(v) for v in f.get(5, []) if not
                           isinstance(v, (bytes, bytearray)))
    _log(f'      JoinMatchMaking: charId={char_id}, missionId={req_mission_id}, '
         f'warMissionFlag={war_flag}, gamemodeId={gamemode_id}, '
         f'quickplay={quickplay}, presets={list(staged_presets)}')

    pid = await sess.ensure_player()
    char_info = None
    if pid is not None:
        # ★ charId=0：客户端有小队会话时 EnterMatchmakingCommand 传
        #   int64.zero（官方由服务端按小队成员行取角色）。不解析则
        #   join 被拒 → 真实小队/单人隐式小队的整队匹配永远起不来。
        #   解析顺序：DB 小队成员行（含换角色，SetSquadMemberStatus 已
        #   更新）→ solo 隐式小队注册表 → 无会话则维持 0（拒绝兜底）。
        if not char_id:
            try:
                squad = await state.STORE.get_squad_for_player(int(pid))
                if squad is not None:
                    for m in await state.STORE.list_squad_members(squad['id']):
                        if int(m['player_id']) == int(pid):
                            char_id = int(m['character_id'])
                            break
            except Exception as e:
                _log(f'[DB] squad char resolve failed: {e}')
            if not char_id:
                solo = _SOLO_SQUADS.get(int(pid))
                if solo is not None:
                    char_id = int(solo.get('char_id', 0) or 0)
            if char_id:
                _log(f'      JoinMatchMaking: charId=0 resolved from squad '
                     f'session -> char {char_id}')
        try:
            for ch in await state.STORE.list_characters(pid):
                if int(ch['id']) == int(char_id):
                    char_info = ch
                    break
        except Exception as e:
            _log(f'[DB] list_characters failed: {e}')

    if char_info is None:
        resp = build_response(cmd_id, 'JoinMatchMakingResponse', pb_int32(1, 0))
        writer.write(resp)
        await writer.drain()
        _log(f'S->C cmd={cmd_id:<4} JoinMatchMakingResponse (rejected: char {char_id} not found)')
        return

    # ★ 将军不参战（官方设计：General 只在大地图指挥 AssaultTeam，无战斗
    #   实体；其 actionid 与伞兵同为 7，不挡会混进战斗当伞兵）。
    #   排队/late-join/rejoin/快速游戏全经此处，单人路径一处拦截即覆盖。
    #   与"角色不存在"同款拒绝（response=0）。
    if is_general_character(int(char_info.get('template', 0) or 0)):
        resp = build_response(cmd_id, 'JoinMatchMakingResponse', pb_int32(1, 0))
        writer.write(resp)
        await writer.drain()
        _log(f'S->C cmd={cmd_id:<4} JoinMatchMakingResponse '
             f'(rejected: char {char_id} is general — generals cannot enter combat)')
        return

    # 记录当前选中士兵（squad 入队规则需要：邀请时解析被邀者角色），
    # 同步落库（player_selected_char，重启回放）
    PLAYER_SELECTED_CHAR[int(pid)] = int(char_info['id'])
    try:
        await state.STORE.set_player_selected_char(int(pid), int(char_info['id']))
    except Exception as e:
        _log(f'[DB] set_player_selected_char failed: {e}')

    faction = int(char_info['faction'])

    resp = build_response(cmd_id, 'JoinMatchMakingResponse', pb_int32(1, 1))
    writer.write(resp)
    await writer.drain()
    _log(f'S->C cmd={cmd_id:<4} JoinMatchMakingResponse (response=1 ok, charId={char_id})')

    view_cmd = sess.playerview_cmd

    # ★ faction 表 3 行（US/GE/SU）必须推送：客户端 FactionTable(xmlTag='faction')
    #   为空时 GetMissionDetailsResponse 处理中 getFromId 返回 null → #1009 卡死进对局。
    #   客户端在 JoinMM ok 后会立即发第一次 GetMissionDetailsRequest，必须先于它推送。
    if view_cmd is not None:
        # f6/f7 带当前 underdog 倍率（#1 人数较少阵营经验/战资加成）
        _ud = await war_campaign.current_underdog_mults(state.STORE)
        for row in _msg_faction_rows(_ud):
            await _push_kvcs(writer, view_cmd, b'faction', row)
        _log(f'S->C cmd={view_cmd:<4} KeyValueChangeSet (faction ×3 rows) ★FactionTable fill')

    # ★ 整队入池：玩家在真实小队（DB squads）且队内有其他在线 InLobby/Ready
    #   成员 → 全队作为原子单位一起排队（group_key=squad_id 不拆队），
    #   成局后整队同 mission 部署。官方语义：队长激活小队全队进匹配，
    #   这里任何成员点匹配都带全队（角色取各自小队成员行）。
    squad, sq_members = await _squad_eligible_members(pid)
    if squad is not None:
        # ★ 小队成员战斗中掉线重连：自己 InBattle 被 eligible 过滤 →
        #   sq_members 为空，原路径 _enqueue_squad 直接 return（静默
        #   卡死到局结束：JoinMM ok 后既不入池、不推 PMM、也不回局）。
        #   改查存活原局直接回局（与 solo 1c-0 rejoin 同路径，含去重
        #   清痕与真实小队归队）。仅未显式指定战局时生效（指定时尊重指定）。
        if not sq_members and not req_mission_id:
            rejoin_mid = _find_player_active_mission(pid)
            if rejoin_mid:
                _log(f'rejoin: squad player {pid} -> original '
                     f'mission={rejoin_mid} (InBattle session recovered)')
                rejoin_ctx = {
                    'writer': writer, 'view_cmd': view_cmd,
                    'gamertag': sess.username or 'Player',
                    'char_name': char_info['name'],
                    'rank': _rank_from_combat_xp(
                        int(char_info.get('combat_xp', 0) or 0)),
                    'faction': faction, 'char_id': int(char_id),
                    'quickplay': quickplay,
                    'req_mission_id': req_mission_id,
                    'char_template': int(char_info.get('template', 0) or 0),
                    'metrics_uid': str(uuid.uuid4()),
                    'metrics_session_id':
                        _next_match_id(_MM_METRICS_SESSION_ID),
                    'squad_db_id': int(squad['id'])}
                rejoin_entry = mm_pool.QueueEntry(
                    player_id=pid, char_id=int(char_id), faction=faction,
                    gamemode_id=gamemode_id, presets=staged_presets)
                await _join_existing_mission(pid, rejoin_mid, rejoin_entry,
                                             rejoin_ctx)
                return
            # ★ 无存活战局可回：status 卡 InBattle（错过战局收尾的
            #   reset_real_squad_to_lobby，如战斗中崩溃掉线 + 局已结束）
            #   → 官服语义"重连即回小队 InLobby"：置回 2 并重查 eligible。
            #   否则 20:52-20:58 式静默卡死（9 次 join 无任何入池/推送）。
            if await _revive_own_membership(squad['id'], pid):
                _log(f'mm: squad player {pid} status stuck InBattle -> '
                     f'InLobby revived (squad={squad["id"]})')
                squad, sq_members = await _squad_eligible_members(pid)
        await _enqueue_squad(squad, sq_members, gamemode_id,
                             staged_presets, req_mission_id)
        if not sq_members:
            _log(f'[MM] squad {squad["id"]} enqueue skipped: no eligible '
                 f'members (pid={pid})')
        return

    # 重复排队：删旧 PMM 行（行 id 即将更换，防客户端表残留）
    old_q = _MM_QUEUES.get(pid)
    if old_q is not None and not old_q.get('mission_id'):
        _cleanup_queued_player(pid)
        POOL.remove(pid)
        if view_cmd is not None:
            try:
                await _push_kvcs_delete(writer, view_cmd, b'PlayerMatchMaking',
                                        old_q['pmm_id'])
            except Exception:
                pass

    # 排队上下文（成局时 _start_match 取用；id 与 PMS/PlayerSettings 同源）
    gamertag = sess.username or 'Player'
    char_name = char_info['name']
    rank = _rank_from_combat_xp(int(char_info.get('combat_xp', 0) or 0))
    template = int(char_info.get('template', 0) or 0)
    pmm_id = _next_match_id(_MM_PMM_ID)
    metrics_uid = str(uuid.uuid4())
    metrics_session_id = _next_match_id(_MM_METRICS_SESSION_ID)
    ctx = {'writer': writer, 'view_cmd': view_cmd, 'gamertag': gamertag,
           'char_name': char_name, 'rank': rank, 'faction': faction,
           'char_id': int(char_id), 'quickplay': quickplay,
           'req_mission_id': req_mission_id,
           'char_template': template,
           'metrics_uid': metrics_uid,
           'metrics_session_id': metrics_session_id}
    _MM_QUEUES[pid] = {'pmm_id': pmm_id, 'mission_id': 0,
                       'gamemode': gamemode_id, 'ctx': ctx}
    _MATCH_PMM_IDS[pid] = pmm_id

    # 1. 推送 PlayerMatchMaking（客户端进入"匹配中"界面；f18/f19 池内实时人数）
    if view_cmd is not None:
        await _push_kvcs(writer, view_cmd, b'PlayerMatchMaking',
                         _msg_player_matchmaking(
                             pmm_id, char_id, faction, req_mission_id,
                             _zulu_now(),
                             POOL.waiting_count(gamemode_id) + 1,
                             POOL.waiting_count() + 1))
        _log(f'S->C cmd={view_cmd:<4} KeyValueChangeSet (PlayerMatchMaking '
             f'id={pmm_id}, gamemode={gamemode_id})')

    # 1b. ★ 单人隐式小队行（官方 solo_kvcs_dump2.txt S2C#4：与 PlayerMatchMaking
    #     同变更集推 SquadSession + SquadMemberSession(Ready)）。缺这两行的后果：
    #     匹配状态机 ActivateSquadCommand 置 squadModel.isActive=true 而
    #     squadSession=null → 小队界面无头像/槽位数据；abandon 后
    #     StatusMediator.onLobbySignalUpdates 读 squadSession.missionId 抛
    #     TypeError #1009 → UI 永卡 "processing"。同一玩家复用行 id（官方行为）。
    solo = _SOLO_SQUADS.get(pid)
    if solo is None:
        solo = {'squad_row': _next_match_id(_MM_SQUAD_ID),
                'member_row': _next_match_id(_MM_SOLO_SMS_ID),
                'mission_id': 0, 'entered': 0}
        _SOLO_SQUADS[pid] = solo
    solo.update({'mission_id': 0, 'faction': faction, 'template': template,
                 'char_id': int(char_id), 'gamertag': gamertag,
                 'char_name': char_name, 'rank': rank,
                 'metrics_session_id': metrics_session_id})
    if view_cmd is not None:
        await _push_kvcs(writer, view_cmd, b'SquadSession',
                         _msg_solo_squad_session(solo['squad_row'], pid, template,
                                                 faction, 0, solo.get('entered', 0)))
        await _push_kvcs(writer, view_cmd, b'SquadMemberSession',
                         await _msg_solo_squad_member_session(
                             solo['member_row'], solo['squad_row'], pid, gamertag,
                             int(char_id), char_name, rank, 3,
                             metrics_session_id, faction))
        _log(f'S->C cmd={view_cmd:<4} KeyValueChangeSet '
             f'(SquadSession id={solo["squad_row"]} + SquadMemberSession '
             f'id={solo["member_row"]} status=3 Ready ★solo squad)')

    # 1b-1. ★ war 会战路由（M2）：指定战局（战争地图点进行中战斗/任务
    #        列表 war 行）且该局是 war 会战 record（battle_id>0）→ 不走
    #        普通撮合/rejoin 搜索：
    #        - state 2/3（已结算）或玩家阵营非交战双方 → 忽略（JoinMM
    #          已回 ok；战报行删除后任务列表自然消失。绝不能入普通池：
    #          war 桶无 battle_id 永不成局，普通撮合还会凑出同阵营局）
    #        - 局内会话存活（_MM_MISSION_DETAILS + COMBAT_INSTANCES）→
    #          同阵营 late join 直接回局（绕 JOIN_WINDOW：官服 war 会战
    #          开打后随时可加入）
    #        - state 0（成局待开局）→ 带 battle_id 入 war 桶（try_form_war
    #          按 battle_id 分桶，攻守双方阵营各 ≥1 即成局）
    if req_mission_id:
        wrec = None
        try:
            wrec = await state.STORE.get_mission_record(int(req_mission_id))
        except Exception as e:
            _log(f'[DB] get_mission_record({req_mission_id}) failed: {e}')
        if wrec is not None and int(wrec.get('battle_id') or 0):
            w_mid = int(req_mission_id)
            w_state = int(wrec.get('state', 0) or 0)
            w_atk = int(wrec.get('attacker_faction', 0) or 0)
            w_dfd = int(wrec.get('defender_faction', 0) or 0)
            w_bid = int(wrec['battle_id'])
            if w_state in (2, 3) or faction not in (w_atk, w_dfd):
                _log(f'[war] JoinMM mission={w_mid} not joinable: '
                     f'state={w_state}, faction={faction} '
                     f'(battle={w_bid}, atk={w_atk}, dfd={w_dfd})')
                return
            if _MM_MISSION_DETAILS.get(w_mid) and w_mid in COMBAT_INSTANCES:
                _log(f'[war] JoinMM mission={w_mid} live -> late join '
                     f'(faction={faction}, battle={w_bid})')
                entry = mm_pool.QueueEntry(
                    player_id=pid, char_id=int(char_id), faction=faction,
                    gamemode_id=10, presets=staged_presets)
                await _join_existing_mission(pid, w_mid, entry, ctx)
                return
            if w_state == 0:
                _log(f'[war] JoinMM mission={w_mid} -> war queue '
                     f'(battle={w_bid}, faction={faction})')
                _MM_QUEUES[pid]['gamemode'] = 10
                entry = mm_pool.QueueEntry(
                    player_id=pid, char_id=int(char_id), faction=faction,
                    gamemode_id=10, presets=staged_presets, battle_id=w_bid)
                POOL.enqueue(entry)
                await _try_form_all()
                return
            # state=1 但局内会话已清（战斗服收尾中）：不可加入，忽略
            _log(f'[war] JoinMM mission={w_mid} state={w_state} no live '
                 f'session -> ignored')
            return

    # 1c. 等待期中途加入：已有同模式、选图兼容、未满员的战局仍处于开局
    #     等待窗口内（mm_config.JOIN_WINDOW）→ 直接进该局（不另开新局）；
    #     玩家指定了战局（f2 missionId，战争地图点进行中战斗）则只考虑该局。
    #     找不到/超窗/满员/阵营不符 → 正常入池撮合。
    # 官服口径"进局必有装备载具之一"：已装备载具资源类别并集随条目
    # 入池（成局建池查缺补齐 + 等待期中途加入过滤共用）
    entry = mm_pool.QueueEntry(
        player_id=pid, char_id=int(char_id), faction=faction,
        gamemode_id=gamemode_id, presets=staged_presets,
        vehicle_cats=await _char_vehicle_cats(pid, char_id))
    # 1c-0. ★ rejoin 只能回原局：玩家在存活战局中掉线重连（drop_player
    #       不清已成局会话）→ 直接回原局，不走 late join 搜索（否则按
    #       创建顺序会撞上别的局）、不另开新局。仅在未显式指定战局
    #       （req_mission_id 空）时生效——玩家指定战局时尊重指定。
    if not req_mission_id:
        rejoin_mid = _find_player_active_mission(pid)
        if rejoin_mid:
            _log(f'rejoin: player {pid} -> original mission={rejoin_mid} '
                 f'(session recovered, skip late-join search)')
            await _join_existing_mission(pid, rejoin_mid, entry, ctx)
            return
    joinable = _find_joinable_mission(gamemode_id, staged_presets, faction,
                                      only_mission=req_mission_id or None,
                                      vehicle_cats=entry.vehicle_cats)
    if joinable is not None:
        await _join_existing_mission(pid, joinable[0], entry, ctx)
        return

    # 2. 入池 + 撮合（训练模式 min=1 立即成组；凑够 min_players 成组；
    #    不足则 _arm_ai_fill 超时 AI 补位）
    POOL.enqueue(entry)
    await _try_form_all()
    if _MM_QUEUES.get(pid, {}).get('mission_id', 0) == 0:
        # 仍在池中 → 启动/重启该玩家的超时 AI 补位计时
        _arm_ai_fill(pid, gamemode_id)


async def _handle_cancel(sess, writer, cmd_id, proto):
    """CancelJoinMatchMakingRequest：响应 ok + 出池 + delete 匹配行（取消排队）。"""
    resp = build_response(cmd_id, 'CancelJoinMatchMakingResponse', pb_int32(1, 1))
    writer.write(resp)
    await writer.drain()
    _log(f'S->C cmd={cmd_id:<4} CancelJoinMatchMakingResponse (response=1 ok)')

    pid = await sess.ensure_player()
    q = _MM_QUEUES.pop(pid, None) if pid else None
    # ★ 整队取消：任何成员取消 → 全队出池回 InLobby（响应已回 ok）
    squad_db_id = int((q or {}).get('ctx', {}).get('squad_db_id') or 0) \
        if q else 0
    if squad_db_id:
        # 本人的队列条目上面已 pop；其余成员由 _cancel_squad_queue 处理
        POOL.remove(pid)
        _MATCH_PMM_IDS.pop(pid, None)
        t = _AI_FILL_TASKS.pop(pid, None)
        if t is not None:
            t.cancel()
        if q and q.get('pmm_id') is not None and sess.playerview_cmd is not None:
            await _push_kvcs_delete(writer, sess.playerview_cmd,
                                    b'PlayerMatchMaking', q['pmm_id'])
            _log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet '
                 f'delete (PlayerMatchMaking id={q["pmm_id"]})')
        await _cancel_squad_queue(squad_db_id)
        await _try_form_all()
        return
    if pid:
        POOL.remove(pid)                       # 出池（未入池时 no-op）
        _MATCH_PMM_IDS.pop(pid, None)
        t = _AI_FILL_TASKS.pop(pid, None)
        if t is not None:
            t.cancel()
    if q and sess.playerview_cmd is not None:
        await _push_kvcs_delete(writer, sess.playerview_cmd, b'PlayerMatchMaking',
                                q['pmm_id'])
        _log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet '
             f'delete (PlayerMatchMaking id={q["pmm_id"]})')
        # 取消匹配：solo squad 回 InLobby（missionId=0），否则客户端
        # SquadSession.f17 残留战局 id → onLobbySignalUpdates 判 LOCKED_IN_COMBAT
        solo = _SOLO_SQUADS.get(pid)
        if solo is not None:
            solo['mission_id'] = 0
            await _push_kvcs(writer, sess.playerview_cmd, b'SquadSession',
                             _msg_solo_squad_session(
                                 solo['squad_row'], pid, solo['template'],
                                 solo['faction'], 0, solo.get('entered', 0)))
            await _push_kvcs(writer, sess.playerview_cmd, b'SquadMemberSession',
                             await _msg_solo_squad_member_session(
                                 solo['member_row'], solo['squad_row'], pid,
                                 solo['gamertag'], solo['char_id'], solo['char_name'],
                                 solo['rank'], 2, solo.get('metrics_session_id', 0),
                                 solo['faction']))
            _log(f'S->C cmd={sess.playerview_cmd:<4} KeyValueChangeSet '
                 f'(SquadSession mission=0 + SquadMemberSession status=2 InLobby)')
    # 取消后：成局可能性变化（如 1v1 队友撤了）+ 剩余排队人数刷新
    await _try_form_all()


def _msg_mission_info_multi(mission_id, details, zulu):
    """多人局 MissionInfo proto（GetMissionDetailsResponse f2 / MissionList 共构）。

    与旧 AI 局 _msg_mission_info 的差异：f3=成局 preset、f5=动态 mapEntityId
    （gamemap → battlefield 行）、f9=玩家数（AI 补位 +1 与单人 AI 局实测一致）。
    """
    players = details.get('players') or []
    creator = int(players[0]['player_id']) if players else 0
    pcount = len(players) + (1 if details.get('ai_fill') else 0)
    # war 会战局（M2）：f2=真实 battleId、f7 isAutogenerated=0（官服口径）
    w_battle = int(details.get('battle_id') or 0)
    m = b''
    m += pb_int64(1, int(mission_id))              # missionId
    m += pb_int64(2, w_battle)                     # battleId
    m += pb_int64(3, int(details.get('preset') or AI_MISSIONPRESET))
    m += pb_int64(5, int(details.get('map_entity_id') or MISSION_MAP_ENTITY_ID))
    m += pb_int64(6, 1)                            # mapEntityTypeId
    m += pb_int32(7, 0 if w_battle else 1)         # isAutogenerated
    # f8 defenderFactionId：客户端 UI 攻防判定（factionIdToUse ==
    # defenderFactionId → 显示 defend）。★曾误填 creator playerId（巨大
    # id 恒不等于玩家阵营）→ 简报恒显示进攻方，与 BattleFieldSettings f3
    # （正确阵营）不一致 = "UI 防守、进游戏进攻"错位。
    # ★ 无默认值：字段缺失/非法由 _handle_get_mission_details 入口拒绝
    #   （_faction_pair 校验），能走到这里说明字段已验证合法。
    m += pb_int64(8, int(details['defender_faction']))
    m += pb_int32(9, pcount)                       # playerCount
    m += pb_int64(10, MISSION_HOSTING_CENTER_ID)
    m += pb_string(11, zulu)                       # activationTimeStamp
    m += pb_int32(12, int(details.get('weather_tod', 1)))    # weatherTimeOfDay
    m += pb_int32(13, int(details.get('weather_condition', 0)))  # weatherCondition
    return m


def _msg_mission_details_response_multi(mission_id, details, zulu):
    """多人局 GetMissionDetailsResponse（结构沿用 AI 局实测值，参战方动态化）。

    - f3 参战方：真人按 faction 分组列 gamertag；防守方无真人（ai_fill / 单侧）
      时保留无成员名单的 AI 方条目
    - f4/f5 突击队/资源：本局 force_pool 抽取的 AT 名单与资源池（类别为
      库值），突击队模板按【参战阵营】查 commandnodetemplate，
      "我方" = 进攻方（_split_factions 成局随机），AI 方 = 防守方
    - 出兵点/据点模板按本局 gamemap 查表；f8/f9 战局位置 = mapEntityId 对应
      battlefield 行坐标（简报战场名与地图标记一致）
    """
    players = details.get('players') or []
    # ★ 无默认值：入口（_handle_get_mission_details）已用 _faction_pair
    #   校验过攻防阵营，非法 det 到不了这里；行 id 直接索引（不回退）。
    attacker, defender = _faction_pair(details)
    rfid = FACTION_ROW_IDS[attacker]
    afid = FACTION_ROW_IDS[defender]
    preset = int(details.get('preset') or 0) or AI_MISSIONPRESET
    gamemap_id = int(_load_preset(preset).get(3, 0) or 0) or 13
    aps = _mission_accesspoints(gamemap_id)   # 不截断（5 线图全参与）
    ours_ap, theirs_ap = aps[0], aps[1]
    # ★按 missiontype 分支（assault-map-spawn-investigation.md §9.2）：
    # Assault 出兵线随机激活（wardb.assault_ap_decision，按 mission_id 播种，
    # 与战斗侧 _accesspoint_layout/BattleFieldSettings f12 同源同值）：
    # 守方 = O 锚 1234567890（引擎虚拟 AP：战斗侧 f12 收编 + f14 挂资源
    # → 守方小队/坦克部署中心 O 区）、进攻方随机 2~3 条边线、★未激活线
    # 不开启。Encounter（Mill 等无连线图）维持原有交替方案不改动
    # （Mill 锚点 8=攻/15=守）。
    assault = _missiontype_for_gamemap(gamemap_id) == 1
    owner_map = None
    d_ap = None
    atk_aps = ()
    if assault:
        d_ap, a_aps, _inactive = assault_ap_decision(gamemap_id, mission_id)
        theirs_ap = d_ap   # = O 锚 1234567890（f5 守方资源锚，同战斗 f14）
        ours_ap = min(a_aps) if a_aps else aps[0]
        atk_aps = set(int(t) for t in a_aps)
        owner_map = {int(d_ap): defender}       # O 锚不在边线 aps → f6 不下发
        for t in a_aps:
            owner_map[int(t)] = attacker
    pos = battlefield_pos(int(details.get('map_entity_id') or 0)
                          or MISSION_MAP_ENTITY_ID)

    by_faction = {}
    for p in players:
        by_faction.setdefault(int(p['faction']), []).append(p['gamertag'])

    m = b''
    m += pb_int32(1, 0)  # 他服私服实测 = 0（非 ResponseCodes.ok=1）
    m += pb_message(2, _msg_mission_info_multi(mission_id, details, zulu))
    # f3 参战方：进攻方（真人名单）+ 防守方（有真人列名单，否则 AI 方无名单）
    atk = pb_int64(1, rfid)
    for tag in by_faction.get(attacker, []):
        atk += pb_message(2, pb_string(1, tag))
    m += pb_message(3, atk)
    dtag = by_faction.get(defender, [])
    dfd = pb_int64(1, afid)
    for tag in dtag:
        dfd += pb_message(2, pb_string(1, tag))
    m += pb_message(3, dfd)
    # f4 突击队：本局 AT 抽取名单跨阵营翻译（无池回退官方锚点 ×2）；
    # ★守方锚 ASSAULT_DEFENDER_LOGO_AP（客户端次选路径画地图中心=O 区域，
    # 见单人版注释；战斗侧 home_ap 独立锚 O 锚 1234567890）
    for cmd_node in _assault_team_nodes(attacker, mission_id):
        m += pb_message(4, pb_int64(1, rfid) + pb_int64(2, cmd_node) +
                        pb_int64(3, ours_ap) + pb_int64(4, 0))
    defender_logo_ap = (ASSAULT_DEFENDER_LOGO_AP
                        if assault else theirs_ap)
    for cmd_node in _assault_team_nodes(defender, mission_id):
        m += pb_message(4, pb_int64(1, afid) + pb_int64(2, cmd_node) +
                        pb_int64(3, defender_logo_ap) + pb_int64(4, 0))
    # f5 资源：从本局资源池渲染（与战斗内 f2/f14 同源同值，消耗后递减）；
    # 每方每类一条挂该方出兵点（客户端跨点求和 → 显示即池剩余量）
    pool = mission_resource_pool(mission_id)
    atk_counts = _pool_counts(pool, attacker)
    dfd_counts = _pool_counts(pool, defender)
    for res_cat, res_air in force_pool.categories(counts_by_faction=pool):
        m += pb_message(5, pb_int64(1, rfid) + pb_int64(2, res_cat) +
                        pb_int64(3, ours_ap) +
                        pb_int64(4, atk_counts.get(res_cat, 0)) +
                        pb_bool(5, res_air))
    for res_cat, res_air in force_pool.categories(counts_by_faction=pool):
        m += pb_message(5, pb_int64(1, afid) + pb_int64(2, res_cat) +
                        pb_int64(3, theirs_ap) +
                        pb_int64(4, dfd_counts.get(res_cat, 0)) +
                        pb_bool(5, res_air))
    # f6 出兵点归属：Encounter 首个=进攻方、其余交替（Mill 实测 8=进攻,
    # 15=防守；f2 为 factionTemplateId）；Assault 展示层只发★攻方激活线
    # （官方简报守方仅拥有中心 O 点——由 f7 涂守方色 + f4 logo 画中心；
    # 守方 O 锚为虚拟点不在边线 aps → 不进 f6）——与战斗侧
    # _accesspoint_layout 同 mission_id 同源（战斗 f12 含守方 O 锚）
    if owner_map is not None:
        for ap in aps:
            if int(ap) not in owner_map:
                continue        # 未激活线：不开启不下发
            m += pb_message(6, pb_int64(1, ap) +
                            pb_int64(2, owner_map[int(ap)]))
    else:
        for i, ap in enumerate(aps):
            m += pb_message(6, pb_int64(1, ap) +
                            pb_int64(2, defender if i % 2 else attacker))
    # f7 据点初始归属：Encounter 全 0 中立（他服私服实测 {3:120, 4:0}）；
    # Assault 只发★O 点（守方）+ 攻方激活线入口据点（挂攻方 AP，攻方色）
    # ——客户端 auto 激活线判定见单人版注释：守方线/未激活线据点不发
    # → 图上不画该线；O 点涂守方色=官方观感
    obj_cps = set(_load_objective_capturepoints(gamemap_id)) if assault else set()
    if assault:
        cp_ap = _capturepoint_ap_map(gamemap_id)
        for cp in _mission_capturepoints(gamemap_id):
            if cp in obj_cps:
                cp_owner = defender
            elif cp_ap.get(cp) in atk_aps:
                cp_owner = attacker
            else:
                continue        # 守方线/未激活线/线中段：展示层不下发
            m += pb_message(7, pb_int32(3, cp) + pb_int32(4, cp_owner))
    else:
        for cp in _mission_capturepoints(gamemap_id):
            m += pb_message(7, pb_int32(3, cp) + pb_int32(4, 0))
    m += pb_float(8, pos[0])   # posX（battlefield 行坐标）
    m += pb_float(9, pos[1])   # posY
    return m


async def _handle_mission_details(sess, writer, cmd_id, proto):
    """GetMissionDetailsRequest：返回 GetMissionDetailsResponse。

    匹配池成局（details 含 'players'）走多人版；旧结构回退 AI 单人版。"""
    f = pb_parse(proto)
    mid = int(f.get(1, 0))
    details = _MM_MISSION_DETAILS.get(mid)
    if details is None:
        # war 会战局回退（M2）：局尚未成（无人加入，_MM_MISSION_DETAILS
        # 未登记）但任务列表已展示 record 行 → 客户端点行拉详情。按 DB
        # record 构造无 'players' 键的最小 det（走 multi 渲染 = 双方空
        # 名单，官服成局前观感）；非 war record（battle_id=0）维持 43。
        wrec = None
        try:
            wrec = await state.STORE.get_mission_record(mid)
        except Exception as e:
            _log(f'[DB] get_mission_record({mid}) failed: {e}')
        if wrec is not None and int(wrec.get('battle_id') or 0):
            details = {'battle_id': int(wrec['battle_id']),
                       'preset': int(wrec.get('preset', 0) or 0),
                       'map_entity_id': int(wrec.get('map_entity_id', 0) or 0),
                       'attacker_faction':
                           int(wrec.get('attacker_faction', 0) or 0),
                       'defender_faction':
                           int(wrec.get('defender_faction', 0) or 0),
                       'war_pending': True}
    if details is None:
        resp = build_response(cmd_id, 'GetMissionDetailsResponse', pb_int32(1, 43))
        writer.write(resp)
        await writer.drain()
        _log(f'S->C cmd={cmd_id:<4} GetMissionDetailsResponse (mission_not_found, '
             f'mission={mid})')
        return
    if 'players' in details:
        # ★ 阵营字段门禁：损坏的 det 直接拒绝（mission_invalid），
        #   不用默认阵营渲染简报（错阵营 f8/f3 → UI 攻防错位 / #1009）。
        if _faction_pair(details) is None:
            resp = build_response(cmd_id, 'GetMissionDetailsResponse',
                                  pb_int32(1, 43))
            writer.write(resp)
            await writer.drain()
            _log(f'S->C cmd={cmd_id:<4} GetMissionDetailsResponse '
                 f'(mission_invalid: faction fields, mission={mid})')
            return
        resp_proto = _msg_mission_details_response_multi(mid, details, _zulu_activated())
        mode = 'multi'
    else:
        # 角色建号即写 faction，无默认值必要（无 faction 的角色不存在）
        resp_proto = _msg_mission_details_response(
            mid, details['player_id'], details['gamertag'], _zulu_activated(),
            int(details['faction']))
        mode = 'AI mode'
    resp = build_response(cmd_id, 'GetMissionDetailsResponse', resp_proto)
    writer.write(resp)
    await writer.drain()
    _log(f'S->C cmd={cmd_id:<4} GetMissionDetailsResponse ({mode}, '
         f'mission={mid}, {len(resp_proto)}B)')


async def handle_request(sess, writer, cmd_id, req_type, proto):
    """realtest_server dispatch 分发入口。"""
    if req_type == 'JoinMatchMakingRequest':
        await _handle_join(sess, writer, cmd_id, proto)
    elif req_type == 'CancelJoinMatchMakingRequest':
        await _handle_cancel(sess, writer, cmd_id, proto)
    elif req_type == 'GetMissionDetailsRequest':
        await _handle_mission_details(sess, writer, cmd_id, proto)
    else:
        return False
    return True
