#!/usr/bin/env bash
# ============================================================
# 解析端（抖音）Cookie 配置 —— 交互式写入 platform_config.yaml
#
# 用法：
#   bash deploy/configure_parser_cookie.sh            # 交互式选择
#   sudo bash deploy/configure_parser_cookie.sh       # 项目属服务用户时
#
# 非交互预设（自动化部署用）：
#   PARSER_COOKIE_MODE=none  bash deploy/configure_parser_cookie.sh            # 不填
#   PARSER_COOKIE_MODE=auto  bash deploy/configure_parser_cookie.sh            # 自动同步开关
#   PARSER_COOKIE_MODE=set   PARSER_COOKIE="a=b; c=d" bash ...                 # 手动
#   PARSER_COOKIE_MODE=file  PARSER_COOKIE_FILE=/path/cookie.txt bash ...      # 从文件
#   PARSER_COOKIE_MODE=keep  bash ...                                          # 保持
#
# 说明：
#   - 解析端与接收端（小号）是两条独立通道；不填 cookie 走移动端接口，公开视频通常够用
#   - 填 cookie 走网页端接口，更稳、抗风控；cookie 写入后即时生效（原项目热更新）
#   - "自动同步"=每次服务启动时把小号最新 cookie 同步到解析端，避免手动 cookie 过期
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"
VENV="$PROJECT_DIR/.venv/bin/python"
APP_USER="${APP_USER:-douyin}"

log()  { printf '\033[1;32m[cookie]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[cookie]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[cookie]\033[0m %s\n' "$*" >&2; exit 1; }

[ -f "$ENV_FILE" ] || die "未找到 $ENV_FILE（请先部署：bash deploy/install_ubuntu.sh）"
[ -x "$VENV" ] || die "未找到 $VENV（请先运行 install_ubuntu.sh 完成依赖安装）"

# 以服务用户身份运行 run.py，保证 platform_config.yaml 属主正确
run_py() {
  if [ "$(id -u)" -eq 0 ]; then
    local home
    home="$(getent passwd "$APP_USER" | cut -d: -f6 || true)"
    runuser -u "$APP_USER" -- env HOME="$home" "$VENV" "$PROJECT_DIR/run.py" "$@"
  else
    "$VENV" "$PROJECT_DIR/run.py" "$@"
  fi
}

# .env 键写入（存在替换、不存在追加；无权限用 sudo tee 保持属主）
set_env_value() {
  local key="$1" value="$2" tmp
  tmp="$(mktemp)"
  awk -v k="$key" -v v="$value" 'BEGIN{FS="="} $1==k{print k"="v; next} {print}' "$ENV_FILE" > "$tmp"
  if ! grep -qE "^[[:space:]]*${key}=" "$ENV_FILE"; then
    printf '\n%s=%s\n' "$key" "$value" >> "$tmp"
  fi
  if [ -w "$ENV_FILE" ]; then
    cat "$tmp" > "$ENV_FILE"
  elif command -v sudo >/dev/null 2>&1; then
    sudo tee "$ENV_FILE" < "$tmp" > /dev/null || { rm -f "$tmp" 2>/dev/null || true; die "写入 $ENV_FILE 失败"; }
  else
    rm -f "$tmp" 2>/dev/null || true
    die "无法写入 $ENV_FILE（请用 sudo 运行）"
  fi
  rm -f "$tmp" 2>/dev/null || true
}

mode="${PARSER_COOKIE_MODE:-}"

# 非交互且未预设 → 跳过
if [ -z "$mode" ] && [ ! -t 0 ]; then
  log "非交互式环境：跳过解析端 cookie 配置（可用 PARSER_COOKIE_MODE=none|auto|set|file|keep 预设）"
  exit 0
fi

if [ -z "$mode" ]; then
  echo
  echo "──────────────────────────────────────────────"
  echo " 解析端 Cookie 配置（影响解析稳定性/抗风控；公开视频通常不需要）"
  echo "   1) 不填 cookie（默认，走移动端接口，公开视频够用）"
  echo "   2) 自动从小号登录态导出（推荐：登录后同步；可选每次启动自动同步）"
  echo "   3) 手动粘贴 cookie 字符串"
  echo "   4) 从文件导入 cookie（每行一条）"
  echo "   5) 保持现状"
  printf " 请选择 [1/2/3/4/5]（回车默认 1）: "
  read -r choice || choice=""
  case "${choice:-1}" in
    2) mode="auto" ;;
    3) mode="set" ;;
    4) mode="file" ;;
    5) mode="keep" ;;
    *) mode="none" ;;
  esac
  echo "──────────────────────────────────────────────"
fi

case "$mode" in
  none)
    run_py cookie-config clear
    log "已选择：不填 cookie（解析走移动端接口）"
    ;;
  auto)
    # 不立即导出（部署时小号通常尚未登录），开启自动同步 + 提示登录后手动导出一次
    set_env_value "FORWARDER_AUTO_SYNC_COOKIE" "true"
    log "已开启「每次启动自动同步小号 cookie 到解析端」（FORWARDER_AUTO_SYNC_COOKIE=true）"
    warn "小号登录完成后，可立即手动同步一次："
    warn "  sudo -u $APP_USER -H $VENV $PROJECT_DIR/run.py export-cookie"
    warn "（之后每次服务启动会自动刷新，无需再手动维护 cookie）"
    ;;
  set)
    if [ -z "${PARSER_COOKIE:-}" ]; then
      printf " 请粘贴 cookie 字符串（形如 a=b; c=d，可整段粘贴）: "
      read -r cookie || cookie=""
    else
      cookie="$PARSER_COOKIE"
    fi
    if [ -z "$cookie" ]; then
      warn "未输入 cookie，放弃修改"
      exit 0
    fi
    run_py cookie-config set "$cookie"
    log "已写入手动 cookie（即时生效）"
    ;;
  file)
    if [ -z "${PARSER_COOKIE_FILE:-}" ]; then
      printf " 请输入 cookie 文件路径（每行一条 cookie）: "
      read -r file || file=""
    else
      file="$PARSER_COOKIE_FILE"
    fi
    [ -n "$file" ] || { warn "未输入路径，放弃修改"; exit 0; }
    run_py cookie-config from-file "$file"
    log "已从文件导入 cookie（即时生效）"
    ;;
  keep)
    log "保持现有解析端 cookie 配置不变"
    ;;
  *)
    die "未知的 PARSER_COOKIE_MODE=$mode（应为 none/auto/set/file/keep）"
    ;;
esac
