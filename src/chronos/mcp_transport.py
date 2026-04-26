"""MCP transport layer: TCP server, stdio bridge, and stdio self-contained mode.

Three async entry points (all running under anyio with asyncio backend):

`serve_tcp(server, *, state)`
    Accepts MCP connections on 127.0.0.1:state.port.  Each client must
    send a JSON auth frame as its first line; connections with the wrong
    token or that time out are closed silently.  Authenticated clients
    get a full MCP session against the shared server instance.

`run_stdio_bridge(state)`
    Connects to the running TCP server and forwards stdin/stdout to it.
    Used by `chronos mcp` when a long-running instance is detected.

`run_stdio_standalone(server)`
    Runs an MCP session directly over stdin/stdout using the standard
    mcp.server.stdio.stdio_server() transport.  Used by `chronos mcp`
    when no running instance is detected.

Message framing: newline-delimited JSON (one SessionMessage per line).
Auth frame: `{"auth": "<token>"}\\n` — first line from the client before
any MCP traffic.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp.server.lowlevel import Server
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage

from chronos.mcp_state import McpServerState

AUTH_TIMEOUT: float = 2.0
CONNECT_TIMEOUT: float = 0.5


async def serve_tcp(
    server: Server[Any, Any],
    *,
    state: McpServerState,
) -> None:
    """Accept MCP connections on 127.0.0.1:state.port until cancelled."""

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

    async with anyio.create_task_group() as tg:
        tg.start_soon(stdin_to_tcp)
        tg.start_soon(tcp_to_stdout)


async def run_stdio_standalone(server: Server[Any, Any]) -> None:
    """Run an MCP session directly over stdin/stdout."""
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


# Connection handler ----------------------------------------------------------


async def _serve_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    server: Server[Any, Any],
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

    in_send: MemoryObjectSendStream[SessionMessage | Exception]
    in_recv: MemoryObjectReceiveStream[SessionMessage | Exception]
    out_send: MemoryObjectSendStream[SessionMessage]
    out_recv: MemoryObjectReceiveStream[SessionMessage]

    in_send, in_recv = anyio.create_memory_object_stream(max_buffer_size=16)
    out_send, out_recv = anyio.create_memory_object_stream(max_buffer_size=16)

    async def tcp_to_in() -> None:
        async with in_send:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = JSONRPCMessage.model_validate_json(line)
                    await in_send.send(SessionMessage(message=msg))
                except Exception as exc:  # noqa: BLE001
                    await in_send.send(exc)

    async def out_to_tcp() -> None:
        async with out_recv:
            async for session_msg in out_recv:
                serialized = (
                    session_msg.message.model_dump_json(
                        by_alias=True, exclude_none=True
                    )
                    + "\n"
                )
                writer.write(serialized.encode())
                await writer.drain()

    async with anyio.create_task_group() as tg:
        tg.start_soon(tcp_to_in)
        tg.start_soon(out_to_tcp)
        await server.run(
            in_recv,
            out_send,
            server.create_initialization_options(),
        )


__all__ = [
    "AUTH_TIMEOUT",
    "CONNECT_TIMEOUT",
    "run_stdio_bridge",
    "run_stdio_standalone",
    "serve_tcp",
]
