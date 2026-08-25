"""HnG real-machine test server（入口）。

服务端按模块划分：
  protocol.py        Chunk 帧编解码 + protobuf wire 编码 + 登录基础消息
  state.py           全局状态：STORE / Session / 路径 / 日志
  wardb.py           WarDatabase 静态数据层（解锁/定价/勋带/经验索引）
  messages.py        HnG_States.* 消息构建（_msg_*）+ KVCS 推送助手
  player_service.py  玩家数据服务（扣款/解锁位图/角色数据回放）
  handlers/          请求处理域模块（auth/views/characters/shop/weapons/
                     vehicles/uniforms/misc）
  matchmaking.py     匹配 / 战局详情 / combat server 拉起
  game_server.py     游戏进程(warserver)协议（GetPlayerSettings 等）
  realtest_server.py 本入口：HTTP(80) 静态资源 + TCP(15110) 游戏协议 + main

Hosts file must redirect game.hng-group.com to 127.0.0.1.

Run:  python realtest_server.py
Requires:  admin (for port 80)  +  hosts entry
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import asyncio
import hashlib
import os
import re
import time

import game_server
import handlers
import matchmaking
import statistics_server

from protocol import (
    Chunk,
    build_response,
    parse_request,
    pb_int64,
    )

from state import (
    FLASH_SESSIONS,
    GAME_DIR,
    PUBLIC_HOST,
    STORE,
    Session,
    WARDDB_DIR,
    log,
    )

from wardb import WARDB_UNIQUE

from messages import _push_kvcs

from http.server import (
    SimpleHTTPRequestHandler,
    ThreadingHTTPServer,
    )


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
    lang_file = os.path.join(GAME_DIR, 'BattlEyeTlm', 'HnG_Strategy_en.xml')
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
        print(f'[HTTP] localization patch failed: {e}')
        _LOCALIZATION_CACHE = body
    return _LOCALIZATION_CACHE


# ================================================================ HTTP server

# 对外地址统一取 state.PUBLIC_HOST（HNG_PUBLIC_HOST 环境变量 / .env）：
# flash_dynhttp=客户端连 warserver 的 TCP、unique_file_servers=HTTP 静态资源、
# account_url=账号页、ircserver=聊天（irc 服务未实现，仅占位）。
SETTINGS_JS = (f"flash_dynhttp=\"{PUBLIC_HOST}:15110\";\n"
               f"unique_file_servers=\"{PUBLIC_HOST}\";\n"
               "localizerFile=\"BattlEyeTlm/HnG_Strategy_en.xml\";\n"
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
_n_flashres = FLASHRES_UNIQUE.count(b'\n')
print(f'[init] flashres_unique generated: {_n_flashres} files, '
      f'{len(FLASHRES_UNIQUE)}B (scanned {os.path.join(GAME_DIR, "images", "Rank")})')


# ================================================================ 注册页面（本地建号）
# GET  /register  -> 表单页；POST /register -> 调 STORE.create_player 写库。
# HTTP 跑在独立线程，STORE 是主线程 asyncio loop 里的 asyncpg 池，
# 因此经 run_coroutine_threadsafe 把建号协程投递回主 loop 执行。

_MAIN_LOOP = None  # main() 启动 HTTP 线程前记录（asyncio.get_running_loop()）

_REGISTER_TMPL = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>HnG 私服 - 账号注册</title>
<style>
 body{font-family:"Segoe UI","Microsoft YaHei",sans-serif;background:#f0f2f5;
      display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0}
 .card{background:#fff;border-radius:8px;box-shadow:0 2px 12px rgba(0,0,0,.08);
       padding:32px 36px;width:320px}
 h1{font-size:20px;margin:0 0 4px}
 p.sub{color:#888;font-size:13px;margin:0 0 24px}
 label{display:block;font-size:13px;color:#555;margin:12px 0 4px}
 input{width:100%;box-sizing:border-box;padding:8px 10px;border:1px solid #ccc;
       border-radius:4px;font-size:14px}
 input:focus{outline:none;border-color:#4a90d9}
 button{width:100%;margin-top:20px;padding:10px;background:#4a90d9;color:#fff;
        border:none;border-radius:4px;font-size:15px;cursor:pointer}
 button:hover{background:#3a7bc0}
 .msg{padding:10px 12px;border-radius:4px;font-size:14px;margin-bottom:16px}
 .ok{background:#e8f5e9;color:#2e7d32}
 .err{background:#fdecea;color:#c62828}
</style>
</head>
<body>
<div class="card">
 <h1>HnG 私服账号注册</h1>
 <p class="sub">注册后即可用该账号密码登录游戏客户端</p>
@@MSG@@
 <form method="post" action="/register">
  <label>用户名（2-24位字母/数字/_-.）</label>
  <input name="username" maxlength="24" required autofocus>
  <label>密码（4-64位）</label>
  <input name="password" type="password" maxlength="64" required>
  <label>确认密码</label>
  <input name="password2" type="password" maxlength="64" required>
  <button type="submit">注 册</button>
 </form>
</div>
</body>
</html>
"""

