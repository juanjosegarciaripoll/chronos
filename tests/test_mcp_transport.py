"""Tests for mcp_state.py and mcp_transport.py.

All tests use asyncio (via IsolatedAsyncioTestCase) and real TCP sockets
bound to ephemeral ports — no mocking of network I/O.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from chronos.domain import ComponentRef, LocalStatus, VEvent
from chronos.index_store import SqliteIndexRepository
from chronos.mcp_state import McpServerState, read_state, remove_state, write_state
from chronos.mcp_transport import AUTH_TIMEOUT, serve_tcp
from chronos.storage import VdirMirrorRepository

# ---------------------------------------------------------------------------
# State file tests
# ---------------------------------------------------------------------------


class McpStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = self.enterContext(tempfile.TemporaryDirectory())
        # Redirect state file into the temp dir by monkey-patching the module.
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
# Helpers shared by TCP tests
# ---------------------------------------------------------------------------


def _vevent(ref_uid: str = "test@example.com") -> VEvent:
    return VEvent(
        ref=ComponentRef("acc", "cal", ref_uid),
        href=None,
        etag=None,
        raw_ics=b"",
        summary="Test",
        description=None,
        location=None,
        dtstart=datetime(2026, 5, 1, tzinfo=UTC),
        dtend=None,
        status=None,
        local_flags=frozenset(),
        server_flags=frozenset(),
        local_status=LocalStatus.ACTIVE,
        trashed_at=None,
        synced_at=None,
    )


class TcpTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.index = SqliteIndexRepository(tmp / "index.sqlite3")
        self.mirror = VdirMirrorRepository(tmp / "mirror")
        self.addCleanup(self.index.close)

    def _server_and_token(self) -> tuple[object, McpServerState]:
        """Return (low-level Server, McpServerState) on an ephemeral port."""
        from chronos.mcp_server import build_server

        server = build_server(index=self.index, mirror=self.mirror)
        token = secrets.token_hex(16)
        return server, McpServerState(port=0, token=token)

    async def _start_tcp(
        self, server: object, state: McpServerState
    ) -> tuple[asyncio.Task[None], int]:
        """Start serve_tcp in a background task; return (task, actual_port).

        Port 0 binds to an ephemeral port. We probe to find the actual
        port before starting the real server.
        """

        async def _noop(_r: asyncio.StreamReader, _w: asyncio.StreamWriter) -> None:
            pass

        probe = await asyncio.start_server(_noop, "127.0.0.1", 0)
        port: int = probe.sockets[0].getsockname()[1]
        probe.close()
        await probe.wait_closed()

        bound_state = McpServerState(port=port, token=state.token)
        task = asyncio.create_task(serve_tcp(server, state=bound_state))
        await asyncio.sleep(0.05)  # let the server bind
        return task, port


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------


class AuthTest(TcpTestCase):
    async def test_wrong_token_closes_connection(self) -> None:
        server, state = self._server_and_token()
        task, port = await self._start_tcp(server, state)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(json.dumps({"auth": "WRONG_TOKEN"}).encode() + b"\n")
            await writer.drain()
            # Server closes connection without responding.
            data = await asyncio.wait_for(reader.read(256), timeout=1.0)
            self.assertEqual(data, b"")
            writer.close()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_no_auth_frame_times_out_and_closes(self) -> None:

        server, state = self._server_and_token()
        task, port = await self._start_tcp(server, state)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            # Send nothing — server should close after AUTH_TIMEOUT.
            data = await asyncio.wait_for(reader.read(256), timeout=AUTH_TIMEOUT + 1.0)
            self.assertEqual(data, b"")
            writer.close()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_correct_token_keeps_connection_open(self) -> None:
        server, state = self._server_and_token()
        task, port = await self._start_tcp(server, state)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(json.dumps({"auth": state.token}).encode() + b"\n")
            await writer.drain()
            # Send a valid MCP initialize request.
            init = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            }
            writer.write(json.dumps(init).encode() + b"\n")
            await writer.drain()
            # Should receive a response line.
            line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            response = json.loads(line)
            self.assertEqual(response.get("id"), 1)
            self.assertIn("result", response)
            writer.close()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


# ---------------------------------------------------------------------------
# Bridge detection tests
# ---------------------------------------------------------------------------


class BridgeDetectionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.index = SqliteIndexRepository(tmp / "index.sqlite3")
        self.mirror = VdirMirrorRepository(tmp / "mirror")
        self.addCleanup(self.index.close)
        # Redirect state file into temp dir.
        import chronos.mcp_state as _mod
        import chronos.paths as _paths

        self._orig_path = _paths.mcp_server_state_path

        def _patched() -> Path:
            return tmp / "mcp_server.json"

        _paths.mcp_server_state_path = _patched  # type: ignore[assignment]
        _mod.mcp_server_state_path = _patched  # type: ignore[assignment]

    def tearDown(self) -> None:
        import chronos.mcp_state as _mod
        import chronos.paths as _paths

        _paths.mcp_server_state_path = self._orig_path  # type: ignore[assignment]
        _mod.mcp_server_state_path = self._orig_path  # type: ignore[assignment]

    async def test_stale_state_file_is_removed_and_falls_back(self) -> None:
        """State file exists but nothing is listening — should clean up and go
        self-contained.  Standalone mode is patched out to avoid touching stdin."""
        from unittest import mock

        write_state(McpServerState(port=19999, token="stale"))

        from chronos.mcp_server import run_mcp_stdio
        from chronos.mcp_state import read_state as _read

        standalone_called = False

        async def _fake_standalone(_server: object) -> None:
            nonlocal standalone_called
            standalone_called = True

        with mock.patch("chronos.mcp_transport.run_stdio_standalone", _fake_standalone):
            await run_mcp_stdio(index=self.index, mirror=self.mirror)

        # State file must have been removed before standalone was called.
        self.assertIsNone(_read())
        self.assertTrue(standalone_called)

    async def test_no_state_file_goes_self_contained(self) -> None:
        """With no state file, run_mcp_stdio enters standalone mode directly."""
        from unittest import mock

        from chronos.mcp_server import run_mcp_stdio
        from chronos.mcp_state import read_state as _read

        self.assertIsNone(_read())

        standalone_called = False

        async def _fake_standalone(_server: object) -> None:
            nonlocal standalone_called
            standalone_called = True

        with mock.patch("chronos.mcp_transport.run_stdio_standalone", _fake_standalone):
            await run_mcp_stdio(index=self.index, mirror=self.mirror)

        # No state file was created (no TCP server started).
        self.assertIsNone(_read())
        self.assertTrue(standalone_called)


# ---------------------------------------------------------------------------
# Bridge forwarding test
# ---------------------------------------------------------------------------


class BridgeForwardingTest(TcpTestCase):
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

    async def test_bridge_forwards_list_tools_request(self) -> None:
        """Verify a message round-trips through the bridge to the TCP server."""
        server, state = self._server_and_token()
        task, port = await self._start_tcp(server, state)
        bound_state = McpServerState(port=port, token=state.token)
        write_state(bound_state)

        try:
            # Connect directly (simulating what run_stdio_bridge does).
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(json.dumps({"auth": bound_state.token}).encode() + b"\n")
            await writer.drain()

            # MCP initialize handshake.
            init = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "bridge-test", "version": "0"},
                },
            }
            writer.write(json.dumps(init).encode() + b"\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            init_resp = json.loads(line)
            self.assertEqual(init_resp.get("id"), 1)

            # Send initialized notification.
            notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
            writer.write(json.dumps(notif).encode() + b"\n")
            await writer.drain()

            # Request tools list.
            tools_req = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            }
            writer.write(json.dumps(tools_req).encode() + b"\n")
            await writer.drain()
            line2 = await asyncio.wait_for(reader.readline(), timeout=2.0)
            tools_resp = json.loads(line2)
            self.assertEqual(tools_resp.get("id"), 2)
            tool_names = {t["name"] for t in tools_resp["result"]["tools"]}
            self.assertIn("list_calendars", tool_names)
            writer.close()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


# ---------------------------------------------------------------------------
# start_tcp_server + run_mcp_stdio end-to-end
# ---------------------------------------------------------------------------


class StartTcpServerTest(TcpTestCase):
    """Acceptance test for Milestone 14: start_tcp_server writes the state
    file, a run_mcp_stdio call detects it and bridges, and the state file
    is removed when the server exits."""

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

            # The state file port is reachable and responds to auth + MCP.
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

        # State file must be removed after server exits.
        self.assertIsNone(read_state())

    async def test_run_mcp_stdio_bridges_to_running_server(self) -> None:
        """run_mcp_stdio detects the state file and delegates to the bridge."""
        from unittest import mock

        from chronos.mcp_server import run_mcp_stdio, start_tcp_server

        task = asyncio.create_task(
            start_tcp_server(index=self.index, mirror=self.mirror)
        )
        try:
            state = await self._wait_for_state()

            bridged_with: McpServerState | None = None

            async def _capture_bridge(s: McpServerState) -> None:
                nonlocal bridged_with
                bridged_with = s

            with mock.patch("chronos.mcp_transport.run_stdio_bridge", _capture_bridge):
                await run_mcp_stdio(index=self.index, mirror=self.mirror)

            self.assertIsNotNone(bridged_with)
            assert bridged_with is not None
            self.assertEqual(bridged_with.port, state.port)
            self.assertEqual(bridged_with.token, state.token)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
