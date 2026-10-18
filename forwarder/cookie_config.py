# -*- coding: utf-8 -*-
"""解析端（抖音）Cookie 管理。

背景：
  - 接收端（小号）与解析端（parsehub）是两条独立通道，登录态互不相通；
  - 解析端 cookie 存原项目的 `parse_hub_bot/data/config/platform_config.yaml`（可选）；
  - 不填 cookie 时 parsehub 走「移动端接口」，通常也能解析公开视频；填了网页 cookie 则
    优先走「网页端接口」（更稳、抗风控、偶发能拿到更高档位）。

本模块**复用原项目已有的热更新服务** `services.platform_config.platform_config_service`，
写入即校验、原子落盘、并原地更新 `pl_cfg`（已运行的服务立即生效，无需重启）。
所有新增代码都在 forwarder/ 内，不改原项目一行代码。
"""

from __future__ import annotations

from typing import Any

from log import logger

logger = logger.bind(name="Forwarder/CookieConfig")

_DOUYIN = "douyin"


def mask_cookie(cookie: str) -> str:
    """只显示 cookie 的键名、隐藏值（日志安全）。"""
    parts = []
    for p in cookie.split(";"):
        p = p.strip()
        if not p:
            continue
        parts.append(p.split("=", 1)[0] + "=***" if "=" in p else p)
    return "; ".join(parts)


async def _edit_cookies(cookies: list[str] | None) -> None:
    """设置（或清空）抖音解析 cookie，复用原项目热更新服务。"""
    from core.platform_config import Platform
    from services.platform_config import platform_config_service

    def fn(cfg: Any) -> None:
        old = cfg.platforms.get(_DOUYIN)
        data: dict[str, Any] = old.to_data() if old is not None else {}
        if cookies:
            data["cookies"] = list(cookies)
        else:
            data.pop("cookies", None)
        cfg.platforms[_DOUYIN] = Platform(**data)

    await platform_config_service.edit(fn)


async def apply_cookies(cookies: list[str]) -> int:
    """写入抖音解析 cookie（多条则随机轮换），返回写入条数。"""
    cookies = [c.strip() for c in cookies if c and c.strip()]
    if not cookies:
        await _edit_cookies(None)
        logger.success("已清空抖音解析 cookie（改走移动端接口）")
        return 0
    await _edit_cookies(cookies)
    logger.success(f"已写入抖音解析 cookie（{len(cookies)} 条，即时生效）")
    for c in cookies:
        logger.info(f"  cookie: {mask_cookie(c)}")
    return len(cookies)


async def clear_cookies() -> None:
    """清空抖音解析 cookie。"""
    await _edit_cookies(None)
    logger.success("已清空抖音解析 cookie（改走移动端接口）")


def current_cookies() -> list[str]:
    """读取当前已配置的抖音解析 cookie（明文，供 show 用）。"""
    from core import pl_cfg

    pc = pl_cfg.get(_DOUYIN)
    if pc is None or not pc.cookies:
        return []
    return [c.get_secret_value() for c in pc.cookies]


async def export_from_browser(receiver: Any) -> str | None:
    """从已登录的小号浏览器上下文导出 douyin 网页 cookie（`k=v; k=v` 串）。

    `receiver` 必须是已启动（登录完成）的 DouyinWebReceiver。
    """
    context = getattr(receiver, "_context", None)
    if context is None:
        return None
    try:
        cookies = await context.cookies("https://www.douyin.com")
    except Exception:
        return None
    if not cookies:
        return None
    parts = [f"{c['name']}={c['value']}" for c in cookies if c.get("value")]
    return "; ".join(parts) if parts else None
