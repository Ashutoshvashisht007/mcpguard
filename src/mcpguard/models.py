"""Data passed between interceptors, the approval broker and the UI."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Verdict(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"


class Reason(str, Enum):
    USER = "user"  # the user clicked a button
    TIMEOUT = "timeout"  # nobody answered in time
    CANCELLED = "cancelled"  # the client cancelled / disconnected while waiting
    SHUTDOWN = "shutdown"  # the daemon is stopping
    UNAVAILABLE = "daemon_unavailable"  # interceptor could not reach the daemon
    ERROR = "error"  # anything unexpected


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: Reason
    detail: str = ""

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW

    def to_wire(self) -> dict[str, Any]:
        return {"verdict": self.verdict.value, "reason": self.reason.value, "detail": self.detail}

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> Decision:
        return cls(Verdict(data["verdict"]), Reason(data["reason"]), str(data.get("detail", "")))

    @classmethod
    def block(cls, reason: Reason, detail: str = "") -> Decision:
        return cls(Verdict.BLOCK, reason, detail)


@dataclass
class ApprovalRequest:
    """Everything the user needs to judge one pending MCP request."""

    source: str  # e.g. "Claude Desktop" / "Claude Web"
    transport: str  # "stdio" | "http"
    server_name: str  # name of the MCP server (config key / gateway name)
    method: str  # JSON-RPC method, normally "tools/call"
    jsonrpc_id: Any = None
    tool_name: str | None = None
    arguments: Any = None
    client_name: str | None = None  # from initialize.clientInfo or _meta (e.g. "claude-ai")
    client_version: str | None = None
    server_info: str | None = None  # serverInfo name/version reported by the MCP server
    tool_title: str | None = None
    tool_description: str | None = None  # from tools/list — supplied by the server, untrusted
    tool_annotations: dict[str, Any] = field(default_factory=dict)
    timeout: float | None = None  # per-request override of the daemon's decision timeout
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)

    def to_wire(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> ApprovalRequest:
        if not isinstance(data, dict):
            raise ValueError("request must be an object")
        for key in ("source", "transport", "server_name", "method"):
            if not isinstance(data.get(key), str):
                raise ValueError(f"request.{key} must be a string")
        allowed = set(cls.__dataclass_fields__)
        clean = {k: v for k, v in data.items() if k in allowed}
        if not isinstance(clean.get("tool_annotations", {}), dict):
            clean["tool_annotations"] = {}
        timeout = clean.get("timeout")
        if timeout is not None and not isinstance(timeout, (int, float)):
            clean["timeout"] = None
        return cls(**clean)

    @property
    def title(self) -> str:
        if self.method == "tools/call" and self.tool_name:
            return f"{self.server_name} → {self.tool_name}"
        return f"{self.server_name} → {self.method}"
