"""局内举报落库回归测试（2026-09-06 后台 0 记录事故）。

事故：局内举报由 hng.exe 经游戏连接直发（player.dll warcomm 注册表实证，
AddPlayerReportRequest 在 player.dll 0x180036596 注册），该连接会话无
username；ensure_player() 查 'demo'（线上库无此账号）→ None →
handle_add_player_report 静默跳过落库但回 RC_OK —— 玩家"举报成功"，
后台 player_reports 恒 0 行。

修复（两手）：
1) social.handle_add_player_report：客户端自带 reporter(f9) 优先，其次会话
   pid；都无法解析时显式记 DROPPED 日志，绝不无声丢弃
2) game_server.GetPlayerSettings：pms 精确命中时把 player_id/gamertag 绑定
   到游戏连接会话（局内社交类消息的身份来源）
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol import Chunk, pb_parse
from state import Session
from handlers import social


# ---------- protobuf 构造 helpers（与 test_player_ids.py 同款） ----------
def pb_varint(field, value):
    if value < 0:
        value += 1 << 64
    out = bytearray([field << 3 | 0])
    while True:
        b = value & 0x7F
        value >>= 7
        out.append(b | 0x80 if value else b)
        if not value:
            break
    return bytes(out)


def pb_msg(field, payload):
    out = bytearray([field << 3 | 2])
    ln = len(payload)
    while True:
        b = ln & 0x7F
        ln >>= 7
        out.append(b | 0x80 if ln else b)
        if not ln:
            break
    return bytes(out + bytearray(payload))


class FakeWriter:
    def __init__(self):
        self.sent = b''

    def write(self, data):
        self.sent += bytes(data)

    async def drain(self):
        return


class FakeStore:
    """线上事故现场：get_player('demo') → None（无 demo 账号）。"""

    def __init__(self):
        self.saved = []

    async def get_player(self, username):
        return None                    # 任何用户名都查不到 → ensure_player=None

    async def save_player_report(self, reporter, reported, category,
                                 session, chatlogs):
        self.saved.append((reporter, reported, category, session, chatlogs))


def _report_proto(reported=7, category=3, chatlogs=b'flame', reporter=0):
    out = pb_varint(5, reported) + pb_varint(6, category) + pb_msg(8, chatlogs)
    if reporter:
        out += pb_varint(9, reporter)
    return out


def _resp_code(w):
    """解析最后一帧 AddPlayerReportResponse 的 f1（response code）。"""
    pos, last = 0, None
    while pos < len(w.sent):
        c, pos = Chunk.parse(w.sent, pos)
        if c is None:
            break
        last = c
    assert last is not None and last.subs, '客户端应收到响应帧'
    assert last.subs[0].data == b'AddPlayerReportResponse'
    return pb_parse(last.subs[0].subs[0].data).get(1)


def _run_report(sess, proto):
    fake = FakeStore()
    w = FakeWriter()
    old = social.STORE
    social.STORE = fake
    try:
        asyncio.run(social.handle_add_player_report(
            sess, w, 50, 'AddPlayerReportRequest', proto))
    finally:
        social.STORE = old
    return fake, w


# ---------- 1. 事故现场：游戏连接（无 username / 无 demo）+ f9 自带举报人 ----------
def test_game_conn_report_with_f9_reporter_saved():
    """hng.exe 原生客户端自带 reporter(f9)：即使会话查不到身份也必须落库。"""
    sess = Session(FakeWriter())       # username=None, player_id=None
    fake, w = _run_report(sess, _report_proto(reporter=42))
    assert fake.saved == [(42, 7, 3, 0, 'flame')], \
        f'应按 f9 落库 reporter=42，实际 {fake.saved}'
    assert _resp_code(w) == social.RC_OK


# ---------- 2. GetPlayerSettings 绑定身份后（f9 缺省）走会话 pid ----------
def test_game_conn_report_with_bound_pid_saved():
    sess = Session(FakeWriter())
    sess.player_id = 1000032           # game_server.GetPlayerSettings 绑定
    sess.username = 'zhai'
    fake, w = _run_report(sess, _report_proto())
    assert fake.saved == [(1000032, 7, 3, 0, 'flame')], \
        f'应按会话 pid 落库，实际 {fake.saved}'
    assert _resp_code(w) == social.RC_OK


# ---------- 3. 身份彻底无法解析：不落库但不报错，客户端仍收 RC_OK ----------
def test_unresolvable_reporter_dropped_but_ok():
    sess = Session(FakeWriter())       # username/pid/f9 全空
    fake, w = _run_report(sess, _report_proto())
    assert fake.saved == [], '无法解析举报人时不得伪造身份落库'
    assert _resp_code(w) == social.RC_OK, '客户端仍应收到 RC_OK（不阻塞队列）'


# ---------- 4. 缺 reportedplayer(f5)：不落库（防脏数据） ----------
def test_missing_reported_field_not_saved():
    sess = Session(FakeWriter())
    sess.player_id = 1
    fake, _w = _run_report(sess, pb_varint(6, 3) + pb_msg(8, b'x'))
    assert fake.saved == [], '无被举报人字段不应落库'


if __name__ == '__main__':
    test_game_conn_report_with_f9_reporter_saved()
    test_game_conn_report_with_bound_pid_saved()
    test_unresolvable_reporter_dropped_but_ok()
    test_missing_reported_field_not_saved()
    print('test_player_report: 4/4 OK')
