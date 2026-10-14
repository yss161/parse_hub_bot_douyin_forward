# -*- coding: utf-8 -*-
"""桥接层：调用 parse_hub_bot 原项目的解析实现（原项目代码零改动）。

调用关系（全部复用原项目公开的服务层）：
    services.ParseService().get_raw_url(url)    ← 展开短链 / 规范化链接（原项目实现）
    services.ParseService().parse(url)          ← 纯解析，不下载（--no-download 时使用）
    services.ParsePipeline(...)                 ← 解析 → 下载 → 媒体处理（原项目实现）

本模块只做三件事：
    1. 提供日志版 StatusReporter（原项目流水线要求调用方实现该协议）；
    2. 组合原项目流水线参数；
    3. 在流水线临时目录被自动清理【之前】完成归档拷贝。

注意：本模块必须在 run.py 完成引导后再导入（sys.path / 工作目录 / 占位环境变量）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from log import logger

# —— 原项目模块（run.py 已把 parse_hub_bot 加入 sys.path）——
from core import bs
from i18n import t_ as _i18n_t
from parsehub.types import AnyParseResult
from services import ParsePipeline, ParseService

from .archive import ArchiveResult, archive_result
from .reporter import LogStatusReporter

logger = logger.bind(name="Forwarder/Bridge")


@dataclass
class ParseOutcome:
    """一次解析的最终结果。"""

    url: str
    raw_url: str = ""
    ok: bool = False
    error: str | None = None
    title: str = ""
    parse_result: AnyParseResult | None = None
    archive: ArchiveResult | None = None
    # 推送结果：None=未启用推送；True=推送成功；False=推送失败（解析与归档本身成功）
    push_ok: bool | None = None


def _default_locale_selector():
    """取默认语言的 i18n 选择器（原项目用法：t_[lang]；lang 为空 → 源语言）。"""
    for key in (None, bs.language, "zh-hans"):
        try:
            return _i18n_t[key]
        except Exception:
            continue
    return _i18n_t


async def parse_url(
    url: str,
    *,
    output_dir: Path,
    save_metadata: bool = True,
    skip_media_processing: bool = True,
    reporter: LogStatusReporter | None = None,
    download: bool = True,
    on_files_ready: Callable[[Any], Awaitable[bool | None]] | None = None,
) -> ParseOutcome:
    """用原项目的解析链路处理一个链接。

    :param url: 待解析链接（私信里提取到的，可能是短链）
    :param output_dir: 归档输出目录
    :param download: False 时只解析（不下载、不归档），用于快速验证解析是否可用
    :param on_files_ready: 可选回调——归档完成后、临时文件清理前调用（传入流水线结果），
        用于可选的 Telegram 推送等后续动作；返回 False 表示后续动作失败
    """
    reporter = reporter or LogStatusReporter()
    ps = ParseService()
    outcome = ParseOutcome(url=url)

    # ── 1. 展开原始链接（原项目实现，含代理/重试） ──
    try:
        raw_url = await ps.get_raw_url(url)
    except Exception as e:
        outcome.error = f"展开原始链接失败: {e}"
        logger.warning(f"{outcome.error} | url={url}")
        return outcome
    outcome.raw_url = raw_url

    # ── 2. 仅解析模式 ──
    if not download:
        try:
            pr = await ps.parse(url)
        except Exception as e:
            outcome.error = f"解析失败: {e}"
            logger.warning(f"{outcome.error} | url={url}")
            return outcome
        outcome.parse_result = pr
        outcome.title = str(getattr(pr, "title", "") or "")
        outcome.ok = True
        return outcome

    # ── 3. 完整流水线：解析 → 下载 →（可选）媒体处理 ──
    t = _default_locale_selector()
    try:
        with ParsePipeline(
            url,
            raw_url,
            reporter,
            singleflight=True,
            skip_media_processing=skip_media_processing,
            save_metadata=save_metadata,
            t=t,
        ) as pipeline:
            result = await pipeline.run()
            if result is None:
                outcome.error = (
                    "相同链接正在被其它任务解析（singleflight）"
                    if pipeline.waited
                    else "流水线执行失败（详见上方日志）"
                )
                return outcome

            outcome.parse_result = result.parse_result
            outcome.title = str(getattr(result.parse_result, "title", "") or "")

            # 【关键】必须在 with 退出（原项目自动清理临时目录）之前完成归档拷贝
            outcome.archive = archive_result(
                result,
                source_url=url,
                raw_url=raw_url,
                output_dir=output_dir,
                save_metadata=save_metadata,
            )
            outcome.ok = True

            # 可选：归档完成后的后续动作（此刻归档文件与临时文件都有效，供推送使用）
            if on_files_ready is not None:
                try:
                    outcome.push_ok = bool(await on_files_ready(result))
                except Exception as e:
                    logger.error(f"归档后回调异常: {e}")
                    outcome.push_ok = False

            return outcome
    except Exception as e:
        logger.opt(exception=e).error(f"解析流水线异常: {url}")
        outcome.error = f"流水线异常: {e}"
        return outcome
