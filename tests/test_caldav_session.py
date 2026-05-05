"""Unit tests for CalDAVHttpSession.

Mocks `chronos.http.Client` instead of the caldav library.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from chronos.authorization import Authorization
from chronos.caldav.errors import (
    CalDAVAuthError,
    CalDAVConflictError,
    CalDAVError,
    CalDAVNotFoundError,
    SyncTokenExpiredError,
)
from chronos.caldav.session import CalDAVHttpSession
from chronos.domain import ComponentKind
from chronos.http import HttpResponse, HttpStatusError


def _session_with_mock_client(mock_client: MagicMock) -> CalDAVHttpSession:
    with patch("chronos.caldav.session.Client", return_value=mock_client):
        return CalDAVHttpSession(
            url="https://caldav.example.com/",
            authorization=Authorization(basic=("user", "pw")),
        )


def _resp(status: int = 207, body: bytes = b"", headers: dict[str, str] | None = None) -> HttpResponse:
    return HttpResponse(status=status, headers=headers or {}, body=body)


def _principal_propfind_body(principal_path: str) -> bytes:
    return (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b'<d:multistatus xmlns:d="DAV:">'
        b"<d:response>"
        b"<d:href>/</d:href>"
        b"<d:propstat><d:prop>"
        b"<d:current-user-principal>"
        + f"<d:href>{principal_path}</d:href>".encode()
        + b"</d:current-user-principal>"
        b"</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
        b"</d:response>"
        b"</d:multistatus>"
    )


def _home_set_body(home_set_path: str) -> bytes:
    return (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b'<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        b"<d:response>"
        b"<d:href>/principal/</d:href>"
        b"<d:propstat><d:prop>"
        b"<c:calendar-home-set>"
        + f"<d:href>{home_set_path}</d:href>".encode()
        + b"</c:calendar-home-set>"
        b"</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
        b"</d:response>"
        b"</d:multistatus>"
    )


def _multistatus(*entries: tuple[str, str, str]) -> bytes:
    """Build a calendar-query REPORT multistatus body."""
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


def _multiget_response(*entries: tuple[str, str, str, str]) -> bytes:
    """Build a calendar-multiget REPORT response body."""
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


def _calendars_body(*cals: tuple[str, str]) -> bytes:
    """Build a calendars PROPFIND body with (href, name) calendar entries."""
    parts = [
        b'<?xml version="1.0" encoding="utf-8"?>',
        b'<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" '
        b'xmlns:cs="http://calendarserver.org/ns/">',
    ]
    for href, name in cals:
        parts.append(
            (
                f"<d:response><d:href>{href}</d:href>"
                "<d:propstat><d:prop>"
                f"<d:displayname>{name}</d:displayname>"
                "<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
                "<c:supported-calendar-component-set>"
                '<c:comp name="VEVENT"/>'
                "</c:supported-calendar-component-set>"
                "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
                "</d:response>"
            ).encode()
        )
    parts.append(b"</d:multistatus>")
    return b"".join(parts)


class DiscoverPrincipalTest(unittest.TestCase):
    def test_returns_principal_url(self) -> None:
        client = MagicMock()
        client._default_scheme = "https"
        client._default_netloc = "caldav.example.com"
        client.request.return_value = _resp(
            207, _principal_propfind_body("/principal/")
        )
        session = _session_with_mock_client(client)
        url = session.discover_principal()
        self.assertEqual(url, "https://caldav.example.com/principal/")

    def test_translates_401_to_auth_error(self) -> None:
        client = MagicMock()
        client._default_scheme = "https"
        client._default_netloc = "caldav.example.com"
        client.request.side_effect = HttpStatusError(401, b"Unauthorized", {})
        session = _session_with_mock_client(client)
        with self.assertRaises(CalDAVAuthError):
            session.discover_principal()

    def test_caches_principal(self) -> None:
        client = MagicMock()
        client._default_scheme = "https"
        client._default_netloc = "caldav.example.com"
        client.request.return_value = _resp(
            207, _principal_propfind_body("/principal/")
        )
        session = _session_with_mock_client(client)
        session.discover_principal()
        session.discover_principal()
        # First call fetches principal, second is cached
        self.assertEqual(client.request.call_count, 1)

    def test_no_principal_in_response_returns_path(self) -> None:
        """If server doesn't return current-user-principal, return path as-is."""
        client = MagicMock()
        client._default_scheme = "https"
        client._default_netloc = "caldav.example.com"
        # Empty multistatus — no principal
        client.request.return_value = _resp(
            207, b'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"/>'
        )
        session = _session_with_mock_client(client)
        url = session.discover_principal()
        self.assertEqual(url, "/")


