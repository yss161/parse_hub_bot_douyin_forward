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

from . import sysinfo
from .archive import update_metadata
from .config import ForwarderSettings, split_accounts
from .control import ControlPlane
from .cookie_config import apply_cookies, clear_cookies, current_cookies, export_from_browser, mask_cookie
from .douyin_web import DouyinWebReceiver
from .failures import classify_failure, short_error
from .fallback import FallbackService
from .jobs import ParseJob
from .parser_bridge import ParseOutcome, parse_url
from .reporter import LogStatusReporter
from .runtime import RuntimeStats
from .state import StateStore, fingerprint, video_id_from_url
from .store import Store
from .tg_push import TgPusher

logger = logger.bind(name="Forwarder/Main")

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
    store = Store(settings.db_file)
    try:
        migrated = store.migrate_legacy_gave_up(state)
        if migrated:
            logger.info(f"已导入 {migrated} 条历史放弃记录到失败收件箱（旧 state.json → SQLite）")
    except Exception as e:
        logger.warning(f"导入历史失败记录失败（不影响启动）: {e}")
    runtime = RuntimeStats()
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
    fallback = FallbackService(settings, pusher, state)
    control = ControlPlane(settings, state, store, pusher, runtime, receiver)
    _live_mode = pusher.effective_live_photo_mode
    logger.info(
        f"实况图模式: {_live_mode}"
        + ("（只发实况图）" if _live_mode == "live"
           else ("（实况图 + 视频）" if _live_mode == "both"
                 else ("（视频发频道，实况图发评论区）" if _live_mode == "comment" else "（只发视频）")))
        + f"；限制 ≤{settings.live_photo_max_seconds}s / ≤{settings.live_photo_max_mb}MB"
        + (
            f"；评论区不限量，逐条间隔 {settings.live_photo_send_interval:g}s"
            if _live_mode == "comment"
            else (
                f"；实况图超过 {settings.live_photo_max_items} 张回退视频"
                if settings.live_photo_max_items > 0
                else "；实况图数量不限制"
            )
        )
    )

    override = state.get_value("main_account")
    if override:
        names = split_accounts(override)
        logger.info(
            f"监测账号使用「控制台设置」值（{len(names)} 个）: {'、'.join(names)}"
            f"（.env 默认: {settings.main_account or '未设置'}；控制台输入 reset 可恢复默认）"
        )

    if settings.cache_ttl_hours > 0:
        sweep_cache(settings)

    queue: asyncio.Queue[ParseJob] = asyncio.Queue()
    wake = asyncio.Event()
    pending: set[str] = set()
    last_parse_ts = 0.0
    last_sweep = time.monotonic()
    last_heartbeat = 0.0

    def enqueue(job: ParseJob) -> bool:
        fp = fingerprint(job.url)
        if fp in pending:
            return False
        pending.add(fp)
        queue.put_nowait(job)
        runtime.queue_size = queue.qsize()
        wake.set()
        return True

    def wake_up() -> None:
        wake.set()

    control.set_job_sink(enqueue, wake_up)

    async def reply_job(job: ParseJob, text: str) -> None:
        if not job.reply_chat_id:
            return
        await control.send_to(
            job.reply_chat_id,
            text,
            reply_to_message_id=job.reply_to_message_id,
        )

    def settle_message(job: ParseJob) -> None:
        if job.msg_fp:
            state.mark_settled(job.msg_fp)
            receiver.mark_inflight_done(job.msg_fp)

    async def ensure_disk() -> bool:
        """磁盘低于阈值时停在这里等待，避免把磁盘写满。"""
        if settings.min_free_gb <= 0:
            return True
        warned = False
        while True:
            free = sysinfo.free_gb(settings.output_dir)
            if free is None or free >= settings.min_free_gb:
                return True
            if not warned:
                store.bump_stat("low_disk")
                await control.alert_low_disk(free)
                warned = True
            await asyncio.sleep(60)

    async def process_job(job: ParseJob) -> None:
        nonlocal last_parse_ts
        fp = fingerprint(job.url)
        if state.is_processed(fp) or state.is_gave_up(fp):
            if job.failure_id and state.is_processed(fp):
                store.set_failure_status(job.failure_id, "resolved")
            settle_message(job)
            return

        vid = video_id_from_url(job.url)
        if vid and state.is_video_processed(vid):
            state.clear_failures(fp)
            if job.failure_id:
                store.set_failure_status(job.failure_id, "resolved")
            settle_message(job)
            return

        await ensure_disk()
        wait = settings.parse_min_interval - (time.monotonic() - last_parse_ts)
        if wait > 0:
            await asyncio.sleep(wait)

        if job.retry:
            store.bump_stat("retry")
            if job.failure_id:
                store.set_failure_status(job.failure_id, "retrying")

        runtime.parse_inflight += 1
        push_result: Any = None

        async def push_cb(result: Any, meta: dict[str, Any] | None = None) -> bool:
            nonlocal push_result
            push_result = await pusher.push(result.parse_result, result.processed_list, meta=meta)
            return bool(getattr(push_result, "ok", False))

        logger.info(f"开始解析（来源: {job.source}）: {job.url}")
        try:
            outcome = await parse_url(
                job.url,
                output_dir=settings.output_dir,
                save_metadata=settings.save_metadata,
                skip_media_processing=settings.skip_media_processing,
                reporter=reporter,
                on_files_ready=push_cb if pusher.enabled else None,
                is_duplicate=(
                    (lambda h: state.is_content_processed(h) or store.archive_by_hash(h) is not None)
                    if settings.content_hash
                    else None
                ),
                fetch_meta=pusher.enabled and settings.normalized_bgm_mode() != "off",
                meta_on_video=bool(settings.bgm_on_video),
            )
        except Exception as e:
            logger.exception(f"解析过程异常: {job.url} —— {e}")
            outcome = ParseOutcome(
                url=job.url,
                raw_url=job.url,
                ok=False,
                error=f"解析过程异常: {e}",
            )
        finally:
            last_parse_ts = time.monotonic()
            runtime.parse_inflight = max(0, runtime.parse_inflight - 1)

        archive = outcome.archive
        push_failed = bool(outcome.ok and pusher.enabled and outcome.push_ok is False)

        # 内容级重复：归档目录已经建好，但内容此前推过。删掉新副本，不重复推送。
        if outcome.ok and outcome.duplicate:
            if archive is not None:
                _remove_cache_dir(settings.output_dir, archive.dir)
            state.mark_processed(fp, job.url, str(archive.dir) if archive else None)
            if outcome.raw_url and outcome.raw_url != job.url:
                state.mark_processed(fingerprint(outcome.raw_url), outcome.raw_url, str(archive.dir) if archive else None)
            for video_url in {outcome.raw_url or job.url, job.url}:
                v = video_id_from_url(video_url)
                if v:
                    state.mark_video_processed(v, video_url, str(archive.dir) if archive else None)
            state.clear_failures(fp)
            store.resolve_failures(fp)
            if job.failure_id:
                store.set_failure_status(job.failure_id, "resolved")
            settle_message(job)
            store.bump_stat("duplicate")
            runtime.last_success_at = time.time()
            logger.info(f"检测到重复内容（文件哈希已存在），跳过推送: {job.url}")
            await reply_job(job, f"♻️ 内容与已归档作品重复，已跳过：\n{job.url}")
            state.save()
            return

        if outcome.ok and not push_failed:
            # 回填 BGM/媒体类型（推送成功后，pusher 返回的 music 含 Telegram 消息位置）
            if archive is not None and settings.save_metadata:
                meta = outcome.meta or {}
                music = getattr(push_result, "music", None) or meta.get("music")
                if music:
                    update_metadata(archive.dir, music=music, media=meta.get("media"))
            archive_dir = str(archive.dir) if archive else ""
            state.mark_processed(fp, job.url, archive_dir)
            if outcome.raw_url and outcome.raw_url != job.url:
                state.mark_processed(fingerprint(outcome.raw_url), outcome.raw_url, archive_dir)
            for video_url in {outcome.raw_url or job.url, job.url}:
                v = video_id_from_url(video_url)
                if v:
                    state.mark_video_processed(v, video_url, archive_dir)
            if settings.content_hash and archive is not None and archive.content_hash:
                state.mark_content_processed(archive.content_hash, job.url, archive_dir)
            state.clear_failures(fp)
            store.resolve_failures(fp)
            if job.failure_id:
                store.set_failure_status(job.failure_id, "resolved")
            settle_message(job)

            archive_id = 0
            if archive is not None:
                archive_id = store.record_archive(
                    video_id=archive.video_id,
                    content_hash=archive.content_hash,
                    title=archive.title,
                    author=archive.author,
                    source_url=job.url,
                    raw_url=outcome.raw_url,
                    output_dir=archive_dir,
                    size=archive.size,
                    file_count=len(archive.files),
                    live_count=archive.live_count,
                    tg_chat_id=getattr(push_result, "chat_id", None),
                    tg_message_id=getattr(push_result, "message_id", None),
                    tg_link=getattr(push_result, "link", "") or "",
                )
            store.bump_stat("success")
            if outcome.push_ok:
                store.bump_stat("push_ok")
            runtime.last_success_at = time.time()
            if outcome.push_ok:
                runtime.last_push_at = time.time()
            logger.success(
                f"解析完成 ✅ 已归档: {archive_dir or '-'} "
                f"（{len(archive.files) if archive else 0} 个文件，DB #{archive_id}）"
                + ("，已推送至 Telegram" if outcome.push_ok else "")
            )
            if archive is not None and settings.cleanup_after_push and outcome.push_ok:
                if _remove_cache_dir(settings.output_dir, archive.dir):
                    logger.info(f"🧹 推送成功，已删除本地缓存: {archive.dir.name}")
            link = getattr(push_result, "link", "") if push_result else ""
            await reply_job(
                job,
                f"✅ 解析完成\n{(outcome.title or '')[:120]}\n"
                f"{outcome.raw_url or job.url}\n"
                + (f"频道消息：{link}" if link else ""),
            )
            state.save()
            return

        # 失败：解析失败，或解析成功但推送失败
        error = (
            "Telegram 推送失败（已归档，稍后重试）"
            if push_failed
            else (outcome.error or "未知错误")
        )
        if push_failed:
            store.bump_stat("push_fail")
        kind = classify_failure(error)
        limit = 1 if kind.permanent else max(1, min(int(kind.max_attempts), int(settings.max_parse_attempts) or 3))
        attempts = state.record_failure(
            fp,
            error,
            category=kind.code,
            url=job.url,
            max_attempts=limit,
            backoff_base=settings.retry_backoff_base,
            backoff_max=settings.retry_backoff_max,
        )
        rec = store.record_failure(
            fp=fp,
            url=job.url,
            raw_url=outcome.raw_url or "",
            error=error,
            category=kind.code,
            attempts=attempts,
        )
        job.failure_id = int(rec.get("id") or 0) or job.failure_id
        runtime.last_failure_at = time.time()
        runtime.last_error = short_error(error, 300)

        if attempts >= limit:
            state.mark_gave_up(fp, job.url, error, category=kind.code)
            settle_message(job)
            if job.failure_id:
                store.set_failure_status(job.failure_id, "gave_up")
            store.bump_stat("failure")
            logger.error(f"已失败 {attempts} 次，放弃: {job.url} —— {error}")
            if job.failure_id:
                await control.notify_failure(rec)
            await reply_job(
                job,
                f"❌ 解析失败（{kind.label}，已放弃）\n{job.url}\n{short_error(error, 200)}",
            )
            if not push_failed:
                await fallback.submit(job.url, error)
        else:
            # 临时候选失败：释放消息 inflight，退避期由 state.failure_due 控制，
            # 到期后 due_failures / 原消息都会重新触发，不会永久卡住。
            if job.msg_fp:
                receiver.mark_inflight_done(job.msg_fp)
            info = state.get_failure(fp)
            remain = max(0, int(info.get("retry_at", 0) or 0) - int(time.time()))
            logger.warning(
                f"处理失败（第 {attempts}/{limit} 次，约 {remain}s 后自动重试）: "
                f"{job.url} —— {error}"
            )
            await reply_job(
                job,
                f"⚠️ 解析失败（第 {attempts}/{limit} 次），约 {remain}s 后自动重试。\n"
                f"{job.url}\n{short_error(error, 200)}",
            )
        state.save()

    async def worker() -> None:
        while True:
            job = await queue.get()
            fp = fingerprint(job.url)
            runtime.queue_size = queue.qsize()
            try:
                await process_job(job)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"解析任务异常: {job.url} —— {e}")
                runtime.last_error = short_error(str(e), 300)
                # 未走正常失败分支（例如 DB/控制台异常）时也要释放消息 inflight，
                # 否则该消息在本进程内会永久卡住。
                if job.msg_fp:
                    receiver.mark_inflight_done(job.msg_fp)
            finally:
                pending.discard(fp)
                runtime.queue_size = queue.qsize()
                queue.task_done()

    async def poll_cycle() -> None:
        nonlocal last_sweep
        if state.get_flag("paused"):
            return
        if settings.min_free_gb > 0:
            free = sysinfo.free_gb(settings.output_dir)
            if free is not None and free < settings.min_free_gb:
                await control.alert_low_disk(free)
                return
        try:
            candidates = await receiver.poll_once()
            runtime.consecutive_poll_errors = 0
        except Exception as e:
            runtime.consecutive_poll_errors += 1
            runtime.last_error = short_error(str(e), 300)
            logger.exception(f"轮询异常（本轮跳过）: {e}")
            candidates = []
        runtime.last_poll_at = time.time()
        runtime.last_poll_candidates = len(candidates)
        runtime.consecutive_empty = 0 if candidates else runtime.consecutive_empty + 1
        if candidates:
            runtime.last_candidate_at = time.time()
        for cand in candidates:
            fp = fingerprint(cand.url)
            if state.is_processed(fp) or state.is_gave_up(fp):
                logger.debug(f"已处理/已放弃，跳过: {cand.url}")
                state.mark_settled(cand.msg_fp)
                receiver.mark_inflight_done(cand.msg_fp)
                continue
            vid = video_id_from_url(cand.url)
            if vid and state.is_video_processed(vid):
                logger.info(f"检测到重复作品（视频 ID {vid} 已处理过），跳过: {cand.url}")
                state.mark_settled(cand.msg_fp)
                receiver.mark_inflight_done(cand.msg_fp)
                continue
            if not state.failure_due(fp):
                # 退避期内不要占着消息的 inflight 标记，否则到期后也无法重新提取
                receiver.mark_inflight_done(cand.msg_fp)
                continue
            if fp in pending:
                # 已有同链接任务在队列/解析中：释放本条消息的 inflight，
                # 等该任务结束后由下一轮轮询补一次 settle。
                receiver.mark_inflight_done(cand.msg_fp)
                continue
            job = ParseJob(url=cand.url, source=cand.source, msg_fp=cand.msg_fp)
            if enqueue(job):
                runtime.last_candidate_at = time.time()
                logger.info(f"已加入解析队列（来源: {cand.source}）: {cand.url}")
            else:
                receiver.mark_inflight_done(cand.msg_fp)

        # 已到退避时间的失败自动重新入队（手动 /retry 不受退避限制）
        for fp, url, _count in state.due_failures(10):
            if not url or fp in pending:
                continue
            if state.is_processed(fp) or state.is_gave_up(fp):
                continue
            vid = video_id_from_url(url)
            if vid and state.is_video_processed(vid):
                state.clear_failures(fp)
                continue
            enqueue(ParseJob(url=url, source="retry", retry=True))

        now = time.monotonic()
        if settings.cache_ttl_hours > 0 and now - last_sweep >= CACHE_SWEEP_INTERVAL:
            last_sweep = now
            try:
                sweep_cache(settings)
            except Exception as e:
                logger.debug(f"缓存清理异常（忽略）: {e}")

    async def maintenance() -> None:
        nonlocal last_heartbeat
        now = time.monotonic()
        if settings.healthcheck_url and now - last_heartbeat >= max(30, settings.healthcheck_interval):
            last_heartbeat = now
            await control.heartbeat()
        await control.maybe_alert_silent()
        await control.maybe_send_summary()

    async def watchdog() -> None:
        while True:
            await asyncio.sleep(max(30, settings.browser_check_interval))
            if state.get_flag("paused"):
                continue
            try:
                rss = sysinfo.chrome_rss_mb()
                age = time.time() - float(receiver._browser_started_at or 0)
                reason = ""
                if not await receiver.check_healthy():
                    reason = "页面无响应"
                elif settings.browser_max_rss_mb > 0 and rss and rss > settings.browser_max_rss_mb:
                    reason = f"Chromium 内存 {rss:.0f}MB 超限"
                elif settings.browser_restart_interval > 0 and age > settings.browser_restart_interval:
                    idle_for = time.time() - max(runtime.last_candidate_at, runtime.started_at)
                    if runtime.queue_size == 0 and idle_for >= 1800:
                        reason = f"浏览器已运行 {age / 3600:.1f}h（空闲 {idle_for / 60:.0f} 分钟）"
                if reason:
                    await receiver.restart_browser(reason)
                    runtime.browser_restarts += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"浏览器看门狗异常（忽略）: {e}")

    await receiver.start()
    try:
        await pusher.start()
    except Exception as e:
        if settings.tg_push:
            raise
        logger.warning(f"Telegram 客户端启动失败（控制台/通知不可用）: {e}")
    try:
        await fallback.start()
    except Exception as e:
        logger.warning(f"失败链接兜底服务启动异常（忽略）: {e}")
    if control.console_enabled and pusher.client_ready and pusher.client:
        try:
            control.register(pusher.client)
        except Exception as e:
            logger.warning(f"Bot 控制台注册失败（忽略）: {e}")
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

    worker_task = asyncio.create_task(worker(), name="parse-worker")
    watchdog_task = asyncio.create_task(watchdog(), name="browser-watchdog")
    logger.info(
        f"解析队列已启动；Owner 控制台: {'已启用' if control.console_enabled else '未启用'}"
        f"；失败通知: {'开' if settings.failure_notify else '关'}"
    )
    if once:
        logger.info("启动完成（--once：只执行一轮轮询并处理队列）")

    try:
        while True:
            await poll_cycle()
            await maintenance()
            if once:
                await queue.join()
                logger.info("--once 模式：单轮结束，退出")
                return
            if receiver.consume_force_poll():
                logger.info("控制台触发：立即执行下一轮检查")
                await asyncio.sleep(1.0)
                continue
            wait = max(1.0, receiver.poll_interval())
            if runtime.last_poll_candidates:
                wait = min(wait, 10.0)
            try:
                await asyncio.wait_for(wake.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
            finally:
                wake.clear()
    finally:
        for task in (worker_task, watchdog_task):
            task.cancel()
        await asyncio.gather(worker_task, watchdog_task, return_exceptions=True)
        await fallback.stop()
        await pusher.stop()
        await receiver.close()
        store.close()
        state.save()
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
