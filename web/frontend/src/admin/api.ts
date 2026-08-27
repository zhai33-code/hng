// 管理端请求封装：fetch（同源 Cookie 会话）+ 超时 + 统一错误结构 + 401 登出
// 写操作统一带 X-Requested-With（后端 write_guard CSRF 依赖）
export interface ApiError {
  status: number
  message: string
}

// 开发期走 vite 代理 /admin/api -> 127.0.0.1:8080（rewrite 去 /admin）；
// 生产同域部署（FastAPI 托管 admin.html），直接 /api。
export const API_BASE = import.meta.env.DEV ? '/admin/api' : '/api'

export class HttpError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

let onUnauthorized: (() => void) | null = null

/** 注册 401 回调（router 注入：清会话并跳登录页）。 */
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn
}

export async function http<T = unknown>(
  method: 'GET' | 'POST',
  path: string,
  body?: unknown,
  timeoutMs = 15000,
  opts: { isLogin?: boolean } = {},
): Promise<T> {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeoutMs)
  try {
    const headers: Record<string, string> = {}
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json'
      // CSRF 自定义头：跨站表单无法携带（write_guard 校验）
      headers['X-Requested-With'] = 'XMLHttpRequest'
    }
    const res = await fetch(API_BASE + path, {
      method,
      headers,
      credentials: 'same-origin',
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: ctrl.signal,
    })
    let data: any = null
    try {
      data = await res.json()
    } catch {
      /* 非 JSON 响应 */
    }
    if (res.status === 401) {
      // 登录接口的 401 = 凭据错误（后端带 detail 文案）：
      // 直接抛给登录页弹窗展示，不触发全局"会话过期"登出逻辑
      if (!opts.isLogin) onUnauthorized?.()
      throw new HttpError(401, data?.detail || '会话已过期，请重新登录')
    }
    if (!res.ok) {
      throw new HttpError(res.status, data?.detail || `请求失败 (${res.status})`)
    }
    return data as T
  } catch (e: unknown) {
    if (e instanceof HttpError) throw e
    if ((e as Error)?.name === 'AbortError') {
      throw new HttpError(0, '请求超时，请检查网络后重试')
    }
    throw new HttpError(0, '网络错误')
  } finally {
    clearTimeout(timer)
  }
}

export const get = <T = unknown>(path: string, timeoutMs?: number) =>
  http<T>('GET', path, undefined, timeoutMs)
export const post = <T = unknown>(path: string, body?: unknown,
                                  opts?: { isLogin?: boolean }) =>
  http<T>('POST', path, body, undefined, opts)
