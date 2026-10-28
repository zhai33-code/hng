# -*- coding: utf-8 -*-
"""管理端鉴权：scrypt 密码哈希 + JWT 会话 + 登录限速 + CSRF 写保护。

- 密码：hashlib.scrypt（标准库），格式 scrypt$N$r$p$salt_hex$hash_hex
- 会话：PyJWT HS256，2 小时时效，HttpOnly + SameSite=Strict Cookie
- 改密踢会话：token iat < credentials_updated_at 即拒绝
- CSRF：写操作（POST/PUT/DELETE）要求 X-Requested-With: XMLHttpRequest
- 限速：按 IP 15 分钟窗口 5 次失败锁 15 分钟（内存计数，进程级）
"""
import hashlib
import hmac
import secrets
import time
from datetime import datetime, timezone

import jwt
from fastapi import HTTPException, Request

from . import config
from . import db          # 注意：导入模块而非 pool 对象——db.init() 是 global 重绑定，
                          # from .db import pool 会在导入期固化 None，导致所有请求 500

# ---- scrypt ----
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1
SESSION_HOURS = 2

_COOKIE = 'hng_admin_session'


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt,
                       n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return f'scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${h.hex()}'


def verify_password(password: str, stored: str) -> bool:
    try:
        alg, n, r, p, salt_hex, hash_hex = stored.split('$')
        if alg != 'scrypt':
            return False
        h = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex),
                           n=int(n), r=int(r), p=int(p))
        return hmac.compare_digest(h.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


# ---- 登录限速（内存）----
_failures: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ''


def _is_rate_limited(ip: str) -> bool:
    now = time.time()
    fails = [t for t in _failures.get(ip, []) if now - t < 900]
    _failures[ip] = fails
    return len(fails) >= 5


def _record_failure(ip: str):
    _failures.setdefault(ip, []).append(time.time())


def _clear_failures(ip: str):
    _failures.pop(ip, None)


# ---- JWT ----
def _sign(admin_id: int, admin_name: str) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {'sub': str(admin_id), 'name': admin_name, 'iat': int(now.timestamp())},
        config.ADMIN_SECRET, algorithm='HS256')


def set_session_cookie(response, token: str):
    response.set_cookie(
        _COOKIE, token,
        max_age=SESSION_HOURS * 3600,
        httponly=True, samesite='strict',
        secure=False,   # 默认回环/内网 http 部署；若挂反代 https 需改 true
        path='/')


def clear_session_cookie(response):
    response.delete_cookie(_COOKIE, path='/')


class AdminUser:
    def __init__(self, admin_id: int, username: str):
        self.id = admin_id
        self.username = username


async def current_admin(request: Request) -> AdminUser:
    """JWT Cookie 校验依赖：无效/过期/被改密踢出 → 401。"""
    token = request.cookies.get(_COOKIE)
    if not token:
        raise HTTPException(401, '未登录')
    try:
        payload = jwt.decode(token, config.ADMIN_SECRET,
                             algorithms=['HS256'],
                             options={'require': ['sub', 'iat']})
    except jwt.PyJWTError:
        raise HTTPException(401, '会话无效或已过期')
    admin_id = int(payload['sub'])
    assert db.pool is not None
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT username, credentials_updated_at FROM admin_users WHERE id = $1',
            admin_id)
    if row is None:
        raise HTTPException(401, '账号不存在')
    # 改密踢会话：签发时间早于凭证更新时间即拒绝
    cred_at = row['credentials_updated_at']
    if cred_at is not None:
        if isinstance(cred_at, datetime):
            cred_at = cred_at.timestamp()
        if payload['iat'] < int(cred_at):
            raise HTTPException(401, '凭证已变更，请重新登录')
    return AdminUser(admin_id, row['username'])


async def write_guard(request: Request):
    """写操作 CSRF 依赖：要求自定义头（跨站表单无法携带）。
    需配合 current_admin 一起使用（router 依赖里两个都挂）。"""
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return
    if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        raise HTTPException(403, '缺少 CSRF 防护头')


async def login(request: Request, username: str, password: str):
    """校验登录。成功返回 (AdminUser, token)；失败抛 401/429。"""
    ip = _client_ip(request)
    if _is_rate_limited(ip):
        raise HTTPException(429, '尝试次数过多，请 15 分钟后再试')
    assert db.pool is not None
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT id, username, password_hash FROM admin_users WHERE username = $1',
            username)
    if row is None or not verify_password(password, row['password_hash']):
        _record_failure(ip)
        raise HTTPException(401, '用户名或密码错误')
    _clear_failures(ip)
    async with db.pool.acquire() as conn:
        await conn.execute(
            'UPDATE admin_users SET last_login_at = now() WHERE id = $1', row['id'])
    admin = AdminUser(int(row['id']), row['username'])
    return admin, _sign(admin.id, admin.username)
