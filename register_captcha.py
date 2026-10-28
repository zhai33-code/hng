"""注册页反滥用模块：滑动验证码出题/校验 + 一次性注册令牌 + 全局限速 + 蜜罐。

设计要点：
- 纯内存状态（进程内 dict + threading.Lock），无 DB 依赖，可被 pytest 直测。
- pi-captcha / Pillow 懒加载：本模块 import 永不失败（缺库时验证码端点退化为
  不可用，web_server 注册入口 fail-closed 拒绝无令牌注册，游戏服不受影响）。
- IP 直连（无反代）：handler.client_address 即真实客户端 IP（TCP 握手不可伪造），
  限速按 IP 记账；全局滑动窗口保留作总兜底（切勿信任 X-Forwarded-For）。
- 挑战与令牌均一次性（校验/消费即弹出），TTL 到期即失效。
- 素材来自 MIT 协议的 wenlng/pi-captcha examples（与 go-captcha 同作者生态），
  存放于 captcha_assets/。

环境变量：
- HNG_REG_CAPTCHA_RATE     每 10 分钟全局出题上限（默认 30，<=0 视为不限）
- HNG_REG_RATE_LIMIT       每小时全局注册（令牌消费）上限（默认 20，<=0 视为不限）
- HNG_REG_CAPTCHA_IP_RATE  每 10 分钟单 IP 出题上限（默认 5，<=0 视为不限）
- HNG_REG_CHECK_IP_RATE    每 10 分钟单 IP 校验尝试上限（默认 10，含失败）
- HNG_REG_REGISTER_IP_RATE 每小时单 IP 注册上限（默认 5，<=0 视为不限）
"""

import os
import secrets
import threading
import time

# ---- 常量 ----
ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'captcha_assets')
CAPTCHA_TTL = 120          # 验证码挑战有效期（秒）
TOKEN_TTL = 300            # 注册令牌有效期（秒）
CHALLENGE_MAX = 512        # 挑战内存上限（防膨胀，超出拒绝出题）
TOKEN_MAX = 1024           # 令牌内存上限
PAD = 5                    # 滑动坐标容差（官方 example 同值）


def _env_int(name, default):
    try:
        return int(os.environ.get(name, '') or default)
    except ValueError:
        return default


# pi-captcha 缺库时保持模块可导入（is_available()=False）
try:
    from pi_captcha import SlideBuilder
    from pi_captcha.slide.block import GraphImage
    from pi_captcha.slide.validate import validate as _slide_validate
    _LIB_OK = True
except ImportError:
    _LIB_OK = False


class WindowLimiter:
    """全局滑动窗口计数限速器（线程安全）。limit<=0 表示不限。"""

    def __init__(self, limit, window):
        self.limit = int(limit)
        self.window = float(window)
        self._events = []          # 命中时间戳列表
        self._lock = threading.Lock()

    def allow(self):
        """预算内返回 True 并记一次；超出返回 False（不记账）。"""
        if self.limit <= 0:
            return True
        now = time.time()
        with self._lock:
            cutoff = now - self.window
            if self._events and self._events[0] < cutoff:
                # 就地裁剪，避免频繁重建列表
                i = 0
                events = self._events
                while i < len(events) and events[i] < cutoff:
                    i += 1
                del events[:i]
            if len(self._events) >= self.limit:
                return False
            self._events.append(now)
            return True


# 全局限速兜底：出题（图像生成 CPU 开销大）+ 注册（DB 写入兜底）
_captcha_budget = WindowLimiter(
    _env_int('HNG_REG_CAPTCHA_RATE', 30), 600)
_register_budget = WindowLimiter(
    _env_int('HNG_REG_RATE_LIMIT', 20), 3600)


