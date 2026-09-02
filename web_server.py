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
  /online.json        内部在线统计（仅限回环地址，管理后台跨进程拉取）
  /admin*             404（管理后台由独立进程 web/server/ 提供，不经 80 端口）
  其余                GAME_DIR 静态文件回退（目录列表与敏感扩展名 404）

启动：main() 里 web_server.init(main_loop) 后 threading.Thread(start_http)。
"""
import asyncio
import bz2
import hashlib
import json
import mimetypes
import os
import re
import time

# Windows 注册表里没有 .webp 的 MIME 映射，guess_type 会返回 None，
# /icons/ 下的压缩 webp 会被当成 octet-stream 下发 → 手动注册
mimetypes.add_type('image/webp', '.webp')

from http.server import (
    SimpleHTTPRequestHandler,
    ThreadingHTTPServer,
    )

from state import (
    FLASH_SESSIONS,
    GAME_DIR,
    PUBLIC_HOST,
    STORE,
    TEXTS_DIR,
    WARDDB_DIR,
    log,
    )

from wardb import WARDB_CAS_MAP, WARDB_SYNC_UNIQUE, WARDB_UNIQUE

# fs 同步 CAS 文件目录：改动过的客户端文件以 <sha256>-<size>_<flag> 为名放入，
# 供 hngsync GET /<sha256>-<size>_<flag> 下载（见 do_GET 内 CAS 路由）
CAS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cas')


# ---- 本地化文件补全 ----
# 客户端 HnG_Strategy_en.xml 的 ErrorMessages/StrategyGame 段缺少部分 ResponseCodes
# 常量对应的 key（如 server_error），购买/操作失败后 Texts.getText 查找失败会显示
# '"ErrorMessages/StrategyGame/xxx" not found' 之类乱码。这里在服务器提供该文件时
# 动态注入缺失 key（不改原始文件）。ok/fail 等已存在，无需注入。
# 另：texts/ 新版（EAC 文案）删掉了全部 StrategyGUI/AntiCheat/battleye_* key，
# 但客户端登录流程（FrontScreenMediator 检测/安装 BattlEye、AntiCheatInformationMediator
# 弹窗）仍会引用，缺失时显示 '"StrategyGUI/AntiCheat/battleye_install_info" not found'，
# 因此从旧版 BattlEyeTlm/HnG_Strategy_en.xml 补回。
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
  <data name="StrategyGUI/AntiCheat/battleye_install_check">
    <value>Checking for battlEye anti-cheat</value>
  </data>
  <data name="StrategyGUI/AntiCheat/battleye_install_info">
    <value>Installing BattlEye anti-cheat</value>
  </data>
  <data name="StrategyGUI/AntiCheat/battleye_install_failure">
    <value>BattlEye anti-cheat was not installed</value>
  </data>
  <data name="StrategyGUI/AntiCheat/battleye_accept_header">
    <value>HEROES &amp; GENERALS USES BATTLEYE</value>
  </data>
  <data name="StrategyGUI/AntiCheat/battleye_accept_calltoaction">
    <value>I HAVE READ THE INFORMATION ABOVE ABOUT ANTI-CHEAT TECHNOLOGIES, \nAND I AGREE TO THE USE OF THESE.</value>
  </data>
  <data name="StrategyGUI/AntiCheat/battleye_information_header">
    <value>1. HEROES &amp; GENERALS USES BATTLEYE
2. BATTLEYE EULA</value>
  </data>
  <data name="StrategyGUI/AntiCheat/battleye_accept_body">
    <value>1. HEROES &amp; GENERALS USES BATTLEYE.
You asked, we listened: New Anti-Cheat system, BattlEye, is now implemented to make Heroes &amp; Generals fun and fair for everyone and provide an effective shield against cheats and hacks. Enjoy!
Please click button below to get started.

AS A FURTHER CONDITION FOR USING HEROES &amp; GENERALS®, YOU AGREE THAT WE MAY USE OUR OWN OR PARTNERS’ ANTI-CHEAT TECHNOLOGIES IN CONNECTION WITH ANY OF OUR GAME SERVICES AND IN PARTICULAR WITH HEROES &amp; GENERALS® ("THE GAME SERVICE").

WHEN PLAYERS CONNECT TO GAME SERVERS, THESE TECHNOLOGIES WILL ACTIVATE AND MONITOR GAME PLAY, THE GAME FILES ASSOCIATED WITH THE GAME SERVICE AND RAM OF THE PLAYER'S COMPUTER. THESE TECHNOLOGIES DETECT CHEATING AND ANY UNAUTHORIZED THIRD-PARTY PROGRAMS RUNNING CONCURRENTLY WITH THE GAME SERVICE AND ANY MODIFICATIONS TO THE GAME SERVICE FILES ENABLING OR FACILITATING CHEATING. AN UNAUTHORIZED THIRD-PARTY PROGRAM IS ANY THIRD-PARTY PROGRAM OR FILE (SUCH AS AN “ADDON”, “MOD”, “HACK”, “TRAINER”, “AIMBOT”, “ESP” OR “CHEAT”) THAT WE BELIEVE ENABLES OR FACILITATES CHEATING OF ANY TYPE, ALLOWS USERS TO MODIFY OR HACK THE GAME SERVICE INTERFACE, ENVIRONMENT, OR EXPERIENCE IN ANY WAY NOT EXPRESSLY AUTHORIZED BY TLM Partners Inc., OR INTERCEPTS, “MINES”, OR OTHERWISE COLLECTS INFORMATION FROM OR THROUGH THE GAME SERVICE. WHEN PLAYERS DISCONNECT FROM THE GAME SERVICE, THESE ANTI-CHEAT TECHNOLOGIES WILL BE AUTOMATICALLY DEACTIVATED.

IF OUR ANTI-CHEAT TECHNOLOGIES SUSPECT CHEATING, WE MAY COLLECT RELEVANT INFORMATION, INCLUDING BUT NOT LIMITED TO YOUR TLM AND/OR HEROES &amp; GENERALS® ACCOUNT NAME, DETAILS ABOUT ANY UNAUTHORIZED THIRD-PARTY PROGRAMS, THE GAME SERVICE FILES MODIFICATION DETECTED, AND THE TIME AND DATE THE CHEATING WAS SUSPECTED TO OCCUR. IF WE DETERMINE A PLAYER HAS BEEN CHEATING, WE MAY ALSO, AT OUR DISCRETION, DECIDE TO TERMINATE THE TARGETED ACCOUNT, EITHER TEMPORARILY OR PERMANENTLY, DEPENDING ON THE OFFENSE BASED ON SUCH FACTORS AS RECURRENCES, SEVERITY OF THE CHEAT, AND IMPACT TO OTHER PLAYERS. IN ADDITION TO TERMINATION OF A PLAYER'S ACCOUNTS, LEGAL ACTION MAY BE TAKEN AGAINST PERSONS OR ENTITIES INFRINGING ON OUR RIGHTS.</value>
  </data>
  <data name="StrategyGUI/AntiCheat/battleye_eula">
    <value>2. BATTLEYE EULA


****** End-User License Agreement ******


This License Agreement ("Agreement") is made between you (the "Licensee") and BattlEye Innovations e.K. (the "Licensor" and the "Data Processor") on behalf of TLM Partners Inc. (the "Data Controller"). The terms of this Agreement apply to all current and future versions and updates of BattlEye anti-cheat software ("BattlEye").


By installing, enabling or using BattlEye, Licensee agrees with all the terms of this Agreement. Licensor reserves all rights not specifically granted and transferred to Licensee.


Licensee understands, acknowledges and agrees with the following:

- Licensor grants Licensee a non-exclusive and non-transferable license to use BattlEye for non-commercial purposes only. Licensee therefore does not own BattlEye, Licensor remains the owner of BattlEye.

- Licensor provides BattlEye on an "as is" basis without warranty of any kind. Licensor neither guarantees the correct, error-free functioning of BattlEye nor is Licensor responsible for any damage caused by the use of BattlEye.

- Licensee may not decompile, disassemble, reverse-engineer, modify or redistribute BattlEye in any way.

- BattlEye will automatically, without notice to Licensee, download and install updates from time to time.

- BattlEye may scan Licensee's entire random access memory (RAM), and any game-related and system-related files and folders on Licensee's system using cheat-program-identifying algorithms, transmit results of such algorithms to other connected computers and/or to Licensor and process such information for the sole purpose of preventing and detecting the use of cheat programs. BattlEye only scans and/or processes data which is reasonably necessary to be scanned and/or processed to meet this purpose.

- BattlEye may collect and process Licensee's Internet Protocol address, game account name and identifier, in-game nickname, and system-related and hardware-related information including, but not limited to, device identifiers and hardware serial numbers.

- Licensor processes all information collected by BattlEye on servers located in the European Union. Licensor may share the information with its partners and/or affiliates.

- Licensee acknowledges that the invasive nature of BattlEye is necessary to meet its purpose and goal of preventing and detecting cheat programs.

- Licensor is allowed to terminate the license at any time for any reason and without notice to Licensee.


This License Agreement constitutes the entire agreement between Licensor and Licensee and supersedes any prior statements.</value>
  </data>
"""

