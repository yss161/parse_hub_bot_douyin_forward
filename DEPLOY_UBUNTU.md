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

> 💡 无需配置 Telegram 相关（`API_ID` / `API_HASH` / `BOT_TOKEN`）：本服务只复用原项目的解析引擎，
> TG 机器人部分不会启动（`run.py` 启动时自动注入占位配置）。日志里 `未找到 platform_config.yaml, 跳过加载` 也属正常。

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

## 8. 故障排查

| 现象 | 处理 |
| --- | --- |
| `systemctl status` 显示 failed，日志报找不到 python | `.venv` 未创建成功：重新执行 `bash deploy/install_ubuntu.sh` |
| 浏览器启动失败，报缺 `.so` 库 | `sudo .venv/bin/playwright install-deps chromium` 后重启服务 |
| 日志提示「profile 被占用」 | 有其它进程在使用 `forwarder_data/browser_profile`：`sudo systemctl stop douyin-forward`，`pkill -f run.py`，再重试 |
| 登录命令报 profile 被占用 | 服务正在运行占用了 profile：`sudo systemctl stop douyin-forward` 后登录，登录完成再 `sudo systemctl start douyin-forward` |
| 提示「验证码中间页」 | 在服务器上以有界面方式扫码（第 4 节方式 ①/②），或间隔一段时间/换网络环境重试；**注意登录态不能跨机器复制**（原因见第 4 节警告框） |
| 提示「未找到私信会话」 | 检查监测昵称是否与大号昵称匹配（也可直接 `echo "大号昵称" \| sudo tee .../agent_cmd.txt` 即时更换）；确认大号已给小号发过消息；开 `FORWARDER_DUMP_HTML=true` 重启查看转储 HTML |
| 预览链接（8765）打不开 | ① 云安全组未放行 8765（建议仅放行自己 IP）；② AWS 等 NAT 实例未设 `FORWARDER_SHOT_HOST`（自动探测到内网 IP）；③ 或改用 SSH 隧道：`ssh -L 8765:127.0.0.1:8765 <用户>@<服务器IP>` 后访问 `http://127.0.0.1:8765/` |
| 日志出现「检测到重复，已跳过」 | 正常行为：该作品此前已处理过（重复转发 / 不同链接指向同一视频）。如需重新处理，删除 `state.json` 后重启 |
| 转发后没解析 | 首次启动只记录"水位"，只处理**启动之后**新转发的视频；想处理历史：设 `DOUYIN_PROCESS_HISTORY=true` 并删除 `state.json` 后重启 |
| 解析失败（自动重试 3 次后放弃） | 查看日志中的具体错误；抖音解析偶发失败可把该视频重新转发一次触发重试 |
| xvfb 登录时二维码扫不了 | 确认 `forwarder_data/login_qr.png` 在持续更新（每 ~25s 刷新）；重新执行登录命令 |
| 想观察浏览器实际画面 | 服务器加装 VNC（如 `x11vnc`）后可直接看到画面，并按第 4 节方式 ③ 登录 |

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
