# -*- coding: utf-8 -*-
"""解析任务描述（抖音轮询、Telegram /parse、失败重试共用）。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ParseJob:
    url: str
    source: str = "telegram"
    msg_fp: str = ""
    # Telegram 回复目标（owner 私聊或讨论组）；None = 不回复
    reply_chat_id: int | None = None
    reply_to_message_id: int | None = None
    requested_by: int | None = None
    failure_id: int | None = None
    retry: bool = False
