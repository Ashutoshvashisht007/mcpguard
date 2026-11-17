"""Filesystem locations and user settings shared by every mcpguard component.

Settings are resolved in this order (later wins):

1. Built-in defaults (the dataclass below).
2. ``$MCPGUARD_HOME/config.json`` (default home: ``~/.mcpguard``).
3. Environment variables named ``MCPGUARD_<FIELD>`` (e.g. ``MCPGUARD_DECISION_TIMEOUT=30``).
4. Command-line flags (applied by :mod:`mcpguard.cli`).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ENV_HOME = "MCPGUARD_HOME"
ENV_SOCKET = "MCPGUARD_SOCKET"

UI_CHOICES = ("auto", "tk", "osascript", "auto-allow", "auto-block")
FAIL_MODES = ("closed", "open")
DENIAL_STYLES = ("tool_error", "jsonrpc_error")


def guard_home() -> Path:
    """Directory holding the broker socket, lock file, audit log and config."""
    override = os.environ.get(ENV_HOME)
    return Path(override).expanduser() if override else Path.home() / ".mcpguard"


@dataclass(frozen=True)
class Paths:
    home: Path

    @classmethod
    def default(cls) -> Paths:
        return cls(guard_home())

    def ensure(self) -> Paths:
        """Create the home directory, readable only by the current user."""
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.home, 0o700)
        except OSError:  # pragma: no cover - e.g. read-only FS
            pass
        return self

    @property
    def socket(self) -> Path:
        override = os.environ.get(ENV_SOCKET)
        return Path(override) if override else self.home / "broker.sock"

    @property
    def tcp_endpoint_file(self) -> Path:
        """Windows only: port + auth token of the TCP broker (no AF_UNIX in asyncio there)."""
        return self.home / "broker.json"

    @property
    def lock_file(self) -> Path:
        return self.home / "daemon.lock"

    @property
    def audit_log(self) -> Path:
        return self.home / "audit.jsonl"

    @property
    def daemon_log(self) -> Path:
        return self.home / "daemon.log"

    @property
    def config_file(self) -> Path:
        return self.home / "config.json"

    @property
    def gateway_token_file(self) -> Path:
        return self.home / "gateway-token"

    @property
    def demo_notes_dir(self) -> Path:
        return self.home / "demo-notes"


@dataclass(frozen=True)
class Settings:
    #: Seconds a request waits for a click before it is blocked automatically.
    decision_timeout: float = 120.0
    #: Popup backend: auto (tk, else osascript on macOS) | tk | osascript | auto-allow | auto-block.
    ui: str = "auto"
    #: What interceptors do when the approval daemon cannot be reached.
    fail_mode: str = "closed"
    #: How a blocked tools/call is reported: an ``isError`` tool result Claude can read and explain,
    #: or a JSON-RPC error object.
    denial_style: str = "tool_error"
    #: JSON-RPC methods that require approval. Add e.g. "resources/read" or "prompts/get".
    intercept_methods: tuple[str, ...] = ("tools/call",)
    #: Let interceptors launch the daemon on demand if it is not running.
    autostart_daemon: bool = True
    #: Append every decision to ``audit.jsonl``.
    audit: bool = True
    #: Argument keys (case-insensitive substrings) whose values are masked in the audit log.
    redact_keys: tuple[str, ...] = (
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "private_key",
        "credential",
    )
    #: Milliseconds the Allow button stays disabled after a popup appears (guards against
    #: a keystroke or click meant for another window landing on "Allow").
    allow_arm_delay_ms: int = 700

    @classmethod
    def load(cls, paths: Paths | None = None, env: dict[str, str] | None = None) -> Settings:
        paths = paths or Paths.default()
        env = os.environ if env is None else env
        values: dict[str, Any] = {}
        if paths.config_file.exists():
            try:
                raw = json.loads(paths.config_file.read_text("utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("top level must be an object")
                values.update(raw)
            except (OSError, ValueError) as exc:
                log.warning("ignoring unreadable %s: %s", paths.config_file, exc)
        for f in fields(cls):
            key = f"MCPGUARD_{f.name.upper()}"
            if key in env:
                values[f.name] = env[key]
        return cls().merged(values)

    def merged(self, values: dict[str, Any]) -> Settings:
        """Return a copy with ``values`` coerced to the right types and validated."""
        known = {f.name: f for f in fields(self)}
        updates: dict[str, Any] = {}
        for name, value in values.items():
            if value is None:
                continue
            if name not in known:
                log.warning("unknown setting %r ignored", name)
                continue
            default = getattr(self, name)
            updates[name] = _coerce(name, value, default)
        result = replace(self, **updates)
        result.validate()
        return result

    def validate(self) -> None:
        if self.ui not in UI_CHOICES:
            raise ValueError(f"ui must be one of {UI_CHOICES}, got {self.ui!r}")
        if self.fail_mode not in FAIL_MODES:
            raise ValueError(f"fail_mode must be one of {FAIL_MODES}, got {self.fail_mode!r}")
        if self.denial_style not in DENIAL_STYLES:
            raise ValueError(f"denial_style must be one of {DENIAL_STYLES}, got {self.denial_style!r}")
        if not 1 <= self.decision_timeout <= 3600:
            raise ValueError("decision_timeout must be between 1 and 3600 seconds")


def _coerce(name: str, value: Any, default: Any) -> Any:
    try:
        if isinstance(default, bool):
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if isinstance(default, int):
            return int(value)
        if isinstance(default, float):
            return float(value)
        if isinstance(default, tuple):
            if isinstance(value, str):
                return tuple(v.strip() for v in value.split(",") if v.strip())
            return tuple(str(v) for v in value)
        return str(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid value for {name}: {value!r}") from exc


def setup_logging(default_level: str = "INFO") -> None:
    """Log to stderr only — stdout is the MCP channel for the stdio wrapper."""
    level = os.environ.get("MCPGUARD_LOG_LEVEL", default_level).upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s [mcpguard:%(name)s] %(message)s",
    )


__all__ = ["Paths", "Settings", "guard_home", "setup_logging"]
