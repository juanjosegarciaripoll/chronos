"""Tests for `chronos.locking.acquire_sync_lock`.

The lock guarantees that at most one process holds it at a time, that
the holder's PID is recorded, and that a stale lock (whose holder
died without releasing) gets reclaimed instead of blocking the next
sync forever.
"""

from __future__ import annotations

import os
import tempfile
import unittest
import unittest.mock
from collections.abc import Callable
from multiprocessing import Process, Queue
from pathlib import Path

from chronos.locking import SyncLockError, acquire_sync_lock


def _hold_lock_until_signaled(
    lock_path: str, hold_started: Queue[str], release: Queue[str]
) -> None:
    """Subprocess entry point: acquire, signal, wait, release."""
    with acquire_sync_lock(Path(lock_path)):
        hold_started.put("acquired")
        # Block until the test releases us.
        release.get()


class AcquireSyncLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.lock_path = self.tmp / "sync.lock"

    def test_acquires_and_releases_sequentially(self) -> None:
        # The simplest case: two non-overlapping acquisitions both
        # succeed. The lock is reusable across runs of the same
        # process.
        with acquire_sync_lock(self.lock_path):
            pass
        with acquire_sync_lock(self.lock_path):
            pass

    def test_writes_pid_into_lockfile(self) -> None:
        # On Windows, the byte-range lock prevents reading the file
        # from another handle while the lock is held, so we read it
        # after release. POSIX flock allows concurrent reads, but
        # post-release works there too.
        with acquire_sync_lock(self.lock_path):
            pass
        content = self.lock_path.read_text(encoding="ascii").strip()
        self.assertEqual(content, str(os.getpid()))

    def test_creates_parent_directory(self) -> None:
        nested = self.tmp / "nested" / "deeper" / "sync.lock"
        with acquire_sync_lock(nested):
            self.assertTrue(nested.exists())

    def test_concurrent_acquisition_raises(self) -> None:
        # Spawn a child process that holds the lock, then try to
        # acquire it from the parent. The second acquisition must
        # raise SyncLockError. On POSIX (where flock allows concurrent
        # reads of the locked file) the message includes the holder's
        # PID; on Windows the byte-range lock blocks the read so we
        # only assert the "already running" hint.
        hold_started: Queue[str] = Queue()
        release: Queue[str] = Queue()
        child = Process(
            target=_hold_lock_until_signaled,
            args=(str(self.lock_path), hold_started, release),
            daemon=True,
        )
        child.start()
        try:
            self.assertEqual(hold_started.get(timeout=10), "acquired")
            with (
                self.assertRaises(SyncLockError) as ctx,
                acquire_sync_lock(self.lock_path),
            ):
                self.fail("acquired a lock another process holds")
            self.assertIn("another chronos sync is already running", str(ctx.exception))
        finally:
            release.put("go")
            child.join(timeout=10)
            self.assertFalse(child.is_alive())

    def test_stale_lock_with_dead_pid_is_reclaimed(self) -> None:
        # Write a lockfile claiming an obviously-dead PID. Acquisition
        # should detect the staleness and proceed. (Read the file
        # after release; Windows blocks reads while the lock is held.)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.write_text("999999\n", encoding="ascii")
        # Force `_pid_alive` to return False for any PID; otherwise
        # 999999 might happen to be alive (unlikely, but cheap to
        # rule out for determinism).
        with (
            unittest.mock.patch("chronos.locking._pid_alive", return_value=False),
            acquire_sync_lock(self.lock_path),
        ):
            pass
        content = self.lock_path.read_text(encoding="ascii").strip()
        self.assertEqual(content, str(os.getpid()))

    def test_unparseable_lockfile_is_treated_as_stale(self) -> None:
        # If the previous holder crashed before flushing its PID, the
        # file might be empty or contain garbage. We must not block on
        # a lock we can't attribute to a live process.
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.write_text("not-a-number\n", encoding="ascii")
        with (
            unittest.mock.patch("chronos.locking._pid_alive", return_value=False),
            acquire_sync_lock(self.lock_path),
        ):
            pass


class SyncLockErrorIsRuntimeErrorTest(unittest.TestCase):
    def test_inheritance(self) -> None:
        # Callers can catch with the broad `RuntimeError` if they
        # want to without importing the chronos-specific class.
        self.assertTrue(issubclass(SyncLockError, RuntimeError))


# Concurrency tests need a function picklable by multiprocessing on
# Windows; declared at module scope above.
_ = Callable[[], None]
