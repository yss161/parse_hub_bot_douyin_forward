# Ubuntu 服务器部署指南

把本项目部署到 Ubuntu 服务器（让「小号」7×24 常驻收私信、自动解析）。

- **部署前**：建议先通读一遍本文（5 分钟），理解「首次登录」环节的特殊性（第 4 节）。
- **本机先行**：建议先在本地按 [README](README.md) 的「快速开始」跑通过一次解析链路，再上服务器。

---

## 0. 前置须知

| 项目 | 要求 / 说明 |
| --- | --- |
| 系统 | Ubuntu 20.04 / 22.04 / 24.04（**推荐 24.04 LTS**，自带 Python 3.12）；x86_64 与 arm64 均可 |
| Python | 3.12+（脚本会自动检测；低于 3.12 时通过 deadsnakes PPA 安装） |
| 内存 | ≥ 1 GB（Chromium 常驻约 300~500 MB） |
| 磁盘 | ≥ 2 GB 可用（依赖 + Chromium 约 500 MB，视频归档另计） |
| 网络 | 服务器需能正常访问 `www.douyin.com`（国内机器最佳，海外 IP 更易触发风控） |
| 端口 | 默认**无需开放任何入站端口**（服务只做出站请求）；如需「登录预览链接」功能，可选放行 `8765`（建议仅放行你自己的 IP，或改用 SSH 隧道） |
| 账号 | 建议使用专门的**小号**，并与大号互相关注 |

> 💡 纯解析**不需要** Telegram：本服务只复用原项目的解析引擎，原项目的 TG 机器人不会启动（`run.py` 启动时自动注入占位配置）。
> 只有当你要「解析后自动推送到 Telegram 群组/频道」时才需要配置 TG 凭据（见第 6 节）。
> 日志里 `未找到 platform_config.yaml, 跳过加载` 也属正常（那是**可选**的抖音 Cookie/代理配置，不配也能解析）。

### 部署前连通性检查（可选，1 分钟）

```bash
# 正确的检查方式：GET 请求（返回 200 即正常）
curl -s -L -o /dev/null -w "HTTP %{http_code} -> %{url_effective}\n" --max-time 10 https://www.douyin.com
# 预期输出：HTTP 200 -> https://www.douyin.com/
```

> ⚠️ 不要用 `curl -I`（HEAD 请求）检查：抖音 CDN 对 HEAD 请求一律返回 `404`，
> 那是**正常现象**（能收到 404 响应就说明 TLS / HTTP2 连接已经建立），不代表网络不通。

---

## 1. 获取项目到服务器

### 方式 A：从 GitHub 克隆（推荐）

```bash
sudo apt-get install -y git
sudo git clone --recursive https://github.com/yss161/parse_hub_bot_douyin_forward.git /opt/douyin_download_bot
sudo chown -R "$USER":"$USER" /opt/douyin_download_bot    # 归属当前用户（若直接用 root 部署可跳过）
```

> `--recursive` 必须加：`parse_hub_bot` 是 git 子模块。若克隆时漏加，补执行：
> `cd /opt/douyin_download_bot && git submodule update --init --recursive`

### 方式 B：本地打包上传（不使用 git 时）

#### 1.1 本地打包（Windows PowerShell，在项目所在目录执行）

```powershell
cd <本地项目目录>                          # 例如 C:\Users\<你的用户名>\Documents\code\douyin_download_bot
$skip = @('forwarder_data','output')      # 排除运行数据（登录态不能跨机器复制，原因见第 4 节）
$items = Get-ChildItem -Force | Where-Object { $_.Name -notin $skip }
Compress-Archive -Path $items.FullName -DestinationPath ..\douyin_download_bot.zip -Force
scp ..\douyin_download_bot.zip ubuntu@<服务器IP>:/tmp/
```

#### 1.2 服务器解压

```bash
sudo apt-get install -y unzip        # 如未安装
sudo mkdir -p /opt/douyin_download_bot
sudo unzip -o /tmp/douyin_download_bot.zip -d /opt/douyin_download_bot
```

> 建议路径 `/opt/douyin_download_bot`（无空格、无中文，systemd 最省心；换用其他路径亦可，部署脚本会自动识别）。

---

## 2. 一键部署（推荐）

```bash
cd /opt/douyin_download_bot
sudo APP_USER=douyin bash deploy/install_ubuntu.sh
```

脚本会依次完成：

