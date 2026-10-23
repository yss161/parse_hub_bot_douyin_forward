#!/usr/bin/env bash
# ============================================================
# 检测模式配置（reload / probe）—— 交互式写入 .env
#
# 用法：
#   bash deploy/configure_detect_mode.sh            # 交互式选择
#   sudo bash deploy/configure_detect_mode.sh       # .env 属主是服务用户时
#
# 非交互预设（自动化部署用）：
#   DETECT_MODE=reload bash deploy/configure_detect_mode.sh
#   DETECT_MODE=probe  bash deploy/configure_detect_mode.sh
#
# 两种模式解析结果完全一致，差别只在"怎么发现新转发"：
#   reload（现状）：每轮整页刷新，实时性最好、最稳；资源消耗高
#   probe （B 方案）：DOM 探针 + 定期强制取数 + 自愈刷新；省资源，最坏延迟约一个取数周期
#
# 运行中也可免重启切换：往 forwarder_data/agent_cmd.txt 写 "mode probe" / "mode reload"
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"

log()  { printf '\033[1;32m[detect-mode]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[detect-mode]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[detect-mode]\033[0m %s\n' "$*" >&2; exit 1; }

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
    # 文件末尾没有换行时先补一个，避免追加后出现多余空行
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

current="$(read_env_value DOUYIN_DETECT_MODE)"
current="${current:-reload}"
mode="${DETECT_MODE:-}"

# 非交互环境且未预设 → 跳过（不影响部署流程）
if [ -z "$mode" ] && [ ! -t 0 ]; then
  log "非交互式环境：跳过检测模式配置（可用 DETECT_MODE=reload|probe 预设）"
  exit 0
fi

case "$mode" in
  ""|reload|probe|keep) ;;
  *) warn "未知的 DETECT_MODE=$mode（应为 reload / probe），改用交互式选择"; mode="" ;;
esac

if [ -z "$mode" ]; then
  echo
  echo "──────────────────────────────────────────────"
  echo " 检测模式：怎么发现「大号转发的新视频」？"
  echo "   当前配置: $current"
  echo
  echo "   1) reload（现状，默认）"
  echo "      每轮整页刷新 + 点开会话取接口数据"
  echo "      ✅ 实时性最好、最稳（推送失效也能发现）"
  echo "      ❌ 资源消耗高：实测每轮约 250 个请求 / 30 秒"
  echo
  echo "   2) probe（B 方案，省资源）"
  echo "      DOM 探针（0 请求）+ 每 45 秒强制取数 + 定期自愈刷新"
  echo "      ✅ 请求量约降 95%+；正常消息约 10 秒内发现"
  echo "      ❌ 推送静默失效时，最坏延迟约一个取数周期（默认 45 秒）"
  echo
  echo "   3) 保持不变（回车默认）"
  echo "   （两种模式的解析结果完全一致，只影响资源占用与检测延迟）"
  echo "──────────────────────────────────────────────"
  printf " 请选择 [1/2/3]: "
  read -r choice || choice=""
  case "${choice:-}" in
    1) mode="reload" ;;
    2) mode="probe" ;;
    3) mode="keep" ;;
    "")
      if [ -n "${current:-}" ]; then mode="keep"; else mode="reload"; fi
      ;;
    *)
      mode="keep"
      warn "未识别的选择（$choice），保持现有配置"
      ;;
  esac
  echo "──────────────────────────────────────────────"
fi

if [ "$mode" = "keep" ]; then
  log "保持现有检测模式: $current"
  exit 0
fi

write_env_value "DOUYIN_DETECT_MODE" "$mode"

if [ "$mode" = "probe" ]; then
  # 仅在用户没有显式配置过时补默认值，避免覆盖手工调优
  [ -n "$(read_env_value DOUYIN_PROBE_INTERVAL)" ] || write_env_value "DOUYIN_PROBE_INTERVAL" "10"
  [ -n "$(read_env_value DOUYIN_FORCE_FETCH_INTERVAL)" ] || write_env_value "DOUYIN_FORCE_FETCH_INTERVAL" "45"
  [ -n "$(read_env_value DOUYIN_RELOAD_INTERVAL)" ] || write_env_value "DOUYIN_RELOAD_INTERVAL" "7200"
  [ -n "$(read_env_value DOUYIN_RELOAD_ON_FAILURES)" ] || write_env_value "DOUYIN_RELOAD_ON_FAILURES" "3"
  log "已写入检测模式: DOUYIN_DETECT_MODE=probe（探针 10s / 强制取数 45s / 自愈 7200s）"
else
  log "已写入检测模式: DOUYIN_DETECT_MODE=reload（现状：每轮整页刷新）"
fi

log "提示: 修改 .env 后需重启服务生效——sudo systemctl restart douyin-forward"
log "提示: 运行中也可免重启切换——echo \"mode probe\" | sudo tee $PROJECT_DIR/forwarder_data/agent_cmd.txt"
