from __future__ import annotations

import unittest
from datetime import UTC, datetime

from chronos.domain import ComponentKind
from chronos.ical_parser import IcalParseError, parse_vcalendar
from tests import corpus


class ParseSimpleEventTest(unittest.TestCase):
    def test_single_event_fields_extracted(self) -> None:
        components = parse_vcalendar(corpus.simple_event())
        self.assertEqual(len(components), 1)
        comp = components[0]
        self.assertEqual(comp.kind, ComponentKind.VEVENT)
        self.assertEqual(comp.uid, "simple-event-1@example.com")
        self.assertEqual(comp.summary, "Simple event")
        self.assertIsNone(comp.recurrence_id)
        self.assertEqual(comp.dtstart, datetime(2026, 5, 1, 9, 0, tzinfo=UTC))
        self.assertEqual(comp.dtend, datetime(2026, 5, 1, 10, 0, tzinfo=UTC))


class ParseAllDayEventTest(unittest.TestCase):
    def test_date_value_normalised_to_utc_midnight(self) -> None:
        (comp,) = parse_vcalendar(corpus.all_day_event())
        self.assertEqual(comp.dtstart, datetime(2026, 5, 1, 0, 0, tzinfo=UTC))
        self.assertEqual(comp.dtend, datetime(2026, 5, 2, 0, 0, tzinfo=UTC))


class ParseTimedWithTzTest(unittest.TestCase):
    def test_tzid_datetime_converted_to_utc(self) -> None:
        (comp,) = parse_vcalendar(corpus.timed_event_with_tz())
        # Madrid DST May => UTC+2, so 11:00 local == 09:00 UTC.
        self.assertIsNotNone(comp.dtstart)
        assert comp.dtstart is not None
        self.assertEqual(comp.dtstart.tzinfo, UTC)
        self.assertEqual(
            comp.dtstart.astimezone(UTC),
            datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
        )


class ParseRecurringWithExceptionsTest(unittest.TestCase):
    def test_master_and_override_both_returned(self) -> None:
        components = parse_vcalendar(corpus.recurring_with_exceptions())
        self.assertEqual(len(components), 2)
        by_rid = {c.recurrence_id: c for c in components}
        master = by_rid[None]
        self.assertIsNone(master.recurrence_id)
        self.assertEqual(master.summary, "Weekly meeting with exceptions")
        override_keys = [k for k in by_rid if k is not None]
        self.assertEqual(len(override_keys), 1)
        override = by_rid[override_keys[0]]
        self.assertEqual(override.summary, "Weekly meeting (rescheduled)")


class ParseTodoTest(unittest.TestCase):
    def test_vtodo_due_is_populated_dtend_is_none(self) -> None:
        (comp,) = parse_vcalendar(corpus.simple_todo())
        self.assertEqual(comp.kind, ComponentKind.VTODO)
        self.assertEqual(comp.due, datetime(2026, 5, 5, 17, 0, tzinfo=UTC))
        self.assertIsNone(comp.dtend)
        self.assertEqual(comp.status, "NEEDS-ACTION")

    def test_completed_todo_has_completed_status(self) -> None:
        (comp,) = parse_vcalendar(corpus.completed_todo())
        self.assertEqual(comp.status, "COMPLETED")


class ParseMalformedTest(unittest.TestCase):
    def test_missing_uid_returns_none_uid(self) -> None:
        (comp,) = parse_vcalendar(corpus.malformed_missing_uid())
        self.assertIsNone(comp.uid)
        self.assertEqual(comp.summary, "No UID present")

    def test_garbage_input_raises(self) -> None:
        with self.assertRaises(IcalParseError):
            parse_vcalendar(b"not an iCalendar document at all")


class ParseEveryCorpusFixtureTest(unittest.TestCase):
    def test_every_single_fixture_yields_at_least_one_component(self) -> None:
        for name, data in corpus.ALL_SINGLE_FIXTURES:
            with self.subTest(fixture=name):
                components = parse_vcalendar(data)
                self.assertGreaterEqual(len(components), 1, msg=name)
