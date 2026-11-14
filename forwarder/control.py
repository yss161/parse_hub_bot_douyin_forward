# -*- coding: utf-8 -*-
"""Owner 控制台：绑定、命令、失败通知、摘要、服务器信息。

设计要点：
  - 与 TgPusher 共用同一个 Pyrogram Client（同一 BOT_TOKEN 不能开两个 session 抢更新）；
  - 只有 owner 能执行命令；绑定码一次性、限时、限速、常量时间比较；
  - /parse 与抖音轮询共用主循环的解析队列，避免并发下载抢资源；
  - 所有 Telegram 导入都放在函数内，未安装 Telegram 依赖时仍可做本地静态测试。
"""

from __future__ import annotations

import asyncio
import hmac
import re
import time
import urllib.request
from datetime import date, datetime
from typing import Any, Callable

from log import logger

from .config import ForwarderSettings
from .failures import category_label, classify_failure, short_error, status_label
from .hashing import short_hash
from .jobs import ParseJob
from . import sysinfo

logger = logger.bind(name="Forwarder/Control")

DOUYIN_URL_RE = re.compile(
    r"https?://[^\s<>'\"]*(?:douyin\.com|iesdouyin\.com)[^\s<>'\"]*", re.I
)

_CMD_ALIASES = {
    "状态": "status",
    "服务器": "server",
    "系统": "server",
    "统计": "stats",
    "最近": "last",
    "失败": "failures",
    "重试": "retry",
    "搜索": "search",
    "暂停": "pause",
    "继续": "resume",
    "恢复": "resume",
    "解析": "parse",
    "帮助": "help",
    "解绑": "unbind",
    "通知": "notify",
}

_PRIVATE_COMMANDS = (
    ("start", "绑定 owner / 显示帮助"),
    ("help", "查看命令说明"),
    ("status", "服务器与转发状态"),
    ("stats", "最近 N 天统计"),
    ("last", "最近归档与失败"),
    ("failures", "失败收件箱"),
    ("retry", "重试失败作品"),
    ("search", "搜索本地归档"),
    ("parse", "解析抖音链接"),
    ("pause", "暂停抖音轮询"),
    ("resume", "恢复抖音轮询"),
    ("notify", "失败通知开关/测试"),
    ("unbind", "解除 owner 绑定"),
)

_GROUP_COMMANDS = (
    ("parse", "解析抖音链接"),
)

_HELP = """🤖 抖音转发控制台
仅 owner 可用；绑定码一次性。

【开始】
/start <绑定码> — 绑定 owner
/help — 显示本菜单

【查看】
/status — 服务器、CPU/内存/磁盘、运行状态
/stats [天数] — 最近 1-30 天统计，默认 1 天
/last — 最近 5 条归档与失败
/failures — 失败收件箱（编号 / 分类 / 链接）
/search <关键词> — 搜索本地归档

【处理】
/parse <链接> — 解析抖音链接，也可直接发链接
/retry <编号> — 重试失败作品
/notify on|off|test — 失败通知开关 / 测试
/pause — 暂停抖音轮询
/resume — 恢复轮询
/unbind — 解除 owner 绑定

【说明】
• 所有命令仅 owner 可用；其他人会提示先绑定。
• 失败卡片按钮：🔁 重试 / 🗑 忽略 / ℹ️ 详情。
• 群组仅在关联讨论组内、由 owner 发 /parse 或链接时生效。
• 中文命令同样可用：/状态 /统计 /最近 /失败 /解析 /重试 /搜索 /暂停 /继续 /通知 /帮助 /解绑
"""


