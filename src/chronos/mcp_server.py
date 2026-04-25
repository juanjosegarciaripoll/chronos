"""Read-only MCP server over the chronos local index.

Exposes five tools, all backed by the same `IndexRepository` the CLI
and TUI read from:

- `list_calendars` — distinct (account, calendar) pairs known locally.
- `query_range(start, end)` — occurrences whose start falls inside
  the half-open ISO-8601 window. Pulls from the `occurrences` cache,
  so it sees expanded recurrences just like the TUI's day/week view.
- `search(query, limit?)` — FTS5 full-text search over summary /
  description / location.
- `get_event(account, calendar, uid)` — full VEVENT detail by UID.
- `get_todo(account, calendar, uid)` — full VTODO detail by UID.

There are no write tools by design (see `ai/AGENTS.md` §7.6); MCP
clients can read but not mutate, so an over-eager LLM can't delete
the user's calendar.

The server is split from its transport. `build_server(index=...)`
returns an `mcp.server.lowlevel.Server` that tests can drive via
mcp's in-process transports; `serve_stdio(index=...)` is the thin
async wrapper the CLI's `chronos mcp` command awaits.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from chronos.domain import (
    CalendarRef,
    ComponentKind,
    ComponentRef,
    Occurrence,
    StoredComponent,
    VEvent,
    VTodo,
)
from chronos.protocols import IndexRepository

SERVER_NAME = "chronos"


def build_server(*, index: IndexRepository) -> Server[dict[str, Any], Any]:
    """Construct the MCP `Server` with chronos's read-only tools.

    The returned server holds a reference to `index`; callers control
    the index's lifecycle (the CLI opens/closes a `SqliteIndexRepository`
    around `serve_stdio`).
    """
    server: Server[dict[str, Any], Any] = Server(SERVER_NAME)

    @server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
    async def _list_tools() -> list[types.Tool]:  # pyright: ignore[reportUnusedFunction]
        return _TOOL_DEFINITIONS

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def _call_tool(  # pyright: ignore[reportUnusedFunction]
        name: str, arguments: dict[str, Any]
    ) -> list[types.TextContent]:
        payload: object
        if name == "list_calendars":
            payload = _tool_list_calendars(index)
        elif name == "query_range":
            payload = _tool_query_range(
                index,
                start=_require_str(arguments, "start"),
                end=_require_str(arguments, "end"),
            )
        elif name == "search":
            payload = _tool_search(
                index,
                query=_require_str(arguments, "query"),
                limit=_optional_int(arguments, "limit", 50),
            )
        elif name == "get_event":
            payload = _tool_get_component(
                index,
                kind=ComponentKind.VEVENT,
                account=_require_str(arguments, "account"),
                calendar=_require_str(arguments, "calendar"),
                uid=_require_str(arguments, "uid"),
            )
        elif name == "get_todo":
            payload = _tool_get_component(
                index,
                kind=ComponentKind.VTODO,
                account=_require_str(arguments, "account"),
                calendar=_require_str(arguments, "calendar"),
                uid=_require_str(arguments, "uid"),
            )
        else:
            raise ValueError(f"unknown tool: {name!r}")
        return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]

    return server


async def serve_stdio(*, index: IndexRepository) -> None:
    """Run the MCP server over stdio until the client disconnects.

    Used by the `chronos mcp` CLI command — the wrapper that wires
    stdin/stdout into the MCP transport. `index` is supplied by the
    caller; the server doesn't open or close it.
    """
    server = build_server(index=index)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


# Tool definitions ------------------------------------------------------------

_LIST_CALENDARS_TOOL = types.Tool(
    name="list_calendars",
    description=(
        "List the (account, calendar) pairs that have at least one component "
        "in the local index. Use these names verbatim in subsequent calls."
    ),
    inputSchema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
)

_QUERY_RANGE_TOOL = types.Tool(
    name="query_range",
    description=(
        "Return occurrences (expanded recurrences included) whose start time "
        "falls inside the half-open window `[start, end)`. `start` and `end` "
        "must be ISO-8601 datetimes."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "start": {
                "type": "string",
                "description": "ISO-8601 datetime, inclusive lower bound.",
            },
            "end": {
                "type": "string",
                "description": "ISO-8601 datetime, exclusive upper bound.",
            },
        },
        "required": ["start", "end"],
        "additionalProperties": False,
    },
)

_SEARCH_TOOL = types.Tool(
    name="search",
    description=(
        "Full-text search (FTS5) over summary / description / location of "
        "every event and todo across all calendars."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "FTS5 query string."},
            "limit": {
                "type": "integer",
                "description": "Maximum hits to return (default 50).",
                "minimum": 1,
                "maximum": 500,
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
)

_GET_EVENT_TOOL = types.Tool(
    name="get_event",
    description="Fetch one VEVENT by (account, calendar, uid).",
    inputSchema={
        "type": "object",
        "properties": {
            "account": {"type": "string"},
            "calendar": {"type": "string"},
            "uid": {"type": "string"},
        },
        "required": ["account", "calendar", "uid"],
        "additionalProperties": False,
    },
)

_GET_TODO_TOOL = types.Tool(
    name="get_todo",
    description="Fetch one VTODO by (account, calendar, uid).",
    inputSchema={
        "type": "object",
        "properties": {
            "account": {"type": "string"},
            "calendar": {"type": "string"},
            "uid": {"type": "string"},
        },
        "required": ["account", "calendar", "uid"],
        "additionalProperties": False,
    },
)

_TOOL_DEFINITIONS: list[types.Tool] = [
    _LIST_CALENDARS_TOOL,
    _QUERY_RANGE_TOOL,
    _SEARCH_TOOL,
    _GET_EVENT_TOOL,
    _GET_TODO_TOOL,
]


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
        # Asked for a VEVENT but found a VTODO (or vice versa). Treat
        # as not-found rather than returning the wrong shape.
        return None
    return _full_dict(component)


# Serialisation helpers -------------------------------------------------------


def _summary_dict(component: StoredComponent) -> dict[str, Any]:
    """Compact projection used in `search` results."""
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
    """Expanded projection used in `get_event` / `get_todo` responses."""
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


def _require_str(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing/invalid string argument {key!r}")
    return value


def _optional_int(arguments: dict[str, Any], key: str, default: int) -> int:
    value = arguments.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"argument {key!r} must be an integer")
    return int(value)


__all__ = [
    "SERVER_NAME",
    "build_server",
    "serve_stdio",
]


# Marker so tests can detect the chronos.mcp_server module without
# importing private helpers.
_ = Sequence
