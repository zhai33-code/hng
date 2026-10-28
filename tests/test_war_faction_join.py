# -*- coding: utf-8 -*-
"""每场战争选阵营（官服 join_war 口径）回归，DB-free。

背景（2026-09-03）：官服 player f10=war id、f11=阵营选择（0=未选 →
客户端 FactionSelectionWindow / 首页 CTA）；选阵营经 join_war_request
落库 player_war_factions(player, war, faction)，新战争（sequel 新
war_id）无行自动重弹。旧实现 f11 恒派生值 → 从不弹窗 + 阵营跨战漂移。

覆盖：
  A. msg_player 编码：war/faction None 回退（1/1）、显式 0（未选 → f11=0）、
     war=0、war=2/faction=3 透传
  B. msg_login2_result 透传：f6 player 行内 f10/f11 一致
  C. _msg_player_for（登录/重推数据源）：未选 → f11=0、已选 SU → f11=3、
     sequel 新战争 → f10=新 id 且 f11=0（重弹）
  D. handle_join_war_request：选阵营落库+ok+重推 player 行（f11=faction）、
     幂等重选 ok、换阵营 79、无 factionid 未选 ok/已选 48、STORE=None 77
  E. STORE 不可用 → 77

写法沿用 test_at_stats_rank.py：monkeypatch + FakeStore + check()，
运行 `python tests/test_war_faction_join.py`（pytest 收集不执行 main）。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol import (
    Chunk,
    pb_int32,
    pb_int64,
    pb_parse,
    msg_player,
    msg_login2_result,
    )
import mm_config
import wardb
import war_campaign

RESULTS = []


def check(label, ok, detail=''):
    RESULTS.append((label, ok))
    print(f'[{"PASS" if ok else "FAIL"}] {label}{("  " + detail) if detail else ""}')


# ------------------------------------------------------- wardb 合成（加载前）
wardb.accesspoints = lambda: []
wardb.supplylines = lambda: []
wardb.battlefields = lambda: []
wardb.capitals = lambda: [{'battlefield_id': 10, 'default_faction': 1}]
wardb.commandnode_templates = lambda: {}
wardb.morale_template = lambda: None
wardb.armyresource_production = lambda: []
war_campaign.common._ADJACENCY_CACHE = None
war_campaign.common._BF_POS_CACHE = None

mm_config.WAR_AUTO_CREATE = True

# ---- handlers 包整体导入（views 有相对导入，须按包加载）----
import handlers  # noqa: E402
import handlers.views as views  # noqa: E402
import player_service  # noqa: E402


# ------------------------------------------------------------- 测试替身

class FakeStore:
    """join_war / _msg_player_for 最小存储接口。"""

    def __init__(self):
        self.pool = object()
        self.wars = {1: {'id': 1, 'name': 'war-1', 'state': 1,
                         'begintime': None, 'endtime': None,
                         'winning_faction_id': 0,
                         'points_needed_for_victory': 15,
                         'sequel_war_id': 0, 'mapid': 1, 'config': {}}}
        self.next_war_id = 2
        # (pid, war_id) -> faction
        self.war_factions = {}
        self.characters = {100: [{'id': 7, 'faction': 2}]}

    # ---- war ----
    async def get_active_war(self):
        for w in self.wars.values():
            if w['state'] == 1:
                return dict(w)
        return None

    async def list_wars(self, limit=20):
        return [dict(w) for w in
                sorted(self.wars.values(), key=lambda w: -w['id'])[:limit]]

    async def create_war(self, name, points_needed_for_victory=15, mapid=1,
                         config=None):
        wid = self.next_war_id
        self.next_war_id += 1
        w = {'id': wid, 'name': name, 'state': 1, 'begintime': None,
             'endtime': None, 'winning_faction_id': 0,
             'points_needed_for_victory': points_needed_for_victory,
             'sequel_war_id': 0, 'mapid': mapid, 'config': config or {}}
        self.wars[wid] = w
        return dict(w)

    async def init_war_cities(self, war_id, owner_map):
        pass

    async def init_war_faction_resources(self, war_id, initial):
        pass

    # ---- 阵营选择 ----
    async def get_player_war_faction(self, pid, war_id):
        return self.war_factions.get((int(pid), int(war_id)))

    async def set_player_war_faction(self, pid, war_id, faction):
        self.war_factions[(int(pid), int(war_id))] = int(faction)
        return True

    # ---- _msg_player_for 依赖 ----
    async def get_currency(self, pid):
        return {'credits': 111, 'warfunds': 222, 'gold': 333}

    async def get_tutorials_completed(self, pid):
        return 0

    async def get_daily_metrics(self, pid):
        return None

    async def list_characters(self, pid):
        return [dict(c) for c in self.characters.get(int(pid), [])]

    async def get_stats(self, cid):
        return {}

    async def get_total_score(self, pid):
        return 0


class FakeSess:
    playerview_cmd = 555
    username = 'tester'

    async def ensure_player(self):
        return 100


class FakeWriter:
    def __init__(self):
        self.buf = bytearray()

    def write(self, data):
        self.buf.extend(data)

    async def drain(self):
        pass


# ------------------------------------------------------------- 帧解析

def _chunks(buf):
    out, off = [], 0
    while off < len(buf):
        c, off = Chunk.parse(bytes(buf), off)
        if c is None:
            break
        out.append(c)
    return out


def _resp(w, title):
    want = title.encode()
    for c in reversed(_chunks(w.buf)):
        if c.subs and c.subs[0].data == want:
            return pb_parse(c.subs[0].subs[0].data)
    raise AssertionError(f'no response {title}')


def _rows(w, table):
    t = table.encode() if isinstance(table, str) else table
    out = []
    for c in _chunks(w.buf):
        if not c.subs or c.subs[0].data != b'KeyValueChangeSet':
            continue
        act = c.subs[0].subs[0]
        if act.data == b'set' and act.subs and act.subs[0].data == t:
            out.append(pb_parse(act.subs[0].subs[0].data))
    return out


def _setup(store=None):
    st = store or FakeStore()
    views.STORE = st
    player_service.STORE = st
    return st


def _restore():
    """恢复 dispatch 时绑定的全局 STORE（防污染其他测试）。"""
    import state
    views.STORE = state.STORE
    player_service.STORE = state.STORE


async def _join(factionid, warid=1):
    w = FakeWriter()
    req = pb_int64(1, warid) + pb_int32(2, factionid)
    await views.handle_join_war_request(
        FakeSess(), w, 901, b'join_war_request', req)
    return w, _resp(w, 'join_war_response')


# ------------------------------------------------------------- A. msg_player

async def section_a():
    p = pb_parse(msg_player('t'))
    check('A1 默认 war/faction 回退（f10=1 f11=1）',
          int(p.get(10) or 0) == 1 and int(p.get(11) or 0) == 1,
          f'f10={p.get(10)} f11={p.get(11)}')
    p = pb_parse(msg_player('t', war=2, faction=3))
    check('A2 显式透传（f10=2 f11=3）',
          int(p.get(10) or 0) == 2 and int(p.get(11) or 0) == 3,
          f'f10={p.get(10)} f11={p.get(11)}')
    p = pb_parse(msg_player('t', war=2, faction=0))
    check('A3 未选阵营 f11=0（触发选择窗）',
          int(p.get(10) or 0) == 2 and int(p.get(11, 0)) == 0,
          f'f10={p.get(10)} f11={p.get(11, 0)}')
    p = pb_parse(msg_player('t', war=0))
    check('A4 无战争 f10=0',
          int(p.get(10, 0)) == 0, f'f10={p.get(10, 0)}')


# ----------------------------------------------------- B. msg_login2_result

async def section_b():
    p = pb_parse(msg_login2_result('t', 97, pid=100, faction=0, war=1))
    sub = pb_parse(p.get(6))
    check('B1 login2 透传未选（f6 内 f10=1 f11=0）',
          int(sub.get(10) or 0) == 1 and int(sub.get(11, 0)) == 0,
          f'f10={sub.get(10)} f11={sub.get(11, 0)}')
    p = pb_parse(msg_login2_result('t', 97, pid=100, faction=3, war=1))
    sub = pb_parse(p.get(6))
    check('B2 login2 透传已选 SU（f11=3）',
          int(sub.get(11) or 0) == 3, f'f11={sub.get(11)}')


# ------------------------------------------------------- C. _msg_player_for

async def section_c():
    st = _setup()
    # 未选：f11=0 f10=1（FakeStore war 1 running）
    p = pb_parse(await player_service._msg_player_for(FakeSess()))
    check('C1 未选阵营 → f11=0（弹选择窗）',
          int(p.get(10) or 0) == 1 and int(p.get(11, 0)) == 0,
          f'f10={p.get(10)} f11={p.get(11, 0)}')
    # 已选 SU
    st.war_factions[(100, 1)] = 3
    p = pb_parse(await player_service._msg_player_for(FakeSess()))
    check('C2 已选 SU → f11=3',
          int(p.get(11) or 0) == 3, f'f11={p.get(11)}')
    # 续战新 war：旧选择不带入
    st.wars[1]['state'] = 2
    st.wars[2] = {'id': 2, 'name': 'war-2', 'state': 1, 'begintime': None,
                  'endtime': None, 'winning_faction_id': 0,
                  'points_needed_for_victory': 15, 'sequel_war_id': 0,
                  'mapid': 1, 'config': {}}
    st.next_war_id = 3
    p = pb_parse(await player_service._msg_player_for(FakeSess()))
    check('C3 sequel 新战争 → f10=2 f11=0（重弹选择窗）',
          int(p.get(10) or 0) == 2 and int(p.get(11, 0)) == 0,
          f'f10={p.get(10)} f11={p.get(11, 0)}')
    _restore()


# --------------------------------------------------- D. handle_join_war_request

async def section_d():
    st = _setup()
    # 首次选 SU：ok + 落库 + 重推 player 行 f11=3
    w, f = await _join(3)
    check('D1 选 SU → ok=1',
          int(f.get(1) or 0) == 1, f'msg={f.get(1)}')
    check('D2 落库 player_war_factions(100,1)=3',
          st.war_factions.get((100, 1)) == 3, str(st.war_factions))
    rows = _rows(w, 'player')
    check('D3 重推 player 行（f11=3）',
          len(rows) == 1 and int(rows[0].get(11) or 0) == 3,
          f'rows={len(rows)} f11={rows[0].get(11) if rows else None}')
    # 幂等重选同阵营：ok，不新增
    w, f = await _join(3)
    check('D4 幂等重选 ok（行数不变）',
          int(f.get(1) or 0) == 1 and len(st.war_factions) == 1,
          f'msg={f.get(1)} rows={len(st.war_factions)}')
    # 换阵营：79 blocked
    w, f = await _join(1)
    check('D5 战争进行中换阵营 → 79',
          int(f.get(1) or 0) == 79 and st.war_factions.get((100, 1)) == 3,
          f'msg={f.get(1)}')
    # 无 factionid 且未选：ok 不落库（重定向流程）
    st.war_factions.pop((100, 1))
    w, f = await _join(0)
    check('D6 无 factionid 未选 → ok 不落库',
          int(f.get(1) or 0) == 1 and not st.war_factions,
          f'msg={f.get(1)}')
    # 无 factionid 且已选：48 moving_to_same_war
    st.war_factions[(100, 1)] = 2
    w, f = await _join(0)
    check('D7 无 factionid 已选 → 48',
          int(f.get(1) or 0) == 48, f'msg={f.get(1)}')
    _restore()


# ----------------------------------------------------------- E. STORE=None

async def section_e():
    views.STORE = None
    w = FakeWriter()
    req = pb_int64(1, 1) + pb_int32(2, 3)
    await views.handle_join_war_request(
        FakeSess(), w, 901, b'join_war_request', req)
    f = _resp(w, 'join_war_response')
    check('E1 STORE=None → 77 war_not_found',
          int(f.get(1) or 0) == 77, f'msg={f.get(1)}')
    _restore()


async def main():
    print('== A. msg_player 编码 ==')
    await section_a()
    print('== B. msg_login2_result 透传 ==')
    await section_b()
    print('== C. _msg_player_for 登录口径 ==')
    await section_c()
    print('== D. join_war_request ==')
    await section_d()
    print('== E. STORE 不可用 ==')
    await section_e()
    failed = [r for r, ok in RESULTS if not ok]
    print(f'\n{"=" * 50}\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed')
    if failed:
        for r in failed:
            print(f'  FAIL: {r}')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
