"""Unit tests for CalDAVHttpSession.

These exercise the wiring to the `caldav` library via monkey-patching.
They do not talk to a real CalDAV server. End-to-end verification
against a real server is in `tests/test_caldav_integration.py`, which
is gated behind `CHRONOS_INTEGRATION=1`.
"""

from __future__ import annotations

import unittest
from collections.abc import Sequence
from typing import Any
from unittest.mock import MagicMock, patch

from caldav.lib.error import (
    AuthorizationError,
    DAVError,
    NotFoundError,
    PutError,
)

from chronos.caldav_client import (
    CalDAVAuthError,
    CalDAVConflictError,
    CalDAVError,
    CalDAVHttpSession,
    CalDAVNotFoundError,
)
from chronos.domain import ComponentKind


def _fake_calendar(
    *,
    url: str,
    name: str,
    supported: Sequence[str] = ("VEVENT", "VTODO"),
    events: Sequence[tuple[str, str, bytes]] = (),
) -> MagicMock:
    cal = MagicMock()
    cal.url = url
    cal.name = name
    cal.get_display_name.return_value = name
    cal.get_supported_components.return_value = list(supported)

    event_mocks: list[MagicMock] = []
    events_by_url: dict[str, MagicMock] = {}
    for href, etag, ics in events:
        event = MagicMock()
        event.url = href
        event.etag = etag
        event.data = ics
        event_mocks.append(event)
        events_by_url[href] = event

    cal.events.return_value = event_mocks

    def _event_by_url(href: str) -> MagicMock:
        if href not in events_by_url:
            raise NotFoundError(f"not found: {href}")
        return events_by_url[href]

    cal.event_by_url.side_effect = _event_by_url
    return cal


def _fake_principal(url: str, calendars: Sequence[MagicMock]) -> MagicMock:
    principal = MagicMock()
    principal.url = url
    principal.calendars.return_value = list(calendars)
    return principal


class CalDAVHttpSessionTestCase(unittest.TestCase):
    def _session_with_client(self, client: Any) -> CalDAVHttpSession:
        with patch("chronos.caldav_client.caldav.DAVClient", return_value=client):
            return CalDAVHttpSession(
                url="https://caldav.example.com/",
                username="user@example.com",
                password="pw",
            )


class DiscoverPrincipalTest(CalDAVHttpSessionTestCase):
    def test_returns_principal_url(self) -> None:
        client = MagicMock()
        client.principal.return_value = _fake_principal(
            "https://caldav.example.com/principal/", ()
        )
        session = self._session_with_client(client)
        self.assertEqual(
            session.discover_principal(),
            "https://caldav.example.com/principal/",
        )

    def test_translates_authorization_error(self) -> None:
        client = MagicMock()
        client.principal.side_effect = AuthorizationError("401 unauthorized")
        session = self._session_with_client(client)
        with self.assertRaises(CalDAVAuthError):
            session.discover_principal()

    def test_caches_principal(self) -> None:
        client = MagicMock()
        client.principal.return_value = _fake_principal("https://x/principal/", ())
        session = self._session_with_client(client)
        session.discover_principal()
        session.discover_principal()
        self.assertEqual(client.principal.call_count, 1)


class ListCalendarsTest(CalDAVHttpSessionTestCase):
    def test_returns_remote_calendars(self) -> None:
        client = MagicMock()
        cal_work = _fake_calendar(
            url="https://x/cal/work/",
            name="Work",
            supported=("VEVENT",),
        )
        cal_todo = _fake_calendar(
            url="https://x/cal/todo/",
            name="Tasks",
            supported=("VTODO",),
        )
        client.principal.return_value = _fake_principal(
            "https://x/p/", (cal_work, cal_todo)
        )
        session = self._session_with_client(client)
        remotes = session.list_calendars("https://x/p/")
        self.assertEqual(len(remotes), 2)
        self.assertEqual(remotes[0].name, "Work")
        self.assertEqual(remotes[0].url, "https://x/cal/work/")
        self.assertEqual(
            remotes[0].supported_components, frozenset({ComponentKind.VEVENT})
        )
        self.assertEqual(
            remotes[1].supported_components, frozenset({ComponentKind.VTODO})
        )

    def test_defaults_to_both_kinds_when_probe_fails(self) -> None:
        client = MagicMock()
        cal = _fake_calendar(url="https://x/cal/work/", name="Work")
        cal.get_supported_components.side_effect = DAVError("probe failed")
        client.principal.return_value = _fake_principal("https://x/p/", (cal,))
        session = self._session_with_client(client)
        remotes = session.list_calendars("https://x/p/")
        self.assertEqual(
            remotes[0].supported_components,
            frozenset({ComponentKind.VEVENT, ComponentKind.VTODO}),
        )

    def test_falls_back_to_url_segment_when_name_missing(self) -> None:
        client = MagicMock()
        cal = _fake_calendar(url="https://x/cal/private/", name="")
        cal.get_display_name.return_value = None
        cal.name = None
        client.principal.return_value = _fake_principal("https://x/p/", (cal,))
        session = self._session_with_client(client)
        remotes = session.list_calendars("https://x/p/")
        self.assertEqual(remotes[0].name, "private")


