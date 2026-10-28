# -*- coding: utf-8 -*-
"""一键跑 tests/（含子目录，如 tests/conformance/）下全部 test_*.py。

tests/ 里混有三种形态（历史演化所致）：
  A. 纯 pytest 模块：模块级 def test_（同步），pytest 直接收集
  B. __main__ 守卫脚本：if __name__ == '__main__' 里 asyncio.run 全量跑
     （含 async 用例，pytest 无 async 插件收集不到）
  C. 模块级脚本：import 即执行全部断言（无守卫、无 test 函数）
判别：有 __main__ 守卫 / 有 async def test_ / 无 def test_ → 按脚本跑（B/C）；
      只有同步 def test_ → 按 pytest 跑（A）。

用法：python tests/run_all.py [子串过滤]
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable
name_filter = sys.argv[1] if len(sys.argv) > 1 else ''

pytest_files, script_files = [], []
for dirpath, dirnames, filenames in os.walk(HERE):
    dirnames[:] = [d for d in dirnames if d != '__pycache__']
    for name in sorted(filenames):
        if not name.startswith('test_') or not name.endswith('.py'):
            continue
        rel = os.path.relpath(os.path.join(dirpath, name), HERE)
        if name_filter and name_filter not in rel:
            continue
        with open(os.path.join(dirpath, name),
                  encoding='utf-8', errors='replace') as fp:
            src = fp.read()
        has_main_guard = 'if __name__' in src
        has_async_test = 'async def test_' in src
        has_sync_test = 'def test_' in src
        if has_main_guard or has_async_test or not has_sync_test:
            script_files.append(rel)
        else:
            pytest_files.append(rel)

failures = []
npass = spass = 0

print('A. pytest 模块 (%d):' % len(pytest_files))
for rel in pytest_files:
    r = subprocess.run(
        [PY, '-m', 'pytest', os.path.join('tests', rel), '-q', '--no-header'],
        cwd=ROOT, capture_output=True, text=True)
    out = r.stdout.strip().splitlines()
    tail = out[-1] if out else ''
    ok = r.returncode == 0 and 'no tests ran' not in tail
    if ok:
        npass += 1
    else:
        failures.append(rel)
    print('  %s %-44s %s' % ('PASS' if ok else 'FAIL', rel, tail))

print('\nB/C. 脚本式 (%d):' % len(script_files))
for rel in script_files:
    r = subprocess.run([PY, os.path.join('tests', rel)],
                       cwd=ROOT, capture_output=True, text=True)
    out = (r.stdout.strip().splitlines() or [''])[-1]
    # 退出码为唯一可信判据（个别脚本失败仍打 PASS 字样 / 退出码失真）
    ok = r.returncode == 0
    if ok:
        spass += 1
    else:
        failures.append(rel)
    print('  %s %-44s %s' % ('PASS' if ok else 'FAIL', rel,
                             ('rc=%d ' % r.returncode) + out[-58:]))

print('\n=== 汇总: pytest %d/%d, 脚本 %d/%d ==='
      % (npass, len(pytest_files), spass, len(script_files)))
if failures:
    print('失败:', ', '.join(failures))
    sys.exit(1)
print('ALL OK')
