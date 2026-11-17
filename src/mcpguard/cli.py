"""Command-line entry point: ``mcpguard <command>`` (or ``python -m mcpguard <command>``)."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from pathlib import Path
from typing import Any

from mcpguard import __version__
from mcpguard.config import Paths, Settings, setup_logging


def _settings(args: argparse.Namespace, **overrides: Any) -> Settings:
    return Settings.load().merged({k: v for k, v in overrides.items() if v is not None})


# ---------------------------------------------------------------------------- commands
def cmd_daemon(args: argparse.Namespace) -> int:
    setup_logging()
    from mcpguard.daemon import run_daemon

    settings = _settings(args, ui=args.ui, decision_timeout=args.timeout)
    return run_daemon(settings)


def cmd_wrap(args: argparse.Namespace) -> int:
    setup_logging()
    from mcpguard.broker.client import BrokerClient
    from mcpguard.interceptor import Interceptor
    from mcpguard.stdio_proxy import StdioProxy

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("usage: mcpguard wrap --name NAME -- <server command> [args...]", file=sys.stderr)
        return 2
    settings = _settings(args)
    interceptor = Interceptor(
        settings, BrokerClient(settings), source=args.source, transport="stdio", server_name=args.name or Path(command[0]).name
    )
    return asyncio.run(StdioProxy(command, interceptor).run())


def cmd_gateway(args: argparse.Namespace) -> int:
    setup_logging()
    from mcpguard.broker.client import BrokerClient
    from mcpguard.http_gateway import HttpGateway, load_or_create_path_token, serve_gateway
    from mcpguard.interceptor import Interceptor

    settings = _settings(args)
    paths = Paths.default()
    token = args.path_token or load_or_create_path_token(paths, rotate=args.rotate_token)
    upstream_headers: dict[str, str] = {}
    for header in args.upstream_header or []:
        name, sep, value = header.partition(":")
        if not sep:
            print(f"bad --upstream-header {header!r}; expected 'Name: value'", file=sys.stderr)
            return 2
        upstream_headers[name.strip()] = value.strip()
    interceptor = Interceptor(
        settings,
        BrokerClient(settings, paths),
        source=args.source,
        transport="http",
        server_name=args.name,
        decision_timeout=args.decision_timeout,
    )
    gateway = HttpGateway(
        upstream=args.upstream,
        interceptor=interceptor,
        path_token=token,
        allowed_origins=args.allow_origin or (),
        upstream_headers=upstream_headers,
    )

    async def main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):  # pragma: no cover - Windows
                pass
        await interceptor.broker.ensure_daemon()
        server = asyncio.ensure_future(serve_gateway(gateway, args.host, args.port, stop))
        await asyncio.sleep(0.3)
        if server.done():
            server.result()  # surface bind errors
        local = f"http://{args.host}:{args.port}{gateway.mcp_path}"
        print(f"\nMCP Guard gateway for '{args.name}' → {args.upstream}", file=sys.stderr)
        print(f"  local endpoint : {local}", file=sys.stderr)
        if args.public_url:
            print(f"  connector URL  : {args.public_url.rstrip('/')}{gateway.mcp_path}", file=sys.stderr)
        else:
            print(f"  expose it with : cloudflared tunnel --url http://{args.host}:{args.port}", file=sys.stderr)
            print(f"  connector URL  : https://<tunnel-host>{gateway.mcp_path}", file=sys.stderr)
        print("  (keep the path secret — it is the only thing protecting the public URL)\n", file=sys.stderr, flush=True)
        await server

    try:
        asyncio.run(main())
    except OSError as exc:
        print(f"mcpguard gateway: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_demo_server(args: argparse.Namespace) -> int:
    from mcpguard.demo_server import run

    run("http" if args.transport == "http" else "stdio", args.host, args.port)
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    from mcpguard import desktop_config

    report = desktop_config.install(
        Path(args.config) if args.config else None,
        servers=_split(args.servers),
        python=args.python,
        with_demo=args.with_demo,
        dry_run=args.dry_run,
    )
    _print_report(report, "wrapped", args.dry_run)
    if report.written:
        print("\nQuit Claude Desktop completely (Cmd+Q) and reopen it to apply.")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    from mcpguard import desktop_config

    report = desktop_config.uninstall(
        Path(args.config) if args.config else None,
        servers=_split(args.servers),
        remove_demo=not args.keep_demo,
        dry_run=args.dry_run,
    )
    _print_report(report, "restored", args.dry_run)
    if report.written:
        print("\nQuit Claude Desktop completely (Cmd+Q) and reopen it to apply.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from mcpguard import autostart, desktop_config
    from mcpguard.broker.client import BrokerClient

    paths = Paths.default()
    settings = Settings.load(paths)
    pong = asyncio.run(BrokerClient(settings, paths).ping())
    print(f"home          : {paths.home}")
    if pong:
        print(f"daemon        : running (pid {pong.get('pid')}, popup={pong.get('ui')}, pending={pong.get('pending')})")
    else:
        print("daemon        : not running (starts automatically on the first intercepted call)")
    print(f"autostart     : {'enabled' if autostart.is_enabled() else 'disabled'}")
    cfg = Path(args.config) if args.config else desktop_config.default_config_path()
    try:
        wrapped, plain = desktop_config.wrapped_servers(cfg)
        print(f"desktop config: {cfg}")
        print(f"  protected   : {', '.join(wrapped) or '-'}")
        print(f"  unprotected : {', '.join(plain) or '-'}")
    except (OSError, ValueError) as exc:
        print(f"desktop config: unreadable ({exc})")
    print(f"settings      : timeout={settings.decision_timeout:g}s fail_mode={settings.fail_mode} "
          f"denial_style={settings.denial_style} intercept={','.join(settings.intercept_methods)}")
    return 0


def cmd_test_popup(args: argparse.Namespace) -> int:
    """Fire fake approval requests at the daemon — checks the popup without involving Claude."""
    setup_logging("WARNING")
    from mcpguard.broker.client import BrokerClient
    from mcpguard.models import ApprovalRequest

    settings = _settings(args)
    client = BrokerClient(settings)
    samples = [
        ("write_note", {"title": "shopping", "content": "milk, eggs, coffee"}, {"destructiveHint": False}),
        ("delete_note", {"title": "shopping"}, {"destructiveHint": True}),
        ("echo", {"text": "hello from MCP Guard"}, {"readOnlyHint": True}),
    ]

    async def one(i: int) -> str:
        tool, arguments, annotations = samples[i % len(samples)]
        request = ApprovalRequest(
            source="Test",
            transport="test",
            server_name="mcpguard-demo",
            method="tools/call",
            jsonrpc_id=i,
            tool_name=tool,
            arguments=arguments,
            client_name="mcpguard test-popup",
            tool_description=f"Sample request #{i + 1} generated by `mcpguard test-popup`.",
            tool_annotations=annotations,
            timeout=args.timeout,
        )
        decision = await client.request_decision(request)
        return f"  #{i + 1} {tool:<12} → {decision.verdict.value.upper()} ({decision.reason.value})"

    async def main() -> list[str]:
        return await asyncio.gather(*(one(i) for i in range(args.count)))

    print(f"sent {args.count} request(s); answer the popup(s)…", flush=True)
    for line in asyncio.run(main()):
        print(line)
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    from mcpguard.audit import tail

    entries = tail(Paths.default().audit_log, args.n)
    if args.json:
        for entry in entries:
            print(json.dumps(entry, ensure_ascii=False))
        return 0
    if not entries:
        print("no decisions recorded yet")
    for e in entries:
        target = e.get("tool") or e.get("method")
        print(f"{e.get('ts')}  {e.get('verdict', '?').upper():<5} {e.get('reason', ''):<18} "
              f"{e.get('source')}: {e.get('server')} → {target}  {e.get('arguments', '')[:80]}")
    return 0


def cmd_autostart(args: argparse.Namespace) -> int:
    from mcpguard import autostart

    try:
        if args.action == "enable":
            print(f"enabled: {autostart.enable(Paths.default())}")
        elif args.action == "disable":
            print("disabled" if autostart.disable() else "was not enabled")
        else:
            print("enabled" if autostart.is_enabled() else "disabled")
    except (RuntimeError, OSError) as exc:
        print(f"mcpguard autostart: {exc}", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------- helpers
def _split(value: str | None) -> list[str] | None:
    return [v.strip() for v in value.split(",") if v.strip()] if value else None


def _print_report(report: Any, verb: str, dry_run: bool) -> None:
    prefix = "[dry run] would have " if dry_run else ""
    print(f"config: {report.config_path}")
    print(f"{prefix}{verb}: {', '.join(report.changed) or 'nothing'}")
    for name, why in report.skipped:
        print(f"skipped {name}: {why}")
    if report.backup:
        print(f"backup: {report.backup}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcpguard",
        description="Approve or block every MCP request from Claude Desktop and Claude Web.",
    )
    parser.add_argument("--version", action="version", version=f"mcpguard {__version__}")
    sub = parser.add_subparsers(dest="command_name", required=True)

    p = sub.add_parser("daemon", help="run the approval service (popup + audit log) in the foreground")
    p.add_argument("--ui", choices=["auto", "tk", "osascript", "auto-allow", "auto-block"])
    p.add_argument("--timeout", type=float, help="seconds before an unanswered request is blocked")
    p.set_defaults(func=cmd_daemon)

    p = sub.add_parser("wrap", help="stdio proxy for one MCP server (used inside claude_desktop_config.json)")
    p.add_argument("--name", help="server name shown in the popup")
    p.add_argument("--source", default="Claude Desktop", help="label for the requesting app")
    p.add_argument("command", nargs=argparse.REMAINDER, help="-- <server command> [args...]")
    p.set_defaults(func=cmd_wrap)

    p = sub.add_parser("gateway", help="HTTP gateway in front of a remote MCP server (Claude Web connectors)")
    p.add_argument("--upstream", required=True, help="upstream Streamable HTTP MCP URL, e.g. http://127.0.0.1:9000/mcp")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--name", default="remote-mcp", help="server name shown in the popup")
    p.add_argument("--source", default="Claude Web", help="label for the requesting app")
    p.add_argument("--decision-timeout", type=float, default=60.0,
                   help="seconds to wait for a decision (keep below your tunnel's response timeout, ~100s on Cloudflare)")
    p.add_argument("--path-token", help="secret path segment (default: generated once and stored in ~/.mcpguard)")
    p.add_argument("--rotate-token", action="store_true", help="generate a new secret path segment")
    p.add_argument("--public-url", help="public base URL of your tunnel, to print the full connector URL")
    p.add_argument("--upstream-header", action="append", help="extra header for upstream requests, 'Name: value'")
    p.add_argument("--allow-origin", action="append", help="browser Origin allowed to call the gateway")
    p.set_defaults(func=cmd_gateway)

    p = sub.add_parser("demo-server", help="run the bundled harmless demo MCP server")
    p.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9000)
    p.set_defaults(func=cmd_demo_server)

    p = sub.add_parser("install", help="wrap the local MCP servers in Claude Desktop's config")
    p.add_argument("--config", help="path to claude_desktop_config.json")
    p.add_argument("--servers", help="comma-separated server names (default: all)")
    p.add_argument("--with-demo", action="store_true", help="also add the bundled demo server")
    p.add_argument("--python", help="interpreter Claude should launch (default: this one)")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("uninstall", help="restore the original Claude Desktop server entries")
    p.add_argument("--config", help="path to claude_desktop_config.json")
    p.add_argument("--servers", help="comma-separated server names (default: all)")
    p.add_argument("--keep-demo", action="store_true", help="keep the demo server entry (unwrapped)")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_uninstall)

    p = sub.add_parser("status", help="show daemon, config and settings status")
    p.add_argument("--config", help="path to claude_desktop_config.json")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("test-popup", help="send fake requests to the daemon to try the popup")
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--timeout", type=float, default=60.0)
    p.set_defaults(func=cmd_test_popup)

    p = sub.add_parser("audit", help="show recent decisions")
    p.add_argument("-n", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("autostart", help="start the daemon at login (macOS)")
    p.add_argument("action", choices=["enable", "disable", "status"])
    p.set_defaults(func=cmd_autostart)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ValueError as exc:  # bad settings / config
        print(f"mcpguard: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
