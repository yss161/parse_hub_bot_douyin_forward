#!/usr/bin/env bash
# ============================================================
# 抖音转发解析服务 —— Ubuntu 一键部署脚本
#
# 用法（在服务器上的项目目录内执行）：
#   bash deploy/install_ubuntu.sh
#   # 以 root 运行时，可指定服务用户（默认 douyin）：
#   APP_USER=douyin bash deploy/install_ubuntu.sh
#
# 脚本会完成：
#   1. 安装系统依赖（Python 3.12+ / venv、中文字体、xvfb、Playwright 系统库）
#   2. 创建虚拟环境 .venv 并安装 requirements-forwarder.txt
#      （默认腾讯云 PyPI 镜像，失败自动回退官方源）
#   3. 安装 Playwright Chromium 浏览器
#   4. 生成 .env（若不存在）并安装 systemd 服务（不自动启动）
#
# 完成后请按脚本末尾提示，先完成首次登录，再启动服务。
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
SERVICE_NAME="douyin-forward"
UNIT_DST="/etc/systemd/system/${SERVICE_NAME}.service"
PIP_INDEX_DEFAULT="https://mirrors.cloud.tencent.com/pypi/simple"

log()  { printf '\033[1;32m[deploy]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[deploy]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[deploy]\033[0m %s\n' "$*" >&2; exit 1; }

# ────────────────────────── 0. 基本检查 ──────────────────────────

case "$PROJECT_DIR" in
  *" "*) warn "项目路径包含空格，可能影响 systemd 服务，建议放到 /opt/douyin_download_bot 这类无空格路径" ;;
esac

if [ -f /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  log "系统: ${PRETTY_NAME:-unknown}"
  [ "${ID:-}" = "ubuntu" ] || warn "当前系统不是 Ubuntu（ID=${ID:-未知}），脚本按 Ubuntu 流程继续，可能不适用"
else
  warn "未找到 /etc/os-release，按 Ubuntu 流程继续"
fi

[ -f "$PROJECT_DIR/requirements-forwarder.txt" ] || die "未在 $PROJECT_DIR 找到 requirements-forwarder.txt，请确认项目完整上传"
[ -f "$PROJECT_DIR/run.py" ] || die "未在 $PROJECT_DIR 找到 run.py，请确认项目完整上传"

# ────────────────────────── 1. 确定服务用户 ──────────────────────────

if [ "$(id -u)" -eq 0 ]; then
  RUN_AS="${APP_USER:-douyin}"
  if ! id -u "$RUN_AS" >/dev/null 2>&1; then
    log "创建服务用户: $RUN_AS"
    useradd -m -s /bin/bash "$RUN_AS"
  fi
  SUDO=""
  APT="apt-get"
  run_as_app() { runuser -u "$RUN_AS" -- env HOME="$APP_HOME" "$@"; }
else
  RUN_AS="$(id -un)"
  command -v sudo >/dev/null 2>&1 || die "当前不是 root 且未安装 sudo；请用 root 运行：bash deploy/install_ubuntu.sh"
  SUDO="sudo"
  APT="sudo apt-get"
  run_as_app() { "$@"; }
  [ -w "$PROJECT_DIR" ] || die "当前用户对 $PROJECT_DIR 无写权限（可能是 root 拷贝的文件），请先：sudo chown -R $(id -un) \"$PROJECT_DIR\""
fi
APP_HOME="$(getent passwd "$RUN_AS" | cut -d: -f6 || true)"
[ -n "$APP_HOME" ] || die "无法确定用户 $RUN_AS 的 HOME 目录"
log "服务将以用户 [$RUN_AS] 运行（HOME=$APP_HOME）"

# root 场景：先把项目目录归属到服务用户（venv/浏览器/数据目录都需要写权限）
if [ "$(id -u)" -eq 0 ]; then
  chown -R "$RUN_AS:$RUN_AS" "$PROJECT_DIR"
fi

# ────────────────────────── 2. 系统依赖 ──────────────────────────

log "安装系统依赖（apt）..."
$APT update -y
$APT install -y \
  python3 python3-venv python3-pip python3-dev \
  build-essential git curl ca-certificates unzip \
  fonts-noto-cjk fonts-noto-color-emoji \
  xvfb xauth software-properties-common

# ────────────────────────── 3. 选择 Python（>=3.12） ──────────────────────────

pick_python() {
  local c
  for c in python3.13 python3.12 python3; do
    if command -v "$c" >/dev/null 2>&1 \
      && "$c" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,12) else 1)' 2>/dev/null; then
      echo "$c"
      return 0
    fi
  done
  return 1
}

PY="$(pick_python || true)"
if [ -z "$PY" ]; then
  warn "系统默认 Python 低于 3.12，尝试通过 deadsnakes PPA 安装 python3.12（适用于 Ubuntu 20.04 / 22.04）"
  $SUDO add-apt-repository -y ppa:deadsnakes/ppa
  $SUDO apt-get update -y
  $SUDO apt-get install -y python3.12 python3.12-venv python3.12-dev
  PY="python3.12"
fi
log "使用 Python: $($PY --version 2>&1) ($(command -v "$PY"))"