1. 安装系统依赖：Python/venv、build-essential、**fonts-noto-cjk**（中文渲染）、**xvfb**（有界面登录用）、以及 Playwright 所需的 Chromium 系统库（`playwright install-deps`）；
2. 检测 Python ≥ 3.12（不足则自动装 `python3.12`）；
3. 创建虚拟环境 `.venv`，安装 `requirements-forwarder.txt`（默认**腾讯云 PyPI 镜像**，失败自动回退官方源）；
4. 安装 Playwright Chromium 浏览器；
5. 生成 `.env`（若不存在），并**交互式询问监测模式**（单会话 / 多会话，输入大号昵称后自动写入 .env）；
6. 安装 systemd 服务 `douyin-forward`（**安装但不启动**）；
7. 打印「首次登录 + 启动服务」的后续步骤。

**监测模式交互示例**：

```
──────────────────────────────────────────────
 监测配置：要监测几个私信会话？
   1) 单会话（只监测一个大号）—— 默认
   2) 多会话（同时监测多个用户/大号的转发）
   （注：大号 = 给你服务器上的小号转发视频的抖音账号；可多个）
 请选择 [1/2/3]:
──────────────────────────────────────────────
```

- 选 1：输入一个大号昵称 → 写入 `DOUYIN_MAIN_ACCOUNT`；
- 选 2：输入昵称列表（如 `小明,小红`）→ 写入 `DOUYIN_MAIN_ACCOUNTS`，**同时监测多个用户**；
- 已配置过时会显示当前值，可直接回车「保持不变」；已部署后随时可重配：

```bash
sudo bash /opt/douyin_download_bot/deploy/configure_accounts.sh
```

- **非交互式部署**（如脚本化/CI）可用环境变量预设，避免卡在交互：

```bash
# 单会话
MONITOR_MODE=single MONITOR_ACCOUNTS="我的大号" sudo -E bash deploy/install_ubuntu.sh
# 多会话
MONITOR_MODE=multi  MONITOR_ACCOUNTS="小明,小红" sudo -E bash deploy/install_ubuntu.sh
```

以 root 运行时脚本会自动创建并切换服务用户（默认 `douyin`）；已有 sudo 的普通用户直接运行即可，服务将以该用户运行。

**可选环境变量**：

| 变量 | 作用 |
| --- | --- |
| `APP_USER=douyin` | root 场景下指定服务用户 |
| `PIP_INDEX_URL=https://pypi.org/simple` | 覆盖 pip 镜像源 |

---

## 3. 手动安装（等价的逐步命令）

不使用一键脚本时，可按以下步骤操作（`RUN_AS` 为运行服务的用户）：

```bash
# ① 系统依赖
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip python3-dev \
  build-essential curl ca-certificates unzip \
  fonts-noto-cjk fonts-noto-color-emoji xvfb xauth software-properties-common
# 若 python3 低于 3.12（Ubuntu 22.04）：sudo add-apt-repository -y ppa:deadsnakes/ppa
#   && sudo apt-get install -y python3.12 python3.12-venv python3.12-dev

# ② 虚拟环境与依赖
cd /opt/douyin_download_bot
python3 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -r requirements-forwarder.txt \
  -i https://mirrors.cloud.tencent.com/pypi/simple   # 失败可去掉 -i 用官方源

# ③ Playwright 浏览器 + 系统库
sudo .venv/bin/playwright install-deps chromium
.venv/bin/playwright install chromium

# ④ 配置（单会话 / 多会话，二选一）
cp .env.example .env && nano .env
#   单会话：DOUYIN_MAIN_ACCOUNT=我的大号
#   多会话：DOUYIN_MAIN_ACCOUNTS=小明,小红
#   （也可以用交互式助手代填：sudo bash deploy/configure_accounts.sh）

# ⑤ systemd 服务
sudo sed -e "s|__APP_USER__|$RUN_AS|g" \
         -e "s|__PROJECT_DIR__|/opt/douyin_download_bot|g" \
         -e "s|__APP_HOME__|$HOME|g" \
         deploy/douyin-forward.service > /etc/systemd/system/douyin-forward.service
sudo systemctl daemon-reload
```

---

## 4. 首次登录（关键步骤）

> 背景：抖音对"无头浏览器 + 全新指纹"会返回「**验证码中间页**」（滑块验证），全新 profile 的无头登录可能被拦。
> 因此**首次登录建议让浏览器处于"有界面"状态**。登录成功一次后，profile 变为"可信"，之后无头模式常驻即可（本机实测通过）。

