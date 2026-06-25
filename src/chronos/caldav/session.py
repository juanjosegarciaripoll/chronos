"""CalDAVHttpSession implementing the CalDAVSession protocol."""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlsplit

from chronos.authorization import Authorization
from chronos.caldav import protocol
from chronos.caldav.errors import SyncTokenExpiredError
from chronos.domain import RemoteCalendar
from chronos.http import Client


def _same_collection(a: str, b: str) -> bool:
    """Compare two collection URLs by path, ignoring a trailing slash."""
    return urlsplit(a).path.rstrip("/") == urlsplit(b).path.rstrip("/")


class CalDAVHttpSession:
    """HTTP-backed CalDAV session using the stdlib HTTP client.

    Satisfies the `CalDAVSession` Protocol in `chronos.protocols`.
    """

    def __init__(self, *, url: str, authorization: Authorization) -> None:
        self._client = Client(url, auth=authorization)
        self._base_url = url
        self._principal_url: str | None = None

    def discover_principal(self) -> str:
        if self._principal_url is None:
            path = urlsplit(self._base_url).path or "/"
            self._principal_url = protocol.discover_principal(
                self._client, path
            )
        return self._principal_url

    def list_calendars(self, principal_url: str) -> Sequence[RemoteCalendar]:
        home_set = protocol.get_calendar_home_set(self._client, principal_url)
        calendars = list(protocol.list_calendars(self._client, home_set))
        # Honor a configured URL that points straight at a calendar
        # collection. Home-set discovery can miss it on servers (e.g.
        # SOGo) that nest calendars below the principal and don't expose
        # a calendar-home-set, leaving discovery stuck at the principal.
        direct = protocol.describe_collection(self._client, self._base_url)
        if direct is not None and not any(
            _same_collection(c.url, direct.url) for c in calendars
        ):
            calendars.append(direct)
        return calendars

    def get_ctag(self, calendar_url: str) -> str | None:
        path = urlsplit(calendar_url).path
        return protocol.get_ctag(self._client, path)

    def calendar_query(
        self, calendar_url: str
    ) -> Sequence[tuple[str, str]]:
        # Pass calendar_url (full) so _absolute_href can resolve relative hrefs
        return protocol.calendar_query(self._client, calendar_url)

    def calendar_multiget(
        self, calendar_url: str, hrefs: Sequence[str]
    ) -> Sequence[tuple[str, str, bytes]]:
        return protocol.calendar_multiget(self._client, calendar_url, hrefs)

    def put(self, href: str, ics: bytes, etag: str | None) -> str:
        path = urlsplit(href).path
        return protocol.put_resource(
            self._client,
            path,
            ics,
            if_none_match=(etag is None),
            if_match=etag,
        )

    def delete(self, href: str, etag: str) -> None:
        path = urlsplit(href).path
        protocol.delete_resource(self._client, path, if_match=etag)

    def sync_collection(
        self,
        calendar_url: str,
        sync_token: str,
    ) -> tuple[
        Sequence[tuple[str, str]],
        Sequence[str],
        str,
    ]:
        changed, deleted, new_token = protocol.sync_collection(
            self._client, calendar_url, sync_token
        )
        if new_token is None:
            raise SyncTokenExpiredError("no sync-token in response")
        return tuple(changed), tuple(deleted), new_token

    def get_sync_token(self, calendar_url: str) -> str | None:
        return protocol.get_sync_token(self._client, calendar_url)
