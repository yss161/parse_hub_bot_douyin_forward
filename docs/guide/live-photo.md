---
title: 实况图（Live Photo）
description: 抖音实况图的识别原理、四种推送模式（含频道+评论区）、超限回退与验证方法。
outline: deep
---

# 实况图（Live Photo）

抖音的「实况图」作品 = **静态图 + 一小段视频**（通常 3 秒左右）。本服务能识别这类作品，并按模式决定发到 Telegram 的形态。

## 识别原理

1. 抖音作品详情里，实况图的 `aweme_detail.images[]` 每一项都带 `video` 字段（静态图 + 实况视频）；实测 `aweme_type=68`。
2. parsehub 把它解析为 `LivePhotoRef`（`url` = 静态图，`video_url` = 实况视频）。
3. 下载后得到 `LivePhotoFile`：`path` = jpg，`video_path` = mp4。
4. **旧行为**：原项目 `build_input_media` 把 `LivePhotoFile` 映射成 `InputMediaVideo(media=video_path)`，所以 Telegram 只收到视频——这就是「实况图解析出来直接是视频」的原因；解析层其实已经拿到了图片和视频。
5. 本服务在 `forwarder/tg_push.py` 接管这个分支：按模式调用 `forwarder/live_photo.py` 直接走 MTProto 发送（修正 kurigram 2.2.26 的 cover 引用错误），或在超限时回退视频。

> 兼容性说明：目前实测抖音实况图走 `images[].video`；如果未来抖音切到 `image_post_info.images[].video`，parsehub 2.3.5/2.3.14 会丢掉视频（只保留 `display_image`），需要再补一层适配。当前版本不受影响。

## 模式行为矩阵

| 模式 | 普通实况图（≤10s/≤10MB） | 超限实况图（>10s 或 >10MB） | 实况图数量 > MAX_ITEMS | 单条发送失败 |
| --- | --- | --- | --- | --- |
| `video`（默认） | 发视频 | 发视频 | 发视频 | — |
| `live` | 发 Live Photo | **改发视频** | **回退视频** | 该条改发视频 |
| `both` | Live Photo + 视频 | **只发视频** | **回退视频** | 该条改发视频 |
| `comment` | 频道发视频/相册；评论区发 Live Photo | 频道发视频；该条不进评论区（视频不转码） | 频道视频完整发送；评论区只发前 N 张 | 该条评论改发视频 |

## 超限与回退规则

- Telegram 硬限制：`live_photo` 视频 **≤10 秒、≤10MB**；超限**不转码**，直接按普通视频发送（避免服务器依赖 ffmpeg）。
- 实况图数量超过 `FORWARDER_LIVE_PHOTO_MAX_ITEMS`（默认 10）→ `live`/`both` 回退为视频；`comment` 只截断评论区，频道视频/相册仍完整发送；设为 `0` 可关闭限制。
- 缺静态图/视频、读取视频时长失败、`send_live_photo` 调用失败 → 该条自动改发视频（`comment` 模式下改发到同一条评论）。
- 所有回退都会在日志中说明原因（`超过 10MB` / `时长 … 超过 10s` / `实况图 … 张 > 上限 …`）。

