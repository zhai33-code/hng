"""statistics_client.dll 统计服务器（TCP 15115）。

战斗内勋带升级弹窗（AddUnlockedRibbon）依赖 player.dll 的会话勋带数据
（character 对象 a2[7]），该数据**仅**由 statistics_client.dll 的 GetTotals
响应初始化（逆向结论见 tmp/ida_out/RIBBON_POPUP_INVESTIGATION.md）：
  statistics_client.dll --TCP--> 统计服务器 GetTotals 响应
    -> sub_18038D550 解析 -> character[7] = set_ribbon_state_from_server(...)
    -> sub_180390E00 战斗中累加 XP -> 升级 -> Flash AddUnlockedRibbon 弹窗

连接握手（comm_start sub_18000A050 逆向，2026-08-26 补）：
  C->S: [u32 LE 14]["GIVEITTOMENOW\0"]（长度前缀 + 14 字节 = 13 字符 + NUL）
  S->C: ["YOUGOTITTODAY\0"]（14 字节裸串，无前缀）+ [u32 会话值]（客户端存 a1+104）
  握手失败客户端静默重连 10 次（每次 1s sleep）→ ~30s 后报
  "#error3 _GetTotalsFromStatisticsServerNotify as == 0, ErrorCode: 2"

协议（statistics_client.dll 逆向，vt6_GetTotals sub_18000ADF0 实锤）：
  帧:  [u32 LE len][u32 LE crc32][payload]，len = payload 长度 + 4，
       crc32 = raw CRC32(len 的 4 字节小端 || payload 字节)  <- 覆盖长度字段本身
              init=0、无 final XOR（sub_18000B3E0 发送 / vt6_GetTotals 接收均如此）
              ≠ Python zlib.crc32()（标准 CRC32，init=0xFFFFFFFF 且 final XOR）
  C->S payload：GetTotals(5)/CreateSession(3) 等为明文（send_buffer 直写），
                周期上报 type 6 经 msg_envelope(deflate) 压缩 → 收到后先尝试解压。
  S->C payload 为 zlib 压缩（客户端 reader_ctor inflateInit 解压）。
  消息（payload 解压后首字节）:
    3 CreateSession   [u8 3][i64 sessionId]           无响应
    4 FinalizeSession [u8 4][i64 sessionId]           无响应
    5 GetTotals       见 _parse_get_totals            同步阻塞读响应
    6 统计上报（压缩）                                  战斗服周期 flush → 解析入库
    7 CreateRandomStat [u8 7][i64 ?]                  同步读响应（同 GetTotals 格式）
    8 bye                                              关连接

  GetTotals 请求: [u8 5][u32 n][i64xn charIds][i64 ?][u32 a5][a5x32B][u32 a7][a7x16B]
  GetTotals 响应（解压后）:
    [i64 sessionId][u32 numChars]
    每角色: [i64 charId][u32 numStats]
            + numStats  x 28B: [u32 statId][i64][i64][i64]
            + [u32 numRibbons]
            + numRibbonsx 28B: [i64 ribbonTemplateId][f64 currentValue]
                                [u32 currentLevel][f64 nextValue]
    （numRibbons 必须在 stats 数组之后！客户端 sub_18000ADF0 读取顺序为
     charId→numStats→stats→numRibbons→ribbons。2026-08-27 双人进战局
     崩溃事故：旧布局 numStats/numRibbons 相邻导致流错位 4 字节，
     第二个角色的 numStats 从勋带浮点字节读出 ~10 亿 → saturated_mul
     4GB 分配失败 → 分配器 __debugbreak fail-fast（exit 0xC0000409）。
     单角色查询恰好不崩（错位后垃圾条目被客户端白名单校验跳过），
     双角色同请求必崩。等级为 1 基，客户端存 level-1）

客户端注入（launch_hng.ps1）：/R "statserver=127.0.0.1" /R "statserverport=15115"

type 6 统计上报（player.dll WriteToStatisticsServer sub_18038DA00 +
statistics_client.dll VT.Write sub_18000BB60 逆向，2026-08-27）：
  payload = zlib 流（envelope 类型字节在压缩流内；兼容 [0x06][zlib] 与
  明文两种变体）。解压后：
    [u8 6][i64 sessionId][u32 numChars]
    每角色: [i64 charId][u32 numStats]
            + numStats  x 28B: [u32 statId][i64 ctx1][i64 ctx2][i64 value]
            + [u32 numRibbons]
            + numRibbonsx 28B: [i64 ribbonId][f64 value][u32 level][f64 ?]
  statId 为 statserver wire id（sub_18038E540 映射），value 为增量
  （上报后计数器清零 → 服务端累加）。ctx1/ctx2 语义按 statId 段位
  （send_stats sub_18038DA00 逆向）：武器段 512-767 ctx1=武器 id；
  载具段 768-1023 ctx1=载具 id；武×载段 1024-1279 ctx1=武器 id、
  ctx2 低 16 位=载具 id；模式段 1280-1535 ctx1=0、gamemode id 在
  ctx2>>16。勋带项仅记录日志（character_ribbon_xp 已由战斗结算
  路径写入，双写会重复计数）。
"""
import asyncio
import struct
import zlib

