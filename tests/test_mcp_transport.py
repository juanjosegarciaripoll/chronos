"""Tests for mcp_state.py, start_tcp_server, and run_mcp_stdio.

TCP auth, bridge forwarding, and bridge-or-standalone logic are tested in
tinymcp/tests/test_server.py.  This file covers Chronos-specific behaviour:
state file format, start_tcp_server (writes state, exposes auth MCP), and
run_mcp_stdio (detects state file and routes correctly).
"""

from __future__ import annotations

import asyncio
import json
import secrets
import tempfile
import unittest
from pathlib import Path

from tinymcp import serve_tcp

from chronos.index_store import SqliteIndexRepository
from chronos.mcp_state import McpServerState, read_state, remove_state, write_state
from chronos.storage import VdirMirrorRepository

# ---------------------------------------------------------------------------
# State file tests
# ---------------------------------------------------------------------------


class McpStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = self.enterContext(tempfile.TemporaryDirectory())
        import chronos.mcp_state as _mod
        import chronos.paths as _paths

        self._orig_path = _paths.mcp_server_state_path

        def _patched() -> Path:
            return Path(self._tmp) / "mcp_server.json"

        _paths.mcp_server_state_path = _patched  # type: ignore[assignment]
        _mod.mcp_server_state_path = _patched  # type: ignore[assignment]

    def tearDown(self) -> None:
        import chronos.mcp_state as _mod
        import chronos.paths as _paths

        _paths.mcp_server_state_path = self._orig_path  # type: ignore[assignment]
        _mod.mcp_server_state_path = self._orig_path  # type: ignore[assignment]

    def test_write_and_read_round_trip(self) -> None:
        state = McpServerState(port=12345, token="abc123")
        write_state(state)
        loaded = read_state()
        self.assertEqual(loaded, state)

    def test_read_returns_none_when_absent(self) -> None:
        self.assertIsNone(read_state())

    def test_read_returns_none_on_malformed_json(self) -> None:
        path = Path(self._tmp) / "mcp_server.json"
        path.write_bytes(b"not json {{{")
        self.assertIsNone(read_state())

    def test_remove_is_silent_when_absent(self) -> None:
        remove_state()  # must not raise

    def test_remove_deletes_file(self) -> None:
        write_state(McpServerState(port=1, token="t"))
        remove_state()
        self.assertIsNone(read_state())

    def test_written_file_has_correct_content(self) -> None:
        state = McpServerState(port=9876, token="secret")
        write_state(state)
        raw = json.loads(Path(self._tmp).joinpath("mcp_server.json").read_bytes())
        self.assertEqual(raw["port"], 9876)
        self.assertEqual(raw["token"], "secret")


# ---------------------------------------------------------------------------
# start_tcp_server + run_mcp_stdio integration tests
# ---------------------------------------------------------------------------


class TcpTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.index = SqliteIndexRepository(tmp / "index.sqlite3")
        self.mirror = VdirMirrorRepository(tmp / "mirror")
        self.addCleanup(self.index.close)

    def _server_and_token(self) -> tuple[object, str]:
        from chronos.mcp_server import build_server

        return build_server(index=self.index, mirror=self.mirror), secrets.token_hex(16)

    async def _start_tcp(
        self, server: object, token: str
    ) -> tuple[asyncio.Task[None], int]:
        port_holder: list[int] = []

        def on_bound(p: int) -> None:
            port_holder.append(p)

        task = asyncio.create_task(
            serve_tcp(server, port=0, token=token, on_bound=on_bound)  # type: ignore[arg-type]
        )
        for _ in range(40):
            if port_holder:
                break
            await asyncio.sleep(0.025)
        self.assertTrue(port_holder, "server did not bind in time")
        return task, port_holder[0]


class StartTcpServerTest(TcpTestCase):
    """start_tcp_server writes the state file; run_mcp_stdio routes via it."""

    def setUp(self) -> None:
        super().setUp()
        tmp_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        import chronos.mcp_state as _mod
        import chronos.paths as _paths

        self._orig_path = _paths.mcp_server_state_path

        def _patched() -> Path:
            return tmp_dir / "mcp_server.json"

        _paths.mcp_server_state_path = _patched  # type: ignore[assignment]
        _mod.mcp_server_state_path = _patched  # type: ignore[assignment]

    def tearDown(self) -> None:
        import chronos.mcp_state as _mod
        import chronos.paths as _paths

        _paths.mcp_server_state_path = self._orig_path  # type: ignore[assignment]
        _mod.mcp_server_state_path = self._orig_path  # type: ignore[assignment]
        super().tearDown()

    async def _wait_for_state(self) -> McpServerState:
        for _ in range(40):
            state = read_state()
            if state is not None:
                return state
            await asyncio.sleep(0.05)
        self.fail("state file not written within 2 s")

    async def test_server_writes_state_file_and_accepts_connections(self) -> None:
        from chronos.mcp_server import start_tcp_server

        task = asyncio.create_task(
            start_tcp_server(index=self.index, mirror=self.mirror)
        )
        try:
            state = await self._wait_for_state()
            self.assertGreater(state.port, 0)
            self.assertTrue(state.token)

            reader, writer = await asyncio.open_connection("127.0.0.1", state.port)
            writer.write(json.dumps({"auth": state.token}).encode() + b"\n")
            await writer.drain()
            init = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "state-test", "version": "0"},
                },
            }
            writer.write(json.dumps(init).encode() + b"\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            resp = json.loads(line)
            self.assertEqual(resp.get("id"), 1)
            self.assertIn("result", resp)
            writer.close()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertIsNone(read_state())

    async def test_run_mcp_stdio_without_state_goes_standalone(self) -> None:
        """No state file → run_mcp_stdio uses standalone mode directly."""
        from unittest.mock import AsyncMock, patch

        from chronos.mcp_server import run_mcp_stdio

        standalone = AsyncMock()
        with patch("tinymcp.transport.run_stdio_standalone", standalone):
            await run_mcp_stdio(index=self.index, mirror=self.mirror)
        standalone.assert_awaited_once()

    async def test_run_mcp_stdio_bridges_to_running_server(self) -> None:
        """run_mcp_stdio detects the state file and delegates to the bridge."""
        from unittest.mock import patch

        from chronos.mcp_server import run_mcp_stdio, start_tcp_server

        task = asyncio.create_task(
            start_tcp_server(index=self.index, mirror=self.mirror)
        )
        try:
            state = await self._wait_for_state()

            bridged_port: int | None = None
            bridged_token: str | None = None

            async def _capture_bridge(*, host: str, port: int, token: str) -> None:  # noqa: ARG001
                nonlocal bridged_port, bridged_token
                bridged_port = port
                bridged_token = token

            with patch("tinymcp.transport.run_stdio_bridge", _capture_bridge):
                await run_mcp_stdio(index=self.index, mirror=self.mirror)

            self.assertEqual(bridged_port, state.port)
            self.assertEqual(bridged_token, state.token)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