class ListCalendarsTest(unittest.TestCase):
    def test_returns_remote_calendars(self) -> None:
        client = MagicMock()
        client._default_scheme = "https"
        client._default_netloc = "caldav.example.com"
        # First call: discover_principal
        # Second call: get_calendar_home_set
        # Third call: list_calendars
        client.request.side_effect = [
            _resp(207, _principal_propfind_body("/principal/")),
            _resp(207, _home_set_body("/calendars/")),
            _resp(207, _calendars_body(
                ("/calendars/work/", "Work"),
                ("/calendars/personal/", "Personal"),
            )),
        ]
        session = _session_with_mock_client(client)
        # discover_principal is called first to populate _principal_url
        session._principal_url = "https://caldav.example.com/principal/"
        # list_calendars calls get_calendar_home_set then list_calendars
        client.request.side_effect = [
            _resp(207, _home_set_body("/calendars/")),
            _resp(207, _calendars_body(
                ("/calendars/work/", "Work"),
                ("/calendars/personal/", "Personal"),
            )),
        ]
        cals = session.list_calendars("https://caldav.example.com/principal/")
        self.assertEqual(len(cals), 2)
        names = {c.name for c in cals}
        self.assertIn("Work", names)
        self.assertIn("Personal", names)
        for cal in cals:
            self.assertIn(ComponentKind.VEVENT, cal.supported_components)


class GetCtagTest(unittest.TestCase):
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

    def test_parses_ctag(self) -> None:
        client = MagicMock()
        client.request.return_value = _resp(207, self._CTAG_RESPONSE)
        session = _session_with_mock_client(client)
        self.assertEqual(session.get_ctag("https://x/work/"), "ctag-42")

    def test_returns_none_when_absent(self) -> None:
        client = MagicMock()
        client.request.return_value = _resp(
            207,
            b'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"><d:response/>'
            b"</d:multistatus>",
        )
        session = _session_with_mock_client(client)
        self.assertIsNone(session.get_ctag("https://x/work/"))

    def test_translates_not_found(self) -> None:
        client = MagicMock()
        client.request.side_effect = HttpStatusError(404, b"Not Found", {})
        session = _session_with_mock_client(client)
        with self.assertRaises(CalDAVNotFoundError):
            session.get_ctag("https://x/work/")


