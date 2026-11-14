# -*- coding: utf-8 -*-
"""服务器基础信息采集（不依赖 psutil，Linux 读 /proc，Windows 自动降级）。

用于 bot 控制台 /status /server：CPU 负载、内存、磁盘、进程/Chromium RSS、
systemd 服务状态、运行时长、转发队列等。
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

_LAST_CPU: tuple[int, int] | None = None


def _read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def cpu_sample() -> float:
    """两次采样之间的 CPU 使用率（0-100）；非 Linux 或首次采样返回 0。"""
    global _LAST_CPU
    text = _read_text("/proc/stat")
    line = text.splitlines()[0] if text else ""
    parts = line.split()
    if len(parts) < 5 or parts[0] != "cpu":
        return 0.0
    try:
        values = [int(x) for x in parts[1:]]
    except ValueError:
        return 0.0
    total = sum(values)
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    if _LAST_CPU is None:
        _LAST_CPU = (total, idle)
        return 0.0
    last_total, last_idle = _LAST_CPU
    _LAST_CPU = (total, idle)
    dt = total - last_total
    di = idle - last_idle
    if dt <= 0:
        return 0.0
    return max(0.0, min(100.0, 100.0 * (dt - di) / dt))


def memory_info() -> dict[str, float] | None:
    text = _read_text("/proc/meminfo")
    if not text:
        return None
    values: dict[str, float] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if not parts:
            continue
        try:
            kb = float(parts[0])
        except ValueError:
            continue
        values[key] = kb / 1024.0  # MiB
    total = values.get("MemTotal", 0.0)
    available = values.get("MemAvailable", values.get("MemFree", 0.0))
    swap_total = values.get("SwapTotal", 0.0)
    swap_free = values.get("SwapFree", 0.0)
    return {
        "total_mb": total,
        "available_mb": available,
        "used_mb": max(0.0, total - available),
        "swap_total_mb": swap_total,
        "swap_used_mb": max(0.0, swap_total - swap_free),
    }


def disk_info(path: Path | str) -> dict[str, float] | None:
    try:
        usage = shutil.disk_usage(str(path))
    except OSError:
        return None
    return {
        "total_gb": usage.total / 1024**3,
        "used_gb": usage.used / 1024**3,
        "free_gb": usage.free / 1024**3,
        "percent": (usage.used / usage.total * 100.0) if usage.total else 0.0,
    }


def free_gb(path: Path | str) -> float | None:
    info = disk_info(path)
    return info["free_gb"] if info else None


def uptime_seconds() -> float | None:
    text = _read_text("/proc/uptime")
    if not text:
        return None
    try:
        return float(text.split()[0])
    except (ValueError, IndexError):
        return None


def _proc_stat(pid: int) -> tuple[int, str] | None:
    """返回 (ppid, comm)。"""
    text = _read_text(f"/proc/{pid}/stat")
    if not text:
        return None
    rparen = text.rfind(")")
    lparen = text.find("(")
    if lparen < 0 or rparen <= lparen:
        return None
    comm = text[lparen + 1 : rparen]
    rest = text[rparen + 2 :].split()
    if len(rest) < 2:
        return None
    try:
        return int(rest[1]), comm
    except ValueError:
        return None


def _proc_rss_mb(pid: int) -> float:
    text = _read_text(f"/proc/{pid}/status")
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            try:
                return float(parts[1]) / 1024.0
            except (ValueError, IndexError):
                return 0.0
    return 0.0


def _descendants(root_pid: int) -> list[int]:
    parents: dict[int, int] = {}
    names: dict[int, str] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        stat = _proc_stat(pid)
        if stat:
            parents[pid] = stat[0]
            names[pid] = stat[1]
    out: list[int] = []
    stack = [root_pid]
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        out.append(pid)
        for child, parent in parents.items():
            if parent == pid and child not in seen:
                stack.append(child)
    return out


def process_rss_mb(root_pid: int | None = None) -> float | None:
    pid = int(root_pid or os.getpid())
    if not Path(f"/proc/{pid}/status").exists():
        return None
    return sum(_proc_rss_mb(p) for p in _descendants(pid))


def chrome_rss_mb(root_pid: int | None = None) -> float | None:
    pid = int(root_pid or os.getpid())
    if not Path(f"/proc/{pid}/status").exists():
        return None
    total = 0.0
    for child in _descendants(pid):
        stat = _proc_stat(child)
        if not stat:
            continue
        comm = stat[1].lower()
        if any(x in comm for x in ("chrome", "chromium", "headless_shell")):
            total += _proc_rss_mb(child)
    return total


def service_status(name: str = "douyin-forward") -> str:
    if platform.system() != "Linux" or not shutil.which("systemctl"):
        return "n/a"
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return (proc.stdout or proc.stderr).strip() or "unknown"
    except Exception:
        return "unknown"


def format_bytes_gb(value: float | None, unit: str = "GiB") -> str:
    if value is None:
        return "?"
    if unit == "MiB":
        if value >= 1024:
            return f"{value / 1024:.1f} GiB"
        return f"{value:.0f} MiB"
    return f"{value:.1f} GiB"


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}天{hours}小时"
    if hours:
        return f"{hours}小时{minutes}分"
    return f"{minutes}分"


def format_age(timestamp: float | None) -> str:
    if not timestamp:
        return "从未"
    delta = max(0.0, time.time() - timestamp)
    if delta < 60:
        return f"{delta:.0f}秒前"
    if delta < 3600:
        return f"{delta / 60:.0f}分钟前"
    if delta < 86400:
        return f"{delta / 3600:.1f}小时前"
    return f"{delta / 86400:.1f}天前"


def collect_system_info(
    settings: Any,
    runtime: Any = None,
    receiver: Any = None,
    store: Any = None,
    state: Any = None,
) -> dict[str, Any]:
    load: tuple[float, float, float] | None = None
    try:
        load = tuple(os.getloadavg())  # type: ignore[assignment]
    except (AttributeError, OSError):
        load = None

    mem = memory_info()
    disk = disk_info(settings.output_dir)
    info: dict[str, Any] = {
        "hostname": socket.gethostname(),
        "platform": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "pid": os.getpid(),
        "uptime": uptime_seconds(),
        "load": load,
        "cpu_count": os.cpu_count() or 1,
        "cpu_percent": cpu_sample(),
        "memory": mem,
        "disk": disk,
        "service": service_status(),
        "process_rss_mb": process_rss_mb(),
        "chrome_rss_mb": chrome_rss_mb(),
    }
    if runtime is not None:
        info["runtime"] = runtime
    if receiver is not None:
        info["mode"] = getattr(receiver, "effective_detect_mode", "?")
        info["browser_started_at"] = getattr(receiver, "_browser_started_at", 0.0)
    if state is not None:
        info["paused"] = bool(state.get_flag("paused"))
        info["owner_id"] = state.get_value("owner_user_id")
        info["owner_name"] = state.get_value("owner_username")
        info["failure_notify"] = state.get_value("failure_notify_enabled") != "0"
    if store is not None:
        info["open_failures"] = store.open_failure_count()
    return info
