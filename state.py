# -*- coding: utf-8 -*-
"""全局状态与配置：路径、数据库存储、日志、会话。

被 wardb / messages / player_service / handlers / realtest_server 共用，
自身只依赖 player_store，避免循环 import。
"""
import os
import sys
import time

from player_store import PlayerStore

STORE = PlayerStore()

# 通用演示密码已移除：所有登录必须命中数据库中该账号的真实密码，
# DB 异常一律拒登，杜绝"任意账号 + 固定密码"绕过认证。


# ------------------------------------------------------------- .env 加载
def _load_dotenv(path):
    """极简 .env 解析：KEY=VALUE 逐行读，# 注释、空行跳过，不做变量展开。

    已存在的环境变量不覆盖（shell 导出的优先级高于文件）。
    解析失败静默跳过该行——.env 是便利设施不是关键配置。"""
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, _, v = line.partition('=')
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass                          # 无 .env：全部走默认值，正常场景


_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_load_dotenv(os.path.join(_PKG_DIR, '.env'))

# 私服客户端目录（images/rank、BattlEyeTlm 本地化、HTTP 静态资源均取自这里；
# 注意：不要指向 D:\Program Files\HnG 个人正式客户端）
#
# 路径解析顺序（打包部署友好）：
#   1. 环境变量 HNG_GAME_DIR（显式指定，最高优先级，部署在非标准位置时用）
#   2. 从包目录向上逐级探测客户端根目录（WarDatabase + images 特征），
#      两种布局均自动命中：根目录自身含之（如 work\hng-server-re → d:\HnG-test），
#      或根目录下 combat-server 含之（warserver 与 combat-server 同层部署）
#   3. 探测失败：启动时打印警告并要求显式设置 HNG_GAME_DIR（无硬编码兜底路径）


def _detect_game_dir():
    env = os.environ.get('HNG_GAME_DIR')
    if env:
        return os.path.abspath(env)
    # 从包目录向上逐级探测客户端根目录（WarDatabase + images 为特征），
    # 任意嵌套深度都能命中，如 d:\HnG-test\work\hng-server-re\server → d:\HnG-test。
    # 两种布局：
    #   a) 根目录自身含 WarDatabase + images（游戏根与服务器代码同处一棵树）
    #   b) 根目录下有 combat-server，且其含 WarDatabase + images（warserver 与
    #      combat-server 同层部署——combat-server 本身就是完整客户端根）
    cur = os.path.dirname(_PKG_DIR)
    for _ in range(6):
        if cur in ('', os.path.dirname(cur)):
            break
        if (os.path.isdir(os.path.join(cur, 'WarDatabase'))
                and os.path.isdir(os.path.join(cur, 'images'))):
            return cur
        cs = os.path.join(cur, 'combat-server')
        if (os.path.isdir(os.path.join(cs, 'WarDatabase'))
                and os.path.isdir(os.path.join(cs, 'images'))):
            return cs
        cur = os.path.dirname(cur)
    print('[warn] 未能自动探测客户端根目录（各级父目录均无 WarDatabase + images）。\n'
          '       请设置环境变量 HNG_GAME_DIR 指向客户端根目录。',
          file=sys.stderr)
    return ''


GAME_DIR = _detect_game_dir()

# WarDatabase 表目录：战斗服 hng.exe 实际加载的是 combat-server 下的副本，
# HTTP 下发（wardb_unique / wardb_sync / CAS 回源）必须与之同源，否则
# 改表后下发给客户端的哈希与战斗服实际用的表不一致。
# 解析：GAME_DIR\combat-server\WarDatabase 存在即用（战斗服副本优先），
# 否则回退 GAME_DIR\WarDatabase。
if os.path.isdir(os.path.join(GAME_DIR, 'combat-server', 'WarDatabase')):
    WARDDB_DIR = os.path.join(GAME_DIR, 'combat-server', 'WarDatabase')