_LOCALIZATION_CACHE = None

# 客户端支持的全部语言（HnGGUIGameVariables.supportedLanguages，与
# combat-server\texts\HnG_Strategy_<lang>.xml 一一对应）
SUPPORTED_LOCALES = ('cs', 'de', 'en', 'es', 'fr', 'ja',
                     'ko', 'pl', 'pt', 'ru', 'tr', 'zh')


def _localization_body():
    """读取原始本地化文件并在 StrategyGame 段后追加缺失 key（结果缓存）。"""
    global _LOCALIZATION_CACHE
    if _LOCALIZATION_CACHE is not None:
        return _LOCALIZATION_CACHE
    # 源 = state.TEXTS_DIR（combat-server\texts，服务器权威副本，含私服
    # 补充 key 如 BackgroundSelector）；hngsync wardb_sync 清单同源。
    # 旧源 BattlEyeTlm/ 是 TLM Partners 时代官方原始版
    lang_file = os.path.join(TEXTS_DIR, 'HnG_Strategy_en.xml')
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
               # 客户端 setLanguage() 查 parameters["locale_"+cookie语言]，查不到会
               # 强制回退英语并重置 cookie（GameContext.setLanguage）——locale 族
               # 必须覆盖 HnGGUIGameVariables.supportedLanguages 全部 12 语言，
               # 否则语言切换永远弹回英文。
               + ''.join(f'locale_{lang}="HnG_Strategy_{lang}.xml";\n'
                         for lang in SUPPORTED_LOCALES)
               + "version=\"173293\";\n"
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
# desktop_client list：swf 登录时让 hngsync 发 CMD_DOWNLOADNAMED
# (set:desktop_client,...)（LocalCallsService.syncGameAssets →
# GameClientAssets.desktop_clientAssestToSync），hngsync 按该清单
# 校验/修复远程客户端的 WarDatabase 表 → 远程改表推送通道。
UNIQUE_FILE_XML = (b'<?xml version="1.0" encoding="utf-8"?>\n<root>\n'
                   b'<list id="WarDatabase" uniquefile="wardb_unique"/>\n'
                   b'<list id="desktop_client" uniquefile="wardb_sync"/>\n'
                   b'<list id="Flash_Resources" uniquefile="flashres_unique"/>\n'
                   b'</root>\n')
