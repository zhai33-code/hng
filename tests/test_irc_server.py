# -*- coding: utf-8 -*-
"""IRC 服务端回放测试：对照 irc-official-transcript.txt 官方抓包逐格式断言。

覆盖：注册 numeric 序列 / PASS 验签（含篡改拒绝）/ 强制改名与 %N 双连接 /
JOIN-PART-PRIVMSG 中继格式（不回显发送者：SWF IRCSend 本地回环自显）/
ULIST=LIST / 私聊直达（同样不回显）/
QUIT 广播 / WHOIS 378 格式 / 客户端怪癖（`:prefix` 行首前缀、`JOIN :#a,#b`）。

Run:  python tests/test_irc_server.py
"""
import asyncio
import base64
import hashlib
import hmac
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import irc_server


PASS_OK = []


def make_session(username, player_id=101):
    """注册表登记 + 返回合法 PASS 参数（R19 口径）。"""
    key = secrets.token_bytes(20)
    sid = secrets.randbits(63)
    irc_server.register_session(sid, player_id, username, key)
    mac = base64.b64encode(
        hmac.new(key, f'{sid}IRC'.encode(), hashlib.sha1).digest()).decode()
    return sid, f'{sid}::{mac}'


class Client:
    def __init__(self, reader, writer):
        self.r, self.w = reader, writer
        self.lines = []
        self.buf = b''          # 实例级行缓冲（跨 read_until 保留未消费行）

    @classmethod
    async def connect(cls, port):
        r, w = await asyncio.open_connection('127.0.0.1', port)
        return cls(r, w)

    async def send(self, line):
        self.w.write(line.encode() + b'\r\n')
        await self.w.drain()

    async def read_until(self, pred, timeout=3.0, collect=True):
        """读行直到 pred(line) 为真；返回匹配行（全部已读行留在 self.lines）。"""
        async def _drain():
            while True:
                # 先消费上批残留
                while True:
                    nl = self.buf.find(b'\n')
                    if nl < 0:
                        break
                    raw, self.buf = self.buf[:nl], self.buf[nl + 1:]
                    line = raw.rstrip(b'\r').decode('utf-8', 'replace')
                    if collect:
                        self.lines.append(line)
                    if pred(line):
                        return line
                data = await self.r.read(4096)
                if not data:
                    raise ConnectionError('closed')
                self.buf += data
        return await asyncio.wait_for(_drain(), timeout=timeout)

    async def drain_initial_notices(self):
        """连接后先收两条 NOTICE（Looking up / Skipping）。"""
        await self.read_until(lambda l: 'Skipping host resolution' in l)

    async def register(self, pass_arg, nick_req, user='u101'):
        await self.send(f'PASS {pass_arg}')
        await self.send(f'NICK {nick_req}')
        await self.send(f'USER {user} "anon.com" "0.0.0.0" :anon')
        return await self.read_until(lambda l: l.startswith(':')
                                     and ' MODE ' in l)      # 注册序列末行 MODE +x

    async def close(self):
        self.w.close()


def find(lines, sub):
    for l in lines:
        if sub in l:
            return l
    return None


async def test_registration_flow(port):
    """注册序列逐行断言（R9）：改名 → welcome NOTICE → 001/002/003/004/005×2/
    042/375/372/376/251.../396/MODE +x。"""
    sid, pw = make_session('DoubleBird')
    c = await Client.connect(port)
    await c.drain_initial_notices()
    await c.register(pw, 'u123', user='c8784008981093438986')
    # 权威改名（请求名 u123 → 玩家名 DoubleBird；43096 路径无 433）
    rename = find(c.lines, 'NICK DoubleBird')
    assert rename == ':u123 NICK DoubleBird', rename
    assert find(c.lines, 'Welcome to HeroesAndGenerals!'), c.lines
    l001 = find(c.lines, 'Welcome to the HeroesAndGenerals IRC Network')
    assert l001 and l001.startswith(':irc.hng-group.com 001 DoubleBird '), l001
    assert '!c8784008981093438986@' in l001
    assert find(c.lines, 'running version inspircd2')
    assert find(c.lines, 'inspircd2 ioswx')
    assert sum(1 for l in c.lines if ' 005 ' in l) == 2
    assert find(c.lines, ' :your unique ID')
    assert sum(1 for l in c.lines if ' 372 ' in l) == 11        # MOTD 11 行
    assert find(c.lines, ' 251 ')
    assert find(c.lines, 'is now your displayed host')
    assert ':DoubleBird MODE DoubleBird +x' in c.lines
    await c.close()
    print('PASS  registration flow')


