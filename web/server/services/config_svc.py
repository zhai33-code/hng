# -*- coding: utf-8 -*-
"""动态配置（server_config 表）：管理端校验/读写，游戏服运行时读取。

CONFIG_REGISTRY 注册各配置项（label + pydantic model + defaults），
routers/config.py 按 key 分发；新增配置项 = 这里注册 + 前端
ConfigView 加 tab + views/config/ 加 Panel 组件，路由层不动。
"""
import json

from pydantic import BaseModel, Field, field_validator, model_validator


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


# ---- combat_hosts 默认值：单机形态，与游戏服 env 回退（MM_MAX_COMBAT_SERVERS
#      / MM_COMBAT_PORT_* 缺省值）对齐；仅作面板展示，未保存配置行时游戏服
#      按各自 env 实际值回退（跨进程不 import，改一处记得改另一处）----
COMBAT_HOSTS_DEFAULTS = {
    'hosts': [{
        'name': 'local', 'addr': '127.0.0.1', 'public_addr': '',
        'max': 4, 'port_base': 5678, 'port_max': 5699,
        'agent': '', 'warserver': '',
    }],
}


class CombatHostEntry(BaseModel):
    """单台战斗服主机（校验规则与游戏服 combat_hosts._parse_hosts 同步）。

    addr          该机在 warserver 网络中的地址（握手来源 IP 匹配）
    public_addr   客户端 game_connect 用地址（缺省 = addr）
    max           本机最大并发实例数（0 = 不限，端口池自然封顶）
    port_base/max 本机独立 UDP 端口池（各机互不挤占，可用同号段）
    agent         空 = 本机直接 spawn；http://ip:port = 远端 combat_agent
    warserver     该机 combat server 回连地址（空 = 游戏服 env）
    """
    name: str = Field(min_length=1, max_length=32)
    addr: str = Field(min_length=1, max_length=64)
    public_addr: str = Field(default='', max_length=64)
    max: int = Field(default=0, ge=0, le=64)
    port_base: int = Field(ge=1024, le=65535)
    port_max: int = Field(ge=1024, le=65535)
    agent: str = Field(default='', max_length=128)
    warserver: str = Field(default='', max_length=64)

    @field_validator('name', 'addr', 'public_addr', 'agent', 'warserver')
    @classmethod
    def _strip(cls, v):
        return v.strip()

    @model_validator(mode='after')
    def _entry_ok(self):
        if self.port_base > self.port_max:
            raise ValueError(f'{self.name}: 端口池 {self.port_base}-'
                             f'{self.port_max} 非法（base > max）')
        ports = self.port_max - self.port_base + 1
        if self.max and ports < self.max:
            raise ValueError(f'{self.name}: 端口池({ports} 个) < 容量'
                             f'({self.max})')
        if self.agent and not self.agent.startswith('http://'):
            raise ValueError(f'{self.name}: agent 须为 http:// URL')
        return self


class CombatHostsConfig(BaseModel):
    """战斗服主机列表：成局时按各机剩余容量/端口池选机，保存即生效。"""
    hosts: list[CombatHostEntry] = Field(min_length=1, max_length=16)

    @model_validator(mode='after')
    def _names_unique(self):
        names = [h.name for h in self.hosts]
        if len(names) != len(set(names)):
            raise ValueError('主机名重复：' + ', '.join(names))
        return self


CONFIG_REGISTRY = {
    'weather': {'label': '战局天气', 'model': WeatherConfig,
                'defaults': WEATHER_DEFAULTS},
    'combat_hosts': {'label': '战斗服主机', 'model': CombatHostsConfig,
                     'defaults': COMBAT_HOSTS_DEFAULTS},
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
