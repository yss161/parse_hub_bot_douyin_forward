---
title: 背景音乐（BGM）归档
description: 把图片、图文与实况图作品的抖音背景音乐下载后归档到对应 Telegram 帖子的评论区。
outline: deep
---

# 背景音乐（BGM）归档

抖音的图片、图文和实况图作品经常带一首背景音乐。解析层（parsehub）只保留图片/视频引用，不暴露 `aweme_detail.music`；本服务在转发层补一次移动端详情请求，把 BGM 下载下来，发到**对应频道帖的评论区**。

> 普通视频默认不发 BGM：视频本身已经包含声音。确需归档时可打开 `FORWARDER_BGM_ON_VIDEO=true`。

## 工作方式

1. 解析成功、媒体归档完成后，用 `DouyinMobileCrawler` 读取作品详情（每次约 5～7 秒，失败只记日志；普通视频默认跳过这一步）；
2. 从 `aweme_detail.music` 取 `title` / `author` / `duration` / `play_url` / 封面；
3. 等频道帖发出并拿到讨论组评论根消息（与 [实况图评论区](/guide/live-photo#comment-模式-频道-评论区) 同一套映射）；
4. 用 `send_audio` 把 mp3 作为一条评论发送，`reply_to_message_id` 指向该帖评论根；
5. 上传成功后删除临时 mp3（除非 `FORWARDER_BGM_LOCAL_COPY=true`），并把 Telegram 消息位置写回 `metadata.json`。

发送顺序：**BGM 第一条，实况图在后**，避免 BGM 被十几张 Live Photo 埋掉。

## 行为矩阵

| 作品类型 | 默认行为 | 说明 |
| --- | --- | --- |
| 图片 / 图文 | 评论区发 BGM | BGM 不进入频道主帖 |
| 实况图 | 评论区发 BGM + Live Photo | BGM 不混入 Live Photo 视频，保持原声 |
| 普通视频 | 不发 | 视频自带声音；`FORWARDER_BGM_ON_VIDEO=true` 才发 |
| 无 BGM / 下载失败 | 跳过 | 不影响主媒体推送与归档 |
| 频道没有评论区 | 跳过 | 不会把 BGM 补发到频道主帖 |

## 配置

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FORWARDER_BGM_MODE` | `comment` | `comment`：评论区归档；`off`：关闭（也省掉详情请求） |
| `FORWARDER_BGM_ON_VIDEO` | `false` | 普通视频是否也发 BGM |
| `FORWARDER_BGM_LOCAL_COPY` | `false` | 是否保留 mp3 到 `forwarder_data/bgm/`；默认上传后删除 |
| `FORWARDER_BGM_MAX_MB` | `20` | 单个 BGM 最大体积，超过跳过 |
| `FORWARDER_BGM_TIMEOUT` | `30` | 下载超时（秒） |

```ini
# 关闭 BGM 归档
FORWARDER_BGM_MODE=off

# 普通视频也归档 BGM（一般不需要）
FORWARDER_BGM_ON_VIDEO=true

# 服务器空间充足时保留本地副本
FORWARDER_BGM_LOCAL_COPY=true
```

## metadata.json

推送成功后，归档目录的 `metadata.json` 会增加 `music` 字段（推送成功即删时，文件会随归档目录一起删除，但 SQLite 索引与 Telegram 评论仍在）：

```json
{
  "music": {
    "title": "@阿迟迟创作的原声",
    "author": "阿迟迟",
    "duration": 16,
    "music_id": "7527305391303084842",
    "url": "https://.../audio_mpeg/...",
    "cover_url": "https://...",
    "prevent_download": false,
    "file_name": "7527305391303084842.mp3",
    "telegram": {
      "chat_id": -1002334557468,
      "message_id": 2201,
      "link": "https://t.me/c/2334557468/2201"
    }
  }
}
```

失败时会记录 `music.error`，不会影响媒体归档。字段说明见[归档与去重](/guide/archive#metadata-json)。

## 限制

- **BGM 是独立音频消息**，不会和图片相册自动同步播放；Telegram 没有「图片 + 背景音乐」的组合消息；
- 评论区需要频道已开启讨论组、bot 在讨论组内有发言权限；拿不到评论根时只跳过 BGM；
- 抖音返回的 `play_url` 是带签名的临时直链，下载失败时不会重试多次（下一轮重新解析会重新取）；
- 读取详情会让每个图片/实况作品多一次请求、多约 5～7 秒；批量转发时可接受，但不建议把 `FORWARDER_BGM_MODE` 与高频轮询叠加；
- 只有公开可下载的 BGM 能取到；`prevent_download=true` 的作品按抖音返回结果处理（仍可能拿到直链，但不保证）。

## 验证

```bash
journalctl -u douyin-forward -f | grep -E "BGM|DouyinMeta"
```

成功日志：

```text
BGM 已归档到评论区: @阿迟迟创作的原声 - 阿迟迟（消息 2201）
```

跳过/失败日志：

```text
目标频道没有关联讨论组，跳过 BGM 评论区归档
BGM 评论区归档失败（不影响主推送）: ...
读取抖音附加信息失败（不影响主流程）: ...
```
