# Architecture

MCP Guard holds every MCP request from Claude until the user clicks **Allow** or **Block** in a popup that stays above other windows. This document covers where the interception happens, why it happens there, and how the system behaves under concurrency and failure.

## 1. Where MCP traffic actually flows

Claude Desktop and Claude Web send MCP traffic along different paths. The design follows from that difference.

| Client | MCP server type | Who opens the connection | Where it can be intercepted |
|---|---|---|---|
| Claude Desktop | Local server in `claude_desktop_config.json` | Claude Desktop starts it as a **child process** and talks newline-delimited JSON-RPC over stdin/stdout | Between Claude and the child process |
| Claude Web (claude.ai) | Remote server added as a **custom connector** | **Anthropic's cloud** calls the server's URL over Streamable HTTP. The browser never sends MCP traffic | In front of the server, on the HTTP path |
| Claude Desktop | Remote custom connector | Same cloud path as Claude Web | Same as Claude Web |

A browser extension or a TLS-intercepting proxy on the user's machine **cannot** see or block Claude Web tool calls: they never pass through the machine. What arrives in the browser is the chat stream, which reports a call only after it has run. MCP Guard therefore intercepts at two points, one per transport, and both report to a single approval service:

```
                          ┌──────────────────────── user's machine ─────────────────────────┐
                          │                                                                 │
 Claude Desktop ──stdio──▶│ mcpguard wrap ──stdio──▶ local MCP server                       │
                          │      │                                                          │
                          │      │ unix socket (0600)        ┌──────────────────────────┐   │
                          │      └──────────────────────────▶│ mcpguard daemon          │   │
                          │                                  │  • approval broker       │   │
                          │      ┌──────────────────────────▶│  • popup UI (Tk)         │   │
                          │      │ unix socket (0600)        │  • timeout / audit log   │   │
                          │      │                           └──────────────────────────┘   │
 claude.ai ─▶ Anthropic ─HTTPS─▶ tunnel ─▶ mcpguard gateway ──HTTP──▶ remote MCP server     │
 (browser)    cloud       │      (ngrok)                                                    │
                          └─────────────────────────────────────────────────────────────────┘
```

## 2. Components

| Module | Responsibility |
|---|---|
| `stdio_proxy.py` | Transparent stdio proxy. `mcpguard install` rewrites each Claude Desktop server entry so Claude launches `mcpguard wrap -- <original command>`. The wrapper starts the real server and relays traffic in both directions. |
| `http_gateway.py` | Reverse proxy (aiohttp) for Streamable HTTP. Serves a single endpoint at `/<secret>/mcp` and forwards GET (SSE stream), POST (JSON-RPC) and DELETE (end session) to the upstream server. |
| `interceptor.py` | Logic shared by both transports. Decides which messages need approval, records context from earlier traffic (client identity, server identity, tool descriptions and annotations from `tools/list`) and builds denial responses. |
| `broker/` | Local IPC. `server.py` runs inside the daemon. `client.py` is used by interceptors, always returns a definite decision, and starts the daemon if it isn't running. |
| `daemon.py` | Single-instance background service: file lock, threading model, signal handling. |
| `ui/` | Popup backends: `tk_ui.py` (default, cross-platform), `osascript_ui.py` (native macOS dialog if Tk is missing), `auto_ui.py` (policy-driven, used by tests and headless runs). |
| `audit.py` | Append-only JSONL record of every decision, with secret-looking argument values masked. |
| `desktop_config.py` | Installs and uninstalls the wrapper in `claude_desktop_config.json`. Backs up and writes atomically. |
| `demo_server.py` | A harmless demo MCP server (stdio or HTTP) whose tools are read-only, state-changing or destructive. |

Why a separate daemon instead of a popup in each wrapper? Claude Desktop starts one wrapper per configured server, so several processes may need approvals at once. One daemon gives a single UI owner (Tk wants exactly one main loop), one place for the timeout and audit log, and a stable thing for the user to trust. Wrappers stay small and fast to start.

## 3. Request lifecycle

```
Claude ── tools/call id=7 ──▶ interceptor
                              │ 1. parse (strict JSON), classify: method ∈ intercept_methods?
                              │ 2. build ApprovalRequest (server, tool, args, client, hints)
                              │ 3. open socket to daemon, send one JSON line
                              │                         daemon: present popup, start deadline
                              │                         user clicks Allow / Block
                              │ 4. ◀── {"verdict": "allow", "reason": "user"}
                              ├─ allow → re-serialise and forward to server → response flows back
                              └─ block → answer Claude directly (server never sees the request)
```

