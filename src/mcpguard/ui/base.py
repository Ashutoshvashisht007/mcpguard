"""Popup backend interface.

A backend shows one confirmation per pending request and reports the user's choice through
the bound callback ``on_decision(request_id, verdict)``. ``present`` and ``dismiss`` may be
called from the broker's event-loop thread, so implementations must be thread-safe and must
not block.
"""

from __future__ import annotations

import abc
import json
import re
from collections.abc import Callable
from typing import Any

from mcpguard.models import ApprovalRequest, Verdict

DecisionCallback = Callable[[str, Verdict], None]


class ApprovalUI(abc.ABC):
    name = "base"
    #: True when the toolkit must own the process main thread (Tk on macOS).
    needs_main_thread = False

    def __init__(self) -> None:
        self._on_decision: DecisionCallback | None = None

    def bind(self, on_decision: DecisionCallback) -> None:
        self._on_decision = on_decision

    def report(self, request_id: str, verdict: Verdict) -> None:
        if self._on_decision is not None:
            self._on_decision(request_id, verdict)

    @abc.abstractmethod
    def present(self, request: ApprovalRequest, deadline: float, pending_count: int) -> None:
        """Show a confirmation for ``request``; ``deadline`` is a ``time.time()`` timestamp."""

    @abc.abstractmethod
    def dismiss(self, request_id: str) -> None:
        """Close the confirmation for a request settled elsewhere (timeout, cancel, shutdown)."""

    def run_mainloop(self, should_stop: Callable[[], bool]) -> None:  # pragma: no cover - GUI only
        raise NotImplementedError

    def stop(self) -> None:
        """Ask a running main loop to exit (thread-safe)."""


# --------------------------------------------------------------------------- display helpers
# Characters that can make displayed text lie about what it contains: bidi overrides
# (e.g. "U+202E" reversing a file name) and zero-width characters hiding content.
_DECEPTIVE = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿]")
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def safe_text(text: str, limit: int | None = None) -> str:
    """Make untrusted text safe to *display*: deceptive/control chars become visible escapes,
    characters Tk 8.6 cannot render (outside the BMP) are escaped too."""
    text = _DECEPTIVE.sub(lambda m: f"<U+{ord(m.group()):04X}>", text)
    text = _CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}", text)
    text = "".join(c if ord(c) <= 0xFFFF else f"\\U{ord(c):08x}" for c in text)
    if limit is not None and len(text) > limit:
        text = text[:limit] + f"\n… ({len(text) - limit:,} more characters not shown)"
    return text


def format_arguments(arguments: Any, limit: int = 20000) -> str:
    if arguments is None:
        return "(no arguments)"
    try:
        text = json.dumps(arguments, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = repr(arguments)
    return safe_text(text, limit)


def hint_labels(annotations: dict[str, Any]) -> list[tuple[str, bool]]:
    """(label, is_warning) pairs for MCP tool annotations (hints are self-reported by the server)."""
    labels: list[tuple[str, bool]] = []
    if annotations.get("destructiveHint") is True:
        labels.append(("destructive", True))
    if annotations.get("readOnlyHint") is True:
        labels.append(("read-only", False))
    if annotations.get("openWorldHint") is True:
        labels.append(("reaches external systems", True))
    if annotations.get("idempotentHint") is True:
        labels.append(("idempotent", False))
    return labels


def summary_lines(request: ApprovalRequest) -> list[tuple[str, str]]:
    requester = request.source
    if request.client_name:
        requester += f" — {request.client_name} {request.client_version or ''}".rstrip()
    rows: list[tuple[str, str]] = [("Requested by", requester)]
    server = request.server_name + (f"  ({request.server_info})" if request.server_info else "")
    rows.append(("MCP server", server))
    if request.method == "tools/call":
        tool = request.tool_name or "(missing tool name)"
        if request.tool_title and request.tool_title != request.tool_name:
            tool += f"  — {request.tool_title}"
        rows.append(("Tool", tool))
    else:
        rows.append(("Method", request.method))
    rows.append(("Transport", request.transport))
    return [(k, safe_text(v, 300)) for k, v in rows]
