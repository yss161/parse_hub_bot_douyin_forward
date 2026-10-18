#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""统一入口（转发链路服务）。

引导顺序【很重要】：
    1. 把工作目录切到 parse_hub_bot/（原项目按相对路径读写 data/、logs/）；
    2. 注入 Telegram 占位环境变量（原项目配置的必填项；本服务不启动 TG Bot）；
    3. 把下载目录指到 forwarder_data/downloads（保持原项目目录干净）；
    4. 将 parse_hub_bot/ 与仓库根目录加入 sys.path；
    5. 启动转发链路。

用法：
    python run.py serve            # 启动常驻服务（默认）
    python run.py serve --once     # 只执行一轮轮询（调试）
    python run.py login            # 仅扫码登录小号后退出
    python run.py test-url <链接>  # 只测试解析链路（不启动浏览器）
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
PARSE_HUB_BOT_DIR = ROOT_DIR / "parse_hub_bot"
FORWARDER_DATA_DIR = ROOT_DIR / "forwarder_data"


def _bootstrap() -> None:
    if not PARSE_HUB_BOT_DIR.exists():
        sys.exit(
            f"未找到原项目目录: {PARSE_HUB_BOT_DIR}\n"
            "请先克隆: git clone https://github.com/z-mio/parse_hub_bot.git"
        )

    # 1) 工作目录 → parse_hub_bot（原项目 data/ 与 logs/ 均按相对路径解析）
    os.chdir(PARSE_HUB_BOT_DIR)

    # 2) 先加载用户配置（根目录 .env、parse_hub_bot/.env），再为缺失项注入占位值——
    #    这样配置了真实 Telegram 凭据（API_ID/API_HASH/BOT_TOKEN）时，TG 推送可直接使用
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT_DIR / ".env")
        load_dotenv(PARSE_HUB_BOT_DIR / ".env")
    except Exception:
        pass

    # 3) Telegram 占位配置：原项目 BotSettings 的必填项；未配置真实凭据时用于跳过校验（TG Bot 不启动）
    for key, value in (("BOT_TOKEN", "0:0"), ("API_ID", "1"), ("API_HASH", "forwarder")):
        os.environ.setdefault(key, value)

    # 4) 下载临时目录 → forwarder_data/downloads
    os.environ.setdefault("DOWNLOAD_DIR", str(FORWARDER_DATA_DIR / "downloads"))

    # 5) 模块搜索路径：原项目优先，其次仓库根目录（forwarder 包）
    sys.path.insert(0, str(PARSE_HUB_BOT_DIR))
    sys.path.insert(0, str(ROOT_DIR))


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="抖音转发解析服务（基于 parse_hub_bot 的解析实现）",
    )
    sub = parser.add_subparsers(dest="mode")

    p_serve = sub.add_parser("serve", help="启动常驻服务（轮询小号私信并解析转发视频）")
    p_serve.add_argument("--once", action="store_true", help="只执行一轮轮询后退出（调试）")

    sub.add_parser("login", help="仅扫码登录抖音小号，保存登录态后退出")

    p_test = sub.add_parser("test-url", help="直接测试解析链路（不启动浏览器）")
    p_test.add_argument("url", help="抖音视频链接（支持短链）")
    p_test.add_argument("--no-download", action="store_true", help="只解析不下载")

    sub.add_parser("export-cookie", help="从小号登录态自动导出抖音 cookie 并同步到解析端")

    p_cc = sub.add_parser("cookie-config", help="解析端抖音 cookie 管理（set/clear/from-file/show）")
    cc_sub = p_cc.add_subparsers(dest="cc_action")
    p_set = cc_sub.add_parser("set", help="手动设置一条 cookie 字符串")
    p_set.add_argument("value", help="cookie 字符串，如 'a=b; c=d'")
    cc_sub.add_parser("clear", help="清空抖音解析 cookie（改走移动端接口）")
    p_file = cc_sub.add_parser("from-file", help="从文件导入 cookie（每行一条）")
    p_file.add_argument("path", help="cookie 文件路径")
    cc_sub.add_parser("show", help="查看当前解析端 cookie（值已掩码）")

    sub.add_parser("doctor", help="环境自检（依赖 / 配置 / 登录态）")

    return parser


