# Demo video script (about 4 minutes)

The assignment asks for a video showing approval and blocking. This script covers both paths, plus concurrency and failure handling. Record at 1080p and hide any personal details: the account email in Claude's sidebar and the ngrok hostname.

## 0. Prep (off camera)

```bash
cd walled && source .venv/bin/activate
mcpguard install --with-demo   # wraps your Desktop servers and adds the demo server
mcpguard status
```

Restart Claude Desktop (Cmd+Q, then reopen). For the web part, start these in three terminals:

```bash
mcpguard demo-server --transport http --port 9000
mcpguard gateway --upstream http://127.0.0.1:9000/mcp --name notes-remote
ngrok http 8080
```

Add `https://<ngrok-host>/<token>/mcp` as a custom connector on claude.ai (Settings → Connectors → Add custom connector).

## 1. Introduction (20 s)

Show the README diagram. Say: "Desktop runs MCP servers locally over stdio, so I wrap them. claude.ai calls connectors from Anthropic's cloud, so I put a gateway in front of the server. Both report to one approval daemon."

## 2. Claude Desktop: Allow (40 s)

Prompt: *"Use the mcpguard-demo tools to save a note called groceries with the text 'milk, eggs'."*

* The popup appears over Claude. Point out the source, MCP server, tool `write_note`, the arguments and the countdown.
* Click **Allow**. Claude confirms the note was saved.

## 3. Claude Desktop: Block (40 s)

Prompt: *"Now delete the groceries note."*

* The popup shows the red **destructive** hint. Click **Block**.
* Claude replies that the user blocked the call. Run `mcpguard audit -n 5` to show both decisions.

## 4. Concurrency and timeout (40 s)

Prompt: *"Call slow_task three times in parallel with 2, 3 and 4 seconds."* Or run `mcpguard test-popup --count 3` in a terminal.

* Three cascaded popups appear, each showing "3 requests waiting". Allow the last one, block the middle one, allow the first. Each result goes to the right call.
* Leave one unanswered to show it blocking automatically when its countdown ends.

## 5. Claude Web (60 s)

On claude.ai with the connector enabled, prompt: *"Use notes-remote to add 19 and 23."*

* The same popup appears, this time labelled **Claude Web** and **http**. Click Allow and the answer is 42.
* Prompt: *"Delete the note called groceries using notes-remote."* Click Block. Claude reports the denial.
* Point at the gateway terminal log lines.

## 6. Reliability (30 s)

* Stop the daemon (Ctrl+C in its terminal, or `pkill -f "mcpguard daemon"`). Ask Claude Desktop for another tool call: the daemon starts again automatically.
* With autostart disabled (`MCPGUARD_AUTOSTART_DAEMON=0`), the call is blocked with a `daemon_unavailable` denial, because it fails closed.
* Show `pytest` passing (about 10 s).

## Cleanup

```bash
mcpguard uninstall    # restores claude_desktop_config.json exactly
```

Remove the custom connector on claude.ai and stop ngrok.
