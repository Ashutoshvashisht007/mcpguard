"""Transparent stdio proxy for local MCP servers launched by Claude Desktop.

Claude Desktop starts every server in ``claude_desktop_config.json`` as a subprocess and speaks
newline-delimited JSON-RPC over its stdin/stdout. ``mcpguard install`` rewrites each entry so
Claude launches *this* proxy instead, which then launches the real server:

    Claude Desktop ──stdin/stdout──▶ mcpguard wrap ──stdin/stdout──▶ real MCP server
                                          │
                                          └──unix socket──▶ approval daemon (popup)

Client → server messages are parsed; requests whose method needs approval (``tools/call`` by
default) are parked in their own task while the user decides, so other traffic — including
other tool calls — keeps flowing. Allowed requests are forwarded; blocked ones get a denial
response written straight back to Claude. Everything else passes through untouched, and
server → client traffic is forwarded byte-for-byte (it is only parsed to learn tool metadata).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import queue
import signal
import sys
import threading
from collections.abc import Sequence
from typing import Any, BinaryIO

from mcpguard import jsonrpc
from mcpguard.interceptor import Interceptor

log = logging.getLogger(__name__)

#: Max size of one JSON-RPC line from the server (tool results can carry whole files).
MAX_MESSAGE_BYTES = 128 * 1024 * 1024
CHILD_EXIT_GRACE = 5.0


class _LineWriter:
    """Serialises writes to the client's stdout on a dedicated thread.

    Many tasks emit concurrently (forwarded server output, denials, errors); a single writer
    keeps lines whole and in order, and a slow reader can never block the event loop.
    """

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._queue: queue.Queue[bytes | None] = queue.Queue()
        self.broken = False
        self._thread = threading.Thread(target=self._run, name="stdout-writer", daemon=True)
        self._thread.start()

    def write(self, data: bytes) -> None:
        if not self.broken:
            self._queue.put(data)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            if self.broken:
                continue
            try:
                self._stream.write(item)
                self._stream.flush()
            except (OSError, ValueError):
                self.broken = True  # client went away; keep draining so producers never block

    def close(self, timeout: float = 3.0) -> None:
        self._queue.put(None)
        self._thread.join(timeout)


class StdioProxy:
    def __init__(
        self,
        command: Sequence[str],
        interceptor: Interceptor,
        *,
        stdin: BinaryIO | None = None,
        stdout: BinaryIO | None = None,
    ) -> None:
        if not command:
            raise ValueError("no MCP server command given")
        self.command = list(command)
        self.interceptor = interceptor
        self._stdin = stdin or sys.stdin.buffer
        self._writer = _LineWriter(stdout or sys.stdout.buffer)
        self._child: asyncio.subprocess.Process | None = None
        self._client_lines: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._child_write_lock = asyncio.Lock()
        #: client→server requests forwarded and awaiting a response: id_key → (method, raw id)
        self._inflight: dict[str, tuple[str, Any]] = {}
        #: requests waiting for the user: id_key → (task, message)
        self._approvals: dict[str, tuple[asyncio.Task[None], dict[str, Any]]] = {}
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ lifecycle
    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        log.info("wrapping MCP server %r: %s", self.interceptor.server_name, " ".join(self.command))
        try:
            self._child = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=None,  # inherit: Claude Desktop records it in mcp-server-<name>.log
                limit=MAX_MESSAGE_BYTES,
            )
        except OSError as exc:
            log.error("cannot start MCP server %r: %s", self.command[0], exc)
            self._writer.close()
            return 127
        self._install_signal_handlers(loop)
        threading.Thread(target=self._read_stdin, args=(loop,), name="stdin-reader", daemon=True).start()
        warmup = asyncio.ensure_future(self.interceptor.broker.ensure_daemon())

        upstream = asyncio.ensure_future(self._pump_client())
        downstream = asyncio.ensure_future(self._pump_server())
        child_exit = asyncio.ensure_future(self._child.wait())
        stop = asyncio.ensure_future(self._stop.wait())
        done, _ = await asyncio.wait({upstream, downstream, child_exit, stop}, return_when=asyncio.FIRST_COMPLETED)

        if child_exit in done or downstream in done:
            code = await self._on_server_gone(downstream)
        else:
            code = await self._on_client_gone(downstream)
        for task in (upstream, downstream, child_exit, stop, warmup):
            task.cancel()
        self._writer.close()
        return code

    def _install_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGHUP", None)):
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except (NotImplementedError, RuntimeError, ValueError):  # pragma: no cover - Windows
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(self._stop.set))

    async def _on_client_gone(self, downstream: asyncio.Future[None]) -> int:
        """Claude closed stdin (or we were signalled): cancel approvals, stop the server politely."""
        log.info("client disconnected; shutting down %r", self.interceptor.server_name)
        self._cancel_all_approvals()
        child = self._child
        assert child is not None
        if child.stdin and not child.stdin.is_closing():
            child.stdin.close()
        try:
            await asyncio.wait_for(child.wait(), CHILD_EXIT_GRACE)
        except asyncio.TimeoutError:
            log.warning("MCP server did not exit after stdin closed; terminating")
            with contextlib.suppress(ProcessLookupError):
                child.terminate()
            try:
                await asyncio.wait_for(child.wait(), 3)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    child.kill()
                await child.wait()
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(downstream), 2)
        return 0

    async def _on_server_gone(self, downstream: asyncio.Future[None]) -> int:
        """The real server exited: flush its output, then fail everything still outstanding."""
        child = self._child
        assert child is not None
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(downstream), 2)
        if child.returncode is None:  # stdout closed/broken but process alive
            with contextlib.suppress(ProcessLookupError):
                child.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(child.wait(), 3)
        code = child.returncode if child.returncode is not None else 1
        log.error("MCP server %r exited with code %s", self.interceptor.server_name, code)
        message = f"MCP server '{self.interceptor.server_name}' exited (code {code})"
        for _, msg in list(self._approvals.values()):
            self._emit(jsonrpc.error_response(msg.get("id"), jsonrpc.INTERNAL_ERROR, message))
        self._cancel_all_approvals()
        for _, request_id in list(self._inflight.values()):
            self._emit(jsonrpc.error_response(request_id, jsonrpc.INTERNAL_ERROR, message))
        self._inflight.clear()
        return code or 1

    def _cancel_all_approvals(self) -> None:
        for task, _ in list(self._approvals.values()):
            task.cancel()
        self._approvals.clear()

    # ------------------------------------------------------------------ client → server
    def _read_stdin(self, loop: asyncio.AbstractEventLoop) -> None:
        """Blocking reader thread (portable; asyncio can't read a console/pipe stdin everywhere).

        Uses raw ``os.read`` rather than ``BufferedReader.readline``: if the proxy exits while
        this daemon thread is blocked, a held buffer lock would abort interpreter shutdown.
        """
        try:
            fd = self._stdin.fileno()
        except (AttributeError, OSError, ValueError):
            fd = None
        try:
            if fd is None:
                for line in iter(self._stdin.readline, b""):
                    loop.call_soon_threadsafe(self._client_lines.put_nowait, line)
                return
            pending = b""
            while True:
                chunk = os.read(fd, 1 << 16)
                if not chunk:
                    break
                pending += chunk
                if b"\n" not in chunk:
                    continue
                *lines, pending = pending.split(b"\n")
                for line in lines:
                    loop.call_soon_threadsafe(self._client_lines.put_nowait, line + b"\n")
            if pending:
                loop.call_soon_threadsafe(self._client_lines.put_nowait, pending)
        except (OSError, ValueError, RuntimeError):
            pass
        finally:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self._client_lines.put_nowait, None)

    async def _pump_client(self) -> None:
        while True:
            line = await self._client_lines.get()
            if line is None:
                return
            if not line.strip():
                continue
            try:
                msg = jsonrpc.loads(line)
            except ValueError:  # includes UnicodeDecodeError
                log.warning("dropping unparseable message from client")
                self._emit(jsonrpc.error_response(None, jsonrpc.PARSE_ERROR, "Parse error"))
                continue
            await self._dispatch(msg)

    async def _dispatch(self, msg: Any) -> None:
        if isinstance(msg, list):
            await self._dispatch_batch(msg)
            return
        if not isinstance(msg, dict):
            self._emit(jsonrpc.error_response(None, jsonrpc.INVALID_REQUEST, "Invalid Request"))
            return
        self.interceptor.observe_client(msg)
        if jsonrpc.is_notification(msg) and msg["method"] == jsonrpc.CANCELLED_NOTIFICATION:
            if self._cancel_pending(msg):
                return  # the server never saw that request; nothing to forward
        if self.interceptor.needs_approval(msg):
            self._start_gate(msg)
            return
        self._track(msg)
        await self._forward(msg)

    async def _dispatch_batch(self, batch: list[Any]) -> None:
        if not batch:
            self._emit(jsonrpc.error_response(None, jsonrpc.INVALID_REQUEST, "Invalid Request: empty batch"))
            return
        for item in batch:
            self.interceptor.observe_client(item)
        gated = [m for m in batch if self.interceptor.needs_approval(m)]
        if not gated:
            for m in batch:
                self._track(m)
            await self._forward(batch)
            return

        async def gate_batch() -> None:
            decisions = await asyncio.gather(*(self.interceptor.decide(m) for m in gated))
            blocked = {id(m) for m, d in zip(gated, decisions) if not d.allowed}
            remaining = [m for m in batch if id(m) not in blocked]
            for m, d in zip(gated, decisions):
                if not d.allowed:
                    self._emit(self.interceptor.denial(m, d))
            if remaining:
                for m in remaining:
                    self._track(m)
                await self._forward(remaining)

        task = asyncio.ensure_future(gate_batch())
        for m in gated:
            self._approvals[jsonrpc.id_key(m["id"])] = (task, m)
        task.add_done_callback(lambda t: self._forget(t, gated))

    def _forget(self, task: asyncio.Task[None], messages: list[dict[str, Any]]) -> None:
        for m in messages:
            key = jsonrpc.id_key(m["id"])
            if self._approvals.get(key, (None,))[0] is task:
                del self._approvals[key]

    def _start_gate(self, msg: dict[str, Any]) -> None:
        key = jsonrpc.id_key(msg["id"])
        if key in self._approvals or key in self._inflight:
            self._emit(jsonrpc.error_response(msg["id"], jsonrpc.INVALID_REQUEST, "Duplicate request id"))
            return
        task = asyncio.ensure_future(self._gate(msg))
        self._approvals[key] = (task, msg)
        task.add_done_callback(lambda t: self._forget(t, [msg]))

    async def _gate(self, msg: dict[str, Any]) -> None:
        decision = await self.interceptor.decide(msg)  # CancelledError → no response (cancelled)
        if decision.allowed:
            self._track(msg)
            await self._forward(msg)
        else:
            self._emit(self.interceptor.denial(msg, decision))

    def _cancel_pending(self, notification: dict[str, Any]) -> bool:
        params = notification.get("params")
        if not isinstance(params, dict) or "requestId" not in params:
            return False
        entry = self._approvals.pop(jsonrpc.id_key(params["requestId"]), None)
        if entry is None:
            return False
        log.info("client cancelled pending request id=%s; dismissing popup", params["requestId"])
        entry[0].cancel()
        return True

    def _track(self, msg: Any) -> None:
        if jsonrpc.is_request(msg):
            self._inflight[jsonrpc.id_key(msg["id"])] = (msg["method"], msg["id"])

    async def _forward(self, msg: Any) -> None:
        child = self._child
        data = jsonrpc.encode(msg)
        async with self._child_write_lock:
            try:
                assert child is not None and child.stdin is not None
                if child.stdin.is_closing():
                    raise ConnectionResetError("server stdin closed")
                child.stdin.write(data)
                await child.stdin.drain()
                return
            except (OSError, AssertionError, RuntimeError) as exc:
                log.warning("could not deliver message to MCP server: %s", exc)
        for item in msg if isinstance(msg, list) else [msg]:
            if jsonrpc.is_request(item):
                self._inflight.pop(jsonrpc.id_key(item["id"]), None)
                self._emit(jsonrpc.error_response(item["id"], jsonrpc.INTERNAL_ERROR, "MCP server is not running"))

    # ------------------------------------------------------------------ server → client
    async def _pump_server(self) -> None:
        child = self._child
        assert child is not None and child.stdout is not None
        while True:
            try:
                line = await child.stdout.readline()
            except ValueError:
                log.error("MCP server sent a message larger than %d bytes; aborting", MAX_MESSAGE_BYTES)
                return
            if not line:
                return
            self._observe_server_line(line)
            self._writer.write(line if line.endswith(b"\n") else line + b"\n")

    def _observe_server_line(self, line: bytes) -> None:
        if not self._inflight:
            return
        try:
            msg = jsonrpc.loads(line)
        except ValueError:
            return
        for item in msg if isinstance(msg, list) else [msg]:
            if jsonrpc.is_response(item):
                entry = self._inflight.pop(jsonrpc.id_key(item["id"]), None)
                if entry is not None:
                    try:
                        self.interceptor.observe_response(entry[0], item)
                    except Exception:  # monitoring must never break forwarding
                        log.debug("could not inspect response", exc_info=True)

    def _emit(self, obj: Any) -> None:
        self._writer.write(jsonrpc.encode(obj))
