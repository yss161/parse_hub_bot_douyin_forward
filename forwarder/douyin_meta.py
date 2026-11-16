# -*- coding: utf-8 -*-
"""抖音作品附加元数据：背景音乐（BGM）与媒体类型。

parsehub 的 ParseResult 只保留图片/视频引用，不暴露 aweme_detail.music；
这里在转发层补一次移动端详情请求，供“BGM 评论区归档”和兜底判断使用。

设计原则：
  - 任何失败都只记日志，不影响主解析/推送；
  - 只读取公开字段，不修改 site-packages；
  - 下载后的音频只作为临时文件，上传 Telegram 后由调用方删除。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from log import logger

logger = logger.bind(name="Forwarder/DouyinMeta")

_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
_UNSAFE_RE = re.compile(r"[^0-9A-Za-z._-]+")


def _first_url(obj: Any) -> str:
    if isinstance(obj, dict):
        urls = obj.get("url_list") or []
        if isinstance(urls, (list, tuple)):
            for u in urls:
                if isinstance(u, str) and u.startswith("http"):
                    return u
    return ""


def extract_music(music: dict[str, Any] | None) -> dict[str, Any] | None:
    """从 aweme_detail.music 提取可下载的 BGM 信息。"""
    if not isinstance(music, dict):
        return None
    url = _first_url(music.get("play_url"))
    if not url:
        return None
    cover = _first_url(music.get("cover_large")) or _first_url(music.get("cover_medium"))
    return {
        "title": str(music.get("title") or "").strip(),
        "author": str(music.get("author") or "").strip(),
        "duration": int(music.get("duration") or 0),
        "music_id": str(music.get("id_str") or music.get("mid") or music.get("id") or ""),
        "url": url,
        "cover_url": cover,
        "prevent_download": bool(music.get("prevent_download")),
    }


def _classify_media(detail: dict[str, Any]) -> dict[str, Any]:
    images = detail.get("images") or []
    if not images:
        image_post = detail.get("image_post_info") or {}
        images = image_post.get("images") or []
    if not isinstance(images, list):
        images = []

    live_count = 0
    image_count = 0
    for image in images:
        if not isinstance(image, dict):
            continue
        if (
            image.get("video") is not None
            or image.get("video_play_addr") is not None
            or image.get("video_url")
        ):
            live_count += 1
        else:
            image_count += 1

    if images:
        kind = "live" if live_count else "image"
    elif detail.get("video"):
        kind = "video"
    else:
        kind = "unknown"

    return {
        "kind": kind,
        "aweme_type": detail.get("aweme_type"),
        "image_count": image_count,
        "live_count": live_count,
        "video_count": 1 if kind == "video" else 0,
        "is_image_post": bool(images),
    }


async def fetch_douyin_meta(url: str) -> dict[str, Any] | None:
    """获取作品的 BGM 与媒体类型；失败返回 None，不抛异常。"""
    try:
        from parsehub.provider_api.douyin import DouyinMobileCrawler

        data = await DouyinMobileCrawler().parse(url)
        detail = (data or {}).get("aweme_detail") or data or {}
        if not isinstance(detail, dict):
            return None
        return {
            "music": extract_music(detail.get("music")),
            "media": _classify_media(detail),
            "aweme_id": str(detail.get("aweme_id") or ""),
        }
    except Exception as e:
        logger.warning(f"读取抖音附加信息失败（不影响主流程）: {e} | {url}")
        return None


async def download_music(
    music: dict[str, Any],
    dest_dir: Path,
    *,
    max_mb: int = 20,
    timeout: int = 30,
) -> Path:
    """下载 BGM 到临时目录；超限/失败抛异常（由调用方决定是否回退）。"""
    import httpx

    url = str(music.get("url") or "")
    if not url:
        raise ValueError("BGM 缺少下载地址")

    dest_dir.mkdir(parents=True, exist_ok=True)
    music_id = _UNSAFE_RE.sub("_", str(music.get("music_id") or ""))[:64] or "bgm"
    target = dest_dir / f"{music_id}.mp3"
    limit = max(1, int(max_mb)) * 1024 * 1024
    headers = {"User-Agent": _USER_AGENT, "Referer": "https://www.douyin.com/"}
    written = 0

    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=max(5, int(timeout)), headers=headers
        ) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                length = int(resp.headers.get("Content-Length") or 0)
                if length and length > limit:
                    raise ValueError(f"BGM 超过 {max_mb}MB（{length / 1048576:.1f}MB）")
                with target.open("wb") as fh:
                    async for chunk in resp.aiter_bytes(64 * 1024):
                        written += len(chunk)
                        if written > limit:
                            raise ValueError(f"BGM 超过 {max_mb}MB")
                        fh.write(chunk)
        if written <= 0:
            raise ValueError("BGM 下载为空文件")
        return target
    except Exception:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise
