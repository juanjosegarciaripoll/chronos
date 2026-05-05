"""CalDAV error hierarchy."""

from __future__ import annotations


class CalDAVError(Exception):
    pass


class CalDAVConflictError(CalDAVError):
    """Raised on 412 Precondition Failed (etag mismatch)."""


class CalDAVNotFoundError(CalDAVError):
    """Raised on 404."""


class CalDAVAuthError(CalDAVError):
    """Raised on 401 / 403."""


class SyncTokenExpiredError(CalDAVError):
    """Raised when the server rejects a sync-token as invalid or expired.

    The sync engine catches this and falls back to the slow path, then
    re-acquires a fresh token via `get_sync_token`.
    """
