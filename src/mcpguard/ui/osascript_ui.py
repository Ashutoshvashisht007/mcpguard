"""macOS fallback backend using the native ``display dialog`` (no Tk required).

Each request gets its own ``osascript`` process, so simultaneous requests show simultaneous
dialogs. Untrusted text is passed through ``argv`` — never spliced into AppleScript source —
so tool arguments cannot inject script. ``Block`` is both the default and the cancel button,
so Return / Esc never allow anything.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time

from mcpguard.models import ApprovalRequest, Verdict
from mcpguard.ui.base import ApprovalUI, format_arguments, hint_labels, safe_text, summary_lines

log = logging.getLogger(__name__)

_SCRIPT = """
on run argv
    set msg to item 1 of argv
    set secs to (item 2 of argv) as integer
    activate
    try
        set r to display dialog msg with title "MCP Guard — approve MCP request?" buttons {"Block", "Allow"} default button "Block" cancel button "Block" with icon caution giving up after secs
        if gave up of r then return "timeout"
        return button returned of r
    on error number -128
        return "Block"
    end try
end run
"""


class OsaScriptUI(ApprovalUI):
    name = "osascript"

    def __init__(self) -> None:
        super().__init__()
        self._procs: dict[str, subprocess.Popen[str]] = {}
        self._lock = threading.Lock()

    def present(self, request: ApprovalRequest, deadline: float, pending_count: int) -> None:
        threading.Thread(target=self._run, args=(request, deadline), daemon=True).start()

    def dismiss(self, request_id: str) -> None:
        with self._lock:
            proc = self._procs.pop(request_id, None)
        if proc is not None and proc.poll() is None:
            proc.kill()

    def _run(self, request: ApprovalRequest, deadline: float) -> None:
        lines = [f"{k}: {v}" for k, v in summary_lines(request)]
        hints = hint_labels(request.tool_annotations)
        if hints:
            lines.append("Server hints: " + ", ".join(label for label, _ in hints))
        if request.tool_description:
            lines.append("Description (from server): " + safe_text(request.tool_description, 300))
        lines.append("")
        lines.append("Arguments:")
        lines.append(format_arguments(request.arguments, limit=1500))
        seconds = max(1, int(deadline - time.time()))
        try:
            proc = subprocess.Popen(
                ["osascript", "-e", _SCRIPT, "\n".join(lines), str(seconds)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except OSError:
            log.exception("osascript unavailable")
            self.report(request.id, Verdict.BLOCK)
            return
        with self._lock:
            self._procs[request.id] = proc
        out, _ = proc.communicate()
        with self._lock:
            dismissed = self._procs.pop(request.id, None) is None
        if dismissed:
            return  # settled elsewhere; the broker already has its answer
        answer = (out or "").strip()
        if answer == "Allow":
            self.report(request.id, Verdict.ALLOW)
        elif answer in ("Block", ""):
            self.report(request.id, Verdict.BLOCK)
        # "timeout": the broker's own timer settles it
