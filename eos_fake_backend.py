# -*- coding: utf-8 -*-
"""EOS 假后端（方案C 最小验证版）
================================
伪装 api.epicgames.dev，让原版 EOSSDK-Win64-Shipping.dll 在离线环境完成认证链：

  hng.exe → EOSSDK(curl+OpenSSL) → https://api.epicgames.dev/sdk/v1/default   ← 本服务下发全部服务路由
                                 → /epic/oauth/v2/.well-known/jwks.json       ← 返回 eos_oidc_jwks.json
                                 → /epic/oauth/v2/token (grant_type=jwt-bearer)← 收 PlayerSettings field999 的 JWT，
                                                                         用本地公钥验签，回 product_user_id

前置条件：
  1. hosts 手动添加:           127.0.0.1 api.epicgames.dev
  2. 管理员运行 eos_fake_setup.ps1 安装 CA（Root store + C:\Windows\cert.pem 双路）
  3. 恢复原版 SDK:            EOSSDK-Win64-Shipping.dll.orig → EOSSDK-Win64-Shipping.dll

验证成功判据（看本服务日志）：
  阶段1  收到 /sdk/v1/default      → TLS 信任链 OK（不锁证书实锤）
  阶段2  之后出现 jwks/token 请求   → 配置 JSON 被 SDK 接受
  阶段3  token 响应后游戏继续走     → 认证链打通
"""
import json
import logging
import os
import ssl
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

_DIR = os.path.dirname(os.path.abspath(__file__))
CERT_DIR = os.path.join(_DIR, 'certs')
LOG_PATH = os.path.join(_DIR, 'eos_fake_backend.log')

# ---- 与游戏客户端一致的常量（EasyAntiCheat/Settings.json + 抓包结论）----
DOMAIN = 'api.epicgames.dev'
CLIENT_ID = 'xyza7891uO05dmOamk2giEDkkLHD6Taj'      # JWT aud / oauth client_id
PRODUCT_ID = 'e47c7a3e00a9486abf47ae08ae9a57f9'
SANDBOX_ID = 'd18b367545b94f8289d18a97ed586742'
DEPLOYMENT_ID = '766af0f217664257953fb2d7ddb43ea7'

# ---- 日志：控制台 + 文件 ----
logger = logging.getLogger('eos_fake')
logger.setLevel(logging.DEBUG)
_fmt = logging.Formatter('%(asctime)s %(message)s', '%H:%M:%S')
for h in (logging.StreamHandler(), logging.FileHandler(LOG_PATH, encoding='utf-8')):
    h.setFormatter(_fmt)
    logger.addHandler(h)


