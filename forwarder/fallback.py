# -*- coding: utf-8 -*-
"""失败链接兜底：把解析失败的链接发到兜底群/频道，等待兜底 bot 出片。

两种部署形态：
  1. 兜底 bot 与成品频道是同一个频道（FORWARDER_FALLBACK_CHAT=@成品频道）
     → 兜底 bot 的频道帖已经出现在成品频道；本模块不再复制视频，
       只在它下面补 Live Photo 评论（避免重复发媒体）。
  2. 兜底 bot 在独立群/频道（FORWARDER_FALLBACK_CHAT=-100...）
     → 本模块把兜底 bot 的视频复制到成品频道，再在复制出的帖子下补 Live Photo 评论。

关键限制：Telegram Bot API 不允许 bot 读取任意历史消息，也不保证 bot 能收到
其他 bot 的群消息；因此本模块只在“能收到更新”的前提下工作。若日志里一直看不到
兜底 bot 的消息，需要改用用户账号监听或公开频道预览轮询。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from log import logger

from .douyin_meta import fetch_douyin_meta
from .video_live import convert_video_to_live

logger = logger.bind(name="Forwarder/Fallback")


@dataclass
class PendingLink:
    url: str
    link_message_id: int
    created_at: float = field(default_factory=time.time)
    meta: dict[str, Any] | None = None
    handled: int = 0
    link_deleted: bool = False
    finished: bool = False


class FallbackService:
    """失败链接兜底服务（仅在 FORWARDER_FALLBACK_CHAT 非空时启动）。"""

    def __init__(self, settings: Any, pusher: Any, state: Any = None) -> None:
        self.settings = settings
        self.pusher = pusher
        self.state = state
        self.enabled = False
        self._cli: Any = None
        self._chat_id: int | None = None
        self._is_channel = False
        self._same_target = False
        self._bot_id: int | None = None
        self._bot_username = ""
        self._self_id: int | None = None
        self._pending: dict[int, PendingLink] = {}
        self._seen: set[tuple[int, int]] = set()
        self._watch_task: asyncio.Task | None = None

    async def start(self) -> None:
        if not self.settings.fallback_enabled:
            return
        if not getattr(self.pusher, "enabled", False):
            logger.warning("兜底服务未启动：未配置成品频道（TG_TARGET_CHAT）")
            return
        if not self.pusher.client_ready or self.pusher.client is None:
            logger.warning("兜底服务未启动：Telegram Client 不可用")
            return
        self._cli = self.pusher.client
        try:
            chat = await self._cli.get_chat(self.settings.fallback_chat.strip())
            self._chat_id = int(chat.id)
            self._is_channel = "channel" in str(getattr(chat, "type", "")).lower()
            self._same_target = bool(
                self.pusher._channel_id and self._chat_id == int(self.pusher._channel_id)
            )
            me = await self._cli.get_me()
            self._self_id = int(getattr(me, "id", 0) or 0)
            logger.info(
                f"兜底服务就绪：chat={self._chat_id}（{'频道' if self._is_channel else '群组'}，"
                f"{'同成品频道' if self._same_target else '独立兜底群'}）"
            )
        except Exception as e:
            logger.error(f"兜底服务初始化失败（{self.settings.fallback_chat}）: {e}")
            return

        ref = (self.settings.fallback_bot or "").strip()
        if ref:
            try:
                user = await self._cli.get_users(ref)
                self._bot_id = int(getattr(user, "id", 0) or 0) or None
                self._bot_username = str(getattr(user, "username", "") or "")
                logger.info(
                    f"兜底 bot: {self._bot_username or ref}"
                    + (f"（id {self._bot_id}）" if self._bot_id else "")
                )
            except Exception as e:
                logger.warning(f"无法解析兜底 bot {ref}（将按消息来源宽松匹配）: {e}")

        from pyrogram import filters

        async def _on_fallback_message(_, message: Any) -> None:
            try:
                await self._handle_message(message)
            except Exception as e:
                logger.warning(f"处理兜底消息异常（忽略）: {e}")

        # kurigram 2.2.26 没有 on_channel_post；频道帖也通过 on_message 分发。
        self._cli.on_message(filters.chat(self._chat_id))(_on_fallback_message)

        self.enabled = True
        self._watch_task = asyncio.create_task(self._watch_timeouts(), name="fallback-watch")

    async def stop(self) -> None:
        if self._watch_task is not None:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
            self._watch_task = None
        self.enabled = False

    async def submit(self, url: str, error: str = "") -> None:
        """解析最终失败后调用：发链接到兜底群，登记等待。"""
        if not self.enabled or self._cli is None or self._chat_id is None:
            return
        text = f"{self.settings.fallback_marker or '#fallback'}\n{url}"
        if error:
            text += f"\n{error[:200]}"
        try:
            from pyrogram.types import LinkPreviewOptions

            message = await self._cli.send_message(
                self._chat_id,
                text,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except Exception as e:
            logger.warning(f"发送兜底链接失败: {e} | {url}")
            return
        pending = PendingLink(url=url, link_message_id=int(message.id))
        self._pending[int(message.id)] = pending
        logger.info(f"已发送兜底链接（消息 {message.id}）: {url}")
        asyncio.create_task(self._prepare_meta(pending), name=f"fallback-meta-{message.id}")

    async def _prepare_meta(self, pending: PendingLink) -> None:
        pending.meta = await fetch_douyin_meta(pending.url)

    def _is_fallback_sender(self, message: Any) -> bool:
        if getattr(message, "outgoing", False):
            return False
        user = getattr(message, "from_user", None)
        uid = int(getattr(user, "id", 0) or 0) if user is not None else 0
        username = str(getattr(user, "username", "") or "")
        if self._self_id and uid == self._self_id:
            return False
        if self._bot_id and uid == self._bot_id:
            return True
        if self._bot_username and username.lower() == self._bot_username.lower():
            return True
        # 频道里的 bot 发帖可能没有 from_user；只接受“非本 bot”的频道媒体消息
        if self._is_channel and uid == 0:
            return True
        return False

    def _find_pending(self, message: Any) -> PendingLink | None:
        reply_id = int(getattr(message, "reply_to_message_id", 0) or 0)
        if reply_id and reply_id in self._pending:
            return self._pending[reply_id]
        candidates = [p for p in self._pending.values() if not p.finished]
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.created_at)

    async def _download_media(self, message: Any, dest: Path) -> tuple[Path, bool] | None:
        """下载消息里的视频/图片；返回 (路径, 是否视频)。"""
        video = getattr(message, "video", None)
        animation = getattr(message, "animation", None)
        document = getattr(message, "document", None)
        photo = getattr(message, "photo", None)
        media = video or animation or document
        is_video = bool(video or animation)
        if media is None and photo is None:
            return None
        if document is not None and not is_video:
            mime = str(getattr(document, "mime_type", "") or "").lower()
            if mime.startswith("video/"):
                is_video = True
        dest.mkdir(parents=True, exist_ok=True)
        suffix = ".mp4" if is_video else ".jpg"
        target = dest / f"{int(getattr(message, 'id', 0) or 0)}{suffix}"
        try:
            path = await message.download(file_name=str(target))
        except Exception as e:
            logger.warning(f"下载兜底媒体失败: {e}")
            return None
        if not path:
            return None
        p = Path(path)
        if not p.is_file() or p.stat().st_size <= 0:
            return None
        return p, is_video

    async def _send_live_comment(self, photo: Path, video: Path, channel_message_id: int) -> bool:
        group_id = self.pusher.discussion_group_id
        if not group_id:
            logger.warning("成品频道没有关联讨论组，无法补 Live Photo 评论")
            return False
        root_id = await self.pusher._wait_discussion_root([int(channel_message_id)])
        if root_id is None:
            logger.warning(f"未拿到频道帖 {channel_message_id} 的评论根，跳过 Live Photo")
            return False
        await self.pusher._send_live(
            photo,
            video,
            "",
            chat_id=group_id,
            reply_to_message_id=root_id,
        )
        return True

    async def _handle_message(self, message: Any) -> None:
        key = (int(getattr(getattr(message, "chat", None), "id", 0) or 0), int(getattr(message, "id", 0) or 0))
        if key in self._seen:
            return
        self._seen.add(key)
        if len(self._seen) > 2000:
            self._seen = set(list(self._seen)[-1000:])
        if not self._is_fallback_sender(message):
            return
        pending = self._find_pending(message)
        if pending is None:
            return
        # 链接消息本身不处理
        if int(getattr(message, "id", 0) or 0) == pending.link_message_id:
            return
        msg_date = getattr(message, "date", None)
        try:
            if msg_date is not None and msg_date.timestamp() < pending.created_at - 5:
                return
        except (TypeError, ValueError, AttributeError):
            pass

        temp_dir = self.settings.data_dir / "fallback_tmp" / str(pending.link_message_id)
        downloaded = await self._download_media(message, temp_dir)
        if downloaded is None:
            return
        media_path, is_video = downloaded
        handled = False
        try:
            meta = pending.meta
            if meta is None:
                meta = await fetch_douyin_meta(pending.url)
                pending.meta = meta
            media_kind = str((meta or {}).get("media", {}).get("kind") or "")

            if is_video and media_kind == "live" and self.settings.video_as_live:
                pair = await asyncio.to_thread(
                    convert_video_to_live,
                    media_path,
                    temp_dir,
                    max_seconds=self.settings.video_as_live_max_seconds,
                    max_mb=self.settings.video_as_live_max_mb,
                )
                if pair is not None:
                    if self._same_target:
                        # 兜底 bot 已在成品频道发视频：只补评论区，不重复发媒体
                        handled = await self._send_live_comment(
                            pair.photo, pair.video, int(message.id)
                        )
                    else:
                        # 独立兜底群：把视频复制到成品频道，再在复制帖下补 Live Photo 评论
                        copied = await message.copy(self.pusher._target)
                        channel_id = int(getattr(copied, "id", 0) or 0)
                        if channel_id:
                            handled = await self._send_live_comment(pair.photo, pair.video, channel_id)
                        if not handled:
                            # 评论区不可用：退化为直接把 Live Photo 发到成品频道
                            await self.pusher._send_live(pair.photo, pair.video, "")
                            handled = True
                    if handled:
                        logger.success(f"兜底视频已转 Live Photo: {pending.url}")
            if not handled and not self._same_target:
                # 非实况或转换失败：把兜底 bot 的视频/图片复制到成品频道
                await message.copy(self.pusher._target)
                handled = True
                logger.success(f"兜底媒体已复制到成品频道: {pending.url}")
            elif not handled and self._same_target:
                # 同频道且不是实况：兜底 bot 已经发过，无需重复
                handled = True
        finally:
            # 转换出的 jpg/mp4 与下载原文件都只做临时用途，处理完即删
            try:
                for p in temp_dir.glob("*"):
                    p.unlink(missing_ok=True)
                temp_dir.rmdir()
            except OSError:
                pass

        if not handled:
            logger.warning(f"兜底消息未能处理（media_kind={media_kind}）: {pending.url}")
            return

        pending.handled += 1
        if self.settings.fallback_delete_link and not pending.link_deleted:
            try:
                await self._cli.delete_messages(self._chat_id, pending.link_message_id)
                pending.link_deleted = True
                logger.info(f"兜底成功，已删除链接消息 {pending.link_message_id}")
            except Exception as e:
                logger.warning(f"删除兜底链接失败（保留）: {e}")

    async def _watch_timeouts(self) -> None:
        while True:
            await asyncio.sleep(10)
            now = time.time()
            for link_id, pending in list(self._pending.items()):
                age = now - pending.created_at
                if pending.handled:
                    if age > 600:
                        self._pending.pop(link_id, None)
                    continue
                if age <= max(30, int(self.settings.fallback_timeout)):
                    continue
                if not pending.finished:
                    pending.finished = True
                    logger.warning(f"兜底超时（{age:.0f}s），保留链接: {pending.url}")
                    await self._notify_timeout(pending)
                if age > max(60, int(self.settings.fallback_timeout)) + 600:
                    self._pending.pop(link_id, None)

    async def _notify_timeout(self, pending: PendingLink) -> None:
        if self.state is None or self._cli is None:
            return
        try:
            owner = int(self.state.get_value("owner_user_id") or 0)
        except Exception:
            owner = 0
        if not owner:
            return
        try:
            await self._cli.send_message(
                owner,
                f"⚠️ 兜底超时（{self.settings.fallback_timeout}s）\n"
                f"{pending.url}\n链接消息 {pending.link_message_id} 已保留",
            )
        except Exception as e:
            logger.debug(f"兜底超时通知失败: {e}")
