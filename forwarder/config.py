# -*- coding: utf-8 -*-
"""转发链路配置。

全部通过环境变量或仓库根目录 `.env` 配置（样例见 `.env.example`）。
本模块不依赖 parse_hub_bot，可独立导入。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# 仓库根目录（douyin_download_bot/），forwarder/ 的上一级
ROOT_DIR = Path(__file__).resolve().parent.parent
# 转发链路的运行数据（浏览器登录态、状态文件、二维码截图、调试转储等）
FORWARDER_DATA_DIR = ROOT_DIR / "forwarder_data"

# 加载配置：先根目录 .env，再 forwarder/.env（均不覆盖已存在的环境变量）
load_dotenv(ROOT_DIR / ".env")
load_dotenv(ROOT_DIR / "forwarder" / ".env")


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on", "y")


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_path(name: str, default: Path) -> Path:
    raw = _env(name)
    if not raw:
        return default
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (ROOT_DIR / p).resolve()


@dataclass
class ForwarderSettings:
    """转发链路设置。"""

    # ─────────────── 抖音端（小号/接收） ───────────────
    # 大号昵称关键字：用于在私信会话列表中定位"大号 → 小号"的会话；
    # 留空时自动选择"有未读角标"的会话，否则选第一条会话。
    main_account: str = field(default_factory=lambda: _env("DOUYIN_MAIN_ACCOUNT"))
    chat_url: str = field(default_factory=lambda: _env("DOUYIN_CHAT_URL", "https://www.douyin.com/chat?isPopup=1"))
    home_url: str = field(default_factory=lambda: _env("DOUYIN_HOME_URL", "https://www.douyin.com"))
    headless: bool = field(default_factory=lambda: _env_bool("DOUYIN_HEADLESS", True))
    qr_timeout: int = field(default_factory=lambda: _env_int("DOUYIN_QR_TIMEOUT", 300))
    poll_interval: int = field(default_factory=lambda: _env_int("DOUYIN_POLL_INTERVAL", 20))
    max_clicks_per_poll: int = field(default_factory=lambda: _env_int("DOUYIN_MAX_CLICKS", 3))
    # 只处理"对方"（大号）发来的消息，忽略小号自己的消息
    only_others: bool = field(default_factory=lambda: _env_bool("DOUYIN_ONLY_OTHERS", True))
    # 首次运行时是否处理会话中的历史消息（默认 false：只处理服务启动后新转发的视频）
    process_history: bool = field(default_factory=lambda: _env_bool("DOUYIN_PROCESS_HISTORY", False))
    # 可选：指定 Chromium/Chrome 可执行文件路径（不设置则用 playwright install chromium 安装的版本）
    chromium_path: str = field(default_factory=lambda: _env("DOUYIN_CHROMIUM_PATH"))

    # ─────────────── Telegram 推送（可选） ───────────────
    # 开启后：解析完成的媒体自动推送至指定群组/频道
    tg_push: bool = field(default_factory=lambda: _env_bool("TG_PUSH", False))
    # 目标：@用户名（公开频道/群）、-100 开头的数字 ID（私有）、或 t.me 链接
    tg_target: str = field(default_factory=lambda: _env("TG_TARGET_CHAT"))

    # ─────────────── 登录截图链接服务（把二维码/页面截图变成可点击链接） ───────────────
    # 只读 HTTP 服务端口（0 = 关闭；仅暴露白名单里的截图/菜单文件，不含登录态）
    shot_port: int = field(default_factory=lambda: _env_int("FORWARDER_SHOT_PORT", 8765))
    # 监听地址（默认 0.0.0.0；只允许本机/隧道访问可设为 127.0.0.1）
    shot_bind: str = field(default_factory=lambda: _env("FORWARDER_SHOT_BIND", "0.0.0.0"))
    # 对外主机名/IP（留空 = 自动探测公网 IP；AWS 等 NAT 实例可用它显式指定公网 IP）
    shot_host: str = field(default_factory=lambda: _env("FORWARDER_SHOT_HOST"))

    # ─────────────── 本地缓存清理 ───────────────
    # 推送成功的作品自动删除本地归档目录（仅当 TG 推送成功时执行；避免磁盘持续增长）
    cleanup_after_push: bool = field(default_factory=lambda: _env_bool("FORWARDER_CLEANUP_AFTER_PUSH", True))
    # 定时清理：删除超过 N 小时的本地缓存（output/ 归档、debug/ 转储、downloads/ 残留）；0 = 关闭
    cache_ttl_hours: int = field(default_factory=lambda: _env_int("FORWARDER_CACHE_TTL_HOURS", 72))

    # ─────────────── 解析/归档 ───────────────
    output_dir: Path = field(default_factory=lambda: _env_path("FORWARDER_OUTPUT_DIR", ROOT_DIR / "output"))
    # 两个视频之间的最小解析间隔（秒），避免请求过于频繁
    parse_min_interval: int = field(default_factory=lambda: _env_int("FORWARDER_PARSE_MIN_INTERVAL", 8))
    # 跳过原项目的 TG 媒体处理（转码/长图切割），归档原始文件（推荐，速度更快）
    skip_media_processing: bool = field(default_factory=lambda: _env_bool("FORWARDER_SKIP_MEDIA_PROCESSING", True))
    save_metadata: bool = field(default_factory=lambda: _env_bool("FORWARDER_SAVE_METADATA", True))

    # ─────────────── 调试 ───────────────
    debug: bool = field(default_factory=lambda: _env_bool("FORWARDER_DEBUG", False))
    # 提取失败时把私信页 HTML 转储到 forwarder_data/debug/，便于调整选择器
    dump_html: bool = field(default_factory=lambda: _env_bool("FORWARDER_DUMP_HTML", False))

    # ─────────────── 路径（一般无需修改） ───────────────
    data_dir: Path = field(default_factory=lambda: FORWARDER_DATA_DIR)
    browser_profile_dir: Path = field(default_factory=lambda: FORWARDER_DATA_DIR / "browser_profile")
    state_file: Path = field(default_factory=lambda: FORWARDER_DATA_DIR / "state.json")
    qr_image_path: Path = field(default_factory=lambda: FORWARDER_DATA_DIR / "login_qr.png")
    debug_dir: Path = field(default_factory=lambda: FORWARDER_DATA_DIR / "debug")

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.browser_profile_dir, self.output_dir, self.debug_dir):
            p.mkdir(parents=True, exist_ok=True)

    def describe(self) -> str:
        lines = [
            "当前配置:",
            f"  大号昵称关键字   : {self.main_account or '(未设置，自动选择会话)'}",
            f"  聊天页地址       : {self.chat_url}",
            f"  无头模式         : {self.headless}",
            f"  轮询间隔         : {self.poll_interval}s",
            f"  解析最小间隔     : {self.parse_min_interval}s",
            f"  输出目录         : {self.output_dir}",
            f"  跳过媒体处理     : {self.skip_media_processing}",
            f"  TG 推送          : {('已启用 → ' + self.tg_target) if self.tg_push else '未启用'}",
            f"  推送后清缓存     : {self.cleanup_after_push}（仅推送成功时执行）",
            f"  缓存保留时长     : {self.cache_ttl_hours}h（0 = 关闭定时清理）",
            f"  登录预览端口     : {self.shot_port}（0 = 关闭；截图/菜单查看用）",
            f"  调试/转储 HTML   : {self.debug}/{self.dump_html}",
        ]
        return "\n".join(lines)
