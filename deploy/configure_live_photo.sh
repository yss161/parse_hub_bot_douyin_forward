#!/usr/bin/env bash
# ============================================================
# 实况图（Live Photo）模式配置 —— 交互式写入 .env
#
# 用法：
#   bash deploy/configure_live_photo.sh            # 交互式选择
#   sudo bash deploy/configure_live_photo.sh       # .env 属主是服务用户时
#
# 非交互预设（自动化部署用）：
#   LIVE_PHOTO_MODE=video bash deploy/configure_live_photo.sh
#   LIVE_PHOTO_MODE=live  bash deploy/configure_live_photo.sh
#   LIVE_PHOTO_MODE=both  bash deploy/configure_live_photo.sh
#
# 说明：
#   1) video：只发视频（现状，最稳；实况图会变成普通视频）
#   2) live ：只发实况图；视频超过 Telegram 限制（10 秒 / 10MB）时自动改发视频
#   3) both ：实况图 + 视频；超限的只发视频
#   只对解析结果为实况图的作品生效；普通视频/图集不受影响。
#   实况图数量超过 FORWARDER_LIVE_PHOTO_MAX_ITEMS（默认 3）时整帖回退视频，避免刷屏。
#
# 运行中也可免重启切换：往 forwarder_data/agent_cmd.txt 写 "live live" / "live both" / "live video"
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"

log()  { printf '\033[1;32m[live-photo]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[live-photo]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[live-photo]\033[0m %s\n' "$*" >&2; exit 1; }

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

current="$(read_env_value FORWARDER_LIVE_PHOTO_MODE)"
current="${current:-video}"
mode="${LIVE_PHOTO_MODE:-}"

# 非交互环境且未预设 → 跳过（不影响部署流程）
if [ -z "$mode" ] && [ ! -t 0 ]; then
  log "非交互式环境：跳过实况图模式配置（可用 LIVE_PHOTO_MODE=video|live|both 预设）"
  exit 0
fi

case "$mode" in
  ""|video|live|both|keep) ;;
  *) warn "未知的 LIVE_PHOTO_MODE=$mode（应为 video / live / both），改用交互式选择"; mode="" ;;
esac

if [ -z "$mode" ]; then
  echo
  echo "──────────────────────────────────────────────"
  echo " 实况图（Live Photo）怎么发？"
  echo "   当前配置: $current"
  echo
  echo "   1) video（现状，默认）"
  echo "      ✅ 只发视频，最稳、聊天最干净（实况图会变成普通视频）"
  echo "      ❌ 丢失实况图的静态图/实况效果"
  echo
  echo "   2) live（只发实况图）"
  echo "      ✅ 聊天里显示为实况图（静态图 + 动态）"
  echo "      ❌ 旧客户端只显示静态图；没有视频备份；多图会逐条发送"
  echo "      ℹ 视频超过 Telegram 限制（10 秒 / 10MB）时自动改发视频"
  echo
  echo "   3) both（实况图 + 视频）"
  echo "      ✅ 实况图和视频都保留，兼容性最好"
  echo "      ❌ 每条实况图会多发一条视频；流量/存储约翻倍"
  echo "      ℹ 超限的只发视频；实况图超过 3 张时整帖回退视频，避免刷屏"
  echo
  echo "   4) 保持不变（回车默认）"
  echo "──────────────────────────────────────────────"
  printf " 请选择 [1/2/3/4]: "
  read -r choice || choice=""
  case "${choice:-}" in
    1) mode="video" ;;
    2) mode="live" ;;
    3) mode="both" ;;
    4) mode="keep" ;;
    "")
      if [ -n "${current:-}" ]; then mode="keep"; else mode="video"; fi
      ;;
    *)
      mode="keep"
      warn "未识别的选择（$choice），保持现有配置"
      ;;
  esac
  echo "──────────────────────────────────────────────"
fi

if [ "$mode" = "keep" ]; then
  log "保持现有实况图模式: $current"
  exit 0
fi

write_env_value "FORWARDER_LIVE_PHOTO_MODE" "$mode"
[ -n "$(read_env_value FORWARDER_LIVE_PHOTO_MAX_SECONDS)" ] || write_env_value "FORWARDER_LIVE_PHOTO_MAX_SECONDS" "10"
[ -n "$(read_env_value FORWARDER_LIVE_PHOTO_MAX_MB)" ] || write_env_value "FORWARDER_LIVE_PHOTO_MAX_MB" "10"
[ -n "$(read_env_value FORWARDER_LIVE_PHOTO_MAX_ITEMS)" ] || write_env_value "FORWARDER_LIVE_PHOTO_MAX_ITEMS" "3"

case "$mode" in
  live) log "已写入实况图模式: FORWARDER_LIVE_PHOTO_MODE=live（只发实况图，超限自动改视频）" ;;
  both) log "已写入实况图模式: FORWARDER_LIVE_PHOTO_MODE=both（实况图 + 视频，超限只发视频）" ;;
  *)    log "已写入实况图模式: FORWARDER_LIVE_PHOTO_MODE=video（只发视频，现状）" ;;
esac

log "提示: 修改 .env 后需重启服务生效——sudo systemctl restart douyin-forward"
log "提示: 运行中也可免重启切换——echo \"live both\" | sudo tee $PROJECT_DIR/forwarder_data/agent_cmd.txt"
