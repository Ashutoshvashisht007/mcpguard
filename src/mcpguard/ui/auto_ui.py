"""Non-interactive backend: decides by policy. Used by tests, CI and headless demos."""

from __future__ import annotations

import threading
from collections.abc import Callable

from mcpguard.models import ApprovalRequest, Verdict
from mcpguard.ui.base import ApprovalUI


class AutoUI(ApprovalUI):
    name = "auto"

    def __init__(self, policy: Verdict | Callable[[ApprovalRequest], Verdict | None], delay: float = 0.0) -> None:
        super().__init__()
        self.policy = policy
        self.delay = delay
        self.presented: list[ApprovalRequest] = []
        self.dismissed: list[str] = []
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def present(self, request: ApprovalRequest, deadline: float, pending_count: int) -> None:
        with self._lock:
            self.presented.append(request)
        verdict = self.policy(request) if callable(self.policy) else self.policy
        if verdict is None:  # policy abstains → behave like an unanswered popup
            return
        if self.delay <= 0:
            self.report(request.id, verdict)
            return
        timer = threading.Timer(self.delay, self.report, args=(request.id, verdict))
        timer.daemon = True
        with self._lock:
            self._timers[request.id] = timer
        timer.start()

    def dismiss(self, request_id: str) -> None:
        with self._lock:
            self.dismissed.append(request_id)
            timer = self._timers.pop(request_id, None)
        if timer is not None:
            timer.cancel()
