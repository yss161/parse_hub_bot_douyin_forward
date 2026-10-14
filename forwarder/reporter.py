# -*- coding: utf-8 -*-
"""状态报告器：实现原项目 services.pipeline.StatusReporter 协议。

原项目把"解析中/下载中/处理中"等状态通过该协议上报给调用方（TG 里是编辑消息），
本服务没有 TG 消息可编辑，因此把状态写入日志。
"""

from __future__ import annotations

from log import logger  # 原项目 loguru logger 绑定（run.py 已确保 sys.path）


class LogStatusReporter:
    """把流水线状态写进日志的 StatusReporter 实现（结构化协议，无需继承）。"""

    def __init__(self, name: str = "Pipeline") -> None:
        self._logger = logger.bind(name=f"Forwarder/{name}")
        self._last_text: str | None = None

    async def report(self, text: str) -> None:
        if text == self._last_text:
            return
        self._last_text = text
        self._logger.info(text)

    async def report_error(self, stage: str, error: Exception) -> None:
        self._logger.error(f"步骤 [{stage}] 失败: {error!r}")

    async def dismiss(self) -> None:
        self._last_text = None
