from __future__ import annotations

import unittest

from tests.corpus import ALL_SINGLE_FIXTURES, duplicate_uid


class CorpusStructureTest(unittest.TestCase):
    def test_every_single_fixture_is_a_vcalendar_with_crlf(self) -> None:
        for name, data in ALL_SINGLE_FIXTURES:
            with self.subTest(fixture=name):
                self.assertIsInstance(data, bytes)
                self.assertTrue(data.startswith(b"BEGIN:VCALENDAR\r\n"))
                self.assertTrue(data.rstrip().endswith(b"END:VCALENDAR"))
                self.assertIn(b"\r\n", data)

    def test_every_fixture_uses_example_com(self) -> None:
        for name, data in ALL_SINGLE_FIXTURES:
            with self.subTest(fixture=name):
                text = data.decode("utf-8")
                if "@" in text:
                    self.assertIn("@example.com", text)

    def test_malformed_missing_uid_has_no_uid(self) -> None:
        for name, data in ALL_SINGLE_FIXTURES:
            if name == "malformed_missing_uid":
                self.assertNotIn(b"UID:", data)
                return
        self.fail("malformed_missing_uid fixture missing")

    def test_duplicate_uid_returns_two_items_sharing_uid(self) -> None:
        first, second = duplicate_uid()
        self.assertIsInstance(first, bytes)
        self.assertIsInstance(second, bytes)
        self.assertNotEqual(first, second)
        self.assertIn(b"UID:duplicate-1@example.com", first)
        self.assertIn(b"UID:duplicate-1@example.com", second)

    def test_recurring_with_exceptions_has_override(self) -> None:
        for name, data in ALL_SINGLE_FIXTURES:
            if name == "recurring_with_exceptions":
                self.assertIn(b"RRULE:", data)
                self.assertIn(b"EXDATE:", data)
                self.assertIn(b"RECURRENCE-ID:", data)
                return
        self.fail("recurring_with_exceptions fixture missing")
