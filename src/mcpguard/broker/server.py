"""Daemon side of the approval channel.

The broker accepts approval requests from any number of interceptor processes concurrently.
Each request becomes an independent pending approval with its own popup, deadline and
connection; resolving one never affects another. A request is settled by exactly one of:

* the user clicking Allow / Block (or closing the popup → Block),
* the decision timeout expiring (→ Block),
* the requesting connection closing (→ cancelled, popup dismissed),
* daemon shutdown (→ Block).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from mcpguard import __version__
from mcpguard.audit import AuditLog
from mcpguard.broker import protocol
from mcpguard.config import Paths, Settings
from mcpguard.models import ApprovalRequest, Decision, Reason, Verdict
from mcpguard.ui.base import ApprovalUI

log = logging.getLogger(__name__)

FIRST_LINE_TIMEOUT = 10.0


@dataclass
class _Pending:
    request: ApprovalRequest
    future: asyncio.Future[Decision]
    deadline: float
    presented_at: float = field(default_factory=time.monotonic)


class ApprovalBroker:
    def __init__(self, ui: ApprovalUI, settings: Settings, paths: Paths, audit: AuditLog | None = None) -> None:
        self.ui = ui
        self.settings = settings
        self.paths = paths
        self.audit = audit or AuditLog(paths.audit_log, settings.redact_keys, settings.audit)
        self._pending: dict[str, _Pending] = {}
        self._server: asyncio.AbstractServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._token = ""
        self._closing = False

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.paths.ensure()
        self.ui.bind(self._on_ui_decision)
        if protocol.USE_UNIX_SOCKET:
            sock = self.paths.socket
            with contextlib.suppress(FileNotFoundError):
                sock.unlink()  # stale socket; the daemon lock guarantees nobody else owns it
            old_umask = os.umask(0o177)
            try:
                self._server = await asyncio.start_unix_server(self._handle, path=str(sock), limit=protocol.MAX_LINE_BYTES)
            finally:
                os.umask(old_umask)
            os.chmod(sock, 0o600)
            log.info("approval broker listening on %s", sock)
        else:  # pragma: no cover - Windows
            self._token = secrets.token_urlsafe(32)
            self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0, limit=protocol.MAX_LINE_BYTES)
            port = self._server.sockets[0].getsockname()[1]
            protocol.write_private_file(self.paths.tcp_endpoint_file, json.dumps({"port": port, "token": self._token}))
            log.info("approval broker listening on 127.0.0.1:%s", port)

    async def shutdown(self) -> None:
        """Block every pending request, stop accepting connections and remove the socket."""
        self._closing = True
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.set_result(Decision.block(Reason.SHUTDOWN))
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2)
        if protocol.USE_UNIX_SOCKET:
            with contextlib.suppress(FileNotFoundError):
                self.paths.socket.unlink()
        else:  # pragma: no cover - Windows
            with contextlib.suppress(FileNotFoundError):
                self.paths.tcp_endpoint_file.unlink()

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    # ------------------------------------------------------------------ connection handling
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                line = await asyncio.wait_for(reader.readline(), FIRST_LINE_TIMEOUT)
            except (asyncio.TimeoutError, ValueError, ConnectionError):
                return
            if not line:
                return
            try:
                msg = json.loads(line)
                if not isinstance(msg, dict):
                    raise ValueError("message must be an object")
            except ValueError as exc:
                await self._send(writer, {"type": "error", "message": f"malformed message: {exc}"})
                return
            if self._token and not secrets.compare_digest(str(msg.get("token", "")), self._token):
                await self._send(writer, {"type": "error", "message": "bad token"})
                return
            kind = msg.get("type")
            if kind == "ping":
                await self._send(
                    writer,
                    {"type": "pong", "pid": os.getpid(), "pending": len(self._pending), "ui": self.ui.name, "version": __version__},
                )
            elif kind == "approve":
                try:
                    request = ApprovalRequest.from_wire(msg.get("request"))
                except (TypeError, ValueError) as exc:
                    await self._send(writer, {"type": "error", "message": f"invalid request: {exc}"})
                    return
                decision = await self._decide(request, reader)
                await self._send(writer, {"type": "decision", "id": request.id, **decision.to_wire()})
            else:
                await self._send(writer, {"type": "error", "message": f"unknown type {kind!r}"})
        except asyncio.CancelledError:
            raise
        except Exception:  # never let one bad connection take the daemon down
            log.exception("error while handling broker connection")
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, obj: dict[str, Any]) -> None:
        try:
            writer.write(protocol.encode(obj))
            await writer.drain()
        except (ConnectionError, RuntimeError):
            pass  # requester went away; nothing to tell it

    async def _decide(self, request: ApprovalRequest, reader: asyncio.StreamReader) -> Decision:
        if self._closing:
            return Decision.block(Reason.SHUTDOWN)
        loop = asyncio.get_running_loop()
        if request.id in self._pending:  # ids are uuids; a duplicate means a confused client
            request.id = os.urandom(16).hex()
        timeout = min(request.timeout or self.settings.decision_timeout, self.settings.decision_timeout)
        pending = _Pending(request, loop.create_future(), deadline=time.time() + timeout)
        self._pending[request.id] = pending
        log.info("pending #%s %s (%s)", request.id[:8], request.title, request.source)

        hangup = asyncio.ensure_future(reader.read(1))  # b"" when the requester disconnects
        try:
            self.ui.present(request, pending.deadline, len(self._pending))
        except Exception as exc:  # UI failure → fail closed
            log.exception("UI failed to present request")
            pending.future.set_result(Decision.block(Reason.ERROR, f"popup failed: {exc}"))

        decision: Decision
        try:
            done, _ = await asyncio.wait({pending.future, hangup}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if pending.future in done:
                decision = pending.future.result()
            elif hangup in done:
                decision = Decision.block(Reason.CANCELLED, "requester disconnected")
            else:
                decision = Decision.block(Reason.TIMEOUT, f"no decision within {int(timeout)}s")
        finally:
            hangup.cancel()
            self._pending.pop(request.id, None)
            if not (pending.future.done() and pending.future.result().reason is Reason.USER):
                with contextlib.suppress(Exception):
                    self.ui.dismiss(request.id)
            if not pending.future.done():
                pending.future.cancel()
        self.audit.record(request, decision)
        log.info("decided #%s %s: %s (%s)", request.id[:8], request.title, decision.verdict.value, decision.reason.value)
        return decision

    # ------------------------------------------------------------------ UI callback (any thread)
    def _on_ui_decision(self, request_id: str, verdict: Verdict) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self._resolve, request_id, verdict)

    def _resolve(self, request_id: str, verdict: Verdict) -> None:
        pending = self._pending.get(request_id)
        if pending is not None and not pending.future.done():
            pending.future.set_result(Decision(verdict, Reason.USER))
