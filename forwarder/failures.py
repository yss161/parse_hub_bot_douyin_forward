# -*- coding: utf-8 -*-
"""解析失败分类与重试策略。

目标：
  - 永久错误（地区版权、审核不通过、作者限制、作品删除）只试一次，立即进失败收件箱；
  - 临时错误（网络、超时、下载、Telegram 上传、singleflight）按指数退避重试；
  - 分类只用于“决定重试几次/怎么解释”，不改变解析器本身。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FailureKind:
    code: str
    label: str
    permanent: bool
    max_attempts: int
    hint: str = ""


# 顺序重要：先匹配更具体的永久错误，再匹配网络/通用错误。
_RULES: list[tuple[tuple[str, ...], FailureKind]] = [
    (
        ("copyright_foreign", "地区版权", "海外版权", "版权限制", "copyright"),
        FailureKind(
            "copyright_foreign",
            "地区版权限制",
            True,
            1,
            "作品受地区版权限制，海外出口无法播放；不是网络问题，换链接/代理也可能无效。",
        ),
    ),
    (
        ("status_audit_not_pass", "审核不通过", "status_audit"),
        FailureKind(
            "status_audit_not_pass",
            "作品审核未通过",
            True,
            1,
            "作品在抖音侧审核未通过，无法解析。",
        ),
    ),
    (
        ("author_secret", "作者设置", "私密作品", "仅作者可见", "author_only"),
        FailureKind(
            "author_secret",
            "作者权限限制",
            True,
            1,
            "作者设置了私密/仅自己可见，当前账号无权查看。",
        ),
    ),
    (
        ("作品不存在", "已删除", "视频不存在", "not found", "deleted", "private"),
        FailureKind(
            "unavailable",
            "作品不可用",
            True,
            1,
            "作品可能已删除、设为私密或链接失效。",
        ),
    ),
    (
        ("singleflight", "正在被其它任务解析", "busy"),
        FailureKind(
            "busy",
            "解析任务冲突",
            False,
            3,
            "同一链接正在被其它任务解析，稍后重试即可。",
        ),
    ),
    (
        ("展开原始链接失败", "timeout", "timed out", "超时", "connection", "网络", "ssl", "eof"),
        FailureKind(
            "network",
            "网络/展开链接失败",
            False,
            5,
            "网络或抖音接口临时异常，会自动退避重试。",
        ),
    ),
    (
        ("下载失败", "download", "下载", "文件系统错误", "no space", "磁盘"),
        FailureKind(
            "download",
            "下载/磁盘失败",
            False,
            4,
            "下载或写入本地文件失败；检查磁盘空间和网络后重试。",
        ),
    ),
    (
        ("savebigfilepart", "upload", "上传", "telegram", "floodwait", "flood_wait"),
        FailureKind(
            "telegram",
            "Telegram 上传失败",
            False,
            3,
            "解析与归档已成功，但 Telegram 上传失败；会自动重试。",
        ),
    ),
]


CATEGORY_LABELS: dict[str, str] = {
    kind.code: kind.label for _, kind in _RULES
}
CATEGORY_LABELS["pipeline"] = "解析流水线失败"

STATUS_LABELS: dict[str, str] = {
    "open": "待重试",
    "retrying": "重试中",
    "gave_up": "已放弃",
    "ignored": "已忽略",
    "resolved": "已解决",
}


def category_label(code: str | None) -> str:
    """把内部失败分类代码翻成中文；未知代码原样返回。"""
    raw = (code or "").strip()
    return CATEGORY_LABELS.get(raw, raw or "未知")


def status_label(status: str | None) -> str:
    """把失败记录状态翻成中文；未知状态原样返回。"""
    raw = (status or "").strip()
    return STATUS_LABELS.get(raw, raw or "未知")


def classify_failure(error: str | None) -> FailureKind:
    """根据错误文本分类；无法识别时按可重试的未知流水线错误处理。"""
    text = (error or "").strip()
    low = text.lower()
    for needles, kind in _RULES:
        if any(n.lower() in low for n in needles):
            return kind
    return FailureKind(
        "pipeline",
        "解析流水线失败",
        False,
        3,
        "流水线未返回更具体的原因；查看服务日志中的 copyright_foreign / 审核 / 作者限制等关键字。",
    )


def short_error(error: str | None, limit: int = 300) -> str:
    text = (error or "未知错误").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"
