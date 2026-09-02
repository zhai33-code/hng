# -*- coding: utf-8 -*-
"""管理 API 路由层：参数解析 -> service 调用 -> 审计/响应，不写 SQL。

各领域逻辑在 server/services/（纯 asyncpg，不依赖 FastAPI）。
"""
import functools

from fastapi import HTTPException


def api_errors(func):
    """service 抛错的统一 HTTP 转换：ValueError->400，LookupError->404。"""
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except LookupError as e:
            raise HTTPException(404, str(e))
    return wrapper
