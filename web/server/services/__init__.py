# -*- coding: utf-8 -*-
"""领域服务层：SQL 访问 + 业务校验 + 响应组装（纯 asyncpg，不依赖 FastAPI）。

错误约定（routers.api_errors 统一转 HTTP）：
- ValueError   -> 400（参数/业务校验失败）
- LookupError  -> 404（目标不存在）
"""
