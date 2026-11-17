"""End-to-end tests of the Claude Web path: official SDK client → gateway → real demo server over
Streamable HTTP, with the approval broker in-process."""

from __future__ import annotations

import asyncio
import contextlib
import sys

import aiohttp
import pytest
from conftest import PY, free_port, manual_ui, policy_ui, running_broker, wait_for, wait_for_port
from mcp import Client

from mcpguard.broker.client import BrokerClient
from mcpguard.config import Settings
from mcpguard.http_gateway import HttpGateway, serve_gateway
from mcpguard.interceptor import Interceptor
from mcpguard.models import Verdict

TOKEN = "test-token-0123456789"


@pytest.fixture
def upstream(guard_home):
    """The demo server over Streamable HTTP on a free port."""
    import subprocess

    port = free_port()
    proc = subprocess.Popen(
        [PY, "-m", "mcpguard", "demo-server", "--transport", "http", "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    asyncio.run(wait_for_port(port))
    yield f"http://127.0.0.1:{port}/mcp"
    proc.terminate()
    proc.wait(10)


@contextlib.asynccontextmanager
async def gateway(upstream_url: str, decision_timeout: float = 60.0):
    settings = Settings.load()
    interceptor = Interceptor(
        settings, BrokerClient(settings), source="Claude Web", transport="http", server_name="remote-demo",
        decision_timeout=decision_timeout,
    )
    gw = HttpGateway(upstream=upstream_url, interceptor=interceptor, path_token=TOKEN)
    port = free_port()
    stop = asyncio.Event()
    task = asyncio.ensure_future(serve_gateway(gw, "127.0.0.1", port, stop))
    await wait_for_port(port)
    try:
        yield f"http://127.0.0.1:{port}/{TOKEN}/mcp"
    finally:
        stop.set()
        await task


def block_destructive(request):
    return Verdict.BLOCK if request.tool_name == "delete_note" else Verdict.ALLOW


def test_sdk_client_through_gateway_both_protocol_eras(upstream) -> None:
    async def main() -> None:
        ui = policy_ui(block_destructive)
        async with running_broker(ui), gateway(upstream) as url:
            for mode in ("legacy", "auto"):
                async with Client(url, mode=mode) as client:
                    tools = await client.list_tools()
                    assert "delete_note" in {t.name for t in tools.tools}
                    ok = await client.call_tool("add", {"a": 20, "b": 22})
                    assert not ok.is_error and "42" in ok.content[0].text
                    denied = await client.call_tool("delete_note", {"title": "x"})
                    assert denied.is_error and "blocked" in denied.content[0].text.lower()
        destructive = [r for r in ui.presented if r.tool_name == "delete_note"]
        assert len(destructive) == 2
        for r in destructive:
            assert r.source == "Claude Web" and r.transport == "http" and r.server_name == "remote-demo"
            assert r.client_name  # learned from initialize (legacy) or _meta (2026-07-28)
            assert r.tool_annotations.get("destructiveHint") is True  # learned from tools/list

    asyncio.run(main())


def test_concurrent_calls_through_gateway(upstream) -> None:
    async def main() -> None:
        ui = manual_ui()
        async with running_broker(ui), gateway(upstream) as url:
            async with Client(url, mode="legacy") as client:
                tasks = [asyncio.ensure_future(client.call_tool("echo", {"text": f"m{i}"})) for i in range(4)]
                await wait_for(lambda: len(ui.presented) == 4, what="4 popups")
                for r in reversed(ui.presented):
                    ui.report(r.id, Verdict.BLOCK if r.arguments["text"] == "m1" else Verdict.ALLOW)
                results = await asyncio.gather(*tasks)
                for i, res in enumerate(results):
                    if i == 1:
                        assert res.is_error
                    else:
                        assert not res.is_error and res.content[0].text == f"m{i}"

    asyncio.run(main())


def test_http_client_disconnect_dismisses_popup(upstream) -> None:
    async def main() -> None:
        ui = manual_ui()
        async with running_broker(ui), gateway(upstream) as url:
            body = {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "echo", "arguments": {"text": "x"}}}
            async with aiohttp.ClientSession() as session:
                post = asyncio.ensure_future(session.post(url, json=body, headers={"Accept": "application/json, text/event-stream"}))
                await wait_for(lambda: len(ui.presented) == 1, what="popup")
                post.cancel()  # client goes away (2026-07-28: cancellation == closing the stream)
            await wait_for(lambda: ui.presented[0].id in ui.dismissed, what="dismissal")

    asyncio.run(main())


def test_gateway_rejects_bad_requests(upstream) -> None:
    async def main() -> None:
        async with running_broker(manual_ui()), gateway(upstream) as url:
            base = url.rsplit("/", 2)[0]
            async with aiohttp.ClientSession() as s:
                async with s.post(f"{base}/wrong-token/mcp", json={}) as r:
                    assert r.status == 404
                async with s.get(f"{base}/.well-known/oauth-protected-resource") as r:
                    assert r.status == 404
                async with s.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Origin": "https://evil.example"}) as r:
                    assert r.status == 403
                async with s.post(url, data=b"{}", headers={"Content-Type": "text/plain"}) as r:
                    assert r.status == 415
                async with s.post(url, data=b"{nope", headers={"Content-Type": "application/json"}) as r:
                    assert r.status == 400 and (await r.json())["error"]["code"] == -32700
                async with s.put(url, data=b"") as r:
                    assert r.status == 405

    asyncio.run(main())


def test_upstream_down_returns_jsonrpc_error(guard_home) -> None:
    async def main() -> None:
        dead = f"http://127.0.0.1:{free_port()}/mcp"
        async with running_broker(policy_ui(lambda r: Verdict.ALLOW)), gateway(dead) as url:
            body = {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "echo", "arguments": {"text": "x"}}}
            async with aiohttp.ClientSession() as s, s.post(url, json=body) as r:
                assert r.status == 502
                payload = await r.json()
                assert payload["id"] == 9 and payload["error"]["code"] == -32603

    asyncio.run(main())


def test_blocked_call_never_reaches_upstream(guard_home) -> None:
    """With the upstream down, a *blocked* call still gets a clean denial: it was never forwarded."""

    async def main() -> None:
        dead = f"http://127.0.0.1:{free_port()}/mcp"
        async with running_broker(policy_ui(lambda r: Verdict.BLOCK)), gateway(dead) as url:
            body = {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "delete_note", "arguments": {"title": "x"}}}
            async with aiohttp.ClientSession() as s, s.post(url, json=body) as r:
                assert r.status == 200
                payload = await r.json()
                assert payload["id"] == 4 and payload["result"]["isError"] is True

    asyncio.run(main())


if sys.platform == "win32":  # pragma: no cover
    pytestmark = pytest.mark.skip("uses unix sockets")
