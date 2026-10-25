# -*- coding: utf-8 -*-
"""转发链路服务入口（由仓库根目录 run.py 引导后调用）。

整体流程：
    【大号】在手机上把视频转发给【小号】
        → 服务器上的小号（抖音网页版，Playwright）收到私信
        → 本模块轮询识别新消息并提取视频链接
        → parser_bridge 调用原项目解析链路（解析 → 下载 → 归档到 output/）
        → （可选）tg_push 把结果推送到指定 Telegram 群组/频道（TG_PUSH=true）

用法（统一从仓库根目录执行）：
    python run.py serve            # 启动常驻服务
    python run.py serve --once     # 只跑一轮（调试）
    python run.py login            # 仅扫码登录小号后退出
    python run.py test-url <链接>  # 只测试解析链路（不启动浏览器）
"""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path
from typing import Any

from log import logger

from .config import ForwarderSettings, split_accounts
from .cookie_config import apply_cookies, clear_cookies, current_cookies, export_from_browser, mask_cookie
from .douyin_web import DouyinWebReceiver
from .parser_bridge import parse_url
from .reporter import LogStatusReporter
from .state import StateStore, fingerprint, video_id_from_url
from .tg_push import TgPusher

logger = logger.bind(name="Forwarder/Main")

# 同一链接最多重试次数，超过后放弃并记录
MAX_PARSE_ATTEMPTS = 3

# 定时缓存清理的执行间隔（秒）；实际保留时长由 FORWARDER_CACHE_TTL_HOURS 决定
CACHE_SWEEP_INTERVAL = 3600


def _remove_cache_dir(output_root: Path, target: Path) -> bool:
    """安全删除归档目录：仅允许删除 output_root 内部的目录（防止误删）。"""
    try:
        root = output_root.resolve()
        t = target.resolve()
        if root in t.parents and t.is_dir():
            shutil.rmtree(t)
            return True
    except Exception:
        pass
    return False


