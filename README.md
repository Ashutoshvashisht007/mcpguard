# MCP Guard

**Approve or block every MCP tool call that Claude Desktop or Claude Web makes, from a popup that stays above all other windows.**

MCP Guard runs in the background and intercepts Model Context Protocol requests. It holds each request until you decide, shows what is about to run (server, tool, arguments, the server's own risk hints) and then forwards the request or returns a denial to Claude. Each simultaneous request is handled on its own. If anything breaks, requests are blocked (it fails closed), and every decision goes to an audit log.

> 🎥 **Demo video:** _add link here_  ·  📐 **Architecture:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)  ·  🎬 **Demo script:** [docs/DEMO.md](docs/DEMO.md)

---

## How it works

Claude Desktop and Claude Web reach MCP servers along **different paths**, so MCP Guard intercepts at two points that share one approval service:

```
 Claude Desktop ──stdio──▶ mcpguard wrap ──stdio──▶ local MCP server
                                │
                                ▼
                        mcpguard daemon  ◀── popup (Allow / Block), timeout, audit log
                                ▲
                                │
 claude.ai ─▶ Anthropic cloud ─HTTPS─▶ tunnel ─▶ mcpguard gateway ─▶ remote MCP server
```

* **Claude Desktop** starts each local MCP server in `claude_desktop_config.json` as a subprocess and talks JSON-RPC over stdin/stdout. `mcpguard install` changes each entry so Claude starts `mcpguard wrap -- <original command>`. The wrapper is a transparent proxy that holds `tools/call` requests until you decide.
* **Claude Web.** claude.ai calls custom connectors **from Anthropic's cloud**, not from your browser. A local proxy or browser extension therefore can't see those calls, let alone block them. `mcpguard gateway` is a reverse proxy placed in front of the MCP server. You expose it with a tunnel and register the tunnel URL as the connector. Remote connectors added in Claude Desktop take the same cloud path, so the gateway covers them too.
* **`mcpguard daemon`** owns the popup, the decision timeout and the audit log. Interceptors reach it over a private Unix socket. It starts automatically the first time it's needed.

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) covers the request lifecycle, concurrency model, failure handling, security measures and the alternatives I rejected.

## Features

- **Popup for every call.** It stays on top of other windows, including full-screen apps, and shows the source app, client, MCP server, tool, arguments (formatted JSON) and the server's own hints (destructive, read-only, reaches external systems). It also shows a countdown and how many requests are waiting.
- **Held until you decide.** If allowed, the request is forwarded unchanged. If blocked, the server never sees it, and Claude receives a clear `isError` result it can explain to you. A JSON-RPC error is available as an option.
- **Concurrent requests.** Each request gets its own popup, deadline and connection, and you can answer them in any order. Other traffic keeps flowing while you decide.
- **Reliability.**
  - A request with no answer is blocked when its timeout expires.
  - If Claude cancels a request or disconnects, its popup closes.
  - If the real server crashes, Claude gets an error for each pending request.
  - If the daemon goes away, requests are blocked (it fails closed). Daemon shutdown also blocks all pending requests.
- **Security.**
  - Messages are parsed strictly and re-serialised before forwarding, so the server receives exactly what was approved.
  - The local socket is private (mode 0600). The public gateway URL contains a long secret path, and requests carrying a browser `Origin` header are rejected.
  - Allow is disabled for 700 ms after a popup appears, so a stray click or keypress can't approve it.
  - Characters that can hide or reverse text (bidi, zero-width) are shown as visible escapes.
  - Secrets are masked in the audit log.
- **Protocol coverage.** Tested with the official MCP SDK client against MCP revisions from 2024-11-05 through the stateless 2026-07-28 protocol, over stdio and Streamable HTTP, including JSON-RPC batches.
- **Easy to undo.** The installer backs up the config and writes it atomically; `mcpguard uninstall` restores it exactly.

## Requirements

- Python **3.10+** with Tk. The python.org installers include Tk. On Homebrew Python, run `brew install python-tk`. Without Tk, macOS falls back to a native dialog.
- Claude Desktop, for the Desktop path.
- For the Web path: a Claude plan that allows custom connectors (see Settings → Connectors) and a tunnel such as [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) (`brew install cloudflared`) or ngrok.
- Developed and tested on macOS. Linux should work. The Windows code path is implemented but untested.

## Installation

```bash
git clone <this repo> walled && cd walled
uv sync                      # or: python3 -m venv .venv && .venv/bin/pip install -e .
source .venv/bin/activate
mcpguard --help
```

On macOS this also installs `pyobjc-framework-Cocoa`, which lets the popup appear on top of full-screen apps.

## Try the popup first (no Claude needed)

```bash
mcpguard daemon                    # terminal 1: the approval service (a background app with no Dock icon)
mcpguard test-popup --count 3      # terminal 2: three simultaneous fake requests
```

Answer the three popups in any order. The terminal shows each decision and `mcpguard audit` lists them.

## Protecting Claude Desktop

```bash
mcpguard install --dry-run         # preview the changes
mcpguard install --with-demo       # wrap all local servers and add the bundled demo server
mcpguard status                    # daemon state + which servers are protected
```

Quit Claude Desktop completely (**Cmd+Q**) and reopen it. Then ask, for example: *"Use mcpguard-demo to write a note called test with the text hello."* The popup appears; Allow or Block.

- To wrap only some servers: `mcpguard install --servers filesystem,github`.
- Server logs, including the wrapper's own lines, are in `~/Library/Logs/Claude/mcp-server-<name>.log`.
- To undo: `mcpguard uninstall`, then restart Claude Desktop.
- Optional, to start the daemon at login: `mcpguard autostart enable` (macOS LaunchAgent). Without it, the first intercepted call starts the daemon anyway.