class GetCtagTest(CalDAVHttpSessionTestCase):
    _CTAG_RESPONSE = (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b'<d:multistatus xmlns:d="DAV:" '
        b'xmlns:cs="http://calendarserver.org/ns/">'
        b"<d:response>"
        b"<d:href>/calendars/user/work/</d:href>"
        b"<d:propstat>"
        b"<d:prop><cs:getctag>ctag-42</cs:getctag></d:prop>"
        b"<d:status>HTTP/1.1 200 OK</d:status>"
        b"</d:propstat>"
        b"</d:response>"
        b"</d:multistatus>"
    )

    def test_parses_ctag_from_raw_body(self) -> None:
        response = MagicMock()
        response.raw = self._CTAG_RESPONSE
        client = MagicMock()
        client.propfind.return_value = response
        session = self._session_with_client(client)
        self.assertEqual(session.get_ctag("https://x/work/"), "ctag-42")

    def test_returns_none_when_ctag_absent(self) -> None:
        response = MagicMock()
        response.raw = (
            b'<?xml version="1.0" encoding="utf-8"?>'
            b'<d:multistatus xmlns:d="DAV:"><d:response/></d:multistatus>'
        )
        client = MagicMock()
        client.propfind.return_value = response
        session = self._session_with_client(client)
        self.assertIsNone(session.get_ctag("https://x/work/"))

    def test_returns_none_when_body_unparseable(self) -> None:
        response = MagicMock()
        response.raw = b"not xml"
        response.content = None
        response.body = None
        response.tree = None
        client = MagicMock()
        client.propfind.return_value = response
        session = self._session_with_client(client)
        self.assertIsNone(session.get_ctag("https://x/work/"))

    def test_translates_not_found(self) -> None:
        client = MagicMock()
        client.propfind.side_effect = NotFoundError("404")
        session = self._session_with_client(client)
        with self.assertRaises(CalDAVNotFoundError):
            session.get_ctag("https://x/work/")


class CalendarQueryTest(CalDAVHttpSessionTestCase):
    def test_returns_href_etag_pairs(self) -> None:
        client = MagicMock()
        cal = _fake_calendar(
            url="https://x/cal/work/",
            name="Work",
            events=(
                ("https://x/cal/work/a.ics", "etag-a", b"..."),
                ("https://x/cal/work/b.ics", "etag-b", b"..."),
            ),
        )
        client.principal.return_value = _fake_principal("https://x/p/", (cal,))
        session = self._session_with_client(client)
        session.list_calendars("https://x/p/")  # warm the cache
        pairs = session.calendar_query("https://x/cal/work/")
        self.assertEqual(
            set(pairs),
            {
                ("https://x/cal/work/a.ics", "etag-a"),
                ("https://x/cal/work/b.ics", "etag-b"),
            },
        )

    def test_skips_events_without_etag(self) -> None:
        client = MagicMock()
        no_etag_event = MagicMock()
        no_etag_event.url = "https://x/cal/work/x.ics"
        no_etag_event.etag = None
        cal = MagicMock()
        cal.url = "https://x/cal/work/"
        cal.name = "Work"
        cal.get_display_name.return_value = "Work"
        cal.get_supported_components.return_value = ["VEVENT"]
        cal.events.return_value = [no_etag_event]
        client.principal.return_value = _fake_principal("https://x/p/", (cal,))
        session = self._session_with_client(client)
        session.list_calendars("https://x/p/")
        self.assertEqual(session.calendar_query("https://x/cal/work/"), ())

    def test_calendar_not_found_raises(self) -> None:
        client = MagicMock()
        client.principal.return_value = _fake_principal("https://x/p/", ())
        session = self._session_with_client(client)
        with self.assertRaises(CalDAVNotFoundError):
            session.calendar_query("https://x/no-such/")


