"""End-to-end tests of the Claude Desktop path: a real `mcpguard wrap` subprocess in front of the
real demo MCP server, with the approval broker running in-process."""

from __future__ import annotations

import asyncio
import json

from conftest import PY, WrappedServer, manual_ui, policy_ui, running_broker, text_of, wait_for
from mcp import Client, StdioServerParameters

from mcpguard.config import Paths
from mcpguard.models import Verdict


def block_destructive(request):
    return Verdict.BLOCK if request.tool_name in ("delete_note", "write_note") else Verdict.ALLOW


def test_allow_and_block_round_trip(guard_home) -> None:
    async def main() -> None:
        ui = policy_ui(block_destructive)
        async with running_broker(ui), WrappedServer() as srv:
            init = await srv.initialize()
            assert init["result"]["serverInfo"]["name"] == "mcpguard-demo"
            tools = await srv.request(1, "tools/list")
            assert {t["name"] for t in tools["result"]["tools"]} >= {"add", "write_note", "delete_note"}

            await srv.call_tool(2, "add", {"a": 2, "b": 40})
            allowed = await srv.response(2)
            assert allowed["result"]["isError"] is False and "42" in text_of(allowed)

            await srv.call_tool(3, "write_note", {"title": "secret plan", "content": "x"})
            blocked = await srv.response(3)
            assert blocked["result"]["isError"] is True
            assert blocked["result"]["_meta"]["mcpguard"]["reason"] == "user"
            assert "blocked" in text_of(blocked).lower()
            # the server never executed the blocked call
            assert not (Paths.default().demo_notes_dir / "secret plan.txt").exists()

        # popup context: client identity, server identity and server-provided tool metadata
        by_tool = {r.tool_name: r for r in ui.presented}
        req = by_tool["write_note"]
        assert req.client_name == "claude-ai" and req.client_version == "9.9"
        assert req.server_info and "mcpguard-demo" in req.server_info
        assert req.tool_description and "note" in req.tool_description.lower()
        assert req.arguments == {"title": "secret plan", "content": "x"}
        assert req.source == "Claude Desktop" and req.server_name == "demo"

    asyncio.run(main())


def test_concurrent_calls_are_independent(guard_home) -> None:
    async def main() -> None:
        ui = manual_ui()
        async with running_broker(ui), WrappedServer() as srv:
            await srv.initialize()
            for i in range(5):
                await srv.call_tool(100 + i, "echo", {"text": f"msg-{i}"})
            await wait_for(lambda: len(ui.presented) == 5, what="5 popups")

            # non-gated traffic keeps flowing while 5 calls wait for the user
            pong = await srv.request("ping-1", "ping")
            assert pong["result"] == {}

            # resolve in reverse order, alternating allow/block
            by_text = {r.arguments["text"]: r for r in ui.presented}
            plan = {f"msg-{i}": (Verdict.ALLOW if i % 2 == 0 else Verdict.BLOCK) for i in range(5)}
            for i in reversed(range(5)):
                ui.report(by_text[f"msg-{i}"].id, plan[f"msg-{i}"])
            for i in range(5):
                resp = await srv.response(100 + i)
                if plan[f"msg-{i}"] is Verdict.ALLOW:
                    assert resp["result"]["isError"] is False and text_of(resp) == f"msg-{i}"
                else:
                    assert resp["result"]["isError"] is True

    asyncio.run(main())


def test_cancel_notification_dismisses_popup(guard_home) -> None:
    async def main() -> None:
        ui = manual_ui()
        async with running_broker(ui), WrappedServer() as srv:
            await srv.initialize()
            await srv.call_tool(7, "delete_note", {"title": "x"})
            await wait_for(lambda: len(ui.presented) == 1, what="popup")
            await srv.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 7}})
            await wait_for(lambda: ui.presented[0].id in ui.dismissed, what="dismissal")
            await asyncio.sleep(0.3)
            assert json.dumps(7) not in srv.responses  # cancelled requests get no response
            # the proxy is still healthy
            assert (await srv.request(8, "ping"))["result"] == {}

    asyncio.run(main())


def test_timeout_blocks(guard_home) -> None:
    async def main() -> None:
        ui = manual_ui()
        async with running_broker(ui, decision_timeout=1), WrappedServer() as srv:
            await srv.initialize()
            await srv.call_tool(1, "echo", {"text": "hi"})
            resp = await srv.response(1)
            assert resp["result"]["isError"] is True
            assert resp["result"]["_meta"]["mcpguard"]["reason"] == "timeout"

    asyncio.run(main())


