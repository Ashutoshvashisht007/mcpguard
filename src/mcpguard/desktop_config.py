"""Install / uninstall the stdio wrapper into Claude Desktop's ``claude_desktop_config.json``.

A wrapped entry is self-describing, so uninstalling needs no side state:

    "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}
        ⇅
    "filesystem": {"command": "/path/to/python", "args": ["-m", "mcpguard", "wrap", "--name", "filesystem",
                   "--", "npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}

Every write is preceded by a timestamped backup and done atomically (temp file + rename).
Entries without a ``command`` (anything that is not a local stdio server) are left alone.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

WRAP_PREFIX = ["-m", "mcpguard", "wrap"]
DEMO_SERVER_NAME = "mcpguard-demo"


def default_config_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if sys.platform == "win32":  # pragma: no cover
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def is_wrapped(entry: dict[str, Any]) -> bool:
    args = entry.get("args") or []
    return isinstance(args, list) and args[: len(WRAP_PREFIX)] == WRAP_PREFIX and "--" in args


def wrap_entry(name: str, entry: dict[str, Any], python: str) -> dict[str, Any]:
    wrapped = copy.deepcopy(entry)
    wrapped["command"] = python
    wrapped["args"] = [*WRAP_PREFIX, "--name", name, "--", entry["command"], *(entry.get("args") or [])]
    return wrapped


def unwrap_entry(entry: dict[str, Any]) -> dict[str, Any]:
    args = entry["args"]
    original = args[args.index("--") + 1 :]
    if not original:
        raise ValueError("wrapped entry has no original command")
    restored = copy.deepcopy(entry)
    restored["command"] = original[0]
    restored["args"] = original[1:]
    return restored


def demo_entry(python: str) -> dict[str, Any]:
    return {"command": python, "args": ["-m", "mcpguard", "demo-server"]}


@dataclass
class ChangeReport:
    config_path: Path
    changed: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    backup: Path | None = None
    written: bool = False


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text("utf-8")
    data = json.loads(text) if text.strip() else {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return data


def _write_config(path: Path, data: dict[str, Any], report: ChangeReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(f"{path.name}.mcpguard-backup-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
        report.backup = backup
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".claude_desktop_config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        if path.exists():
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    report.written = True


def install(
    config_path: Path | None = None,
    *,
    servers: list[str] | None = None,
    python: str | None = None,
    with_demo: bool = False,
    dry_run: bool = False,
) -> ChangeReport:
    path = config_path or default_config_path()
    python = python or sys.executable
    data = load_config(path)
    entries = data.setdefault("mcpServers", {})
    if not isinstance(entries, dict):
        raise ValueError("mcpServers must be an object")
    report = ChangeReport(path)
    targets = set(servers) if servers else None
    if with_demo:
        if DEMO_SERVER_NAME not in entries:
            entries[DEMO_SERVER_NAME] = demo_entry(python)
        if targets is not None:
            targets.add(DEMO_SERVER_NAME)
    for name, entry in list(entries.items()):
        if targets is not None and name not in targets:
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get("command"), str):
            report.skipped.append((name, "not a local stdio server"))
            continue
        if is_wrapped(entry):
            report.skipped.append((name, "already wrapped"))
            continue
        entries[name] = wrap_entry(name, entry, python)
        report.changed.append(name)
    for name in servers or []:
        if name not in entries:
            report.skipped.append((name, "no such server in config"))
    if report.changed and not dry_run:
        _write_config(path, data, report)
    return report


def uninstall(
    config_path: Path | None = None,
    *,
    servers: list[str] | None = None,
    remove_demo: bool = True,
    dry_run: bool = False,
) -> ChangeReport:
    path = config_path or default_config_path()
    data = load_config(path)
    entries = data.get("mcpServers") or {}
    report = ChangeReport(path)
    for name, entry in list(entries.items()):
        if servers and name not in servers:
            continue
        if not isinstance(entry, dict) or not is_wrapped(entry):
            continue
        restored = unwrap_entry(entry)
        if remove_demo and name == DEMO_SERVER_NAME:
            del entries[name]
        else:
            entries[name] = restored
        report.changed.append(name)
    if report.changed and not dry_run:
        _write_config(path, data, report)
    return report


def wrapped_servers(config_path: Path | None = None) -> tuple[list[str], list[str]]:
    """(wrapped, unwrapped) local server names."""
    data = load_config(config_path or default_config_path())
    wrapped, plain = [], []
    for name, entry in (data.get("mcpServers") or {}).items():
        if isinstance(entry, dict) and isinstance(entry.get("command"), str):
            (wrapped if is_wrapped(entry) else plain).append(name)
    return wrapped, plain