else:
    WARDDB_DIR = os.path.join(GAME_DIR, 'WarDatabase')

# texts 语言文件目录（hngsync desktop_client 清单/CAS 推送源）：
# 与 WARDDB_DIR 同源逻辑——combat-server 副本优先（服务器权威版本）。
# 注意区分：HTTP /HnG_Strategy_en.xml 路由仍读 GAME_DIR\texts（客户端侧
# 副本，_localization_body）。更新流验证：清单哈希取 combat-server 版 →
# 客户端本地旧文件哈希不匹配 → hngsync 下载新版写回客户端本地。
if os.path.isdir(os.path.join(GAME_DIR, 'combat-server', 'texts')):
    TEXTS_DIR = os.path.join(GAME_DIR, 'combat-server', 'texts')
else:
    TEXTS_DIR = os.path.join(GAME_DIR, 'texts')

# 当前连接的数据库 player_id（contextvar）：messages._msg_* 构建士兵视图
# 各表行（character/weapon/Vehicle/UniformPart…）的 playerId 字段从这里读。
# 客户端 CharacterTable 等按 playerId.Equals(Global.currentPlayer.id) 过滤
# 自己的行——恒发 1 会让 DB id≥2 的玩家看不到自己的角色/装备。
# dispatch 入口（handlers/__init__.py）按会话设置；asyncio task 各自持有
# context 拷贝，跨连接/后台 _spawn 任务不会串号。
import contextvars
CURRENT_PID = contextvars.ContextVar('current_pid', default=1)

# 服务器对外地址（下发给客户端的连接地址）：
# - Settings.js 的 flash_dynhttp / unique_file_servers / account_url 等
# - 匹配成局后 PMS 的 game_connect（客户端 hng.exe 连战斗服）
# - HostingCenter ping 地址
# 外网开服 = 填服务器公网 IP；默认 127.0.0.1（本机单机/局域网内可用机器 IP）。
# 监听地址不受此影响（始终 0.0.0.0）。
PUBLIC_HOST = os.environ.get('HNG_PUBLIC_HOST', '127.0.0.1').strip() or '127.0.0.1'


class Session:
    def __init__(self, writer):
        self.writer = writer
        self.username = None
        self.login_ctx = None
        self.soldier_view_cmd = None  # SOLDIER_VIEW 的 cmd_id，用于推送 character 数据
        self.shop_view_cmd = None     # SHOP_VIEW 的 cmd_id，用于后续推送商店更新
        self.playerview_cmd = None    # PLAYER_VIEW 的 cmd_id，用于购买后推送 player 更新
        self.player_id = None         # 数据库 players.id（登录后惰性加载）
        self.pw = None                # 该账号的数据库密码（login2_begin 时加载）
        self.user_exists = True       # 账号是否存在于数据库（False 时直接拒登）

    async def ensure_player(self):
        """返回数据库 player_id；账号不存在 / DB 不可用返回 None（不自动建号）。"""
        if self.player_id is None:
            try:
                self.player_id = await STORE.get_player(self.username or 'demo')
            except Exception as e:
                log(f'[DB] get_player failed: {e}')
        return self.player_id


# 大厅"当前选中士兵"注册表：player_id → character_id。
# 客户端不发独立的选角消息，服务器只能从携带 characterId 的请求侧面感知：
# JoinMatchMaking f1（单人排队带选中角色）、SquadInvitePlayer f1（ownerCharacterId
# 即邀请者选中角色）、SquadRespondToInvite f3。小队入队规则（B 必须换到与 A
# 同阵营同兵种的角色）需要知道 B 的当前选中士兵，均从此表取，无记录则按
# "首个同阵营同兵种角色"兜底。
PLAYER_SELECTED_CHAR = {}


