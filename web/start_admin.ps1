# 启动管理后台（web/server/main.py，FastAPI 独立进程，默认 127.0.0.1:8080）
# 与游戏服进程解耦：仅共享 PostgreSQL，重启互不影响
# 首次使用：先 python -m server.main create-admin <用户名> 创建管理员
$ErrorActionPreference = 'Stop'
$web_dir = $PSScriptRoot                                   # <仓库根>\web
$repo_root = Split-Path $web_dir -Parent                   # 仓库根（hng-re）
$hng_py = Join-Path $repo_root '.venv\Scripts\python.exe'
if (-not (Test-Path $hng_py)) { $hng_py = 'python' }
# 隐藏窗口运行：进程不挂任何可见控制台，彻底避免 Windows 控制台 QuickEdit
# 模式被鼠标点选后冻结进程（表现为 web 无响应、按 Enter 才恢复）。
# 日志查看（相当于 tail -f，随时开新窗口看，不影响服务）：
#   Get-Content -Wait <日志文件>
$log_stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
# logs centralized under server\logs (2026-09-08); migrate old ones from web dir
$hng_logs = Join-Path $repo_root 'logs'
New-Item -ItemType Directory -Force -Path $hng_logs | Out-Null
Get-ChildItem -Path $web_dir -Filter 'admin_*.log' -File -ErrorAction SilentlyContinue |
    Move-Item -Destination $hng_logs -Force -ErrorAction SilentlyContinue
$hng_stdout = Join-Path $hng_logs "admin_stdout_$log_stamp.log"
$hng_stderr = Join-Path $hng_logs "admin_stderr_$log_stamp.log"
Start-Process -FilePath $hng_py -ArgumentList "-m server.main" `
    -WorkingDirectory $web_dir `
    -WindowStyle Hidden `
    -RedirectStandardOutput $hng_stdout `
    -RedirectStandardError $hng_stderr
Write-Host "管理后台启动中：http://127.0.0.1:8080/"
Write-Host "日志：$hng_stdout"
Write-Host "实时查看：Get-Content -Wait '$hng_stdout'"
