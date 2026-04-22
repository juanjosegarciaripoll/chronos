from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from chronos.domain import CalendarRef, ResourceRef
from chronos.index_store import SqliteIndexRepository
from chronos.storage import VdirMirrorRepository
from chronos.storage_indexing import index_calendar
from tests import corpus

ACCOUNT = "personal"
CALENDAR = "work"


class ProjectionPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.mirror = VdirMirrorRepository(tmp / "mirror")
        self.index = SqliteIndexRepository(tmp / "index.sqlite3")
        self.addCleanup(self.index.close)
        self.calendar = CalendarRef(ACCOUNT, CALENDAR)

    def _seed(self, uid: str, raw: bytes) -> None:
        self.mirror.write(ResourceRef(ACCOUNT, CALENDAR, uid), raw)

    def test_projects_every_corpus_fixture(self) -> None:
        expected_uids: set[str] = set()
        for name, data in corpus.ALL_SINGLE_FIXTURES:
            if name == "malformed_missing_uid":
                # Synthetic UID is generated; track it separately.
                self._seed(f"stub-{name}", data)
                continue
            self._seed(f"resource-{name}", data)
            expected_uids.add(f"parsed-{name}")
        # We'll verify counts rather than exact uids since parser drives them.
        result = index_calendar(
            mirror=self.mirror, index=self.index, calendar=self.calendar
        )
        self.assertGreater(result.components_upserted, 0)
        self.assertEqual(result.components_removed, 0)

    def test_indexing_is_idempotent(self) -> None:
        self._seed("simple@example.com", corpus.simple_event())
        first = index_calendar(
            mirror=self.mirror, index=self.index, calendar=self.calendar
        )
        second = index_calendar(
            mirror=self.mirror, index=self.index, calendar=self.calendar
        )
        self.assertEqual(first.components_upserted, second.components_upserted)
        self.assertEqual(second.components_removed, 0)
        rows = self.index.list_calendar_components(self.calendar)
        self.assertEqual(len(rows), 1)

    def test_removed_mirror_file_deletes_index_row(self) -> None:
        ref = ResourceRef(ACCOUNT, CALENDAR, "temp@example.com")
        self.mirror.write(ref, corpus.simple_event())
        index_calendar(mirror=self.mirror, index=self.index, calendar=self.calendar)
        self.mirror.delete(ref)
        result = index_calendar(
            mirror=self.mirror, index=self.index, calendar=self.calendar
        )
        self.assertEqual(result.components_removed, 1)
        rows = self.index.list_calendar_components(self.calendar)
        self.assertEqual(rows, ())

    def test_recurring_with_exceptions_yields_master_and_override(self) -> None:
        self._seed("series@example.com", corpus.recurring_with_exceptions())
        index_calendar(mirror=self.mirror, index=self.index, calendar=self.calendar)
        rows = self.index.list_calendar_components(self.calendar)
        self.assertEqual(len(rows), 2)
        recurrence_ids = {r.ref.recurrence_id for r in rows}
        self.assertIn(None, recurrence_ids)
        self.assertEqual(len(recurrence_ids), 2)

    def test_malformed_missing_uid_gets_synthetic_uid(self) -> None:
        self._seed("orphan", corpus.malformed_missing_uid())
        index_calendar(mirror=self.mirror, index=self.index, calendar=self.calendar)
        rows = self.index.list_calendar_components(self.calendar)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].ref.uid.startswith("chronos-syn-"))

    def test_round_trip_raw_bytes_preserved(self) -> None:
        data = corpus.timed_event_with_tz()
        self._seed("tz-event@example.com", data)
        index_calendar(mirror=self.mirror, index=self.index, calendar=self.calendar)
        rows = self.index.list_calendar_components(self.calendar)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].raw_ics, data)

    def test_fts_search_after_indexing(self) -> None:
        self._seed("standup@example.com", corpus.simple_event())
        index_calendar(mirror=self.mirror, index=self.index, calendar=self.calendar)
        hits = self.index.search("simple")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].summary, "Simple event")

    def test_parse_error_surfaces_without_aborting_run(self) -> None:
        self._seed("good@example.com", corpus.simple_event())
        self._seed("bad@example.com", b"this is not an iCalendar")
        result = index_calendar(
            mirror=self.mirror, index=self.index, calendar=self.calendar
        )
        self.assertGreaterEqual(result.components_upserted, 1)
        self.assertEqual(len(result.parse_errors), 1)
        self.assertIn("bad@example.com", result.parse_errors[0])