> ⚠️ **重要：登录态只能在本机（服务器）生成，不能从其它电脑复制！**
> 浏览器 Cookie 按操作系统绑定加密（Windows 用 DPAPI，绑定当前电脑+用户），跨机器/跨系统复制
> `browser_profile` 后**无法解密**，登录态会"**静默失效**"（表现为服务日志「未检测到有效登录态」，且没有任何报错提示）。
> 唯一例外：来源与目标为同一套加密体系（如两台 Linux 且都未启用钥匙串），可以尝试，但不保证成功。

### 方式 ①（推荐）：在服务器上完成一次扫码（xvfb）

```bash
sudo -u douyin -H env DOUYIN_HEADLESS=false \
  /usr/bin/xvfb-run -a /opt/douyin_download_bot/.venv/bin/python \
  /opt/douyin_download_bot/run.py login
```

- 二维码自动截图到 `forwarder_data/login_qr.png`（约每 25 秒刷新），下载后用【小号】抖音 App 扫码
  （**用你 ssh 登录服务器的账号**下载，如 root / ubuntu；`douyin` 是服务用户，通常没有 SSH 权限）：
  ```bash
  scp <登录用户>@<服务器IP>:/opt/douyin_download_bot/forwarder_data/login_qr.png .
  ```
- **终端直扫（零下载）**：二维码出现时会**直接以字符画形式打印在日志里**（深色背景的 SSH 终端对准扫即可），
  同时输出二维码的 **base64 编码**（data URI，可自行还原成图片）；二维码每次刷新后若内容变化会重新输出。
  浅色背景终端若识别不了，改用下面的预览链接或截图文件。
- **免 scp 方式（推荐）**：日志里会直接输出**可点击链接**（内置只读 HTTP 服务，端口 8765），形如
  `http://<服务器IP>:8765/login_qr.png?t=...` —— 需先在云安全组放行 `8765`（建议只放行你自己的 IP）。
  若不想开端口：本地执行 `ssh -L 8765:127.0.0.1:8765 <登录用户>@<服务器IP>`，再访问 `http://127.0.0.1:8765/`（每 15 秒自动刷新）。
  **AWS 等 NAT 实例**请把公网 IP 写进 `.env` 的 `FORWARDER_SHOT_HOST`（如 `FORWARDER_SHOT_HOST=54.153.141.184`），否则链接里可能是内网地址。
- **出现「身份验证」页怎么办**：日志会给出【登录操作菜单】（编号选项），把编号写进指令文件即可代点（无需记任何指令）：
  ```bash
  echo 1 | sudo tee /opt/douyin_download_bot/forwarder_data/agent_cmd.txt        # 例：点击「接收短信验证码」
  echo 123456 | sudo tee /opt/douyin_download_bot/forwarder_data/agent_cmd.txt   # 收到短信后：自动填入
  echo "123456 go" | sudo tee /opt/douyin_download_bot/forwarder_data/agent_cmd.txt  # 填入并回车提交（一条完成）
  ```
  ⏱ 时间提醒：二维码约 1 分钟过期（截图每 25 秒自动刷新，日志每次刷新都会重新输出最新链接）；
  短信验证码官方限制约 60 秒一次、有效期约 5 分钟（连续获取时服务会提示冷却剩余秒数）。
- 扫码窗口默认 300 秒；**超时会自动重开新窗口，可以继续扫到成功为止**。想更从容可先调大窗口：
  ```bash
  sudo sed -i 's/^DOUYIN_QR_TIMEOUT=.*/DOUYIN_QR_TIMEOUT=900/' /opt/douyin_download_bot/.env
  grep -n "QR_TIMEOUT" /opt/douyin_download_bot/.env    # 确认显示 900
  sudo chown douyin:douyin /opt/douyin_download_bot/.env
  ```
- 若日志出现「验证码中间页」（xvfb 里无法拖滑块）：说明该服务器 IP 风控较严，可间隔一段时间或更换网络环境重试。

### 方式 ②：通过 systemd 服务扫码（滚动重试，不怕 SSH 断线）

直接按第 5 节启动服务即可：服务检测到未登录时会**自动进入扫码模式**（二维码同样截图到 `forwarder_data/login_qr.png`）；
超时失败后 systemd 会自动重启服务（每 ~10 秒），**相当于每 5 分钟自动重开一个新窗口**，扫到成功为止，全程不受 SSH 断线影响。

### 方式 ③：服务器本身有桌面（VNC / RDP）

```bash
DOUYIN_HEADLESS=false .venv/bin/python run.py login
```
直接在弹出的浏览器里扫码即可。

