# -*- coding: utf-8 -*-
"""把兜底 bot 输出的普通视频转换成 Telegram Live Photo。

Live Photo 必须成对：静态图 + 短视频。这里用 OpenCV 读取视频第一帧作为静态图，
原视频作为实况视频，并复用 forwarder/live_photo.py 的 send_live_photo。

限制（Telegram Bot API）：
  - live_photo 视频 ≤ 10 秒、≤ 10MB；
  - 静态图/视频都必须存在，且视频编码需 Telegram 可识别（H.264/MP4 最稳）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from log import logger

logger = logger.bind(name="Forwarder/VideoLive")


@dataclass
class LivePair:
    photo: Path
    video: Path
    duration: float = 0.0
    width: int = 0
    height: int = 0


def _video_duration(video: Path) -> float:
    try:
        from parsehub.utils.media_info import MediaInfoReader

        return float(MediaInfoReader.read(video).duration or 0)
    except Exception:
        try:
            import cv2

            cap = cv2.VideoCapture(str(video))
            try:
                fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
                frames = float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                return frames / fps if fps > 0 else 0.0
            finally:
                cap.release()
        except Exception:
            return 0.0


def convert_video_to_live(
    video: Path,
    dest_dir: Path,
    *,
    max_seconds: int = 10,
    max_mb: int = 10,
    frame: str = "first",
) -> LivePair | None:
    """把视频转成 (静态图, 视频) 对；不满足限制返回 None（调用方回退普通视频）。

    只读取第一帧，不转码视频；视频本身必须已是 Telegram 支持的容器/编码。
    """
    video = Path(video)
    if not video.is_file():
        return None
    try:
        size = video.stat().st_size
    except OSError:
        return None
    if size > max(1, int(max_mb)) * 1024 * 1024:
        logger.info(f"兜底视频超过 {max_mb}MB（{size / 1048576:.1f}MB），不转 Live Photo")
        return None

    duration = _video_duration(video)
    if duration and duration > max(1, int(max_seconds)):
        logger.info(f"兜底视频时长 {duration:.1f}s 超过 {max_seconds}s，不转 Live Photo")
        return None

    try:
        import cv2
    except Exception as e:
        logger.warning(f"未安装 OpenCV，无法抽帧转 Live Photo: {e}")
        return None

    dest_dir.mkdir(parents=True, exist_ok=True)
    photo = dest_dir / f"{video.stem}_live.jpg"
    cap = cv2.VideoCapture(str(video))
    try:
        if not cap.isOpened():
            logger.warning(f"OpenCV 无法打开视频: {video.name}")
            return None
        # frame 参数预留：目前只支持首帧（first），后续可扩展 middle/last
        ok, image = cap.read()
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        if not ok or image is None:
            logger.warning(f"OpenCV 抽帧失败: {video.name}")
            return None
        if not cv2.imwrite(str(photo), image, [int(cv2.IMWRITE_JPEG_QUALITY), 95]):
            logger.warning(f"OpenCV 写入静态图失败: {photo}")
            return None
    finally:
        cap.release()

    if not photo.is_file() or photo.stat().st_size <= 0:
        return None
    return LivePair(photo=photo, video=video, duration=duration, width=width, height=height)
