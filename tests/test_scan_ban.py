"""web_server 扫描封禁测试（黑名单制：扫描器签名命中 → 封禁来源 IP）。

覆盖：合法流量（含日志回放发现的真实路径形态）绝不命中、扫描器签名
（扩展名/点文件/探测关键字/穿越）命中、封禁持久化、回环地址永不封禁、
重启恢复（_load_bans）、过期自动放行、process_request 对封禁 IP 直接
断连、端到端 HTTP 行为。

用法：
  .venv\\Scripts\\python.exe tests\\test_scan_ban.py
  .venv\\Scripts\\python.exe -m pytest tests\\test_scan_ban.py
"""

import contextlib
import os
import socket
import sys
import tempfile
import threading
import time
import types
from http.client import HTTPConnection

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import web_server


def _fake_handler(ip='203.0.113.9'):
    """_guard_scan 最小 handler 替身（只用到 client_address/send_error）。"""
    return types.SimpleNamespace(
        client_address=(ip, 51234),
        send_error=lambda *a, **k: None)


@contextlib.contextmanager
def _isolated_bans(ttl=3600.0, never_ban=('127.0.0.1', '::1')):
    """把封禁状态隔离到临时文件，测完恢复模块全局（不污染真实 scan_bans.json）。"""
    tmpdir = tempfile.mkdtemp(prefix='scan_ban_test_')
    old_file = web_server.SCAN_BAN_FILE
    old_ttl = web_server.SCAN_BAN_TTL
    old_never = web_server._NEVER_BAN
    old_banned = dict(web_server._BANNED)
    web_server.SCAN_BAN_FILE = os.path.join(tmpdir, 'scan_bans.json')
    web_server.SCAN_BAN_TTL = ttl
    web_server._NEVER_BAN = set(never_ban)
    web_server._BANNED.clear()
    try:
        yield web_server.SCAN_BAN_FILE
    finally:
        web_server.SCAN_BAN_FILE = old_file
        web_server.SCAN_BAN_TTL = old_ttl
        web_server._NEVER_BAN = old_never
        web_server._BANNED.clear()
        web_server._BANNED.update(old_banned)


def test_legit_paths_never_banned():
    """真实流量全形态不命中黑名单（含文件缺失时的目录路径资源请求）。

    来源：realtest_game.log 真实请求回放（/audio/*.mp3 85 次/天）+ 入伍页
    浏览器 + hngsync CAS + 旧书签/试验性 poke（黑名单制下只 404 不封）。"""
    sha = 'a' * 64
    legit = [
        '/', '//',                      # hngsync HostingCenter ping 探测
        '/index.html', '//status', '/settings.js', '/undefined',
        '/online.json', '/favicon.ico', '/soldier.png',
        '/hng_unique_v173293', '/hng_unique_v173293.bz2',
        '/wardb_unique', '/wardb_unique.bz2',
        '/wardb_sync', '/wardb_sync.bz2',
        '/flashres_unique', '/flashres_unique.bz2',
        '/gce64_unique', '/gce64_unique.bz2',
        '/currentfiles.txt',
        # 客户端 SWF 按目录路径请求的音频资源（文件缺失也只是 404，不封）
        '/audio/Troops_Depart.mp3', '/audio/Tanks_Loop.mp3',
        '/audio/campaign/Campaign_Enemy_Attack_01.mp3',
        '/audio/money/Money_ExchangeGold_01.mp3',
        '/audio/missing_file_not_on_disk.mp3',
        # 本地化文件（含不支持语言 → 404 但不封）
        '/HnG_Strategy_en.xml', '//HnG_Strategy_zh.xml',
        '/HnG_Strategy_xx.xml',
        # 入伍页 SPA
        '/join', '/join/', '//join', '/join.html',
        '/join/veteran', '/join/veteran/', '//join/veteran',
        '/assets/index-Bw4x7ZkC.js', '/assets/index-Bw4x7ZkC.css',
        '/assets/index-Bw4x7ZkC.js.map',   # F12 打开时浏览器自动请求
        '/icons/weapons/mp40.webp', '/icons/ranks/general.png',
        '/images/Weapons/90.png', '/images/Weapons/90_341.png',
        '/images/Weapons/Thumbnails/90_341.png',
        '/images/Rank/General.png',
        '/WarDatabase/factiontemplate', '/WarDatabase/weaponModifiertemplate',
        # API（注册/迁移全链路 + 旧书签/试验 poke → 404 不封）
        '/api/captcha/get', '/api/captcha/check',
        '/api/register', '/api/import',
        '/api/war/battles', '/api/captcha',
        '/register', '/import', '/account', '/admin',
        # CAS 哈希名：裸名 / .bz2 / _<flag> / .sgn 变体
        f'/{sha}-5178', f'/{sha}-5178.bz2',
        f'/{sha}-5178_0', f'/{sha}-5178_0.bz2',
        f'/{sha}-5178_0_2026.sgn', f'/{sha}-5178_2026.sgn',
        f'//{sha}-5178_0',
    ]
    for p in legit:
        assert not web_server._path_hostile(p), f'合法路径误判: {p}'


