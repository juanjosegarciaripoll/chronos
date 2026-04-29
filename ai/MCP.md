# MCP.md

Current MCP architecture.

## Modes

- `chronos mcp` in self-contained stdio mode when no running app instance is detected.
- `chronos mcp` in stdio-to-TCP bridge mode when a running TUI instance has published MCP server state.
- TCP server started by the TUI on `127.0.0.1` with a per-session auth token stored in the MCP state file.

## Current tools

- `list_calendars`
- `query_range`
- `search`
- `get_event`
- `get_todo`
- `import_ics`

## Policy

- MCP can query local calendar state.
- MCP can add data through `.ics` import.
- MCP must not expose destructive tools.

## Transport notes

- State file carries TCP port and auth token.
- Bridge mode exists to avoid opening the same local state from competing processes.
- Implementation uses standard `asyncio` plus the MCP SDK; no web server stack is involved.
