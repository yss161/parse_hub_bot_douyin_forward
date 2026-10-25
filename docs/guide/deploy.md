---
title: Ubuntu 部署
description: 在 Ubuntu 服务器上一键部署、首次登录、systemd 与日常运维。
outline: deep
---

# Ubuntu 部署

完整细节见仓库 [DEPLOY_UBUNTU.md](https://github.com/yss161/parse_hub_bot_douyin_forward/blob/main/DEPLOY_UBUNTU.md)。

## 一键部署

```bash
cd /opt/douyin_download_bot
sudo APP_USER=douyin bash deploy/install_ubuntu.sh
```

脚本会依次完成：

1. 安装系统依赖（Python、编译工具、中文字体、xvfb、Playwright 依赖）；
2. 检测 Python ≥3.12（不足则安装）；
3. 创建 `.venv`，安装 `requirements-forwarder.txt`；
4. 安装 Playwright Chromium；
5. 生成 `.env`，交互式询问：监测账号 → 解析端 Cookie → 检测模式 → 实况图模式；
6. 安装 systemd 服务 `douyin-forward`（安装但不启动）；
7. 打印「首次登录 + 启动服务」步骤。

## 交互式配置

| 顺序 | 内容 | 说明 |
| --- | --- | --- |
| 1 | 监测账号 | 单会话 / 多会话 |
| 2 | 解析端 Cookie | 不填 / 自动同步 / 手动 / 文件 / 保持 |
| 3 | 检测模式 | reload（现状）/ probe（省资源） |
| 4 | 实况图模式 | video（现状）/ live / both |

已部署后随时可重配：

```bash
sudo bash deploy/configure_accounts.sh
sudo bash deploy/configure_parser_cookie.sh
sudo bash deploy/configure_detect_mode.sh
sudo bash deploy/configure_live_photo.sh
```

## 首次登录

⚠️ **登录态只能在服务器本机生成**，不能从其他电脑复制（Cookie 按系统加密，复制后静默失效）。

推荐用 xvfb 有界面扫码：

```bash
sudo -u douyin -H env DOUYIN_HEADLESS=false \
  /usr/bin/xvfb-run -a /opt/douyin_download_bot/.venv/bin/python \
  /opt/douyin_download_bot/run.py login
```

二维码保存到 `forwarder_data/login_qr.png`；也可看日志里的预览链接（默认端口 8765）。

## systemd

```bash
sudo systemctl enable --now douyin-forward
journalctl -u douyin-forward -f
```

常用命令：

```bash
sudo systemctl restart douyin-forward
sudo systemctl stop douyin-forward
sudo systemctl status douyin-forward
```

## 非交互部署

```bash
MONITOR_MODE=multi MONITOR_ACCOUNTS="小明,小红" \
PARSER_COOKIE_MODE=auto \
DETECT_MODE=probe \
LIVE_PHOTO_MODE=both \
sudo -E bash deploy/install_ubuntu.sh
```

## 目录与权限

| 路径 | 说明 |
| --- | --- |
| `/opt/douyin_download_bot` | 项目目录 |
| `.venv` | Python 虚拟环境 |
| `forwarder_data/browser_profile` | 浏览器登录态（不要跨机复制） |
| `forwarder_data/state.json` | 去重/水位/控制台设置 |
| `output` | 解析归档 |
| `deploy/douyin-forward.service` | systemd 模板 |

## 日常运维

- 查看日志：`journalctl -u douyin-forward -f`；
- 换监测账号：`echo "新昵称" | sudo tee forwarder_data/agent_cmd.txt`；
- 切检测/实况图模式：`echo "mode probe"` / `echo "live both"`；
- 升级：见[升级与回滚](/guide/upgrade)。
