# -*- coding: utf-8 -*-
"""外服数据采集器：模拟真实客户端登录外部 HnG 服务器，
把登录后推送（及可选拉取）的全部 KVCS 数据落盘为可分析文件。

用途：账号迁移前期探查——先看清目标服务器登录时到底推哪些表、
每行有哪些字段，再决定全量同步（装备/勋带/解锁）的映射方案。

协议与 fake_teammate.py 完全同源（login2 digest 握手 + KVCS 订阅推送），
核心类 ForeignClient 后续供 web 端 /api/import 直接复用。

用法（在外服先小号验证，勿用主力号）：
  python tools/foreign_sync_probe.py --host 1.2.3.4 --port 15110 \
      --username MyUser --password MyPass
  python tools/foreign_sync_probe.py ... --password-env HNG_PW   # 密码走环境变量
  python tools/foreign_sync_probe.py ... --pull                  # 追发只读拉取请求
  python tools/foreign_sync_probe.py ... --duration 30           # 采集窗口（秒）

输出（--out 目录，默认 tmp/foreign_dump/<user>_<时间戳>/）：
  session.jsonl       全事件流：连接/登录/订阅/每个 KVCS 行（hex+递归解析字段）
  tables/<表名>.json  按表聚合：set 行字段 + delete 行 id
  responses/<标题>.json  --pull 拉取响应原样记录（含 ResponseReadCharacterStats
                      的勋带 XP/战斗 XP/单角色统计）
  summary.txt         人读汇总：表清单、行数、账号/角色/货币速览

已知字段速查（player / character 行，源自本服 protocol.msg_player）：
  player:    f2=gamertag f11=faction f14=gold f17=credits f24=warfunds
             f36=totalScore f38=level f37=unlockBits(repeated bool)
  character: f1=id f2=name f5=faction
"""
import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from protocol import Chunk, build_response, pb_parse_multi

# 与真实客户端一致的默认 cmd id（服务器按 title 分发、回显 cmdId）
CMD = {'login_begin': 1, 'login_response': 2, 'playerview': 3,
       'soldierview': 4, 'createchar': 5, 'joinmm': 6, 'cancelmm': 7}

# --pull 时追发的只读请求（均为空 proto；响应原样记录进 session.jsonl）
PULL_REQUESTS = (
    # 勋带 XP/战斗 XP/单角色全部统计（击杀/死亡/时长/武器击杀）——不在登录
    # KVCS 推送里，必须主动请求才拿得到；GetTotals 的账号汇总即由这些
    # 单角色统计求和重建，无需另连 statistics 协议
    'RequestReadCharacterStats',
    'GetMissionListRequest',
    'GetChatChannelsSubscribedRequest',
    'GetGoldPricesRequest',
    'QueryActiveSurveyRequest',
    'QueryVoucherPacksRequest',
    'QueryShopWarBondItemsRequest',
    'GetAssaultTeamStatsRequest',
)

MAX_FIELD_NUM = 512
MAX_DEPTH = 6


class LoginError(RuntimeError):
    """登录外服失败（code=结果码：97 之外均失败，98=密码错）。"""

    def __init__(self, code, msg=''):
        super().__init__(msg or '登录失败 code=%d' % code)
        self.code = int(code)


# ---------------------------------------------------------------- 递归解析
def _printable(s):
    return all(ord(c) >= 32 or c in '\t\n\r' for c in s)