def sweep_cache(settings: ForwarderSettings, *, ttl_hours: float | None = None) -> None:
    """定时清理本地缓存：output/ 作品目录、debug/ 转储、downloads/ 残留。

    - 只删除"最后修改时间超过保留时长"的内容（正在处理的目录 mtime 是新的，不受影响）；
    - 已处理记录保存在 state.json（含视频 ID 指纹），缓存删除后也不会重复下载。
    """
    ttl = settings.cache_ttl_hours if ttl_hours is None else ttl_hours
    if ttl <= 0:
        return
    cutoff = time.time() - ttl * 3600
    removed_dirs = removed_files = 0

    def _is_old(p: Path) -> bool:
        try:
            return p.stat().st_mtime < cutoff
        except Exception:
            return False

    # 1) output/ 作品归档目录
    try:
        for entry in settings.output_dir.iterdir():
            if entry.is_dir() and _is_old(entry) and _remove_cache_dir(settings.output_dir, entry):
                removed_dirs += 1
    except Exception:
        pass

    # 2) debug/ HTML 转储
    try:
        for f in settings.debug_dir.glob("*.html"):
            if _is_old(f):
                f.unlink(missing_ok=True)
                removed_files += 1
    except Exception:
        pass

    # 3) downloads/ 残留目录（正常情况下原项目会自行清理，异常退出时兜底）
    try:
        for entry in (settings.data_dir / "downloads").iterdir():
            if entry.is_dir() and _is_old(entry):
                shutil.rmtree(entry, ignore_errors=True)
                removed_dirs += 1
    except Exception:
        pass

    if removed_dirs or removed_files:
        logger.info(f"🧹 缓存清理: 删除 {removed_dirs} 个目录 / {removed_files} 个转储文件（保留最近 {ttl:g} 小时）")
    else:
        logger.debug(f"缓存清理: 无需处理（保留最近 {ttl:g} 小时）")


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
    logger.info(
        f"检测模式: {receiver.effective_detect_mode}"
        + ("（DOM 探针 + 定期强制取数 + 自愈刷新）" if receiver.effective_detect_mode == "probe"
           else "（每轮整页刷新）")
        + f"；轮询间隔 {receiver.poll_interval():g}s"
        + ("（控制台设置，优先于 .env；mode reset 可恢复）" if state.get_value("detect_mode") else "（.env 默认）")
    )
    _mode_override = state.get_value("detect_mode")
    if _mode_override and _mode_override != settings.normalized_detect_mode():
        logger.warning(
            f"检测模式使用控制台设置: {receiver.effective_detect_mode}"
            f"（.env 为 {settings.normalized_detect_mode()}）；如需恢复 .env 默认，往 agent_cmd.txt 写 mode reset"
        )
    reporter = LogStatusReporter("Pipeline")
    pusher = TgPusher(settings, state)
    logger.info(
        f"实况图模式: {pusher.effective_live_photo_mode}"
        + ("（只发实况图）" if pusher.effective_live_photo_mode == "live"
           else ("（实况图 + 视频）" if pusher.effective_live_photo_mode == "both" else "（只发视频）"))
        + f"；限制 ≤{settings.live_photo_max_seconds}s / ≤{settings.live_photo_max_mb}MB"
        + (
            f"；实况图超过 {settings.live_photo_max_items} 张回退视频"
            if settings.live_photo_max_items > 0
            else "；实况图数量不限制"
        )
    )

    override = state.get_value("main_account")
    if override:
        names = split_accounts(override)
        logger.info(
            f"监测账号使用「控制台设置」值（{len(names)} 个）: {'、'.join(names)}"
            f"（.env 默认: {settings.main_account or '未设置'}；控制台输入 reset 可恢复默认）"
        )

    # 启动时先做一次缓存清理（清掉上次运行留下的过期内容）
    if settings.cache_ttl_hours > 0:
        sweep_cache(settings)

    await receiver.start()
    if settings.tg_push:
        await pusher.start()
    # 可选：每次启动（小号登录成功后）自动把小号 cookie 同步到解析端（解决手动 cookie 过期问题）
    if settings.auto_sync_cookie:
        try:
            cookie = await export_from_browser(receiver)
            if cookie:
                await apply_cookies([cookie])
                logger.success("已自动把小号登录态 cookie 同步到解析端 ✅")
            else:
                logger.warning("自动同步解析 cookie 失败：未从小号浏览器获取到 cookie")
        except Exception as e:
            logger.warning(f"自动同步解析 cookie 异常（忽略，不影响运行）: {e}")
    if once:
        logger.info("启动完成（--once：只执行一轮轮询）")

    last_parse_ts = 0.0
    last_sweep = time.monotonic()
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
                    state.mark_settled(cand.msg_fp)
                    receiver.mark_inflight_done(cand.msg_fp)
                    continue

                # 作品级重复检测：同一视频换链接/换消息重复转发时也跳过
                vid = video_id_from_url(cand.url)
                if vid and state.is_video_processed(vid):
                    logger.info(f"检测到重复作品（视频 ID {vid} 已处理过），跳过: {cand.url}")
                    state.mark_settled(cand.msg_fp)
                    receiver.mark_inflight_done(cand.msg_fp)
                    continue

                # 与上一次解析保持最小间隔，避免请求过于频繁
                wait = settings.parse_min_interval - (time.monotonic() - last_parse_ts)
                if wait > 0:
                    await asyncio.sleep(wait)

                async def push_cb(result: Any) -> bool:
                    return await pusher.push(result.parse_result, result.processed_list)

                logger.info(f"开始解析（来源: {cand.source}）: {cand.url}")
                outcome = await parse_url(
                    cand.url,
                    output_dir=settings.output_dir,
                    save_metadata=settings.save_metadata,
                    skip_media_processing=settings.skip_media_processing,
                    reporter=reporter,
                    on_files_ready=push_cb if pusher.enabled else None,
                )
                last_parse_ts = time.monotonic()

                push_failed = outcome.ok and outcome.push_ok is False
                if outcome.ok and outcome.archive is not None and not push_failed:
                    state.mark_processed(fp, cand.url, str(outcome.archive.dir))
                    if outcome.raw_url and outcome.raw_url != cand.url:
                        state.mark_processed(
                            fingerprint(outcome.raw_url), outcome.raw_url, str(outcome.archive.dir)
                        )
                    # 作品级去重记录（含展开后链接里的视频 ID）
                    for video_url in {outcome.raw_url or cand.url, cand.url}:
                        video_id = video_id_from_url(video_url)
                        if video_id:
                            state.mark_video_processed(video_id, video_url, str(outcome.archive.dir))
                    state.clear_failures(fp)
                    state.mark_settled(cand.msg_fp)
                    receiver.mark_inflight_done(cand.msg_fp)
                    logger.success(
                        f"解析完成 ✅ 已归档: {outcome.archive.dir} "
                        f"（{len(outcome.archive.files)} 个文件）"
                        + ("，已推送至 Telegram" if outcome.push_ok else "")
                    )
                    # 推送成功 → 按配置删除本地缓存（媒体已在 Telegram，无需保留副本）
                    if settings.cleanup_after_push and outcome.push_ok:
                        if _remove_cache_dir(settings.output_dir, outcome.archive.dir):
                            logger.info(f"🧹 推送成功，已删除本地缓存: {outcome.archive.dir.name}")
                else:
                    error = (
                        "解析与归档成功，但 Telegram 推送失败（下一轮自动重试）"
                        if push_failed
                        else (outcome.error or "未知错误")
                    )
                    attempts = state.record_failure(fp, error)
                    if attempts >= MAX_PARSE_ATTEMPTS:
                        state.mark_gave_up(fp, cand.url, error)
                        state.mark_settled(cand.msg_fp)
                        receiver.mark_inflight_done(cand.msg_fp)
                        logger.error(f"已失败 {attempts} 次，放弃: {cand.url} —— {error}")
                    else:
                        # 撤销消息的"已识别"标记，下一轮重新提取并重试
                        state.unmark_seen_message(cand.msg_fp)
                        receiver.mark_inflight_done(cand.msg_fp)
                        logger.warning(
                            f"处理失败（第 {attempts}/{MAX_PARSE_ATTEMPTS} 次，稍后自动重试）: "
                            f"{cand.url} —— {error}"
                        )

                state.save()

            if once:
                logger.info("--once 模式：单轮结束，退出")
                return

            elapsed = time.monotonic() - cycle_start
            if receiver.consume_force_poll():
                logger.info("控制台触发：立即执行下一轮检查")
                await asyncio.sleep(1.0)
            else:
                wait = max(1.0, receiver.poll_interval() - elapsed)
                logger.debug(
                    f"本轮结束（耗时 {elapsed:.1f}s，模式 {receiver.effective_detect_mode}），"
                    f"{wait:.0f}s 后进入下一轮"
                )
                await asyncio.sleep(wait)

            # 定时缓存清理（默认每小时检查一次；保留时长见 FORWARDER_CACHE_TTL_HOURS）
            now = time.monotonic()
            if settings.cache_ttl_hours > 0 and now - last_sweep >= CACHE_SWEEP_INTERVAL:
                last_sweep = now
                try:
                    sweep_cache(settings)
                except Exception as e:
                    logger.debug(f"缓存清理异常（忽略）: {e}")
    finally:
        await pusher.stop()
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


