"""web_server / file_server 文件下发域 byte 级快照测试（重构基线）。

目的：web_server.py 瘦身重构（docs/web-server-refactor-plan.md）期间，
锁住静态文件下发域的模块级生成产物——任何拆分/去重不得改变这些字节。

覆盖：settings.js、hngsync 主/子清单（UNIQUE_FILE_XML / MANIFEST_CAS_MAP /
wardb_sync / flashres / gce64）、texts 与 flashres 的 CAS 映射、
CAS 命名自洽性。不覆盖 wardb.py 自身的表扫描（WARDB_UNIQUE 由
wardb 模块负责，仅经 WARDB_SYNC_FULL 间接覆盖）。

用法：
  .venv\\Scripts\\python.exe tests\\test_file_server_snapshot.py --update   # 生成基线
  .venv\\Scripts\\python.exe tests\\test_file_server_snapshot.py            # 对比基线
  .venv\\Scripts\\python.exe -m pytest tests\\test_file_server_snapshot.py  # 同对比

注意：基线含本机 GAME_DIR/TEXTS_DIR 内容哈希与 HNG_PUBLIC_HOST 值
（.env 参与 SETTINGS_JS 组装），换环境需重新 --update。
"""

import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import web_server

BASELINE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'file_server_snapshot_baseline.json')

_BYTES_SUBJECTS = (
    'SETTINGS_JS',
    'UNIQUE_FILE_XML',
    'UNIQUE_FILE_XML_BZ2',
    'WARDB_SYNC_FULL',
    'WARDB_SYNC_BZ2',
    'FLASHRES_UNIQUE',
    'FLASHRES_UNIQUE_BZ2',
    'WARDB_UNIQUE_BZ2',
    'GCE64_UNIQUE',
    'GCE64_UNIQUE_BZ2',
)
_MAP_SUBJECTS = ('MANIFEST_CAS_MAP', 'TEXTS_CAS_MAP', 'FLASHRES_CAS_MAP')


def _digest_bytes(body):
    return {'kind': 'bytes', 'sha256': hashlib.sha256(body).hexdigest(),
            'size': len(body)}


def _digest_map(m):
    return {'kind': 'map', 'entries': len(m),
            'items': sorted([k, hashlib.sha256(v).hexdigest(), len(v)]
                            for k, v in m.items())}


def _subjects():
    """收集全部快照项。阶段 1 拆分后符号迁移到 file_server，只需改这里。"""
    out = {name: _digest_bytes(getattr(web_server, name))
           for name in _BYTES_SUBJECTS}
    # UNIQUE_VERSION_FILENAME 是短字符串，直接存值便于人工核对
    out['UNIQUE_VERSION_FILENAME'] = {
        'kind': 'str', 'value': web_server.UNIQUE_VERSION_FILENAME}
    out.update({name: _digest_map(getattr(web_server, name))
                for name in _MAP_SUBJECTS})
    return out


def _describe(subject):
    if subject['kind'] == 'map':
        return f"map({subject['entries']} entries)"
    if subject['kind'] == 'str':
        return repr(subject['value'])
    return f"{subject['size']}B sha256={subject['sha256'][:16]}…"


def _diff(base, cur):
    out = []
    for name in sorted(set(base) | set(cur)):
        if name not in cur:
            out.append(f'{name}: 基线有、当前无')
        elif name not in base:
            out.append(f'{name}: 当前新增（基线无）')
        elif base[name] != cur[name]:
            out.append(f'{name}: CHANGED  基线 {_describe(base[name])} '
                       f'-> 当前 {_describe(cur[name])}')
    return out


# ---------- 恒真结构断言（不依赖基线文件） ----------

def test_settings_js_locale_family_complete():
    """settings.js 必须覆盖全部 12 个 locale（缺语言会闪回英文）。"""
    for lang in web_server.SUPPORTED_LOCALES:
        assert f'locale_{lang}="HnG_Strategy_{lang}.xml";'.encode() \
            in web_server.SETTINGS_JS, lang


def test_settings_js_uniqueversionfilename_is_cas_name():
    """settings.js 的清单名必须指向当前主清单 CAS 名。"""
    assert (f'uniqueversionfilename="{web_server.UNIQUE_VERSION_FILENAME}"'
            .encode()) in web_server.SETTINGS_JS


def test_manifest_cas_names_selfconsistent():
    """MANIFEST_CAS_MAP 的键必须是内容的 sha256-size_0（hngsync 前缀校验）。"""
    for name, body in web_server.MANIFEST_CAS_MAP.items():
        expect = f'{hashlib.sha256(body).hexdigest()}-{len(body)}_0'
        assert name == expect, (name, expect)


def test_unique_version_filename_matches_xml():
    assert web_server.UNIQUE_VERSION_FILENAME == web_server._cas_name(
        web_server.UNIQUE_FILE_XML)


# ---------- 基线对比 ----------

def test_baseline_matches():
    import pytest
    if not os.path.exists(BASELINE):
        pytest.fail('基线文件不存在：先运行 '
                    '.venv\\Scripts\\python.exe tests\\test_file_server_snapshot.py '
                    '--update 生成')
    with open(BASELINE, encoding='utf-8') as f:
        base = json.load(f)
    diffs = _diff(base['subjects'], _subjects())
    assert not diffs, '快照与基线不一致:\n  ' + '\n  '.join(diffs)


def _write_baseline():
    import state
    data = {
        'note': 'web_server 重构基线，生成后勿手工编辑；'
                '内容变化需先确认来源（改表/加 cas 文件属正常变化）再 --update',
        'public_host': state.PUBLIC_HOST,
        'game_dir': state.GAME_DIR,
        'subjects': _subjects(),
    }
    with open(BASELINE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write('\n')
    print(f'[snapshot] baseline written: {BASELINE} '
          f'({len(data["subjects"])} subjects)')


if __name__ == '__main__':
    if '--update' in sys.argv:
        _write_baseline()
    else:
        if not os.path.exists(BASELINE):
            print('[snapshot] FAIL 基线不存在，先 --update 生成')
            sys.exit(1)
        with open(BASELINE, encoding='utf-8') as f:
            base = json.load(f)
        diffs = _diff(base['subjects'], _subjects())
        if diffs:
            print('[snapshot] FAIL 与基线不一致:')
            for d in diffs:
                print('  ' + d)
            sys.exit(1)
        print(f'[snapshot] PASS {len(base["subjects"])} subjects match baseline')
