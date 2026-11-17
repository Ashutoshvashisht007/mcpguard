"""Tkinter popup backend (default).

Tk must own the main thread on macOS, so the daemon runs its asyncio broker on a worker thread
and calls :meth:`TkApprovalUI.run_mainloop` on the main thread. Cross-thread calls
(``present``/``dismiss``/``stop``) only enqueue commands; a periodic ``after`` callback on the
Tk thread drains the queue, so no Tk call is ever made off the main thread.

Every pending request gets its own always-on-top window (cascaded), with its own countdown —
simultaneous requests are decided independently and in any order.
"""

from __future__ import annotations

import logging
import queue
import sys
import time
from collections.abc import Callable
from typing import Any

from mcpguard.models import ApprovalRequest, Verdict
from mcpguard.ui.base import ApprovalUI, format_arguments, hint_labels, safe_text, summary_lines

log = logging.getLogger(__name__)

PUMP_MS = 100
WINDOW_WIDTH_CHARS = 78


def tk_available() -> bool:
    try:
        import tkinter  # noqa: F401
    except ImportError:
        return False
    if sys.platform.startswith("linux"):
        import os

        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True


class TkApprovalUI(ApprovalUI):
    name = "tk"
    needs_main_thread = True

    def __init__(self, allow_arm_delay_ms: int = 700) -> None:
        super().__init__()
        self._commands: queue.Queue[tuple[str, Any, Any]] = queue.Queue()
        self._windows: dict[str, _ApprovalWindow] = {}
        self._arm_delay_ms = allow_arm_delay_ms
        self._root: Any = None
        self._should_stop: Callable[[], bool] = lambda: False
        self._quit = False

    # ------------------------------------------------------------------ thread-safe API
    def present(self, request: ApprovalRequest, deadline: float, pending_count: int) -> None:
        self._commands.put(("present", request, deadline))

    def dismiss(self, request_id: str) -> None:
        self._commands.put(("dismiss", request_id, None))

    def stop(self) -> None:
        self._commands.put(("stop", None, None))

    # ------------------------------------------------------------------ Tk thread
    def run_mainloop(self, should_stop: Callable[[], bool]) -> None:  # pragma: no cover - GUI
        import tkinter as tk

        self._should_stop = should_stop
        root = tk.Tk(className="MCP Guard")
        root.withdraw()
        root.title("MCP Guard")
        self._root = root
        if sys.platform == "darwin":
            try:  # Cmd+Q / Dock "Quit" → graceful shutdown instead of an abrupt exit
                root.createcommand("tk::mac::Quit", self.stop)
            except tk.TclError:
                pass
        root.after(PUMP_MS, self._pump)
        try:
            root.mainloop()
        finally:
            for window in list(self._windows.values()):
                window.close()
            self._windows.clear()
            try:
                root.destroy()
            except tk.TclError:
                pass

    def _pump(self) -> None:  # pragma: no cover - GUI
        try:
            while True:
                try:
                    kind, payload, extra = self._commands.get_nowait()
                except queue.Empty:
                    break
                try:
                    if kind == "present":
                        self._open(payload, extra)
                    elif kind == "dismiss":
                        window = self._windows.pop(payload, None)
                        if window is not None:
                            window.close()
                    elif kind == "stop":
                        self._quit = True
                except Exception:
                    log.exception("popup command %s failed", kind)
                    if kind == "present":
                        self.report(payload.id, Verdict.BLOCK)  # fail closed
            now = time.time()
            for window in list(self._windows.values()):
                window.tick(now, len(self._windows))
        finally:
            if self._quit or self._should_stop():
                self._root.quit()
            else:
                self._root.after(PUMP_MS, self._pump)

    def _open(self, request: ApprovalRequest, deadline: float) -> None:  # pragma: no cover - GUI
        slot = len(self._windows) % 8
        window = _ApprovalWindow(self, self._root, request, deadline, slot, self._arm_delay_ms)
        self._windows[request.id] = window

    def _chosen(self, window: _ApprovalWindow, verdict: Verdict) -> None:  # pragma: no cover - GUI
        self._windows.pop(window.request.id, None)
        window.close()
        self.report(window.request.id, verdict)