# ══════════════════════════ 模式四：导出小号 cookie 到解析端 ══════════════════════════


async def run_export_cookie(settings: ForwarderSettings | None = None) -> None:
    """启动已登录的小号浏览器，把其抖音 cookie 自动同步到解析端 platform_config。"""
    settings = settings or ForwarderSettings()
    settings.ensure_dirs()
    receiver = DouyinWebReceiver(settings, StateStore(settings.state_file))
    try:
        await receiver.start()  # 启动浏览器并确保登录（未登录会进入扫码流程）
        cookie = await export_from_browser(receiver)
        if not cookie:
            logger.error("未能从小号浏览器获取抖音 cookie（可能尚未登录成功）")
            raise SystemExit(1)
        await apply_cookies([cookie])
        logger.success("已自动从小号登录态导出 cookie 并填入解析端 ✅（即时生效）")
    finally:
        await receiver.close()


# ══════════════════════════ 模式五：解析端 cookie 管理 ══════════════════════════


async def run_cookie_config(
    action: str,
    value: str = "",
    settings: ForwarderSettings | None = None,
) -> None:
    """解析端抖音 cookie 的增删查（不启动浏览器，直接操作原项目 platform_config）。"""
    settings = settings or ForwarderSettings()
    settings.ensure_dirs()

    if action == "show":
        cookies = current_cookies()
        if not cookies:
            print("当前解析端抖音 cookie：未配置（走移动端接口）")
            return
        print(f"当前解析端抖音 cookie：{len(cookies)} 条")
        for i, c in enumerate(cookies, 1):
            print(f"  [{i}] {mask_cookie(c)}")
        return

    if action == "clear":
        await clear_cookies()
        return

    if action == "set":
        if not value.strip():
            logger.error("set 需要提供 cookie 字符串，如: python run.py cookie-config set 'a=b; c=d'")
            raise SystemExit(1)
        await apply_cookies([value])
        return

    if action == "from-file":
        from pathlib import Path

        p = Path(value.strip())
        if not p.exists():
            logger.error(f"文件不存在: {p}")
            raise SystemExit(1)
        lines = [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if not lines:
            logger.error(f"文件为空: {p}")
            raise SystemExit(1)
        await apply_cookies(lines)
        return

    logger.error(f"未知动作: {action}")
    raise SystemExit(1)
