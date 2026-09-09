#!/usr/bin/env bash
# HnG 私服发布脚本：本地构建前端 → tar 打包代码 → SSH 推到 Windows VPS → 重启游戏服
#
# 用法：
#   ./deploy.sh                 # 推送服务端代码 + 重启游戏服（默认不含管理端）
#   ./deploy.sh --web           # 管理端整包发布：构建前端 dist + web/server（两者成对发布，缺一不可）
#   ./deploy.sh --no-restart    # 只传文件，不重启
#   ./deploy.sh --cas           # 额外推送 cas/ 大文件（144M，平时不变别推）
#   ./deploy.sh --clean-web     # 推送前清空远端 web/frontend/dist（清残留 hash 资源，需配合 --web）
#   ./deploy.sh --restart-admin # 重启后顺带重启管理后台（8080，FastAPI）
#   ./deploy.sh --setup-key     # 把本机公钥装到 VPS（administrators_authorized_keys），以后免密码
#
# 密码次数：没装公钥时，连接复用保证一次发布只输 1 次密码；装了公钥（--setup-key）0 次。
# 远端要求：Windows 10+ 自带 tar.exe（System32），无需额外安装。
set -euo pipefail

# ===== 配置（可用环境变量覆盖）=====
SSH_TARGET="${HNG_SSH_TARGET:-Administrator@64.90.31.56}"
SSH_PORT="${HNG_SSH_PORT:-22}"
REMOTE_DIR="${HNG_REMOTE_DIR:-C:/Users/Administrator/Desktop/HngServer/server}"  # 正斜杠/反斜杠均可用
REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"

# 连接复用：首个连接做 master，后续全部走它（密码只输 1 次；10 分钟内重跑连密码都不用）
SSH_OPTS=(-p "$SSH_PORT" -o ControlMaster=auto -o ControlPath=/tmp/hng-ssh-%r@%h-%p -o ControlPersist=10m)

# ===== 参数 =====
WITH_WEB=0; NO_RESTART=0; WITH_CAS=0; CLEAN_WEB=0; RESTART_ADMIN=0; SETUP_KEY=0
for arg in "$@"; do
  case "$arg" in
    --web) WITH_WEB=1 ;;
    --no-restart) NO_RESTART=1 ;;
    --cas) WITH_CAS=1 ;;
    --clean-web) CLEAN_WEB=1 ;;
    --restart-admin) RESTART_ADMIN=1 ;;
    --setup-key) SETUP_KEY=1 ;;
    *) echo "未知参数: $arg"; exit 1 ;;
  esac
done
if [ "$CLEAN_WEB" -eq 1 ] && [ "$WITH_WEB" -eq 0 ]; then
  echo "--clean-web 需配合 --web 使用（否则清掉远端 dist 却不补回）"; exit 1
fi

# ===== 0. 公钥安装（--setup-key，一次性）=====
# 注意：Administrator 属于管理员组，sshd 默认只认 C:\ProgramData\ssh\administrators_authorized_keys
# （~/.ssh/authorized_keys 会被默认 sshd_config 的 Match Group administrators 块忽略），
# 且该文件 ACL 必须只有 SYSTEM/Administrators，否则 sshd 拒绝读取。
if [ "$SETUP_KEY" -eq 1 ]; then
  [ -f "$HOME/.ssh/id_ed25519.pub" ] || ssh-keygen -t ed25519 -N '' -f "$HOME/.ssh/id_ed25519"
  hng_pub="$(tr -d '\n' < "$HOME/.ssh/id_ed25519.pub")"
  echo "安装公钥到 VPS（输一次密码）..."
  ssh "${SSH_OPTS[@]}" "$SSH_TARGET" \
    "powershell -NoProfile -Command \"\$p='C:\ProgramData\ssh\administrators_authorized_keys'; Add-Content -Path \$p -Value '$hng_pub'; icacls \$p /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F'\""
  echo "[+] 公钥已安装，验证连接："
  ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "echo ok"
  exit 0
fi

# ===== 1. 管理端前端构建（本地 Mac，仅 --web 时）=====
if [ "$WITH_WEB" -eq 1 ]; then
  echo "[1/5] 构建管理前端 (pnpm build)..."
  cd "$REPO_ROOT/web/frontend"
  pnpm install
  pnpm build
else
  echo "[1/5] 跳过管理端（加 --web 发布前端 dist + web/server）"