class _ApprovalWindow:  # pragma: no cover - GUI
    def __init__(
        self, ui: TkApprovalUI, root: Any, request: ApprovalRequest, deadline: float, slot: int, arm_delay_ms: int
    ) -> None:
        import tkinter as tk
        from tkinter import font as tkfont
        from tkinter import ttk

        self.ui = ui
        self.request = request
        self.deadline = deadline
        self.closed = False
        self._last_countdown = ""

        top = tk.Toplevel(root)
        self.top = top
        top.title(f"MCP Guard — {safe_text(request.source, 60)} request")
        top.attributes("-topmost", True)
        top.protocol("WM_DELETE_WINDOW", lambda: ui._chosen(self, Verdict.BLOCK))
        top.bind("<Escape>", lambda _e: ui._chosen(self, Verdict.BLOCK))
        top.columnconfigure(0, weight=1)
        top.rowconfigure(0, weight=1)

        base = tkfont.nametofont("TkDefaultFont")
        size = abs(int(base.cget("size"))) or 13
        self._title_font = tkfont.Font(font=base)
        self._title_font.configure(size=size + 5, weight="bold")
        self._bold_font = tkfont.Font(font=base)
        self._bold_font.configure(weight="bold")
        mono = tkfont.nametofont("TkFixedFont")

        frame = ttk.Frame(top, padding=18)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(1, weight=1)
        row = 0

        ttk.Label(frame, text="Allow this MCP request?", font=self._title_font).grid(
            row=row, column=0, columnspan=2, sticky="w"
        )
        row += 1
        ttk.Label(
            frame,
            text="It is on hold and will not run until you choose. Closing this window blocks it.",
            foreground="#666666",
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(2, 12))
        row += 1

        for key, value in summary_lines(request):
            ttk.Label(frame, text=key, foreground="#555555").grid(row=row, column=0, sticky="nw", padx=(0, 14), pady=1)
            font = self._bold_font if key in ("Tool", "Method") else None
            ttk.Label(frame, text=value, font=font, wraplength=520, justify="left").grid(
                row=row, column=1, sticky="w", pady=1
            )
            row += 1

        hints = hint_labels(request.tool_annotations)
        if hints:
            ttk.Label(frame, text="Server hints", foreground="#555555").grid(row=row, column=0, sticky="nw", padx=(0, 14))
            badges = ttk.Frame(frame)
            badges.grid(row=row, column=1, sticky="w", pady=2)
            for label, warn in hints:
                tk.Label(
                    badges,
                    text=f" {label} ",
                    bg="#c62828" if warn else "#455a64",
                    fg="white",
                    font=self._bold_font,
                ).pack(side="left", padx=(0, 6))
            row += 1

        if request.tool_description:
            ttk.Label(frame, text="Description", foreground="#555555").grid(row=row, column=0, sticky="nw", padx=(0, 14))
            ttk.Label(
                frame,
                text=safe_text(request.tool_description, 400) + "\n(provided by the MCP server — may be inaccurate)",
                wraplength=520,
                justify="left",
            ).grid(row=row, column=1, sticky="w", pady=1)
            row += 1

        ttk.Label(frame, text="Arguments", font=self._bold_font).grid(row=row, column=0, columnspan=2, sticky="w", pady=(12, 4))
        row += 1
        args_text = format_arguments(request.arguments)
        line_count = args_text.count("\n") + 1
        box = ttk.Frame(frame)
        box.grid(row=row, column=0, columnspan=2, sticky="nsew")
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        frame.rowconfigure(row, weight=1)
        text = tk.Text(box, height=max(3, min(16, line_count)), width=WINDOW_WIDTH_CHARS, wrap="char", font=mono, relief="flat", padx=8, pady=6)
        scroll = ttk.Scrollbar(box, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.insert("1.0", args_text)
        text.configure(state="disabled")
        text.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        row += 1

        footer = ttk.Frame(frame)
        footer.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        footer.columnconfigure(0, weight=1)
        self._countdown = ttk.Label(footer, text="", foreground="#666666")
        self._countdown.grid(row=0, column=0, sticky="w")
        ttk.Button(footer, text="Block", command=lambda: ui._chosen(self, Verdict.BLOCK)).grid(row=0, column=1, padx=(0, 8))
        self._allow = ttk.Button(footer, text="Allow", command=lambda: ui._chosen(self, Verdict.ALLOW), state="disabled")
        self._allow.grid(row=0, column=2)
        # Arm "Allow" only after a short delay so a click/keypress meant for another window
        # cannot approve a request the user has not seen.
        top.after(max(0, arm_delay_ms), self._arm)

        self.tick(time.time(), 1)
        top.update_idletasks()
        offset = slot * 28
        width = top.winfo_reqwidth()
        x = max(20, (top.winfo_screenwidth() - width) // 2 + offset)
        y = 90 + offset
        top.geometry(f"+{x}+{y}")
        top.lift()
        top.focus_force()
        _bring_app_to_front()

    def _arm(self) -> None:
        if not self.closed:
            try:
                self._allow.configure(state="normal")
            except Exception:
                pass

    def tick(self, now: float, open_count: int) -> None:
        if self.closed:
            return
        remaining = max(0, int(self.deadline - now))
        text = f"Blocks automatically in {remaining // 60}:{remaining % 60:02d}"
        if open_count > 1:
            text += f"   ·   {open_count} requests waiting"
        if text != self._last_countdown:
            self._last_countdown = text
            try:
                self._countdown.configure(text=text)
            except Exception:
                pass

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.top.destroy()
        except Exception:
            pass


def _bring_app_to_front() -> None:  # pragma: no cover - GUI
    """Best effort: make our process the active app on macOS so the popup also gets focus.
    Windows are already floating above other apps via ``-topmost``; this only adds focus.
    Requires the optional ``pyobjc-framework-Cocoa`` (``pip install 'mcpguard[macos]'``)."""
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication  # type: ignore[import-not-found]

        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception:
        pass
