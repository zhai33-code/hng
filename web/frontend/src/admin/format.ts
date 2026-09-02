// 共享格式化：时间统一转浏览器本地时区显示（国内管理员即北京时间）。
// 后端返回：TIMESTAMPTZ -> 带时区 ISO 串；epoch 秒 -> UTC ISO 串（battle_start_at）。
export function fmtTime(s: string | number | null | undefined): string {
  if (s == null || s === '') return '-'
  const d = typeof s === 'number' ? new Date(s * 1000) : new Date(s)
  if (isNaN(d.getTime())) return '-'
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
    `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

export function fmtNum(n: number | null | undefined): string {
  return (n ?? 0).toLocaleString('en-US')
}