* **Held, not polled.** A request waits in its own asyncio task for as long as the user takes, up to the decision timeout. The server never sees it before a decision.
* **Denial format.** By default a blocked `tools/call` gets a valid `CallToolResult` with `isError: true` and a sentence explaining the block, so Claude can tell the user what happened instead of showing a protocol error. Set `denial_style: "jsonrpc_error"` to get a JSON-RPC error (code `-32010`) instead. Other gated methods always get a JSON-RPC error.
* **Default allow-list is narrow.** Only `tools/call` is gated by default. `initialize`, `tools/list`, `ping`, notifications and server-initiated requests pass through, so the session behaves normally. Add `resources/read` or `prompts/get` in `intercept_methods` to gate those too.

## 4. Concurrency

* **Every gated request is independent.** In the wrapper, each one runs in its own task, so other requests (including other tool calls and pings) keep flowing while some wait. In the gateway, each HTTP request is its own aiohttp handler.
* **The daemon matches decisions by request.** Each pending approval has its own future, deadline and connection, and the decision travels back on the connection that asked. Answering popups in any order is safe; the tests resolve 20 concurrent requests in random order and 5 in reverse order.
* **One popup per request.** Popups cascade, each with its own countdown and a count of how many requests are waiting.
* **Writes are serialised.** Many tasks write to Claude's stdout (forwarded responses, denials, errors), so all writes go through one writer thread, which keeps lines whole and in order. A slow reader on Claude's side can't stall the event loop.
* **Popups appear over full-screen apps.** On macOS a full-screen app has its own Space, and other apps' windows normally never show there. Tk's "always on top" setting is not enough on its own. So the daemon runs as a background agent app (no Dock icon), and each popup window is set (through AppKit) to appear in every Space, including full-screen ones, above normal windows. A test opens a real full-screen window and confirms through macOS's window list that the popups are on screen and that no Space switch happens.
* **Tk runs on the main thread.** Tk must own the main thread on macOS, so with the Tk backend the asyncio broker runs on a worker thread. The UI's public methods only enqueue commands, and a Tk `after()` loop drains the queue, so Tk is never called from another thread.

## 5. Failure handling

| Situation | Behaviour |
|---|---|
| Nobody answers | Blocked at the deadline (`decision_timeout`, default 120 s; gateway default 60 s), and the popup closes. |
| User closes the popup or presses Esc | Blocked. Return/Enter is not bound to anything, so a stray keypress can't approve. |
| Claude cancels (`notifications/cancelled`) | The popup is dismissed and the request is never forwarded. Per spec, no response is sent. |
| HTTP client disconnects (this is how 2026-07-28 cancels) | The handler is cancelled, which closes the daemon connection, which dismisses the popup. |
| Daemon not running | The wrapper or gateway starts it in the background (`autostart_daemon`). The file lock makes simultaneous starts safe. |
| Daemon unreachable or crashed | **Fail closed**: Block with reason `daemon_unavailable`. `fail_mode: "open"` exists but isn't recommended. |
| Daemon shutting down (SIGTERM, Cmd+Q) | Every pending request is blocked with reason `shutdown` before the socket is removed. |
| Real MCP server crashes | Every in-flight and pending request gets a JSON-RPC error, popups are dismissed, and the wrapper exits with the server's exit code so Claude Desktop shows it as disconnected. |
| Server binary missing | The wrapper exits with code 127 and logs the reason to stderr, which Claude Desktop keeps in `mcp-server-<name>.log`. |
| Claude Desktop quits (stdin closed) | Pending approvals are cancelled, the server's stdin is closed, and the server is terminated (then killed) if it doesn't exit within 5 s. |
| Upstream HTTP server down | Allowed calls get HTTP 502 with a JSON-RPC error carrying the request's id. Blocked calls are answered normally, since they were never forwarded. |
| Malformed input | Parse error `-32700` or invalid request `-32600`. Nothing is forwarded. |
| Popup backend throws | That request is blocked. |

## 6. Security considerations

