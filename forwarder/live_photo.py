# -*- coding: utf-8 -*-
"""Telegram Live Photo 发送的兼容实现。

kurigram 2.2.26 的 InputMediaLivePhoto.write 在第二次
messages.UploadMedia 时把视频再次作为 InputFile 传给
InputMediaUploadedPhoto.video，而该字段的类型是 InputDocument。
Telegram 会因此报 FILE_REFERENCE_COVER_EXPIRED（或被客户端重试掩盖）。
这里复用第一次上传得到的 InputDocument，只上传一次视频，再发消息。

只依赖 pyrogram，导入时不加载；便于在无 Telegram 依赖的环境中做静态检查。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


async def send_live_photo(
    client: Any,
    chat_id: Any,
    photo: Path,
    video: Path,
    caption: str = "",
    reply_to_message_id: int | None = None,
    top_msg_id: int | None = None,
) -> Any:
    """发送一条 Live Photo，返回 pyrogram Message（或 None）。

    参数:
        client: 已登录的 pyrogram Client。
        chat_id: 目标会话（@username / 数字 ID）。
        photo: 静态图路径（JPEG）。
        video: 实况视频路径（MP4，≤10 秒 / ≤10MB）。
        caption: 文案；为空时不带文案。
        reply_to_message_id: 回复的消息 ID；评论区模式传讨论组根消息 ID。
        top_msg_id: 论坛话题 ID；普通讨论组留空。
    """
    from pyrogram import raw, utils

    photo = Path(photo)
    video = Path(video)
    if not photo.is_file():
        raise FileNotFoundError(f"实况图静态图不存在: {photo}")
    if not video.is_file():
        raise FileNotFoundError(f"实况视频不存在: {video}")

    peer = await client.resolve_peer(chat_id)

    # 1) 上传实况视频，得到可复用的 InputDocument（不是 InputFile）。
    uploaded_video = await client.invoke(
        raw.functions.messages.UploadMedia(
            peer=peer,
            media=raw.types.InputMediaUploadedDocument(
                mime_type=client.guess_mime_type(str(video)) or "video/mp4",
                file=await client.save_file(str(video)),
                attributes=[
                    raw.types.DocumentAttributeVideo(duration=0, w=0, h=0),
                ],
            ),
        )
    )
    document = getattr(uploaded_video, "document", None)
    if document is None:
        raise RuntimeError("实况视频上传后未返回 document")

    input_document = raw.types.InputDocument(
        id=document.id,
        access_hash=document.access_hash,
        file_reference=document.file_reference,
    )

    # 2) 上传静态图，并把上一步的 InputDocument 作为 live photo 的 video。
    #    原实现这里传入的是 save_file(video)（InputFile），会触发
    #    FILE_REFERENCE_COVER_EXPIRED。
    uploaded_photo = await client.invoke(
        raw.functions.messages.UploadMedia(
            peer=peer,
            media=raw.types.InputMediaUploadedPhoto(
                file=await client.save_file(str(photo)),
                video=input_document,
                live_photo=True,
            ),
        )
    )
    uploaded = getattr(uploaded_photo, "photo", None)
    if uploaded is None:
        raise RuntimeError("实况图静态图上传后未返回 photo")

    media = raw.types.InputMediaPhoto(
        id=raw.types.InputPhoto(
            id=uploaded.id,
            access_hash=uploaded.access_hash,
            file_reference=uploaded.file_reference,
        ),
        live_photo=True,
        video=input_document,
    )

    # 3) 发送 Live Photo 消息；caption 的实体解析复用 pyrogram 的工具。
    #    评论区模式：回复讨论组里的自动转发消息（root），而不是频道帖子。
    reply_to = None
    if reply_to_message_id:
        reply_to = raw.types.InputReplyToMessage(
            reply_to_msg_id=int(reply_to_message_id),
            top_msg_id=int(top_msg_id) if top_msg_id else None,
        )
    text = await utils.parse_text_entities(client, caption or "", None, None)
    result = await client.invoke(
        raw.functions.messages.SendMedia(
            peer=peer,
            media=media,
            random_id=client.rnd_id(),
            reply_to=reply_to,
            **text,
        )
    )
    messages = await utils.parse_messages(client=client, messages=result)
    return messages[0] if messages else None
