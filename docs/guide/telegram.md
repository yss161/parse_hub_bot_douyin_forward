---
title: Telegram 推送
description: 配置 Telegram 机器人、推送视频/图文/Live Photo，以及失败重试与代理。
outline: deep
---

# Telegram 推送

解析完成后可自动把媒体发送到指定 Telegram 群组/频道。

## 1. 申请凭据

1. 打开 https://my.telegram.org → **API development tools** → 创建应用 → 记下 `API_ID`、`API_HASH`；
2. Telegram 里找 **@BotFather** → `/newbot` → 记下 `BOT_TOKEN`。

## 2. 把机器人加入目标

- **频道**：频道 → 管理 → 管理员 → 添加机器人，勾选「发布消息」；
- **群组**：直接把机器人拉进群；限制发言时给管理员权限。

## 3. 配置 .env

```ini
TG_PUSH=true
TG_TARGET_CHAT=@你的频道用户名       # 私有频道/群用 -100 开头的数字ID，也支持 t.me 链接
API_ID=1234567
API_HASH=xxxxxxxxxxxxxxxxxxxxxxxx
BOT_TOKEN=123456789:AAxxxxxxxxxxxxxx
```

> 私有频道/群的数字 ID：把任意消息转发给 @userinfobot / @RawDataBot，返回的 `Forwarded from ... id: -100xxxx` 即所需 ID。

## 4. 重启并验证

```bash
sudo systemctl restart douyin-forward
journalctl -u douyin-forward -f
```

日志出现 `TG 推送已就绪 ✅ 目标: xxx` 即配置成功；之后每次解析完成会多出 `已推送至 Telegram`。

## 推送行为

- 视频：带封面、流式播放；
- 图文：相册（每批最多 10 个）；
- GIF：动画；
- 实况图：按 `FORWARDER_LIVE_PHOTO_MODE` 发送 Live Photo / 视频 / 两者；
- 发送失败自动降级为文档；解析与本地归档不受影响。

## 实况图推送

Telegram 的 `sendLivePhoto` 需要两个文件：

| 参数 | 要求 |
| --- | --- |
| `live_photo` | 实况视频，≤10 秒、≤10MB |
| `photo` | 静态图 |

超限或数量超过 `FORWARDER_LIVE_PHOTO_MAX_ITEMS` 时自动改发视频。详见[实况图](/guide/live-photo)。

## 失败与重试

- 推送失败与解析共用重试机制（最多 3 次）；
- 本地归档始终保留（除非开启推送成功即删）；
- 常见失败：凭据错误、机器人无权限、服务器无法访问 Telegram。

## 国内服务器代理

```ini
BOT_PROXY=http://127.0.0.1:7890
```

支持 socks5:// 等；海外服务器一般无需代理。
