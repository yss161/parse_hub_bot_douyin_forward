---
title: 开发与测试
description: 代码结构、本地检查、选择器调试与实况图联调。
outline: deep
---

# 开发与测试

## 代码结构

| 模块 | 职责 |
| --- | --- |
| `forwarder/douyin_web.py` | 浏览器生命周期、登录、检测（reload/probe）、消息提取、远程控制台 |
| `forwarder/parser_bridge.py` | 调用原项目 `ParseService` / `ParsePipeline`，在临时目录清理前归档 |
| `forwarder/archive.py` | 复制媒体、生成 `metadata.json`、实况图视频归档 |
| `forwarder/state.py` | 去重/水位/失败计数/控制台设置 |
| `forwarder/tg_push.py` | 构建并发送 Telegram 媒体（含 Live Photo 分支） |
| `forwarder/main.py` | 服务主循环、解析重试、缓存清理 |
| `forwarder/config.py` | 环境变量配置与模式规范化 |
| `run.py` | 统一入口与 bootstrap（chdir / 注入占位环境变量 / sys.path） |

## 本地检查

```bash
python -m compileall -q forwarder run.py
python run.py doctor
python run.py test-url "https://v.douyin.com/xxxxxxx/" --no-download
python run.py serve --once
bash -n deploy/*.sh
```

## 调试选择器

抖音改版导致 DOM 选择器失效时：

1. `.env` 设 `FORWARDER_DUMP_HTML=true`；
2. 复现一次提取失败；
3. 查看 `forwarder_data/debug/*.html`，对照 `forwarder/douyin_web.py` 顶部的
   `CONVERSATION_ITEM_SELECTORS` / `MESSAGE_CONTAINER_SELECTORS` / `MESSAGE_ITEM_SELECTORS` / `LINK_PATTERNS` 调整。

## 实况图联调

- 用一条公开实况图作品验证解析：结果应为 `ImageParseResult` + 多个 `LivePhotoRef`；
- 下载后应为 `LivePhotoFile`（jpg + mp4），归档目录同时出现两种文件；
- 若只看到视频，检查 `FORWARDER_LIVE_PHOTO_MODE` 是否为 `video`。

## 约束

- **parse_hub_bot 零改动**：所有新代码放在 `forwarder/`，用 `run.py` 引导；
- 解析必须通过原项目服务层：`services.ParseService` / `services.ParsePipeline`；
- 不启用 uvloop/winloop（与 Playwright 子进程兼容性风险）。

> 当前仓库未内置 pytest 套件；以上命令 + `serve --once` 日志是最小回归路径。
