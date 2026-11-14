---
title: Bot 控制台
description: 用 Telegram 私聊绑定 owner，远程查看服务器状态、重试失败作品、提交链接解析。
outline: deep
---

# Bot 控制台

服务内置一个 **Telegram Bot 控制台**。绑定唯一 owner 后，可以直接在私聊里查看运行状态、服务器资源、失败作品，并让 bot 解析你发来的链接。

控制台与成品推送共用同一个 bot / Pyrogram Client，不需要额外进程。

## 1. 开启并生成绑定码

推荐用部署脚本交互式配置：

```bash
sudo bash deploy/configure_bot_console.sh
```

脚本会：

1. 询问是否开启控制台；
2. 生成一个 64 位十六进制绑定码（`openssl rand -hex 32`）；
3. 写入 `.env`：

```ini
FORWARDER_BOT_CONSOLE=true
FORWARDER_BOT_BIND_CODE=粘贴脚本输出的绑定码
FORWARDER_FAILURE_NOTIFY=true
FORWARDER_SUMMARY_ENABLED=true
```

也可以手动编辑 `.env`，然后重启：

```bash
sudo systemctl restart douyin-forward
```

::: tip 不需要成品频道也能用
即使 `TG_PUSH=false` 或没有配置 `TG_TARGET_CHAT`，只要开启控制台并配置了有效绑定码 / 已绑定 owner，bot 仍会启动，用于私聊命令和失败通知。
:::

## 2. 绑定 owner

在 Telegram 私聊你的 bot，发送：

```text
/start <绑定码>
```

第一个绑定成功的账号成为**唯一 owner**，绑定码随即失效。之后：

- 只有 owner 能执行 `/status`、`/parse`、`/retry` 等命令；
- 其他账号发送命令会被拒绝；发送抖音链接会提示先绑定；
- 失败通知只会私聊发给 owner。

绑定信息保存在 `forwarder_data/state.json`：

| 键 | 含义 |
| --- | --- |
| `owner_user_id` | owner 的 Telegram 数字 ID |
| `owner_username` | owner 用户名（仅展示） |
| `notify_chat_id` | 失败通知接收人（当前等于 owner） |
| `failure_notify_enabled` | 失败通知开关 |
| `bound_at` | 绑定时间 |

::: warning 绑定码有效期
默认 `FORWARDER_BIND_CODE_TTL=900`，**从服务启动时开始计时**。如果服务已经运行超过 15 分钟才去绑定，会提示“绑定码已过期”；重启一次服务即可重新获得有效期，或把该值设为 `0` 关闭过期限制。

绑定码只用于首次绑定。绑定成功后它的指纹会写入 `state.json`，同一个码不能再用于重新绑定；可以把它从 `.env` 删除，owner 状态仍在 `state.json` 中，重启后控制台继续可用。
:::

## 3. 命令一览

| 命令 | 作用 |
| --- | --- |
| `/start <绑定码>` | 首次绑定 owner |
| `/help` | 中文命令帮助（参数、作用、注意事项） |
| `/status` / `/server` | 服务器 + 转发状态（CPU、内存、磁盘、进程、Chromium、模式、队列、最近成功/失败） |
| `/stats [天数]` | 成功 / 重复 / 失败 / 重试 / 推送统计，默认今天，最多 30 天 |
| `/last` | 最近 5 条归档 + 最近 5 条失败 |
| `/failures` | 失败收件箱（优先显示已放弃，最多 10 条） |
| `/retry <编号>` | 重新解析失败编号（清掉放弃/重试状态后入队） |
| `/search <关键词>` | 搜索本地归档（标题/作者/作品 ID/链接） |
| `/parse <链接>` | 让 bot 解析一条抖音链接；也可以直接发链接 |
| `/pause` / `/resume` | 暂停 / 恢复抖音轮询（队列中的任务仍会继续） |
| `/notify on\|off\|test` | 开关失败通知 / 发送测试消息 |
| `/unbind` | 解除 owner 绑定；旧绑定码不会恢复，需换新码并重启才能重新绑定 |

中文命令与英文命令等价，例如 `/状态` `/服务器` `/统计` `/最近` `/失败` `/重试` `/搜索` `/解析` `/暂停` `/继续` `/通知` `/帮助` `/解绑`。

### /status 能看到什么

```text
🖥 服务器
主机：xxx / Linux / Python 3.12.x
运行：3d 4h；服务：active
CPU：2 核，负载 0.42/0.38/0.35，采样 18%
内存：1.8 GiB / 3.7 GiB，可用 1.6 GiB
磁盘：27.0 / 77.0 GiB（35%），可用 50.0 GiB
进程：Python 240 MB / Chromium 560 MB

📡 转发
检测：probe（接口探针）；▶️ 运行中；队列 0 个待解析
轮询：上次检查在 12 秒前（候选 2 个，连续空 0 轮）
最近发现候选：12 秒前；最近解析成功：3 分钟前
最近失败：2 小时前；最近推送：3 分钟前
TG 推送：已启用 → @your_channel
Owner：your_name；失败通知：开
失败收件箱：3 条未结；今日归档：12；浏览器重启：0
```

Linux 下资源数据来自 `/proc`；Windows 本地调试时部分字段会显示 `0` / `n/a`，属正常降级。

## 4. 失败收件箱

作品达到重试上限（永久错误只尝试 1 次）后，owner 会收到一张失败卡片：