def deep_parse(buf, depth=0):
    """尝试把 bytes 解析为 protobuf 消息 -> {字段号: [值,...]}，失败返回 None。

    值的表示：varint -> int（>2^63 时附 signed 解释）；
    定长/长度块 -> {'hex', 'str'?, 'u32'/'f32'?, 'u64'?, 'msg'?}，
    同一块的多种解释并存，供人工复核（启发式，不保证唯一正确）。
    """
    if depth > MAX_DEPTH or not buf:
        return None
    try:
        fields = pb_parse_multi(buf)
    except (ValueError, IndexError):
        return None
    if not fields or any(not 1 <= fn <= MAX_FIELD_NUM for fn in fields):
        return None
    out = {}
    for fn, vals in fields.items():
        arr = []
        for v in vals:
            if isinstance(v, int):
                item = v if v < (1 << 63) else {'u64': v, 'signed': v - (1 << 64)}
            else:
                item = {'hex': v.hex()}
                if len(v) == 4:
                    u = struct.unpack('<I', v)[0]
                    item['u32'] = u
                    item['f32'] = struct.unpack('<f', v)[0]
                elif len(v) == 8:
                    item['u64'] = struct.unpack('<Q', v)[0]
                try:
                    s = v.decode('utf-8')
                    if s and _printable(s):
                        item['str'] = s
                except UnicodeDecodeError:
                    pass
                sub = deep_parse(v, depth + 1)
                if sub is not None and len(sub) > 0:
                    item['msg'] = sub
            arr.append(item)
        out[str(fn)] = arr
    return out


def _iv(v):
    """字段值 -> int（protobuf int64 小端 bytes 兼容）。"""
    if isinstance(v, int):
        return v
    if isinstance(v, bytes) and v:
        return int.from_bytes(v, 'little', signed=True)
    return 0


