from __future__ import annotations

import unittest
from datetime import date

from chronos.tui.goto import GotoError, parse_goto

# A Wednesday, viewed in the middle of a month.
VIEWED = date(2026, 9, 23)
TODAY = date(2026, 9, 25)


def goto(text: str) -> date:
    return parse_goto(text, viewed=VIEWED, today=TODAY)


class ParseGotoTest(unittest.TestCase):
    def test_iso_date(self) -> None:
        self.assertEqual(goto("2026-12-01"), date(2026, 12, 1))

    def test_today(self) -> None:
        self.assertEqual(goto("today"), TODAY)
        self.assertEqual(goto(" T "), TODAY)

    def test_day_of_viewed_month(self) -> None:
        self.assertEqual(goto("15"), date(2026, 9, 15))
        self.assertEqual(goto("30"), date(2026, 9, 30))

    def test_day_that_month_lacks(self) -> None:
        with self.assertRaisesRegex(GotoError, "September has no day 31"):
            goto("31")

    def test_weekday_is_strictly_after_viewed(self) -> None:
        self.assertEqual(goto("fri"), date(2026, 9, 25))
        self.assertEqual(goto("Friday"), date(2026, 9, 25))
        # Same weekday as the viewed date: the following week.
        self.assertEqual(goto("wed"), date(2026, 9, 30))
        self.assertEqual(goto("mo"), date(2026, 9, 28))

    def test_ambiguous_weekday_prefix_is_rejected(self) -> None:
        # "s" could be saturday or sunday (and is too short anyway).
        with self.assertRaises(GotoError):
            goto("s")

    def test_relative(self) -> None:
        self.assertEqual(goto("+2w"), date(2026, 10, 7))
        self.assertEqual(goto("-3d"), date(2026, 9, 20))
        self.assertEqual(goto("+1m"), date(2026, 10, 23))
        self.assertEqual(goto("-1y"), date(2025, 9, 23))

    def test_garbage(self) -> None:
        for text in ("", "   ", "next tuesday", "2026-13-01", "+2x"):
            with self.subTest(text=text), self.assertRaises(GotoError):
                goto(text)


if __name__ == "__main__":
    unittest.main()
