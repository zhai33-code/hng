# -*- coding: utf-8 -*-
"""假玩家机器人：以独立客户端身份连接私服，与真实玩家一起排队匹配。

不改任何服务器代码——完全走真实客户端协议：
  1. 账号+角色由 setup_bots.py 一次性预置（直接写库，无需服务器运行）
  2. TCP 127.0.0.1:15110 login2 握手（digest 算法同 selftest_client.py）
  3. subscribeplayerview（注册 Flash 会话，成局 KVCS 推送目标）
  4. subscribesoldierview 解析预置角色
  5. JoinMatchMakingRequest 入池（不选图 = 通配，与任何真人玩家选图兼容）
  6. 挂机收推送：PMS Admitted=成局、PMS delete=战局结束
     （--loop 时战局结束后自动重新排队）

用法（服务器须已运行；账号先经 setup_bots.py 预置）：
  python fake_teammate.py --username BotUS1              # 排队前按回车
  python fake_teammate.py --username BotGE1 --faction 2   # 德国阵营
  python fake_teammate.py --username BotUS1 --gamemode 3 --auto   # Skirmish, 直接排队
  python fake_teammate.py --username BotUS1 --loop        # 战局结束后自动重新排队

阵营 pack 映射（character_packs.py，rank 0 免费起步包）：
  1=US Infantry(pack 1)  2=GE Infantry(pack 21)  3=SU Infantry(pack 35)
"""
import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from protocol import Chunk, build_response, pb_string, pb_int64, pb_int32, pb_bool

HOST = '127.0.0.1'
GAME_PORT = 15110
HTTP_PORT = 80

FACTION_PACKS = {1: 1, 2: 21, 3: 35}     # faction -> CreateCharacterRequest packId
FACTION_NAMES = {1: 'US', 2: 'GE', 3: 'SU'}

# 与真实客户端一致的默认队列 cmd id（服务器只按 title 分发、回显 cmdId）
CMD = {'login_begin': 1, 'login_response': 2, 'playerview': 3,
       'soldierview': 4, 'createchar': 5, 'joinmm': 6, 'cancelmm': 7}