# 清单类文件同时提供 .bz2 变体（hngsync 拉清单优先 bz2，见
# hng-client-file-integrity.md §2.5.2）
UNIQUE_FILE_XML_BZ2 = bz2.compress(UNIQUE_FILE_XML)


# texts/ 语言文件并入 hngsync desktop_client 同步清单（远程客户端推送）：
# 官方 currentfiles.txt L17514+ 同样登记 texts/HnG_Strategy_*.xml。
# 推送源 = state.TEXTS_DIR（combat-server\texts，服务器权威副本），逐文件原样
# 登记——客户端本地旧文件哈希/大小不匹配 → hngsync 走 CAS 拉新版写回客户端。
# 注意：HTTP /HnG_Strategy_en.xml 路由读的是 GAME_DIR\texts（客户端侧副本）
# + LOCALIZATION_EXTRA 运行时注入，与本清单互不影响。
def _generate_texts_sync():
    lines = []
    cas = {}
    if os.path.isdir(TEXTS_DIR):
        first = True
        for name in sorted(os.listdir(TEXTS_DIR)):
            fpath = os.path.join(TEXTS_DIR, name)
            if not os.path.isfile(fpath):
                continue
            with open(fpath, 'rb') as f:
                data = f.read()
            sha = hashlib.sha256(data).hexdigest()
            size = len(data)
            # 目录切换：上一节 cur_dir=WarDatabase/，须用 ../ 回退再进 texts/
            # （官方清单同法，如 ../DevIL.dll）；后续条目裸文件名即可
            rel = f'../texts/{name}' if first else name
            first = False
            lines.append(f'"{rel}",{sha}-{size}_0,{size}')
            cas[f'{sha}-{size}_0'] = data
    body = ('\n'.join(lines) + '\n').encode('utf-8') if lines else b''
    return body, cas