class CalendarQueryTest(unittest.TestCase):
    def test_returns_href_etag_pairs(self) -> None:
        client = MagicMock()
        client.request.return_value = _resp(
            207,
            _multistatus(
                ("/cal/work/a.ics", '"etag-a"', "200 OK"),
                ("/cal/work/b.ics", '"etag-b"', "200 OK"),
            ),
        )
        session = _session_with_mock_client(client)
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
        client.request.return_value = _resp(
            207,
            _multistatus(
                ("https://other.example/cal/a.ics", '"etag-a"', "200 OK"),
            ),
        )
        session = _session_with_mock_client(client)
        pairs = session.calendar_query("https://x.example.com/cal/work/")
        self.assertEqual(pairs, (("https://other.example/cal/a.ics", "etag-a"),))

    def test_events_without_etag_get_sentinel(self) -> None:
        client = MagicMock()
        client.request.return_value = _resp(
            207,
            _multistatus(("/cal/work/x.ics", "", "200 OK")),
        )
        session = _session_with_mock_client(client)
        pairs = session.calendar_query("https://x.example.com/cal/work/")
        self.assertEqual(pairs, (("https://x.example.com/cal/work/x.ics", ""),))

    def test_non_2xx_propstat_yields_sentinel_etag(self) -> None:
        client = MagicMock()
        client.request.return_value = _resp(
            207,
            _multistatus(("/cal/work/a.ics", "", "404 Not Found")),
        )
        session = _session_with_mock_client(client)
        pairs = session.calendar_query("https://x.example.com/cal/work/")
        self.assertEqual(pairs, (("https://x.example.com/cal/work/a.ics", ""),))

    def test_unparseable_body_returns_empty(self) -> None:
        client = MagicMock()
        client.request.return_value = _resp(207, b"not xml at all")
        session = _session_with_mock_client(client)
        self.assertEqual(session.calendar_query("https://x.example.com/cal/work/"), ())

    def test_not_found_translated(self) -> None:
        client = MagicMock()
        client.request.side_effect = HttpStatusError(404, b"Not Found", {})
        session = _session_with_mock_client(client)
        with self.assertRaises(CalDAVNotFoundError):
            session.calendar_query("https://x.example.com/no-such/")

    def test_auth_error_translated(self) -> None:
        client = MagicMock()
        client.request.side_effect = HttpStatusError(401, b"Unauthorized", {})
        session = _session_with_mock_client(client)
        with self.assertRaises(CalDAVAuthError):
            session.calendar_query("https://x.example.com/cal/work/")


class CalendarMultigetTest(unittest.TestCase):
    def test_returns_href_etag_bytes_tuples(self) -> None:
        client = MagicMock()
        client.request.return_value = _resp(
            207,
            _multiget_response(
                (
                    "/cal/work/a.ics",
                    '"etag-a"',
                    "BEGIN:VCALENDAR\r\nA\r\nEND:VCALENDAR\r\n",
                    "200 OK",
                ),
            ),
        )
        session = _session_with_mock_client(client)
        results = session.calendar_multiget(
            "https://x.example.com/cal/work/",
            ["https://x.example.com/cal/work/a.ics"],
        )
        self.assertEqual(len(results), 1)
        href, etag, ics = results[0]
        self.assertEqual(href, "https://x.example.com/cal/work/a.ics")
        self.assertEqual(etag, "etag-a")
        self.assertEqual(ics, b"BEGIN:VCALENDAR\r\nA\r\nEND:VCALENDAR\r\n")

    def test_empty_hrefs_returns_empty(self) -> None:
        client = MagicMock()
        session = _session_with_mock_client(client)
        self.assertEqual(
            session.calendar_multiget("https://x.example.com/cal/work/", []), []
        )
        client.request.assert_not_called()

    def test_missing_etag_synthesises_content_hash(self) -> None:
        client = MagicMock()
        client.request.return_value = _resp(
            207,
            _multiget_response(
                (
                    "/cal/work/a.ics",
                    "",
                    "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
                    "200 OK",
                ),
            ),
        )
        session = _session_with_mock_client(client)
        results = session.calendar_multiget(
            "https://x.example.com/cal/work/",
            ["https://x.example.com/cal/work/a.ics"],
        )
        self.assertEqual(len(results), 1)
        _, etag, _ = results[0]
        self.assertTrue(etag.startswith('W/"chronos-'), etag)

    def test_chunks_large_href_lists(self) -> None:
        # 250 hrefs / 100 = 3 chunks
        client = MagicMock()
        client.request.return_value = _resp(207, _multiget_response())
        session = _session_with_mock_client(client)
        many = [f"https://x.example.com/cal/{i}.ics" for i in range(250)]
        session.calendar_multiget("https://x.example.com/cal/", many)
        self.assertEqual(client.request.call_count, 3)

    def test_non_2xx_propstat_rows_dropped(self) -> None:
        client = MagicMock()
        client.request.return_value = _resp(
            207,
            _multiget_response(
                ("/cal/work/a.ics", "", "", "404 Not Found"),
            ),
        )
        session = _session_with_mock_client(client)
        results = session.calendar_multiget(
            "https://x.example.com/cal/work/",
            ["https://x.example.com/cal/work/a.ics"],
        )
        self.assertEqual(results, [])

    def test_request_body_carries_each_href(self) -> None:
        client = MagicMock()
        client.request.return_value = _resp(
            207,
            _multiget_response(
                (
                    "/cal/work/a.ics",
                    '"etag-a"',
                    "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
                    "200 OK",
                ),
            ),
        )
        session = _session_with_mock_client(client)
        session.calendar_multiget(
            "https://x.example.com/cal/work/",
            [
                "https://x.example.com/cal/work/a.ics",
                "https://x.example.com/cal/work/b.ics",
            ],
        )
        call_kwargs = client.request.call_args
        body = call_kwargs[1]["body"]
        self.assertIn(b"<d:href>/cal/work/a.ics</d:href>", body)
        self.assertIn(b"<d:href>/cal/work/b.ics</d:href>", body)

    def test_at_in_href_is_encoded_for_request(self) -> None:
        client = MagicMock()
        client.request.return_value = _resp(207, _multiget_response())
        session = _session_with_mock_client(client)
        session.calendar_multiget(
            "https://apidata.googleusercontent.com/caldav/v2/me@x.com/events/",
            [
                "https://apidata.googleusercontent.com/caldav/v2/me@x.com/events/a.ics",
            ],
        )
        body = client.request.call_args[1]["body"]
        self.assertIn(b"/caldav/v2/me%40x.com/events/a.ics", body)

    def test_not_found_translated(self) -> None:
        client = MagicMock()
        client.request.side_effect = HttpStatusError(404, b"Not Found", {})
        session = _session_with_mock_client(client)
        with self.assertRaises(CalDAVNotFoundError):
            session.calendar_multiget(
                "https://x.example.com/cal/",
                ["https://x.example.com/cal/a.ics"],
            )


