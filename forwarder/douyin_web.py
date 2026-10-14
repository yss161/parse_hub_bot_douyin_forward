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
    无头模式下给出"先在桌面机登录、再同步 browser_profile 目录到服务器"的指引。
    实测（2026-10 于国内家庭宽带）：
      · 有界面首次打开 douyin.com → 正常展示「抖音精选」页并自动弹出登录二维码 ✅
      · 无头模式首次访问 → 易触发滑块验证码（风控），需先用有界面完成一次登录
"""

from __future__ import annotations

import asyncio
import html as html_lib
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

from .config import ForwarderSettings
from .state import StateStore, fingerprint

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

# 登录态 cookie 名称
SESSION_COOKIE_NAMES = {"sessionid", "sid_guard", "sid_tt", "sid_ucp_virtual"}


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

    # ══════════════════════ 生命周期 ══════════════════════

    async def start(self) -> None:
        """启动浏览器并确保小号已登录。"""
        await self._ensure_browser()
        await self.ensure_login()

    async def close(self) -> None:
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
        logger.info(f"浏览器已启动（profile: {self.settings.browser_profile_dir}, headless={self.settings.headless}）")

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
        - 无头模式：无法手动操作，给出「先本机登录、再同步浏览器 profile 到服务器」的指引并报错退出。
        """
        page = self._page
        assert page is not None
        shot = self.settings.data_dir / "login_captcha.png"
        try:
            await page.screenshot(path=str(shot))
        except Exception:
            pass

        if self.settings.headless:
            raise RuntimeError(
                "抖音返回了「验证码中间页」（滑块验证），无头模式无法手动完成。\n"
                "可选方案：\n"
                "  1) 在【有桌面的机器】上执行:  DOUYIN_HEADLESS=false python run.py login\n"
                "     扫码登录成功后，把 forwarder_data/browser_profile/ 整个目录复制到服务器（覆盖同名目录）；\n"
                "  2) Linux 服务器安装 xvfb（apt-get install -y xvfb），用 xvfb + VNC 打开桌面窗口手动过验证；\n"
                "  3) 间隔一段时间再试（风控可能临时触发）。\n"
                f"本次页面截图: {shot}"
            )

        logger.warning(
            f"检测到抖音「验证码中间页」（滑块验证），请在浏览器窗口中【手动完成验证】；"
            f"页面截图: {shot}（{self.settings.qr_timeout}s 内未通过将超时）"
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
        """
        page = self._page
        assert page is not None
        candidates: list[ElementHandle] = []

        try:
            for img in await page.query_selector_all("img"):
                try:
                    src = (await img.get_attribute("src")) or ""
                    if not src.startswith("data:image"):
                        continue
                    box = await img.bounding_box()
                    if box and 120 <= box["width"] <= 340 and abs(box["width"] - box["height"]) <= 20:
                        candidates.append(img)
                except Exception:
                    continue
        except Exception:
            pass

        try:
            for cv in await page.query_selector_all("canvas"):
                try:
                    box = await cv.bounding_box()
                    if box and 120 <= box["width"] <= 340 and abs(box["width"] - box["height"]) <= 20:
                        candidates.append(cv)
                except Exception:
                    continue
        except Exception:
            pass

        for el in candidates:
            try:
                await el.screenshot(path=str(path))
                return True
            except Exception:
                continue
        return False

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
        """执行「远程协助」指令文件（forwarder_data/agent_cmd.txt）。

        用于人工介入未知页面（如「身份验证」）：把要做的操作写入该文件（UTF-8，每行一条），
        本方法在登录等待期间每 3 秒检查一次，执行后自动删除文件。
        支持指令：
            click_text 接收短信验证码     # 按文本点击元素
            fill 123456                   # 向页面第一个可见输入框输入内容
            type 123456                   # 向当前焦点输入
            press Enter
            click 720 459                 # 按视口坐标点击（与 login_page_state.png 像素一致）
            drag 300 400 800 400          # 按住拖动（滑块等）
            wait 2                        # 等待秒数
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
                op, _, arg = line.partition(" ")
                op, arg = op.strip().lower(), arg.strip()
                if op == "click_text":
                    await page.locator(f"text={arg}").first.click(timeout=5000)
                elif op == "fill":
                    # 优先匹配验证码/密码输入框，避免误填到背景页面的搜索框等
                    target = None
                    for sel in ("input[placeholder*='验证码']", "input[type='password']", "input[placeholder*='密码']"):
                        for el in await page.query_selector_all(sel):
                            try:
                                if await el.is_visible():
                                    target = el
                                    break
                            except Exception:
                                continue
                        if target is not None:
                            break
                    if target is None:
                        # 兜底：取 DOM 中最后一个可见输入框（弹窗一般后置在页面末尾）
                        for el in reversed(await page.query_selector_all("input")):
                            try:
                                if await el.is_visible():
                                    target = el
                                    break
                            except Exception:
                                continue
                    if target is None:
                        raise RuntimeError("页面上没有可见的输入框")
                    await target.click()
                    await page.keyboard.type(arg, delay=60)
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
                else:
                    raise RuntimeError(f"未知指令: {op}")
                executed = True
                logger.info(f"远程协助指令已执行: {line}")
            except Exception as e:
                logger.warning(f"远程协助指令执行失败: {line} -> {e}")
        return executed

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
            + f"\n  二维码截图: {qr_path}"
            + "\n  （在服务器上可用 scp / 面板文件管理下载后扫码；"
            + "\n    二维码约 1 分钟过期、截图每 25 秒自动刷新——【每次扫之前请重新下载最新图】，重复扫旧图会因过期而无效；"
            + f"\n    {self.settings.qr_timeout}s 内未扫码将超时）"
            + "\n" + "=" * 64
        )

        # 等待扫码（期间定期重新截取二维码，应对二维码过期刷新）
        deadline = time.monotonic() + self.settings.qr_timeout
        last_refresh = time.monotonic()
        idv_warned = False
        while time.monotonic() < deadline:
            if await self._is_logged_in():
                await self._save_storage_state()
                return

            # 远程协助：执行 agent_cmd.txt 中的指令（人工介入未知页面用，执行后自动删除）
            if await self._try_agent_cmd():
                await self._snapshot_login_page()

            # 抖音风控的「身份验证」页面（异地登录常见）：提示等待人工介入，不作为验证码拦截处理
            if await self._has_identity_verification():
                if not idv_warned:
                    idv_warned = True
                    logger.warning(
                        "检测到「身份验证」页面（抖音风控要求二次验证，异地登录常见）。\n"
                        f"  查看当前页面截图: {self.settings.data_dir / 'login_page_state.png'}\n"
                        f"  远程协助: 在 {self.settings.data_dir / 'agent_cmd.txt'} 写入指令（UTF-8，每行一条，执行后自动删除）\n"
                        "  指令: click_text 文本 / fill 内容 / type 内容 / press Enter / click x y / drag x1 y1 x2 y2 / wait 秒"
                    )
            # 其他安全验证（滑块等），无头环境无法完成——留存截图并明确报错
            elif await self._is_captcha_present():
                await self._snapshot_login_page()
                raise RuntimeError(
                    "扫码后在浏览器端出现了安全验证页面（滑块等），当前无头环境无法自动完成。\n"
                    f"页面截图已保存: {self.settings.data_dir / 'login_page_state.png'}（可下载查看）\n"
                    "建议: 1) 给服务器加装 VNC（x11vnc）后按部署文档第 4 节方式③用有界面方式登录；"
                    "2) 或更换服务器网络环境后重试（抖音风控与访问 IP 强相关）。"
                )

            if time.monotonic() - last_refresh > 25:
                last_refresh = time.monotonic()
                got_qr = await self._capture_qr(qr_path)
                await self._snapshot_login_page()
                logger.info(f"等待扫码中（页面: {page.url}，二维码刷新: {'成功' if got_qr else '失败'}）")
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
        """执行一轮轮询，返回新发现的候选链接（已做消息级去重）。"""
        await self._ensure_browser()
        assert self._page is not None and self._context is not None

        # 登录态失效 → 重新走扫码流程
        if not await self._is_logged_in():
            logger.warning("登录态失效，重新扫码登录")
            await self.ensure_login()

        # 风控拦截检测（节流提示，避免刷屏）
        if await self._is_captcha_present():
            self._warn_captcha_throttled()
            return []

        await self._ensure_chat_page()

        if not await self._open_main_conversation():
            logger.warning(
                "未找到私信会话。请检查：1) 大号是否已给小号发过消息；"
                "2) DOUYIN_MAIN_ACCOUNT 是否与大号昵称匹配；"
                "3) 必要时设置 FORWARDER_DUMP_HTML=true 转储页面排查。"
            )
            return []

        candidates = await self._scan_messages()

        # 首次运行：默认仅记录当前消息水位（避免把服务启动前的旧转发全部解析）；
        # 设置 DOUYIN_PROCESS_HISTORY=true 可改为处理历史消息。
        if not self.state.get_flag("history_initialized"):
            self.state.set_flag("history_initialized", True)
            self.state.save()
            if not self.settings.process_history:
                if candidates:
                    logger.info(f"首次运行：已跳过 {len(candidates)} 条历史转发（只处理今后新转发的视频；如需处理历史可设 DOUYIN_PROCESS_HISTORY=true 后重置 state.json）")
                else:
                    logger.info("首次运行：已记录当前消息水位，只处理今后新转发的视频")
                return []
            logger.info("首次运行：DOUYIN_PROCESS_HISTORY=true，历史消息将一并处理")

        self.state.save()
        return candidates

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

    async def _open_main_conversation(self) -> bool:
        page = self._page
        assert page is not None
        items = await self._conversation_items()
        if not items:
            return False

        target: ElementHandle | None = None
        target_text = ""

        # 1) 按大号昵称匹配
        if self.settings.main_account:
            for el in items:
                text = await self._safe_text(el)
                if self.settings.main_account in text:
                    target, target_text = el, text
                    break

        # 2) 有未读角标的会话
        if target is None:
            for el in items:
                if await self._has_unread(el):
                    target, target_text = el, await self._safe_text(el)
                    break

        # 3) 第一条会话
        if target is None:
            target = items[0]
            target_text = await self._safe_text(target)

        # 已打开则跳过
        if self._current_conv_text and target_text and self._current_conv_text == target_text:
            return True

        try:
            await target.click()
        except Exception as e:
            logger.warning(f"点击会话失败: {e}")
            return False
        await self._human_delay(1.2, 2.5)
        self._current_conv_text = target_text
        logger.debug(f"已打开会话: {target_text[:40]!r}")
        return True

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
                    results.append(IncomingCandidate(url=url, source="text", msg_fp=fp))
                    logger.info(f"发现转发链接: {url}")
                    continue

                # 无链接但像视频卡片 → 点击提取
                if clicks < self.settings.max_clicks_per_poll and self._looks_like_card(html, text):
                    clicks += 1
                    url, completed = await self._click_extract(item)
                    if url:
                        self.state.mark_seen_message(fp)
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
        """判断消息是否为"对方（大号）"发出。类名提示 → 几何位置 → 默认视为对方。"""
        try:
            cls = (await item.get_attribute("class") or "").lower()
            for hint in SELF_CLASS_HINTS:
                if hint in cls:
                    return False
        except Exception:
            pass

        # 几何位置：自己发的消息在右半边
        try:
            box = await item.bounding_box()
            cbox = await (container.bounding_box() if container is not None else None)
            if box and cbox and cbox["width"] > 0:
                return (box["x"] + box["width"] / 2) < (cbox["x"] + cbox["width"] / 2)
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
                url = m.group(0).rstrip("。，,;；)）]】\"'")
                if url not in found:
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
            found = self._extract_links(now_url)
            await self._return_to_chat()
            return (found[0] if found else None), True

        # 3) 同页弹层：扫描新出现的视频链接
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