def test_scanner_signatures_banned():
    """典型扫描器签名全部命中（扩展名 / 点文件 / 探测关键字 / 穿越）。"""
    hostile = [
        # 脚本扩展名
        '/wp-login.php', '/index.php', '/WP-LOGIN.PHP', '/info.php',
        '/admin/config.php', '/shell.asp', '/cmd.aspx', '/index.jsp',
        '/cgi-bin/test.cgi', '/backup.sh', '/dump.sql', '/site.bak',
        # 打包归档
        '/backup.zip', '/site.tar.gz', '/db.rar', '/www.7z',
        # 点文件 / 敏感目录段
        '/.env', '/.env.production', '/.git/config', '/.git/HEAD',
        '/.svn/entries', '/.htaccess', '/.htpasswd',
        '/.ssh/id_rsa', '/.aws/credentials', '/.DS_Store',
        '/config/.env', '/deep/.git/refs',
        # WordPress / phpMyAdmin
        '/wp-admin/setup-config.php', '/wp-json/wp/v2/users',
        '/wp-content/uploads/', '/wp-includes/js/', '/wordpress/',
        '/xmlrpc.php', '/phpmyadmin/', '/phpMyAdmin/index.php',
        '/fckeditor/editor/filemanager/',
        # Java 中间件 / Laravel RCE
        '/actuator/env', '/solr/admin/', '/_ignition/execute',
        '/vendor/phpunit/phpunit/src/Util/PHP/eval-stdin.php',
        '/jmx-console/', '/web-console/', '/manager/html',
        # 路由器 IoT
        '/HNAP1', '/boaform/admin/formlogin', '/goform/setSysAdm',
        # 其他
        '/cgi-bin/', '/xampp/', '/autodiscover/autodiscover.xml',
        '/muieblackcat',
        # 路径穿越
        '/index.php?page=../../etc/passwd', '//../secret.txt',
        '/..%2f..%2fetc%2fpasswd'.replace('%2f', ''),   # 原样 .. 形态
        '/../../../windows/win.ini',
    ]
    for p in hostile:
        assert web_server._path_hostile(p), f'扫描签名漏判: {p}'


def test_guard_bans_and_persists():
    """黑名单命中 → 封禁 + 落盘 + 回 404。"""
    with _isolated_bans() as ban_file:
        h = _fake_handler('203.0.113.9')
        assert web_server._guard_scan(h, '/wp-login.php') is True
        assert web_server._ban_active('203.0.113.9')
        assert os.path.isfile(ban_file)
        import json
        with open(ban_file, encoding='utf-8') as f:
            saved = json.load(f)
        assert '203.0.113.9' in saved


def test_guard_passes_legit():
    """合法路径（含缺失文件）不封禁、不回错误。"""
    with _isolated_bans():
        h = _fake_handler('203.0.113.9')
        for p in ('/settings.js', '/audio/whatever_missing.mp3',
                  '/register', '/join'):
            assert web_server._guard_scan(h, p) is False, p
        assert web_server._BANNED == {}


def test_loopback_never_banned():
    """回环地址（管理后台拉 /online.json）即使触发守卫也不封。"""
    with _isolated_bans():
        for ip in ('127.0.0.1', '::1'):
            h = _fake_handler(ip)
            assert web_server._guard_scan(h, '/wp-login.php') is True
            assert not web_server._ban_active(ip)
        assert web_server._BANNED == {}


