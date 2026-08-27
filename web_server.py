"""HnG 私服 HTTP 服务（独立线程，ThreadingHTTPServer 0.0.0.0:80）。

从 realtest_server.py 拆出（2026-08-26）：HTTP 是公网安全敏感面
（注册 / 静态资源下发 / WarDatabase 表服务），独立成模块便于审计。

职责：
  /register /account  注册页（Vite/Vue SPA，web/dist，HnG 官方风格）
  /api/register       账号注册 JSON API（经主 loop 投递 STORE.create_player）
  /settings.js        客户端入口配置（warserver 地址 / 静态资源地址 / statserver 等）
  /hng_unique_v173293 静态数据文件清单 XML
  /wardb_unique       WarDatabase 表清单（表名,sha256-size）
  /WarDatabase/<tbl>  单个静态表文件下发
  /flashres_unique    Flash_Resources 清单（images/Rank 军衔图标）
  /undefined, /HnG_Strategy_en.xml 等本地化文件（动态注入缺失 key）
  /admin*             404（管理后台由独立进程 web/server/ 提供，不经 80 端口）
  其余                GAME_DIR 静态文件回退（目录列表与敏感扩展名 404）

启动：main() 里 web_server.init(main_loop) 后 threading.Thread(start_http)。
"""
import asyncio
import hashlib
import json
import mimetypes
import os
import re

from http.server import (
    SimpleHTTPRequestHandler,
    ThreadingHTTPServer,
    )

from state import (
    GAME_DIR,
    PUBLIC_HOST,
    STORE,
    WARDDB_DIR,
    log,
    )

from wardb import WARDB_UNIQUE


# ---- 本地化文件补全 ----
# 客户端 HnG_Strategy_en.xml 的 ErrorMessages/StrategyGame 段缺少部分 ResponseCodes
# 常量对应的 key（如 server_error），购买/操作失败后 Texts.getText 查找失败会显示
# '"ErrorMessages/StrategyGame/xxx" not found' 之类乱码。这里在服务器提供该文件时
# 动态注入缺失 key（不改原始文件）。ok/fail 等已存在，无需注入。
LOCALIZATION_EXTRA = """  <data name="ErrorMessages/StrategyGame/server_error">
    <value>An error occurred on the server</value>
  </data>
  <data name="ErrorMessages/StrategyGame/character_template_not_found">
    <value>Character type not found</value>
  </data>
  <data name="ErrorMessages/StrategyGame/not_for_sale">
    <value>This item is not for sale</value>
  </data>
  <data name="ErrorMessages/StrategyGame/price_not_found">
    <value>Price not found</value>
  </data>
  <data name="ErrorMessages/StrategyGame/invalid_currency">
    <value>Invalid currency</value>
  </data>
  <data name="ErrorMessages/StrategyGame/variant_not_found">
    <value>Variant not found</value>
  </data>
  <data name="ErrorMessages/StrategyGame/variant_not_owned_by_player">
    <value>You don't own this variant</value>
  </data>
  <data name="ErrorMessages/StrategyGame/variant_template_not_found">
    <value>Variant type not found</value>
  </data>
  <data name="ErrorMessages/StrategyGame/weapon_ammo_not_found">
    <value>Ammunition not found</value>
  </data>
  <data name="ErrorMessages/StrategyGame/weapon_ammo_not_owned_by_player">
    <value>You don't own this ammunition</value>
  </data>
  <data name="ErrorMessages/StrategyGame/store_bundle_not_found">
    <value>Bundle not found</value>
  </data>
  <data name="ErrorMessages/StrategyGame/store_discount_not_found">
    <value>Discount not found</value>
  </data>
  <data name="ErrorMessages/StrategyGame/account_locked">
    <value>Account locked</value>
  </data>
  <data name="ErrorMessages/StrategyGame/character_already_added">
    <value>Character already added</value>
  </data>
  <data name="ErrorMessages/StrategyGame/operation_not_permitted">
    <value>Operation not permitted</value>
  </data>
  <data name="ErrorMessages/StrategyGame/pricing_mismatch">
    <value>Pricing mismatch</value>
  </data>
"""

_LOCALIZATION_CACHE = None


