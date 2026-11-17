"""Installing/uninstalling the wrapper into claude_desktop_config.json."""

from __future__ import annotations

import json

from mcpguard import desktop_config as dc

ORIGINAL = {
    "globalShortcut": "Ctrl+Space",
    "mcpServers": {
        "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]},
        "github": {"command": "docker", "args": ["run", "-i", "ghcr.io/github/github-mcp-server"], "env": {"GITHUB_TOKEN": "t"}},
        "weird": {"url": "https://example.com/mcp"},
    },
}


def write(tmp_path, data=ORIGINAL):
    path = tmp_path / "claude_desktop_config.json"
    path.write_text(json.dumps(data, indent=2))
    return path


def test_install_wraps_and_uninstall_restores_exactly(tmp_path) -> None:
    path = write(tmp_path)
    report = dc.install(path, python="/py")
    assert sorted(report.changed) == ["filesystem", "github"]
    assert ("weird", "not a local stdio server") in report.skipped
    assert report.backup and report.backup.exists()

    data = json.loads(path.read_text())
    fs = data["mcpServers"]["filesystem"]
    assert fs["command"] == "/py"
    assert fs["args"] == ["-m", "mcpguard", "wrap", "--name", "filesystem", "--", "npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    assert data["mcpServers"]["github"]["env"] == {"GITHUB_TOKEN": "t"}
    assert data["globalShortcut"] == "Ctrl+Space"

    again = dc.install(path, python="/py")
    assert again.changed == [] and not again.written  # idempotent

    dc.uninstall(path)
    assert json.loads(path.read_text()) == ORIGINAL


def test_selected_servers_dry_run_and_demo(tmp_path) -> None:
    path = write(tmp_path)
    report = dc.install(path, python="/py", servers=["github", "nope"], dry_run=True)
    assert report.changed == ["github"] and not report.written
    assert ("nope", "no such server in config") in report.skipped
    assert json.loads(path.read_text()) == ORIGINAL

    dc.install(path, python="/py", servers=["github"], with_demo=True)
    wrapped, plain = dc.wrapped_servers(path)
    assert sorted(wrapped) == ["github", dc.DEMO_SERVER_NAME] and plain == ["filesystem"]

    dc.uninstall(path)
    assert json.loads(path.read_text()) == ORIGINAL  # demo entry removed again


def test_missing_config_is_created(tmp_path) -> None:
    path = tmp_path / "sub" / "claude_desktop_config.json"
    report = dc.install(path, python="/py", with_demo=True)
    assert report.changed == [dc.DEMO_SERVER_NAME] and report.backup is None
    assert dc.is_wrapped(json.loads(path.read_text())["mcpServers"][dc.DEMO_SERVER_NAME])
