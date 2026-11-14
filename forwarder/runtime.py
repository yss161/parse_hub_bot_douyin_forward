# -*- coding: utf-8 -*-
"""进程内运行状态（供 /status、/stats、告警使用；不持久化）。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class RuntimeStats:
    started_at: float = field(default_factory=time.time)
    last_poll_at: float = 0.0
    last_poll_candidates: int = 0
    consecutive_empty: int = 0
    consecutive_poll_errors: int = 0
    last_candidate_at: float = 0.0
    last_success_at: float = 0.0
    last_failure_at: float = 0.0
    last_push_at: float = 0.0
    last_error: str = ""
    queue_size: int = 0
    parse_inflight: bool = False
    browser_restarts: int = 0
    heartbeats: int = 0
    last_heartbeat_at: float = 0.0