* **Parsing differences.** If the guard and the server could read the same bytes differently (duplicate keys, `NaN`), a disguised `tools/call` could get through. To prevent this, client messages are parsed strictly (`NaN` and `Infinity` rejected) and **re-serialised** before forwarding, so the server receives exactly what the policy checked. On 2026-07-28 HTTP, the upstream server also checks the `Mcp-Method`/`Mcp-Name` headers against the body.
* **Local IPC.** The daemon socket lives in `~/.mcpguard` (mode `0700`) and is itself `0600`, so other users on the machine can't connect. On Windows, where asyncio has no AF_UNIX, the daemon uses localhost TCP with a random token stored in a `0600` file.
* **Public gateway endpoint.**
  * The tunnel URL is protected by a 192-bit random path segment, compared via the routing table. Every other path returns 404, including OAuth discovery, so claude.ai treats the server as needing no auth.
  * Requests that carry an `Origin` header are rejected unless allow-listed, which stops web pages and DNS-rebinding attacks from driving the endpoint. claude.ai's server-side calls send no `Origin`.
  * The gateway binds to 127.0.0.1 by default and caps request bodies at 16 MB.
* **Untrusted text in the popup.** Tool descriptions and annotations come from the server and are labelled as such.
  * Bidi overrides (e.g. U+202E), zero-width characters and control characters are shown as visible escapes, so arguments can't hide or reverse text.
  * Huge arguments are truncated for display only. The request itself is unchanged.
  * The osascript backend passes text via `argv`, never inside the script, so it can't inject AppleScript.
* **Accidental approval.** Allow stays disabled for 700 ms after a popup appears, so a click or keypress meant for another window can't approve it. Esc and closing the window both block.
* **Audit log.** The log is `0600`. Values under keys like `password`, `token`, `api_key` and `authorization` are masked, and long arguments are truncated.
* **Config changes are reversible.** The installer takes a timestamped backup and writes atomically. Wrapped entries describe themselves, so uninstall needs no saved state.

## 7. Protocol compatibility

The SDK this was built and tested against (MCP Python SDK 2.3) speaks every released revision: `2024-11-05`, `2025-03-26`, `2025-06-18`, `2025-11-25` and the stateless `2026-07-28` (per-request envelope, no `initialize`). The interceptors don't depend on the version:

* `tools/call` is identified by method name, the same in every revision.
* Client identity comes from `initialize.clientInfo` (handshake era) or from `params._meta["io.modelcontextprotocol/clientInfo"]` (2026-07-28).
* Denials include `resultType: "complete"` only when the request carries the 2026-07-28 envelope. That schema requires the field, and older strict clients must not see it.
* JSON-RPC batches (2025-03-26) are supported. Allowed items are forwarded together and denied items are answered individually.
* The test suite runs the official SDK client through both the wrapper and the gateway, in both `legacy` (handshake) and `auto` (2026-07-28 discovery) modes.

## 8. Alternatives considered

| Approach | Why it was not used |
|---|---|
| TLS-intercepting proxy (mitmproxy) on claude.ai traffic | Remote MCP calls never pass through the user's machine. The proxy would only see the chat stream, after the tool has already run. It would also break certificate pinning and tamper with a third-party service. |
| Browser extension | Same issue: it only sees the UI stream and can't hold a request made from Anthropic's cloud. |
| Patching Claude Desktop or injecting into its process | Fragile across updates, needs elevated access, and is invasive. The config-level stdio wrapper uses a supported extension point and survives updates. |
| A popup inside each wrapper | Several processes would compete to own the UI. There would be no single place for the timeout and audit log, and Tk's main-thread rule would apply in every wrapper. |
| Holding requests on Claude's side (relying on Claude Desktop's built-in permission prompt) | Not under the user's or organisation's policy control, and it doesn't exist for every path. MCP Guard enforces approval regardless of client settings. |

## 9. Known limitations

* **Desktop Extensions (`.mcpb`/`.dxt`)** installed through Claude Desktop's UI aren't listed in `claude_desktop_config.json`, so `mcpguard install` can't wrap them. Add the same server as a regular config entry instead.
* **Connectors Anthropic hosts** (first-party integrations) run entirely in Anthropic's infrastructure. Only servers whose traffic you route through the gateway can be gated.
* **Proxy timeouts on the web path.** Long waits can hit timeouts in the tunnel (ngrok), and claude.ai may also give up on a slow tool call. Keep the gateway's `--decision-timeout` moderate. A future improvement is to switch the response to SSE and send keep-alive pings while waiting, as the 2026-07-28 transport does.
* **Batch cancellation.** Cancelling one item of a pending JSON-RPC batch cancels the whole batch's approval. Batches were removed from the protocol after 2025-03-26.
* **Platforms.** Developed and tested on macOS. Linux should work (Tk popup, Unix socket). The Windows path (TCP broker, config location) is implemented but untested.