## Protecting Claude Web (custom connectors)

```bash
# 1. the MCP server to protect (here, the bundled demo server over HTTP)
mcpguard demo-server --transport http --port 9000

# 2. the gateway in front of it (prints the secret endpoint path)
mcpguard gateway --upstream http://127.0.0.1:9000/mcp --name notes-remote --port 8080

# 3. a public HTTPS URL for the gateway
cloudflared tunnel --url http://127.0.0.1:8080
```

On claude.ai, open **Settings → Connectors → Add custom connector** and enter `https://<tunnel-host>/<secret-token>/mcp`. Enable the connector in a chat and ask Claude to use it. Each tool call triggers the same popup, labelled *Claude Web*.

- The secret path segment is created once and stored in `~/.mcpguard/gateway-token`. Rotate it with `--rotate-token`.
- `--public-url https://<tunnel-host>` makes the gateway print the full connector URL.
- If the upstream needs credentials, add them with `--upstream-header "Authorization: Bearer …"`.
- Keep `--decision-timeout` (default 60 s) under the tunnel's response timeout. Cloudflare closes idle responses after about 100 s.
- When finished, remove the connector and stop the tunnel.

## Configuration

Settings are read from `~/.mcpguard/config.json`, then from `MCPGUARD_<NAME>` environment variables, then from command-line flags (later ones win):

| Key | Default | Meaning |
|---|---|---|
| `decision_timeout` | `120` | Seconds before an unanswered request is blocked |
| `ui` | `auto` | `auto` (Tk, else macOS dialog), `tk`, `osascript`, `auto-allow` or `auto-block` (headless/testing) |
| `fail_mode` | `closed` | What interceptors do if the daemon is unreachable: `closed` blocks, `open` allows |
| `denial_style` | `tool_error` | `tool_error` returns an `isError` result Claude can explain; `jsonrpc_error` returns a JSON-RPC error with code `-32010` |
| `intercept_methods` | `["tools/call"]` | Methods that need approval, e.g. add `resources/read` or `prompts/get` |
| `autostart_daemon` | `true` | Interceptors start the daemon if it isn't running |
| `audit` | `true` | Write decisions to `~/.mcpguard/audit.jsonl` |
| `redact_keys` | password, token, api_key… | Argument keys masked in the audit log |
| `allow_arm_delay_ms` | `700` | How long Allow stays disabled after a popup appears |

Example: `{"decision_timeout": 60, "intercept_methods": ["tools/call", "resources/read"]}`.

Other environment variables:
- `MCPGUARD_HOME` sets the state directory (default `~/.mcpguard`).
- `MCPGUARD_LOG_LEVEL` sets log verbosity. Logs go to stderr only; stdout carries the MCP traffic.

## Commands

| Command | Purpose |
|---|---|
| `mcpguard daemon [--ui …] [--timeout N]` | Run the approval service in the foreground |
| `mcpguard wrap --name N -- <cmd…>` | Stdio proxy; normally only Claude Desktop runs this, via its config |
| `mcpguard gateway --upstream URL […]` | HTTP gateway for remote MCP servers |
| `mcpguard install / uninstall [--servers a,b] [--dry-run]` | Edit `claude_desktop_config.json` |
| `mcpguard status` | Daemon, config and settings overview |
| `mcpguard test-popup [--count N]` | Send fake requests to try the popup |
| `mcpguard audit [-n 20] [--json]` | Recent decisions |
| `mcpguard demo-server [--transport stdio\|http]` | Harmless demo MCP server |
| `mcpguard autostart enable\|disable\|status` | Start the daemon at login (macOS) |

## Tests

```bash
uv run pytest            # 40 tests, about 10 s, no Claude account needed
```

The suite runs the real wrapper and gateway as they are used in production, against the real demo server, with the approval broker in-process and a scripted UI. It covers:
- allow and block;
- concurrent requests answered out of order (20 at the broker, 5 through the wrapper, 4 through the gateway);
- timeouts, cancellation and client disconnects;
- fail-closed behaviour;
- server crash and missing binary;
- malformed input and batches;
- the 2026-07-28 `resultType` requirement;
- Origin and path checks;
- installer round trips;
- the official MCP SDK client in both protocol eras.

## Project layout

```
src/mcpguard/
  cli.py              command-line entry point
  stdio_proxy.py      Claude Desktop interception (stdio)
  http_gateway.py     Claude Web interception (Streamable HTTP reverse proxy)
  interceptor.py      shared policy: what needs approval, request context, denials
  broker/             daemon ⇄ interceptor IPC (protocol, server, client)
  daemon.py           single-instance background service
  ui/                 popup backends: Tk (default), macOS dialog, automatic (tests)
  audit.py            JSONL decision log with redaction
  desktop_config.py   install/uninstall into claude_desktop_config.json
  autostart.py        macOS LaunchAgent
  demo_server.py      demo MCP server (stdio/HTTP)
tests/                end-to-end and unit tests
docs/                 architecture and demo script
```

## Limitations

- Desktop Extensions installed through Claude's UI (`.mcpb`) aren't listed in `claude_desktop_config.json`. To protect one, add the same server as a config entry.
- Anthropic-hosted connectors run entirely in Anthropic's cloud. Only servers whose traffic goes through the gateway can be gated.
- On the web path, very long waits can hit tunnel or claude.ai timeouts. Keep the gateway timeout moderate.

More detail is in [docs/ARCHITECTURE.md § Known limitations](docs/ARCHITECTURE.md#9-known-limitations).