TEXTS_SYNC_UNIQUE, TEXTS_CAS_MAP = _generate_texts_sync()

# 完整 desktop_client 清单 = WarDatabase 表 + texts 语言文件
WARDB_SYNC_FULL = WARDB_SYNC_UNIQUE + TEXTS_SYNC_UNIQUE
WARDB_SYNC_BZ2 = bz2.compress(WARDB_SYNC_FULL)

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


# 武器变体图标回退：客户端 HnGGUIWeaponImage 对带 equippedWeaponVariantId 的
# 武器实例请求 images/Weapons/<tid>_<vid>.png（Thumbnails/ 同理），而本地素材库
# 只有部分变体组合的图（如 90_341.png 存在、90_340.png 缺失）。变体图缺失时
# 回退基础图标 <tid>.png，避免礼包发货的变体武器图标空白（loadFailed 后会被
# 记入 _dontTryLoad，整个会话都不再重试）。
_WEAPON_VARIANT_IMG_RE = re.compile(
    r'^/+images/Weapons/(Thumbnails/)?(\d+)_(\d+)\.png$')


def _serve_weapon_variant_fallback(handler, path):
    m = _WEAPON_VARIANT_IMG_RE.match(path)
    if not m:
        return False
    weapons_root = os.path.realpath(os.path.join(GAME_DIR, 'images', 'Weapons'))
    rel_dir = 'images/Weapons/' + (m.group(1) or '')
    full = os.path.realpath(
        os.path.join(GAME_DIR, rel_dir, f'{m.group(2)}_{m.group(3)}.png'))
    if not full.startswith(weapons_root + os.sep) or not os.path.isfile(full):
        return False  # 变体图存在，交给静态处理器
    base = os.path.realpath(os.path.join(GAME_DIR, rel_dir, f'{m.group(2)}.png'))
    if not base.startswith(weapons_root + os.sep) or not os.path.isfile(base):
        return False
    ctype = mimetypes.guess_type(base)[0] or 'image/png'
    with open(base, 'rb') as f:
        body = f.read()
    handler.send_response(200)
    handler.send_header('Content-Type', ctype)
    handler.send_header('Content-Length', str(len(body)))
    handler.send_header('Cache-Control', 'no-cache')
    handler.end_headers()
    handler.wfile.write(body)
    log(f'[HTTP] weapon variant icon fallback {path} -> {m.group(2)}.png')
    return True


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


