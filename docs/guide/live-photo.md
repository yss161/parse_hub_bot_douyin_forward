---
title: 实况图（Live Photo）
description: 抖音实况图的识别原理、三种推送模式、超限回退与验证方法。
outline: deep
---

# 实况图（Live Photo）

抖音的「实况图」作品 = **静态图 + 一小段视频**（通常 3 秒左右）。本服务能识别这类作品，并按模式决定发到 Telegram 的形态。

## 识别原理

1. 抖音作品详情里，实况图的 `aweme_detail.images[]` 每一项都带 `video` 字段（静态图 + 实况视频）；实测 `aweme_type=68`。
2. parsehub 把它解析为 `LivePhotoRef`（`url` = 静态图，`video_url` = 实况视频）。
3. 下载后得到 `LivePhotoFile`：`path` = jpg，`video_path` = mp4。
4. **旧行为**：原项目 `build_input_media` 把 `LivePhotoFile` 映射成 `InputMediaVideo(media=video_path)`，所以 Telegram 只收到视频——这就是「实况图解析出来直接是视频」的原因；解析层其实已经拿到了图片和视频。
5. 本服务在 `forwarder/tg_push.py` 接管这个分支：按模式发送 `send_live_photo(photo, live_photo)`，或在超限时回退视频。

> 兼容性说明：目前实测抖音实况图走 `images[].video`；如果未来抖音切到 `image_post_info.images[].video`，parsehub 2.3.5/2.3.14 会丢掉视频（只保留 `display_image`），需要再补一层适配。当前版本不受影响。

## 模式行为矩阵

| 模式 | 普通实况图（≤10s/≤10MB） | 超限实况图（>10s 或 >10MB） | 实况图数量 > MAX_ITEMS | 单条发送失败 |
| --- | --- | --- | --- | --- |
| `video`（默认） | 发视频 | 发视频 | 发视频 | — |
| `live` | 发 Live Photo | **改发视频** | **回退视频** | 该条改发视频 |
| `both` | Live Photo + 视频 | **只发视频** | **回退视频** | 该条改发视频 |

## 超限与回退规则

- Telegram 硬限制：`live_photo` 视频 **≤10 秒、≤10MB**；超限**不转码**，直接按普通视频发送（避免服务器依赖 ffmpeg）。
- 实况图数量超过 `FORWARDER_LIVE_PHOTO_MAX_ITEMS`（默认 3）→ 实况图回退为视频，避免 14 张图变成 14 条消息刷屏；设为 `0` 可关闭限制。
- 缺静态图/视频、读取视频时长失败、`send_live_photo` 调用失败 → 该条自动改发视频。
- 所有回退都会在日志中说明原因（`超过 10MB` / `时长 … 超过 10s` / `实况图 … 张 > 上限 …`）。

## 配置

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FORWARDER_LIVE_PHOTO_MODE` | `video` | `video` / `live` / `both` |
| `FORWARDER_LIVE_PHOTO_MAX_SECONDS` | `10` | 实况视频最大秒数，超过改发视频 |
| `FORWARDER_LIVE_PHOTO_MAX_MB` | `10` | 实况视频最大 MB，超过改发视频 |
| `FORWARDER_LIVE_PHOTO_MAX_ITEMS` | `3` | 实况图数量超过该值回退视频；`0` = 不限 |

## 部署时选择

```bash
sudo bash deploy/configure_live_photo.sh
# 或非交互：
LIVE_PHOTO_MODE=both sudo -E bash deploy/install_ubuntu.sh
```

## 运行中免重启切换

```bash
echo "live video" > forwarder_data/agent_cmd.txt   # 只发视频（现状）
echo "live live"  > forwarder_data/agent_cmd.txt   # 只发实况图
echo "live both"  > forwarder_data/agent_cmd.txt   # 实况图 + 视频
echo "live"       > forwarder_data/agent_cmd.txt   # 查看当前模式与限制
echo "live reset" > forwarder_data/agent_cmd.txt   # 恢复 .env 默认
```

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
实况图模式: both（实况图 + 视频）；限制 ≤10s / ≤10MB；实况图超过 3 张回退视频

# 推送日志
实况图推送: 2 张（模式: both，限制 ≤10s / ≤10MB）
已推送至 Telegram: xxx
```

- 单张实况图作品：`live` 模式应收到一条可播放的 Live Photo；
- 14 张实况图作品：默认 `MAX_ITEMS=3`，应回退为视频相册；
- 超 10 秒的视频：日志出现 `时长 … 超过 10s，改发视频`。

## 限制与已知问题

- Telegram 的 Live Photo **不支持相册**（Bot API 的 `sendMediaGroup` 没有 live photo 类型），多条只能逐条发送；
- 旧版 Telegram 客户端可能只显示静态图；
- 实况图视频超过 10 秒/10MB 只能改发视频（本版本不转码）；
- `live` 模式没有视频备份，兼容性要求高时建议选 `both` 或 `video`。

## 关于 Apple Live Photo 格式

Telegram 的 Live Photo 是「照片 + 视频」两个文件，**不需要** Apple 的 `ContentIdentifier` / `still-image-time` 元数据。

只有要把文件导出到 iPhone 相册、让系统识别为 Apple Live Photo 时，才需要 JPEG+MOV 配对与元数据；`makelive` 是 macOS 专用，Linux 服务器需 `exiftool` / `ffmpeg` / `live-photo-conv`，本服务暂未包含。
