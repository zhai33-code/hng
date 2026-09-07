# 提权启动 realtest_server（stdout 直接进服务器窗口实时显示，stderr 留文件诊断），不启动客户端
# 路径基于脚本所在目录：包内布局 venv 在本目录，开发布局 venv 在上一级（hng-server-re\.venv）
# 变量名带 hng_ 前缀，避免被 powershell profile 变量污染（fnm 等工具）
$ErrorActionPreference = 'Stop'
$hng_root = $PSScriptRoot
$hng_py = Join-Path $hng_root '.venv\Scripts\python.exe'
if (-not (Test-Path $hng_py)) { $hng_py = Join-Path (Split-Path $hng_root -Parent) '.venv\Scripts\python.exe' }
if (-not (Test-Path $hng_py)) { $hng_py = 'python' }
$hng_server_py = Join-Path $hng_root 'realtest_server.py'
$hng_logs = Join-Path $hng_root 'logs'
New-Item -ItemType Directory -Force -Path $hng_logs | Out-Null
Move-Item (Join-Path $hng_root 'realtest_stderr.log') $hng_logs -ErrorAction SilentlyContinue
$hng_stderr = Join-Path $hng_logs 'realtest_stderr.log'
Start-Process -FilePath $hng_py -ArgumentList "-u `"$hng_server_py`"" `
    -WindowStyle Normal `
    -RedirectStandardError $hng_stderr
