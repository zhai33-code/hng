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

# 2. 启动新服务器（文件日志由 state.py 写 logs\realtest_game.log）
#    ★必须经 WMI Win32_Process.Create 启动（2026-09-09）：deploy.sh 经 SSH
#    执行本脚本，sshd 把会话子进程放进 Job Object，Start-Process 拉起的
#    python 同在 Job 内 → SSH 断开（发布脚本退出）进程即被连带杀死——
#    症状：脚本内验证监听正常，部署完服务器就没了。WMI 启动的进程父级
#    为 WmiPrvSE，不在 SSH Job 内，断开不受影响。代价：无交互控制台窗口，
#    stdout/stderr 合并追加进 logs\realtest_stderr.log。
#    WMI Create 不经 shell，重定向需 cmd /c 包装（首尾双引号规则）。
$hng_server_py = Join-Path $hng_root 'realtest_server.py'
$hng_logs = Join-Path $hng_root 'logs'
New-Item -ItemType Directory -Force -Path $hng_logs | Out-Null
Move-Item (Join-Path $hng_root 'realtest_stderr.log') $hng_logs -ErrorAction SilentlyContinue
$hng_stderr = Join-Path $hng_logs 'realtest_stderr.log'
$hng_cmd = 'cmd.exe /c ""' + $hng_py + '" -u "' + $hng_server_py + '" >> "' + $hng_stderr + '" 2>&1"'
$hng_r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine      = $hng_cmd
    CurrentDirectory = $hng_root
}
if ($hng_r.ReturnValue -ne 0) {
    Write-Host "[!] WMI process create failed: ReturnValue=$($hng_r.ReturnValue)" -ForegroundColor Red
    exit 1
}
Write-Host "[*] Started server via WMI, PID $($hng_r.ProcessId)"

# 3. 验证（等待监听就绪，最多 20s——python 起动+DB 连接有耗时）
$hng_conns = $null
foreach ($hng_i in 1..20) {
    Start-Sleep -Seconds 1
    $hng_conns = Get-NetTCPConnection -LocalPort 80,15110 -State Listen -ErrorAction SilentlyContinue
    if ($hng_conns) { break }
}
if ($hng_conns) {
    Write-Host '[+] Server listening:' -ForegroundColor Green
    $hng_conns | Select-Object LocalPort, OwningProcess | Format-Table -AutoSize
} else {
    Write-Host '[!] Server failed to start:' -ForegroundColor Red
    Get-Content $hng_stderr -Tail 8 -ErrorAction SilentlyContinue
    exit 1
}
