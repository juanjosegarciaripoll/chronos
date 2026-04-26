# Purpose

To refactor the MCP server architecture for Chronos (and the same pattern will apply to Pony). The current implementation uses uvicorn/HTTP, which pulls in too many dependencies. Replace it with a minimal asyncio-based design.

# Architecture

Chronos exposes MCP via two transports, both implemented on top of the standard mcp.server.Server class from the MCP Python SDK:

Long-running TCP transport. When Chronos runs as an interactive application (Textual frontend, or a daemon mode), it listens on 127.0.0.1 on an OS-assigned ephemeral port. The port number, plus a randomly generated auth token, is written to a per-user state file at a platform-appropriate location — use the platformdirs library to pick the right directory on Linux, macOS, and Windows. The file should be readable only by the current user (0600 on POSIX; on Windows, rely on the default per-user directory permissions).
stdio bridge mode (chronos mcp-server --stdio). This subcommand is what external MCP clients (Claude Desktop, editor integrations, etc.) actually launch. Its behavior depends on whether a long-running Chronos is already active:

If a running instance is detected (state file exists, port is reachable, token validates): act as a transparent stdio↔TCP forwarder. Read JSON-RPC from stdin, write to the TCP socket; read from the socket, write to stdout. Two asyncio tasks pumping bytes in each direction. This is essential because Chronos's calendar mirror cannot be opened by two processes concurrently — only the long-running instance owns the data, and the stdio process delegates to it.

If no running instance is detected: start a self-contained MCP server in this process that owns the calendar mirror directly, talking JSON-RPC over stdin/stdout via mcp.server.stdio.stdio_server(). When the client disconnects, exit cleanly and release the mirror.

Detection logic: read the state file; if it doesn't exist, go self-contained. If it exists, attempt a TCP connection and a quick MCP initialize handshake with the token. If that fails (stale file, crashed instance), remove the state file and fall back to self-contained mode. Use a short timeout (~500ms) so the bridge starts fast.

# TCP server implementation

Write a transport adapter that takes an asyncio.StreamReader/StreamWriter pair from asyncio.start_server and produces the two anyio memory object streams that Server.run(read_stream, write_stream, init_options) expects. Frame messages as newline-delimited JSON (one JSONRPCMessage per line). Parse incoming lines with JSONRPCMessage.model_validate_json and serialize outgoing ones with model_dump_json(by_alias=True, exclude_none=True). The first message from the client must be an authentication frame containing the token; reject the connection otherwise. After auth succeeds, hand the streams to Server.run inside an anyio task group alongside the two pump tasks.
Each accepted connection gets its own MCP session against the shared application state. Concurrency control over the calendar mirror happens inside Chronos's domain logic, not at the transport.

# Constraints

Standard library asyncio plus anyio and the mcp package only. No uvicorn, starlette, fastapi, h11, or httpx for the server side. platformdirs is fine for the state file path.
All three modes (TCP server, stdio bridge, stdio self-contained) must work identically on Linux, macOS, and Windows. No Unix-domain-socket code paths.
The Textual app starts serve_tcp as a task on its existing event loop — don't spawn a separate thread or process for it.
The connection port must have a large default value, but it can be configured in config.toml
Clean shutdown: SIGINT/SIGTERM cancels the server task, closes connections, and removes the state file.
Add tests for: framing round-trip, auth rejection, stale-state-file recovery in the bridge, and the bridge's bidirectional forwarding.

Before writing code, sketch the module boundaries and the connection-handling state machine so I can review the design.