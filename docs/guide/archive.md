---
title: 归档与去重
description: 输出目录结构、metadata 字段、三层去重与状态恢复。
outline: deep
---

# 归档与去重

## 归档目录

每次解析成功后，会在 `FORWARDER_OUTPUT_DIR`（默认 `./output`）下创建独立目录：

```text
output/
└── 20261025_112210_7408859975616957696/
    ├── 001_开心-好天气.jpg
    ├── 001_开心-好天气_video.mp4
    ├── 002_开心-好天气.jpg
    ├── 002_开心-好天气_video.mp4
    ├── ...
    └── metadata.json
```

- 目录名：`<时间戳>_<作品ID>`；
- 普通视频：mp4 + `metadata.json`；
- 图文/图集：多张图片 + `metadata.json`；
- **实况图**：每张同时归档 `xxx.jpg` + `xxx_video.mp4`。

## metadata.json

| 字段 | 说明 |
| --- | --- |
| `platform` / `platform_name` | 平台标识（douyin / 抖音） |
| `type` | 作品类型（video / image / multimedia） |
| `title` / `content` | 标题 / 正文 |
| `author` | 作者昵称 |
| `media_count` | 媒体引用数量（实况图按一张计 1） |
| `live_photo_count` | 实况图数量（普通视频/图集为 0） |
| `source_url` | 私信里提取到的原始链接 |
| `raw_url` | 展开后的真实链接 |
| `video_key` | 作品 ID |
| `media` | 实际归档文件列表（文件名 + 字节数） |
| `archived_at` | 归档时间（带时区） |

> 实况图示例：`media_count=14`、`live_photo_count=14`、`media` 里 28 个文件（14 jpg + 14 mp4）。

## 三层去重

| 层级 | 记录内容 | 作用 |
| --- | --- | --- |
| 消息指纹 | 消息 HTML/文本的 SHA1 | 防止同一条私信重复点击/提取 |
| 链接指纹 | 输入链接 + 展开后的真实链接 | 防止同一链接重复解析 |
| 视频 ID 指纹 | `video/<id>`、`modal_id`、`aweme_id` | 同一作品换链接/重复转发也只处理一次 |

## state.json

状态保存在 `forwarder_data/state.json`（原子写入；损坏时自动改名 `*.corrupt` 并从空状态启动）。

| 桶 | 说明 |
| --- | --- |
| `processed` | 已成功解析/归档的链接与视频 ID |
| `seen_messages` | 已识别过的消息指纹 |
| `settled` | 已有定论（成功/放弃/首轮水位跳过） |
| `failures` | 失败次数与最后一次错误（最多 3 次） |
| `gave_up` | 超过重试次数后放弃的记录 |
| `flags` | 运行标记（如 `history_initialized`） |
| `values` | 控制台设置（昵称、检测模式、实况图模式） |

## 首次水位与中断恢复

- **首次运行**：默认只记录当前消息水位，不处理历史（`DOUYIN_PROCESS_HISTORY=false`）；
- **重启恢复**：已识别但未定论、且不在当前处理中的消息，视为「上次中断」自动补处理；
- **失败重试**：解析失败撤销 `seen_messages`，下一轮重试；3 次失败后写入 `gave_up`。

## 手动重置

```bash
# 只清控制台设置（保留解析记录）
echo "reset" > forwarder_data/agent_cmd.txt
echo "mode reset" > forwarder_data/agent_cmd.txt
echo "live reset" > forwarder_data/agent_cmd.txt

# 清空所有去重记录（会重新处理历史/重复作品，谨慎）
sudo systemctl stop douyin-forward
rm forwarder_data/state.json
sudo systemctl start douyin-forward

# 连登录态一起重置（需要重新扫码）
rm -rf forwarder_data/browser_profile
```

## 缓存清理

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FORWARDER_CLEANUP_AFTER_PUSH` | `true` | 推送成功后删除本地归档目录 |
| `FORWARDER_CACHE_TTL_HOURS` | `72` | 定时清理超过该时长的 output/debug/downloads；`0` = 关闭 |

清理不会丢失去重记录，重复转发仍会跳过。
