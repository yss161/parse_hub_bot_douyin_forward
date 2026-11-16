---
title: 失败兜底
description: 解析最终失败时把链接转发到兜底群，等待兜底 bot 出片，成功后删除链接；可选把兜底视频还原成 Live Photo。
outline: deep
---

# 失败兜底（可选）

抖音作品可能因为地区版权、审核、作者私密、接口变动等原因解析失败。若你的频道里已经有一个能解析这类链接的兜底 bot，本服务可以在**最终放弃**时把原始链接发到兜底群/频道，等它出片，再把成品接回自己的频道。

> 兜底默认关闭。设置 `FORWARDER_FALLBACK_CHAT` 才会启动；不配置时行为与旧版本完全一致。

## 触发条件

- 解析失败并按重试策略**最终放弃**（`attempts >= limit`）；
- 不是「解析成功但 Telegram 推送失败」——推送失败只重试推送，不转发链接；
- **作品已删除或不可见时不触发**：发送前先查抖音详情，`status_deleted`、`status_self_see`、`status_unavailable`（以及提示“作品不见了/已删除/已下架”）直接跳过，失败记录标记为 `unavailable`；换解析 bot 也拿不到，发出去只会刷屏；
- 地区版权（`copyright_foreign`）、接口临时失败等仍会走兜底；
- 同一链接只会发一次；成功接回后按配置删除我们发出的链接消息。

## 两种部署形态

### A. 兜底 bot 与成品频道是同一个频道

```ini
FORWARDER_FALLBACK_CHAT=@你的成品频道
FORWARDER_FALLBACK_BOT=@ParseHubot
```

- 兜底 bot 的频道帖已经出现在成品频道，本服务**不复制视频**，避免重复；
- 若原作品是实况图、兜底 bot 只发了视频，本服务在该频道帖下补 Live Photo 评论；
- 非实况作品：兜底 bot 已经发过，本服务只删链接，不重复发。

### B. 兜底 bot 在独立群/频道

```ini
FORWARDER_FALLBACK_CHAT=-1001234567890
FORWARDER_FALLBACK_BOT=@ParseHubot
```

- 本服务把兜底 bot 的视频/图片 `copy` 到 `TG_TARGET_CHAT`；
- 实况来源：先复制视频到成品频道，再在复制出的帖子评论区补 Live Photo；评论区不可用时直接把 Live Photo 发到频道；
- 非实况：只复制媒体，不补评论。

## 配置

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FORWARDER_FALLBACK_CHAT` | 空 | 兜底群/频道：`@username` 或 `-100...`；留空关闭 |
| `FORWARDER_FALLBACK_BOT` | `@ParseHubot` | 兜底 bot 用户名，用于识别「谁发的算成品」 |
| `FORWARDER_FALLBACK_TIMEOUT` | `180` | 等待出片秒数（最小 30）；超时删除频道里的链接，只把原始链接私聊给 owner |
| `FORWARDER_FALLBACK_DELETE_LINK` | `true` | 成功接回后删除我们发的链接 |
| `FORWARDER_FALLBACK_MARKER` | `#fallback` | 链接消息前缀，便于人工识别 |
| `FORWARDER_VIDEO_AS_LIVE` | `true` | 兜底视频是否尝试还原成 Live Photo |
| `FORWARDER_VIDEO_AS_LIVE_MAX_SECONDS` | `10` | 转换最大秒数 |
| `FORWARDER_VIDEO_AS_LIVE_MAX_MB` | `10` | 转换最大体积 |

我们发出的链接消息形如：

```text
#fallback
https://www.douyin.com/video/7706162379688245967
copyright_foreign
```

消息里的错误文本也会参与删除判定：若失败原因本身是 `status_deleted` / `status_self_see`，即使不查详情也会直接跳过。

## 视频还原成 Live Photo

原始项目把实况图的每个 `LivePhotoFile` 发成一条短视频（静态图只作为视频封面）。本服务收到兜底视频后：

1. 先读原链接的作品详情，确认媒体类型是 `live`（`aweme_type=68` 或 `images[].video`）；
2. 下载视频，用 OpenCV 读取**第一帧**作为静态图（不转码视频）；
3. 校验视频 ≤ `FORWARDER_VIDEO_AS_LIVE_MAX_SECONDS` 秒、≤ `FORWARDER_VIDEO_AS_LIVE_MAX_MB` MB；
4. 调用与正常实况图相同的 `send_live_photo`；超限/失败回退为普通视频。

> 只对确认是实况来源的视频转换，普通视频不会误转。转换需要 `opencv-python-headless`（已在 `requirements-forwarder.txt` 声明；未安装时自动跳过转换，按普通视频处理）。

## 历史失败重跑

服务运行中即可触发，不需要重启。把任务写到工作目录下的 `forwarder_data/fallback_replay.json`：

```json
{"mode": "all"}
```

| 字段 | 说明 |
| --- | --- |
| `mode` | `all` 重跑全部 `gave_up`，`unavailable` / `resolved` 不再重跑 |
| `limit` | 最多处理几条（按时间倒序） |
| `ids` | 指定失败记录 id 列表 |
| `urls` | 直接指定链接列表（不依赖失败库） |

服务发现文件后会删除它，并在 `forwarder_data/fallback_replay_report.json` 写入结果。批量模式先发一条清单私信，结束后发汇总；单条模式逐条私信。已归档和已删除/不可见的链接在清单阶段就被过滤，不会发到兜底群。

## 前置条件与风险

- bot 必须已在兜底群/频道内，并有读取消息的权限；
- **最大的不确定项**：Telegram Bot API 不保证 bot 能收到**其他 bot** 在群里的消息。频道帖（`channel_post`）通常可以收到，普通群消息经常收不到；
- Bot API 不允许 bot 读取任意历史消息（`messages.GetHistory` 会报 `BOT_METHOD_INVALID`），所以只能实时监听，不能补读；
- 如果日志里始终看不到兜底 bot 的消息，可改用：把兜底 bot 设为频道管理员让它发频道帖、或使用专用用户账号监听、或轮询公开频道预览（`t.me/s/...`，仅公开频道且解析 HTML 脆弱）；
- 并发失败时，未带 `reply` 的兜底消息会归给最近一条待处理链接；建议不要同时堆积大量失败链接；
- BGM 不会混入兜底 Live Photo，也不会为兜底接回的作品补发 BGM（原链接详情可能已不可用）。

## 验证

```bash
journalctl -u douyin-forward -f | grep -i fallback
```

正常日志：

```text
兜底服务就绪：chat=-1001234567890（群组，独立兜底群）
兜底 bot: ParseHubot（id 7947627028）
已发送兜底链接（消息 3311）: https://www.douyin.com/video/...
兜底视频已转 Live Photo: https://www.douyin.com/video/...
兜底成功，已删除链接消息 3311
```

超时日志：

```text
兜底超时（181s），已删除频道链接，私信保留: https://www.douyin.com/video/...
```

已删除作品跳过：

```text
作品已删除/不可见（status_deleted），跳过兜底: https://www.douyin.com/video/...
```

## 关闭兜底

```ini
FORWARDER_FALLBACK_CHAT=
```

重启后不再启动监听，也不会发送任何兜底链接；服务停止期间已发出的链接不会自动清理，可手动删除。
