"""Request coverage test: 全量 SWF 请求类对照服务器注册 handler。

静态检查（不依赖运行中的服务器/DB）：
1. 读 decompiled/_as3_classlist.txt 提取客户端请求候选
   （HnG_States.*/netsysmessages.* 中以 Request/request 结尾、非 *Response 的类）
2. 覆盖集 = handlers._EXACT（精确注册）+ matchmaking/game_server 委托名单
   + subscribe*/unsubscribe* 前缀规则
3. 白名单 = 明确不实现的请求（U6 战争地图/统计族跳过、无响应 proto 定义族、
   响应类误报）
4. 断言：未覆盖请求 ⊆ 白名单（出现白名单外未覆盖即 FAIL，
   防止新增客户端请求被遗漏后卡请求队列）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# AS3 类清单（逆向产物，仅开发机存在；打包环境缺失时本测试跳过）
_CLASSLIST = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    '..', 'decompiled', '_as3_classlist.txt')
_CLASSLIST = os.path.normpath(_CLASSLIST)

# matchmaking.handle_request / game_server.handle_request 委托处理
# （不经 handlers._EXACT，见 realtest_server.dispatch）
_DELEGATED = {
    'JoinMatchMakingRequest', 'CancelJoinMatchMakingRequest',
    'GetMissionDetailsRequest',
    'GetPlayerSettings', 'RequestReadCharacterStats', 'RequestEarnings',
    'GetPlayerUidByNickRequest', 'SquadRequest', 'RequestBattleFieldSettings',
}

# 白名单：明确不实现（回 HandlerErrorResponse 解锁队列，无功能影响）
_WHITELIST = {
    # --- U6 战争地图/统计（用户指示跳过；warmap.py/stats.py 暂缓）---
    'CreateCommandNodeRequest', 'DeployCommandNodeRequest',
    'RetreatCommandNodeRequest', 'RenameCommandnodeRequest',
    'UpgradeCommandNodeRequest', 'SetCommandNodeEquipmentRequest',
    'ReorderSupplyQueueRequest', 'LeaveSupplyQueueRequest',
    'QueryFactionsOnBattlefieldRequest', 'MonitorLoadRequest',
    'CompleteAssaultTeamRankRequest', 'GetAssaultTeamStatsRequest',
    'AddCharacterStatisticsRequest', 'AddAssaultTeamStatisticsRequest',
    'resupply_request', 'war_create_request',
    # stats 族（RequestReadRibbonValue/RequestXPs 等命名不含 Request 结尾的
    # 由类清单 Request 结尾过滤自然排除，此处为结尾匹配到的）
    # --- 无响应 proto 定义（ZServer 未注册，回 HandlerErrorResponse）---
    'AdminToolQueryFastRequest', 'DebugGetWarCatalogueRequest',
    'DebugGetPlayerRequest', 'DebugAddCharacterRibbonXPRequest',
}


def _load_request_candidates():
    """类清单 → 请求候选集合。"""
    reqs = set()
    with open(_CLASSLIST, encoding='utf-8-sig') as fp:
        for line in fp:
            parts = line.strip().rsplit(' ', 1)
            if len(parts) != 2:
                continue
            fqcn = parts[0]
            if not (fqcn.startswith('HnG_States.')
                    or fqcn.startswith('netsysmessages.')):
                continue
            name = fqcn.split('.', 1)[1]
            if name.endswith('Response'):
                continue
            if name.endswith('Request') or name.endswith('request'):
                reqs.add(name)
    return reqs


def _load_coverage():
    """服务器覆盖集 = _EXACT + 委托 + subscribe/unsubscribe 前缀。"""
    import handlers
    covered = set(handlers._EXACT) | set(_DELEGATED)
    return covered, handlers


def main():
    if not os.path.exists(_CLASSLIST):
        print('[skip] AS3 类清单不存在（打包环境无逆向产物），跳过请求覆盖测试')
        return 0
    candidates = _load_request_candidates()
    covered, handlers = _load_coverage()
    # subscribe*/unsubscribe* 走前缀 handler（views.handle_subscribe/unsubscribe）
    effective = {c for c in candidates
                 if c in covered
                 or c.lower().startswith('subscribe')
                 or c.lower().startswith('unsubscribe')}
    uncovered = sorted(candidates - effective)

    unexpected = sorted(set(uncovered) - _WHITELIST)
    stale = sorted(_WHITELIST - set(uncovered))

    print(f'total request candidates : {len(candidates)}')
    print(f'covered (exact/delegate)  : {len(covered)}')
    print(f'handled candidates        : {len(effective)}')
    print(f'uncovered                 : {len(uncovered)} '
          f'(whitelisted: {len(set(uncovered) & _WHITELIST)})')

    if uncovered:
        print('\n-- uncovered requests --')
        for name in uncovered:
            tag = ' [whitelist]' if name in _WHITELIST else ' [UNEXPECTED]'
            print(f'  {name}{tag}')

    ok = True
    if unexpected:
        ok = False
        print(f'\nFAIL: {len(unexpected)} uncovered requests NOT in whitelist:')
        for name in unexpected:
            print(f'  {name}')
    if stale:
        print(f'\nNOTE: {len(stale)} whitelisted names no longer appear as '
              f'uncovered (implemented or renamed): {stale}')
    print('\nRESULT:', 'PASS' if ok else 'FAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