class CalendarMultigetTest(CalDAVHttpSessionTestCase):
    def test_returns_href_etag_bytes_tuples(self) -> None:
        client = MagicMock()
        cal = _fake_calendar(
            url="https://x/cal/work/",
            name="Work",
            events=(
                ("https://x/cal/work/a.ics", "etag-a", b"A-bytes"),
                ("https://x/cal/work/b.ics", "etag-b", b"B-bytes"),
            ),
        )
        client.principal.return_value = _fake_principal("https://x/p/", (cal,))
        session = self._session_with_client(client)
        session.list_calendars("https://x/p/")
        results = session.calendar_multiget(
            "https://x/cal/work/",
            [
                "https://x/cal/work/a.ics",
                "https://x/cal/work/missing.ics",
                "https://x/cal/work/b.ics",
            ],
        )
        self.assertEqual(len(results), 2)
        self.assertIn(("https://x/cal/work/a.ics", "etag-a", b"A-bytes"), results)
        self.assertIn(("https://x/cal/work/b.ics", "etag-b", b"B-bytes"), results)

    def test_converts_string_data_to_bytes(self) -> None:
        client = MagicMock()
        event = MagicMock()
        event.url = "https://x/cal/work/a.ics"
        event.etag = "etag-a"
        event.data = "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"  # str, not bytes
        cal = MagicMock()
        cal.url = "https://x/cal/work/"
        cal.name = "Work"
        cal.get_display_name.return_value = "Work"
        cal.get_supported_components.return_value = ["VEVENT"]
        cal.events.return_value = [event]
        cal.event_by_url.return_value = event
        client.principal.return_value = _fake_principal("https://x/p/", (cal,))
        session = self._session_with_client(client)
        session.list_calendars("https://x/p/")
        results = session.calendar_multiget(
            "https://x/cal/work/", ["https://x/cal/work/a.ics"]
        )
        self.assertEqual(results[0][2], b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")


class PutTest(CalDAVHttpSessionTestCase):
    def _session_with_put(
        self, put_response: MagicMock, *, raise_exc: BaseException | None = None
    ) -> CalDAVHttpSession:
        client = MagicMock()
        if raise_exc is not None:
            client.put.side_effect = raise_exc
        else:
            client.put.return_value = put_response
        client.principal.return_value = _fake_principal("https://x/p/", ())
        return self._session_with_client(client)

    def test_if_none_match_for_new_resource(self) -> None:
        response = MagicMock()
        response.headers = {"ETag": '"etag-new"'}
        client = MagicMock()
        client.put.return_value = response
        client.principal.return_value = _fake_principal("https://x/p/", ())
        session = self._session_with_client(client)
        etag = session.put(
            "https://x/cal/work/a.ics", b"BEGIN:VCALENDAR\r\n", etag=None
        )
        self.assertEqual(etag, "etag-new")
        call = client.put.call_args
        headers = call.args[2]
        self.assertEqual(headers["If-None-Match"], "*")
        self.assertNotIn("If-Match", headers)

    def test_if_match_for_update(self) -> None:
        response = MagicMock()
        response.headers = {"ETag": "etag-v2"}
        client = MagicMock()
        client.put.return_value = response
        client.principal.return_value = _fake_principal("https://x/p/", ())
        session = self._session_with_client(client)
        session.put(
            "https://x/cal/work/a.ics",
            b"BEGIN:VCALENDAR\r\n",
            etag="etag-v1",
        )
        headers = client.put.call_args.args[2]
        self.assertEqual(headers["If-Match"], "etag-v1")
        self.assertNotIn("If-None-Match", headers)

    def test_412_becomes_conflict_error(self) -> None:
        session = self._session_with_put(
            MagicMock(), raise_exc=PutError("412 Precondition Failed")
        )
        with self.assertRaises(CalDAVConflictError):
            session.put("https://x/a.ics", b"...", etag="old")

    def test_404_becomes_not_found(self) -> None:
        session = self._session_with_put(
            MagicMock(), raise_exc=PutError("404 Not Found")
        )
        with self.assertRaises(CalDAVNotFoundError):
            session.put("https://x/a.ics", b"...", etag=None)

    def test_generic_put_error_becomes_caldav_error(self) -> None:
        session = self._session_with_put(
            MagicMock(), raise_exc=PutError("500 Server Error")
        )
        with self.assertRaises(CalDAVError) as ctx:
            session.put("https://x/a.ics", b"...", etag=None)
        self.assertNotIsInstance(ctx.exception, CalDAVConflictError)
        self.assertNotIsInstance(ctx.exception, CalDAVNotFoundError)

    def test_missing_etag_in_response_returns_empty_string(self) -> None:
        response = MagicMock()
        response.headers = {}
        client = MagicMock()
        client.put.return_value = response
        client.principal.return_value = _fake_principal("https://x/p/", ())
        session = self._session_with_client(client)
        self.assertEqual(
            session.put("https://x/a.ics", b"BEGIN:VCALENDAR\r\n", etag=None),
            "",
        )


class DeleteTest(CalDAVHttpSessionTestCase):
    def test_delete_calls_client_without_headers(self) -> None:
        client = MagicMock()
        client.principal.return_value = _fake_principal("https://x/p/", ())
        session = self._session_with_client(client)
        session.delete("https://x/cal/a.ics", etag="ignored-in-v1")
        client.delete.assert_called_once_with("https://x/cal/a.ics")

    def test_not_found_translated(self) -> None:
        client = MagicMock()
        client.delete.side_effect = NotFoundError("404")
        client.principal.return_value = _fake_principal("https://x/p/", ())
        session = self._session_with_client(client)
        with self.assertRaises(CalDAVNotFoundError):
            session.delete("https://x/cal/a.ics", etag="etag")
