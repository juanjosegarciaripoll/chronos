"""CalDAVHttpSession implementing the CalDAVSession protocol."""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlsplit

from chronos.authorization import Authorization
from chronos.caldav import protocol
from chronos.caldav.errors import SyncTokenExpiredError
from chronos.domain import RemoteCalendar
from chronos.http import Client


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
        return protocol.list_calendars(self._client, home_set)

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
        protocol.delete_resource(self._client, path)

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
