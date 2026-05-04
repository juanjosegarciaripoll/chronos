"""MCP transport layer: TCP server, stdio bridge, and stdio self-contained mode.

Three async entry points (all pure asyncio — no external MCP SDK):

`serve_tcp(server, *, state)`
    Accepts MCP connections on 127.0.0.1:state.port.  Each client must
    send a JSON auth frame as its first line; connections with the wrong
    token or that time out are closed silently.  Authenticated clients
    get a full MCP session against the shared server instance.

`run_stdio_bridge(state)`
    Connects to the running TCP server and forwards stdin/stdout to it.
    Used by `chronos mcp` when a long-running instance is detected.

`run_stdio_standalone(server)`
    Runs an MCP session directly over stdin/stdout.
    Used by `chronos mcp` when no running instance is detected.

Message framing: newline-delimited JSON (one JSON-RPC object per line).
Auth frame: `{"auth": "<token>"}\\n` — first line from the client before
any MCP traffic.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable

from chronos.mcp_server import McpServer
from chronos.mcp_state import McpServerState

AUTH_TIMEOUT: float = 2.0
CONNECT_TIMEOUT: float = 0.5


async def serve_tcp(
    server: McpServer,
    *,
    state: McpServerState,
    on_bound: Callable[[McpServerState], None] | None = None,
) -> None:
    """Accept MCP connections on 127.0.0.1:state.port until cancelled.

    If *on_bound* is provided it is called with the actual `McpServerState`
    (which may have a different port than *state* when port=0 was requested)
    once the socket is bound and ready to accept connections.
    """

    async def _handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await _serve_connection(reader, writer, server, state.token)
        except Exception:  # noqa: BLE001 — individual connection errors don't kill the server
            pass
        finally:
            writer.close()

    tcp_server = await asyncio.start_server(_handle, "127.0.0.1", state.port)
    actual_port: int = tcp_server.sockets[0].getsockname()[1]
    if on_bound is not None:
        on_bound(McpServerState(port=actual_port, token=state.token))
    async with tcp_server:
        await tcp_server.serve_forever()


async def run_stdio_bridge(state: McpServerState) -> None:
    """Forward stdin/stdout to the running TCP server.

    Sends the auth frame first, then pumps bytes in both directions
    until either end closes.
    """
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", state.port),
        timeout=CONNECT_TIMEOUT,
    )
    writer.write(json.dumps({"auth": state.token}).encode() + b"\n")
    await writer.drain()

    loop = asyncio.get_running_loop()

    async def stdin_to_tcp() -> None:
        while True:
            line = await loop.run_in_executor(None, sys.stdin.buffer.readline)
            if not line:
                break
            writer.write(line)
            await writer.drain()
        writer.close()

    async def tcp_to_stdout() -> None:
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()

    tasks = [
        asyncio.create_task(stdin_to_tcp()),
        asyncio.create_task(tcp_to_stdout()),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_stdio_standalone(server: McpServer) -> None:
    """Run an MCP session directly over stdin/stdout."""
    await server._run_stdio()  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# Connection handler
# ---------------------------------------------------------------------------


async def _serve_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    server: McpServer,
    token: str,
) -> None:
    """Authenticate one TCP connection then run an MCP session on it."""
    try:
        raw = await asyncio.wait_for(reader.readline(), timeout=AUTH_TIMEOUT)
        auth = json.loads(raw)
        if auth.get("auth") != token:
            return
    except (TimeoutError, json.JSONDecodeError, AttributeError):
        return

    async def readline() -> bytes:
        return await reader.readline()

    async def writeline(data: bytes) -> None:
        writer.write(data)
        await writer.drain()

    await server._serve(readline, writeline)  # pyright: ignore[reportPrivateUsage]


__all__ = [
    "AUTH_TIMEOUT",
    "CONNECT_TIMEOUT",
    "run_stdio_bridge",
    "run_stdio_standalone",
    "serve_tcp",
]