def _localization_body():
    """读取原始本地化文件并在 StrategyGame 段后追加缺失 key（结果缓存）。"""
    global _LOCALIZATION_CACHE
    if _LOCALIZATION_CACHE is not None:
        return _LOCALIZATION_CACHE
    # texts/ 为 hng-group.com 生态新版（EAC 反作弊文案 + 私服品牌信息），
    # 旧源 BattlEyeTlm/ 是 TLM Partners 时代官方原始版
    lang_file = os.path.join(GAME_DIR, 'texts', 'HnG_Strategy_en.xml')
    try:
        with open(lang_file, 'rb') as f:
            body = f.read()
        # 逐条注入缺失 key：每条 data 条目单独判断 name 是否已存在
        inject = b''
        for name in re.findall(rb'name="([^"]+)"', LOCALIZATION_EXTRA.encode('utf-8')):
            if b'name="' + name + b'"' not in body:
                idx = LOCALIZATION_EXTRA.index(name.decode('utf-8'))
                entry_start = LOCALIZATION_EXTRA.rfind('<data', 0, idx)
                entry_end = LOCALIZATION_EXTRA.find('</data>', idx) + len('</data>')
                inject += LOCALIZATION_EXTRA[entry_start:entry_end].encode('utf-8') + b'\n'
        if inject:
            # 追加到文件末尾 </root> 之前
            body = body.replace(b'</root>', inject + b'</root>', 1)
        _LOCALIZATION_CACHE = body
    except Exception as e:
        log(f'[HTTP] localization patch failed: {e}')
        _LOCALIZATION_CACHE = body
    return _LOCALIZATION_CACHE


# 对外地址统一取 state.PUBLIC_HOST（HNG_PUBLIC_HOST 环境变量 / .env）：
# flash_dynhttp=客户端连 warserver 的 TCP、unique_file_servers=HTTP 静态资源、
# account_url=账号页、ircserver=聊天（irc 服务未实现，仅占位）。
SETTINGS_JS = (f"flash_dynhttp=\"{PUBLIC_HOST}:15110\";\n"
               f"unique_file_servers=\"{PUBLIC_HOST}\";\n"
               # 官方格式：语言文件用 locale_<lang> 族（裸文件名，请求 file server 根路径）。
               # localizerFile 不是下发项——客户端 setLanguage() 会用它覆盖任意已下发的
               # localizerFile，缺失时被覆盖为 undefined → /undefined 请求。
               "locale_en=\"HnG_Strategy_en.xml\";\n"
               "version=\"173293\";\n"
               "distribution=\"live\";\n"
               "language=\"en\";\n"
               "releasename=\"QQ\";\n"
               f"ircserver=\"{PUBLIC_HOST}:6667\";\n"
               # 战斗内勋带弹窗：statistics_client.dll GetTotals 统计服务器
               # （swf 将 settings.js 变量转发进 hngsync CMD_RUN_HNG -> hng.exe /R）
               f"statserver=\"{PUBLIC_HOST}\";\n"
               "statserverport=\"15115\";\n"
               f"account_url=\"http://{PUBLIC_HOST}/account\";\n"
               "uniqueversionfilename=\"hng_unique_v173293\";\n"
               "siteonly=\"false\";\n").encode()

# hng_unique_v173293: 静态数据文件清单，客户端登录成功后请求
# 格式: <root><list id="WarDatabase" uniquefile="wardb_unique"/><list id="Flash_Resources" uniquefile="flashres_unique"/></root>
UNIQUE_FILE_XML = b'<?xml version="1.0" encoding="utf-8"?>\n<root>\n<list id="WarDatabase" uniquefile="wardb_unique"/>\n<list id="Flash_Resources" uniquefile="flashres_unique"/>\n</root>\n'

# Flash_Resources 文件：列出 images/Rank 目录下军衔图标的清单映射。
# 客户端 HnGGUIRankIcon/HnGGUIButtonRank 用 settings.uniqueUrlExists(path) 判断图标是否可加载，
# 清单为空 → 所有军衔图标 loadFailed 且被记入 _dontTryLoad → 图标不显示。
# 格式（目录累积式相对路径，客户端 parseUniqueFileList 用 sCurrentDir 拼接）:
#   "images/Rank/1_1_0.png",sha256hex-size,size,0,0
#   "1_1_1.png",sha256hex-size,size,0,0
def _generate_flashres_unique():
    import posixpath as _pp
    root = os.path.join(GAME_DIR, 'images', 'Rank')
    if not os.path.isdir(root):
        return b'\n'
    files = sorted(n for n in os.listdir(root)
                   if os.path.isfile(os.path.join(root, n)))
    lines = []
    cur_dir = ''
    for fn in files:
        rel = _pp.join('images/Rank', fn)
        rel_path = _pp.relpath(rel, cur_dir)
        fpath = os.path.join(root, fn)
        try:
            with open(fpath, 'rb') as f:
                data = f.read()
        except OSError:
            continue
        sha = hashlib.sha256(data).hexdigest()
        size = len(data)
        lines.append(f'"{rel_path}",{sha}-{size},{size},0,0')
        cur_dir = _pp.dirname(rel)
    return ('\n'.join(lines) + '\n').encode('utf-8')


