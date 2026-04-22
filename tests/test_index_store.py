from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from chronos.domain import (
    CalendarRef,
    ComponentRef,
    LocalStatus,
    SyncState,
    VEvent,
    VTodo,
)
from chronos.index_store import SqliteIndexRepository


def _ref(uid: str, recurrence_id: str | None = None) -> ComponentRef:
    return ComponentRef(
        account_name="personal",
        calendar_name="work",
        uid=uid,
        recurrence_id=recurrence_id,
    )


def _event(
    ref: ComponentRef,
    *,
    href: str | None = None,
    etag: str | None = None,
    summary: str | None = "Weekly sync",
    description: str | None = None,
    location: str | None = None,
    raw_ics: bytes = b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
    local_status: LocalStatus = LocalStatus.ACTIVE,
    local_flags: frozenset[str] = frozenset(),
) -> VEvent:
    return VEvent(
        ref=ref,
        href=href,
        etag=etag,
        raw_ics=raw_ics,
        summary=summary,
        description=description,
        location=location,
        dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
        dtend=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        status=None,
        local_flags=local_flags,
        server_flags=frozenset(),
        local_status=local_status,
        trashed_at=None,
        synced_at=None,
    )


def _todo(ref: ComponentRef, *, due: datetime | None = None) -> VTodo:
    return VTodo(
        ref=ref,
        href="/dav/todo.ics",
        etag="etag-1",
        raw_ics=b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
        summary="File tax",
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


class IndexRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = SqliteIndexRepository(tmp / "index.sqlite3")
        self.addCleanup(self.repo.close)

    def test_upsert_then_get_event(self) -> None:
        ref = _ref("e1@example.com")
        event = _event(ref, href="/dav/e1.ics", etag="v1")
        self.repo.upsert_component(event)
        fetched = self.repo.get_component(ref)
        self.assertIsNotNone(fetched)
        assert isinstance(fetched, VEvent)
        self.assertEqual(fetched.ref, ref)
        self.assertEqual(fetched.summary, "Weekly sync")
        self.assertEqual(fetched.dtstart, event.dtstart)

    def test_upsert_is_idempotent(self) -> None:
        ref = _ref("e1@example.com")
        event = _event(ref, href="/dav/e1.ics")
        self.repo.upsert_component(event)
        self.repo.upsert_component(event)
        components = self.repo.list_calendar_components(CalendarRef("personal", "work"))
        self.assertEqual(len(components), 1)

    def test_upsert_updates_on_conflict(self) -> None:
        ref = _ref("e1@example.com")
        first = _event(ref, href="/dav/e1.ics", etag="v1", summary="Original")
        second = _event(ref, href="/dav/e1.ics", etag="v2", summary="Revised")
        self.repo.upsert_component(first)
        self.repo.upsert_component(second)
        fetched = self.repo.get_component(ref)
        assert isinstance(fetched, VEvent)
        self.assertEqual(fetched.etag, "v2")
        self.assertEqual(fetched.summary, "Revised")

    def test_master_and_override_are_distinct_rows(self) -> None:
        master = _event(_ref("series@example.com"), href="/dav/s.ics", etag="v1")
        override = _event(
            _ref("series@example.com", recurrence_id="2026-05-08T09:00:00+00:00"),
            href="/dav/s.ics",
            etag="v1",
            summary="Rescheduled",
        )
        self.repo.upsert_component(master)
        self.repo.upsert_component(override)
        rows = self.repo.list_calendar_components(CalendarRef("personal", "work"))
        self.assertEqual(len(rows), 2)

    def test_delete_component(self) -> None:
        ref = _ref("gone@example.com")
        self.repo.upsert_component(_event(ref, href="/dav/gone.ics"))
        self.repo.delete_component(ref)
        self.assertIsNone(self.repo.get_component(ref))

    def test_list_pending_pushes_returns_href_null_rows(self) -> None:
        pending = _event(_ref("pending@example.com"))  # href is None
        synced = _event(_ref("synced@example.com"), href="/dav/s.ics", etag="v1")
        self.repo.upsert_component(pending)
        self.repo.upsert_component(synced)
        pushes = self.repo.list_pending_pushes(CalendarRef("personal", "work"))
        uids = {c.ref.uid for c in pushes}
        self.assertEqual(uids, {"pending@example.com"})

    def test_local_flags_round_trip(self) -> None:
        ref = _ref("flagged@example.com")
        flagged = _event(
            ref,
            href="/dav/f.ics",
            etag="v1",
            local_flags=frozenset({"starred", "important"}),
        )
        self.repo.upsert_component(flagged)
        fetched = self.repo.get_component(ref)
        assert fetched is not None
        self.assertEqual(fetched.local_flags, frozenset({"starred", "important"}))

    def test_vtodo_round_trip_preserves_due_not_dtend(self) -> None:
        ref = _ref("t1@example.com")
        todo = _todo(ref)
        self.repo.upsert_component(todo)
        fetched = self.repo.get_component(ref)
        self.assertIsInstance(fetched, VTodo)
        assert isinstance(fetched, VTodo)
        self.assertEqual(fetched.due, todo.due)

    def test_sync_state_round_trip(self) -> None:
        calendar = CalendarRef("personal", "work")
        state = SyncState(
            calendar=calendar,
            ctag="ctag-1",
            sync_token="https://example.com/tok/1",
            synced_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
        )
        self.assertIsNone(self.repo.get_sync_state(calendar))
        self.repo.set_sync_state(state)
        fetched = self.repo.get_sync_state(calendar)
        self.assertEqual(fetched, state)

    def test_sync_state_overwrites_on_upsert(self) -> None:
        calendar = CalendarRef("personal", "work")
        self.repo.set_sync_state(
            SyncState(calendar=calendar, ctag="a", sync_token=None, synced_at=None)
        )
        self.repo.set_sync_state(
            SyncState(calendar=calendar, ctag="b", sync_token="t", synced_at=None)
        )
        fetched = self.repo.get_sync_state(calendar)
        assert fetched is not None
        self.assertEqual(fetched.ctag, "b")
        self.assertEqual(fetched.sync_token, "t")


class FtsSearchTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = SqliteIndexRepository(tmp / "index.sqlite3")
        self.addCleanup(self.repo.close)
        self.repo.upsert_component(
            _event(
                _ref("standup@example.com"),
                href="/dav/s.ics",
                etag="v1",
                summary="Daily standup",
                description="Team sync every morning",
                location="Office",
            )
        )
        self.repo.upsert_component(
            _event(
                _ref("review@example.com"),
                href="/dav/r.ics",
                etag="v2",
                summary="Quarterly review",
                description="Planning session",
                location="Board room",
            )
        )

    def test_search_matches_summary(self) -> None:
        hits = self.repo.search("standup")
        self.assertEqual([h.ref.uid for h in hits], ["standup@example.com"])

    def test_search_matches_description(self) -> None:
        hits = self.repo.search("planning")
        self.assertEqual([h.ref.uid for h in hits], ["review@example.com"])

    def test_search_matches_location(self) -> None:
        hits = self.repo.search("office")
        self.assertEqual([h.ref.uid for h in hits], ["standup@example.com"])

    def test_search_empty_query_returns_nothing(self) -> None:
        self.assertEqual(self.repo.search(""), ())

    def test_search_updated_content_reflected(self) -> None:
        ref = _ref("standup@example.com")
        updated = _event(
            ref,
            href="/dav/s.ics",
            etag="v2",
            summary="Daily gathering",
            description="Team sync every morning",
            location="Office",
        )
        self.repo.upsert_component(updated)
        hits = self.repo.search("standup")
        self.assertEqual(hits, ())
        hits = self.repo.search("gathering")
        self.assertEqual([h.ref.uid for h in hits], ["standup@example.com"])

    def test_search_respects_calendar_filter(self) -> None:
        other = _ref("standup2@example.com")
        other_ref = ComponentRef(
            account_name=other.account_name,
            calendar_name="personal",
            uid=other.uid,
        )
        self.repo.upsert_component(
            _event(
                other_ref,
                href="/dav/other.ics",
                etag="v1",
                summary="Daily standup",
            )
        )
        personal_hits = self.repo.search(
            "standup", calendar=CalendarRef("personal", "personal")
        )
        self.assertEqual(len(personal_hits), 1)
        self.assertEqual(personal_hits[0].ref.calendar_name, "personal")


class ConnectionContextManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = SqliteIndexRepository(tmp / "index.sqlite3")
        self.addCleanup(self.repo.close)

    def test_batched_writes_are_one_transaction(self) -> None:
        with self.repo.connection():
            self.repo.upsert_component(
                _event(_ref("a@example.com"), href="/dav/a.ics", etag="v1")
            )
            self.repo.upsert_component(
                _event(_ref("b@example.com"), href="/dav/b.ics", etag="v1")
            )
        rows = self.repo.list_calendar_components(CalendarRef("personal", "work"))
        self.assertEqual(len(rows), 2)

    def test_rollback_on_exception_reverts_writes(self) -> None:
        with (
            self.assertRaises(RuntimeError),
            self.repo.connection(),
        ):
            self.repo.upsert_component(
                _event(_ref("a@example.com"), href="/dav/a.ics", etag="v1")
            )
            raise RuntimeError("boom")
        rows = self.repo.list_calendar_components(CalendarRef("personal", "work"))
        self.assertEqual(rows, ())