def parse_fields(buf):
    """轻量 protobuf 字段解析：{field_num: 值(int 或 bytes)}。"""
    fields = {}
    i, n = 0, len(buf)
    while i < n:
        # tag 本身是 varint：字段号 >= 16 时占 2 字节（如 f16=0x80 0x01），
        # 只读 1 字节会从 f16 起失步 → f17-f21（含 f20 gameConnect）全丢
        tag, shift = 0, 0
        while i < n:
            b = buf[i]; i += 1
            tag |= (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                break
        fn, wt = tag >> 3, tag & 7
        if wt == 0:                        # varint
            val, shift = 0, 0
            while i < n:
                b = buf[i]; i += 1
                val |= (b & 0x7F) << shift
                shift += 7
                if not b & 0x80:
                    break
            fields[fn] = val
        elif wt == 2:                      # length-delimited
            ln, shift = 0, 0
            while i < n:
                b = buf[i]; i += 1
                ln |= (b & 0x7F) << shift
                shift += 7
                if not b & 0x80:
                    break
            fields[fn] = buf[i:i + ln]; i += ln
        elif wt == 5:                      # 32-bit
            fields[fn] = buf[i:i + 4]; i += 4
        elif wt == 1:                      # 64-bit
            fields[fn] = buf[i:i + 8]; i += 8
        else:
            break                          # 未知 wire type：放弃剩余
    return fields


def _iv(v):
    """字段值 -> int（protobuf int64 小端 bytes 兼容）。"""
    if isinstance(v, int):
        return v
    if isinstance(v, bytes) and v:
        return int.from_bytes(v, 'little', signed=True)
    return 0


class Bot:
    def __init__(self, args):
        self.args = args
        self.r = self.w = None
        self.buf = b''
        self.chars = {}          # char_id -> {'faction': f, 'name': str}
        self.in_battle = False

    def log(self, msg):
        print('[bot %s] %s' % (self.args.username, msg), flush=True)

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

    async def read_until(self, want_title=None, timeout=30):
        """读到目标 title 的 chunk；途中其他 chunk 交给 on_chunk。"""
        import time as _t
        deadline = _t.monotonic() + timeout
        while True:
            chunk = self._next_chunk()
            if chunk is None:
                remain = deadline - _t.monotonic()
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

    async def on_chunk(self, chunk):
        """后台 chunk 处理：解析 KVCS，维护角色表 / 检测战局状态。"""
        title = chunk.subs[0].as_str() if chunk.subs else ''
        if title != 'KeyValueChangeSet':
            return
        main = chunk.subs[0]
        if not main.subs:
            return
        action = main.subs[0]
        if not action.subs:
            return
        table = action.subs[0]
        table_name = table.as_str()
        if action.as_str() == 'set':
            for row in table.subs:
                f = parse_fields(row.data)
                if table_name == 'character':
                    cid = _iv(f.get(1))
                    if cid:
                        name = f.get(2)
                        self.chars[cid] = {
                            'faction': _iv(f.get(5)),
                            'name': name.decode('utf-8', 'replace')
                                    if isinstance(name, bytes) else str(name)}
                        self.log('收到角色行 id=%s name=%s faction=%s' % (
                            cid, self.chars[cid]['name'],
                            FACTION_NAMES.get(self.chars[cid]['faction'], '?')))
                elif table_name == 'PlayerMissionSession':
                    f14 = _iv(f.get(14))
                    if f14 == 2:
                        self.in_battle = True
                        if self.args.launch_client:
                            self._launch_game_client(f)   # 真·第二玩家进战斗服
                        else:
                            self.log('★ 已成局（PMS Admitted）——真人玩家将进入'
                                     '同一战局（机器人不连战斗服，等同挂机玩家）')
                    elif f14 == 3:
                        self.log('战局 Armed（PMS f14=3）')
        elif action.as_str() == 'delete':
            if table_name == 'PlayerMissionSession':
                self.in_battle = False
                self.log('战局已结束（PMS delete）'
                         + ('，3 秒后自动重新排队' if self.args.loop else
                            '（--loop 可自动重排）'))

    async def send(self, cmd_id, title, proto=b''):
        self.w.write(build_response(cmd_id, title, proto))
        await self.w.drain()

    # ---- 真实客户端拉起（--launch-client） ----
    def _launch_game_client(self, f):
        """PMS Admitted 后拉起真实 hng.exe 客户端连入战斗服（真·第二玩家）。

        命令行模板取自本机抓包实录（hng_boot.log COMMANDLINE）：
        /M mission_id /P pms_id /W warserver /R actionhost=<gameConnect> …
        刻意不带 /x LaunchedByHngSync（共享内存互斥会与正在运行的实例冲突）。
        """
        pms_id = _iv(f.get(1))
        mission_id = _iv(f.get(8))
        gc = f.get(20)
        game_connect = gc.decode('utf-8', 'replace') if isinstance(gc, bytes) else str(gc or '')
        if not pms_id or not game_connect:
            self.log('PMS 缺少 pms_id/gameConnect，放弃拉起客户端')
            return
        exe = self.args.client_exe
        if not os.path.isfile(exe):
            self.log('未找到客户端可执行文件 %s（--client-exe 可指定）' % exe)
            return
        cmd = [exe,
               '/M', str(mission_id),            # scenefile（loadfromserver=1）
               '/P', str(pms_id),                # MissionSession ID
               '/W', '%s:%d' % (HOST, GAME_PORT),  # warserver 入口
               '/lang', 'en',
               '/NOSERVER',
               '/H', '0',
               '/PS', '1',
               '/V', '173293',
               '/R', 'sync.delayShow=1',
               '/R', 'actionhost=%s' % game_connect,   # 战斗服 UDP 地址
               '/R', 'useDX12=0',
               '/R', 'render.enable_pbr=0']
        try:
            subprocess.Popen(cmd, cwd=os.path.dirname(exe))
        except OSError as e:
            if getattr(e, 'winerror', None) != 740:
                raise
            # WinError 740：hng.exe 清单要求管理员权限，CreateProcess 拒绝；
            # 改走 ShellExecuteW（verb=runas）——会弹一次 UAC 确认框
            import ctypes
            params = ' '.join(
                ('"%s"' % a) if (' ' in a and not a.startswith('/')) else a
                for a in cmd[1:])
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, 'runas', exe, params, os.path.dirname(exe), 1)
            if rc <= 32:
                self.log('ShellExecuteW 失败 code=%s，放弃拉起客户端' % rc)
                return
        self.log('★ 已拉起真实客户端 hng.exe（pms=%s mission=%s actionhost=%s）'
                 % (pms_id, mission_id, game_connect))

    # ---- 流程 ----
    async def login(self):
        self.r, self.w = await asyncio.open_connection(HOST, GAME_PORT)
        self.log('已连接 %s:%d' % (HOST, GAME_PORT))

        # 1. login2_begin
        m = pb_string(1, self.args.username)
        m += pb_string(2, 'windows-bot')
        m += pb_bool(3, True)
        m += pb_bool(4, True)
        await self.send(CMD['login_begin'], 'login2_begin', m)

        # 2. challenge
        chunk = await self.read_until('login2_challenge')
        f = parse_fields(chunk.subs[0].subs[0].data)
        salt = base64.b64decode(f[1])
        temp_sid = base64.b64decode(f[2])
        enc_key = base64.b64decode(f[3])
        pw_hash = hashlib.sha1(self.args.password.encode() + salt).digest()
        k1 = hashlib.sha1(pw_hash + temp_sid).digest()
        session_key = bytes(b ^ k1[i % len(k1)] for i, b in enumerate(enc_key))
        digest = hmac.new(session_key, temp_sid, hashlib.sha1).digest()

        # 3. login2_response（f1 = base64 的 tempSessionid 原串）
        m = pb_string(1, f[2]) + pb_string(2, base64.b64encode(digest).decode())
        await self.send(CMD['login_response'], 'login2_response', m)

        # 4. result
        chunk = await self.read_until('login2_result')
        f = parse_fields(chunk.subs[0].subs[0].data)
        code = _iv(f.get(3))   # login2_result f3=result（97=成功）
        if code != 97:
            raise RuntimeError('登录失败 code=%d（97=成功）' % code)
        self.log('登录成功')

    async def subscribe(self):
        await self.send(CMD['playerview'], 'subscribeplayerview')
        await self.read_until('subscriberesponse')
        self.log('playerview 已订阅（Flash 会话已注册）')

        await self.send(CMD['soldierview'], 'subscribesoldierview')
        await self.read_until('subscriberesponse')
        self.log('soldierview 已订阅（现有 %d 个角色）' % len(self.chars))

    def _pick_char(self):
        for cid, info in self.chars.items():
            if info['faction'] == self.args.faction:
                return cid
        return None

    async def ensure_character(self):
        """选取预置账号中指定阵营的角色（账号+角色由 setup_bots.py 预置）。"""
        for _ in range(25):        # 等待 soldierview 角色行推送到达
            cid = self._pick_char()
            if cid:
                self.log('使用角色 %s (id=%s)' % (self.chars[cid]['name'], cid))
                return cid
            await asyncio.sleep(0.2)
        raise RuntimeError('账号无 %s 阵营角色——先运行 setup_bots.py 预置'
                           % FACTION_NAMES.get(self.args.faction, '?'))

    async def join_matchmaking(self, char_id):
        m = pb_int64(1, char_id)          # charId
        m += pb_int64(2, 0)               # missionId（不指定战局）
        m += pb_bool(4, False)            # warMissionFlag
        m += pb_int32(7, self.args.gamemode)
        m += pb_bool(8, False)            # quickplay
        # f5 stagedMissionPresetList 省略 = 不限图（与任何真人选图兼容）
        await self.send(CMD['joinmm'], 'JoinMatchMakingRequest', m)
        chunk = await self.read_until('JoinMatchMakingResponse')
        f = parse_fields(chunk.subs[0].subs[0].data)
        if _iv(f.get(1)) != 1:
            raise RuntimeError('JoinMatchMaking 被拒 result=%d' % _iv(f.get(1)))
        self.log('已进入匹配队列（gamemode=%d）' % self.args.gamemode)

    async def run(self):
        await self.login()
        await self.subscribe()
        char_id = await self.ensure_character()

        if not self.args.auto:
            print(flush=True)
            self.log('准备就绪。现在在游戏里点 To Battle 开始匹配，然后按回车'
                     '让机器人排队（30 秒内双方会汇合成局）。')
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, input)

        if self.args.setup_only:
            self.log('预置完成（账号+角色已就绪），退出。以后直接运行即可排队。')
            return

        await self.join_matchmaking(char_id)
        self.log('排队中，保持连接挂机（Ctrl+C 退出）…')

        # 挂机：持续消费服务器推送；--loop 时战局结束后重新排队
        was_in_battle = False
        while True:
            chunk = self._next_chunk()
            if chunk is None:
                await self._read_more()
                continue
            await self.on_chunk(chunk)
            if self.args.loop and was_in_battle and not self.in_battle:
                was_in_battle = False
                await asyncio.sleep(3)
                cid = self._pick_char() or await self.ensure_character()
                await self.join_matchmaking(cid)
            was_in_battle = self.in_battle


