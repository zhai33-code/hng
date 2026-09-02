# HnG real-machine test launcher
# Requires: Administrator (for port 80) + hosts entries (managed manually)
#   hosts需手动添加：
#     127.0.0.1 game.hng-group.com
#     127.0.0.1 fs1.hng-group.com

$ErrorActionPreference = 'Stop'

# 私服静态资源目录由 state.py 自动探测（HNG_GAME_DIR 环境变量 > 包父级探测，无默认兜底）；
# 客户端 AIR 启动器在正式客户端目录 D:\Program Files\HnG（hosts 已导向本机）
# 变量名带 hng_ 前缀，避免被 powershell profile 变量污染（fnm 等工具）
$hng_root = $PSScriptRoot
$hng_py = Join-Path $hng_root '.venv\Scripts\python.exe'
if (-not (Test-Path $hng_py)) { $hng_py = Join-Path (Split-Path $hng_root -Parent) '.venv\Scripts\python.exe' }
if (-not (Test-Path $hng_py)) { $hng_py = 'python' }
$hng_server_dir = $hng_root

# 客户端根目录探测（与 state.py _detect_game_dir 同逻辑：WarDatabase + images 特征）
$hng_game_dir = $env:HNG_GAME_DIR
if (-not $hng_game_dir) {
    # .env 回退（state.py 同源：server 目录下 HNG_GAME_DIR=...）
    $hng_env_file = Join-Path $hng_root '.env'
    if (Test-Path $hng_env_file) {
        foreach ($hng_line in Get-Content $hng_env_file) {
            if ($hng_line -match '^\s*HNG_GAME_DIR\s*=\s*(.+?)\s*$') { $hng_game_dir = $Matches[1]; break }
        }
    }
}
if (-not $hng_game_dir) {
    # 从 server 目录向上逐级探测（与 state.py 同步：任意嵌套深度；
    # 根目录自身含 WarDatabase+images，或其下 combat-server 含之）
    $hng_game_dir = ''
    $hng_c = Split-Path $hng_root -Parent
    for ($hng_i = 0; $hng_i -lt 6; $hng_i++) {
        if (-not $hng_c) { break }
        $hng_cs = Join-Path $hng_c 'combat-server'
        if ((Test-Path (Join-Path $hng_c 'WarDatabase')) -and (Test-Path (Join-Path $hng_c 'images'))) { $hng_game_dir = $hng_c; break }
        if ((Test-Path (Join-Path $hng_cs 'WarDatabase')) -and (Test-Path (Join-Path $hng_cs 'images'))) { $hng_game_dir = $hng_cs; break }
        $hng_c = Split-Path $hng_c -Parent
    }
    if (-not $hng_game_dir) {
        Write-Host '[!] 未能自动探测客户端根目录（各级父目录均无 WarDatabase + images）。' -ForegroundColor Red
        Write-Host '    请设置环境变量 HNG_GAME_DIR 指向客户端根目录。' -ForegroundColor Red
        exit 1
    }
}

# 1. Check admin (port 80 needs it)
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host '[!] Administrator required (port 80). Run PowerShell as Admin.' -ForegroundColor Red
    exit 1
}

# 2. Start server (background)
# stdout 不重定向 → 服务器窗口实时显示日志（-u 无缓冲）；
# 全量日志由 state.py 写 realtest_game.log（行缓冲），stderr 仅留崩溃诊断
Write-Host '[*] Starting realtest_server...' -ForegroundColor Cyan
$hng_srv = Start-Process -FilePath $hng_py -ArgumentList "-u `"$hng_server_dir\realtest_server.py`"" `
    -PassThru -WindowStyle Normal `
    -RedirectStandardError "$hng_server_dir\realtest_stderr.log"

# 2b. (removed) action_replay UDP 15110 重放服已退役：
#     战斗流量改由专用服 combat-server\start_server.bat（hng.exe /D, UDP 5678）承载

Start-Sleep -Seconds 3

if ($hng_srv.HasExited) {
    Write-Host '[!] Server failed to start:' -ForegroundColor Red
    Get-Content "$hng_server_dir\realtest_stderr.log" -ErrorAction SilentlyContinue | Select-Object -First 10
    exit 1
}
Write-Host '[+] Server started (PID:' $hng_srv.Id ')' -ForegroundColor Green

# 3. Launch client（AIR 桌面启动器，与手动启动流程一致：
#    Start-Process 'D:\Program Files\HnG\HeroesAndGeneralsDesktop.exe' -ArgumentList 'launchMode=desktop'）
Write-Host '[*] Starting HeroesAndGeneralsDesktop...' -ForegroundColor Cyan
$hng_client_exe = Join-Path $hng_game_dir 'HeroesAndGeneralsDesktop.exe'
if (-not (Test-Path $hng_client_exe)) {
    Write-Host "[!] Client not found: $hng_client_exe" -ForegroundColor Red
    Stop-Process -Id $hng_srv.Id -Force
    exit 1
}

# EAC：服务端 player.dll 已二进制补丁禁用，客户端按常规方式启动即可
$hng_client = Start-Process -FilePath $hng_client_exe -ArgumentList 'launchMode=desktop' -PassThru
Write-Host '[+] Client started (PID:' $hng_client.Id ')' -ForegroundColor Green
Write-Host ''
Write-Host '=== Real-machine test running ===' -ForegroundColor Green
Write-Host '  Server PID:    ' $hng_srv.Id
Write-Host '  Client PID:    ' $hng_client.Id
Write-Host '  Login password: demodemo123'
Write-Host ''
Write-Host 'Press any key to stop server...' -ForegroundColor Yellow
$null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')

# 4. Cleanup (hosts 不动，你自己管；combat-server 独立窗口自行关闭)
Write-Host '[*] Stopping server...' -ForegroundColor Cyan
Stop-Process -Id $hng_srv.Id -Force -ErrorAction SilentlyContinue
Stop-Process -Id $hng_client.Id -Force -ErrorAction SilentlyContinue

Write-Host '=== Done ===' -ForegroundColor Green

# Show server log
Write-Host ''
Write-Host '--- Server log ---' -ForegroundColor Cyan
Get-Content "$hng_server_dir\realtest_game.log" -ErrorAction SilentlyContinue | Select-Object -Last 20