---

## 5. 启动服务

> ⚠️ 顺序很重要：**先完成第 4 节登录，再启动服务**。如果服务已经启动（或在循环重启），执行登录命令前必须先
> `sudo systemctl stop douyin-forward`——否则浏览器 profile 会被服务进程占用，登录命令会因无法启动浏览器而失败。

```bash
sudo systemctl enable --now douyin-forward
journalctl -u douyin-forward -f            # 实时日志
sudo systemctl status douyin-forward       # 状态
```

看到「抖音转发解析服务启动」与「小号登录态有效 ✅」后（多会话模式还会打印 `已锁定 N 个目标会话，仅处理这些会话内的转发`），即可在手机上的**大号**里把视频转发 / 分享到小号的私信，稍候片刻：

- 新视频会实时打印解析全程日志；
- 结果归档到 `/opt/douyin_download_bot/output/<时间戳>_<视频ID>/`。

> **重启与补处理**：解析记录持久化在 `state.json`——已成功下载的视频重启后**不会重复下载**；
> 解析到一半被中断（例如下载中重启服务）的视频，重启后**会自动补处理**（日志出现 `恢复未完成的消息…`）。
> 只有两类不会重试：①「首轮水位跳过的历史消息」（除非设 `DOUYIN_PROCESS_HISTORY=true`）；②「失败 3 次已放弃的」（重新转发一次可触发重试）。

---

## 6. 推送到 Telegram 群组/频道（可选）

希望解析完成后自动把视频发送到你的 Telegram 群组/频道：

1. 按 README「推送到 Telegram 群组/频道」章节申请 `API_ID` / `API_HASH`（my.telegram.org）与 `BOT_TOKEN`（@BotFather），并把机器人加入目标群组/频道（频道需设为管理员并有发帖权限）；
2. 编辑 `/opt/douyin_download_bot/.env`：

   ```ini
   TG_PUSH=true
   TG_TARGET_CHAT=@你的频道用户名      # 或 -100 开头的数字ID
   API_ID=1234567
   API_HASH=xxxxxxxxxxxxxxxx
   BOT_TOKEN=123456789:AAxxxxxxxx
   ```

3. 重启服务：`sudo systemctl restart douyin-forward`，日志出现 `TG 推送已就绪 ✅ 目标: ...` 即成功。

> - 服务器需能访问 Telegram：海外服务器一般直连可用；**国内服务器请配置 `BOT_PROXY=http://127.0.0.1:7890`**；
> - 如需与原项目完全一致的媒体处理（转码/长图切割等），设置 `FORWARDER_SKIP_MEDIA_PROCESSING=false` 并安装 ffmpeg（`apt-get install -y ffmpeg`）。

---

## 7. 日常运维

| 操作 | 命令 |
| --- | --- |
| 查看日志 | `journalctl -u douyin-forward -f`；文件日志 `parse_hub_bot/logs/bot.log` |
| 修改配置 | 编辑 `/opt/douyin_download_bot/.env` 后 `sudo systemctl restart douyin-forward` |
| 重启 / 停止 | `sudo systemctl restart douyin-forward` / `sudo systemctl stop douyin-forward` |
| 更新 Chromium | `sudo -u douyin -H .venv/bin/playwright install chromium` |
| 更新解析能力 | `sudo -u douyin git -C /opt/douyin_download_bot/parse_hub_bot pull origin main`（git 子模块，解析链路随上游升级） |
| 更新转发链路代码 | 服务器：`sudo -u douyin git -C /opt/douyin_download_bot pull && sudo systemctl restart douyin-forward`；或本地修改后重新打包上传覆盖（保留 `.env` 与 `forwarder_data/`） |
| 重置解析记录 | `rm forwarder_data/state.json && sudo systemctl restart douyin-forward` |
| 更换/追加监测账号（免重启） | `echo "小明,小红" \| sudo tee .../forwarder_data/agent_cmd.txt`（设为两个）；`echo "+小刚" \| sudo tee ...`（追加）；`echo "-小明" \| sudo tee ...`（移除）；`echo reset \| sudo tee ...`（恢复 .env 默认值） |
| 重配监测模式（改 .env） | `sudo bash /opt/douyin_download_bot/deploy/configure_accounts.sh`（单会话/多会话交互选择），再 `sudo systemctl restart douyin-forward` |
| 登录/验证期间远程操作 | `echo 1 \| sudo tee .../forwarder_data/agent_cmd.txt` 点击菜单第 1 项；`echo 123456 \| sudo tee ...` 填验证码（详见 README「远程控制台」） |
| 重置登录态 | `rm -rf forwarder_data/browser_profile && sudo systemctl restart douyin-forward`（需重新登录） |
| 备份登录态 | 定期打包 `forwarder_data/browser_profile/` 留存，登录失效时可快速恢复 |
| 设置时区 | `sudo timedatectl set-timezone Asia/Shanghai`（日志时间更直观，可选） |

