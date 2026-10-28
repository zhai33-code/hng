"""战斗服异常退出兜底收尾回归测试（2026-08-27 卡 closing 事故）。

事故：玩家中途退出战局，combat server 未走 earnings 直接退出
（ReportServerShutdown + exit code=10016）。旧版 _watch_proc_exit
只 reclaim 坑位（release_match），不推 PMS DELETE，客户端 closing
流程收不到会话结束信号 → 每秒轮询 GetMissionDetails 拿
mission_not_found，56 秒超时后才自行 Abandon 回大厅。

验证：
1) _finish_mission_sessions(mid, None)（无战报路径）：推
   PlayerMissionSession DELETE KVCS、不推 Notification、清内部状态；
2) 幂等：PMS 映射已清后重复调用不再推送；
3) _watch_proc_exit 兜底：未 release 的实例进程退出 → 触发 1) 收尾；
4) 正常路径（earnings 已先 release_match 出队）：兜底空转不重复触发。
"""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import combat_instances
import mm_state
from handlers import combat as combat_handlers
from protocol import Chunk
from state import FLASH_SESSIONS


class _MockWriter:
    """收集响应字节的 writer 替身（write/drain 均 async）。"""

    def __init__(self):
        self.sent = b''

    def write(self, data):
        self.sent += bytes(data)

    async def drain(self):
        return


class _FakeProc:
    """已退出的战斗服进程替身（returncode=10016，wait 立即返回）。"""

    returncode = 10016

    async def wait(self):
        return 0


def _setup(mid, pms, pid, tag):
    """登记一名在局玩家：PMS 映射 + 在线 Flash 会话，返回收集 writer。"""
    writer = _MockWriter()
    flash = types.SimpleNamespace(writer=writer, playerview_cmd=8,
                                  soldier_view_cmd=None)
    FLASH_SESSIONS[tag] = flash
    mm_state.PMS_TO_MISSION[pms] = mid
    mm_state.PMS_TO_SESSION[pms] = {
        'player_id': pid, 'gamertag': tag, 'squad_db_id': 0}
    return writer


def _cleanup(mid, pms, tag):
    FLASH_SESSIONS.pop(tag, None)
    mm_state.PMS_TO_MISSION.pop(pms, None)
    mm_state.PMS_TO_SESSION.pop(pms, None)
    combat_instances.COMBAT_INSTANCES.pop(mid, None)


def test_finish_sessions_without_report():
    mid, pms = 987001, 987101
    writer = _setup(mid, pms, 1, 'ExitGuyA')
    try:
        asyncio.run(combat_handlers._finish_mission_sessions(mid, None))
        assert b'PlayerMissionSession' in writer.sent \
            and b'delete' in writer.sent, '未推 PMS DELETE KVCS'
        assert b'Notification' not in writer.sent, \
            '无战报不应推 NewBattleReport 通知'
        assert pms not in mm_state.PMS_TO_MISSION, 'PMS_TO_MISSION 未清'
        assert pms not in mm_state.PMS_TO_SESSION, 'PMS_TO_SESSION 未清'
        # 幂等：PMS 映射已清，重复调用不再推送
        before = writer.sent
        asyncio.run(combat_handlers._finish_mission_sessions(mid, None))
        assert writer.sent == before, '重复调用不应再推送'
    finally:
        _cleanup(mid, pms, 'ExitGuyA')


def test_watch_proc_exit_finishes_sessions():
    mid, pms = 987002, 987102
    writer = _setup(mid, pms, 2, 'ExitGuyB')
    released = []
    real_release = combat_instances.release_match
    combat_instances.release_match = lambda m: released.append(m)  # 隔离副作用
    try:
        inst = {'seq': 777, 'proc': _FakeProc(), 'port': 5678,
                'mission_id': mid}
        combat_instances.COMBAT_INSTANCES[mid] = inst

        async def _run():
            await combat_instances._watch_proc_exit(inst, mid)
            await asyncio.sleep(0.05)   # 让 _spawn 的收尾任务跑完

        asyncio.run(_run())
        assert released == [mid], '未回收坑位'
        assert b'PlayerMissionSession' in writer.sent \
            and b'delete' in writer.sent, '兜底未推 PMS DELETE（卡 closing 根因）'
        assert pms not in mm_state.PMS_TO_MISSION, '兜底未清 PMS 映射'
    finally:
        combat_instances.release_match = real_release
        _cleanup(mid, pms, 'ExitGuyB')


