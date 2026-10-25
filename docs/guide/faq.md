---
title: 常见问题
description: 抖音转发解析服务常见问题：登录、消息、解析、实况图、检测模式与升级。
outline: deep
---

# 常见问题

## 首次启动后，我转发的视频没被解析？

首次启动只记录「消息水位」，只处理**启动之后**新转发的视频。要处理历史：设置 `DOUYIN_PROCESS_HISTORY=true`，删除 `forwarder_data/state.json` 后重启。

## 收不到消息 / 日志提示「未找到私信会话」？

- 确认大号已给小号发过消息；
- 确认监测昵称匹配（包含匹配），可往 `agent_cmd.txt` 写昵称免重启更换；
- 大号小号互关，把消息移出「陌生人消息」；
- 开 `FORWARDER_DUMP_HTML=true` 查看页面 HTML 并调整选择器。

## 视频卡片提取链接失败？

抖音改版会导致选择器失效。开 `FORWARDER_DUMP_HTML=true` 复现，对照 `forwarder/douyin_web.py` 顶部的 `LINK_PATTERNS` / `MESSAGE_ITEM_SELECTORS` 调整。

## 登录态多久过期？

一般数天到数周。失效时服务会自动重新进入扫码流程。

## 提示「验证码中间页」（滑块）怎么办？

- 在有界面环境执行 `DOUYIN_HEADLESS=false python run.py login`，手动完成滑块后扫码；
- 服务器可用 `xvfb-run -a` 包装；
- 间隔一段时间或更换网络环境重试；
- **不要跨机器复制登录态**。

## 会不会有账号风险？

建议只用小号做接收端，控制频率（默认轮询 20s、解析间隔 8s；probe 更少），不要把服务部署在频繁更换 IP 的环境。

## 日志在哪里？

控制台（stderr）+ `parse_hub_bot/logs/bot.log`。

## 如何完全重置？

- 清解析记录/控制台设置：删除 `forwarder_data/state.json`；
- 清登录态：删除 `forwarder_data/browser_profile`（需重新扫码）；
- 只重置昵称：控制台写 `reset`。

## 为什么依赖清单里没有 tgcrypto？

tgcrypto 仅用于 Telegram 连接加密加速（可选），新版 Python 无预编译轮子。本服务不启动 TG Bot，用不到；如要跑原项目 TG Bot 可单独安装。

## 需要配置原项目的 Telegram Bot 吗？

不需要（除非使用「TG 推送」）。`run.py` 会注入占位配置，不启动 Bot。日志出现 `未找到 platform_config.yaml` 属正常。

## 国内服务器连不上 Telegram？

配置 `BOT_PROXY=http://127.0.0.1:7890`（支持 socks5://），重启服务。海外服务器一般无需代理。

## 同一作品重复转发，会不会重复下载？

不会。消息指纹 + 链接指纹 + 视频 ID 指纹三层去重；缓存清理后记录仍在 `state.json`。

## 不想改 .env / 敲长命令？

用远程控制台：`echo "我的大号" > forwarder_data/agent_cmd.txt` 换昵称；`echo 1` 点登录菜单；`echo "mode probe"` 切检测模式。

## 服务器磁盘会不会被占满？

默认「推送成功即删」+「72 小时 TTL 清理」；不用 TG 又想长期保留可设 `FORWARDER_CACHE_TTL_HOURS=0`。

## 能同时监测多个聊天用户吗？

可以。`DOUYIN_MAIN_ACCOUNTS=小明,小红`，或运行中写 `echo "小明,小红" > forwarder_data/agent_cmd.txt`；`+` 追加、`-` 移除。

## 服务重启后，没下载完的视频会自动补吗？

会。已成功的不会重复；解析到一半被中断的会补处理（日志 `恢复未完成的消息…`）；首轮水位跳过与失败 3 次放弃的不会。

## 实况图为什么还是视频？

三种可能：① 模式仍是 `video`；② 视频超 10 秒/10MB；③ 实况图数量超过 `MAX_ITEMS`（默认 3）。查看启动日志「实况图模式」与推送日志。

## 实况图三种模式怎么选？

- 最稳、聊天最干净：`video`；
- 真正 Live Photo：`live`（旧客户端只显示静态图）；
- 兼容性最好：`both`（消息数约翻倍）。

多图实况建议保留 `MAX_ITEMS=3`。

## 检测模式怎么选？

默认 `reload` 最稳；`probe` 请求量降 95%+，适合长期挂机，最坏延迟约一个强制取数周期（默认 45 秒）。可随时用 `mode` 切换。

## 升级后默认行为会变吗？

不会。检测模式默认 `reload`、实况图模式默认 `video`，新配置项都有默认值，旧 `.env` 不用改。

## 为什么实况图没有相册，是一条条发？

Telegram Bot API 的 `sendMediaGroup` 不支持 Live Photo，只能逐条 `sendLivePhoto`。用 `video` 模式可以回到相册发送。

## 实况图归档为什么有 jpg 和 mp4？

实况图 = 静态图 + 短视频；两者都保留，便于以后重新生成 Live Photo 或单独使用静态图。`metadata.json` 的 `live_photo_count` 记录数量。

## 怎么验证实况图端到端？

1. 用公开实况图作品跑 `python run.py test-url <链接>`，确认解析结果包含 `LivePhotoRef`；
2. 检查归档目录同时出现 `.jpg` 和 `_video.mp4`；
3. 测试频道用 `live` 模式发一条，iOS/Android/Desktop 各看一次；
4. 用 14 张图作品验证 `MAX_ITEMS` 回退。