class PutTest(unittest.TestCase):
    def test_if_none_match_for_new_resource(self) -> None:
        client = MagicMock()
        client.request.return_value = _resp(201, b"", {"etag": '"etag-new"'})
        session = _session_with_mock_client(client)
        etag = session.put(
            "https://x/cal/work/a.ics", b"BEGIN:VCALENDAR\r\n", etag=None
        )
        self.assertEqual(etag, "etag-new")
        headers = client.request.call_args[1]["headers"]
        self.assertEqual(headers["If-None-Match"], "*")
        self.assertNotIn("If-Match", headers)

    def test_if_match_for_update(self) -> None:
        client = MagicMock()
        client.request.return_value = _resp(204, b"", {"etag": "etag-v2"})
        session = _session_with_mock_client(client)
        session.put(
            "https://x/cal/work/a.ics",
            b"BEGIN:VCALENDAR\r\n",
            etag="etag-v1",
        )
        headers = client.request.call_args[1]["headers"]
        self.assertEqual(headers["If-Match"], "etag-v1")
        self.assertNotIn("If-None-Match", headers)

    def test_412_becomes_conflict_error(self) -> None:
        client = MagicMock()
        client.request.side_effect = HttpStatusError(
            412, b"Precondition Failed", {}
        )
        session = _session_with_mock_client(client)
        with self.assertRaises(CalDAVConflictError):
            session.put("https://x/a.ics", b"...", etag="old")

    def test_404_becomes_not_found(self) -> None:
        client = MagicMock()
        client.request.side_effect = HttpStatusError(404, b"Not Found", {})
        session = _session_with_mock_client(client)
        with self.assertRaises(CalDAVNotFoundError):
            session.put("https://x/a.ics", b"...", etag=None)

    def test_missing_etag_in_response_returns_empty_string(self) -> None:
        client = MagicMock()
        client.request.return_value = _resp(201, b"")
        session = _session_with_mock_client(client)
        result = session.put("https://x/a.ics", b"BEGIN:VCALENDAR\r\n", etag=None)
        self.assertEqual(result, "")


