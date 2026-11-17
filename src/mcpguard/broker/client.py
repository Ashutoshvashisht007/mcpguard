"""Interceptor side of the approval channel.

``request_decision`` never raises for operational problems: if the daemon cannot be reached
or answers garbage, it returns a Block decision (or Allow when ``fail_mode = "open"``), so
interceptors always have a definite answer. Cancelling the awaiting task closes the
connection, which makes the daemon dismiss the popup.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import subprocess
import sys
import time
from typing import Any

from mcpguard.broker import protocol
from mcpguard.config import Paths, Settings
from mcpguard.models import ApprovalRequest, Decision, Reason, Verdict

log = logging.getLogger(__name__)

#: Extra time we wait beyond the daemon's own decision timeout before giving up on it.
GRACE_SECONDS = 15.0
DAEMON_START_WAIT = 10.0
RESPAWN_BACKOFF = 30.0


class BrokerClient:
    def __init__(self, settings: Settings, paths: Paths | None = None) -> None:
        self.settings = settings
        self.paths = paths or Paths.default()
        self._spawn_lock = asyncio.Lock()
        self._last_spawn = 0.0

    # ------------------------------------------------------------------ public API
    async def ping(self, timeout: float = 2.0) -> dict[str, Any] | None:
        try:
            reader, writer = await protocol.Endpoint.discover(self.paths).open(timeout)
        except (OSError, ValueError, asyncio.TimeoutError):
            return None
        try:
            writer.write(protocol.encode({"v": protocol.PROTOCOL_VERSION, "type": "ping", "token": self._token()}))
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout)
            reply = json.loads(line) if line else None
            return reply if isinstance(reply, dict) and reply.get("type") == "pong" else None
        except (OSError, ValueError, asyncio.TimeoutError):
            return None
        finally:
            writer.close()

    async def ensure_daemon(self) -> bool:
        """Make sure the daemon is up before the first tool call (starts it if allowed)."""
        if await self.ping() is not None:
            return True
        if not self.settings.autostart_daemon:
            outcome = "BLOCKED" if self.settings.fail_mode == "closed" else "ALLOWED without asking"
            log.warning("approval daemon is not running; tool calls will be %s until it starts", outcome)
            return False
        try:
            _, writer = await self._connect()
        except Exception as exc:
            log.warning("could not start the approval daemon: %s", exc)
            return False
        writer.close()
        return True

    async def request_decision(self, request: ApprovalRequest) -> Decision:
        try:
            reader, writer = await self._connect()
        except Exception as exc:
            log.warning("approval daemon unreachable (%s); fail_mode=%s", exc, self.settings.fail_mode)
            return self._unavailable(str(exc))
        try:
            writer.write(
                protocol.encode(
                    {"v": protocol.PROTOCOL_VERSION, "type": "approve", "token": self._token(), "request": request.to_wire()}
                )
            )
            await writer.drain()
            wait = (request.timeout or self.settings.decision_timeout) + GRACE_SECONDS
            line = await asyncio.wait_for(reader.readline(), wait)
            if not line:
                return self._unavailable("daemon closed the connection without a decision")
            reply = json.loads(line)
            if reply.get("type") != "decision":
                log.error("daemon rejected approval request: %s", reply.get("message"))
                return Decision.block(Reason.ERROR, str(reply.get("message", "daemon error")))
            return Decision.from_wire(reply)
        except asyncio.CancelledError:
            raise  # closing the socket (finally) tells the daemon to dismiss the popup
        except asyncio.TimeoutError:
            return Decision.block(Reason.TIMEOUT, "daemon did not answer in time")
        except (OSError, ValueError, KeyError) as exc:
            return self._unavailable(f"broken daemon connection: {exc}")
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    # ------------------------------------------------------------------ helpers
    def _unavailable(self, detail: str) -> Decision:
        if self.settings.fail_mode == "open":
            return Decision(Verdict.ALLOW, Reason.UNAVAILABLE, detail)
        return Decision.block(Reason.UNAVAILABLE, detail)

    def _token(self) -> str:
        if protocol.USE_UNIX_SOCKET:
            return ""
        try:  # pragma: no cover - Windows
            return protocol.Endpoint.discover(self.paths).token
        except (OSError, ValueError, KeyError):  # pragma: no cover
            return ""

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        try:
            return await protocol.Endpoint.discover(self.paths).open()
        except (OSError, ValueError, KeyError, asyncio.TimeoutError):
            if not self.settings.autostart_daemon:
                raise
        async with self._spawn_lock:
            if time.monotonic() - self._last_spawn > RESPAWN_BACKOFF:
                self._last_spawn = time.monotonic()
                spawn_daemon(self.paths)
            deadline = time.monotonic() + DAEMON_START_WAIT
            last_exc: Exception | None = None
            while time.monotonic() < deadline:
                try:
                    return await protocol.Endpoint.discover(self.paths).open()
                except (OSError, ValueError, KeyError, asyncio.TimeoutError) as exc:
                    last_exc = exc
                    await asyncio.sleep(0.2)
            raise ConnectionError(f"daemon did not come up: {last_exc}")


def spawn_daemon(paths: Paths) -> None:
    """Start ``mcpguard daemon`` fully detached. Its single-instance lock makes races harmless."""
    paths.ensure()
    log.info("starting approval daemon in the background")
    log_fd = os.open(paths.daemon_log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": log_fd, "stderr": log_fd, "close_fds": True}
        if sys.platform == "win32":  # pragma: no cover
            kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([sys.executable, "-m", "mcpguard", "daemon"], **kwargs)
    finally:
        os.close(log_fd)
