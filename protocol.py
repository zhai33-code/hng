# -*- coding: utf-8 -*-
"""HnG wire protocol primitives (Chunk codec + protobuf wire codec + base messages).

Implements the wire protocol reverse-engineered from the AIR client
(dk.reto.heroesandgenerals.service.Chunk / SocketConnector / ServiceHandler):

  TCP, little-endian. Chunk tree:
    [u32 totalSize][u32 selfSize][data][subchunks...]
    totalSize = 8 + len(data) + sum(sub.totalSize)
    selfSize  = 4 + len(data)

  Request (C->S):  main{data=int32 cmdId, sub0{data="requestType", sub0{data=proto}}}
  Response (S->C): main{data=int32 cmdId, sub0{data="responseTitle", sub0{data=proto}}}

本模块只含纯编解码原语与登录基础消息构建，无 I/O、无全局状态，
供 realtest_server / game_server / matchmaking / handlers 共用。
"""
import gzip
import struct
import time

MAX_CHUNK_SIZE = 16 * 1024 * 1024

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
        if total < 8 or total > MAX_CHUNK_SIZE:
            raise ValueError(f'invalid chunk size {total}')
        if off + total > len(buf):
            return None, off
        self_size = struct.unpack_from('<i', buf, off + 4)[0]
        if self_size < 4 or self_size > total - 4:
            raise ValueError(f'invalid chunk self size {self_size}/{total}')
        data_end = off + 8 + (self_size - 4)
        c = Chunk(buf[off + 8:data_end])
        pos = data_end
        remaining = total - 4 - self_size  # bytes belonging to subchunks
        while remaining > 0:
            sub, nxt = Chunk.parse(buf, pos)
            if sub is None:
                raise ValueError('truncated subchunk')
            c.subs.append(sub)
            consumed = nxt - pos
            remaining -= consumed
            if remaining < 0:
                raise ValueError('subchunks exceed parent chunk')
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


def build_zipchunk(inner_frame_bytes, cmd_id=0):
    """官方 zipchunk 压缩帧：一个完整内层帧（如 KVCS main 帧）标准 gzip
    后装进 zipchunk proto（netsysmessages.zipchunk：required bytes
    data=1，wire type 2），外层走标准 response 布局。

    客户端契约（Chunk.as:94-103）：收到标题 "zipchunk" 的消息 → 取
    subChunks[0]（proto 体）data 字段 → GZIPBytesEncoder.uncompress
    ToByteArray 解压 → parseData 只解析**第一棵** Chunk 树 → 单个
    zipchunk 只能装 1 个内层完整帧（多帧被丢弃，大批量靠 KVCS 批量
    消息本身装多行）。外层 cmd_id 客户端丢弃，固定 0。官方流量同为
    标准 gzip 流（hagcp wiresharkDecode 可直接 gunzip 验证）。"""
    return build_response(cmd_id, 'zipchunk',
                          pb_string(1, gzip.compress(inner_frame_bytes)))

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

def pb_sfixed64(field, value):
    """SFIXED64 字段（wire type 1，定长 8 字节小端有符号）。

    battle/battlefieldstatus/supplylinestatus/accesspointstatus 的 f1 id
    为 S64（docs/hng-rts-protocol.md §3/§7.5），与 INT64 varint 编码不同。"""
    return _pb_tag(field, 1) + struct.pack('<q', int(value))

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

def pb_bool(field, value):
    """bool 字段 = varint 0/1。"""
    return pb_varint(field, 1 if value else 0)

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
        if shift >= 70:
            raise ValueError('varint too long')

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
            if i + 8 > len(b):
                raise ValueError('fixed64 truncated')
            val = b[i:i + 8]; i += 8
        elif wt == 2:
            ln, i = _pb_read_varint(b, i)
            if ln > len(b) - i:
                raise ValueError('length-delimited field truncated')
            val = b[i:i + ln]; i += ln
        elif wt == 5:
            if i + 4 > len(b):
                raise ValueError('fixed32 truncated')
            val = b[i:i + 4]; i += 4
        else:
            raise ValueError(f'unsupported wiretype {wt}')
        fields[fn] = val
    return fields

def pb_parse_multi(b):
    """Repeated-aware wire decoder -> {field_num: [value, ...]}（保持出现顺序）。
    wiretype 0 → int；1/5/2 → bytes。与 pb_parse 互补，用于 repeated 请求
    （如 GetBattleReportRequest.missionBattleReportIds）。"""
    fields = {}
    i = 0
    while i < len(b):
        tag, i = _pb_read_varint(b, i)
        fn, wt = tag >> 3, tag & 7
        if wt == 0:
            val, i = _pb_read_varint(b, i)
        elif wt == 1:
            if i + 8 > len(b):
                raise ValueError('fixed64 truncated')
            val = b[i:i + 8]; i += 8
        elif wt == 2:
            ln, i = _pb_read_varint(b, i)
            if ln > len(b) - i:
                raise ValueError('length-delimited field truncated')
            val = b[i:i + ln]; i += ln
        elif wt == 5:
            if i + 4 > len(b):
                raise ValueError('fixed32 truncated')
            val = b[i:i + 4]; i += 4
        else:
            raise ValueError(f'unsupported wiretype {wt}')
        fields.setdefault(fn, []).append(val)
    return fields

def pb_parse_loose(data):
    """宽松版 pb_parse（原 game_server.hng_parse）：
    - wiretype 5 解为 float（pb_parse 返回原始 4 字节）
    - 未知 wiretype 直接停止解析返回已得字段（pb_parse 抛异常）
    用于游戏进程(warserver)协议的请求解析。"""
    fields = {}

    def rd_varint(buf, off):
        val = 0
        shift = 0
        while True:
            b = buf[off]
            off += 1
            val |= (b & 0x7F) << shift
            if not (b & 0x80):
                return val, off
            shift += 7

    off = 0
    while off < len(data):
        tag, off = rd_varint(data, off)
        field = tag >> 3
        wt = tag & 7
        if wt == 0:
            v, off = rd_varint(data, off)
            fields[field] = v
        elif wt == 1:
            fields[field] = data[off:off + 8]
            off += 8
        elif wt == 2:
            ln, off = rd_varint(data, off)
            fields[field] = data[off:off + ln]
            off += ln
        elif wt == 5:
            fields[field] = struct.unpack('<f', data[off:off + 4])[0]
            off += 4
        else:
            break
    return fields

def pb_split_top(raw):
    """protobuf 顶层拆成 [(field_num, 原始字节块)]（保持顺序，支持 repeated）。"""
    items = []
    i, n = 0, len(raw)
    while i < n:
        start = i
        key = shift = 0
        while True:
            b = raw[i]; i += 1
            key |= (b & 0x7F) << shift; shift += 7
            if not (b & 0x80):
                break
        fn, wt = key >> 3, key & 7
        if wt == 0:
            while True:
                b = raw[i]; i += 1
                if not (b & 0x80):
                    break
        elif wt == 2:
            ln = lsh = 0
            while True:
                b = raw[i]; i += 1
                ln |= (b & 0x7F) << lsh; lsh += 7
                if not (b & 0x80):
                    break
            i += ln
        elif wt == 5:
            i += 4
        elif wt == 1:
            i += 8
        else:
            raise ValueError(f'wiretype {wt}')
        items.append((fn, raw[start:i]))
    return items

def pb_field_payload(chunk):
    """完整字段块（tag+len+payload）→ payload 字节（非 length-delimited 返回 None）。"""
    i = s = key = 0
    while True:
        b = chunk[i]; i += 1
        key |= (b & 0x7f) << s; s += 7
        if not b & 0x80:
            break
    if (key & 7) != 2:
        return None
    ln = lsh = 0
    while True:
        b = chunk[i]; i += 1
        ln |= (b & 0x7f) << lsh; lsh += 7
        if not b & 0x80:
            break
    return chunk[i:i + ln]

# ------------------------------------------------------------------ messages

RC = {'fail': 0, 'ok': 1, 'redirect': 93, 'challenge': 96,
      'login_success': 97, 'wrong_password': 98,
      # 账号封禁（客户端 ResponseCodes：91 弹"永久封禁"、92 弹"限时封禁至 xx"）
      'user_ban_permanent': 91, 'user_ban_temporary': 92}


def msg_login2_challenge(salt_b64, temp_sid_b64, enc_key_b64):
    m = b''
    m += pb_string(1, salt_b64)
    m += pb_string(2, temp_sid_b64)
    m += pb_string(3, enc_key_b64)
    m += pb_int32(7, RC['challenge'])
    return m