from state import STORE, log
from wardb import _RIBBON_LEVEL_XP, _ribbon_level_from_xp

STATS_PORT = 15115

# ---------------------------------------------------------------------------
# wire statId → 统计名（player.dll sub_18038E540 + statvar_map sub_180389A60
# 双表合并逆向）。武器段 513-537 / 载具段 769-785 / 模式段 1281-1299 /
# 武器×载具段 1010-1033 / 升级段 1793-1794 / 小队指令段 2049-2056。
# ---------------------------------------------------------------------------
WIRE_STAT_NAMES = {
    11: 'PlayerScore', 50: 'PlayerXp', 51: 'PlayerCredits', 52: 'PlayerWarfunds',
    3: 'PlayerKills', 60: 'PlayerKillAssists', 2: 'PlayerDeaths',
    14: 'PlayerSuicides', 12: 'PlayerSpawns', 1: 'PlayerBestMultiKill',
    10: 'PlayerSaviorKills', 9: 'PlayerRunDistance', 20: 'PlayerWalkDistance',
    13: 'PlayerSprintDistance', 16: 'PlayerCrouchDistance',
    17: 'PlayerTimeCrouched', 15: 'PlayerSwimDistance', 18: 'PlayerTimeOnFoot',
    19: 'PlayerTimePlayed', 53: 'PlayerActiveTime', 8: 'PlayerParachuteTime',
    5: 'PlayerParachuteFreefallDist', 6: 'PlayerParachuteFreefallTime',
    7: 'PlayerParachuteSpawns', 4: 'PlayerParachuteEscape',
    21: 'PlayerSessionsPlayed', 54: 'PlayerSessionsCompleted',
    34: 'PlayerDamageDealt', 35: 'PlayerParachuteJumpsSuccessfull',
    36: 'PlayerFreefallJumps', 37: 'PlayerTeamKills', 38: 'PlayerRevengeKills',
    41: 'PlayerTeamScoreboard_3_4_5', 40: 'PlayerTeamScoreboard_2',
    39: 'PlayerTeamScoreboard_1', 42: 'PlayerScoreboard_1',
    43: 'PlayerBattlesWithoutDying', 44: 'PlayerNrOfMultiKill',
    22: 'PlayerNrOfBattleInfantryKillXArmor', 23: 'PlayerNrOfBattleArmorKillXArmor',
    24: 'PlayerNrOfBattlePlaneKillXArmor', 25: 'PlayerNrOfBattleInfantryKillXInfantry',
    26: 'PlayerNrOfBattleArmorKillXInfantry', 27: 'PlayerNrOfBattlePlaneKillXInfantry',
    28: 'PlayerNrOfBattleXSaviorKills', 29: 'PlayerNrOfBattleXRevengeKills',
    30: 'PlayerNrOfBattleXHeadshots', 31: 'PlayerNrOfBattleXKnifeKills',
    32: 'PlayerNrOfBattleXGrenadeKills', 33: 'PlayerNrOfBattleXPistolKills',
    45: 'PlayerNrOfBattleWith1000ShotsFired',
    46: 'PlayerNrOfBattleWithKillSameEnemy4Times',
    56: 'SquadLeaderTimePlayed', 57: 'SquadMemberTimePlayed', 58: 'SquadXp',
    62: 'SquadVetXp', 61: 'ArmorProximityXp', 63: 'SupportSpawns',
    # 59：player.dll 全天高频上报（远端 2026-08-31 日志 79 次，值 1-5）但
    # sub_18038E540 名表未还原出名字 —— 先给可回读的占位名（原走 x59/ 死键
    # 永不回发），逆向确认后连 WIRE_STAT_NAMES 与 DB 键一起改名。
    59: 'Unknown59',
    64: 'DestroyFriendlyExplosive', 65: 'DestroyEnemyExplosive',
    259: 'PlayerSpecKills', 260: 'PlayerSpecDamageDealt',
    257: 'PlayerSpecRevengeKill', 258: 'PlayerSpecKillsInARow',
    # 载具段（ctx1 = 载具模板 id）
    770: 'VehicleDestroyed', 782: 'VehicleDestroyedAssist', 773: 'VehicleLost',
    771: 'VehicleDistDriven', 772: 'VehicleDistDrivenWPassanger',
    778: 'VehicleDistSailed', 779: 'VehicleDistSailedWPassanger',
    774: 'VehicleTimeSpent', 769: 'VehicleDamageDealt', 775: 'VehicleUnlocked',
    776: 'VehicleMobileSpawnOwnerUsed', 777: 'VehicleMobileSpawnUserUsed',
    780: 'VehicleSupportGunnerDamageDealt', 781: 'VehicleSupportGunnerDestroyed',
    783: 'VehicleComponentDamaged', 784: 'VehicleComponentDestroyed',
    785: 'VehicleComponentArmorDamaged',
    # 武器段（ctx1 = 武器模板 id）
    519: 'WeaponShotsFired', 514: 'WeaponHeadShots', 515: 'WeaponHits',
    517: 'WeaponKills', 535: 'WeaponKillAssists', 516: 'WeaponKilled',
    520: 'WeaponTimeSpent', 513: 'WeaponPlayerKills', 518: 'WeaponMostHitsInARow',
    521: 'WeaponDamageToPlayers', 522: 'WeaponDamageCloseRange',
    523: 'WeaponDamageMidLowRange', 524: 'WeaponDamageMidHighRange',
    525: 'WeaponDamageFarRange', 526: 'WeaponKillsCloseRange',
    527: 'WeaponKillsMidLowRange', 528: 'WeaponKillsMidHighRange',
    529: 'WeaponKillsFarRange', 531: 'WeaponUseOwner', 532: 'WeaponUseUser',
    534: 'WeaponSupportGunnerShotsFired', 533: 'WeaponSupportGunnerKills',
    536: 'WeaponConsumableOnSelf', 537: 'WeaponConsumableOnOther',
    # 武器×载具段
    1028: 'WeaponVehicleDamage', 1025: 'WeaponVehicleKill',
    1033: 'WeaponVehicleKillAssists', 1029: 'WeaponVehicleUseOwner',
    1030: 'WeaponVehicleUseUser', 1032: 'WeaponVehicleSupportGunnerDamage',
    1010: 'WeaponVehicleSupportGunnerKill',
    # 模式段（ctx1 = gamemode id）
    1282: 'GamemodeCPCaptures', 1281: 'GamemodeCPAssists',
    1284: 'GamemodeCPTotalInfluence', 1283: 'GamemodeCPNotSpotted',
    1295: 'GamemodeCPNeutralize', 1296: 'GamemodeCPNeutralizeTotalInfluence',
    1285: 'GamemodeKillAnEnemyCapturing', 1286: 'GamemodeKilledWhileCapturing',
    1289: 'GamemodeKillAnEnemyWhileCapturing',
    1287: 'GamemodeResourcesCaptured', 1288: 'GamemodeResourcesTotalInfluence',
    1290: 'GamemodeAssaultAttackerWon', 1291: 'GamemodeAssaultDefenderWon',
    1292: 'GamemodeAssaultBattles', 1293: 'GamemodeSkirmishBattlesWon',
    1294: 'GamemodeSkirmishBattles', 1297: 'GamemodeEncounterBattlesWon',
    1298: 'GamemodeEncounterBattles', 1299: 'GamemodeDefenceKills',
    1793: 'VehicleUpgradeOwnerUsed', 1794: 'VehicleUpgradeUserUsed',
    2049: 'SquadOrderAttackKill', 2050: 'SquadOrderAttackCapture',
    2051: 'SquadOrderAttackSuccess', 2052: 'SquadOrderDefendKill',
    2053: 'SquadOrderDefendSuccess', 2054: 'SquadOrderRallyKill',
    2055: 'SquadOrderRallySuccess', 2056: 'SquadOrderClearKill',
}

