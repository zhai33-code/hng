"""HnG 私服 HTTP 服务（独立线程，ThreadingHTTPServer 0.0.0.0:80）。

从 realtest_server.py 拆出（2026-08-26）：HTTP 是公网安全敏感面
（注册 / 静态资源下发 / WarDatabase 表服务），独立成模块便于审计。

职责：
  /join /join/veteran 入伍 SPA（Vite/Vue，web/dist，HnG 官方风格：
                     /join 新兵报到注册、/join/veteran 老兵归队外服迁移；
                     客户端登录界面 account_url 指向 /join）
  /api/register       账号注册 JSON API（经主 loop 投递 STORE.create_player）
  /api/captcha/get    滑动验证码出题（register_captcha.py，pi-captcha 出图）
  /api/captcha/check  验证码校验 → 签发一次性注册令牌
  /settings.js        客户端入口配置（warserver 地址 / 静态资源地址 / statserver 等）
  /hng_unique_v173293 静态数据文件清单 XML
  /wardb_unique       WarDatabase 表清单（表名,sha256-size）
  /WarDatabase/<tbl>  单个静态表文件下发
  /flashres_unique    Flash_Resources 清单（images/Rank 军衔图标）
  /undefined, /HnG_Strategy_en.xml 等本地化文件（动态注入缺失 key）
  /online.json        内部在线统计（仅限回环地址，管理后台跨进程拉取）
  /admin*             404（管理后台由独立进程 web/server/ 提供，不经 80 端口）
  扫描封禁            扫描器黑名单命中（脚本扩展名/敏感点文件/已知探测路径）→
                     封禁来源 IP 一段时间（scan_bans.json 持久化，重启保留；
                     回环地址永不封禁，仅作用于 HTTP 80）
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

import register_captcha

from http.server import (
    SimpleHTTPRequestHandler,
    ThreadingHTTPServer,
    )
import socket
import threading

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
import combat_hosts
import combat_instances
import mm_state

# fs 同步 CAS 文件目录：改动过的客户端文件以 <sha256>-<size>_<flag> 为名放入，
# 供 hngsync GET /<sha256>-<size>_<flag> 下载（见 do_GET 内 CAS 路由）
CAS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cas')

# CAS 文件 .bz2 变体内存缓存：hngsync 拉 CAS 文件优先 GET <name>.bz2
# （downloadfiles.log 实锤），MB 级 dll 现压太慢，压缩一次复用
_CAS_BZ2_CACHE = {}


def _cas_bz2_body(cas_name, body):
    hit = _CAS_BZ2_CACHE.get(cas_name)
    if hit is None:
        hit = bz2.compress(body)
        _CAS_BZ2_CACHE[cas_name] = hit
    return hit


# ---- hngsync .sgn 动态签名（2026-09-06 定案，hng-client-file-integrity.md §2.5.6）----
# hngsync 下载清单/文件走 sub_1400483A0：GET <name>.bz2 成功后必再 GET
# <name>_<年>.sgn，sub_1400516E0 验签 = CryptQueryObject 从本机 hngservice.exe
# 的 Authenticode 证书取公钥（实测日志 "using D:\...\hngservice.exe"）→
# SHA1(明文字节) → CryptVerifySignatureA（RSA PKCS#1 v1.5，CryptoAPI
# little-endian 字节序）。
# 缺 .sgn（404）= 文件不落盘 → 空同步"成功"（实测最后断链点
# 6d0f77e2…-5178_0_2026.sgn 404）。闭环方案（私服同款）：客户端
# hngservice.exe 已用 certs/hngsync_codesign.pfx 的证书重签
# （Set-AuthenticodeSignature，CN=hng-ps-codesign thumb F66D0D7C…，pfx
# 文件名 hngsync_* 为历史遗留），服务器在此用同一私钥对
# 文件明文动态签名（Http_FileDownload 收 .bz2 后解压，验签哈希明文）。
_CODESIGN_PFX = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             'certs', 'hngsync_codesign.pfx')
_SGN_KEY = None
if os.path.isfile(_CODESIGN_PFX):
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.serialization import pkcs12
        with open(_CODESIGN_PFX, 'rb') as f:
            _SGN_KEY, _sgn_cert, _ = pkcs12.load_key_and_certificates(
                f.read(), b'hngps')
    except Exception as _e:
        print(f'[web_server] codesign pfx load failed: {_e}')
_SGN_CACHE = {}


def _sgn_body(cas_name, plain_body):
    """对文件明文做 CryptoAPI 兼容签名（CryptVerifySignatureA）。

    hngsync 端 Http_FileDownload 收 .bz2 后内部解压，验签哈希的是明文
    （sub_1400516E0 CryptHashData，data_ 转储实测），故签名对象 = 明文。
    """
    if _SGN_KEY is None:
        return None
    hit = _SGN_CACHE.get(cas_name)
    if hit is None:
        # CryptVerifySignatureA 期望 little-endian 签名 blob，需字节反转
        hit = _SGN_KEY.sign(
            plain_body, padding.PKCS1v15(), hashes.SHA1())[::-1]
        _SGN_CACHE[cas_name] = hit
    return hit


# 本地化文件补全（2026-09-07 起）：客户端引用而 texts/ 新版（EAC 文案）缺失的
# key 已全部固化进 combat-server\texts\HnG_Strategy_<lang>.xml 源文件——
#   ① StrategyGUI/AntiCheat/battleye_* 8 项 + ExternalText/Legal/BattlEyeInfo，
#     从旧版官方 BattlEyeTlm/HnG_Strategy_<lang>.xml 原样合并（12 语言）；
#   ② ErrorMessages/StrategyGame/* 16 项（server_error/not_for_sale 等私服
#      ResponseCodes 文案），en 沿用原运行时注入文本，其余语言补充翻译。
# 此前仅英语走 LOCALIZATION_EXTRA 运行时注入，其余语言缺 key；现所有语言
# 统一直接下发源文件，语言路由无特判。
# 2026-09-10 增补：ErrorMessages/StrategyGame/no_resupply_in_transport
# （ResponseCode 116，resupply_request 在 AT 运输 state=1 时返回）12 语言
# 全缺 → 客户端弹 "not found"；文案取各语言已有 no_resupply_commandnode_
# in_transport（122）译文，语义一致。另修 cs 文件头 '<?xml' 前杂散文本
# 'hng-group'（XML 损坏，Flash 解析失败风险）。

# 客户端支持的全部语言（HnGGUIGameVariables.supportedLanguages，与
# combat-server\texts\HnG_Strategy_<lang>.xml 一一对应）
SUPPORTED_LOCALES = ('cs', 'de', 'en', 'es', 'fr', 'ja',
                     'ko', 'pl', 'pt', 'ru', 'tr', 'zh')


# 对外地址统一取 state.PUBLIC_HOST（HNG_PUBLIC_HOST 环境变量 / .env）：
# flash_dynhttp=客户端连 warserver 的 TCP、unique_file_servers=HTTP 静态资源、
# account_url=账号页、ircserver=聊天（与 irc_server.LISTEN_PORT 一致）。
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
               # hngsync settings.js 解析器（settings_js_parse）要求 4 个必需键，
               # 缺 retoxdistribution 整体 return 0 → DOWNLOADNAMED 同步中止
               # （2026-09-05 根因：hngsync 零网络请求、currentfiles.txt 被清 0）。
               # 值与官方 live 实测一致（hng-client-file-integrity.md §2.5.1）
               "retoxdistribution=\"live\";\n"
               "language=\"en\";\n"
               "releasename=\"QQ\";\n"
               f"ircserver=\"{PUBLIC_HOST}:16667\";\n"
               # 战斗内勋带弹窗：statistics_client.dll GetTotals 统计服务器
               # （swf 将 settings.js 变量转发进 hngsync CMD_RUN_HNG -> hng.exe /R）
               f"statserver=\"{PUBLIC_HOST}\";\n"
               "statserverport=\"15115\";\n"
               f"account_url=\"http://{PUBLIC_HOST}/join\";\n"
               "uniqueversionfilename=\"hng_unique_v173293\";\n"
               "siteonly=\"false\";\n").encode()

# hng_unique_v173293: 静态数据文件清单，客户端登录成功后 hngsync 请求。
# 根元素必须是 <uniquelists>：hngsync 的 XML 解析器（sub_1400430F0）只认它，
# 写成 <root> 会导致解析失败 → set 全部无法解析 → 同步第 1 步即中止，
# cas/ 里的补丁文件永远不会被比对下载（2026-09-05 实锤：客户端拉取
# /hng_unique_v173293 后，服务器日志再无任何清单/CAS 请求）。
# list 条目 = swf 登录同步固定请求的集合（HngSyncDownloadSession）：
#   Game_Client_Executables_64  m_sMandatory 强制集，缺失会让同步报错
#   desktop_client              → wardb_sync（WarDatabase 表 + texts + 补丁 player.dll）
#   Flash_Resources             → flashres_unique（军衔图标清单）
#   WarDatabase                 → wardb_unique（表清单）
# 清单体在文件后段生成（见 MANIFEST_BODIES）：uniquefile 必须用 CAS 名，
# 不能是裸名——hngsync 下载后强制校验文件名前缀（详见后段注释）。
# 清单类文件同时提供 .bz2 变体（hngsync 拉清单优先 bz2，见
# hng-client-file-integrity.md §2.5.2）

# GCE64 集清单：swf 端 HngSyncDownloadSession.m_sMandatory 写死的强制同步集，
# 官方清单登记 hng.exe/player.dll 等 663 个游戏二进制。本地客户端二进制是
# 手工维护的基线（如 7235512C 版 player.dll），不逐一登记真实哈希；补丁文件
# 推送统一走 desktop_client（wardb_sync），避免两个清单对同一文件登记不同
# 哈希导致反复互拉。
# 2026-09-06 根因修正（hngsync.exe 逆向实锤，详见 hng-client-file-integrity.md
# §2.5.7）：子清单解析出 0 条目会被 sub_14004BB40 当作整体失败（return 0）
# → sub_14004A5D0 管线提前退出、不发 0x2002 完成消息 → 监控线程
# sub_14001FF40 死等 byte_1400B4D35 → 客户端登录同步卡死（三行 Downloading
# 后 code 200 永不返回）。故不管理的集合也必须登记 ≥1 个真实条目：引用
# cas/ 已部署的 64 字节 nop.txt（CAS 名同 _SMALLFILES_BUNDLE，行格式同
# player.dll 已验证的三字段格式），客户端下载校验一次即过，文件落在客户端
# 根目录，无害。
_NOP_CAS = ('e2d5813ed1e0b3eba3e85ea9591bf436a6e808d5'
            '2b84ddd7df894a74ee67c5aa-64_0')
GCE64_UNIQUE = f'"nop.txt",{_NOP_CAS},64\n'.encode('utf-8')
GCE64_UNIQUE_BZ2 = bz2.compress(GCE64_UNIQUE)


# texts/ 语言文件并入 hngsync desktop_client 同步清单（远程客户端推送）：
# 官方 currentfiles.txt L17514+ 同样登记 texts/HnG_Strategy_*.xml。
# 推送源 = state.TEXTS_DIR（combat-server\texts，服务器权威副本），逐文件原样
# 登记——客户端本地旧文件哈希/大小不匹配 → hngsync 走 CAS 拉新版写回客户端。
# HTTP /HnG_Strategy_<lang>.xml 路由与 CAS 回源（TEXTS_CAS_MAP）读同一份
# 源文件，两条通道字节一致（缺失 key 已固化进源文件，无运行时注入）。
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


# player.dll 雪补丁版推送（snow-unified-patch.md 方案一 + §8 rollout）：
# 本体 = cas/ 目录手工放置的补丁版 dll（CAS 名 = sha256-<size>_0，由
# work/patch_playerdll_snow.py 生成，客户端基线 7235512C 不含战斗服 EAC
# 适配 6 字节）；此处启动时按尺寸 9941400（与客户端 player.dll 同 build
# 同尺寸）识别并登记进 desktop_client 清单 → 登录期 hngsync 比对客户端
# player.dll 哈希不符 → 走 CAS 通道自动下载补丁版（客户端 dll 自动更新）。
# 文件本体下发走 CAS 路由第 1 优先级（cas/ 目录）。
# 回滚 = 删 cas 文件 + 重启 warserver：清单不再登记，客户端保持现状
# （如需强制还原官方版，另放官方哈希的 cas 文件并登记即可）。
def _generate_playerdll_sync():
    if os.path.isdir(CAS_DIR):
        for fn in os.listdir(CAS_DIR):
            m = re.fullmatch(r'([0-9a-f]{64})-(\d+)_0', fn)
            if not m:
                continue
            sha, size = m.group(1), int(m.group(2))
            if size == 9941400 and os.path.isfile(os.path.join(CAS_DIR, fn)):
                # 目录切换：上一节 cur_dir=texts/，须用 ../ 回退到客户端根
                return f'"../player.dll",{sha}-{size}_0,{size}\n'.encode('utf-8')
    return b''


PLAYERDLL_SYNC_UNIQUE = _generate_playerdll_sync()

# 完整 desktop_client 清单 = WarDatabase 表 + texts 语言文件 + player.dll
WARDB_SYNC_FULL = (WARDB_SYNC_UNIQUE + TEXTS_SYNC_UNIQUE
                   + PLAYERDLL_SYNC_UNIQUE)
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
        # 空清单会被 hngsync 视为失败（见 GCE64_UNIQUE 注释），兜底登记 nop.txt
        return GCE64_UNIQUE, {}
    files = sorted(n for n in os.listdir(root)
                   if os.path.isfile(os.path.join(root, n)))
    lines = []
    cas = {}
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
        # 注意条目名无 _0 后缀（flashres 格式），hngsync 按注册名原样请求
        # GET /<sha>-<size>（.bz2/.sgn 同名），CAS 路由须兼容无 _0 形式
        lines.append(f'"{rel_path}",{sha}-{size},{size},0,0')
        cas[f'{sha}-{size}'] = data
        cur_dir = _pp.dirname(rel)
    return ('\n'.join(lines) + '\n').encode('utf-8'), cas


FLASHRES_UNIQUE, FLASHRES_CAS_MAP = _generate_flashres_unique()
FLASHRES_UNIQUE_BZ2 = bz2.compress(FLASHRES_UNIQUE)
WARDB_UNIQUE_BZ2 = bz2.compress(WARDB_UNIQUE)
_flashres_n = FLASHRES_UNIQUE.count(b'\n')
log(f'[init] flashres_unique generated: {_flashres_n} files, '
    f'{len(FLASHRES_UNIQUE)}B (scanned {os.path.join(GAME_DIR, "images", "Rank")})')


# ================================================================ hngsync 清单 CAS 命名
# 2026-09-06 根因（hngsync.exe 逆向实锤，hng-client-file-integrity.md §2.5.5）：
# hngsync 下载任何清单后强制校验——sha256(明文内容) 的 64 位小写 hex 必须是
# 请求文件名的前缀（sub_14003A7F0 = strncmp(文件名, hex, 64)；sub_14003D5D0
# = SHA-256；sub_14003F430 = hex 小写编码）。裸名（hng_unique_v173293 /
# wardb_sync / ...）不满足 → "Checksum failure" → 清单从不落盘缓存 →
# 所有 set 查空 → 空同步但协议层报 200 成功（text/player.dll 推送失效）。
# 官方 CAS 名 <sha256>-<size>_0 天然满足前缀规则（官方 downloadfiles.log 的
# file list 均为 CAS 名），故所有 hngsync 清单一律按自身内容哈希命名，
# 经 MANIFEST_CAS_MAP 按名回源（.bz2 变体按需压缩）。
def _cas_name(body):
    return f'{hashlib.sha256(body).hexdigest()}-{len(body)}_0'


# 子清单体（= <uniquelists> 各 <list> 的 uniquefile 目标，明文；顺序即 XML 顺序）
# 集合与顺序 = 参考私服主清单（synccache111 9013d578…-1654_0）同构：13 个裸 id。
# 未管理的集合一律空清单（b'\n'，与 GCE64_UNIQUE 同体共享同一 CAS 名合法），
# 即集合可解析、0 文件校验（基线二进制手工维护，不推送）。
MANIFEST_BODIES = {
    'DirectX': GCE64_UNIQUE,                      # 登录基础集，空
    'Game_Client_Executables_64': GCE64_UNIQUE,   # 强制集（刻意置空）
    'game_client_executables_linux': GCE64_UNIQUE,
    'desktop_client_boot': GCE64_UNIQUE,
    'desktop_client': WARDB_SYNC_FULL,            # 表 + texts + 补丁 player.dll
    'T00M00_AllLevels': GCE64_UNIQUE,
    'Flash_Resources': FLASHRES_UNIQUE,           # 军衔图标
    'Game_Client_data': GCE64_UNIQUE,
    'Game_Client_texts': GCE64_UNIQUE,
    'Game_Client_video': GCE64_UNIQUE,
    'RetoX_Depends': GCE64_UNIQUE,
    'WarDatabase': WARDB_UNIQUE,                  # 表清单
    'SmallFiles': GCE64_UNIQUE,                   # 登录基础集，空
}
MANIFEST_CAS_MAP = {_cas_name(b): b for b in MANIFEST_BODIES.values()}

# 主清单 uniquelists：与参考私服逐属性同构——
#   <?xml version="1.0" ?>（无 encoding）、tab 缩进、13 个裸 id（无 "set:" 前缀副本）、
#   SmallFiles 带 uniquefilebundle。
# 2026-09-06 实测：双 id + encoding="utf-8" 版主清单虽能被 hngsync 拉取落盘，
# 但解析后不发任何子清单请求（参考版则触发 desktop_client 子清单下载），
# 故除 uniquefile 指向外全盘照抄参考格式（差异项未逐一二分）。
_SMALLFILES_BUNDLE = _NOP_CAS  # = nop.txt 本体，cas/ 已部署
_lines = []
for _lid, _body in MANIFEST_BODIES.items():
    if _lid == 'SmallFiles':
        _lines.append(f'\t<list id="{_lid}" uniquefile="{_cas_name(_body)}" '
                      f'uniquefilebundle="{_SMALLFILES_BUNDLE}"/>')
    else:
        _lines.append(f'\t<list id="{_lid}" uniquefile="{_cas_name(_body)}"/>')
UNIQUE_FILE_XML = ('<?xml version="1.0" ?>\n<uniquelists>\n'
                   + '\n'.join(_lines) + '\n</uniquelists>\n').encode('utf-8')
UNIQUE_FILE_XML_BZ2 = bz2.compress(UNIQUE_FILE_XML)
UNIQUE_VERSION_FILENAME = _cas_name(UNIQUE_FILE_XML)
MANIFEST_CAS_MAP[UNIQUE_VERSION_FILENAME] = UNIQUE_FILE_XML
# settings.js 下发给客户端的清单名（swf 经 1003 param[2] 原样转发给 hngsync）
SETTINGS_JS = SETTINGS_JS.replace(
    b'uniqueversionfilename="hng_unique_v173293";',
    f'uniqueversionfilename="{UNIQUE_VERSION_FILENAME}";'.encode())
log(f'[init] hngsync manifests CAS-named: uniqueversionfilename='
    f'{UNIQUE_VERSION_FILENAME} ({len(MANIFEST_CAS_MAP)} manifests)')
log(f'[init] hngsync .sgn codesign key: '
    f'{"loaded" if _SGN_KEY is not None else "MISSING"}')


def _cas_source_body(cas_name):
    """CAS 名 → 明文体，按 CAS 路由的 5 级回源（cas/ 目录 → 内存清单
    → WarDatabase 表 → texts → flashres 图标）。.bz2 与 .sgn 路由共用，
    保证同源。"""
    cas_path = os.path.realpath(os.path.join(CAS_DIR, cas_name))
    if (cas_path.startswith(os.path.realpath(CAS_DIR) + os.sep)
            and os.path.isfile(cas_path)):
        with open(cas_path, 'rb') as f:
            return f.read()
    body = MANIFEST_CAS_MAP.get(cas_name)
    if body is not None:
        return body
    wb = WARDB_CAS_MAP.get(cas_name)
    if wb and os.path.isfile(wb):
        with open(wb, 'rb') as f:
            return f.read()
    body = TEXTS_CAS_MAP.get(cas_name)
    if body is not None:
        return body
    # flashres 军衔图标（注册名 <sha>-<size>，无 _0，与清单条目同名）
    return FLASHRES_CAS_MAP.get(cas_name)


# ================================================================ 入伍页（Vue SPA）
# 前端源码在仓库内 web/frontend（Vite + Vue3 + TS，MPA 双入口：join + admin），
# 构建产物 web/frontend/dist 由本模块下发：
#   /join /join/veteran    -> dist/join.html（history 路由：注册 / 外服迁移）
#   /assets/*              -> dist/assets/*（vite 构建的 js/css）
#   /icons/* /soldier.png  -> dist 根下复制的客户端素材（frontend/public 随 build 并入）
# POST /api/register       -> JSON 建号，经主 loop 投递 STORE.create_player。
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


def _client_ip(handler):
    """客户端真实 IP（IP 直连，client_address 可信；XFF 可伪造一律不信任）。"""
    try:
        return handler.client_address[0] if handler.client_address else None
    except Exception:
        return None


def _handle_captcha_get(handler):
    """GET /api/captcha/get：出题（服务端出题，前端 go-captcha-vue 展示）。"""
    data = register_captcha.issue_captcha(_client_ip(handler))
    if data is None:
        # 区分两种不可用：缺 pi-captcha 库 / captcha_assets 素材 vs 限速命中
        if not register_captcha.is_available():
            log('[captcha] 503: pi-captcha lib or captcha_assets/ missing')
        else:
            log('[captcha] 503: rate-limited or empty resources')
        _json_reply(handler, {'error': '验证码服务不可用，请稍后再试'}, 503)
        return
    log('[captcha] issued challenge')
    _json_reply(handler, data)


def _handle_captcha_check(handler):
    """POST /api/captcha/check：校验滑动坐标，签发一次性注册令牌。"""
    try:
        length = int(handler.headers.get('Content-Length') or 0)
        if length > 4096:
            _json_reply(handler, {'error': '请求过大'}, 413)
            return
        raw = handler.rfile.read(length) if length > 0 else b''
        data = json.loads(raw or b'{}')
    except Exception:
        _json_reply(handler, {'error': '请求格式错误'}, 400)
        return
    token = register_captcha.check_captcha(
        data.get('captcha_id'), data.get('x'), data.get('y'),
        ip=_client_ip(handler))
    if token is None:
        log(f'[captcha] check failed ip={_client_ip(handler)} (wrong/expired/rate-limited)')
        _json_reply(handler, {'error': '验证失败，请重试'}, 401)
        return
    _json_reply(handler, {'ok': True, 'token': token})


def _record_web_login_event(player_id, username, ip, result, source):
    """web 侧（注册/导入成功）写 login_events；失败仅日志不影响主流程。

    HTTP handler 跑在线程池，须投递主 loop（与 create_player 同模式），
    fire-and-forget 不等待结果。"""
    if _MAIN_LOOP is None or STORE.pool is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(
            STORE.record_login_event(player_id, username, ip, result,
                                     source=source),
            _MAIN_LOOP)
    except Exception as e:
        log(f'[{source}] record_login_event failed: {e}')


def _handle_register_api(handler):
    """POST /api/register：JSON -> 蜜罐/令牌/限速 -> 投递 create_player。"""
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

    # 蜜罐：隐藏字段被填 = 脚本，静默拒绝（对外报通用错误）
    if register_captcha.honeypot_hit(data):
        log(f'[register] BLOCKED honeypot (username={username!r})')
        _json_reply(handler, {'error': '无法注册，请稍后重试'}, 400)
        return
    # 人机验证令牌：一次性消费（fail-closed——无令牌一律拒绝）
    if not register_captcha.consume_token(data.get('token')):
        _json_reply(handler, {'error': '请先完成人机验证'}, 403)
        return
    # 限速兜底：单 IP 为主 + 全局兜底（令牌校验之后、DB 写入之前）
    if not register_captcha.allow_register(_client_ip(handler)):
        log(f'[register] rate-limited ip={_client_ip(handler)} username={username!r}')
        _json_reply(handler, {'error': '注册过于频繁，请稍后再试'}, 429)
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
    # 客户端登录界面 LoginModel.checkPassword 要求密码 >=6 位（SWF 实锤），
    # 服务端必须同口径，否则 4/5 位密码注册成功却登不进游戏。
    if not (6 <= len(password) <= 64):
        _json_reply(handler, {'error': '密码长度需为 6-64 位'}, 400)
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
        _record_web_login_event(pid, username, _client_ip(handler), 97, 'register')
        _json_reply(handler, {'ok': True, 'player_id': pid}, 201)


def _foreign_server_addr():
    """外服地址（.env / 环境变量 HNG_FOREIGN_SERVER=host:port）。"""
    raw = os.environ.get('HNG_FOREIGN_SERVER', '51.68.235.145:15120').strip()
    host, _, port = raw.rpartition(':')
    if not host or not port.isdigit():
        log(f'[import] HNG_FOREIGN_SERVER 配置无效: {raw!r}，用默认值')
        return '51.68.235.145', 15120
    return host, int(port)


# 导入全流程（登录外服 + 采集 15s + 落库）在主 asyncio 循环上跑，
# HTTP 线程只等待结果；采集失败自动重试一次（订阅超时 60s ×2 + 采集窗口），
# 210s 覆盖最坏情况：首次 62s 失败 + 2s 退避 + 重试完整 ~100s + 落库
_IMPORT_TIMEOUT_S = 210


def _handle_import_api(handler):
    """POST /api/import：外服账密 + 新本地账密 → 采集 → 建号导入。

    校验口径与 /api/register 完全一致（蜜罐/人机令牌/限速/用户名规则），
    之后在主循环跑 foreign_import.run_import（单事务，重复导入/用户名
    冲突/外服密码错误均类型化拒绝）。"""
    import foreign_import
    try:
        length = int(handler.headers.get('Content-Length') or 0)
        if length > 65536:
            _json_reply(handler, {'error': '请求过大'}, 413)
            return
        raw = handler.rfile.read(length) if length > 0 else b''
        data = json.loads(raw or b'{}')
        foreign_username = (data.get('foreign_username') or '').strip()
        foreign_password = data.get('foreign_password') or ''
        username = (data.get('username') or '').strip()
        password = data.get('password') or ''
    except Exception as e:
        _json_reply(handler, {'error': '请求格式错误'}, 400)
        log(f'[import] json parse failed: {e}')
        return

    if register_captcha.honeypot_hit(data):
        log(f'[import] BLOCKED honeypot (username={username!r})')
        _json_reply(handler, {'error': '无法迁移，请稍后重试'}, 400)
        return
    if not register_captcha.consume_token(data.get('token')):
        _json_reply(handler, {'error': '请先完成人机验证'}, 403)
        return
    if not register_captcha.allow_register(_client_ip(handler)):
        log(f'[import] rate-limited ip={_client_ip(handler)} username={username!r}')
        _json_reply(handler, {'error': '操作过于频繁，请稍后再试'}, 429)
        return

    if not foreign_username or not foreign_password:
        _json_reply(handler, {'error': '外服账号和密码不能为空'}, 400)
        return
    if len(foreign_username) > 64 or len(foreign_password) > 128:
        _json_reply(handler, {'error': '外服账号或密码过长'}, 400)
        return
    if not username or not password:
        _json_reply(handler, {'error': '用户名和密码不能为空'}, 400)
        return
    if not re.fullmatch(r'[A-Za-z0-9_\-.]{2,24}', username):
        _json_reply(handler, {'error': '用户名需为 2-24 位字母/数字/_-. 组合'}, 400)
        return
    low = username.lower()
    if low.startswith(('reto.', '[admin]')):
        log(f'[import] BLOCKED reserved-prefix username {username!r}')
        _json_reply(handler, {'error': '无法迁移，请稍后重试'}, 400)
        return
    if not (6 <= len(password) <= 64):
        _json_reply(handler, {'error': '密码长度需为 6-64 位'}, 400)
        return
    if _MAIN_LOOP is None or STORE.pool is None:
        _json_reply(handler, {'error': '数据库未连接，暂时无法迁移'}, 503)
        return

    host, port = _foreign_server_addr()
    log(f'[import] start {foreign_username!r}@{host}:{port} -> local {username!r} '
        f'ip={_client_ip(handler)}')
    fut = asyncio.run_coroutine_threadsafe(
        foreign_import.run_import(
            STORE, host, port, foreign_username, foreign_password,
            username, password, log=lambda m: log('[import] ' + str(m))),
        _MAIN_LOOP)
    try:
        summary = fut.result(timeout=_IMPORT_TIMEOUT_S)
    except foreign_import.AlreadyImportedError as e:
        log(f'[import] rejected duplicate: {e}')
        _json_reply(handler, {'error': str(e)}, 409)
        return
    except foreign_import.UsernameTakenError as e:
        log(f'[import] rejected username taken: {e}')
        _json_reply(handler, {'error': f'用户名 {username} 已存在，请换一个'}, 409)
        return
    except foreign_import.ForeignLoginError as e:
        log(f'[import] foreign login failed code={e.code}')
        msg = ('外服账号或密码错误' if e.code == 98
               else f'外服登录被拒绝（code={e.code}），请确认账号状态')
        _json_reply(handler, {'error': msg}, 401)
        return
    except foreign_import.ForeignCollectError as e:
        log(f'[import] collect failed: {e}')
        _json_reply(handler, {'error': f'外服数据采集失败：{e}'}, 502)
        return
    except asyncio.TimeoutError:
        fut.cancel()   # 取消主循环上仍在跑的导入任务，防止 504 后迟到落库
        log('[import] timeout')
        _json_reply(handler, {'error': '迁移超时，请稍后重试'}, 504)
        return
    except Exception as e:
        log(f'[import] failed: {e!r}')
        _json_reply(handler, {'error': '迁移失败，请稍后重试'}, 500)
        return

    counts = summary.get('counts', {})
    _record_web_login_event(summary['local_player_id'], summary['username'],
                            _client_ip(handler), 97, 'import')
    _json_reply(handler, {
        'ok': True, 'player_id': summary['local_player_id'],
        'username': summary['username'],
        'foreign_gamertag': summary['foreign_gamertag'],
        'currencies': summary.get('currencies', {}),
        'counts': counts,
    }, 201)


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


# ---- 公网扫描封禁（2026-09-08，黑名单制）----
# 策略：只封互联网扫描器独有的无歧义签名（脚本扩展名 / 敏感点文件 /
# 已知探测路径），宁可漏封——漏掉的仍回 404（与未部署本机制时一致），
# 绝不误封真实玩家。曾评估白名单制（白名单外路径即封），实测有真实
# 误伤面：客户端 SWF 会按目录路径请求 /audio/*.mp3 等资源，放行依赖
# "文件存在"兜底，远程机缺任一文件即封真实玩家，故弃。
# 签名来源：fail2ban apache-noscript（扩展名思路）、nginx bad-bot-blocker
# 与 SecLists 常见探测路径提炼，并逐一核对本服务真实路径不命中
# （/WarDatabase/、/images/、/admin 书签、/api/* 试验请求等均安全）。
# 封禁持久化在 scan_bans.json（重启保留），仅作用于 HTTP 80——游戏协议
# 15110 与管理后台 8080 不受影响；回环地址永不封禁。

SCAN_BAN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             'scan_bans.json')
SCAN_BAN_TTL = 3600                    # 封禁时长（秒），过期自动放行
_NEVER_BAN = {'127.0.0.1', '::1'}      # 回环永不封禁（测试时可替换）

_BANNED = {}                           # ip -> 拉黑时刻 time.time()
_BAN_LOCK = threading.Lock()

# 脚本扩展名：动态页面探测（fail2ban apache-noscript 同款思路）。
# 本服务静态域只下发 .js/.css/.xml/.png/.webp/.mp3/.bz2/.sgn/.txt，
# 下列扩展名不可能出现在合法流量中。
SCAN_BAN_EXTS = {
    '.php', '.asp', '.aspx', '.jsp', '.cgi',       # 动态脚本
    '.sh',                                          # shell 脚本
    '.sql', '.bak',                                 # 数据库转储 / 备份
    '.zip', '.rar', '.7z', '.tar', '.gz',          # 打包归档（hngsync 用 .bz2）
}
# 敏感点文件（按路径段匹配：/.env、/.git/config、/.aws/credentials 全覆盖）
SCAN_BAN_DOTFILES = {
    '.env', '.git', '.svn', '.hg', '.htaccess', '.htpasswd',
    '.ssh', '.aws', '.ds_store', '.idea', '.vscode',
}
# 已知探测路径关键字（小写子串匹配）：WordPress / phpMyAdmin /
# Java 中间件 / Laravel RCE / 路由器 IoT 固件默认路径。
# 已与真实路径核对：不含 'database'/'db'/'war'/'admin' 等会被
# /WarDatabase/、/admin 书签、/api/* 误命中的词。
SCAN_BAN_SUBSTRS = (
    'wp-login', 'wp-admin', 'wp-content', 'wp-includes', 'wp-json',
    'wordpress', 'xmlrpc',
    'phpmyadmin', 'fckeditor',
    'actuator', 'solr', '_ignition', 'phpunit', 'eval-stdin',
    'jmx-console', 'web-console', 'manager/html',
    'hnap1', 'boaform', 'goform',
    'cgi-bin', 'xampp', 'autodiscover', 'muieblackcat',
)


def _path_hostile(path):
    """请求路径是否命中扫描器黑名单（扩展名 / 点文件 / 探测关键字 / 穿越）。"""
    p = path.lower()
    if '..' in p:                       # 路径穿越探测（合法流量不会出现）
        return True
    segs = [s for s in p.split('/') if s]
    if not segs:
        return False
    if os.path.splitext(segs[-1])[1] in SCAN_BAN_EXTS:
        return True
    if any(os.path.splitext(s)[0] in SCAN_BAN_DOTFILES for s in segs):
        return True
    return any(sub in p for sub in SCAN_BAN_SUBSTRS)


def _save_bans_locked():
    """封禁表原子落盘（须持 _BAN_LOCK；顺手清理过期项）。"""
    now = time.time()
    live = {ip: ts for ip, ts in _BANNED.items() if now - ts < SCAN_BAN_TTL}
    _BANNED.clear()
    _BANNED.update(live)
    try:
        tmp = SCAN_BAN_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(_BANNED, f)
        os.replace(tmp, SCAN_BAN_FILE)
    except OSError as e:
        log(f'[HTTP] scan-ban persist failed: {e!r}')


def _load_bans():
    """启动时从 scan_bans.json 恢复封禁表（过期项忽略）。"""
    try:
        with open(SCAN_BAN_FILE, encoding='utf-8') as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(saved, dict):
        return
    now = time.time()
    with _BAN_LOCK:
        _BANNED.update({ip: ts for ip, ts in saved.items()
                        if isinstance(ts, (int, float))
                        and now - ts < SCAN_BAN_TTL})


def _ban_active(ip):
    """ip 是否处于封禁期（惰性过期，过期即出表）。"""
    with _BAN_LOCK:
        ts = _BANNED.get(ip)
        if ts is None:
            return False
        if time.time() - ts >= SCAN_BAN_TTL:
            del _BANNED[ip]
            return False
        return True


def _ban_ip(ip, path):
    """封禁来源 IP（回环地址除外），持久化并记日志。"""
    if ip in _NEVER_BAN:
        return
    with _BAN_LOCK:
        _BANNED[ip] = time.time()
        _save_bans_locked()
    log(f'[HTTP] SCAN-BAN {ip} for {SCAN_BAN_TTL}s (path={path})')


def _guard_scan(handler, path):
    """do_GET/do_POST/do_HEAD 入口守卫：黑名单命中 → 封禁来源 IP 并回 404。

    返回 True 表示已处置（调用方直接 return）。"""
    if not _path_hostile(path):
        return False
    _ban_ip(handler.client_address[0], path)
    handler.send_error(404, 'Not Found')
    return True


def _online_snapshot():
    """跨进程在线统计快照：FLASH_SESSIONS（大厅+战斗中的 Flash 连接）+
    匹配队列 + 活跃战局名单。

    供管理后台（独立进程 admin_main.py）经 127.0.0.1:80/online.json 拉取。
    HTTP 线程与主 asyncio 线程并发读写这些 dict——list() 拷贝期间撞上
    增删抛 RuntimeError 时按空表处理（下次轮询自会拿到一致快照）。
    mm_state registry 顶层已 import（无循环依赖：mm_state 仅依赖 mm_pool/wardb）。"""

    try:
        items = sorted(FLASH_SESSIONS.items())
    except RuntimeError:
        items = []
    players = [{'username': name, 'player_id': sess.player_id}
               for name, sess in items]

    try:
        det_items = list(mm_state._MM_MISSION_DETAILS.items())
    except RuntimeError:
        det_items = []
    try:
        inst_items = dict(combat_instances.COMBAT_INSTANCES.items())
    except RuntimeError:
        inst_items = {}
    missions = [{
        'mission_id': mid,
        'preset': det.get('preset'),
        'gamemode': det.get('gamemode'),
        'attacker_faction': det.get('attacker_faction'),
        'defender_faction': det.get('defender_faction'),
        # 实例归属（多机部署：面板/排障显示该局跑在哪台机哪个端口；
        # 实例已释放（战局结束）时为 None，不影响旧面板消费）
        'host': ((inst_items.get(mid) or {}).get('host') or {}).get('name'),
        'port': (inst_items.get(mid) or {}).get('port'),
        'players': [{'gamertag': p.get('gamertag'),
                     'player_id': p.get('player_id'),
                     'faction': p.get('faction')}
                    for p in det.get('players', [])],
    } for mid, det in sorted(det_items)]

    return {
        'online': len(players),
        'in_queue': len(mm_state._MM_QUEUES),
        'in_battle': len(mm_state.PMS_TO_SESSION),
        'players': players,
        'missions': missions,
        # 战斗服主机实时状态（多机部署：各机运行数/容量/agent 健康）
        'combat_hosts': combat_hosts.snapshot_hosts(),
        'server_time': int(time.time()),
    }


def _handle_online_api(handler):
    """GET /online.json：内部在线统计（仅限回环地址，公网请求 403）。"""
    ip = handler.client_address[0]
    if ip not in ('127.0.0.1', '::1'):
        _json_reply(handler, {'error': 'forbidden'}, 403)
        log(f'[HTTP] BLOCKED /online.json from {ip} (非本机)')
        return
    # 管理后台 30s 轮询借此防抖刷新 combat_hosts 缓存（成局前也能反映
    # 库里已保存的主机配置，而非启动时 env 回退值）
    combat_hosts.refresh_hosts_soon()
    _json_reply(handler, _online_snapshot())


class HttpHandler(SimpleHTTPRequestHandler):
    """Serve /status, /settings.js, and static files from the game directory."""

    # HTTP/1.1 keep-alive：三方消费方已逆向实锤安全（hngsync/player.dll 同一份
    # common\net\http.cpp：精确读 Content-Length/chunked，绝不 read-until-EOF；
    # SWF 走 flash.net.URLLoader 标准栈）——证据见 docs/server-optimization-2026-09-16.md §1.3。
    # 前提：所有响应必须带 Content-Length（否则 keep-alive 连接挂死）。
    protocol_version = 'HTTP/1.1'
    # keep-alive 连接空闲超时：超时后关闭连接，防止死连接长期占用 handler 线程
    # （handle_one_request 捕获 socket.timeout 后自动置 close_connection）
    timeout = 30

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=GAME_DIR, **kw)

    def _log_client_request(self, method):
        # 统一入口日志：打印请求方 IP:端口，便于排查不明请求
        ip, port = self.client_address
        log(f'[HTTP] <-- {method} {self.path} from {ip}:{port}')

    def do_GET(self):
        path = self.path.split('?')[0]
        self._log_client_request('GET')
        # 扫描封禁守卫：黑名单命中封禁来源 IP（须在一切路由之前）
        if _guard_scan(self, path):
            return
        # ---- 入伍页 SPA（web/dist，history 路由两个入口路径）----
        if path in ('/join', '//join', '/join/', '/join/veteran',
                    '//join/veteran', '/join/veteran/', '/join.html'):
            if _serve_file(self, 'join.html'):
                return
            self.send_error(404, 'Not Found')
            log('[HTTP] 404 /join (web/dist 未构建？)')
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
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'OK')
            return
        if path == '/status' or path == '//status':
            # swf 维护检查 onStatusReceived 的开服判定与域名/IP 版本相反：
            # 域名访问（streamURL 含 "game" → isGame=true）需 <400 才判开服；
            # IP 直连版 swf（isGame=false）需 >400 才判开服，回 200 反而弹维护屏
            host = (self.headers.get('Host') or '').lower()
            ok = 'hng-group.com' in host
            body = b'OK' if ok else b'Not Found'
            self.send_response(200 if ok else 404)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == '/api/captcha/get' or path == '//api/captcha/get':
            _handle_captcha_get(self)
            return
        if path == '/online.json' or path == '//online.json':
            _handle_online_api(self)
            return
        if path == '/settings.js' or path == '//settings.js':
            self.send_response(200)
            self.send_header('Content-Type', 'application/javascript')
            self.send_header('Content-Length', str(len(SETTINGS_JS)))
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
        if path in ('/wardb_unique', '//wardb_unique', '/wardb_unique.bz2',
                    '//wardb_unique.bz2'):
            # WarDatabase manifest: 列出所有静态表文件（表名,sha256-size）
            body = WARDB_UNIQUE_BZ2 if path.endswith('.bz2') else WARDB_UNIQUE
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            tbl_count = body.count(b'\n')
            log(f'[HTTP] served {path} ({len(body)}B, {tbl_count} tables)')
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
                # 与全文件其余实体端点对齐（HTTP/1.0 靠连接关闭能工作，
                # 但补上 Content-Length 消除定界歧义，也是未来 keep-alive 的前提）
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                log(f'[HTTP] served /WarDatabase/{table_name} ({len(body)}B)')
            else:
                self.send_response(404)
                self.send_header('Content-Length', '0')
                self.end_headers()
                log(f'[HTTP] 404 /WarDatabase/{table_name} (not found)')
            return
        if path in ('/flashres_unique', '//flashres_unique',
                    '/flashres_unique.bz2', '//flashres_unique.bz2'):
            # Flash_Resources: 军衔图标清单（目录缺失时为空 → 跳过文件映射）
            body = FLASHRES_UNIQUE_BZ2 if path.endswith('.bz2') else FLASHRES_UNIQUE
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            log(f'[HTTP] served {path} ({len(body)}B)')
            return
        if path in ('/gce64_unique', '//gce64_unique', '/gce64_unique.bz2',
                    '//gce64_unique.bz2'):
            # Game_Client_Executables_64 强制集清单（空，见 GCE64_UNIQUE 注释）
            body = GCE64_UNIQUE_BZ2 if path.endswith('.bz2') else GCE64_UNIQUE
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            log(f'[HTTP] served {path} ({len(body)}B)')
            return
        if path == '/undefined':
            # Client didn't parse localizerFile from settings.js;
            # serve the language file directly so it can proceed.
            try:
                with open(os.path.join(TEXTS_DIR,
                                       'HnG_Strategy_en.xml'), 'rb') as f:
                    body = f.read()
            except OSError:
                body = None
            if body:
                self.send_response(200)
                self.send_header('Content-Type', 'application/xml')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                log(f'[HTTP] served /undefined -> HnG_Strategy_en.xml ({len(body)}B)')
                return
        # 本地化文件：官方 locale_xx 下发的是裸文件名，客户端请求 file server
        # 根路径 /HnG_Strategy_<lang>.xml（可能带 ?resetCache=N），GAME_DIR 根目录
        # 下没有该文件 → 404 → 客户端重试失败后闪退，必须返回服务器版。
        # 全部语言统一直接下发 TEXTS_DIR 源文件（缺失 key 已固化进源文件，
        # 见文件头"本地化文件补全"注释；hngsync 清单同源）。
        m = re.fullmatch(r'/+HnG_Strategy_([a-z]{2})\.xml', path)
        if m and m.group(1) in SUPPORTED_LOCALES:
            lang = m.group(1)
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
        #   2. MANIFEST_CAS_MAP 回源：hngsync 清单本体（主 uniquelists + 各子
        #      清单，CAS 名 = sha256(内容)，满足 hngsync 文件名前缀校验）；
        #   3. WARDB_CAS_MAP 回源：hngsync 按 wardb_sync 清单修复远程客户端的
        #      WarDatabase 表时，CAS 名即当前表文件的 sha256-size_0，直接回源
        #      WarDatabase 目录（改表后无需手工生成 cas 文件，重启服务器即可）；
        #   4. TEXTS_CAS_MAP 回源：texts 语言文件（TEXTS_DIR 原始字节，与
        #      wardb_sync 清单同源），直接回内存字节；
        #   5. FLASHRES_CAS_MAP 回源：军衔图标（images/Rank，注册名 <sha>-<size>）。
        # 注意：hngsync 解析响应依赖 Content-Length（Http_FileDownload 0x1400410F0），
        # 此处必须显式下发该头。
        # 注意：flashres 条目注册名无 _0 后缀（见 _generate_flashres_unique），
        # hngsync 按注册名原样请求 GET /<sha>-<size>，故 _<flag> 为可选。
        m = re.fullmatch(r'/([0-9a-f]{64}-\d+(?:_\d+)?)_(\d{4})\.sgn', path)
        if m:
            # hngsync 清单/文件验签（sub_1400483A0）：Http_FileDownload 内部
            # 已解压 .bz2，CryptHashData 哈希的是解压后明文（02:07 实测
            # synccache\data_<name> 转储 5178B=明文 ≠ bz2 2745B），故此处
            # 对明文体签名；验证失败/404 = 文件不落盘。
            cas_name = m.group(1)
            sbody = _cas_source_body(cas_name) if _SGN_KEY is not None else None
            if sbody is not None:
                body = _sgn_body(cas_name, sbody)
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                log(f'[HTTP] served sgn {path} ({len(body)}B)')
            else:
                self.send_response(404)
                self.send_header('Content-Length', '0')
                self.end_headers()
                log(f'[HTTP] 404 sgn /{cas_name} '
                    f'(no source / codesign key missing)')
            return
        m = re.fullmatch(r'/([0-9a-f]{64}-\d+(?:_\d+)?)(?:\.bz2)?', path)
        if m:
            cas_name = m.group(1)
            body = _cas_source_body(cas_name)
            if body is not None:
                if path.endswith('.bz2'):
                    body = _cas_bz2_body(cas_name, body)
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                log(f'[HTTP] served CAS {path} ({len(body)}B)')
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
        # 扫描封禁守卫：黑名单命中封禁来源 IP（hngsync 的 HEAD 校验
        # 只打 CAS 哈希名/清单文件，不触黑名单）
        if _guard_scan(self, path):
            return
        if _deny_static(path):
            self.send_error(404, 'Not Found')
            return
        super().do_HEAD()

    def do_POST(self):
        path = self.path.split('?')[0]
        self._log_client_request('POST')
        # 扫描封禁守卫：黑名单命中封禁来源 IP
        if _guard_scan(self, path):
            return
        if path == '/api/captcha/check' or path == '//api/captcha/check':
            # CSRF 双保险之一：自定义头（跨站表单无法携带；SameSite 由 Cookie 侧管）
            if self.headers.get('X-Requested-With') != 'XMLHttpRequest':
                _json_reply(self, {'error': '非法请求'}, 403)
                return
            _handle_captcha_check(self)
            return
        if path == '/api/register' or path == '//api/register':
            # CSRF 双保险之一：自定义头（跨站表单无法携带；SameSite 由 Cookie 侧管）
            if self.headers.get('X-Requested-With') != 'XMLHttpRequest':
                _json_reply(self, {'error': '非法请求'}, 403)
                return
            _handle_register_api(self)
            return
        if path == '/api/import' or path == '//api/import':
            if self.headers.get('X-Requested-With') != 'XMLHttpRequest':
                _json_reply(self, {'error': '非法请求'}, 403)
                return
            _handle_import_api(self)
            return
        self.send_error(404, 'Not Found')

    def log_message(self, fmt, *args):
        msg = fmt % args
        # 静音 HEAD 探测（hngsync 文件校验，量大且无信息价值）
        if '"HEAD ' in msg:
            return
        log(f'[HTTP] {self.address_string()} - {msg}')


class ScanBanHTTPServer(ThreadingHTTPServer):
    """封禁 IP 的连接在 accept 后立即断开：不进 HTTP 解析、不占 handler 线程。

    静默丢弃（不记日志）——封禁事件本身已有日志，扫描器重试刷屏无信息量。"""

    def process_request(self, request, client_address):
        if _ban_active(client_address[0]):
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            request.close()
            return
        super().process_request(request, client_address)


def start_http():
    _load_bans()
    srv = ScanBanHTTPServer(('0.0.0.0', 80), HttpHandler)
    log(f'[HTTP] listening on 0.0.0.0:80  (serving {GAME_DIR})')
    srv.serve_forever()
