# Start the HnG admin backend (web/server/main.py, FastAPI, default 127.0.0.1:8080)
# Independent process: shares only PostgreSQL with the game server, restarts are decoupled.
# First run: python -m server.main create-admin <username>
#
# NOTE 2026-10-28: PowerShell's Start-Process rejects -WindowStyle together with
# -RedirectStandardOutput/-RedirectStandardError (ParameterArgumentValidationError),
# which is why this script silently failed before. -NoNewWindow is compatible with
# redirection and keeps the process detached from this console once the script exits.
$ErrorActionPreference = 'Stop'
$web_dir = $PSScriptRoot
$repo_root = Split-Path $web_dir -Parent
$hng_py = Join-Path $repo_root '.venv\Scripts\python.exe'
if (-not (Test-Path $hng_py)) { $hng_py = 'python' }

$listen = Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue
if ($listen) {
    Write-Host "[=] Admin backend already running (PID $($listen.OwningProcess -join ',')) -- http://127.0.0.1:8080/"
    return
}

$log_stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$hng_logs = Join-Path $repo_root 'logs'
New-Item -ItemType Directory -Force -Path $hng_logs | Out-Null
Get-ChildItem -Path $web_dir -Filter 'admin_*.log' -File -ErrorAction SilentlyContinue |
    Move-Item -Destination $hng_logs -Force -ErrorAction SilentlyContinue
$hng_stdout = Join-Path $hng_logs "admin_stdout_$log_stamp.log"
$hng_stderr = Join-Path $hng_logs "admin_stderr_$log_stamp.log"

Start-Process -FilePath $hng_py -ArgumentList '-m','server.main' `
    -WorkingDirectory $web_dir `
    -NoNewWindow `
    -RedirectStandardOutput $hng_stdout `
    -RedirectStandardError $hng_stderr

foreach ($i in 1..20) {
    Start-Sleep -Seconds 1
    if (Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue) {
        Write-Host '[+] Admin backend listening on http://127.0.0.1:8080/'
        Write-Host "    log: $hng_stdout"
        return
    }
}
Write-Host '[!] Admin backend failed to start, stderr tail:'
Get-Content $hng_stderr -Tail 15 -ErrorAction SilentlyContinue
