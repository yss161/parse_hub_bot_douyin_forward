# 抖音转发解析服务

> 仓库地址：https://github.com/yss161/parse_hub_bot_douyin_forward

基于 [parse_hub_bot](https://github.com/z-mio/parse_hub_bot) 的抖音视频解析服务。

**工作链路**：

```
【你的抖音大号】                       【服务器上的抖音小号】
  在手机上浏览视频                        浏览器（Playwright）常驻运行
      │                                      │
      │  ① 转发（分享→私信）                  │  ② 轮询私信，识别新消息
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

---

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

## 目录结构

```
douyin_download_bot/
├── parse_hub_bot/               # 原项目（git 子模块，未改动）
├── forwarder/                   # 新增：转发链路
│   ├── config.py                #   配置（读 .env / 环境变量）
│   ├── douyin_web.py            #   抖音网页接收端（登录+轮询+链接提取）
│   ├── parser_bridge.py         #   桥接原项目解析流水线
│   ├── archive.py               #   归档（复制媒体 + metadata.json）
│   ├── state.py                 #   去重/水位状态
│   ├── reporter.py              #   日志版状态报告器
│   ├── tg_push.py               #   Telegram 推送（可选，TG_PUSH=true 启用）
│   └── main.py                  #   服务主循环
├── run.py                       # 统一入口
├── requirements-forwarder.txt   # 依赖清单
├── .env.example                 # 配置样例（复制为 .env）
├── deploy/                      # Ubuntu 部署（一键脚本 + systemd 模板）
│   ├── install_ubuntu.sh        #   一键部署脚本
│   └── douyin-forward.service   #   systemd 服务模板
├── DEPLOY_UBUNTU.md             # Ubuntu 服务器部署指南
├── forwarder_data/              # 运行数据（浏览器登录态/状态/二维码，自动创建）
└── output/                      # 解析结果归档（自动创建）
```

---

## 环境要求

- **Python 3.12+**（原项目要求，建议 3.13）
- 一台**能正常访问抖音网页版**的服务器（国内机器最佳；海外 IP 可能触发风控），Linux / Windows 均可
- 建议使用**专门的抖音小号**，并与大号**互相关注**（否则消息可能进入「陌生人消息」折叠，且可能影响私信功能）

---

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

编辑 `.env`，至少建议填写：

```ini
DOUYIN_MAIN_ACCOUNT=你的大号昵称
```

其余项均有默认值（详见 [.env.example](.env.example) 内注释）。

### 3. 登录小号（首次）

```bash
# 推荐：在【有桌面的机器】上用有界面模式登录（首次必须，见下方说明）
DOUYIN_HEADLESS=false python run.py login     # Windows PowerShell: $env:DOUYIN_HEADLESS="false"; python run.py login
```

- 脚本会打开抖音网页版，**登录二维码弹窗会自动出现**，脚本自动截取二维码保存到 `forwarder_data/login_qr.png`；
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

## 运行模式

| 命令 | 作用 |
| --- | --- |
| `python run.py serve` | 启动常驻服务（正常使用） |
| `python run.py serve --once` | 只执行一轮轮询后退出（调试登录/收信） |
| `python run.py login` | 仅扫码登录小号后退出 |
| `python run.py test-url <链接>` | 不启动浏览器，直接验证解析链路是否可用（排障用） |

排障示例：

```bash
python run.py test-url "https://v.douyin.com/xxxxxxx/"
```

---

## 配置说明

完整配置见 [.env.example](.env.example)，常用项：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DOUYIN_MAIN_ACCOUNT` | 空 | 大号昵称关键字，用于定位会话；**会严格锁定该会话**（只解析该会话内的新转发，其他会话/群聊的分享会被忽略）；留空则选有未读/第一条会话 |
| `DOUYIN_POLL_INTERVAL` | `20` | 私信轮询间隔（秒） |
| `FORWARDER_PARSE_MIN_INTERVAL` | `8` | 两次解析之间的最小间隔（秒），防频繁请求 |
| `FORWARDER_OUTPUT_DIR` | `./output` | 归档目录 |
| `DOUYIN_PROCESS_HISTORY` | `false` | 首次运行是否处理历史转发 |
| `FORWARDER_SKIP_MEDIA_PROCESSING` | `true` | 跳过原项目的媒体处理（转码等），归档原始文件；设 `false` 需装 ffmpeg |
| `TG_PUSH` | `false` | 是否推送解析结果到 Telegram 群组/频道（见下文专节） |
| `TG_TARGET_CHAT` | 空 | 推送目标：`@用户名`／`-100...` 数字 ID／t.me 链接 |
| `DOUYIN_HEADLESS` | `true` | 无头模式；本机调试可设 `false` 观察浏览器 |
| `FORWARDER_DUMP_HTML` | `false` | 提取失败时转储页面 HTML 到 `forwarder_data/debug/`（调选择器用） |

### 可选：为「解析」配置抖音 Cookie / 代理

解析能力完全来自原项目，原项目的平台配置同样生效：编辑
`parse_hub_bot/data/config/platform_config.yaml`（不存在则新建）：

```yaml
# 全局默认代理（国内直连抖音时可禁用）
platforms:
  douyin:
    disable_parser_proxy: true
    disable_downloader_proxy: true
    # 如需提高解析稳定性，可补充抖音网页版 Cookie（可选）
    # cookies:
    #   - "你的抖音网页版 Cookie"
```

> 说明：接收端（小号登录、收私信）与解析端（parsehub 拉取视频信息）是两条独立的通道；
> 解析端不配置 Cookie 通常也能解析公开视频，配置后更稳。

---

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

## 常见问题（FAQ）

**Q1. 首次启动后，我转发的视频没被解析？**
首次启动只记录"消息水位"，只处理**启动之后**新转发的视频（防抖设计）。如果希望处理历史转发：
设置 `DOUYIN_PROCESS_HISTORY=true`，并删除 `forwarder_data/state.json` 后重启。

**Q2. 收不到消息 / 日志提示"未找到私信会话"？**
- 确认大号已经给小号发过至少一条消息；
- 确认 `DOUYIN_MAIN_ACCOUNT` 与大号昵称匹配（包含匹配）；
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
2. 若服务器上无法完成（例如验证码需要手动拖动而 xvfb 中没有可见窗口）：间隔一段时间或更换网络环境重试；服务运行中途偶发该验证码时，日志会给出提示（每 10 分钟最多一次），一般重新登录或等待一段时间即恢复；
3. ⚠️ 注意：**登录态不能从其他电脑复制**（浏览器 Cookie 按操作系统加密，复制后无法解密、静默失效），务必在服务器本机完成登录。

> 本服务**不会**自动破解验证码（不可靠且不合规），只做检测与引导。

**Q6. 会不会有账号风险？**
两条通用建议：① 只用小号做接收端，控制频率（默认轮询 20s、解析间隔 8s，已比较保守）；
② 不要把服务部署在频繁更换 IP 的环境。详见下方「重要提示」。

**Q7. 日志在哪里？**
- 控制台（stderr）；
- `parse_hub_bot/logs/bot.log`（原项目日志目录，滚动 10MB）。

**Q8. 如何完全重置？**
停止服务，删除 `forwarder_data/state.json`（只清解析记录）或整个 `forwarder_data/browser_profile/`（同时清除登录态，需重新扫码）。

**Q9. 为什么依赖清单里没有 tgcrypto？**
原项目将其列为依赖，但 tgcrypto 仅用于 Telegram 连接的原生加密加速（可选），且对新版 Python 无预编译轮子、无编译链时会安装失败。本服务不启动 Telegram Bot，运行时用不到，故省略；如你同时还要跑原项目的 TG Bot，可单独 `pip install tgcrypto`。

**Q10. 需要配置原项目的 Telegram Bot 吗（API_ID / API_HASH / BOT_TOKEN）？**
**不需要**（除非你要用「推送到 Telegram 群组/频道」功能）。本服务只复用原项目的「解析引擎」（`services.ParseService` / `services.ParsePipeline` + parsehub 库），
Telegram 机器人部分不会被启动——`run.py` 启动时会自动注入占位配置，与 Telegram 完全无关。
日志里出现 `未找到 platform_config.yaml, 跳过加载` 也属正常（那是**可选**的平台 Cookie / 代理配置，不配置也能解析）。
只有当你启用 **TG 推送**或想**另外**运行原项目的完整 TG Bot 时，才需要去 my.telegram.org 申请 API_ID / API_HASH，并向 @BotFather 申请 BOT_TOKEN。

**Q11. 国内服务器连不上 Telegram / 推送超时？**
Telegram 在境内不可直连。搭好代理后（如本机 7890 端口），在 `.env` 中配置 `BOT_PROXY=http://127.0.0.1:7890`（支持 socks5:// 等），重启服务即可。海外服务器一般无需代理。

---

## 重要提示（请阅读）

- 本工具用于**个人保存自己账号可访问的内容**，请遵守抖音用户协议，控制使用频率；
- 使用自动化操作自己的账号存在被平台风控的可能（例如限制私信功能），**请使用专门的小号**，并避免高频操作；
- **首次登录务必使用有界面模式**完成一次（见「快速开始-3」），之后即可无头运行；
- 直播、付费内容、私密作品等受保护内容不在解析范围内，请勿尝试绕过；
- 解析能力来自开源项目 parse_hub_bot / parsehub，请一并遵守其开源协议（MIT）。

---

## 技术细节补充

- **接收端**：Playwright 持久化浏览器上下文（`forwarder_data/browser_profile/`），注入 stealth 脚本降低自动化特征；私信页使用 `https://www.douyin.com/chat?isPopup=1`，如抖音改版可通过 `DOUYIN_CHAT_URL` 覆盖。
- **链接提取**：三级策略 —— ①消息 HTML/文本正则（含 `v.douyin.com` 短链）；②视频卡片点击（新标签页/同页跳转/弹层三种路径）；③失败不标记、下轮重试；失败 3 次后放弃并记录到 `forwarder_data/state.json`。
- **去重**：消息 DOM 指纹（防重复点击）+ 链接指纹（输入链接与展开后的原始链接各记录一次，防重复解析）。
- **归档时机**：原项目流水线退出时会清理临时目录，归档发生在 `with ParsePipeline(...)` 块内、清理之前（见 `forwarder/parser_bridge.py` 注释）。
- **媒体处理**：默认 `FORWARDER_SKIP_MEDIA_PROCESSING=true`——原项目的媒体处理是为适配 Telegram 限制（转码/长图切割）而设计的，本服务归档原始文件即可；如需与原项目完全一致的产物，可设为 `false`。
