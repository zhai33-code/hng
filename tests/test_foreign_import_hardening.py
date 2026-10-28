# -*- coding: utf-8 -*-
"""外服导入加固（2026-09-07，china... 502 事故）：
1. collect_foreign 网络类失败（超时/断连）自动整体重试一次，重试成功即返回
2. 两次都失败 → ForeignCollectError（带最后一次错误）
3. 登录凭据错误（LoginError）不重试，立即抛 ForeignLoginError
4. 重试尝试独立 dump 目录（_retryN 后缀防同秒覆盖）
5. run_import 建号前用户名预检：占用时采集器完全不启动
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools'))

import foreign_import
from foreign_sync_probe import LoginError


class FlakyClient:
    """前 fail 次尝试抛网络错误，之后成功。记录每次实例化的 out_dir。"""

    fail_times = 0
    instances = []

    def __init__(self, host, port, username, password, out_dir, log=print):
        self.out_dir = out_dir
        self.w = None
        self.r = None
        self.tables = {'player': {'set': [], 'delete': []},
                       'character': {'set': [], 'delete': []}}
        self.dumped = 0
        FlakyClient.instances.append(out_dir)
        self._n = len(FlakyClient.instances)

    async def login(self):
        pass

    async def subscribe(self, timeout=60):
        if self._n <= FlakyClient.fail_times:
            raise TimeoutError('等待 subscriberesponse 超时')

    async def pull(self):
        pass

    async def collect(self, duration):
        if self._n <= FlakyClient.fail_times:
            raise ConnectionError('服务器连接已断开')

    def dump(self):
        self.dumped += 1

    def log(self, *a):
        pass


def _run(coro):
    return asyncio.run(coro)


def test_retry_then_success():
    FlakyClient.fail_times = 1
    FlakyClient.instances = []
    import foreign_import as fi
    orig = fi.ForeignClient
    fi.ForeignClient = FlakyClient
    try:
        logs = []
        cli, out_dir = _run(fi.collect_foreign(
            'h', 1, 'u', 'p', duration=0, dump_base=tempfile.mkdtemp(),
            log=logs.append))
        assert len(FlakyClient.instances) == 2
        assert out_dir == FlakyClient.instances[-1]
        assert cli.dumped == 1
        assert any('1/2' in m for m in logs), logs
    finally:
        fi.ForeignClient = orig


def test_both_attempts_fail():
    FlakyClient.fail_times = 2
    FlakyClient.instances = []
    import foreign_import as fi
    orig = fi.ForeignClient
    fi.ForeignClient = FlakyClient
    try:
        try:
            _run(fi.collect_foreign(
                'h', 1, 'u', 'p', duration=0, dump_base=tempfile.mkdtemp(),
                log=lambda *a: None))
            assert False, '应当抛 ForeignCollectError'
        except foreign_import.ForeignCollectError as e:
            assert 'subscriberesponse' in str(e), e   # 最后一次的错误
        assert len(FlakyClient.instances) == 2
    finally:
        fi.ForeignClient = orig


def test_login_error_no_retry():
    """凭据错误立即抛 ForeignLoginError，不浪费重试。"""
    FlakyClient.fail_times = 99   # collect 永远失败，但 login 先抛
    FlakyClient.instances = []

    class BadAuthClient(FlakyClient):
        async def login(self):
            raise LoginError(98, '登录失败 code=98')

    import foreign_import as fi
    orig = fi.ForeignClient
    fi.ForeignClient = BadAuthClient
    try:
        try:
            _run(fi.collect_foreign(
                'h', 1, 'u', 'p', duration=0, dump_base=tempfile.mkdtemp(),
                log=lambda *a: None))
            assert False, '应当抛 ForeignLoginError'
        except foreign_import.ForeignLoginError as e:
            assert e.code == 98
        assert len(FlakyClient.instances) == 1   # 未重试
    finally:
        fi.ForeignClient = orig


def test_retry_dump_dir_suffix():
    """重试目录带 _retry2 后缀，与首次目录区分。"""
    FlakyClient.fail_times = 1
    FlakyClient.instances = []
    import foreign_import as fi
    orig = fi.ForeignClient
    fi.ForeignClient = FlakyClient
    try:
        base = tempfile.mkdtemp()
        _run(fi.collect_foreign('h', 1, 'u', 'p', duration=0,
                                dump_base=base, log=lambda *a: None))
        first, second = FlakyClient.instances
        assert not first.endswith('_retry2')
        assert second.endswith('_retry2'), second
        assert os.path.dirname(second) == base
    finally:
        fi.ForeignClient = orig


class TakenStore:
    """用户名已占用的 store：get_player 返回已存在 id。"""

    async def get_player(self, username):
        return 1000082


class ProbeCounter:
    """探针是否被启动的哨兵。"""

    def __init__(self):
        self.started = 0


def test_run_import_username_precheck():
    """用户名占用 → 采集器完全不启动，立即 UsernameTakenError。"""
    counter = ProbeCounter()

    async def fake_collect(*a, **kw):
        counter.started += 1
        raise AssertionError('不应启动采集')

    import foreign_import as fi
    orig = fi.collect_foreign
    fi.collect_foreign = fake_collect
    try:
        try:
            _run(fi.run_import(TakenStore(), 'h', 1, 'foreign', 'fp',
                               'taken_name', 'lp'))
            assert False, '应当抛 UsernameTakenError'
        except foreign_import.UsernameTakenError as e:
            assert 'taken_name' in str(e)
        assert counter.started == 0
    finally:
        fi.collect_foreign = orig


if __name__ == '__main__':
    test_retry_then_success()
    test_both_attempts_fail()
    test_login_error_no_retry()
    test_retry_dump_dir_suffix()
    test_run_import_username_precheck()
    print('ALL OK')
