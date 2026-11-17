"""Append-only JSONL audit trail of every approval decision (written by the daemon only)."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from mcpguard.models import ApprovalRequest, Decision

log = logging.getLogger(__name__)

MAX_ARGUMENT_CHARS = 4000
REDACTED = "[redacted]"


def redact(value: Any, keys: Iterable[str]) -> Any:
    """Deep-copy ``value`` masking any mapping entry whose key contains one of ``keys``."""
    needles = tuple(k.lower() for k in keys)

    def walk(v: Any) -> Any:
        if isinstance(v, dict):
            return {
                k: (REDACTED if isinstance(k, str) and any(n in k.lower() for n in needles) else walk(item))
                for k, item in v.items()
            }
        if isinstance(v, list):
            return [walk(item) for item in v]
        return v

    return walk(value)


class AuditLog:
    def __init__(self, path: Path, redact_keys: Iterable[str], enabled: bool = True) -> None:
        self.path = path
        self.redact_keys = tuple(redact_keys)
        self.enabled = enabled
        self._lock = threading.Lock()

    def record(self, request: ApprovalRequest, decision: Decision, decided_at: float | None = None) -> None:
        if not self.enabled:
            return
        decided_at = decided_at or time.time()
        args = json.dumps(redact(request.arguments, self.redact_keys), ensure_ascii=False, default=str)
        if len(args) > MAX_ARGUMENT_CHARS:
            args = args[:MAX_ARGUMENT_CHARS] + "…(truncated)"
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(decided_at)),
            "request_id": request.id,
            "source": request.source,
            "transport": request.transport,
            "client": request.client_name,
            "server": request.server_name,
            "method": request.method,
            "tool": request.tool_name,
            "arguments": args,
            "verdict": decision.verdict.value,
            "reason": decision.reason.value,
            "wait_ms": int((decided_at - request.created_at) * 1000),
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        try:
            with self._lock:
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as fh:
                    fh.write(line)
        except OSError as exc:  # auditing must never break request handling
            log.warning("could not write audit log %s: %s", self.path, exc)


def tail(path: Path, count: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = path.read_text("utf-8", errors="replace").splitlines()[-count:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
