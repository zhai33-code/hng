# -*- coding: utf-8 -*-
"""IRC 聊天服务端（asyncio 明文 6667，阶段 D）。

对齐官方 InspIRCd2 定制行为——协议子集、消息格式、numeric 序列均照
irc-official-transcript.txt（2026-09 实测抓包）复刻，结论见
.trae/documents/irc-chat-service-research-and-implementation.md 二·五章节。

身份贯通（R19）：login2_postlogin 分配唯一 sid 并经 register_session()
登记（player_id / username / session_key）；IRC PASS 携带
`<sid>::<Base64(HMAC-SHA1(session_key, sid+"IRC"))>`，校验通过后服务器
权威改名 `<username>`（占用时 `<username>%<1..255>`，对应抓包 %N 语义）。

启动：realtest_server.main 经 _supervise('IRC', irc_server.start) 拉起。

关键行为（均对应抓包实测）：
- 注册三要素任意顺序；行首 `:prefix ` 前缀剥离（ULIST/JOIN 客户端怪癖）
- 强制改名不发 433：直接 `:req NICK <权威名>`（43096 流路径）
- JOIN 回显 `:nick JOIN :#chan`（频道带冒号）+ 353/366；PART 无冒号
- PRIVMSG 回显给发送者（SWF privateMsgHandler 以 Parameters[0]==自己
  rawNick 识别"我说的话"，私聊回显目标=发送者 nick）
- ULIST/LIST：321/322/323，322 trailing = `[+modes 6:8] `（末尾空格照抄）
- 20 个官方频道常驻（GlobalChat* 10 语言 + PlayersHelp* 10 语言）
- `#<数字>` 频道首 JOIN 自动建（原生 MD5 混淆频道 / squad 频道）
- WHOIS 回 378（`is connecting from <user>@<ip> <ip>`，SWP 截取 user@*
  做 GLINE 目标）；KICK/GLINE 无 mod 体系回 481；381（moderator）不发
"""

import asyncio
import base64
import hashlib
import hmac
import random
import secrets
import string
import time

try:
    from state import log
except ImportError:          # 独立跑（python irc_server.py）时无游戏服上下文
    log = print


SERVER_NAME = 'irc.hng-group.com'
SERVER_VERSION = 'inspircd2'
LISTEN_PORT = 6667
MAX_CHANNELS_PER_CLIENT = 30          # 005 MAXCHANNELS=30
MAX_LINE = 512                        # IRC 标准（含 CRLF）
REGISTRATION_TIMEOUT = 60             # 秒，PASS/NICK/USER 未齐断开
UID_ALPHABET = string.ascii_uppercase + string.digits

# 20 个官方常驻频道（GlobalChat* + PlayersHelp* × 10 语言，R16）。
# modes 位对齐抓包：GlobalChatEN 带 +D（默认频道），其余 +fnt。
OFFICIAL_CHANNELS = []
for _lang in ('EN', 'RU', 'DE', 'ES', 'FR', 'PL', 'CZ', 'TR', 'PT', 'ZH'):
    _modes = '+Dfnt' if _lang == 'EN' else '+fnt'
    OFFICIAL_CHANNELS.append((f'#GlobalChat{_lang}', _modes))
for _lang in ('EN', 'RU', 'DE', 'ES', 'FR', 'PL', 'CZ', 'TR', 'PT', 'ZH'):
    OFFICIAL_CHANNELS.append((f'#PlayersHelp{_lang}', '+fnt'))

# MOTD 照抄抓包 11 行（375/372×11/376）
MOTD = [
    '**                 HEROES AND GENERALS                **',
    '**                                                       **',
    '**  This is a private IRC server, intended for use with  **',
    '**  the Heroes and Generals game only.                   **',
    '**                                                       **',
    '**  Operators:                                           **',
    '**   - [RETO] G Lands  (gl@reto.dk)                      **',
    '**   - [RETO] M Lund   (ml@reto.dk)                      **',
    '**                                                       **',
    '**  We reserve the right to bar anyone from using this   **',
    '**  server.                                              **',
]


