"""statistics_client.dll 统计服务器（TCP 15115）。

战斗内勋带升级弹窗（AddUnlockedRibbon）依赖 player.dll 的会话勋带数据
（character 对象 a2[7]），该数据**仅**由 statistics_client.dll 的 GetTotals
响应初始化（逆向结论见 tmp/ida_out/RIBBON_POPUP_INVESTIGATION.md）：
  statistics_client.dll --TCP--> 统计服务器 GetTotals 响应
    -> sub_18038D550 解析 -> character[7] = set_ribbon_state_from_server(...)
    -> sub_180390E00 战斗中累加 XP -> 升级 -> Flash AddUnlockedRibbon 弹窗

连接握手（comm_start sub_18000A050 逆向，2026-08-26 补）：
  C->S: [u32 LE 14]["GIVEITTOMENOW\0"]（长度前缀 + 14 字节 = 13 字符 + NUL）
  S->C: ["YOUGOTITTODAY\0"]（14 字节裸串，无前缀）+ [u32 会话值]（客户端存 a1+104）
  握手失败客户端静默重连 10 次（每次 1s sleep）→ ~30s 后报
  "#error3 _GetTotalsFromStatisticsServerNotify as == 0, ErrorCode: 2"

协议（statistics_client.dll 逆向，vt6_GetTotals sub_18000ADF0 实锤）：
  帧:  [u32 LE len][u32 LE crc32][payload]，len = payload 长度 + 4，
       crc32 = zlib CRC32(len 的 4 字节小端 || payload 字节)  <- CRC 覆盖长度字段
  C->S payload：GetTotals(5)/CreateSession(3) 等为明文（send_buffer 直写），
                周期上报 type 6 经 msg_envelope(deflate) 压缩 → 收到后先尝试解压。
  S->C payload 为 zlib 压缩（客户端 reader_ctor inflateInit 解压）。
  消息（payload 解压后首字节）:
    3 CreateSession   [u8 3][i64 sessionId]           无响应
    4 FinalizeSession [u8 4][i64 sessionId]           无响应
    5 GetTotals       见 _parse_get_totals            同步阻塞读响应
    6 统计上报（压缩）                                  战斗服周期 flush，忽略
    7 CreateRandomStat [u8 7][i64 ?]                  同步读响应（同 GetTotals 格式）
    8 bye                                              关连接

  GetTotals 请求: [u8 5][u32 n][i64xn charIds][i64 ?][u32 a5][a5x32B][u32 a7][a7x16B]
  GetTotals 响应（解压后）:
    [i64 sessionId][u32 numChars]
    每角色: [i64 charId][u32 numStats][u32 numRibbons]
            + numStats  x 28B: [u32 statId][i64][i64][i64]
            + numRibbonsx 28B: [i64 ribbonTemplateId][f64 currentValue]
                                [u32 currentLevel][f64 nextValue]
    （两个数组顺序内联，客户端自行分配指针；等级为 1 基，客户端存 level-1）

客户端注入（launch_hng.ps1）：/R "statserver=127.0.0.1" /R "statserverport=15115"
"""
import asyncio
import struct
import zlib

from state import STORE, log
from wardb import _RIBBON_LEVEL_XP, _ribbon_level_from_xp

STATS_PORT = 15115


def _frame(payload: bytes) -> bytes:
    """S->C 帧：payload 需 zlib 压缩。"""
    data = zlib.compress(payload)
    ln = len(data) + 4
    crc = zlib.crc32(struct.pack('<I', ln) + data) & 0xFFFFFFFF
    return struct.pack('<II', ln, crc) + data


def _parse_get_totals(payload: bytes):
    """解析 GetTotals 请求，返回 (charIds, echo_i64)。"""
    off = 1
    n, = struct.unpack_from('<I', payload, off)
    off += 4
    ids = []
    for i in range(n):
        ids.append(struct.unpack_from('<q', payload, off)[0])
        off += 8
    echo = 0
    if off + 8 <= len(payload):
        echo = struct.unpack_from('<q', payload, off)[0]
        off += 8
    return ids, echo


