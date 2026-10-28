"""register_captcha 单测：限速器 / 蜜罐 / 出题与校验一次性 / 令牌消费。

pi-captcha 已在 .venv 安装，真实出题路径直接覆盖；
缺库环境自动跳过出题相关用例（纯逻辑用例不受影响）。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import register_captcha as rc


@pytest.fixture(autouse=True)
def _clean_state():
    rc._reset_state()
    yield
    rc._reset_state()


# ---- WindowLimiter ----

class TestWindowLimiter:

    def test_allows_within_limit_then_blocks(self):
        lim = rc.WindowLimiter(3, 60)
        assert [lim.allow() for _ in range(3)] == [True, True, True]
        assert lim.allow() is False

    def test_unlimited_when_nonpositive(self):
        assert all(rc.WindowLimiter(0, 60).allow() for _ in range(50))
        assert all(rc.WindowLimiter(-1, 60).allow() for _ in range(50))

    def test_window_expiry_frees_budget(self):
        lim = rc.WindowLimiter(1, 60)
        assert lim.allow() is True
        assert lim.allow() is False
        lim._events[0] -= 61          # 拨到窗口外
        assert lim.allow() is True

    def test_no_accounting_on_deny(self):
        lim = rc.WindowLimiter(1, 60)
        lim.allow()
        lim.allow()                   # 被拒，不应记账
        assert len(lim._events) == 1


# ---- 蜜罐 ----

class TestHoneypot:

    @pytest.mark.parametrize('field', rc.HONEYPOT_FIELDS)
    def test_hit(self, field):
        assert rc.honeypot_hit({field: 'spam.example'}) is True

    def test_miss(self):
        assert rc.honeypot_hit({'website': '', 'nickname2': None,
                                'username': 'player1'}) is False
        assert rc.honeypot_hit({'username': 'player1'}) is False

    def test_non_dict(self):
        assert rc.honeypot_hit(None) is False
        assert rc.honeypot_hit('website=spam') is False


# ---- env 参数 ----

class TestEnvInt:

    def test_valid_and_invalid(self, monkeypatch):
        monkeypatch.setenv('X_TEST_N', '7')
        assert rc._env_int('X_TEST_N', 3) == 7
        monkeypatch.setenv('X_TEST_N', 'abc')
        assert rc._env_int('X_TEST_N', 3) == 3
        monkeypatch.setenv('X_TEST_N', '')
        assert rc._env_int('X_TEST_N', 3) == 3


# ---- PerKeyWindowLimiter（单 IP 限速）----

class TestPerKeyWindowLimiter:

    def test_allows_within_limit_then_blocks(self):
        lim = rc.PerKeyWindowLimiter(3, 60)
        assert [lim.allow('1.1.1.1') for _ in range(3)] == [True, True, True]
        assert lim.allow('1.1.1.1') is False

    def test_keys_are_independent(self):
        lim = rc.PerKeyWindowLimiter(2, 60)
        assert lim.allow('1.1.1.1') and lim.allow('1.1.1.1')
        assert lim.allow('2.2.2.2') is True       # 另一 IP 不受影响
        assert lim.allow('1.1.1.1') is False

    def test_unlimited_and_empty_key(self):
        lim = rc.PerKeyWindowLimiter(0, 60)
        assert all(lim.allow('1.1.1.1') for _ in range(50))
        assert rc.PerKeyWindowLimiter(3, 60).allow(None) is True
        assert rc.PerKeyWindowLimiter(3, 60).allow('') is True

    def test_window_expiry_frees_budget(self):
        lim = rc.PerKeyWindowLimiter(1, 60)
        assert lim.allow('1.1.1.1') is True
        assert lim.allow('1.1.1.1') is False
        lim._events['1.1.1.1'][0] -= 61
        assert lim.allow('1.1.1.1') is True

    def test_no_accounting_on_deny(self):
        lim = rc.PerKeyWindowLimiter(1, 60)
        lim.allow('1.1.1.1')
        lim.allow('1.1.1.1')
        assert len(lim._events['1.1.1.1']) == 1

    def test_inactive_keys_pruned(self):
        lim = rc.PerKeyWindowLimiter(1, 60)
        lim.allow('1.1.1.1')
        lim._events['1.1.1.1'][0] -= 120          # 拨出窗口
        lim._last_prune = 0.0
        lim.allow('2.2.2.2')                      # 触发偶发清理
        assert '1.1.1.1' not in lim._events


# ---- 出题 / 校验 / 令牌（真实 pi-captcha 链路）----

needs_lib = pytest.mark.skipif(not rc._LIB_OK, reason='pi-captcha 未安装')


@needs_lib
class TestCaptchaFlow:

    def test_issue_returns_slide_data(self):
        data = rc.issue_captcha()
        assert data is not None
        assert set(data) == {'captcha_id', 'image', 'thumb',
                             'thumbX', 'thumbY', 'thumbWidth', 'thumbHeight'}
        assert isinstance(data['captcha_id'], str) and data['captcha_id']
        assert data['image'].startswith('data:image/jpeg;base64,')
        assert data['thumb'].startswith('data:image/png;base64,')
        # 出图 300x220，拼图块随机 50-60px（官方默认 RangeVal）
        assert 50 <= data['thumbWidth'] <= 60
        assert 50 <= data['thumbHeight'] <= 60
        assert 0 <= data['thumbX'] < 300
        assert 0 <= data['thumbY'] < 220

    def test_check_correct_coords_issues_token(self):
        data = rc.issue_captcha()
        dx, dy, _ = rc._challenges[data['captcha_id']]
        token = rc.check_captcha(data['captcha_id'], dx, dy)
        assert isinstance(token, str) and len(token) == 64
        assert data['captcha_id'] not in rc._challenges   # 挑战一次性

    def test_check_wrong_coords_fails(self):
        data = rc.issue_captcha()
        dx, dy, _ = rc._challenges[data['captcha_id']]
        assert rc.check_captcha(data['captcha_id'], dx + 7, dy) is None
        assert rc.check_captcha(data['captcha_id'], dx, dy + 7) is None

    def test_check_is_one_shot(self):
        data = rc.issue_captcha()
        dx, dy, _ = rc._challenges[data['captcha_id']]
        assert rc.check_captcha(data['captcha_id'], dx, dy) is not None
        assert rc.check_captcha(data['captcha_id'], dx, dy) is None

    def test_check_unknown_or_bad_id(self):
        assert rc.check_captcha('deadbeef', 10, 10) is None
        assert rc.check_captcha(None, 10, 10) is None
        assert rc.check_captcha('x', 'not-int', 10) is None

    def test_token_consume_one_shot(self):
        data = rc.issue_captcha()
        dx, dy, _ = rc._challenges[data['captcha_id']]
        token = rc.check_captcha(data['captcha_id'], dx, dy)
        assert rc.consume_token(token) is True
        assert rc.consume_token(token) is False           # 一次性
        assert rc.consume_token(None) is False
        assert rc.consume_token('') is False
        assert rc.consume_token(12345) is False

    def test_register_budget_after_token(self):
        data = rc.issue_captcha()
        dx, dy, _ = rc._challenges[data['captcha_id']]
        token = rc.check_captcha(data['captcha_id'], dx, dy)
        assert rc.consume_token(token) is True
        assert rc.allow_register() is True
        # 拨满注册预算 → 后续拒绝
        rc._register_budget._events = [rc._register_budget._events[0]] * 20
        assert rc.allow_register() is False

    def test_captcha_budget_exhausted_returns_none(self):
        for _ in range(2):
            assert rc.issue_captcha() is not None
        rc._captcha_budget._events = [rc._captcha_budget._events[0]] * 30
        assert rc.issue_captcha() is None

    def test_issue_per_ip_budget(self):
        assert rc.issue_captcha('1.1.1.1') is not None
        rc._captcha_ip_budget._events['1.1.1.1'] = [time.time()] * 5
        assert rc.issue_captcha('1.1.1.1') is None
        assert rc.issue_captcha('2.2.2.2') is not None   # 其他 IP 不受影响

    def test_check_per_ip_budget(self):
        data = rc.issue_captcha()
        dx, dy, _ = rc._challenges[data['captcha_id']]
        ip = '9.9.9.9'
        rc._check_ip_budget._events[ip] = [time.time()] * 10   # 记满校验预算
        assert rc.check_captcha(data['captcha_id'], dx, dy, ip=ip) is None
        # 超频时挑战未被弹出（保留至自然过期）
        assert data['captcha_id'] in rc._challenges

    def test_register_per_ip_budget(self):
        data = rc.issue_captcha()
        dx, dy, _ = rc._challenges[data['captcha_id']]
        ip = '7.7.7.7'
        token = rc.check_captcha(data['captcha_id'], dx, dy, ip=ip)
        assert rc.consume_token(token) is True
        rc._register_ip_budget._events[ip] = [time.time()] * 5
        assert rc.allow_register(ip) is False
        assert rc.allow_register('8.8.8.8') is True      # 其他 IP 不受影响
