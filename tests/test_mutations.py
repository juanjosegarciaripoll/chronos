from __future__ import annotations

import unittest
from datetime import UTC, date, datetime

from chronos.ical_parser import parse_vcalendar
from chronos.mutations import all_day_bounds, build_event_ics, is_all_day_span

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


class AllDayTest(unittest.TestCase):
    def test_bounds_are_utc_midnight_with_exclusive_end(self) -> None:
        self.assertEqual(
            all_day_bounds(date(2026, 9, 25), date(2026, 9, 26)),
            (datetime(2026, 9, 25, tzinfo=UTC), datetime(2026, 9, 27, tzinfo=UTC)),
        )

    def test_is_all_day_span(self) -> None:
        start, end = all_day_bounds(date(2026, 9, 25), date(2026, 9, 25))
        self.assertTrue(is_all_day_span(start, end))
        self.assertFalse(is_all_day_span(start, None))
        self.assertFalse(
            is_all_day_span(start, datetime(2026, 9, 25, 10, 0, tzinfo=UTC))
        )
        self.assertFalse(
            is_all_day_span(
                datetime(2026, 9, 25, 9, 0, tzinfo=UTC),
                datetime(2026, 9, 26, 9, 0, tzinfo=UTC),
            )
        )

    def test_all_day_ics_round_trips_through_the_parser(self) -> None:
        start, end = all_day_bounds(date(2026, 9, 25), date(2026, 9, 26))
        ics = build_event_ics("u@x", "Trip", start, end, NOW, all_day=True)
        self.assertIn(b"DTSTART;VALUE=DATE:20260925\r\n", ics)
        self.assertIn(b"DTEND;VALUE=DATE:20260927\r\n", ics)
        parsed = parse_vcalendar(ics)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].dtstart, start)
        self.assertEqual(parsed[0].dtend, end)

    def test_all_day_end_defaults_to_next_day(self) -> None:
        start, _ = all_day_bounds(date(2026, 9, 25), date(2026, 9, 25))
        ics = build_event_ics("u@x", "Day", start, None, NOW, all_day=True)
        self.assertIn(b"DTEND;VALUE=DATE:20260926\r\n", ics)


if __name__ == "__main__":
    unittest.main()
