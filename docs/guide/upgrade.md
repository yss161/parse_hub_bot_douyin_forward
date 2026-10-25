---
title: 升级与回滚
description: 安全升级、行为兼容性与回滚步骤。
outline: deep
---

# 升级与回滚

## 升级

```bash
cd /opt/douyin_download_bot
sudo systemctl stop douyin-forward

# 备份配置与状态（profile 不建议跨机器复制，但同机备份可用于回滚）
cp .env .env.bak
cp forwarder_data/state.json forwarder_data/state.json.bak 2>/dev/null || true

sudo -u douyin git pull --recurse-submodules
sudo -u douyin .venv/bin/pip install -r requirements-forwarder.txt
sudo -u douyin .venv/bin/playwright install chromium

sudo systemctl start douyin-forward
journalctl -u douyin-forward -f
```

升级后建议：

```bash
sudo -u douyin .venv/bin/python run.py doctor
sudo -u douyin .venv/bin/python run.py serve --once
```

## 行为兼容性

- 检测模式默认 `reload`、实况图模式默认 `video`——**不配置任何项，行为与旧版一致**；
- 新配置项都有默认值，旧 `.env` 不需要改；
- 控制台设置保存在 `state.json`，升级不会清空。

## 回滚

```bash
cd /opt/douyin_download_bot
sudo systemctl stop douyin-forward
sudo -u douyin git checkout <上一个提交>
sudo -u douyin .venv/bin/pip install -r requirements-forwarder.txt
sudo systemctl start douyin-forward
```

如升级后异常，可先恢复 `.env.bak` / `state.json.bak`，再回滚代码。