class PerKeyWindowLimiter:
    """按 key（客户端 IP）的滑动窗口限速器（线程安全）。limit<=0 或 key 为空不限。"""

    def __init__(self, limit, window):
        self.limit = int(limit)
        self.window = float(window)
        self._events = {}        # key -> 命中时间戳列表
        self._lock = threading.Lock()
        self._last_prune = 0.0

    def allow(self, key):
        """预算内返回 True 并记一次；超出返回 False（不记账）。"""
        if self.limit <= 0 or not key:
            return True
        now = time.time()
        with self._lock:
            events = self._events
            ev = events.get(key)
            if ev is None:
                ev = events[key] = []
            cutoff = now - self.window
            while ev and ev[0] < cutoff:
                del ev[0]
            if len(ev) >= self.limit:
                return False
            ev.append(now)
            # 偶发清理失活 key，防长期运行内存膨胀
            if now - self._last_prune > self.window:
                for k in [k for k, e in events.items() if not e or e[-1] < cutoff]:
                    del events[k]
                self._last_prune = now
            return True

    def reset(self):
        """测试辅助：清空全部按 key 记账。"""
        with self._lock:
            self._events.clear()


# 单 IP 限速（IP 直连，client_address 可信；XFF 一律不信任）
_captcha_ip_budget = PerKeyWindowLimiter(
    _env_int('HNG_REG_CAPTCHA_IP_RATE', 5), 600)
_check_ip_budget = PerKeyWindowLimiter(
    _env_int('HNG_REG_CHECK_IP_RATE', 10), 600)
_register_ip_budget = PerKeyWindowLimiter(
    _env_int('HNG_REG_REGISTER_IP_RATE', 5), 3600)

_lock = threading.Lock()
_challenges = {}   # captcha_id -> (dx, dy, expires_at)
_tokens = {}       # token -> expires_at
_resources = None  # 懒加载 (backgrounds, graphs)


def is_available():
    """pi-captcha 库与素材是否就绪（不就绪时出题端点回 503）。"""
    return _LIB_OK and os.path.isdir(ASSETS_DIR)


def _load_resources():
    """懒加载底图与拼图块三件套（仅首次调用）。"""
    global _resources
    if _resources is not None or not _LIB_OK:
        return _resources
    from PIL import Image
    backgrounds = []
    bg_dir = os.path.join(ASSETS_DIR, 'backgrounds')
    for name in sorted(os.listdir(bg_dir)):
        # 点开头跳过：macOS 拷贝残留的 AppleDouble（._bg-1.jpg）会被
        # endswith('.jpg') 误收 → PIL UnidentifiedImageError 出题 500
        if name.startswith('.'):
            continue
        if name.lower().endswith(('.jpg', '.jpeg', '.png')):
            backgrounds.append(
                Image.open(os.path.join(bg_dir, name)).convert('RGB'))
    graphs = []
    tiles_dir = os.path.join(ASSETS_DIR, 'tiles')
    for name in sorted(os.listdir(tiles_dir)):
        if name.startswith('.'):
            continue
        sub = os.path.join(tiles_dir, name)
        if not os.path.isdir(sub):
            continue
        paths = {k: os.path.join(sub, f'{k}.png')
                 for k in ('tile', 'tile-shadow', 'tile-mask')}
        if not all(os.path.isfile(p) for p in paths.values()):
            continue
        g = GraphImage()
        # 三件套均需保持 RGBA 官方语义：mask 形状编码在 alpha 通道（库内部
        # resize 后 .convert('RGBA') 取 A 通道做拼图形状蒙版）。此前误用
        # convert('L') 会在 L->RGBA 时丢失形状（alpha 恒 255）→ 拼图块退化
        # 为整块方形背景图。convert 同时 eager 加载（共享 Image 缓存跨线程
        # 只读安全，裸 open 的惰性文件句柄并发 resize 会竞争文件指针）。
        g.overlay_image = Image.open(paths['tile']).convert('RGBA')
        g.shadow_image = Image.open(paths['tile-shadow']).convert('RGBA')
        g.mask_image = Image.open(paths['tile-mask']).convert('RGBA')
        graphs.append(g)
    _resources = (backgrounds, graphs)
    return _resources


