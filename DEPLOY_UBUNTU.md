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
| 端口 | **无需开放任何入站端口**（服务只做出站请求） |
| 账号 | 建议使用专门的**小号**，并与大号互相关注 |

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
$skip = @('forwarder_data','output')      # 排除运行数据；如需同步登录态见第 4 节
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
5. 生成 `.env`（若不存在）并安装 systemd 服务 `douyin-forward`（**安装但不启动**）；
6. 打印「首次登录 + 启动服务」的后续步骤。

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

# ④ 配置
cp .env.example .env && nano .env        # 填 DOUYIN_MAIN_ACCOUNT

# ⑤ systemd 服务
sudo sed -e "s|__APP_USER__|$RUN_AS|g" \
         -e "s|__PROJECT_DIR__|/opt/douyin_download_bot|g" \
         -e "s|__APP_HOME__|$HOME|g" \
         deploy/douyin-forward.service > /etc/systemd/system/douyin-forward.service
sudo systemctl daemon-reload
```

---

## 4. 首次登录（关键步骤）

> 背景：抖音对"无头浏览器 + 全新指纹"会返回「**验证码中间页**」（滑块验证），全新 profile 的无头登录大概率被拦。
> 因此**首次登录必须让浏览器处于"有界面"状态**。登录成功一次后，profile 变为"可信"，之后无头模式常驻即可（本机实测通过）。

### 方案 ①（推荐先试）：服务器上 xvfb 有界面登录

```bash
sudo -u douyin -H env DOUYIN_HEADLESS=false \
  /usr/bin/xvfb-run -a /opt/douyin_download_bot/.venv/bin/python \
  /opt/douyin_download_bot/run.py login
```

- 二维码会自动截图到 `forwarder_data/login_qr.png`，下载到本地扫码（**用你 ssh 登录服务器的账号**，如 root / ubuntu；`douyin` 是服务用户，通常没有 SSH 权限）：
  ```bash
  scp <登录用户>@<服务器IP>:/opt/douyin_download_bot/forwarder_data/login_qr.png .
  ```
- 若看到日志出现「验证码中间页」提示 → xvfb 环境无法手动拖滑块，转方案 ②。

### 方案 ②（保底可行）：本地登录 + 同步浏览器 profile

1. **本地（Windows）** 有桌面环境，直接登录（必要时手动过滑块）：
   ```powershell
   cd <本地项目目录>
   $env:DOUYIN_HEADLESS="false"; python run.py login     # 扫码登录成功
   Compress-Archive -Path .\forwarder_data\browser_profile -DestinationPath .\browser_profile.zip -Force
   scp .\browser_profile.zip ubuntu@<服务器IP>:/tmp/
   ```
2. **服务器**解压并修正权限：
   ```bash
   cd /opt/douyin_download_bot
   sudo unzip -o /tmp/browser_profile.zip -d forwarder_data/
   sudo chown -R douyin:douyin forwarder_data
   ```
3. 完成后启动服务（第 5 节），日志出现「小号登录态有效 ✅」即成功。

> 小提示：若同步后启动报 profile 相关错误，删除 `forwarder_data/browser_profile/SingletonLock`、`SingletonCookie`、`SingletonSocket` 三个锁文件后重试。

### 方案 ③：服务器本身有桌面（VNC/RDP）

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

看到「抖音转发解析服务启动」与「小号登录态有效 ✅」后，即可在手机上的**大号**里把视频转发 / 分享到小号的私信，稍候片刻：

- 新视频会实时打印解析全程日志；
- 结果归档到 `/opt/douyin_download_bot/output/<时间戳>_<视频ID>/`。

---

## 6. 日常运维

| 操作 | 命令 |
| --- | --- |
| 查看日志 | `journalctl -u douyin-forward -f`；文件日志 `parse_hub_bot/logs/bot.log` |
| 修改配置 | 编辑 `/opt/douyin_download_bot/.env` 后 `sudo systemctl restart douyin-forward` |
| 重启 / 停止 | `sudo systemctl restart douyin-forward` / `sudo systemctl stop douyin-forward` |
| 更新 Chromium | `sudo -u douyin -H .venv/bin/playwright install chromium` |
| 更新解析能力 | `sudo -u douyin git -C /opt/douyin_download_bot/parse_hub_bot pull origin main`（git 子模块，解析链路随上游升级） |
| 更新转发链路代码 | 服务器：`sudo -u douyin git -C /opt/douyin_download_bot pull && sudo systemctl restart douyin-forward`；或本地修改后重新打包上传覆盖（保留 `.env` 与 `forwarder_data/`） |
| 重置解析记录 | `rm forwarder_data/state.json && sudo systemctl restart douyin-forward` |
| 重置登录态 | `rm -rf forwarder_data/browser_profile && sudo systemctl restart douyin-forward`（需重新登录） |
| 备份登录态 | 定期打包 `forwarder_data/browser_profile/` 留存，登录失效时可快速恢复 |
| 设置时区 | `sudo timedatectl set-timezone Asia/Shanghai`（日志时间更直观，可选） |

### 磁盘管理

- `forwarder_data/downloads/` 是解析临时目录，**自动清理**，目录为空属正常；
- 视频归档在 `output/` 下，**不会自动清理**，请定期整理 / 转移；
- 日志自动滚动（单文件 10 MB）。

---

## 7. 故障排查

| 现象 | 处理 |
| --- | --- |
| `systemctl status` 显示 failed，日志报找不到 python | `.venv` 未创建成功：重新执行 `bash deploy/install_ubuntu.sh` |
| 浏览器启动失败，报缺 `.so` 库 | `sudo .venv/bin/playwright install-deps chromium` 后重启服务 |
| 日志提示「profile 被占用」 | 有其它进程在使用 `forwarder_data/browser_profile`：`sudo systemctl stop douyin-forward`，`pkill -f run.py`，再重试 |
| 登录命令报 profile 被占用 | 服务正在运行占用了 profile：`sudo systemctl stop douyin-forward` 后登录，登录完成再 `sudo systemctl start douyin-forward` |
| 提示「验证码中间页」 | 见第 4 节方案 ②（本地登录 + 同步 profile）；或等待一段时间再试 |
| 提示「未找到私信会话」 | 检查 `.env` 的 `DOUYIN_MAIN_ACCOUNT` 是否与大号昵称匹配；确认大号已给小号发过消息；开 `FORWARDER_DUMP_HTML=true` 重启查看转储 HTML |
| 转发后没解析 | 首次启动只记录"水位"，只处理**启动之后**新转发的视频；想处理历史：设 `DOUYIN_PROCESS_HISTORY=true` 并删除 `state.json` 后重启 |
| 解析失败（自动重试 3 次后放弃） | 查看日志中的具体错误；抖音解析偶发失败可把该视频重新转发一次触发重试 |
| xvfb 登录时二维码扫不了 | 确认 `forwarder_data/login_qr.png` 在持续更新（每 ~45s 刷新）；重新执行登录命令 |
| 想观察浏览器实际画面 | 服务器加装 VNC（如 `x11vnc`）后可看到；或改用方案 ② 本地调试 |

---

## 8. 安全与合规

- 服务只发出请求，无需开放任何入站端口，**请不要**将调试端口暴露到公网；
- 服务以普通用户（默认 `douyin`）运行，项目目录归属该用户，遵循最小权限；
- 使用自动化操作自己的账号存在平台风控风险，**请使用专门小号**并保持默认的保守频率（轮询 20s / 解析间隔 8s）；
- 请遵守抖音用户协议，仅用于个人保存自己账号可访问的内容。
