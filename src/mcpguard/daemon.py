"""The background approval service.

Owns the popup UI, the decision timeout and the audit log; interceptors connect to it over a
local socket (see :mod:`mcpguard.broker.protocol`). Only one daemon runs per user: a
non-blocking file lock guards startup, so concurrent autostarts from several wrappers are safe.

Threading: Tk needs the main thread (macOS), so with the Tk backend the asyncio broker runs on
a worker thread. Shutdown (SIGINT/SIGTERM/SIGHUP, Cmd+Q) blocks every pending request before
the socket is removed, so no interceptor is left waiting.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import threading
from typing import IO, Any

from mcpguard.broker.server import ApprovalBroker
from mcpguard.config import Paths, Settings
from mcpguard.ui import create_ui

log = logging.getLogger(__name__)


def acquire_lock(paths: Paths) -> IO[str] | None:
    """Return an open, exclusively locked file, or None if another daemon holds it."""
    paths.ensure()
    fh = open(paths.lock_file, "a+", encoding="utf-8")  # noqa: SIM115 - held for process lifetime
    try:
        if sys.platform == "win32":  # pragma: no cover
            import msvcrt

            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def run_daemon(settings: Settings, paths: Paths | None = None) -> int:
    paths = (paths or Paths.default()).ensure()
    lock = acquire_lock(paths)
    if lock is None:
        print("mcpguard daemon is already running", file=sys.stderr)
        return 0
    try:
        ui = create_ui(settings)
    except RuntimeError as exc:
        print(f"mcpguard: {exc}", file=sys.stderr)
        return 2
    broker = ApprovalBroker(ui, settings, paths)
    stop = threading.Event()
    result: dict[str, Any] = {"code": 0}

    def on_signal(signum: int, _frame: Any) -> None:
        log.info("received signal %s, shutting down", signum)
        stop.set()
        ui.stop()

    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if sig is not None:
            signal.signal(sig, on_signal)

    async def serve() -> None:
        try:
            await broker.start()
        except OSError as exc:
            log.error("cannot start approval broker: %s", exc)
            result["code"] = 1
            stop.set()
            ui.stop()
            return
        print(
            f"MCP Guard daemon ready (pid {os.getpid()}, popup={ui.name}, timeout={int(settings.decision_timeout)}s, "
            f"fail_mode={settings.fail_mode}). Audit log: {paths.audit_log}",
            file=sys.stderr,
            flush=True,
        )
        try:
            while not stop.is_set():
                await asyncio.sleep(0.2)
        finally:
            await broker.shutdown()
            log.info("daemon stopped")

    try:
        if ui.needs_main_thread:

            def worker() -> None:
                try:
                    asyncio.run(serve())
                except Exception:
                    log.exception("broker thread crashed")
                    result["code"] = 1
                finally:
                    stop.set()
                    ui.stop()

            thread = threading.Thread(target=worker, name="broker", daemon=True)
            thread.start()
            ui.run_mainloop(should_stop=stop.is_set)
            stop.set()  # UI exited (e.g. Cmd+Q) → stop the broker too
            thread.join(timeout=5)
        else:
            asyncio.run(serve())
    except KeyboardInterrupt:
        pass
    finally:
        lock.close()
    return int(result["code"])
