from __future__ import annotations

import os
import tempfile
import unittest
import unittest.mock
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


class MirrorCrashSafetyTest(unittest.TestCase):
    """The mirror must survive a crash (KeyboardInterrupt, kill -9) at
    any point during a write: the previous file must be intact, and
    no half-written `.ics` or stale `.tmp-*` file may be left behind
    that would later be mistaken for real data.
    """

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.mirror = VdirMirrorRepository(self.root)

    def _calendar_dir(self, calendar: str = CALENDAR) -> Path:
        return self.root / ACCOUNT / calendar

    def _leftovers(self, calendar: str = CALENDAR) -> list[str]:
        return [
            p.name
            for p in self._calendar_dir(calendar).iterdir()
            if p.name.startswith(".tmp-")
        ]

    def test_keyboard_interrupt_mid_write_preserves_prior_file(self) -> None:
        # Set up a known-good file, then arrange for the next write
        # to be interrupted mid-stream. The original file must remain
        # readable and identical, with no temp file behind.
        ref = _ref("interrupt@example.com")
        self.mirror.write(ref, b"original")

        original_replace = os.replace

        def boom(*_args: object, **_kwargs: object) -> None:
            raise KeyboardInterrupt

        with (
            unittest.mock.patch("chronos.storage.os.replace", side_effect=boom),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.mirror.write(ref, b"new bytes that would have replaced original")

        self.assertEqual(self.mirror.read(ref), b"original")
        self.assertEqual(self._leftovers(), [])
        # And the regular original_replace symbol still works on resume.
        self.assertIs(os.replace, original_replace)

    def test_first_write_failure_leaves_no_resource(self) -> None:
        # If the very first write to a resource is interrupted, the
        # resource must not exist (a partial file would be readable
        # later and misinterpreted as legitimate content).
        ref = _ref("never-saved@example.com")

        def boom(*_args: object, **_kwargs: object) -> None:
            raise KeyboardInterrupt

        with (
            unittest.mock.patch("chronos.storage.os.replace", side_effect=boom),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.mirror.write(ref, b"some content")

        self.assertFalse(self.mirror.exists(ref))
        self.assertEqual(self._leftovers(), [])

    def test_write_failure_before_replace_cleans_up_tmp(self) -> None:
        # If the write fails before os.replace (e.g. disk full mid-fsync),
        # the temp file must still be cleaned up so the directory
        # doesn't accumulate orphaned `.tmp-*` files across many
        # failed runs. fsync is inside the same try/finally as
        # os.replace, so patching it exercises the cleanup path.
        ref = _ref("disk-full@example.com")
        self.mirror.write(ref, b"original")

        with (
            unittest.mock.patch(
                "chronos.storage.os.fsync", side_effect=OSError("disk full")
            ),
            self.assertRaises(OSError),
        ):
            self.mirror.write(ref, b"new content")

        # Original file unchanged.
        self.assertEqual(self.mirror.read(ref), b"original")
        # No temp leftovers.
        self.assertEqual(self._leftovers(), [])

    def test_list_resources_ignores_stale_tmp_files(self) -> None:
        # Even if a previous chronos run died so abruptly that the
        # exception cleanup didn't get a chance to run (kill -9, OOM),
        # `list_resources` must not surface the orphaned `.tmp-*`
        # file as a real resource. The next sync's `_apply_server_deletions`
        # walks `list_resources`; a stale tmp would be treated as a
        # missing-from-server resource and trip the mass-deletion guard.
        good_ref = _ref("real@example.com")
        self.mirror.write(good_ref, b"real")
        # Drop a fake leftover tmp by hand — what `kill -9` mid-write
        # would leave behind.
        cal_dir = self._calendar_dir()
        leftover = cal_dir / ".tmp-deadbeef.ics"
        leftover.write_bytes(b"half written garbage")

        listed = self.mirror.list_resources(ACCOUNT, CALENDAR)
        self.assertEqual({r.uid for r in listed}, {"real@example.com"})
