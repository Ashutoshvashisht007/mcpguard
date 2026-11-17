"""Shared fixtures: an isolated MCPGUARD_HOME per test and helpers to run the broker in-process."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import socket
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from mcpguard.broker.server import ApprovalBroker
from mcpguard.config import Paths, Settings
from mcpguard.models import ApprovalRequest, Verdict
from mcpguard.ui.auto_ui import AutoUI

PY = sys.executable


@pytest.fixture
def guard_home(monkeypatch: pytest.MonkeyPatch) -> Any:
    # Short path on purpose: AF_UNIX socket paths are limited to ~104 bytes on macOS.
    home = Path(tempfile.mkdtemp(prefix="mg-"))
    monkeypatch.setenv("MCPGUARD_HOME", str(home))
    monkeypatch.setenv("MCPGUARD_AUTOSTART_DAEMON", "0")
    monkeypatch.setenv("MCPGUARD_LOG_LEVEL", "WARNING")
    monkeypatch.delenv("MCPGUARD_SOCKET", raising=False)
    yield home
    shutil.rmtree(home, ignore_errors=True)


def manual_ui() -> AutoUI:
    """A UI that never answers by itself; tests call ``ui.report(id, verdict)``."""
    return AutoUI(lambda _r: None)


def policy_ui(policy: Callable[[ApprovalRequest], Verdict | None]) -> AutoUI:
    return AutoUI(policy)


@contextlib.asynccontextmanager
async def running_broker(ui: AutoUI, **settings: Any) -> AsyncIterator[ApprovalBroker]:
    paths = Paths.default()
    broker = ApprovalBroker(ui, Settings.load(paths).merged(settings), paths)
    await broker.start()
    try:
        yield broker
    finally:
        await broker.shutdown()


async def wait_for(predicate: Callable[[], bool], timeout: float = 10.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.02)


class WrappedServer:
    """Runs ``mcpguard wrap -- <server>`` as a subprocess and talks raw JSON-RPC to it."""

    def __init__(self, server_cmd: list[str] | None = None, name: str = "demo") -> None:
        self.server_cmd = server_cmd or [PY, "-m", "mcpguard", "demo-server"]
        self.name = name
        self.proc: asyncio.subprocess.Process | None = None
        self.responses: dict[Any, dict[str, Any]] = {}
        self.unmatched: list[Any] = []
        self._reader: asyncio.Task[None] | None = None

    async def __aenter__(self) -> WrappedServer:
        self.proc = await asyncio.create_subprocess_exec(
            PY, "-m", "mcpguard", "wrap", "--name", self.name, "--", *self.server_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=64 * 1024 * 1024,
        )
        self._reader = asyncio.ensure_future(self._read())
        return self

    async def __aexit__(self, *exc: Any) -> None:
        assert self.proc is not None
        if self.proc.returncode is None:
            with contextlib.suppress(Exception):
                self.proc.stdin.close()  # type: ignore[union-attr]
            try:
                await asyncio.wait_for(self.proc.wait(), 10)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        if self._reader:
            self._reader.cancel()

    async def _read(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                return
            msg = json.loads(line)
            if isinstance(msg, dict) and "id" in msg and "method" not in msg:
                self.responses[json.dumps(msg["id"])] = msg
            else:
                self.unmatched.append(msg)

    async def send(self, msg: Any) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self.proc.stdin.drain()

    async def send_raw(self, data: bytes) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write(data)
        await self.proc.stdin.drain()

    async def response(self, request_id: Any, timeout: float = 15.0) -> dict[str, Any]:
        key = json.dumps(request_id)
        await wait_for(lambda: key in self.responses, timeout, f"response to id {request_id!r}")
        return self.responses[key]

    async def request(self, request_id: Any, method: str, params: dict[str, Any] | None = None, timeout: float = 15.0) -> dict[str, Any]:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            msg["params"] = params
        await self.send(msg)
        return await self.response(request_id, timeout)

    async def initialize(self) -> dict[str, Any]:
        result = await self.request(
            0,
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "claude-ai", "version": "9.9"}},
        )
        await self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    async def call_tool(self, request_id: Any, name: str, arguments: dict[str, Any]) -> None:
        await self.send(
            {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        )


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def wait_for_port(port: int, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            return
        except OSError:
            if time.monotonic() > deadline:
                raise
            await asyncio.sleep(0.1)


def text_of(response: dict[str, Any]) -> str:
    content = response.get("result", {}).get("content") or []
    return " ".join(c.get("text", "") for c in content if isinstance(c, dict))


def env_with(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(extra)
    return env
