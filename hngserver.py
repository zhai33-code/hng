"""HnG community-server mock / protocol recorder.

Implements the wire protocol reverse-engineered from the AIR client
(dk.reto.heroesandgenerals.service.Chunk / SocketConnector / ServiceHandler):

  TCP, little-endian. Chunk tree:
    [u32 totalSize][u32 selfSize][data][subchunks...]
    totalSize = 8 + len(data) + sum(sub.totalSize)
    selfSize  = 4 + len(data)

  Request (C->S):  main{data=int32 cmdId, sub0{data="requestType", sub0{data=proto}}}
  Response (S->C): main{data=int32 cmdId, sub0{data="responseTitle", sub0{data=proto}}}

Login2 flow: login2_begin -> login2_challenge -> login2_response -> login2_result.

Demo credentials: none (fixed demo password removed; see realtest_server.py /register).
Run:  python hngserver.py [port]      (default 15110, as seen in Settings.as)
"""
import asyncio
import base64
import hashlib
import hmac
import os
import secrets
import struct
import sys
import time

# ---------------------------------------------------------------- chunk codec

class Chunk:
    __slots__ = ('data', 'subs')

    def __init__(self, data=b'', subs=None):
        self.data = data
        self.subs = subs if subs is not None else []

    @staticmethod
    def parse(buf, off=0):
        """Parse one chunk at off; returns (Chunk, next_off) or (None, off)."""
        if off + 4 > len(buf):
            return None, off
        total = struct.unpack_from('<i', buf, off)[0]
        if total < 8 or off + total > len(buf):
            return None, off
        self_size = struct.unpack_from('<i', buf, off + 4)[0]
        data_end = off + 8 + (self_size - 4)
        c = Chunk(buf[off + 8:data_end])
        pos = data_end
        remaining = total - 4 - self_size  # bytes belonging to subchunks
        while remaining > 0:
            sub, nxt = Chunk.parse(buf, pos)
            if sub is None:
                break
            c.subs.append(sub)
            consumed = nxt - pos
            remaining -= consumed
            pos = nxt
        return c, off + total

    def total_size(self):
        return 8 + len(self.data) + sum(s.total_size() for s in self.subs)

    def write(self, out):
        out += struct.pack('<i', self.total_size())
        out += struct.pack('<i', 4 + len(self.data))
        out += self.data
        for s in self.subs:
            out = s.write(out)
        return out

    def as_int(self):
        return struct.unpack('<i', self.data[:4])[0] if len(self.data) >= 4 else 0

    def as_str(self):
        return self.data.decode('utf-8', 'replace')


def parse_request(chunk):
    """main -> (cmdId, requestType, protoBytes)"""
    cmd_id = chunk.as_int()
    t = chunk.subs[0]
    req_type = t.as_str()
    proto = t.subs[0].data if t.subs else b''
    return cmd_id, req_type, proto


def build_response(cmd_id, title, proto_bytes=b''):
    main = Chunk(struct.pack('<i', cmd_id))
    main.subs.append(Chunk(title.encode(), [Chunk(proto_bytes)]))
    return main.write(b'')

# ------------------------------------------------------------- protobuf wire

def _pb_varint_value(value):
    if value < 0:
        value += 1 << 64
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)

def _pb_tag(field, wire_type):
    # tag is itself a varint; fields >= 16 overflow a single byte
    return _pb_varint_value(field << 3 | wire_type)

def pb_varint(field, value):
    return _pb_tag(field, 0) + _pb_varint_value(value)

def pb_string(field, value):
    v = value.encode() if isinstance(value, str) else value
    return _pb_tag(field, 2) + _pb_len(len(v)) + v

def pb_int32(field, value):
    return pb_varint(field, value)

def pb_int64(field, value):
    return pb_varint(field, value)

def pb_uint32(field, value):
    return pb_varint(field, value)

def pb_float(field, value):
    return _pb_tag(field, 5) + struct.pack('<f', value)

def _pb_len(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)

def pb_message(field, payload):
    return _pb_tag(field, 2) + _pb_len(len(payload)) + payload

def pb_packed_bool(field, bool_list):
    """proto2 repeated bool (非 packed 格式，兼容 hurlant AS3 protobuf 库)
    每个 bool 元素单独 tag(wire_type=0) + varint(0/1)"""
    tag = _pb_tag(field, 0)
    out = bytearray()
    for b in bool_list:
        out += tag
        out += b'\x01' if b else b'\x00'
    return bytes(out)

def _pb_read_varint(b, i):
    shift = 0
    val = 0
    while True:
        if i >= len(b):
            raise ValueError('varint truncated')
        c = b[i]; i += 1
        val |= (c & 0x7F) << shift
        if not c & 0x80:
            return val, i
        shift += 7

