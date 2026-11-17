"""Small, dependency-free JSON-RPC 2.0 helpers for MCP traffic.

MCP over stdio frames messages as newline-delimited JSON; Streamable HTTP carries the same
JSON-RPC objects in POST bodies / SSE events. Batches (JSON arrays) are accepted for
compatibility with protocol 2025-03-26.
"""

from __future__ import annotations

import json
from typing import Any

from mcpguard.models import Decision, Reason

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
INTERNAL_ERROR = -32603
#: Implementation-defined code (-32000..-32019 band; -32000/-32001 are taken by the MCP SDK).
REQUEST_BLOCKED = -32010

CANCELLED_NOTIFICATION = "notifications/cancelled"
CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"
SERVER_INFO_META_KEY = "io.modelcontextprotocol/serverInfo"
PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"


def encode(message: Any) -> bytes:
    """Serialize one message (or batch) as a single compact line.

    Interceptors always *re-serialize* client messages instead of forwarding the raw bytes, so
    the server parses exactly what the policy check parsed (no duplicate-key / parser
    differential tricks).
    """
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"


def loads(data: bytes | str) -> Any:
    """Strict JSON parse: rejects NaN/Infinity, which other JSON parsers disagree on."""
    return json.loads(data, parse_constant=_reject_constant)


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-standard JSON constant {name}")


def is_request(msg: Any) -> bool:
    return isinstance(msg, dict) and isinstance(msg.get("method"), str) and "id" in msg


def is_notification(msg: Any) -> bool:
    return isinstance(msg, dict) and isinstance(msg.get("method"), str) and "id" not in msg


def is_response(msg: Any) -> bool:
    return isinstance(msg, dict) and "method" not in msg and "id" in msg and ("result" in msg or "error" in msg)


def id_key(request_id: Any) -> str:
    """Hashable key that keeps 1 and "1" distinct."""
    return json.dumps(request_id, sort_keys=True)


def error_response(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": err}


def tool_call_parts(msg: dict[str, Any]) -> tuple[str | None, Any]:
    params = msg.get("params")
    if not isinstance(params, dict):
        return None, None
    name = params.get("name")
    return (name if isinstance(name, str) else None), params.get("arguments")


def client_info_from(msg: Any) -> dict[str, Any] | None:
    """clientInfo from a legacy ``initialize`` request or a 2026-07-28 per-request ``_meta``."""
    if not isinstance(msg, dict):
        return None
    params = msg.get("params")
    if not isinstance(params, dict):
        return None
    if msg.get("method") == "initialize" and isinstance(params.get("clientInfo"), dict):
        return params["clientInfo"]
    meta = params.get("_meta")
    if isinstance(meta, dict) and isinstance(meta.get(CLIENT_INFO_META_KEY), dict):
        return meta[CLIENT_INFO_META_KEY]
    return None


def uses_request_envelope(msg: Any) -> bool:
    """True for 2026-07-28+ requests, which carry their protocol version in ``params._meta``."""
    params = msg.get("params") if isinstance(msg, dict) else None
    meta = params.get("_meta") if isinstance(params, dict) else None
    return isinstance(meta, dict) and PROTOCOL_VERSION_META_KEY in meta


def server_info_from(method: str, result: Any) -> dict[str, Any] | None:
    """serverInfo from an ``initialize`` or ``server/discover`` result."""
    if not isinstance(result, dict):
        return None
    if method in ("initialize", "server/discover") and isinstance(result.get("serverInfo"), dict):
        return result["serverInfo"]
    meta = result.get("_meta")
    if isinstance(meta, dict) and isinstance(meta.get(SERVER_INFO_META_KEY), dict):
        return meta[SERVER_INFO_META_KEY]
    return None


def describe_implementation(info: dict[str, Any] | None) -> tuple[str | None, str | None]:
    if not info:
        return None, None
    name = info.get("title") or info.get("name")
    version = info.get("version")
    return (str(name) if name else None), (str(version) if version else None)


_REASON_TEXT = {
    Reason.USER: "The user blocked this request in MCP Guard.",
    Reason.TIMEOUT: "No decision was made in time, so MCP Guard blocked this request automatically.",
    Reason.CANCELLED: "The request was cancelled before the user decided.",
    Reason.SHUTDOWN: "MCP Guard is shutting down, so the request was blocked.",
    Reason.UNAVAILABLE: "The MCP Guard approval service is not running, so the request was blocked (fail-closed).",
    Reason.ERROR: "MCP Guard hit an internal error and blocked the request (fail-closed).",
}


def denial_response(
    msg: dict[str, Any], decision: Decision, style: str, server_name: str
) -> dict[str, Any]:
    """Build the response returned to the client when a request is not allowed."""
    method = msg.get("method")
    tool_name, _ = tool_call_parts(msg) if method == "tools/call" else (None, None)
    target = f"tool '{tool_name}' on MCP server '{server_name}'" if tool_name else f"'{method}' on MCP server '{server_name}'"
    text = f"{_REASON_TEXT.get(decision.reason, 'Request blocked.')} Blocked: {target}."
    if decision.reason is Reason.USER:
        text += " Do not retry this call unless the user asks you to."
    data = {"blockedBy": "mcpguard", "reason": decision.reason.value}
    if decision.detail:
        data["detail"] = decision.detail
    if method == "tools/call" and style == "tool_error":
        result: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": True, "_meta": {"mcpguard": data}}
        if uses_request_envelope(msg):
            result["resultType"] = "complete"  # required by the 2026-07-28 schema, absent before
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": result}
    return error_response(msg.get("id"), REQUEST_BLOCKED, text, data)
