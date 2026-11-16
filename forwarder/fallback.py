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
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from log import logger

from .douyin_meta import deleted_reason, deleted_reason_from_text, fetch_douyin_meta
from .state import fingerprint, video_id_from_url
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
    failure_id: int | None = None
    error: str = ""
    batch: bool = False
    result: str = ""  # ok / timeout / error


class FallbackService:
    """失败链接兜底服务（仅在 FORWARDER_FALLBACK_CHAT 非空时启动）。"""

    def __init__(self, settings: Any, pusher: Any, state: Any = None, store: Any = None) -> None:
        self.settings = settings
        self.pusher = pusher
        self.state = state
        self.store = store
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
        self._replay_task: asyncio.Task | None = None
        self._replay_running = False

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
        await self._cleanup_stale_links()
        self._watch_task = asyncio.create_task(self._watch_timeouts(), name="fallback-watch")
        self._replay_task = asyncio.create_task(
            self._watch_replay_requests(), name="fallback-replay-watch"
        )

    async def stop(self) -> None:
        for task in (self._watch_task, self._replay_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._watch_task = None
        self._replay_task = None
        self.enabled = False

    async def _cleanup_stale_links(self) -> int:
        """按配置的消息 ID 清理频道里历史残留的兜底链接。

        Telegram Bot API 不允许 bot 读取频道历史（messages.GetHistory 会返回
        BOT_METHOD_INVALID），所以只能用 FORWARDER_FALLBACK_CLEANUP_IDS 指定已知 ID。
        """
        ids = [int(i) for i in (getattr(self.settings, "fallback_cleanup_ids", []) or [])]
        if not ids or self._cli is None or self._chat_id is None:
            return 0
        deleted = 0
        for message_id in ids:
            try:
                await self._cli.delete_messages(self._chat_id, message_id)
                deleted += 1
                logger.info(f"已清理历史兜底链接消息 {message_id}")
            except Exception as e:
                logger.warning(f"清理历史兜底链接失败（消息 {message_id}）: {e}")
        return deleted

    async def submit(
        self,
        url: str,
        error: str = "",
        *,
        failure_id: int | None = None,
        batch: bool = False,
        meta: dict[str, Any] | None = None,
    ) -> PendingLink | None:
        """解析最终失败后调用：发链接到兜底群，登记等待。

        作品已被抖音删除/不可见时直接跳过：换一个解析 bot 也拿不到，
        发到兜底群只会刷屏并白等超时。
        """
        if not self.enabled or self._cli is None or self._chat_id is None:
            return None
        reason = deleted_reason_from_text(error)
        if not reason:
            if meta is None:
                meta = await fetch_douyin_meta(url)
            reason = deleted_reason(meta)
        if reason:
            logger.info(f"作品已删除/不可见（{reason}），跳过兜底: {url}")
            if failure_id and self.store is not None:
                try:
                    self.store.set_failure_status(int(failure_id), "unavailable")
                except Exception as e:
                    logger.warning(f"标记作品不可用失败（忽略）: {e}")
            return None
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
            return None
        pending = PendingLink(
            url=url,
            link_message_id=int(message.id),
            failure_id=failure_id,
            error=error,
            batch=batch,
        )
        self._pending[int(message.id)] = pending
        logger.info(f"已发送兜底链接（消息 {message.id}）: {url}")
        if meta is not None:
            pending.meta = meta
        else:
            asyncio.create_task(self._prepare_meta(pending), name=f"fallback-meta-{message.id}")
        return pending

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
            target.unlink(missing_ok=True)
            return None
        if not path:
            target.unlink(missing_ok=True)
            return None
        p = Path(path)
        if not p.is_file() or p.stat().st_size <= 0:
            p.unlink(missing_ok=True)
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

    async def _delete_link(self, pending: PendingLink) -> bool:
        """从兜底群/频道删除链接消息；成功返回 True。"""
        if pending.link_deleted:
            return True
        if self._cli is None or self._chat_id is None:
            return False
        try:
            await self._cli.delete_messages(self._chat_id, pending.link_message_id)
        except Exception as e:
            logger.warning(f"删除兜底链接失败（消息 {pending.link_message_id}）: {e}")
            return False
        pending.link_deleted = True
        logger.info(f"已删除兜底链接消息 {pending.link_message_id}")
        return True

    async def _finish_failed(self, pending: PendingLink, result: str, reason: str) -> None:
        """兜底未出片（超时 / bot 明确报错）：删频道链接，私信保留链接。"""
        if pending.finished:
            return
        pending.finished = True
        pending.result = result
        pending.error = (pending.error or reason)[:500]
        await self._delete_link(pending)
        if not pending.batch:
            await self._notify_unhandled(pending, reason)

    async def _notify_unhandled(self, pending: PendingLink, reason: str) -> None:
        if self.state is None or self._cli is None:
            return
        try:
            owner = int(self.state.get_value("owner_user_id") or 0)
        except Exception:
            owner = 0
        if not owner:
            return
        state_text = "频道链接已删除" if pending.link_deleted else "频道链接删除失败，请手动删除"
        text = (
            f"⚠️ 兜底未成功（{reason}）\n"
            f"{pending.url}\n"
            f"{state_text}；链接仅保留在本私信。"
        )
        try:
            await self._cli.send_message(owner, text)
        except Exception as e:
            logger.debug(f"兜底失败私信通知失败: {e}")

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
        if pending is None or pending.finished:
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

        # 兜底 bot 可能直接回一条文字（如“解析失败/不支持”）。这类消息不是媒体，
        # 不必再等满 180 秒，立即结束并走“删除频道链接 + 私信保留”的收尾。
        text = str(getattr(message, "text", "") or "").strip()
        if text:
            lower = text.lower()
            keywords = ("失败", "错误", "无法", "不支持", "取消", "失效", "删除", "error", "fail", "invalid")
            if any(k in lower for k in keywords):
                logger.warning(f"兜底 bot 返回失败文本（消息 {message.id}）: {text[:200]}")
                await self._finish_failed(pending, "error", text)
            return

        temp_dir = self.settings.data_dir / "fallback_tmp" / str(pending.link_message_id)
        downloaded = await self._download_media(message, temp_dir)
        if downloaded is None:
            try:
                temp_dir.rmdir()
            except OSError:
                pass
            return
        media_path, is_video = downloaded
        logger.info(
            f"兜底消息 {message.id} 匹配链接 {pending.link_message_id}"
            f"（回复 {getattr(message, 'reply_to_message_id', None) or '-'}，"
            f"video={is_video}，同频道={self._same_target}）"
        )
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
        pending.result = "ok"
        pending.finished = True
        if pending.failure_id and self.store is not None:
            try:
                self.store.set_failure_status(int(pending.failure_id), "resolved")
            except Exception as e:
                logger.debug(f"更新兜底成功记录失败: {e}")
        if self.settings.fallback_delete_link and not pending.link_deleted:
            if await self._delete_link(pending):
                logger.info(f"兜底成功，已删除链接消息 {pending.link_message_id}")

    async def _send_owner_chunks(self, lines: list[str], header: str = "") -> None:
        """把长清单拆成多条私信发给 owner（保留原始链接，便于人工处理）。"""
        if self.state is None or self._cli is None:
            return
        try:
            owner = int(self.state.get_value("owner_user_id") or 0)
        except Exception:
            owner = 0
        if not owner:
            logger.warning("未绑定 owner，兜底重跑清单无法私信保留")
            return
        from pyrogram.types import LinkPreviewOptions

        chunks: list[str] = []
        current: list[str] = []
        size = 0
        for line in ([header] if header else []) + lines:
            if current and size + len(line) + 1 > 3500:
                chunks.append("\n".join(current))
                current, size = [], 0
            current.append(line)
            size += len(line) + 1
        if current:
            chunks.append("\n".join(current))
        for chunk in chunks:
            try:
                await self._cli.send_message(
                    owner,
                    chunk,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            except Exception as e:
                logger.warning(f"发送兜底重跑私信失败: {e}")

    async def replay_failures(self, records: list[dict[str, Any]], *, label: str = "历史失败兜底重跑") -> dict[str, Any]:
        """按顺序把历史失败链接交给兜底 bot；频道链接成功/超时都删除，链接私信保留。"""
        if self._replay_running:
            return {"ok": 0, "timeout": 0, "error": 0, "skipped": 0, "total": 0, "message": "已有重跑任务在运行"}
        if not self.enabled:
            return {"ok": 0, "timeout": 0, "error": 0, "skipped": 0, "total": 0, "message": "兜底服务未启用"}
        self._replay_running = True
        result: dict[str, Any] = {
            "ok": 0,
            "timeout": 0,
            "error": 0,
            "skipped": 0,
            "archived": 0,
            "unavailable": 0,
            "total": 0,
            "failed": [],
        }
        try:
            items: list[dict[str, Any]] = []
            seen: set[str] = set()
            for raw in records:
                rec = dict(raw)
                url = str(rec.get("url") or "").strip()
                if not url.startswith("http"):
                    continue
                key = video_id_from_url(url) or url
                if key in seen:
                    continue
                seen.add(key)
                fp = str(rec.get("fp") or "") or fingerprint(url)
                already = False
                if self.state is not None:
                    try:
                        already = bool(self.state.is_processed(fp)) or bool(
                            video_id_from_url(url) and self.state.is_video_processed(video_id_from_url(url))
                        )
                    except Exception:
                        already = False
                if already:
                    result["archived"] += 1
                    result["skipped"] += 1
                    if self.store is not None and rec.get("id"):
                        try:
                            self.store.set_failure_status(int(rec["id"]), "resolved")
                        except Exception:
                            pass
                    continue
                # 发链接前先确认作品还能看；已被抖音删除/仅自己可见的作品，
                # 兜底 bot 同样拿不到，直接跳过并标记，避免反复重跑。
                meta = await fetch_douyin_meta(url)
                reason = deleted_reason(meta)
                if reason:
                    result["unavailable"] += 1
                    result["skipped"] += 1
                    result["unavailable_links"] = result.get("unavailable_links", []) + [url]
                    if self.store is not None and rec.get("id"):
                        try:
                            self.store.set_failure_status(int(rec["id"]), "unavailable")
                        except Exception:
                            pass
                    logger.info(f"兜底重跑跳过已删除/不可见作品（{reason}）: {url}")
                    continue
                rec["_meta"] = meta
                items.append(rec)
            result["total"] = len(items)
            if not items:
                await self._send_owner_chunks(
                    [],
                    f"📋 {label}：没有需要重跑的链接"
                    f"（已归档 {result['archived']} 条，作品已删除/不可见 {result['unavailable']} 条）。",
                )
                return result

            manifest = [f"#{r.get('id') or '-'} {r.get('url')}" for r in items]
            await self._send_owner_chunks(
                manifest,
                f"📋 {label}：共 {len(items)} 条"
                + (f"（已过滤已归档 {result['archived']} 条、已删除/不可见 {result['unavailable']} 条）" if result["skipped"] else "")
                + "\n频道里的链接无论成功还是超时都会删除；原始链接仅保留在本私信。",
            )
            timeout = max(30, int(self.settings.fallback_timeout))
            for index, rec in enumerate(items, 1):
                url = str(rec.get("url") or "")
                fid = int(rec["id"]) if rec.get("id") else None
                pending = await self.submit(
                    url,
                    str(rec.get("error") or "")[:200],
                    failure_id=fid,
                    batch=True,
                    meta=rec.get("_meta"),
                )
                if pending is None:
                    result["error"] += 1
                    result["failed"].append(url)
                    continue
                deadline = time.monotonic() + timeout + 60
                while not pending.finished and time.monotonic() < deadline:
                    await asyncio.sleep(2)
                if not pending.finished:
                    await self._finish_failed(pending, "timeout", f"{timeout}s 超时")
                if pending.result == "ok":
                    result["ok"] += 1
                    logger.success(f"兜底重跑 {index}/{len(items)} 成功: {url}")
                elif pending.result == "timeout":
                    result["timeout"] += 1
                    result["failed"].append(url)
                    logger.warning(f"兜底重跑 {index}/{len(items)} 超时: {url}")
                else:
                    result["error"] += 1
                    result["failed"].append(url)
                    logger.warning(f"兜底重跑 {index}/{len(items)} 未成功: {url}")
                # 留出时间让兜底 bot 把同一作品的后续媒体/相册发完，避免串到下一条。
                await asyncio.sleep(8)
            report = [
                f"📦 {label}完成",
                f"成功 {result['ok']} / 超时 {result['timeout']} / 未成功 {result['error']}"
                f" / 已删除或不可见 {result['unavailable']} / 已归档跳过 {result['archived']}",
            ]
            if result["failed"]:
                report.append("未成功的链接（频道已删除，仅私信保留）：")
                report.extend(result["failed"])
            await self._send_owner_chunks([], "\n".join(report))
            return result
        finally:
            self._replay_running = False

    async def _watch_replay_requests(self) -> None:
        """监听 forwarder_data/fallback_replay.json，支持免重启触发历史失败重跑。

        文件格式：
          {"mode": "all"}                  # 所有 gave_up
          {"mode": "all", "limit": 10}
          {"ids": [51, 50]}                # 指定失败编号
          {"urls": ["https://..."]}        # 指定链接
        """
        path = Path(self.settings.data_dir) / "fallback_replay.json"
        while True:
            await asyncio.sleep(10)
            if not path.exists():
                continue
            if self._replay_running:
                logger.warning("兜底重跑任务运行中，暂不处理新的 fallback_replay.json")
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"fallback_replay.json 解析失败（忽略）: {e}")
                try:
                    path.replace(path.with_name(f"fallback_replay.bad.{int(time.time())}.json"))
                except OSError:
                    pass
                continue
            try:
                path.unlink()
            except OSError:
                pass
            records: list[dict[str, Any]] = []
            if isinstance(data, dict) and data.get("mode") == "all":
                if self.store is None:
                    logger.warning("兜底重跑无法读取失败库（store 未注入）")
                    continue
                limit = int(data.get("limit") or 500)
                records = self.store.list_failures("gave_up", limit)
            elif isinstance(data, dict) and isinstance(data.get("ids"), list):
                for fid in data["ids"]:
                    try:
                        rec = self.store.get_failure(int(fid)) if self.store is not None else None
                    except Exception:
                        rec = None
                    if rec:
                        records.append(rec)
            elif isinstance(data, dict) and isinstance(data.get("urls"), list):
                records = [{"url": str(u)} for u in data["urls"] if str(u).startswith("http")]
            if not records:
                logger.warning("fallback_replay.json 没有可执行内容")
                continue
            label = str((data or {}).get("label") or "历史失败兜底重跑") if isinstance(data, dict) else "历史失败兜底重跑"
            try:
                result = await self.replay_failures(records, label=label)
                try:
                    (Path(self.settings.data_dir) / "fallback_replay_report.json").write_text(
                        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                except OSError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"兜底重跑任务异常: {e}")

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
                timeout = max(30, int(self.settings.fallback_timeout))
                if age <= timeout:
                    continue
                if not pending.finished:
                    logger.warning(f"兜底超时（{age:.0f}s），删除频道链接: {pending.url}")
                    await self._finish_failed(pending, "timeout", f"{timeout}s 超时")
                if age > max(60, timeout) + 600:
                    self._pending.pop(link_id, None)
