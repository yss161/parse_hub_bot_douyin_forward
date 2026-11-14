# -*- coding: utf-8 -*-
"""去重状态存储（JSON 文件，原子写入）。

记录三类指纹/数据：
  - processed      ：已经解析/归档过的视频（输入链接、展开后的原始链接、视频ID 各记一次），
                     防止同一条视频被重复解析（重复转发检测的最终防线）；
  - seen_messages  ：私信里已经识别过的消息（DOM 指纹 / 接口指纹），
                     避免每轮重复点击/提取同一条转发；
  - values         ：会话中设置的持久化配置（如通过控制台输入的"监测大号昵称"，
                     免编辑 .env、免重启）。

文件损坏时自动重命名为 *.corrupt 并从空状态开始，不影响服务启动。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path


def fingerprint(text: str | bytes, length: int = 20) -> str:
    """稳定指纹（跨进程一致，区别于 Python 内置 hash）；接受字符串或字节。"""
    data = text.encode("utf-8", errors="ignore") if isinstance(text, str) else bytes(text)
    return hashlib.sha1(data).hexdigest()[:length]


# 抖音链接中的纯数字作品 ID（video/note/slides 路径、modal_id / aweme_id 参数）
_VIDEO_ID_PATTERNS = (
    re.compile(r"/(?:video|note|slides)/(\d{10,})"),
    re.compile(r"(?:modal_id|aweme_id|item_id)=(\d{10,})"),
)


def video_id_from_url(url: str) -> str:
    """从抖音链接中提取纯数字作品 ID（用于跨链接形式的重复检测）。

    取不到时返回空串（如 v.douyin.com 短链——短链需展开后才知道作品 ID）。
    """
    if not url:
        return ""
    for pattern in _VIDEO_ID_PATTERNS:
        m = pattern.search(url)
        if m:
            return m.group(1)
    return ""


class StateStore:
    def __init__(self, path: Path, max_entries: int = 5000) -> None:
        self.path = Path(path)
        self.max_entries = max(100, max_entries)
        self._data: dict[str, dict] = {
            "processed": {},
            "content": {},
            "seen_messages": {},
            "settled": {},
            "flags": {},
            "values": {},
        }
        self._load()

    # ────────────────────────── 持久化 ──────────────────────────

    def _load(self) -> None:
        try:
            if not self.path.exists():
                return
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            for key, value in raw.items():
                if isinstance(value, dict):
                    self._data.setdefault(key, {})
                    self._data[key].update(value)
        except Exception:
            # 状态文件损坏：保留现场（重命名），从空状态继续
            try:
                self.path.replace(self.path.with_name(self.path.name + ".corrupt"))
            except OSError:
                pass

    def save(self) -> None:
        self._prune()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def _prune(self) -> None:
        for key in ("processed", "content", "seen_messages", "settled", "failures", "gave_up"):
            bucket = self._data.get(key)
            if not isinstance(bucket, dict) or len(bucket) <= self.max_entries:
                continue
            for old_key in list(bucket.keys())[: len(bucket) - self.max_entries]:
                bucket.pop(old_key, None)

    # ────────────────────────── processed ──────────────────────────

    def is_processed(self, fp: str) -> bool:
        return fp in self._data["processed"]

    def mark_processed(self, fp: str, url: str, output_dir: str | None = None) -> None:
        self._data["processed"][fp] = {
            "url": url,
            "output": output_dir,
            "at": int(time.time()),
        }

    # ────────────────────────── 作品（视频ID）去重 ──────────────────────────
    # 说明：同一作品可能以不同链接形式（/video/xx、短链、modal_id）重复转发，
    # 视频 ID 是唯一稳定标识；本地缓存被清理后该记录依然保留，保证不会重复下载。

    def is_video_processed(self, video_id: str) -> bool:
        return bool(video_id) and fingerprint("video:" + video_id) in self._data["processed"]

    def mark_video_processed(self, video_id: str, url: str, output_dir: str | None = None) -> None:
        if not video_id:
            return
        self.mark_processed(fingerprint("video:" + video_id), url, output_dir)

    # ────────────────────────── 内容级去重（文件 SHA-256） ──────────────────────────
    # 同一段内容被重新发布成新的 aweme_id 时，视频 ID 去重无效；文件哈希是最后一道防线。
    # 键直接使用 64 位 SHA-256（可读、可人工核对），不另做截断。

    def is_content_processed(self, content_hash: str) -> bool:
        return bool(content_hash) and content_hash in self._data.get("content", {})

    def mark_content_processed(
        self, content_hash: str, url: str = "", output_dir: str | None = None
    ) -> None:
        if not content_hash:
            return
        self._data.setdefault("content", {})[content_hash] = {
            "url": url,
            "output": output_dir,
            "at": int(time.time()),
        }

    # ────────────────────────── values（会话设置的持久化值） ──────────────────────────

    def get_value(self, key: str, default: str = "") -> str:
        value = self._data.get("values", {}).get(key)
        return value if isinstance(value, str) else default

    def set_value(self, key: str, value: str) -> None:
        self._data.setdefault("values", {})[key] = str(value)

    def clear_value(self, key: str) -> None:
        self._data.setdefault("values", {}).pop(key, None)

    # ────────────────────────── seen_messages ──────────────────────────

    def is_seen_message(self, fp: str) -> bool:
        return fp in self._data["seen_messages"]

    def mark_seen_message(self, fp: str) -> None:
        self._data["seen_messages"][fp] = int(time.time())

    def unmark_seen_message(self, fp: str) -> None:
        """撤销"已识别"标记（解析失败时调用，让下一轮重新提取并重试）。"""
        self._data["seen_messages"].pop(fp, None)

    # ────────────────────────── settled（消息已有"定论"） ──────────────────────────
    # 说明：一条消息被识别（seen）后，最终会有三种结局——
    #   成功（processed）/ 失败放弃（gave_up）/ 首轮水位跳过（历史消息）。
    # settled 记录"这条消息已不必再处理"。它与 seen 的区别：
    #   · seen = 已识别过（可能还没处理完，比如解析中进程被杀）；
    #   · settled = 已有定论（不再重试）。
    # 于是：seen 且未 settled 且不在当前进程处理中 → 上次被中断，重启后应恢复处理。

    def is_settled(self, fp: str) -> bool:
        return fp in self._data["settled"]

    def mark_settled(self, fp: str) -> None:
        self._data["settled"][fp] = int(time.time())

    def unmark_settled(self, fp: str) -> None:
        self._data["settled"].pop(fp, None)

    # ────────────────────────── 失败计数 / 放弃 ──────────────────────────

    def record_failure(
        self,
        fp: str,
        error: str,
        *,
        category: str = "",
        url: str = "",
        max_attempts: int = 3,
        backoff_base: int = 60,
        backoff_max: int = 1800,
    ) -> int:
        """记录一次解析失败，返回累计失败次数。

        同时写入下一次可重试时间（指数退避），避免每轮轮询都立刻重试。
        手动重试（owner 按钮）会绕过该时间。
        """
        bucket = self._data.setdefault("failures", {})
        item = bucket.setdefault(
            fp,
            {"count": 0, "last_error": "", "category": "", "url": "", "at": 0, "retry_at": 0},
        )
        item["count"] = int(item.get("count", 0)) + 1
        if url:
            item["url"] = url
        item["last_error"] = error[:300]
        item["category"] = category
        item["at"] = int(time.time())
        count = int(item["count"])
        if count < max(1, max_attempts):
            delay = min(max(1, backoff_max), max(1, backoff_base) * (2 ** (count - 1)))
            item["retry_at"] = int(time.time()) + delay
        else:
            item["retry_at"] = 0
        return count

    def failure_due(self, fp: str) -> bool:
        """是否已过退避时间（没有失败记录时返回 True）。"""
        item = self._data.get("failures", {}).get(fp)
        if not isinstance(item, dict):
            return True
        return int(item.get("retry_at", 0) or 0) <= int(time.time())

    def get_failure(self, fp: str) -> dict:
        item = self._data.get("failures", {}).get(fp)
        return dict(item) if isinstance(item, dict) else {}

    def due_failures(self, limit: int = 20) -> list[tuple[str, str, int]]:
        """返回已到退避时间、尚未放弃的失败 (fp, url, count)。"""
        now = int(time.time())
        out: list[tuple[str, str, int]] = []
        for fp, item in self._data.get("failures", {}).items():
            if fp in self._data.get("gave_up", {}):
                continue
            if not isinstance(item, dict):
                continue
            retry_at = int(item.get("retry_at", 0) or 0)
            if retry_at and retry_at <= now:
                out.append((fp, str(item.get("url") or ""), int(item.get("count", 0) or 0)))
                if len(out) >= max(1, int(limit)):
                    break
        return out

    def clear_failures(self, fp: str) -> None:
        self._data.setdefault("failures", {}).pop(fp, None)

    def mark_gave_up(self, fp: str, url: str, reason: str, category: str = "") -> None:
        self._data.setdefault("gave_up", {})[fp] = {
            "url": url,
            "reason": reason[:300],
            "category": category,
            "at": int(time.time()),
        }

    def is_gave_up(self, fp: str) -> bool:
        return fp in self._data.get("gave_up", {})

    def get_gave_up(self, fp: str) -> dict:
        item = self._data.get("gave_up", {}).get(fp)
        return dict(item) if isinstance(item, dict) else {}

    def clear_gave_up(self, fp: str) -> None:
        self._data.get("gave_up", {}).pop(fp, None)

    # ────────────────────────── flags ──────────────────────────

    def get_flag(self, name: str) -> bool:
        return bool(self._data.get("flags", {}).get(name))

    def set_flag(self, name: str, value: bool = True) -> None:
        self._data.setdefault("flags", {})[name] = bool(value)
