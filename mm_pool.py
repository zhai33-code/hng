# -*- coding: utf-8 -*-
"""匹配池（纯逻辑、无 IO）：按 gamemodeId 分池 + 按玩家选图交集撮合。

数据来源（JoinMatchMakingRequest，反编译确认）：
  f7 gamemodeId —— 决定进入哪个模式的池
  f5 stagedMissionPresetList（repeated int64）—— 玩家勾选的地图 preset，
  空 = 不限图（通配）

撮合规则：
  1. FIFO：取池中最早的 entry 为锚点
  2. 收集与其选图有交集（任一方空 = 通配）的同模式 entry
  3. 凑够 min_players 即成组（截断到 max_players，剩余留池）
  4. 选图：组内被勾选次数最多的公共 preset（并列取 id 小者）；
     全空 → 从该模式 fallback_presets 随机选（无配置 → FALLBACK_PRESET）
"""
from dataclasses import dataclass, field
import random
import time

import mm_config


@dataclass
class QueueEntry:
    player_id: int
    char_id: int
    faction: int
    gamemode_id: int
    presets: tuple = ()          # 玩家选图（int preset id），空 = 不限
    enqueued_at: float = field(default_factory=time.monotonic)


@dataclass
class Match:
    players: list                # list[QueueEntry]
    preset_id: int
    ai_fill: bool = False        # True = 超时强制成组（AI 补位）
    mission_id: int = 0          # matchmaking 发号后回填


class MatchPool:
    """线程模型：单 asyncio 事件循环内使用（enqueue/try_form 均同步调用）。"""

    def __init__(self):
        self._by_mode = {}       # gamemode_id -> list[QueueEntry]（FIFO 序）
        self._by_player = {}     # player_id -> QueueEntry（快速去重/移除）

    # ---- 入池 / 出池 ----
    def enqueue(self, entry):
        """入池；同 player_id 已在池中则覆盖旧条目（防重复排队人数虚增）。"""
        self.remove(entry.player_id)
        self._by_mode.setdefault(entry.gamemode_id, []).append(entry)
        self._by_player[entry.player_id] = entry

    def remove(self, player_id):
        """取消匹配出池；返回被移除的 entry（不在池中返回 None）。"""
        entry = self._by_player.pop(player_id, None)
        if entry is None:
            return None
        lst = self._by_mode.get(entry.gamemode_id, [])
        if entry in lst:
            lst.remove(entry)
        return entry

    def get(self, player_id):
        return self._by_player.get(player_id)

    def waiting_count(self, gamemode_id=None):
        """池内等待人数；gamemode_id=None 时为全部模式合计。"""
        if gamemode_id is None:
            return len(self._by_player)
        return len(self._by_mode.get(gamemode_id, ()))

    # ---- 撮合 ----
    def try_form(self):
        """尝试从池中撮合一局（遍历各模式，任一模式凑够即成）。

        返回 Match 或 None（所有模式人数均不足 / 池空）。
        锚点 entry 一定进组（FIFO 公平）。
        """
        # 从等待人数最多的模式开始试（锚点公平 + 提高成局率）
        for mode_id, entries in sorted(self._by_mode.items(),
                                       key=lambda kv: -len(kv[1])):
            if not entries:
                continue
            mcfg = mm_config.gamemode_cfg(mode_id)
            group = self._collect_group(entries, mcfg['min_players'],
                                        mcfg['max_players'])
            if group is None:
                continue
            preset = self._pick_preset(group, mode_id)
            for e in group:
                self.remove(e.player_id)
            return Match(players=group, preset_id=preset, ai_fill=False)
        return None

    def force_form(self, cfg, gamemode_id):
        """超时 AI 补位：以该模式现有等待者强制成组（1 人也开）。"""
        entries = self._by_mode.get(gamemode_id, [])
        if not entries:
            return None
        max_p = max(1, int(cfg.get('max_players', 18)))
        group = self._collect_group(entries, 1, max_p)
        if group is None:
            return None
        preset = self._pick_preset(group, gamemode_id)
        for e in group:
            self.remove(e.player_id)
        return Match(players=group, preset_id=preset, ai_fill=True)

    # ---- 内部 ----
    def _collect_group(self, entries, min_p, max_p):
        """以最早 entry 为锚点收集同图兼容组；不足 min_p 返回 None。"""
        anchor = entries[0]
        group = [anchor]
        for e in entries[1:]:
            if len(group) >= max_p:
                break
            if self._compatible(anchor, e):
                group.append(e)
        if len(group) < min_p:
            return None
        return group

    @staticmethod
    def _compatible(a, b):
        """选图兼容：任一方空 = 通配；否则要求有交集。"""
        if not a.presets or not b.presets:
            return True
        return bool(set(a.presets) & set(b.presets))

    @staticmethod
    def _pick_preset(group, gamemode_id=None):
        """选图：组内多数票（并列取 id 小者）；全空 → 模式兜底池随机。

        无人勾图（quickplay / 只选模式）时从该模式 fallback_presets 随机
        选一张，模式无配置则回退全局 FALLBACK_PRESET。
        """
        votes = {}
        for e in group:
            for p in e.presets:
                votes[p] = votes.get(p, 0) + 1
        if votes:
            return min(votes.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        if gamemode_id is not None:
            fb = mm_config.gamemode_cfg(gamemode_id).get('fallback_presets')
            if fb:
                return random.choice(fb)
        return mm_config.FALLBACK_PRESET
