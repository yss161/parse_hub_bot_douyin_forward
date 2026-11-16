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

如果还想用同一个 bot 查看状态、重试失败或直接发链接解析，继续配置 [Bot 控制台](/guide/console)。

## 推送行为

- 视频：带封面、流式播放；
- 图文：相册（每批最多 10 个）；
- GIF：动画；
- 实况图：按 `FORWARDER_LIVE_PHOTO_MODE` 发送 Live Photo / 视频 / 两者 / 频道+评论区；
- 背景音乐：图片/图文/实况的 BGM 作为一条音频评论发到对应帖子评论区（普通视频默认不发）；
- 发送失败自动降级为文档；解析与本地归档不受影响。

## 实况图推送

Telegram 的 `sendLivePhoto` 需要两个文件：

| 参数 | 要求 |
| --- | --- |
| `live_photo` | 实况视频，≤10 秒、≤10MB |
| `photo` | 静态图 |

超限或 `live`/`both` 下数量超过 `FORWARDER_LIVE_PHOTO_MAX_ITEMS` 时自动改发视频；`comment` 模式把 Live Photo 发到帖子评论区（不限张数，逐条间隔默认 2 秒）。详见[实况图](/guide/live-photo)。

### 频道 + 评论区（comment）

频道只发视频/相册，Live Photo 逐条发到该帖评论区。需要：

1. 频道开启评论区（关联讨论组）；
2. bot 加入讨论组并有发言权限（建议管理员）；
3. 频道发帖后等待讨论组自动转发，拿到评论根消息再回复。

bot 无法调用 `messages.GetDiscussionMessage`，因此评论根映射完全依赖讨论组更新；等待超时会跳过评论，但频道媒体不受影响。

### 背景音乐（BGM）

图片、图文与实况图作品的 BGM 会发在评论区第一条（在 Live Photo 之前），普通视频默认不发。详见[背景音乐归档](/guide/bgm)。

## 失败与重试

- 失败按类型重试：网络最多 5 次、下载 4 次、Telegram 3 次；地区版权/审核/作者私密等永久错误只试 1 次；
- 临时错误按 60s → 120s → … 指数退避，上限 1800s；
- 达到上限后进入失败收件箱，owner 会收到卡片，可用 `/retry` 重试；
- 本地归档始终保留（除非开启推送成功即删）；
- 常见失败：凭据错误、机器人无权限、服务器无法访问 Telegram。

失败分类和 Bot 命令见 [Bot 控制台](/guide/console#失败收件箱)。

如果频道里已有能解析失败链接的兜底 bot，可开启[失败兜底](/guide/fallback)：最终放弃时把链接发到兜底群，等待出片后接回频道，并删除链接。

## 国内服务器代理

```ini
BOT_PROXY=http://127.0.0.1:7890
```

支持 socks5:// 等；海外服务器一般无需代理。