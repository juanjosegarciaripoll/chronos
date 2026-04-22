from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from chronos.domain import (
    ComponentRef,
    LocalStatus,
    VEvent,
    VTodo,
)
from chronos.recurrence import (
    MAX_OCCURRENCES,
    RecurrenceExpansionError,
    expand,
)
from tests import corpus

ACCOUNT = "personal"
CALENDAR = "work"


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
    dtstart: datetime | None = None,
    dtend: datetime | None = None,
    recurrence_id: str | None = None,
    summary: str | None = None,
) -> VEvent:
    return VEvent(
        ref=_ref(uid, recurrence_id),
        href=None,
        etag=None,
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


class NonRecurringExpansionTest(unittest.TestCase):
    def test_single_event_in_window_returns_one_occurrence(self) -> None:
        master = _event(
            "simple-event-1@example.com",
            corpus.simple_event(),
            dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        )
        occs = expand(
            master=master,
            window_start=datetime(2026, 5, 1, tzinfo=UTC),
            window_end=datetime(2026, 5, 2, tzinfo=UTC),
        )
        self.assertEqual(len(occs), 1)
        self.assertEqual(occs[0].start, datetime(2026, 5, 1, 9, 0, tzinfo=UTC))
        self.assertEqual(occs[0].end, datetime(2026, 5, 1, 10, 0, tzinfo=UTC))
        self.assertFalse(occs[0].is_override)

    def test_event_outside_window_returns_empty(self) -> None:
        master = _event(
            "simple-event-1@example.com",
            corpus.simple_event(),
            dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        )
        occs = expand(
            master=master,
            window_start=datetime(2026, 6, 1, tzinfo=UTC),
            window_end=datetime(2026, 7, 1, tzinfo=UTC),
        )
        self.assertEqual(occs, [])

    def test_empty_window_returns_empty(self) -> None:
        master = _event(
            "simple-event-1@example.com",
            corpus.simple_event(),
            dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        )
        occs = expand(
            master=master,
            window_start=datetime(2026, 5, 1, tzinfo=UTC),
            window_end=datetime(2026, 5, 1, tzinfo=UTC),
        )
        self.assertEqual(occs, [])

    def test_missing_anchor_returns_empty(self) -> None:
        master = _event(
            "no-anchor@example.com",
            b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
            dtstart=None,
            dtend=None,
        )
        occs = expand(
            master=master,
            window_start=datetime(2026, 5, 1, tzinfo=UTC),
            window_end=datetime(2026, 6, 1, tzinfo=UTC),
        )
        self.assertEqual(occs, [])


class AllDayExpansionTest(unittest.TestCase):
    def test_all_day_event_midnight_utc(self) -> None:
        master = _event(
            "all-day-1@example.com",
            corpus.all_day_event(),
            dtstart=datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 2, 0, 0, tzinfo=UTC),
        )
        occs = expand(
            master=master,
            window_start=datetime(2026, 4, 1, tzinfo=UTC),
            window_end=datetime(2026, 6, 1, tzinfo=UTC),
        )
        self.assertEqual(len(occs), 1)
        self.assertEqual(occs[0].start, datetime(2026, 5, 1, 0, 0, tzinfo=UTC))
        self.assertEqual(occs[0].end, datetime(2026, 5, 2, 0, 0, tzinfo=UTC))


class ZeroDurationTest(unittest.TestCase):
    def test_event_without_dtend_has_none_end(self) -> None:
        master = _event(
            "zero-duration-1@example.com",
            corpus.zero_duration_event(),
            dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            dtend=None,
        )
        occs = expand(
            master=master,
            window_start=datetime(2026, 5, 1, tzinfo=UTC),
            window_end=datetime(2026, 5, 2, tzinfo=UTC),
        )
        self.assertEqual(len(occs), 1)
        self.assertEqual(occs[0].start, datetime(2026, 5, 1, 9, 0, tzinfo=UTC))
        self.assertIsNone(occs[0].end)


class WeeklyRecurrenceTest(unittest.TestCase):
    def _master(self) -> VEvent:
        return _event(
            "weekly-1@example.com",
            corpus.recurring_weekly(),
            dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        )

    def test_weekly_one_month_window(self) -> None:
        occs = expand(
            master=self._master(),
            window_start=datetime(2026, 5, 1, tzinfo=UTC),
            window_end=datetime(2026, 6, 1, tzinfo=UTC),
        )
        # FREQ=WEEKLY;BYDAY=FR, anchor 2026-05-01 (Fri).
        # Fridays in May 2026: 1, 8, 15, 22, 29.
        self.assertEqual(len(occs), 5)
        starts = [o.start.day for o in occs]
        self.assertEqual(starts, [1, 8, 15, 22, 29])

    def test_infinite_rrule_bounded_by_window(self) -> None:
        # 25-month window: weekly FR event => ~108 occurrences.
        # Proves no runaway expansion on an infinite RRULE.
        occs = expand(
            master=self._master(),
            window_start=datetime(2026, 5, 1, tzinfo=UTC),
            window_end=datetime(2028, 6, 1, tzinfo=UTC),
        )
        self.assertGreater(len(occs), 100)
        self.assertLess(len(occs), MAX_OCCURRENCES)

    def test_all_durations_equal_master_duration(self) -> None:
        occs = expand(
            master=self._master(),
            window_start=datetime(2026, 5, 1, tzinfo=UTC),
            window_end=datetime(2026, 6, 1, tzinfo=UTC),
        )
        for occ in occs:
            assert occ.end is not None
            self.assertEqual(occ.end - occ.start, timedelta(hours=1))