def _online_snapshot():
    """跨进程在线统计快照：FLASH_SESSIONS（大厅+战斗中的 Flash 连接）+
    匹配队列 + 活跃战局名单。

    供管理后台（独立进程 admin_main.py）经 127.0.0.1:80/online.json 拉取。
    HTTP 线程与主 asyncio 线程并发读写这些 dict——list() 拷贝期间撞上
    增删抛 RuntimeError 时按空表处理（下次轮询自会拿到一致快照）。
    matchmaking 惰性 import：web_server 在 realtest_server 启动序里先于
    matchmaking 就绪，也避免任何潜在循环依赖。"""
    import matchmaking

    try:
        items = sorted(FLASH_SESSIONS.items())
    except RuntimeError:
        items = []
    players = [{'username': name, 'player_id': sess.player_id}
               for name, sess in items]

    try:
        det_items = list(matchmaking._MM_MISSION_DETAILS.items())
    except RuntimeError:
        det_items = []
    missions = [{
        'mission_id': mid,
        'preset': det.get('preset'),
        'gamemode': det.get('gamemode'),
        'attacker_faction': det.get('attacker_faction'),
        'defender_faction': det.get('defender_faction'),
        'players': [{'gamertag': p.get('gamertag'),
                     'faction': p.get('faction')}
                    for p in det.get('players', [])],
    } for mid, det in sorted(det_items)]

    return {
        'online': len(players),
        'in_queue': len(matchmaking._MM_QUEUES),
        'in_battle': len(matchmaking.PMS_TO_SESSION),
        'players': players,
        'missions': missions,
        'server_time': int(time.time()),
    }