FLASHRES_UNIQUE = _generate_flashres_unique()
_flashres_n = FLASHRES_UNIQUE.count(b'\n')
log(f'[init] flashres_unique generated: {_flashres_n} files, '
    f'{len(FLASHRES_UNIQUE)}B (scanned {os.path.join(GAME_DIR, "images", "Rank")})')


# ================================================================ 注册页（Vue SPA）
# 前端源码在仓库内 web/frontend（Vite + Vue3 + TS，MPA 双入口），
# 构建产物 web/frontend/dist 由本模块下发：
#   /register /account      -> dist/register.html
#   /assets/*               -> dist/assets/*（vite 构建的 js/css）
#   /icons/* /soldier.png   -> dist 根下复制的客户端素材（frontend/public 随 build 并入）
# POST /api/register        -> JSON 建号，经主 loop 投递 STORE.create_player。
# HTTP 跑在独立线程，STORE 是主线程 asyncio loop 里的 asyncpg 池，
# 因此经 run_coroutine_threadsafe 把建号协程投递回主 loop 执行。

_WEB_DIST = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'web', 'frontend', 'dist')

_MAIN_LOOP = None  # init() 由 main() 记录（asyncio.get_running_loop()）


def init(main_loop):
    """注入主线程 asyncio loop（/api/register 投递 create_player 用）。"""
    global _MAIN_LOOP
    _MAIN_LOOP = main_loop


def _serve_file(handler, path, cache=False):
    """下发 dist 内文件（realpath 前缀校验防穿越）。文件不存在返回 False。"""
    full = os.path.realpath(os.path.join(_WEB_DIST, path))
    dist_root = os.path.realpath(_WEB_DIST) + os.sep
    if not full.startswith(dist_root) or not os.path.isfile(full):
        return False
    ctype = mimetypes.guess_type(full)[0] or 'application/octet-stream'
    with open(full, 'rb') as f:
        body = f.read()
    handler.send_response(200)
    handler.send_header('Content-Type', ctype)
    handler.send_header('Content-Length', str(len(body)))
    # vite 产物带内容哈希文件名，可安全长缓存；html 不缓存
    if cache:
        handler.send_header('Cache-Control', 'public, max-age=86400')
    else:
        handler.send_header('Cache-Control', 'no-cache')
    handler.end_headers()
    handler.wfile.write(body)
    return True


def _json_reply(handler, obj, status=200):
    body = json.dumps(obj).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _handle_register_api(handler):
    """POST /api/register：JSON -> 校验 -> 投递 create_player 到主 loop。"""
    try:
        length = int(handler.headers.get('Content-Length') or 0)
        if length > 65536:            # 上限防内存 DoS
            _json_reply(handler, {'error': '请求过大'}, 413)
            return
        raw = handler.rfile.read(length) if length > 0 else b''
        data = json.loads(raw or b'{}')
        username = (data.get('username') or '').strip()
        password = data.get('password') or ''
    except Exception as e:
        _json_reply(handler, {'error': '请求格式错误'}, 400)
        log(f'[register] json parse failed: {e}')
        return

    if not username or not password:
        _json_reply(handler, {'error': '用户名和密码不能为空'}, 400)
        return
    if not re.fullmatch(r'[A-Za-z0-9_\-.]{2,24}', username):
        _json_reply(handler, {'error': '用户名需为 2-24 位字母/数字/_-. 组合'}, 400)
        return
    # cheat 门控封禁：客户端 ApplicationPlayer::PreInit 对昵称做
    # "reto.*" / "[admin]*" 通配匹配，命中即解锁控制台 + cheat 选项。
    # 注册正则不含 [ ]，[admin] 前缀天然挡住；reto. 全合法字符，必须显式拒绝。
    low = username.lower()
    if low.startswith(('reto.', '[admin]')):
        log(f'[register] BLOCKED reserved-prefix username {username!r}')
        _json_reply(handler, {'error': '无法注册，请稍后重试'}, 400)
        return
    if not (4 <= len(password) <= 64):
        _json_reply(handler, {'error': '密码长度需为 4-64 位'}, 400)
        return
    if _MAIN_LOOP is None or STORE.pool is None:
        _json_reply(handler, {'error': '数据库未连接，暂时无法注册'}, 503)
        return

    fut = asyncio.run_coroutine_threadsafe(
        STORE.create_player(username, password), _MAIN_LOOP)
    try:
        pid = fut.result(timeout=10)
    except Exception as e:
        log(f'[register] create_player({username!r}) failed: {e}')
        _json_reply(handler, {'error': '注册失败，请稍后重试'}, 500)
        return
    if pid is None:
        log(f'[register] user {username!r} already exists (web)')
        _json_reply(handler, {'error': f'用户名 {username} 已存在，请换一个'}, 409)
    else:
        log(f'[register] user {username!r} created via web (id={pid})')
        _json_reply(handler, {'ok': True, 'player_id': pid}, 201)