class ControlPlane:
    def __init__(
        self,
        settings: ForwarderSettings,
        state: Any,
        store: Any,
        pusher: Any,
        runtime: Any,
        receiver: Any = None,
    ) -> None:
        self.settings = settings
        self.state = state
        self.store = store
        self.pusher = pusher
        self.runtime = runtime
        self.receiver = receiver
        self._client: Any = None
        self._menu_task: Any = None
        self._job_sink: Callable[[ParseJob], None] | None = None
        self._wake: Callable[[], None] | None = None
        self._bind_started = time.monotonic()
        self._bind_attempts: dict[int, list[float]] = {}
        self._last_alert_at = 0.0
        self._last_low_disk_alert = 0.0
        # 绑定码错误日志脱敏：只记录用户，不记录输入内容。

    # ────────────────────────── 生命周期 / 队列 ──────────────────────────

    @property
    def client(self) -> Any:
        return self._client or getattr(self.pusher, "client", None)

    def set_job_sink(self, sink: Callable[[ParseJob], None], wake: Callable[[], None] | None = None) -> None:
        self._job_sink = sink
        self._wake = wake

    @property
    def owner_user_id(self) -> int:
        try:
            return int(self.state.get_value("owner_user_id") or 0)
        except ValueError:
            return 0

    @property
    def owner_bound(self) -> bool:
        return self.owner_user_id > 0

    @property
    def notify_enabled(self) -> bool:
        return self.state.get_value("failure_notify_enabled", "1") != "0"

    @property
    def bind_code_fingerprint(self) -> str:
        code = (self.settings.bot_bind_code or "").strip()
        return short_hash(code) if code else ""

    @property
    def bind_code_used(self) -> bool:
        """当前 .env 绑定码是否已经被消费过（换新码后自动恢复可用）。"""
        fp = self.bind_code_fingerprint
        if not fp:
            return False
        try:
            return str(self.state.get_value("bind_code_used") or "") == fp
        except Exception:
            return False

    @property
    def bind_code_available(self) -> bool:
        return bool(self.bind_code_fingerprint) and not self.bind_code_used

    @property
    def console_enabled(self) -> bool:
        return bool(
            self.settings.bot_console
            and (self.bind_code_available or self.owner_bound)
        )

    def register(self, client: Any) -> None:
        """在已启动的 Pyrogram Client 上注册私聊/群组/按钮处理器。"""
        from pyrogram import filters

        self._client = client

        @client.on_message(filters.private & filters.text)
        async def _private(_, message: Any) -> None:  # noqa: ANN001
            await self.handle_private(message)

        group_id = int(getattr(self.pusher, "discussion_group_id", 0) or 0)
        if group_id and self.settings.parse_in_group:

            @client.on_message(filters.chat(group_id) & filters.text)
            async def _group(_, message: Any) -> None:  # noqa: ANN001
                await self.handle_group(message)

        @client.on_callback_query()
        async def _callback(_, query: Any) -> None:  # noqa: ANN001
            await self.handle_callback(query)

        try:
            self._menu_task = asyncio.create_task(self.setup_command_menu(client))
        except RuntimeError as e:
            logger.warning(f"设置 Bot 命令菜单失败（没有运行中的事件循环）: {e}")

        logger.info("Bot 控制台已注册（私聊命令、按钮、群组 /parse）")

    async def setup_command_menu(self, client: Any) -> None:
        """设置 Telegram 的 / 命令菜单（私聊全量，讨论组仅 /parse）。"""
        try:
            from pyrogram import types
        except Exception as e:
            logger.warning(f"设置 Bot 命令菜单跳过（Pyrogram 不可用）: {e}")
            return

        private = [types.BotCommand(cmd, desc) for cmd, desc in _PRIVATE_COMMANDS]
        try:
            await client.set_bot_commands(
                private,
                scope=types.BotCommandScopeAllPrivateChats(),
            )
            logger.info(f"Bot 命令菜单已更新（私聊 {len(private)} 条）")
        except Exception as e:
            logger.warning(f"设置私聊命令菜单失败: {e}")

        group_id = int(getattr(self.pusher, "discussion_group_id", 0) or 0)
        if not group_id:
            return
        group = [types.BotCommand(cmd, desc) for cmd, desc in _GROUP_COMMANDS]
        try:
            await client.set_bot_commands(
                group,
                scope=types.BotCommandScopeChat(chat_id=group_id),
            )
            logger.info(f"Bot 命令菜单已更新（讨论组 {len(group)} 条）")
        except Exception as e:
            logger.warning(f"设置讨论组命令菜单失败: {e}")

    # ────────────────────────── 绑定 ──────────────────────────

    def bind_owner(self, user_id: int, username: str, code: str) -> tuple[bool, str]:
        if self.owner_bound:
            return False, "已绑定 owner，不能重复绑定；如需更换请先 /unbind。"
        expected = (self.settings.bot_bind_code or "").strip()
        if not expected:
            return False, "服务端未配置 FORWARDER_BOT_BIND_CODE，无法绑定。"
        if self.bind_code_used:
            return False, "绑定码已被使用；如需重新绑定，请更换 FORWARDER_BOT_BIND_CODE 并重启服务。"
        ttl = max(0, int(self.settings.bind_code_ttl or 0))
        if ttl and time.monotonic() - self._bind_started > ttl:
            return False, "绑定码已过期，请重启服务生成新的有效期。"

        now = time.monotonic()
        attempts = [t for t in self._bind_attempts.get(user_id, []) if now - t < 600]
        if len(attempts) >= 5:
            return False, "尝试过于频繁，请 10 分钟后再试。"
        attempts.append(now)
        self._bind_attempts[user_id] = attempts

        if not hmac.compare_digest(str(code or "").strip(), expected):
            logger.warning(f"owner 绑定失败（用户 {user_id}，绑定码不匹配）")
            return False, "绑定码错误。"

        self.state.set_value("owner_user_id", str(int(user_id)))
        self.state.set_value("owner_username", username or "")
        self.state.set_value("notify_chat_id", str(int(user_id)))
        self.state.set_value("failure_notify_enabled", "1")
        self.state.set_value("bound_at", str(int(time.time())))
        self.state.set_value("bind_code_used", self.bind_code_fingerprint)
        self.state.save()
        logger.success(f"owner 绑定成功: {username or user_id}（{user_id}）")
        return True, (
            "✅ 绑定成功。\n"
            f"owner: {username or user_id}\n"
            "之后解析失败的链接会私聊发给你，可用 /status 查看状态、/help 查看命令。"
        )

    def unbind(self) -> None:
        for key in (
            "owner_user_id",
            "owner_username",
            "notify_chat_id",
            "failure_notify_enabled",
            "bound_at",
        ):
            self.state.clear_value(key)
        self.state.save()

    # ────────────────────────── 消息入口 ──────────────────────────

    @staticmethod
    def _split_command(text: str) -> tuple[str, str]:
        parts = (text or "").strip().split(maxsplit=1)
        if not parts:
            return "", ""
        cmd = parts[0].lstrip("/").split("@", 1)[0].lower()
        return _CMD_ALIASES.get(cmd, cmd), (parts[1].strip() if len(parts) > 1 else "")

    @staticmethod
    def _sender_id(message: Any) -> int:
        return int(getattr(getattr(message, "from_user", None), "id", 0) or 0)

    @staticmethod
    def _sender_name(message: Any) -> str:
        user = getattr(message, "from_user", None)
        return str(getattr(user, "username", None) or getattr(user, "first_name", None) or "")

    def _is_owner_message(self, message: Any) -> bool:
        return self.owner_bound and self._sender_id(message) == self.owner_user_id

    async def handle_private(self, message: Any) -> None:
        if message is None or not getattr(message, "text", None):
            return
        text = str(message.text).strip()
        cmd, arg = self._split_command(text)

        if cmd in ("start", "auth"):
            if arg:
                ok, reply = self.bind_owner(self._sender_id(message), self._sender_name(message), arg)
                await self._reply(message, reply)
                return
            if self.owner_bound and self._sender_id(message) == self.owner_user_id:
                await self._reply(message, _HELP)
            elif self.owner_bound:
                await self._reply(message, "该 bot 已绑定其他 owner。")
            else:
                await self._reply(message, "请发送：/start <绑定码>")
            return

        if not self._is_owner_message(message):
            if DOUYIN_URL_RE.search(text):
                await self._reply(message, "请先发送 /start <绑定码> 绑定 owner，再提交链接。")
            elif cmd in ("help", "status", "stats", "parse", "retry", "failures"):
                await self._reply(message, "无权限：请先绑定 owner（/start <绑定码>）。")
            return

        await self._dispatch(message, cmd, arg, text)

    async def handle_group(self, message: Any) -> None:
        if message is None or not getattr(message, "text", None):
            return
        # 频道自动转发到讨论组的帖子没有 from_user，忽略
        if getattr(message, "forward_origin", None) or getattr(message, "forward_from_chat", None):
            return
        if not self._is_owner_message(message):
            return
        text = str(message.text).strip()
        cmd, arg = self._split_command(text)
        if cmd == "parse":
            await self._dispatch(message, cmd, arg, text)
            return
        if DOUYIN_URL_RE.search(text):
            await self._submit_from_message(message, text, source="telegram-group")

    async def _dispatch(self, message: Any, cmd: str, arg: str, text: str) -> None:
        try:
            if cmd in ("start", "help", "menu"):
                await self._reply(message, _HELP)
            elif cmd in ("status", "server", "sys", "sysinfo"):
                await self._reply(message, self.status_text())
            elif cmd == "stats":
                days = 1
                if arg.isdigit():
                    days = max(1, min(30, int(arg)))
                await self._reply(message, self.stats_text(days))
            elif cmd == "last":
                await self._reply(message, self.last_text())
            elif cmd == "failures":
                await self._reply(message, self.failures_text())
            elif cmd == "retry":
                if not arg.isdigit():
                    await self._reply(message, "用法：/retry <失败编号>（编号见 /failures）")
                else:
                    ok, reply = self.retry_failure(int(arg))
                    await self._reply(message, reply)
            elif cmd == "search":
                await self._reply(message, self.search_text(arg))
            elif cmd == "pause":
                self.state.set_flag("paused", True)
                self.state.save()
                await self._reply(message, "⏸ 已暂停抖音轮询。已在队列中的任务仍会继续；/resume 恢复。")
            elif cmd == "resume":
                self.state.set_flag("paused", False)
                self.state.save()
                if self._wake:
                    self._wake()
                await self._reply(message, "▶️ 已恢复抖音轮询。")
            elif cmd == "parse":
                url = self._extract_url(arg or text)
                if not url and getattr(message, "reply_to_message", None):
                    url = self._extract_url(getattr(message.reply_to_message, "text", "") or "")
                if not url:
                    await self._reply(message, "用法：/parse <抖音链接>（也可直接发链接）")
                else:
                    await self._submit_from_message(message, url, source="telegram")
            elif cmd == "notify":
                await self._handle_notify(message, arg)
            elif cmd == "unbind":
                self.unbind()
                await self._reply(
                    message,
                    "已解除 owner 绑定。当前绑定码已被使用；如需重新绑定，"
                    "请更换 .env 中的 FORWARDER_BOT_BIND_CODE 并重启服务。",
                )
            else:
                if DOUYIN_URL_RE.search(text):
                    await self._submit_from_message(message, text, source="telegram")
                else:
                    await self._reply(message, _HELP)
        except Exception as e:
            logger.warning(f"控制台命令执行失败（{cmd}）: {e}")
            await self._reply(message, f"命令执行失败：{short_error(str(e), 200)}")

    async def _handle_notify(self, message: Any, arg: str) -> None:
        value = (arg or "").strip().lower()
        if value in ("on", "1", "true", "开", "开启"):
            self.state.set_value("failure_notify_enabled", "1")
            self.state.save()
            await self._reply(message, "失败通知已开启。")
        elif value in ("off", "0", "false", "关", "关闭"):
            self.state.set_value("failure_notify_enabled", "0")
            self.state.save()
            await self._reply(message, "失败通知已关闭（失败仍会进入 /failures）。")
        elif value in ("test", "测试"):
            await self.send_owner("🔔 测试通知：bot 可以主动私聊你。")
            await self._reply(message, "已发送测试消息。")
        else:
            await self._reply(message, "用法：/notify on|off|test")

    # ────────────────────────── 提交解析 ──────────────────────────

    @staticmethod
    def _extract_url(text: str) -> str:
        m = DOUYIN_URL_RE.search(text or "")
        if not m:
            return ""
        return m.group(0).rstrip("。，,.;；)）]】")

    async def _submit_from_message(self, message: Any, text: str, *, source: str) -> None:
        url = self._extract_url(text)
        if not url:
            await self._reply(message, "没有识别到抖音链接。")
            return
        chat_id = int(getattr(getattr(message, "chat", None), "id", 0) or 0) or None
        ok = self.submit_url(
            url,
            source=source,
            reply_chat_id=chat_id,
            reply_to_message_id=int(getattr(message, "id", 0) or 0) or None,
            requested_by=self._sender_id(message),
        )
        await self._reply(
            message,
            "📥 已加入解析队列，完成后会在频道推送（如有配置），并在这里回报结果。"
            if ok
            else "队列不可用，请稍后重试。",
        )

    def submit_url(
        self,
        url: str,
        *,
        source: str = "telegram",
        reply_chat_id: int | None = None,
        reply_to_message_id: int | None = None,
        requested_by: int | None = None,
        failure_id: int | None = None,
        retry: bool = False,
    ) -> bool:
        if not self._job_sink:
            return False
        job = ParseJob(
            url=url,
            source=source,
            reply_chat_id=reply_chat_id,
            reply_to_message_id=reply_to_message_id,
            requested_by=requested_by,
            failure_id=failure_id,
            retry=retry,
        )
        accepted = self._job_sink(job)
        if accepted is False:
            return False
        if self._wake:
            self._wake()
        return True

    def retry_failure(self, failure_id: int) -> tuple[bool, str]:
        rec = self.store.get_failure(failure_id)
        if not rec:
            return False, f"没有找到失败编号 #{failure_id}。"
        url = str(rec.get("url") or "").strip()
        if not url:
            return False, f"失败 #{failure_id} 没有记录链接，无法重试。"
        fp = str(rec.get("fp") or "")
        if fp:
            self.state.clear_gave_up(fp)
            self.state.clear_failures(fp)
            self.state.unmark_settled(fp)
            self.state.save()
        self.store.set_failure_status(failure_id, "retrying")
        ok = self.submit_url(
            url,
            source="retry",
            failure_id=failure_id,
            retry=True,
            requested_by=self.owner_user_id or None,
        )
        if not ok:
            return False, "解析队列不可用，稍后重试。"
        return True, f"🔁 已把失败 #{failure_id} 重新加入解析队列。"

    # ────────────────────────── Telegram 发送 ──────────────────────────

    async def _reply(self, message: Any, text: str, reply_markup: Any = None) -> None:
        try:
            from pyrogram.types import LinkPreviewOptions

            await message.reply_text(
                text,
                reply_markup=reply_markup,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except Exception:
            try:
                await message.reply_text(text, reply_markup=reply_markup)
            except Exception as e:
                logger.debug(f"回复消息失败: {e}")

    async def send_to(
        self,
        chat_id: int | None,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        reply_markup: Any = None,
    ) -> Any:
        if not chat_id or not self.client:
            return None
        try:
            from pyrogram.types import LinkPreviewOptions

            return await self.client.send_message(
                chat_id,
                text[:4000],
                reply_to_message_id=reply_to_message_id,
                reply_markup=reply_markup,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except TypeError:
            try:
                return await self.client.send_message(
                    chat_id,
                    text[:4000],
                    reply_to_message_id=reply_to_message_id,
                    reply_markup=reply_markup,
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.warning(f"发送消息到 {chat_id} 失败（兼容回退）: {e}")
                return None
        except Exception as e:
            logger.warning(f"发送消息到 {chat_id} 失败: {e}")
            return None

    async def send_owner(self, text: str, reply_markup: Any = None) -> Any:
        if not self.owner_bound:
            return None
        return await self.send_to(self.owner_user_id, text, reply_markup=reply_markup)

    async def notify_failure(self, record: dict[str, Any]) -> None:
        if not self.owner_bound or not self.settings.failure_notify or not self.notify_enabled:
            return
        fid = int(record.get("id") or 0)
        kind = classify_failure(str(record.get("error") or ""))
        text = (
            f"❌ 解析失败 #{fid}\n"
            f"分类：{kind.label}\n"
            f"链接：{record.get('url') or '-'}\n"
            f"错误：{short_error(str(record.get('error') or ''), 300)}\n"
            f"尝试：{record.get('attempts') or 1} 次\n"
            f"提示：{kind.hint}"
        )
        markup = None
        if fid:
            from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("🔁 重试", callback_data=f"fr:retry:{fid}"),
                        InlineKeyboardButton("🗑 忽略", callback_data=f"fr:ignore:{fid}"),
                        InlineKeyboardButton("ℹ️ 详情", callback_data=f"fr:detail:{fid}"),
                    ]
                ]
            )
        message = await self.send_owner(text, reply_markup=markup)
        if message is not None and fid:
            self.store.mark_notified(fid, int(getattr(message, "id", 0) or 0))

    async def handle_callback(self, query: Any) -> None:
        try:
            data = str(getattr(query, "data", "") or "")
            user_id = int(getattr(getattr(query, "from_user", None), "id", 0) or 0)
            if not self.owner_bound or user_id != self.owner_user_id:
                await query.answer("无权限", show_alert=True)
                return
            parts = data.split(":")
            if len(parts) != 3 or parts[0] != "fr" or not parts[2].isdigit():
                await query.answer()
                return
            action, fid = parts[1], int(parts[2])
            if action == "retry":
                ok, reply = self.retry_failure(fid)
                await query.answer("已加入重试" if ok else reply, show_alert=not ok)
                if ok:
                    await self._clear_callback_markup(query)
            elif action == "ignore":
                self.store.set_failure_status(fid, "ignored")
                await query.answer("已忽略")
                await self._clear_callback_markup(query)
            elif action == "detail":
                rec = self.store.get_failure(fid) or {}
                await query.answer(short_error(str(rec.get("error") or "无详情"), 900), show_alert=True)
            else:
                await query.answer()
        except Exception as e:
            logger.warning(f"处理按钮回调失败: {e}")
            try:
                await query.answer("操作失败，请重试", show_alert=True)
            except Exception:
                pass

    @staticmethod
    async def _clear_callback_markup(query: Any) -> None:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

    # ────────────────────────── 文本报告 ──────────────────────────

    def status_text(self) -> str:
        info = sysinfo.collect_system_info(
            self.settings, self.runtime, self.receiver, self.store, self.state
        )
        load = info.get("load")
        load_s = f"{load[0]:.2f}/{load[1]:.2f}/{load[2]:.2f}" if load else "n/a"
        mem = info.get("memory") or {}
        disk = info.get("disk") or {}
        rt = self.runtime
        queue = int(getattr(rt, "queue_size", 0) or 0)
        mode = info.get("mode", "?")
        paused = bool(self.state.get_flag("paused"))
        target = getattr(self.pusher, "target_name", "") or self.settings.tg_target or "-"
        lines = [
            "🖥 服务器",
            f"主机：{info.get('hostname')} / {info.get('platform')} / Python {info.get('python')}",
            f"运行：{sysinfo.format_duration(info.get('uptime'))}；服务：{info.get('service')}",
            f"CPU：{info.get('cpu_count')} 核，负载 {load_s}，采样 {info.get('cpu_percent', 0):.0f}%",
            f"内存：{sysinfo.format_bytes_gb(mem.get('used_mb'), 'MiB')} / "
            f"{sysinfo.format_bytes_gb(mem.get('total_mb'), 'MiB')}，可用 "
            f"{sysinfo.format_bytes_gb(mem.get('available_mb'), 'MiB')}",
            f"磁盘：{disk.get('used_gb', 0):.1f} / {disk.get('total_gb', 0):.1f} GiB"
            f"（{disk.get('percent', 0):.0f}%），可用 {disk.get('free_gb', 0):.1f} GiB",
            f"进程：Python {info.get('process_rss_mb') or 0:.0f} MB / Chromium "
            f"{info.get('chrome_rss_mb') or 0:.0f} MB",
            "",
            "📡 转发",
            f"检测：{mode}（{'接口探针' if mode == 'probe' else '整页刷新' if mode == 'reload' else '未知'}）；"
            f"{'⏸ 已暂停' if paused else '▶️ 运行中'}；队列 {queue} 个待解析",
            f"轮询：上次检查在 {sysinfo.format_age(rt.last_poll_at)}（候选 "
            f"{rt.last_poll_candidates} 个，连续空 {rt.consecutive_empty} 轮）",
            f"最近发现候选：{sysinfo.format_age(rt.last_candidate_at)}；"
            f"最近解析成功：{sysinfo.format_age(rt.last_success_at)}",
            f"最近失败：{sysinfo.format_age(rt.last_failure_at)}；"
            f"最近推送：{sysinfo.format_age(rt.last_push_at)}",
            f"TG 推送：{'已启用' if getattr(self.pusher, 'enabled', False) else '未启用'} → {target}",
            f"Owner：{self.state.get_value('owner_username') or self.owner_user_id or '未绑定'}；"
            f"失败通知：{'开' if self.notify_enabled else '关'}",
            f"失败收件箱：{self.store.open_failure_count()} 条未结；"
            f"今日归档：{self.store.archive_count()}；浏览器重启：{rt.browser_restarts}",
        ]
        if rt.last_error:
            lines.append(f"最近错误：{short_error(rt.last_error, 200)}")
        return "\n".join(lines)

    def stats_text(self, days: int = 1) -> str:
        stats = self.store.stats_range(days)
        title = "今日" if days == 1 else f"最近 {days} 天"
        lines = [
            f"📊 {title}统计（{date.today().isoformat()}）",
            f"解析成功：{stats.get('success', 0)}；重复跳过：{stats.get('duplicate', 0)}",
            f"失败：{stats.get('failure', 0)}；重试：{stats.get('retry', 0)}；"
            f"磁盘跳过：{stats.get('low_disk', 0)}",
            f"推送成功：{stats.get('push_ok', 0)}；推送失败：{stats.get('push_fail', 0)}",
            f"今日归档：{self.store.archive_count()}；未结失败：{self.store.open_failure_count()}",
        ]
        if days > 1:
            lines.append(f"区间归档：{self.store.archive_count_range(days)}")
        return "\n".join(lines)

    def last_text(self) -> str:
        lines = ["🕘 最近归档"]
        rows = self.store.recent_archive(5)
        if not rows:
            lines.append("（暂无）")
        for r in rows:
            title = (r.get("title") or "(无标题)")[:60]
            link = r.get("tg_link") or ""
            lines.append(f"#{r['id']} {title}" + (f"\n   {link}" if link else ""))
        lines.append("")
        lines.append("❌ 最近失败")
        fails = self.store.list_failures(None, 5)
        if not fails:
            lines.append("（暂无）")
        for r in fails:
            lines.append(
                f"#{r['id']} [{category_label(r.get('category'))}] "
                f"{status_label(r.get('status'))} {r.get('url')}"
            )
        return "\n".join(lines)

    def failures_text(self) -> str:
        rows = self.store.list_failures("gave_up", 10)
        if not rows:
            rows = self.store.list_failures("open", 10)
        if not rows:
            return "✅ 当前没有待处理失败。"
        lines = ["❌ 失败收件箱（最近 10 条）"]
        for r in rows:
            lines.append(
                f"#{r['id']} [{category_label(r.get('category'))}] "
                f"{status_label(r.get('status'))}\n"
                f"   原因：{short_error(str(r.get('error') or ''), 100)}\n"
                f"   链接：{r.get('url')}"
            )
        lines.append("\n用 /retry <编号> 重试。")
        return "\n".join(lines)

    def search_text(self, query: str) -> str:
        q = (query or "").strip()
        if not q:
            return "用法：/search <关键词>（标题/作者/作品ID/链接）"
        rows = self.store.search_archive(q, 10)
        if not rows:
            return f"没有找到包含「{q}」的归档。"
        lines = [f"🔎 搜索「{q}」：{len(rows)} 条"]
        for r in rows:
            title = (r.get("title") or "(无标题)")[:70]
            lines.append(
                f"#{r['id']} {title}\n"
                f"   {r.get('author') or '-'} / {r.get('video_id') or '-'}"
                + (f"\n   {r.get('tg_link')}" if r.get("tg_link") else "")
            )
        return "\n".join(lines)

    # ────────────────────────── 主动提醒 ──────────────────────────

    async def alert_low_disk(self, free: float | None) -> None:
        now = time.monotonic()
        if now - self._last_low_disk_alert < 3600:
            return
        self._last_low_disk_alert = now
        text = (
            f"💾 磁盘空间不足：输出目录剩余 {free or 0:.1f} GB，"
            f"低于阈值 {self.settings.min_free_gb:g} GB。\n"
            "已暂停新的解析；请清理 output/ 或扩容后自动恢复。"
        )
        logger.warning(text.replace("\n", " "))
        await self.send_owner(text)

    async def maybe_alert_silent(self) -> None:
        if self.settings.silent_alert_minutes <= 0 or not self.owner_bound:
            return
        now = time.monotonic()
        if now - self._last_alert_at < 21600:
            return
        last = self.runtime.last_candidate_at or self.runtime.started_at
        if time.time() - last < self.settings.silent_alert_minutes * 60:
            return
        self._last_alert_at = now
        await self.send_owner(
            f"⏰ 已 {self.settings.silent_alert_minutes} 分钟没有发现新的转发消息。\n"
            "如果确实没有转发，可忽略；如果预期有消息，请检查小号登录/私信页是否被风控。"
        )

    async def maybe_send_summary(self) -> None:
        if not self.settings.summary_enabled or not self.owner_bound:
            return
        now = datetime.now()
        if now.hour < max(0, min(23, int(self.settings.summary_hour))):
            return
        today = now.date().isoformat()
        if self.state.get_value("last_summary_day") != today:
            await self.send_owner("🗓 每日摘要\n" + self.stats_text(1) + "\n\n" + self.status_text())
            self.state.set_value("last_summary_day", today)
            self.state.save()
        if now.weekday() == 0 and self.state.get_value("last_weekly_summary") != today:
            await self.send_owner("🗓 每周摘要\n" + self.stats_text(7))
            self.state.set_value("last_weekly_summary", today)
            self.state.save()

    async def heartbeat(self) -> None:
        url = (self.settings.healthcheck_url or "").strip()
        if not url:
            return
        method = (self.settings.healthcheck_method or "GET").upper()
        try:
            req = urllib.request.Request(url, method=method)
            await asyncio.to_thread(lambda: urllib.request.urlopen(req, timeout=10).read())
            self.runtime.heartbeats += 1
            self.runtime.last_heartbeat_at = time.time()
        except Exception as e:
            logger.warning(f"心跳上报失败: {e}")