# ────────────────────────── 4. 虚拟环境与依赖 ──────────────────────────

log "创建虚拟环境: $VENV_DIR"
run_as_app "$PY" -m venv "$VENV_DIR"
run_as_app "$VENV_DIR/bin/python" -m pip install -q -U pip

PIP_INDEX="${PIP_INDEX_URL:-$PIP_INDEX_DEFAULT}"
log "安装 Python 依赖（镜像: $PIP_INDEX）..."
if ! run_as_app "$VENV_DIR/bin/python" -m pip install -r "$PROJECT_DIR/requirements-forwarder.txt" -i "$PIP_INDEX"; then
  warn "镜像源安装失败，回退官方 PyPI 重试..."
  run_as_app "$VENV_DIR/bin/python" -m pip install -r "$PROJECT_DIR/requirements-forwarder.txt"
fi

# ────────────────────────── 5. Playwright 浏览器 ──────────────────────────

log "安装 Playwright 系统依赖库（apt）..."
$SUDO "$VENV_DIR/bin/playwright" install-deps chromium || warn "install-deps 失败：如浏览器启动报缺库，请手动执行 $VENV_DIR/bin/playwright install-deps chromium"

log "安装 Playwright Chromium（下载约 150MB）..."
run_as_app "$VENV_DIR/bin/playwright" install chromium

# ────────────────────────── 6. 配置与目录 ──────────────────────────

if [ ! -f "$PROJECT_DIR/.env" ]; then
  cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
  warn "已生成 $PROJECT_DIR/.env —— 请编辑并填写 DOUYIN_MAIN_ACCOUNT（大号昵称关键字）"
else
  log "已存在 .env，保持不变"
fi

mkdir -p "$PROJECT_DIR/forwarder_data" "$PROJECT_DIR/output"
if [ "$(id -u)" -eq 0 ]; then
  chown -R "$RUN_AS:$RUN_AS" "$PROJECT_DIR"
fi

# ────────────────────────── 7. systemd 服务 ──────────────────────────

log "安装 systemd 服务: $UNIT_DST（暂不启动）"
sed -e "s|__APP_USER__|$RUN_AS|g" \
    -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    -e "s|__APP_HOME__|$APP_HOME|g" \
    "$SCRIPT_DIR/douyin-forward.service" > "/tmp/${SERVICE_NAME}.service.tmp"
$SUDO mv "/tmp/${SERVICE_NAME}.service.tmp" "$UNIT_DST"
$SUDO chmod 644 "$UNIT_DST"
$SUDO systemctl daemon-reload

# ────────────────────────── 8. 完成提示 ──────────────────────────

cat <<EOF

============================================================
 ✅ 部署完成
============================================================
 项目目录 : $PROJECT_DIR
 服务用户 : $RUN_AS
 虚拟环境 : $VENV_DIR

 【剩余两步】

 第 1 步：完成小号首次登录（按顺序尝试）
    ※ 登录前请确保服务未启动：如已启动，先执行 sudo systemctl stop $SERVICE_NAME

   ① 首选：服务器上以"有界面"方式扫码（xvfb，最接近真实浏览器）
     sudo -u $RUN_AS -H env DOUYIN_HEADLESS=false /usr/bin/xvfb-run -a $VENV_DIR/bin/python $PROJECT_DIR/run.py login
     # 二维码会自动截图保存，下载到本地后用【小号】抖音 App 扫码：
     #   $PROJECT_DIR/forwarder_data/login_qr.png
     #   # 在本机执行（<登录用户> = 你 ssh 登录服务器的账号，如 root / ubuntu）：
     #   scp <登录用户>@<server>:$PROJECT_DIR/forwarder_data/login_qr.png .

   ② 若日志提示「验证码中间页」（滑块风控，服务器上无法手动完成）：
     在本地有桌面的电脑（Windows 亦可）执行：
       DOUYIN_HEADLESS=false python run.py login      # 扫码登录（必要时手动过滑块）
     然后把整个 forwarder_data/browser_profile 目录同步到服务器：
       # 本地 Windows：
       Compress-Archive -Path .\\forwarder_data\\browser_profile -DestinationPath .\\browser_profile.zip -Force
       scp .\\browser_profile.zip <user>@<server>:/tmp/
       # 服务器：
       unzip -o /tmp/browser_profile.zip -d $PROJECT_DIR/forwarder_data/
       chown -R $RUN_AS:$RUN_AS $PROJECT_DIR/forwarder_data

   ③ 兜底：纯无头直接登录（新 profile 容易被风控，仅在 ① 不可用时尝试）
     sudo -u $RUN_AS -H $VENV_DIR/bin/python $PROJECT_DIR/run.py login

 第 2 步：启动服务为系统服务
     sudo systemctl enable --now $SERVICE_NAME
     journalctl -u $SERVICE_NAME -f        # 查看日志

 其他：
   编辑配置     : $PROJECT_DIR/.env
   重启服务     : sudo systemctl restart $SERVICE_NAME
   更新浏览器   : sudo -u $RUN_AS -H $VENV_DIR/bin/playwright install chromium
   详细说明     : 见项目根目录 DEPLOY_UBUNTU.md 与 README.md
============================================================
EOF
