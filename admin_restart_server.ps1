# 提权重启 realtest_server：停止占用 80/15110 的旧进程 → 启动新代码
# 路径基于脚本所在目录：包内布局 venv 在本目录，开发布局 venv 在上一级（hng-server-re\.venv）
# 变量名带 hng_ 前缀，避免被 powershell profile 变量污染（fnm 等工具）
$ErrorActionPreference = 'Continue'
$hng_root = $PSScriptRoot
$hng_py = Join-Path $hng_root '.venv\Scripts\python.exe'
if (-not (Test-Path $hng_py)) { $hng_py = Join-Path (Split-Path $hng_root -Parent) '.venv\Scripts\python.exe' }
if (-not (Test-Path $hng_py)) { $hng_py = 'python' }

# 1. 停止监听 80/15110 的进程（旧服务器，可能为管理员权限启动）
$hng_conns = Get-NetTCPConnection -LocalPort 80,15110 -State Listen -ErrorAction SilentlyContinue
$hng_pids = $hng_conns | Select-Object -ExpandProperty OwningProcess -Unique
foreach ($hng_p in $hng_pids) {
    Stop-Process -Id $hng_p -Force -ErrorAction SilentlyContinue
    Write-Host "[*] Stopped old server PID $hng_p"
}
Start-Sleep -Seconds 2

# 2. 启动新服务器（stdout 不重定向 → 服务器窗口实时显示日志；文件日志由 state.py 写 logs\realtest_game.log）
#    日志统一收进 logs\ 目录；旧版散落在包目录的 stderr 在此迁移（此刻旧进程已停、句柄已释放）
$hng_server_py = Join-Path $hng_root 'realtest_server.py'
$hng_logs = Join-Path $hng_root 'logs'
New-Item -ItemType Directory -Force -Path $hng_logs | Out-Null
Move-Item (Join-Path $hng_root 'realtest_stderr.log') $hng_logs -ErrorAction SilentlyContinue
$hng_stderr = Join-Path $hng_logs 'realtest_stderr.log'
Start-Process -FilePath $hng_py `
    -ArgumentList "-u `"$hng_server_py`"" `
    -WindowStyle Normal `
    -RedirectStandardError $hng_stderr
Start-Sleep -Seconds 3

# 3. 验证
$hng_conns = Get-NetTCPConnection -LocalPort 80,15110 -State Listen -ErrorAction SilentlyContinue
if ($hng_conns) {
    Write-Host '[+] Server listening:' -ForegroundColor Green
    $hng_conns | Select-Object LocalPort, OwningProcess | Format-Table -AutoSize
} else {
    Write-Host '[!] Server failed to start:' -ForegroundColor Red
    Get-Content $hng_stderr -Tail 8 -ErrorAction SilentlyContinue
}