async def test_pass_rejected(port):
    """PASS 篡改 / 无 PASS → ERROR unauthorized + 断开。"""
    sid, pw = make_session('Hacker')
    for bad in (pw[:-2] + 'xx',          # 篡改 HMAC
                f'{secrets.randbits(63)}::{pw.split("::")[1]}',  # 未知 sid
                ):
        c = await Client.connect(port)
        await c.drain_initial_notices()
        await c.send(f'PASS {bad}')
        await c.send('NICK u1')
        await c.send('USER u1 x y :z')
        err = await c.read_until(lambda l: l.startswith('ERROR'))
        assert 'unauthorized' in err, err
        await c.close()
    # 无 PASS：三要素齐也被拒
    c = await Client.connect(port)
    await c.drain_initial_notices()
    await c.send('NICK u1')
    await c.send('USER u1 x y :z')
    err = await c.read_until(lambda l: l.startswith('ERROR'))
    assert 'unauthorized' in err, err
    await c.close()
    print('PASS  reject tampered/unknown/missing PASS')


async def test_join_part_privmsg(port):
    """JOIN 批量冒号形式 → 回显带冒号频道 + 353/366；PRIVMSG 中继含发送者；
    PART 回显无冒号频道；断开广播 QUIT。"""
    _, pw1 = make_session('Alice', 1)
    _, pw2 = make_session('Bob', 2)
    a = await Client.connect(port)
    await a.drain_initial_notices()
    await a.register(pw1, 'u1')
    b = await Client.connect(port)
    await b.drain_initial_notices()
    await b.register(pw2, 'u2')
    a.lines.clear(); b.lines.clear()
    # 原生 SyncChannels 批量形式 `JOIN :#a,#b`
    await a.send('JOIN :#790452278987754075,#GlobalChatEN')
    j1 = await a.read_until(lambda l: l.endswith('JOIN :#790452278987754075'))
    assert j1.startswith(':Alice JOIN :#'), j1
    await a.read_until(lambda l: ' 366 ' in l and '#790452278987754075' in l)
    await a.read_until(lambda l: ' 366 ' in l and '#GlobalChatEN' in l)
    # Bob 加入（普通参数形式）→ Alice 看到 JOIN 广播
    await b.send('JOIN #790452278987754075')
    await b.read_until(lambda l: l.endswith('JOIN :#790452278987754075'))
    await b.read_until(lambda l: ' 366 ' in l)
    await a.read_until(lambda l: l == ':Bob JOIN :#790452278987754075')
    # 353 含双成员
    names = find(b.lines, ' 353 ')
    assert names and 'Alice' in names and 'Bob' in names, b.lines
    # PRIVMSG 中继：Bob 收到；发送者 Alice 不回显（SWF IRCSend 发送后
    # 本地回环 parseString 已自显，官方服务器不回显，回显即双显）
    a.lines.clear(); b.lines.clear()
    await a.send('PRIVMSG #790452278987754075 :hello world')
    await b.read_until(lambda l: l == ':Alice PRIVMSG #790452278987754075 :hello world')
    await a.send('PING :echo')                       # 探测：下一响应前不得混入 PRIVMSG
    await a.read_until(lambda l: l == 'PONG :echo')
    assert not find(a.lines, 'PRIVMSG'), a.lines
    # 私聊：Bob 收到目标=Bob；Alice 同样不回显
    a.lines.clear(); b.lines.clear()
    await a.send('PRIVMSG Bob :psst')
    await b.read_until(lambda l: l == ':Alice PRIVMSG Bob :psst')
    await a.send('PING :echo2')
    await a.read_until(lambda l: l == 'PONG :echo2')
    assert not find(a.lines, 'PRIVMSG'), a.lines
    # PART：回显频道无冒号
    a.lines.clear(); b.lines.clear()
    await b.send('PART #790452278987754075')
    await a.read_until(lambda l: l == ':Bob PART #790452278987754075')
    got = await b.read_until(lambda l: 'PART' in l)
    assert got == ':Bob PART #790452278987754075', got
    # QUIT 广播（Bob 重新入频道，QUIT 才有共同频道成员可通知）
    await b.send('JOIN #790452278987754075')
    await b.read_until(lambda l: ' 366 ' in l)
    await a.read_until(lambda l: l == ':Bob JOIN :#790452278987754075')
    await b.send('QUIT')
    await a.read_until(lambda l: l == ':Bob QUIT :Connection closed')
    await a.close(); await b.close()
    print('PASS  join/part/privmsg/quit relay formats')