# ============================================================
# 证书：自签 CA + api.epicgames.dev 服务器证书（首次运行生成）
# ============================================================
def _gen_certs():
    os.makedirs(CERT_DIR, exist_ok=True)
    ca_key_p = os.path.join(CERT_DIR, 'eos_fake_ca.key')
    ca_crt_p = os.path.join(CERT_DIR, 'eos_fake_ca.crt')
    srv_key_p = os.path.join(CERT_DIR, f'{DOMAIN}.key')
    srv_crt_p = os.path.join(CERT_DIR, f'{DOMAIN}.crt')   # 含证书链

    if all(os.path.exists(p) for p in (ca_crt_p, srv_key_p, srv_crt_p)):
        return srv_crt_p, srv_key_p

    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'HnG Fake EOS Root CA')])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name).issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False,
                                     key_encipherment=False, data_encipherment=False,
                                     key_agreement=False, key_cert_sign=True,
                                     crl_sign=True, encipher_only=False,
                                     decipher_only=False), critical=True)
        # CDP: schannel 默认查吊销，自签 CA 无 CRL 会报 CERT_TRUST_REVOCATION_STATUS_UNKNOWN，
        # 空名单 CRL 由本服务 80 端口下发
        .add_extension(x509.CRLDistributionPoints([
            x509.DistributionPoint(
                full_name=[x509.UniformResourceIdentifier(
                    f'http://{DOMAIN}/crls/eos_fake_ca.crl')],
                relative_name=None, reasons=None, crl_issuer=None)]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    srv_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    srv_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, DOMAIN)]))
        .issuer_name(ca_cert.subject)
        .public_key(srv_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1825))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(DOMAIN),
                                         x509.DNSName('rtcp.on.epicgames.com')]),
            critical=False)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                     key_encipherment=True, data_encipherment=False,
                                     key_agreement=False, key_cert_sign=False,
                                     crl_sign=False, encipher_only=False,
                                     decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
                       critical=False)
        # 叶子证书也带 CDP：schannel 对叶子同样查吊销，空 CRL 覆盖全部
        .add_extension(x509.CRLDistributionPoints([
            x509.DistributionPoint(
                full_name=[x509.UniformResourceIdentifier(
                    f'http://{DOMAIN}/crls/eos_fake_ca.crl')],
                relative_name=None, reasons=None, crl_issuer=None)]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    # 空吊销名单（10 年），80 端口下发
    from cryptography.x509 import ocsp  # noqa: F401  (确保子模块可用)
    crl = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(ca_cert.subject)
        .last_update(now - timedelta(days=1))
        .next_update(now + timedelta(days=3650))
        .sign(ca_key, hashes.SHA256())
    )
    with open(os.path.join(CERT_DIR, 'eos_fake_ca.crl'), 'wb') as f:
        f.write(crl.public_bytes(serialization.Encoding.DER))

    def _pem_key(k):
        return k.private_bytes(serialization.Encoding.PEM,
                               serialization.PrivateFormat.TraditionalOpenSSL,
                               serialization.NoEncryption())

    with open(ca_key_p, 'wb') as f:
        f.write(_pem_key(ca_key))
    with open(ca_crt_p, 'wb') as f:
        f.write(ca_cert.public_bytes(serialization.Encoding.PEM))
    with open(srv_key_p, 'wb') as f:
        f.write(_pem_key(srv_key))
    with open(srv_crt_p, 'wb') as f:
        f.write(srv_cert.public_bytes(serialization.Encoding.PEM))
        f.write(ca_cert.public_bytes(serialization.Encoding.PEM))   # fullchain
    logger.info('[cert] 已生成 CA + %s 服务器证书 → %s/', DOMAIN, CERT_DIR)
    return srv_crt_p, srv_key_p


# ============================================================
# JWT 验签：复用 oidc_issuer 的 JWKS 公钥（game_server 签的 token 同一把钥匙）
# ============================================================
import jwt  # PyJWT
from jwt.algorithms import RSAAlgorithm

with open(os.path.join(_DIR, 'eos_oidc_jwks.json'), 'r', encoding='utf-8') as f:
    JWKS = json.load(f)
_PUB_KEY = RSAAlgorithm.from_jwk(json.dumps(JWKS['keys'][0]))
ISSUER = 'https://auth.hng-group.com'


def verify_assertion(token: str):
    """验证客户端提交的 openid JWT；返回 (sub, name) 或抛异常"""
    payload = jwt.decode(token, _PUB_KEY, algorithms=['RS256'],
                         audience=CLIENT_ID, issuer=ISSUER)
    return payload['sub'], payload.get('name', '')


def _puid(sub: str) -> str:
    """由 sub 确定性生成 product_user_id / account_id（同一玩家每次相同）"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'hng-eos|{sub}'))


def make_access_token(sub: str, name: str) -> str:
    """假 Epic 签发的 access_token（SDK 只当不透明串透传，签不签都行，签上便于排查）"""
    now = int(time.time())
    payload = {'iss': f'https://{DOMAIN}', 'sub': _puid(sub), 'aud': CLIENT_ID,
               'iat': now, 'exp': now + 300, 'name': name,
               'pf': 'unknown', 't': 'access', 'clid': CLIENT_ID,
               'prd': PRODUCT_ID, 'sbx': SANDBOX_ID, 'dep': DEPLOYMENT_ID}
    import oidc_issuer
    with open(os.path.join(_DIR, 'eos_oidc_private.pem'), 'rb') as f:
        pem = f.read()
    return jwt.encode(payload, pem, algorithm='RS256',
                      headers={'kid': JWKS['keys'][0]['kid']})


# ============================================================
# SDKConfig 响应：全部服务指向本域名（同证书，单域名闭环）
# 键名取自 DLL 逆向：BaseUrl / AltBaseUrl / HttpRetryLimit / DevPhase
# 服务名集覆盖常见 EOS 服务；缺的会被 SDK 记 not found 并跳过 → 看日志补
# ============================================================
_SERVICE_NAMES = [
    'SDKConfig', 'Auth', 'Connect', 'Friends', 'Presence', 'UserInfo', 'Ecom',
    'Stats', 'Leaderboards', 'Lobbies', 'SessionTracker', 'Sanctions',
    'Reports', 'AntiCheat', 'Voice', 'Telemetry', 'Metrics', 'TitleStorage',
    'PlayerDataStorage', 'ModMetadata', 'Achievements', 'Progression',
    'Social', 'Identity', 'Account', 'OAuth', 'DataRouter',
]

def sdk_config_body() -> dict:
    base = f'https://{DOMAIN}'
    services = {}
    for name in _SERVICE_NAMES:
        services[name] = {
            'BaseUrl': base,
            'AltBaseUrl': base,
            'HttpRetryLimit': 3,
            'DevPhase': 'Production',
            'routes': [],
        }
    return {'services': services,
            'client': {'BaseUrl': base, 'AltBaseUrl': base}}


def oauth_token_body(sub: str, name: str) -> dict:
    now = int(time.time())
    return {
        'access_token': make_access_token(sub, name),
        'expires_in': 300,
        'expires_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now + 300)),
        'token_type': 'bearer',
        'refresh_token': 'hng-fake-refresh-' + _puid(sub)[:8],
        'refresh_expires': -1,
        'account_id': _puid(sub),
        'client_id': CLIENT_ID,
        'application_id': 'hng',
        'product_id': PRODUCT_ID,
        'sandbox_id': SANDBOX_ID,
        'deployment_id': DEPLOYMENT_ID,
        'organization_id': 'hng-org',
        'product_user_id': _puid('connect|' + sub),
        'display_name': name or 'Player',
    }


# ============================================================
# HTTP 处理
# ============================================================
class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'nginx/1.25.3'   # 伪装常规反代指纹

    def _log_req(self, tag=''):
        logger.info('>> %s %s%s %s', self.command, self.headers.get('Host', '-'),
                    self.path, tag)

    def _send(self, code: int, obj, ctype='application/json'):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        logger.info('<< %d (%d bytes)', code, len(body))

    # ---------- GET ----------
    def do_GET(self):
        self._log_req()
        u = urlparse(self.path)
        p = u.path.rstrip('/')

        # 阶段1/2：SDK 配置下发
        if p in ('/sdk/v1/default',) or p.startswith('/sdk/v1/product/'):
            return self._send(200, sdk_config_body())

        # 阶段3：JWKS（两种路径都注册了）
        if p.endswith('/.well-known/jwks.json') or p.endswith('/oauth/jwks'):
            return self._send(200, JWKS)

        if p == '/' :
            return self._send(200, {'status': 'hng fake eos backend'})

        return self._send(404, {'errorCode': 0, 'errorMessage': 'no route (fake)'})

    # ---------- POST ----------
    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length) if length else b''
        self._log_req(f'body[{length}B]={raw[:300]!r}')
        u = urlparse(self.path)
        p = u.path.rstrip('/')

        # 遥测：直接吞掉
        if '/datarouter/' in p or '/telemetry/' in p:
            return self._send(204, b'', 'text/plain')

        # token 端点（auth v1 / epic oauth v2 同一处理）
        if p.endswith('/oauth/token') or p.endswith('/oauth/v2/token'):
            q = parse_qs(raw.decode('utf-8', 'replace'))
            grant = (q.get('grant_type') or [''])[0]
            assertion = (q.get('assertion') or q.get('code') or [''])[0]
            logger.info('   grant_type=%r assertion=%s...', grant, assertion[:40])
            if 'jwt-bearer' in grant and assertion:
                try:
                    sub, name = verify_assertion(assertion)
                    logger.info('   JWT 验签通过 sub=%s name=%s', sub, name)
                    return self._send(200, oauth_token_body(sub, name))
                except Exception as e:
                    logger.warning('   JWT 验签失败: %s', e)
                    return self._send(401, {
                        'errorCode': 'errors.com.epicgames.account.auth_token.invalid_platform_token',
                        'errorMessage': str(e)})
            return self._send(400, {
                'errorCode': 'errors.com.epicgames.common.oauth.invalid_request',
                'errorMessage': f'unsupported grant_type={grant!r} (fake)'})

        # 其余 connect 类 POST：宽容处理，返回成功体让流程走下去
        if '/connect' in p:
            return self._send(200, {'productUserId': _puid('anon'), 'client_id': CLIENT_ID})

        return self._send(404, {'errorCode': 0, 'errorMessage': 'no route (fake)'})

    def log_message(self, *a):   # 关闭默认 stderr 噪声，全走 logger
        pass


class CrlHandler(BaseHTTPRequestHandler):
    """80 端口：只负责下发 CRL（schannel 吊销检查用）"""
    protocol_version = 'HTTP/1.1'

    def do_GET(self):
        crl_p = os.path.join(CERT_DIR, 'eos_fake_ca.crl')
        if urlparse(self.path).path == '/crls/eos_fake_ca.crl' and os.path.exists(crl_p):
            with open(crl_p, 'rb') as f:
                body = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'application/pkix-crl')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            logger.info('>> [80] CRL 下发 (%d bytes)', len(body))
        else:
            self.send_response(404)
            self.send_header('Content-Length', '0')
            self.end_headers()
            logger.info('>> [80] %s → 404', self.path)

    def log_message(self, *a):
        pass


def main():
    srv_crt, srv_key = _gen_certs()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(srv_crt, srv_key)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2

    httpd = ThreadingHTTPServer(('0.0.0.0', 443), Handler)
    httpd.daemon_threads = True
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    # CRL 的 HTTP URL 下发默认关闭：CRL 已由 eos_fake_setup.ps1 装入 Windows CA store，
    # schannel 链验证命中本地缓存，无需 80 端口（80 留给 realtest 的 fs1 文件同步）
    if os.environ.get('EOS_FAKE_CRL_HTTP'):
        try:
            crl_httpd = ThreadingHTTPServer(('0.0.0.0', 80), CrlHandler)
            crl_httpd.daemon_threads = True
            threading.Thread(target=crl_httpd.serve_forever, daemon=True).start()
            logger.info('CRL 分发器已启动 :80')
        except OSError as e:
            logger.warning('80 端口不可用（%s）', e)
    logger.info('=== EOS 假后端已启动 :443 (SNI=%s) ===', DOMAIN)
    logger.info('hosts 需含: 127.0.0.1 %s', DOMAIN)
    logger.info('等待 SDK 请求…（阶段1 出现 /sdk/v1/default 即 TLS 信任 OK）')
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info('已停止')


if __name__ == '__main__':
    main()
