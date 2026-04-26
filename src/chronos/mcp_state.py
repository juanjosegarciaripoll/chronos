"""MCP server state file: port + auth token for the running TCP server.

A long-running chronos instance (TUI, future daemon) writes this file
when it starts its MCP TCP server.  The `chronos mcp` stdio command
reads it to decide whether to bridge to the running instance or run
self-contained.

File format: `{"port": N, "token": "..."}`, written atomically with
mode 0600 on POSIX so the token is only readable by the current user.
On Windows the file is placed in APPDATA which is already user-scoped;
an explicit ACL is not set, matching the pattern used elsewhere in
`paths.py`.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass

from chronos.paths import mcp_server_state_path


@dataclass(frozen=True)
class McpServerState:
    port: int
    token: str


def write_state(state: McpServerState) -> None:
    path = mcp_server_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"port": state.port, "token": state.token}).encode()
    fd, tmp = tempfile.mkstemp(prefix=".tmp-mcp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def read_state() -> McpServerState | None:
    try:
        raw = mcp_server_state_path().read_bytes()
        data = json.loads(raw)
        return McpServerState(port=int(data["port"]), token=str(data["token"]))
    except (FileNotFoundError, KeyError, ValueError, TypeError):
        return None


def remove_state() -> None:
    with contextlib.suppress(FileNotFoundError):
        mcp_server_state_path().unlink()


__all__ = ["McpServerState", "read_state", "remove_state", "write_state"]