fi
cd "$REPO_ROOT"

# ===== 2. 打包（排除 venv/logs/.env/__pycache__ 等远端自有内容）=====
echo "[2/5] 打包代码..."
TAR_FILE="$(mktemp /tmp/hng-deploy.XXXXXX.tar.gz)"
INCLUDES=(
  *.py *.ps1 requirements.txt README.md
  handlers war_campaign store tools captcha_assets
  web/*.ps1
)
[ "$WITH_WEB" -eq 1 ] && INCLUDES+=(web/server web/frontend/dist)
[ "$WITH_CAS" -eq 1 ] && INCLUDES+=(cas)
tar -czf "$TAR_FILE" --exclude '__pycache__' --exclude '*.pyc' --exclude '.DS_Store' \
  --exclude '._*' --exclude '.AppleDouble' --exclude '.Spotlight-V100' --exclude '.Trashes' \
  --exclude 'tests' --exclude 'logs' --exclude 'docs' --exclude 'eos' \
  --exclude 'web/server/config.py' --exclude 'web/server/.admin_secret' \
  --exclude 'web/frontend/dist/icons' "${INCLUDES[@]}"
echo "    打包完成: $(du -h "$TAR_FILE" | cut -f1)  $TAR_FILE"

# ===== 3. 推送到 VPS（tar 直接在远端解压覆盖，远端自有文件不受影响）=====
echo "[3/5] 推送到 $SSH_TARGET:$REMOTE_DIR ..."
if [ "$CLEAN_WEB" -eq 1 ]; then
  ssh "${SSH_OPTS[@]}" "$SSH_TARGET" \
    "powershell -NoProfile -Command \"Remove-Item -Recurse -Force '$REMOTE_DIR/web/frontend/dist' -ErrorAction SilentlyContinue\""
fi
ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "tar -xzf - -C $REMOTE_DIR" < "$TAR_FILE"
rm -f "$TAR_FILE"
# 清理远端 macOS 垃圾（AppleDouble/.DS_Store 为历史 SMB/Finder 拷贝残留，
# 解压覆盖不删多余文件；._bg-1.jpg 混进验证码素材曾致 PIL 出题 500）
ssh "${SSH_OPTS[@]}" "$SSH_TARGET" \
  "powershell -NoProfile -Command \"Get-ChildItem '$REMOTE_DIR' -Recurse -Force -ErrorAction SilentlyContinue | Where-Object { \\\$_.Name -like '._*' -or \\\$_.Name -eq '.DS_Store' } | Remove-Item -Force -ErrorAction SilentlyContinue\""

# ===== 4. 重启游戏服（停 80/15110 旧进程 → 拉起 realtest_server）=====
if [ "$NO_RESTART" -eq 0 ]; then
  echo "[4/5] 重启游戏服..."
  ssh "${SSH_OPTS[@]}" "$SSH_TARGET" \
    "powershell -NoProfile -ExecutionPolicy Bypass -File $REMOTE_DIR/admin_restart_server.ps1"
  if [ "$RESTART_ADMIN" -eq 1 ]; then
    echo "    重启管理后台 (8080)..."
    ssh "${SSH_OPTS[@]}" "$SSH_TARGET" \
      "powershell -NoProfile -ExecutionPolicy Bypass -File $REMOTE_DIR/web/restart_admin.ps1"
  fi
else
  echo "[4/5] 跳过重启 (--no-restart)"
fi

# ===== 5. 验证监听 =====
if [ "$NO_RESTART" -eq 0 ]; then
  echo "[5/5] 验证端口监听..."
  sleep 3
  hng_ports="80,15110"
  [ "$RESTART_ADMIN" -eq 1 ] && hng_ports="80,15110,8080"
  if ssh "${SSH_OPTS[@]}" "$SSH_TARGET" \
    "powershell -NoProfile -Command \"Get-NetTCPConnection -LocalPort $hng_ports -State Listen -ErrorAction SilentlyContinue | Select-Object LocalPort,OwningProcess | Format-Table -AutoSize\""; then
    echo "[+] 发布完成（服务器已脱离 SSH 会话运行，日志见远端 logs\\realtest_stderr.log）"
  else
    echo "[!] 端口验证未通过：远端 $hng_ports 无监听或查询失败，查看 $REMOTE_DIR/logs/realtest_stderr.log" >&2
    exit 1
  fi
fi
