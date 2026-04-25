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

from chronos.authorization import Authorization
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
                authorization=Authorization(basic=("user@example.com", "pw")),
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


def _multistatus(*entries: tuple[str, str, str]) -> bytes:
    """Build a calendar-query REPORT multistatus body.

    Each entry is `(href, etag, status)` where `status` is the HTTP
    status text in the propstat (`"200 OK"`, `"404 Not Found"`, ...).
    Empty etag means "no getetag element in the propstat".
    """
    parts = [
        b'<?xml version="1.0" encoding="utf-8"?>',
        b'<d:multistatus xmlns:d="DAV:">',
    ]
    for href, etag, status in entries:
        parts.append(b"<d:response>")
        parts.append(f"<d:href>{href}</d:href>".encode())
        parts.append(b"<d:propstat>")
        if etag:
            parts.append(f"<d:prop><d:getetag>{etag}</d:getetag></d:prop>".encode())
        else:
            parts.append(b"<d:prop/>")
        parts.append(f"<d:status>HTTP/1.1 {status}</d:status>".encode())
        parts.append(b"</d:propstat>")
        parts.append(b"</d:response>")
    parts.append(b"</d:multistatus>")
    return b"".join(parts)


def _report_response(body: bytes) -> MagicMock:
    response = MagicMock()
    response.raw = body
    return response


class CalendarQueryTest(CalDAVHttpSessionTestCase):
    def test_returns_href_etag_pairs(self) -> None:
        client = MagicMock()
        client.principal.return_value = _fake_principal("https://x/p/", ())
        client.report.return_value = _report_response(
            _multistatus(
                ("/cal/work/a.ics", '"etag-a"', "200 OK"),
                ("/cal/work/b.ics", '"etag-b"', "200 OK"),
            )
        )
        session = self._session_with_client(client)
        pairs = session.calendar_query("https://x.example.com/cal/work/")
        self.assertEqual(
            set(pairs),
            {
                ("https://x.example.com/cal/work/a.ics", "etag-a"),
                ("https://x.example.com/cal/work/b.ics", "etag-b"),
            },
        )

    def test_absolute_hrefs_pass_through_unchanged(self) -> None:
        client = MagicMock()
        client.principal.return_value = _fake_principal("https://x/p/", ())
        client.report.return_value = _report_response(
            _multistatus(
                ("https://other.example/cal/a.ics", '"etag-a"', "200 OK"),
            )
        )
        session = self._session_with_client(client)
        pairs = session.calendar_query("https://x.example.com/cal/work/")
        # Already absolute → not rewritten.
        self.assertEqual(pairs, (("https://other.example/cal/a.ics", "etag-a"),))

    def test_events_without_etag_get_sentinel_not_dropped(self) -> None:
        # Some CalDAV servers (notably Exchange-style gateways) don't
        # return getetag in calendar-query responses. The wrapper must
        # NOT silently drop those events; the multiget pass will
        # synthesize a content-hash etag.
        client = MagicMock()
        client.principal.return_value = _fake_principal("https://x/p/", ())
        client.report.return_value = _report_response(
            _multistatus(("/cal/work/x.ics", "", "200 OK"))
        )
        session = self._session_with_client(client)
        pairs = session.calendar_query("https://x.example.com/cal/work/")
        self.assertEqual(pairs, (("https://x.example.com/cal/work/x.ics", ""),))

    def test_propstat_with_non_2xx_status_yields_sentinel_etag(self) -> None:
        # Google embeds a 404 propstat for properties it doesn't expose
        # on a given resource. The href is still listable, but no etag
        # is available — the multiget pass derives a content-hash etag.
        client = MagicMock()
        client.principal.return_value = _fake_principal("https://x/p/", ())
        client.report.return_value = _report_response(
            _multistatus(("/cal/work/a.ics", "", "404 Not Found"))
        )
        session = self._session_with_client(client)
        pairs = session.calendar_query("https://x.example.com/cal/work/")
        self.assertEqual(pairs, (("https://x.example.com/cal/work/a.ics", ""),))

    def test_responses_without_href_are_dropped(self) -> None:
        client = MagicMock()
        client.principal.return_value = _fake_principal("https://x/p/", ())
        # Hand-roll a response with an empty <d:href/> to exercise the
        # "no href → skip" branch.
        client.report.return_value = _report_response(
            b'<?xml version="1.0" encoding="utf-8"?>'
            b'<d:multistatus xmlns:d="DAV:">'
            b"<d:response><d:href></d:href></d:response>"
            b"</d:multistatus>"
        )
        session = self._session_with_client(client)
        self.assertEqual(session.calendar_query("https://x.example.com/cal/work/"), ())

    def test_unparseable_body_returns_empty(self) -> None:
        client = MagicMock()
        client.principal.return_value = _fake_principal("https://x/p/", ())
        client.report.return_value = _report_response(b"not xml at all")
        session = self._session_with_client(client)
        self.assertEqual(session.calendar_query("https://x.example.com/cal/work/"), ())

    def test_not_found_translated(self) -> None:
        client = MagicMock()
        client.report.side_effect = NotFoundError("404 calendar gone")
        client.principal.return_value = _fake_principal("https://x/p/", ())
        session = self._session_with_client(client)
        with self.assertRaises(CalDAVNotFoundError):
            session.calendar_query("https://x.example.com/no-such/")

    def test_generic_dav_error_translated(self) -> None:
        client = MagicMock()
        client.report.side_effect = DAVError("server exploded")
        client.principal.return_value = _fake_principal("https://x/p/", ())
        session = self._session_with_client(client)
        with self.assertRaises(CalDAVError) as ctx:
            session.calendar_query("https://x.example.com/cal/work/")
        self.assertNotIsInstance(ctx.exception, CalDAVNotFoundError)


