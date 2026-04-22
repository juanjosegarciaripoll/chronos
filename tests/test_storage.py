from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from chronos.domain import ResourceRef
from chronos.storage import (
    ResourceNotFoundError,
    VdirMirrorRepository,
)
from tests import corpus

ACCOUNT = "personal"
CALENDAR = "work"
OTHER_CALENDAR = "archive"


def _ref(uid: str, calendar: str = CALENDAR) -> ResourceRef:
    return ResourceRef(account_name=ACCOUNT, calendar_name=calendar, uid=uid)


class MirrorConformanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.mirror = VdirMirrorRepository(self.root)

    def test_list_returns_empty_when_account_or_calendar_missing(self) -> None:
        self.assertEqual(self.mirror.list_calendars(ACCOUNT), ())
        self.assertEqual(self.mirror.list_resources(ACCOUNT, CALENDAR), ())

    def test_write_then_read_round_trips_bytes(self) -> None:
        data = corpus.simple_event()
        ref = _ref("simple-event-1@example.com")
        self.mirror.write(ref, data)
        self.assertEqual(self.mirror.read(ref), data)

    def test_write_is_crash_safe_atomic_replace(self) -> None:
        ref = _ref("atomic@example.com")
        self.mirror.write(ref, b"first")
        self.mirror.write(ref, b"second")
        self.assertEqual(self.mirror.read(ref), b"second")
        leftovers = [
            p
            for p in self.mirror._path_for(ref).parent.iterdir()
            if p.name.startswith(".tmp-")
        ]
        self.assertEqual(leftovers, [])

    def test_write_creates_nested_dirs(self) -> None:
        ref = _ref("nested@example.com", calendar="deep/nested/calendar")
        self.mirror.write(ref, b"payload")
        self.assertTrue(self.mirror.exists(ref))

    def test_read_missing_raises(self) -> None:
        with self.assertRaises(ResourceNotFoundError):
            self.mirror.read(_ref("ghost@example.com"))

    def test_delete_missing_raises(self) -> None:
        with self.assertRaises(ResourceNotFoundError):
            self.mirror.delete(_ref("ghost@example.com"))

    def test_list_resources_after_writes(self) -> None:
        refs = [
            _ref("a@example.com"),
            _ref("b@example.com"),
            _ref("c+special@example.com"),
        ]
        for r in refs:
            self.mirror.write(r, b"payload")
        listed = self.mirror.list_resources(ACCOUNT, CALENDAR)
        self.assertEqual({r.uid for r in listed}, {r.uid for r in refs})

    def test_list_calendars_returns_subdirectories(self) -> None:
        self.mirror.write(_ref("a@example.com", "one"), b"x")
        self.mirror.write(_ref("b@example.com", "two"), b"y")
        self.assertEqual(self.mirror.list_calendars(ACCOUNT), ("one", "two"))

    def test_filename_preserves_non_ascii_uids(self) -> None:
        ref = _ref("café@example.com")
        self.mirror.write(ref, b"payload")
        listed = self.mirror.list_resources(ACCOUNT, CALENDAR)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].uid, "café@example.com")

    def test_move_between_calendars(self) -> None:
        src = _ref("move@example.com", CALENDAR)
        dst = _ref("move@example.com", OTHER_CALENDAR)
        self.mirror.write(src, b"payload")
        self.mirror.move(src, dst)
        self.assertFalse(self.mirror.exists(src))
        self.assertEqual(self.mirror.read(dst), b"payload")

    def test_move_missing_source_raises(self) -> None:
        src = _ref("missing@example.com")
        dst = _ref("missing@example.com", OTHER_CALENDAR)
        with self.assertRaises(ResourceNotFoundError):
            self.mirror.move(src, dst)

    def test_round_trip_every_corpus_fixture(self) -> None:
        for name, data in corpus.ALL_SINGLE_FIXTURES:
            with self.subTest(fixture=name):
                ref = _ref(f"{name}@example.com")
                self.mirror.write(ref, data)
                self.assertEqual(self.mirror.read(ref), data)
