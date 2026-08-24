# -*- coding: utf-8 -*-
"""端到端自测：用 Python/OpenSSL 客户端（与 DLL 的 OpenSSL 后端同语义）走完三阶段"""
import json
import os
import ssl
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  'certs', 'eos_fake_ca.crt')

# 模拟 hosts 条目 "127.0.0.1 api.epicgames.dev"（正式环境由 hosts 文件承担）
import socket
_gai = socket.getaddrinfo
socket.getaddrinfo = lambda host, *a, **k: (
    _gai('127.0.0.1', *a, **k) if host == 'api.epicgames.dev' else _gai(host, *a, **k))

ctx = ssl.create_default_context(cafile=CA)
ctx.check_hostname = True

def get(url):
    with urllib.request.urlopen(url, context=ctx, timeout=5) as r:
        return json.loads(r.read())

# 阶段1: SDKConfig
d = get('https://api.epicgames.dev/sdk/v1/default?platformId=WIN')
print('阶段1 SDKConfig: services=%d Connect.BaseUrl=%s' % (
    len(d['services']), d['services']['Connect']['BaseUrl']))

# 阶段2: JWKS
d = get('https://api.epicgames.dev/epic/oauth/v2/.well-known/jwks.json')
print('阶段2 JWKS: kid=%s kty=%s' % (d['keys'][0]['kid'], d['keys'][0]['kty']))

# 阶段3: token (jwt-bearer)
import oidc_issuer
oidc_issuer.configure('xyza7891uO05dmOamk2giEDkkLHD6Taj')
tok = oidc_issuer.make_id_token('DoubleBird', subject='300001')
req = urllib.request.Request(
    'https://api.epicgames.dev/epic/oauth/v2/token',
    data=('grant_type=jwt-bearer&assertion=%s' % tok).encode(),
    headers={'Content-Type': 'application/x-www-form-urlencoded'})
with urllib.request.urlopen(req, context=ctx, timeout=5) as r:
    d = json.loads(r.read())
print('阶段3 Token: account_id=%s product_user_id=%s expires_in=%s display_name=%s' % (
    d['account_id'], d['product_user_id'], d['expires_in'], d['display_name']))
print('\n全部通过：TLS 信任链 + JWT 验签 + token 签发闭环 OK')
