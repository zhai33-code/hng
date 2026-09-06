# -*- coding: utf-8 -*-
"""动态配置（server_config 表）：管理端校验/读写，游戏服运行时读取。

CONFIG_REGISTRY 注册各配置项（label + pydantic model + defaults），
routers/config.py 按 key 分发；新增配置项 = 这里注册 + 前端
ConfigView 加 tab + views/config/ 加 Panel 组件，路由层不动。
"""
import json

from pydantic import BaseModel, Field, field_validator


# ---- weather 默认值：与游戏服 mm_weather.py 同步（跨进程不 import，改一处记得改另一处）----
WEATHER_DEFAULTS = {
    'tod_weights': [2, 2, 4, 2],            # 夜/黎/白/黄
    'cond_weights': [5, 4, 1],              # 少云/阴/雾
    'excluded': [[0, 2], [1, 2], [3, 2]],   # 夜雾/黎明雾/黄昏雾（原版不设）
    'snow_chance': 0.3,
}


class WeatherConfig(BaseModel):
    """战局天气配置（存储结构见方案文档：维度独立，组合概率 = 相乘）。"""
    tod_weights: list[float] = Field(min_length=4, max_length=4)
    cond_weights: list[float] = Field(min_length=3, max_length=3)
    excluded: list[list[int]] = Field(default_factory=list)
    snow_chance: float = Field(ge=0.0, le=1.0)

    @field_validator('tod_weights', 'cond_weights')
    @classmethod
    def _weights_nonneg(cls, v):
        if any(w < 0 for w in v):
            raise ValueError('权重不能为负数')
        if sum(v) <= 0:
            raise ValueError('权重不能全为 0')
        return v

    @field_validator('excluded')
    @classmethod
    def _excluded_pairs(cls, v):
        pairs = []
        for item in v:
            if len(item) != 2:
                raise ValueError(f'排除组合格式错误：{item}（应为 [时段, 天气]）')
            tod, cond = item
            if not 0 <= tod <= 3 or not 0 <= cond <= 2:
                raise ValueError(f'排除组合越界：{item}（时段 0..3，天气 0..2）')
            if [tod, cond] not in pairs:
                pairs.append([tod, cond])
        if len(pairs) >= 4 * 3:
            raise ValueError('不能排除全部 12 种组合')
        return pairs


CONFIG_REGISTRY = {
    'weather': {'label': '战局天气', 'model': WeatherConfig,
                'defaults': WEATHER_DEFAULTS},
}


async def get_config_item(pool, key: str) -> dict:
    """读一项配置：有行返回落库值 + 保存者信息；无行返回内置默认。"""
    entry = CONFIG_REGISTRY[key]
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT value, updated_at, updated_by FROM server_config '
            'WHERE key = $1', key)
    if row is None:
        return {'configured': False, 'config': entry['defaults'],
                'updated_at': None, 'updated_by': ''}
    # asyncpg 未注册 codec 时 JSONB 列返回 str（war.py 同款坑），须 json.loads
    return {'configured': True, 'config': json.loads(row['value']),
            'updated_at': row['updated_at'].isoformat(),
            'updated_by': row['updated_by']}


async def save_config_item(pool, key: str, payload: dict, admin_name: str) -> dict:
    """校验并保存一项配置（UPSERT），返回落库值。校验失败抛 ValueError。"""
    entry = CONFIG_REGISTRY[key]
    try:
        cfg = entry['model'](**payload)
    except Exception as e:
        raise ValueError(str(e)) from e
    value = cfg.model_dump()
    async with pool.acquire() as conn:
        await conn.execute(
            'INSERT INTO server_config (key, value, updated_by) '
            'VALUES ($1, $2::jsonb, $3) '
            'ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, '
            'updated_at = now(), updated_by = EXCLUDED.updated_by',
            key,
            json.dumps(value, ensure_ascii=False, separators=(',', ':')),
            admin_name)
    return value