def _handle_online_api(handler):
    """GET /online.json：内部在线统计（仅限回环地址，公网请求 403）。"""
    ip = handler.client_address[0]
    if ip not in ('127.0.0.1', '::1'):
        _json_reply(handler, {'error': 'forbidden'}, 403)
        log(f'[HTTP] BLOCKED /online.json from {ip} (非本机)')
        return
    _json_reply(handler, _online_snapshot())


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
            # swf 维护检查 onStatusReceived 的开服判定与域名/IP 版本相反：
            # 域名访问（streamURL 含 "game" → isGame=true）需 <400 才判开服；
            # IP 直连版 swf（isGame=false）需 >400 才判开服，回 200 反而弹维护屏
            host = (self.headers.get('Host') or '').lower()
            ok = 'hng-group.com' in host
            self.send_response(200 if ok else 404)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'OK' if ok else b'Not Found')
            return
        if path == '/online.json' or path == '//online.json':
            _handle_online_api(self)
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
            self.send_header('Content-Length', str(len(UNIQUE_FILE_XML)))
            self.end_headers()
            self.wfile.write(UNIQUE_FILE_XML)
            log(f'[HTTP] served /hng_unique_v173293 ({len(UNIQUE_FILE_XML)}B)')
            return
        if path == '/hng_unique_v173293.bz2' or path == '//hng_unique_v173293.bz2':
            # hngsync 拉清单优先 bz2 变体
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(len(UNIQUE_FILE_XML_BZ2)))
            self.end_headers()
            self.wfile.write(UNIQUE_FILE_XML_BZ2)
            log(f'[HTTP] served /hng_unique_v173293.bz2 ({len(UNIQUE_FILE_XML_BZ2)}B)')
            return
        if path == '/wardb_unique' or path == '//wardb_unique':
            # WarDatabase manifest: 列出所有静态表文件（表名,sha256-size）
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Length', str(len(WARDB_UNIQUE)))
            self.end_headers()
            self.wfile.write(WARDB_UNIQUE)
            tbl_count = WARDB_UNIQUE.count(b'\n')
            log(f'[HTTP] served /wardb_unique ({len(WARDB_UNIQUE)}B, {tbl_count} tables)')
            return
        if path in ('/wardb_sync', '//wardb_sync', '/wardb_sync.bz2',
                    '//wardb_sync.bz2'):
            # hngsync（set:desktop_client）用清单：官方 3 列 wire 格式，
            # 覆盖 WarDatabase 表 + texts 语言文件（远程改表/语言文件推送通道）。
            # hngsync 解析响应依赖 Content-Length（Http_FileDownload），必须显式下发。
            body = WARDB_SYNC_BZ2 if path.endswith('.bz2') else WARDB_SYNC_FULL
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            log(f'[HTTP] served {path} ({len(body)}B)')
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
        # 根路径 /HnG_Strategy_<lang>.xml（可能带 ?resetCache=N），GAME_DIR 根目录
        # 下没有该文件 → 404 → 客户端重试失败后闪退，必须返回服务器版。
        # en 走 _localization_body（TEXTS_DIR 版 + LOCALIZATION_EXTRA 注入）；
        # 其余语言直接下发 TEXTS_DIR 原文件（含私服补充 key）。
        m = re.fullmatch(r'/+HnG_Strategy_([a-z]{2})\.xml', path)
        if m and m.group(1) in SUPPORTED_LOCALES:
            lang = m.group(1)
            if lang == 'en':
                body = _localization_body()
            else:
                try:
                    with open(os.path.join(TEXTS_DIR,
                                           f'HnG_Strategy_{lang}.xml'), 'rb') as f:
                        body = f.read()
                except OSError:
                    body = None
            if body:
                self.send_response(200)
                self.send_header('Content-Type', 'application/xml')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                log(f'[HTTP] served HnG_Strategy_{lang}.xml ({len(body)}B)')
                return
        # fs 同步 CAS 下发：hngsync 以 GET /<sha256>-<size>_<flag> 拉文件本体。
        # 三个来源：
        #   1. cas/ 目录：手工放入的改动文件（如 IP 版 HeroesAndGeneralsDesktop.swf）
        #      —— 与 currentfiles.txt 登记的哈希保持一致；
        #   2. WARDB_CAS_MAP 回源：hngsync 按 wardb_sync 清单修复远程客户端的
        #      WarDatabase 表时，CAS 名即当前表文件的 sha256-size_0，直接回源
        #      WarDatabase 目录（改表后无需手工生成 cas 文件，重启服务器即可）；
        #   3. TEXTS_CAS_MAP 回源：texts 语言文件（TEXTS_DIR 原始字节，与
        #      wardb_sync 清单同源），直接回内存字节。
        # 注意：hngsync 解析响应依赖 Content-Length（Http_FileDownload 0x1400410F0），
        # 此处必须显式下发该头。
        m = re.fullmatch(r'/([0-9a-f]{64}-\d+_\d+)', path)
        if m:
            cas_name = m.group(1)
            cas_path = os.path.realpath(os.path.join(CAS_DIR, cas_name))
            body = None
            if (cas_path.startswith(os.path.realpath(CAS_DIR) + os.sep)
                    and os.path.isfile(cas_path)):
                with open(cas_path, 'rb') as f:
                    body = f.read()
            if body is None:
                wb = WARDB_CAS_MAP.get(cas_name)
                if wb and os.path.isfile(wb):
                    with open(wb, 'rb') as f:
                        body = f.read()
            if body is None:
                body = TEXTS_CAS_MAP.get(cas_name)
            if body is not None:
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                log(f'[HTTP] served CAS /{cas_name} ({len(body)}B)')
            else:
                self.send_response(404)
                self.send_header('Content-Length', '0')
                self.end_headers()
                log(f'[HTTP] 404 CAS /{cas_name} (not in cas dir / wardb / texts)')
            return
        # 安全校验：目录列表与敏感扩展名一律 404（统一在静态回退前拦截）
        if _deny_static(path):
            self.send_error(404, 'Not Found')
            return
        # 武器变体图标回退（须在静态处理器之前）
        if _serve_weapon_variant_fallback(self, path):
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
