"""Unit tests for the pure helpers: JSON-RPC classification, denials, settings, display safety."""

from __future__ import annotations

import json

import pytest

from mcpguard import jsonrpc
from mcpguard.audit import REDACTED, redact
from mcpguard.config import Paths, Settings
from mcpguard.http_gateway import SseTap
from mcpguard.models import Decision, Reason
from mcpguard.ui.base import format_arguments, hint_labels, safe_text


def test_message_classification() -> None:
    assert jsonrpc.is_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call"})
    assert jsonrpc.is_request({"jsonrpc": "2.0", "id": 0, "method": "ping"})
    assert not jsonrpc.is_request({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert jsonrpc.is_notification({"jsonrpc": "2.0", "method": "notifications/cancelled"})
    assert jsonrpc.is_response({"jsonrpc": "2.0", "id": 1, "result": {}})
    assert not jsonrpc.is_response({"jsonrpc": "2.0", "id": 1, "method": "x"})
    assert jsonrpc.id_key(1) != jsonrpc.id_key("1")


def test_strict_parser_rejects_nan() -> None:
    with pytest.raises(ValueError):
        jsonrpc.loads('{"a": NaN}')


def test_reencoding_resolves_duplicate_keys_once() -> None:
    # The policy check and the forwarded bytes must agree: re-serialising after parsing
    # collapses duplicate keys so the server sees exactly what was approved.
    msg = jsonrpc.loads('{"jsonrpc":"2.0","id":1,"method":"tools/list","method":"tools/call","params":{"name":"x"}}')
    assert msg["method"] == "tools/call"
    assert jsonrpc.encode(msg).count(b'"method"') == 1


def test_client_info_legacy_and_modern() -> None:
    legacy = {"id": 0, "method": "initialize", "params": {"clientInfo": {"name": "claude-ai", "version": "1"}}}
    modern = {"id": 3, "method": "tools/call", "params": {"_meta": {jsonrpc.CLIENT_INFO_META_KEY: {"name": "claude-ai"}}}}
    assert jsonrpc.client_info_from(legacy) == {"name": "claude-ai", "version": "1"}
    assert jsonrpc.client_info_from(modern) == {"name": "claude-ai"}
    assert jsonrpc.client_info_from({"id": 1, "method": "tools/call", "params": {}}) is None


def test_denial_styles() -> None:
    msg = {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "delete_note", "arguments": {}}}
    decision = Decision.block(Reason.USER)
    as_tool_error = jsonrpc.denial_response(msg, decision, "tool_error", "demo")
    assert as_tool_error["id"] == 9
    assert as_tool_error["result"]["isError"] is True
    assert "delete_note" in as_tool_error["result"]["content"][0]["text"]
    as_rpc_error = jsonrpc.denial_response(msg, decision, "jsonrpc_error", "demo")
    assert as_rpc_error["error"]["code"] == jsonrpc.REQUEST_BLOCKED
    assert as_rpc_error["error"]["data"]["reason"] == "user"
    other = {"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": "file:///x"}}
    assert "error" in jsonrpc.denial_response(other, decision, "tool_error", "demo")


def test_denial_matches_protocol_era() -> None:
    # 2026-07-28 results must carry resultType; handshake-era results must not gain new keys.
    legacy = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "x"}}
    modern = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "x", "_meta": {jsonrpc.PROTOCOL_VERSION_META_KEY: "2026-07-28"}}}
    decision = Decision.block(Reason.USER)
    assert "resultType" not in jsonrpc.denial_response(legacy, decision, "tool_error", "s")["result"]
    assert jsonrpc.denial_response(modern, decision, "tool_error", "s")["result"]["resultType"] == "complete"


def test_settings_env_override_and_validation(tmp_path) -> None:
    paths = Paths(tmp_path)
    settings = Settings.load(paths, env={"MCPGUARD_DECISION_TIMEOUT": "30", "MCPGUARD_INTERCEPT_METHODS": "tools/call, resources/read"})
    assert settings.decision_timeout == 30.0
    assert settings.intercept_methods == ("tools/call", "resources/read")
    (tmp_path / "config.json").write_text(json.dumps({"fail_mode": "open", "audit": False}))
    settings = Settings.load(paths, env={})
    assert settings.fail_mode == "open" and settings.audit is False
    with pytest.raises(ValueError):
        Settings().merged({"fail_mode": "sometimes"})
    with pytest.raises(ValueError):
        Settings().merged({"decision_timeout": 0})


def test_redaction_is_deep_and_case_insensitive() -> None:
    data = {"user": "a", "API_KEY": "k", "nested": [{"Password": "p", "ok": 1}], "headers": {"Authorization": "x"}}
    out = redact(data, ["password", "api_key", "authorization"])
    assert out == {"user": "a", "API_KEY": REDACTED, "nested": [{"Password": REDACTED, "ok": 1}], "headers": {"Authorization": REDACTED}}
    assert data["API_KEY"] == "k"  # original untouched


def test_safe_text_exposes_deceptive_characters() -> None:
    shown = safe_text("invoice‮fdp.exe​\x07\U0001f600")
    assert "‮" not in shown and "<U+202E>" in shown
    assert "<U+200B>" in shown and "\\x07" in shown
    assert "\\U0001f600" in shown
    assert "more characters not shown" in safe_text("x" * 50, limit=10)


def test_format_arguments_and_hints() -> None:
    assert format_arguments(None) == "(no arguments)"
    assert '"a": 1' in format_arguments({"a": 1})
    labels = hint_labels({"destructiveHint": True, "readOnlyHint": False, "openWorldHint": True})
    assert ("destructive", True) in labels and ("reaches external systems", True) in labels


def test_sse_tap_handles_split_chunks() -> None:
    seen = []
    tap = SseTap(seen.append)
    payload = b'event: message\r\ndata: {"jsonrpc":"2.0","id":1,"result":{"tools":[]}}\r\n\r\n'
    for i in range(0, len(payload), 7):
        tap.feed(payload[i : i + 7])
    tap.feed(b": keepalive\n\n")
    assert seen == [{"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}]
