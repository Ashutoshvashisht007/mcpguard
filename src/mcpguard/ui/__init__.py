"""Popup backends and the factory that picks one."""

from __future__ import annotations

import sys

from mcpguard.config import Settings
from mcpguard.models import Verdict
from mcpguard.ui.base import ApprovalUI


def create_ui(settings: Settings) -> ApprovalUI:
    choice = settings.ui
    if choice == "auto-allow":
        from mcpguard.ui.auto_ui import AutoUI

        return AutoUI(Verdict.ALLOW)
    if choice == "auto-block":
        from mcpguard.ui.auto_ui import AutoUI

        return AutoUI(Verdict.BLOCK)
    if choice in ("auto", "tk"):
        from mcpguard.ui.tk_ui import TkApprovalUI, tk_available

        if tk_available():
            return TkApprovalUI(settings.allow_arm_delay_ms)
        if choice == "tk":
            raise RuntimeError("tkinter is not available (on Homebrew Python: brew install python-tk)")
    if choice in ("auto", "osascript") and sys.platform == "darwin":
        from mcpguard.ui.osascript_ui import OsaScriptUI

        return OsaScriptUI()
    raise RuntimeError(f"no popup backend available for ui={choice!r}; install tkinter")


__all__ = ["ApprovalUI", "create_ui"]
