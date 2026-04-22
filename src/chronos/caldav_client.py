"""HTTP-backed CalDAVSession implementation.

For v1 this is a stub: it satisfies the `CalDAVSession` Protocol in
`chronos.protocols` so sync code type-checks against a real HTTP
implementation, but every network-touching method raises
NotImplementedError. The sync engine is exercised end-to-end via
`tests.fake_caldav.FakeCalDAVSession`, which covers every reconciliation
path without a real server.

Wiring this class to the `caldav` library is deferred to a follow-up
milestone alongside credential resolution (M5). When that work lands it
will replace every NotImplementedError with a real implementation and
translate `caldav.lib.error` exceptions into the CalDAVError hierarchy
defined below.
"""

from __future__ import annotations

from collections.abc import Sequence

from chronos.domain import RemoteCalendar


class CalDAVError(Exception):
    pass


class CalDAVConflictError(CalDAVError):
    """Raised on 412 Precondition Failed (etag mismatch)."""


class CalDAVNotFoundError(CalDAVError):
    """Raised on 404."""


class CalDAVAuthError(CalDAVError):
    """Raised on 401 / 403."""


class CalDAVHttpSession:
    def __init__(
        self,
        *,
        url: str,
        username: str,
        password: str,
    ) -> None:
        self._url = url
        self._username = username
        self._password = password

    def discover_principal(self) -> str:
        raise NotImplementedError(_STUB_MESSAGE)

    def list_calendars(self, principal_url: str) -> Sequence[RemoteCalendar]:
        raise NotImplementedError(_STUB_MESSAGE)

    def get_ctag(self, calendar_url: str) -> str | None:
        raise NotImplementedError(_STUB_MESSAGE)

    def calendar_query(self, calendar_url: str) -> Sequence[tuple[str, str]]:
        raise NotImplementedError(_STUB_MESSAGE)

    def calendar_multiget(
        self, calendar_url: str, hrefs: Sequence[str]
    ) -> Sequence[tuple[str, str, bytes]]:
        raise NotImplementedError(_STUB_MESSAGE)

    def put(self, href: str, ics: bytes, etag: str | None) -> str:
        raise NotImplementedError(_STUB_MESSAGE)

    def delete(self, href: str, etag: str) -> None:
        raise NotImplementedError(_STUB_MESSAGE)


_STUB_MESSAGE = (
    "CalDAVHttpSession is a v1 stub. Wire it to the `caldav` library in "
    "the next milestone before invoking sync with real credentials. "
    "Tests use FakeCalDAVSession."
)


__all__ = [
    "CalDAVAuthError",
    "CalDAVConflictError",
    "CalDAVError",
    "CalDAVHttpSession",
    "CalDAVNotFoundError",
]
