# -*- coding: utf-8 -*-
"""登录截图 / 菜单的只读 HTTP 服务。

把服务器上的「登录二维码截图、页面预览截图、菜单文本」变成可点击的链接，
直接写进日志（如 http://<host>:8765/login_qr.png?t=1729578123），
浏览器打开即可看到最新画面——不再依赖 scp 来回下载。

安全设计（重要）：
  - 只暴露白名单文件（二维码 / 页面预览 / 验证码页截图 / 菜单文本），
    绝不暴露 browser_profile（登录态）、state.json 等敏感内容；
  - 路径解析只接受字面量文件名（拒绝任何目录层级），杜绝目录穿越；
  - 每次请求返回 Cache-Control: no-store，配合日志链接里的 ?t=时间戳，
    保证看到的永远是最新画面（浏览器不会缓存旧二维码）；
  - 端口可配置（FORWARDER_SHOT_PORT，0 = 关闭）；建议把该端口限制为
    你自己的 IP，或使用 SSH 隧道访问（见 README 对应章节）。
"""

from __future__ import annotations

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from log import logger

logger = logger.bind(name="Forwarder/ShotServer")

# 白名单：仅允许访问这些文件名（内容均为"给人看"的截图/文本，不含登录态）
ALLOWED_FILES: dict[str, str] = {
    "login_qr.png": "image/png",
    "login_page_state.png": "image/png",
    "login_captcha.png": "image/png",
    "login_menu.txt": "text/plain; charset=utf-8",
    "agent_help.txt": "text/plain; charset=utf-8",
}


def _detect_host() -> str:
    """探测一个"从外部浏览器可访问"的主机名/IP（失败时回退 localhost）。"""
    # 1) 外网服务回显（能拿到公网 IP 最好；3 秒超时，失败忽略）
    try:
        import urllib.request

        with urllib.request.urlopen("https://api.ipify.org", timeout=3) as resp:
            ip = resp.read().decode("ascii", "ignore").strip()
            if ip:
                return ip
    except Exception:
        pass
    # 2) 本机出口网卡 IP（VPS 直连公网 / 局域网时可用）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return "localhost"


class _Handler(BaseHTTPRequestHandler):
    """极简只读服务：白名单文件 + 一个索引页。"""

    server_version = "ForwarderShot/1.0"

    # 静默访问日志，避免污染主日志
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        pass

    # ── 路径 → 文件名（仅接受字面量文件名，杜绝目录穿越） ──
    def _resolve(self) -> str | None:
        path = unquote(urlsplit(self.path).path or "/")
        name = path.strip("/")
        if not name or "/" in name or "\\" in name or ".." in name:
            return None
        return name if name in ALLOWED_FILES else None

    def _directory(self) -> Path:
        return self.server.directory  # type: ignore[attr-defined]

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except Exception:
                pass

    def _index(self) -> None:
        ts = int(time.time())
        rows = [
            f'<li><a href="/{name}?t={ts}">{name}</a></li>'
            for name in ALLOWED_FILES
            if (self._directory() / name).exists()
        ]
        html = (
            "<html><head><meta charset='utf-8'><title>登录预览</title>"
            "<meta http-equiv='refresh' content='15'></head><body>"
            "<h3>登录预览（每 15 秒自动刷新，始终最新）</h3><ul>"
            + ("".join(rows) or "<li>（暂无文件；服务运行并截图后会出现在这里）</li>")
            + "</ul><p>本服务仅提供登录二维码 / 页面截图 / 菜单文本，不含任何登录态文件。</p>"
            "</body></html>"
        )
        self._send(200, "text/html; charset=utf-8", html.encode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        name = self._resolve()
        if name is None:
            self._index()
            return
        path = self._directory() / name
        try:
            body = path.read_bytes()
        except Exception:
            self._send(404, "text/plain; charset=utf-8", b"not found (file not generated yet)")
            return
        self._send(200, ALLOWED_FILES[name], body)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr: tuple[str, int], directory: Path) -> None:
        super().__init__(addr, _Handler)
        self.directory = directory


class ShotServer:
    """登录截图 / 菜单的只读 HTTP 服务（后台线程运行，随主服务生命周期启停）。"""

    def __init__(
        self,
        directory: Path,
        *,
        port: int,
        bind: str = "0.0.0.0",
        public_host: str = "",
    ) -> None:
        self.directory = Path(directory)
        self.port = int(port)
        self.bind = bind or "0.0.0.0"
        self.public_host = public_host.strip()
        self.base_url = ""
        self._httpd: _Server | None = None

    # ────────────────────────── 生命周期 ──────────────────────────

    def start(self) -> bool:
        """启动服务；返回是否可用（端口为 0 / 被占用 / 其它异常时返回 False，不影响主流程）。"""
        if self.port <= 0:
            return False
        if self._httpd is not None:
            return True
        try:
            httpd = _Server((self.bind, self.port), self.directory)
        except OSError as e:
            logger.warning(f"登录预览 HTTP 服务启动失败（{self.bind}:{self.port} -> {e}），日志将改用本地文件路径")
            return False

        self._httpd = httpd
        threading.Thread(target=httpd.serve_forever, name="forwarder-shot-server", daemon=True).start()

        host = self.public_host or _detect_host()
        self.base_url = f"http://{host}:{self.port}"
        logger.info(f"登录预览服务已启动: {self.base_url}（截图/菜单可直接在浏览器查看；FORWARDER_SHOT_PORT=0 可关闭）")
        if host != "localhost":
            logger.debug(
                "提示：若该链接打不开，请在云安全组放行端口或使用 SSH 隧道（README「登录预览链接」章节）；"
                "也可用 FORWARDER_SHOT_HOST 指定对外 IP。"
            )
        return True

    def stop(self) -> None:
        if self._httpd is None:
            return
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception:
            pass
        self._httpd = None

    # ────────────────────────── 链接 ──────────────────────────

    def url(self, name: str) -> str:
        """生成带时间戳的访问链接（每次调用都不同，保证浏览器不缓存旧图）。"""
        if not self.base_url:
            return ""
        return f"{self.base_url}/{name}?t={int(time.time())}"

    @property
    def running(self) -> bool:
        return self._httpd is not None