class CountBoundedRrule(unittest.TestCase):
    def test_count_limits_to_five(self) -> None:
        master = _event(
            "count-1@example.com",
            corpus.recurring_count(),
            dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        )
        occs = expand(
            master=master,
            window_start=datetime(2026, 5, 1, tzinfo=UTC),
            window_end=datetime(2027, 1, 1, tzinfo=UTC),
        )
        self.assertEqual(len(occs), 5)


class UntilBoundedRrule(unittest.TestCase):
    def test_until_stops_series(self) -> None:
        master = _event(
            "until-1@example.com",
            corpus.recurring_until(),
            dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        )
        # UNTIL=20260626T090000Z => last occurrence on June 26.
        occs = expand(
            master=master,
            window_start=datetime(2026, 5, 1, tzinfo=UTC),
            window_end=datetime(2027, 1, 1, tzinfo=UTC),
        )
        self.assertTrue(occs)
        last = occs[-1]
        self.assertEqual(last.start, datetime(2026, 6, 26, 9, 0, tzinfo=UTC))


class ExdateOverrideTest(unittest.TestCase):
    def _master(self) -> VEvent:
        return _event(
            "with-exceptions-1@example.com",
            corpus.recurring_with_exceptions(),
            dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        )

    def _override(self) -> VEvent:
        # Override for 2026-05-08 09:00 UTC, rescheduled to 10:00.
        return _event(
            "with-exceptions-1@example.com",
            corpus.recurring_with_exceptions(),
            dtstart=datetime(2026, 5, 8, 10, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 8, 11, 0, tzinfo=UTC),
            recurrence_id="2026-05-08T09:00:00+00:00",
            summary="Weekly meeting (rescheduled)",
        )

    def test_exdate_skips_occurrence(self) -> None:
        occs = expand(
            master=self._master(),
            window_start=datetime(2026, 5, 1, tzinfo=UTC),
            window_end=datetime(2026, 5, 31, tzinfo=UTC),
        )
        starts = [o.start for o in occs]
        self.assertNotIn(datetime(2026, 5, 15, 9, 0, tzinfo=UTC), starts)

    def test_override_replaces_occurrence(self) -> None:
        occs = expand(
            master=self._master(),
            overrides=(self._override(),),
            window_start=datetime(2026, 5, 1, tzinfo=UTC),
            window_end=datetime(2026, 5, 31, tzinfo=UTC),
        )
        overrides = [o for o in occs if o.is_override]
        self.assertEqual(len(overrides), 1)
        self.assertEqual(overrides[0].start, datetime(2026, 5, 8, 10, 0, tzinfo=UTC))
        self.assertEqual(overrides[0].end, datetime(2026, 5, 8, 11, 0, tzinfo=UTC))


class MaxOccurrencesTest(unittest.TestCase):
    def test_exceeds_cap_raises(self) -> None:
        master = _event(
            "weekly-1@example.com",
            corpus.recurring_weekly(),
            dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        )
        with self.assertRaises(RecurrenceExpansionError):
            expand(
                master=master,
                window_start=datetime(2026, 5, 1, tzinfo=UTC),
                window_end=datetime(2026, 8, 1, tzinfo=UTC),
                max_occurrences=3,
            )


class VtodoExpansionTest(unittest.TestCase):
    def test_vtodo_with_due_as_anchor(self) -> None:
        due = datetime(2026, 5, 5, 17, 0, tzinfo=UTC)
        todo = VTodo(
            ref=_ref("todo-1@example.com"),
            href=None,
            etag=None,
            raw_ics=corpus.simple_todo(),
            summary="x",
            description=None,
            location=None,
            dtstart=None,
            due=due,
            status="NEEDS-ACTION",
            local_flags=frozenset(),
            server_flags=frozenset(),
            local_status=LocalStatus.ACTIVE,
            trashed_at=None,
            synced_at=None,
        )
        occs = expand(
            master=todo,
            window_start=datetime(2026, 5, 1, tzinfo=UTC),
            window_end=datetime(2026, 6, 1, tzinfo=UTC),
        )
        self.assertEqual(len(occs), 1)
        self.assertEqual(occs[0].start, due)