def _multiget_response(*entries: tuple[str, str, str, str]) -> bytes:
    """Build a calendar-multiget REPORT response body.

    Each entry is `(href, etag, ics_text, status)`. Empty ics means
    "no calendar-data element present". The ics text is wrapped in
    CDATA so any embedded XML special chars don't fight the parser.
    """
    parts = [
        b'<?xml version="1.0" encoding="utf-8"?>',
        b'<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">',
    ]
    for href, etag, ics, status in entries:
        parts.append(b"<d:response>")
        parts.append(f"<d:href>{href}</d:href>".encode())
        parts.append(b"<d:propstat>")
        prop_inner = b""
        if etag:
            prop_inner += f"<d:getetag>{etag}</d:getetag>".encode()
        if ics:
            prop_inner += (
                f"<c:calendar-data><![CDATA[{ics}]]></c:calendar-data>".encode()
            )
        parts.append(b"<d:prop>" + prop_inner + b"</d:prop>")
        parts.append(f"<d:status>HTTP/1.1 {status}</d:status>".encode())
        parts.append(b"</d:propstat>")
        parts.append(b"</d:response>")
    parts.append(b"</d:multistatus>")
    return b"".join(parts)


class CalendarMultigetTest(CalDAVHttpSessionTestCase):
    def _session_with_multiget_response(self, body: bytes) -> CalDAVHttpSession:
        client = MagicMock()
        client.principal.return_value = _fake_principal("https://x/p/", ())
        client.report.return_value = _report_response(body)
        return self._session_with_client(client)

    def test_returns_href_etag_bytes_tuples(self) -> None:
        session = self._session_with_multiget_response(
            _multiget_response(
                (
                    "/cal/work/a.ics",
                    '"etag-a"',
                    "BEGIN:VCALENDAR\r\nA\r\nEND:VCALENDAR\r\n",
                    "200 OK",
                ),
                (
                    "/cal/work/b.ics",
                    '"etag-b"',
                    "BEGIN:VCALENDAR\r\nB\r\nEND:VCALENDAR\r\n",
                    "200 OK",
                ),
            )
        )
        results = session.calendar_multiget(
            "https://x.example.com/cal/work/",
            [
                "https://x.example.com/cal/work/a.ics",
                "https://x.example.com/cal/work/b.ics",
            ],
        )
        self.assertEqual(len(results), 2)
        self.assertIn(
            (
                "https://x.example.com/cal/work/a.ics",
                "etag-a",
                b"BEGIN:VCALENDAR\r\nA\r\nEND:VCALENDAR\r\n",
            ),
            results,
        )

    def test_empty_hrefs_skips_request(self) -> None:
        client = MagicMock()
        client.principal.return_value = _fake_principal("https://x/p/", ())
        session = self._session_with_client(client)
        self.assertEqual(
            session.calendar_multiget("https://x.example.com/cal/work/", []), ()
        )
        client.report.assert_not_called()

    def test_responses_with_only_non_2xx_propstat_are_dropped(self) -> None:
        # Google can return a 404 propstat for hrefs from calendar-query
        # that aren't multiget-fetchable (e.g. recurrence-id override
        # URLs). Those rows must drop out — the calendar_multiget
        # caller's existing empty-result handling treats them as "skip,
        # try again next sync".
        session = self._session_with_multiget_response(
            _multiget_response(
                ("/cal/work/a.ics", "", "", "404 Not Found"),
            )
        )
        self.assertEqual(
            session.calendar_multiget(
                "https://x.example.com/cal/work/",
                ["https://x.example.com/cal/work/a.ics"],
            ),
            (),
        )

    def test_missing_etag_synthesises_content_hash(self) -> None:
        # Counterpart to calendar_query's missing-etag handling.
        session = self._session_with_multiget_response(
            _multiget_response(
                (
                    "/cal/work/a.ics",
                    "",
                    "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
                    "200 OK",
                ),
            )
        )
        results = session.calendar_multiget(
            "https://x.example.com/cal/work/",
            ["https://x.example.com/cal/work/a.ics"],
        )
        self.assertEqual(len(results), 1)
        _href, etag, data = results[0]
        self.assertEqual(data, b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")
        self.assertTrue(etag.startswith('W/"chronos-'), etag)

    def test_request_body_carries_each_href(self) -> None:
        # The multiget body must include a <D:href> for every requested
        # resource — that's what makes one request fetch many bodies.
        client = MagicMock()
        client.principal.return_value = _fake_principal("https://x/p/", ())
        client.report.return_value = _report_response(
            _multiget_response(
                (
                    "/cal/work/a.ics",
                    '"etag-a"',
                    "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
                    "200 OK",
                ),
            )
        )
        session = self._session_with_client(client)
        session.calendar_multiget(
            "https://x.example.com/cal/work/",
            [
                "https://x.example.com/cal/work/a.ics",
                "https://x.example.com/cal/work/b.ics",
            ],
        )
        body = client.report.call_args.args[1]
        self.assertIn("<d:href>/cal/work/a.ics</d:href>", body)
        self.assertIn("<d:href>/cal/work/b.ics</d:href>", body)

    def test_decoded_at_in_local_index_is_re_encoded_for_request(self) -> None:
        # Google returns hrefs URL-encoded (`%40`) and the local index
        # stores the decoded form (`@`). When we send the multiget,
        # the path must be re-encoded so the server matches it against
        # its own canonical form.
        client = MagicMock()
        client.principal.return_value = _fake_principal("https://x/p/", ())
        client.report.return_value = _report_response(_multiget_response())
        session = self._session_with_client(client)
        session.calendar_multiget(
            "https://apidata.googleusercontent.com/caldav/v2/me@x.com/events/",
            [
                "https://apidata.googleusercontent.com/caldav/v2/me@x.com/events/a.ics",
            ],
        )
        body = client.report.call_args.args[1]
        self.assertIn("/caldav/v2/me%40x.com/events/a.ics", body)

    def test_chunks_large_href_lists(self) -> None:
        # Hrefs over the batch size must be split across multiple
        # REPORT requests so individual response bodies stay bounded.
        client = MagicMock()
        client.principal.return_value = _fake_principal("https://x/p/", ())
        client.report.return_value = _report_response(_multiget_response())
        session = self._session_with_client(client)
        many = [f"https://x.example.com/cal/{i}.ics" for i in range(250)]
        session.calendar_multiget("https://x.example.com/cal/", many)
        # 250 / 100 = 3 chunks (100, 100, 50).
        self.assertEqual(client.report.call_count, 3)

    def test_not_found_translated(self) -> None:
        client = MagicMock()
        client.report.side_effect = NotFoundError("404 calendar gone")
        client.principal.return_value = _fake_principal("https://x/p/", ())
        session = self._session_with_client(client)
        with self.assertRaises(CalDAVNotFoundError):
            session.calendar_multiget(
                "https://x.example.com/cal/", ["https://x.example.com/cal/a.ics"]
            )


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
