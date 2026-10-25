# -*- coding: utf-8 -*-
"""抖音网页版接收端（运行在服务器上的「小号」）。

职责：
  1. 用 Playwright 打开抖音网页版，扫码登录小号，登录态持久化（重启免扫码）；
  2. 轮询私信会话（大号 → 小号 的转发消息）；
  3. 多策略提取消息中的视频链接：
       ① 消息 HTML / 文本里直接出现的链接（含短链 v.douyin.com）；
       ② 视频卡片消息：点击卡片 → 从新页面 / 同页跳转 / 弹层中读取视频地址；
  4. 去重（消息指纹 + 视频指纹）后交给上层解析。

健壮性说明（重要）：
  - 抖音网页版是 React SPA，类名是哈希值且会随版本变化。本模块所有选择器均为
    "多级候选 + 兜底"，并且不确定时会【不标记已处理】，下一轮重试；
  - 设置 FORWARDER_DUMP_HTML=true 可以把页面 HTML 转储到 forwarder_data/debug/，
    用于在真实账号上调整选择器；
  - 反检测：真实 UA / 时区 / locale、隐藏 webdriver 特征、操作间随机延时；
  - 【风控须知】抖音对"无头浏览器 + 陌生指纹"较敏感，可能返回「验证码中间页」（滑块）。
    本模块会检测该情况：有界面模式（DOUYIN_HEADLESS=false）下提示手动完成验证；
    无头模式下指引改用 xvfb/有界面方式在服务器本机登录（登录态不能跨机器复制）。
    实测（2026-10 于国内家庭宽带）：
      · 有界面首次打开 douyin.com → 正常展示「抖音精选」页并自动弹出登录二维码 ✅
      · 无头模式首次访问 → 易触发滑块验证码（风控），需先用有界面完成一次登录

交互方式（远程控制台，免记忆指令）：
  - 登录页/验证页会自动识别页面上的可点击按钮并生成【编号菜单】输出到日志，
    只需把编号（如 1）写入 forwarder_data/agent_cmd.txt 即可点击对应按钮
    （例：回复 1 = 点击「接收短信验证码」，并附官方刷新时间提醒）；
  - 纯数字（4~8 位，如 123456）= 自动填入验证码输入框；后加 go 表示填入并回车提交；
  - 纯文本（如 我的大号）= 设为"监测的大号昵称"，免编辑 .env、免重启、即时生效；
  - 登录二维码 / 页面预览截图 / 菜单 通过内置只读 HTTP 服务变成【可点击链接】写进日志，
    并在每次自动刷新时重新输出（链接带时间戳，永远是最新画面）。
"""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path

from log import logger
from playwright.async_api import (
    BrowserContext,
    ElementHandle,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from .config import ForwarderSettings, split_accounts
from .shot_server import ShotServer
from .state import StateStore, fingerprint, video_id_from_url

logger = logger.bind(name="Forwarder/DouyinWeb")

# ────────────────────────────── 常量 ──────────────────────────────

STEALTH_SCRIPT = """
// 隐藏 webdriver 标志
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
// 覆盖 chrome 属性
if (!window.chrome) { window.chrome = {}; }
window.chrome.runtime = window.chrome.runtime || {};
// 覆盖 languages
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
// 覆盖 plugins
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
"""

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    # 提取链接时可能打开视频页：禁止自动播放 + 全程静音，
    # 避免视频播放/后台自动暂停带来的干扰与资源消耗
    "--autoplay-policy=user-gesture-required",
    "--mute-audio",
]

# 私信消息中可能出现的抖音链接形式
LINK_PATTERNS = [
    re.compile(r"https?://v\.douyin\.com/[A-Za-z0-9_\-/]+"),
    re.compile(r"https?://(?:www\.)?douyin\.com/(?:video|note)/\d+"),
    re.compile(r"https?://(?:www\.)?iesdouyin\.com/share/(?:video|note)/\d+"),
    re.compile(r"https?://(?:www\.)?douyin\.com/[^\s\"'<>]*(?:modal_id|aweme_id)=\d+[^\s\"'<>]*"),
    re.compile(r"https?://(?:www\.)?douyin\.com/note/\d+"),
]

# 会话列表项（优先使用实测的真实类名，其余为旧版兜底）
CONVERSATION_ITEM_SELECTORS = [
    "[class*='ConversationItemwrapper']",  # 抖音网页版实际类名（2026-10 实测）
    "[class*='conversationConversationItem']",
    "[class*='conversation-item']",
    "[class*='conversationItem']",
    "[class*='session-item']",
    "[class*='chat-item']",
]

# 未读角标（多级候选）
UNREAD_BADGE_SELECTORS = [
    "[class*='unread']",
    "[class*='badge']",
    "[class*='reddot']",
    "[class*='red-dot']",
    "[class*='dot']",
]

# 消息区域容器（优先使用实测的真实类名，其余为旧版兜底）
MESSAGE_CONTAINER_SELECTORS = [
    "[class*='MessageListwrapper']",  # 抖音网页版实际类名（2026-10 实测）
    "[class*='MessageListlist']",
    "[class*='message-list']",
    "[class*='messageList']",
    "[class*='chat-content']",
    "[class*='chatContent']",
]

# 单条消息（优先使用实测的真实类名，其余为旧版兜底）
MESSAGE_ITEM_SELECTORS = [
    "[class*='messageMessageBoxmessageBox']",  # 抖音网页版实际类名：每条消息外层（2026-10 实测）
    "[class*='MessageBoxContentcolumnBox']",
    "[class*='message-item']",
    "[class*='chat-message']",
    "[class*='msg-item']",
]

# 判定"自己的消息"的类名提示（命中则视为小号自己发出，跳过）
SELF_CLASS_HINTS = ("self", "mine", "is-me", "isme", "right")

# 私信消息接口数据中，视频分享卡片的视频 ID 字段（实测 2026-10）
MESSAGE_ITEM_ID_RE = re.compile(r'"itemId"\s*:\s*"(\d{15,})"')

# 消息接口数据中的「会话标记」：每条消息记录归属的会话（如 #0:1:67269605150:7525653966451180602）
CONVERSATION_MARKER_RE = re.compile(r"#\d{1,2}(?::\d{1,25}){1,4}")

# 接口请求体（protobuf 二进制）中的会话 ID（ASCII 形式，如 0:1:67269605150:7525653966451180602）
CONVERSATION_ID_RE = re.compile(rb"\d{1,3}:\d{1,3}:\d{6,25}(?::\d{6,25})?")


# probe 模式探针：一次 evaluate 读取「会话列表 + 消息区尾部」，不发网络请求
PROBE_SCRIPT = r"""
() => {
  const convSel = "[class*='ConversationItemwrapper'],[class*='conversationConversationItem'],"
    + "[class*='conversation-item'],[class*='conversationItem'],[class*='session-item'],[class*='chat-item']";
  const msgSel = "[class*='messageMessageBoxmessageBox'],[class*='MessageBoxContentcolumnBox'],"
    + "[class*='message-item'],[class*='chat-message'],[class*='msg-item']";
  const timeRe = /(\d+\s*(?:秒|分钟|小时|天)前|昨天|前天|刚刚|周[一二三四五六日]|\d{1,2}:\d{2}|\d{1,4}[\/-]\d{1,2}(?:[\/-]\d{1,2})?)/g;
  const clean = (s) => (s || '').replace(/\s+/g, ' ').replace(timeRe, '').trim().slice(0, 160);
  const convs = [...document.querySelectorAll(convSel)].slice(0, 40).map((el) => {
    const unread = !!el.querySelector(
      "[class*='unread'],[class*='badge'],[class*='reddot'],[class*='red-dot']"
    );
    return clean(el.innerText) + (unread ? '|u' : '');
  });
  const boxes = [...document.querySelectorAll(
    "[class*='MessageListwrapper'],[class*='MessageListlist'],[class*='message-list'],"
    + "[class*='messageList'],[class*='chat-content'],[class*='chatContent']"
  )];
  const box = boxes[boxes.length - 1];
  let tail = '';
  if (box) {
    const msgs = box.querySelectorAll(msgSel);
    const last = msgs[msgs.length - 1];
    if (last) tail = clean(last.innerText).slice(0, 120);
  }
  return { n: convs.length, convs, tail };
}
"""

# 登录态 cookie 名称
SESSION_COOKIE_NAMES = {"sessionid", "sid_guard", "sid_tt", "sid_ucp_virtual"}

# ── 登录页「编号菜单」：自动识别这些常见选项并生成编号供回复 ──
LOGIN_OPTION_TEXTS = [
    "接收短信验证码",
    "获取验证码",
    "发送验证码",
    "重新获取",
    "使用密码验证",
    "密码验证",
    "切换验证方式",
    "其他验证方式",
    "同意并登录",
    "确认登录",
    "同意",
    "确认",
    "登录",
    "下一步",
    "刷新二维码",
    "点击刷新",
]

# 命中这些字样即视为"获取短信验证码"类操作（会提示官方 60 秒频率限制）
SMS_OPTION_HINTS = ("接收短信验证码", "获取验证码", "发送验证码", "重新获取")
SMS_COOLDOWN_SECONDS = 60

# 编号菜单里"刷新"项的占位 label
_REFRESH_OPTION = "__refresh__"
# 旧版自由指令的动词集合（向后兼容）
_LEGACY_VERBS = {"click_text", "fill", "type", "press", "click", "drag", "wait"}


# ────────────────────────────── 数据结构 ──────────────────────────────


@dataclass
class IncomingCandidate:
    """一条待解析的候选消息。"""

    url: str
    source: str  # "text"（消息里直接有链接）| "card"（点击视频卡片提取）
    msg_fp: str


# ────────────────────────────── 接收端 ──────────────────────────────


