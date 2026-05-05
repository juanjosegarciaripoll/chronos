"""Unit tests for chronos.caldav.protocol.

Each function is exercised with a stub Client (MagicMock) that returns recorded
response bodies. Tests verify the returned chronos-typed values and correct
exception translation for 401/403/404/409/412/500.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, call

from chronos.caldav.errors import (
    CalDAVAuthError,
    CalDAVConflictError,
    CalDAVError,
    CalDAVNotFoundError,
    SyncTokenExpiredError,
)
from chronos.caldav.protocol import (
    calendar_multiget,
    calendar_query,
    delete_resource,
    discover_principal,
    get_calendar_home_set,
    get_ctag,
    get_sync_token,
    list_calendars,
    put_resource,
    sync_collection,
)
from chronos.domain import ComponentKind
from chronos.http import HttpResponse, HttpStatusError


# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------


def _client(scheme: str = "https", netloc: str = "cal.example.com") -> MagicMock:
    c = MagicMock()
    c._default_scheme = scheme
    c._default_netloc = netloc
    return c


def _resp(
    status: int = 207,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> HttpResponse:
    return HttpResponse(status=status, headers=headers or {}, body=body)


def _err(status: int) -> HttpStatusError:
    return HttpStatusError(status, b"error", {})


# ---------------------------------------------------------------------------
# XML response fixtures (minimal valid CalDAV server responses)
# ---------------------------------------------------------------------------


def _principal_response(principal_path: str) -> bytes:
    return (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b'<d:multistatus xmlns:d="DAV:">'
        b"<d:response><d:href>/</d:href>"
        b"<d:propstat><d:prop>"
        b"<d:current-user-principal>"
        + f"<d:href>{principal_path}</d:href>".encode()
        + b"</d:current-user-principal>"
        b"</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
        b"</d:response></d:multistatus>"
    )


def _home_set_response(home_set_path: str) -> bytes:
    return (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b'<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        b"<d:response><d:href>/principal/</d:href>"
        b"<d:propstat><d:prop>"
        b"<c:calendar-home-set>"
        + f"<d:href>{home_set_path}</d:href>".encode()
        + b"</c:calendar-home-set>"
        b"</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
        b"</d:response></d:multistatus>"
    )


def _calendars_response(*cals: tuple[str, str]) -> bytes:
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


def _ctag_response(ctag: str) -> bytes:
    return (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b'<d:multistatus xmlns:d="DAV:" xmlns:cs="http://calendarserver.org/ns/">'
        b"<d:response><d:href>/calendars/work/</d:href>"
        b"<d:propstat><d:prop>"
        + f"<cs:getctag>{ctag}</cs:getctag>".encode()
        + b"</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
        b"</d:response></d:multistatus>"
    )


def _sync_token_response(token: str) -> bytes:
    return (
        b'<?xml version="1.0"?>'
        b'<d:multistatus xmlns:d="DAV:">'
        b"<d:response><d:propstat><d:prop>"
        + f"<d:sync-token>{token}</d:sync-token>".encode()
        + b"</d:prop></d:propstat></d:response>"
        b"</d:multistatus>"
    )


def _calendar_query_response(*entries: tuple[str, str]) -> bytes:
    parts = [
        b'<?xml version="1.0" encoding="utf-8"?>',
        b'<d:multistatus xmlns:d="DAV:">',
    ]
    for href, etag in entries:
        parts.append(b"<d:response>")
        parts.append(f"<d:href>{href}</d:href>".encode())
        parts.append(b"<d:propstat><d:prop>")
        if etag:
            parts.append(f"<d:getetag>{etag}</d:getetag>".encode())
        parts.append(b"</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>")
        parts.append(b"</d:response>")
    parts.append(b"</d:multistatus>")
    return b"".join(parts)


def _multiget_response(*entries: tuple[str, str, str]) -> bytes:
    """Build a calendar-multiget REPORT body: (href, etag, ics_text)."""
    parts = [
        b'<?xml version="1.0" encoding="utf-8"?>',
        b'<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">',
    ]
    for href, etag, ics in entries:
        prop_inner = b""
        if etag:
            prop_inner += f"<d:getetag>{etag}</d:getetag>".encode()
        if ics:
            prop_inner += f"<c:calendar-data><![CDATA[{ics}]]></c:calendar-data>".encode()
        parts.append(
            b"<d:response>"
            + f"<d:href>{href}</d:href>".encode()
            + b"<d:propstat><d:prop>"
            + prop_inner
            + b"</d:prop><d:status>HTTP/1.1 200 OK</d:status>"
            b"</d:propstat></d:response>"
        )
    parts.append(b"</d:multistatus>")
    return b"".join(parts)


def _sync_collection_response(
    changed: list[tuple[str, str]],
    deleted: list[str],
    new_token: str,
) -> bytes:
    parts = [
        b'<?xml version="1.0" encoding="utf-8"?>',
        b'<d:multistatus xmlns:d="DAV:">',
    ]
    for href, etag in changed:
        parts.append(
            b"<d:response>"
            + f"<d:href>{href}</d:href>".encode()
            + b"<d:propstat><d:prop>"
            + f"<d:getetag>{etag}</d:getetag>".encode()
            + b"</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
            b"</d:response>"
        )
    for href in deleted:
        parts.append(
            b"<d:response>"
            + f"<d:href>{href}</d:href>".encode()
            + b"<d:propstat>"
            b"<d:prop/>"
            b"<d:status>HTTP/1.1 404 Not Found</d:status>"
            b"</d:propstat>"
            b"</d:response>"
        )
    parts.append(f"<d:sync-token>{new_token}</d:sync-token>".encode())
    parts.append(b"</d:multistatus>")
    return b"".join(parts)


# ---------------------------------------------------------------------------
# discover_principal
# ---------------------------------------------------------------------------


class DiscoverPrincipalTest(unittest.TestCase):
    def test_returns_absolute_principal_url(self) -> None:
        client = _client()
        client.request.return_value = _resp(207, _principal_response("/principal/"))
        url = discover_principal(client, "/")
        self.assertEqual(url, "https://cal.example.com/principal/")

    def test_returns_base_path_when_no_principal_in_response(self) -> None:
        client = _client()
        client.request.return_value = _resp(
            207, b'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"/>'
        )
        url = discover_principal(client, "/")
        self.assertEqual(url, "/")

    def test_uses_custom_base_path(self) -> None:
        client = _client()
        client.request.return_value = _resp(
            207, b'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"/>'
        )
        url = discover_principal(client, "/dav/")
        self.assertEqual(url, "/dav/")

    def test_401_raises_auth_error(self) -> None:
        client = _client()
        client.request.side_effect = _err(401)
        with self.assertRaises(CalDAVAuthError):
            discover_principal(client, "/")

    def test_403_raises_auth_error(self) -> None:
        client = _client()
        client.request.side_effect = _err(403)
        with self.assertRaises(CalDAVAuthError):
            discover_principal(client, "/")

    def test_404_raises_not_found(self) -> None:
        client = _client()
        client.request.side_effect = _err(404)
        with self.assertRaises(CalDAVNotFoundError):
            discover_principal(client, "/")

    def test_500_raises_caldav_error(self) -> None:
        client = _client()
        client.request.side_effect = _err(500)
        with self.assertRaises(CalDAVError) as ctx:
            discover_principal(client, "/")
        self.assertNotIsInstance(ctx.exception, (CalDAVAuthError, CalDAVNotFoundError))

    def test_sends_propfind_with_depth_0(self) -> None:
        client = _client()
        client.request.return_value = _resp(207, _principal_response("/p/"))
        discover_principal(client, "/")
        method, path = client.request.call_args[0]
        headers = client.request.call_args[1]["headers"]
        self.assertEqual(method, "PROPFIND")
        self.assertEqual(headers["Depth"], "0")


# ---------------------------------------------------------------------------
# get_calendar_home_set
# ---------------------------------------------------------------------------


class GetCalendarHomeSetTest(unittest.TestCase):
    def test_returns_absolute_home_set_url(self) -> None:
        client = _client()
        client.request.return_value = _resp(207, _home_set_response("/calendars/"))
        url = get_calendar_home_set(client, "https://cal.example.com/principal/")
        self.assertEqual(url, "https://cal.example.com/calendars/")

    def test_returns_principal_url_when_no_home_set(self) -> None:
        client = _client()
        client.request.return_value = _resp(
            207, b'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"/>'
        )
        principal_url = "https://cal.example.com/principal/"
        url = get_calendar_home_set(client, principal_url)
        self.assertEqual(url, principal_url)

    def test_401_raises_auth_error(self) -> None:
        client = _client()
        client.request.side_effect = _err(401)
        with self.assertRaises(CalDAVAuthError):
            get_calendar_home_set(client, "https://cal.example.com/p/")

    def test_403_raises_auth_error(self) -> None:
        client = _client()
        client.request.side_effect = _err(403)
        with self.assertRaises(CalDAVAuthError):
            get_calendar_home_set(client, "https://cal.example.com/p/")

    def test_404_raises_not_found(self) -> None:
        client = _client()
        client.request.side_effect = _err(404)
        with self.assertRaises(CalDAVNotFoundError):
            get_calendar_home_set(client, "https://cal.example.com/p/")

    def test_500_raises_caldav_error(self) -> None:
        client = _client()
        client.request.side_effect = _err(500)
        with self.assertRaises(CalDAVError):
            get_calendar_home_set(client, "https://cal.example.com/p/")


# ---------------------------------------------------------------------------
# list_calendars
# ---------------------------------------------------------------------------


class ListCalendarsTest(unittest.TestCase):
    def test_returns_remote_calendars_tuple(self) -> None:
        client = _client()
        client.request.return_value = _resp(
            207,
            _calendars_response(
                ("/calendars/work/", "Work"),
                ("/calendars/personal/", "Personal"),
            ),
        )
        cals = list_calendars(client, "https://cal.example.com/calendars/")
        self.assertEqual(len(cals), 2)
        names = {c.name for c in cals}
        self.assertIn("Work", names)
        self.assertIn("Personal", names)

    def test_calendars_have_vevent_component(self) -> None:
        client = _client()
        client.request.return_value = _resp(
            207,
            _calendars_response(("/calendars/work/", "Work")),
        )
        cals = list_calendars(client, "https://cal.example.com/calendars/")
        self.assertEqual(len(cals), 1)
        self.assertIn(ComponentKind.VEVENT, cals[0].supported_components)

    def test_empty_response_returns_empty_tuple(self) -> None:
        client = _client()
        client.request.return_value = _resp(
            207, b'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"/>'
        )
        cals = list_calendars(client, "https://cal.example.com/calendars/")
        self.assertEqual(len(cals), 0)

    def test_sends_propfind_depth_1(self) -> None:
        client = _client()
        client.request.return_value = _resp(207, _calendars_response())
        list_calendars(client, "https://cal.example.com/calendars/")
        headers = client.request.call_args[1]["headers"]
        self.assertEqual(headers["Depth"], "1")

    def test_401_raises_auth_error(self) -> None:
        client = _client()
        client.request.side_effect = _err(401)
        with self.assertRaises(CalDAVAuthError):
            list_calendars(client, "https://cal.example.com/calendars/")

    def test_404_raises_not_found(self) -> None:
        client = _client()
        client.request.side_effect = _err(404)
        with self.assertRaises(CalDAVNotFoundError):
            list_calendars(client, "https://cal.example.com/calendars/")

    def test_500_raises_caldav_error(self) -> None:
        client = _client()
        client.request.side_effect = _err(500)
        with self.assertRaises(CalDAVError):
            list_calendars(client, "https://cal.example.com/calendars/")


# ---------------------------------------------------------------------------
# get_ctag
# ---------------------------------------------------------------------------


class GetCtagTest(unittest.TestCase):
    def test_returns_ctag_string(self) -> None:
        client = _client()
        client.request.return_value = _resp(207, _ctag_response("ctag-42"))
        result = get_ctag(client, "https://cal.example.com/calendars/work/")
        self.assertEqual(result, "ctag-42")

    def test_returns_none_when_ctag_absent(self) -> None:
        client = _client()
        client.request.return_value = _resp(
            207,
            b'<?xml version="1.0"?>'
            b'<d:multistatus xmlns:d="DAV:"><d:response/></d:multistatus>',
        )
        result = get_ctag(client, "https://cal.example.com/calendars/work/")
        self.assertIsNone(result)

    def test_sends_propfind_depth_0(self) -> None:
        client = _client()
        client.request.return_value = _resp(207, _ctag_response("c1"))
        get_ctag(client, "https://cal.example.com/work/")
        method = client.request.call_args[0][0]
        headers = client.request.call_args[1]["headers"]
        self.assertEqual(method, "PROPFIND")
        self.assertEqual(headers["Depth"], "0")

    def test_404_raises_not_found(self) -> None:
        client = _client()
        client.request.side_effect = _err(404)
        with self.assertRaises(CalDAVNotFoundError):
            get_ctag(client, "https://cal.example.com/work/")

    def test_401_raises_auth_error(self) -> None:
        client = _client()
        client.request.side_effect = _err(401)
        with self.assertRaises(CalDAVAuthError):
            get_ctag(client, "https://cal.example.com/work/")

    def test_403_raises_auth_error(self) -> None:
        client = _client()
        client.request.side_effect = _err(403)
        with self.assertRaises(CalDAVAuthError):
            get_ctag(client, "https://cal.example.com/work/")

    def test_500_raises_caldav_error(self) -> None:
        client = _client()
        client.request.side_effect = _err(500)
        with self.assertRaises(CalDAVError):
            get_ctag(client, "https://cal.example.com/work/")


# ---------------------------------------------------------------------------
# get_sync_token
# ---------------------------------------------------------------------------


class GetSyncTokenTest(unittest.TestCase):
    def test_returns_sync_token(self) -> None:
        client = _client()
        client.request.return_value = _resp(
            207, _sync_token_response("https://example.com/sync/7")
        )
        result = get_sync_token(client, "https://cal.example.com/work/")
        self.assertEqual(result, "https://example.com/sync/7")

    def test_returns_none_when_token_absent(self) -> None:
        client = _client()
        client.request.return_value = _resp(
            207, b'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"/>'
        )
        result = get_sync_token(client, "https://cal.example.com/work/")
        self.assertIsNone(result)

    def test_returns_none_on_any_http_error(self) -> None:
        for status in (400, 401, 403, 404, 501):
            with self.subTest(status=status):
                client = _client()
                client.request.side_effect = _err(status)
                result = get_sync_token(client, "https://cal.example.com/work/")
                self.assertIsNone(result)


# ---------------------------------------------------------------------------
# calendar_query
# ---------------------------------------------------------------------------


class CalendarQueryTest(unittest.TestCase):
    def test_returns_href_etag_pairs(self) -> None:
        client = _client()
        client.request.return_value = _resp(
            207,
            _calendar_query_response(
                ("/cal/work/a.ics", '"etag-a"'),
                ("/cal/work/b.ics", '"etag-b"'),
            ),
        )
        pairs = calendar_query(client, "https://cal.example.com/cal/work/")
        self.assertEqual(
            set(pairs),
            {
                ("https://cal.example.com/cal/work/a.ics", "etag-a"),
                ("https://cal.example.com/cal/work/b.ics", "etag-b"),
            },
        )

    def test_absolute_hrefs_pass_through_unchanged(self) -> None:
        client = _client()
        client.request.return_value = _resp(
            207,
            _calendar_query_response(("https://other.example/cal/a.ics", '"etag-a"')),
        )
        pairs = calendar_query(client, "https://cal.example.com/cal/work/")
        self.assertEqual(pairs, (("https://other.example/cal/a.ics", "etag-a"),))

    def test_sends_report_depth_1(self) -> None:
        client = _client()
        client.request.return_value = _resp(207, _calendar_query_response())
        calendar_query(client, "https://cal.example.com/work/")
        method = client.request.call_args[0][0]
        headers = client.request.call_args[1]["headers"]
        self.assertEqual(method, "REPORT")
        self.assertEqual(headers["Depth"], "1")

    def test_404_raises_not_found(self) -> None:
        client = _client()
        client.request.side_effect = _err(404)
        with self.assertRaises(CalDAVNotFoundError):
            calendar_query(client, "https://cal.example.com/no-such/")

    def test_401_raises_auth_error(self) -> None:
        client = _client()
        client.request.side_effect = _err(401)
        with self.assertRaises(CalDAVAuthError):
            calendar_query(client, "https://cal.example.com/work/")

    def test_403_raises_auth_error(self) -> None:
        client = _client()
        client.request.side_effect = _err(403)
        with self.assertRaises(CalDAVAuthError):
            calendar_query(client, "https://cal.example.com/work/")

    def test_500_raises_caldav_error(self) -> None:
        client = _client()
        client.request.side_effect = _err(500)
        with self.assertRaises(CalDAVError) as ctx:
            calendar_query(client, "https://cal.example.com/work/")
        self.assertNotIsInstance(ctx.exception, (CalDAVAuthError, CalDAVNotFoundError))


# ---------------------------------------------------------------------------
# calendar_multiget
# ---------------------------------------------------------------------------


class CalendarMultigetTest(unittest.TestCase):
    _CAL = "https://cal.example.com/cal/work/"
    _ICS = "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"

    def test_returns_href_etag_bytes_tuples(self) -> None:
        client = _client()
        client.request.return_value = _resp(
            207,
            _multiget_response(("/cal/work/a.ics", '"etag-a"', self._ICS)),
        )
        results = calendar_multiget(
            client, self._CAL, ["https://cal.example.com/cal/work/a.ics"]
        )
        self.assertEqual(len(results), 1)
        href, etag, ics = results[0]
        self.assertEqual(href, "https://cal.example.com/cal/work/a.ics")
        self.assertEqual(etag, "etag-a")
        self.assertEqual(ics, self._ICS.encode())

    def test_empty_hrefs_returns_empty_list_no_request(self) -> None:
        client = _client()
        result = calendar_multiget(client, self._CAL, [])
        self.assertEqual(result, [])
        client.request.assert_not_called()

    def test_missing_etag_synthesises_content_hash(self) -> None:
        client = _client()
        client.request.return_value = _resp(
            207,
            _multiget_response(("/cal/work/a.ics", "", self._ICS)),
        )
        results = calendar_multiget(
            client, self._CAL, ["https://cal.example.com/cal/work/a.ics"]
        )
        self.assertEqual(len(results), 1)
        _, etag, _ = results[0]
        self.assertTrue(etag.startswith('W/"chronos-'), etag)

    def test_large_href_list_batched(self) -> None:
        client = _client()
        client.request.return_value = _resp(207, _multiget_response())
        hrefs = [f"https://cal.example.com/cal/{i}.ics" for i in range(250)]
        calendar_multiget(client, self._CAL, hrefs)
        # 250 hrefs at batch size 100 → 3 requests
        self.assertEqual(client.request.call_count, 3)

    def test_request_body_contains_hrefs(self) -> None:
        client = _client()
        client.request.return_value = _resp(
            207,
            _multiget_response(("/cal/work/a.ics", '"e"', self._ICS)),
        )
        calendar_multiget(
            client,
            self._CAL,
            [
                "https://cal.example.com/cal/work/a.ics",
                "https://cal.example.com/cal/work/b.ics",
            ],
        )
        body = client.request.call_args[1]["body"]
        self.assertIn(b"<d:href>/cal/work/a.ics</d:href>", body)
        self.assertIn(b"<d:href>/cal/work/b.ics</d:href>", body)

    def test_at_sign_in_href_is_percent_encoded(self) -> None:
        client = _client(netloc="apidata.googleusercontent.com")
        client.request.return_value = _resp(207, _multiget_response())
        calendar_multiget(
            client,
            "https://apidata.googleusercontent.com/caldav/v2/me@x.com/events/",
            ["https://apidata.googleusercontent.com/caldav/v2/me@x.com/events/a.ics"],
        )
        body = client.request.call_args[1]["body"]
        self.assertIn(b"/caldav/v2/me%40x.com/events/a.ics", body)

    def test_non_2xx_propstat_rows_dropped(self) -> None:
        client = _client()
        # Simulate a response where the propstat status is 404
        body = (
            b'<?xml version="1.0" encoding="utf-8"?>'
            b'<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            b"<d:response>"
            b"<d:href>/cal/work/a.ics</d:href>"
            b"<d:propstat><d:prop/>"
            b"<d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>"
            b"</d:response></d:multistatus>"
        )
        client.request.return_value = _resp(207, body)
        results = calendar_multiget(
            client, self._CAL, ["https://cal.example.com/cal/work/a.ics"]
        )
        self.assertEqual(results, [])

    def test_404_raises_not_found(self) -> None:
        client = _client()
        client.request.side_effect = _err(404)
        with self.assertRaises(CalDAVNotFoundError):
            calendar_multiget(
                client, self._CAL, ["https://cal.example.com/cal/a.ics"]
            )

    def test_401_raises_auth_error(self) -> None:
        client = _client()
        client.request.side_effect = _err(401)
        with self.assertRaises(CalDAVAuthError):
            calendar_multiget(
                client, self._CAL, ["https://cal.example.com/cal/a.ics"]
            )

    def test_500_raises_caldav_error(self) -> None:
        client = _client()
        client.request.side_effect = _err(500)
        with self.assertRaises(CalDAVError) as ctx:
            calendar_multiget(
                client, self._CAL, ["https://cal.example.com/cal/a.ics"]
            )
        self.assertNotIsInstance(ctx.exception, (CalDAVAuthError, CalDAVNotFoundError))


# ---------------------------------------------------------------------------
# sync_collection
# ---------------------------------------------------------------------------


class SyncCollectionTest(unittest.TestCase):
    _CAL = "https://cal.example.com/dav/work/"
    _TOK = "https://example.com/sync/tok-3"
    _NEW_TOK = "https://example.com/sync/tok-4"

    def test_happy_path_changed_and_deleted(self) -> None:
        client = _client()
        client.request.return_value = _resp(
            207,
            _sync_collection_response(
                changed=[("/dav/work/a.ics", '"e1"')],
                deleted=["/dav/work/old.ics"],
                new_token=self._NEW_TOK,
            ),
        )
        changed, deleted, new_token = sync_collection(client, self._CAL, self._TOK)
        self.assertEqual(len(changed), 1)
        self.assertEqual(len(deleted), 1)
        self.assertEqual(new_token, self._NEW_TOK)

    def test_returns_none_token_when_absent_in_response(self) -> None:
        client = _client()
        client.request.return_value = _resp(
            207,
            b'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"></d:multistatus>',
        )
        changed, deleted, new_token = sync_collection(client, self._CAL, self._TOK)
        self.assertIsNone(new_token)

    def test_403_raises_sync_token_expired(self) -> None:
        client = _client()
        client.request.side_effect = _err(403)
        with self.assertRaises(SyncTokenExpiredError):
            sync_collection(client, self._CAL, self._TOK)

    def test_409_raises_sync_token_expired(self) -> None:
        client = _client()
        client.request.side_effect = _err(409)
        with self.assertRaises(SyncTokenExpiredError):
            sync_collection(client, self._CAL, self._TOK)

    def test_404_raises_not_found(self) -> None:
        client = _client()
        client.request.side_effect = _err(404)
        with self.assertRaises(CalDAVNotFoundError):
            sync_collection(client, self._CAL, self._TOK)

    def test_401_raises_auth_error(self) -> None:
        client = _client()
        client.request.side_effect = _err(401)
        with self.assertRaises(CalDAVAuthError):
            sync_collection(client, self._CAL, self._TOK)

    def test_500_raises_caldav_error_not_token_expired(self) -> None:
        client = _client()
        client.request.side_effect = _err(500)
        with self.assertRaises(CalDAVError) as ctx:
            sync_collection(client, self._CAL, self._TOK)
        self.assertNotIsInstance(ctx.exception, SyncTokenExpiredError)

    def test_sends_report_with_sync_token(self) -> None:
        client = _client()
        client.request.return_value = _resp(
            207,
            _sync_collection_response([], [], self._NEW_TOK),
        )
        sync_collection(client, self._CAL, self._TOK)
        method = client.request.call_args[0][0]
        body = client.request.call_args[1]["body"]
        self.assertEqual(method, "REPORT")
        self.assertIn(self._TOK.encode(), body)


# ---------------------------------------------------------------------------
# put_resource
# ---------------------------------------------------------------------------


class PutResourceTest(unittest.TestCase):
    _URL = "https://cal.example.com/cal/work/a.ics"
    _ICS = b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"

    def test_returns_etag_from_response_header(self) -> None:
        client = _client()
        client.request.return_value = _resp(201, b"", {"etag": '"etag-v1"'})
        result = put_resource(client, self._URL, self._ICS)
        self.assertEqual(result, "etag-v1")

    def test_strips_quotes_from_etag(self) -> None:
        client = _client()
        client.request.return_value = _resp(204, b"", {"etag": '"etag-42"'})
        result = put_resource(client, self._URL, self._ICS)
        self.assertEqual(result, "etag-42")

    def test_returns_empty_string_when_etag_absent(self) -> None:
        client = _client()
        client.request.return_value = _resp(201, b"")
        result = put_resource(client, self._URL, self._ICS)
        self.assertEqual(result, "")

    def test_if_none_match_sets_header(self) -> None:
        client = _client()
        client.request.return_value = _resp(201, b"")
        put_resource(client, self._URL, self._ICS, if_none_match=True)
        headers = client.request.call_args[1]["headers"]
        self.assertEqual(headers["If-None-Match"], "*")
        self.assertNotIn("If-Match", headers)

    def test_if_match_sets_header(self) -> None:
        client = _client()
        client.request.return_value = _resp(204, b"")
        put_resource(client, self._URL, self._ICS, if_match="etag-v1")
        headers = client.request.call_args[1]["headers"]
        self.assertEqual(headers["If-Match"], "etag-v1")
        self.assertNotIn("If-None-Match", headers)

    def test_no_conditional_header_by_default(self) -> None:
        client = _client()
        client.request.return_value = _resp(201, b"")
        put_resource(client, self._URL, self._ICS)
        headers = client.request.call_args[1]["headers"]
        self.assertNotIn("If-None-Match", headers)
        self.assertNotIn("If-Match", headers)

    def test_412_raises_conflict_error(self) -> None:
        client = _client()
        client.request.side_effect = _err(412)
        with self.assertRaises(CalDAVConflictError):
            put_resource(client, self._URL, self._ICS, if_match="old-etag")

    def test_404_raises_not_found(self) -> None:
        client = _client()
        client.request.side_effect = _err(404)
        with self.assertRaises(CalDAVNotFoundError):
            put_resource(client, self._URL, self._ICS)

    def test_500_raises_caldav_error(self) -> None:
        client = _client()
        client.request.side_effect = _err(500)
        with self.assertRaises(CalDAVError) as ctx:
            put_resource(client, self._URL, self._ICS)
        self.assertNotIsInstance(
            ctx.exception, (CalDAVConflictError, CalDAVNotFoundError)
        )

    def test_sends_put_with_calendar_content_type(self) -> None:
        client = _client()
        client.request.return_value = _resp(201, b"")
        put_resource(client, self._URL, self._ICS)
        method = client.request.call_args[0][0]
        headers = client.request.call_args[1]["headers"]
        self.assertEqual(method, "PUT")
        self.assertIn("text/calendar", headers.get("Content-Type", ""))


# ---------------------------------------------------------------------------
# delete_resource
# ---------------------------------------------------------------------------


class DeleteResourceTest(unittest.TestCase):
    _URL = "https://cal.example.com/cal/work/a.ics"

    def test_sends_delete_request(self) -> None:
        client = _client()
        client.request.return_value = _resp(204, b"")
        delete_resource(client, self._URL)
        method, path = client.request.call_args[0]
        self.assertEqual(method, "DELETE")
        self.assertEqual(path, "/cal/work/a.ics")

    def test_returns_none_on_success(self) -> None:
        client = _client()
        client.request.return_value = _resp(204, b"")
        result = delete_resource(client, self._URL)
        self.assertIsNone(result)

    def test_404_raises_not_found(self) -> None:
        client = _client()
        client.request.side_effect = _err(404)
        with self.assertRaises(CalDAVNotFoundError):
            delete_resource(client, self._URL)

    def test_500_raises_caldav_error(self) -> None:
        client = _client()
        client.request.side_effect = _err(500)
        with self.assertRaises(CalDAVError) as ctx:
            delete_resource(client, self._URL)
        self.assertNotIsInstance(ctx.exception, CalDAVNotFoundError)

    def test_uses_path_from_url(self) -> None:
        client = _client()
        client.request.return_value = _resp(204, b"")
        delete_resource(client, "https://cal.example.com/dav/events/xyz.ics")
        _, path = client.request.call_args[0]
        self.assertEqual(path, "/dav/events/xyz.ics")


if __name__ == "__main__":
    unittest.main()