def test_watch_proc_exit_normal_path_no_finish():
    """正常路径：earnings 已先 release_match（实例出队），兜底空转。"""
    mid, pms = 987003, 987103
    writer = _setup(mid, pms, 3, 'ExitGuyC')
    released = []
    real_release = combat_instances.release_match
    combat_instances.release_match = lambda m: released.append(m)
    try:
        inst = {'seq': 778, 'proc': _FakeProc(), 'port': 5679,
                'mission_id': mid}
        combat_instances.COMBAT_INSTANCES[mid] = inst
        combat_instances.COMBAT_INSTANCES.pop(mid)   # 模拟 earnings 已 release

        async def _run():
            await combat_instances._watch_proc_exit(inst, mid)
            await asyncio.sleep(0.05)

        asyncio.run(_run())
        assert released == [], '已释放实例不应重复 release'
        assert writer.sent == b'', '正常路径不应触发兜底收尾'
    finally:
        combat_instances.release_match = real_release
        _cleanup(mid, pms, 'ExitGuyC')


def test_finish_sessions_repush_earned_chars():
    """结算角色行重推必须按战报实际获 XP 的角色，而非会话 char_id。

    事故（2026-09-01）：进队时角色 11，部署阶段换兵为 12（会话 char_id
    不回写），XP 落到 12 → 重推却推 11，人物界面等级/XP 不刷新。"""
    mid, pms = 987004, 987104
    writer = _MockWriter()
    flash = types.SimpleNamespace(writer=writer, playerview_cmd=8,
                                  soldier_view_cmd=9)
    FLASH_SESSIONS['ExitGuyD'] = flash
    mm_state.PMS_TO_MISSION[pms] = mid
    mm_state.PMS_TO_SESSION[pms] = {
        'player_id': 1, 'gamertag': 'ExitGuyD', 'squad_db_id': 0,
        'char_id': 11}                       # 进队时角色（非实际参战）

    class _Store:
        @staticmethod
        async def list_characters(pid):
            return [{'id': 11, 'name': 'Konrad', 'faction': 2, 'template': 0,
                     'combat_xp': 0},
                    {'id': 12, 'name': 'Bastian', 'faction': 2, 'template': 0,
                     'combat_xp': 70}]

    old_store = combat_handlers.STORE
    old_bits = combat_handlers._char_unlock_bits_for
    combat_handlers.STORE = _Store()

    async def _bits(sess, template, faction, char_id=0):
        return []
    combat_handlers._char_unlock_bits_for = _bits
    try:
        asyncio.run(combat_handlers._finish_mission_sessions(
            mid, 555, {1: {12}}))             # 战报：实际获 XP 的是 12
        assert b'Bastian' in writer.sent, '未重推实际参战角色 12'
        assert b'Konrad' not in writer.sent, '不应重推会话残留角色 11'
    finally:
        combat_handlers.STORE = old_store
        combat_handlers._char_unlock_bits_for = old_bits
        _cleanup(mid, pms, 'ExitGuyD')


def _kvcs_rows(buf):
    """从原始字节流收集 KVCS 行：(action, 表名, 行 proto)，按推送顺序。"""
    rows, off = [], 0
    while off < len(buf):
        c, off = Chunk.parse(buf, off)
        if c is None:
            break
        title = c.subs[0] if c.subs else None
        if title is not None and title.as_str() == 'KeyValueChangeSet':
            action = title.subs[0]
            table = action.subs[0]
            row = table.subs[0] if table.subs else None
            rows.append((action.as_str(), table.as_str(),
                         row.data if row is not None else b''))
    return rows


def _pb_fields(b):
    """极简 protobuf 解析：{字段号: [varint 或 bytes]}。"""
    out, i = {}, 0
    while i < len(b):
        tag = b[i]
        i += 1
        fn, wt = tag >> 3, tag & 7
        if wt == 0:
            v = 0
            shift = 0
            while True:
                byte = b[i]
                i += 1
                v |= (byte & 0x7F) << shift
                shift += 7
                if not byte & 0x80:
                    break
            out.setdefault(fn, []).append(v)
        elif wt == 2:
            n, shift, j = 0, 0, i
            while True:
                byte = b[j]
                j += 1
                n |= (byte & 0x7F) << shift
                shift += 7
                if not byte & 0x80:
                    break
            i = j
            out.setdefault(fn, []).append(b[i:i + n])
            i += n
        else:
            break
    return out


