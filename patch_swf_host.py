#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 HeroesAndGeneralsDesktop.swf 里写死的 settings 主机 64.90.31.56/ 换成 127.0.0.2///
原理：
  SWF 为 CWS（zlib 压缩体）。ABC 常量池里有一条长度前缀 u30=0x0C 的字符串 "64.90.31.56/"，
  它被用来拼 settings 地址： HTTP_PREFIX("http://") + "64.90.31.56/" + SETTINGS_FILE("/settings.js")
  => http://64.90.31.56//settings.js
  替换成等长 12 字节的 "127.0.0.2///" 后：
  => http://127.0.0.2///settings.js   （本地服务端合并多斜杠，已实测 200）
等长替换是关键：ABC 常量池的 u30 长度前缀与后续字节偏移全部不变，无需重算 ABC/DoABC 长度。
仅重新压缩外层 zlib 流，头部 FileLength 仍为解压后长度，保持不变。

用法：
  python patch_swf_host.py <原SWF> <输出SWF> [--inplace-backup 后缀]
"""
import sys, os, zlib, shutil

OLD = b"64.90.31.56/"          # 12 bytes
NEW = b"127.0.0.2///"          # 12 bytes（必须等长）
assert len(OLD) == len(NEW) == 12


def patch(src, dst):
    with open(src, "rb") as f:
        hdr = f.read(8)
        if hdr[:3] != b"CWS":
            raise SystemExit("不是 CWS 压缩 SWF，实际 magic=%r" % hdr[:3])
        f.seek(8)
        d = zlib.decompressobj()
        out = bytearray()
        hits = []
        total_in = 0
        while True:
            chunk = f.read(4 * 1024 * 1024)
            if not chunk:
                break
            total_in += len(chunk)
            out += d.decompress(chunk)
        rest = d.flush()
        if rest:
            out += rest
    data = bytes(out)
    print("解压完成: %d bytes (压缩流 %d bytes)" % (len(data), total_in))

    i = 0
    while True:
        i = data.find(OLD, i)
        if i < 0:
            break
        # 只认 ABC 字符串池里的那条：前一字节必须是长度前缀 0x0C
        if i >= 1 and data[i - 1] == len(OLD):
            hits.append(i)
        i += 1
    print("命中常量(带 0x0C 长度前缀):", [hex(x) for x in hits])
    if len(hits) != 1:
        raise SystemExit("命中数不是 1，停止（避免误改）")

    off = hits[0]
    patched = bytearray(data)
    patched[off:off + len(OLD)] = NEW
    assert len(patched) == len(data)

    comp = zlib.compress(bytes(patched), 6)
    with open(dst, "wb") as f:
        f.write(b"CWS")          # magic
        f.write(hdr[3:4])        # version
        f.write(hdr[4:8])        # 原 FileLength（= 解压后长度+8），等长替换后不变
        f.write(comp)
    print("已写出: %s  (压缩体 %d bytes, 文件 %d bytes)" %
          (dst, len(comp), os.path.getsize(dst)))
    print("替换: %r -> %r  @ decompressed 0x%X" % (OLD, NEW, off))
    return off


def verify(path, expect_new):
    with open(path, "rb") as f:
        hdr = f.read(8)
        f.seek(8)
        d = zlib.decompressobj()
        data = bytearray()
        while True:
            chunk = f.read(4 * 1024 * 1024)
            if not chunk:
                break
            data += d.decompress(chunk)
        data += d.flush()
    d2 = bytes(data)
    ok_len = len(d2) == int.from_bytes(hdr[4:8], "little") - 8
    print("verify: magic=%r version=%d fileLength=%d decompressed=%d len_ok=%s" %
          (hdr[:3], hdr[3], int.from_bytes(hdr[4:8], "little"), len(d2), ok_len))
    print("  仍含 '64.90.31.56/': %s" % (d2.find(b"64.90.31.56/") >= 0))
    print("  含新常量:         %s" % (d2.find(expect_new) >= 0))
    return ok_len


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    src, dst = sys.argv[1], sys.argv[2]
    patch(src, dst)
    verify(dst, NEW)
