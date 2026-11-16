---
title: 项目简介
description: 抖音转发解析服务的定位、功能特性、工作流程与整体架构。
outline: deep
---

# 项目简介

> 仓库：https://github.com/yss161/parse_hub_bot_douyin_forward

本项目基于 [parse_hub_bot](https://github.com/z-mio/parse_hub_bot)，把「抖音私信转发 → 解析 → 下载 → 归档 → Telegram 推送」串成一条常驻服务。

**工作链路**：

```text
【你的抖音大号】                       【服务器上的抖音小号】
  在手机上浏览视频                        浏览器（Playwright）常驻运行
      │                                      │
      │  ① 转发（分享→私信）                  │  ② 检测私信（reload/probe），识别新消息
      ▼                                      ▼
  小号的抖音私信  ────────────────▶  提取视频/图文/实况图链接
                                             │
                                             │  ③ 交给 parse_hub_bot 解析
                                             ▼
                                   解析 → 下载 → 归档到 output/
                                             │
                                             │  ④（可选）推送到指定 TG 群组/频道
                                             ▼
                                   Telegram 频道 / 群组
                                  （解析部分 = 原项目实现，零改动）
```

## 功能特性

| 能力 | 说明 |
| --- | --- |
| 私信自动接收 | Playwright 常驻抖音网页版小号，自动发现大号转发的新消息 |
| 双检测模式 | `reload`（整页刷新，最稳）/ `probe`（DOM 探针 + 定期取数，省资源），运行中可切换 |
| 多会话监测 | 同时监测多个大号/聊天用户，按昵称锁定会话，其他会话的分享一律忽略 |
| 接口优先提取 | 直接读取私信消息接口中的 `itemId`，准确、无需点击视频卡片 |
| 四层去重 | 消息指纹 + 链接指纹 + 视频 ID 指纹 + 内容 SHA-256；换链接/换作品 ID 重复转发也不会重复下载 |
| 实况图支持 | 识别 `LivePhotoFile`，按 `video` / `live` / `both` / `comment` 四种模式推送；超限自动改发视频，`comment` 可把 Live Photo 发到评论区 |
| BGM 归档 | 图片/图文/实况的背景音乐下载后发到对应帖评论区；普通视频默认不发（视频自带声音） |
| 失败兜底 | 最终解析失败时把链接转发到兜底群，识别兜底 bot 出片并接回频道，成功后删除链接 |
| 兜底还原 Live Photo | 兜底 bot 把实况输出成视频时，用 OpenCV 抽第一帧作静态图，还原成 Telegram Live Photo（超限回退视频） |
| Telegram 推送 | 视频带封面流式播放、图文相册、GIF、Live Photo；失败自动降级文档 |
| 归档与元数据 | 原始媒体 + `metadata.json`（标题/作者/链接/媒体类型/实况图数量） |
| 文件控制台 | 写 `agent_cmd.txt` 即可换昵称、切检测/实况图模式、填验证码、点登录菜单，免重启 |
| Bot 控制台 | Telegram 私聊绑定唯一 owner；`/status` 看服务器状态、`/retry` 重试失败、发链接直接解析 |
| 失败收件箱 | 失败分类 + 指数退避 + SQLite 落库；永久错误只试一次，达到上限推送卡片（重试/忽略/详情） |
| 内容级去重 | 消息/链接/视频 ID 之外，再按最大媒体文件 SHA-256 去重，换号重发也不重复推 |
| 自愈与告警 | 浏览器看门狗（无响应/内存/空闲重启）、磁盘水位、静默告警、外部心跳、每日摘要 |
| 登录辅助 | 二维码字符画/base64、编号菜单、页面预览链接 |
| 缓存清理 | 推送成功即删 + 定时 TTL 清理，磁盘不会无限增长 |
| 本地索引 | SQLite 记录归档、失败和每日统计，支持 `/search`、`/stats`、`/last` |
| 部署友好 | 一键 Ubuntu 脚本 + systemd + 交互式配置（账号 / 检测模式 / 实况图 / cookie） |
| 零改动原项目 | `parse_hub_bot` 作为 git 子模块，解析/下载/媒体处理全部复用，未改一行 |

## 工作流程

```text
启动
 ├─ 启动 Chromium（持久化 profile）→ 检查登录态（失效则扫码）
 ├─ 打开私信页
 └─ 进入检测循环
       ├─ reload 模式：page.reload → 重新拉取 get_message_by_init
       └─ probe 模式：DOM 探针（0 请求）→ 变化/到期 → 点会话取 get_by_conversation
             ↓
       从接口响应提取候选（itemId / 文本链接）→ 会话过滤 → 消息/视频去重
             ↓
       ParsePipeline：解析 → 下载 →（可选媒体处理）
             ↓
       archive_result：复制媒体到 output/ + metadata.json
             ↓
       TgPusher：视频 / 图片相册 / GIF / Live Photo（按模式）
             ↓
       BGM：图片/图文/实况 → 对应帖评论区；实况图在后
             ↓
       推送成功后按配置删除本地归档；SQLite 记录归档/消息 ID/统计
             ↓
       最终失败 → 兜底群链接 → 兜底 bot 出片 → 接回频道（实况可还原 Live Photo）
             ↓
       Owner 控制台：/status /stats /last /failures /retry /search；每日摘要/告警
```

各阶段对应代码：

| 阶段 | 代码位置 |
| --- | --- |
| 检测 / 登录 / 控制台 | `forwarder/douyin_web.py` |
| 检测模式与 probe 状态机 | `forwarder/douyin_web.py` 的 `poll_once` / `_poll_probe` |
| 解析桥接 | `forwarder/parser_bridge.py` |
| 归档 | `forwarder/archive.py` |
| 去重 / 水位 | `forwarder/state.py` |
| Telegram 推送 / BGM 评论区 | `forwarder/tg_push.py` |
| BGM 与媒体类型探测 | `forwarder/douyin_meta.py` |
| 兜底群 / 兜底视频 | `forwarder/fallback.py`、`forwarder/video_live.py` |
| 服务主循环 / 队列 / 缓存清理 | `forwarder/main.py` |
| Bot 控制台 / 失败卡片 | `forwarder/control.py` |
| 失败分类 / 退避 | `forwarder/failures.py`、`forwarder/state.py` |
| 本地索引（SQLite） | `forwarder/store.py` |
| 服务器信息 / 看门狗 | `forwarder/sysinfo.py`、`forwarder/douyin_web.py` |
| 配置 | `forwarder/config.py` / `.env.example` |

## 与原项目的关系

| 部分 | 来源 | 说明 |
| --- | --- | --- |
| 解析 / 下载 / 媒体处理 | **parse_hub_bot 原项目** | 通过 `services.ParseService` / `services.ParsePipeline` 直接调用，未修改一行代码 |
| 抖音 Web 接收端 | 本项目 `forwarder/douyin_web.py` | 登录、检测、消息提取、文件控制台 |
| 桥接与归档 | 本项目 `forwarder/parser_bridge.py`、`archive.py` | 在原项目临时目录清理前完成归档 |
| 服务入口 | 本项目 `run.py`、`forwarder/main.py` | 常驻循环、解析队列、失败重试、缓存清理 |
| Bot 控制台 / 失败通知 | 本项目 `forwarder/control.py` | 复用同一个 bot Client，不额外起进程 |
| Telegram 推送 | 复用原项目构建逻辑 | 新增 `forwarder/tg_push.py`，pyrogram 直连发送 |

原项目以 **git 子模块**引入，可通过以下命令验证：

```bash
git submodule status parse_hub_bot
git -C parse_hub_bot status --porcelain
```

## 适合谁

- 想把自己抖音账号看到的视频/图文/实况图自动保存到 Telegram 或服务器；
- 有一台能访问抖音网页版的 Linux/Windows 机器（国内网络最佳）；
- 接受使用**专门的小号**做接收端，并遵守平台协议、控制频率。

## 下一步

- [快速开始](/guide/getting-started) — 安装、登录、启动
- [检测模式](/guide/detection) — reload 与 probe 怎么选
- [实况图](/guide/live-photo) — Live Photo 四种模式
- [背景音乐](/guide/bgm) — BGM 评论区归档
- [失败兜底](/guide/fallback) — 兜底群与 Live Photo 还原
- [Bot 控制台](/guide/console) — 绑定 owner、远程运维
- [完整配置](/guide/configuration) — 所有环境变量