### 磁盘管理（现已默认自动）

- `forwarder_data/downloads/` 是解析临时目录，**自动清理**，目录为空属正常；
- `output/` 归档：**TG 推送成功即自动删除本地副本**（`FORWARDER_CLEANUP_AFTER_PUSH=true`，仅当推送启用且成功时）；
  其余内容按 `FORWARDER_CACHE_TTL_HOURS`（默认 **72 小时**）定时清理（每小时检查一次，启动时也先清理一次）；
- 不用 TG 推送、想长期保留归档：设 `FORWARDER_CACHE_TTL_HOURS=0` 关闭定时清理；
- 清理**不影响去重**：`state.json` 保存视频 ID 指纹，重复转发照常跳过，不会重新下载；
- 日志自动滚动（单文件 10 MB）。

---

## 8. 故障排查（可能遇到的问题）

按阶段分类，先定位到对应小节再对号入座。排查通用口诀：**先看日志** `journalctl -u douyin-forward -f`；多数问题日志里都有直接提示。

### 8.1 部署 / 依赖

| 现象 | 处理 |
| --- | --- |
| `systemctl status` 显示 failed，日志报找不到 python | `.venv` 未创建成功：重新执行 `bash deploy/install_ubuntu.sh` |
| pip 安装报「找不到 parsehub」或版本过低 | 换腾讯云镜像：`pip install -r requirements-forwarder.txt -i https://mirrors.cloud.tencent.com/pypi/simple`（清华/阿里镜像落后，`parsehub>=2.3.5` 只有腾讯云/官方源有） |
| pip 安装 `tgcrypto` 报编译错误 | **忽略即可**：它是 Telegram 加密加速的可选项，本项目用不到，依赖清单已剔除 |
| Playwright 浏览器启动报缺 `.so` 库 | `sudo .venv/bin/playwright install-deps chromium` 后重启服务 |
| 二维码/截图里的中文显示为方框/乱码 | 缺中文字体：`sudo apt-get install -y fonts-noto-cjk`（一键脚本已包含） |
| Python 版本低于 3.12 | 脚本会自动装 `python3.12`；手动装见第 3 节 deadsnakes PPA 说明 |
| `git clone` 后 `parse_hub_bot/` 是空的 | 克隆时漏了 `--recursive`：`cd /opt/douyin_download_bot && git submodule update --init --recursive` |
| 项目路径含空格 | 换无空格路径（推荐 `/opt/douyin_download_bot`），systemd 更省心 |

### 8.2 登录 / 风控

| 现象 | 处理 |
| --- | --- |
| 出现「验证码中间页」（滑块） | 无头 + 全新 profile 易触发。用**有界面**方式扫码（第 4 节方式 ①/②）；或间隔一段时间、更换网络出口重试 |
| 出现「身份验证」页（异地登录二次验证） | 日志自动给出【登录操作菜单】：`echo 1 \| sudo tee .../agent_cmd.txt` 点击「接收短信验证码」→ 短信到后 `echo "123456 go" \| ...` 填入并提交；或按菜单选「使用密码验证」 |
| 二维码扫了没反应 / 提示已过期 | 二维码约 1 分钟过期——每次扫**最新**一张（日志每次刷新重新输出最新链接，或直接打开 `http://<IP>:8765/` 看自动刷新的预览页） |
| 短信验证码提示「操作频繁」 | 官方约 60 秒才能获取一次，等冷却后再点（菜单里也会提示剩余秒数） |
| 登录命令报「profile 被占用」 | 服务正在运行占用了 profile：先 `sudo systemctl stop douyin-forward`，登录完成再 `start` |
| 反复提示「未检测到有效登录态」 | 登录态失效，或**跨机器复制了 browser_profile**（Cookie 按机器加密、复制后无法解密）→ 必须在服务器本机重新扫码（第 4 节） |
| xvfb 里拖不了滑块 | xvfb/无头都无法拖滑块：换有桌面环境（VNC/RDP，第 4 节方式 ③）或换网络重试 |