def pb_parse(b):
    """Minimal proto2 wire decoder -> {field_num: value(bytes for wiretype 2, int otherwise)}.
    Repeated fields keep the last occurrence (enough for login messages)."""
    fields = {}
    i = 0
    while i < len(b):
        tag, i = _pb_read_varint(b, i)
        fn, wt = tag >> 3, tag & 7
        if wt == 0:
            val, i = _pb_read_varint(b, i)
        elif wt == 1:
            val = b[i:i + 8]; i += 8
        elif wt == 2:
            ln, i = _pb_read_varint(b, i)
            val = b[i:i + ln]; i += ln
        elif wt == 5:
            val = b[i:i + 4]; i += 4
        else:
            raise ValueError(f'unsupported wiretype {wt}')
        fields[fn] = val
    return fields

# ------------------------------------------------------------------ messages

RC = {'fail': 0, 'ok': 1, 'redirect': 93, 'challenge': 96,
      'login_success': 97, 'wrong_password': 98}


def msg_login2_challenge(salt_b64, temp_sid_b64, enc_key_b64):
    m = b''
    m += pb_string(1, salt_b64)
    m += pb_string(2, temp_sid_b64)
    m += pb_string(3, enc_key_b64)
    m += pb_int32(7, RC['challenge'])
    return m


def msg_player(gamertag, credits=None, warfunds=None, gold=None,
               tutorials_completed=0):
    m = b''
    # 货币默认与数据库新账号默认值一致（新账号给默认货币）
    if credits is None:
        credits = 100000
    if warfunds is None:
        warfunds = 100000
    if gold is None:
        gold = 100000
    m += pb_int64(1, 1)                 # id
    m += pb_string(2, gamertag)
    m += pb_uint32(3, 0)                # invertmouse
    m += pb_uint32(4, 0)                # invertmouseairplane
    m += pb_float(5, 0.5)               # mousesensitivity
    m += pb_string(6, '')               # administrator
    m += pb_uint32(7, 0)                # mousecontrolledcars
    m += pb_uint32(8, 1)                # showcrosshair
    m += pb_uint32(9, 1)                # showcrosshairVehicles
    m += pb_int64(10, 1)                # war
    m += pb_int64(11, 1)                # factionid (1=US 2=GE 3=USSR)
    m += pb_string(12, '')              # subscribedupto
    # 货币：Bought/Earned/Used 三段式，余额 = Bought + Earned - Used
    m += pb_int64(13, 0)                # goldBought
    m += pb_int64(14, gold)             # goldEarned (初始金币)
    m += pb_int64(15, 0)                # goldUsed
    m += pb_int64(16, 0)                # creditsBought
    m += pb_int64(17, credits)          # creditsEarned (初始银币)
    m += pb_int64(18, 0)                # creditsUsed
    m += pb_string(19, '')              # firstActionGameDate
    m += pb_int32(20, 1)                # numloginsFlash
    m += pb_int32(21, 0)                # numloginsMobile
    m += pb_string(22, 'en')            # language
    m += pb_int64(23, 0)                # warfundsBought
    m += pb_int64(24, warfunds)         # warfundsEarned (初始战争资金)
    m += pb_int64(25, 0)                # warfundsUsed
    m += pb_string(26, 'metrics-demo')  # metricsUid (optional)
    m += pb_int32(27, 0)                # yellowGriefPoints
    m += pb_int32(28, 0)                # redGriefPoints
    m += pb_string(29, '')              # latestRedGriefPointTimestamp
    m += pb_int32(30, 1)                # tier
    m += pb_string(31, time.strftime('%Y-%m-%d %H:%M:%S'))
    m += pb_int32(32, tutorials_completed)  # tutorialsCompleted
    m += pb_int64(33, 0)                # goldBoughtSteam
    m += pb_float(34, 0.0)              # skillLevel
    m += pb_uint32(35, 0)               # invertmouseturret
    m += pb_int64(36, 0)                # totalScore
    # unlockBits (field 37): repeated bool, 位索引 = PlayerLevelUnlock.id
    # 全 true = 解锁所有玩家级功能（War模式/匹配模式/职业等）
    m += pb_packed_bool(37, [True] * 41)  # PlayerLevelUnlock ids 1-40 all exist
    m += pb_int32(38, 1)                # level
    m += pb_varint(40, 0)               # isDeleted (required bool=false)
    m += pb_string(41, 'EUR')          # currencyCode
    m += pb_string(42, 'EU')           # currencyRegion
    # playeruid (43) optional, 省略
    return m


