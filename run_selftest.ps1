# 自测：后台起 hngserver 实例 → 跑 tests\selftest_client.py → 停进程
# 变量名带 hng_ 前缀，避免被 powershell profile 变量污染（fnm 等工具）
$hng_root = $PSScriptRoot
$hng_py = Join-Path $hng_root '.venv\Scripts\python.exe'
if (-not (Test-Path $hng_py)) { $hng_py = Join-Path (Split-Path $hng_root -Parent) '.venv\Scripts\python.exe' }
if (-not (Test-Path $hng_py)) { $hng_py = 'python' }
$env:JAVA_HOME = 'x'  # noop
Set-Location $hng_root
$hng_p = Start-Process -FilePath $hng_py -ArgumentList 'hngserver.py','15110' -PassThru -WindowStyle Hidden -RedirectStandardOutput 'server_stdout.log' -RedirectStandardError 'server_stderr.log'
Start-Sleep -Seconds 2
& $hng_py tests\selftest_client.py
$hng_code = $LASTEXITCODE
Stop-Process -Id $hng_p.Id -Force
Write-Output ("selftest exit: " + $hng_code)
Get-Content 'server_stdout.log' -ErrorAction SilentlyContinue | Select-Object -First 20