### 8.3 运行 / 解析

| 现象 | 处理 |
| --- | --- |
| 转发后**没解析** | 首次启动只记录「水位」，只处理**启动之后**新转发的视频；想处理历史：`DOUYIN_PROCESS_HISTORY=true` 并删除 `state.json` 后重启 |
| 提示「未找到私信会话」 | ① 大号给小号发过消息；② 监测昵称与大号昵称匹配（`echo "昵称" \| sudo tee .../agent_cmd.txt` 即时更换）；③ 互相关注、把消息移出「陌生人消息」；④ `FORWARDER_DUMP_HTML=true` 转储页面排查 |
| 解析到了**别的会话/群聊**的内容 | 会话锁定没生效：确认监测昵称唯一、不含会误匹配的子串；日志应有 `已锁定 N 个目标会话` |
| 提取到的链接不对（活动推广位垃圾链接） | 已用「消息接口数据 itemId」根治，正常不会出现；若复现开 `FORWARDER_DUMP_HTML=true` 把 HTML 发出来排查 |
| 日志出现「检测到重复，已跳过」 | 正常：该作品此前已处理过（重复转发/不同链接同一视频），视频 ID 级去重；要重新处理就删 `state.json` |
| 解析失败（自动重试 3 次后放弃） | 看日志具体错误；删除/私密作品解析失败属正常；偶发失败把该视频重新转发一次可触发重试 |
| 多会话没全部监测到 | 确认 `DOUYIN_MAIN_ACCOUNTS` 逗号分隔；看日志 `已锁定 N 个` 的 N 是否符合预期；昵称需精确（包含匹配） |

### 8.4 Telegram 推送（可选功能）

| 现象 | 处理 |
| --- | --- |
| `TG_PUSH=true` 但没推送 | 看启动日志：① `API_ID`/`API_HASH`/`BOT_TOKEN` 缺一不可；② `TG_TARGET_CHAT` 没填 |
| 日志「TG 推送初始化失败」 | ① 机器人没进目标群/频道（**频道需设为管理员并勾选发帖权限**）；② `TG_TARGET_CHAT` 格式错误；③ 服务器连不上 Telegram |
| 国内服务器连不上 Telegram | 配置 `BOT_PROXY=http://127.0.0.1:7890`（或 socks5://…）后重启 |
| 推送成功但想保留本地副本 | `.env` 设 `FORWARDER_CLEANUP_AFTER_PUSH=false` |

### 8.5 预览链接 / 磁盘

| 现象 | 处理 |
| --- | --- |
| 预览链接（8765）打不开 | ① 云安全组未放行 8765（建议仅放行自己 IP）；② AWS 等 NAT 实例未设 `FORWARDER_SHOT_HOST`（自动探测到内网 IP）；③ 或 SSH 隧道：`ssh -L 8765:127.0.0.1:8765 <用户>@<服务器IP>` 后访问 `http://127.0.0.1:8765/` |
| 不想开放预览端口 | `.env` 设 `FORWARDER_SHOT_PORT=0` 关闭；或 `FORWARDER_SHOT_BIND=127.0.0.1` 仅本机 + 隧道访问 |
| `output/` 目录持续增大 | 默认自动清理：推送成功即删本地副本 + 超过 `FORWARDER_CACHE_TTL_HOURS`（默认 72h）定时清理；想长期保留设 `FORWARDER_CACHE_TTL_HOURS=0` |
| 想观察浏览器实际画面 | 服务器加装 VNC（如 `x11vnc`），用第 4 节方式 ③ 登录观察 |

---

## 9. 安全与合规

- 服务默认只发出请求、无需入站端口；「登录预览链接」会监听 `8765`（**只读**、仅白名单截图/菜单文件，不含登录态），
  如启用请仅在安全组放行**你自己的 IP**，登录完成后可用 `FORWARDER_SHOT_PORT=0` 关闭；
  ⚠️ 二维码等同登录凭证，切勿把预览端口暴露给公网所有人；
- 服务以普通用户（默认 `douyin`）运行，项目目录归属该用户，遵循最小权限；
- 使用自动化操作自己的账号存在平台风控风险，**请使用专门小号**并保持默认的保守频率（轮询 20s / 解析间隔 8s）；
- 请遵守抖音用户协议，仅用于个人保存自己账号可访问的内容。

---

## 10. 功能验收清单（更新后逐项确认生效）