def test_persistence_roundtrip():
    """封禁落盘后清空内存，_load_bans 能恢复（模拟重启）。"""
    with _isolated_bans():
        web_server._ban_ip('198.51.100.7', '/.env')
        assert web_server._ban_active('198.51.100.7')
        web_server._BANNED.clear()
        assert not web_server._ban_active('198.51.100.7')
        web_server._load_bans()
        assert web_server._ban_active('198.51.100.7')


def test_ban_expiry():
    """超过 SCAN_BAN_TTL 自动放行（惰性过期）。"""
    with _isolated_bans(ttl=100.0):
        web_server._ban_ip('198.51.100.8', '/admin.php')
        assert web_server._ban_active('198.51.100.8')
        web_server._BANNED['198.51.100.8'] = time.time() - 101
        assert not web_server._ban_active('198.51.100.8')
        assert '198.51.100.8' not in web_server._BANNED


def test_load_bans_ignores_expired_and_garbage():
    """_load_bans 过滤过期项与畸形数据（坏文件不致命）。"""
    import json
    with _isolated_bans() as ban_file:
        now = time.time()
        with open(ban_file, 'w', encoding='utf-8') as f:
            json.dump({'198.51.100.1': now - 99999,
                       '198.51.100.2': now,
                       'bad-ip': 'not-a-number'}, f)
        web_server._load_bans()
        assert not web_server._ban_active('198.51.100.1')
        assert web_server._ban_active('198.51.100.2')
        assert not web_server._ban_active('bad-ip')
        # 坏 JSON 不抛异常
        with open(ban_file, 'w', encoding='utf-8') as f:
            f.write('{not json')
        web_server._load_bans()


def test_process_request_drops_banned():
    """封禁 IP 的连接在 process_request 即断开，不进 handler。"""
    srv = web_server.ScanBanHTTPServer(
        ('127.0.0.1', 0), web_server.HttpHandler, bind_and_activate=False)
    try:
        with _isolated_bans():
            web_server._BANNED['203.0.113.9'] = time.time()
            a, b = socket.socketpair()
            try:
                b.settimeout(2)
                srv.process_request(a, ('203.0.113.9', 51234))
                assert b.recv(64) == b''    # 对端立即收到 EOF/断连
            finally:
                b.close()
    finally:
        srv.server_close()


def test_end_to_end_http():
    """端到端：合法+缺失文件路径 200/404 不封 → 扫描路径 404 且封禁 →
    后续连接（连合法路径）被直接断开。"""
    with _isolated_bans(never_ban=()):    # 测试放行回环封禁
        srv = web_server.ScanBanHTTPServer(('127.0.0.1', 0),
                                           web_server.HttpHandler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            # 合法路径正常服务
            conn = HTTPConnection('127.0.0.1', port, timeout=5)
            conn.request('GET', '/settings.js')
            assert conn.getresponse().status == 200
            conn.close()

            # 缺失文件目录路径：404 但不封禁（白名单制下的误伤场景）
            conn = HTTPConnection('127.0.0.1', port, timeout=5)
            conn.request('GET', '/audio/whatever_missing.mp3')
            assert conn.getresponse().status == 404
            conn.close()
            conn = HTTPConnection('127.0.0.1', port, timeout=5)
            conn.request('GET', '/register')          # 旧书签路径
            assert conn.getresponse().status == 404
            conn.close()
            assert not web_server._ban_active('127.0.0.1')

            # 扫描路径 → 404 + 封禁
            conn = HTTPConnection('127.0.0.1', port, timeout=5)
            conn.request('GET', '/wp-login.php')
            assert conn.getresponse().status == 404
            conn.close()
            assert web_server._ban_active('127.0.0.1')

            # 封禁后：连合法路径的连接也直接被断（process_request 丢弃）
            conn = HTTPConnection('127.0.0.1', port, timeout=5)
            dropped = False
            try:
                conn.request('GET', '/settings.js')
                conn.getresponse()
            except OSError:
                dropped = True
            finally:
                conn.close()
            assert dropped
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items())
           if k.startswith('test_') and callable(v)]
    for fn in fns:
        fn()
        print(f'PASS {fn.__name__}')
    print(f'{len(fns)} tests passed')
