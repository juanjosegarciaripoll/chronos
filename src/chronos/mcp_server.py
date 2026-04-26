"""MCP server over the chronos local index.

Exposes six tools backed by the same `IndexRepository` and
`MirrorRepository` the CLI and TUI use:

- `list_calendars` — distinct (account, calendar) pairs known locally.
- `query_range(start, end)` — occurrences whose start falls inside
  the half-open ISO-8601 window.
- `search(query, limit?)` — FTS5 full-text search over summary /
  description / location.
- `get_event(account, calendar, uid)` — full VEVENT detail by UID.
- `get_todo(account, calendar, uid)` — full VTODO detail by UID.
- `import_ics(account, calendar, ics, on_conflict?)` — ingest a raw
  RFC 5545 payload into a calendar.  Additive only; no delete path.

No destructive tools are present (see `ai/AGENTS.md` §7.6`): an
over-eager LLM can add data but cannot delete events or calendars.

Transports
----------
stdio (default)
    ``chronos mcp``
    Use with Claude Desktop or any local MCP client.

Streamable HTTP
    ``chronos mcp --port 8765``
    Use in Docker or remote deployments.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from mcp.server.fastmcp import FastMCP

from chronos.domain import (
    CalendarRef,
    ComponentKind,
    ComponentRef,
    Occurrence,
    StoredComponent,
    VEvent,
    VTodo,
)
from chronos.protocols import IndexRepository, MirrorRepository

SERVER_NAME = "chronos"


def build_mcp_server(*, index: IndexRepository, mirror: MirrorRepository) -> FastMCP:
    """Build a `FastMCP` instance with all chronos tools registered.

    The returned server captures `index` and `mirror` in closures;
    callers control their lifecycles.
    """
    mcp: FastMCP = FastMCP(SERVER_NAME)

    @mcp.tool()
    def list_calendars() -> str:  # pyright: ignore[reportUnusedFunction]
        """List the (account, calendar) pairs with at least one component in
        the local index. Use these names verbatim in subsequent calls."""
        return json.dumps(_tool_list_calendars(index), indent=2)

    @mcp.tool()
    def query_range(start: str, end: str) -> str:  # pyright: ignore[reportUnusedFunction]
        """Return occurrences (expanded recurrences included) whose start
        falls inside the half-open window [start, end). Both arguments
        must be ISO-8601 datetimes."""
        return json.dumps(_tool_query_range(index, start=start, end=end), indent=2)

    @mcp.tool()
    def search(query: str, limit: int = 50) -> str:  # pyright: ignore[reportUnusedFunction]
        """Full-text search (FTS5) over summary / description / location of
        every event and todo across all calendars."""
        return json.dumps(_tool_search(index, query=query, limit=limit), indent=2)

    @mcp.tool()
    def get_event(account: str, calendar: str, uid: str) -> str:  # pyright: ignore[reportUnusedFunction]
        """Fetch one VEVENT by (account, calendar, uid). Returns JSON null if
        not found or if the UID belongs to a VTODO."""
        return json.dumps(
            _tool_get_component(
                index,
                kind=ComponentKind.VEVENT,
                account=account,
                calendar=calendar,
                uid=uid,
            ),
            indent=2,
        )

    @mcp.tool()
    def get_todo(account: str, calendar: str, uid: str) -> str:  # pyright: ignore[reportUnusedFunction]
        """Fetch one VTODO by (account, calendar, uid). Returns JSON null if
        not found or if the UID belongs to a VEVENT."""
        return json.dumps(
            _tool_get_component(
                index,
                kind=ComponentKind.VTODO,
                account=account,
                calendar=calendar,
                uid=uid,
            ),
            indent=2,
        )

    @mcp.tool()
    def import_ics(  # pyright: ignore[reportUnusedFunction]
        account: str,
        calendar: str,
        ics: str,
        on_conflict: str = "skip",
    ) -> str:
        """Ingest a raw RFC 5545 iCalendar payload into a local calendar.
        Components land with href=NULL so the next chronos sync pushes
        them to the server. Both account and calendar must match a pair
        from list_calendars. on_conflict: skip (default), replace, or rename.
        Additive only — cannot delete events or calendars."""
        return json.dumps(
            _tool_import_ics(
                index,
                mirror,
                account=account,
                calendar=calendar,
                ics=ics,
                on_conflict=on_conflict,
            ),
            indent=2,
        )

    return mcp


def build_server(*, index: IndexRepository, mirror: MirrorRepository) -> Any:
    """Return the underlying low-level MCP Server for in-process testing.

    Tests drive this via `mcp.shared.memory.create_connected_server_and_client_session`.
    Production code should use `run_mcp_server` instead.
    """
    return build_mcp_server(index=index, mirror=mirror)._mcp_server  # pyright: ignore[reportPrivateUsage]


def run_mcp_server(
    *,
    index: IndexRepository,
    mirror: MirrorRepository,
    host: str = "127.0.0.1",
    port: int | None = None,
) -> None:
    """Run the MCP server until the client disconnects.

    Uses stdio when *port* is ``None`` (local / Claude Desktop use).
    Uses Streamable HTTP on *host*:*port* when *port* is given.
    """
    mcp = build_mcp_server(index=index, mirror=mirror)
    if port is not None:
        mcp.settings.host = host
        mcp.settings.port = port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


# Tool implementations --------------------------------------------------------


def _tool_list_calendars(index: IndexRepository) -> list[dict[str, str]]:
    return [
        {"account": ref.account_name, "calendar": ref.calendar_name}
        for ref in index.list_calendars()
    ]


def _tool_query_range(
    index: IndexRepository, *, start: str, end: str
) -> list[dict[str, Any]]:
    window_start = _parse_datetime(start, label="start")
    window_end = _parse_datetime(end, label="end")
    if window_end <= window_start:
        raise ValueError(
            f"query_range: end ({end!r}) must be strictly after start ({start!r})"
        )
    out: list[dict[str, Any]] = []
    for calendar_ref in index.list_calendars():
        for occ in index.query_occurrences(calendar_ref, window_start, window_end):
            out.append(_occurrence_to_dict(calendar_ref, occ, index))
    out.sort(key=lambda row: cast(str, row["start"]))
    return out


def _tool_search(
    index: IndexRepository, *, query: str, limit: int
) -> list[dict[str, Any]]:
    return [_summary_dict(c) for c in index.search(query, limit=limit)]


def _tool_get_component(
    index: IndexRepository,
    *,
    kind: ComponentKind,
    account: str,
    calendar: str,
    uid: str,
) -> dict[str, Any] | None:
    component = index.get_component(
        ComponentRef(
            account_name=account,
            calendar_name=calendar,
            uid=uid,
            recurrence_id=None,
        )
    )
    if component is None:
        return None
    actual_kind = (
        ComponentKind.VEVENT if isinstance(component, VEvent) else ComponentKind.VTODO
    )
    if actual_kind != kind:
        # Asked for a VEVENT but found a VTODO (or vice versa): treat as
        # not-found so an LLM asking for an event doesn't get a todo.
        return None
    return _full_dict(component)


def _tool_import_ics(
    index: IndexRepository,
    mirror: MirrorRepository,
    *,
    account: str,
    calendar: str,
    ics: str,
    on_conflict: str,
) -> dict[str, Any]:
    from chronos.ingest import ingest_ics_bytes

    if on_conflict not in ("skip", "replace", "rename"):
        raise ValueError(
            f"on_conflict must be 'skip', 'replace', or 'rename'; got {on_conflict!r}"
        )

    known = {(ref.account_name, ref.calendar_name) for ref in index.list_calendars()}
    if (account, calendar) not in known:
        pairs = sorted(f"{a}/{c}" for a, c in known)
        raise ValueError(
            f"unknown (account={account!r}, calendar={calendar!r}). "
            f"Known calendars: {pairs}"
        )

    report = ingest_ics_bytes(
        ics.encode("utf-8"),
        target=CalendarRef(account_name=account, calendar_name=calendar),
        mirror=mirror,
        index=index,
        on_conflict=on_conflict,  # type: ignore[arg-type]
    )
    return {
        "imported": report.imported,
        "skipped": report.skipped,
        "replaced": report.replaced,
        "renamed": report.renamed,
        "details": list(report.details),
    }


# Serialisation helpers -------------------------------------------------------


def _summary_dict(component: StoredComponent) -> dict[str, Any]:
    return {
        "account": component.ref.account_name,
        "calendar": component.ref.calendar_name,
        "uid": component.ref.uid,
        "kind": (
            ComponentKind.VEVENT.value
            if isinstance(component, VEvent)
            else ComponentKind.VTODO.value
        ),
        "summary": component.summary,
        "start": _datetime_to_iso(component.dtstart),
        "end": _datetime_to_iso(_end_of(component)),
        "status": component.status,
    }


def _full_dict(component: StoredComponent) -> dict[str, Any]:
    base = _summary_dict(component)
    base["description"] = component.description
    base["location"] = component.location
    base["raw_ics"] = component.raw_ics.decode("utf-8", errors="replace")
    return base


def _occurrence_to_dict(
    calendar: CalendarRef, occurrence: Occurrence, index: IndexRepository
) -> dict[str, Any]:
    component = index.get_component(occurrence.ref)
    summary = component.summary if component is not None else None
    location = component.location if component is not None else None
    kind = ComponentKind.VEVENT
    if isinstance(component, VTodo):
        kind = ComponentKind.VTODO
    return {
        "account": calendar.account_name,
        "calendar": calendar.calendar_name,
        "uid": occurrence.ref.uid,
        "kind": kind.value,
        "summary": summary,
        "location": location,
        "start": _datetime_to_iso(occurrence.start),
        "end": _datetime_to_iso(occurrence.end),
        "is_override": occurrence.is_override,
    }


def _end_of(component: StoredComponent) -> datetime | None:
    if isinstance(component, VEvent):
        return component.dtend
    return component.due


def _datetime_to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _parse_datetime(value: str, *, label: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label}: not an ISO-8601 datetime: {value!r}") from exc


__all__ = [
    "SERVER_NAME",
    "build_mcp_server",
    "build_server",
    "run_mcp_server",
]

# Keep Sequence imported so basedpyright sees all collection types used
# in return annotations of the tool helpers above.
_ = Sequence
