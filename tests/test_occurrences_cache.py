from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from chronos.domain import CalendarRef, ComponentRef, LocalStatus, VEvent
from chronos.index_store import SqliteIndexRepository
from chronos.recurrence import populate_occurrences
from chronos.storage import VdirMirrorRepository
from chronos.storage_indexing import index_calendar
from tests import corpus

ACCOUNT = "personal"
CALENDAR = "work"
CAL = CalendarRef(ACCOUNT, CALENDAR)


def _ref(uid: str, recurrence_id: str | None = None) -> ComponentRef:
    return ComponentRef(
        account_name=ACCOUNT,
        calendar_name=CALENDAR,
        uid=uid,
        recurrence_id=recurrence_id,
    )


def _event(
    uid: str,
    raw_ics: bytes,
    *,
    dtstart: datetime,
    dtend: datetime,
    href: str | None = "/dav/x.ics",
    etag: str | None = "v1",
    recurrence_id: str | None = None,
    summary: str | None = None,
) -> VEvent:
    return VEvent(
        ref=_ref(uid, recurrence_id),
        href=href,
        etag=etag,
        raw_ics=raw_ics,
        summary=summary,
        description=None,
        location=None,
        dtstart=dtstart,
        dtend=dtend,
        status=None,
        local_flags=frozenset(),
        server_flags=frozenset(),
        local_status=LocalStatus.ACTIVE,
        trashed_at=None,
        synced_at=None,
    )


class SetAndQueryOccurrencesTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.mirror = VdirMirrorRepository(tmp / "mirror")
        self.index = SqliteIndexRepository(tmp / "index.sqlite3")
        self.addCleanup(self.index.close)

    def _seed_weekly(self) -> None:
        from chronos.domain import ResourceRef

        self.mirror.write(
            ResourceRef(ACCOUNT, CALENDAR, "weekly-1@example.com"),
            corpus.recurring_weekly(),
        )
        index_calendar(mirror=self.mirror, index=self.index, calendar=CAL)

    def test_populate_writes_occurrences_into_window(self) -> None:
        self._seed_weekly()
        written = populate_occurrences(
            index=self.index,
            calendar=CAL,
            window_start=datetime(2026, 5, 1, tzinfo=UTC),
            window_end=datetime(2026, 6, 1, tzinfo=UTC),
        )
        self.assertEqual(written, 5)
        occs = self.index.query_occurrences(
            CAL,
            datetime(2026, 5, 1, tzinfo=UTC),
            datetime(2026, 6, 1, tzinfo=UTC),
        )
        self.assertEqual(len(occs), 5)
        for occ in occs:
            self.assertEqual(occ.ref.uid, "weekly-1@example.com")
            self.assertFalse(occ.is_override)

    def test_query_respects_window_bounds(self) -> None:
        self._seed_weekly()
        populate_occurrences(
            index=self.index,
            calendar=CAL,
            window_start=datetime(2026, 5, 1, tzinfo=UTC),
            window_end=datetime(2026, 6, 1, tzinfo=UTC),
        )
        narrow = self.index.query_occurrences(
            CAL,
            datetime(2026, 5, 1, tzinfo=UTC),
            datetime(2026, 5, 9, tzinfo=UTC),
        )
        self.assertEqual(len(narrow), 2)  # May 1 and May 8

    def test_repopulate_replaces_previous_rows(self) -> None:
        self._seed_weekly()
        populate_occurrences(
            index=self.index,
            calendar=CAL,
            window_start=datetime(2026, 5, 1, tzinfo=UTC),
            window_end=datetime(2026, 6, 1, tzinfo=UTC),
        )
        first = self.index.query_occurrences(
            CAL,
            datetime(2026, 5, 1, tzinfo=UTC),
            datetime(2026, 6, 1, tzinfo=UTC),
        )
        populate_occurrences(
            index=self.index,
            calendar=CAL,
            window_start=datetime(2026, 6, 1, tzinfo=UTC),
            window_end=datetime(2026, 7, 1, tzinfo=UTC),
        )
        after = self.index.query_occurrences(
            CAL,
            datetime(2026, 5, 1, tzinfo=UTC),
            datetime(2026, 6, 1, tzinfo=UTC),
        )
        self.assertEqual(after, ())
        june = self.index.query_occurrences(
            CAL,
            datetime(2026, 6, 1, tzinfo=UTC),
            datetime(2026, 7, 1, tzinfo=UTC),
        )
        self.assertGreater(len(june), 0)
        self.assertNotEqual(first, june)


class InvalidationOnWriteTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.index = SqliteIndexRepository(tmp / "index.sqlite3")
        self.addCleanup(self.index.close)
        self.master = _event(
            "weekly-1@example.com",
            corpus.recurring_weekly(),
            dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        )
        self.index.upsert_component(self.master)
        populate_occurrences(
            index=self.index,
            calendar=CAL,
            window_start=datetime(2026, 5, 1, tzinfo=UTC),
            window_end=datetime(2026, 6, 1, tzinfo=UTC),
        )

    def _count(self) -> int:
        return len(
            self.index.query_occurrences(
                CAL,
                datetime(2026, 5, 1, tzinfo=UTC),
                datetime(2026, 6, 1, tzinfo=UTC),
            )
        )

    def test_upserting_master_clears_occurrences(self) -> None:
        self.assertGreater(self._count(), 0)
        self.index.upsert_component(self.master)
        self.assertEqual(self._count(), 0)

    def test_upserting_override_clears_master_occurrences(self) -> None:
        self.assertGreater(self._count(), 0)
        override = _event(
            "weekly-1@example.com",
            corpus.recurring_weekly(),
            dtstart=datetime(2026, 5, 8, 10, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 8, 11, 0, tzinfo=UTC),
            recurrence_id="2026-05-08T09:00:00+00:00",
            summary="Rescheduled",
        )
        self.index.upsert_component(override)
        self.assertEqual(self._count(), 0)

    def test_deleting_master_clears_occurrences(self) -> None:
        self.assertGreater(self._count(), 0)
        self.index.delete_component(self.master.ref)
        self.assertEqual(self._count(), 0)