def _prune_locked(now):
    """调用方须已持 _lock：清理过期挑战/令牌。"""
    for cid in [k for k, v in _challenges.items() if v[2] <= now]:
        del _challenges[cid]
    for tok in [k for k, v in _tokens.items() if v <= now]:
        del _tokens[tok]


def issue_captcha(ip=None):
    """出题。返回 go-captcha-vue SlideData 结构（附 captcha_id）；
    限速 / 缺库 / 缺素材返回 None。"""
    if not is_available() or not _captcha_budget.allow():
        return None
    if not _captcha_ip_budget.allow(ip):
        return None
    backgrounds, graphs = _load_resources()
    if not backgrounds or not graphs:
        return None
    data = SlideBuilder().set_resources(
        SlideBuilder.with_backgrounds(list(backgrounds)),
        SlideBuilder.with_graph_images(list(graphs)),
    ).make().generate()
    block = data.get_data()
    now = time.time()
    with _lock:
        _prune_locked(now)
        if len(_challenges) >= CHALLENGE_MAX:
            return None
        cid = secrets.token_hex(16)
        _challenges[cid] = (block.dx, block.dy, now + CAPTCHA_TTL)
    return {
        'captcha_id': cid,
        'image': data.get_master_image().to_base64(),
        'thumb': data.get_tile_image().to_base64(),
        'thumbX': block.tile_x,
        'thumbY': block.tile_y,
        'thumbWidth': block.width,
        'thumbHeight': block.height,
    }


def check_captcha(captcha_id, x, y, ip=None):
    """校验滑动坐标。通过返回一次性注册令牌；失败/过期/超频返回 None。
    挑战无论成败均弹出（一次性）。ip 记账含失败尝试（防离线爆破坐标）。"""
    try:
        px, py = int(x), int(y)
    except (TypeError, ValueError):
        return None
    # 校验尝试预算：在弹出挑战之前记（超频时挑战保留自然过期）
    if not _check_ip_budget.allow(ip):
        return None
    now = time.time()
    with _lock:
        _prune_locked(now)
        entry = _challenges.pop(captcha_id, None) \
            if isinstance(captcha_id, str) else None
    if entry is None:
        return None
    dx, dy, expires = entry
    if expires <= now or not _slide_validate(px, py, dx, dy, PAD):
        return None
    token = secrets.token_hex(32)
    with _lock:
        _prune_locked(now)
        if len(_tokens) >= TOKEN_MAX:
            return None
        _tokens[token] = now + TOKEN_TTL
    return token


def consume_token(token):
    """一次性消费注册令牌。有效返回 True。"""
    if not isinstance(token, str) or not token:
        return False
    now = time.time()
    with _lock:
        _prune_locked(now)
        expires = _tokens.pop(token, None)
    return expires is not None and expires > now


def allow_register(ip=None):
    """注册限速预算：单 IP 为主 + 全局兜底（web_server /api/register
    在令牌校验后调用）。"""
    return _register_ip_budget.allow(ip) and _register_budget.allow()


# ---- 蜜罐 ----

HONEYPOT_FIELDS = ('website', 'nickname2')


def honeypot_hit(data):
    """蜜罐字段命中（机器人填了隐藏字段）→ True。正常用户不可见不会填。"""
    if not isinstance(data, dict):
        return False
    return any(str(data.get(f) or '').strip() for f in HONEYPOT_FIELDS)


def _reset_state():
    """测试辅助：清空全部内存状态。"""
    global _resources
    with _lock:
        _challenges.clear()
        _tokens.clear()
    _captcha_budget._events.clear()
    _register_budget._events.clear()
    _captcha_ip_budget.reset()
    _check_ip_budget.reset()
    _register_ip_budget.reset()