# ---------------------------------------------------------------- 会话注册表
# sid(int64) -> {'player_id', 'username', 'session_key', 'ts'}
# auth.py login2_postlogin 登记；logout / 游戏连接断开注销。
SESSIONS = {}


def register_session(sid, player_id, username, session_key):
    """登记一次已认证登录（auth.py login2_postlogin 调用）。"""
    SESSIONS[sid] = {
        'player_id': int(player_id),
        'username': username,
        'session_key': session_key,
        'ts': time.time(),
    }
    log(f'[IRC] session registered sid={sid} user={username!r} pid={player_id}')


def unregister_session(sid):
    """注销登录会话（logout / 游戏连接断开）。"""
    ent = SESSIONS.pop(sid, None)
    if ent is not None:
        log(f'[IRC] session unregistered sid={sid} user={ent["username"]!r}')
        # 该 sid 名下已注册的 IRC 连接一并断开（官方 logout 后聊天随断）
        for cli in list(CLIENTS):
            if cli.sid == sid:
                cli.kick('logged out')


def _gen_uid():
    """InspIRCd uuid 形如 464AAR6LU（3 数字 + 6 base36 大写）。"""
    return ''.join(random.choices(string.digits, k=3)) + \
           ''.join(random.choices(UID_ALPHABET, k=6))


def _sanitize_nick(name):
    """玩家名 → IRC 安全昵称（剔控制/IRC 特殊字符，压到 30 字符）。"""
    bad = set('!@:,*?. \t\r\n\0')
    out = ''.join('_' if c in bad else c for c in name)
    return out[:30] or 'player'


# ---------------------------------------------------------------- 频道 / 客户端
class Channel:
    def __init__(self, name):
        self.name = name          # 规范显示名（官方表或首建写法）
        self.key = name.lower()
        self.members = set()      # IrcClient


CHANNELS = {}                     # lower(name) -> Channel
CLIENTS = set()                   # 已完成注册的 IrcClient


def _channel(name, create=True):
    ch = CHANNELS.get(name.lower())
    if ch is None and create:
        ch = Channel(name)
        CHANNELS[ch.key] = ch
    return ch


def get_channel_counts():
    """ULIST 视角：官方 20 频道恒列出 + 其余活跃频道（实时人数）。"""
    rows = []
    official = {n.lower() for n, _ in OFFICIAL_CHANNELS}
    for name, modes in OFFICIAL_CHANNELS:
        ch = CHANNELS.get(name.lower())
        rows.append((name, len(ch.members) if ch else 0, modes))
    for ch in CHANNELS.values():
        if ch.key not in official:
            rows.append((ch.name, len(ch.members), '+fnt'))
    return rows