_REGISTER_RESULT_TMPL = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>HnG 私服 - 注册结果</title>
<style>
 body{font-family:"Segoe UI","Microsoft YaHei",sans-serif;background:#f0f2f5;
      display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0}
 .card{background:#fff;border-radius:8px;box-shadow:0 2px 12px rgba(0,0,0,.08);
       padding:32px 36px;width:320px;text-align:center}
 h1{font-size:20px;margin:0 0 20px}
 .msg{padding:12px;border-radius:4px;font-size:14px;margin-bottom:20px}
 .ok{background:#e8f5e9;color:#2e7d32}
 .err{background:#fdecea;color:#c62828}
 a{color:#4a90d9;text-decoration:none;font-size:14px}
</style>
</head>
<body>
<div class="card">
 <h1>HnG 私服账号注册</h1>
 <div class="@@CLS@@">@@TEXT@@</div>
 <a href="/register">&larr; 返回注册页</a>
</div>
</body>
</html>
"""


def _register_form_html(msg_html=''):
    return _REGISTER_TMPL.replace('@@MSG@@', msg_html).encode('utf-8')


def _register_result_html(ok, text):
    import html as _html
    cls = 'ok' if ok else 'err'
    body = _REGISTER_RESULT_TMPL.replace('@@CLS@@', cls) \
        .replace('@@TEXT@@', _html.escape(text))
    return body.encode('utf-8')


def _register_reply(handler, body, status=200):
    handler.send_response(status)
    handler.send_header('Content-Type', 'text/html; charset=utf-8')
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _handle_register_post(handler):
    """处理 POST /register：解析表单 -> 校验 -> 投递 create_player 到主 loop。"""
    from urllib.parse import parse_qs
    try:
        length = int(handler.headers.get('Content-Length') or 0)
        raw = handler.rfile.read(length) if length > 0 else b''
        form = parse_qs(raw.decode('utf-8', 'replace'))
        username = (form.get('username', [''])[0] or '').strip()
        password = form.get('password', [''])[0] or ''
        password2 = form.get('password2', [''])[0] or ''
    except Exception as e:
        _register_reply(handler, _register_result_html(False, f'表单解析失败: {e}'))
        return

    if not username or not password:
        _register_reply(handler, _register_result_html(False, '用户名和密码不能为空'))
        return
    if not re.fullmatch(r'[A-Za-z0-9_\-.]{2,24}', username):
        _register_reply(handler, _register_result_html(
            False, '用户名需为 2-24 位字母/数字/_ - . 组合'))
        return
    if not (4 <= len(password) <= 64):
        _register_reply(handler, _register_result_html(False, '密码长度需为 4-64 位'))
        return
    if password != password2:
        _register_reply(handler, _register_result_html(False, '两次输入的密码不一致'))
        return
    if _MAIN_LOOP is None or STORE.pool is None:
        _register_reply(handler, _register_result_html(
            False, '数据库未连接（PostgreSQL 不可用），无法注册'))
        return

    fut = asyncio.run_coroutine_threadsafe(
        STORE.create_player(username, password), _MAIN_LOOP)
    try:
        pid = fut.result(timeout=10)
    except Exception as e:
        log(f'[register] create_player({username!r}) failed: {e}')
        _register_reply(handler, _register_result_html(False, f'数据库错误: {e}'))
        return
    if pid is None:
        log(f'[register] user {username!r} already exists (web)')
        _register_reply(handler, _register_result_html(
            False, f'用户名 {username} 已存在，请换一个'))
    else:
        log(f'[register] user {username!r} created via web (id={pid})')
        _register_reply(handler, _register_result_html(
            True, f'账号 {username} 注册成功（id={pid}），现在可以用它登录游戏了'))


class HttpHandler(SimpleHTTPRequestHandler):
    """Serve /status, /settings.js, and static files from the game directory."""

    protocol_version = 'HTTP/1.0'

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=GAME_DIR, **kw)

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/register' or path == '//register':
            # 本地建号页面（表单）
            _register_reply(self, _register_form_html())
            return
        if path == '/' or path == '/index.html':
            # hngsync daemon 的 HostingCenter ping 探测（GET /）：
            # 回环 RTT 常 <1ms，客户端 int()>=1 过滤会判 0 失败 → 延迟 60ms 保证通过
            import time as _ping_t
            _ping_t.sleep(0.06)
            print('[HTTP] ping probe GET / (delayed 60ms for RTT>=1ms filter)')
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
            print(f'[HTTP] served /hng_unique_v173293 ({len(UNIQUE_FILE_XML)}B)')
            return
        if path == '/wardb_unique' or path == '//wardb_unique':
            # WarDatabase manifest: 列出所有静态表文件（表名,sha256-size）
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(WARDB_UNIQUE)
            tbl_count = WARDB_UNIQUE.count(b'\n')
            print(f'[HTTP] served /wardb_unique ({len(WARDB_UNIQUE)}B, {tbl_count} tables)')
            return
        if path.startswith('/WarDatabase/') or path.startswith('//WarDatabase/'):
            # 服务单个静态表文件（如 /WarDatabase/factiontemplate）
            table_name = path.split('WarDatabase/')[-1].lstrip('/')
            table_path = os.path.join(WARDDB_DIR, table_name)
            if os.path.isfile(table_path):
                with open(table_path, 'rb') as f:
                    body = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.end_headers()
                self.wfile.write(body)
                print(f'[HTTP] served /WarDatabase/{table_name} ({len(body)}B)')
            else:
                self.send_response(404)
                self.end_headers()
                print(f'[HTTP] 404 /WarDatabase/{table_name} (not found)')
            return
        if path == '/flashres_unique' or path == '//flashres_unique':
            # Flash_Resources: 空内容，让 parseUniqueFileList 跳过文件映射
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(FLASHRES_UNIQUE)
            print(f'[HTTP] served /flashres_unique ({len(FLASHRES_UNIQUE)}B)')
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
                print(f'[HTTP] served /undefined -> patched HnG_Strategy_en.xml ({len(body)}B)')
                return
        # 本地化文件：返回补全版（注入缺失的 ErrorMessages/StrategyGame key）
        if path == '/BattlEyeTlm/HnG_Strategy_en.xml' or path == '//BattlEyeTlm/HnG_Strategy_en.xml':
            body = _localization_body()
            if body:
                self.send_response(200)
                self.send_header('Content-Type', 'application/xml')
                self.end_headers()
                self.wfile.write(body)
                print(f'[HTTP] served patched HnG_Strategy_en.xml ({len(body)}B)')
                return
        # serve static files (language xml, etc.)
        super().do_GET()

    def do_POST(self):
        path = self.path.split('?')[0]
        if path == '/register' or path == '//register':
            _handle_register_post(self)
            return
        self.send_error(404, 'Not Found')

    def log_message(self, fmt, *args):
        print(f'[HTTP] {self.address_string()} - {fmt % args}')


def start_http():
    srv = ThreadingHTTPServer(('0.0.0.0', 80), HttpHandler)
    print(f'[HTTP] listening on 0.0.0.0:80  (serving {GAME_DIR})')
    srv.serve_forever()


# 客户端连接活性：MainLayoutMediator.pulseTimerTicks 每 5 秒检查一次，
# 距上次收到服务器数据 >20s（SERVER_SLOW_LIMIT_SECONDS）就显示左上角 loading +
# "server slow"，>40s 还会发 keepalive。因此登录后需定期推送 ViewHandlerPulse
# （ServerPulseTable 数据），客户端收到即 resetTimeout() 重置计数器。
async def _send_pulse(sess, writer):
    """向已登录会话推送 ViewHandlerPulse（id/lastTickTime/serverTime，单位毫秒）。
    ★ game server 连接是新 TCP 流，playerview_cmd 未设（客户端未在 game server 连接上
      SubscribePlayerView）。用 cmd_id=0 fallback（与私服抓包一致: frame 62 ViewHandlerPulse
      在 game server 连接上 cmdId=0），否则 game server 连接 20s 无数据 → 超时 → 10005。"""
    view_cmd = sess.playerview_cmd if sess.playerview_cmd is not None else 0
    now_ms = int(time.time() * 1000)
    m = pb_int64(1, 1) + pb_int64(2, now_ms) + pb_int64(3, now_ms)
    await _push_kvcs(writer, view_cmd, b'ViewHandlerPulse', m)
    log(f'S->C cmd={view_cmd:<4} KeyValueChangeSet (ViewHandlerPulse, serverTime={now_ms})')


PULSE_INTERVAL = 5  # 秒；私服 PMS→Pulse 间隔约 5s，客户端 SERVER_SLOW_LIMIT_SECONDS=20


async def handle_tcp(reader, writer):
    addr = writer.get_extra_info('peername')
    sock = writer.get_extra_info('socket')
    fd = sock.fileno() if sock else -1
    log(f'TCP CONNECT from {addr} (fd={fd})')
    sess = Session(writer)
    buf = b''
    try:
        while True:
            try:
                data = await asyncio.wait_for(reader.read(65536), timeout=PULSE_INTERVAL)
            except asyncio.TimeoutError:
                # 空闲超过 10s：推送 ViewHandlerPulse 保持连接活性，防止客户端报 server slow
                # ★ ZServer wardatacomm 连接（BFSettings 握手时打标）不推：
                #   官方 warserver 从不给专用服发 KVCS，ZServer 的 Unhandled 路径
                #   疑似堆损坏源（崩溃 0x4AD46D / 0x269E2，时间与 pulse 逐条吻合）
                if getattr(sess, 'is_combat_server', False):
                    continue
                log(f'[pulse] timeout fired fd={fd} playerview_cmd={sess.playerview_cmd}')
                try:
                    await _send_pulse(sess, writer)
                except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
                    break
                except Exception as e:
                    log(f'[pulse] send failed: {e}')
                    break
                continue
            if not data:
                break
            buf += data
            while True:
                chunk, nxt = Chunk.parse(buf, 0)
                if chunk is None:
                    break
                buf = buf[nxt:]
                await dispatch(sess, chunk, writer)
    except (ConnectionResetError, asyncio.IncompleteReadError):
        pass
    except Exception as e:
        log(f'[handle_tcp] UNEXPECTED {addr}: {type(e).__name__}: {e}')
    finally:
        log(f'TCP DISCONNECT {addr} (fd={fd})')
        # Flash 连接注册表注销（仅当注册的就是本连接；重登录已被覆盖时不动）
        if sess.username and FLASH_SESSIONS.get(sess.username) is sess:
            del FLASH_SESSIONS[sess.username]
        # 匹配排队清理：断线玩家出池（防幽灵条目虚增人数/被撮合成局；
        # 已成局的不动，战局照常进行）
        try:
            matchmaking.drop_player(sess.player_id)
        except Exception as e:
            log(f'[drop_player] {type(e).__name__}: {e}')
        writer.close()


async def dispatch(sess, chunk, writer):
    try:
        cmd_id, req_type, proto = parse_request(chunk)
    except Exception as e:
        log(f'PARSE-ERR {e}')
        return
    log(f'C->S cmd={cmd_id:<4} type={req_type:<45} proto={len(proto)}B'
        + (f' hex={proto[:32].hex()}' if proto else ''))

    # 单请求处理异常兜底：任何 handler 抛异常只记日志 + 回 HandlerErrorResponse，
    # 绝不让异常冒泡到 handle_tcp 杀掉整条连接（客户端请求队列串行，
    # 无响应会永久堵塞；连接断开则会话全丢——一个 handler 的 bug
    # 不应等于玩家掉线）。
    try:
        if await handlers.dispatch(sess, writer, cmd_id, req_type, proto):
            return
        if req_type in ('JoinMatchMakingRequest', 'CancelJoinMatchMakingRequest',
                        'GetMissionDetailsRequest'):
            # 战斗匹配：匹配请求 / 取消匹配 / 战局详情（简报部署界面），独立模块实现
            await matchmaking.handle_request(sess, writer, cmd_id, req_type, proto)
            return
        if await game_server.handle_request(sess, writer, cmd_id, req_type, proto):
            # 游戏进程(warserver)握手请求：GetPlayerSettings 等由独立模块处理
            return
    except Exception as e:
        import traceback
        log(f'[dispatch] HANDLER-ERR {req_type}: {type(e).__name__}: {e}')
        log(traceback.format_exc())
        try:
            resp = build_response(cmd_id, 'HandlerErrorResponse', b'')
            writer.write(resp)
            await writer.drain()
        except Exception:
            pass                    # 连接已断：handle_tcp 会收尾
        return
    # 未知请求：必须回复 HandlerErrorResponse 解锁客户端请求队列！
    # 客户端 ServiceHandler 串行发送请求，未收到响应的请求会永久堵塞队列
    # （HandlerErrorResponse 不解析 proto 内容，客户端只显示错误提示并继续）
    log(f'      (no handler for {req_type}, sending HandlerErrorResponse to unblock queue)')
    resp = build_response(cmd_id, 'HandlerErrorResponse', b'')
    writer.write(resp)
    await writer.drain()


async def start_tcp():
    server = await asyncio.start_server(handle_tcp, '0.0.0.0', 15110)
    log('TCP game server listening on 0.0.0.0:15110  (demo password: demodemo123)')
    async with server:
        await server.serve_forever()


# HostingCenter ping 应答器：daemon 的 CMD_PING 只收 hostname（不带端口），
# 固定 ping 到 20201 端口（真实 HnG 托管中心的 ping 服务端口）。
# 收到连接后记录 daemon 发来的探测内容并延迟 50ms 回 HTTP 200（RTT>=1ms 过客户端 int()>=1 过滤）。
async def _handle_ping_probe(reader, writer):
    peer = writer.get_extra_info('peername')
    try:
        try:
            data = await asyncio.wait_for(reader.read(4096), timeout=2)
            if data:
                log(f'[PING] probe from {peer}: {data[:120]!r}')
        except asyncio.TimeoutError:
            log(f'[PING] probe from {peer}: (no data, waiting for server reply)')
        # 最小延迟：确保 RTT 经 daemon 取整后 >=1ms（客户端 HostingCenterTable
        # 过滤 pingResponse>=1，否则弹 "unable to ping game server"）。
        # 本地 loopback 实测 ≥1ms 即满足，无需 50ms。
        await asyncio.sleep(0.001)
        writer.write(b'HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\n1\r\n')
        await writer.drain()
    except Exception as e:
        log(f'[PING] responder error: {e}')
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def start_ping_responder():
    """HostingCenter ping 应答器（TCP+UDP 双监听 20201）。
    daemon 的 CMD_PING 只收 hostname，固定 ping 到 20201 端口（真实 HnG 托管中心 ping 端口）。
    实测 TCP 监听收不到任何连接 → 判定为 UDP 探测，故 UDP 收到数据报后记录并延迟 50ms 回显
    （RTT>=1ms 过客户端 int()>=1 过滤）。"""
    class _UdpProbe(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, data, addr):
            log(f'[PING] UDP probe from {addr}: {data[:120]!r}')

            async def _reply():
                await asyncio.sleep(0.05)
                try:
                    self.transport.sendto(data, addr)   # 回显探测内容
                except Exception:
                    pass
            asyncio.ensure_future(_reply())

    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(_UdpProbe,
                                                       local_addr=('0.0.0.0', 20201))
    server = await asyncio.start_server(_handle_ping_probe, '0.0.0.0', 20201)
    log('Ping responder listening on 0.0.0.0:20201 (TCP+UDP, HostingCenter ping)')
    async with server:
        await server.serve_forever()


# ================================================================ main


async def main():
    # 匹配模块依赖注入（PostgreSQL 连接池 + 统一日志）
    matchmaking.configure(STORE, log)
    # OpenID provider：audience = EOS client_id（私服抓包确认）
    # ★ 私服 JWT aud = "xyza7891uO05dmOamk2giEDkkLHD6Taj"，非 Epic Deployment ID
    try:
        from eos import oidc_issuer
        oidc_issuer.configure('xyza7891uO05dmOamk2giEDkkLHD6Taj')
        log('[oidc] configured (audience=xyza7891uO05dmOamk2giEDkkLHD6Taj)')
    except Exception as e:
        log(f'[oidc] configure FAILED: {e}')
    # Run HTTP in a thread, TCP in asyncio
    import threading
    # 连接数据库（失败不阻断启动，仅失去持久化）
    try:
        await STORE.start()
        log(f'[DB] PostgreSQL ready: {STORE.dsn.split("@")[-1]}')
        # 重启孤儿清理：上一次运行遗留的组队中/运行中任务全部置为结束
        #（内存态已丢，不清会被 GetMissionList 当活局展示空壳局）
        try:
            n = await STORE.close_stale_missions()
            if n:
                log(f'[DB] closed {n} stale mission_records (server restart)')
        except Exception as e:
            log(f'[DB] close_stale_missions failed: {e}')
    except Exception as e:
        log(f'[DB] PostgreSQL UNAVAILABLE ({e}) -- 持久化已禁用')
    # 记录主 loop：HTTP 线程的 /register 经它投递 create_player 协程
    global _MAIN_LOOP
    _MAIN_LOOP = asyncio.get_running_loop()
    http_thread = threading.Thread(target=start_http, daemon=True)
    http_thread.start()
    log(f'[HTTP] register page: http://127.0.0.1/register')
    # TCP / ping 监听异常不死进程：内层异常重启服务（端口瞬时占用等场景），
    # 连续快速失败 5 次才放弃（避免错误配置下无限重启刷日志）。
    async def _supervise(name, factory):
        failures = 0
        while True:
            try:
                await factory()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                failures += 1
                log(f'[{name}] CRASHED ({type(e).__name__}: {e}), '
                    f'restart {failures}/5')
                if failures >= 5:
                    log(f'[{name}] giving up after 5 consecutive failures')
                    raise
                await asyncio.sleep(min(2 ** failures, 10))
            else:
                failures = 0        # 正常退出（不应发生）→ 也重启
    try:
        await asyncio.gather(_supervise('TCP', start_tcp),
                             _supervise('PING', start_ping_responder),
                             _supervise('STATS', statistics_server.start))
    finally:
        await STORE.close()


if __name__ == '__main__':
    asyncio.run(main())