_WEAPON_RANGE = range(513, 538)
_VEHICLE_RANGE = range(769, 786)
_GAMEMODE_RANGE = range(1281, 1300)
_WEAPONVEHICLE_RANGE = set(range(1010, 1034)) & set(WIRE_STAT_NAMES)


def _stat_db_key(stat_id: int, ctx1: int, ctx2: int) -> str:
    """wire (statId, ctx1, ctx2) → character_stats.stat_key。

    标量统计直接用统计名；带维度的统计把 ctx 编进 key
    （w<id>/v<id>/gm<id>/wv<id>_<id> 前缀），回读端按前缀分组重建
    WeaponStats/VehicleStats/GameModeStats 数组。

    ctx 语义（player.dll send_stats sub_18038DA00 逆向实锤）：
      武器段 512-767   ctx1 = 武器模板 id（32 位，desc[8:10]|desc[24:26]<<16）
      载具段 768-1023  ctx1 = 载具模板 id（desc[8:12]）
      武×载 1024-1279  ctx1 = 武器 id，ctx2 低 16 位 = 载具 id
      模式段 1280-1535 ctx1 = 0，gamemode id 在 ctx2 >> 16（desc[4:6]）
    """
    name = WIRE_STAT_NAMES.get(stat_id)
    if name is None:
        return f'x{stat_id}/{ctx1}/{ctx2}'
    if stat_id in _WEAPON_RANGE:
        return f'w{ctx1 & 0xFFFFFFFF}/{name}'
    if stat_id in _VEHICLE_RANGE:
        return f'v{ctx1 & 0xFFFFFFFF}/{name}'
    if stat_id in _GAMEMODE_RANGE:
        return f'gm{(ctx2 >> 16) & 0xFFFF}/{name}'
    if stat_id in _WEAPONVEHICLE_RANGE:
        return f'wv{ctx1 & 0xFFFFFFFF}_{ctx2 & 0xFFFF}/{name}'
    return name


