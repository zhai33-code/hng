# -*- coding: utf-8 -*-
"""UnlockNotification 推送回归（2026-09-17 P2-3，纯单元级，无需 DB）。

背景：此前服务端从不推 UnlockNotification 表，客户端解锁弹窗/士兵选择
面包屑角标全哑，misc.py 的 RespondToUnlockNotification 回执 handler 空转
（一直在等一条永远不会来的通知）。补齐三件套：
  1. messages._msg_unlock_notification：HnG_States.UnlockNotification 9 字段
  2. player_service.push_ribbon_unlock_notifications：勋带等级提升区间
     (old_lv, new_lv] 内每个 progressionUnlock uid 一行
  3. combat 结算 / shop AddRibbonLevel 两处生效点 + misc 回执 delete 清表

客户端契约（UnlockNotificationModel.as / UnlockNotificationTable.as）：
  - 行 ADD/UPDATE → newNotifications → 按 progressionUnlockId 的
    unlockItemType 弹 12 种解锁窗之一
  - 已拥有武器/载具 checkIfWeaponIsOwned 自清不弹；eCharacterType
    clearNotification 自清——服务端无需甄别物品类型
"""
import asyncio
import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol import Chunk, pb_parse          # noqa: E402
from messages import _msg_unlock_notification  # noqa: E402
from player_service import (                   # noqa: E402
    push_ribbon_unlock_notifications, _UNLOCK_NOTIF_SEQ)
from wardb import _RIBBON_META_LEVEL           # noqa: E402

PASS = FAIL = 0


def check(label, ok, detail=''):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f'[PASS] {label}{("  " + detail) if detail else ""}')
    else:
        FAIL += 1
        print(f'[FAIL] {label}{("  " + detail) if detail else ""}')


class _FakeWriter:
    def __init__(self):
        self.frames = []

    def write(self, data):
        self.frames.append(data)

    async def drain(self):
        pass


def _kvcs_frames(frames):
    """KVCS 帧列表 → [(action, table, row_proto_dict)]（单行推送 helper 产物）。"""
    out = []
    for buf in frames:
        root, _ = Chunk.parse(buf, 0)
        assert root.subs[0].as_str() == 'KeyValueChangeSet'
        for act in root.subs[0].subs:
            for tbl in act.subs:
                for row in tbl.subs:
                    out.append((act.as_str(), tbl.as_str(),
                                pb_parse(row.data)))
    return out


def _pick_ribbon():
    """选一个等级条目丰富（≥4 个不同 unlockMetaLevel）的真实勋带。"""
    best = None
    for rid, entries in _RIBBON_META_LEVEL.items():
        lvs = {lv for _, lv in entries}
        if best is None or len(lvs) > best[2]:
            best = (rid, entries, len(lvs))
        if len(lvs) >= 6:
            break
    return best[0], best[1]


def _main():
    rid, entries = _pick_ribbon()
    lvs = sorted({lv for _, lv in entries})
    old_lv, new_lv = lvs[1], lvs[-1]
    expect_uids = {uid for uid, lv in entries if old_lv < lv <= new_lv}
    print(f'-- 用例勋带 {rid}：level 集 {lvs[:8]}...，区间 ({old_lv},{new_lv}] '
          f'期望 {len(expect_uids)} 行')

    # T1 行构建器：9 字段逐一对拍
    row = _msg_unlock_notification(77, 101, 202, 303, 3000, 5)
    f = pb_parse(row)
    check('T1 f1 id / f2 playerId / f3 characterId / f4 progressionUnlockId',
          (f[1], f[2], f[3], f[4]) == (77, 101, 202, 303),
          str((f[1], f[2], f[3], f[4])))
    check('T1 f5 ribbonTemplateId / f6 ribbonLevel',
          (f[5], f[6]) == (3000, 5), str((f[5], f[6])))
    check('T1 f7 unlockTimestamp 为 Zulu 14 位',
          re.fullmatch(r'\d{14}Z',
                       (f[7] or b'').decode()) is not None, str(f[7]))
    check('T1 f8 notificationResponse=0 / f9 markedForDelete=0',
          f[8] == 0 and f[9] in (False, 0), str((f[8], f[9])))

    # T2 推送：区间行数/表名/字段/id 递增唯一
    w = _FakeWriter()
    seq0 = _UNLOCK_NOTIF_SEQ[0]
    n = asyncio.run(push_ribbon_unlock_notifications(
        w, 4242, 101, 202, int(rid), old_lv, new_lv))
    rows = _kvcs_frames(w.frames)
    got_uids = {r[2][4] for r in rows}
    check(f'T2 行数 = 区间条目数（{len(expect_uids)}）',
          n == len(expect_uids) == len(rows), f'n={n} rows={len(rows)}')
    check('T2 全部为 set UnlockNotification 表行',
          all(a == 'set' and t == 'UnlockNotification' for a, t, _ in rows))
    check('T2 progressionUnlockId 集合 = wardb 区间推导',
          got_uids == expect_uids,
          f'差异 {sorted(got_uids ^ expect_uids)[:6]}')
    ok_fields = all(r[2][2] == 101 and r[2][3] == 202 and r[2][5] == int(rid)
                    for r in rows)
    check('T2 行内 playerId/characterId/ribbonTemplateId 正确', ok_fields)
    ids = [r[2][1] for r in rows]
    check('T2 行 id 全局递增唯一（发号器连续）',
          ids == list(range(seq0 + 1, seq0 + 1 + len(rows))), str(ids[:5]))
    ok_lv = all(old_lv < r[2][6] <= new_lv for r in rows)
    check('T2 ribbonLevel 落在提升区间', ok_lv)

    # T3 等级未涨不推（floor 持平/下降、防重复推送）
    w3 = _FakeWriter()
    n3 = asyncio.run(push_ribbon_unlock_notifications(
        w3, 4242, 101, 202, int(rid), new_lv, new_lv))
    check('T3 new_lv == old_lv → 0 行', n3 == 0 and not w3.frames)
    n3b = asyncio.run(push_ribbon_unlock_notifications(
        _FakeWriter(), 4242, 101, 202, int(rid), new_lv, old_lv))
    check('T3 new_lv < old_lv → 0 行', n3b == 0)

    # T4 无订阅视图（view_cmd=None）不推不炸
    n4 = asyncio.run(push_ribbon_unlock_notifications(
        _FakeWriter(), None, 101, 202, int(rid), old_lv, new_lv))
    check('T4 view_cmd=None → 0 行（无订阅静默跳过）', n4 == 0)

    # T5 未知勋带 id → 0 行（progressionUnlock 表无此 metaName 条目）
    n5 = asyncio.run(push_ribbon_unlock_notifications(
        _FakeWriter(), 4242, 101, 202, 999_999_999, 0, 12))
    check('T5 未知 ribbon id → 0 行', n5 == 0)

    print()
    print(f'== UnlockNotification 推送: {PASS} PASS / {FAIL} FAIL ==')
    return FAIL


if __name__ == '__main__':
    sys.exit(_main())
