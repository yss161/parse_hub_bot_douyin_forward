# -*- coding: utf-8 -*-
"""去重状态存储（JSON 文件，原子写入）。

记录两类指纹：
  - processed      ：已经解析/归档过的视频（输入链接与展开后的原始链接各记一次），
                     防止同一条视频被重复解析；
  - seen_messages  ：私信里已经识别过的消息（DOM 指纹），避免每轮重复点击同一张视频卡片。

文件损坏时自动重命名为 *.corrupt 并从空状态开始，不影响服务启动。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path


def fingerprint(text: str, length: int = 20) -> str:
    """稳定指纹（跨进程一致，区别于 Python 内置 hash）。"""
    return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()[:length]


class StateStore:
    def __init__(self, path: Path, max_entries: int = 5000) -> None:
        self.path = Path(path)
        self.max_entries = max(100, max_entries)
        self._data: dict[str, dict] = {"processed": {}, "seen_messages": {}, "flags": {}}
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
        for key in ("processed", "seen_messages", "failures", "gave_up"):
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

    # ────────────────────────── seen_messages ──────────────────────────

    def is_seen_message(self, fp: str) -> bool:
        return fp in self._data["seen_messages"]

    def mark_seen_message(self, fp: str) -> None:
        self._data["seen_messages"][fp] = int(time.time())

    def unmark_seen_message(self, fp: str) -> None:
        """撤销"已识别"标记（解析失败时调用，让下一轮重新提取并重试）。"""
        self._data["seen_messages"].pop(fp, None)

    # ────────────────────────── 失败计数 / 放弃 ──────────────────────────

    def record_failure(self, fp: str, error: str) -> int:
        """记录一次解析失败，返回累计失败次数。"""
        bucket = self._data.setdefault("failures", {})
        item = bucket.setdefault(fp, {"count": 0, "last_error": "", "at": 0})
        item["count"] += 1
        item["last_error"] = error[:300]
        item["at"] = int(time.time())
        return int(item["count"])

    def clear_failures(self, fp: str) -> None:
        self._data.setdefault("failures", {}).pop(fp, None)

    def mark_gave_up(self, fp: str, url: str, reason: str) -> None:
        self._data.setdefault("gave_up", {})[fp] = {
            "url": url,
            "reason": reason[:300],
            "at": int(time.time()),
        }

    def is_gave_up(self, fp: str) -> bool:
        return fp in self._data.get("gave_up", {})

    # ────────────────────────── flags ──────────────────────────

    def get_flag(self, name: str) -> bool:
        return bool(self._data.get("flags", {}).get(name))

    def set_flag(self, name: str, value: bool = True) -> None:
        self._data.setdefault("flags", {})[name] = bool(value)
