---
title: 检测模式
description: reload 与 probe 两种检测模式的原理、参数、切换与验证。
outline: deep
---

# 检测模式

两种模式**解析结果完全一致**（同一个消息接口、同一套去重与解析链路），差别只在「怎么发现新转发」。

| | `reload`（现状，默认） | `probe`（B 方案） |
| --- | --- | --- |
| 检测方式 | 每轮 `page.reload` 整页刷新 + 点开监测会话 | DOM 探针（0 请求）+ 定期强制取数 + 自愈刷新 |
| 请求量（实测） | 约 250 个/轮，20 秒间隔 ≈ 65–108 万/天 | 约 3.6 万/天（↓95%+） |
| 单轮耗时 | 6–32 秒（整页重载） | 0.0 秒（无变化）/ 1–2 秒（取数） |
| 检测延迟 | 20–60 秒 | 正常 ≤10 秒；推送失效时 ≤45 秒 |
| 稳定性 | 最高：每轮全量重同步 | 高：定期取数 + 自愈 reload 兜底 |
| 适用 | 服务器配置充足、要求最稳 | 长期挂机 / 低配服务器 / 想省资源 |

## 工作原理

### reload（现状）

1. 每轮 `page.reload()` 整页刷新；
2. 页面重新请求 `get_message_by_init`（约 176 KB，包含目标会话全部消息）；
3. 逐个点开监测会话，触发 `get_by_conversation`（约 280 KB/会话）；
4. 从接口响应提取 `itemId` / 文本链接。

优点是每轮**全量重同步**，即使页面推送失效也能发现新消息；代价是请求量与 CPU 占用高。

### probe（B 方案）

1. 用一次 `page.evaluate` 读取「会话列表 + 消息区最后一条」生成指纹（**0 网络请求**）；
2. 指纹变化，或到达强制取数周期（默认 45 秒）→ 点开目标会话，用 `expect_response` 精确等待 `get_by_conversation`；
3. 取到数据后走与 reload 完全相同的候选提取 / 去重 / 解析链路；
4. 以下情况自动 reload 自愈：
   - 连续 3 次取数失败；
   - 连续 3 次探针读不到会话列表；
   - 到达自愈周期（默认 2 小时）；
   - 当前页面不在私信页。

首次运行（或删除 `state.json` 后）仍会执行一次 reload 建立消息水位。

## 参数

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DOUYIN_DETECT_MODE` | `reload` | `reload` / `probe` |
| `DOUYIN_PROBE_INTERVAL` | `10` | DOM 探针间隔（秒），只读页面、0 网络请求 |
| `DOUYIN_FORCE_FETCH_INTERVAL` | `45` | 强制取数间隔（秒），兜住推送静默失效 |
| `DOUYIN_RELOAD_INTERVAL` | `7200` | 自愈整页刷新间隔（秒），`0` = 关闭 |
| `DOUYIN_RELOAD_ON_FAILURES` | `3` | 连续取数失败多少次触发自愈 |

## 部署时选择

```bash
sudo bash deploy/configure_detect_mode.sh
# 或非交互：
DETECT_MODE=probe sudo -E bash deploy/install_ubuntu.sh
```

## 运行中免重启切换

```bash
echo "mode probe"  > forwarder_data/agent_cmd.txt   # 切到 B 方案（省资源）
echo "mode reload" > forwarder_data/agent_cmd.txt   # 切回现状（每轮整页刷新）
echo "mode"        > forwarder_data/agent_cmd.txt   # 查看当前模式、参数与说明
echo "mode reset"  > forwarder_data/agent_cmd.txt   # 恢复 .env 默认
```

切换结果持久化到 `forwarder_data/state.json`，优先级高于 `.env`，**下一轮检测立即生效**。

## 验证方法

把 `FORWARDER_DEBUG=true` 打开后观察日志：

```text
检测模式: probe（DOM 探针 + 定期强制取数 + 自愈刷新）；轮询间隔 10s；来源: .env 默认

# 空闲轮次（0 请求）
probe: 页面无变化，跳过本轮（0 请求）
本轮结束（耗时 0.0s，模式 probe），10s 后进入下一轮

# 强制取数轮次
已捕获消息接口数据: https://imapi.douyin.com/v1/message/get_by_conversation（274127 字符）
```

实测：

| 场景 | 结果 |
| --- | --- |
| probe 模式 110 秒 | 1 次导航、1 次强制取数、9 次 0 请求探针；每轮 0.0s |
| reload 模式 70 秒 | 3 轮，每轮整页刷新（`get_message_by_init` 重复 5 次） |
| 运行中切换 | `mode reload` / `mode probe` 下一轮立即生效 |

## 注意事项

- 修改 `.env` 需要重启服务；控制台 `mode` 命令不需要；
- 先用控制台切过模式后，改 `.env` 不会覆盖它（日志会提示「控制台设置，优先于 .env」），需要 `mode reset`；
- probe 模式最坏延迟 = 强制取数周期（默认 45 秒）；
- 两种模式都保留首次全量建水位与失败自愈。