> 适用版本：commit `043a5cf` 及以后（登录编号菜单 / 截图链接 / 作品去重 / 对话式加账号 / 静音暂停 / 缓存清理 / 多会话）。
> 更新命令：`sudo systemctl stop douyin-forward && cd /opt/douyin_download_bot && sudo -u douyin git pull && sudo systemctl start douyin-forward`

**基础检查（1 分钟）**

```bash
cd /opt/douyin_download_bot
git log --oneline -2                                   # 应能看到 043a5cf / a6cdfc6（或更新提交）
grep -E "SHOT_PORT|SHOT_HOST|CLEANUP_AFTER_PUSH|CACHE_TTL|MAIN_ACCOUNT" .env   # 相关配置是否就位
journalctl -u douyin-forward -f                        # 实时日志
```

**逐项验收**

| # | 功能 | 怎么验证 | 预期结果 |
| --- | --- | --- | --- |
| ① | 登录编号菜单 | 下次登录/验证时观察日志；或运行中执行 `echo menu \| sudo tee forwarder_data/agent_cmd.txt` | 日志出现【登录操作菜单】「回复 1 → 点击「接收短信验证码」」及 ⏱（60 秒一次 / 5 分钟有效）提醒；`echo 1 \| ...` 即执行对应点击 |
| ② | 截图/预览链接 | 登录期间看日志；或 `echo menu \| ...` 后查看 `agent_help.txt` 链接 | 日志出现【登录预览链接】`http://<IP>:8765/login_qr.png?t=…`；**每次二维码刷新（25 秒）都会重新输出最新链接**；浏览器打开 `http://<IP>:8765/` 可见预览页 |
| ③ | 作品重复检测 | 用大号把**已处理过**的视频再转发一次 | 日志 `检测到重复，已跳过: 作品已在库（视频 ID …）`，不会重复下载（缓存清理后依然有效） |
| ④ | 对话式加账号 | `echo "小明,小红" \| sudo tee forwarder_data/agent_cmd.txt`（`+追加` / `-移除` / `reset` 同理） | 日志 `✅ 已设置监测账号，当前 2 个: 小明、小红`；下一轮轮询即按新设置工作、免重启 |
| ⑤ | 视频静音暂停 | 有界面模式（VNC）观察提取链接时打开的浏览器 | 视频不会播放/出声音（无头模式无感知，属静默改进，无需操作） |
| ⑥ | 缓存自动清理 | 配好 TG 推送后转发一个新视频；或等定时清理生效 | 日志 `🧹 推送成功，已删除本地缓存: <目录名>`；`ls output/` 不再堆积。未开推送则按 `FORWARDER_CACHE_TTL_HOURS`（默认 72h）清理 |
| ⑦ | 多会话监测 | 已配置多个昵称时看启动/轮询日志；两个大号各转发一条视频 | 日志 `已锁定 2 个目标会话，仅处理这些会话内的转发: 小明（ID: …）；小红（ID: …）`，两条视频都被解析 |

**一键抽查**（服务运行期间随时执行，看最近半小时是否各就位）：

```bash
journalctl -u douyin-forward --since "30 min ago" | grep -E "已锁定|登录操作菜单|预览链接|检测到重复|已设置监测账号|已删除本地缓存"
```

任何一项对不上：把对应日志片段（`journalctl -u douyin-forward --since "10 min ago"`）发出来即可快速定位。

---

## 11. 远程控制台指令速查（forwarder_data/agent_cmd.txt）

服务运行期间（登录等待每 3 秒、日常轮询每轮）都会检查这个文件：把内容写进去（UTF-8，每行一条，可多行）即可执行，**执行后文件自动删除**。这是「登录编号菜单 / 验证码直填 / 对话式加账号」的统一入口，无需记任何指令。

| 输入 | 作用 | 适用时机 |
| --- | --- | --- |
| `1` ~ `9` | 点击【登录操作菜单】第 N 项（如 `1` = 点击「接收短信验证码」） | 登录/验证页出现菜单时 |
| `123456` | 自动填入短信验证码 | 登录流程中 |
| `123456 go` | 填入验证码并回车提交（`go` / `enter` / `回车` 均可） | 登录流程中 |
| `小明,小红` | 设为监测账号（逗号分隔 = 多会话监测） | 任意 |
| `+小刚` / `-小红` | 追加 / 移除一个（或多个）监测账号 | 任意 |
| `reset` / `重置` | 清除自定义账号，恢复 `.env` 默认值 | 任意 |
| `menu` / `菜单` / `help` | 重新输出菜单 / 使用说明（含当前监测账号列表） | 任意 |
| `force` / `立即` | 不等轮询周期，立刻执行下一轮检查 | 任意 |
| `刷新` / `refresh` | 重新截图并输出最新预览链接 | 登录期间 |