def test_finish_sessions_pushes_maintenance_notification():
    """自动维修消耗记录（2026-09-04 玩家反馈缺失）：

    结算扣费后应推 NotificationData(credits/gold 金额) + Notification
    (type=6 ItemMaintenance)；数据行必须先于通知行（客户端渲染器
    set data 时一次性读 NotificationDataTable）。"""
    mid, pms = 987005, 987105
    writer = _MockWriter()
    flash = types.SimpleNamespace(writer=writer, playerview_cmd=8,
                                  soldier_view_cmd=None)
    FLASH_SESSIONS['ExitGuyE'] = flash
    mm_state.PMS_TO_MISSION[pms] = mid
    mm_state.PMS_TO_SESSION[pms] = {
        'player_id': 4, 'gamertag': 'ExitGuyE', 'squad_db_id': 0}
    try:
        asyncio.run(combat_handlers._finish_mission_sessions(
            mid, 666, None, {4: {'credits': 120, 'gold': 7}}))
        rows = _kvcs_rows(writer.sent)
        data_rows = [(t, r) for a, t, r in rows if t == 'NotificationData']
        notif_rows = [(t, r) for a, t, r in rows if t == 'Notification']
        assert len(data_rows) == 2, f'应有 credits/gold 两条数据行，实际 {data_rows}'
        amounts = {}
        for _, r in data_rows:
            f = _pb_fields(r)
            key = f[3][0].decode()
            amounts[key] = f[4][0]                 # key → value
        assert amounts == {'credits': 120, 'gold': 7}, amounts
        # 通知行：type=6 ItemMaintenance、未读；数据行先于通知行
        maint = [r for _, r in notif_rows
                 if _pb_fields(r).get(3) == [6]]
        assert len(maint) == 1, '应推一条 ItemMaintenance 通知'
        nf = _pb_fields(maint[0])
        assert nf[5] == [0] and nf[2] == [4], '应未读且归属本局玩家'
        notif_id = nf[1][0]
        for _, r in data_rows:
            assert _pb_fields(r)[2] == [notif_id], '数据行应挂在本通知下'
        order = [t for _, t, _ in rows]
        maint_idx = max(i for i, (_, t, r) in enumerate(rows)
                        if t == 'Notification' and _pb_fields(r).get(3) == [6])
        assert maint_idx > max(i for i, t in enumerate(order)
                               if t == 'NotificationData'), \
            'NotificationData 必须先于 ItemMaintenance 通知推送'
    finally:
        _cleanup(mid, pms, 'ExitGuyE')


def test_finish_sessions_resets_solo_squad():
    """战斗结束 solo 假小队重置（2026-09-16 改，取代 09-09 删行）。

    旧版删 solo 行 → 客户端 wasThroughLoginProcessAndHasSessions=true 的
    会话 squadSession 变 null → 切屏 SquadModel.activate() 发信号 →
    StatusMediator.onSquadStateUpdates 解引用 squadSession.missionId
    TypeError #1009 → isActive/isOwnedByPlayer 卡死（幽灵小队：面板无
    头像、解散静默失败）→ onWarBattleFiltersValidated 早退 → 战争图点
    城市不出现加入战斗按钮（2026-09-16 Berlin 实锤）。新语义与 abandon/
    取消匹配一致：
    - SquadSession set（missionId=0）先
    - SquadMemberSession set（status=2 InLobby）后
    - 均先于 PlayerMissionSession delete（onLobbySignalUpdates 读
      squadSession 非 null 且 missionId=0 → BASE_STATE）
    - 不删任何小队行；solo 注册表条目保留（下次排队复用行 id）、
      mission_id 归零
    """
    mid, pms = 987006, 987106
    writer = _setup(mid, pms, 5, 'ExitSoloGuy')
    mm_state._SOLO_SQUADS[5] = {
        'squad_row': 7001, 'member_row': 7002, 'mission_id': mid,
        'faction': 1, 'template': 1, 'entered': 1,
        'gamertag': 'ExitSoloGuy', 'char_id': 25, 'char_name': 'ESG',
        'rank': 5}
    try:
        asyncio.run(combat_handlers._finish_mission_sessions(mid, None))
        rows = _kvcs_rows(writer.sent)
        squad_rows = [(a, t) for a, t, _ in rows
                      if t in ('SquadSession', 'SquadMemberSession')]
        assert squad_rows == [('set', 'SquadSession'),
                              ('set', 'SquadMemberSession')], \
            f'solo 假小队应重置不删行，实际 {squad_rows}'
        ss = [r for a, t, r in rows
              if a == 'set' and t == 'SquadSession'][0]
        # f17 missionId=0 线格式锚点：tag 0x88 0x01（字段号≥16 多字节，
        # _pb_fields 单字节 tag 解析不适用）+ value 0x00
        assert b'\x88\x01\x00' in ss, 'SquadSession missionId 应归零'
        sm = [r for a, t, r in rows
              if a == 'set' and t == 'SquadMemberSession'][0]
        assert _pb_fields(sm).get(12) == [2], '成员状态应为 2 InLobby'
        order = [t for _, t, _ in rows]
        assert order.index('SquadSession') < order.index('SquadMemberSession') \
            < order.index('PlayerMissionSession'), \
            f'重置顺序错误：{order}'
        solo = mm_state._SOLO_SQUADS.get(5)
        assert solo is not None and solo['mission_id'] == 0, \
            '注册表条目应保留且 mission_id 归零'
    finally:
        _cleanup(mid, pms, 'ExitSoloGuy')
        mm_state._SOLO_SQUADS.pop(5, None)