async def test_ulist_and_official(port):
    """ULIST（`：nick ` 前缀怪癖）= LIST：321/322×20 官方频道/323；数字频道
    在 ULIST 里也列出；GlobalChatEN 带 +D。"""
    _, pw = make_session('DoubleBird', 55)
    c = await Client.connect(port)
    await c.drain_initial_notices()
    await c.register(pw, 'u55')
    c.lines.clear()
    await c.send(':DoubleBird ULIST')           # 抓包实测带前缀
    await c.read_until(lambda l: ' 323 ' in l)
    assert find(c.lines, ' 321 ') and 'Channel :Users Name' in find(c.lines, ' 321 ')
    n322 = [l for l in c.lines if ' 322 ' in l]
    assert len(n322) >= 20, len(n322)
    names = {l.split()[3] for l in n322}
    assert '#GlobalChatEN' in names and '#GlobalChatZH' in names \
        and '#PlayersHelpDE' in names, sorted(names)
    en = find(n322, '#GlobalChatEN ') or find(n322, '#GlobalChatEN')
    assert en and '[+Dfnt 6:8] ' in en, en      # +D 仅 EN，末尾空格照抄
    zh = [l for l in n322 if '#GlobalChatZH' in l][0]
    assert '[+fnt 6:8] ' in zh, zh
    # 加入数字频道后再 ULIST：该频道计入
    await c.send('JOIN #1234567890123456789')
    await c.read_until(lambda l: ' 366 ' in l)
    c.lines.clear()
    await c.send('LIST')
    await c.read_until(lambda l: ' 323 ' in l)
    assert any('#1234567890123456789 1 ' in l for l in c.lines), c.lines
    await c.close()
    print('PASS  ULIST/LIST + 20 official channels')


async def test_dual_connection_suffix(port):
    """同玩家双连接：先到得纯名，后来得 名%N（抓包 %N 语义）。"""
    _, pw = make_session('DoubleBird', 77)
    c1 = await Client.connect(port)
    await c1.drain_initial_notices()
    await c1.register(pw, 'u77a')
    assert ':u77a NICK DoubleBird' in c1.lines
    c2 = await Client.connect(port)
    await c2.drain_initial_notices()
    await c2.register(pw, 'u77b')
    rename = find(c2.lines, 'NICK DoubleBird%')
    assert rename and rename.startswith(':u77b NICK DoubleBird%'), rename
    suffix = rename.rsplit('%', 1)[1].split()[0]
    assert 1 <= int(suffix) <= 255, suffix
    await c1.close(); await c2.close()
    print('PASS  dual connection %N suffix')


async def test_whois_gline_mode(port):
    """WHOIS → 378 第 4 空格分片 <user>@<ip>（SWF GLINE 截取依据）；
    GLINE/KICK 无 mod → 481；频道 MODE +V 静默接受（不回错误）。"""
    _, pw = make_session('Alice', 1)
    _, pw2 = make_session('Bob', 2)
    a = await Client.connect(port)
    await a.drain_initial_notices()
    await a.register(pw, 'u1')
    b = await Client.connect(port)
    await b.drain_initial_notices()
    await b.register(pw2, 'u2')
    a.lines.clear()
    await a.send('JOIN #GlobalChatEN')
    await a.read_until(lambda l: ' 366 ' in l)
    await a.send('MODE #GlobalChatEN +V 1')
    await a.send('WHOIS Bob')
    l378 = await a.read_until(lambda l: ' 378 ' in l)
    seg = l378.split(' :', 1)[1].split(' ')[3]
    assert '@' in seg, l378                        # <user>@<ip>
    assert seg == 'u101@127.0.0.1', seg            # Bob 的 USER 字段 + 客户端 IP
    await a.read_until(lambda l: ' 318 ' in l)
    await a.send('GLINE Bob@* :test')
    await a.read_until(lambda l: ' 481 ' in l)
    await a.send('KICK #GlobalChatEN Bob')
    await a.read_until(lambda l: l.endswith('481') or ' 481 ' in l)
    assert not any(' 421 ' in l for l in a.lines)  # MODE +V 未触发 421
    await a.close(); await b.close()
    print('PASS  WHOIS 378 / GLINE 481 / MODE +V no-op')


async def test_unregister_kicks(port):
    """unregister_session（logout/断线）断开名下已注册 IRC 连接。"""
    sid, pw = make_session('Victim', 9)
    c = await Client.connect(port)
    await c.drain_initial_notices()
    await c.register(pw, 'u9')
    irc_server.unregister_session(sid)
    err = await c.read_until(lambda l: l.startswith('ERROR'))
    assert 'logged out' in err, err
    await c.close()
    print('PASS  unregister kicks IRC connection')


async def main():
    irc_server.LISTEN_PORT = 0                     # 测试用随机端口
    server = await asyncio.start_server(irc_server.handle_conn,
                                        '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    try:
        await test_registration_flow(port)
        await test_pass_rejected(port)
        await test_join_part_privmsg(port)
        await test_ulist_and_official(port)
        await test_dual_connection_suffix(port)
        await test_whois_gline_mode(port)
        await test_unregister_kicks(port)
        # 全局态复核：连接全部关闭后无泄漏
        await asyncio.sleep(0.1)
        assert not irc_server.CLIENTS, irc_server.CLIENTS
    finally:
        server.close()
        await server.wait_closed()
    print('ALL IRC TESTS PASSED')


if __name__ == '__main__':
    asyncio.run(main())
