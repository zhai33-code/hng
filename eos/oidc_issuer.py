# -*- coding: utf-8 -*-
"""OpenID provider 集成：warserver 用本地 RSA 私钥签发 id_token（JWT），
作为 PlayerSettings.eos_openid_token 返回客户端，Epic 后端用 JWKS 公钥验证。
私钥文件: eos_oidc_private.pem（本目录）；JWKS 公钥已导出为 eos_oidc_jwks.json。
"""
import base64
import hashlib
import json
import os
import time
import uuid

import jwt  # PyJWT

_DIR = os.path.dirname(os.path.abspath(__file__))
_PRIV_PATH = os.path.join(_DIR, 'eos_oidc_private.pem')

# 与 Epic Dev Portal 的 OpenID provider 配置一致
# ★ 私服抓包确认 (private_login.pcapng, frame 10+11, JWT field 999):
#   iss = "https://auth.hng-group.com"
#   aud = "xyza7891uO05dmOamk2giEDkkLHD6Taj"  (EOS client_id, 非 Deployment ID)
#   exp = iat + 300  (非 3600)
#   claim 名 = "name" (非 "displayName")
ISSUER = 'https://auth.hng-group.com'
AUDIENCE = None  # 由 configure() 设置（= EOS client_id）
KEY_ID = None   # 由 _load_key() 读取（jwks kid）


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def load_jwks():
    """读取 eos_oidc_jwks.json（供上传 Gitee 用 / 本地检查）"""
    with open(os.path.join(_DIR, 'eos_oidc_jwks.json'), 'r', encoding='utf-8') as f:
        return json.load(f)


def _load_private_key():
    global KEY_ID
    with open(_PRIV_PATH, 'rb') as f:
        pem = f.read()
    jwks = load_jwks()
    KEY_ID = jwks['keys'][0]['kid']
    return pem


def configure(audience):
    """设置 JWT audience（= Epic Deployment ID）"""
    global AUDIENCE
    AUDIENCE = audience
    _load_private_key()


def make_id_token(display_name: str, subject: str = None) -> str:
    """签发 id_token。subject 缺省时生成稳定 UUID（同一玩家每次登录同一 PUID）。
    Epic 会从 sub 提取用户账号 ID；displayName 用于显示名。
    ★ subject 必须等于 PlayerSettings 响应的 field 1 (player_id)，否则客户端
      EOS_Connect_Login callback 校验失败 → disconnect_reason 非 1/2 → 10005。"""
    if AUDIENCE is None:
        raise RuntimeError('oidc not configured (call configure)')
    pem = _load_private_key()
    now = int(time.time())
    if subject is None:
        subject = str(uuid.uuid5(uuid.NAMESPACE_DNS, display_name))
    payload = {
        'iss': ISSUER,
        'sub': subject,
        'aud': AUDIENCE,
        'iat': now,
        'exp': now + 300,          # ★ 私服 = 300 秒，非 3600
        'name': display_name,     # ★ 私服 claim 名为 "name"，非 "displayName"
    }
    # ★ 不带 kid header：官服 id_token 618B（无 kid），带 kid 时 647B。
    # ZServer EAC 路径 (sub_1800B18A0) 处理 >618B token 时堆越界 →
    # canary 崩溃 (player.dll+0x4ad46d, 调用链经 ZStringBuffer<256> 析构)。
    # 本地 eos_stub 验签不依赖 kid，可安全省略。
    return jwt.encode(payload, pem, algorithm='RS256')


if __name__ == '__main__':
    # 自测：生成 token 并解码验证
    configure('xyza7891uO05dmOamk2giEDkkLHD6Taj')
    tok = make_id_token('DemoPlayer', subject='300001')
    print('token head:', tok[:60], '...')
    print('payload:', json.dumps(jwt.decode(tok, options={'verify_signature': False}), indent=1))
