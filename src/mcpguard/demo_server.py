"""A small, harmless MCP server for demos and tests (stdio or Streamable HTTP).

Tools cover the cases worth demonstrating: read-only (``echo``, ``add``, ``list_notes``),
state-changing (``write_note``) and destructive (``delete_note``) calls, plus ``slow_task`` for
concurrency. Notes live in a sandbox directory (``~/.mcpguard/demo-notes``) and titles are
restricted to a safe character set, so nothing outside it can be touched.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from mcpguard.config import Paths

_TITLE = re.compile(r"^[A-Za-z0-9 _-]{1,64}$")

server = MCPServer(
    "mcpguard-demo",
    version="0.1.0",
    instructions="Demo tools for MCP Guard: echo, arithmetic, and a tiny sandboxed notes store.",
)


def _notes_dir() -> Path:
    path = Paths.default().demo_notes_dir
    path.mkdir(parents=True, exist_ok=True)
    return path


def _note_path(title: str) -> Path:
    if not _TITLE.match(title):
        raise ValueError("title may only contain letters, digits, spaces, '-' and '_' (max 64 chars)")
    return _notes_dir() / f"{title.strip()}.txt"


@server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def echo(text: str) -> str:
    """Return the given text unchanged."""
    return text


@server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def add(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b


@server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def get_time() -> str:
    """Current local time on the machine running the server."""
    return time.strftime("%Y-%m-%d %H:%M:%S %Z")


@server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def list_notes() -> list[str]:
    """List the titles of saved notes."""
    return sorted(p.stem for p in _notes_dir().glob("*.txt"))


@server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def read_note(title: str) -> str:
    """Read a saved note."""
    path = _note_path(title)
    if not path.exists():
        raise ValueError(f"no note called {title!r}")
    return path.read_text("utf-8")


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
def write_note(title: str, content: str) -> str:
    """Create or overwrite a note in the demo sandbox."""
    _note_path(title).write_text(content, "utf-8")
    return f"saved note {title!r} ({len(content)} characters)"


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False))
def delete_note(title: str) -> str:
    """Permanently delete a note."""
    path = _note_path(title)
    if not path.exists():
        raise ValueError(f"no note called {title!r}")
    path.unlink()
    return f"deleted note {title!r}"


@server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def slow_task(seconds: float = 3.0) -> str:
    """Pretend to work for a few seconds (max 30) — handy for concurrency demos."""
    seconds = max(0.0, min(float(seconds), 30.0))
    await asyncio.sleep(seconds)
    return f"finished after {seconds:g}s"


def run(transport: str = "stdio", host: str = "127.0.0.1", port: int = 9000) -> None:
    if transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=host, port=port)
