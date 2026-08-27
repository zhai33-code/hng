# 启动管理后台（web/server/main.py，FastAPI 独立进程，默认 127.0.0.1:8080）
# 与游戏服进程解耦：仅共享 PostgreSQL，重启互不影响
# 首次使用：先 python -m server.main create-admin <用户名> 创建管理员
$ErrorActionPreference = 'Stop'
$web_dir = $PSScriptRoot                                   # <仓库根>\web
$repo_root = Split-Path $web_dir -Parent                   # 仓库根（hng-re）
$hng_py = Join-Path $repo_root '.venv\Scripts\python.exe'
if (-not (Test-Path $hng_py)) { $hng_py = 'python' }
$hng_stderr = Join-Path $web_dir 'admin_stderr.log'
Start-Process -FilePath $hng_py -ArgumentList "-m server.main" `
    -WorkingDirectory $web_dir `
    -WindowStyle Normal `
    -RedirectStandardError $hng_stderr
Write-Host "管理后台启动中：http://127.0.0.1:8080/（stderr: $hng_stderr）"
