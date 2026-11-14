# -*- coding: utf-8 -*-
"""Telegram 推送：把解析结果发送到指定的群组 / 频道。

设计说明：
  - **文案与媒体构建完全复用原项目实现**（plugins.helpers.build_caption、
    plugins.parse.sender.build_input_media、services.media.resolve_media_info），
    输出形态与原项目一致：视频带封面流式播放、图文走相册、GIF 走动画，
    上传失败自动降级为文档；
  - 发送使用与原项目相同的库（pyrogram / kurigram）直连 MTProto，按 chat_id
    显式发送——不依赖"收到消息再回复"，因此可以发往任意已授权的群组/频道；
  - 凭据复用原项目的环境变量：API_ID / API_HASH / BOT_TOKEN（写在 .env），
    可选 BOT_PROXY（国内服务器访问 Telegram 时使用）。

配置（.env）：
    TG_PUSH=true
    TG_TARGET_CHAT=@your_channel     # 或 -100 开头的数字ID，或 t.me 链接
"""

from __future__ import annotations

import asyncio
from itertools import batched
from pathlib import Path
from typing import Any

from log import logger

from .config import ForwarderSettings, normalize_live_photo_mode
from .fast_aes import install_fast_aes
from .live_photo import send_live_photo

logger = logger.bind(name="Forwarder/TgPush")

# Telegram 媒体 caption 上限 1024 字符，留出余量
_CAPTION_MAX = 1000


def _clip(text: str) -> str:
    return text if len(text) <= _CAPTION_MAX else text[: _CAPTION_MAX - 1] + "…"


