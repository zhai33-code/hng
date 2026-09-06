# -*- coding: utf-8 -*-
"""外服账号迁移导入器：采集外服数据 → 一次性校验 → 本地建号 → 全量导入。

调用方：web_server.py /api/import（HTTP 表单）、tools/foreign_import_cli.py（CLI）。

流程（run_import）：
  1. ForeignClient 连外服：login2 握手 → 订阅 playerview/soldierview →
     追发拉取请求（RequestReadCharacterStats 等）→ 采集 KVCS 推送 → dump 落盘
  2. 解析 player/character/装备/统计行（字段号与本服 KVCS 完全一致）
  3. 去重：foreign_imports 表按 (外服地址, 外服 playerId) 查重——
     同一外服账号只许导入一次
  4. 单事务：本地建号（等同注册步骤）→ 角色/装备/勋带 XP/统计/货币写入 →
     登记 foreign_imports；任一步失败整体回滚，不留半截账号
  5. 返回导入摘要

外服→本地 id 重映射：外服实例 id 是雪花 int64，本地按序列重新分配，
按依赖顺序（角色→武器→配件/弹药→皮肤→载具…）插入并回填引用列。

不导入的数据（本地按库内数据动态推导，导入反而会漂移）：
  player f37 / character f21 unlockBits（服务端按等级/勋带/已购物品重算）、
  商店目录（StoreDiscount/StoreCampaign）、战争/阵营（本地战争体系独立）。
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tools'))
from foreign_sync_probe import ForeignClient, LoginError          # noqa: E402
from messages import (_CS_SCALAR_KEYS, _WEAPON_FIELDS,            # noqa: E402
                      _VEHICLE_FIELDS, _GAMEMODE_FIELDS)

# 玩家等级（players.level_granted 起始值用，防等级奖励重复发放）
try:
    import wardb
except ImportError:      # tests 直接 import 本模块时 wardb 可能不可用
    wardb = None


class ForeignLoginError(Exception):
    """外服登录失败（code=login2_result 结果码）。"""

    def __init__(self, code, msg=''):
        super().__init__(msg or '外服登录失败（code=%d）' % code)
        self.code = int(code)


class ForeignCollectError(Exception):
    """外服数据采集失败（未收到 player 行 / 连接异常 / 超时）。"""


class AlreadyImportedError(Exception):
    """该外服账号已导入过（foreign_imports 已有记录）。"""


class UsernameTakenError(Exception):
    """本地用户名已被占用。"""


# ---------------------------------------------------------------- 字段提取
def _val(fields, num):
    """deep_parse 字段首值（原样：int 或 dict）。"""
    vals = (fields or {}).get(str(num))
    return vals[0] if vals else None


def _iv(fields, num, default=0):
    """字段值 → int（varint/int64/int32）。"""
    v = _val(fields, num)
    if v is None:
        return default
    if isinstance(v, int):
        return v
    if isinstance(v, dict):
        for k in ('signed', 'u64', 'u32', 'str'):
            if k in v:
                try:
                    return int(v[k])
                except (TypeError, ValueError):
                    continue
    return default


def _fv(fields, num, default=0.0):
    """字段值 → float（f32 优先）。"""
    v = _val(fields, num)
    if v is None:
        return default
    if isinstance(v, int):
        return float(v)
    if isinstance(v, dict):
        if 'f32' in v:
            return float(v['f32'])
        for k in ('signed', 'u64', 'u32'):
            if k in v:
                try:
                    return float(v[k])
                except (TypeError, ValueError):
                    continue
    return default


def _sv(fields, num, default=''):
    """字段值 → str。"""
    v = _val(fields, num)
    if isinstance(v, dict) and 'str' in v:
        return v['str']
    return default


def _rows(cli, tname):
    """采集到的某表 set 行（fields 视图列表）。"""
    return [r for r in cli.tables.get(tname, {}).get('set', []) if r.get('fields')]


# ---------------------------------------------------------------- 外服数据解析
def _parse_account(cli):
    """player 行 → 账号身份 + 货币（余额 = Bought + Earned - Used）。"""
    rows = _rows(cli, 'player')
    if not rows:
        raise ForeignCollectError('外服未推送 player 行（登录可能未完成）')
    f = rows[0]['fields']
    gold = _iv(f, 13) + _iv(f, 14) - _iv(f, 15)
    credits = _iv(f, 16) + _iv(f, 17) - _iv(f, 18)
    warfunds = _iv(f, 23) + _iv(f, 24) - _iv(f, 25)
    return {
        'foreign_pid': _iv(f, 1),
        'gamertag': _sv(f, 2),
        'level': max(1, _iv(f, 38, 1)),
        'tutorials_completed': _iv(f, 32),
        'language': _sv(f, 22, 'en') or 'en',
        'gold': gold, 'credits': credits, 'warfunds': warfunds,
    }


def _parse_characters(cli, foreign_pid):
    """character 行 → 待导入角色列表（软删行跳过；unlockBits 不导）。"""
    out = []
    for r in _rows(cli, 'character'):
        f = r['fields']
        if _iv(f, 14):                      # f14 markedDeleted
            continue
        if foreign_pid and _iv(f, 3) != foreign_pid:
            continue                        # 他人角色行（理论不出现，防御）
        out.append({
            'fid': _iv(f, 1),
            'name': _sv(f, 2, 'Soldier') or 'Soldier',
            'template': _iv(f, 4),
            'faction': _iv(f, 5),
            'rank': _iv(f, 13),
            'badge0': _iv(f, 10), 'badge1': _iv(f, 11),
            'uniform_fid': _iv(f, 17),      # UniformVariant 实例 id（重映射）
            'headgear_fid': _iv(f, 18),     # UniformPart 实例 id（重映射）
            'prev_template': _iv(f, 19),
            'head_id': _iv(f, 22),
            'on_leave': bool(_iv(f, 24)),
        })
    return out


def _parse_stats(cli):
    """ResponseReadCharacterStats → {外服角色id: {combat_xp, ribbons, stats}}。

    stats 键与 statistics_server._stat_db_key 落库格式一致：标量用统计名，
    维度统计用 w<模板id>/v<模板id>/gm<模式id> 前缀键。"""
    out = {}
    for rec in cli.responses.get('ResponseReadCharacterStats', []):
        entries = (rec.get('fields') or {}).get('1') or []
        for e in entries:
            if not isinstance(e, dict) or 'msg' not in e:
                continue
            em = e['msg']
            cid = _iv(em, 1)
            if not cid:
                continue
            info = {'combat_xp': 0, 'ribbons': {}, 'stats': {}}
            # f3 attributes：Ribbon {f1=模板id, f2=xp}
            for rb in em.get('3') or []:
                rm = rb.get('msg') if isinstance(rb, dict) else None
                if rm:
                    rid, xp = _iv(rm, 1), _fv(rm, 2)
                    if rid:
                        info['ribbons'][rid] = xp
            # f2 alltimeCharacterStatistics
            sv = _val(em, 2)
            sm = sv.get('msg') if isinstance(sv, dict) else None
            if sm:
                for fn in range(1, 50):
                    if fn == 36:            # proto 缺号
                        continue
                    if fn == 35:            # characterXp → combat_xp
                        info['combat_xp'] = _iv(sm, fn)
                        continue
                    keys = _CS_SCALAR_KEYS.get(fn)
                    if keys:
                        info['stats'][keys[0]] = _fv(sm, fn)
                for grp in sm.get('50') or []:
                    gm = grp.get('msg') if isinstance(grp, dict) else None
                    if gm:
                        vid = _iv(gm, 1)
                        for fn, name in _VEHICLE_FIELDS.items():
                            v = _fv(gm, fn)
                            if v:
                                info['stats']['v%d/%s' % (vid, name)] = v
                for grp in sm.get('51') or []:             # f51 武器维度
                    gm = grp.get('msg') if isinstance(grp, dict) else None
                    if gm:
                        wid = _iv(gm, 1)
                        for fn, name in _WEAPON_FIELDS.items():
                            v = _fv(gm, fn)
                            if v:
                                info['stats']['w%d/%s' % (wid, name)] = v
                for grp in sm.get('53') or []:             # f53 模式维度
                    gm = grp.get('msg') if isinstance(grp, dict) else None
                    if gm:
                        mid = _iv(gm, 1)
                        for fn, name in _GAMEMODE_FIELDS.items():
                            v = _fv(gm, fn)
                            if v:
                                info['stats']['gm%d/%s' % (mid, name)] = v
            prev = out.get(cid)
            if prev:                        # 多条响应合并
                prev['combat_xp'] = max(prev['combat_xp'], info['combat_xp'])
                prev['ribbons'].update(info['ribbons'])
                prev['stats'].update(info['stats'])
            else:
                out[cid] = info
    return out


# ---------------------------------------------------------------- 采集
async def collect_foreign(host, port, username, password, duration=15,
                          dump_base=None, log=None):
    """连外服采集全量数据并 dump 落盘，返回 ForeignClient（tables/responses）。"""
    log = log or (lambda *a: None)
    if not dump_base:
        dump_base = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 'tmp', 'foreign_dump')
    stamp = time.strftime('%Y%m%d_%H%M%S')
    out_dir = os.path.join(dump_base, '%s_%s' % (username, stamp))
    cli = ForeignClient(host, port, username, password, out_dir, log=log)
    try:
        await cli.login()
        await cli.subscribe()
        await cli.pull()
        await cli.collect(duration)
    except LoginError as e:
        raise ForeignLoginError(e.code, str(e))
    except (ConnectionError, TimeoutError, OSError) as e:
        raise ForeignCollectError('外服连接失败: %s' % e)
    finally:
        try:
            cli.dump()
        except Exception as e:             # dump 失败不影响主流程
            log('[import] dump 落盘失败（忽略）: %s' % e)
        if cli.w:
            cli.w.close()
    return cli, out_dir


# ---------------------------------------------------------------- 导入（单事务）
async def _import_into_db(store, conn, local_pid, account, chars, stats_map,
                          cli, log):
    """全部数据写入（在 store.transaction() 内调用，异常整体回滚）。"""
    skipped = {'refs': 0}
    counts = {}

    def remap(fid, mapping, what):
        """外服实例 id → 本地 id；找不到（引用已删对象）置 0 并计数。"""
        if not fid:
            return 0
        lid = mapping.get(fid)
        if lid is None:
            skipped['refs'] += 1
            log('[import] 丢失引用 %s %d → 置 0' % (what, fid))
            return 0
        return lid

    # ---- 1. 角色 ----
    char_map = {}
    for c in chars:
        st = stats_map.get(c['fid'])
        combat_xp = st['combat_xp'] if st else 0
        if not st and c['rank'] and wardb is not None:
            # 无统计响应时按军衔取阈值下限（XP 不会凭空多）
            try:
                lv = wardb._EXPERIENCE_LEVELS
                if 0 <= c['rank'] < len(lv):
                    combat_xp = lv[c['rank']]
            except Exception:
                pass
        cid = await conn.fetchval(
            'INSERT INTO characters (player_id, name, faction, template, combat_xp, '
            'combat_badge_0, combat_badge_1, is_on_leave, previous_template, head_id) '
            'VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id',
            local_pid, c['name'], c['faction'], c['template'], int(combat_xp),
            c['badge0'], c['badge1'], c['on_leave'], c['prev_template'],
            c['head_id'])
        char_map[c['fid']] = cid
    counts['characters'] = len(char_map)

    # ---- 2. 勋带 XP + 单角色统计 ----
    ribbon_rows, stat_rows = [], []
    for fid, cid in char_map.items():
        st = stats_map.get(fid)
        if not st:
            continue
        for rid, xp in st['ribbons'].items():
            ribbon_rows.append((cid, int(rid), float(xp)))
        for key, val in st['stats'].items():
            if val:
                stat_rows.append((cid, key, float(val)))
    if ribbon_rows:
        await conn.executemany(
            'INSERT INTO character_ribbon_xp (character_id, ribbon_template_id, '
            'total_xp) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING', ribbon_rows)
    if stat_rows:
        await conn.executemany(
            'INSERT INTO character_stats (character_id, stat_key, value) '
            'VALUES ($1,$2,$3) ON CONFLICT DO NOTHING', stat_rows)
    counts['ribbon_xp_rows'] = len(ribbon_rows)
    counts['stat_rows'] = len(stat_rows)

    # ---- 3. 军服迷彩 / 军装部件（角色引用列随后回填）----
    uv_map = {}
    for r in _rows(cli, 'UniformVariant'):
        f = r['fields']
        lid = await conn.fetchval(
            'INSERT INTO uniform_variants (player_id, uniform_variant_template_id, '
            'character_id) VALUES ($1,$2,$3) RETURNING id',
            local_pid, _iv(f, 4), remap(_iv(f, 3), char_map, 'uniform→char'))
        uv_map[_iv(f, 1)] = lid
    counts['uniform_variants'] = len(uv_map)

    up_map = {}
    for r in _rows(cli, 'UniformPart'):
        f = r['fields']
        lid = await conn.fetchval(
            'INSERT INTO uniform_parts (player_id, character_id, '
            'uniform_part_template_id, uniform_part_type) '
            'VALUES ($1,$2,$3,$4) RETURNING id',
            local_pid, remap(_iv(f, 3), char_map, 'part→char'),
            _iv(f, 4), _iv(f, 5))
        up_map[_iv(f, 1)] = lid
    counts['uniform_parts'] = len(up_map)

    for c in chars:
        cid = char_map[c['fid']]
        await conn.execute(
            'UPDATE characters SET uniform_variant_id = $1, headgear_id = $2 '
            'WHERE id = $3',
            remap(c['uniform_fid'], uv_map, 'char→uniform'),
            remap(c['headgear_fid'], up_map, 'char→headgear'), cid)

    # ---- 4. 武器 ----
    w_map = {}
    for r in _rows(cli, 'weapon'):
        f = r['fields']
        lid = await conn.fetchval(
            'INSERT INTO weapons (player_id, name, template_id, character_id, '
            'container_id, container_slot, condition, clip_count, auto_repair) '
            'VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id',
            local_pid, _sv(f, 2), _iv(f, 3),
            remap(_iv(f, 5), char_map, 'weapon→char'),
            remap(_iv(f, 10), char_map, 'weapon→container'),
            _iv(f, 11), _fv(f, 12), _iv(f, 14), _iv(f, 13))
        w_map[_iv(f, 1)] = lid
    counts['weapons'] = len(w_map)

    wv_map = {}
    for r in _rows(cli, 'WeaponVariant'):
        f = r['fields']
        lid = await conn.fetchval(
            'INSERT INTO weapon_variants (player_id, weapon_id, character_id, '
            'weapon_variant_template_id) VALUES ($1,$2,$3,$4) RETURNING id',
            local_pid, remap(_iv(f, 4), w_map, 'wvariant→weapon'),
            remap(_iv(f, 3), char_map, 'wvariant→char'), _iv(f, 5))
        wv_map[_iv(f, 1)] = lid
    counts['weapon_variants'] = len(wv_map)

    for r in _rows(cli, 'weapon'):
        f = r['fields']
        fid = _iv(f, 1)
        await conn.execute(
            'UPDATE weapons SET variant_id = $1 WHERE id = $2 AND $1 <> 0',
            remap(_iv(f, 15), wv_map, 'weapon→variant'), w_map.get(fid, 0))

    if _rows(cli, 'WeaponModifier'):
        await conn.executemany(
            'INSERT INTO weapon_modifiers (player_id, template_id, character_id, '
            'weapon_id, condition, auto_repair) VALUES ($1,$2,$3,$4,$5,$6)',
            [(local_pid, _iv(r['fields'], 2),
              remap(_iv(r['fields'], 4), char_map, 'mod→char'),
              remap(_iv(r['fields'], 5), w_map, 'mod→weapon'),
              _fv(r['fields'], 6), _iv(r['fields'], 7))
             for r in _rows(cli, 'WeaponModifier')])
    counts['weapon_modifiers'] = len(_rows(cli, 'WeaponModifier'))

    if _rows(cli, 'WeaponAmmo'):
        await conn.executemany(
            'INSERT INTO weapon_ammo (player_id, ammo_template_id, character_id, '
            'weapon_id, slot_index, condition, auto_repair) '
            'VALUES ($1,$2,$3,$4,$5,$6,$7)',
            [(local_pid, _iv(r['fields'], 2),
              remap(_iv(r['fields'], 4), char_map, 'ammo→char'),
              remap(_iv(r['fields'], 5), w_map, 'ammo→weapon'),
              _iv(r['fields'], 6), _fv(r['fields'], 7), _iv(r['fields'], 8))
             for r in _rows(cli, 'WeaponAmmo')])
    counts['weapon_ammo'] = len(_rows(cli, 'WeaponAmmo'))

    t_map = {}
    for r in _rows(cli, 'TracerEffect'):
        f = r['fields']
        lid = await conn.fetchval(
            'INSERT INTO tracer_effects (player_id, tracer_template_id, '
            'character_id, weapon_id, is_equipped) '
            'VALUES ($1,$2,$3,$4,$5) RETURNING id',
            local_pid, _iv(f, 2),
            remap(_iv(f, 4), char_map, 'tracer→char'),
            remap(_iv(f, 5), w_map, 'tracer→weapon'), bool(_iv(f, 6)))
        t_map[_iv(f, 1)] = lid
    counts['tracer_effects'] = len(t_map)
    for r in _rows(cli, 'TracerEffect'):
        f = r['fields']
        if _iv(f, 6) and _iv(f, 5):
            wid = w_map.get(_iv(f, 5))
            if wid:
                await conn.execute(
                    'UPDATE weapons SET tracer_effect_id = $1 WHERE id = $2',
                    t_map[_iv(f, 1)], wid)

    # ---- 5. 载具 ----
    v_map = {}
    for r in _rows(cli, 'Vehicle'):
        f = r['fields']
        lid = await conn.fetchval(
            'INSERT INTO vehicles (player_id, vehicle_variant_template_id, '
            'character_id, equipped_in_slot, condition, auto_repair) '
            'VALUES ($1,$2,$3,$4,$5,$6) RETURNING id',
            local_pid, _iv(f, 3),
            remap(_iv(f, 5), char_map, 'vehicle→char'),
            _iv(f, 6), _fv(f, 7), _iv(f, 8))
        v_map[_iv(f, 1)] = lid
    counts['vehicles'] = len(v_map)

    vv_map = {}
    for r in _rows(cli, 'VehicleVariant'):
        f = r['fields']
        lid = await conn.fetchval(
            'INSERT INTO vehicle_variants (player_id, vehicle_id, character_id, '
            'vehicle_variant_template_id) VALUES ($1,$2,$3,$4) RETURNING id',
            local_pid, remap(_iv(f, 4), v_map, 'vvariant→vehicle'),
            remap(_iv(f, 3), char_map, 'vvariant→char'), _iv(f, 5))
        vv_map[_iv(f, 1)] = lid
    counts['vehicle_variants'] = len(vv_map)

    for r in _rows(cli, 'Vehicle'):
        f = r['fields']
        fid = _iv(f, 1)
        await conn.execute(
            'UPDATE vehicles SET variant_id = $1 WHERE id = $2 AND $1 <> 0',
            remap(_iv(f, 9), vv_map, 'vehicle→variant'), v_map.get(fid, 0))

    if _rows(cli, 'VehicleUpgrade'):
        await conn.executemany(
            'INSERT INTO vehicle_upgrades (player_id, upgrade_template_id, '
            'vehicle_id, character_id, equipped_slot_id, condition, auto_repair) '
            'VALUES ($1,$2,$3,$4,$5,$6,$7)',
            [(local_pid, _iv(r['fields'], 2),
              remap(_iv(r['fields'], 3), v_map, 'vup→vehicle'),
              remap(_iv(r['fields'], 6), char_map, 'vup→char'),
              _iv(r['fields'], 4), _fv(r['fields'], 7), _iv(r['fields'], 8))
             for r in _rows(cli, 'VehicleUpgrade')])
    counts['vehicle_upgrades'] = len(_rows(cli, 'VehicleUpgrade'))

    if _rows(cli, 'VehicleAmmo'):
        await conn.executemany(
            'INSERT INTO vehicle_ammo (player_id, ammo_template_id, vehicle_id, '
            'character_id, equipped_storage_id, slot_index, condition, '
            'auto_repair) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)',
            [(local_pid, _iv(r['fields'], 2),
              remap(_iv(r['fields'], 3), v_map, 'vammo→vehicle'),
              remap(_iv(r['fields'], 7), char_map, 'vammo→char'),
              _iv(r['fields'], 4), _iv(r['fields'], 5),
              _fv(r['fields'], 8), _iv(r['fields'], 9))
             for r in _rows(cli, 'VehicleAmmo')])
    counts['vehicle_ammo'] = len(_rows(cli, 'VehicleAmmo'))

    # ---- 6. 账号级：货币 / 教程 / 等级奖励防重发 ----
    await conn.execute(
        'UPDATE players SET gold = $1, credits = $2, warfunds = $3, '
        'tutorials_completed = $4, language_code = $5, level_granted = $6 '
        'WHERE id = $7',
        int(account['gold']), int(account['credits']),
        int(account['warfunds']), account['tutorials_completed'],
        account['language'][:8], account['level'], local_pid)

    counts['skipped_refs'] = skipped['refs']
    return counts


# ---------------------------------------------------------------- 主入口
async def run_import(store, host, port, foreign_username, foreign_password,
                     local_username, local_password, collect_duration=15,
                     dump_base=None, log=None):
    """完整迁移：采集 → 去重校验 → 建号+导入（单事务）→ 登记。

    返回摘要 dict；失败抛类型化异常（见模块顶部异常类）。"""
    log = log or (lambda *a: None)
    server_key = '%s:%d' % (host, int(port))

    # 1. 采集（网络阶段，不占数据库）
    cli, dump_dir = await collect_foreign(
        host, port, foreign_username, foreign_password,
        duration=collect_duration, dump_base=dump_base, log=log)

    # 2. 解析
    account = _parse_account(cli)
    if not account['foreign_pid']:
        raise ForeignCollectError('外服 player 行缺少 id，无法去重')
    chars = _parse_characters(cli, account['foreign_pid'])
    stats_map = _parse_stats(cli)
    log('[import] 外服账号 %s（id=%d）：角色 %d，统计条目 %d'
        % (account['gamertag'], account['foreign_pid'], len(chars),
           len(stats_map)))

    # 3. 一次性导入校验（DB 唯一约束兜底并发）
    existing = await store.get_foreign_import(server_key,
                                              account['foreign_pid'])
    if existing:
        raise AlreadyImportedError(
            '外服账号 %s 已于 %s 导入过（本地账号 id=%d）'
            % (account['gamertag'],
               existing['created_at'].strftime('%Y-%m-%d %H:%M')
               if existing.get('created_at') else '?',
               existing['local_player_id']))

    # 4. 建号 + 导入 + 登记（单事务，任一步失败整体回滚）
    async with store.transaction() as conn:
        local_pid = await conn.fetchval(
            'INSERT INTO players (username, password) VALUES ($1, $2) '
            'ON CONFLICT (username) DO NOTHING RETURNING id',
            local_username, local_password)
        if local_pid is None:
            raise UsernameTakenError('用户名 %s 已存在' % local_username)
        counts = await _import_into_db(store, conn, local_pid, account,
                                       chars, stats_map, cli, log)
        await store.record_foreign_import(
            conn, server_key, account['foreign_pid'], account['gamertag'],
            local_pid, dump_path=dump_dir,
            characters=counts.get('characters', 0),
            weapons=counts.get('weapons', 0),
            vehicles=counts.get('vehicles', 0))
    log('[import] 导入完成：%s → 本地 %s（id=%d）'
        % (account['gamertag'], local_username, local_pid))
    return {
        'local_player_id': local_pid,
        'username': local_username,
        'foreign_gamertag': account['gamertag'],
        'foreign_player_id': account['foreign_pid'],
        'dump_dir': dump_dir,
        'currencies': {'gold': account['gold'],
                       'credits': account['credits'],
                       'warfunds': account['warfunds']},
        'counts': counts,
    }