def _parse_type6(data: bytes):
    """解析 type-6 解压后的载荷（不含首字节 6）。

    返回 (sessionId, [(charId, [(statId, ctx1, ctx2, value), ...],
                        [(ribbonId, value, level, x), ...]), ...])。
    解析失败抛 ValueError。
    """
    off = 0

    def need(n):
        nonlocal off
        if off + n > len(data):
            raise ValueError(f'truncated at {off} (+{n} > {len(data)})')
        chunk = data[off:off + n]
        off += n
        return chunk

    session_id, = struct.unpack('<q', need(8))
    n_chars, = struct.unpack('<I', need(4))
    if n_chars > 4096:
        raise ValueError(f'bad numChars {n_chars}')
    out = []
    for _ in range(n_chars):
        char_id, = struct.unpack('<q', need(8))
        n_stats, = struct.unpack('<I', need(4))
        if n_stats > 65536:
            raise ValueError(f'bad numStats {n_stats} (char {char_id})')
        stats = [struct.unpack('<Iqqq', need(28)) for _ in range(n_stats)]
        n_ribbons, = struct.unpack('<I', need(4))
        if n_ribbons > 65536:
            raise ValueError(f'bad numRibbons {n_ribbons} (char {char_id})')
        ribbons = []
        for _ in range(n_ribbons):
            rid, val, lv, x = struct.unpack('<qdId', need(28))
            ribbons.append((rid, val, lv, x))
        out.append((char_id, stats, ribbons))
    return session_id, out


async def _persist_type6(chars):
    """type-6 统计增量落库（character_stats，标量名 + 维度前缀 key）。"""
    if STORE is None:
        return
    for char_id, stats, ribbons in chars:
        if not stats:
            continue
        try:
            for stat_id, ctx1, ctx2, value in stats:
                if not value:
                    continue
                key = _stat_db_key(stat_id, ctx1, ctx2)
                await STORE.add_stat(int(char_id), key, int(value))
            names = [WIRE_STAT_NAMES.get(s[0]) or f'id{s[0]}' for s in stats[:8]]
            log(f'[stats] type6 char={char_id}: {len(stats)} stats '
                f'({", ".join(f"{n}={v}" for n, v in zip(names, [s[3] for s in stats[:8]]))})')
        except Exception as e:
            log(f'[stats] type6 persist failed (char={char_id}): {e}')
        if ribbons:
            sample = ', '.join(f'r{r[0]}:v{r[1]:.0f}:lv{r[2]}' for r in ribbons[:6])
            log(f'[stats] type6 char={char_id}: {len(ribbons)} ribbons ({sample}) '
                f'- logged only (ribbon_xp written by earnings path)')