class TgPusher:
    """把解析结果推送到 Telegram 群组/频道（发送逻辑由本模块实现，构建逻辑复用原项目）。"""

    def __init__(
        self, settings: ForwarderSettings, state: Any = None, session_name: str = "forwarder_tg"
    ) -> None:
        self.settings = settings
        self.state = state
        self._session_name = session_name or "forwarder_tg"
        self.enabled = False
        self._cli: Any = None
        self._target: Any = None
        self._target_name = ""
        self._crypto_backend = "unknown"
        self._channel_id: int | None = None
        self._discussion_group_id: int | None = None
        # 频道帖 ID -> 讨论组里的自动转发消息 ID（即评论根消息）
        self._forward_cache: dict[int, int] = {}
        # 正在等待某几个频道帖的自动转发；同一时间只允许一个评论任务
        self._forward_waiter: tuple[set[int], asyncio.Future] | None = None
        self._discussion_lock = asyncio.Lock()

    @property
    def effective_live_photo_mode(self) -> str:
        """当前生效的实况图模式：控制台覆盖（state）优先，其次 .env。"""
        raw = ""
        if self.state is not None:
            try:
                raw = self.state.get_value("live_photo_mode")
            except Exception:
                raw = ""
        return normalize_live_photo_mode(raw or self.settings.live_photo_mode)

    # ────────────────────────── 生命周期 ──────────────────────────

    async def start(self) -> None:
        if not self.settings.tg_push:
            return
        if not self.settings.tg_target:
            logger.error("已启用 TG 推送（TG_PUSH=true）但未配置 TG_TARGET_CHAT，推送功能不可用")
            return

        import os

        api_id = (os.environ.get("API_ID") or "").strip()
        api_hash = (os.environ.get("API_HASH") or "").strip()
        bot_token = (os.environ.get("BOT_TOKEN") or "").strip()
        if not api_id or not api_hash or not bot_token or bot_token == "0:0" or api_hash == "forwarder":
            logger.error(
                "已启用 TG 推送，但 Telegram 凭据未配置完整。请在 .env 中填写 API_ID / API_HASH / BOT_TOKEN：\n"
                "  API_ID、API_HASH -> https://my.telegram.org 创建应用获得\n"
                "  BOT_TOKEN        -> Telegram 中向 @BotFather 创建机器人获得"
            )
            return

        import re

        try:
            from core import bs
            from plugins.helpers import parse_channel_ref
            from pyrogram import Client
        except Exception as e:
            logger.error(f"TG 推送依赖导入失败: {e}")
            return

        # 大文件上传优化：Pyrogram 默认用纯 Python AES 加密每个 512KB 分片，
        # 49MB 视频可能耗时数十分钟；启动时切到 cryptography/pycryptodome 的 C 后端。
        self._crypto_backend = install_fast_aes()
        if self._crypto_backend == "pyaes":
            logger.warning(
                "Telegram 加密未启用 C 加速（无 tgcrypto / cryptography / pycryptodome），"
                "大文件上传会非常慢；建议 pip install cryptography"
            )
        else:
            logger.info(f"Telegram 加密加速: {self._crypto_backend}")

        try:
            raw = self.settings.tg_target.strip()
            # 宽容处理: t.me/用户名/123 这类带消息 ID 的链接 -> 规范为 @用户名
            m = re.match(r"^(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{4,31})(?:/\d+)?/?$", raw)
            if m:
                raw = "@" + m.group(1)
            target = parse_channel_ref(raw)
        except ValueError as e:
            logger.error(f"TG_TARGET_CHAT 配置无效: {e}")
            return

        cli = Client(
            name=self._session_name,
            api_id=api_id,
            api_hash=api_hash,
            bot_token=bot_token,
            workdir=str(bs.sessions_path),
            # 评论模式需要监听讨论组的自动转发，才能把频道帖映射到评论根消息；
            # 解析、发送本身仍是显式 chat_id，不依赖更新。
            no_updates=False,
            proxy=bs.bot_proxy or None,
        )
        try:
            await cli.start()
            chat = await cli.get_chat(target)
            self._target_name = getattr(chat, "title", None) or str(target)
            self._channel_id = int(chat.id)
            await self._setup_discussion_routing(cli, target)
        except Exception as e:
            logger.error(
                f"TG 推送初始化失败: {e}\n"
                "请检查: 1) API_ID / API_HASH / BOT_TOKEN 是否正确；"
                "2) 机器人是否已加入目标群组（频道需要设为管理员并有发帖权限）；"
                "3) 服务器能否访问 Telegram（国内服务器需配置 BOT_PROXY）"
            )
            try:
                await cli.stop()
            except Exception:
                pass
            return

        self._cli = cli
        self._target = target
        self.enabled = True
        logger.success(f"TG 推送已就绪 ✅ 目标: {self._target_name}")

    async def _setup_discussion_routing(self, cli: Any, target: Any) -> None:
        """发现频道关联的讨论组，并监听频道帖的自动转发。

        Bot API 不允许 bot 调用 messages.GetDiscussionMessage / GetHistory，
        但频道发帖后 Telegram 会把帖子自动转发到关联讨论组；该转发消息的 id
        就是评论区根消息。监听这条更新即可完成映射。
        """
        from pyrogram import filters, raw as tg_raw

        try:
            peer = await cli.resolve_peer(target)
            full = await cli.invoke(tg_raw.functions.channels.GetFullChannel(channel=peer))
            linked = int(getattr(full.full_chat, "linked_chat_id", 0) or 0)
        except Exception as e:
            logger.warning(f"读取频道关联讨论组失败，评论模式不可用: {e}")
            return

        if not linked:
            logger.info("目标频道未开启评论区（没有关联讨论组），comment 模式将回退为频道内发送")
            return

        group_id = linked if linked < 0 else -1_000_000_000_000 - linked
        self._discussion_group_id = group_id

        @cli.on_message(filters.chat(group_id))
        async def _capture_forward(_, message: Any) -> None:
            self._capture_discussion_forward(message)

        logger.info(f"评论模式已就绪：讨论组 {group_id}（监听频道帖自动转发）")

    def _capture_discussion_forward(self, message: Any) -> None:
        """记录频道帖 -> 讨论组根消息的映射，并唤醒等待者。"""
        origin = getattr(message, "forward_origin", None)
        channel_id = int(getattr(getattr(origin, "chat", None), "id", 0) or 0)
        post_id = int(getattr(origin, "message_id", 0) or 0)
        if not channel_id or not post_id:
            # 兼容旧版 pyrogram 的 forward_from_* 字段
            channel_id = int(getattr(getattr(message, "forward_from_chat", None), "id", 0) or 0)
            post_id = int(getattr(message, "forward_from_message_id", 0) or 0)
        if not channel_id or not post_id:
            return
        if self._channel_id and channel_id != self._channel_id:
            return

        self._forward_cache[post_id] = int(message.id)
        if len(self._forward_cache) > 300:
            for old in list(self._forward_cache)[:100]:
                self._forward_cache.pop(old, None)

        waiter = self._forward_waiter
        if waiter is not None:
            wanted, future = waiter
            if post_id in wanted and not future.done():
                future.set_result(int(message.id))

    async def _wait_discussion_root(self, post_ids: list[int], timeout: float = 10.0) -> int | None:
        """等待频道帖在讨论组产生自动转发，返回评论根消息 id。"""
        wanted = {int(x) for x in post_ids if x}
        if not wanted or not self._discussion_group_id:
            return None

        future = asyncio.get_running_loop().create_future()
        self._forward_waiter = (wanted, future)
        try:
            for post_id in wanted:
                if post_id in self._forward_cache:
                    return self._forward_cache[post_id]
            return int(await asyncio.wait_for(future, timeout=timeout))
        except asyncio.TimeoutError:
            logger.warning(
                f"等待讨论组自动转发超时（帖子 {sorted(wanted)}，{timeout:.0f}s）；"
                "评论模式本次跳过评论区"
            )
            return None
        finally:
            if self._forward_waiter is not None and self._forward_waiter[1] is future:
                self._forward_waiter = None

    async def stop(self) -> None:
        if self._cli is not None:
            try:
                await self._cli.stop()
            except Exception:
                pass
            self._cli = None
            self.enabled = False

    # ────────────────────────── 发送 ──────────────────────────

    def build_payload(self, parse_result: Any, processed_list: list) -> tuple[str, list, list]:
        """构建推送载荷：文案 + 视频/图片（相册） + 动画（复用原项目构建逻辑）。"""
        from plugins.helpers import build_caption
        from plugins.parse.sender import build_input_media
        from repo.settings import SettingsConfig
        from utils.helpers import to_list

        config = SettingsConfig()
        caption = _clip(build_caption(parse_result, config=config) or "")
        photos_videos, animations = build_input_media(
            to_list(parse_result.media), processed_list, video_cover=config.video_cover
        )
        return caption, photos_videos, animations

    async def push(self, parse_result: Any, processed_list: list) -> bool:
        """推送一次解析结果；成功返回 True。异常在内部消化并记录日志。"""
        if not self.enabled or self._cli is None:
            return True

        mode = self.effective_live_photo_mode
        live_count = self._live_photo_count(processed_list)
        if mode == "comment" and live_count > 0:
            try:
                return await self._push_comment(parse_result, processed_list)
            except Exception as e:
                logger.warning(f"评论模式推送失败（{e}），回退为普通视频推送")
            return await self._push_normal(parse_result, processed_list)

        if mode != "video" and live_count > 0:
            max_items = self.settings.live_photo_max_items
            if max_items > 0 and live_count > max_items:
                logger.info(
                    f"实况图 {live_count} 张 > 上限 {max_items}，本次回退为视频模式（避免逐条发送刷屏）"
                )
            else:
                try:
                    return await self._push_live_photo(parse_result, processed_list)
                except Exception as e:
                    logger.warning(f"Live Photo 推送失败（{e}），回退为视频模式")

        return await self._push_normal(parse_result, processed_list)

    # ────────────── 普通推送（视频/图片/动画） ──────────────

    async def _push_normal(self, parse_result: Any, processed_list: list) -> bool:
        """原有推送逻辑：视频/图片/动画（LivePhotoFile 会被原项目映射为视频）。"""
        message = await self._push_normal_message(parse_result, processed_list)
        if message is None:
            return False
        logger.success(f"已推送至 Telegram: {self._target_name}")
        return True

    async def _push_normal_message(self, parse_result: Any, processed_list: list) -> Any:
        """普通推送，返回频道里的首条消息（供评论模式定位评论区）。"""
        try:
            caption, photos_videos, animations = self.build_payload(parse_result, processed_list)
        except Exception as e:
            logger.error(f"构建 TG 推送载荷失败: {e}")
            return None

        total = len(photos_videos) + len(animations)
        logger.info(f"开始推送至 Telegram（媒体数: {total}）...")
        try:
            return await self._send_media_payload(photos_videos, animations, caption)
        except Exception as e:
            logger.warning(f"常规推送失败（{e}），降级为文档发送")
            try:
                return await self._send_as_documents(photos_videos, animations, caption)
            except Exception as e2:
                logger.error(f"TG 推送失败: {e2}")
                return None

    async def _send_media_payload(self, photos_videos: list, animations: list, caption: str) -> Any:
        """按数量选择单条 / 相册 / 纯文本发送，返回首条消息。"""
        total = len(photos_videos) + len(animations)
        if total == 0:
            return await self._send_text(caption)
        if total == 1:
            return await self._send_single(animations + photos_videos, caption)
        return await self._send_multi(photos_videos, animations, caption)

    # ────────────── 实况图推送 ──────────────

    @staticmethod
    def _live_photo_count(processed_list: list) -> int:
        from parsehub.types import LivePhotoFile

        return sum(1 for pm in processed_list if isinstance(pm.source, LivePhotoFile))

    def _build_live_plan(self, parse_result: Any, processed_list: list) -> tuple[list, list, list]:
        """拆分实况图与普通媒体。

        返回 (live_pairs, normal_refs, normal_processed)：
          - live_pairs: [(photo_path, video_path, ProcessedMedia), ...]
          - normal_*  : 其余媒体（both/comment 模式下实况视频也会进 normal，走视频相册）
        """
        from parsehub.types import LivePhotoFile
        from utils.helpers import to_list

        refs = to_list(parse_result.media)
        mode = self.effective_live_photo_mode
        live_pairs: list[tuple[Path, Path, Any]] = []
        normal_refs: list[Any] = []
        normal_processed: list[Any] = []

        for ref, pm in zip(refs, processed_list, strict=False):
            if isinstance(pm.source, LivePhotoFile) and mode in ("live", "both", "comment"):
                if self._live_pair_ok(pm):
                    live_pairs.append(
                        (Path(str(pm.source.path)), Path(str(pm.source.video_path)), pm)
                    )
                    if mode in ("both", "comment"):
                        # 视频也进普通列表（build_input_media 会把 LivePhotoFile 映射成视频）
                        normal_refs.append(ref)
                        normal_processed.append(pm)
                    continue
            normal_refs.append(ref)
            normal_processed.append(pm)

        return live_pairs, normal_refs, normal_processed

    def _live_pair_ok(self, pm: Any) -> bool:
        """检查实况图视频是否满足 Telegram 限制；超限/缺文件则改发视频。"""
        from parsehub.utils.media_info import MediaInfoReader

        photo = Path(str(getattr(pm.source, "path", "") or ""))
        video = Path(str(getattr(pm.source, "video_path", "") or ""))
        if not photo.is_file() or not video.is_file():
            logger.warning(f"实况图缺少静态图或视频，改发视频: {photo} / {video}")
            return False

        max_mb = max(1, self.settings.live_photo_max_mb)
        try:
            size = video.stat().st_size
        except OSError:
            return False
        if size > max_mb * 1024 * 1024:
            logger.info(
                f"实况视频 {video.name} 超过 {max_mb}MB（{size / 1048576:.1f}MB），改发视频"
            )
            return False

        max_seconds = max(1, self.settings.live_photo_max_seconds)
        try:
            duration = MediaInfoReader.read(video).duration or 0
        except Exception as e:
            logger.warning(f"读取实况视频时长失败（{e}），改发视频: {video.name}")
            return False
        if duration and duration > max_seconds:
            logger.info(f"实况视频 {video.name} 时长 {duration}s 超过 {max_seconds}s，改发视频")
            return False
        return True

    async def _push_comment(self, parse_result: Any, processed_list: list) -> bool:
        """评论模式：频道发视频/相册，Live Photo 逐条发到该帖评论区。

        - 频道优先把解析出的视频（LivePhotoFile 会被 build_input_media 映射成视频）
          按普通逻辑发成相册；没有视频时发第一张静态图（用户要求的 A 方案）。
        - 频道消息发出后，等待讨论组的自动转发消息（评论根），再逐条 reply。
        - 频道发送成功但评论区失败时不回退重发，避免频道出现重复帖子。
        """
        from plugins.helpers import build_caption
        from plugins.parse.sender import build_input_media
        from repo.settings import SettingsConfig

        config = SettingsConfig()
        caption = _clip(build_caption(parse_result, config=config) or "")
        live_pairs, normal_refs, normal_processed = self._build_live_plan(parse_result, processed_list)
        if not live_pairs:
            logger.info("评论模式没有合格的实况图，改走普通推送")
            return await self._push_normal(parse_result, processed_list)

        # 评论区不限制张数：逐条发到同一评论根下，超长图集也不会截断。
        interval = max(0.0, float(self.settings.live_photo_send_interval or 0))
        logger.info(
            f"评论模式: 频道媒体 {len(normal_refs)} 项，评论区实况图 {len(live_pairs)} 张"
            + (f"，逐条间隔 {interval:g}s" if interval else "")
        )

        async with self._discussion_lock:
            channel_message = None
            if normal_refs:
                try:
                    photos_videos, animations = build_input_media(
                        normal_refs, normal_processed, video_cover=config.video_cover
                    )
                    if photos_videos or animations:
                        channel_message = await self._send_media_payload(
                            photos_videos, animations, caption
                        )
                except Exception as e:
                    logger.warning(f"评论模式频道媒体发送失败（{e}），尝试改用首张静态图")

            if channel_message is None:
                # A 方案：实在没有可发送的视频/媒体时，频道只发第一张静态图。
                try:
                    channel_message = await self._cli.send_photo(
                        self._target,
                        str(live_pairs[0][0]),
                        caption=caption,
                    )
                except Exception as e:
                    logger.error(f"评论模式频道静态图发送失败: {e}")
                    return False

            root_id = await self._wait_discussion_root([channel_message.id])
            if root_id is None:
                logger.warning(
                    f"频道已发送（消息 {channel_message.id}），但未拿到讨论组根消息；"
                    "本次评论区跳过，不回退重发"
                )
                logger.success(f"已推送至 Telegram: {self._target_name}")
                return True

            sent = 0
            failed = 0
            for index, (photo, video, pm) in enumerate(live_pairs):
                # 每张 Live Photo 要上传静态图 + 实况视频，连续发送容易触发限流；
                # 用可配置间隔把上传摊开（0 = 不等待）。
                if index and interval:
                    await asyncio.sleep(interval)
                try:
                    await self._send_live(
                        photo,
                        video,
                        "",
                        chat_id=self._discussion_group_id,
                        reply_to_message_id=root_id,
                    )
                    sent += 1
                except Exception as e:
                    failed += 1
                    logger.warning(f"评论区 Live Photo 发送失败（{e}），改发视频: {video.name}")
                    try:
                        await self._send_video_file(
                            pm,
                            "",
                            chat_id=self._discussion_group_id,
                            reply_to_message_id=root_id,
                        )
                        sent += 1
                    except Exception as e2:
                        logger.error(f"评论区视频兜底发送失败: {e2}")

            if failed:
                logger.warning(f"评论模式完成，但有 {failed} 张实况图发送失败")
            logger.success(
                f"评论模式完成：频道消息 {channel_message.id}，评论区 {sent} 条 "
                f"（讨论组根消息 {root_id}）"
            )
            return True

    async def _push_live_photo(self, parse_result: Any, processed_list: list) -> bool:
        """实况图推送：逐条 send_live_photo；失败/超限回退视频。"""
        from plugins.helpers import build_caption
        from plugins.parse.sender import build_input_media
        from repo.settings import SettingsConfig

        config = SettingsConfig()
        caption = _clip(build_caption(parse_result, config=config) or "")
        live_pairs, normal_refs, normal_processed = self._build_live_plan(parse_result, processed_list)
        if not live_pairs:
            # 全部超限/缺文件 → 回到普通视频推送
            return await self._push_normal(parse_result, processed_list)

        logger.info(
            f"实况图推送: {len(live_pairs)} 张（模式: {self.effective_live_photo_mode}，"
            f"限制 ≤{self.settings.live_photo_max_seconds}s / ≤{self.settings.live_photo_max_mb}MB）"
        )
        caption_pending = caption
        media_sent = False
        failed = 0
        interval = max(0.0, float(self.settings.live_photo_send_interval or 0))

        for index, (photo, video, pm) in enumerate(live_pairs):
            # 与 comment 模式一致：逐条上传之间留出间隔，降低连续发送触发限流的概率。
            if index and interval:
                await asyncio.sleep(interval)
            cap = caption_pending
            try:
                await self._send_live(photo, video, cap)
                media_sent = True
                caption_pending = ""
            except Exception as e:
                failed += 1
                logger.warning(f"Live Photo 发送失败（{e}），改发视频: {video.name}")
                try:
                    await self._send_video_file(pm, cap)
                    media_sent = True
                    caption_pending = ""
                except Exception as e2:
                    logger.error(f"视频兜底发送失败: {e2}")

        if normal_refs:
            try:
                photos_videos, animations = build_input_media(
                    normal_refs, normal_processed, video_cover=config.video_cover
                )
                await self._send_media_payload(photos_videos, animations, caption_pending)
                media_sent = True
                caption_pending = ""
            except Exception as e:
                failed += 1
                logger.error(f"实况图附带的普通媒体发送失败: {e}")
        if not media_sent:
            if failed:
                logger.error("实况图推送失败：没有任何媒体发送成功")
                return False
            if caption_pending:
                await self._send_text(caption_pending)
                media_sent = True

        if failed:
            logger.warning(f"实况图推送完成，但有 {failed} 项失败（已尽量回退视频）")
        logger.success(f"已推送至 Telegram: {self._target_name}")
        return True

    async def _send_live(
        self,
        photo: Path,
        video: Path,
        caption: str,
        chat_id: Any = None,
        reply_to_message_id: int | None = None,
    ) -> Any:
        """发送一条 Live Photo（修正 kurigram 2.2.26 的 cover 引用问题）。"""
        return await send_live_photo(
            self._cli,
            chat_id or self._target,
            photo=photo,
            video=video,
            caption=caption or "",
            reply_to_message_id=reply_to_message_id,
        )

    async def _send_video_file(
        self,
        pm: Any,
        caption: str,
        chat_id: Any = None,
        reply_to_message_id: int | None = None,
    ) -> Any:
        """把实况图降级为普通视频发送（带封面，失败去掉封面重试）。"""
        video = str(pm.source.video_path)
        cover = str(pm.source.path) if getattr(pm.source, "path", None) else None
        target = chat_id or self._target
        try:
            return await self._cli.send_video(
                target,
                video,
                caption=caption or "",
                video_cover=cover,
                supports_streaming=True,
                reply_to_message_id=reply_to_message_id,
            )
        except Exception as e:
            logger.warning(f"视频带封面发送失败（{e}），移除封面重试")
            return await self._cli.send_video(
                target,
                video,
                caption=caption or "",
                supports_streaming=True,
                reply_to_message_id=reply_to_message_id,
            )

    # ────────────── 单条 / 多条 / 文档降级 ──────────────

    async def _send_text(self, caption: str) -> Any:
        from pyrogram.types import LinkPreviewOptions

        return await self._cli.send_message(
            self._target,
            caption or "-",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    async def _send_single(self, items: list, caption: str) -> Any:
        from pyrogram.types import InputMediaAnimation, InputMediaPhoto, InputMediaVideo

        item = items[0]
        if isinstance(item, InputMediaAnimation):
            return await self._cli.send_animation(
                self._target,
                item.media,
                caption=caption,
                duration=item.duration,
                width=item.width,
                height=item.height,
            )
        if isinstance(item, InputMediaPhoto):
            return await self._cli.send_photo(self._target, item.media, caption=caption)
        if isinstance(item, InputMediaVideo):
            try:
                return await self._cli.send_video(
                    self._target,
                    item.media,
                    caption=caption,
                    duration=item.duration,
                    width=item.width,
                    height=item.height,
                    video_cover=item.video_cover,
                    supports_streaming=True,
                )
            except Exception as e:
                logger.warning(f"视频带封面发送失败（{e}），移除封面重试")
                return await self._cli.send_video(
                    self._target,
                    item.media,
                    caption=caption,
                    duration=item.duration,
                    width=item.width,
                    height=item.height,
                    supports_streaming=True,
                )
        return await self._cli.send_document(self._target, item.media, caption=caption)

    async def _send_multi(self, photos_videos: list, animations: list, caption: str) -> Any:
        # 动画逐条发送（与原项目一致：仅当没有图片/视频时，caption 落在最后一条动画上）
        first = None
        for i, ani in enumerate(animations):
            cap = caption if (i == len(animations) - 1 and not photos_videos) else ""
            message = await self._cli.send_animation(
                self._target,
                ani.media,
                caption=cap,
                duration=ani.duration,
                width=ani.width,
                height=ani.height,
            )
            first = first or message

        if not photos_videos:
            return first

        # 图片/视频分批走相册（每批最多 10 个），caption 落在最后一批的第一项
        for batch in batched(photos_videos, 10):
            batch = list(batch)
            if batch[-1] is photos_videos[-1]:
                batch[0].caption = caption
            if len(batch) == 1:
                message = await self._send_single(batch, getattr(batch[0], "caption", "") or "")
            else:
                messages = await self._cli.send_media_group(self._target, media=batch)
                message = messages[0] if messages else None
            first = first or message
        return first

    async def _send_as_documents(self, photos_videos: list, animations: list, caption: str) -> Any:
        from pyrogram.types import InputMediaDocument

        docs = [InputMediaDocument(media=str(item.media)) for item in (animations + photos_videos)]
        if not docs:
            if caption:
                return await self._send_text(caption)
            return None

        first = None
        for batch in batched(docs, 10):
            batch = list(batch)
            if batch[-1] is docs[-1]:
                batch[-1].caption = caption
            if len(batch) == 1:
                message = await self._cli.send_document(
                    self._target, batch[0].media, caption=getattr(batch[0], "caption", "") or ""
                )
            else:
                messages = await self._cli.send_media_group(self._target, media=batch)
                message = messages[0] if messages else None
            first = first or message
        return first
