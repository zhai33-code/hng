# eos_fake_backend 一次性部署脚本（需管理员）
# 作用：
#   1. 自签 CA 装入 Windows Root store   → 覆盖 curl-Schannel 后端
#   2. CA 追加到 C:\Windows\cert.pem     → 覆盖 curl-OpenSSL 1.1.1n 后端（DLL 内编译死的默认 CA 路径）
#   3. 打印 hosts 需手动添加的行（hosts 由用户手工维护，脚本不改）
# 用法：
#   powershell -ExecutionPolicy Bypass -File eos_fake_setup.ps1            # 安装
#   powershell -ExecutionPolicy Bypass -File eos_fake_setup.ps1 -Uninstall # 卸载
param([switch]$Uninstall)

$ErrorActionPreference = 'Stop'
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host '必须以管理员运行' -ForegroundColor Red; exit 1
}

$caCrt    = Join-Path $PSScriptRoot 'certs\eos_fake_ca.crt'
$caCrl    = Join-Path $PSScriptRoot 'certs\eos_fake_ca.crl'
$certPem  = 'C:\Windows\cert.pem'
$certBak  = 'C:\Windows\cert.pem.hngbak'
$caSubj   = 'HnG Fake EOS Root CA'
$marker   = 'HnG Fake EOS Root CA'

if ($Uninstall) {
    certutil -delstore Root $caSubj | Out-Null
    certutil -delstore CA $caSubj 2>$null | Out-Null   # 移除缓存 CRL
    Write-Host '[1/2] 已从 Root store 移除 CA 及 CRL 缓存'
    if ((Test-Path $certBak) -and (Test-Path $certPem)) {
        Copy-Item $certBak $certPem -Force
        Write-Host '[2/2] 已还原 C:\Windows\cert.pem（备份恢复）'
    } else {
        Write-Host '[2/2] 跳过 cert.pem 还原（无备份）'
    }
    Write-Host '记得手动删除 hosts 里的: 127.0.0.1 api.epicgames.dev'
    exit 0
}

# 0. 先跑一次假后端生成证书（只生成，不监听）
if (-not (Test-Path $caCrt)) {
    Write-Host '[0] 首次运行：生成 CA + 服务器证书…'
    python -c "import sys; sys.path.insert(0, r'$PSScriptRoot'); import eos_fake_backend as m; m._gen_certs()"
    if (-not (Test-Path $caCrt)) { Write-Host '证书生成失败（先启动一次 eos_fake_backend.py）' -ForegroundColor Red; exit 1 }
}

# 1. Windows Root store（Schannel 后端）+ 本地 CRL 缓存
#    CRL 装入 CA store 后，schannel 链验证直接命中本地缓存，
#    不再请求证书里的 http:// CDP URL（80 端口已被 realtest_server 占用，URL 不可达也无妨）
certutil -addstore Root $caCrt | Out-Null
Write-Host '[1/3] CA 已装入 Root store（Schannel）'
if (Test-Path $caCrl) {
    certutil -addstore CA $caCrl | Out-Null
    Write-Host '      CRL 已装入 CA store（schannel 本地吊销缓存）'
}

# 2. OpenSSL 默认 CA 文件（DLL 内写死的路径 C:\Windows\cert.pem）
#    幂等检查用 base64 主体匹配（PEM 里 CN 不以明文出现，明文 marker 永远匹配不到）
if (Test-Path $certPem) {
    $cur   = (Get-Content $certPem -Raw -ErrorAction SilentlyContinue) -replace '\s',''
    $caB64 = (Get-Content $caCrt -Raw) -replace '-----[^-]+-----','' -replace '\s',''
    if ($cur -notlike "*$caB64*") {
        Copy-Item $certPem $certBak -Force
        Add-Content $certPem "`n"
        Get-Content $caCrt | Add-Content $certPem
        Write-Host '[2/3] CA 已追加到 C:\Windows\cert.pem（原文件备份为 cert.pem.hngbak）'
    } else {
        Write-Host '[2/3] cert.pem 已包含本 CA，跳过'
    }
} else {
    Copy-Item $caCrt $certPem
    Write-Host '[2/3] 已创建 C:\Windows\cert.pem'
}

# 3. hosts 手动提示 + 443 端口检查
Write-Host ''
Write-Host '[3/3] 请手动在 hosts 添加（脚本不代管 hosts）：' -ForegroundColor Yellow
Write-Host '      127.0.0.1 api.epicgames.dev'
Write-Host ''
$port = Get-NetTCPConnection -LocalPort 443 -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
if ($port) {
    $proc = Get-Process -Id $port.OwningProcess -ErrorAction SilentlyContinue
    Write-Host ("注意: 443 已被 {0} (PID {1}) 占用，启动假后端前需释放" -f $proc.ProcessName, $port.OwningProcess) -ForegroundColor Yellow
} else {
    Write-Host '443 端口空闲'
}
Write-Host '完成。启动: python eos_fake_backend.py'