# ===== 每局实际用量（结算磨损数据源，2026-09-09）=====
# 背景：战后维护费原按 timeActive 估算发数/spawn 数扣减（combat.
# _apply_equipment_wear），对一发未开的玩家也按估算满额扣费（2026-09-08
# 1000093 三局被按 60/33/38 发手雷回购弹药，实际全天才扔 3 发）。
# 官服按实际消耗计费；type6 统计在结算（AnnounceBattleFieldEarnings）
# 前同秒到达，含每武器模板实际发数（stat 519 WeaponShotsFired）与角色
# 出生数（stat 12 PlayerSpawns），即官服语义的完整数据源。
#
# session_id 关联：type6 session_id = <前缀>×10⁶ + missionId（2026-09-08
# 全天日志实证，如 730433917384180 ↔ earnings f1 missionId=384180），结算
# 侧只有 missionId，故两侧统一按 missionId = session_id % 10⁶ 索引
# （missionId 溢出 10⁶ 时两侧同样回绕，仍能对上）。
_BATTLE_USAGE = {}        # missionId -> {charId: {'shots': {武器模板: 发数}, 'spawns': 出生数}}
_BATTLE_USAGE_KEYS = []   # 插入序（FIFO 淘汰，防未结算局堆积泄漏）
_BATTLE_USAGE_MAX = 128   # 最多缓存局数（正常流程结算即取走）
_BATTLE_USAGE_DONE = {}   # 已 pop 的局（重复结算免空等；FIFO 淘汰防泄漏）


def _accumulate_battle_usage(session_id, chars):
    """type6 增量 → 每局每角色实际用量累积（发数/出生数）。

    同帧去重：载具武器发数会以裸模板与 (载具模板<<16|模板) 两种 ctx1
    各报一次且值相同（char 177 实测 w88/WeaponShotsFired=30 与
    w3801176/WeaponShotsFired=30 并存），按低 16 位武器模板取 max 防
    双计；帧间是增量语义，跨帧累加。"""
    mission_id = int(session_id) % 1_000_000
    if mission_id <= 0:
        return
    usage = _BATTLE_USAGE.setdefault(mission_id, {})
    if mission_id not in _BATTLE_USAGE_KEYS:
        _BATTLE_USAGE_KEYS.append(mission_id)
        while len(_BATTLE_USAGE_KEYS) > _BATTLE_USAGE_MAX:
            _BATTLE_USAGE.pop(_BATTLE_USAGE_KEYS.pop(0), None)
    for char_id, stats, _ribbons in chars:
        u = usage.setdefault(int(char_id), {'shots': {}, 'spawns': 0})
        frame = {}
        for stat_id, ctx1, ctx2, value in stats:
            if not value:
                continue
            if stat_id == 519:              # WeaponShotsFired
                wt = int(ctx1) & 0xFFFF
                frame[wt] = max(frame.get(wt, 0), int(value))
            elif stat_id == 12:             # PlayerSpawns（标量）
                u['spawns'] += int(value)
        for wt, v in frame.items():
            u['shots'][wt] = u['shots'].get(wt, 0) + v


def pop_battle_usage(mission_id):
    """结算取走该局每角色实际用量 {charId: {...}}；无数据返回 None。

    取走即删：重复结算/迟到补报不会对同一局二次计损。"""
    mission_id = int(mission_id or 0) % 1_000_000
    if mission_id not in _BATTLE_USAGE:
        return None
    try:
        _BATTLE_USAGE_KEYS.remove(mission_id)
    except ValueError:
        pass
    _BATTLE_USAGE_DONE[mission_id] = True
    while len(_BATTLE_USAGE_DONE) > _BATTLE_USAGE_MAX:
        _BATTLE_USAGE_DONE.pop(next(iter(_BATTLE_USAGE_DONE)))
    return _BATTLE_USAGE.pop(mission_id)


