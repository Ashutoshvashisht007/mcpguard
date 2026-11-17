"""Wire format between interceptors and the approval daemon.

One connection per approval. The client writes a single JSON line and the daemon answers with
a single JSON line once the user has decided. **Closing the connection early cancels the
approval** (the daemon dismisses the popup) — this is how client-side cancellation, wrapper
shutdown and HTTP client disconnects propagate without extra messages.

    -> {"v": 1, "type": "approve", "token": "...", "request": {...ApprovalRequest...}}
    <- {"type": "decision", "id": "...", "verdict": "allow"|"block", "reason": "...", "detail": ""}

    -> {"v": 1, "type": "ping", "token": "..."}
    <- {"type": "pong", "pid": 123, "pending": 0, "ui": "tk", "version": "0.1.0"}

    <- {"type": "error", "message": "..."}   (malformed input, bad token, ...)

Transport: a Unix domain socket in ``~/.mcpguard`` (directory 0700, socket 0600) on macOS and
Linux. Windows asyncio has no AF_UNIX support, so there the daemon listens on 127.0.0.1 with a
random port and a random token, both stored in a 0600 file.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from typing import Any

from mcpguard.config import Paths

PROTOCOL_VERSION = 1
#: Generous limit so large tool arguments (e.g. file contents) still fit in one line.
MAX_LINE_BYTES = 32 * 1024 * 1024
USE_UNIX_SOCKET = sys.platform != "win32"


@dataclass(frozen=True)
class Endpoint:
    unix_path: str | None = None
    host: str | None = None
    port: int | None = None
    token: str = ""

    @classmethod
    def discover(cls, paths: Paths) -> Endpoint:
        if USE_UNIX_SOCKET:
            return cls(unix_path=str(paths.socket))
        data = json.loads(paths.tcp_endpoint_file.read_text("utf-8"))
        return cls(host="127.0.0.1", port=int(data["port"]), token=str(data["token"]))

    async def open(self, timeout: float = 3.0) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self.unix_path:
            coro = asyncio.open_unix_connection(self.unix_path, limit=MAX_LINE_BYTES)
        else:
            coro = asyncio.open_connection(self.host, self.port, limit=MAX_LINE_BYTES)
        return await asyncio.wait_for(coro, timeout)


def encode(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8") + b"\n"


def write_private_file(path: os.PathLike[str] | str, content: str) -> None:
    """Write ``content`` to a file only the current user can read."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