写法示例（服务器上，用 `sudo tee` 规避权限问题）：

```bash
echo "小明,小红" | sudo tee /opt/douyin_download_bot/forwarder_data/agent_cmd.txt   # 设置 / 多会话
echo "+小刚"     | sudo tee /opt/douyin_download_bot/forwarder_data/agent_cmd.txt   # 追加一个
echo "-小红"     | sudo tee /opt/douyin_download_bot/forwarder_data/agent_cmd.txt   # 移除一个
echo reset       | sudo tee /opt/douyin_download_bot/forwarder_data/agent_cmd.txt   # 恢复 .env 默认
echo menu        | sudo tee /opt/douyin_download_bot/forwarder_data/agent_cmd.txt   # 查看帮助
```

> - 菜单/帮助也会同步写入 `forwarder_data/login_menu.txt` 与 `agent_help.txt`，可随时 `cat` 查看；
> - 兼容旧版自由指令（一般用不到）：`click_text 文本` / `fill 内容` / `type 内容` / `press Enter` / `click x y` / `drag x1 y1 x2 y2` / `wait 秒`。

---

## 12. 完整配置参考（.env）

所有变量都有默认值，**一般只需设置「监测账号」与「TG 推送」两组**；其余按需覆盖。修改后 `sudo systemctl restart douyin-forward` 生效（监测账号除外——它也能免重启经控制台改）。

### 监测账号（二选一）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DOUYIN_MAIN_ACCOUNT` | 空 | 单会话：一个大号昵称 |
| `DOUYIN_MAIN_ACCOUNTS` | 空 | 多会话：多个昵称逗号分隔，优先于上一项 |

### 抖音端

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DOUYIN_CHAT_URL` | `https://www.douyin.com/chat?isPopup=1` | 私信页地址（抖音改版才需要改） |
| `DOUYIN_HEADLESS` | `true` | 无头模式；首次登录/本机调试可设 `false` |
| `DOUYIN_QR_TIMEOUT` | `300` | 扫码等待秒数（超时自动重开窗口滚动重试） |
| `DOUYIN_POLL_INTERVAL` | `20` | 私信轮询间隔（秒） |
| `DOUYIN_ONLY_OTHERS` | `true` | 只处理对方（大号）发来的消息 |
| `DOUYIN_PROCESS_HISTORY` | `false` | 首次运行是否处理历史转发 |

### Telegram 推送（可选）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `TG_PUSH` | `false` | 推送开关 |
| `TG_TARGET_CHAT` | 空 | `@用户名` / `-100` 数字 ID / t.me 链接 |
| `API_ID` / `API_HASH` / `BOT_TOKEN` | 空 | my.telegram.org 申请 + @BotFather 建机器人 |
| `BOT_PROXY` | 空 | 国内服务器代理，如 `http://127.0.0.1:7890` |

### 登录预览链接（截图变可点击链接）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FORWARDER_SHOT_PORT` | `8765` | 只读 HTTP 服务端口；`0` = 关闭 |
| `FORWARDER_SHOT_BIND` | `0.0.0.0` | 监听地址；仅本机/隧道用 `127.0.0.1` |
| `FORWARDER_SHOT_HOST` | 空 | 链接里显示的对外 IP（AWS 等 NAT 实例建议显式填公网 IP） |

### 解析 / 归档 / 清理

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FORWARDER_OUTPUT_DIR` | `./output` | 归档目录 |
| `FORWARDER_PARSE_MIN_INTERVAL` | `8` | 两次解析最小间隔（秒），防频繁请求 |
| `FORWARDER_SKIP_MEDIA_PROCESSING` | `true` | 跳过转码、归档原始文件；`false` 需装 ffmpeg |
| `FORWARDER_SAVE_METADATA` | `true` | 归档目录写入 `metadata.json` |
| `FORWARDER_CLEANUP_AFTER_PUSH` | `true` | TG 推送成功即删除本地归档 |
| `FORWARDER_CACHE_TTL_HOURS` | `72` | 本地缓存保留时长（小时）；`0` = 关闭定时清理 |

### 调试

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FORWARDER_DEBUG` | `false` | 调试日志 |
| `FORWARDER_DUMP_HTML` | `false` | 提取失败时转储私信页 HTML 到 `forwarder_data/debug/`（调选择器用） |