def msg_player(gamertag, credits=None, warfunds=None, gold=None,
               tutorials_completed=0, pid=0, faction=None, total_score=0,
               first_action_date='', level=None, war=None, unlock_ids=()):
    m = b''
    # 缺失余额不能冒充新账号礼包；真实新账号余额由数据库初始化值提供。
    if credits is None:
        credits = 0
    if warfunds is None:
        warfunds = 0
    if gold is None:
        gold = 0
    if first_action_date is None:
        first_action_date = ''
    # id：必须用数据库真实 player_id——客户端 Global.currentPlayer.id 即
    # 此值，战报界面 getCurrentPlayerInformation() 按它匹配本局玩家行；
    # 恒发 1 会导致 DB id≥2 的玩家战报匹配不到自己 → 结算窗口不弹
    m += pb_int64(1, int(pid or 0))     # id
    m += pb_string(2, gamertag)
    m += pb_uint32(3, 0)                # invertmouse
    m += pb_uint32(4, 0)                # invertmouseairplane
    m += pb_float(5, 0.5)               # mousesensitivity
    m += pb_string(6, '')               # administrator
    m += pb_uint32(7, 0)                # mousecontrolledcars
    m += pb_uint32(8, 1)                # showcrosshair
    m += pb_uint32(9, 1)                # showcrosshairVehicles
    # war（f10）：加入的战争 id；0 = 无战争。
    # （客户端 PlayerModel war==0 直接 return，不进战争逻辑）
    m += pb_int64(10, int(war) if war is not None else 0)
    # factionid：客户端阵营颜色/战场部署过滤/资源总览都读 currentPlayer.factionid，
    # None/0 = 本场战争未选阵营（官服 join_war 口径）——
    # 客户端 factionTemplateId==0 → 首页 warUpdateGreetingCallToAction CTA
    # / 进战争图弹 FactionSelectionWindow → join_war_request 落库后重推本行
    m += pb_int64(11, int(faction) if faction is not None else 0)
    m += pb_string(12, '')              # subscribedupto
    # 货币：Bought/Earned/Used 三段式，余额 = Bought + Earned - Used
    m += pb_int64(13, 0)                # goldBought
    m += pb_int64(14, gold)             # goldEarned (初始金币)
    m += pb_int64(15, 0)                # goldUsed
    m += pb_int64(16, 0)                # creditsBought
    m += pb_int64(17, credits)          # creditsEarned (初始银币)
    m += pb_int64(18, 0)                # creditsUsed
    m += pb_string(19, first_action_date)  # firstActionGameDate（YYYYMMDD，签到连续天数用）
    m += pb_int32(20, 1)                # numloginsFlash
    m += pb_int32(21, 0)                # numloginsMobile
    m += pb_string(22, 'en')            # language
    m += pb_int64(23, 0)                # warfundsBought
    m += pb_int64(24, warfunds)         # warfundsEarned (初始战争资金)
    m += pb_int64(25, 0)                # warfundsUsed
    m += pb_string(26, 'metrics-demo')  # metricsUid（保持 46d7f89 的客户端兼容值）传空会导致进不去游戏！！！
    m += pb_int32(27, 0)                # yellowGriefPoints
    m += pb_int32(28, 0)                # redGriefPoints
    m += pb_string(29, '')              # latestRedGriefPointTimestamp
    m += pb_int32(30, 1)                # tier
    m += pb_string(31, time.strftime('%Y-%m-%d %H:%M:%S'))
    m += pb_int32(32, tutorials_completed)  # tutorialsCompleted
    m += pb_int64(33, 0)                # goldBoughtSteam
    m += pb_float(34, 0.0)              # skillLevel
    m += pb_uint32(35, 0)               # invertmouseturret
    # totalScore：统计界面"分数"读 Global.currentPlayer.totalScore（此处 f36），
    # 不来自 CharacterStats——恒 0 会导致统计页分数显示 0（2026-09-01）。
    # 同时驱动玩家等级进度（PlayerLevelUnlock 按 totalScore 阈值解锁）
    m += pb_int64(36, int(total_score or 0))  # totalScore
    # level（f38）：客户端 currentPlayer.level，matchmaking 地图/模式列表按
    # missionpreset min/maxPlayerLevel 过滤用它（恒 1 会藏高级地图）；
    # None 时按 totalScore 经 PlayerLevel 阈值推导（与客户端 playerLevel 一致）
    if level is None:
        from wardb import player_level_from_score
        level = player_level_from_score(int(total_score or 0))
    # unlockBits (field 37): repeated bool, 位索引 = 解锁条目 id。
    # 客户端 hasPlayerLevelUnlock 用它门控 War 模式（eWar lv10）/匹配模式
    # （eMissionType）/武器组等——必须按等级置位，全 true 会让低级玩家
    # 也解锁全部模式（进展限制失效）；兵种模板位按等级置位（将军 lv12，
    # 客户端士兵栏不生成将军 fake 卡，仅 SoldierShop 页签解锁）。
    # unlock_ids：player_unlocks 永久解锁（通行证面部迷彩 2387-2395 等，
    # 按 progressionUnlock id 索引位图，等级推导不含这些 id）
    from wardb import player_unlock_bits_for_level
    m += pb_packed_bool(37, player_unlock_bits_for_level(int(level),
                                                         unlock_ids))
    m += pb_int32(38, int(level))     # level
    m += pb_varint(40, 0)               # isDeleted (required bool=false)
    m += pb_string(41, 'EUR')          # currencyCode
    m += pb_string(42, 'EU')           # currencyRegion
    # playeruid (43) optional, 省略
    return m


def msg_login2_result(username, code, pid=0, faction=None, war=None,
                      ban_reason=None, ban_expires=None):
    m = b''
    m += pb_string(1, username)
    m += pb_string(2, time.strftime('%Y-%m-%d %H:%M:%S'))
    m += pb_int32(3, code)
    if code in (RC['user_ban_permanent'], RC['user_ban_temporary']):
        # f4 banExpires / f5 banReason：客户端 StandaloneLoginCommand 消费，
        # f4 是 epoch 毫秒字符串（TimeUtilities.TimeAsString 直接 Number() 解析）
        if ban_expires is not None:
            m += pb_string(4, str(int(ban_expires.timestamp() * 1000)))
        if ban_reason:
            m += pb_string(5, ban_reason)
    if code == RC['login_success']:
        m += pb_message(6, msg_player(username, pid=pid, faction=faction,
                                      war=war))
    return m