class IrcClient:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        peer = writer.get_extra_info('peername')
        self.ip = peer[0] if peer else '0.0.0.0'
        # 注册三要素
        self.pass_arg = None       # 原始 PASS 参数
        self.req_nick = None       # 客户端请求 nick
        self.user = None           # USER 参数 1（SWF: <playerId>，原生: c<playerId>）
        # PASS 校验成功后（注册完成时生效）
        self.sid = None
        self.player_id = None
        self.username = None
        # 注册结果
        self.nick = None           # 权威昵称
        self.uid = _gen_uid()
        self.registered = False

    # ---------------- 发送原语
    async def send_line(self, line):
        try:
            self.writer.write(line.encode('utf-8', 'surrogateescape') + b'\r\n')
            await self.writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass

    async def send(self, prefix, rest):
        """prefix 为空发无前缀行；否则 ':prefix rest'。"""
        if prefix:
            await self.send_line(f':{prefix} {rest}')
        else:
            await self.send_line(rest)

    def kick(self, reason):
        """同步断开（unregister_session 等同步上下文调用）：写入即关，
        由事件循环 flush 后关闭连接。"""
        try:
            self.writer.write(f'ERROR :Closing link: '
                              f'({self.nick or "?"})[{reason}]'.encode() + b'\r\n')
        except Exception:
            pass
        self.writer.close()

    # ---------------- 完整消息
    @property
    def hostmask(self):
        return f'{self.nick}!{self.user}@{self.ip}'

    async def numeric(self, code, text):
        await self.send(SERVER_NAME, f'{code} {self.nick or "*"} {text}')

    async def notice_auth(self, text):
        await self.send(SERVER_NAME, f'NOTICE Auth :{text}')

    async def notice_uid(self, text):
        await self.send(SERVER_NAME, f'NOTICE {self.uid} :{text}')

    async def motd(self):
        await self.numeric('375', f':{SERVER_NAME} Message of the day')
        for line in MOTD:
            await self.numeric('372', f':{line}')
        await self.numeric('376', ':End of message of the day.')

    async def send_lusers(self):
        n = len(CLIENTS)
        await self.numeric('251', f':There are {n} users and 0 invisible on 1 servers')
        await self.numeric('253', '0 :unknown connections')
        await self.numeric('254', f'{len(CHANNELS)} :channels formed')
        await self.numeric('255', f':I have {n} clients and 1 servers')
        await self.numeric('265', f'{n} {n} :current local users {n} max {n}')
        await self.numeric('266', f'{n} {n} :current global users {n} max {n}')

    async def send_isupport(self):
        await self.numeric('005', 'NAMESX SAFELIST ELIST=cmntU CHANTYPES=# '
                            'PREFIX=(oUEv)@&%+ CHANMODES=beI,k,RNjl,DUfnt,PL '
                            ':are supported by this server')
        await self.numeric('005', f'MAXCHANNELS={MAX_CHANNELS_PER_CLIENT} '
                            'CHANLIMIT=#:90 NICKLEN=64 CHANNELLEN=64 TOPICLEN=307 '
                            'AWAYLEN=200 KICKLEN=255 MAXLIST=beI:100 '
                            ':are supported by this server')

    async def finish_registration(self):
        """PASS 校验通过且 NICK/USER 齐 → 权威改名 + 官方 numeric 序列（R9）。"""
        self.registered = True
        CLIENTS.add(self)
        auth_nick = self._assign_nick()
        # 强制改名（抓包 43096 路径：直接改权威名，不发 433）
        if auth_nick != self.req_nick:
            await self.send(self.req_nick, f'NICK {auth_nick}')
        self.nick = auth_nick
        await self.notice_auth('Welcome to HeroesAndGenerals!')
        await self.numeric('001', f':Welcome to the HeroesAndGenerals IRC Network '
                            f'{self.hostmask}')
        # 002 版本串按原生客户端解析格式（%[^ [,], running version %s）
        await self.numeric('002', f':Your host is {SERVER_NAME}, '
                            f'running version {SERVER_VERSION}')
        await self.numeric('003', ':This server was created 19:33:16 Jul  2 2026')
        await self.numeric('004', f'{SERVER_NAME} {SERVER_VERSION} ioswx '
                            'DEPUVYbfiklmnopstv EUVYbfklov')
        await self.send_isupport()
        await self.numeric('042', f'{self.uid} :your unique ID')
        await self.motd()
        await self.send_lusers()
        await self.numeric('396', f'{self.ip}.IP :is now your displayed host')
        await self.send(self.nick, f'MODE {self.nick} +x')
        log(f'[IRC] registered {self.nick}!{self.user}@{self.ip} '
            f'(pid={self.player_id}, sid={self.sid})')

    def _assign_nick(self):
        """权威昵称：MungeNick(username)；被占（同玩家旧连接/他人）→ %<1..255>。"""
        base = _sanitize_nick(self.username or 'player')
        if not any(c.nick and c.nick.lower() == base.lower() for c in CLIENTS):
            return base
        for _ in range(64):
            cand = f'{base}%{random.randint(1, 255)}'
            if not any(c.nick and c.nick.lower() == cand.lower() for c in CLIENTS):
                return cand
        return f'{base}%{random.randint(1, 255)}'

    # ---------------- 入站命令
    async def handle_line(self, line):
        # 剥客户端怪癖：行首 NUL 帧 / `:prefix ` 自指前缀（ULIST、JOIN 实测）
        line = line.lstrip('\x00').lstrip(' ')
        if line.startswith(':'):
            _prefix, _, line = line.partition(' ')
            line = line.lstrip(' ')
        if not line:
            return
        parts = line.split(' ')
        cmd = parts[0].upper()
        trailing = None
        if ' :' in line:
            head, _, trailing = line.partition(' :')
            params = head.split(' ')[1:]
            if trailing is not None:
                params.append(trailing)
        else:
            params = parts[1:]
        params = [p for p in params if p != '']

        if cmd == 'PASS':
            self.pass_arg = trailing if trailing is not None else (params[0] if params else '')
        elif cmd == 'NICK':
            if not self.registered:
                self.req_nick = (trailing if trailing is not None
                                 else (params[0] if params else None))
            # 已注册的 NICK 重试（客户端 433 处理器）：忽略——服务器从不发 433
        elif cmd == 'USER':
            if not self.registered:
                self.user = params[0] if params else 'anon'
        elif cmd == 'QUIT':
            await self.quit()
        elif not self.registered:
            # 未注册只认 CAP（忽略）与 PING
            if cmd == 'PING':
                await self.send(None, f'PONG :{trailing or params[0] if params else SERVER_NAME}')
            elif cmd != 'CAP':
                await self.numeric('451', ':You have not registered')
            return
        else:
            await self.dispatch_command(cmd, params, trailing)
        if (not self.registered and self.req_nick and self.user):
            await self.try_complete_registration()

    async def try_complete_registration(self):
        if self.registered:
            return
        # PASS 必须携带 sid::hmac 且校验通过（R19）
        arg = self.pass_arg or ''
        sid_str, sep, hmac_b64 = arg.partition('::')
        entry = None
        if sep and sid_str.isdigit():
            try:
                entry = SESSIONS.get(int(sid_str))
            except ValueError:
                entry = None
        ok = False
        if entry is not None:
            expected = base64.b64encode(
                hmac.new(entry['session_key'], f'{sid_str}IRC'.encode(),
                         hashlib.sha1).digest()).decode()
            ok = hmac.compare_digest(hmac_b64, expected)
        if not ok:
            await self.send(None, 'ERROR :Closing link: (unauthorized)')
            self.writer.close()
            log(f'[IRC] PASS rejected from {self.ip} (sid={sid_str!r})')
            return
        self.sid = int(sid_str)
        self.player_id = entry['player_id']
        self.username = entry['username']
        await self.finish_registration()

    async def dispatch_command(self, cmd, params, trailing):
        handler = getattr(self, f'_cmd_{cmd.lower()}', None)
        if handler is None:
            await self.numeric('421', f'{cmd} :Unknown command')
            return
        await handler(params, trailing)

    # JOIN：参数可能带前导 ':'（批量 trailing 形式 `JOIN :#a,#b`）
    async def _cmd_join(self, params, trailing):
        joined_names = ','.join(params)
        if trailing and not joined_names:
            joined_names = trailing
        wanted = [c for c in joined_names.split(',') if c]
        if not wanted:
            await self.numeric('461', 'JOIN :Not enough parameters')
            return
        mine = sum(1 for ch in CHANNELS.values() if self in ch.members)
        for chan_name in wanted:
            if not chan_name.startswith('#'):
                chan_name = '#' + chan_name
            if len(chan_name) > 64:
                await self.numeric('403', f'{chan_name} :No such channel')
                continue
            ch = _channel(chan_name)
            if self in ch.members:
                continue
            if mine >= MAX_CHANNELS_PER_CLIENT:
                await self.numeric('405', f'{ch.name} :You have joined too many channels')
                continue
            mine += 1
            ch.members.add(self)
            # 回显 + 广播（频道带冒号，照抄抓包）
            for member in list(ch.members):
                await member.send(self.nick, f'JOIN :{ch.name}')
            await self._send_names(ch)

    async def _send_names(self, ch):
        nicks = [m.nick for m in ch.members]
        # 分行（每行 ≤ ~480 字节）
        batch, size = [], 0
        lines = [[]]
        for nk in nicks:
            if size + len(nk) + 1 > 460:
                lines.append([])
                size = 0
            lines[-1].append(nk)
            size += len(nk) + 1
        for row in lines:
            await self.numeric('353', f'= {ch.name} :{" ".join(row)}')
        await self.numeric('366', f'{ch.name} :End of /NAMES list.')

    async def _cmd_part(self, params, trailing):
        wanted = ','.join(params).split(',')
        for chan_name in [c for c in wanted if c]:
            if not chan_name.startswith('#'):
                chan_name = '#' + chan_name
            ch = CHANNELS.get(chan_name.lower())
            if ch is None or self not in ch.members:
                await self.numeric('442', f'{chan_name} :You\'re not on that channel')
                continue
            ch.members.discard(self)
            for member in list(ch.members) + [self]:
                await member.send(self.nick, f'PART {ch.name}')
            if not ch.members and ch.key not in {n.lower() for n, _ in OFFICIAL_CHANNELS}:
                del CHANNELS[ch.key]

    async def _cmd_privmsg(self, params, trailing):
        if not params or trailing is None:
            await self.numeric('412', ':No text to send')
            return
        target, text = params[0], trailing
        if target.startswith('#'):
            ch = CHANNELS.get(target.lower())
            if ch is None or self not in ch.members:
                await self.numeric('404', f'{target} :Cannot send to channel')
                return
            # 不回显给发送者：SWF IRCSocket.IRCSend 发送后本地回环
            # parseString 已自行显示（IRCSocket.as:508），官方服务器同样
            # 不回显，回显会导致客户端一条消息显示两次
            for member in list(ch.members):
                if member is not self:
                    await member.send(self.nick, f'PRIVMSG {ch.name} :{text}')
        else:
            dest = self._find_client(target)
            if dest is None:
                await self.numeric('401', f'{target} :No such nick/channel')
                return
            # 私聊同样不回显给发送者（SWF 本地回环走 onPrivMsg，
            # RawNickName==param4 识别即为此设计）
            await dest.send(self.nick, f'PRIVMSG {dest.nick} :{text}')

    def _find_client(self, nick):
        n = nick.lower()
        for c in CLIENTS:
            if c.nick and c.nick.lower() == n:
                return c
        # %N 变体：按剥离 % 后缀匹配（玩家名唯一 → 最多一个基名）
        base = n.split('%')[0]
        for c in CLIENTS:
            if c.nick and c.nick.lower().split('%')[0] == base:
                return c
        return None

    async def _cmd_ping(self, params, trailing):
        await self.send(None, f'PONG :{trailing or (params[0] if params else SERVER_NAME)}')

    async def _cmd_pong(self, params, trailing):
        pass

    # ULIST = LIST 别名（R11）：321/322/323，modes 位照抄
    async def _cmd_ulist(self, params, trailing):
        await self._cmd_list(params, trailing)

    async def _cmd_list(self, params, trailing):
        await self.numeric('321', 'Channel :Users Name')
        for name, count, modes in get_channel_counts():
            # 末尾空格照抄官方（`[+fnt 6:8] `）
            await self.numeric('322', f'{name} {count} :[{modes} 6:8] ')
        await self.numeric('323', ':End of channel list.')

    async def _cmd_mode(self, params, trailing):
        if not params:
            return
        target = params[0]
        if target.lower() == self.nick.lower():
            return                  # 自指 MODE：客户端不期待应答
        # 频道 MODE（含 joinFactionChannel 的 `MODE #chan +V <templateId>`
        # 阵营验证命令）：v1 静默接受（无 mod/权限体系）
        log(f'[IRC] MODE {params} from {self.nick} (accepted, no-op)')

    async def _cmd_whois(self, params, trailing):
        target = (trailing or (params[-1] if params else '')).split(' ')[0]
        dest = self._find_client(target) if target else None
        if dest is None:
            await self.numeric('401', f'{target} :No such nick/channel')
            return
        await self.numeric('311', f'{dest.nick} {dest.user} {dest.ip} * :{dest.nick}')
        # 378 第 4 空格分片须为 <user>@<ip>（SWF 截取 user@* 作 GLINE 目标）
        await self.numeric('378', f'{dest.nick} :is connecting from '
                            f'{dest.user}@{dest.ip} {dest.ip}')
        await self.numeric('318', f'{dest.nick} :End of /WHOIS list.')

    async def _cmd_gline(self, params, trailing):
        # 无 mod 体系（381 从不发送）→ 481；真实封禁走管理端 DB + ban 监听
        await self.numeric('481', ':Permission Denied - You do not have the '
                            'required operator privileges')
        log(f'[IRC] GLINE attempt from {self.nick}: {params} {trailing or ""}')

    async def _cmd_kick(self, params, trailing):
        await self.numeric('481', ':Permission Denied - You do not have the '
                            'required operator privileges')

    async def _cmd_topic(self, params, trailing):
        pass

    async def _cmd_names(self, params, trailing):
        if params:
            ch = CHANNELS.get(params[0].lstrip(':').lower())
            if ch:
                await self._send_names(ch)

    async def quit(self):
        if self.registered:
            for ch in list(CHANNELS.values()):
                if self in ch.members:
                    ch.members.discard(self)
                    for member in list(ch.members):
                        await member.send(self.nick, 'QUIT :Connection closed')
        try:
            await self.send(None, f'ERROR :Closing link: ({self.nick})[Quit]')
        except Exception:
            pass
        self.writer.close()

    def cleanup(self):
        CLIENTS.discard(self)
        for ch in CHANNELS.values():
            ch.members.discard(self)
        # QUIT 广播由 disconnect 处理路径完成（handle_conn finally）


