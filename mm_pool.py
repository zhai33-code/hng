# -*- coding: utf-8 -*-
"""匹配池（纯逻辑、无 IO）：按 gamemodeId 分池 + 按玩家选图交集撮合。

数据来源（JoinMatchMakingRequest，反编译确认）：
  f7 gamemodeId —— 决定进入哪个模式的池
  f5 stagedMissionPresetList（repeated int64）—— 玩家勾选的地图 preset，
  空 = 不限图（通配）

撮合规则：
  1. FIFO：取池中最早的 entry 为锚点
  2. 按 FIFO 收集与当前整组仍有共同选图的同模式 entry（空 = 通配）
  3. 凑够 min_players 即成组（收编到 max_players，剩余留池；锚点所在
     整队全员进组、可超 max_players——绝不截断拆队）
  4. 选图：只从整组共同 preset 中选择（并列取 id 小者）；
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
    group_key: int = 0           # 整队入池键（=squad_id）；0 = 单人
    battle_id: int = 0           # war 会战局 id（M2）；0 = 普通撮合
    vehicle_cats: tuple = ()     # 已装备载具资源类别并集（成局覆盖保证）
    char_cat: int = 0            # 角色自身士兵类别（wardb.
                                 # character_resource_category；伞兵=4 等，
                                 # 池缺该类别引擎建小队失败→开局判负）


@dataclass
class Match:
    players: list                # list[QueueEntry]
    preset_id: int
    ai_fill: bool = False        # True = 超时强制成组（AI 补位）
    mission_id: int = 0          # matchmaking 发号后回填
    battle_id: int = 0           # war 会战局（M2）：mission_id = record id


class MatchPool:
    """线程模型：单 asyncio 事件循环内使用（enqueue/try_form 均同步调用）。"""

    def __init__(self):
        self._by_mode = {}       # gamemode_id -> list[QueueEntry]（FIFO 序）
        self._by_player = {}     # player_id -> QueueEntry（快速去重/移除）
        # war 会战成局门控（可选 callable(battle_id)->bool）：False = 该
        # 会战准备期未到，try_form_war 跳过（matchmaking 装配，客户端
        # battle_ready_in 集结倒计时口径）；None = 不门控（纯逻辑单测）
        self.war_ready = None

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

    def waiting_counts(self):
        """各模式当前等待人数 {gamemode_id: n}（快速匹配选池解析用）。"""
        return {m: len(lst) for m, lst in self._by_mode.items() if lst}

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
            # ★ war 会战桶条目（battle_id>0）只由 try_form_war 成局：
            #   被普通撮合撮走会丢 battle_id（Match 无此字段来源），
            #   _start_match 落成 staged 随机局（发新 mission id、攻守
            #   掷硬币、镜像随机兵力池）——单人排队也能成局（gamemode 10
            #   的 min_players 可被 MM_MIN_PLAYERS 压到 1）。
            entries = [e for e in entries if not e.battle_id]
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
        # war 会战桶条目不 AI 补位（同 try_form：battle_id 丢失会成随机局）
        entries = [e for e in entries if not e.battle_id]
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

    def try_form_war(self):
        """war 会战撮合（M2，gamemode 10）：按 battle_id 分桶，桶内攻守
        双方阵营各≥1 即成局（官服口径：交战双方都有真人排队才开局）。

        入池侧（mm_handler._handle_join war 路由）已保证只有该会战
        攻/守两阵营的玩家能入桶，桶内凑齐两阵营即可。war 局无 AI 补位
        （gamemode 10 ai_fill_after=0，凑不齐继续等）。
        ★ mm_config.WAR_SOLO_FORM=1（单人测试）时放宽为桶内≥1 人即成局
        （单方排队立即开局，对面留空，后续真人 late join 补入）。
        Match.preset_id 恒 0：地图/阵营来自 mission_records 行，
        _start_match war 分支按 battle_id 反查。
        """
        entries = self._by_mode.get(10, [])
        if not entries:
            return None
        buckets = {}
        for e in entries:
            if e.battle_id:
                buckets.setdefault(int(e.battle_id), []).append(e)
        min_factions = 1 if mm_config.WAR_SOLO_FORM else 2
        max_p = max(1, int(mm_config.gamemode_cfg(10).get(
            'max_players', 18)))
        for battle_id, entries in buckets.items():
            if self.war_ready is not None and not self.war_ready(battle_id):
                continue                  # 准备期未到（集结倒计时中）
            group = self._collect_war_group(entries, max_p, min_factions)
            if group is None:
                continue
            factions = {int(e.faction) for e in group}
            if len(factions) < min_factions:
                continue
            for e in group:
                self.remove(e.player_id)
            return Match(players=group, preset_id=0, ai_fill=False,
                         battle_id=battle_id)
        return None

    @staticmethod
    def _atomic_units(entries):
        """按 FIFO 顺序把散人/整队整理成不可拆分的入池单元。"""
        units = []
        seen_groups = set()
        for e in entries:
            if not e.group_key:
                units.append([e])
                continue
            if e.group_key in seen_groups:
                continue
            seen_groups.add(e.group_key)
            units.append([x for x in entries
                          if x.group_key == e.group_key])
        return units

    def _collect_war_group(self, entries, max_p, min_factions):
        """从会战桶取不超过上限的一局，并保持真实小队原子性。

        双方真人模式先为最早等待方寻找最早的对方阵营单元，再按 FIFO
        填满剩余席位。这样不会因队首同阵营人数过多而把对方挤到下一局。
        """
        units = self._atomic_units(entries)
        if not units:
            return None

        first = units[0]
        if len(first) > max_p:
            return None                  # 非法超大小队不能突破战局硬上限
        selected = [first]
        selected_ids = {id(first)}
        factions = {int(e.faction) for e in first}

        if len(factions) < min_factions:
            opponent = next(
                (u for u in units[1:]
                 if ({int(e.faction) for e in u} - factions)
                 and len(first) + len(u) <= max_p),
                None)
            if opponent is None:
                return None
            selected.append(opponent)
            selected_ids.add(id(opponent))
            factions.update(int(e.faction) for e in opponent)

        used = sum(len(u) for u in selected)
        for unit in units[1:]:
            if id(unit) in selected_ids:
                continue
            if used + len(unit) > max_p:
                continue
            selected.append(unit)
            used += len(unit)

        chosen = {id(e) for unit in selected for e in unit}
        return [e for e in entries if id(e) in chosen]

    def war_bucket(self, battle_id):
        """war 会战桶内等待条目（FIFO 序）：详情简报"战局玩家列表"数据源
        （本会战已排队、尚未成局的玩家；battle_id=0 / 无条目返回 ()）。"""
        if not battle_id:
            return ()
        return tuple(e for e in self._by_mode.get(10, ())
                     if int(e.battle_id) == int(battle_id))

    # ---- 内部 ----
    def _collect_group(self, entries, min_p, max_p):
        """以最早 entry 为锚点收集同图兼容组；不足 min_p 返回 None。

        整队（group_key 非 0）原子进出：锚点所在队先全员进组（同队
        选图由入池方保证一致，跳过兼容判断）；其他队要么整队收编
        （全队兼容且装得下），要么整队跳过——绝不拆队。"""
        anchor = entries[0]
        group = [anchor]
        if anchor.group_key:
            # ★ 锚点所在队全员进组，不受 max_p 截断（绝不拆队）：
            #   截断会把落选成员留在池中单独成局，而 _start_match 已把
            #   全队 DB 状态置 InBattle，留守者会被 _squad_eligible_members
            #   过滤（status 非 2/3）→ 永久卡在匹配中（拆队根因）。
            #   队规模可超 max_p：整队原子性优先于人数上限，后续散人
            #   收编按 len(group) >= max_p 自然停止。
            for e in entries[1:]:
                if e.group_key == anchor.group_key and e not in group:
                    group.append(e)
        if self._common_presets(group) == set():
            return None
        for e in entries[1:]:
            if len(group) >= max_p:
                break
            if e in group:
                continue
            if e.group_key:
                mates = [x for x in entries
                         if x.group_key == e.group_key and x not in group]
                if not mates or len(group) + len(mates) > max_p:
                    continue           # 装不下整队 → 跳过（不拆队）
                if self._common_presets(group + mates) == set():
                    continue
                group.extend(mates)
            elif self._common_presets(group + [e]) != set():
                group.append(e)
        if len(group) < min_p:
            return None
        return group

    @staticmethod
    def _common_presets(group):
        """整组共同可接受地图；None 表示全员通配，空集合表示无交集。"""
        constrained = [set(e.presets) for e in group if e.presets]
        if not constrained:
            return None
        common = constrained[0]
        for presets in constrained[1:]:
            common &= presets
        return common

    @staticmethod
    def _pick_preset(group, gamemode_id=None):
        """选图：只从整组共同交集中选；并列取 id 小者。

        无人勾图（quickplay / 只选模式）时从该模式 fallback_presets 随机
        选一张，模式无配置则回退全局 FALLBACK_PRESET。
        """
        common = MatchPool._common_presets(group)
        votes = {}
        for e in group:
            for p in e.presets:
                if common is None or p in common:
                    votes[p] = votes.get(p, 0) + 1
        if votes:
            return min(votes.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        if gamemode_id is not None:
            fb = mm_config.gamemode_cfg(gamemode_id).get('fallback_presets')
            if fb:
                return random.choice(fb)
        return mm_config.FALLBACK_PRESET
