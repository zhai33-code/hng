# -*- coding: utf-8 -*-
"""封禁踢下线：管理端 web 落库封禁后 NOTIFY hng_ban_player（payload=
player_id，事务 COMMIT 后送达），游戏服监听并关闭该玩家全部 TCP 连接
（Flash UI + hng.exe 双连接同踢）。连接关闭由 handle_tcp 统一断线清理
接管（登出广播 / 排队释放 / 匹配出池）。解封不踢——只影响后续登录，
登录拦截在 handlers/auth.handle_login2_response（91/92）。"""
import asyncio

from state import STORE, TCP_SESSIONS, log

BAN_NOTIFY_CHANNEL = 'hng_ban_player'


async def kick_player_sessions(player_id, username=None):
    """关闭该玩家的全部在线 TCP 会话（pid / 用户名任一匹配），返回踢掉数。"""
    kicked = 0
    for sess in list(TCP_SESSIONS):
        if sess.player_id == player_id or \
                (username and sess.username == username):
            kicked += 1
            log(f'[ban] kicking session user={sess.username!r} '
                f'pid={sess.player_id}')
            try:
                sess.writer.close()
            except Exception as e:
                log(f'[ban] session close failed: {type(e).__name__}: {e}')
    return kicked


async def _kick_by_id(player_id):
    username = None
    try:
        username = await STORE.get_username(int(player_id))
    except Exception as e:
        log(f'[ban] get_username failed: {e}')
    n = await kick_player_sessions(int(player_id), username)
    log(f'[ban] player {player_id} ({username!r}) banned, '
        f'kicked {n} session(s)')


def _on_ban_notify(conn, pid, channel, payload):
    """NOTIFY 回调（asyncpg 在连接的事件循环里同步调用，只做调度）。"""
    pid = (payload or '').strip()
    if pid.isdigit():
        asyncio.ensure_future(_kick_by_id(int(pid)))


async def _ban_listener_loop():
    """LISTEN 连接守护：断线自动重连（5s 退避），同 mail listener 模式。"""
    import asyncpg
    while True:
        conn = None
        try:
            conn = await asyncpg.connect(STORE.dsn)
            await conn.add_listener(BAN_NOTIFY_CHANNEL, _on_ban_notify)
            log(f'[ban] listener ready (channel={BAN_NOTIFY_CHANNEL})')
            # 挂住直到连接关闭（断网/服务端重启后退出 → 重连）
            while not conn.is_closed():
                await asyncio.sleep(5)
            log('[ban] listener connection lost, reconnecting')
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f'[ban] listener died ({e}), retry in 5s')
        finally:
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    pass
        await asyncio.sleep(5)


def start_ban_listener():
    asyncio.ensure_future(_ban_listener_loop())
