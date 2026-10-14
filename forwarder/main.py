# -*- coding: utf-8 -*-
"""转发链路服务入口（由仓库根目录 run.py 引导后调用）。

整体流程：
    【大号】在手机上把视频转发给【小号】
        → 服务器上的小号（抖音网页版，Playwright）收到私信
        → 本模块轮询识别新消息并提取视频链接
        → parser_bridge 调用原项目解析链路（解析 → 下载 → 归档）
        → 结果落盘到 output/ 目录

用法（统一从仓库根目录执行）：
    python run.py serve            # 启动常驻服务
    python run.py serve --once     # 只跑一轮（调试）
    python run.py login            # 仅扫码登录小号后退出
    python run.py test-url <链接>  # 只测试解析链路（不启动浏览器）
"""

from __future__ import annotations

import asyncio
import time

from log import logger

from .config import ForwarderSettings
from .douyin_web import DouyinWebReceiver
from .parser_bridge import parse_url
from .reporter import LogStatusReporter
from .state import StateStore, fingerprint

logger = logger.bind(name="Forwarder/Main")

# 同一链接最多重试次数，超过后放弃并记录
MAX_PARSE_ATTEMPTS = 3


# ══════════════════════════ 模式一：常驻服务 ══════════════════════════


async def run_service(settings: ForwarderSettings | None = None, *, once: bool = False) -> None:
    settings = settings or ForwarderSettings()
    settings.ensure_dirs()

    logger.info("=" * 64)
    if once:
        logger.info("抖音转发解析服务 —— 单轮运行（调试）")
    else:
        logger.info("抖音转发解析服务启动")
    logger.info(settings.describe())
    logger.info("=" * 64)

    state = StateStore(settings.state_file)
    receiver = DouyinWebReceiver(settings, state)
    reporter = LogStatusReporter("Pipeline")

    await receiver.start()
    if once:
        logger.info("启动完成（--once：只执行一轮轮询）")

    last_parse_ts = 0.0
    try:
        while True:
            cycle_start = time.monotonic()
            try:
                candidates = await receiver.poll_once()
            except Exception as e:
                logger.exception(f"轮询异常（本轮跳过）: {e}")
                candidates = []

            for cand in candidates:
                fp = fingerprint(cand.url)
                if state.is_processed(fp) or state.is_gave_up(fp):
                    logger.debug(f"已处理/已放弃，跳过: {cand.url}")
                    continue

                # 与上一次解析保持最小间隔，避免请求过于频繁
                wait = settings.parse_min_interval - (time.monotonic() - last_parse_ts)
                if wait > 0:
                    await asyncio.sleep(wait)

                logger.info(f"开始解析（来源: {cand.source}）: {cand.url}")
                outcome = await parse_url(
                    cand.url,
                    output_dir=settings.output_dir,
                    save_metadata=settings.save_metadata,
                    skip_media_processing=settings.skip_media_processing,
                    reporter=reporter,
                )
                last_parse_ts = time.monotonic()

                if outcome.ok and outcome.archive is not None:
                    state.mark_processed(fp, cand.url, str(outcome.archive.dir))
                    if outcome.raw_url and outcome.raw_url != cand.url:
                        state.mark_processed(
                            fingerprint(outcome.raw_url), outcome.raw_url, str(outcome.archive.dir)
                        )
                    state.clear_failures(fp)
                    logger.success(
                        f"解析完成 ✅ 已归档: {outcome.archive.dir} "
                        f"（{len(outcome.archive.files)} 个文件）"
                    )
                else:
                    attempts = state.record_failure(fp, outcome.error or "未知错误")
                    if attempts >= MAX_PARSE_ATTEMPTS:
                        state.mark_gave_up(fp, cand.url, outcome.error or "未知错误")
                        logger.error(f"已失败 {attempts} 次，放弃: {cand.url} —— {outcome.error}")
                    else:
                        # 撤销消息的"已识别"标记，下一轮重新提取并重试
                        state.unmark_seen_message(cand.msg_fp)
                        logger.warning(
                            f"解析失败（第 {attempts}/{MAX_PARSE_ATTEMPTS} 次，稍后自动重试）: "
                            f"{cand.url} —— {outcome.error}"
                        )

                state.save()

            if once:
                logger.info("--once 模式：单轮结束，退出")
                return

            elapsed = time.monotonic() - cycle_start
            wait = max(1.0, settings.poll_interval - elapsed)
            logger.debug(f"本轮结束（耗时 {elapsed:.1f}s），{wait:.0f}s 后进入下一轮")
            await asyncio.sleep(wait)
    finally:
        await receiver.close()
        logger.info("服务已停止")


# ══════════════════════════ 模式二：仅登录 ══════════════════════════


async def run_login(settings: ForwarderSettings | None = None) -> None:
    settings = settings or ForwarderSettings()
    settings.ensure_dirs()
    receiver = DouyinWebReceiver(settings, StateStore(settings.state_file))
    try:
        await receiver.start()
        logger.success("登录流程完成。登录态已保存，之后直接 `python run.py serve` 即可。")
    finally:
        await receiver.close()


# ══════════════════════════ 模式三：测试解析链路 ══════════════════════════


async def run_test_url(url: str, *, download: bool = True, settings: ForwarderSettings | None = None) -> None:
    """不启动浏览器，直接验证"原项目解析链路"是否可用。"""
    settings = settings or ForwarderSettings()
    settings.ensure_dirs()

    logger.info(f"测试解析: {url}（download={download}）")
    outcome = await parse_url(
        url,
        output_dir=settings.output_dir,
        save_metadata=settings.save_metadata,
        skip_media_processing=settings.skip_media_processing,
        download=download,
    )
    if not outcome.ok:
        logger.error(f"解析失败 ❌ {outcome.error}")
        raise SystemExit(1)

    pr = outcome.parse_result
    logger.success(
        "解析成功 ✅\n"
        f"  原始链接 : {outcome.raw_url}\n"
        f"  标题     : {outcome.title}\n"
        f"  类型     : {getattr(pr, 'type', '')}\n"
        + (f"  归档目录 : {outcome.archive.dir}（{len(outcome.archive.files)} 个文件）" if outcome.archive else "  （仅解析模式，未下载）")
    )
