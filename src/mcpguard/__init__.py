"""MCP Guard — intercept MCP requests from Claude Desktop / Claude Web and let the user
Allow or Block every one of them from an always-on-top confirmation popup.

Components (see docs/ARCHITECTURE.md):

* ``mcpguard.stdio_proxy``  – wraps local (stdio) MCP servers launched by Claude Desktop.
* ``mcpguard.http_gateway`` – reverse proxy in front of remote (Streamable HTTP) MCP servers
  used as Claude Web / Desktop custom connectors.
* ``mcpguard.daemon``       – background approval service: owns the popup UI, the decision
  timeout and the audit log. Interceptors talk to it over a local socket.
"""

__version__ = "0.1.0"