async def _ribbons_for_char(char_id: int):
    """从 character_ribbon_xp 取该角色勋带状态 [(rid, currentValue, level, nextValue)]。"""
    if STORE is None:
        return []
    try:
        xps = await STORE.get_ribbon_xp_by_char(int(char_id))
    except Exception as e:
        log(f'[stats] ribbon_xp lookup failed (char={char_id}): {e}')
        return []
    out = []
    for rid, total in sorted(xps.items()):
        if total <= 0:
            continue
        lv = _ribbon_level_from_xp(total)
        nxt = _RIBBON_LEVEL_XP.get(lv + 1, _RIBBON_LEVEL_XP.get(lv, 0))
        out.append((int(rid), float(total), int(lv), float(nxt)))
    return out


def _build_totals(session_id: int, char_ribbons) -> bytes:
    """char_ribbons: [(charId, [(rid, cur, lv, next), ...]), ...]"""
    out = struct.pack('<qI', session_id, len(char_ribbons))
    for cid, ribbons in char_ribbons:
        out += struct.pack('<qII', int(cid), 0, len(ribbons))
        for rid, cur, lv, nxt in ribbons:
            out += struct.pack('<qdId', int(rid), float(cur), int(lv), float(nxt))
    return out


async def _handle_conn(reader, writer):
    peer = writer.get_extra_info('peername')
    try:
        # ---- GIVEITTOMENOW 握手（comm_start：失败客户端会静默重连 10 次）----
        # 注意：串是 13 字符 + NUL = 14 字节（DLL write/read/memcmp 均按 14 算）
        pre = await reader.readexactly(4)
        n, = struct.unpack('<I', pre)
        hs = await reader.readexactly(n) if n else b''
        if n != 14 or not hs.startswith(b'GIVEITTOMENOW'):
            log(f'[stats] bad handshake from {peer}: n={n} {hs!r}')
            return
        # 回复：14 字节裸串（含 NUL，无长度前缀）+ 4 字节会话值（客户端存 a1+104）
        writer.write(b'YOUGOTITTODAY\x00' + struct.pack('<I', 1))
        await writer.drain()
        log(f'[stats] handshake OK from {peer}')

        while True:
            hdr = await reader.readexactly(8)
            ln, crc = struct.unpack('<II', hdr)
            if ln < 4 or ln > 16 * 1024 * 1024:
                break
            payload = await reader.readexactly(ln - 4) if ln > 4 else b''
            if (zlib.crc32(hdr[:4] + payload) & 0xFFFFFFFF) != crc:
                log(f'[stats] CRC mismatch from {peer}, dropping frame')
                break
            if not payload:
                break
            # type 6 周期上报是压缩的（msg_envelope deflate）；其余明文
            if payload[0] != 0x78:
                mtype = payload[0]
            else:
                try:
                    payload = zlib.decompress(payload)
                    mtype = payload[0]
                except zlib.error:
                    mtype = payload[0]
            if mtype == 8:                       # bye
                break
            if mtype == 3:                       # CreateSession
                sid = struct.unpack_from('<q', payload, 1)[0] if len(payload) >= 9 else 0
                log(f'[stats] CreateSession sid={sid} from {peer}')
                continue
            if mtype == 4:                       # FinalizeSession
                sid = struct.unpack_from('<q', payload, 1)[0] if len(payload) >= 9 else 0
                log(f'[stats] FinalizeSession sid={sid} from {peer}')
                continue
            if mtype == 5:                       # GetTotals（需立即响应）
                char_ids, echo = _parse_get_totals(payload)
                entries = []
                for cid in char_ids:
                    ribbons = await _ribbons_for_char(cid)
                    entries.append((cid, ribbons))
                    log(f'[stats] GetTotals char={cid} -> {len(ribbons)} ribbons '
                        f'({", ".join(f"r{r[0]}:lv{r[2]}" for r in ribbons[:6])})')
                writer.write(_frame(_build_totals(echo or 1, entries)))
                await writer.drain()
                continue
            if mtype == 7:                       # CreateRandomStat（读响应，回空表）
                writer.write(_frame(_build_totals(1, [])))
                await writer.drain()
                continue
            log(f'[stats] unknown msg type {mtype} from {peer}')
    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass
    except Exception as e:
        log(f'[stats] conn error ({peer}): {type(e).__name__}: {e}')
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def start():
    server = await asyncio.start_server(_handle_conn, '0.0.0.0', STATS_PORT)
    log(f'Statistics server listening on 0.0.0.0:{STATS_PORT} '
        f'(statistics_client.dll GetTotals)')
    async with server:
        await server.serve_forever()