async def wait_battle_usage(mission_id, timeout=3.0):
    """结算等待该局 type6 用量到达并取走（确保消耗数据到达才计费）。

    type6 帧与结算请求分属不同 TCP 连接、同秒到达（实测先 type6 后
    结算）；若结算先走到磨损环节而用量帧仍在路上/仍在落库，轮询等待
    免得把"数据在路上"误判成"无数据"。已取走过的局（重复结算）与
    超时未到的局返回 None——宁可不扣不多扣。"""
    mission_id = int(mission_id or 0) % 1_000_000
    if mission_id <= 0 or mission_id in _BATTLE_USAGE_DONE:
        return None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if mission_id in _BATTLE_USAGE:
            return pop_battle_usage(mission_id)
        if loop.time() >= deadline:
            return None
        await asyncio.sleep(0.1)


async def process_type6_body(body, peer=''):
    """type6 载荷处理：解压 → 解析 → 累积用量 → 落库。

    顺序铁律：_accumulate_battle_usage 必须先于 _persist_type6——
    累积是纯内存同步操作；落库逐角色逐条 await STORE.add_stat 会反复
    让出事件循环，结算（AnnounceBattleFieldEarnings → 结算磨损）可在
    落库间隙插队，若先落库后累积，结算 pop 时用量还未入账 → 整局磨损
    被误判"无 type6 数据"跳过（2026-09-09 01:40 mission=356460 四角色
    全漏扣的根因）。"""
    if body[:1] == b'\x78':                  # [0x06][zlib] 变体
        try:
            body = zlib.decompress(body)
        except zlib.error as e:
            log(f'[stats] type6 decompress failed: {e}')
            return
    try:
        session_id, chars = _parse_type6(body)
        _accumulate_battle_usage(session_id, chars)   # 先入账（同步无 await）
        await _persist_type6(chars)                   # 再落库（有 await）
        log(f'[stats] type6 processed session={session_id} '
            f'chars={len(chars)} from {peer}')
    except Exception as e:
        log(f'[stats] type6 parse failed: {type(e).__name__}: {e}')


def _raw_crc32(data: bytes) -> int:
    """statistics_client.dll 使用的 raw CRC32（init=0、无 final XOR）。

    sub_18000B3E0（发送）与 vt6_GetTotals（接收）均从此初值起步、且不异或
    0xFFFFFFFF 收尾；而 Python ``zlib.crc32`` 默认走标准 CRC32（init=0xFFFFFFFF
    且 final XOR），二者对任何非空输入都不相等 → 必须自己实现 raw 变体。

    推导：zlib.crc32(data, value) 内部为 ``crc = value ^ 0xFFFFFFFF`` 起步、
    收尾再 ``^ 0xFFFFFFFF``。令 value=0xFFFFFFFF 即得 init=0 的 raw 中间值，
    再撤销收尾异或即为 raw CRC32。
    """
    return (zlib.crc32(data, 0xFFFFFFFF) ^ 0xFFFFFFFF) & 0xFFFFFFFF


def _frame(payload: bytes) -> bytes:
    """S->C 帧：payload 需 zlib 压缩。"""
    data = zlib.compress(payload)
    ln = len(data) + 4
    crc = _raw_crc32(struct.pack('<I', ln) + data)
    return struct.pack('<II', ln, crc) + data


def _parse_get_totals(payload: bytes):
    """解析 GetTotals 请求，返回 (charIds, echo_i64)。"""
    off = 1
    n, = struct.unpack_from('<I', payload, off)
    off += 4
    ids = []
    for i in range(n):
        ids.append(struct.unpack_from('<q', payload, off)[0])
        off += 8
    echo = 0
    if off + 8 <= len(payload):
        echo = struct.unpack_from('<q', payload, off)[0]
        off += 8
    return ids, echo


async def _ribbons_for_char(char_id: int):
    """从 character_ribbon_xp 取该角色勋带状态 [(rid, currentValue, level, nextValue)]。"""
    if STORE is None:
        return []
    try:
        xps = await STORE.get_ribbon_xp_by_char(int(char_id))
    except Exception as e:
        log(f'[stats] ribbon_xp lookup failed (char={char_id}): {e}')
        return []
    out = []
    for rid, total in sorted(xps.items()):
        if total <= 0:
            continue
        lv = _ribbon_level_from_xp(total)
        nxt = _RIBBON_LEVEL_XP.get(lv + 1, _RIBBON_LEVEL_XP.get(lv, 0))
        out.append((int(rid), float(total), int(lv), float(nxt)))
    return out


