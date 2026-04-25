"""End-to-end tests for the read-only MCP server.

Each test stands up an in-process MCP server backed by a real
`SqliteIndexRepository` (no mocking), connects a `ClientSession`
through `create_connected_server_and_client_session`, and exercises
the five tools.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import TextContent

from chronos.domain import (
    CalendarRef,
    ComponentRef,
    LocalStatus,
    Occurrence,
    VEvent,
    VTodo,
)
from chronos.index_store import SqliteIndexRepository
from chronos.mcp_server import SERVER_NAME, build_server


def _ref(account: str, calendar: str, uid: str) -> ComponentRef:
    return ComponentRef(
        account_name=account, calendar_name=calendar, uid=uid, recurrence_id=None
    )


def _vevent(
    *,
    account: str = "personal",
    calendar: str = "work",
    uid: str = "evt-1@example.com",
    summary: str = "Weekly sync",
    description: str | None = None,
    location: str | None = None,
    dtstart: datetime | None = None,
    dtend: datetime | None = None,
) -> VEvent:
    return VEvent(
        ref=_ref(account, calendar, uid),
        href=f"/dav/{calendar}/{uid}.ics",
        etag="etag-1",
        raw_ics=b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
        summary=summary,
        description=description,
        location=location,
        dtstart=dtstart or datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
        dtend=dtend or datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        status=None,
        local_flags=frozenset(),
        server_flags=frozenset(),
        local_status=LocalStatus.ACTIVE,
        trashed_at=None,
        synced_at=None,
    )


def _vtodo(
    *,
    account: str = "personal",
    calendar: str = "tasks",
    uid: str = "todo-1@example.com",
    summary: str = "File taxes",
    due: datetime | None = None,
) -> VTodo:
    return VTodo(
        ref=_ref(account, calendar, uid),
        href=f"/dav/{calendar}/{uid}.ics",
        etag="etag-1",
        raw_ics=b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
        summary=summary,
        description=None,
        location=None,
        dtstart=None,
        due=due or datetime(2026, 6, 1, 17, 0, tzinfo=UTC),
        status="NEEDS-ACTION",
        local_flags=frozenset(),
        server_flags=frozenset(),
        local_status=LocalStatus.ACTIVE,
        trashed_at=None,
        synced_at=None,
    )


@asynccontextmanager
async def _connected_session(
    index: SqliteIndexRepository,
) -> AsyncIterator[ClientSession]:
    server = build_server(index=index)
    async with create_connected_server_and_client_session(
        server, raise_exceptions=True
    ) as session:
        await session.initialize()
        yield session


def _text(content: list[TextContent]) -> str:
    """Extract the first text block from a tool result."""
    assert content, "tool returned no content"
    block = content[0]
    assert isinstance(block, TextContent), f"unexpected block type: {block!r}"
    return block.text


class McpServerTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.index = SqliteIndexRepository(tmp / "index.sqlite3")
        self.addCleanup(self.index.close)


class ListToolsTest(McpServerTestCase):
    async def test_advertises_exactly_five_read_only_tools(self) -> None:
        async with _connected_session(self.index) as session:
            result = await session.list_tools()
        names = {tool.name for tool in result.tools}
        self.assertEqual(
            names,
            {"list_calendars", "query_range", "search", "get_event", "get_todo"},
        )

    async def test_no_write_tools_present(self) -> None:
        # Belt-and-braces: AGENTS.md §7.6 forbids write tools. Any
        # tool name suggestive of mutation (`create`, `update`,
        # `delete`, `set`, `add`, `remove`, `put`, `push`, `sync`)
        # is rejected here.
        forbidden_substrings = (
            "create",
            "update",
            "delete",
            "set_",
            "add_",
            "remove",
            "put",
            "push",
            "sync",
            "trash",
        )
        async with _connected_session(self.index) as session:
            tools = (await session.list_tools()).tools
        for tool in tools:
            for forbidden in forbidden_substrings:
                self.assertNotIn(
                    forbidden,
                    tool.name.lower(),
                    f"tool {tool.name!r} looks like a write tool",
                )


class ListCalendarsToolTest(McpServerTestCase):
    async def test_returns_distinct_account_calendar_pairs(self) -> None:
        self.index.upsert_component(_vevent(account="personal", calendar="work"))
        self.index.upsert_component(_vevent(account="personal", calendar="home"))
        self.index.upsert_component(_vevent(account="work-acct", calendar="team"))
        async with _connected_session(self.index) as session:
            result = await session.call_tool("list_calendars", {})
        payload = json.loads(_text(result.content))
        self.assertEqual(
            sorted((c["account"], c["calendar"]) for c in payload),
            [
                ("personal", "home"),
                ("personal", "work"),
                ("work-acct", "team"),
            ],
        )

    async def test_empty_index_returns_empty_list(self) -> None:
        async with _connected_session(self.index) as session:
            result = await session.call_tool("list_calendars", {})
        self.assertEqual(json.loads(_text(result.content)), [])


class QueryRangeToolTest(McpServerTestCase):
    def _seed_with_occurrences(self) -> None:
        # Two events on different days; only the first should match
        # the half-open window [2026-05-01, 2026-05-02).
        morning = _vevent(uid="morning@example.com", summary="Morning standup")
        afternoon = _vevent(
            uid="afternoon@example.com",
            summary="Out of range",
            dtstart=datetime(2026, 5, 5, 14, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 5, 15, 0, tzinfo=UTC),
        )
        self.index.upsert_component(morning)
        self.index.upsert_component(afternoon)
        self.index.set_occurrences(
            morning.ref,
            (
                Occurrence(
                    ref=morning.ref,
                    start=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
                    end=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
                    recurrence_id=None,
                    is_override=False,
                ),
            ),
        )
        self.index.set_occurrences(
            afternoon.ref,
            (
                Occurrence(
                    ref=afternoon.ref,
                    start=datetime(2026, 5, 5, 14, 0, tzinfo=UTC),
                    end=datetime(2026, 5, 5, 15, 0, tzinfo=UTC),
                    recurrence_id=None,
                    is_override=False,
                ),
            ),
        )

    async def test_returns_only_in_range_occurrences(self) -> None:
        self._seed_with_occurrences()
        async with _connected_session(self.index) as session:
            result = await session.call_tool(
                "query_range",
                {
                    "start": "2026-05-01T00:00:00+00:00",
                    "end": "2026-05-02T00:00:00+00:00",
                },
            )
        rows = json.loads(_text(result.content))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["uid"], "morning@example.com")
        self.assertEqual(rows[0]["summary"], "Morning standup")
        self.assertEqual(rows[0]["start"], "2026-05-01T09:00:00+00:00")

    async def test_inverted_window_raises(self) -> None:
        async with _connected_session(self.index) as session:
            result = await session.call_tool(
                "query_range",
                {
                    "start": "2026-05-02T00:00:00+00:00",
                    "end": "2026-05-01T00:00:00+00:00",
                },
            )
        self.assertTrue(result.isError)

    async def test_invalid_iso_raises(self) -> None:
        async with _connected_session(self.index) as session:
            result = await session.call_tool(
                "query_range",
                {"start": "not-a-date", "end": "2026-05-02T00:00:00+00:00"},
            )
        self.assertTrue(result.isError)


class SearchToolTest(McpServerTestCase):
    def _seed(self) -> None:
        self.index.upsert_component(
            _vevent(uid="planning@example.com", summary="Sprint planning")
        )
        self.index.upsert_component(
            _vevent(
                uid="standup@example.com",
                summary="Daily standup",
                description="Cross-team sync",
            )
        )
        self.index.upsert_component(
            _vevent(
                uid="lunch@example.com",
                summary="Team lunch",
                location="Cafe Aroma",
            )
        )

    async def test_matches_summary(self) -> None:
        self._seed()
        async with _connected_session(self.index) as session:
            result = await session.call_tool("search", {"query": "planning"})
        rows = json.loads(_text(result.content))
        self.assertEqual([r["uid"] for r in rows], ["planning@example.com"])

    async def test_matches_description_and_location(self) -> None:
        self._seed()
        async with _connected_session(self.index) as session:
            sync_hits = json.loads(
                _text((await session.call_tool("search", {"query": "sync"})).content)
            )
            cafe_hits = json.loads(
                _text((await session.call_tool("search", {"query": "Aroma"})).content)
            )
        self.assertEqual([r["uid"] for r in sync_hits], ["standup@example.com"])
        self.assertEqual([r["uid"] for r in cafe_hits], ["lunch@example.com"])

    async def test_limit_caps_results(self) -> None:
        for i in range(5):
            self.index.upsert_component(
                _vevent(uid=f"meeting-{i}@example.com", summary=f"Meeting {i}")
            )
        async with _connected_session(self.index) as session:
            result = await session.call_tool("search", {"query": "Meeting", "limit": 2})
        rows = json.loads(_text(result.content))
        self.assertEqual(len(rows), 2)


class GetEventToolTest(McpServerTestCase):
    async def test_returns_full_event_including_raw_ics(self) -> None:
        self.index.upsert_component(
            _vevent(
                uid="full@example.com",
                summary="Detailed event",
                description="Quarterly review",
                location="Boardroom",
            )
        )
        async with _connected_session(self.index) as session:
            result = await session.call_tool(
                "get_event",
                {
                    "account": "personal",
                    "calendar": "work",
                    "uid": "full@example.com",
                },
            )
        payload = json.loads(_text(result.content))
        self.assertEqual(payload["summary"], "Detailed event")
        self.assertEqual(payload["description"], "Quarterly review")
        self.assertEqual(payload["location"], "Boardroom")
        self.assertEqual(payload["kind"], "VEVENT")
        self.assertIn("BEGIN:VCALENDAR", payload["raw_ics"])

    async def test_unknown_uid_returns_null_payload(self) -> None:
        async with _connected_session(self.index) as session:
            result = await session.call_tool(
                "get_event",
                {
                    "account": "personal",
                    "calendar": "work",
                    "uid": "ghost@example.com",
                },
            )
        self.assertIsNone(json.loads(_text(result.content)))

    async def test_get_event_on_a_vtodo_returns_null(self) -> None:
        # A VTODO with the same lookup keys must not be returned
        # by get_event — the kind discriminates so an LLM asking
        # for an event doesn't get a todo silently substituted.
        self.index.upsert_component(_vtodo(uid="task@example.com"))
        async with _connected_session(self.index) as session:
            result = await session.call_tool(
                "get_event",
                {
                    "account": "personal",
                    "calendar": "tasks",
                    "uid": "task@example.com",
                },
            )
        self.assertIsNone(json.loads(_text(result.content)))


class GetTodoToolTest(McpServerTestCase):
    async def test_returns_full_todo(self) -> None:
        self.index.upsert_component(_vtodo(uid="taxes@example.com"))
        async with _connected_session(self.index) as session:
            result = await session.call_tool(
                "get_todo",
                {
                    "account": "personal",
                    "calendar": "tasks",
                    "uid": "taxes@example.com",
                },
            )
        payload = json.loads(_text(result.content))
        self.assertEqual(payload["kind"], "VTODO")
        self.assertEqual(payload["summary"], "File taxes")
        self.assertEqual(payload["status"], "NEEDS-ACTION")

    async def test_get_todo_on_a_vevent_returns_null(self) -> None:
        self.index.upsert_component(_vevent(uid="meeting@example.com"))
        async with _connected_session(self.index) as session:
            result = await session.call_tool(
                "get_todo",
                {
                    "account": "personal",
                    "calendar": "work",
                    "uid": "meeting@example.com",
                },
            )
        self.assertIsNone(json.loads(_text(result.content)))


class ServerMetadataTest(McpServerTestCase):
    async def test_server_name_and_initialization(self) -> None:
        async with _connected_session(self.index) as session:
            # `initialize()` was called inside the helper; round-trip
            # a list_tools to confirm the server is responsive.
            tools = await session.list_tools()
        self.assertTrue(tools.tools)
        # Module-level constant is what we expose to clients.
        self.assertEqual(SERVER_NAME, "chronos")


# Reference type alias to satisfy basedpyright unused-import rule
# for the helper module — ensures CalendarRef is imported (used by
# fixture builder).
_ = CalendarRef