class DeleteTest(unittest.TestCase):
    def test_delete_calls_client(self) -> None:
        client = MagicMock()
        client.request.return_value = _resp(204, b"")
        session = _session_with_mock_client(client)
        session.delete("https://x/cal/a.ics", etag="ignored")
        client.request.assert_called_once()
        call = client.request.call_args
        self.assertEqual(call[0][0], "DELETE")

    def test_not_found_translated(self) -> None:
        client = MagicMock()
        client.request.side_effect = HttpStatusError(404, b"Not Found", {})
        session = _session_with_mock_client(client)
        with self.assertRaises(CalDAVNotFoundError):
            session.delete("https://x/cal/a.ics", etag="etag")


class SyncCollectionTest(unittest.TestCase):
    _BASE = "https://cal.example.com/dav/work/"
    _TOK = "https://example.com/sync/tok-3"

    def _body_with_token(self, token: str = "tok-next") -> bytes:
        return (
            b'<?xml version="1.0" encoding="utf-8"?>'
            b'<d:multistatus xmlns:d="DAV:">'
            b"<d:response><d:href>/dav/work/x.ics</d:href>"
            b'<d:propstat><d:prop><d:getetag>"e1"</d:getetag></d:prop>'
            b"<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
            + f"<d:sync-token>{token}</d:sync-token>".encode()
            + b"</d:multistatus>"
        )

    def test_happy_path(self) -> None:
        client = MagicMock()
        client.request.return_value = _resp(207, self._body_with_token("tok-next"))
        session = _session_with_mock_client(client)
        changed, deleted, new_token = session.sync_collection(self._BASE, self._TOK)
        self.assertEqual(new_token, "tok-next")
        self.assertEqual(len(changed), 1)
        self.assertEqual(deleted, ())

    def test_403_raises_sync_token_expired(self) -> None:
        client = MagicMock()
        client.request.side_effect = HttpStatusError(403, b"Forbidden", {})
        session = _session_with_mock_client(client)
        with self.assertRaises(SyncTokenExpiredError):
            session.sync_collection(self._BASE, self._TOK)

    def test_409_raises_sync_token_expired(self) -> None:
        client = MagicMock()
        client.request.side_effect = HttpStatusError(409, b"Conflict", {})
        session = _session_with_mock_client(client)
        with self.assertRaises(SyncTokenExpiredError):
            session.sync_collection(self._BASE, self._TOK)

    def test_other_error_raises_caldav_error(self) -> None:
        client = MagicMock()
        client.request.side_effect = HttpStatusError(500, b"Server Error", {})
        session = _session_with_mock_client(client)
        with self.assertRaises(CalDAVError) as ctx:
            session.sync_collection(self._BASE, self._TOK)
        self.assertNotIsInstance(ctx.exception, SyncTokenExpiredError)

    def test_missing_sync_token_in_response_raises_expired(self) -> None:
        client = MagicMock()
        client.request.return_value = _resp(
            207,
            b'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"></d:multistatus>',
        )
        session = _session_with_mock_client(client)
        with self.assertRaises(SyncTokenExpiredError):
            session.sync_collection(self._BASE, self._TOK)


class GetSyncTokenTest(unittest.TestCase):
    _BASE = "https://cal.example.com/dav/work/"

    def test_returns_token(self) -> None:
        client = MagicMock()
        client.request.return_value = _resp(
            207,
            b'<?xml version="1.0"?>'
            b'<d:multistatus xmlns:d="DAV:">'
            b"<d:response><d:propstat><d:prop>"
            b"<d:sync-token>https://example.com/sync/7</d:sync-token>"
            b"</d:prop></d:propstat></d:response>"
            b"</d:multistatus>",
        )
        session = _session_with_mock_client(client)
        self.assertEqual(
            session.get_sync_token(self._BASE), "https://example.com/sync/7"
        )

    def test_returns_none_on_http_error(self) -> None:
        client = MagicMock()
        client.request.side_effect = HttpStatusError(501, b"Not Implemented", {})
        session = _session_with_mock_client(client)
        self.assertIsNone(session.get_sync_token(self._BASE))

    def test_returns_none_when_absent(self) -> None:
        client = MagicMock()
        client.request.return_value = _resp(
            207,
            b'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"></d:multistatus>',
        )
        session = _session_with_mock_client(client)
        self.assertIsNone(session.get_sync_token(self._BASE))
