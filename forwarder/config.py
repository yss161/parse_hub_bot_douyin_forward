# -*- coding: utf-8 -*-
"""转发链路配置。

全部通过环境变量或仓库根目录 `.env` 配置（样例见 `.env.example`）。
本模块不依赖 parse_hub_bot，可独立导入。
"""

from __future__ import annotations

import os
import re
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

# 监测昵称列表的分隔符（兼容中英文逗号 / 分号 / 换行）
_ACCOUNT_SEP_RE = re.compile(r"[,，;；\n]+")


def split_accounts(raw: str) -> list[str]:
    """把"监测昵称"配置拆分为列表（自动去空白；支持多个用户同时监测）。"""
    if not raw:
        return []
    return [p.strip() for p in _ACCOUNT_SEP_RE.split(raw) if p.strip()]


def normalize_live_photo_mode(raw: str) -> str:
    """规范化实况图模式。

    video  ：只发视频（最稳）
    live   ：只发实况图
    both   ：实况图 + 视频
    comment：视频/相册发频道，实况图逐条发该帖评论区
    """
    value = (raw or "video").strip().lower()
    if value in ("live", "2", "livephoto", "live_photo", "实况", "实况图"):
        return "live"
    if value in ("both", "3", "all", "两者", "都要"):
        return "both"
    if value in ("comment", "4", "comments", "评论区", "评论"):
        return "comment"
    return "video"


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


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int_list(name: str, default: tuple[int, ...] = ()) -> list[int]:
    raw = _env(name)
    if not raw:
        return list(default)
    values: list[int] = []
    for part in re.split(r"[,，;；\s]+", raw):
        if not part:
            continue
        try:
            values.append(int(part))
        except ValueError:
            continue
    return values


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
    # 监测的大号昵称：用于在私信会话列表中定位"大号 → 小号"的会话；
    # 支持【多个】（英文/中文逗号、分号或换行分隔）→ 同时监测多个聊天用户/会话：
    #   DOUYIN_MAIN_ACCOUNT=我的大号
    #   DOUYIN_MAIN_ACCOUNTS=小明,小红        ← 多会话写法（优先于单数形式）
    # 留空时自动选择"有未读角标"的会话，否则选第一条会话。
    main_account: str = field(
        default_factory=lambda: _env("DOUYIN_MAIN_ACCOUNTS") or _env("DOUYIN_MAIN_ACCOUNT")
    )
    chat_url: str = field(default_factory=lambda: _env("DOUYIN_CHAT_URL", "https://www.douyin.com/chat?isPopup=1"))
    home_url: str = field(default_factory=lambda: _env("DOUYIN_HOME_URL", "https://www.douyin.com"))
    headless: bool = field(default_factory=lambda: _env_bool("DOUYIN_HEADLESS", True))
    qr_timeout: int = field(default_factory=lambda: _env_int("DOUYIN_QR_TIMEOUT", 300))
    poll_interval: int = field(default_factory=lambda: _env_int("DOUYIN_POLL_INTERVAL", 20))
    # ─────────────── 检测模式（reload / probe） ───────────────
    # reload：现状——每轮 page.reload 整页刷新（实时性最好、最稳；资源消耗高）
    # probe ：B 方案——DOM 探针 + 定期强制取数 + 自愈刷新（资源消耗低；最坏延迟约一个取数周期）
    # 运行中可免重启切换：控制台写 "mode probe" / "mode reload"（持久化到 state.json，优先于 .env）
    detect_mode: str = field(default_factory=lambda: _env("DOUYIN_DETECT_MODE", "reload"))
    # probe 模式：DOM 探针间隔（秒）——只读一次页面，不发网络请求
    probe_interval: int = field(default_factory=lambda: _env_int("DOUYIN_PROBE_INTERVAL", 10))
    # probe 模式：强制取数间隔（秒）——无论探针是否变化都点一次会话，防止推送静默失效导致漏消息
    force_fetch_interval: int = field(default_factory=lambda: _env_int("DOUYIN_FORCE_FETCH_INTERVAL", 45))
    # probe 模式：自愈刷新间隔（秒）——定期整页 reload 重置页面状态；0 = 关闭
    reload_interval: int = field(default_factory=lambda: _env_int("DOUYIN_RELOAD_INTERVAL", 7200))
    # probe 模式：连续取数失败多少次后触发自愈刷新
    reload_on_failures: int = field(default_factory=lambda: _env_int("DOUYIN_RELOAD_ON_FAILURES", 3))
    # ─────────────── 实况图（Live Photo） ───────────────
    # video：只发视频（最稳）；live：只发实况图；both：实况图 + 视频
    # comment：视频/相册发频道，实况图逐条发该帖评论区（频道需开启评论区，bot 在关联讨论组）
    # 仅对解析结果为 LivePhotoFile 的作品生效；视频超过 Telegram 限制（10 秒 / 10MB）时自动改发视频
    live_photo_mode: str = field(default_factory=lambda: _env("FORWARDER_LIVE_PHOTO_MODE", "video"))
    live_photo_max_seconds: int = field(
        default_factory=lambda: _env_int("FORWARDER_LIVE_PHOTO_MAX_SECONDS", 10)
    )
    live_photo_max_mb: int = field(default_factory=lambda: _env_int("FORWARDER_LIVE_PHOTO_MAX_MB", 10))
    # 实况图数量超过该值时实况图回退为视频（避免逐条发送刷屏）；0 = 不限制
    # 只对 live / both 模式生效；comment 模式评论区不限量（逐张发到评论区）。
    live_photo_max_items: int = field(default_factory=lambda: _env_int("FORWARDER_LIVE_PHOTO_MAX_ITEMS", 10))
    # 逐条发送实况图时的间隔（秒），用于降低连续上传触发限流/风控的概率；0 = 不等待
    # comment 模式评论根建立后，每张 Live Photo 之间等待该间隔；live/both 模式同理。
    live_photo_send_interval: float = field(
        default_factory=lambda: _env_float("FORWARDER_LIVE_PHOTO_SEND_INTERVAL", 2.0)
    )
    # ─────────────── 背景音乐（BGM）评论区归档 ───────────────
    # off    ：不解析/不发送 BGM
    # comment：图片/图文/实况作品解析 BGM，上传到对应频道帖的评论区（普通视频默认不处理）
    bgm_mode: str = field(default_factory=lambda: _env("FORWARDER_BGM_MODE", "comment"))
    # 普通视频是否也发 BGM（默认 false：视频本身已包含声音，避免重复）
    bgm_on_video: bool = field(default_factory=lambda: _env_bool("FORWARDER_BGM_ON_VIDEO", False))
    # 是否在本地保留 music.mp3 副本（默认 false：上传后删除临时文件，节省服务器空间）
    bgm_local_copy: bool = field(default_factory=lambda: _env_bool("FORWARDER_BGM_LOCAL_COPY", False))
    bgm_max_mb: int = field(default_factory=lambda: _env_int("FORWARDER_BGM_MAX_MB", 20))
    bgm_timeout: int = field(default_factory=lambda: _env_int("FORWARDER_BGM_TIMEOUT", 30))
    # ─────────────── 兜底：视频转 Live Photo ───────────────
    # 当来源确认是实况作品、但兜底 bot 只输出视频时，用 OpenCV 抽第一帧配对成 Live Photo。
    video_as_live: bool = field(default_factory=lambda: _env_bool("FORWARDER_VIDEO_AS_LIVE", True))
    video_as_live_max_seconds: int = field(
        default_factory=lambda: _env_int("FORWARDER_VIDEO_AS_LIVE_MAX_SECONDS", 10)
    )
    video_as_live_max_mb: int = field(default_factory=lambda: _env_int("FORWARDER_VIDEO_AS_LIVE_MAX_MB", 10))
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

    # ─────────────── Bot 控制台 / Owner 绑定 ───────────────
    # 一次性绑定码：用户在私聊中发送 /start <code> 或 /auth <code>，第一个成功者成为 owner。
    # 留空 = 不启用绑定/控制台（已有 owner 时仍会启动控制台）。生成：openssl rand -hex 32
    bot_bind_code: str = field(default_factory=lambda: _env("FORWARDER_BOT_BIND_CODE"))
    # 绑定码有效期（秒）；从服务启动开始计时，0 = 不限制。默认 15 分钟。
    bind_code_ttl: int = field(default_factory=lambda: _env_int("FORWARDER_BIND_CODE_TTL", 900))
    # 是否启用 bot 控制台（绑定成功后可用 /status、/retry、/parse 等）
    bot_console: bool = field(default_factory=lambda: _env_bool("FORWARDER_BOT_CONSOLE", True))
    # 最终失败时是否私聊 owner（需要先绑定；只发一次，避免重试刷屏）
    failure_notify: bool = field(default_factory=lambda: _env_bool("FORWARDER_FAILURE_NOTIFY", True))
    # 允许 owner 在关联讨论组里直接发链接/使用 /parse（群默认 privacy mode 可能收不到普通消息）
    parse_in_group: bool = field(default_factory=lambda: _env_bool("FORWARDER_PARSE_IN_GROUP", True))

    # ─────────────── 失败链接兜底群 ───────────────
    # 解析最终失败后，把原始链接发到兜底群/频道；兜底 bot 出片后本 bot 补 Live Photo。
    # 留空 = 关闭。也可以直接填成品频道（与兜底 bot 同频道时只补评论区，不重复发媒体）。
    fallback_chat: str = field(default_factory=lambda: _env("FORWARDER_FALLBACK_CHAT"))
    # 兜底 bot 的用户名或数字 ID（用于识别它的出片消息）
    fallback_bot: str = field(default_factory=lambda: _env("FORWARDER_FALLBACK_BOT", "@ParseHubot"))
    # 等待兜底 bot 出片的超时（秒）；超时保留链接并通知 owner
    fallback_timeout: int = field(default_factory=lambda: _env_int("FORWARDER_FALLBACK_TIMEOUT", 180))
    # 成功后是否删除我们发出的链接消息
    fallback_delete_link: bool = field(
        default_factory=lambda: _env_bool("FORWARDER_FALLBACK_DELETE_LINK", True)
    )
    # 链接消息前缀标记，便于人工识别与日志排查
    fallback_marker: str = field(default_factory=lambda: _env("FORWARDER_FALLBACK_MARKER", "#fallback"))
    # 启动时按消息 ID 清理频道里历史残留的兜底链接（Bot API 不能读取频道历史，只能按已知 ID 删）
    fallback_cleanup_ids: list[int] = field(
        default_factory=lambda: _env_int_list("FORWARDER_FALLBACK_CLEANUP_IDS")
    )

    # ─────────────── 失败重试策略 ───────────────
    # 临时错误最大尝试次数（永久错误只试 1 次；分类见 forwarder/failures.py）
    max_parse_attempts: int = field(default_factory=lambda: _env_int("FORWARDER_MAX_PARSE_ATTEMPTS", 3))
    # 指数退避：第 1 次失败后 60s，第 2 次 120s……上限 30 分钟
    retry_backoff_base: int = field(default_factory=lambda: _env_int("FORWARDER_RETRY_BACKOFF_BASE", 60))
    retry_backoff_max: int = field(default_factory=lambda: _env_int("FORWARDER_RETRY_BACKOFF_MAX", 1800))

    # ─────────────── 磁盘/心跳/摘要 ───────────────
    # 输出目录所在磁盘剩余空间低于该值时暂停解析并告警；0 = 关闭
    min_free_gb: float = field(default_factory=lambda: _env_float("FORWARDER_MIN_FREE_GB", 3.0))
    # 外部心跳 URL（如 healthchecks.io / Uptime Kuma push URL）；留空 = 关闭
    healthcheck_url: str = field(default_factory=lambda: _env("FORWARDER_HEALTHCHECK_URL"))
    healthcheck_interval: int = field(default_factory=lambda: _env_int("FORWARDER_HEALTHCHECK_INTERVAL", 300))
    healthcheck_method: str = field(default_factory=lambda: _env("FORWARDER_HEALTHCHECK_METHOD", "GET").upper())
    # 超过该分钟数没有发现任何新候选时提醒 owner（0 = 关闭；默认 6 小时）
    silent_alert_minutes: int = field(default_factory=lambda: _env_int("FORWARDER_SILENT_ALERT_MINUTES", 360))
    # 每日摘要（本地时间 0-23）；关闭请用 FORWARDER_SUMMARY_ENABLED=false
    summary_enabled: bool = field(default_factory=lambda: _env_bool("FORWARDER_SUMMARY_ENABLED", True))
    summary_hour: int = field(default_factory=lambda: _env_int("FORWARDER_SUMMARY_HOUR", 22))

    # ─────────────── 浏览器看门狗 ───────────────
    # 检查间隔（秒）；页面无响应 / 内存超限 / 定时到期时自动重启 Chromium
    browser_check_interval: int = field(default_factory=lambda: _env_int("FORWARDER_BROWSER_CHECK_INTERVAL", 300))
    browser_max_rss_mb: int = field(default_factory=lambda: _env_int("FORWARDER_BROWSER_MAX_RSS_MB", 1800))
    browser_restart_interval: int = field(default_factory=lambda: _env_int("FORWARDER_BROWSER_RESTART_INTERVAL", 86400))
    # 内容级去重：归档后对最大媒体文件做 SHA-256，同内容换作品 ID 也不重复推送
    content_hash: bool = field(default_factory=lambda: _env_bool("FORWARDER_CONTENT_HASH", True))

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
    # 每次服务启动（小号登录成功后）自动把小号登录态 cookie 同步到解析端 platform_config
    # （解决"手动/一次性填的 cookie 会过期"的问题；可选，默认关闭）
    auto_sync_cookie: bool = field(default_factory=lambda: _env_bool("FORWARDER_AUTO_SYNC_COOKIE", False))

    # ─────────────── 调试 ───────────────
    debug: bool = field(default_factory=lambda: _env_bool("FORWARDER_DEBUG", False))
    # 提取失败时把私信页 HTML 转储到 forwarder_data/debug/，便于调整选择器
    dump_html: bool = field(default_factory=lambda: _env_bool("FORWARDER_DUMP_HTML", False))

    # ─────────────── 路径（一般无需修改） ───────────────
    data_dir: Path = field(default_factory=lambda: FORWARDER_DATA_DIR)
    browser_profile_dir: Path = field(default_factory=lambda: FORWARDER_DATA_DIR / "browser_profile")
    state_file: Path = field(default_factory=lambda: FORWARDER_DATA_DIR / "state.json")
    # 归档索引 / 失败收件箱 / 统计（SQLite；与 state.json 分开，避免状态文件持续膨胀）
    db_file: Path = field(default_factory=lambda: FORWARDER_DATA_DIR / "forwarder.db")
    qr_image_path: Path = field(default_factory=lambda: FORWARDER_DATA_DIR / "login_qr.png")
    debug_dir: Path = field(default_factory=lambda: FORWARDER_DATA_DIR / "debug")

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.browser_profile_dir, self.output_dir, self.debug_dir):
            p.mkdir(parents=True, exist_ok=True)

    @property
    def main_accounts(self) -> list[str]:
        """监测昵称列表（拆分后的结果；为空表示未设置、自动选择会话）。"""
        return split_accounts(self.main_account)

    def normalized_live_photo_mode(self) -> str:
        """规范化实况图模式（兼容 1/2/3、live/both 等写法）。"""
        return normalize_live_photo_mode(self.live_photo_mode)

    def normalized_detect_mode(self) -> str:
        """规范化检测模式：只认 reload / probe（兼容 b、探针等别名）。"""
        raw = (self.detect_mode or "reload").strip().lower()
        return "probe" if raw in ("probe", "b", "planb", "plan_b", "探针", "轻量") else "reload"

    def normalized_bgm_mode(self) -> str:
        """规范化 BGM 模式：只认 off / comment。"""
        raw = (self.bgm_mode or "comment").strip().lower()
        return "off" if raw in ("off", "0", "false", "no", "none", "关闭", "不发") else "comment"

    @property
    def fallback_enabled(self) -> bool:
        return bool(self.fallback_chat.strip())

    def describe(self) -> str:
        accounts = self.main_accounts
        if not accounts:
            account_desc = "(未设置，自动选择会话)"
        elif len(accounts) == 1:
            account_desc = accounts[0]
        else:
            account_desc = f"多会话（{len(accounts)} 个）→ {'、'.join(accounts)}"
        lines = [
            "当前配置:",
            f"  监测账号         : {account_desc}",
            f"  聊天页地址       : {self.chat_url}",
            f"  无头模式         : {self.headless}",
            f"  轮询间隔         : {self.poll_interval}s",
            f"  检测模式(.env)   : {self.normalized_detect_mode()}"
            + ("（DOM 探针 + 定期取数）" if self.normalized_detect_mode() == "probe" else "（每轮整页刷新）"),
            f"  探针/取数/自愈   : {self.probe_interval}s / {self.force_fetch_interval}s / "
            f"{self.reload_interval}s（连续失败 {self.reload_on_failures} 次自愈）",
            f"  实况图模式       : {self.normalized_live_photo_mode()}"
            + ("（只发实况图）" if self.normalized_live_photo_mode() == "live"
               else ("（实况图 + 视频）" if self.normalized_live_photo_mode() == "both"
                     else ("（视频发频道，实况图发评论区）" if self.normalized_live_photo_mode() == "comment"
                           else "（只发视频）"))),
            f"  实况图限制       : ≤{self.live_photo_max_seconds}s / ≤{self.live_photo_max_mb}MB"
            + (f" / 最多 {self.live_photo_max_items} 张（0=不限）"
               if self.normalized_live_photo_mode() in ("live", "both")
               else " / 评论区不限量" if self.normalized_live_photo_mode() == "comment"
               else " / 只发视频"),
            f"  实况图发送间隔   : {self.live_photo_send_interval:g}s（逐条上传之间的等待）",
            f"  BGM 归档         : {self.normalized_bgm_mode()}"
            + ("（图片/图文/实况发评论区）" if self.normalized_bgm_mode() == "comment" else "（关闭）")
            + (f"；普通视频也发" if self.bgm_on_video else "；普通视频不发"),
            f"  兜底视频转实况   : {'已启用' if self.video_as_live else '关闭'}"
            + f"（≤{self.video_as_live_max_seconds}s / ≤{self.video_as_live_max_mb}MB）",
            f"  失败兜底群       : {self.fallback_chat or '未配置'}",
            f"  解析最小间隔     : {self.parse_min_interval}s",
            f"  输出目录         : {self.output_dir}",
            f"  跳过媒体处理     : {self.skip_media_processing}",
            f"  TG 推送          : {('已启用 → ' + self.tg_target) if self.tg_push and self.tg_target else '未启用'}",
            f"  Bot 控制台       : {'已启用' if self.bot_console else '未启用'}"
            + (f"（绑定码有效期 {self.bind_code_ttl}s）" if self.bot_bind_code else "（未配置绑定码；已有 owner 仍可用）"),
            f"  失败通知         : {'已启用' if self.failure_notify else '未启用'}（需先绑定 owner）",
            f"  重试策略         : 最多 {self.max_parse_attempts} 次；退避 {self.retry_backoff_base}s → {self.retry_backoff_max}s",
            f"  磁盘水位         : {self.min_free_gb:g} GB（低于则暂停解析）" if self.min_free_gb > 0 else "  磁盘水位         : 关闭",
            f"  心跳/静默告警    : {self.healthcheck_url or '未配置'} / {self.silent_alert_minutes}min",
            f"  每日摘要         : {'%02d:00' % self.summary_hour if self.summary_enabled and self.summary_hour >= 0 else '关闭'}",
            f"  浏览器看门狗     : 每 {self.browser_check_interval}s；无响应 / RSS>{self.browser_max_rss_mb}MB / 运行 {self.browser_restart_interval}s 且空闲时重启",
            f"  内容级去重       : {'已启用' if self.content_hash else '关闭'}",
            f"  解析 cookie 自动同步: {self.auto_sync_cookie}（启动时把小号 cookie 同步到解析端）",
            f"  推送后清缓存     : {self.cleanup_after_push}（仅推送成功时执行）",
            f"  缓存保留时长     : {self.cache_ttl_hours}h（0 = 关闭定时清理）",
            f"  登录预览端口     : {self.shot_port}（0 = 关闭；截图/菜单查看用）",
            f"  调试/转储 HTML   : {self.debug}/{self.dump_html}",
        ]
        return "\n".join(lines)
