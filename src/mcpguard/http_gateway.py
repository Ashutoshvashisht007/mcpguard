"""Reverse proxy for remote (Streamable HTTP) MCP servers — the Claude Web path.

Custom connectors added on claude.ai are called **from Anthropic's cloud**, not from the user's
browser, so the only place to intercept them is in front of the MCP server itself:

    claude.ai (Anthropic cloud) ──HTTPS──▶ tunnel ──▶ mcpguard gateway ──▶ upstream MCP server
                                                            │
                                                            └──unix socket──▶ approval daemon

The gateway serves a single MCP endpoint at ``/<secret-token>/mcp`` and forwards GET (SSE
stream), POST (JSON-RPC) and DELETE (session end) to the upstream URL. POST bodies containing
requests that need approval are held until the user decides; blocked requests are answered by
the gateway and never reach the upstream. Responses stream through unchanged (JSON or SSE).

Works with both protocol eras: the session-based 2025-xx transport (``initialize`` +
``Mcp-Session-Id``) and the stateless 2026-07-28 per-request envelope (client identity in
``_meta``, ``Mcp-Method``/``Mcp-Name`` routing headers, cancellation = closing the stream).
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections import OrderedDict
from collections.abc import Callable, Iterable
from typing import Any

import aiohttp
from aiohttp import web
from multidict import CIMultiDict

from mcpguard import jsonrpc
from mcpguard.broker.protocol import write_private_file
from mcpguard.config import Paths
from mcpguard.interceptor import Interceptor
from mcpguard.models import Decision, Reason

log = logging.getLogger(__name__)

HOP_BY_HOP = frozenset(
    {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "trailers", "transfer-encoding", "upgrade"}
)
#: Request headers never forwarded upstream (Host is rewritten by the client library; Origin
#: has been validated here; Accept-Encoding is forced to identity so bodies stay inspectable).
DROP_REQUEST_HEADERS = HOP_BY_HOP | {"host", "content-length", "accept-encoding", "origin"}
DROP_RESPONSE_HEADERS = HOP_BY_HOP | {"content-length"}
MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_SESSIONS = 1000
SESSION_HEADER = "Mcp-Session-Id"


def load_or_create_path_token(paths: Paths, rotate: bool = False) -> str:
    """Random URL path segment that keeps the publicly tunnelled endpoint unguessable."""
    paths.ensure()
    token_file = paths.gateway_token_file
    if not rotate and token_file.exists():
        token = token_file.read_text("utf-8").strip()
        if len(token) >= 16:
            return token
    token = secrets.token_urlsafe(24)
    write_private_file(token_file, token)
    return token


class SseTap:
    """Incrementally parses an SSE byte stream (without altering it) and reports each event's
    JSON payload — used to learn tool metadata from ``tools/list`` responses."""

    MAX_BUFFER = 32 * 1024 * 1024

    def __init__(self, on_message: Callable[[Any], None]) -> None:
        self._on_message = on_message
        self._buffer = b""

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk.replace(b"\r\n", b"\n")
        while b"\n\n" in self._buffer:
            event, self._buffer = self._buffer.split(b"\n\n", 1)
            data = [line[5:].lstrip(b" ") for line in event.split(b"\n") if line.startswith(b"data:")]
            if data:
                try:
                    self._on_message(jsonrpc.loads(b"\n".join(data)))
                except ValueError:
                    pass
        if len(self._buffer) > self.MAX_BUFFER:
            self._buffer = b""


def sse_event(obj: Any) -> bytes:
    return b"event: message\ndata: " + json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode() + b"\n\n"


class HttpGateway:
    def __init__(
        self,
        *,
        upstream: str,
        interceptor: Interceptor,
        path_token: str,
        allowed_origins: Iterable[str] = (),
        upstream_headers: dict[str, str] | None = None,
    ) -> None:
        self.upstream = upstream
        self.interceptor = interceptor
        self.mcp_path = f"/{path_token}/mcp"
        self.allowed_origins = frozenset(allowed_origins)
        self.upstream_headers = dict(upstream_headers or {})
        self._sessions: OrderedDict[str, dict[str, Any]] = OrderedDict()  # session id → clientInfo
        self._pending: dict[tuple[str, str], asyncio.Future[Decision]] = {}
        self._http: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------------ app wiring
    def make_app(self) -> web.Application:
        app = web.Application(client_max_size=MAX_BODY_BYTES)
        app.router.add_route("*", self.mcp_path, self._handle)
        app.router.add_route("*", "/{tail:.*}", self._not_found)
        app.on_startup.append(self._on_startup)
        app.on_cleanup.append(self._on_cleanup)
        return app

    async def _on_startup(self, _app: web.Application) -> None:
        self._http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=10),
            auto_decompress=False,
        )

    async def _on_cleanup(self, _app: web.Application) -> None:
        for fut in list(self._pending.values()):
            fut.cancel()
        if self._http is not None:
            await self._http.close()

    async def _not_found(self, _request: web.Request) -> web.Response:
        return web.Response(status=404, text="Not Found")

    # ------------------------------------------------------------------ request handling
    async def _handle(self, request: web.Request) -> web.StreamResponse:
        origin = request.headers.get("Origin")
        if origin and origin not in self.allowed_origins:
            # Browsers always send Origin; claude.ai's server-side connector calls do not.
            # Rejecting unknown origins stops web pages from driving the endpoint (DNS rebinding).
            log.warning("rejected request with Origin %s", origin)
            return web.json_response(jsonrpc.error_response(None, jsonrpc.INVALID_REQUEST, "Origin not allowed"), status=403)
        if request.method == "POST":
            return await self._post(request)
        if request.method in ("GET", "DELETE"):
            if request.method == "DELETE":
                self._sessions.pop(request.headers.get(SESSION_HEADER, ""), None)
            return await self._forward(request, None)
        return web.Response(status=405, headers={"Allow": "GET, POST, DELETE"})

    async def _post(self, request: web.Request) -> web.StreamResponse:
        if request.content_type != "application/json":
            return web.json_response(
                jsonrpc.error_response(None, jsonrpc.INVALID_REQUEST, "Content-Type must be application/json"), status=415
            )
        raw = await request.read()
        try:
            msg = jsonrpc.loads(raw)
        except ValueError:
            return web.json_response(jsonrpc.error_response(None, jsonrpc.PARSE_ERROR, "Parse error"), status=400)
        items = msg if isinstance(msg, list) else [msg]
        if not items or not all(isinstance(m, dict) for m in items):
            return web.json_response(jsonrpc.error_response(None, jsonrpc.INVALID_REQUEST, "Invalid Request"), status=400)

        session_id = request.headers.get(SESSION_HEADER, "")
        request_client_info: dict[str, Any] | None = None
        for item in items:
            info = self.interceptor.observe_client(item)
            if info:
                request_client_info = info
                if session_id:
                    self._remember_session(session_id, info)
            if jsonrpc.is_notification(item) and item["method"] == jsonrpc.CANCELLED_NOTIFICATION:
                self._cancel_pending(session_id, item)
        methods = {jsonrpc.id_key(m["id"]): m["method"] for m in items if jsonrpc.is_request(m)}

        gated = [m for m in items if self.interceptor.needs_approval(m)]
        if not gated:
            return await self._forward(request, jsonrpc.encode(msg), methods, request_client_info)

        client_info = request_client_info or self._sessions.get(session_id) or self.interceptor.client_info
        if not client_info and request.headers.get("User-Agent"):
            client_info = {"name": f"User-Agent: {request.headers['User-Agent'][:80]}"}
        decisions = await asyncio.gather(*(self._decide(m, session_id, client_info) for m in gated))
        blocked = {id(m) for m, d in zip(gated, decisions) if not d.allowed}
        denials = [self.interceptor.denial(m, d) for m, d in zip(gated, decisions) if not d.allowed]

        if not isinstance(msg, list):
            if denials:
                return web.json_response(denials[0])
            return await self._forward(request, jsonrpc.encode(msg), methods, request_client_info)
        remaining = [m for m in items if id(m) not in blocked]
        if not remaining:
            return web.json_response(denials)
        return await self._forward(request, jsonrpc.encode(remaining), methods, request_client_info, extra=denials)

    async def _decide(self, msg: dict[str, Any], session_id: str, client_info: dict[str, Any] | None) -> Decision:
        key = (session_id, jsonrpc.id_key(msg["id"]))
        task = asyncio.ensure_future(self.interceptor.decide(msg, client_info))
        self._pending[key] = task
        try:
            # asyncio.wait does not raise if *task* is cancelled (cancel notification), only if
            # this handler is cancelled (client disconnected) — the two cases need different answers.
            await asyncio.wait({task})
            if task.cancelled():
                return Decision.block(Reason.CANCELLED, "client sent notifications/cancelled")
            return task.result()
        finally:
            if not task.done():
                task.cancel()  # closes the daemon connection → popup dismissed
            if self._pending.get(key) is task:
                del self._pending[key]

    def _cancel_pending(self, session_id: str, notification: dict[str, Any]) -> None:
        params = notification.get("params")
        if isinstance(params, dict) and "requestId" in params:
            fut = self._pending.get((session_id, jsonrpc.id_key(params["requestId"])))
            if fut is not None:
                log.info("client cancelled pending request %s", params["requestId"])
                fut.cancel()

    def _remember_session(self, session_id: str, client_info: dict[str, Any]) -> None:
        self._sessions[session_id] = client_info
        self._sessions.move_to_end(session_id)
        while len(self._sessions) > MAX_SESSIONS:
            self._sessions.popitem(last=False)

    # ------------------------------------------------------------------ upstream forwarding
    def _upstream_request_headers(self, request: web.Request) -> CIMultiDict[str]:
        headers: CIMultiDict[str] = CIMultiDict()
        for name, value in request.headers.items():
            if name.lower() not in DROP_REQUEST_HEADERS:
                headers.add(name, value)
        headers["Accept-Encoding"] = "identity"
        for name, value in self.upstream_headers.items():
            headers[name] = value
        return headers

    async def _forward(
        self,
        request: web.Request,
        body: bytes | None,
        methods: dict[str, str] | None = None,
        init_client_info: dict[str, Any] | None = None,
        extra: list[dict[str, Any]] | None = None,
    ) -> web.StreamResponse:
        assert self._http is not None
        url = self.upstream + (f"?{request.query_string}" if request.query_string else "")
        try:
            upstream = await self._http.request(
                request.method, url, headers=self._upstream_request_headers(request), data=body, allow_redirects=False
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            log.warning("upstream %s unreachable: %s", self.upstream, exc)
            return self._upstream_failure(body)
        try:
            session_id = upstream.headers.get(SESSION_HEADER)
            if session_id and init_client_info:
                self._remember_session(session_id, init_client_info)
            headers = CIMultiDict(
                (k, v) for k, v in upstream.headers.items() if k.lower() not in DROP_RESPONSE_HEADERS
            )
            content_type = upstream.headers.get("Content-Type", "").split(";")[0].strip().lower()

            if content_type != "text/event-stream":
                data = await upstream.read()
                if methods and content_type == "application/json":
                    self._observe_payload(data, methods)
                if extra:
                    data = self._merge_batch(data, extra)
                return web.Response(status=upstream.status, body=data, headers=headers)

            response = web.StreamResponse(status=upstream.status, headers=headers)
            await response.prepare(request)
            for denial in extra or ():
                await response.write(sse_event(denial))
            tap = SseTap(lambda obj: self._observe_obj(obj, methods)) if methods else None
            async for chunk in upstream.content.iter_any():
                await response.write(chunk)
                if tap is not None:
                    tap.feed(chunk)
            await response.write_eof()
            return response
        finally:
            upstream.release()

    def _observe_payload(self, data: bytes, methods: dict[str, str]) -> None:
        try:
            self._observe_obj(jsonrpc.loads(data), methods)
        except ValueError:
            pass

    def _observe_obj(self, obj: Any, methods: dict[str, str] | None) -> None:
        if not methods:
            return
        for item in obj if isinstance(obj, list) else [obj]:
            if jsonrpc.is_response(item):
                method = methods.get(jsonrpc.id_key(item["id"]))
                if method:
                    try:
                        self.interceptor.observe_response(method, item)
                    except Exception:
                        log.debug("could not inspect response", exc_info=True)

    @staticmethod
    def _merge_batch(data: bytes, extra: list[dict[str, Any]]) -> bytes:
        try:
            parsed = jsonrpc.loads(data) if data.strip() else []
        except ValueError:
            log.warning("could not merge denials into a non-JSON upstream batch response")
            return data
        merged = (parsed if isinstance(parsed, list) else [parsed]) + extra
        return json.dumps(merged, separators=(",", ":"), ensure_ascii=False).encode()

    @staticmethod
    def _upstream_failure(body: bytes | None) -> web.Response:
        message = "Upstream MCP server is unreachable"
        errors: list[dict[str, Any]] = []
        if body:
            try:
                msg = jsonrpc.loads(body)
                for item in msg if isinstance(msg, list) else [msg]:
                    if jsonrpc.is_request(item):
                        errors.append(jsonrpc.error_response(item["id"], jsonrpc.INTERNAL_ERROR, message))
            except ValueError:
                pass
        if not errors:
            return web.Response(status=502, text=message)
        payload: Any = errors if len(errors) > 1 else errors[0]
        return web.json_response(payload, status=502)


async def serve_gateway(gateway: HttpGateway, host: str, port: int, stop: asyncio.Event) -> None:
    runner = web.AppRunner(gateway.make_app(), handler_cancellation=True, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    try:
        await stop.wait()
    finally:
        await runner.cleanup()
