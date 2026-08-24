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

DEMO_PASSWORD = b'demodemo123'
# 私服客户端目录（images/rank、BattlEyeTlm 本地化、HTTP 静态资源均取自这里；
# 注意：不要指向 D:\Program Files\HnG 个人正式客户端）
#
# 路径解析顺序（打包部署友好）：
#   1. 环境变量 HNG_GAME_DIR（显式指定，最高优先级）
#   2. 包目录的父级/上两级探测（存在 WarDatabase + images 即视为客户端根目录，
#      把 zip 解压到客户端根目录下时自动命中，如 d:\HnG-test\hng-server）
#   3. 兜底开发机默认路径 d:\HnG-test
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))


def _detect_game_dir():
    env = os.environ.get('HNG_GAME_DIR')
    if env:
        return os.path.abspath(env)
    for cand in (os.path.dirname(_PKG_DIR),
                 os.path.dirname(os.path.dirname(_PKG_DIR))):
        if (os.path.isdir(os.path.join(cand, 'WarDatabase'))
                and os.path.isdir(os.path.join(cand, 'images'))):
            return cand
    return r'd:\HnG-test'


GAME_DIR = _detect_game_dir()
WARDDB_DIR = os.path.join(GAME_DIR, 'WarDatabase')


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


# 已登录 Flash(playerview) 连接注册表：username → Session。
# 真实客户端是双连接：Flash UI 连接（subscribeplayerview）+ hng.exe 游戏连接。
# PlayerExitBatch 由游戏连接上报（playerview_cmd=None），但 PMS Admitted KVCS
# 只能推给 Flash 连接——必须按玩家名跨连接查找（views.py 注册 / 连接断开注销）。
FLASH_SESSIONS = {}


LOG_FILE = os.path.join(os.path.dirname(__file__), 'realtest_game.log')
_LOG_FH = open(LOG_FILE, 'a', encoding='utf-8', buffering=1)


class _Tee:
    """stdout 双写：控制台 + LOG_FILE。

    2026-08-21：启动脚本不再重定向 stdout（服务器窗口需实时显示日志），
    裸 print（HTTP 访问日志 / 初始化信息等不经 log() 的输出）经此落盘，
    文件日志保持完整。"""

    def __init__(self, fh):
        self._fh = fh
        self._stdout = sys.__stdout__

    def write(self, s):
        self._stdout.write(s)
        self._fh.write(s)

    def flush(self):
        self._stdout.flush()
        self._fh.flush()


sys.stdout = _Tee(_LOG_FH)


def log(msg):
    print(f'[{time.strftime("%H:%M:%S")}] {msg}')
