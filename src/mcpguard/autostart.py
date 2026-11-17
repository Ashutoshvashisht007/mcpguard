"""Run the approval daemon at login (macOS LaunchAgent).

The daemon is also started on demand by the first wrapped MCP server, so this is optional; it
just means the popup service is already up when Claude Desktop launches. ``KeepAlive`` with
``SuccessfulExit = false`` restarts it after a crash but not after a deliberate quit.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from mcpguard.config import ENV_HOME, Paths

LABEL = "ai.mcpguard.daemon"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise RuntimeError(
            "autostart is implemented for macOS (launchd). On other systems add "
            f"'{sys.executable} -m mcpguard daemon' to your session's startup programs."
        )


def enable(paths: Paths) -> Path:
    _require_macos()
    paths.ensure()
    plist = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-m", "mcpguard", "daemon"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Interactive",
        "StandardOutPath": str(paths.daemon_log),
        "StandardErrorPath": str(paths.daemon_log),
    }
    if os.environ.get(ENV_HOME):
        plist["EnvironmentVariables"] = {ENV_HOME: str(paths.home)}
    target = plist_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "wb") as fh:
        plistlib.dump(plist, fh)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True, capture_output=True)
    return target


def disable() -> bool:
    _require_macos()
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    target = plist_path()
    if target.exists():
        target.unlink()
        return True
    return False


def is_enabled() -> bool:
    return sys.platform == "darwin" and plist_path().exists()
