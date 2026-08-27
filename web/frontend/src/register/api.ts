// 注册应用 fetch 封装：JSON 序列化 + 超时 + 统一错误结构
export interface ApiError {
  status: number
  message: string
}

export async function apiPost<T = unknown>(url: string, body: unknown, timeoutMs = 10000): Promise<T> {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeoutMs)
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    })
    let data: any = null
    try {
      data = await res.json()
    } catch {
      /* 非 JSON 响应 */
    }
    if (!res.ok) {
      const err: ApiError = { status: res.status, message: data?.error || `请求失败 (${res.status})` }
      throw err
    }
    return data as T
  } catch (e: any) {
    if (e?.name === 'AbortError') {
      throw { status: 0, message: '请求超时，请检查网络后重试' } satisfies ApiError
    }
    throw e
  } finally {
    clearTimeout(timer)
  }
}