# 已登录 Flash(playerview) 连接注册表：username → Session。
# 真实客户端是双连接：Flash UI 连接（subscribeplayerview）+ hng.exe 游戏连接。
# PlayerExitBatch 由游戏连接上报（playerview_cmd=None），但 PMS Admitted KVCS
# 只能推给 Flash 连接——必须按玩家名跨连接查找（views.py 注册 / 连接断开注销）。
FLASH_SESSIONS = {}


LOG_FILE = os.path.join(_PKG_DIR, 'realtest_game.log')


def _env_int(name, default, minimum=1):
    v = os.environ.get(name, '')
    try:
        n = int(v)
        return n if n >= minimum else default
    except ValueError:
        if v:                         # 设了但非法 → 用默认并告警
            sys.stderr.write(f'[env] {name}={v!r} invalid, using {default}\n')
        return default


LOG_KEEP_DAYS = _env_int('LOG_KEEP_DAYS', 3)   # 保留天数（含当天）


def _date_tag(t=None):
    return time.strftime('%Y-%m-%d', time.localtime(t))


def _rotate_daily(fh, path, today):
    """按天滚动：当前文件 mtime 不是今天 → 改名为带日期的历史文件，
    重开新句柄继续写。返回新句柄；任何 OSError 吞掉（不影响主流程）。"""
    try:
        if os.path.exists(path):
            mtime_tag = _date_tag(os.path.getmtime(path))
            if mtime_tag == today:
                return fh                  # 还在今天，不滚
            os.replace(path, f'{path}.{mtime_tag}.log')
        try:
            fh.close()
        except OSError:
            pass
        return open(path, 'a', encoding='utf-8', buffering=1)
    except OSError:
        return fh


def _purge_old_logs(path, keep_days):
    """删除超过保留天数的历史日志（按文件名日期判断）。"""
    import re
    base = os.path.basename(path)
    pat = re.compile(re.escape(base) + r'\.(\d{4}-\d{2}-\d{2})\.log$')
    cutoff = time.mktime(time.strptime(
        _date_tag(time.time() - keep_days * 86400), '%Y-%m-%d'))
    try:
        for name in os.listdir(os.path.dirname(path) or '.'):
            m = pat.match(name)
            if not m:
                continue
            try:
                t = time.mktime(time.strptime(m.group(1), '%Y-%m-%d'))
                if t < cutoff:
                    os.remove(os.path.join(os.path.dirname(path), name))
            except (OSError, ValueError):
                pass                        # 删不掉就留着，不影响运行
    except OSError:
        pass


_LOG_FH = open(LOG_FILE, 'a', encoding='utf-8', buffering=1)


class _Tee:
    """stdout 双写：控制台 + LOG_FILE。

    2026-08-21：启动脚本不再重定向 stdout（服务器窗口需实时显示日志），
    裸 print（HTTP 访问日志 / 初始化信息等不经 log() 的输出）经此落盘，
    文件日志保持完整。
    2026-08-25：按天滚动，保留 LOG_KEEP_DAYS 天（见 _rotate_daily）。"""

    def __init__(self, fh, path):
        self._fh = fh
        self._path = path
        self._day = None            # None → 首次 maybe_rotate 强制检查
        self._stdout = sys.__stdout__
        # 启动即检查：上次运行遗留的昨日日志当场滚动（否则要等运行中跨天）
        self.maybe_rotate()

    def write(self, s):
        self._stdout.write(s)
        self._fh.write(s)

    def flush(self):
        self._stdout.flush()
        self._fh.flush()

    def maybe_rotate(self):
        """跨天检查（log() 每条调用；纯字符串比较，开销可忽略）。"""
        today = _date_tag()
        if today == self._day:
            return
        self._day = today
        self._fh = _rotate_daily(self._fh, self._path, today)
        _purge_old_logs(self._path, LOG_KEEP_DAYS)


sys.stdout = _Tee(_LOG_FH, LOG_FILE)


def log(msg):
    rotate = getattr(sys.stdout, 'maybe_rotate', None)
    if callable(rotate):
        rotate()
    print(f'[{time.strftime("%H:%M:%S")}] {msg}')