## 配置

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FORWARDER_LIVE_PHOTO_MODE` | `video` | `video` / `live` / `both` / `comment` |
| `FORWARDER_LIVE_PHOTO_MAX_SECONDS` | `10` | 实况视频最大秒数，超过改发视频 |
| `FORWARDER_LIVE_PHOTO_MAX_MB` | `10` | 实况视频最大 MB，超过改发视频 |
| `FORWARDER_LIVE_PHOTO_MAX_ITEMS` | `10` | `live`/`both`：超过该值回退视频；`comment`：只限制评论区条数；`0` = 不限 |

## 部署时选择

```bash
sudo bash deploy/configure_live_photo.sh
# 或非交互：
LIVE_PHOTO_MODE=comment sudo -E bash deploy/install_ubuntu.sh
```

## 运行中免重启切换

```bash
echo "live video" > forwarder_data/agent_cmd.txt   # 只发视频（现状）
echo "live live"  > forwarder_data/agent_cmd.txt   # 只发实况图
echo "live both"  > forwarder_data/agent_cmd.txt   # 实况图 + 视频
echo "live comment" > forwarder_data/agent_cmd.txt # 视频发频道，实况图发评论区
echo "live"       > forwarder_data/agent_cmd.txt   # 查看当前模式与限制
echo "live reset" > forwarder_data/agent_cmd.txt   # 恢复 .env 默认
```

## comment 模式（频道 + 评论区）

`comment` 的目标是「频道只展示视频，Live Photo 放到该作品的评论区」：

1. 频道按原有方式发送解析后的视频/视频相册（`LivePhotoFile` 也以视频形式进入相册，保证频道内容完整、集合发送）；
2. 同一作品里符合 Telegram 限制的 Live Photo（≤10 秒、≤10MB）逐条发到该帖的评论区；
3. 作品只有实况图时，频道仍把这些实况视频当普通视频/相册发送（因此频道能集合展示），Live Photo 另发评论区；
4. 只有「连实况视频都没有」或频道媒体发送失败时，才退化为发送第一张静态图（A 方案）。

### 前置条件

- 频道已开启「评论区」（关联讨论组）；
- bot 已加入该讨论组，且有发消息权限（建议管理员）；
- bot 能收到讨论组的自动转发消息——这是唯一可靠的「频道帖 → 评论根」映射方式。

### 评论根映射

Telegram Bot API 不允许 bot 调用 `messages.GetDiscussionMessage` / `messages.GetHistory`（会报 `BOT_METHOD_INVALID`）。本服务的做法：

1. 频道发帖后，讨论组会收到一条自动转发消息，字段包含 `forward_from_chat.id`（频道 ID）和 `forward_from_message_id`（频道帖 ID）；
2. 服务启动时通过 `channels.GetFullChannel` 取得 `linked_chat_id`，注册讨论组更新监听；
3. 发送评论前等待该频道帖对应的转发消息（默认最多 10 秒），拿到讨论组消息 ID 作为 `reply_to_message_id`；
4. 用 MTProto `messages.SendMedia` + `InputReplyToMessage` 发送 Live Photo；失败时改发视频到同一条评论。

> 实测：频道帖 1940 → 讨论组根消息 1955，Live Photo 评论 1956（`reply_to=1955`）；相册帖只产生一个根消息，多条评论可共用。

### 限制

- 评论逐条发送，不组成相册；`FORWARDER_LIVE_PHOTO_MAX_ITEMS` 只限制评论条数（默认 10，`0` = 不限）；
- 评论发送失败不会回退到频道重发，避免同作品重复；只会在该评论位置改发视频；
- 等待评论根超时会跳过本次评论（频道媒体已发送成功），日志会记录原因；
- 该模式需要 bot 持续接收更新，资源占用略高于纯发送模式。

## 归档

实况图归档会同时保存 jpg + mp4：

```text
output/20261025_112210_7408859975616957696/
├── 001_开心-好天气.jpg
├── 001_开心-好天气_video.mp4
├── ...
└── metadata.json
```

`metadata.json` 的 `live_photo_count` 记录实况图数量（普通视频/图集为 0）。详见[归档与去重](/guide/archive)。

## 验证方法

```text
# 启动日志
实况图模式: comment（视频发频道，实况图发评论区）；限制 ≤10s / ≤10MB；评论区最多 10 张

# 推送日志
评论模式: 频道媒体 2 项，评论区实况图 2 张
评论模式完成：频道消息 1942，评论区 2 条（讨论组根消息 1959）
```

- 单张实况图作品：`live` 模式应收到一条可播放的 Live Photo；`comment` 模式频道收到视频、评论区收到 Live Photo；
- 14 张实况图作品：默认 `MAX_ITEMS=10`，`live`/`both` 应回退为视频相册；`comment` 频道仍发完整视频相册、评论区最多 10 条；
- 超 10 秒的视频：日志出现 `时长 … 超过 10s，改发视频`。

## 限制与已知问题

- Telegram 的 Live Photo **不支持相册**（Bot API 的 `sendMediaGroup` 没有 live photo 类型），多条只能逐条发送；
- 旧版 Telegram 客户端可能只显示静态图；
- 实况图视频超过 10 秒/10MB 只能改发视频（本版本不转码）；
- `live` 模式没有视频备份，兼容性要求高时建议选 `both` 或 `video`；
- `comment` 模式依赖频道评论区与讨论组自动转发，频道未开启评论区或 bot 不在讨论组时不会发送评论（频道媒体仍会发送）。

## 关于 Apple Live Photo 格式

Telegram 的 Live Photo 是「照片 + 视频」两个文件，**不需要** Apple 的 `ContentIdentifier` / `still-image-time` 元数据。

只有要把文件导出到 iPhone 相册、让系统识别为 Apple Live Photo 时，才需要 JPEG+MOV 配对与元数据；`makelive` 是 macOS 专用，Linux 服务器需 `exiftool` / `ffmpeg` / `live-photo-conv`，本服务暂未包含。