def _build_totals(session_id: int, char_data) -> bytes:
    """char_data: [(charId, stats_entries, ribbons)]

    stats_entries: [(statId, ctx1, ctx2, value)]，与 type-6 上报同布局
    （u32 statId + i64 ctx1 + i64 ctx2 + i64 value，客户端读取端
    sub_18038D550 按 ctx 段位取 16/32 位子字段）。

    每角色字段顺序：charId → numStats → stats 数组 → numRibbons →
    ribbons 数组（客户端 sub_18000ADF0 读取顺序，见模块 docstring
    2026-08-27 崩溃分析；numRibbons 放 stats 前会错位 4 字节）。
    """
    out = struct.pack('<qI', session_id, len(char_data))
    for cid, stats, ribbons in char_data:
        out += struct.pack('<qI', int(cid), len(stats))
        for wid, c1, c2, val in stats:
            out += struct.pack('<Iqqq', int(wid), int(c1), int(c2), int(val))
        out += struct.pack('<I', len(ribbons))
        for rid, cur, lv, nxt in ribbons:
            out += struct.pack('<qdId', int(rid), float(cur), int(lv), float(nxt))
    return out


_NAME_TO_WIRE = {}
for _wid, _nm in WIRE_STAT_NAMES.items():
    _NAME_TO_WIRE.setdefault(_nm, _wid)


async def _stats_entries_for_char(char_id: int):
    """character_stats → GetTotals wire 条目 [(statId, ctx1, ctx2, value)]。

    只回发能映射回 wire id 的键（标量 WIRE 名 + w/v/gm/wv 维度前缀）；
    legacy 结算键（kills/score 等）与未知键不发 —— 与 game_server
    RequestReadCharacterStats 的"WIRE 键优先"策略一致，避免双计数。
    ctx 编码与 _stat_db_key 相反方向：
      w<id>  → ctx1=id；v<id> → ctx1=id
      gm<id> → ctx2=id<<16（写入端语义：模式 id 在 ctx2 高 16 位）
      wv<w>_<v> → ctx1=w, ctx2=v
    维度 id 非正（0/负，键碰撞或编码错位产物）整条丢弃 —— 战斗服
    statistics.cpp 收到后按白名单校验拒掉即 "#error1 Character N had
    X invalid stats"（2026-08-31 远端日志：6 角色 2-5 条/人）。回读端
    无法得知客户端白名单，先滤掉确定非法的 0 id，其余正常下发并在
    dims 日志里列出全部维度 id，供下次对局与 invalid 数逐条对账。
    """
    if STORE is None:
        return []
    try:
        stats = await STORE.get_stats(int(char_id))
    except Exception as e:
        log(f'[stats] get_stats failed (char={char_id}): {e}')
        return []
    out = []
    dropped = []
    for key, val in stats.items():
        if not val:
            continue
        slash = key.find('/')
        if slash < 0:
            wid = _NAME_TO_WIRE.get(key)
            if wid is not None:
                out.append((wid, 0, 0, int(val)))
            continue
        prefix, name = key[:slash], key[slash + 1:]
        wid = _NAME_TO_WIRE.get(name)
        if wid is None:
            continue
        if prefix.startswith('gm') and prefix[2:].isdigit():
            gm = int(prefix[2:])
            if gm <= 0:
                dropped.append(key)
                continue
            out.append((wid, 0, gm << 16, int(val)))
        elif prefix.startswith('wv') and '_' in prefix:
            w, _, v = prefix[2:].partition('_')
            if not (w.isdigit() and v.isdigit()):
                continue
            if int(w) <= 0 or int(v) <= 0:
                dropped.append(key)
                continue
            out.append((wid, int(w), int(v), int(val)))
        elif prefix.startswith('w') and prefix[1:].isdigit():
            if int(prefix[1:]) <= 0:
                dropped.append(key)
                continue
            out.append((wid, int(prefix[1:]), 0, int(val)))
        elif prefix.startswith('v') and prefix[1:].isdigit():
            if int(prefix[1:]) <= 0:
                dropped.append(key)
                continue
            out.append((wid, int(prefix[1:]), 0, int(val)))
    if dropped:
        log(f'[stats] GetTotals char={char_id}: dropped {len(dropped)} '
            f'invalid dim entries ({", ".join(dropped[:8])})')
    if out:
        w_ids = sorted({c1 for wid, c1, _c2, _v in out if wid in _WEAPON_RANGE})
        v_ids = sorted({c1 for wid, c1, _c2, _v in out if wid in _VEHICLE_RANGE})
        gm_ids = sorted({c2 >> 16 for wid, _c1, c2, _v in out
                         if wid in _GAMEMODE_RANGE})
        log(f'[stats] GetTotals dims char={char_id}: w={w_ids} v={v_ids} '
            f'gm={gm_ids}')
    return out