# ---------------------------------------------------------------- 采集器
class ForeignClient:
    """模拟客户端连外部 HnG 服务器：login2 握手 + 订阅 + 全量事件采集。

    与 fake_teammate.Bot 的区别：不排队、不建角色；每个收到的 chunk
    （含全部 KVCS 表行）原样记录，供迁移映射分析。
    """

    def __init__(self, host, port, username, password, out_dir, log=print):
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.out_dir = out_dir
        self.log = log
        self.r = self.w = None
        self.buf = b''
        self._cmd = 10                      # 自定义请求从 10 起递增
        self.events = []                    # session.jsonl 全事件
        self.tables = {}                    # 表名 -> {'set': [行], 'delete': [id]}
        self.responses = {}                 # 响应 title -> [记录]（--pull 拉取结果）
        self.stats = {'chunks': 0, 'kvcs_rows': 0, 'other': 0}

    # ---- 事件记录 ----
    def _ts(self):
        return time.strftime('%H:%M:%S') + '.%03d' % (int(time.time() * 1000) % 1000)

    def record(self, kind, **kw):
        ev = {'ts': self._ts(), 'kind': kind}
        ev.update(kw)
        self.events.append(ev)

    # ---- 帧解析 ----
    async def _read_more(self):
        data = await self.r.read(65536)
        if not data:
            raise ConnectionError('服务器连接已断开')
        self.buf += data

    def _next_chunk(self):
        chunk, off = Chunk.parse(self.buf, 0)
        if chunk is not None:
            self.buf = self.buf[off:]
        return chunk

    async def read_until(self, want_title, timeout=30):
        deadline = time.monotonic() + timeout
        while True:
            chunk = self._next_chunk()
            if chunk is None:
                remain = deadline - time.monotonic()
                if remain <= 0:
                    raise TimeoutError('等待 %s 超时' % want_title)
                try:
                    await asyncio.wait_for(self._read_more(), timeout=remain)
                except asyncio.TimeoutError:
                    raise TimeoutError('等待 %s 超时' % want_title)
                continue
            title = chunk.subs[0].as_str() if chunk.subs else ''
            if title.lower() == want_title.lower():
                return chunk
            await self.on_chunk(chunk)

    async def send(self, cmd_id, title, proto=b''):
        self.w.write(build_response(cmd_id, title, proto))
        await self.w.drain()
        self.record('send', cmd=cmd_id, title=title, proto_bytes=len(proto))

    # ---- chunk 处理 ----
    async def on_chunk(self, chunk):
        self.stats['chunks'] += 1
        cmd = chunk.as_int()
        if not chunk.subs:
            self.record('chunk', cmd=cmd, note='no-sub')
            return
        title_chunk = chunk.subs[0]
        title = title_chunk.as_str()
        if title == 'KeyValueChangeSet':
            for action in title_chunk.subs:
                action_name = action.as_str() if action.data else '?'
                for table in action.subs:
                    tname = table.as_str()
                    rows = self.tables.setdefault(
                        tname, {'set': [], 'delete': []})
                    for row in table.subs:
                        data = row.data
                        rec = {'cmd': cmd, 'action': action_name,
                               'hex': data.hex(),
                               'fields': deep_parse(data)}
                        rows[action_name].append(rec)
                        self.stats['kvcs_rows'] += 1
                        self.record('kvcs', cmd=cmd, action=action_name,
                                    table=tname, hex=rec['hex'],
                                    fields=rec['fields'])
            return
        # 非 KVCS：响应/推送原样记录
        proto = title_chunk.subs[0].data if title_chunk.subs else b''
        rec = {'cmd': cmd, 'hex': proto.hex(),
               'fields': deep_parse(proto) if proto else None}
        self.responses.setdefault(title, []).append(rec)
        self.record('chunk', cmd=cmd, title=title, hex=rec['hex'],
                    fields=rec['fields'])
        self.stats['other'] += 1

    # ---- 流程 ----
    async def login(self):
        self.r, self.w = await asyncio.open_connection(self.host, self.port)
        self.log('已连接 %s:%d' % (self.host, self.port))
        self.record('event', msg='connected %s:%d' % (self.host, self.port))

        m = b''
        m += _pb_string(1, self.username)
        m += _pb_string(2, 'windows')
        m += _pb_bool(3, True)
        m += _pb_bool(4, True)
        await self.send(CMD['login_begin'], 'login2_begin', m)

        chunk = await self.read_until('login2_challenge')
        f = pb_parse_multi(chunk.subs[0].subs[0].data)
        salt = base64.b64decode(f[1][0])
        temp_sid = base64.b64decode(f[2][0])
        enc_key = base64.b64decode(f[3][0])
        pw_hash = hashlib.sha1(self.password.encode() + salt).digest()
        k1 = hashlib.sha1(pw_hash + temp_sid).digest()
        session_key = bytes(b ^ k1[i % len(k1)] for i, b in enumerate(enc_key))
        digest = hmac.new(session_key, temp_sid, hashlib.sha1).digest()

        m = _pb_string(1, f[2][0]) + _pb_string(
            2, base64.b64encode(digest).decode())
        await self.send(CMD['login_response'], 'login2_response', m)

        chunk = await self.read_until('login2_result')
        f = pb_parse_multi(chunk.subs[0].subs[0].data)
        self.record('login2_result', fields=deep_parse(chunk.subs[0].subs[0].data),
                    hex=chunk.subs[0].subs[0].data.hex())
        code = _iv(f.get(3, [0])[0])   # f3=result（97=成功 98=密码错）
        if code != 97:
            raise LoginError(code, '登录失败 code=%d（97=成功 98=密码错）' % code)
        self.log('登录成功')
        self.record('event', msg='login_success')

    async def subscribe(self):
        await self.send(CMD['playerview'], 'subscribeplayerview')
        await self.read_until('subscriberesponse')
        self.log('playerview 已订阅')
        await self.send(CMD['soldierview'], 'subscribesoldierview')
        await self.read_until('subscriberesponse')
        self.log('soldierview 已订阅')
        self.record('event', msg='subscribed')

    async def pull(self):
        """追发只读拉取请求（模拟客户端登录后的请求序列）。

        RequestReadCharacterStats 必须等 character 行到达后再发——
        外服按会话已同步的角色组织响应，发早了回空 proto。
        且真实客户端从不发空请求：characterids（f1，repeated uint64）
        必须带上角色 id（外服对空请求回空 body → 勋带/统计/战斗 XP
        全部采不到，2026-09-06 zcmk123 迁移勋带丢失事故）。
        """
        others = [t for t in PULL_REQUESTS if t != 'RequestReadCharacterStats']
        for title in others:
            self._cmd += 1
            await self.send(self._cmd, title)
            await asyncio.sleep(0.3)   # 温和节流，避免触发对方限速
        # 等 character 行（subscribesoldierview 推送）到达
        for _ in range(50):
            if self.tables.get('character', {}).get('set'):
                break
            await asyncio.sleep(0.2)
        else:
            self.log('等待 character 行超时，仍尝试请求统计')
        await asyncio.sleep(1)
        # 角色行 f1（id）→ 请求 characterids；缺失时仍发（本服忽略参数）
        char_ids = []
        for row in self.tables.get('character', {}).get('set') or []:
            for v in (row.get('fields') or {}).get('1') or []:
                if isinstance(v, int):
                    char_ids.append(v)
                elif isinstance(v, dict) and 'u64' in v:
                    char_ids.append(v['u64'])
        proto = b''
        for cid in char_ids:
            proto += _pb_varint(1 << 3 | 0) + _pb_varint(cid)
        proto += _pb_bool(2, False)     # ignoreSessionId=false，真实客户端同款
        self.log('RequestReadCharacterStats 带 %d 个角色 id' % len(char_ids))
        self._cmd += 1
        await self.send(self._cmd, 'RequestReadCharacterStats', proto=proto)

    async def collect(self, duration):
        self.log('采集窗口 %ds，保持连接接收推送…' % duration)
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            chunk = self._next_chunk()
            if chunk is None:
                remain = deadline - time.monotonic()
                if remain <= 0:
                    break
                try:
                    await asyncio.wait_for(self._read_more(),
                                           timeout=min(remain, 5))
                except asyncio.TimeoutError:
                    continue
                continue
            await self.on_chunk(chunk)

    # ---- 落盘 ----
    def dump(self):
        os.makedirs(self.out_dir, exist_ok=True)
        os.makedirs(os.path.join(self.out_dir, 'tables'), exist_ok=True)
        os.makedirs(os.path.join(self.out_dir, 'responses'), exist_ok=True)

        with open(os.path.join(self.out_dir, 'session.jsonl'), 'w',
                  encoding='utf-8') as fp:
            for ev in self.events:
                fp.write(json.dumps(ev, ensure_ascii=False) + '\n')

        for tname, rows in self.tables.items():
            path = os.path.join(self.out_dir, 'tables', tname + '.json')
            with open(path, 'w', encoding='utf-8') as fp:
                json.dump({'table': tname, 'set': rows['set'],
                           'delete': rows['delete']},
                          fp, ensure_ascii=False, indent=1)

        for title, recs in self.responses.items():
            safe = ''.join(c if c.isalnum() or c in '-_' else '_'
                           for c in title)[:80]
            path = os.path.join(self.out_dir, 'responses', safe + '.json')
            with open(path, 'w', encoding='utf-8') as fp:
                json.dump({'title': title, 'count': len(recs),
                           'responses': recs},
                          fp, ensure_ascii=False, indent=1)

        with open(os.path.join(self.out_dir, 'summary.txt'), 'w',
                  encoding='utf-8') as fp:
            fp.write(self._summary_text())
        return os.path.join(self.out_dir, 'summary.txt')

    def _summary_text(self):
        lines = []
        lines.append('=== 外服数据采集汇总 ===')
        lines.append('目标: %s:%d  用户: %s' % (self.host, self.port,
                                                self.username))
        lines.append('chunks=%(chunks)d kvcs_rows=%(kvcs_rows)d '
                     'other_chunks=%(other)d' % self.stats)
        lines.append('')
        lines.append('表统计:')
        for tname, rows in sorted(self.tables.items()):
            lines.append('  %-28s set=%-5d delete=%d'
                         % (tname, len(rows['set']), len(rows['delete'])))
        lines.append('')
        lines.append(self._account_brief())
        return '\n'.join(lines) + '\n'

    def _account_brief(self):
        """从 player / character 行提取账号速览（字段号见模块 docstring）。"""
        out = ['账号速览（启发式提取，详见 tables/*.json）:']
        for row in self.tables.get('player', {}).get('set', []):
            f = row['fields'] or {}
            def g(num, default=None):
                v = f.get(str(num))
                if not v:
                    return default
                v = v[0]
                return v.get('signed', v) if isinstance(v, dict) else v
            def gs(num, default=''):
                v = f.get(str(num))
                if v and isinstance(v[0], dict):
                    return v[0].get('str', default)
                return default
            out.append('  玩家: %s  faction=%s' % (gs(2), g(11)))
            out.append('  货币: gold=%s credits=%s warfunds=%s'
                       % (g(14), g(17), g(24)))
            out.append('  totalScore=%s level=%s'
                       % (g(36), g(38)))
        chars = []
        for row in self.tables.get('character', {}).get('set', []):
            f = row['fields'] or {}
            cid = f.get('1', [{}])[0]
            name = f.get('2', [{}])[0]
            fac = f.get('5', [{}])[0]
            def sv(v):
                return v.get('str') if isinstance(v, dict) else v
            chars.append('%s(faction=%s id=%s)' % (sv(name), sv(fac), sv(cid)))
        if chars:
            out.append('  角色(%d): %s' % (len(chars), ', '.join(chars)))
        if len(out) == 1:
            out.append('  （未收到 player/character 行——检查登录是否成功）')
        return '\n'.join(out)


