---
title: 完整配置
description: 抖音转发解析服务全部环境变量参考。
outline: deep
---

# 完整配置

配置文件为仓库根目录 `.env`（可从 `.env.example` 复制）。优先级：

**控制台设置（state.json） > `.env` > 代码默认值**

仅监测账号、检测模式、实况图模式支持运行中覆盖；其余配置以 `.env` 为准。修改 `.env` 后需重启服务；`agent_cmd.txt` / Bot 控制台命令下一轮生效。

## 监测账号

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DOUYIN_MAIN_ACCOUNT` | 空 | 单会话：一个大号昵称（包含匹配） |
| `DOUYIN_MAIN_ACCOUNTS` | 空 | 多会话：多个昵称，逗号/分号/换行分隔；优先于单数形式 |
| `DOUYIN_ONLY_OTHERS` | `true` | 只处理对方（大号）发来的消息 |

## 抖音接收端 / 检测

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DOUYIN_CHAT_URL` | `https://www.douyin.com/chat?isPopup=1` | 私信页地址（抖音改版时才需要改） |
| `DOUYIN_HOME_URL` | `https://www.douyin.com` | 首页地址（登录/回退用） |
| `DOUYIN_HEADLESS` | `true` | 无头模式；首次登录/本机调试建议 `false` |
| `DOUYIN_QR_TIMEOUT` | `300` | 扫码等待秒数 |
| `DOUYIN_CHROMIUM_PATH` | 空 | 指定 Chromium/Chrome 可执行文件 |
| `DOUYIN_POLL_INTERVAL` | `20` | reload 模式轮询间隔（秒） |
| `DOUYIN_MAX_CLICKS` | `3` | DOM 兜底扫描时最多点击几张视频卡片 |
| `DOUYIN_PROCESS_HISTORY` | `false` | 首次运行是否处理历史转发 |
| `DOUYIN_DETECT_MODE` | `reload` | `reload`（整页刷新）/ `probe`（DOM 探针 + 定期取数） |
| `DOUYIN_PROBE_INTERVAL` | `10` | probe：DOM 探针间隔（秒） |
| `DOUYIN_FORCE_FETCH_INTERVAL` | `45` | probe：强制取数间隔（秒） |
| `DOUYIN_RELOAD_INTERVAL` | `7200` | probe：自愈整页刷新间隔（秒），`0` = 关闭 |
| `DOUYIN_RELOAD_ON_FAILURES` | `3` | probe：连续取数失败多少次触发自愈 |

## 实况图（Live Photo）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FORWARDER_LIVE_PHOTO_MODE` | `video` | `video`（只发视频）/ `live`（只发实况图）/ `both`（两者都发）/ `comment`（频道发视频/相册，实况图发评论区） |
| `FORWARDER_LIVE_PHOTO_MAX_SECONDS` | `10` | 实况视频最大秒数，超过改发视频 |
| `FORWARDER_LIVE_PHOTO_MAX_MB` | `10` | 实况视频最大 MB，超过改发视频 |
| `FORWARDER_LIVE_PHOTO_MAX_ITEMS` | `10` | 只对 `live`/`both` 生效：超过该值回退视频；`0` = 不限；`comment` 不生效 |
| `FORWARDER_LIVE_PHOTO_SEND_INTERVAL` | `2.0` | 逐条发送实况图的间隔（秒）；`0` = 不等待；用于降低连续上传触发限流的概率 |

## 解析 / 归档 / 清理

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FORWARDER_OUTPUT_DIR` | `./output` | 归档根目录（相对仓库根或绝对路径） |
| `FORWARDER_PARSE_MIN_INTERVAL` | `8` | 两次解析之间的最小间隔（秒） |
| `FORWARDER_SKIP_MEDIA_PROCESSING` | `true` | 跳过原项目媒体处理，归档原始文件；`false` 需装 ffmpeg |
| `FORWARDER_SAVE_METADATA` | `true` | 归档目录写入 `metadata.json` |
| `FORWARDER_AUTO_SYNC_COOKIE` | `false` | 每次启动把小号 cookie 同步到解析端 |
| `FORWARDER_CLEANUP_AFTER_PUSH` | `true` | TG 推送成功后删除本地归档 |
| `FORWARDER_CACHE_TTL_HOURS` | `72` | 定时清理超过该时长的缓存；`0` = 关闭 |

## Telegram 推送

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `TG_PUSH` | `false` | 推送开关 |
| `TG_TARGET_CHAT` | 空 | `@用户名` / `-100` 数字 ID / t.me 链接 |
| `API_ID` / `API_HASH` | 空 | my.telegram.org 创建应用获得 |
| `BOT_TOKEN` | 空 | @BotFather 创建机器人获得 |
| `BOT_PROXY` | 空 | 国内服务器代理，如 `http://127.0.0.1:7890` |

