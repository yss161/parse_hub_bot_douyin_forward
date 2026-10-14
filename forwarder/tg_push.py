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

from itertools import batched
from typing import Any

from log import logger

from .config import ForwarderSettings

logger = logger.bind(name="Forwarder/TgPush")

# Telegram 媒体 caption 上限 1024 字符，留出余量
_CAPTION_MAX = 1000


def _clip(text: str) -> str:
    return text if len(text) <= _CAPTION_MAX else text[: _CAPTION_MAX - 1] + "…"


class TgPusher:
    """把解析结果推送到 Telegram 群组/频道（发送逻辑由本模块实现，构建逻辑复用原项目）。"""

    def __init__(self, settings: ForwarderSettings) -> None:
        self.settings = settings
        self.enabled = False
        self._cli: Any = None
        self._target: Any = None
        self._target_name = ""

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
            name="forwarder_tg",
            api_id=api_id,
            api_hash=api_hash,
            bot_token=bot_token,
            workdir=str(bs.sessions_path),
            no_updates=True,
            proxy=bs.bot_proxy or None,
        )
        try:
            await cli.start()
            chat = await cli.get_chat(target)
            self._target_name = getattr(chat, "title", None) or str(target)
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

        try:
            caption, photos_videos, animations = self.build_payload(parse_result, processed_list)
        except Exception as e:
            logger.error(f"构建 TG 推送载荷失败: {e}")
            return False

        total = len(photos_videos) + len(animations)
        logger.info(f"开始推送至 Telegram（媒体数: {total}）...")
        try:
            if total == 0:
                await self._send_text(caption)
            elif total == 1:
                await self._send_single(animations + photos_videos, caption)
            else:
                await self._send_multi(photos_videos, animations, caption)
        except Exception as e:
            logger.warning(f"常规推送失败（{e}），降级为文档发送")
            try:
                await self._send_as_documents(photos_videos, animations, caption)
            except Exception as e2:
                logger.error(f"TG 推送失败: {e2}")
                return False

        logger.success(f"已推送至 Telegram: {self._target_name}")
        return True

    # ────────────── 单条 / 多条 / 文档降级 ──────────────

    async def _send_text(self, caption: str) -> None:
        from pyrogram.types import LinkPreviewOptions

        await self._cli.send_message(
            self._target,
            caption or "-",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    async def _send_single(self, items: list, caption: str) -> None:
        from pyrogram.types import InputMediaAnimation, InputMediaPhoto, InputMediaVideo

        item = items[0]
        if isinstance(item, InputMediaAnimation):
            await self._cli.send_animation(
                self._target,
                item.media,
                caption=caption,
                duration=item.duration,
                width=item.width,
                height=item.height,
            )
        elif isinstance(item, InputMediaPhoto):
            await self._cli.send_photo(self._target, item.media, caption=caption)
        elif isinstance(item, InputMediaVideo):
            try:
                await self._cli.send_video(
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
                await self._cli.send_video(
                    self._target,
                    item.media,
                    caption=caption,
                    duration=item.duration,
                    width=item.width,
                    height=item.height,
                    supports_streaming=True,
                )
        else:
            await self._cli.send_document(self._target, item.media, caption=caption)

    async def _send_multi(self, photos_videos: list, animations: list, caption: str) -> None:
        # 动画逐条发送（与原项目一致：仅当没有图片/视频时，caption 落在最后一条动画上）
        for i, ani in enumerate(animations):
            cap = caption if (i == len(animations) - 1 and not photos_videos) else ""
            await self._cli.send_animation(
                self._target,
                ani.media,
                caption=cap,
                duration=ani.duration,
                width=ani.width,
                height=ani.height,
            )

        if not photos_videos:
            return

        # 图片/视频分批走相册（每批最多 10 个），caption 落在最后一批的第一项
        for batch in batched(photos_videos, 10):
            batch = list(batch)
            if batch[-1] is photos_videos[-1]:
                batch[0].caption = caption
            if len(batch) == 1:
                await self._send_single(batch, getattr(batch[0], "caption", "") or "")
            else:
                await self._cli.send_media_group(self._target, media=batch)

    async def _send_as_documents(self, photos_videos: list, animations: list, caption: str) -> None:
        from pyrogram.types import InputMediaDocument

        docs = [InputMediaDocument(media=str(item.media)) for item in (animations + photos_videos)]
        if not docs:
            if caption:
                await self._send_text(caption)
            return

        for batch in batched(docs, 10):
            batch = list(batch)
            if batch[-1] is docs[-1]:
                batch[-1].caption = caption
            if len(batch) == 1:
                await self._cli.send_document(
                    self._target, batch[0].media, caption=getattr(batch[0], "caption", "") or ""
                )
            else:
                await self._cli.send_media_group(self._target, media=batch)