def main():
    ap = argparse.ArgumentParser(description='HnG 假玩家机器人（与真人一起匹配）')
    ap.add_argument('--username', default='BotPlayer1')
    ap.add_argument('--password', default='botbot123')
    ap.add_argument('--gamemode', type=int, default=11,
                    help='gamemodeId：11=Quick Play, 3=Skirmish(C&H Inf), '
                         '5/6=Assault, 10=War（默认 11）')
    ap.add_argument('--faction', type=int, default=1, choices=(1, 2, 3),
                    help='机器人阵营 1=US 2=GE 3=SU（默认 1；与真人同阵营=队友）')
    ap.add_argument('--auto', action='store_true', help='跳过回车确认，直接排队')
    ap.add_argument('--loop', action='store_true', help='战局结束后自动重新排队')
    ap.add_argument('--setup-only', action='store_true',
                    help='仅预置账号+角色后退出（一次性操作，之后运行无需再建）')
    ap.add_argument('--launch-client', action='store_true',
                    help='成局后拉起真实 hng.exe 客户端连入战斗服（真·第二玩家）')
    ap.add_argument('--client-exe', default=r'd:\HnG-test\hng.exe',
                    help='客户端 hng.exe 路径（--launch-client 用）')
    args = ap.parse_args()

    bot = Bot(args)
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print('\n[bot %s] 已退出' % args.username)
    except (ConnectionError, TimeoutError, RuntimeError) as e:
        print('[bot %s] 失败: %s' % (args.username, e), file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()





