# 抖音转发解析服务

> 仓库地址：https://github.com/yss161/parse_hub_bot_douyin_forward

基于 [parse_hub_bot](https://github.com/z-mio/parse_hub_bot) 的抖音转发解析服务：支持**视频、图文、实况图（Live Photo）**。

**工作链路**：

```
【你的抖音大号】                       【服务器上的抖音小号】
  在手机上浏览视频                        浏览器（Playwright）常驻运行
      │                                      │
      │  ① 转发（分享→私信）                  │  ② 检测私信（reload/probe），识别新消息
      ▼                                      ▼
  小号的抖音私信  ────────────────▶  提取视频链接（短链/卡片）
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

你在手机上看到想保存的视频，直接「转发」给服务器上登录的小号即可，服务器自动完成解析与下载。

<a id="docs-site"></a>

## 文档站点

在线文档（GitHub Pages）：**https://yss161.github.io/parse_hub_bot_douyin_forward/**

> 注：账号的 `yss161.github.io` 已绑定自定义域名 `blog.mojian.fan`，因此 github.io 链接会跳转到
> `https://blog.mojian.fan/parse_hub_bot_douyin_forward/`；两者内容一致。

本地预览：

```bash
cd docs
pnpm install
pnpm docs:dev
```

文档源码在 [docs/](docs/)，基于 [VitePress](https://vitepress.dev/) 构建；推送 `docs/` 变更会自动通过 GitHub Actions 部署。

---

## 目录

- [文档站点](#docs-site) · [功能特性](#features) · [工作流程](#flow) · [与原项目的关系](#relation) · [目录结构](#layout)
- [环境要求](#requirements) · [快速开始](#quickstart) · [运行模式](#run-modes) · [远程控制台](#console)
- [检测模式](#detect-mode) · [实况图](#live-photo) · [归档与元数据](#archive) · [去重与状态](#dedupe)
- [本地缓存清理](#cache) · [配置说明](#config) · [推送到 Telegram](#tg-push) · [部署到 Ubuntu](#deploy)
- [性能与资源](#performance) · [故障排查速查](#troubleshooting) · [常见问题](#faq)
- [升级与回滚](#upgrade) · [开发与测试](#development) · [重要提示](#notes) · [技术细节](#tech) · [最近更新](#changelog)

<a id="features"></a>

## 功能特性

| 能力 | 说明 |
| --- | --- |
| 私信自动接收 | Playwright 常驻抖音网页版小号，自动发现大号转发的新消息 |
| 双检测模式 | `reload`（整页刷新，最稳）/ `probe`（DOM 探针 + 定期取数，省资源），运行中可切换 |
| 多会话监测 | 同时监测多个大号/聊天用户，按昵称锁定会话，其他会话的分享一律忽略 |
| 接口优先提取 | 直接读取私信消息接口中的 `itemId`，准确、无需点击视频卡片 |
| 三层去重 | 消息指纹 + 链接指纹 + 视频 ID 指纹；换链接重复转发也不会重复下载 |
| 实况图支持 | 识别 `LivePhotoFile`，按 `video` / `live` / `both` 三种模式推送；超限自动改发视频 |
| Telegram 推送 | 视频带封面流式播放、图文相册、GIF、Live Photo；失败自动降级文档 |
| 归档与元数据 | 原始媒体 + `metadata.json`（标题/作者/链接/媒体类型/实况图数量） |
| 远程控制台 | 写 `agent_cmd.txt` 即可换昵称、切检测/实况图模式、填验证码、点登录菜单，免重启 |
| 登录辅助 | 二维码字符画/base64、编号菜单、页面预览链接 |
| 缓存清理 | 推送成功即删 + 定时 TTL 清理，磁盘不会无限增长 |
| 部署友好 | 一键 Ubuntu 脚本 + systemd + 交互式配置（账号 / 检测模式 / 实况图 / cookie） |
| 零改动原项目 | `parse_hub_bot` 作为 git 子模块，解析/下载/媒体处理全部复用，未改一行 |

<a id="flow"></a>

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
       推送成功后按配置删除本地归档；定时清理过期缓存
```

各阶段对应代码：

| 阶段 | 代码位置 |
| --- | --- |
| 检测 / 登录 / 控制台 | [forwarder/douyin_web.py](forwarder/douyin_web.py) |
| 检测模式与 probe 状态机 | [forwarder/douyin_web.py](forwarder/douyin_web.py) 的 `poll_once` / `_poll_probe` |
| 解析桥接 | [forwarder/parser_bridge.py](forwarder/parser_bridge.py) |
| 归档 | [forwarder/archive.py](forwarder/archive.py) |
| 去重 / 水位 | [forwarder/state.py](forwarder/state.py) |
| Telegram 推送 | [forwarder/tg_push.py](forwarder/tg_push.py) |
| 服务主循环 / 缓存清理 | [forwarder/main.py](forwarder/main.py) |
| 配置 | [forwarder/config.py](forwarder/config.py) / [.env.example](.env.example) |


---

<a id="relation"></a>

## 与原项目的关系（解析部分零改动）

| 部分 | 来源 | 说明 |
| --- | --- | --- |
| 解析 / 下载 / 媒体处理 | **parse_hub_bot 原项目** | 通过 `services.ParseService` / `services.ParsePipeline` 直接调用，未修改一行代码 |
| 抖音 Web 接收端（小号收私信） | 本项目 `forwarder/douyin_web.py` | 新增 |
| 桥接与归档 | 本项目 `forwarder/parser_bridge.py`、`archive.py` | 新增 |
| 服务入口 | 本项目 `run.py`、`forwarder/main.py` | 新增 |
| Telegram 推送（可选） | 复用原项目构建逻辑（`build_caption` / `build_input_media`） | 新增 `forwarder/tg_push.py`，pyrogram 直连发送 |

原项目以 **git 子模块**引入（固定在某个上游提交上，一行代码未改），可通过以下命令验证：

```bash
git submodule status parse_hub_bot          # 应显示上游提交（如 88bd2cf...）
git -C parse_hub_bot status --porcelain     # 应无输出（运行后可能多出 __pycache__ 缓存目录，属正常）
```

**同步上游更新**：`git -C parse_hub_bot pull origin main`（或 `git submodule update --remote parse_hub_bot`），
解析能力随之升级，本服务无需改动。

---

<a id="layout"></a>

## 目录结构

```
douyin_download_bot/
├── parse_hub_bot/               # 原项目（git 子模块，未改动）
├── forwarder/                   # 新增：转发链路
│   ├── config.py                #   配置（读 .env / 环境变量）
│   ├── douyin_web.py            #   抖音网页接收端（登录+双检测模式+链接提取+远程控制台）
│   ├── parser_bridge.py         #   桥接原项目解析流水线
│   ├── archive.py               #   归档（复制媒体 + metadata.json）
│   ├── state.py                 #   去重（消息/链接/视频ID）/水位/会话设置值
│   ├── shot_server.py           #   登录截图/菜单的只读 HTTP 服务（日志输出可点击链接）
│   ├── reporter.py              #   日志版状态报告器
│   ├── tg_push.py               #   Telegram 推送（可选，TG_PUSH=true 启用）
│   └── main.py                  #   服务主循环（含缓存清理）
├── run.py                       # 统一入口
├── requirements-forwarder.txt   # 依赖清单
├── .env.example                 # 配置样例（复制为 .env）
├── deploy/                      # Ubuntu 部署（一键脚本 + systemd 模板）
│   ├── install_ubuntu.sh        #   一键部署脚本
│   ├── configure_accounts.sh    #   交互式配置：单会话 / 多会话
│   ├── configure_detect_mode.sh #   交互式配置：检测模式 reload / probe
│   ├── configure_live_photo.sh  #   交互式配置：实况图 video / live / both
│   ├── configure_parser_cookie.sh #  交互式配置：解析端 cookie 方式
│   └── douyin-forward.service   #   systemd 服务模板
├── docs/                        # 文档站点（VitePress，GitHub Pages）
├── .github/workflows/           # GitHub Actions（文档自动部署）
├── DEPLOY_UBUNTU.md             # Ubuntu 服务器部署指南
├── forwarder_data/              # 运行数据（浏览器登录态/状态/二维码，自动创建）
└── output/                      # 解析结果归档（自动创建）
```

---

<a id="requirements"></a>

## 环境要求

- **Python 3.12+**（原项目要求，建议 3.13）
- 一台**能正常访问抖音网页版**的服务器（国内机器最佳；海外 IP 可能触发风控），Linux / Windows 均可
- 建议使用**专门的抖音小号**，并与大号**互相关注**（否则消息可能进入「陌生人消息」折叠，且可能影响私信功能）

---

<a id="quickstart"></a>

## 快速开始

### 0. 获取代码

```bash
# parse_hub_bot 以 git 子模块引入，克隆时建议加 --recursive
git clone --recursive https://github.com/yss161/parse_hub_bot_douyin_forward.git douyin_download_bot
cd douyin_download_bot

# 如果克隆时漏了 --recursive，补执行：
#   git submodule update --init --recursive
```

### 1. 安装依赖

```bash
cd douyin_download_bot

python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate

# 国内网络建议加镜像：-i https://mirrors.cloud.tencent.com/pypi/simple
pip install -r requirements-forwarder.txt

# 安装 Playwright 浏览器（首次必须执行）
playwright install chromium
# Linux 服务器如缺系统依赖：playwright install-deps chromium
# 建议同时安装中文字体，便于查看登录二维码截图：apt-get install -y fonts-noto-cjk
```

> ⚠️ 不要执行 `pip install -e parse_hub_bot`：原项目是 uv 工程、非安装型包结构；
> 本服务通过 `run.py` 直接把 `parse_hub_bot/` 加入模块路径运行，无需安装。

### 2. 配置

```bash
cp .env.example .env
```

编辑 `.env`，选择监测模式（二选一）：

```ini
# 单会话：只监测一个大号
DOUYIN_MAIN_ACCOUNT=你的大号昵称

# 多会话：同时监测多个用户/大号（多个昵称用逗号分隔）
DOUYIN_MAIN_ACCOUNTS=小明,小红
```

> 💡 **Ubuntu 一键部署时脚本会交互式询问「单会话 / 多会话」并代填**（见 [DEPLOY_UBUNTU.md](DEPLOY_UBUNTU.md)）；
> 已经部署好的也可以随时重新选择：`sudo bash deploy/configure_accounts.sh`。
> 运行中还可以完全不改配置——往 `forwarder_data/agent_cmd.txt` 写昵称即可免重启更换（见「远程控制台」）。

其余项均有默认值（详见 [.env.example](.env.example) 内注释）。

### 3. 登录小号（首次）

```bash
# 推荐：在【有桌面的机器】上用有界面模式登录（首次必须，见下方说明）
DOUYIN_HEADLESS=false python run.py login     # Windows PowerShell: $env:DOUYIN_HEADLESS="false"; python run.py login
```

- 脚本会打开抖音网页版，**登录二维码弹窗会自动出现**，脚本自动截取二维码保存到 `forwarder_data/login_qr.png`；
- 二维码会**直接以字符画打印在日志里**（深色背景终端可直接扫）并同时输出 base64 编码——SSH 会话里无需下载图片；
- 用**小号**的抖音 App 扫码；如果浏览器窗口可见，直接对着屏幕上的二维码扫也可以；
- 登录成功后登录态会持久化到 `forwarder_data/browser_profile/`，之后重启免扫码。

> ⚠️ **为什么首次要有界面？** 实测（2026-10）抖音对"无头浏览器 + 全新指纹"会返回「验证码中间页」（滑块验证）。
> 有界面模式下页面正常，登录一次后 profile 即为"可信"状态，后续可以切回无头模式（`DOUYIN_HEADLESS=true`）在服务器上长期运行。
>
> **服务器无桌面的部署方式**：在服务器上用 xvfb 以"有界面"方式扫码
> （`apt-get install -y xvfb`，然后 `DOUYIN_HEADLESS=false xvfb-run -a python run.py login`，二维码自动截图到 `forwarder_data/login_qr.png`）。
>
> ❌ **不要从其他电脑复制登录态**：浏览器 Cookie 按操作系统绑定加密（Windows 用 DPAPI），跨机器/跨系统复制后**无法解密**，
> 登录态会"静默失效"（日志表现为「未检测到有效登录态」）。**必须先在服务器本机登录一次**。

### 4. 启动服务

```bash
python run.py serve
```

服务会常驻轮询小号的私信。首次启动会**记录当前消息水位**（跳过历史消息，只处理今后新转发的；如需处理历史，见下文配置）。

### 5. 使用

用**大号**在抖音里把想保存的视频「转发 / 分享 → 私信」发给小号。稍等片刻（取决于轮询间隔、解析和下载耗时）：

- 控制台 / 日志（`parse_hub_bot/logs/bot.log`）会打印全过程；
- 解析结果归档到 `output/<时间戳>_<视频ID>/`，包含视频文件 + `metadata.json`（标题、作者、链接等）。

---

<a id="run-modes"></a>

## 运行模式

| 命令 | 作用 |
| --- | --- |
| `python run.py serve` | 启动常驻服务（正常使用） |
| `python run.py serve --once` | 只执行一轮轮询后退出（调试登录/收信） |
| `python run.py login` | 仅扫码登录小号后退出 |
| `python run.py test-url <链接>` | 不启动浏览器，直接验证解析链路是否可用（排障用） |
| `python run.py export-cookie` | 从小号登录态自动导出抖音 cookie 并同步到解析端（即时生效） |
| `python run.py cookie-config <set/clear/from-file/show>` | 手动管理解析端抖音 cookie |

排障示例：

```bash
python run.py test-url "https://v.douyin.com/xxxxxxx/"
# 解析端 cookie 管理示例
python run.py cookie-config show                       # 查看当前（值已掩码）
python run.py cookie-config set "a=b; c=d"             # 手动设置一条
python run.py cookie-config from-file cookie.txt       # 从文件导入（每行一条）
python run.py cookie-config clear                      # 清空（改走移动端接口）
```

---

<a id="console"></a>

## 远程控制台（登录操作 / 更换监测昵称）

服务运行期间（登录等待、日常轮询），**把一句话写入 `forwarder_data/agent_cmd.txt`** 即可与它「对话」；
文件会被服务读取后自动删除，可一次写多行（每行一条，按顺序执行）。

```bash
# 示例：把「我的大号」设为监测对象（免编辑 .env、免重启，立即生效）
echo "我的大号" > forwarder_data/agent_cmd.txt          # 服务器上: /opt/douyin_download_bot/forwarder_data/agent_cmd.txt

# 同时监测多个用户（逗号分隔）；+ 追加、- 移除：
echo "小明,小红" > forwarder_data/agent_cmd.txt    # 同时监测 小明 和 小红
echo "+小刚" > forwarder_data/agent_cmd.txt       # 再追加 小刚（现在监测 3 个）
echo "-小红" > forwarder_data/agent_cmd.txt       # 移除 小红
```

| 输入 | 作用 |
| --- | --- |
| 任意文本（如 `我的大号`） | 设为**监测账号**；多个用逗号分隔（如 `小明,小红`）→ 同时监测多个聊天用户（免指令、免重启；持久化到 `state.json`） |
| `+昵称` / `-昵称` | **追加 / 移除**一个（或逗号分隔多个）监测账号 |
| `reset` / `重置` | 清除自定义账号，恢复 `.env` 的 `DOUYIN_MAIN_ACCOUNT` / `DOUYIN_MAIN_ACCOUNTS` |
| `1` ~ `9` | **登录操作菜单**编号（登录/验证页面出现，见下） |
| `123456` | 登录流程中自动填入验证码；`123456 go` = 填入并回车提交 |
| `menu` / `菜单` | 重新输出操作菜单 / 使用说明（含当前监测账号列表） |
| `force` / `立即` | 不等轮询周期，立刻执行下一轮检查 |
| `mode reload` / `mode probe` / `mode reset` | 切换**检测模式**（免重启，下一轮生效）；不带参数显示当前模式、参数与说明 |
| `live video` / `live live` / `live both` / `live reset` | 切换**实况图模式**（免重启，下一轮生效）；不带参数显示当前模式与限制 |

### 登录操作菜单（回复数字即可点击）

需要人工操作时（扫码、身份验证、短信验证码），服务会**自动扫描页面上的按钮并生成编号菜单**，
同时写进日志与 `forwarder_data/login_menu.txt`，例如：

```
【登录操作菜单】当前页面: https://...
  回复 1  → 点击「接收短信验证码」（⏱ 官方限制约 60 秒一次；验证码有效期约 5 分钟）
  回复 2  → 点击「使用密码验证」
  回复 3  → 刷新页面截图并重新输出菜单
  ...
```

此时只需：

```bash
echo 1 > forwarder_data/agent_cmd.txt            # 回复 1 = 点击「接收短信验证码」
echo 123456 > forwarder_data/agent_cmd.txt       # 收到短信后：自动填入验证码
echo "123456 go" > forwarder_data/agent_cmd.txt  # 填入并回车提交（一条搞定）
```

> ⏱ **刷新时间提醒**：二维码约 **1 分钟**过期（服务每 25 秒自动刷新截图，并在日志重新输出最新链接）；
> 短信验证码官方限制约 **60 秒**一次、有效期约 **5 分钟**——连续获取时服务会提示冷却剩余秒数，请勿频繁重发。

### 登录预览链接（截图/二维码直接用浏览器看）

服务内置一个**只读** HTTP 服务（默认端口 `8765`），把登录二维码、页面预览截图、菜单文本变成日志里的**可点击链接**：

```
【登录预览链接】
  二维码   : http://<服务器IP>:8765/login_qr.png?t=1729578123
  页面预览 : http://<服务器IP>:8765/login_page_state.png?t=1729578123
```

- 链接带时间戳，**每次自动刷新都会在日志重新输出最新链接**——扫码前请使用最新的一条；
- 打开根地址 `http://<服务器IP>:8765/` 是一个每 15 秒自动刷新的预览页，无需反复复制链接；
- 打不开链接时：① 在云控制台安全组放行 `8765` 端口（建议只放行你自己的 IP）；② 或本地执行
  `ssh -L 8765:127.0.0.1:8765 ubuntu@<服务器IP>` 后访问 `http://127.0.0.1:8765/`；
  ③ 仍可用 `scp` 下载 `forwarder_data/login_qr.png`；
- 安全说明：该服务**只暴露白名单文件**（登录二维码/页面截图/菜单），不含任何登录态文件；
  但仍建议限制访问来源，或登录完成后以 `FORWARDER_SHOT_PORT=0` 关闭。

<a id="detect-mode"></a>

## 检测模式（reload / probe）

两种模式**解析结果完全一致**（同一个消息接口、同一套去重与解析链路），差别只在「怎么发现新转发」：

| | `reload`（现状，默认） | `probe`（B 方案） |
| --- | --- | --- |
| 检测方式 | 每轮 `page.reload` 整页刷新 + 点开监测会话 | DOM 探针（0 请求）+ 定期强制取数 + 自愈刷新 |
| 请求量（实测） | 约 250 个/轮，20 秒间隔 ≈ 65–108 万/天 | 约 2–4 万/天（↓95%+） |
| 单轮耗时 | 32–62 秒（整页重载） | 0.05 秒（无变化）/ 1–2 秒（取数） |
| 检测延迟 | 20–60 秒 | 正常 ≤10 秒；推送失效时 ≤45 秒（兜底） |
| 稳定性 | 最高：每轮全量重同步 | 高：靠定期取数 + 自愈 reload 兜底 |
| 适用 | 服务器配置充足、要求最稳 | 长期挂机 / 低配服务器 / 想省资源 |

### 工作原理

**reload（现状）**

1. 每轮 `page.reload()` 整页刷新；
2. 页面重新请求 `get_message_by_init`（实测约 176 KB，包含目标会话的全部消息）；
3. 逐个点开监测会话，触发 `get_by_conversation`（实测约 280 KB/会话）；
4. 从接口响应中提取 `itemId` / 文本链接。

优点是每轮**全量重同步**，即使页面推送失效也能发现新消息；代价是请求量与 CPU 占用高。

**probe（B 方案）**

1. 用一次 `page.evaluate` 读取「会话列表 + 消息区最后一条」生成指纹（**0 网络请求**）；
2. 指纹变化，或到达强制取数周期（默认 45 秒）→ 点开目标会话，用 `expect_response` 精确等待 `get_by_conversation`；
3. 取到数据后，走与 reload 完全相同的候选提取 / 去重 / 解析链路；
4. 以下情况自动触发一次整页 reload 自愈：
   - 连续 3 次取数失败；
   - 连续 3 次探针读不到会话列表（页面可能卡死）；
   - 到达自愈周期（默认 2 小时）；
   - 当前页面不在私信页。

首次运行（或删除 `state.json` 后）仍会执行一次 reload 建立消息水位，避免漏掉启动前的历史。

### 怎么选

- **默认 `reload`**：行为与旧版完全一致，升级后什么都不改也不会变；
- **想省资源选 `probe`**：请求量降 95%+，正常消息反而更快（探针 10 秒一次），代价是「推送彻底失效」时最坏延迟约 45 秒；
- 两种模式都保留**首次运行全量建水位**与**失败自愈**，不会因为省资源而漏掉启动前的历史。

### 部署时选择

```bash
sudo bash deploy/configure_detect_mode.sh
# 或非交互：
DETECT_MODE=probe sudo -E bash deploy/install_ubuntu.sh
```

### 运行中免重启切换

```bash
echo "mode probe"  > forwarder_data/agent_cmd.txt   # 切到 B 方案（省资源）
echo "mode reload" > forwarder_data/agent_cmd.txt   # 切回现状（每轮整页刷新）
echo "mode"        > forwarder_data/agent_cmd.txt   # 查看当前模式、参数与说明
echo "mode reset"  > forwarder_data/agent_cmd.txt   # 恢复 .env 默认
```

切换结果持久化到 `forwarder_data/state.json`，优先级高于 `.env`，**下一轮检测立即生效，无需重启服务**。

### probe 模式参数（.env）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DOUYIN_PROBE_INTERVAL` | `10` | DOM 探针间隔（秒），只读页面、0 网络请求 |
| `DOUYIN_FORCE_FETCH_INTERVAL` | `45` | 强制取数间隔（秒），兜住「推送静默失效」 |
| `DOUYIN_RELOAD_INTERVAL` | `7200` | 自愈整页刷新间隔（秒），`0` = 关闭 |
| `DOUYIN_RELOAD_ON_FAILURES` | `3` | 连续取数失败多少次触发自愈刷新 |

### 验证方法

把 `FORWARDER_DEBUG=true` 打开后，观察日志：

```text
检测模式: probe（DOM 探针 + 定期强制取数 + 自愈刷新）；轮询间隔 10s；来源: .env 默认

# 空闲轮次（0 请求）
probe: 页面无变化，跳过本轮（0 请求）
本轮结束（耗时 0.0s，模式 probe），10s 后进入下一轮

# 强制取数轮次
已捕获消息接口数据: https://imapi.douyin.com/v1/message/get_by_conversation（274127 字符）
```

实测（本机真实账号）：

| 场景 | 结果 |
| --- | --- |
| probe 模式 110 秒 | 1 次导航、1 次强制取数、9 次 0 请求探针；每轮 0.0s |
| reload 模式 70 秒 | 3 轮，每轮整页刷新（`get_message_by_init` 重复 5 次） |
| 运行中切换 | `mode reload` / `mode probe` 下一轮立即生效，无需重启 |

### 注意事项

- 修改 `.env` 里的 `DOUYIN_DETECT_MODE` 需要重启服务；控制台 `mode` 命令不需要；
- 如果先用控制台切过模式，之后改 `.env` 不会覆盖它（日志会提示「控制台设置，优先于 .env」），需要 `mode reset`；
- probe 模式的最坏延迟 = 强制取数周期（默认 45 秒）；对实时性要求极高可调小 `DOUYIN_FORCE_FETCH_INTERVAL`；
- 两种模式都保留首次运行全量建水位与失败自愈，不会因为省资源而漏掉启动前的历史。

<a id="live-photo"></a>

## 实况图（Live Photo）

抖音的「实况图」作品 = **静态图 + 一小段视频**（通常 3 秒左右）。本服务能识别这类作品，并按模式决定发到 Telegram 的形态。

### 识别原理

1. 抖音作品详情里，实况图的 `aweme_detail.images[]` 每一项都带 `video` 字段（静态图 + 实况视频）；实测 `aweme_type=68`。
2. parsehub 把它解析为 `LivePhotoRef`（`url` = 静态图，`video_url` = 实况视频）。
3. 下载后得到 `LivePhotoFile`：`path` = jpg，`video_path` = mp4。
4. **旧行为**：原项目 `build_input_media` 把 `LivePhotoFile` 映射成 `InputMediaVideo(media=video_path)`，所以 Telegram 只收到视频——这就是「实况图解析出来直接是视频」的原因；解析层其实已经拿到了图片和视频。
5. 本服务在 [forwarder/tg_push.py](forwarder/tg_push.py) 接管这个分支：按模式发送 `send_live_photo(photo, live_photo)`，或在超限时回退视频。

> 兼容性说明：目前实测的抖音实况图走 `images[].video`；如果未来抖音切到 `image_post_info.images[].video`，parsehub 2.3.5/2.3.14 会丢掉视频（只保留 `display_image`），需要再补一层适配。当前版本不受影响。

### 模式行为矩阵

| 模式 | 普通实况图（≤10s/≤10MB） | 超限实况图（>10s 或 >10MB） | 实况图数量 > MAX_ITEMS | 单条发送失败 |
| --- | --- | --- | --- | --- |
| `video`（默认） | 发视频 | 发视频 | 发视频 | — |
| `live` | 发 Live Photo | **改发视频** | **实况图回退视频** | 该条改发视频 |
| `both` | Live Photo + 视频 | **只发视频** | **实况图回退视频** | 该条改发视频 |

### 超限与回退规则

- Telegram 硬限制：`live_photo` 视频 **≤10 秒、≤10MB**；超限**不转码**，直接按普通视频发送（避免服务器依赖 ffmpeg）。
- 实况图数量超过 `FORWARDER_LIVE_PHOTO_MAX_ITEMS`（默认 3）→ 实况图回退为视频（普通图片仍按图集），避免 14 张图变成 14 条消息刷屏；设为 `0` 可关闭该限制。
- 缺静态图/视频、读取视频时长失败、`send_live_photo` 调用失败 → 该条自动改发视频，不影响其他媒体。
- 所有回退都会在日志中说明原因（`超过 10MB` / `时长 … 超过 10s` / `实况图 … 张 > 上限 …`）。

### 部署时选择

```bash
sudo bash deploy/configure_live_photo.sh
# 或非交互：
LIVE_PHOTO_MODE=both sudo -E bash deploy/install_ubuntu.sh
```

### 运行中免重启切换

```bash
echo "live video" > forwarder_data/agent_cmd.txt   # 只发视频（现状）
echo "live live"  > forwarder_data/agent_cmd.txt   # 只发实况图
echo "live both"  > forwarder_data/agent_cmd.txt   # 实况图 + 视频
echo "live"       > forwarder_data/agent_cmd.txt   # 查看当前模式与限制
echo "live reset" > forwarder_data/agent_cmd.txt   # 恢复 .env 默认
```

### 配置项

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FORWARDER_LIVE_PHOTO_MODE` | `video` | `video` / `live` / `both` |
| `FORWARDER_LIVE_PHOTO_MAX_SECONDS` | `10` | 实况视频最大秒数，超过改发视频 |
| `FORWARDER_LIVE_PHOTO_MAX_MB` | `10` | 实况视频最大 MB，超过改发视频 |
| `FORWARDER_LIVE_PHOTO_MAX_ITEMS` | `3` | 实况图数量超过该值时实况图回退视频；`0` = 不限 |

### 验证方法

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

### 限制与已知问题

- Telegram 的 Live Photo **不支持相册**（Bot API 的 `sendMediaGroup` 没有 live photo 类型），多条只能逐条发送；
- 旧版 Telegram 客户端可能只显示静态图；
- 实况图视频超过 10 秒/10MB 只能改发视频（本版本不转码）；
- 归档会同时保存 jpg + mp4，`metadata.json` 的 `live_photo_count` 记录实况图数量（见[归档与元数据](#archive)）；
- `live` 模式没有视频备份，兼容性要求高时建议选 `both` 或 `video`。

### 关于 Apple Live Photo 格式

Telegram 的 Live Photo 是「照片 + 视频」两个文件，**不需要** Apple 的 `ContentIdentifier` / `still-image-time` 元数据。
只有要把文件导出到 iPhone 相册、让系统识别为 Apple Live Photo 时，才需要 JPEG+MOV 配对与元数据；`makelive` 是 macOS 专用，Linux 服务器需 `exiftool` / `ffmpeg` / `live-photo-conv`，本服务暂未包含。


<a id="archive"></a>

## 归档与元数据

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

- 目录名：`<时间戳>_<作品ID>`（短链会先展开为 `/video/` 或 `/note/` 链接）；
- 普通视频：媒体文件（mp4）+ `metadata.json`；
- 图文/图集：多张图片（按 `001_`、`002_` 顺序）+ `metadata.json`；
- **实况图**：每张实况图同时归档 `xxx.jpg` + `xxx_video.mp4`（共 2 个文件），不会只留静态图。

`metadata.json` 字段：

| 字段 | 说明 |
| --- | --- |
| `platform` / `platform_name` | 平台标识（douyin / 抖音） |
| `type` | 作品类型（video / image / multimedia） |
| `title` / `content` | 标题 / 正文 |
| `author` | 作者昵称 |
| `media_count` | 媒体引用数量（实况图按「一张实况图」计 1） |
| `live_photo_count` | 实况图数量（普通视频/图集为 0） |
| `source_url` | 私信里提取到的原始链接（可能是短链） |
| `raw_url` | 展开后的真实链接 |
| `video_key` | 作品 ID（用于目录命名/排查） |
| `media` | 实际归档文件列表（文件名 + 字节数） |
| `archived_at` | 归档时间（带时区） |

> 实况图示例：`media_count=14`、`live_photo_count=14`、`media` 里有 **28** 个文件（14 jpg + 14 mp4）。

---

<a id="dedupe"></a>

## 去重与状态

去重状态保存在 `forwarder_data/state.json`（原子写入；损坏时自动改名为 `*.corrupt` 并从空状态开始）。

### 三层去重

| 层级 | 记录内容 | 作用 |
| --- | --- | --- |
| 消息指纹 | 消息 HTML/文本的 SHA1 | 防止同一条私信被重复点击/重复提取 |
| 链接指纹 | 输入链接 + 展开后的真实链接 | 防止同一链接被重复解析 |
| 视频 ID 指纹 | `video/<id>`、`modal_id`、`aweme_id` 中的作品 ID | 同一作品换短链/长链/重复转发也只处理一次 |

### 状态桶

| 桶 | 说明 |
| --- | --- |
| `processed` | 已成功解析/归档的链接与视频 ID |
| `seen_messages` | 已识别过的消息指纹 |
| `settled` | 已有定论（成功/放弃/首轮水位跳过），不再重试 |
| `failures` | 失败次数与最后一次错误（最多重试 3 次） |
| `gave_up` | 超过重试次数后放弃的记录 |
| `flags` | 运行标记（如 `history_initialized`） |
| `values` | 控制台设置（监测昵称、检测模式、实况图模式） |

### 首次水位与中断恢复

- **首次运行**：默认只记录当前消息水位，不处理历史消息（`DOUYIN_PROCESS_HISTORY=false`）；如需处理历史，设为 `true` 并删除 `state.json` 后重启。
- **重启恢复**：已识别但未定论、且不在当前处理中的消息，会被视为「上次中断」并自动补处理；日志出现 `恢复未完成的消息…`。
- **失败重试**：解析失败会撤销 `seen_messages` 标记，下一轮重试；累计 3 次失败后写入 `gave_up` 并停止重试。

### 手动重置

```bash
# 只清控制台设置的昵称/模式（保留解析记录）
echo "reset" > forwarder_data/agent_cmd.txt      # 昵称
echo "mode reset" > forwarder_data/agent_cmd.txt # 检测模式
echo "live reset" > forwarder_data/agent_cmd.txt # 实况图模式

# 清空所有去重记录（会重新处理历史/重复作品，谨慎）
sudo systemctl stop douyin-forward
rm forwarder_data/state.json
sudo systemctl start douyin-forward

# 连登录态一起重置（需要重新扫码）
rm -rf forwarder_data/browser_profile
```

---

<a id="cache"></a>

## 本地缓存清理

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FORWARDER_CLEANUP_AFTER_PUSH` | `true` | **推送成功后自动删除本地归档目录**（媒体已到 Telegram，无需保留副本） |
| `FORWARDER_CACHE_TTL_HOURS` | `72` | 定时清理：删除超过该时长的 `output/` 归档、`debug/` 转储、`downloads/` 残留；`0` 关闭 |

- 只删除"最后修改时间超过保留时长"的内容（正在处理的不受影响）；每小时检查一次，启动时也会先清理一次；
- 清理**不会**丢失去重记录（`state.json` 保存了视频 ID 指纹）：重复转发的作品照常跳过，不会重新下载。

---

<a id="config"></a>

## 配置说明

完整配置见 [.env.example](.env.example)，常用项：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DOUYIN_MAIN_ACCOUNT` | 空 | **单会话**：只监测一个大号（昵称包含匹配）；**匹配到的会话会被严格锁定**——只解析这些会话内的新转发，其他会话/群聊的分享会被忽略 |
| `DOUYIN_MAIN_ACCOUNTS` | 空 | **多会话**：同时监测多个用户/大号，昵称用逗号分隔（如 `小明,小红`）；设置后优先于 `DOUYIN_MAIN_ACCOUNT`。两者都留空则自动选「有未读角标/第一条」会话。**也可以不改这里**——运行中直接往控制台写昵称即可（见「远程控制台」） |
| `DOUYIN_POLL_INTERVAL` | `20` | 私信轮询间隔（秒）；`reload` 模式使用 |
| `DOUYIN_DETECT_MODE` | `reload` | 检测模式：`reload`（现状，整页刷新）/ `probe`（B 方案，省资源）；运行中可用控制台 `mode` 免重启切换 |
| `FORWARDER_LIVE_PHOTO_MODE` | `video` | 实况图模式：`video`（只发视频）/ `live`（只发实况图）/ `both`（两者都发）；超限自动改发视频 |
| `FORWARDER_LIVE_PHOTO_MAX_ITEMS` | `3` | 实况图数量超过该值时实况图回退视频（防刷屏）；`0` = 不限 |
| `FORWARDER_PARSE_MIN_INTERVAL` | `8` | 两次解析之间的最小间隔（秒），防频繁请求 |
| `FORWARDER_OUTPUT_DIR` | `./output` | 归档目录 |
| `DOUYIN_PROCESS_HISTORY` | `false` | 首次运行是否处理历史转发 |
| `FORWARDER_SKIP_MEDIA_PROCESSING` | `true` | 跳过原项目的媒体处理（转码等），归档原始文件；设 `false` 需装 ffmpeg |
| `TG_PUSH` | `false` | 是否推送解析结果到 Telegram 群组/频道（见下文专节） |
| `TG_TARGET_CHAT` | 空 | 推送目标：`@用户名`／`-100...` 数字 ID／t.me 链接 |
| `FORWARDER_SAVE_METADATA` | `true` | 归档目录写入 `metadata.json` |
| `FORWARDER_AUTO_SYNC_COOKIE` | `false` | 每次启动（小号登录后）自动把小号 cookie 同步到解析端（手动同步一次：`python run.py export-cookie`） |
| `FORWARDER_CLEANUP_AFTER_PUSH` | `true` | 推送成功后删除本地归档目录 |
| `FORWARDER_CACHE_TTL_HOURS` | `72` | 本地缓存保留时长（小时）；`0` = 关闭定时清理 |
| `FORWARDER_SHOT_PORT` | `8765` | 登录预览 HTTP 端口（0 = 关闭；截图/菜单查看用） |
| `FORWARDER_SHOT_HOST` | 空 | 预览链接里显示的对外 IP（留空自动探测；AWS 等 NAT 实例建议显式指定公网 IP） |
| `DOUYIN_HEADLESS` | `true` | 无头模式；本机调试可设 `false` 观察浏览器 |
| `FORWARDER_DUMP_HTML` | `false` | 提取失败时转储页面 HTML 到 `forwarder_data/debug/`（调选择器用） |

### 完整配置参考

> 优先级：控制台设置（`state.json`）> `.env` > 代码默认值。修改 `.env` 后需重启服务；控制台命令下一轮生效。

#### 监测账号

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DOUYIN_MAIN_ACCOUNT` | 空 | 单会话：一个大号昵称（包含匹配） |
| `DOUYIN_MAIN_ACCOUNTS` | 空 | 多会话：多个昵称，逗号/分号/换行分隔；优先于单数形式 |
| `DOUYIN_ONLY_OTHERS` | `true` | 只处理对方（大号）发来的消息，忽略小号自己发的 |

#### 抖音接收端 / 检测

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DOUYIN_CHAT_URL` | `https://www.douyin.com/chat?isPopup=1` | 私信页地址（抖音改版时才需要改） |
| `DOUYIN_HOME_URL` | `https://www.douyin.com` | 首页地址（登录/回退用） |
| `DOUYIN_HEADLESS` | `true` | 无头模式；首次登录/本机调试建议 `false` |
| `DOUYIN_QR_TIMEOUT` | `300` | 扫码等待秒数（超时自动重开窗口滚动重试） |
| `DOUYIN_CHROMIUM_PATH` | 空 | 指定 Chromium/Chrome 可执行文件（默认用 Playwright 自带） |
| `DOUYIN_POLL_INTERVAL` | `20` | reload 模式的轮询间隔（秒） |
| `DOUYIN_MAX_CLICKS` | `3` | 每轮 DOM 兜底扫描时最多点击几张视频卡片 |
| `DOUYIN_PROCESS_HISTORY` | `false` | 首次运行是否处理历史转发（改 true 前先删 `state.json`） |
| `DOUYIN_DETECT_MODE` | `reload` | `reload`（整页刷新）/ `probe`（DOM 探针 + 定期取数） |
| `DOUYIN_PROBE_INTERVAL` | `10` | probe：DOM 探针间隔（秒），0 网络请求 |
| `DOUYIN_FORCE_FETCH_INTERVAL` | `45` | probe：强制取数间隔（秒），兜住推送静默失效 |
| `DOUYIN_RELOAD_INTERVAL` | `7200` | probe：自愈整页刷新间隔（秒），`0` = 关闭 |
| `DOUYIN_RELOAD_ON_FAILURES` | `3` | probe：连续取数失败多少次触发自愈 |

#### 实况图（Live Photo）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FORWARDER_LIVE_PHOTO_MODE` | `video` | `video`（只发视频）/ `live`（只发实况图）/ `both`（两者都发） |
| `FORWARDER_LIVE_PHOTO_MAX_SECONDS` | `10` | 实况视频最大秒数，超过改发视频 |
| `FORWARDER_LIVE_PHOTO_MAX_MB` | `10` | 实况视频最大 MB，超过改发视频 |
| `FORWARDER_LIVE_PHOTO_MAX_ITEMS` | `3` | 实况图数量超过该值实况图回退视频；`0` = 不限 |

#### 解析 / 归档 / 清理

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FORWARDER_OUTPUT_DIR` | `./output` | 归档根目录（相对仓库根或绝对路径） |
| `FORWARDER_PARSE_MIN_INTERVAL` | `8` | 两次解析之间的最小间隔（秒），防频繁请求 |
| `FORWARDER_SKIP_MEDIA_PROCESSING` | `true` | 跳过原项目媒体处理（转码/长图切割），归档原始文件；`false` 需装 ffmpeg |
| `FORWARDER_SAVE_METADATA` | `true` | 归档目录写入 `metadata.json` |
| `FORWARDER_AUTO_SYNC_COOKIE` | `false` | 每次启动把小号 cookie 同步到解析端（也可手动 `python run.py export-cookie`） |
| `FORWARDER_CLEANUP_AFTER_PUSH` | `true` | TG 推送成功后删除本地归档目录 |
| `FORWARDER_CACHE_TTL_HOURS` | `72` | 定时清理超过该时长的 output/debug/downloads；`0` = 关闭 |

#### Telegram 推送（可选）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `TG_PUSH` | `false` | 推送开关 |
| `TG_TARGET_CHAT` | 空 | `@用户名` / `-100` 数字 ID / t.me 链接 |
| `API_ID` / `API_HASH` | 空 | my.telegram.org 创建应用获得 |
| `BOT_TOKEN` | 空 | @BotFather 创建机器人获得 |
| `BOT_PROXY` | 空 | 国内服务器代理，如 `http://127.0.0.1:7890` |

#### 登录预览 / 调试

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FORWARDER_SHOT_PORT` | `8765` | 只读预览 HTTP 端口；`0` = 关闭 |
| `FORWARDER_SHOT_BIND` | `0.0.0.0` | 监听地址；仅本机/SSH 隧道用 `127.0.0.1` |
| `FORWARDER_SHOT_HOST` | 空 | 链接里显示的对外 IP（AWS NAT 实例建议显式指定） |
| `FORWARDER_DEBUG` | `false` | 调试日志（probe 的「页面无变化」等） |
| `FORWARDER_DUMP_HTML` | `false` | 提取失败时转储私信页 HTML 到 `forwarder_data/debug/` |

### 可选：为「解析」配置抖音 Cookie

解析能力完全来自原项目，抖音解析 cookie 存 `parse_hub_bot/data/config/platform_config.yaml`。**三种配置方式**：

| 方式 | 做法 | 适用 |
| --- | --- | --- |
| ① 不填（默认） | 什么都不用做 | 公开视频，解析走移动端接口，通常够用 |
| ② 自动同步（推荐） | `.env` 设 `FORWARDER_AUTO_SYNC_COOKIE=true`，之后每次启动自动把小号 cookie 同步给解析端；登录后也可手动 `python run.py export-cookie` | 想更稳、抗风控，又不想维护 cookie |
| ③ 手动填 | `python run.py cookie-config set "a=b; c=d"`（或 `from-file cookie.txt`） | 想用指定账号（如大号）的 cookie |

```bash
# 手动方式等价于直接编辑 platform_config.yaml：
python run.py cookie-config set "sessionid=xxx; sid_tt=xxx"
python run.py cookie-config show      # 查看（值已掩码）
python run.py cookie-config clear     # 清空，改回移动端接口
```

> 说明：
> - 接收端（小号登录、收私信）与解析端（parsehub 拉取视频信息）是**两条独立的通道**，cookie 互不相通；
> - 不填 cookie 也能解析公开视频，填了优先走网页端接口、更稳；
> - 写入通过原项目自带热更新**即时生效**，无需重启服务。

---

<a id="tg-push"></a>

## 推送到 Telegram 群组/频道（可选）

希望解析完成后自动把视频发送到你的 Telegram 群组/频道（而不是只存服务器本地）时，按以下步骤配置：

### 1. 申请 Telegram 凭据

1. 打开 <https://my.telegram.org>（浏览器需能访问 Telegram），用你的 TG 账号登录 → **API development tools** → 创建应用 → 记下 `API_ID`、`API_HASH`；
2. 在 Telegram 里打开 **@BotFather** → 发送 `/newbot` 按提示创建机器人 → 记下 `BOT_TOKEN`。

### 2. 把机器人加入目标群组/频道

- **频道**：频道 → 管理 → 管理员 → 添加你的机器人（勾选「发布消息」权限）；
- **群组**：直接把机器人拉进群（如群组限制发言，给机器人管理员权限）。

### 3. 配置 .env

```ini
TG_PUSH=true
TG_TARGET_CHAT=@你的频道用户名       # 私有频道/群用 -100 开头的数字ID，也支持 t.me 链接
API_ID=1234567
API_HASH=xxxxxxxxxxxxxxxxxxxxxxxx
BOT_TOKEN=123456789:AAxxxxxxxxxxxxxx
```

> 私有频道/群的数字 ID 获取：把频道/群里任意一条消息**转发给 @userinfobot**（或 @RawDataBot），返回里的 `Forwarded from ... id: -100xxxx` 即为所需 ID。

### 4. 重启服务并验证

```bash
sudo systemctl restart douyin-forward
journalctl -u douyin-forward -f
```

日志出现 `TG 推送已就绪 ✅ 目标: xxx` 即配置成功；之后每次解析完成会多出一行 `已推送至 Telegram`。

**说明**：

- 推送内容与原项目输出一致（视频带封面、流式播放 + 标题/来源文案；图文走相册；失败自动降级为文档）；
- 推送失败会自动重试（与解析共用重试机制，最多 3 次），只影响推送，本地归档不受影响；
- 服务器需要能访问 Telegram：海外服务器一般直连可用；**国内服务器请配置 `BOT_PROXY`**（如 `BOT_PROXY=http://127.0.0.1:7890`）。

---

<a id="deploy"></a>

## 部署到 Ubuntu 服务器

完整指南见 **[DEPLOY_UBUNTU.md](DEPLOY_UBUNTU.md)**（含首次登录方案、日常运维、故障排查表）。

一键部署（把项目上传到服务器后执行）：

```bash
cd /opt/douyin_download_bot
sudo APP_USER=douyin bash deploy/install_ubuntu.sh
```

脚本会自动完成：系统依赖 → 虚拟环境 `.venv` → Playwright Chromium → systemd 服务安装
（模板：`deploy/douyin-forward.service`），并在结束时打印「首次登录 + 启动服务」的具体命令。

> ⚠️ 关键提醒：**首次登录必须在服务器本机完成**（登录态不能跨机器复制——浏览器 Cookie 按系统加密，复制后会静默失效）：
> 服务器上用 `xvfb-run -a` 以 `DOUYIN_HEADLESS=false` 扫码，二维码自动截图到 `forwarder_data/login_qr.png`。
> 原因与详细做法见 DEPLOY_UBUNTU.md 第 4 节。

---

<a id="performance"></a>

## 性能与资源

以下为本机真实账号实测（2026-10，Windows + 本地网络；服务器数值会有差异，但比例接近）：

| 指标 | reload（现状） | probe（B 方案） |
| --- | --- | --- |
| 单轮请求数 | 约 250 个 | 空闲 0 个；强制取数约 19 个 |
| 单轮耗时 | 6–32 秒（整页重载） | 0.0 秒（无变化）/ 1–2 秒（取数） |
| 每天请求量（20s/10s 间隔估算） | 65–108 万 | 约 3.6 万（↓95%+） |
| 首次加载 | 253 请求 / 4.37 MB / 21.7 秒 | 同左（仅启动一次） |
| JS 堆内存 | 约 65 MB | 同左 |
| CPU | 持续重载，占用高 | 空闲几乎为 0 |

实况图额外开销：

- 每张实况图归档 **2 个文件**（jpg + mp4）；实测单张约 82 KB + 389 KB，14 张作品约 6–7 MB；
- `live`/`both` 模式逐条发送，消息数约为实况图张数；`MAX_ITEMS` 默认 3 可避免大图集刷屏。

建议配置：

- 1–2 vCPU / 1 GB+ 内存（建议 2 GB）即可运行；长期挂机优先 `probe` + `video`/`both`；
- 磁盘按 `FORWARDER_CACHE_TTL_HOURS`（默认 72h）自动清理，无需手工维护。

---

<a id="troubleshooting"></a>

## 故障排查速查

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| 日志「未检测到有效登录态」 | 登录态过期/被风控 | 重新执行 `python run.py login`（服务器用 xvfb 有界面） |
| 「验证码中间页」/滑块 | 无头 + 新指纹触发风控 | 用 `DOUYIN_HEADLESS=false` 完成一次登录；不要跨机器复制 profile |
| 「未找到私信会话」 | 昵称不匹配/未互关/消息在陌生人折叠 | 控制台写正确昵称；大号小号互关；把消息移出陌生人消息 |
| 解析失败 | 解析端 cookie 失效/作品受限 | `python run.py test-url <链接>` 验证；检查 `cookie-config show` |
| TG 推送失败 | 凭据错误/机器人无权限/网络不通 | 检查 API_ID/API_HASH/BOT_TOKEN、机器人管理员权限、`BOT_PROXY` |
| 实况图仍是视频 | 模式为 `video`；视频超 10s/10MB；实况图数量 > MAX_ITEMS；客户端不支持 | 查看启动日志的「实况图模式」与推送日志；调大限制或设 `MAX_ITEMS=0` |
| probe 模式延迟大 | 强制取数周期长/页面卡死 | 调小 `DOUYIN_FORCE_FETCH_INTERVAL`；看是否有自愈 reload 日志 |
| 预览链接打不开 | 8765 未放行/绑定地址 | 安全组放行或 SSH 隧道；检查 `FORWARDER_SHOT_BIND` |
| 磁盘持续增长 | 未开清理/未开推送删除 | 检查 `FORWARDER_CACHE_TTL_HOURS` 与 `FORWARDER_CLEANUP_AFTER_PUSH` |
| `state.json` 损坏 | 异常断电/写入中断 | 服务会自动改名为 `*.corrupt` 并从空状态启动；必要时手工删除重建 |
| 日志在哪 | — | 控制台 + `parse_hub_bot/logs/bot.log` |

更完整的部署排障见 [DEPLOY_UBUNTU.md](DEPLOY_UBUNTU.md) 第 8 节。

---

<a id="upgrade"></a>

## 升级与回滚

### 升级

```bash
cd /opt/douyin_download_bot
sudo systemctl stop douyin-forward

# 备份配置与状态（profile 不建议跨机器复制，但同机备份可用于回滚）
cp .env .env.bak
cp forwarder_data/state.json forwarder_data/state.json.bak 2>/dev/null || true

sudo -u douyin git pull --recurse-submodules
sudo -u douyin .venv/bin/pip install -r requirements-forwarder.txt
sudo -u douyin .venv/bin/playwright install chromium

sudo systemctl start douyin-forward
journalctl -u douyin-forward -f
```

升级后建议跑一遍：

```bash
sudo -u douyin .venv/bin/python run.py doctor
sudo -u douyin .venv/bin/python run.py serve --once
```

### 行为兼容性

- 检测模式默认 `reload`、实况图模式默认 `video`——**升级后不配置任何项，行为与旧版一致**；
- 新配置项都有默认值，旧 `.env` 不需要改；
- 控制台设置保存在 `state.json`，升级不会清空。

### 回滚

```bash
cd /opt/douyin_download_bot
sudo systemctl stop douyin-forward
sudo -u douyin git checkout <上一个提交>
sudo -u douyin .venv/bin/pip install -r requirements-forwarder.txt
sudo systemctl start douyin-forward
```

如升级后出现异常，可先把 `.env.bak` / `state.json.bak` 恢复，再回滚代码。

---

<a id="development"></a>

## 开发与测试

### 代码结构

| 模块 | 职责 |
| --- | --- |
| [forwarder/douyin_web.py](forwarder/douyin_web.py) | 浏览器生命周期、登录、检测（reload/probe）、消息提取、远程控制台 |
| [forwarder/parser_bridge.py](forwarder/parser_bridge.py) | 调用原项目 `ParseService` / `ParsePipeline`，在临时目录清理前完成归档 |
| [forwarder/archive.py](forwarder/archive.py) | 复制媒体、生成 `metadata.json`、实况图视频归档 |
| [forwarder/state.py](forwarder/state.py) | 去重/水位/失败计数/控制台设置 |
| [forwarder/tg_push.py](forwarder/tg_push.py) | 构建并发送 Telegram 媒体（含 Live Photo 分支） |
| [forwarder/main.py](forwarder/main.py) | 服务主循环、解析重试、缓存清理 |
| [forwarder/config.py](forwarder/config.py) | 环境变量配置与模式规范化 |
| [run.py](run.py) | 统一入口与 bootstrap（chdir / 注入占位环境变量 / sys.path） |

### 本地检查

```bash
python -m compileall -q forwarder run.py
python run.py doctor
python run.py test-url "https://v.douyin.com/xxxxxxx/" --no-download
python run.py serve --once
bash -n deploy/*.sh
```

### 调试选择器

抖音改版导致 DOM 选择器失效时：

1. `.env` 设 `FORWARDER_DUMP_HTML=true`；
2. 复现一次提取失败；
3. 查看 `forwarder_data/debug/*.html`，对照 `forwarder/douyin_web.py` 顶部的
   `CONVERSATION_ITEM_SELECTORS` / `MESSAGE_CONTAINER_SELECTORS` / `MESSAGE_ITEM_SELECTORS` / `LINK_PATTERNS` 调整。

### 实况图联调

- 用一条公开实况图作品验证解析：解析结果应为 `ImageParseResult` + 多个 `LivePhotoRef`；
- 下载后应为 `LivePhotoFile`（jpg + mp4），归档目录同时出现两种文件；
- 若只看到视频，检查 `FORWARDER_LIVE_PHOTO_MODE` 是否为 `video`，以及启动日志中的实况图模式。

> 当前仓库未内置 pytest 套件；以上命令 + `serve --once` 日志是最小回归路径。如需自动化，可在 `forwarder/tests/` 下补充单元测试。

---

<a id="faq"></a>

## 常见问题（FAQ）

**Q1. 首次启动后，我转发的视频没被解析？**
首次启动只记录「消息水位」，只处理**启动之后**新转发的视频（防抖设计）。如果希望处理历史转发：
设置 `DOUYIN_PROCESS_HISTORY=true`，并删除 `forwarder_data/state.json` 后重启。

**Q2. 收不到消息 / 日志提示「未找到私信会话」？**
- 确认大号已经给小号发过至少一条消息；
- 确认监测昵称与大号昵称匹配（包含匹配）——**无需改 .env**，直接往 `forwarder_data/agent_cmd.txt` 写入大号昵称即可更换（见「远程控制台」）；
- 大号与小号互相添加关注、把消息从「陌生人消息」里移出；
- 开启 `FORWARDER_DUMP_HTML=true`，查看 `forwarder_data/debug/` 里转储的页面 HTML，并按需调整 `forwarder/douyin_web.py` 顶部的选择器常量。

**Q3. 视频卡片提取链接失败？**
抖音网页版改版会导致选择器失效。开 `FORWARDER_DUMP_HTML=true` 复现一次，把转储的 HTML 对照
`forwarder/douyin_web.py` 顶部的 `LINK_PATTERNS` / `MESSAGE_ITEM_SELECTORS` 调整即可（一般只需补一条正则或选择器）。

**Q4. 登录态多久过期？**
抖音 Web 登录态一般可维持数天到数周。失效时服务会自动重新进入扫码流程（重新查看 `forwarder_data/login_qr.png` 扫码即可）。

**Q5. 提示「验证码中间页」（滑块验证）怎么办？**
这是抖音的风控拦截，多见于「无头浏览器 + 全新 profile」的首次访问。处理方式：
1. 在**有界面的环境**中执行 `DOUYIN_HEADLESS=false python run.py login`（服务器上可用 `/usr/bin/xvfb-run -a` 包装），窗口可见时手动拖一下滑块、再扫码登录；
2. 若服务器上无法完成：间隔一段时间或更换网络环境重试；服务运行中途偶发该验证码时，日志会给出提示（每 10 分钟最多一次），一般重新登录或等待一段时间即恢复；
3. ⚠️ **登录态不能从其他电脑复制**（浏览器 Cookie 按操作系统加密，复制后无法解密、静默失效），务必在服务器本机完成登录。

> 本服务**不会**自动破解验证码（不可靠且不合规），只做检测与引导。

**Q6. 会不会有账号风险？**
两条通用建议：① 只用小号做接收端，控制频率（默认轮询 20s、解析间隔 8s，已比较保守；probe 模式请求更少）；
② 不要把服务部署在频繁更换 IP 的环境。详见「重要提示」。

**Q7. 日志在哪里？**
- 控制台（stderr）；
- `parse_hub_bot/logs/bot.log`（原项目日志目录，滚动 10MB）。

**Q8. 如何完全重置？**
停止服务，删除 `forwarder_data/state.json`（清解析记录 + 控制台设置）或整个 `forwarder_data/browser_profile/`（同时清除登录态，需重新扫码）。
自定义昵称也可以只重置昵称：往控制台写 `reset`。

**Q9. 为什么依赖清单里没有 tgcrypto？**
原项目将其列为依赖，但 tgcrypto 仅用于 Telegram 连接的原生加密加速（可选），且对新版 Python 无预编译轮子、无编译链时会安装失败。本服务不启动 Telegram Bot，运行时用不到，故省略；如你同时还要跑原项目的 TG Bot，可单独 `pip install tgcrypto`。

**Q10. 需要配置原项目的 Telegram Bot 吗（API_ID / API_HASH / BOT_TOKEN）？**
**不需要**（除非你要用「推送到 Telegram 群组/频道」功能）。本服务只复用原项目的「解析引擎」（`services.ParseService` / `services.ParsePipeline` + parsehub 库），
Telegram 机器人部分不会被启动——`run.py` 启动时会自动注入占位配置，与 Telegram 完全无关。
日志里出现 `未找到 platform_config.yaml, 跳过加载` 也属正常（那是**可选**的平台 Cookie / 代理配置，不配置也能解析）。

**Q11. 国内服务器连不上 Telegram / 推送超时？**
Telegram 在境内不可直连。搭好代理后（如本机 7890 端口），在 `.env` 中配置 `BOT_PROXY=http://127.0.0.1:7890`（支持 socks5:// 等），重启服务即可。海外服务器一般无需代理。

**Q12. 同一作品被重复转发，会不会重复下载？**
不会。去重做了三层：消息指纹、链接指纹、**视频 ID 指纹**——即使同一条视频用不同链接（短链/长链）重复转发，日志也会提示
`检测到重复，已跳过: 作品已在库（视频 ID ...）`，不会重复解析/下载。**本地缓存被自动清理后依然有效**（记录保存在 `state.json`）。

**Q13. 监控的昵称/登录操作不想每次改 .env、也不想到服务器上敲长命令？**
用「远程控制台 + 登录编号菜单」：写一句话/一个数字到 `forwarder_data/agent_cmd.txt` 即可
（如 `echo 1 > ...` 点击发送短信验证码、`echo 我的大号 > ...` 换监测对象），详见「远程控制台」专节。

**Q14. 服务器磁盘会不会被占满？**
不会无限增长：默认「推送成功即删本地归档」+「超过 72 小时的缓存定时清理」（见「本地缓存清理」）。
如果你不用 TG 推送、又希望长期保留归档，把 `FORWARDER_CACHE_TTL_HOURS=0` 即可关闭定时清理。

**Q15. 能同时监测多个聊天用户吗（多个大号都往小号转发）？**
可以，两种方式任选：
① `.env` 里用 `DOUYIN_MAIN_ACCOUNTS=小明,小红`（Ubuntu 部署脚本会交互式询问「单会话 / 多会话」并代填）；
② 运行中往控制台写：`echo "小明,小红" > forwarder_data/agent_cmd.txt`（`+` 追加 / `-` 移除，免重启）。
所有匹配到的会话都会被逐个打开并**锁定**，只解析这些会话里的新转发；同一个视频无论谁转发过、转发几次，也只处理一次（视频 ID 级去重）。

**Q16. 服务重启后，没下载完的视频会自动补吗？**
会。解析记录持久化在 `state.json`：已下载成功的不会重复下载；**解析到一半被中断（比如下载中重启）的视频，重启后会自动补处理**（日志出现 `恢复未完成的消息…`）。只有「首轮水位跳过的历史消息」和「失败 3 次已放弃的」不重试——后者重新转发一次即可重新触发。

**Q17. 实况图为什么还是视频？**
三种可能：① `FORWARDER_LIVE_PHOTO_MODE` 仍是默认的 `video`；② 实况视频超过 10 秒或 10MB，按规则改发视频；③ 实况图数量超过 `FORWARDER_LIVE_PHOTO_MAX_ITEMS`（默认 3），实况图回退视频。
查看启动日志的「实况图模式」和推送日志即可确认；调大限制或设 `MAX_ITEMS=0` 可解除数量限制。

**Q18. 实况图三种模式怎么选？**
- 想要聊天最干净、最稳：`video`；
- 想要真正的 Live Photo：`live`（旧客户端只显示静态图）；
- 想要兼容性最好：`both`（实况图 + 视频，消息数约翻倍）。
多图实况建议保留 `MAX_ITEMS=3`，避免 14 张图变成 14 条消息。

**Q19. 检测模式怎么选？**
- 默认 `reload`：每轮整页刷新，最稳，适合配置充足/要求实时；
- `probe`：DOM 探针 + 定期取数，请求量降 95%+，适合长期挂机/低配服务器；最坏延迟约一个强制取数周期（默认 45 秒）。
两种模式解析结果完全一致，可随时用控制台 `mode` 切换。

**Q20. 升级后默认行为会变吗？**
不会。检测模式默认 `reload`、实况图模式默认 `video`，新配置项都有默认值；旧 `.env` 不需要改。

**Q21. 为什么实况图没有相册，是一条条发？**
Telegram Bot API 的 `sendMediaGroup` 不支持 Live Photo，只能逐条 `sendLivePhoto`。这是 Telegram 的限制，不是本服务的选择；用 `video` 模式可以回到相册发送。

**Q22. 实况图归档为什么有 jpg 和 mp4 两个文件？**
实况图 = 静态图 + 短视频；归档两者都保留，便于以后重新生成 Live Photo 或单独使用静态图。`metadata.json` 的 `live_photo_count` 会记录实况图数量。

**Q23. 怎么验证实况图端到端？**
1. 用一条公开实况图作品跑 `python run.py test-url <链接>`，确认解析结果包含 `LivePhotoRef`；
2. 检查 `output/` 归档目录同时出现 `.jpg` 和 `_video.mp4`；
3. 在测试频道用 `live` 模式发一条，iOS/Android/Desktop 各看一次是否能播放；
4. 再用 14 张图的作品验证 `MAX_ITEMS` 回退是否生效。

**Q24. 为什么改了 `.env` 的实况图模式没生效？**
如果之前用控制台 `live ...` 切过，`state.json` 里的控制台设置优先级高于 `.env`；执行 `echo "live reset" > forwarder_data/agent_cmd.txt` 恢复默认，或重启服务前确认没有覆盖值。

---

<a id="notes"></a>

## 重要提示（请阅读）

- 本工具用于**个人保存自己账号可访问的内容**，请遵守抖音用户协议，控制使用频率；
- 使用自动化操作自己的账号存在被平台风控的可能（例如限制私信功能），**请使用专门的小号**，并避免高频操作；
- **首次登录务必使用有界面模式**完成一次（见「快速开始-3」），之后即可无头运行；
- 直播、付费内容、私密作品等受保护内容不在解析范围内，请勿尝试绕过；
- 解析能力来自开源项目 parse_hub_bot / parsehub，请一并遵守其开源协议（MIT）。

---

<a id="tech"></a>

## 技术细节补充

- **接收端**：Playwright 持久化浏览器上下文（`forwarder_data/browser_profile/`），注入 stealth 脚本降低自动化特征；私信页使用 `https://www.douyin.com/chat?isPopup=1`，如抖音改版可通过 `DOUYIN_CHAT_URL` 覆盖。
- **链接提取**：优先**拦截「私信消息接口」响应**直接读取视频卡片 ID（`itemId` → `/video/{id}`，准确稳定、无需点击）；同时从接口请求中捕获**会话 ID 并锁定指定会话**（其他会话/群聊的分享一律忽略）；接口不可用时回退 DOM 扫描 + 卡片点击（新标签页/同页跳转/弹层三种路径）；失败不标记、下轮重试，失败 3 次后放弃并记录。
- **去重**：消息指纹（防重复点击）+ 链接指纹（输入链接与展开后的原始链接各记录一次）+ **视频 ID 指纹**（同作品换链接重复转发也会跳过；缓存清理后依然有效）。
- **提取时的视频播放**：启动参数禁用自动播放 + 全程静音；打开视频页/弹层后立即暂停页内全部音视频，避免播放噪音与"后台自动暂停"干扰。
- **登录交互**：登录/验证页自动扫描可点击项生成**编号菜单**（写日志 + `login_menu.txt`）；`agent_cmd.txt` 支持回复编号、验证码直填（`123456` / `123456 go`）、纯文本设置监测昵称（免重启）；二维码/页面截图经内置只读 HTTP 服务（`shot_server.py`）输出为带时间戳的可点击链接，每次刷新重新输出。
- **缓存清理**：推送成功后按配置删除本地归档；另按 `FORWARDER_CACHE_TTL_HOURS` 定时清理 `output/`、`debug/`、`downloads/` 中过期内容（启动时也会清理一次）。
- **归档时机**：原项目流水线退出时会清理临时目录，归档发生在 `with ParsePipeline(...)` 块内、清理之前（见 `forwarder/parser_bridge.py` 注释）。
- **媒体处理**：默认 `FORWARDER_SKIP_MEDIA_PROCESSING=true`——原项目的媒体处理是为适配 Telegram 限制（转码/长图切割）而设计的，本服务归档原始文件即可；如需与原项目完全一致的产物，可设为 `false`。
- **检测模式**：reload 每轮 `page.reload` 后重新拉取 `get_message_by_init`；probe 用一次 `page.evaluate` 读取会话列表/消息尾部生成指纹，变化或到期才点会话取 `get_by_conversation`，并用 `expect_response` 精确等待；连续失败/探针读不到/定时触发自愈 reload。
- **实况图**：抖音 `aweme_detail.images[].video` → parsehub `LivePhotoRef` → 下载为 `LivePhotoFile`（jpg + mp4）；`forwarder/tg_push.py` 用 pyrogram `send_live_photo(photo, live_photo)` 发送；超过 10 秒/10MB 或数量超过阈值时回退视频。
- **Telegram Live Photo 限制**：Bot API `sendLivePhoto` 需要静态图 + 视频两个文件，视频 ≤10s/≤10MB；`sendMediaGroup` 不支持 Live Photo，因此多图只能逐条发送。
- **状态与恢复**：`state.json` 区分 `seen_messages`（已识别）与 `settled`（已有定论），配合 `_inflight` 实现重启后补处理；`failures` 最多重试 3 次后进入 `gave_up`。
- **归档**：`archive_result` 在原项目临时目录清理前复制媒体；实况图额外复制 `source.video_path`，并在 `metadata.json` 写入 `live_photo_count`。

---

<a id="changelog"></a>

## 最近更新

| 日期 | 内容 |
| --- | --- |
| 2026-10-25 | 实况图（Live Photo）三模式：`video` / `live` / `both`；超限自动改发视频；归档 jpg+mp4；控制台 `live` 命令 |
| 2026-10-23 | 检测模式 `reload` / `probe`：probe 用 DOM 探针 + 定期取数 + 自愈，请求量约降 95% |
| 2026-10-18 | 部署时可选解析端 cookie 方式 + 自动同步小号 cookie |
| 2026-10-16 | 多会话监测（`DOUYIN_MAIN_ACCOUNTS`）+ 部署交互选择 |
| 2026-10-14 | 项目初始版本：私信转发 → 解析 → 归档 / TG 推送 |