def test_fail_closed_when_daemon_missing(guard_home) -> None:
    async def main() -> None:
        async with WrappedServer() as srv:  # no broker, autostart disabled by fixture
            await srv.initialize()
            await srv.call_tool(1, "echo", {"text": "hi"})
            resp = await srv.response(1)
            assert resp["result"]["isError"] is True
            assert resp["result"]["_meta"]["mcpguard"]["reason"] == "daemon_unavailable"

    asyncio.run(main())


def test_jsonrpc_error_denial_style(guard_home, monkeypatch) -> None:
    monkeypatch.setenv("MCPGUARD_DENIAL_STYLE", "jsonrpc_error")

    async def main() -> None:
        async with running_broker(policy_ui(lambda r: Verdict.BLOCK)), WrappedServer() as srv:
            await srv.initialize()
            await srv.call_tool(1, "echo", {"text": "hi"})
            resp = await srv.response(1)
            assert resp["error"]["code"] == -32010

    asyncio.run(main())


def test_malformed_input_and_batches(guard_home) -> None:
    async def main() -> None:
        ui = policy_ui(block_destructive)
        async with running_broker(ui), WrappedServer() as srv:
            await srv.initialize()
            await srv.send_raw(b"this is not json\n")
            await wait_for(lambda: any(isinstance(m, dict) and m.get("error", {}).get("code") == -32700 for m in [*srv.unmatched, *srv.responses.values()]), what="parse error")
            # 2025-03-26 style batch: the allowed call is forwarded, the blocked one is denied
            await srv.send([
                {"jsonrpc": "2.0", "id": 21, "method": "tools/call", "params": {"name": "echo", "arguments": {"text": "ok"}}},
                {"jsonrpc": "2.0", "id": 22, "method": "tools/call", "params": {"name": "delete_note", "arguments": {"title": "x"}}},
            ])
            assert (await srv.response(22))["result"]["isError"] is True

    asyncio.run(main())


def test_server_crash_fails_outstanding_requests(guard_home) -> None:
    crashing = [PY, "-c", "import sys; sys.stdin.readline(); sys.exit(3)"]

    async def main() -> None:
        async with running_broker(policy_ui(lambda r: Verdict.ALLOW)), WrappedServer(server_cmd=crashing) as srv:
            resp = await srv.request(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
            assert resp["error"]["code"] == -32603 and "exited" in resp["error"]["message"]
            assert await asyncio.wait_for(srv.proc.wait(), 10) == 3

    asyncio.run(main())


def test_missing_server_binary(guard_home) -> None:
    async def main() -> None:
        async with WrappedServer(server_cmd=["/definitely/not/a/real/binary"]) as srv:
            assert await asyncio.wait_for(srv.proc.wait(), 10) == 127

    asyncio.run(main())


def test_client_disconnect_stops_server(guard_home) -> None:
    async def main() -> None:
        ui = manual_ui()
        async with running_broker(ui):
            srv = WrappedServer()
            await srv.__aenter__()
            await srv.initialize()
            await srv.call_tool(1, "echo", {"text": "pending"})
            await wait_for(lambda: len(ui.presented) == 1, what="popup")
            srv.proc.stdin.close()  # Claude Desktop quits
            assert await asyncio.wait_for(srv.proc.wait(), 10) == 0
            await wait_for(lambda: len(ui.dismissed) == 1, what="popup dismissed on shutdown")
            await srv.__aexit__(None, None, None)

    asyncio.run(main())


def _sdk_params() -> StdioServerParameters:
    import os

    env = {k: v for k, v in os.environ.items() if k.startswith("MCPGUARD_")}
    return StdioServerParameters(command=PY, args=["-m", "mcpguard", "wrap", "--name", "demo", "--", PY, "-m", "mcpguard", "demo-server"], env=env)


def test_official_sdk_client_legacy_and_modern(guard_home) -> None:
    """The official MCP Python SDK works through the wrapper with both protocol eras."""

    async def main() -> None:
        ui = policy_ui(block_destructive)
        async with running_broker(ui):
            for mode in ("legacy", "auto"):
                async with Client(_sdk_params(), mode=mode) as client:
                    tools = await client.list_tools()
                    assert "delete_note" in {t.name for t in tools.tools}
                    ok = await client.call_tool("add", {"a": 1, "b": 2})
                    assert not ok.is_error and "3" in ok.content[0].text
                    denied = await client.call_tool("delete_note", {"title": "x"})
                    assert denied.is_error and "blocked" in denied.content[0].text.lower()
        destructive = [r for r in ui.presented if r.tool_name == "delete_note"]
        assert len(destructive) == 2
        assert all(r.client_name for r in destructive)

    asyncio.run(main())
