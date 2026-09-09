# 重启管理后台：停止占用 8080 的旧进程 → 复用 start_admin.ps1 拉起新进程
# 手动用：powershell -ExecutionPolicy Bypass -File web\restart_admin.ps1
$ErrorActionPreference = 'Continue'
$hng_web = $PSScriptRoot

# 1. 停止监听 8080 的旧进程（可能为管理员权限启动）
$hng_conns = Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue
$hng_pids = $hng_conns | Select-Object -ExpandProperty OwningProcess -Unique
foreach ($hng_p in $hng_pids) {
    Stop-Process -Id $hng_p -Force -ErrorAction SilentlyContinue
    Write-Host "[*] Stopped old admin PID $hng_p"
}
Start-Sleep -Seconds 1

# 2. 复用启动脚本（隐藏窗口防 QuickEdit 冻结 + 日志收进 logs\）
& (Join-Path $hng_web 'start_admin.ps1')

# 3. 验证
Start-Sleep -Seconds 2
$hng_conns = Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue
if ($hng_conns) {
    Write-Host '[+] Admin backend listening:' -ForegroundColor Green
    $hng_conns | Select-Object LocalPort, OwningProcess | Format-Table -AutoSize
} else {
    Write-Host '[!] Admin backend failed to start' -ForegroundColor Red
    Get-Content (Join-Path (Split-Path $hng_web -Parent) 'logs\admin_stderr_*.log') -Tail 8 -ErrorAction SilentlyContinue
}