## Bot 控制台 / Owner

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FORWARDER_BOT_CONSOLE` | `true` | 是否启用 Telegram bot 控制台 |
| `FORWARDER_BOT_BIND_CODE` | 空 | 首次绑定 owner 的密钥；为空时无法绑定 |
| `FORWARDER_BIND_CODE_TTL` | `900` | 绑定码有效期（秒，从服务启动计时）；`0` = 不过期 |
| `FORWARDER_FAILURE_NOTIFY` | `true` | 失败作品达到上限时私聊 owner |
| `FORWARDER_PARSE_IN_GROUP` | `true` | 允许 owner 在关联讨论组发链接解析 |

绑定和命令详见 [Bot 控制台](/guide/console)。

## 失败处理 / 磁盘保护

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FORWARDER_MAX_PARSE_ATTEMPTS` | `3` | 临时错误的最大重试次数；永久错误固定 1 次 |
| `FORWARDER_RETRY_BACKOFF_BASE` | `60` | 首次退避秒数，之后逐次翻倍 |
| `FORWARDER_RETRY_BACKOFF_MAX` | `1800` | 单次退避上限（秒） |
| `FORWARDER_MIN_FREE_GB` | `3.0` | 输出目录可用空间低于该值时暂停解析；`0` = 关闭 |

失败记录写入 `forwarder_data/forwarder.db`，可用 `/failures` 查看、`/retry` 重试。

## 监控 / 摘要 / 看门狗

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FORWARDER_HEALTHCHECK_URL` | 空 | 外部心跳 URL（healthchecks.io / Uptime Kuma push） |
| `FORWARDER_HEALTHCHECK_INTERVAL` | `300` | 心跳间隔（秒） |
| `FORWARDER_HEALTHCHECK_METHOD` | `GET` | 心跳请求方法 |
| `FORWARDER_SILENT_ALERT_MINUTES` | `360` | 多久没有新候选就提醒 owner；`0` = 关闭 |
| `FORWARDER_SUMMARY_ENABLED` | `true` | 每日摘要开关 |
| `FORWARDER_SUMMARY_HOUR` | `22` | 每日摘要触发小时（本地时间 0-23） |
| `FORWARDER_BROWSER_CHECK_INTERVAL` | `300` | 看门狗检查间隔（秒） |
| `FORWARDER_BROWSER_MAX_RSS_MB` | `1800` | Chromium 内存超过该值时重启；`0` = 不按内存 |
| `FORWARDER_BROWSER_RESTART_INTERVAL` | `86400` | 定时重启间隔（秒）；仅在队列空闲且 30 分钟无新候选时执行 |
| `FORWARDER_CONTENT_HASH` | `true` | 按最大媒体文件 SHA-256 做内容级去重 |

## 登录预览 / 调试

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FORWARDER_SHOT_PORT` | `8765` | 只读预览 HTTP 端口；`0` = 关闭 |
| `FORWARDER_SHOT_BIND` | `0.0.0.0` | 监听地址；仅本机/SSH 隧道用 `127.0.0.1` |
| `FORWARDER_SHOT_HOST` | 空 | 链接里显示的对外 IP |
| `FORWARDER_DEBUG` | `false` | 调试日志 |
| `FORWARDER_DUMP_HTML` | `false` | 提取失败时转储 HTML 到 `forwarder_data/debug/` |

## 解析端 Cookie

解析能力来自原项目，抖音解析 cookie 存 `parse_hub_bot/data/config/platform_config.yaml`。三种方式：

| 方式 | 做法 | 适用 |
| --- | --- | --- |
| ① 不填（默认） | 什么都不做 | 公开视频，走移动端接口 |
| ② 自动同步（推荐） | `FORWARDER_AUTO_SYNC_COOKIE=true`，或手动 `python run.py export-cookie` | 更稳、抗风控 |
| ③ 手动填 | `python run.py cookie-config set "a=b; c=d"` | 指定账号 cookie |

> 接收端与解析端是两条独立通道；不填 cookie 也能解析公开视频。

## 示例 .env

```ini
# 监测账号
DOUYIN_MAIN_ACCOUNT=我的大号

# 检测模式（默认 reload；省资源选 probe）
DOUYIN_DETECT_MODE=probe
DOUYIN_PROBE_INTERVAL=10
DOUYIN_FORCE_FETCH_INTERVAL=45

# 实况图（默认 video；想发 Live Photo 选 live / both；频道+评论区选 comment）
FORWARDER_LIVE_PHOTO_MODE=comment
FORWARDER_LIVE_PHOTO_SEND_INTERVAL=2.0

# Telegram 推送
TG_PUSH=true
TG_TARGET_CHAT=@your_channel
API_ID=1234567
API_HASH=xxxxxxxxxxxxxxxxxxxxxxxx
BOT_TOKEN=123456789:AAxxxxxxxxxxxxxx

# Bot 控制台（先私聊 bot 发送 /start <绑定码>）
FORWARDER_BOT_CONSOLE=true
FORWARDER_BOT_BIND_CODE=64位随机十六进制
FORWARDER_FAILURE_NOTIFY=true
```