def msg_login2_result(username, code):
    m = b''
    m += pb_string(1, username)
    m += pb_string(2, time.strftime('%Y-%m-%d %H:%M:%S'))
    m += pb_int32(3, code)
    if code == RC['login_success']:
        m += pb_message(6, msg_player(username))
    return m

# -------------------------------------------------------------------- server

DEMO_PASSWORD = b''  # 旧演示密码已移除：digest 由 session_key 派生，不再依赖固定密码
LOG = open(os.path.join(os.path.dirname(__file__), 'requests.log'), 'a', encoding='utf-8')

def log(msg):
    line = f'[{time.strftime("%H:%M:%S")}] {msg}'
    print(line)
    LOG.write(line + '\n')
    LOG.flush()


class Session:
    def __init__(self, writer):
        self.writer = writer
        self.pending = {}   # cmdId -> requestType
        self.username = None

    def send(self, raw):
        self.writer.write(raw)
        # asyncio: flush via drain in handler


async def handle(reader, writer):
    addr = writer.get_extra_info('peername')
    log(f'CONNECT from {addr}')
    sess = Session(writer)
    buf = b''
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            buf += data
            while True:
                chunk, nxt = Chunk.parse(buf, 0)
                if chunk is None:
                    break
                buf = buf[nxt:]
                await dispatch(sess, chunk, writer)
    except (ConnectionResetError, asyncio.IncompleteReadError):
        pass
    finally:
        log(f'DISCONNECT {addr}')
        writer.close()


async def dispatch(sess, chunk, writer):
    try:
        cmd_id, req_type, proto = parse_request(chunk)
    except Exception as e:
        log(f'PARSE-ERR {e}')
        return
    sess.pending[cmd_id] = req_type
    log(f'C->S cmd={cmd_id:<4} type={req_type:<45} proto={len(proto)}B')

    if req_type == 'login2_begin':
        try:
            username = pb_parse(proto).get(1, b'').decode('utf-8', 'replace')
        except Exception:
            username = 'DemoPlayer'
        salt = secrets.token_bytes(16)
        temp_sid = secrets.token_bytes(16)
        session_key = secrets.token_bytes(20)
        # session key encrypted as: key XOR SHA1(SHA1(pwd||salt) || temp_sid)
        pw_hash = hashlib.sha1(DEMO_PASSWORD + salt).digest()
        k1 = hashlib.sha1(pw_hash + temp_sid).digest()
        enc = bytes(b ^ k1[i % len(k1)] for i, b in enumerate(session_key))
        sess.username = username
        sess.login_ctx = (salt, temp_sid, session_key)
        resp = build_response(cmd_id, 'login2_challenge',
                              msg_login2_challenge(base64.b64encode(salt).decode(),
                                                   base64.b64encode(temp_sid).decode(),
                                                   base64.b64encode(enc).decode()))
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} login2_challenge (challenge=96) user={username!r}')

    elif req_type == 'login2_response':
        if not hasattr(sess, 'login_ctx'):
            log('login2_response without challenge - ignored')
            return
        salt, temp_sid, session_key = sess.login_ctx
        try:
            f = pb_parse(proto)
            digest = base64.b64decode(f.get(2, b''))
        except Exception as e:
            log(f'login2_response parse error: {e}')
            digest = b''
        expected = hmac.new(session_key, temp_sid, hashlib.sha1).digest()
        if hmac.compare_digest(digest, expected):
            code = RC['login_success']
            note = 'login_success=97'
        else:
            code = RC['wrong_password']
            note = 'wrong_password=98 (digest mismatch)'
        resp = build_response(cmd_id, 'login2_result', msg_login2_result(sess.username or 'DemoPlayer', code))
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} login2_result -- {note}')

    elif req_type.startswith('subscribe'):
        resp = build_response(cmd_id, 'subscriberesponse')
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} subscriberesponse')

    elif req_type.startswith('unsubscribe'):
        resp = build_response(cmd_id, 'unsubscriberesponse')
        writer.write(resp)
        await writer.drain()
        log(f'S->C cmd={cmd_id:<4} unsubscriberesponse')

    elif req_type in ('keepalive', 'keepaliverequest', 'keepaliveresponse',
                      'login2_player_updated_response'):
        pass  # fire-and-forget

    else:
        # TODO: implement handlers per decompiled/proto-files/*.proto
        log(f'      (no handler for {req_type} - see decompiled/proto-files/ for its schema)')


async def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 15110
    server = await asyncio.start_server(handle, '0.0.0.0', port)
    log(f'HnG mock server listening on 0.0.0.0:{port}')
    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    asyncio.run(main())
