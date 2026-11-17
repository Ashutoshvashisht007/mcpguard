"""Transport-independent interception logic shared by the stdio wrapper and the HTTP gateway.

The interceptor decides *which* messages need approval, enriches the approval request with
context learned from earlier traffic (client identity from ``initialize``/``_meta``, server
identity, tool descriptions and annotations from ``tools/list``) and builds denial responses.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from mcpguard import jsonrpc
from mcpguard.broker.client import BrokerClient
from mcpguard.config import Settings
from mcpguard.models import ApprovalRequest, Decision, Reason

log = logging.getLogger(__name__)

MAX_DESCRIPTION_CHARS = 1000
#: Arguments larger than this are shown truncated in the popup (the real request is unchanged).
MAX_ARGUMENT_PREVIEW_BYTES = 1024 * 1024


class Interceptor:
    def __init__(
        self,
        settings: Settings,
        broker: BrokerClient,
        *,
        source: str,
        transport: str,
        server_name: str,
        decision_timeout: float | None = None,
    ) -> None:
        self.settings = settings
        self.broker = broker
        self.source = source
        self.transport = transport
        self.server_name = server_name
        self.decision_timeout = decision_timeout
        self.client_info: dict[str, Any] | None = None
        self.server_info: dict[str, Any] | None = None
        self.tools: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ classification
    def needs_approval(self, msg: Any) -> bool:
        return jsonrpc.is_request(msg) and msg["method"] in self.settings.intercept_methods

    # ------------------------------------------------------------------ passive monitoring
    def observe_client(self, msg: Any) -> dict[str, Any] | None:
        info = jsonrpc.client_info_from(msg)
        if info:
            self.client_info = info
        return info

    def observe_response(self, method: str, msg: dict[str, Any]) -> None:
        result = msg.get("result")
        info = jsonrpc.server_info_from(method, result)
        if info:
            self.server_info = info
        if method == "tools/list" and isinstance(result, dict) and isinstance(result.get("tools"), list):
            for tool in result["tools"]:
                if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                    self.tools[tool["name"]] = tool

    # ------------------------------------------------------------------ approval
    def build_request(self, msg: dict[str, Any], client_info: dict[str, Any] | None = None) -> ApprovalRequest:
        method = msg["method"]
        tool_name, arguments = jsonrpc.tool_call_parts(msg) if method == "tools/call" else (None, None)
        if method != "tools/call":
            arguments = msg.get("params")
        arguments = _preview(arguments)
        client_name, client_version = jsonrpc.describe_implementation(client_info or self.client_info)
        server_name, server_version = jsonrpc.describe_implementation(self.server_info)
        tool = self.tools.get(tool_name or "", {})
        annotations = tool.get("annotations") if isinstance(tool.get("annotations"), dict) else {}
        description = tool.get("description")
        if isinstance(description, str) and len(description) > MAX_DESCRIPTION_CHARS:
            description = description[:MAX_DESCRIPTION_CHARS] + "…"
        return ApprovalRequest(
            source=self.source,
            transport=self.transport,
            server_name=self.server_name,
            method=method,
            jsonrpc_id=msg.get("id"),
            tool_name=tool_name,
            arguments=arguments,
            client_name=client_name,
            client_version=client_version,
            server_info=" ".join(p for p in (server_name, server_version) if p) or None,
            tool_title=tool.get("title") if isinstance(tool.get("title"), str) else None,
            tool_description=description if isinstance(description, str) else None,
            tool_annotations={k: v for k, v in annotations.items() if isinstance(v, (bool, str, int, float))},
            timeout=self.decision_timeout,
        )

    async def decide(self, msg: dict[str, Any], client_info: dict[str, Any] | None = None) -> Decision:
        try:
            request = self.build_request(msg, client_info)
            log.info("approval needed: %s (id=%s)", request.title, msg.get("id"))
            decision = await self.broker.request_decision(request)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # fail closed on anything unexpected
            log.exception("approval failed")
            decision = Decision.block(Reason.ERROR, str(exc))
        log.info("decision for id=%s: %s (%s)", msg.get("id"), decision.verdict.value, decision.reason.value)
        return decision

    def denial(self, msg: dict[str, Any], decision: Decision) -> dict[str, Any]:
        return jsonrpc.denial_response(msg, decision, self.settings.denial_style, self.server_name)


def _preview(arguments: Any) -> Any:
    try:
        size = len(json.dumps(arguments, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return repr(arguments)[:MAX_ARGUMENT_PREVIEW_BYTES]
    if size <= MAX_ARGUMENT_PREVIEW_BYTES:
        return arguments
    text = json.dumps(arguments, ensure_ascii=False, default=str)[: MAX_ARGUMENT_PREVIEW_BYTES // 2]
    return {"_mcpguard_preview": f"arguments are {size:,} bytes; showing the beginning only", "truncated": text}