def test_finish_sessions_resets_auto_squad():
    """凑队（auto squad）战斗结束：删凑队行（成员行先 + SquadSession 后，
    同 mission.py release 路径）+ solo 行重置（set mission=0/InLobby）+
    PMS delete。"""
    mid, pms = 987007, 987107
    writer = _setup(mid, pms, 6, 'ExitAutoGuy')
    mm_state.PMS_TO_SESSION[pms]['auto_sq_id'] = 9001
    mm_state.PMS_TO_SESSION[pms]['auto_sq_rows'] = [9101, 9102]
    mm_state._SOLO_SQUADS[6] = {
        'squad_row': 7003, 'member_row': 7004, 'mission_id': mid,
        'faction': 2, 'template': 1, 'entered': 1,
        'gamertag': 'ExitAutoGuy', 'char_id': 26, 'char_name': 'EAG',
        'rank': 4}
    try:
        asyncio.run(combat_handlers._finish_mission_sessions(mid, None))
        rows = _kvcs_rows(writer.sent)
        squad_rows = [(a, t) for a, t, _ in rows
                      if t in ('SquadSession', 'SquadMemberSession')]
        assert squad_rows == [('delete', 'SquadMemberSession'),
                              ('delete', 'SquadMemberSession'),
                              ('delete', 'SquadSession'),
                              ('set', 'SquadSession'),
                              ('set', 'SquadMemberSession')], \
            f'凑队删行+solo 重置序列错误，实际 {squad_rows}'
        import struct
        member_ids = [struct.unpack('<q', r)[0] for a, t, r in rows[:2]
                      if t == 'SquadMemberSession']
        assert member_ids == [9101, 9102], member_ids
        assert rows[2] == ('delete', 'SquadSession',
                           struct.pack('<q', 9001)), \
            f'凑队 SquadSession 应在成员行后删除，实际 {rows[2]}'
        # solo 重置行内容：missionId=0 + 成员 status=2
        ss = [r for a, t, r in rows
              if a == 'set' and t == 'SquadSession'][0]
        assert b'\x88\x01\x00' in ss, 'SquadSession missionId 应归零'
        sm = [r for a, t, r in rows
              if a == 'set' and t == 'SquadMemberSession'][0]
        # f12 status=2 线格式锚点（成员行含多字节 tag 字段，
        # _pb_fields 单字节 tag 解析会脱轨）
        assert b'\x60\x02' in sm, '成员状态应为 2 InLobby'
        order = [t for _, t, _ in rows]
        assert order.index('PlayerMissionSession') > 4, \
            f'PMS delete 应在小队行推送之后，实际 {order}'
    finally:
        _cleanup(mid, pms, 'ExitAutoGuy')
        mm_state._SOLO_SQUADS.pop(6, None)


if __name__ == '__main__':
    test_finish_sessions_without_report()
    test_watch_proc_exit_finishes_sessions()
    test_watch_proc_exit_normal_path_no_finish()
    test_finish_sessions_repush_earned_chars()
    test_finish_sessions_pushes_maintenance_notification()
    test_finish_sessions_resets_solo_squad()
    test_finish_sessions_resets_auto_squad()
    print('test_combat_exit_finish: 7 项全部通过')