```text
❌ 解析失败 #42
分类：地区/版权限制
链接：https://v.douyin.com/xxxx/
错误：copyright_foreign
尝试：1 次
提示：该作品在当前出口地区不可用，重试通常无效
```

卡片按钮：

- **重试**：清除放弃/失败状态并重新入队；
- **忽略**：标记为已忽略，不再出现在未结列表；
- **详情**：弹出完整错误文本。

失败记录同时写入 `forwarder_data/forwarder.db` 的 `failures` 表，可用 `/failures` 和 `/retry` 管理。解析成功后会标记为已解决（`resolved`）。从旧版本升级时，首次启动会把 `state.json` 里的 `gave_up` 记录导入失败收件箱；旧版 `failures` 桶没有保存链接，无法重试，因此不导入。

### 失败分类与重试策略

| 分类 | 典型错误 | 策略 |
| --- | --- | --- |
| 地区/版权限制 | `copyright_foreign` / 地区版权 | 永久，1 次 |
| 审核不通过 | `status_audit_not_pass` | 永久，1 次 |
| 作者权限 | `author_secret` | 永久，1 次 |
| 作品不可用 | 已删除 / 不存在 / private | 永久，1 次 |
| 网络 | 超时 / 连接失败 / 展开短链失败 | 最多 5 次，指数退避 |
| 下载/磁盘 | 下载失败 / 磁盘错误 | 最多 4 次 |
| Telegram | 上传失败 / FloodWait | 最多 3 次 |
| 其他流水线错误 | 默认 | 最多 3 次 |

临时错误按 `FORWARDER_RETRY_BACKOFF_BASE=60` 起步，逐次翻倍，最多 `FORWARDER_RETRY_BACKOFF_MAX=1800` 秒。永久错误不会反复重试，避免浪费解析配额。

## 5. 主动给 bot 发链接

owner 私聊发送抖音链接（或 `/parse 链接`）时，会进入与私信转发相同的解析队列：

- 解析完成后仍在 `TG_TARGET_CHAT` 推送；
- 结果会回到你发链接的那条消息下（成功/失败/重复都会回复）；
- 支持回复一条包含链接的消息再发 `/parse`。

在频道关联讨论组里，owner 发送链接也会触发解析，但需要：

1. `FORWARDER_PARSE_IN_GROUP=true`；
2. bot 在讨论组内，且能收到该消息（群组 privacy mode 下普通链接可能收不到，建议用 `/parse 链接` 或给 bot 管理员权限）。

## 6. 每日摘要与静默告警

- **每日摘要**：每天 `FORWARDER_SUMMARY_HOUR` 点后发送一次 `/stats 1` + `/status`；周一额外发送最近 7 天统计。关闭用 `FORWARDER_SUMMARY_ENABLED=false`。
- **静默告警**：超过 `FORWARDER_SILENT_ALERT_MINUTES` 没有发现任何新候选时提醒 owner（默认 360 分钟，0 = 关闭）。收到提醒后先检查小号登录、私信页是否被风控。
- **磁盘水位**：输出目录可用空间低于 `FORWARDER_MIN_FREE_GB` 时暂停新解析并提醒（每小时最多一次），清理空间后自动恢复。
- **心跳**：配置 `FORWARDER_HEALTHCHECK_URL` 后，服务会定时请求该 URL（healthchecks.io / Uptime Kuma push），用于外部监控。

## 7. 安全建议

- 绑定码是 256 位随机值，不要发到群聊/截图；脚本只显示一次。
- 绑定码不是永久密码：绑定成功后同一码立即失效；如需换人，先 `/unbind`，再更换 `FORWARDER_BOT_BIND_CODE` 并重启服务。
- 控制台只允许一个 owner；其他账号即使知道命令也无法操作。
- 错误文本会截断后展示，避免把 cookie、代理密码、完整路径带进 Telegram。
- `/unbind` 只清除 owner 状态，不会停止服务，也不会让旧绑定码恢复可用；如需重新绑定，换一个新码并重启。彻底关闭控制台设 `FORWARDER_BOT_CONSOLE=false` 并重启。

## 8. 常见问题

**Q：bot 不回复 /start？**

1. 确认 `FORWARDER_BOT_CONSOLE=true` 且配置了 `FORWARDER_BOT_BIND_CODE`（或已绑定 owner）；
2. 确认 `BOT_TOKEN` 正确、服务器能访问 Telegram；
3. 看日志是否有“Bot 控制台已注册”；
4. 如果同时开了成品推送，确认没有另一个进程用同一个 token 抢占更新。

**Q：提示“绑定码已过期”？**

默认从服务启动起 15 分钟有效。重启服务，或把 `FORWARDER_BIND_CODE_TTL` 设为 `0`。

**Q：提示“已绑定 owner，不能重复绑定”？**

当前实例已有 owner。先让原 owner 发送 `/unbind`，然后更换 `FORWARDER_BOT_BIND_CODE` 并重启服务。直接删除 `state.json` 的 `owner_user_id` 等键不会让旧码恢复可用，也需要换码。

**Q：提示“绑定码已被使用”？**

同一个码已经成功绑定过；`/unbind` 或删除 owner 状态都不会让它恢复。请在 `.env` 换一个新码并重启服务。

**Q：bot 不能主动私聊我？**

Telegram 限制 bot 不能给没联系过它的用户发消息。必须先由你私聊 bot 发送 `/start <绑定码>`，建立会话后才能接收失败通知。
