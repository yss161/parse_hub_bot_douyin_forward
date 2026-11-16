# -*- coding: utf-8 -*-
"""新功能离线测试：BGM 元数据、视频转 Live Photo、兜底消息识别、归档元数据更新。

运行：python -m unittest tests.test_features -v
不依赖 Telegram / parsehub；OpenCV 仅用于生成/读取测试视频（未安装时跳过该用例）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_log_stub() -> None:
    if "log" in sys.modules:
        return
    module = types.ModuleType("log")

    class _Logger:
        def bind(self, **_: object) -> "_Logger":
            return self

        def debug(self, *_: object, **__: object) -> None:
            pass

        def info(self, *_: object, **__: object) -> None:
            pass

        def warning(self, *_: object, **__: object) -> None:
            pass

        def error(self, *_: object, **__: object) -> None:
            pass

        def success(self, *_: object, **__: object) -> None:
            pass

        def exception(self, *_: object, **__: object) -> None:
            pass

        def opt(self, **_: object) -> "_Logger":
            return self

    module.logger = _Logger()
    sys.modules["log"] = module


_install_log_stub()

from forwarder.archive import update_metadata
from forwarder.douyin_meta import _classify_media, extract_music
from forwarder.fallback import FallbackService, PendingLink
from forwarder.tg_push import PushResult, TgPusher
from forwarder.video_live import convert_video_to_live


class DouyinMetaTests(unittest.TestCase):
    def test_extract_music(self) -> None:
        music = extract_music(
            {
                "title": " @阿迟迟创作的原声 ",
                "author": "阿迟迟",
                "duration": 16,
                "id_str": "7527305391303084842",
                "play_url": {"url_list": ["https://example.com/a.mp3"]},
                "cover_large": {"url_list": ["https://example.com/c.jpg"]},
                "prevent_download": False,
            }
        )
        assert music is not None
        self.assertEqual(music["title"], "@阿迟迟创作的原声")
        self.assertEqual(music["author"], "阿迟迟")
        self.assertEqual(music["duration"], 16)
        self.assertEqual(music["music_id"], "7527305391303084842")
        self.assertEqual(music["cover_url"], "https://example.com/c.jpg")
        self.assertFalse(music["prevent_download"])

    def test_extract_music_without_play_url(self) -> None:
        self.assertIsNone(extract_music({"title": "x"}))
        self.assertIsNone(extract_music(None))

    def test_classify_media(self) -> None:
        live = _classify_media(
            {"images": [{"video": {}}, {"url_list": ["https://example.com/1.jpg"]}]}
        )
        self.assertEqual(live["kind"], "live")
        self.assertEqual(live["live_count"], 1)
        self.assertEqual(live["image_count"], 1)

        image = _classify_media({"images": [{"url_list": ["https://example.com/1.jpg"]}]})
        self.assertEqual(image["kind"], "image")

        video = _classify_media({"video": {"play_addr": {}}})
        self.assertEqual(video["kind"], "video")

        image_post = _classify_media(
            {"image_post_info": {"images": [{"video": {}}, {"video": {}}]}}
        )
        self.assertEqual(image_post["kind"], "live")
        self.assertEqual(image_post["live_count"], 2)


class ArchiveMetadataTests(unittest.TestCase):
    def test_update_metadata_merges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / "metadata.json").write_text(
                json.dumps({"title": "旧标题", "media": [{"type": "image"}]}),
                encoding="utf-8",
            )
            self.assertTrue(
                update_metadata(
                    target,
                    music={"title": "BGM", "telegram": {"message_id": 9}},
                    media={"kind": "live"},
                )
            )
            data = json.loads((target / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(data["title"], "旧标题")
            self.assertEqual(data["media"], {"kind": "live"})
            self.assertEqual(data["music"]["telegram"]["message_id"], 9)

    def test_update_metadata_creates_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.assertTrue(update_metadata(target, music={"title": "BGM"}))
            data = json.loads((target / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(data["music"]["title"], "BGM")


class VideoLiveTests(unittest.TestCase):
    def test_convert_video_to_live(self) -> None:
        try:
            import cv2
            import numpy as np
        except Exception as e:  # pragma: no cover
            self.skipTest(f"OpenCV/numpy 不可用: {e}")
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "sample.mp4"
            writer = cv2.VideoWriter(
                str(source), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (64, 64)
            )
            self.assertTrue(writer.isOpened())
            for index in range(10):
                frame = np.full((64, 64, 3), index * 20, dtype=np.uint8)
                writer.write(frame)
            writer.release()

            pair = convert_video_to_live(source, Path(tmp) / "out", max_seconds=10, max_mb=10)
            assert pair is not None
            self.assertTrue(pair.photo.is_file())
            self.assertEqual(pair.video, source)
            self.assertGreater(pair.photo.stat().st_size, 0)
            self.assertGreater(pair.width, 0)
            self.assertGreater(pair.height, 0)

    def test_reject_too_large(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "sample.mp4"
            source.write_bytes(b"x" * 2048)
            self.assertIsNone(
                convert_video_to_live(source, Path(tmp) / "out", max_mb=0)
            )


class _FakeSettings:
    bgm_mode = "comment"
    bgm_on_video = False

    def normalized_bgm_mode(self) -> str:
        return self.bgm_mode


class BgmDecisionTests(unittest.TestCase):
    def _pusher(self, mode: str = "comment", on_video: bool = False) -> TgPusher:
        pusher = TgPusher.__new__(TgPusher)
        settings = _FakeSettings()
        settings.bgm_mode = mode
        settings.bgm_on_video = on_video
        pusher.settings = settings
        pusher._bgm_attempted = False
        pusher._bgm_music = None
        pusher._bgm_message = None
        return pusher

    def test_caption(self) -> None:
        self.assertEqual(TgPusher._bgm_caption({"title": "歌", "author": "人"}), "BGM：歌 - 人")
        self.assertEqual(TgPusher._bgm_caption({"title": "歌"}), "BGM：歌")
        self.assertEqual(TgPusher._bgm_caption({"author": "人"}), "BGM：人")
        self.assertEqual(TgPusher._bgm_caption({}), "BGM")

    def test_image_and_live_send(self) -> None:
        pusher = self._pusher()
        meta = {"music": {"url": "https://example.com/a.mp3"}, "media": {"kind": "image"}}
        self.assertTrue(pusher._should_send_bgm(None, [], meta))
        meta["media"]["kind"] = "live"
        self.assertTrue(pusher._should_send_bgm(None, [], meta))

    def test_video_default_skip(self) -> None:
        pusher = self._pusher()
        meta = {"music": {"url": "https://example.com/a.mp3"}, "media": {"kind": "video"}}
        self.assertFalse(pusher._should_send_bgm(None, [], meta))
        pusher = self._pusher(on_video=True)
        self.assertTrue(pusher._should_send_bgm(None, [], meta))

    def test_disabled_and_missing_music(self) -> None:
        meta = {"music": {"url": "https://example.com/a.mp3"}, "media": {"kind": "image"}}
        self.assertFalse(self._pusher(mode="off")._should_send_bgm(None, [], meta))
        self.assertFalse(self._pusher()._should_send_bgm(None, [], {}))
        self.assertFalse(self._pusher()._should_send_bgm(None, [], None))

    def test_push_result_defaults(self) -> None:
        result = PushResult()
        self.assertIsNone(result.music)
        self.assertFalse(result.bgm_sent)
        self.assertIsNone(result.bgm_message_id)
        self.assertEqual(result.bgm_link, "")


class _FakeUser:
    def __init__(self, uid: int, username: str = "") -> None:
        self.id = uid
        self.username = username


class _FakeMessage:
    def __init__(
        self,
        uid: int | None,
        *,
        outgoing: bool = False,
        mid: int = 1,
        reply_to_message_id: int = 0,
        username: str = "",
    ) -> None:
        self.outgoing = outgoing
        self.id = mid
        self.reply_to_message_id = reply_to_message_id
        self.from_user = _FakeUser(uid, username) if uid is not None else None
        self.chat = types.SimpleNamespace(id=-100123)


class DiscussionWaiterTests(unittest.TestCase):
    def test_concurrent_waiters_resolve(self) -> None:
        import asyncio

        pusher = TgPusher.__new__(TgPusher)
        pusher._discussion_group_id = -100999
        pusher._channel_id = -100123
        pusher._forward_cache = {}
        pusher._forward_waiters = []

        async def run() -> tuple[int | None, int | None]:
            first = asyncio.create_task(pusher._wait_discussion_root([1], timeout=2))
            second = asyncio.create_task(pusher._wait_discussion_root([2], timeout=2))
            await asyncio.sleep(0)
            for post_id, message_id in ((1, 101), (2, 102)):
                origin = types.SimpleNamespace(
                    chat=types.SimpleNamespace(id=-100123), message_id=post_id
                )
                message = types.SimpleNamespace(
                    id=message_id,
                    forward_origin=origin,
                    forward_from_chat=None,
                    forward_from_message_id=0,
                )
                pusher._capture_discussion_forward(message)
            return await first, await second

        self.assertEqual(asyncio.run(run()), (101, 102))


class FallbackTests(unittest.TestCase):
    def _service(self) -> FallbackService:
        service = FallbackService.__new__(FallbackService)
        service._self_id = 100
        service._bot_id = 200
        service._bot_username = "ParseHubot"
        service._is_channel = False
        service._pending = {}
        service._seen = set()
        return service

    def test_sender_detection(self) -> None:
        service = self._service()
        self.assertTrue(service._is_fallback_sender(_FakeMessage(200)))
        self.assertTrue(service._is_fallback_sender(_FakeMessage(0, username="parsehubot")))
        self.assertFalse(service._is_fallback_sender(_FakeMessage(100)))
        self.assertFalse(service._is_fallback_sender(_FakeMessage(200, outgoing=True)))
        self.assertFalse(service._is_fallback_sender(_FakeMessage(300)))

        service._is_channel = True
        self.assertTrue(service._is_fallback_sender(_FakeMessage(None)))

    def test_find_pending_by_reply_and_latest(self) -> None:
        service = self._service()
        first = PendingLink(url="https://a", link_message_id=11, created_at=1.0)
        second = PendingLink(url="https://b", link_message_id=22, created_at=2.0)
        service._pending = {11: first, 22: second}
        self.assertIs(service._find_pending(_FakeMessage(200, reply_to_message_id=11)), first)
        self.assertIs(service._find_pending(_FakeMessage(200)), second)

    def test_finished_pending_ignored(self) -> None:
        service = self._service()
        pending = PendingLink(url="https://a", link_message_id=11, finished=True)
        service._pending = {11: pending}
        self.assertIsNone(service._find_pending(_FakeMessage(200)))


if __name__ == "__main__":
    unittest.main()
