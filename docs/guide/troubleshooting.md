---
title: 故障排查
description: 常见故障现象、原因与处理方式。
outline: deep
---

# 故障排查

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| 日志「未检测到有效登录态」 | 登录态过期/被风控 | 重新 `python run.py login`（服务器用 xvfb 有界面） |
| 「验证码中间页」/滑块 | 无头 + 新指纹触发风控 | 用 `DOUYIN_HEADLESS=false` 完成一次登录；不要跨机器复制 profile |
| 「未找到私信会话」 | 昵称不匹配/未互关/消息在陌生人折叠 | 控制台写正确昵称；大号小号互关；把消息移出陌生人消息 |
| 解析失败 | 解析端 cookie 失效/作品受限 | `python run.py test-url <链接>` 验证；检查 `cookie-config show` |
| TG 推送失败 | 凭据错误/机器人无权限/网络不通 | 检查 API_ID/API_HASH/BOT_TOKEN、机器人管理员权限、`BOT_PROXY` |
| 实况图仍是视频 | 模式为 `video`；视频超 10s/10MB；数量 > MAX_ITEMS；客户端不支持 | 查看启动日志与推送日志；调大限制或设 `MAX_ITEMS=0` |
| probe 模式延迟大 | 强制取数周期长/页面卡死 | 调小 `DOUYIN_FORCE_FETCH_INTERVAL`；看是否有自愈 reload 日志 |
| 预览链接打不开 | 8765 未放行/绑定地址 | 安全组放行或 SSH 隧道；检查 `FORWARDER_SHOT_BIND` |
| 磁盘持续增长 | 未开清理/未开推送删除 | 检查 `FORWARDER_CACHE_TTL_HOURS` 与 `FORWARDER_CLEANUP_AFTER_PUSH` |
| `state.json` 损坏 | 异常断电/写入中断 | 服务会自动改名 `*.corrupt` 并从空状态启动；必要时手工删除重建 |
| 日志在哪 | — | 控制台 + `parse_hub_bot/logs/bot.log` |

## 调试开关

```ini
FORWARDER_DEBUG=true        # 调试日志（probe 的「页面无变化」等）
FORWARDER_DUMP_HTML=true    # 提取失败时转储页面 HTML
```

转储目录：`forwarder_data/debug/`。

## 环境自检

```bash
python run.py doctor
```

会检查 Python、Playwright、parsehub、原项目服务层、配置与登录态。

## 还是解决不了？

收集以下信息再排查：

```bash
journalctl -u douyin-forward --since "10 min ago"
python run.py doctor
ls -la forwarder_data/
```