async def handle_conn(reader, writer):
    cli = IrcClient(reader, writer)
    peer = writer.get_extra_info('peername')
    log(f'[IRC] CONNECT from {peer}')
    try:
        # 抓包序列前两条（对齐官方，客户端不依赖但无害）
        await cli.notice_auth('*** Looking up your hostname...')
        await cli.notice_uid('*** Skipping host resolution '
                             '(disabled by server administrator)')
        buf = b''
        while True:
            if not cli.registered:
                # 注册超时（PASS/NICK/USER 未齐）：断开防泄漏
                data = await asyncio.wait_for(reader.read(4096),
                                              timeout=REGISTRATION_TIMEOUT)
            else:
                data = await reader.read(4096)
            if not data:
                break
            buf += data
            while True:
                nl = buf.find(b'\n')
                if nl < 0:
                    if len(buf) > 8192:      # 无换行的垃圾流防护
                        raise ConnectionError('line overflow')
                    break
                raw = buf[:nl]
                buf = buf[nl + 1:]
                line = raw.rstrip(b'\r').decode('utf-8', 'replace')
                if line:
                    await cli.handle_line(line)
                if writer.is_closing():
                    return
    except asyncio.TimeoutError:
        await cli.send(None, 'ERROR :Closing link: (registration timeout)')
    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
        pass
    except Exception as e:
        log(f'[IRC] conn error {peer}: {type(e).__name__}: {e}')
    finally:
        # 断开：向所有共同频道广播 QUIT（官方 `:nick QUIT :Connection closed`）
        if cli.registered:
            touched = [ch for ch in CHANNELS.values() if cli in ch.members]
            for ch in touched:
                ch.members.discard(cli)
                for member in list(ch.members):
                    await member.send(cli.nick, 'QUIT :Connection closed')
                if not ch.members and ch.key not in {n.lower() for n, _ in OFFICIAL_CHANNELS}:
                    if CHANNELS.get(ch.key) is ch:
                        CHANNELS.pop(ch.key, None)
            log(f'[IRC] DISCONNECT {cli.nick}!{cli.user}@{cli.ip}')
        cli.cleanup()
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def start():
    """realtest_server._supervise('IRC', irc_server.start) 入口。"""
    server = await asyncio.start_server(handle_conn, '0.0.0.0', LISTEN_PORT)
    log(f'[IRC] server listening on 0.0.0.0:{LISTEN_PORT}')
    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    # 独立运行（无游戏服/DB）：自动注册演示账号便于手测
    import sys
    demo_key = secrets.token_bytes(20)
    demo_sid = secrets.randbits(63)
    register_session(demo_sid, 1, 'DemoPlayer', demo_key)
    hmac_b64 = base64.b64encode(hmac.new(demo_key, f'{demo_sid}IRC'.encode(),
                                         hashlib.sha1).digest()).decode()
    print(f'demo: PASS {demo_sid}::{hmac_b64}')
    print(f'      NICK DemoPlayer')
    print(f'      USER 1 "anon.com" "0.0.0.0" :anon')
    asyncio.run(start())
