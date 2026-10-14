# -*- coding: utf-8 -*-
"""归档：把原项目解析流水线的结果复制到输出目录，并写入元数据。

说明：
  - 流水线的临时目录（downloads/<随机目录>）在 `with ParsePipeline(...)` 退出时会被
    原项目自动清理，因此必须在清理之前调用本模块完成拷贝；
  - 默认跳过 TG 媒体处理（skip_media_processing=True）时，processed_list 中的
    output_paths 就是下载到的原始文件路径，直接复制即可；
  - 若关闭跳过（走原项目的转码/处理），则复制处理后的文件。
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# 从抖音链接中提取视频/图文 ID（用于目录命名与去重指纹）
_VIDEO_ID_RE = re.compile(r"/(?:video|note|slides)/(\d+)")
_SHORT_ID_RE = re.compile(r"v\.douyin\.com/([A-Za-z0-9_\-]+)")
_MODAL_ID_RE = re.compile(r"modal_id=(\d+)")


def video_key_from_url(url: str) -> str:
    """从链接中提取一个可读的 key（视频 ID / 短链码）；取不到时返回空串。"""
    for pattern in (_VIDEO_ID_RE, _MODAL_ID_RE, _SHORT_ID_RE):
        m = pattern.search(url)
        if m:
            return m.group(1)
    return ""


@dataclass
class ArchiveResult:
    dir: Path
    files: list[Path] = field(default_factory=list)


def _defensive_get(obj: Any, *names: str, default: Any = None) -> Any:
    """从对象上按顺序尝试多个属性名（兼容 parsehub 不同版本的字段差异）。"""
    for name in names:
        value = getattr(obj, name, None)
        if value not in (None, "", [], {}):
            return value
    return default


def _stringify_author(pr: Any) -> str:
    author = _defensive_get(pr, "author", "author_name", "nickname", "uploader")
    if author is None:
        return ""
    if isinstance(author, str):
        return author
    # parsehub 的 author 可能是对象（含 name / nickname 等字段）
    for attr in ("name", "nickname", "title"):
        value = getattr(author, attr, None)
        if isinstance(value, str) and value:
            return value
    return str(author)


def _collect_media_files(result: Any) -> list[Path]:
    """从 PipelineResult.processed_list 中收集需要归档的文件路径。"""
    files: list[Path] = []
    processed_list = getattr(result, "processed_list", None) or []
    for item in processed_list:
        candidates: list[Path] = []
        for p in getattr(item, "output_paths", None) or []:
            candidates.append(Path(p))
        if not candidates:
            # skip_media_processing=False 且无输出路径时，退回源文件路径
            src = _defensive_get(getattr(item, "source", None), "path")
            if src:
                candidates.append(Path(src))
        for c in candidates:
            if c.is_file():
                files.append(c)
    # 去重（保持顺序）
    return list(dict.fromkeys(files))


def build_metadata(
    parse_result: Any,
    *,
    source_url: str,
    raw_url: str,
    files: list[Path],
) -> dict[str, Any]:
    media_files = []
    for f in files:
        media_files.append({"file": f.name, "size": f.stat().st_size})

    media_obj = _defensive_get(parse_result, "media", default=[])
    if isinstance(media_obj, (list, tuple)):
        media_count = len(media_obj)
    else:
        media_count = 1 if media_obj else 0

    return {
        "platform": "douyin",
        "platform_name": str(_defensive_get(parse_result, "platform", default="") or ""),
        "type": str(_defensive_get(parse_result, "type", default="") or ""),
        "title": str(_defensive_get(parse_result, "title", default="") or ""),
        "author": _stringify_author(parse_result),
        "content": str(_defensive_get(parse_result, "content", default="") or ""),
        "media_count": media_count,
        "source_url": source_url,   # 私信里提取到的链接（可能是短链）
        "raw_url": raw_url,         # 原项目 get_raw_url 展开后的原始链接
        "video_key": video_key_from_url(raw_url) or video_key_from_url(source_url),
        "media": media_files,
        "archived_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def archive_result(
    result: Any,
    *,
    source_url: str,
    raw_url: str,
    output_dir: Path,
    save_metadata: bool = True,
) -> ArchiveResult:
    """在流水线清理临时目录之前调用，把结果拷贝到 output_dir 下的独立归档目录。"""
    files = _collect_media_files(result)

    # 目录名：时间戳_视频key（防重名追加序号）
    key = video_key_from_url(raw_url) or video_key_from_url(source_url) or "unknown"
    base = output_dir / f"{datetime.now():%Y%m%d_%H%M%S}_{key}"
    target = base
    index = 1
    while target.exists():
        target = base.with_name(f"{base.name}_{index}")
        index += 1

    target.mkdir(parents=True, exist_ok=True)

    copied: list[Path] = []
    used_names: set[str] = set()
    for f in files:
        name = f.name
        stem_index = 1
        while name in used_names or (target / name).exists():
            name = f"{f.stem}_{stem_index}{f.suffix}"
            stem_index += 1
        used_names.add(name)
        dst = target / name
        shutil.copy2(f, dst)
        copied.append(dst)

    # 原项目 save_metadata 生成的 json（若存在）一并归档
    dl_dir = getattr(result, "output_dir", None)
    if dl_dir:
        for extra in sorted(Path(dl_dir).glob("*.json")):
            name = extra.name
            if name in used_names or (target / name).exists():
                continue
            dst = target / name
            shutil.copy2(extra, dst)
            used_names.add(name)

    if save_metadata:
        meta = build_metadata(result.parse_result, source_url=source_url, raw_url=raw_url, files=copied)
        (target / "metadata.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return ArchiveResult(dir=target, files=copied)