async def _handle_conn(reader, writer):
    peer = writer.get_extra_info('peername')
    try:
        # ---- GIVEITTOMENOW 握手（comm_start：失败客户端会静默重连 10 次）----
        # 注意：串是 13 字符 + NUL = 14 字节（DLL write/read/memcmp 均按 14 算）
        pre = await reader.readexactly(4)
        n, = struct.unpack('<I', pre)
        hs = await reader.readexactly(n) if n else b''
        if n != 14 or not hs.startswith(b'GIVEITTOMENOW'):
            log(f'[stats] bad handshake from {peer}: n={n} {hs!r}')
            return
        # 回复：14 字节裸串（含 NUL，无长度前缀）+ 4 字节会话值（客户端存 a1+104）
        writer.write(b'YOUGOTITTODAY\x00' + struct.pack('<I', 1))
        await writer.drain()
        log(f'[stats] handshake OK from {peer}')

        while True:
            hdr = await reader.readexactly(8)
            ln, crc = struct.unpack('<II', hdr)
            if ln < 4 or ln > 16 * 1024 * 1024:
                break
            payload = await reader.readexactly(ln - 4) if ln > 4 else b''
            if _raw_crc32(hdr[:4] + payload) != crc:
                log(f'[stats] CRC mismatch from {peer}, dropping frame')
                break
            if not payload:
                break
            # type 6 周期上报是压缩的（msg_envelope deflate）；其余明文
            if payload[0] != 0x78:
                mtype = payload[0]
            else:
                try:
                    payload = zlib.decompress(payload)
                    mtype = payload[0]
                except zlib.error:
                    mtype = payload[0]
            if mtype == 8:                       # bye
                break
            if mtype == 3:                       # CreateSession
                sid = struct.unpack_from('<q', payload, 1)[0] if len(payload) >= 9 else 0
                log(f'[stats] CreateSession sid={sid} from {peer}')
                continue
            if mtype == 4:                       # FinalizeSession
                sid = struct.unpack_from('<q', payload, 1)[0] if len(payload) >= 9 else 0
                log(f'[stats] FinalizeSession sid={sid} from {peer}')
                continue
            if mtype == 5:                       # GetTotals（需立即响应）
                char_ids, echo = _parse_get_totals(payload)
                entries = []
                for cid in char_ids:
                    ribbons = await _ribbons_for_char(cid)
                    stats = await _stats_entries_for_char(cid)
                    entries.append((cid, stats, ribbons))
                    log(f'[stats] GetTotals char={cid} -> {len(stats)} stats, '
                        f'{len(ribbons)} ribbons '
                        f'({", ".join(f"r{r[0]}:lv{r[2]}" for r in ribbons[:6])})')
                writer.write(_frame(_build_totals(echo or 1, entries)))
                await writer.drain()
                continue
            if mtype == 7:                       # CreateRandomStat（读响应，回空表）
                writer.write(_frame(_build_totals(1, [])))
                await writer.drain()
                continue
            if mtype == 6:                       # 周期统计上报 → 解析入库
                await process_type6_body(payload[1:], peer)
                continue
            log(f'[stats] unknown msg type {mtype} from {peer}')
    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass
    except Exception as e:
        log(f'[stats] conn error ({peer}): {type(e).__name__}: {e}')
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def start():
    server = await asyncio.start_server(_handle_conn, '0.0.0.0', STATS_PORT)
    log(f'Statistics server listening on 0.0.0.0:{STATS_PORT} '
        f'(statistics_client.dll GetTotals)')
    async with server:
        await server.serve_forever()