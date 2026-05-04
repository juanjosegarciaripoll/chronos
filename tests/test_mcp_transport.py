"""Tests for mcp_state.py and TCP/bridge transport (backed by tinymcp).

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

from tinymcp import AUTH_TIMEOUT, serve_tcp

from chronos.domain import ComponentRef, LocalStatus, VEvent
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

    def _server_and_token(self) -> tuple[object, str]:
        """Return (McpServer, token)."""
        from chronos.mcp_server import build_server

        server = build_server(index=self.index, mirror=self.mirror)
        token = secrets.token_hex(16)
        return server, token

    async def _start_tcp(
        self, server: object, token: str
    ) -> tuple[asyncio.Task[None], int]:
        """Start serve_tcp on an ephemeral port; return (task, actual_port)."""
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


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------


class AuthTest(TcpTestCase):
    async def test_wrong_token_closes_connection(self) -> None:
        server, token = self._server_and_token()
        task, port = await self._start_tcp(server, token)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(json.dumps({"auth": "WRONG_TOKEN"}).encode() + b"\n")
            await writer.drain()
            data = await asyncio.wait_for(reader.read(256), timeout=1.0)
            self.assertEqual(data, b"")
            writer.close()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_no_auth_frame_times_out_and_closes(self) -> None:
        server, token = self._server_and_token()
        task, port = await self._start_tcp(server, token)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            data = await asyncio.wait_for(reader.read(256), timeout=AUTH_TIMEOUT + 1.0)
            self.assertEqual(data, b"")
            writer.close()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_correct_token_keeps_connection_open(self) -> None:
        server, token = self._server_and_token()
        task, port = await self._start_tcp(server, token)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(json.dumps({"auth": token}).encode() + b"\n")
            await writer.drain()
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

        with mock.patch("tinymcp.run_stdio_standalone", _fake_standalone):
            await run_mcp_stdio(index=self.index, mirror=self.mirror)

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

        with mock.patch("tinymcp.run_stdio_standalone", _fake_standalone):
            await run_mcp_stdio(index=self.index, mirror=self.mirror)

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
        server, token = self._server_and_token()
        task, port = await self._start_tcp(server, token)
        write_state(McpServerState(port=port, token=token))

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(json.dumps({"auth": token}).encode() + b"\n")
            await writer.drain()

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
            self.assertEqual(json.loads(line).get("id"), 1)

            notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
            writer.write(json.dumps(notif).encode() + b"\n")
            await writer.drain()

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

    async def test_run_mcp_stdio_bridges_to_running_server(self) -> None:
        """run_mcp_stdio detects the state file and delegates to the bridge."""
        from unittest import mock

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

            with mock.patch("tinymcp.run_stdio_bridge", _capture_bridge):
                await run_mcp_stdio(index=self.index, mirror=self.mirror)

            self.assertEqual(bridged_port, state.port)
            self.assertEqual(bridged_token, state.token)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