# 禁止经 HTTP 暴露的扩展名：IDA 分析库、运行日志、脚本/文档等非游戏运行文件。
# 这些文件放在 GAME_DIR 里会被静态处理器直接下发（公网扫描器已下载过）。
_STATIC_DENY_EXTS = {
    '.i64', '.idb', '.id0', '.id1', '.id2', '.nam', '.til',  # IDA 数据库及附属文件
    '.log',                                                     # 运行日志（hng_boot.log 等）
    '.py', '.pyw', '.md',                                       # 脚本/笔记
}


def _deny_static(path):
    """目录列表与敏感扩展名一律拒绝，防止公网扫描器经 frp 枚举服务器文件结构"""
    if path.endswith('/'):
        return True
    return os.path.splitext(path)[1].lower() in _STATIC_DENY_EXTS


class HttpHandler(SimpleHTTPRequestHandler):
    """Serve /status, /settings.js, and static files from the game directory."""

    protocol_version = 'HTTP/1.0'

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=GAME_DIR, **kw)

    def _log_client_request(self, method):
        # 统一入口日志：打印请求方 IP:端口，便于排查不明请求
        ip, port = self.client_address
        log(f'[HTTP] <-- {method} {self.path} from {ip}:{port}')

    def do_GET(self):
        path = self.path.split('?')[0]
        self._log_client_request('GET')
        # ---- 注册页 SPA（web/dist）----
        if path in ('/register', '//register', '/account', '//account',
                    '/register.html'):
            if _serve_file(self, 'register.html'):
                return
            self.send_error(404, 'Not Found')
            log('[HTTP] 404 /register (web/dist 未构建？)')
            return
        if path.startswith('/assets/') or path.startswith('//assets/'):
            # vite 构建产物（带内容哈希文件名，可缓存）
            rel = path.lstrip('/').removeprefix('assets/')
            if not rel or '..' in rel:
                self.send_error(404, 'Not Found')
                return
            if _serve_file(self, os.path.join('assets', rel), cache=True):
                return
            self.send_error(404, 'Not Found')
            return
        if path.startswith('/icons/') or path.startswith('//icons/') \
                or path == '/soldier.png' or path == '//soldier.png':
            # web/public 复制的客户端素材（武器/载具/军衔/勋带图标 + 注册页士兵立绘）
            rel = path.lstrip('/')
            if '..' in rel:
                self.send_error(404, 'Not Found')
                return
            if _serve_file(self, rel, cache=True):
                return
            self.send_error(404, 'Not Found')
            return
        if path == '/admin' or path.startswith('/admin'):
            # 管理后台由独立进程（web/server/main.py）提供，公网 80 端口不可达
            self.send_error(404, 'Not Found')
            return
        if path == '/' or path == '/index.html':
            # hngsync daemon 的 HostingCenter ping 探测（GET /）：
            # 回环 RTT 常 <1ms，客户端 int()>=1 过滤会判 0 失败 → 延迟 60ms 保证通过
            import time as _ping_t
            _ping_t.sleep(0.06)
            log('[HTTP] ping probe GET / (delayed 60ms for RTT>=1ms filter)')
            # 直接回 200，避免落入静态处理器把 GAME_DIR 根目录列表泄露给公网扫描器
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'OK')
            return
        if path == '/status' or path == '//status':
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'OK')
            return
        if path == '/settings.js' or path == '//settings.js':
            self.send_response(200)
            self.send_header('Content-Type', 'application/javascript')
            self.end_headers()
            self.wfile.write(SETTINGS_JS)
            return
        if path == '/hng_unique_v173293' or path == '//hng_unique_v173293':
            # 静态数据文件清单，客户端登录成功后请求
            # 返回 XML 包含 WarDatabase 和 Flash_Resources 的 uniquefile
            self.send_response(200)
            self.send_header('Content-Type', 'application/xml')
            self.end_headers()
            self.wfile.write(UNIQUE_FILE_XML)
            log(f'[HTTP] served /hng_unique_v173293 ({len(UNIQUE_FILE_XML)}B)')
            return
        if path == '/wardb_unique' or path == '//wardb_unique':
            # WarDatabase manifest: 列出所有静态表文件（表名,sha256-size）
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(WARDB_UNIQUE)
            tbl_count = WARDB_UNIQUE.count(b'\n')
            log(f'[HTTP] served /wardb_unique ({len(WARDB_UNIQUE)}B, {tbl_count} tables)')
            return
        if path.startswith('/WarDatabase/') or path.startswith('//WarDatabase/'):
            # 服务单个静态表文件（如 /WarDatabase/factiontemplate）
            table_name = path.split('WarDatabase/')[-1].lstrip('/')
            # 路径穿越防护：解析后必须仍在 WARDDB_DIR 内（../ 穿越、绝对路径一律 404）
            table_path = os.path.realpath(os.path.join(WARDDB_DIR, table_name))
            if (table_path.startswith(os.path.realpath(WARDDB_DIR) + os.sep)
                    and os.path.isfile(table_path)):
                with open(table_path, 'rb') as f:
                    body = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.end_headers()
                self.wfile.write(body)
                log(f'[HTTP] served /WarDatabase/{table_name} ({len(body)}B)')
            else:
                self.send_response(404)
                self.end_headers()
                log(f'[HTTP] 404 /WarDatabase/{table_name} (not found)')
            return
        if path == '/flashres_unique' or path == '//flashres_unique':
            # Flash_Resources: 空内容，让 parseUniqueFileList 跳过文件映射
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(FLASHRES_UNIQUE)
            log(f'[HTTP] served /flashres_unique ({len(FLASHRES_UNIQUE)}B)')
            return
        if path == '/undefined':
            # Client didn't parse localizerFile from settings.js;
            # serve the language file directly so it can proceed.
            body = _localization_body()
            if body:
                self.send_response(200)
                self.send_header('Content-Type', 'application/xml')
                self.end_headers()
                self.wfile.write(body)
                log(f'[HTTP] served /undefined -> patched HnG_Strategy_en.xml ({len(body)}B)')
                return
        # 本地化文件：官方 locale_xx 下发的是裸文件名，客户端请求 file server
        # 根路径 /HnG_Strategy_en.xml（可能带 ?resetCache=N），GAME_DIR 根目录下
        # 没有该文件 → 404 → 客户端重试失败后闪退，必须返回补全版
        if path in ('/HnG_Strategy_en.xml', '//HnG_Strategy_en.xml'):
            body = _localization_body()
            if body:
                self.send_response(200)
                self.send_header('Content-Type', 'application/xml')
                self.end_headers()
                self.wfile.write(body)
                log(f'[HTTP] served patched HnG_Strategy_en.xml ({len(body)}B)')
                return
        # 安全校验：目录列表与敏感扩展名一律 404（统一在静态回退前拦截）
        if _deny_static(path):
            self.send_error(404, 'Not Found')
            return
        # serve static files (language xml, etc.)
        super().do_GET()

    def do_HEAD(self):
        # HEAD 探测同样不得泄露敏感文件的存在性与大小
        path = self.path.split('?')[0]
        if _deny_static(path):
            self.send_error(404, 'Not Found')
            return
        super().do_HEAD()

    def do_POST(self):
        path = self.path.split('?')[0]
        self._log_client_request('POST')
        if path == '/api/register' or path == '//api/register':
            # CSRF 双保险之一：自定义头（跨站表单无法携带；SameSite 由 Cookie 侧管）
            if self.headers.get('X-Requested-With') != 'XMLHttpRequest':
                _json_reply(self, {'error': '非法请求'}, 403)
                return
            _handle_register_api(self)
            return
        self.send_error(404, 'Not Found')

    def log_message(self, fmt, *args):
        msg = fmt % args
        # 静音 HEAD 探测（hngsync 文件校验，量大且无信息价值）
        if '"HEAD ' in msg:
            return
        log(f'[HTTP] {self.address_string()} - {msg}')


def start_http():
    srv = ThreadingHTTPServer(('0.0.0.0', 80), HttpHandler)
    log(f'[HTTP] listening on 0.0.0.0:80  (serving {GAME_DIR})')
    srv.serve_forever()
