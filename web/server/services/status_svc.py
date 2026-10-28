# -*- coding: utf-8 -*-
"""服务器状态 / 在线统计（非 DB：TCP 探测 + 游戏服 /online.json 拉取）。"""
import json
import socket
import urllib.request

from ..config import GAME_HTTP_BASE


def probe(host: str, port: int, timeout: float = 2.0) -> bool:
    """TCP 端口连通性探测。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


async def database_ok(pool) -> bool:
    if pool is None:
        return False
    try:
        async with pool.acquire() as conn:
            return await conn.fetchval('SELECT 1') == 1
    except Exception:
        return False


def fetch_online_snapshot(timeout: float = 3.0):
    """同步拉取游戏服 /online.json（调用方用 to_thread 走线程池）。"""
    url = f'{GAME_HTTP_BASE}/online.json'
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception:
        return None