class DouyinWebReceiver:
    def __init__(self, settings: ForwarderSettings, state: StateStore) -> None:
        self.settings = settings
        self.state = state
        self._pw: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._current_conv_text: str | None = None
        # 已捕获的私信消息接口数据（时间戳, 文本），用于准确提取视频卡片 ID
        self._msg_payloads: list[tuple[float, str]] = []
        # 接口请求中出现的会话 ID（时间戳, ID），用于锁定「指定会话」
        self._conv_id_events: list[tuple[float, str]] = []
        # 锁定后的目标会话 ID 集合（多会话监测：只处理这些会话内的消息数据）
        self._target_conv_ids: set[str] = set()
        # 登录截图/菜单的只读 HTTP 服务（日志中输出可点击链接用）
        self._shots: ShotServer | None = None
        # 登录页当前可点击选项（编号菜单）：[(label, note)]
        self._login_options: list[tuple[str, str]] = []
        # 上次输出菜单的内容签名与时间（避免日志刷屏）
        self._login_menu_state: str = ""
        self._last_menu_log_ts: float = 0.0
        # 上次点击"获取短信验证码"类按钮的时间（用于官方 60 秒频率提醒）
        self._last_sms_at: float = 0.0
        # 重复作品/消息的日志去重（同一 key 在单次进程内最多提示一次）
        self._dup_logged: set[str] = set()
        # 控制台要求"立即执行下一轮"的标记（由主循环消费）
        self._force_poll: bool = False
        # 是否已提示过"未能锁定目标会话"
        self._warned_conv_lock: bool = False
        # 上次输出到日志的二维码内容指纹（内容没变不重复输出 base64/字符画）
        self._last_qr_fp: str = ""
        # 当前进程内「正在处理」的消息指纹（用于区分"上次中断"与"本轮处理中"）
        self._inflight: set[str] = set()
        # ── 检测模式（probe / B 方案）运行状态 ──
        self._last_probe_sig: str = ""
        self._last_force_fetch_ts: float = time.monotonic()
        self._last_reload_ts: float = time.monotonic()
        self._probe_failures: int = 0
        self._empty_probe_count: int = 0

    # ══════════════════════ 生命周期 ══════════════════════

    async def start(self) -> None:
        """启动浏览器并确保小号已登录。"""
        await self._ensure_browser()
        self._start_shots()
        await self.ensure_login()
        now = time.monotonic()
        self._last_reload_ts = now
        self._last_force_fetch_ts = now
        _mode = self.effective_detect_mode
        _source = "控制台设置，优先于 .env" if self.state.get_value("detect_mode") else ".env 默认"
        logger.info(
            f"检测模式: {_mode}"
            + ("（DOM 探针 + 定期强制取数 + 自愈刷新）" if _mode == "probe" else "（每轮整页刷新）")
            + f"；轮询间隔 {self.poll_interval():g}s；来源: {_source}"
        )

    async def close(self) -> None:
        if self._shots is not None:
            self._shots.stop()
            self._shots = None
        for closer in (
            getattr(self._context, "close", None),
            getattr(self._pw, "stop", None),
        ):
            if closer is None:
                continue
            try:
                await closer()
            except Exception:
                pass
        self._context = None
        self._page = None
        self._pw = None

    # ────────────── 登录截图链接服务 ──────────────

    def _start_shots(self) -> None:
        """启动登录截图/菜单的只读 HTTP 服务（把截图变成日志里可点击的链接）。"""
        if self._shots is not None:
            return
        server = ShotServer(
            self.settings.data_dir,
            port=self.settings.shot_port,
            bind=self.settings.shot_bind,
            public_host=self.settings.shot_host,
        )
        if server.start():
            self._shots = server

    def _shot_link(self, name: str) -> str:
        """截图/菜单文件的查看链接；HTTP 服务不可用时回退为本地路径。"""
        if self._shots is not None:
            url = self._shots.url(name)
            if url:
                return url
        return str(self.settings.data_dir / name)

    async def _ensure_browser(self) -> None:
        if self._context is not None:
            return
        self.settings.ensure_dirs()

        self._pw = await async_playwright().start()
        kwargs: dict = {
            "user_data_dir": str(self.settings.browser_profile_dir),
            "headless": self.settings.headless,
            "args": LAUNCH_ARGS,
            "viewport": {"width": 1440, "height": 900},
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "user_agent": USER_AGENT,
            "ignore_default_args": ["--enable-automation"],
        }
        if self.settings.chromium_path:
            kwargs["executable_path"] = self.settings.chromium_path

        try:
            self._context = await self._pw.chromium.launch_persistent_context(**kwargs)
        except Exception as e:
            await self._pw.stop()
            self._pw = None
            raise RuntimeError(
                f"启动 Chromium 失败: {e}\n"
                "提示: 1) 确认已执行 `playwright install chromium`；"
                "2) 若提示 profile 被占用，请先关闭其它使用该目录的进程；"
                "3) Linux 服务器缺依赖时执行 `playwright install-deps chromium`。"
            ) from e

        await self._context.add_init_script(STEALTH_SCRIPT)
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        self._page.set_default_timeout(20000)
        # 拦截私信消息接口（准确提取视频卡片的 itemId，见 _capture_message_payload）
        self._page.on("response", self._on_response)
        # 捕获接口请求中的会话 ID（用于锁定目标会话）
        self._page.on("request", self._on_request)
        logger.info(f"浏览器已启动（profile: {self.settings.browser_profile_dir}, headless={self.settings.headless}）")

    # ────────────── 私信消息接口拦截 ──────────────

    def _on_response(self, response: Any) -> None:
        """同步回调：命中消息接口时异步读取响应体。"""
        try:
            if "imapi.douyin.com/v1/message/" in response.url:
                asyncio.ensure_future(self._capture_message_payload(response))
        except Exception:
            pass

    def _on_request(self, request: Any) -> None:
        """同步回调：从消息/会话接口请求（protobuf 请求体）中提取会话 ID。"""
        try:
            url = request.url
            if "imapi.douyin.com" not in url or not ("message" in url or "conversation" in url):
                return
            buf = request.post_data_buffer
            if not buf:
                return
            for m in CONVERSATION_ID_RE.finditer(buf):
                self._record_conv_id(m.group(0).decode("ascii", "ignore"))
        except Exception:
            pass

    def _record_conv_id(self, cid: str) -> None:
        now = time.monotonic()
        self._conv_id_events.append((now, cid))
        cutoff = now - 120
        self._conv_id_events = [e for e in self._conv_id_events if e[0] >= cutoff][-40:]

    async def _capture_message_payload(self, response: Any) -> None:
        """捕获消息接口响应（含分享卡片的 itemId、文本消息原文）。"""
        try:
            body = await response.body()
            text = body.decode("utf-8", errors="ignore")
        except Exception:
            return
        if not text:
            return
        # 无论大小都记录会话标记（用于锁定目标会话）
        for m in CONVERSATION_MARKER_RE.finditer(text):
            self._record_conv_id(m.group(0)[1:])
        # 只有这两个接口返回「完整消息正文」（含分享卡片 itemId）；
        # get_user_message 等小响应是心跳/状态，不进入候选缓存，避免误判"取数成功"
        url = response.url
        if "get_by_conversation" not in url and "get_message_by_init" not in url:
            return
        self._msg_payloads.append((time.monotonic(), text))
        # 只保留最近 5 分钟、最多 10 条
        cutoff = time.monotonic() - 300
        self._msg_payloads = [p for p in self._msg_payloads if p[0] >= cutoff][-10:]
        logger.debug(f"已捕获消息接口数据: {url.split('?')[0]}（{len(text)} 字符）")

    def _collect_payload_candidates(self) -> list[tuple[str, str]]:
        """从已捕获的接口数据中提取候选：(指纹, 链接)。

        - 分享卡片：itemId -> https://www.douyin.com/video/{itemId}
        - 文本消息：正文中的抖音链接（正则）

        已锁定目标会话时，只提取这些会话段内的内容（其他会话/群聊的分享会被忽略）。
        已处理过的作品（视频 ID 级去重）在这里直接跳过，避免重复解析。
        """
        found: dict[str, str] = {}
        for _ts, text in self._msg_payloads:
            for marker, chunk in self._split_payload_sections(text):
                if self._target_conv_ids and marker not in self._target_conv_ids:
                    continue
                for m in MESSAGE_ITEM_ID_RE.finditer(chunk):
                    item_id = m.group(1)
                    key = f"aweme:{item_id}"
                    if key in found:
                        continue
                    if self.state.is_video_processed(item_id):
                        self._log_duplicate(key, f"作品已在库（视频 ID {item_id}），重复转发不再解析")
                        continue
                    found[key] = f"https://www.douyin.com/video/{item_id}"
                for url in self._extract_links(chunk):
                    vid = video_id_from_url(url)
                    if vid and self.state.is_video_processed(vid):
                        self._log_duplicate(f"url:{url}", f"作品已在库（视频 ID {vid}），重复链接不再解析")
                        continue
                    found.setdefault(f"url:{url}", url)
        return list(found.items())

    @staticmethod
    def _split_payload_sections(text: str) -> list[tuple[str, str]]:
        """按会话标记切分消息接口数据。

        返回 [(会话ID, 该段内容)]；第一个标记之前的内容归到空 ID 段（锁定时会被过滤）。
        """
        matches = list(CONVERSATION_MARKER_RE.finditer(text))
        if not matches:
            return [("", text)]
        sections: list[tuple[str, str]] = []
        if matches[0].start() > 0:
            sections.append(("", text[: matches[0].start()]))
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            sections.append((m.group(0)[1:], text[m.end() : end]))
        return sections

    async def _refresh_for_payload(self) -> bool:
        """刷新聊天页（触发消息接口重新拉取最新消息）并打开目标会话（支持多个）。

        期间从接口请求中锁定「目标会话 ID 集合」，供候选提取时按会话过滤。
        返回 True 表示本轮捕获到了较新的接口数据。
        """
        page = self._page
        assert page is not None
        try:
            await page.reload(wait_until="commit", timeout=45000)
        except Exception as e:
            logger.debug(f"刷新私信页失败（继续）: {e}")
        self._current_conv_text = None
        await self._human_delay(3.5, 5.0)

        # 逐个打开目标会话（触发这些会话的消息接口；点击后新出现的会话 ID 即为目标）
        try:
            await self._open_and_lock_conversations()
        except Exception as e:
            logger.debug(f"打开目标会话异常（继续）: {e}")

        now = time.monotonic()
        return any(now - ts <= 30 for ts, _ in self._msg_payloads)

    # ══════════════════════ 登录 ══════════════════════

    async def _is_logged_in(self) -> bool:
        try:
            cookies = await self._context.cookies("https://www.douyin.com")  # type: ignore[union-attr]
        except Exception:
            return False
        names = {c.get("name") for c in cookies}
        return bool(names & SESSION_COOKIE_NAMES)

    async def ensure_login(self) -> None:
        """确保已登录；未登录则走扫码流程（二维码截图保存到本地文件）。"""
        page = self._page
        assert page is not None

        await page.goto(self.settings.home_url, wait_until="domcontentloaded")
        await self._human_delay(3.0, 5.0)

        if await self._is_logged_in():
            logger.info("小号登录态有效 ✅")
            return

        # 风控拦截（验证码中间页）优先处理
        if await self._is_captcha_present():
            await self._handle_captcha_block()
            if await self._is_logged_in():
                return

        logger.warning("未检测到有效登录态，开始扫码登录流程")
        await self._login_flow()
        # 登录完成后清空编号菜单（避免日常运行时误点登录页元素）
        self._login_options = []
        self._login_menu_state = ""
        logger.success("小号登录完成 ✅（可继续使用）")

    # ────────────── 风控检测与处理 ──────────────

    async def _is_captcha_present(self) -> bool:
        """检测是否被风控到验证页面（「验证码中间页」滑块、扫码后的安全验证等）。"""
        page = self._page
        if page is None:
            return False
        try:
            title = await page.title()
            if "验证" in title:
                return True
            for text in ("按住左按钮拖动完成上方拼图", "拖动滑块", "完成拼图", "请完成安全验证"):
                if await page.locator(f"text={text}").count() > 0:
                    return True
        except Exception:
            pass
        return False

    async def _handle_captcha_block(self) -> None:
        """处理「验证码中间页」风控拦截。

        本服务不尝试自动破解验证码（既不可靠也不安全）：
        - 有界面模式（DOUYIN_HEADLESS=false）：提示在浏览器窗口中手动完成滑块验证，等待通过；
        - 无头模式：无法手动操作，指引改用 xvfb/有界面方式在服务器【本机】登录并报错退出
          （登录态不能跨机器复制：浏览器 Cookie 按机器加密，复制后会静默失效）。
        """
        page = self._page
        assert page is not None
        shot = self.settings.data_dir / "login_captcha.png"
        try:
            await page.screenshot(path=str(shot))
        except Exception:
            pass
        shot_link = self._shot_link("login_captcha.png")

        if self.settings.headless:
            raise RuntimeError(
                "抖音返回了「验证码中间页」（滑块验证），无头模式无法手动完成。\n"
                "可选方案：\n"
                "  1) 在服务器上用 xvfb 以「有界面」方式完成一次登录（二维码/截图可通过日志中的预览链接查看）：\n"
                "     sudo -u <服务用户> -H env DOUYIN_HEADLESS=false xvfb-run -a python run.py login\n"
                "  2) 在本地电脑上跑:  DOUYIN_HEADLESS=false python run.py login 手动过滑块后扫码；\n"
                "     ⚠️ 不要把 browser_profile 复制到服务器——Cookie 按机器加密，跨机器复制会静默失效；\n"
                "  3) 间隔一段时间再试（风控可能临时触发），或更换服务器网络出口。\n"
                f"本次页面截图: {shot_link}"
            )

        logger.warning(
            f"检测到抖音「验证码中间页」（滑块验证），请在浏览器窗口中【手动完成验证】；\n"
            f"  当前页面预览: {shot_link}\n"
            f"  （{self.settings.qr_timeout}s 内未通过将超时）"
        )
        deadline = time.monotonic() + self.settings.qr_timeout
        while time.monotonic() < deadline:
            if not await self._is_captcha_present():
                logger.success("滑块验证已通过 ✅")
                await self._human_delay(1.5, 3.0)
                return
            await asyncio.sleep(3)
        raise TimeoutError("验证码未在限时内完成，请重新运行 `python run.py login`。")

    # ────────────── 扫码登录 ──────────────

    async def _capture_qr(self, path: Path) -> bool:
        """定位并截取登录二维码。

        抖音登录弹窗里的二维码是一个 base64 的方形图片（约 160~220px）；
        以「data:image 前缀 + 方形尺寸」双重条件筛选，避免误截 logo 等小图标。

        截取成功后会把【二维码 base64 编码】与【终端字符画版二维码】打印到日志
        （内容变化时才输出）——SSH 会话里不用下载图片，直接扫终端上的字符画即可。
        """
        page = self._page
        assert page is not None
        candidates: list[tuple[ElementHandle, str]] = []   # (元素, data URI；canvas 为空串)

        try:
            for img in await page.query_selector_all("img"):
                try:
                    src = (await img.get_attribute("src")) or ""
                    if not src.startswith("data:image"):
                        continue
                    box = await img.bounding_box()
                    if box and 120 <= box["width"] <= 340 and abs(box["width"] - box["height"]) <= 20:
                        candidates.append((img, src))
                except Exception:
                    continue
        except Exception:
            pass

        try:
            for cv in await page.query_selector_all("canvas"):
                try:
                    box = await cv.bounding_box()
                    if box and 120 <= box["width"] <= 340 and abs(box["width"] - box["height"]) <= 20:
                        candidates.append((cv, ""))
                except Exception:
                    continue
        except Exception:
            pass

        for el, data_uri in candidates:
            try:
                await el.screenshot(path=str(path))
            except Exception:
                continue
            try:
                self._log_qr_details(data_uri, path.read_bytes())
            except Exception:
                pass
            return True
        return False

    def _log_qr_details(self, data_uri: str, png_bytes: bytes) -> None:
        """把二维码 base64 与「终端字符画」输出到日志（内容与上次相同则跳过，避免刷屏）。"""
        fp = fingerprint(png_bytes)
        if fp == self._last_qr_fp:
            return
        self._last_qr_fp = fp

        parts = [
            "【登录二维码 · 终端直扫】（深色背景终端效果最佳；识别不了就用下方 base64 / 预览链接 / 截图文件）",
        ]
        ascii_qr = self._qr_terminal_blocks(png_bytes)
        if ascii_qr:
            parts.append(ascii_qr)
        if data_uri:
            parts.append(f"二维码 base64（即 <img src> 的 data URI，可直接还原为图片）:\n{data_uri}")
        else:
            parts.append(f"（本次二维码来自 canvas 截图、无 base64；图片文件: {self.settings.qr_image_path}）")
        logger.info("\n".join(parts))

    @staticmethod
    def _qr_terminal_blocks(png_bytes: bytes) -> str | None:
        """把二维码 PNG 渲染成 Unicode 半块字符画（终端可直接扫码），失败返回 None。

        不依赖任何二维码库：黑色像素包围盒定位二维码 → 用顶部定位图案的首段黑色游标
        （恰为 7 个模块宽）推算模块尺寸 → 逐模块 3x3 多数采样 → ▀▄█/空格 渲染。
        渲染按「深色背景终端」假设：白模块=亮块 █，黑模块=空格（透出暗背景）。
        """
        try:
            import io

            from PIL import Image

            gray = Image.open(io.BytesIO(png_bytes)).convert("L")
        except Exception:
            return None
        try:
            width, height = gray.size
            if width < 40 or height < 40:
                return None
            bw = gray.point(lambda v: 255 if v < 128 else 0)   # 黑色→255
            bbox = bw.getbbox()                                 # 黑色像素包围盒 (l, t, r, b)
            if not bbox:
                return None
            min_x, min_y = bbox[0], bbox[1]
            max_x, max_y = bbox[2] - 1, bbox[3] - 1
            px = gray.load()

            # 顶部定位图案的首段黑色游标 ≈ 7 个模块宽 → 粗估模块数；
            # 再用包围盒宽度（=模块数×模块尺寸）精化，消除累计漂移
            run = 0
            x = min_x
            while x <= max_x and px[x, min_y] < 128:
                run += 1
                x += 1
            if run < 14:      # 至少 2px/模块，采样才可靠
                return None
            count = round((max_x - min_x + 1) / (run / 7.0))
            if not 21 <= count <= 177:
                return None
            module = (max_x - min_x + 1) / count

            def is_dark(mx: int, my: int) -> bool:
                cx = min_x + int((mx + 0.5) * module)
                cy = min_y + int((my + 0.5) * module)
                total = dark_cnt = 0
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        x2, y2 = cx + dx, cy + dy
                        if 0 <= x2 < width and 0 <= y2 < height:
                            total += 1
                            if px[x2, y2] < 128:
                                dark_cnt += 1
                return dark_cnt * 2 > total

            # 渲染：1 模块=1 字符宽、2 模块=1 字符行（▀▄█/空格），四周留 2 模块静区
            quiet = 2
            grid = count + quiet * 2

            def cell(top_dark: bool, bottom_dark: bool) -> str:
                if top_dark and bottom_dark:
                    return " "
                if not top_dark and not bottom_dark:
                    return "█"
                return "▄" if top_dark else "▀"

            lines = []
            for gy in range(0, grid, 2):
                chars = []
                for gx in range(grid):
                    my1, my2 = gy - quiet, gy + 1 - quiet
                    td = is_dark(gx, my1) if 0 <= my1 < count else False
                    bd = is_dark(gx, my2) if 0 <= my2 < count else False
                    chars.append(cell(td, bd))
                lines.append("".join(chars))
            return "\n".join(lines)
        except Exception:
            return None

    async def _snapshot_login_page(self) -> None:
        """保存登录页面整页截图（排查扫码不生效 / 风控拦截时用）。"""
        page = self._page
        if page is None:
            return
        try:
            path = self.settings.data_dir / "login_page_state.png"
            await page.screenshot(path=str(path))
            logger.debug(f"登录页面快照已更新: {path}（页面地址: {page.url}）")
        except Exception:
            pass

    async def _has_identity_verification(self) -> bool:
        """检测抖音「身份验证」页面（异地登录时的二次验证：短信码/密码）。"""
        page = self._page
        if page is None:
            return False
        try:
            for text in ("为保障账号安全，请先完成身份验证", "接收短信验证码"):
                if await page.locator(f"text={text}").count() > 0:
                    return True
        except Exception:
            pass
        return False

    async def _try_agent_cmd(self) -> bool:
        """执行「远程控制台」输入文件（forwarder_data/agent_cmd.txt，每行一条，执行后自动删除）。

        免记忆指令的对话式交互（登录等待期间每 3 秒检查一次，日常运行每轮轮询检查）：

            1 ~ 9             登录页面编号菜单中的选项（回复 1 = 点击「接收短信验证码」……）
            123456            纯数字（4~8 位）= 填入验证码输入框（登录流程中）
            123456 go         填入验证码并回车提交（go / enter / 回车 均可）
            menu / help / 菜单 重新输出菜单 / 使用说明
            任意其它文本       设为"监测的大号昵称"（免编辑 .env、免重启，即时生效）
            reset / 重置       清除自定义昵称，恢复 .env 默认值
            force / now / 立即 不等轮询周期，立刻执行下一轮检查

        兼容旧版自由指令：click_text / fill / type / press / click x y / drag x1 y1 x2 y2 / wait
        """
        page = self._page
        if page is None:
            return False
        cmd_file = self.settings.data_dir / "agent_cmd.txt"
        if not cmd_file.exists():
            return False
        try:
            lines = [ln.strip() for ln in cmd_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        except Exception:
            lines = []
        try:
            cmd_file.unlink()
        except Exception:
            pass

        executed = False
        for line in lines:
            try:
                if await self._run_console_line(line):
                    executed = True
                    logger.info(f"控制台指令已执行: {line}")
                else:
                    logger.warning(f"控制台指令未执行: {line}")
            except Exception as e:
                logger.warning(f"控制台指令执行失败: {line} -> {e}")

        if executed:
            # 指令执行后：刷新页面快照与菜单，便于连续操作
            await self._snapshot_login_page()
            if self._login_options:
                await self._log_login_menu(force=True)
        return executed

    # ────────────── 远程控制台：解析与执行 ──────────────

    async def _run_console_line(self, line: str) -> bool:
        """执行一条控制台输入，返回是否已处理。"""
        page = self._page
        low = line.strip().lower()

        # 1) 菜单 / 帮助
        if low in ("menu", "help", "菜单", "指令", "状态", "status"):
            if self._login_options:
                await self._log_login_menu(force=True)
            else:
                self._log_console_help()
            return True

        # 2) 清除自定义监测昵称
        if low in ("reset", "重置", "清除", "默认"):
            old = self.state.get_value("main_account")
            self.state.clear_value("main_account")
            self.state.save()
            self._target_conv_ids.clear()
            self._current_conv_text = None
            self._warned_conv_lock = False
            logger.success(
                f"✅ 已清除自定义监测账号（原值: {old or '(无)'}），恢复 .env 默认值: "
                f"{self.settings.main_account or '(未设置，自动选择会话)'}"
            )
            return True

        # 3) 检测模式（reload / probe）：免重启切换
        m = re.fullmatch(
            r"(?:mode|模式|detect|检测模式)\s*(reload|probe|a|b|轮询|探针|reset|重置|default|默认)?",
            low,
        )
        if m:
            return self._handle_mode_command((m.group(1) or "").strip())

        # 4) 实况图模式（video / live / both）：免重启切换
        m = re.fullmatch(
            r"(?:live|livephoto|live_photo|实况|实况图)\s*(video|live|both|1|2|3|视频|实况|两者|reset|重置)?",
            low,
        )
        if m:
            return self._handle_live_photo_command((m.group(1) or "").strip())

        # 5) 编号选项（登录菜单）：回复数字即点击对应按钮
        if re.fullmatch(r"\d{1,2}", low):
            n = int(low)
            if self._login_options:
                if 1 <= n <= len(self._login_options):
                    await self._click_login_option(self._login_options[n - 1][0])
                    return True
                raise RuntimeError(f"选项 {n} 超范围（当前菜单共 {len(self._login_options)} 项）")
            self._log_console_help()
            return True

        # 6) 纯数字验证码：自动填入（可选 go 回车提交）
        m = re.fullmatch(r"(\d{4,8})(?:\s+(go|enter|回车|提交))?", low)
        if m:
            if page is None:
                raise RuntimeError("浏览器尚未启动")
            code, submit = m.group(1), bool(m.group(2))
            if not await self._fill_verification_code(code):
                raise RuntimeError("当前页面没有可见的验证码输入框（登录流程中才有）")
            if submit:
                await page.keyboard.press("Enter")
                await self._human_delay(0.6, 1.2)
                logger.info("已回车提交验证码")
            return True

        # 7) 刷新页面截图
        if low in ("刷新", "refresh"):
            await self._capture_qr(self.settings.qr_image_path)
            await self._snapshot_login_page()
            logger.info(f"已刷新页面截图: {self._shot_link('login_page_state.png')}")
            return True

        # 8) 立即执行下一轮
        if low in ("force", "now", "立即", "现在"):
            self._force_poll = True
            logger.info("已记录：本轮结束后立即执行下一轮检查")
            return True

        # 9) 兼容旧版自由指令
        legacy = await self._run_legacy_command(line)
        if legacy is not None:
            return legacy

        # 10) 页面上的编号选项若能被文本直接命中，也允许按文本点击（免记编号）
        if self._login_options and page is not None:
            for label, _note in self._login_options:
                if label != _REFRESH_OPTION and label and label in line:
                    await self._click_login_option(label)
                    return True

        # 11) 其它纯文本 = 设置"监测账号"（支持多个；对话式添加，免指令、免重启）
        raw = line.strip()
        if not raw or len(raw) > 200:
            raise RuntimeError("无法识别的输入（长度需 1~200 字符）")

        current = self.effective_main_accounts
        if raw.startswith("+"):
            names = split_accounts(raw[1:])
            if not names:
                raise RuntimeError("追加格式：+昵称（多个用逗号分隔，如 +小明,小红）")
            new_list = current + [n for n in names if n not in current]
            action = f"追加 {'、'.join(names)}"
        elif raw.startswith("-"):
            names = split_accounts(raw[1:])
            if not names:
                raise RuntimeError("移除格式：-昵称（多个用逗号分隔，如 -小明,小红）")
            new_list = [n for n in current if n not in names]
            action = f"移除 {'、'.join(names)}"
        else:
            new_list = split_accounts(raw)
            action = "设置"

        self.state.set_value("main_account", ", ".join(new_list))
        self.state.save()
        self._target_conv_ids.clear()
        self._current_conv_text = None
        self._warned_conv_lock = False
        shown = "、".join(new_list) or "(空：自动选择会话)"
        logger.success(
            f"✅ 已{action}监测账号，当前 {len(new_list)} 个: {shown}（免重启，下一轮轮询即生效；"
            "输入 reset 可恢复 .env 默认值）"
        )
        return True

    def _handle_mode_command(self, arg: str) -> bool:
        """检测模式查看/切换（reload=现状，probe=B 方案）。"""
        arg = (arg or "").strip().lower()
        if arg in ("", "status", "状态"):
            self._log_mode_help()
            return True
        if arg in ("reset", "重置", "default", "默认"):
            self.state.clear_value("detect_mode")
            self.state.save()
            self._reset_probe_state()
            logger.success(
                f"检测模式已恢复 .env 默认: {self.settings.normalized_detect_mode()}（下一轮生效）"
            )
            self._log_mode_help()
            return True
        if arg in ("reload", "a", "轮询", "全量", "重载"):
            self.state.set_value("detect_mode", "reload")
            self.state.save()
            self._reset_probe_state()
            logger.success("检测模式已切换为 reload（每轮整页刷新；实时性最好，资源消耗高；下一轮生效）")
            self._log_mode_help()
            return True
        if arg in ("probe", "b", "探针", "轻量"):
            self.state.set_value("detect_mode", "probe")
            self.state.save()
            self._reset_probe_state()
            logger.success("检测模式已切换为 probe（DOM 探针 + 定期强制取数；省资源；下一轮生效）")
            self._log_mode_help()
            return True
        raise RuntimeError("用法: mode reload | mode probe | mode reset")

    def _log_mode_help(self) -> None:
        """输出检测模式说明（含当前参数）。"""
        mode = self.effective_detect_mode
        source = "控制台设置" if self.state.get_value("detect_mode") else ".env 默认"
        lines = [
            f"【检测模式】当前: {mode}（来源: {source}）",
            "  reload（现状）：每轮 page.reload 整页刷新 → 实时性最好、最稳；实测每轮约 250 请求 / 30 秒",
            "  probe （B 方案）：DOM 探针 + 定期强制取数 + 自愈刷新 → 请求量约降 95%+；最坏延迟约一个取数周期",
            "  切换（免重启，下一轮生效）: mode reload | mode probe | mode reset",
            f"  当前参数: 探针 {self.settings.probe_interval}s / 强制取数 {self.settings.force_fetch_interval}s"
            f" / 自愈 {self.settings.reload_interval}s（连续失败 {self.settings.reload_on_failures} 次）",
        ]
        logger.info("\n" + "\n".join(lines))

    def _handle_live_photo_command(self, arg: str) -> bool:
        """实况图模式查看/切换（video=只发视频，live=只发实况图，both=两者）。"""
        arg = (arg or "").strip().lower()
        if arg in ("", "status", "状态"):
            self._log_live_photo_help()
            return True
        if arg in ("reset", "重置", "default", "默认"):
            self.state.clear_value("live_photo_mode")
            self.state.save()
            logger.success(
                f"实况图模式已恢复 .env 默认: {self.settings.normalized_live_photo_mode()}（下一轮生效）"
            )
            self._log_live_photo_help()
            return True
        mapping = {
            "video": "video", "1": "video", "视频": "video",
            "live": "live", "2": "live", "实况": "live", "实况图": "live",
            "both": "both", "3": "both", "两者": "both", "都要": "both",
        }
        mode = mapping.get(arg)
        if not mode:
            raise RuntimeError("用法: live video | live live | live both | live reset")
        self.state.set_value("live_photo_mode", mode)
        self.state.save()
        logger.success(f"实况图模式已切换为 {mode}（下一轮生效）")
        self._log_live_photo_help()
        return True

    def _log_live_photo_help(self) -> None:
        """输出实况图模式说明（含当前限制）。"""
        raw = self.state.get_value("live_photo_mode")
        mode = raw or self.settings.normalized_live_photo_mode()
        source = "控制台设置" if raw else ".env 默认"
        lines = [
            f"【实况图模式】当前: {mode}（来源: {source}）",
            "  video：只发视频（现状，最稳）",
            "  live ：只发实况图；视频超过 Telegram 限制时自动改发视频",
            "  both ：实况图 + 视频；超限的只发视频",
            "  切换（免重启，下一轮生效）: live video | live live | live both | live reset",
            f"  当前限制: ≤{self.settings.live_photo_max_seconds}s / ≤{self.settings.live_photo_max_mb}MB"
            f"；实况图超过 {self.settings.live_photo_max_items} 张时整帖回退视频（0=不限）",
        ]
        logger.info("\n" + "\n".join(lines))

    async def _run_legacy_command(self, line: str) -> bool | None:
        """兼容旧版自由指令；非旧指令返回 None。"""
        page = self._page
        op, _, arg = line.partition(" ")
        op, arg = op.strip().lower(), arg.strip()
        if op not in _LEGACY_VERBS:
            return None
        if page is None:
            raise RuntimeError("浏览器尚未启动")

        if op == "click_text":
            await page.locator(f"text={arg}").first.click(timeout=5000)
        elif op == "fill":
            if not await self._fill_input(arg):
                raise RuntimeError("页面上没有可见的输入框")
        elif op == "type":
            await page.keyboard.type(arg, delay=60)
        elif op == "press":
            await page.keyboard.press(arg or "Enter")
        elif op == "click":
            x, y = (float(v) for v in arg.split()[:2])
            await page.mouse.click(x, y)
        elif op == "drag":
            x1, y1, x2, y2 = (float(v) for v in arg.split()[:4])
            await page.mouse.move(x1, y1)
            await page.mouse.down()
            for i in range(1, 11):
                await page.mouse.move(x1 + (x2 - x1) * i / 10, y1 + (y2 - y1) * i / 10)
                await asyncio.sleep(0.03)
            await page.mouse.up()
        elif op == "wait":
            await asyncio.sleep(float(arg or "1"))
        return True

    async def _find_visible_input(self) -> ElementHandle | None:
        """定位当前页面上"最可能"的目标输入框（验证码/密码优先，兜底最后一个可见 input）。"""
        page = self._page
        if page is None:
            return None
        for sel in ("input[placeholder*='验证码']", "input[type='password']", "input[placeholder*='密码']"):
            for el in await page.query_selector_all(sel):
                try:
                    if await el.is_visible():
                        return el
                except Exception:
                    continue
        # 兜底：取 DOM 中最后一个可见输入框（弹窗一般后置在页面末尾）
        for el in reversed(await page.query_selector_all("input")):
            try:
                if await el.is_visible():
                    return el
            except Exception:
                continue
        return None

    async def _fill_input(self, text: str) -> bool:
        """向页面第一个可见输入框填入内容（优先验证码/密码框）。"""
        target = await self._find_visible_input()
        if target is None:
            return False
        await target.click()
        await target.fill(text)
        return True

    async def _fill_verification_code(self, code: str) -> bool:
        """填入短信验证码。"""
        target = await self._find_visible_input()
        if target is None:
            return False
        await target.click()
        await target.fill(code)
        logger.info(f"已填入验证码: {code}（如需回车提交，输入 `{code} go`）")
        return True

    # ────────────── 登录编号菜单 ──────────────

    async def _login_option_locator(self, label: str):
        """定位登录页面上某个选项（优先 button 角色，兜底文本匹配）。"""
        page = self._page
        assert page is not None
        loc = page.get_by_role("button", name=label)
        try:
            if await loc.count() > 0:
                return loc
        except Exception:
            pass
        return page.locator(f"text={label}")

    async def _collect_login_options(self) -> None:
        """扫描登录页上的可点击项，生成编号菜单（供"回复数字"使用）。"""
        page = self._page
        if page is None:
            return
        options: list[tuple[str, str]] = []
        seen: set[str] = set()
        for text in LOGIN_OPTION_TEXTS:
            try:
                loc = await self._login_option_locator(text)
                if await loc.count() > 0 and await loc.first.is_visible():
                    if text not in seen:
                        seen.add(text)
                        options.append((text, self._option_note(text)))
            except Exception:
                continue
        # 兜底：页面改版时，补充可见按钮/role=button（避免菜单为空）
        if len(options) < 9:
            try:
                for el in await page.query_selector_all("button:visible, [role='button']:visible"):
                    try:
                        label = re.sub(r"\s+", " ", (await el.inner_text()) or "").strip()
                        if not label or len(label) > 14 or label in seen:
                            continue
                        if any(bad in label for bad in ("下载", "客户端", "反馈", "举报")):
                            continue
                        seen.add(label)
                        options.append((label, ""))
                        if len(options) >= 9:
                            break
                    except Exception:
                        continue
            except Exception:
                pass
        # 末尾固定追加"刷新"
        options.append((_REFRESH_OPTION, "刷新页面截图，并重新输出菜单"))
        self._login_options = options

    @staticmethod
    def _option_note(text: str) -> str:
        if any(h in text for h in SMS_OPTION_HINTS):
            return f"⏱ 官方限制约 {SMS_COOLDOWN_SECONDS} 秒一次；验证码有效期约 5 分钟"
        if "密码" in text:
            return "改用账号密码方式通过验证"
        if text in ("登录", "确认登录", "同意并登录", "确认", "下一步"):
            return "提交当前页面操作"
        return ""

    async def _log_login_menu(self, *, force: bool = False) -> None:
        """输出登录操作菜单（编号选项）；菜单未变化时每 60 秒最多重复一次。"""
        await self._collect_login_options()
        state_key = "|".join(label for label, _ in self._login_options)
        now = time.monotonic()
        if not force and state_key == self._login_menu_state and now - self._last_menu_log_ts < 60:
            return
        self._login_menu_state = state_key
        self._last_menu_log_ts = now

        page = self._page
        url = (page.url if page is not None else "") or "-"
        lines = [f"【登录操作菜单】当前页面: {url}"]
        for i, (label, note) in enumerate(self._login_options, start=1):
            if label == _REFRESH_OPTION:
                lines.append(f"  回复 {i}  → 刷新页面截图并重新输出菜单")
            else:
                suffix = f"（{note}）" if note else ""
                lines.append(f"  回复 {i}  → 点击「{label}」{suffix}")
        lines += [
            "  输入 6 位验证码（如 123456）→ 自动填入；输入 `123456 go` → 填入并回车提交",
            "  输入菜单以外的文本 → 设为监测账号（多个用逗号分隔；+追加、-移除，免重启即时生效）",
            "  ⏱ 刷新时间提醒：二维码约 1 分钟过期（本服务每 25 秒自动刷新截图）；",
            f"     短信验证码官方限制约 {SMS_COOLDOWN_SECONDS} 秒一次、有效期约 5 分钟，请勿频繁重发",
            f"  回复方式：把编号或内容写入 {self.settings.data_dir / 'agent_cmd.txt'}（UTF-8，执行后自动删除）",
            f"  菜单文件（可随时查看最新版）: {self._shot_link('login_menu.txt')}",
        ]
        block = "\n".join(lines)
        logger.info("\n" + block)
        try:
            (self.settings.data_dir / "login_menu.txt").write_text(block + "\n", encoding="utf-8")
        except Exception:
            pass

    def _log_console_help(self) -> None:
        """日常运行时的控制台使用说明（无登录菜单时）。"""
        accounts = self.effective_main_accounts
        shown = "、".join(accounts) if accounts else "(未设置：自动选择会话)"
        lines = [
            "【远程控制台】使用说明",
            f"  当前监测账号（{len(accounts)} 个）: {shown}",
            "  任意文本（如：我的大号）→ 设为监测账号；多个用逗号分隔（如：小明,小红）",
            "  +昵称 / -昵称        → 追加 / 移除一个监测账号（多个用逗号分隔）",
            "  reset / 重置        → 清除自定义账号，恢复 .env 默认值",
            "  force / now / 立即  → 不等待轮询周期，立刻执行下一轮检查",
            "  mode / 模式         → 查看/切换检测模式（mode reload | mode probe | mode reset，免重启）",
            "  live / 实况         → 查看/切换实况图模式（live video | live live | live both | live reset）",
            "  菜单 / help         → 重新显示本说明",
            "  纯数字验证码(4~8位)  → 登录流程中填入验证码输入框；`123456 go` = 填入并回车",
            "  登录/验证页会自动给出编号菜单：把数字写入指令文件即可点击对应按钮",
            f"  写入方式: 把内容写入 {self.settings.data_dir / 'agent_cmd.txt'}（UTF-8，每行一条，执行后自动删除）",
            f"  说明文件（可随时查看最新版）: {self._shot_link('agent_help.txt')}",
        ]
        block = "\n".join(lines)
        logger.info("\n" + block)
        try:
            (self.settings.data_dir / "agent_help.txt").write_text(block + "\n", encoding="utf-8")
        except Exception:
            pass

    async def _click_login_option(self, label: str) -> None:
        """点击登录菜单中的一个选项（带官方刷新时间提醒）。"""
        page = self._page
        assert page is not None

        if label == _REFRESH_OPTION:
            await self._capture_qr(self.settings.qr_image_path)
            await self._snapshot_login_page()
            logger.info(f"已刷新页面截图: {self._shot_link('login_page_state.png')}")
            return

        # 短信验证码类操作：提醒官方频率限制（仅提醒，不阻止）
        if any(h in label for h in SMS_OPTION_HINTS):
            now = time.monotonic()
            if self._last_sms_at and now - self._last_sms_at < SMS_COOLDOWN_SECONDS:
                waited = int(now - self._last_sms_at)
                logger.warning(
                    f"⏱ 提示：距上次获取验证码仅 {waited} 秒，抖音官方限制约 "
                    f"{SMS_COOLDOWN_SECONDS} 秒一次，过频可能提示「操作频繁」；"
                    f"建议再等 {max(1, SMS_COOLDOWN_SECONDS - waited)} 秒（仍将尝试点击）"
                )
            self._last_sms_at = now

        loc = await self._login_option_locator(label)
        await loc.first.click(timeout=5000)
        await self._human_delay(1.0, 2.0)
        logger.info(f"已点击「{label}」")
        # 页面状态可能变化：等待加载后刷新快照与菜单
        await asyncio.sleep(0.8)
        await self._snapshot_login_page()
        await self._log_login_menu(force=True)

    async def _log_login_links(self, *, full: bool = False, reason: str = "") -> None:
        """把登录二维码 / 页面预览输出的可点击链接（每次带新时间戳=最新画面）。"""
        qr = self._shot_link("login_qr.png")
        state_shot = self._shot_link("login_page_state.png")
        title = "【登录预览链接】" + (f"（{reason}）" if reason else "")
        lines = [title, f"  二维码   : {qr}", f"  页面预览 : {state_shot}"]
        if full:
            lines += [
                "  ⏱ 二维码约 1 分钟自动过期——请始终使用本行【最新】链接/截图，重复扫旧图无效；",
                "     截图每 25 秒自动刷新，日志中也会同步重新输出。",
                "  打不开链接时：① 云安全组放行端口；② 或 SSH 端口转发后访问 http://127.0.0.1:8765/；",
                f"     ③ 或直接下载文件: {self.settings.data_dir / 'login_qr.png'}",
            ]
        logger.info("\n" + "\n".join(lines))

    # ────────────── 会话设置的大号昵称 ──────────────

    @property
    def effective_main_accounts(self) -> list[str]:
        """当前生效的监测账号列表：会话设置值优先，其次 .env 配置（支持多个）。"""
        raw = self.state.get_value("main_account") or self.settings.main_account
        return split_accounts(raw)

    def consume_force_poll(self) -> bool:
        """取出并清除"立即执行下一轮"标记（供主循环使用）。"""
        value = self._force_poll
        self._force_poll = False
        return value

    def mark_inflight_done(self, fp: str) -> None:
        """消息处理结束（成功/放弃/跳过），从"处理中"集合移除。"""
        self._inflight.discard(fp)

    # ────────────── 重复作品日志去重 ──────────────

    def _log_duplicate(self, key: str, reason: str) -> None:
        """输出"重复作品/消息"提示（同一 key 在单次进程内最多一次，避免刷屏）。"""
        if key in self._dup_logged:
            return
        if len(self._dup_logged) > 2000:
            self._dup_logged.clear()
        self._dup_logged.add(key)
        logger.info(f"检测到重复，已跳过: {reason}")

    # ────────────── 媒体暂停（防止播放干扰） ──────────────

    async def _stop_media(self, page: Page | None = None) -> None:
        """暂停并静音页面上的所有音视频。

        获取链接时会短暂打开视频页/弹层，视频自动播放会带来噪音、资源消耗，
        还会因多标签页被后台自动暂停等造成干扰——这里统一在不使用后立即停下。
        """
        target = page or self._page
        if target is None:
            return
        try:
            await target.evaluate(
                "() => { document.querySelectorAll('video,audio').forEach(el => "
                "{ try { el.pause(); el.muted = true; } catch (e) {} }); }"
            )
        except Exception:
            pass

    async def _login_flow(self) -> None:
        page = self._page
        assert page is not None
        qr_path = self.settings.qr_image_path

        # 抖音首页会重定向到 /jingxuan；未登录时登录弹窗（含二维码）会自动弹出。
        if "douyin.com" not in page.url:
            await page.goto(self.settings.home_url, wait_until="domcontentloaded")
        await self._human_delay(4.0, 6.0)

        if await self._is_captcha_present():
            await self._handle_captcha_block()
            if await self._is_logged_in():
                return

        # 弹窗一般已自动打开；直接尝试截取二维码
        got = await self._capture_qr(qr_path)
        if not got:
            # 兜底：点击可见的登录按钮后重试
            for sel in ("button:has-text('登录'):visible", "span:has-text('登录'):visible"):
                try:
                    await page.locator(sel).first.click(timeout=3000)
                    await self._human_delay(2.0, 3.0)
                    break
                except Exception:
                    continue
            got = await self._capture_qr(qr_path)

        if not got:
            await page.screenshot(path=str(qr_path), full_page=False)
            logger.debug("未定位到二维码元素，已保存整页截图作为兜底")

        logger.warning(
            "\n" + "=" * 64
            + "\n  请用【小号】的抖音 App 扫码登录"
            + "\n  链接与操作菜单如下（也可 scp 下载 forwarder_data/login_qr.png）："
        )
        await self._log_login_links(full=True, reason="扫码登录")
        await self._log_login_menu(force=True)
        logger.warning("=" * 64)

        # 等待扫码（期间定期重新截取二维码，应对二维码过期刷新）
        deadline = time.monotonic() + self.settings.qr_timeout
        last_refresh = time.monotonic()
        idv_warned = False
        while time.monotonic() < deadline:
            if await self._is_logged_in():
                await self._save_storage_state()
                return

            # 远程控制台：执行 agent_cmd.txt 中的输入（编号选项 / 验证码 / 昵称等，执行后自动删除）
            if await self._try_agent_cmd():
                await self._snapshot_login_page()

            # 抖音风控的「身份验证」页面（异地登录常见）：生成编号菜单等待人工操作
            if await self._has_identity_verification():
                first_time = not idv_warned
                if first_time:
                    idv_warned = True
                    logger.warning(
                        "检测到「身份验证」页面（抖音风控要求二次验证，异地登录常见）——已生成【操作菜单】，见下方。\n"
                        f"  查看当前页面预览: {self._shot_link('login_page_state.png')}\n"
                        f"  操作方式: 将菜单编号写入 {self.settings.data_dir / 'agent_cmd.txt'}（如回复 1 表示点击「接收短信验证码」）"
                    )
                await self._log_login_menu(force=first_time)
            # 其他安全验证（滑块等），无头环境无法完成——留存截图并明确报错
            elif await self._is_captcha_present():
                await self._snapshot_login_page()
                raise RuntimeError(
                    "扫码后在浏览器端出现了安全验证页面（滑块等），当前无头环境无法自动完成。\n"
                    f"页面截图已保存: {self._shot_link('login_page_state.png')}（可打开查看）\n"
                    "建议: 1) 给服务器加装 VNC（x11vnc）后按部署文档第 4 节方式③用有界面方式登录；"
                    "2) 或更换服务器网络环境后重试（抖音风控与访问 IP 强相关）。"
                )

            if time.monotonic() - last_refresh > 25:
                last_refresh = time.monotonic()
                got_qr = await self._capture_qr(qr_path)
                await self._snapshot_login_page()
                logger.info(f"等待扫码中（页面: {page.url}，二维码刷新: {'成功' if got_qr else '失败'}）")
                # 刷新时重新输出最新截图链接与菜单（旧二维码已过期，必须使用最新一张）
                await self._log_login_links(reason="自动刷新")
                await self._log_login_menu()
            await asyncio.sleep(3)

        raise TimeoutError(
            f"扫码登录超时（{self.settings.qr_timeout}s）。请重新运行 `python run.py login` 再试。"
        )

    async def _save_storage_state(self) -> None:
        try:
            path = self.settings.data_dir / "storage_state.json"
            await self._context.storage_state(path=str(path))  # type: ignore[union-attr]
            logger.debug(f"登录态备份已保存: {path}")
        except Exception as e:
            logger.debug(f"保存登录态备份失败（不影响运行）: {e}")

    # ══════════════════════ 轮询 ══════════════════════

    async def poll_once(self) -> list[IncomingCandidate]:
        """执行一轮检测，返回新发现的候选链接（已做消息级去重）。

        检测模式（settings.detect_mode，可被控制台覆盖）：
          - reload：现状——每轮 page.reload 整页刷新后提取（最稳、资源消耗高）；
          - probe ：B 方案——DOM 探针 + 定期强制取数 + 自愈刷新（资源消耗低）。
        """
        await self._ensure_browser()
        assert self._page is not None and self._context is not None

        # 登录态失效 → 重新走扫码流程
        if not await self._is_logged_in():
            logger.warning("登录态失效，重新扫码登录")
            await self.ensure_login()

        # 远程控制台：随时可写输入（设置监测昵称 / 切换检测模式 / 立即轮询 / menu 等）
        try:
            await self._try_agent_cmd()
        except Exception as e:
            logger.warning(f"处理控制台输入异常: {e}")

        # 风控拦截检测（节流提示，避免刷屏）
        if await self._is_captcha_present():
            self._warn_captcha_throttled()
            return []

        await self._ensure_chat_page()
        # 防止上一轮打开过的视频仍在播放（自动播放/后台暂停会干扰后续操作）
        await self._stop_media()

        if self.effective_detect_mode == "probe":
            return await self._poll_probe()
        return await self._poll_reload()

    # ────────────── 检测模式：reload（现状） ──────────────

    async def _poll_reload(self) -> list[IncomingCandidate]:
        """现状模式：每轮整页刷新，重新拉取全部消息接口数据。"""
        payload_ok = await self._refresh_for_payload()

        if payload_ok:
            candidates = self._collect_new_candidates()
        else:
            candidates = await self._scan_conversations_dom()

        return self._finalize_candidates(candidates)

    def _collect_new_candidates(self) -> list[IncomingCandidate]:
        """从已捕获的接口数据中提取新候选（含消息级去重与 inflight 标记）。"""
        accounts = self.effective_main_accounts
        if accounts and not self._target_conv_ids and not self._warned_conv_lock:
            self._warned_conv_lock = True
            logger.warning(
                f"未能锁定目标会话 ID（监测昵称 {'、'.join(accounts)} 是否匹配到会话？"
                "可在控制台直接输入新的昵称来更换监测对象），本轮暂按全量扫描"
            )
        candidates: list[IncomingCandidate] = []
        for fp, url in self._collect_payload_candidates():
            if self.state.is_seen_message(fp):
                if self.state.is_settled(fp):
                    # 已有定论（成功/放弃/首轮水位跳过）：不再处理
                    self._log_duplicate(fp, f"{url}（此前已处理过的转发）")
                    continue
                if fp in self._inflight:
                    # 本轮/本进程正在解析中（下载耗时可能跨多轮轮询）：避免重复
                    continue
                # 已识别但既无定论、又不在处理中 → 上次被中断（解析前/下载中重启），恢复处理
                logger.info(f"恢复未完成的消息（上次被中断，重启后补处理）: {url}")
            self.state.mark_seen_message(fp)
            self._inflight.add(fp)
            candidates.append(IncomingCandidate(url=url, source="api", msg_fp=fp))
            logger.info(f"从消息数据中发现转发链接: {url}")
        return candidates

    async def _scan_conversations_dom(self) -> list[IncomingCandidate]:
        """回退：DOM 扫描（消息接口未捕获时）——逐个打开监测会话扫描。"""
        items = await self._conversation_items()
        matched = await self._match_conversation_items(items)
        if not matched:
            logger.warning(
                "未找到私信会话。请检查：1) 大号是否已给小号发过消息；"
                "2) 监测昵称是否与大号昵称匹配（可在控制台直接输入昵称更新）；"
                "3) 必要时设置 FORWARDER_DUMP_HTML=true 转储页面排查。"
            )
            return []
        candidates: list[IncomingCandidate] = []
        for el, text in matched[:10]:
            try:
                await el.click()
                await self._human_delay(1.2, 2.5)
                self._current_conv_text = text
            except Exception as e:
                logger.debug(f"打开会话失败（跳过）: {e}")
                continue
            candidates.extend(await self._scan_messages())
        return candidates

    def _finalize_candidates(self, candidates: list[IncomingCandidate]) -> list[IncomingCandidate]:
        """首轮水位处理 + 落盘，返回最终候选。"""
        # 首次运行：默认仅记录当前消息水位（避免把服务启动前的旧转发全部解析）；
        # 设置 DOUYIN_PROCESS_HISTORY=true 可改为处理历史消息。
        if not self.state.get_flag("history_initialized"):
            self.state.set_flag("history_initialized", True)
            self.state.save()
            if not self.settings.process_history:
                # 把这些历史消息标记为「已定论」，重启后也不会被误当作"中断"而补处理
                for cand in candidates:
                    self.state.mark_settled(cand.msg_fp)
                    self._inflight.discard(cand.msg_fp)
                self.state.save()
                if candidates:
                    logger.info(f"首次运行：已跳过 {len(candidates)} 条历史转发（只处理今后新转发的视频；如需处理历史可设 DOUYIN_PROCESS_HISTORY=true 后重置 state.json）")
                else:
                    logger.info("首次运行：已记录当前消息水位，只处理今后新转发的视频")
                return []
            logger.info("首次运行：DOUYIN_PROCESS_HISTORY=true，历史消息将一并处理")

        self.state.save()
        return candidates

    # ────────────── 检测模式：probe（B 方案） ──────────────

    async def _poll_probe(self) -> list[IncomingCandidate]:
        """B 方案：DOM 探针（0 请求）+ 定期强制取数 + 自愈刷新。

        设计要点：
          - 探针只读一次页面，不触发网络；无变化且未到取数周期 → 直接返回；
          - 强制取数（默认 45s）保证"推送静默失效"时也能靠服务端重同步发现新消息；
          - 连续取数失败 / 定期（默认 2h）自动 reload，避免页面状态长期漂移。
        """
        now = time.monotonic()

        # 首次运行（或 state 被重置）：先完整刷新一次，建立历史水位（与 reload 模式一致）
        if not self.state.get_flag("history_initialized"):
            logger.info("检测模式=probe：首次运行先执行一次完整刷新（建立消息水位）")
            return await self._poll_reload()

        # 自愈：定期 reload / 连续取数失败 / 页面结构缺失
        if self._should_self_heal(now):
            await self._self_heal_reload()
            return self._finalize_candidates(self._collect_new_candidates())

        # 1) 廉价探针（0 网络请求）
        sig = await self._probe_signature()
        if not sig:
            # evaluate 失败（页面可能卡死/正在导航）：累计失败，连续 3 次触发自愈
            changed = False
            self._empty_probe_count += 1
        else:
            self._empty_probe_count = 0
            if not self._last_probe_sig:
                # 进程启动/自愈后的第一次探针：只记录基线，不立即触发取数
                # （页面加载本身已经拉过一轮接口数据，等强制取数周期再兜底）
                changed = False
            else:
                changed = sig != self._last_probe_sig
            self._last_probe_sig = sig

        # 2) 是否到了强制取数时间（服务端重同步，兜住"推送静默失效"）
        force_due = (now - self._last_force_fetch_ts) >= max(5, self.settings.force_fetch_interval)

        if not changed and not force_due:
            logger.debug("probe: 页面无变化，跳过本轮（0 请求）")
            return []

        # 3) 取数：点开目标会话，触发 get_by_conversation
        pulled = await self._open_and_lock_conversations(precise=True)
        if pulled:
            self._last_force_fetch_ts = time.monotonic()
            self._probe_failures = 0
            # 取数后页面结构会变化（打开会话/已读角标消失），稍等 UI 稳定再刷新指纹，避免下一轮重复触发
            await asyncio.sleep(0.6)
            self._last_probe_sig = await self._probe_signature() or self._last_probe_sig
            return self._finalize_candidates(self._collect_new_candidates())

        # 4) 取数失败：探针有变化时用 DOM 扫描兜底；否则累计失败次数等待自愈
        self._probe_failures += 1
        if changed:
            logger.debug("probe: 检测到页面变化但接口未返回，回退 DOM 扫描")
            return self._finalize_candidates(await self._scan_conversations_dom())
        logger.debug(f"probe: 强制取数未拿到接口数据（连续失败 {self._probe_failures} 次）")
        return []

    async def _probe_signature(self) -> str:
        """读取一次页面结构指纹（会话列表 + 消息区最后一条），不发网络请求。"""
        page = self._page
        if page is None:
            return ""
        try:
            data = await page.evaluate(PROBE_SCRIPT)
        except Exception as e:
            logger.debug(f"probe: 探针执行失败（{e}）")
            return ""
        if not isinstance(data, dict):
            return ""
        # 即使没有会话也返回稳定指纹（空列表），避免"账号本来就没消息"被误判为页面异常
        return fingerprint(json.dumps(data, ensure_ascii=False, sort_keys=True))

    def _should_self_heal(self, now: float) -> bool:
        """是否需要自愈刷新（定期 / 连续失败 / 页面结构缺失）。"""
        s = self.settings
        page = self._page
        if page is not None and "/chat" not in page.url and "/messages" not in page.url:
            logger.warning(f"probe: 当前页面不在私信页（{page.url}），执行自愈刷新")
            return True
        if s.reload_interval > 0 and (now - self._last_reload_ts) >= s.reload_interval:
            logger.info(f"probe: 距上次整页刷新已超过 {s.reload_interval}s，执行自愈刷新")
            return True
        if self._probe_failures >= max(1, s.reload_on_failures):
            logger.warning(f"probe: 连续 {self._probe_failures} 次取数失败，执行自愈刷新")
            return True
        if self._empty_probe_count >= 3:
            logger.warning("probe: 连续 3 次探针未读到会话列表（页面可能异常），执行自愈刷新")
            return True
        return False

    async def _self_heal_reload(self) -> None:
        """执行一次整页刷新并重置 probe 计时器。"""
        await self._refresh_for_payload()
        now = time.monotonic()
        self._last_reload_ts = now
        self._last_force_fetch_ts = now
        # 刷新后重建指纹（页面结构已重置）
        self._last_probe_sig = await self._probe_signature() or ""
        self._probe_failures = 0
        self._empty_probe_count = 0

    @property
    def effective_detect_mode(self) -> str:
        """当前生效的检测模式：控制台覆盖（state）优先，其次 .env。"""
        raw = (self.state.get_value("detect_mode") or self.settings.detect_mode or "reload").strip().lower()
        return "probe" if raw in ("probe", "b", "planb", "plan_b", "探针", "轻量") else "reload"

    def poll_interval(self) -> float:
        """当前模式建议的轮询间隔（秒）。"""
        if self.effective_detect_mode == "probe":
            return float(max(3, self.settings.probe_interval))
        return float(max(5, self.settings.poll_interval))

    def _reset_probe_state(self) -> None:
        """切换模式/重置后清空 probe 计时与计数。"""
        now = time.monotonic()
        self._last_probe_sig = ""
        self._last_force_fetch_ts = now
        self._last_reload_ts = now
        self._probe_failures = 0
        self._empty_probe_count = 0

    def _warn_captcha_throttled(self) -> None:
        """风控拦截提示（每 10 分钟最多一次）。"""
        now = time.monotonic()
        if now - getattr(self, "_last_captcha_warn", 0.0) < 600:
            return
        self._last_captcha_warn = now
        logger.warning(
            "私信页被抖音风控拦截（验证码中间页），本轮跳过。"
            "如持续出现：1) 确认登录态是否有效；2) 用有界面方式（DOUYIN_HEADLESS=false）登录一次刷新信任状态；"
            "3) 降低轮询频率（DOUYIN_POLL_INTERVAL）。"
        )

    async def _ensure_chat_page(self) -> None:
        page = self._page
        assert page is not None
        if "/chat" in page.url or "/messages" in page.url:
            return
        logger.debug(f"导航到私信页: {self.settings.chat_url}")
        try:
            # 抖音为重度 SPA：domcontentloaded 可能长时间不完（实测会超时），用 commit + 固定等待更稳
            await page.goto(self.settings.chat_url, wait_until="commit", timeout=45000)
        except Exception as e:
            logger.warning(f"打开私信页失败: {e}，回退到首页")
            try:
                await page.goto(self.settings.home_url, wait_until="commit", timeout=45000)
                await self._human_delay(3.0, 5.0)
                await page.locator("text=私信").first.click(timeout=5000)
            except Exception:
                pass
        await self._human_delay(3.5, 5.0)
        self._current_conv_text = None

    # ────────────── 会话定位 ──────────────

    async def _conversation_items(self) -> list[ElementHandle]:
        page = self._page
        assert page is not None
        items = await self._find_all(page, CONVERSATION_ITEM_SELECTORS)
        if items:
            # 过滤容器级误匹配：保留"像一条会话"的元素（含 img 或较短文本）
            result = []
            seen_xpaths = set()
            for el in items:
                try:
                    box = await el.bounding_box()
                    if box is None or box["height"] > 220:
                        continue
                    key = await el.evaluate("el => { const r = el.getBoundingClientRect(); return `${r.x},${r.y},${r.width}`; }")
                    if key in seen_xpaths:
                        continue
                    seen_xpaths.add(key)
                    result.append(el)
                except Exception:
                    continue
            if result:
                return result
        return items

    async def _match_conversation_items(self, items: list[ElementHandle]) -> list[tuple[ElementHandle, str]]:
        """挑出需要监测的会话（支持多个）：

        1) 按监测昵称列表匹配（名称包含即命中，可同时命中多个会话）；
        2) 未匹配到时取有未读角标的会话（最多 3 个）；
        3) 仍未匹配到则退回第一条会话（保持旧行为）。
        """
        accounts = self.effective_main_accounts
        matched: list[tuple[ElementHandle, str]] = []
        if accounts:
            for el in items:
                text = await self._safe_text(el)
                if any(name in text for name in accounts):
                    matched.append((el, text))
        if not matched:
            for el in items:
                if await self._has_unread(el):
                    matched.append((el, await self._safe_text(el)))
                    if len(matched) >= 3:
                        break
        if not matched and items:
            matched.append((items[0], await self._safe_text(items[0])))
        return matched

    async def _open_and_lock_conversations(self, *, precise: bool = False) -> bool:
        """逐个打开监测会话，并把它们的会话 ID 加入锁定集合。

        precise=True 用于 probe 模式：用 expect_response 精确等待消息接口（通常 0.4~1s），
        不再固定 sleep；返回本轮是否真的拿到了接口数据。
        """
        page = self._page
        assert page is not None
        items = await self._conversation_items()
        if not items:
            return False
        matched = await self._match_conversation_items(items)
        start_ts = time.monotonic()

        newly_locked: list[tuple[str, str]] = []
        for el, text in matched[:10]:
            n_before = len(self._conv_id_events)
            try:
                if precise:
                    # 仅单会话时才做"点别的会话再点回来"的兜底，避免多账号场景多余点击
                    others = [it for it in items if it is not el] if len(matched) == 1 else []
                    await self._click_conversation_precise(el, others)
                else:
                    await el.click()
                    await self._human_delay(1.5, 2.5)
            except Exception as e:
                logger.debug(f"点击会话失败（跳过该会话）: {e}")
                continue
            self._current_conv_text = text
            after = [cid for _ts, cid in self._conv_id_events[n_before:]]
            if not after:
                continue
            cid = after[-1]
            if cid not in self._target_conv_ids:
                self._target_conv_ids.add(cid)
                newly_locked.append((text.split("\n")[0][:24], cid))

        if newly_locked:
            detail = "；".join(f"{n}（ID: {c}）" for n, c in newly_locked)
            logger.info(
                f"已锁定 {len(self._target_conv_ids)} 个目标会话，仅处理这些会话内的转发: {detail}"
            )

        if not precise:
            return False

        # 接口响应体是异步读取的，给它一点时间落库
        for _ in range(20):
            if any(ts >= start_ts for ts, _ in self._msg_payloads):
                return True
            await asyncio.sleep(0.15)
        return any(ts >= start_ts for ts, _ in self._msg_payloads)

    async def _click_conversation_precise(self, el: ElementHandle, others: list[ElementHandle]) -> None:
        """点击会话并等待消息接口；若"已打开会话"不重新拉取，则先点别的会话再点回来。"""
        if await self._click_and_wait_message(el):
            return
        for other in others[:1]:
            try:
                await other.click()
                await asyncio.sleep(0.3)
            except Exception:
                continue
            if await self._click_and_wait_message(el):
                return

    async def _click_and_wait_message(self, el: ElementHandle) -> bool:
        """点击元素并等待消息接口响应（6 秒超时）。"""
        page = self._page
        if page is None:
            return False
        try:
            async with page.expect_response(
                lambda r: "imapi.douyin.com" in r.url
                and ("/message/get_by_conversation" in r.url or "/message/get_message_by_init" in r.url),
                timeout=6000,
            ):
                await el.click()
            return True
        except PlaywrightTimeoutError:
            return False
        except Exception:
            return False

    async def _has_unread(self, item: ElementHandle) -> bool:
        for sel in UNREAD_BADGE_SELECTORS:
            try:
                el = await item.query_selector(sel)
                if el is not None and await el.is_visible():
                    return True
            except Exception:
                continue
        return False

    # ────────────── 消息扫描与提取 ──────────────

    async def _scan_messages(self) -> list[IncomingCandidate]:
        page = self._page
        assert page is not None

        container = await self._find_first(page, MESSAGE_CONTAINER_SELECTORS)
        scope = container or page

        # 滚动到底部，确保加载最新消息
        if container is not None:
            try:
                await container.evaluate("el => { el.scrollTop = el.scrollHeight; }")
                await asyncio.sleep(0.5)
            except Exception:
                pass

        items = await self._find_all(scope, MESSAGE_ITEM_SELECTORS)
        if not items:
            items = await self._message_items_fallback(scope)
            if items:
                logger.debug(f"使用兜底策略获取到 {len(items)} 条消息")

        if not items:
            if self.settings.dump_html:
                await self._dump_page_html("no_message_items")
            logger.warning("未能定位消息列表（页面结构可能已变化）。可开启 FORWARDER_DUMP_HTML=true 转储页面用于排查")
            return []

        results: list[IncomingCandidate] = []
        clicks = 0
        cycle_fps: set[str] = set()
        cycle_urls: set[str] = set()

        for item in reversed(items):  # DOM 中新消息在后，倒序处理
            try:
                if self.settings.only_others and not await self._is_others(item, container):
                    continue

                html = await item.inner_html()
                text = await self._safe_text(item)
                fp = fingerprint(html[:4000] + "\n" + text[:600])
                if fp in cycle_fps or self.state.is_seen_message(fp):
                    continue
                cycle_fps.add(fp)

                links = self._extract_links(html) or self._extract_links(text)
                if links:
                    url = links[0]
                    self.state.mark_seen_message(fp)
                    vid = video_id_from_url(url)
                    if vid and self.state.is_video_processed(vid):
                        self._log_duplicate(f"url:{url}", f"作品已在库（视频 ID {vid}），重复链接不再解析")
                    elif url not in cycle_urls:
                        cycle_urls.add(url)
                        results.append(IncomingCandidate(url=url, source="text", msg_fp=fp))
                        logger.info(f"发现转发链接: {url}")
                    continue

                # 无链接但像视频卡片 → 点击提取
                if clicks < self.settings.max_clicks_per_poll and self._looks_like_card(html, text):
                    clicks += 1
                    url, completed = await self._click_extract(item)
                    if url:
                        self.state.mark_seen_message(fp)
                        vid = video_id_from_url(url)
                        if vid and self.state.is_video_processed(vid):
                            self._log_duplicate(f"url:{url}", f"作品已在库（视频 ID {vid}），重复链接不再解析")
                        elif url not in cycle_urls:
                            cycle_urls.add(url)
                            results.append(IncomingCandidate(url=url, source="card", msg_fp=fp))
                            logger.info(f"从视频卡片提取到链接: {url}")
                    elif completed:
                        # 点击流程正常，但这张卡片没有可解析的视频 —— 不再重复尝试
                        self.state.mark_seen_message(fp)
                    else:
                        logger.warning("点击视频卡片未能提取链接，下一轮将重试（可开启 HTML 转储排查）")
                    continue

                # 普通文本消息
                if not self._looks_like_card(html, text):
                    self.state.mark_seen_message(fp)
            except Exception as e:
                logger.debug(f"单条消息处理失败（忽略继续）: {e}")
                continue

        return results

    async def _message_items_fallback(self, scope) -> list[ElementHandle]:
        """兜底：以 span[data-text='true'] 为锚点，取其祖先元素作为"消息"。"""
        try:
            spans = await scope.query_selector_all("span[data-text='true']")
        except Exception:
            return []
        items: list[ElementHandle] = []
        for span in spans[-60:]:
            try:
                anc = await span.evaluate_handle("el => el.parentElement?.parentElement?.parentElement ?? el")
                items.append(anc.as_element())  # type: ignore[arg-type]
            except Exception:
                continue
        return [i for i in items if i is not None]

    async def _is_others(self, item: ElementHandle, container: ElementHandle | None) -> bool:
        """判断消息是否为"对方（大号）"发出。类名提示 → 气泡几何位置 → 默认视为对方。"""
        try:
            cls = (await item.get_attribute("class") or "").lower()
            for hint in SELF_CLASS_HINTS:
                if hint in cls:
                    return False
        except Exception:
            pass

        # 几何位置：以消息"气泡"（activeClickArea / MessageItem* 内容容器）的中心判断左右。
        # 注意：消息最外层（messageMessageBox）是全宽的"行"，其中点与列表中点重合，不能用它判断。
        probe = None
        for sel in ("[class*='activeClickArea']", "[class*='MessageItem']"):
            try:
                probe = await item.query_selector(sel)
                if probe is not None:
                    break
            except Exception:
                continue
        target = probe or item
        try:
            box = await target.bounding_box()
            cbox = await (container.bounding_box() if container is not None else None)
            if box and cbox and cbox["width"] > 0:
                left_gap = box["x"] - cbox["x"]
                right_gap = (cbox["x"] + cbox["width"]) - (box["x"] + box["width"])
                return left_gap <= right_gap
        except Exception:
            pass

        return True

    def _looks_like_card(self, html: str, text: str) -> bool:
        low = html.lower()
        return "<img" in low and len(text) < 400

    def _extract_links(self, raw: str) -> list[str]:
        if not raw:
            return []
        text = html_lib.unescape(raw)
        found: list[str] = []
        for pattern in LINK_PATTERNS:
            for m in pattern.finditer(text):
                url = m.group(0)
                # 处理 JSON 转义残留：\u0026 -> &，以及尾部反斜杠/标点
                url = re.sub(r"\\u([0-9a-fA-F]{4})", lambda x: chr(int(x.group(1), 16)), url)
                url = html_lib.unescape(url)
                url = url.rstrip("。，,;；)）]】\"'\\&")
                if url and url not in found:
                    found.append(url)
        return found

    async def _click_extract(self, item: ElementHandle) -> tuple[str | None, bool]:
        """点击消息卡片提取视频链接。

        返回 (url, completed)：
          - url 非空：成功提取；
          - completed=True 且 url 为空：点击流程已完整执行但确实没有视频链接（不必重试）；
          - (None, False)：点击/检测过程中出现异常（下一轮重试）。
        """
        page = self._page
        assert page is not None
        before_url = page.url
        popup_page = None
        clicked = False

        # 1) 新标签页打开
        try:
            async with page.context.expect_page(timeout=4000) as popup_info:
                await item.click(timeout=5000)
                clicked = True
            popup_page = await popup_info.value
        except PlaywrightTimeoutError:
            pass
        except Exception as e:
            logger.debug(f"点击消息元素异常: {e}")
            return None, False

        if not clicked:
            return None, False

        await self._human_delay(0.8, 1.6)

        if popup_page is not None:
            try:
                await popup_page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
            # 打开的视频页可能立即开始播放：先静音并暂停，再读取地址、关闭
            await self._stop_media(popup_page)
            url = popup_page.url
            try:
                await popup_page.close()
            except Exception:
                pass
            found = self._extract_links(url)
            return (found[0] if found else None), True

        # 2) 同页跳转（SPA 路由变化）
        now_url = page.url
        if now_url != before_url:
            await self._stop_media()
            found = self._extract_links(now_url)
            await self._return_to_chat()
            return (found[0] if found else None), True

        # 3) 同页弹层：扫描新出现的视频链接（弹层里的视频同样先停播）
        await self._stop_media()
        try:
            anchors = await page.query_selector_all("a[href*='/video/'], a[href*='/note/'], a[href*='v.douyin.com']")
            for a in reversed(anchors[-8:]):
                href = await a.get_attribute("href") or ""
                found = self._extract_links(href)
                if found:
                    await self._safe_escape()
                    return found[0], True
        except Exception:
            pass

        # 4) 兜底：整页 HTML 中查找（弹层可能把视频 ID 写进 DOM）
        try:
            html = await page.content()
            found = self._extract_links(html)
            if found:
                await self._safe_escape()
                return found[0], True
        except Exception:
            pass

        await self._safe_escape()
        if self.settings.dump_html:
            await self._dump_page_html("card_click_failed")
        return None, True

    async def _return_to_chat(self) -> None:
        page = self._page
        assert page is not None
        try:
            await page.go_back(timeout=8000)
            await self._human_delay(0.8, 1.5)
        except Exception:
            pass
        if "/chat" not in page.url and "/messages" not in page.url:
            try:
                await page.goto(self.settings.chat_url, wait_until="domcontentloaded")
                await self._human_delay(1.5, 2.5)
            except Exception as e:
                logger.debug(f"返回私信页失败: {e}")
        await self._stop_media()
        self._current_conv_text = None

    async def _safe_escape(self) -> None:
        page = self._page
        assert page is not None
        try:
            await page.keyboard.press("Escape")
            await self._human_delay(0.4, 0.9)
        except Exception:
            pass

    # ══════════════════════ 工具方法 ══════════════════════

    async def _human_delay(self, lo: float = 0.8, hi: float = 2.0) -> None:
        await asyncio.sleep(random.uniform(lo, hi))

    async def _safe_text(self, el: ElementHandle) -> str:
        try:
            return (await el.inner_text()).strip()
        except Exception:
            return ""

    async def _find_first(self, scope, selectors: list[str]) -> ElementHandle | None:
        for sel in selectors:
            try:
                el = await scope.query_selector(sel)
                if el is not None:
                    return el
            except Exception:
                continue
        return None

    async def _find_all(self, scope, selectors: list[str]) -> list[ElementHandle]:
        for sel in selectors:
            try:
                els = await scope.query_selector_all(sel)
                if els:
                    return els
            except Exception:
                continue
        return []

    async def _dump_page_html(self, tag: str) -> None:
        page = self._page
        if page is None:
            return
        try:
            self.settings.debug_dir.mkdir(parents=True, exist_ok=True)
            path = self.settings.debug_dir / f"{tag}_{time.strftime('%Y%m%d_%H%M%S')}.html"
            content = await page.content()
            path.write_text(content, encoding="utf-8", errors="ignore")
            logger.info(f"页面 HTML 已转储: {path}")
        except Exception as e:
            logger.debug(f"转储页面 HTML 失败: {e}")
