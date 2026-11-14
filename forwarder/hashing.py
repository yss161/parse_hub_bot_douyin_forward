# -*- coding: utf-8 -*-
"""文件内容哈希（内容级去重）。

只对归档目录里最大的媒体文件做 SHA-256：同一作品重新发布成新 aweme_id 时，
视频文件通常完全相同；对最大文件取哈希比全目录便宜，也避开封面/元数据差异。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK = 1024 * 1024


def file_sha256(path: Path | str) -> str:
    """流式计算文件 SHA-256；文件不存在/不可读时返回空串。"""
    p = Path(path)
    try:
        if not p.is_file():
            return ""
        h = hashlib.sha256()
        with p.open("rb") as fh:
            while True:
                chunk = fh.read(_CHUNK)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def largest_file(files: list[Path | str]) -> Path | None:
    """返回体积最大的普通文件（跳过 metadata.json 等小文件）。"""
    best: Path | None = None
    best_size = -1
    for raw in files:
        p = Path(raw)
        try:
            if not p.is_file():
                continue
            size = p.stat().st_size
        except OSError:
            continue
        if p.name == "metadata.json":
            continue
        if size > best_size:
            best, best_size = p, size
    return best


def content_hash_for_files(files: list[Path | str]) -> str:
    """对最大媒体文件取 SHA-256；没有可读文件时返回空串。"""
    target = largest_file(files)
    return file_sha256(target) if target else ""


def short_hash(text: str, length: int = 16) -> str:
    """短文本指纹（绑定码等）；只存指纹，避免 state 里出现明文。"""
    data = (text or "").encode("utf-8")
    return hashlib.sha256(data).hexdigest()[: max(4, min(64, int(length)))]
