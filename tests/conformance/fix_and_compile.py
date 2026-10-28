# -*- coding: utf-8 -*-
"""vendor hagcp proto → protoc 可编译版本 → 生成 Python bindings。

P3 conformance 基建的物料生成工具（spike %TEMP%\\hagcp_proto_spike 落地版）。
生成物（protos/*.proto + *_pb2.py）已提交仓库；仅当上游 hagcp proto 更新
时才需要重跑本脚本（venv 需 grpcio-tools）。

hagcp 的 proto 由 AS3 InitDescriptors 正则生成，给 protobufjs（宽松）用：
  - package 行在 syntax 行之前（protoc 要求 syntax 首行）
  - 声明 proto3 却用 required/optional（proto2 特性）
修补：syntax/proto2 置首行，按文件名补 package 与 import 关系，
required → optional（wire 字节完全等价，仅编码前 presence 校验差异，
且绕开 upb mini table "Too many required fields" 上限）。

出处（MIT）：Heroes-and-Generals-community-project /
packages/hagcp-network-client/src/protos/，E 盘只读引用。
用法：python tests/conformance/fix_and_compile.py [hagcp protos 目录]
"""
import re
import subprocess
import sys
from pathlib import Path

SRC = Path(sys.argv[1] if len(sys.argv) > 1 else
           r"E:\devProject\Heroes-and-Generals-community-project"
           r"\packages\hagcp-network-client\src\protos")
HERE = Path(__file__).parent
FIX = HERE / 'protos'
GEN = HERE

HEADER = {
    "Common":         ['syntax = "proto2";', '', 'package Common;'],
    "netsysmessages": ['syntax = "proto2";', '', 'package netsysmessages;',
                       '', 'import "Common.proto";'],
    "statrequests":   ['syntax = "proto2";', '', 'package statrequests;',
                       '', 'import "Common.proto";'],
    "HnG_States":     ['syntax = "proto2";', '', 'package HnG_States;',
                       '', 'import "Common.proto";'],
}

ORIGIN = [
    '// vendored from Heroes-and-Generals-community-project (MIT)',
    '// packages/hagcp-network-client/src/protos/%s.proto',
    '// converted: syntax/proto2 header, required -> optional (wire equal)',
    '',
]

FIX.mkdir(exist_ok=True)
for f in SRC.glob('*.proto'):
    stem = f.stem
    if stem == 'All':
        # 聚合壳：只留 import 四个包
        body = ['syntax = "proto2";', '',
                'import "HnG_States.proto";',
                'import "statrequests.proto";',
                'import "netsysmessages.proto";',
                'import "Common.proto";']
    else:
        body = [ln for ln in f.read_text(encoding='utf-8').splitlines()
                if not re.match(r'^(package|syntax)[\s=]', ln)]
        body = [re.sub(r'^(\s*)required(\s)', r'\1optional\2', ln)
                for ln in body]
    while body and not body[0].strip():
        body.pop(0)
    hdr = HEADER.get(stem, [])
    origin = [ln % stem if '%s' in ln else ln for ln in ORIGIN]
    (FIX / f.name).write_text('\n'.join(origin + hdr + body) + '\n',
                              encoding='utf-8')

r = subprocess.run(
    [sys.executable, '-m', 'grpc_tools.protoc',
     f'--proto_path={FIX}', f'--python_out={GEN}']
    + [str(FIX / (stem + '.proto')) for stem in HEADER]
    + [str(FIX / 'All.proto')],
    capture_output=True, text=True)
err = [ln for ln in r.stderr.splitlines()
       if 'absl' not in ln and 'WARNING' not in ln
       and not ln.startswith('W0000')]
print('exit =', r.returncode)
print('\n'.join(err[:30]) if err else '(stderr clean)')
for p in sorted(GEN.glob('*_pb2.py')):
    print('generated:', p.name, p.stat().st_size, 'bytes')
