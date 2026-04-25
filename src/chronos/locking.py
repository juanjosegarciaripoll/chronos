"""Cross-platform sync lockfile.

Holds an exclusive lock on a file in the user data dir so two
concurrent `chronos sync` runs (CLI + TUI, or two terminals) don't
race on the same SQLite database / mirror directory. Uses `fcntl`
on POSIX and `msvcrt` on Windows; both fall through a stale-PID
detection step that replaces a lock held by a dead process.

The lock file contains the PID of the holder, written via
`writelines` after `LOCK_EX`. Releasing the lock (via the context
manager's exit, or process death) drops the OS lock automatically;
the file itself is left behind as a hint for diagnostics.
"""

from __future__ import annotations

import contextlib
import errno
import os
import sys
from collections.abc import Generator
from pathlib import Path


class SyncLockError(RuntimeError):
    pass


@contextlib.contextmanager
def acquire_sync_lock(lock_path: Path) -> Generator[None]:
    """Hold an exclusive lock on `lock_path` for the body's duration.

    Raises `SyncLockError` if another live process already holds the
    lock; the error message includes the holder's PID so the user
    knows what to kill if they really want to run a second sync. A
    lock left behind by a process that died (PID no longer alive on
    this host) is replaced silently.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        with _windows_lock(lock_path):
            yield
    else:
        with _posix_lock(lock_path):
            yield


@contextlib.contextmanager
def _posix_lock(lock_path: Path) -> Generator[None]:
    # `fcntl` is POSIX-only; mypy on Windows doesn't see flock /
    # LOCK_* on it. The branch we're in is gated by sys.platform
    # above, so the type-ignores are platform-dispatch noise, not
    # genuine type errors.
    import fcntl  # noqa: I001 — local import keeps Windows imports light

    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
        except BlockingIOError as exc:
            holder = _read_pid(lock_path)
            if holder is not None and not _pid_alive(holder):
                # Stale lock — the previous holder died without
                # releasing. Reclaim it.
                fcntl.flock(fd, fcntl.LOCK_EX)  # type: ignore[attr-defined]
            else:
                raise SyncLockError(
                    f"another chronos sync is already running "
                    f"(pid={holder if holder is not None else '?'}); "
                    f"lockfile: {lock_path}"
                ) from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        os.fsync(fd)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore[attr-defined]
    finally:
        os.close(fd)


@contextlib.contextmanager
def _windows_lock(lock_path: Path) -> Generator[None]:
    import msvcrt

    # Open the lockfile read+write; create if missing. We lock the
    # first byte (msvcrt locks operate on byte ranges).
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            # Windows raises OSError(EDEADLK or EACCES) when the
            # range is already locked.
            if exc.errno not in (errno.EACCES, errno.EDEADLK):
                raise
            holder = _read_pid(lock_path)
            if holder is not None and not _pid_alive(holder):
                # Stale: holder died. Reclaim.
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:
                raise SyncLockError(
                    f"another chronos sync is already running "
                    f"(pid={holder if holder is not None else '?'}); "
                    f"lockfile: {lock_path}"
                ) from exc
        # Write our PID. Truncate to clear any stale content first.
        os.ftruncate(fd, 0)
        # On Windows, the locked-byte range begins at the current
        # file offset; seek to 0 so the write lands inside the lock.
        os.lseek(fd, 0, 0)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                # Release: seek back to the locked byte.
                os.lseek(fd, 0, 0)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)


def _read_pid(lock_path: Path) -> int | None:
    try:
        text = lock_path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check for a PID on the current host."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000  # noqa: N806
            STILL_ACTIVE = 259  # noqa: N806
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except OSError:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The PID exists but belongs to another user — still "alive".
        return True
    return True


__all__ = ["SyncLockError", "acquire_sync_lock"]
