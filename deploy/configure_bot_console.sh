#!/usr/bin/env bash
# ============================================================
# Bot 控制台 / Owner 绑定配置 —— 交互式写入 .env
#
# 用法：
#   bash deploy/configure_bot_console.sh
#   sudo bash deploy/configure_bot_console.sh
#
# 非交互预设（自动化部署用）：
#   BOT_CONSOLE=on BOT_BIND_CODE=... FAILURE_NOTIFY=on SUMMARY=on bash deploy/configure_bot_console.sh
#   BOT_CONSOLE=off bash deploy/configure_bot_console.sh
#
# 说明：
#   1) 开启后，先私聊 bot 发送：/start <绑定码>
#   2) 第一个绑定成功的账号成为唯一 owner；绑定码立即失效
#   3) 绑定后可使用 /status /server /stats /failures /retry /parse /pause 等命令
#   4) 失败作品达到重试上限时，会推送失败卡片（重试 / 忽略 / 详情）
#   5) 绑定码只在首次绑定时需要，之后可以留空
#
# ⚠ 如果服务通过 systemd drop-in 覆盖了同名环境变量，改 .env 不会生效；
#   请同时修改 drop-in 或删除对应覆盖项。
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"

log()  { printf '\033[1;32m[bot-console]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[bot-console]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[bot-console]\033[0m %s\n' "$*" >&2; exit 1; }

[ -f "$ENV_FILE" ] || die "未找到 $ENV_FILE（请先部署：bash deploy/install_ubuntu.sh，或 cp .env.example .env）"

read_env_value() {
  local key="$1"
  grep -E "^[[:space:]]*${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true
}

write_env_value() {
  local key="$1" value="$2" tmp
  tmp="$(mktemp)"
  awk -v k="$key" -v v="$value" '
    BEGIN { FS = "=" }
    $1 == k { print k "=" v; next }
    { print }
  ' "$ENV_FILE" > "$tmp"
  if ! grep -qE "^[[:space:]]*${key}=" "$ENV_FILE"; then
    if [ -s "$tmp" ] && [ -n "$(tail -c 1 "$tmp")" ]; then
      printf '\n' >> "$tmp"
    fi
    printf '%s=%s\n' "$key" "$value" >> "$tmp"
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
  rm -f "$tmp" 2>/dev/null || true
}

gen_code() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import secrets; print(secrets.token_hex(32))'
  else
    die "无法生成绑定码：需要 openssl 或 python3"
  fi
}

current_console="$(read_env_value FORWARDER_BOT_CONSOLE)"
current_console="${current_console:-true}"
current_code="$(read_env_value FORWARDER_BOT_BIND_CODE)"
current_notify="$(read_env_value FORWARDER_FAILURE_NOTIFY)"
current_notify="${current_notify:-true}"
current_summary="$(read_env_value FORWARDER_SUMMARY_ENABLED)"
current_summary="${current_summary:-true}"

mode="${BOT_CONSOLE:-}"
if [ -z "$mode" ] && [ ! -t 0 ]; then
  log "非交互式环境：跳过 Bot 控制台配置（可用 BOT_CONSOLE=on|off 预设）"
  exit 0
fi

case "$mode" in
  ""|on|off|keep) ;;
  *) warn "未知的 BOT_CONSOLE=$mode（应为 on / off），改用交互式选择"; mode="" ;;
esac

if [ -z "$mode" ]; then
  echo
  echo "──────────────────────────────────────────────"
  echo " 是否开启 Telegram Bot 控制台 / 失败通知？"
  echo "   当前配置: FORWARDER_BOT_CONSOLE=$current_console"
  if [ -n "$current_code" ]; then
    echo "   绑定码: 已配置（不显示）"
  else
    echo "   绑定码: 尚未配置"
  fi
  echo
  echo "   开启后：先私聊 bot 发送 /start <绑定码>，绑定为唯一 owner。"
  echo "   之后可远程查看状态/服务器信息、重试失败作品、直接发链接解析。"
  echo
  echo "   1) 开启（推荐；会生成新的绑定码）"
  echo "   2) 保持现状"
  echo "   3) 关闭"
  echo "──────────────────────────────────────────────"
  printf " 请选择 [1/2/3]: "
  read -r choice || choice=""
  case "${choice:-}" in
    1) mode="on" ;;
    2) mode="keep" ;;
    3) mode="off" ;;
    *) mode="keep"; warn "未识别的选择（$choice），保持现状" ;;
  esac
  echo "──────────────────────────────────────────────"
fi

if [ "$mode" = "off" ]; then
  write_env_value "FORWARDER_BOT_CONSOLE" "false"
  log "已关闭 Bot 控制台（FORWARDER_BOT_CONSOLE=false）"
  log "提示: 修改 .env 后需重启服务——sudo systemctl restart douyin-forward"
  exit 0
fi

if [ "$mode" = "keep" ]; then
  if [ -z "$current_code" ]; then
    log "保持现状（未配置绑定码；如需绑定请重新运行并选择开启）"
  else
    log "保持现状（绑定码已存在；绑定成功后会自动失效，无需手动删除）"
  fi
  exit 0
fi

# mode = on
write_env_value "FORWARDER_BOT_CONSOLE" "true"

# 选择“开启”时默认生成新绑定码；自动化部署可显式传 BOT_BIND_CODE 复用
code="${BOT_BIND_CODE:-}"
if [ -z "$code" ]; then
  code="$(gen_code)"
fi
write_env_value "FORWARDER_BOT_BIND_CODE" "$code"

notify="${FAILURE_NOTIFY:-}"
if [ -z "$notify" ]; then
  if [ -t 0 ]; then
    echo
    printf " 失败作品达到重试上限时推送通知？[Y/n]: "
    read -r ans || ans=""
    case "${ans:-}" in
      n|N|no|NO|0) notify="off" ;;
      *) notify="on" ;;
    esac
  else
    notify="$current_notify"
  fi
fi
case "$notify" in
  off|false|0|no) write_env_value "FORWARDER_FAILURE_NOTIFY" "false"; notify="off" ;;
  *) write_env_value "FORWARDER_FAILURE_NOTIFY" "true"; notify="on" ;;
esac

summary="${SUMMARY:-}"
if [ -z "$summary" ]; then
  if [ -t 0 ]; then
    printf " 每天发送一次运行摘要？[Y/n]: "
    read -r ans || ans=""
    case "${ans:-}" in
      n|N|no|NO|0) summary="off" ;;
      *) summary="on" ;;
    esac
  else
    summary="$current_summary"
  fi
fi
case "$summary" in
  off|false|0|no) write_env_value "FORWARDER_SUMMARY_ENABLED" "false"; summary="off" ;;
  *) write_env_value "FORWARDER_SUMMARY_ENABLED" "true"; summary="on" ;;
esac

log "已开启 Bot 控制台"
log "  FORWARDER_BOT_CONSOLE=true"
log "  FORWARDER_FAILURE_NOTIFY=$notify"
log "  FORWARDER_SUMMARY_ENABLED=$summary"
echo
echo "  绑定码（只显示这一次，请立即保存）:"
echo
echo "    $code"
echo
echo "  下一步："
echo "    1) 在 Telegram 私聊你的 bot，发送：/start $code"
echo "    2) 收到「绑定成功」后即可使用 /status /server /help"
echo "    3) 绑定码会被消费，后续重启不再需要"
echo
log "提示: 修改 .env 后需重启服务——sudo systemctl restart douyin-forward"
