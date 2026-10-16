#!/usr/bin/env bash
# ============================================================
# 监测账号配置（单会话 / 多会话）—— 交互式写入 .env
#
# 用法：
#   bash deploy/configure_accounts.sh            # 交互式选择
#   sudo bash deploy/configure_accounts.sh       # .env 属主是服务用户时
#
# 非交互预设（自动化部署用）：
#   MONITOR_MODE=single MONITOR_ACCOUNTS="我的大号"        bash deploy/configure_accounts.sh
#   MONITOR_MODE=multi  MONITOR_ACCOUNTS="小明,小红"       bash deploy/configure_accounts.sh
#
# 说明：
#   - 单会话：只监测一个大号的转发（写入 DOUYIN_MAIN_ACCOUNT）
#   - 多会话：同时监测多个用户/大号的转发（写入 DOUYIN_MAIN_ACCOUNTS，逗号分隔）
#   - 运行中也可以不改 .env：往 forwarder_data/agent_cmd.txt 写昵称即可免重启更换
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"

log()  { printf '\033[1;32m[accounts]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[accounts]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[accounts]\033[0m %s\n' "$*" >&2; exit 1; }

[ -f "$ENV_FILE" ] || die "未找到 $ENV_FILE（请先部署：bash deploy/install_ubuntu.sh，或 cp .env.example .env）"

# ── .env 读写工具 ──

read_env_value() {
  local key="$1"
  grep -E "^[[:space:]]*${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true
}

# 写入/更新一个键（存在则替换整行，不存在则追加）；无写权限时用 sudo tee（保持文件属主不变）
write_env_value() {
  local key="$1" value="$2" tmp
  tmp="$(mktemp)"
  awk -v k="$key" -v v="$value" '
    BEGIN { FS = "=" }
    $1 == k { print k "=" v; next }
    { print }
  ' "$ENV_FILE" > "$tmp"
  if ! grep -qE "^[[:space:]]*${key}=" "$ENV_FILE"; then
    printf '\n%s=%s\n' "$key" "$value" >> "$tmp"
  fi
  if [ -w "$ENV_FILE" ]; then
    cat "$tmp" > "$ENV_FILE"
  elif command -v sudo >/dev/null 2>&1; then
    if ! sudo tee "$ENV_FILE" < "$tmp" > /dev/null; then
      rm -f "$tmp" 2>/dev/null || true
      die "写入 $ENV_FILE 失败（sudo 执行异常）"
    fi
  else
    rm -f "$tmp" 2>/dev/null || true
    die "无法写入 $ENV_FILE（无写权限且没有 sudo；请用 sudo 重新运行）"
  fi
  # 清理临时文件：失败不影响结果（写入已完成）
  rm -f "$tmp" 2>/dev/null || true
}

# ── 读取当前配置 ──

current_multi="$(read_env_value DOUYIN_MAIN_ACCOUNTS)"
current_single="$(read_env_value DOUYIN_MAIN_ACCOUNT)"
current="${current_multi:-$current_single}"

mode="${MONITOR_MODE:-}"
accounts="${MONITOR_ACCOUNTS:-}"

# 非交互环境且未预设 → 跳过（不影响部署流程）
if [ -z "$mode" ] && [ ! -t 0 ]; then
  log "非交互式环境：跳过监测账号配置（可用 MONITOR_MODE=single|multi + MONITOR_ACCOUNTS=昵称1,昵称2 预设）"
  exit 0
fi

case "$mode" in
  ""|single|multi|keep) ;;
  *) warn "未知的 MONITOR_MODE=$mode（应为 single / multi），改用交互式选择"; mode="" ;;
esac

# ── 交互式选择：单会话 / 多会话 ──

if [ -z "$mode" ]; then
  echo
  echo "──────────────────────────────────────────────"
  echo " 监测配置：要监测几个私信会话？"
  if [ -n "$current" ]; then
    echo "   当前配置: $current"
    echo "   1) 改为单会话（只监测一个大号）"
    echo "   2) 改为多会话（同时监测多个用户/大号）"
    echo "   3) 保持不变（回车默认）"
  else
    echo "   1) 单会话（只监测一个大号）—— 默认"
    echo "   2) 多会话（同时监测多个用户/大号的转发）"
    echo "   （注：大号 = 给你服务器上的小号转发视频的抖音账号；可多个）"
  fi
  printf " 请选择 [1/2/3]: "
  read -r choice || choice=""
  case "${choice:-}" in
    2) mode="multi" ;;
    1) mode="single" ;;
    3) mode="keep" ;;
    "")
      if [ -n "$current" ]; then mode="keep"; else mode="single"; fi
      ;;
    *)
      mode="keep"
      warn "未识别的选择（$choice），保持现有配置"
      ;;
  esac
  echo "──────────────────────────────────────────────"
fi

if [ "$mode" = "keep" ]; then
  log "保持现有监测配置: ${current:-（未设置，自动选择会话）}"
  exit 0
fi

# ── 写入配置 ──

if [ "$mode" = "multi" ]; then
  if [ -z "$accounts" ]; then
    printf " 请输入要监测的昵称列表（多个用逗号分隔，如：小明,小红）: "
    read -r accounts || accounts=""
  fi
  if [ -z "$accounts" ]; then
    warn "未输入昵称，放弃修改（之后可随时重跑本脚本，或往 agent_cmd.txt 写昵称）"
    exit 0
  fi
  write_env_value "DOUYIN_MAIN_ACCOUNTS" "$accounts"
  write_env_value "DOUYIN_MAIN_ACCOUNT" ""
  log "已写入多会话监测配置: DOUYIN_MAIN_ACCOUNTS=$accounts"
else
  if [ -z "$accounts" ]; then
    default_one="$(printf '%s' "$current" | sed 's/[,，;；].*//')"
    printf " 请输入大号昵称（可留空 = 不指定、自动选择会话）%s: " "${default_one:+ [默认: $default_one]}"
    answer=""
    read -r answer || answer=""
    if [ -n "$answer" ]; then
      accounts="$answer"
    else
      accounts="$default_one"
    fi
  fi
  write_env_value "DOUYIN_MAIN_ACCOUNT" "$accounts"
  write_env_value "DOUYIN_MAIN_ACCOUNTS" ""
  log "已写入单会话监测配置: DOUYIN_MAIN_ACCOUNT=${accounts:-（留空，自动选择会话）}"
fi

log "提示: 服务运行中也可免重启更换账号——echo \"昵称\" | sudo tee $PROJECT_DIR/forwarder_data/agent_cmd.txt"
log "提示: 修改 .env 后需重启服务生效——sudo systemctl restart douyin-forward"
