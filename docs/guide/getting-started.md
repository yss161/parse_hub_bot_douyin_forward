---
title: 快速开始
description: 从零开始安装、配置、登录并启动抖音转发解析服务。
outline: deep
---

# 快速开始

## 环境要求

- **Python 3.12+**（建议 3.13）
- 一台**能正常访问抖音网页版**的机器（国内网络最佳；海外 IP 可能触发风控）
- 建议使用**专门的抖音小号**，并与大号**互相关注**

## 获取代码

```bash
git clone --recursive https://github.com/yss161/parse_hub_bot_douyin_forward.git douyin_download_bot
cd douyin_download_bot

# 如果克隆时漏了 --recursive：
git submodule update --init --recursive
```

## 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate

pip install -r requirements-forwarder.txt
playwright install chromium
# Linux 缺系统依赖：playwright install-deps chromium
```

> 不要执行 `pip install -e parse_hub_bot`：原项目是 uv 工程，本服务通过 `run.py` 直接把它加入模块路径。

## 配置

```bash
cp .env.example .env
```

最小配置（二选一）：

```ini
# 单会话：只监测一个大号
DOUYIN_MAIN_ACCOUNT=你的大号昵称

# 多会话：同时监测多个用户/大号
DOUYIN_MAIN_ACCOUNTS=小明,小红
```

其余项都有默认值。完整说明见[完整配置](/guide/configuration)。

> Ubuntu 一键部署会交互式询问「单会话 / 多会话」「检测模式」「实况图模式」「解析端 Cookie」，见[Ubuntu 部署](/guide/deploy)。

## 登录小号

```bash
# 推荐：有桌面的机器，首次用有界面模式
DOUYIN_HEADLESS=false python run.py login
```

- 二维码弹窗会自动出现，截图保存到 `forwarder_data/login_qr.png`；
- 二维码会以字符画 + base64 打印在日志里，SSH 会话可直接扫；
- 登录态持久化到 `forwarder_data/browser_profile/`，之后免扫码。

> ⚠️ **首次必须有界面**：抖音对「无头浏览器 + 全新指纹」容易触发滑块验证。
> 服务器无桌面时用 xvfb：`DOUYIN_HEADLESS=false xvfb-run -a python run.py login`。
>
> ❌ **不要跨机器复制登录态**：浏览器 Cookie 按操作系统加密（Windows 用 DPAPI），复制到别的机器会静默失效，必须在本机登录一次。

## 启动服务

```bash
python run.py serve
```

首次启动会**记录当前消息水位**，只处理启动之后新转发的消息。需要处理历史消息时：

```ini
DOUYIN_PROCESS_HISTORY=true
```

并删除 `forwarder_data/state.json` 后重启。

## 第一次使用

用**大号**把想保存的视频/图文/实况图「转发 / 分享 → 私信」给小号。稍等片刻：

- 控制台和 `parse_hub_bot/logs/bot.log` 会打印全过程；
- 解析结果归档到 `output/<时间戳>_<作品ID>/`；
- 如启用 TG 推送，会收到视频/相册/Live Photo。

## 运行模式

| 命令 | 作用 |
| --- | --- |
| `python run.py serve` | 启动常驻服务 |
| `python run.py serve --once` | 只执行一轮（调试） |
| `python run.py login` | 仅扫码登录后退出 |
| `python run.py test-url <链接>` | 不启动浏览器，验证解析链路 |
| `python run.py export-cookie` | 导出小号 cookie 到解析端 |
| `python run.py cookie-config <set/clear/from-file/show>` | 管理解析端 cookie |
| `python run.py doctor` | 环境自检 |

## 下一步

- [完整配置](/guide/configuration)
- [检测模式](/guide/detection)
- [实况图](/guide/live-photo)
- [Telegram 推送](/guide/telegram)
