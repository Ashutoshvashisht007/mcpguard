#!/bin/sh
# Launch the demo MCP server behind the MCP Guard stdio wrapper — exactly what Claude Desktop
# runs after `mcpguard install --with-demo`. Handy for MCP clients (e.g. the MCP Inspector)
# that take a single command without arguments.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
exec "$PY" -m mcpguard wrap --name mcpguard-demo -- "$PY" -m mcpguard demo-server