def _run_doctor() -> None:
    """环境自检：不启动浏览器，仅检查依赖导入与配置。"""
    import platform

    print("=" * 60)
    print("环境自检")
    print("=" * 60)
    print(f"Python        : {platform.python_version()} ({sys.executable})")
    print(f"工作目录      : {os.getcwd()}")

    ok = True

    def check(name: str, fn) -> None:
        nonlocal ok
        try:
            result = fn()
            print(f"[OK]   {name}" + (f" — {result}" if result else ""))
        except Exception as e:
            ok = False
            print(f"[FAIL] {name} — {type(e).__name__}: {e}")

    check("playwright", lambda: __import__("playwright").__name__)
    check("parsehub", lambda: __import__("parsehub").__version__ if hasattr(__import__("parsehub"), "__version__") else "已导入")
    check("services（原项目解析链路）", lambda: f"ParsePipeline={__import__('services').ParsePipeline.__name__}")

    from core import bs  # noqa: F401  （导入成功即说明占位 TG 配置生效）
    print(f"[OK]   core（原项目配置） — download_dir={bs.download_dir}")

    from forwarder.config import ForwarderSettings

    s = ForwarderSettings()
    print()
    print(s.describe())
    print()
    env_file = ROOT_DIR / ".env"
    print(f".env          : {'已找到 ' + str(env_file) if env_file.exists() else '未找到（可选，可复制 .env.example）'}")
    profile = s.browser_profile_dir
    print(f"浏览器 profile: {'已存在（如登录态失效请重新执行 python run.py login）' if profile.exists() and any(profile.iterdir()) else '不存在（首次使用请执行 python run.py login）'}")
    print(f"状态文件      : {'已存在' if s.state_file.exists() else '不存在（首次运行自动创建）'}")
    print()
    print("自检结果: " + ("全部通过 ✅" if ok else "存在失败项 ❌（见上方 [FAIL]）"))
    if not ok:
        raise SystemExit(1)


def main() -> None:
    _bootstrap()

    # 引导完成后再导入（这些模块会加载原项目的配置与日志）
    from log import setup_logging  # noqa: E402

    from forwarder.config import ForwarderSettings  # noqa: E402
    from forwarder.main import run_cookie_config, run_export_cookie, run_login, run_service, run_test_url  # noqa: E402

    args = _build_arg_parser().parse_args()
    settings = ForwarderSettings()

    setup_logging(debug=settings.debug)

    # 说明：刻意不启用原项目的 uvloop / winloop 事件循环优化。
    # 原因：winloop 与 Playwright 子进程启动（startupinfo 参数）不兼容（Windows 实测报错）；
    # Playwright 在 Linux 上同样会传递 startupinfo 参数，uvloop 存在同类兼容风险。
    # 本服务是"轮询 + 下载"的轻量负载，事件循环优化收益可忽略，稳定性优先。

    mode = args.mode or "serve"
    if mode == "doctor":
        _run_doctor()
    elif mode == "serve":
        asyncio.run(run_service(settings, once=args.once))
    elif mode == "login":
        asyncio.run(run_login(settings))
    elif mode == "test-url":
        asyncio.run(run_test_url(args.url, download=not args.no_download, settings=settings))
    elif mode == "export-cookie":
        asyncio.run(run_export_cookie(settings))
    elif mode == "cookie-config":
        action = args.cc_action or "show"
        value = getattr(args, "value", "") or getattr(args, "path", "") or ""
        asyncio.run(run_cookie_config(action, value, settings))


if __name__ == "__main__":
    main()
