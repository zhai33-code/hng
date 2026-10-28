"""warserver 日志按天滚动（state._rotate_daily / _Tee）回归测试。

根因（2026-09-08）：Windows 上 CPython open() 句柄不含 FILE_SHARE_DELETE，
旧实现先 os.replace 后 close —— 句柄还开着时 rename 必报 WinError 32，
被 except OSError 静默吞掉，realtest_game.log 从不拆分（累积到 18MB+）。

场景：
 1. 昨日 mtime + 打开句柄 → 滚动成功：旧文件改名为 *.YYYY-MM-DD.log，
    新句柄可写且写入落新文件
 2. 今日 mtime → 不滚动（原句柄原样返回）
 3. 文件被另一句柄占用（模拟多进程）→ 放弃改名，返回可写句柄继续写原文件
 4. _Tee.maybe_rotate 集成：跨天后 _fh 换新句柄，旧文件已改名
 5. _purge_old_logs：过期历史删除、保留期内不动、主文件不动
 6. 日志集中 server/logs/（2026-09-08）：LOG_FILE 落在 LOG_DIR 内；
    _adopt_legacy_logs 迁移包目录散落的旧日志（目标已存在不覆盖）
"""
import io
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import state


def _mk_log(dir_, mtime=None):
    path = os.path.join(dir_, 'realtest_game.log')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('seed\n')
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_rotate_renames_yesterday_file_with_open_handle():
    with tempfile.TemporaryDirectory() as d:
        path = _mk_log(d, mtime=time.time() - 86400)
        fh = open(path, 'a', encoding='utf-8', buffering=1)
        today = state._date_tag()
        new_fh = state._rotate_daily(fh, path, today)
        assert new_fh is not fh
        new_fh.write('today\n')
        new_fh.flush()
        archived = f'{path}.{state._date_tag(time.time() - 86400)}.log'
        assert os.path.exists(archived)
        assert os.path.exists(path)
        with open(archived, encoding='utf-8') as f:
            assert f.read() == 'seed\n'
        with open(path, encoding='utf-8') as f:
            assert f.read() == 'today\n'
        new_fh.close()


def test_rotate_keeps_today_file():
    with tempfile.TemporaryDirectory() as d:
        path = _mk_log(d)                      # mtime = 现在
        fh = open(path, 'a', encoding='utf-8', buffering=1)
        ret = state._rotate_daily(fh, path, state._date_tag())
        assert ret is fh
        assert os.listdir(d) == ['realtest_game.log']   # 无改名副产物
        fh.close()


def test_rotate_falls_back_when_file_locked_by_another_handle():
    with tempfile.TemporaryDirectory() as d:
        path = _mk_log(d, mtime=time.time() - 86400)
        fh = open(path, 'a', encoding='utf-8', buffering=1)
        lock = open(path, 'a', encoding='utf-8')        # 模拟其他进程占用
        try:
            ret = state._rotate_daily(fh, path, state._date_tag())
        finally:
            lock.close()
        ret.write('still-writable\n')                   # 返回句柄必须可写
        ret.flush()
        with open(path, encoding='utf-8') as f:
            assert 'still-writable' in f.read()
        assert os.listdir(d) == ['realtest_game.log']   # 放弃了改名
        ret.close()


def test_tee_maybe_rotate_switches_handle_across_days():
    with tempfile.TemporaryDirectory() as d:
        path = _mk_log(d)                              # 今天：构造时不滚
        fh = open(path, 'a', encoding='utf-8', buffering=1)
        tee = state._Tee(fh, path)
        assert tee._day == state._date_tag()
        yesterday = time.time() - 86400
        tee._day = state._date_tag(yesterday)          # 模拟跨天：_day 还是昨天
        os.utime(path, (yesterday, yesterday))         # 文件最后写入在昨天
        try:
            tee.maybe_rotate()
            assert tee._fh is not fh
            tee._stdout = io.StringIO()                # 不依赖真实控制台
            tee.write('new-day\n')                     # _Tee.write 落新文件
            assert os.path.exists(
                f'{path}.{state._date_tag(yesterday)}.log')
        finally:
            tee._fh.close()
            if not fh.closed:
                fh.close()
        with open(path, encoding='utf-8') as f:
            assert f.read() == 'new-day\n'


def test_purge_old_logs_by_filename_date():
    with tempfile.TemporaryDirectory() as d:
        path = _mk_log(d)
        old = os.path.join(d, 'realtest_game.log.2020-01-01.log')
        fresh = os.path.join(d,
                             f'realtest_game.log.{state._date_tag()}.log')
        for p in (old, fresh):
            with open(p, 'w', encoding='utf-8') as f:
                f.write('x\n')
        state._purge_old_logs(path, 3)
        assert not os.path.exists(old)
        assert os.path.exists(fresh)
        assert os.path.exists(path)                     # 主文件永不删


def test_log_dir_layout():
    assert os.path.isdir(state.LOG_DIR)
    assert os.path.dirname(state.LOG_FILE) == state.LOG_DIR
    assert os.path.basename(state.LOG_FILE) == 'realtest_game.log'


def test_adopt_legacy_logs_migrates_scattered_files():
    with tempfile.TemporaryDirectory() as d:
        pkg = os.path.join(d, 'server')
        logs = os.path.join(pkg, 'logs')
        os.makedirs(logs)
        legacy = [
            os.path.join(pkg, 'realtest_game.log'),
            os.path.join(pkg, 'realtest_game.log.2026-09-01.log'),
            os.path.join(pkg, 'requests.log'),
        ]
        for p in legacy:
            with open(p, 'w', encoding='utf-8') as f:
                f.write('legacy\n')
        keep = os.path.join(pkg, 'realtest_stderr.log')   # 不归 state 管
        with open(keep, 'w', encoding='utf-8') as f:
            f.write('x\n')
        # 新位置已有同名文件 → 旧文件保留原地（不覆盖新版本）
        with open(os.path.join(logs, 'requests.log'), 'w') as f:
            f.write('new\n')
        old_pkg, old_dir = state._PKG_DIR, state.LOG_DIR
        try:
            state._PKG_DIR, state.LOG_DIR = pkg, logs
            state._adopt_legacy_logs()
        finally:
            state._PKG_DIR, state.LOG_DIR = old_pkg, old_dir
        for p in legacy[:2]:
            assert not os.path.exists(p)                 # 游戏日志+历史全迁走
        assert os.path.exists(os.path.join(logs, 'realtest_game.log'))
        assert os.path.exists(
            os.path.join(logs, 'realtest_game.log.2026-09-01.log'))
        assert os.path.exists(legacy[2])                 # 目标存在 → 不迁不覆盖
        assert os.path.exists(keep)                      # stderr 不动
