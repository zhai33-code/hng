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
    _load_preset, _rank_from_combat_xp, battlefield_pos,
    character_resource_category, is_general_character,
)
import mm_pool
import force_pool
import war_campaign
import mm_config

from matchmaking import (
    AI_MISSIONPRESET, _arm_ai_fill, _assault_team_nodes, _cancel_squad_queue,
    _briefing_layout, _char_vehicle_cats, _cleanup_queued_player,
    _enqueue_squad, _find_joinable_mission, _find_player_active_mission,
    _init_war_mission_resources, _join_existing_mission,
    _msg_briefing_f4_war, _msg_briefing_f5, _msg_briefing_f6,
    _msg_briefing_f7,
    _msg_mission_details_response, _msg_player_matchmaking,
    _msg_solo_squad_member_session, _msg_solo_squad_session, _multi_first,
    _push_kvcs, _push_kvcs_delete,
    _resolve_mapfilter_gamemode, _resolve_quickplay_gamemode,
    _revive_own_membership,
    _squad_eligible_members, _try_form_all, _zulu_activated, _zulu_now,
    mission_resource_pool, register_war_battle_prep,
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


async def _specified_mission_join_error(req_mission_id, faction, char_cat=0):
    """指定战局加入（JoinMM f2：好友列表"跟随进入战斗"/战争图点战斗）的
    前置校验：返回错误码（无法加入）或 None（可加入）。

    官服语义（客户端 MatchmakingService.requestJoinMatchMakingCallback
    反推）：指定局无法加入时回错误码——43 mission_not_found / 62
    player_faction_invalid 客户端会清跟随状态并弹"无法找到战斗/无效
    阵营"提示；227 mission_faction_full 文案"该战斗人数已满"。
    此前服务端一律回 ok 再静默处理：指定局撮合不中（士兵阵营不在该局
    攻防两方/局已结束/满员）时玩家被普通撮合排进另一场新局——
    2026-09-08 用户报"好友列表加入好友对局排到新对局"根因。
    """
    mid = int(req_mission_id)
    wrec = None
    try:
        wrec = await state.STORE.get_mission_record(mid)
    except Exception as e:
        _log(f'[DB] get_mission_record({mid}) failed: {e}')
    if wrec is not None and int(wrec.get('battle_id') or 0):
        # war 会战 record：state 2/3（已结算）→ 43；阵营非交战双方 → 62
        # （原 1b-1 war 分支静默 return：回 ok 但无事发生，玩家永卡匹配中）
        if int(wrec.get('state', 0) or 0) in (2, 3):
            return 43
        w_atk = int(wrec.get('attacker_faction', 0) or 0)
        w_dfd = int(wrec.get('defender_faction', 0) or 0)
        if w_atk > 0 and w_dfd > 0 and faction not in (w_atk, w_dfd):
            return 62
        # state=1（战斗中）但局内会话已清（战斗服收尾中）→ 局实际已不可
        # 加入（原 war 分支同样静默忽略 → 玩家永卡匹配中）
        if (int(wrec.get('state', 0) or 0) == 1
                and not (_MM_MISSION_DETAILS.get(mid)
                         and mid in COMBAT_INSTANCES)):
            return 43
        return None
    # 普通撮合局（内存口径，与 friendinfo 推送/late join 同源）
    det = _MM_MISSION_DETAILS.get(mid)
    if (det is None or 'preset' not in det
            or COMBAT_INSTANCES.get(mid) is None):
        return 43                       # 局不存在 / 已结束
    atk = int(det.get('attacker_faction', 0) or 0)
    dfd = int(det.get('defender_faction', 0) or 0)
    if atk <= 0 or dfd <= 0 or faction not in (atk, dfd):
        return 62                       # 士兵阵营不在该局攻防两方
    # ★ 兵种准入（官方 GamemodesMediator 矩阵，见 mm_config.MODE_CHAR_CATS）：
    #   好友跟随不得绕过 tile 限制（坦克兵跟不进侦察局）。类别 0（未知
    #   模板）不拦。与阵营不符同款 62（客户端清跟随态）。
    allowed = mm_config.mode_char_cats(int(det.get('gamemode', 0) or 0))
    if allowed and int(char_cat or 0) and int(char_cat) not in allowed:
        return 62
    max_p = int(mm_config.gamemode_cfg(
        int(det.get('gamemode', 0) or 0)).get('max_players', 18))
    if len(det.get('players') or ()) >= max_p:
        return 227                      # 局满
    return None


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

    # 2026-09-15 兜底：据点详情开窗瞬间客户端先把 warBattleId 设成 battle
    # 行 id（MissionDetailsMediator.onBattleChanged dispatch
    # selectedBattle.id），详情响应返回后才覆盖成真实 missionId；玩家在
    # 响应前抢先点"加入战局"时 missionId 实为 battleId。查不到 mission
    # record 时按 battle id 解析回真实 missionId；仍查不到则维持原值，
    # 走原有 43 校验。DB 异常时不做纠正，原逻辑兜底。
    if req_mission_id:
        try:
            _rec = await state.STORE.get_mission_record(req_mission_id)
        except Exception as e:
            _log(f'[DB] get_mission_record({req_mission_id}) failed: {e}')
        else:
            if _rec is None:
                try:
                    _brec = await state.STORE.get_mission_by_battle(
                        req_mission_id)
                except Exception as e:
                    _log(f'[DB] get_mission_by_battle({req_mission_id}) '
                         f'failed: {e}')
                    _brec = None
                if _brec is not None:
                    _log(f'      JoinMatchMaking: missionId={req_mission_id} '
                         f'实为 battleId（详情响应前抢先点击），纠正为 '
                         f'missionId={int(_brec["id"])}')
                    req_mission_id = int(_brec['id'])

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
    # 士兵资源类别（1 步兵/2 飞行员/4 伞兵/5 坦克兵/6 侦察兵，未知 0）：
    # quickplay/mapfilter 解析、指定战局校验与直排门禁全需要
    char_cat = character_resource_category(int(char_info.get('template', 0) or 0))

    # ★ 快速匹配模式解析（11 按阵营 / 12 按士兵 / 14 全模式，客户端
    #   GamemodesMediator）：虚拟模式号绝不能入池——按模式分池会把它与
    #   所有具体模式池隔离（每个 quickplay 玩家孤立成局、det.gamemode
    #   挂虚拟号把后续 late join/好友跟随全挡掉；12/14 还会走 DEFAULT
    #   兜底成局在训练 preset 10 Mill 图上）。入池前解析为具体模式：
    #   并入等待人数最多的具体模式池（全空随机通用模式）；1c 的跨模式
    #   搜局（_find_joinable_mission(None,...)）独立于此。小队/单人
    #   路径都经此解析。指定战局（f2）/战争路由（f4）尊重原值不解析。
    _is_quickplay = (gamemode_id in mm_config.QUICKPLAY_GAMEMODES
                     and not req_mission_id and not war_flag)
    if _is_quickplay:
        resolved = _resolve_quickplay_gamemode(char_cat)
        _log(f'[MM] quickplay: gamemode {gamemode_id} -> {resolved} '
             f'(merge into busiest pool / random fallback)')
        gamemode_id = resolved

    # ★ 地图筛选模式解析（客户端高级筛选 To Battle 恒发 13，见
    #   mm_config.MAPFILTER_GAMEMODE 注释）：同 quickplay 虚拟模式号绝不
    #   入池——此前 13 落 DEFAULT（min=2/ai_fill=30）→ 用地图筛选勾训练图
    #   匹人机模式被 30s 超时补位卡慢（2026-09-10 修复）。按勾选 preset
    #   的 missionCategoryId 解析为具体模式并收窄选图（防混合勾选把训练图
    #   多数票进 PvP 局）。小队整队/单人/late-join 全经此解析（早于
    #   squad 路径与 1c 搜局）；指定战局（f2）尊重指定不解析。
    if (gamemode_id == mm_config.MAPFILTER_GAMEMODE
            and not req_mission_id):
        resolved, keep_presets = _resolve_mapfilter_gamemode(staged_presets, char_cat)
        if resolved is not None:
            _log(f'[MM] mapfilter: gamemode 13 -> {resolved} '
                 f'(presets {list(staged_presets)} -> {list(keep_presets)})')
            gamemode_id = resolved
            staged_presets = keep_presets

    # ★ 指定战局加入（f2 missionId：好友列表"跟随进入战斗"/战争图点战斗）
    #   前置校验：阵营不符/局不存在/已结束/满员必须回错误码（43/62/227，
    #   客户端 requestJoinMatchMakingCallback 会 sendErrorMessage 并对
    #   43/62 清跟随态），绝不能回 ok 后落入普通撮合——否则士兵阵营不在
    #   该局攻防两方时玩家被排进另一场新局（2026-09-08 好友跟随失效根因）。
    if req_mission_id:
        join_err = await _specified_mission_join_error(
            req_mission_id, faction, char_cat)
        if join_err is not None:
            resp = build_response(cmd_id, 'JoinMatchMakingResponse',
                                  pb_int32(1, join_err))
            writer.write(resp)
            await writer.drain()
            _log(f'S->C cmd={cmd_id:<4} JoinMatchMakingResponse '
                 f'(response={join_err}: specified mission {req_mission_id} '
                 f'not joinable, faction={faction})')
            return

    # ★ 兵种准入门禁（官方 GamemodesMediator.as:572-626 矩阵镜像，见
    #   mm_config.MODE_CHAR_CATS）：坦克掌控(3)只进坦克兵、侦查掌控(8)只进
    #   侦察兵等。quickplay/mapfilter/指定战局路径在解析/校验时已各自执法，
    #   此处兜底直排（玩家手动点 tile 排队）路径。指定战局（f2）时
    #   gamemode_id 是客户端参数而非目标局实际模式，权威校验在
    #   _specified_mission_join_error（按 det.gamemode），跳过此处。
    #   类别 0（未知模板）不拦，与将军拦截同款拒绝（response=0）。
    allowed = (mm_config.mode_char_cats(gamemode_id)
               if not req_mission_id else None)
    if allowed and char_cat and int(char_cat) not in allowed:
        resp = build_response(cmd_id, 'JoinMatchMakingResponse', pb_int32(1, 0))
        writer.write(resp)
        await writer.drain()
        _log(f'S->C cmd={cmd_id:<4} JoinMatchMakingResponse '
             f'(rejected: char {char_id} cat {char_cat} not allowed in '
             f'gamemode {gamemode_id} (allowed {allowed}))')
        return

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
                # 准备期门控登记：到点（started_at+准备期）才成局
                await register_war_battle_prep(w_bid)
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
    # 入池（成局建池查缺补齐 + 等待期中途加入过滤共用）；char_cat=角色
    # 自身兵种类别（伞兵=4 等）一并保底，缺类别进局引擎建小队失败判负
    entry = mm_pool.QueueEntry(
        player_id=pid, char_id=int(char_id), faction=faction,
        gamemode_id=gamemode_id, presets=staged_presets,
        char_cat=character_resource_category(template),
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
    joinable = _find_joinable_mission(
        None if _is_quickplay else gamemode_id, staged_presets, faction,
        only_mission=req_mission_id or None,
        vehicle_cats=entry.vehicle_cats, char_cat=entry.char_cat)
    if joinable is not None:
        await _join_existing_mission(pid, joinable[0], entry, ctx)
        return
    if req_mission_id:
        # 前置校验已过但撮合瞬间失效（满员/局结束竞态）：绝不回退普通撮合
        # 排进新局（JoinMM 已回 ok 无法二次回错，保持匹配中由玩家取消）。
        _log(f'[MM] specified mission {req_mission_id} vanished before '
             f'join (pid={pid}) — no fallback to normal matchmaking pool')
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
    # f5/f6 mapEntityId/TypeId：war 局优先 battle 行口径（城战=城市 bf/1、
    # 野外=补给线 id/3，det['entity_id']——修复野外遭遇简报城市名取段
    # 起点城市的问题 §三.5 B1/B2）；缺省回退 record.map_entity_id/1
    m += pb_int64(5, int(details.get('entity_id')
                         or details.get('map_entity_id')
                         or MISSION_MAP_ENTITY_ID))
    m += pb_int64(6, int(details.get('entity_type') or 1))   # mapEntityTypeId
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
    # f11 activationTimeStamp：war 局 = battle 行激活时刻（单一事实源，
    # war_pending det['activation']）；普通局回退 zulu（_zulu_activated
    # 回溯口径）。客户端"已激活时长" = serverTime − f11（修复刚成局显示
    # "已启动于 1 小时"）
    m += pb_string(11, details.get('activation') or zulu)  # activationTimeStamp
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
    # 布局/锚点/f4(war)/f5/f6/f7 与单人版共用（matchmaking 简报共用段，
    # 2026-09-15 抽取）；布局与战斗侧 _accesspoint_layout/f12 同源同值
    L = _briefing_layout(mission_id, gamemap_id, attacker, defender)
    pos = battlefield_pos(int(details.get('map_entity_id') or 0)
                         or MISSION_MAP_ENTITY_ID)
    if pos is None:
        # battlefield 行不在 wardb（无 WarDatabase 环境 / 异常 map_entity_id）
        # → 回退原点，f8/f9 坐标仅驱动简报战场标记位置
        pos = (0.0, 0.0)

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
    # f4 突击队：war_pending 优先发 battle roster 真实 AT（f2=template_id、
    # f4=ownerPlayerId 真实属主——官服 war 局简报 AT 属主非 0）；已成局/
    # staged 局走 force_pool 名单（无池回退官方锚点 ×2）。war 局逐 AT
    # 挂真实激活线（共用 _msg_briefing_f4_war）；守方挂 O 锚（Brno 官服
    # 参考图：守军红星在 O 区不在边线）；非 war 局维持旧锚口径
    war_ats = details.get('war_at_rows') or {}
    if war_ats:
        for node in war_ats.get(attacker, []):
            _lane = force_pool.at_lane_by_id(
                mission_id, int(node.get('id') or 0))
            m += pb_message(4, pb_int64(1, rfid)
                            + pb_int64(2, int(node.get('template_id') or 0))
                            + pb_int64(3, _lane or L['atk_anchor'])
                            + pb_int64(4, int(node.get('owner_player_id')
                                              or 0)))
        for node in war_ats.get(defender, []):
            m += pb_message(4, pb_int64(1, afid)
                            + pb_int64(2, int(node.get('template_id') or 0))
                            + pb_int64(3, L['dfn_ap_f4'])
                            + pb_int64(4, int(node.get('owner_player_id')
                                              or 0)))
    elif L['war'] and L['assault']:
        # 无 war_at_rows（已成局 det）：按分线名单逐 AT 挂线（同单人版）
        m += _msg_briefing_f4_war(mission_id, attacker, defender,
                                  L['atk_anchor'], L['dfn_ap_f4'])
    else:
        defender_logo_ap = (ASSAULT_DEFENDER_LOGO_AP if L['assault']
                            else L['dfn_pt'])
        for cmd_node in _assault_team_nodes(attacker, mission_id):
            m += pb_message(4, pb_int64(1, rfid) + pb_int64(2, cmd_node) +
                            pb_int64(3, L['atk_anchor']) + pb_int64(4, 0))
        for cmd_node in _assault_team_nodes(defender, mission_id):
            m += pb_message(4, pb_int64(1, afid) + pb_int64(2, cmd_node) +
                            pb_int64(3, defender_logo_ap) + pb_int64(4, 0))
    # f5 资源（与单人版共用 _msg_briefing_f5）：从本局资源池渲染（与战斗
    # 内 f2/f14 同源同值，消耗后递减）；war_pending 无池 → 用 battle
    # roster AT 当前编制合计（双方真实兵力，非 FALLBACK 静态锚点）；
    # war 局攻方逐线分行，守方挂红线首条/无红线 O 锚
    pool = mission_resource_pool(mission_id)
    if pool is None and details.get('war_counts'):
        pool = details['war_counts']
    m += _msg_briefing_f5(mission_id, attacker, defender, pool, L)
    # f6 出兵点归属 / f7 据点初始归属（与单人版共用）
    m += _msg_briefing_f6(L, attacker, defender)
    m += _msg_briefing_f7(gamemap_id, L, attacker, defender)
    m += pb_float(8, pos[0])   # posX（battlefield 行坐标）
    m += pb_float(9, pos[1])   # posY
    return m


async def _war_pending_roster(battle_id):
    """war_pending（会战成立、FPS 局未成）简报兵力源：battle roster
    参战 AT 行 + 当前编制合计（resources 按类别求和，含损耗——与成局
    后 war 池初值同口径）。

    成局后（_start_match 建 war 池）不走此路径——f5 与战斗内 f2/f14
    同源读池（含消耗递减）。DB 失败/无 roster 回 None（渲染层维持
    旧静态兜底）。返回 (ats_by_faction, counts_by_faction,
    activation_zulu, entity_id_type)。"""
    from war_campaign.common import (
        _as_resources, battle_activation_ts, battle_map_entity)
    if state.STORE is None or not battle_id:
        return None
    try:
        wb = await state.STORE.get_war_battle(int(battle_id))
        roster = (wb or {}).get('factions') or []
        at_ids = [int(f.get('at_id', 0) or 0) for f in roster]
        nodes = ({int(n['id']): n for n
                  in await state.STORE.get_command_nodes_by_ids(at_ids)}
                 if at_ids else {})
    except Exception as e:
        _log(f'[DB] war_pending roster({battle_id}) failed: {e}')
        return None
    ats = {}
    counts = {}
    for f in roster:
        fac = int(f.get('faction', 0) or 0)
        node = nodes.get(int(f.get('at_id', 0) or 0))
        if not fac or node is None or int(node.get('state') or 0) >= 3:
            continue
        ats.setdefault(fac, []).append(node)
        cc = counts.setdefault(fac, {})
        for cat, v in _as_resources(node.get('resources')).items():
            try:
                cc[int(cat)] = cc.get(int(cat), 0) + int(v)
            except (TypeError, ValueError):
                continue
    return ats, counts, battle_activation_ts(wb), battle_map_entity(wb)


async def _build_war_rec(mission_id, wrec):
    """war_pending 简报的 force_pool rec 构建（2026-09-15）。

    war rec 此前只在成局时构建——重启后旧战斗 / 未开战的 pending 简报
    查详情无 rec → multi 构造器走非 war 兜底（兵力挤一点、无守方红线、
    f5 不分线，用户实测 Tatabánya 简报仍错的原因）。幂等：同 roster
    重建同数据，已成局 rec 被重建亦无害。"""
    if state.STORE is None or not wrec:
        return
    try:
        await _init_war_mission_resources(
            int(mission_id), int(wrec.get('battle_id') or 0),
            int(wrec.get('attacker_faction', 0) or 0),
            int(wrec.get('defender_faction', 0) or 0))
    except Exception as e:
        _log(f'[war] pending rec build(mission={mission_id}) failed: {e}')


def _war_queue_players(battle_id):
    """war_pending 简报"战局玩家列表"（f3 参战方 gamertag 名单）：
    POOL mode-10 桶内本会战已排队玩家。

    成局前（攻守双方未凑齐，try_form_war 不成局）_MM_MISSION_DETAILS
    无 live det，此前 war_pending 恒 players=[] → 玩家点"加入战斗"后
    再开据点详情也看不到自己名字。gamertag 取入池时登记的
    _MM_QUEUES[pid]['ctx']（与 PlayerMatchMaking 行同源）。"""
    players = []
    for e in POOL.war_bucket(battle_id):
        ctx = (_MM_QUEUES.get(int(e.player_id)) or {}).get('ctx') or {}
        players.append({'player_id': int(e.player_id),
                        'gamertag': ctx.get('gamertag')
                        or f'Player{int(e.player_id)}',
                        'char_id': int(e.char_id),
                        'faction': int(e.faction)})
    return players


def _war_battle_players(battle_id, live_players=None):
    """会战简报"战局玩家名单"全量口径：已进局 FPS（live det players）
    + 排队中（war 桶）并集，按 player_id 去重（live 优先——成局后
    _join_existing_mission 会把玩家从队列移进 det['players']，两源
    不会长期重叠，但成局/掉线窗口期可能瞬时出现于两侧）。"""
    players = list(live_players or [])
    seen = {int(p.get('player_id', 0) or 0) for p in players}
    for p in _war_queue_players(battle_id):
        if p['player_id'] not in seen:
            players.append(p)
    return players


async def _handle_mission_details(sess, writer, cmd_id, proto):
    """GetMissionDetailsRequest：返回 GetMissionDetailsResponse。

    匹配池成局（details 含 'players'）走多人版；旧结构回退 AI 单人版。"""
    f = pb_parse(proto)
    mid = int(f.get(1, 0))
    bid = int(f.get(2, 0))
    details = _MM_MISSION_DETAILS.get(mid)
    if details is not None and int(details.get('battle_id', 0) or 0):
        # war 会战 live 局：排队中未进局的玩家并入简报名单（copy 渲染，
        # 不改 live det 本体；f9 pcount 口径随之含排队人数=全部参与者）
        details = dict(details)
        details['players'] = _war_battle_players(
            int(details['battle_id']), details.get('players'))
    if details is None and mid == 0 and bid > 0:
        # ★ 战争图城市点击链（2026-09-08 修复）：有会战的城市点击 →
        # GetMissionDetailsRequest{f1=0, f2=battleId}（HandleWarBattleCommand
        # 恒发 missionId=0）。此前只读 f1 → 恒查不到 → 回 43 → 客户端
        # MissionDetailsWindow 秒关（用户报"点不开城市详情"）。
        # 按 battle_id 反查 record 构造 war_pending det（走 multi 渲染）。
        try:
            wrec = await state.STORE.get_mission_by_battle(bid)
        except Exception as e:
            _log(f'[DB] get_mission_by_battle({bid}) failed: {e}')
            wrec = None
        if wrec is not None:
            # ★ 修复 1（docs/war-battle-details-missionid-bug.md）：回填
            #   真实 missionId——此前 mid 恒 0 → MissionInfo.f1=0 →
            #   客户端 warBattleId=0 → 开战按钮 BASE_STATE/WAR_BATTLE_
            #   UNAVAILABLE（现象 2），且成局后 live det 永远查不到。
            mid = int(wrec['id'])
            live = _MM_MISSION_DETAILS.get(mid)
            if live is not None:
                # 局已开打：复用运行中详情（真实 players 名单/pcount）；
                # 排队中未进局玩家并入名单（copy 渲染，不改 live det）
                details = dict(live)
                details['players'] = _war_battle_players(
                    int(bid), live.get('players'))
            else:
                details = {'battle_id': int(bid),
                           'preset': int(wrec.get('preset', 0) or 0),
                           'map_entity_id': int(wrec.get('map_entity_id', 0) or 0),
                           'attacker_faction':
                               int(wrec.get('attacker_faction', 0) or 0),
                           'defender_faction':
                               int(wrec.get('defender_faction', 0) or 0),
                           'war_pending': True,
                           'players': _war_battle_players(int(bid))}
                _roster = await _war_pending_roster(int(bid))
                if _roster:
                    (details['war_at_rows'], details['war_counts'],
                     details['activation'], _ent) = _roster
                    if _ent and _ent[0]:
                        details['entity_id'], details['entity_type'] = _ent
            await _build_war_rec(mid, wrec)
    if details is None:
        # war 会战局回退（M2）：局尚未成（无人加入，_MM_MISSION_DETAILS
        # 未登记）但任务列表已展示 record 行 → 客户端点行拉详情。按 DB
        # record 构造 war_pending det（走 multi 渲染；players = 该会战
        # war 桶排队真人名单——2026-09-14 实装排队玩家进详情名单；此前
        # 恒空名单是官服成局前观感的旧口径。曾漏 'players' 键走 AI 分支
        # KeyError 'player_id' → HandlerErrorResponse）；非 war record
        # （battle_id=0）维持 43。
        wrec = None
        try:
            wrec = await state.STORE.get_mission_record(mid)
        except Exception as e:
            _log(f'[DB] get_mission_record({mid}) failed: {e}')
        if wrec is not None and int(wrec.get('battle_id') or 0):
            mid = int(wrec['id'])          # 回填真实 missionId（修复 1）
            details = {'battle_id': int(wrec['battle_id']),
                       'preset': int(wrec.get('preset', 0) or 0),
                       'map_entity_id': int(wrec.get('map_entity_id', 0) or 0),
                       'attacker_faction':
                           int(wrec.get('attacker_faction', 0) or 0),
                       'defender_faction':
                           int(wrec.get('defender_faction', 0) or 0),
                       'war_pending': True,
                       'players': _war_battle_players(
                           int(wrec['battle_id']))}
            _roster = await _war_pending_roster(int(wrec['battle_id']))
            if _roster:
                (details['war_at_rows'], details['war_counts'],
                 details['activation'], _ent) = _roster
                if _ent and _ent[0]:
                    details['entity_id'], details['entity_type'] = _ent
            await _build_war_rec(mid, wrec)
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