def _pb_string(field, value):
    v = value.encode() if isinstance(value, str) else value
    out = bytearray()
    out += _pb_varint(field << 3 | 2)
    n = len(v)
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | 0x80 if n else b)
        if not n:
            break
    out += v
    return bytes(out)


def _pb_varint(value):
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        out.append(b | 0x80 if value else b)
        if not value:
            break
    return bytes(out)


def _pb_bool(field, value):
    return _pb_varint(field << 3 | 0) + _pb_varint(1 if value else 0)


async def run(args):
    out_dir = args.out
    if not out_dir:
        stamp = time.strftime('%Y%m%d_%H%M%S')
        base = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), 'tmp', 'foreign_dump')
        out_dir = os.path.join(base, '%s_%s' % (args.username, stamp))
    cli = ForeignClient(args.host, args.port, args.username,
                        args.password, out_dir)
    failed = None
    try:
        await cli.login()
        await cli.subscribe()
        if args.pull:
            await cli.pull()
        await cli.collect(args.duration)
    except Exception as e:
        failed = e
        cli.record('event', msg='error: %r' % e)
    finally:
        try:
            if cli.w:
                cli.w.close()
        except Exception:
            pass
    summary = cli.dump()
    print('\n' + open(summary, encoding='utf-8').read())
    print('完整数据: %s' % out_dir)
    if failed is not None:
        raise failed


