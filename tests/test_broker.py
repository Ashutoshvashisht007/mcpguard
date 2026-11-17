"""Approval broker ⇄ client: decisions, timeouts, cancellation, concurrency, fail-closed."""

from __future__ import annotations

import asyncio
import json
import random

from conftest import manual_ui, policy_ui, running_broker, wait_for

from mcpguard.broker.client import BrokerClient
from mcpguard.config import Paths, Settings
from mcpguard.models import ApprovalRequest, Reason, Verdict


def make_request(tool: str = "write_note", **kw) -> ApprovalRequest:
    return ApprovalRequest(
        source="test", transport="stdio", server_name="demo", method="tools/call", tool_name=tool,
        arguments=kw.pop("arguments", {"title": "x"}), **kw,
    )


def client(**overrides) -> BrokerClient:
    return BrokerClient(Settings.load().merged(overrides))


def test_ping_and_policy_decisions(guard_home) -> None:
    async def main() -> None:
        ui = policy_ui(lambda r: Verdict.BLOCK if r.tool_name == "delete_note" else Verdict.ALLOW)
        async with running_broker(ui):
            c = client()
            pong = await c.ping()
            assert pong and pong["type"] == "pong" and pong["pending"] == 0
            allowed = await c.request_decision(make_request("write_note"))
            blocked = await c.request_decision(make_request("delete_note"))
            assert allowed.allowed and allowed.reason is Reason.USER
            assert not blocked.allowed and blocked.reason is Reason.USER

    asyncio.run(main())


def test_timeout_blocks_and_dismisses_popup(guard_home) -> None:
    async def main() -> None:
        ui = manual_ui()
        async with running_broker(ui, decision_timeout=1):
            decision = await client().request_decision(make_request())
            assert not decision.allowed and decision.reason is Reason.TIMEOUT
            assert ui.dismissed == [ui.presented[0].id]

    asyncio.run(main())


def test_requester_disconnect_cancels_and_dismisses(guard_home) -> None:
    async def main() -> None:
        ui = manual_ui()
        async with running_broker(ui):
            task = asyncio.ensure_future(client().request_decision(make_request()))
            await wait_for(lambda: len(ui.presented) == 1, what="popup")
            task.cancel()
            await wait_for(lambda: len(ui.dismissed) == 1, what="dismissal")
        entry = json.loads(Paths.default().audit_log.read_text().splitlines()[-1])
        assert entry["reason"] == "cancelled" and entry["verdict"] == "block"

    asyncio.run(main())


def test_many_concurrent_requests_resolved_in_random_order(guard_home) -> None:
    async def main() -> None:
        ui = manual_ui()
        async with running_broker(ui) as broker:
            c = client()
            requests = [make_request(f"tool{i}") for i in range(20)]
            tasks = [asyncio.ensure_future(c.request_decision(r)) for r in requests]
            await wait_for(lambda: len(ui.presented) == 20, what="20 popups")
            assert broker.pending_count == 20
            expected = {}
            for presented in random.sample(ui.presented, k=20):
                verdict = random.choice([Verdict.ALLOW, Verdict.BLOCK])
                expected[presented.tool_name] = verdict
                ui.report(presented.id, verdict)
            results = await asyncio.gather(*tasks)
            for request, decision in zip(requests, results):
                assert decision.verdict is expected[request.tool_name]

    asyncio.run(main())


def test_shutdown_blocks_pending(guard_home) -> None:
    async def main() -> None:
        ui = manual_ui()
        async with running_broker(ui) as broker:
            task = asyncio.ensure_future(client().request_decision(make_request()))
            await wait_for(lambda: len(ui.presented) == 1, what="popup")
            await broker.shutdown()
            decision = await task
            assert not decision.allowed and decision.reason is Reason.SHUTDOWN

    asyncio.run(main())


def test_fail_closed_and_fail_open_without_daemon(guard_home) -> None:
    async def main() -> None:
        closed = await client().request_decision(make_request())
        assert not closed.allowed and closed.reason is Reason.UNAVAILABLE
        opened = await client(fail_mode="open").request_decision(make_request())
        assert opened.allowed and opened.reason is Reason.UNAVAILABLE

    asyncio.run(main())


def test_malformed_messages_get_errors(guard_home) -> None:
    async def main() -> None:
        async with running_broker(manual_ui()):
            for payload in (b"not json\n", b'{"type":"approve","request":{"source":1}}\n', b'{"type":"bogus"}\n'):
                reader, writer = await asyncio.open_unix_connection(str(Paths.default().socket))
                writer.write(payload)
                await writer.drain()
                reply = json.loads(await reader.readline())
                assert reply["type"] == "error"
                writer.close()

    asyncio.run(main())


def test_audit_log_redacts_secrets(guard_home) -> None:
    async def main() -> None:
        async with running_broker(policy_ui(lambda r: Verdict.ALLOW)):
            await client().request_decision(make_request(arguments={"url": "https://x", "api_key": "sk-123"}))
        log_file = Paths.default().audit_log
        entry = json.loads(log_file.read_text().splitlines()[-1])
        assert "sk-123" not in entry["arguments"] and "[redacted]" in entry["arguments"]
        assert entry["verdict"] == "allow" and entry["tool"] == "write_note"
        assert oct(log_file.stat().st_mode & 0o777) == "0o600"

    asyncio.run(main())


def test_socket_is_private(guard_home) -> None:
    async def main() -> None:
        async with running_broker(manual_ui()):
            paths = Paths.default()
            assert oct(paths.socket.stat().st_mode & 0o777) == "0o600"
            assert oct(paths.home.stat().st_mode & 0o777) == "0o700"

    asyncio.run(main())