def main():
    ap = argparse.ArgumentParser(
        description='外服 HnG 数据采集器（账号迁移前期探查）')
    ap.add_argument('--host', required=True, help='目标服务器地址')
    ap.add_argument('--port', type=int, default=15110,
                    help='登录端口（默认 15110）')
    ap.add_argument('--username', required=True)
    pw_group = ap.add_mutually_exclusive_group(required=True)
    pw_group.add_argument('--password', help='明文密码（建议改用 --password-env）')
    pw_group.add_argument('--password-env',
                          help='从该环境变量读密码（不进命令历史）')
    ap.add_argument('--out', default=None,
                    help='输出目录（默认 tmp/foreign_dump/<user>_<ts>/）')
    ap.add_argument('--duration', type=int, default=15,
                    help='采集窗口秒数（默认 15）')
    ap.add_argument('--pull', action='store_true',
                    help='订阅后追发只读拉取请求（GetMissionList 等 7 个）')
    args = ap.parse_args()

    if args.password_env:
        args.password = os.environ.get(args.password_env)
        if not args.password:
            print('环境变量 %s 未设置' % args.password_env, file=sys.stderr)
            sys.exit(1)

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print('\n已中断')
    except (ConnectionError, TimeoutError, RuntimeError, OSError) as e:
        print('失败: %s' % e, file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
