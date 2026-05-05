"""Unit tests for chronos.caldav.xml — all parsers and body builders."""

from __future__ import annotations

import unittest
from xml.etree import ElementTree as ET

from chronos.caldav.xml import (
    _build_multiget_body,
    _build_sync_collection_body,
    _parse_calendar_home_set,
    _parse_calendar_query,
    _parse_calendars_propfind,
    _parse_ctag,
    _parse_current_user_principal,
    _parse_multiget,
    _parse_sync_collection,
    _parse_sync_token_propfind,
)
from chronos.domain import ComponentKind

_BASE = "https://cal.example.com/dav/"


class ParseCtagTest(unittest.TestCase):
    def test_extracts_ctag(self) -> None:
        body = (
            b'<?xml version="1.0" encoding="utf-8"?>'
            b'<d:multistatus xmlns:d="DAV:" '
            b'xmlns:cs="http://calendarserver.org/ns/">'
            b"<d:response>"
            b"<d:href>/calendars/work/</d:href>"
            b"<d:propstat>"
            b"<d:prop><cs:getctag>ctag-42</cs:getctag></d:prop>"
            b"<d:status>HTTP/1.1 200 OK</d:status>"
            b"</d:propstat>"
            b"</d:response>"
            b"</d:multistatus>"
        )
        self.assertEqual(_parse_ctag(body), "ctag-42")

    def test_returns_none_when_absent(self) -> None:
        body = (
            b'<?xml version="1.0"?>'
            b'<d:multistatus xmlns:d="DAV:"><d:response/></d:multistatus>'
        )
        self.assertIsNone(_parse_ctag(body))

    def test_returns_none_on_malformed_xml(self) -> None:
        self.assertIsNone(_parse_ctag(b"not xml"))

    def test_returns_none_on_empty_body(self) -> None:
        self.assertIsNone(_parse_ctag(b""))


class ParseCalendarQueryTest(unittest.TestCase):
    def _multistatus(self, *entries: tuple[str, str, str]) -> bytes:
        parts = [
            b'<?xml version="1.0" encoding="utf-8"?>',
            b'<d:multistatus xmlns:d="DAV:">',
        ]
        for href, etag, status in entries:
            parts.append(b"<d:response>")
            parts.append(f"<d:href>{href}</d:href>".encode())
            parts.append(b"<d:propstat>")
            if etag:
                parts.append(
                    f"<d:prop><d:getetag>{etag}</d:getetag></d:prop>".encode()
                )
            else:
                parts.append(b"<d:prop/>")
            parts.append(f"<d:status>HTTP/1.1 {status}</d:status>".encode())
            parts.append(b"</d:propstat>")
            parts.append(b"</d:response>")
        parts.append(b"</d:multistatus>")
        return b"".join(parts)

    def test_parses_relative_hrefs(self) -> None:
        body = self._multistatus(("/dav/work/a.ics", '"etag-a"', "200 OK"))
        pairs = _parse_calendar_query(body, base_url=_BASE)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0][0], "https://cal.example.com/dav/work/a.ics")
        self.assertEqual(pairs[0][1], "etag-a")

    def test_parses_absolute_hrefs_unchanged(self) -> None:
        body = self._multistatus(
            ("https://other.server/cal/a.ics", '"etag-a"', "200 OK")
        )
        pairs = _parse_calendar_query(body, base_url=_BASE)
        self.assertEqual(pairs[0][0], "https://other.server/cal/a.ics")

    def test_non_2xx_status_yields_sentinel_etag(self) -> None:
        body = self._multistatus(("/dav/work/a.ics", "", "404 Not Found"))
        pairs = _parse_calendar_query(body, base_url=_BASE)
        self.assertEqual(pairs[0][0], "https://cal.example.com/dav/work/a.ics")
        self.assertEqual(pairs[0][1], "")

    def test_missing_etag_yields_sentinel(self) -> None:
        body = self._multistatus(("/dav/work/a.ics", "", "200 OK"))
        pairs = _parse_calendar_query(body, base_url=_BASE)
        self.assertEqual(pairs[0][1], "")

    def test_empty_href_is_dropped(self) -> None:
        body = (
            b'<?xml version="1.0"?>'
            b'<d:multistatus xmlns:d="DAV:">'
            b"<d:response><d:href></d:href></d:response>"
            b"</d:multistatus>"
        )
        self.assertEqual(_parse_calendar_query(body, base_url=_BASE), ())

    def test_empty_body_returns_empty(self) -> None:
        self.assertEqual(_parse_calendar_query(b"", base_url=_BASE), ())

    def test_malformed_xml_returns_empty(self) -> None:
        self.assertEqual(
            _parse_calendar_query(b"not xml <<>>", base_url=_BASE), ()
        )


class ParseMultigetTest(unittest.TestCase):
    def _build(self, *entries: tuple[str, str, str, str]) -> bytes:
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

    def test_parses_etag_and_ics(self) -> None:
        body = self._build(
            (
                "/dav/work/a.ics",
                '"etag-a"',
                "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
                "200 OK",
            )
        )
        results = _parse_multiget(body, base_url=_BASE)
        self.assertEqual(len(results), 1)
        href, etag, ics = results[0]
        self.assertTrue(href.endswith("a.ics"))
        self.assertEqual(etag, "etag-a")
        self.assertEqual(ics, b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")

    def test_missing_etag_synthesises_content_hash(self) -> None:
        body = self._build(
            (
                "/dav/work/a.ics",
                "",
                "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
                "200 OK",
            )
        )
        results = _parse_multiget(body, base_url=_BASE)
        _, etag, _ = results[0]
        self.assertTrue(etag.startswith('W/"chronos-'))

    def test_non_2xx_propstat_dropped(self) -> None:
        body = self._build(
            ("/dav/work/a.ics", "", "", "404 Not Found"),
        )
        self.assertEqual(_parse_multiget(body, base_url=_BASE), [])

    def test_empty_body_returns_empty(self) -> None:
        self.assertEqual(_parse_multiget(b"", base_url=_BASE), [])


class ParseCalendarsPropfindTest(unittest.TestCase):
    def _calendar_response(
        self,
        href: str,
        name: str,
        *,
        ctag: str | None = "ct-1",
        sync_token: str | None = "tok-1",
        components: list[str] | None = None,
    ) -> str:
        if components is None:
            components = ["VEVENT"]
        comp_set = "".join(f'<c:comp name="{c}"/>' for c in components)
        ctag_xml = f"<cs:getctag>{ctag}</cs:getctag>" if ctag else ""
        st_xml = f"<d:sync-token>{sync_token}</d:sync-token>" if sync_token else ""
        return (
            f"<d:response><d:href>{href}</d:href>"
            "<d:propstat><d:prop>"
            f"<d:displayname>{name}</d:displayname>"
            "<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
            "<c:supported-calendar-component-set>"
            f"{comp_set}"
            "</c:supported-calendar-component-set>"
            f"{ctag_xml}{st_xml}"
            "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
            "</d:response>"
        )

    def _wrap(self, *responses: str) -> bytes:
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:multistatus xmlns:d="DAV:" '
            'xmlns:c="urn:ietf:params:xml:ns:caldav" '
            'xmlns:cs="http://calendarserver.org/ns/">'
            + "".join(responses)
            + "</d:multistatus>"
        ).encode()

    def test_parses_calendar_with_ctag_and_sync_token(self) -> None:
        body = self._wrap(
            self._calendar_response(
                "/dav/work/",
                "Work",
                ctag="ctag-99",
                sync_token="tok-12",
                components=["VEVENT", "VTODO"],
            )
        )
        result = _parse_calendars_propfind(body, base_url=_BASE)
        self.assertEqual(len(result), 1)
        cal = result[0]
        self.assertEqual(cal.name, "Work")
        self.assertTrue(cal.url.endswith("/dav/work/"))
        self.assertEqual(cal.ctag, "ctag-99")
        self.assertEqual(cal.sync_token, "tok-12")
        self.assertIn(ComponentKind.VEVENT, cal.supported_components)
        self.assertIn(ComponentKind.VTODO, cal.supported_components)

    def test_filters_non_calendar_collections(self) -> None:
        non_cal = (
            "<d:response><d:href>/dav/contacts/</d:href>"
            "<d:propstat><d:prop>"
            "<d:displayname>Contacts</d:displayname>"
            "<d:resourcetype><d:collection/></d:resourcetype>"
            "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
            "</d:response>"
        )
        body = self._wrap(non_cal)
        self.assertEqual(_parse_calendars_propfind(body, base_url=_BASE), ())

    def test_multiple_calendars(self) -> None:
        body = self._wrap(
            self._calendar_response("/dav/work/", "Work", ctag="ct-1"),
            self._calendar_response("/dav/home/", "Home", ctag="ct-2"),
        )
        result = _parse_calendars_propfind(body, base_url=_BASE)
        self.assertEqual(len(result), 2)
        self.assertEqual({c.name for c in result}, {"Work", "Home"})

    def test_missing_ctag_yields_none(self) -> None:
        body = self._wrap(
            self._calendar_response("/dav/work/", "Work", ctag=None, sync_token=None)
        )
        result = _parse_calendars_propfind(body, base_url=_BASE)
        self.assertIsNone(result[0].ctag)
        self.assertIsNone(result[0].sync_token)

    def test_empty_body_returns_empty(self) -> None:
        self.assertEqual(_parse_calendars_propfind(b"", base_url=_BASE), ())

    def test_malformed_body_returns_empty(self) -> None:
        self.assertEqual(
            _parse_calendars_propfind(b"not xml <<>>", base_url=_BASE), ()
        )


class ParseSyncCollectionTest(unittest.TestCase):
    def test_parses_changed_and_deleted(self) -> None:
        body = (
            b'<?xml version="1.0" encoding="utf-8"?>'
            b'<d:multistatus xmlns:d="DAV:">'
            b"<d:response>"
            b"<d:href>/dav/work/a.ics</d:href>"
            b"<d:propstat><d:prop><d:getetag>etag-a</d:getetag></d:prop>"
            b"<d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
            b"</d:response>"
            b"<d:response>"
            b"<d:href>/dav/work/b.ics</d:href>"
            b"<d:propstat><d:prop/>"
            b"<d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>"
            b"</d:response>"
            b"<d:sync-token>https://example.com/sync/tok-7</d:sync-token>"
            b"</d:multistatus>"
        )
        changed, deleted, new_token = _parse_sync_collection(body, base_url=_BASE)
        self.assertEqual(len(changed), 1)
        self.assertTrue(changed[0][0].endswith("a.ics"))
        self.assertEqual(changed[0][1], "etag-a")
        self.assertEqual(len(deleted), 1)
        self.assertTrue(deleted[0].endswith("b.ics"))
        self.assertEqual(new_token, "https://example.com/sync/tok-7")

    def test_returns_none_token_when_absent(self) -> None:
        body = (
            b'<?xml version="1.0"?>'
            b'<d:multistatus xmlns:d="DAV:"></d:multistatus>'
        )
        _, _, new_token = _parse_sync_collection(body, base_url=_BASE)
        self.assertIsNone(new_token)

    def test_empty_body(self) -> None:
        changed, deleted, token = _parse_sync_collection(b"", base_url=_BASE)
        self.assertEqual(changed, [])
        self.assertEqual(deleted, [])
        self.assertIsNone(token)

    def test_malformed_body_returns_empty(self) -> None:
        changed, deleted, token = _parse_sync_collection(
            b"garbage", base_url=_BASE
        )
        self.assertEqual(changed, [])
        self.assertIsNone(token)

    def test_missing_etag_gets_sentinel(self) -> None:
        body = (
            b'<?xml version="1.0"?>'
            b'<d:multistatus xmlns:d="DAV:">'
            b"<d:response>"
            b"<d:href>/dav/work/c.ics</d:href>"
            b"<d:propstat><d:prop/>"
            b"<d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
            b"</d:response>"
            b"<d:sync-token>tok-1</d:sync-token>"
            b"</d:multistatus>"
        )
        changed, _, _ = _parse_sync_collection(body, base_url=_BASE)
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0][1], "")


class ParseSyncTokenPropfindTest(unittest.TestCase):
    def test_extracts_token(self) -> None:
        body = (
            b'<?xml version="1.0"?>'
            b'<d:multistatus xmlns:d="DAV:">'
            b"<d:response><d:propstat><d:prop>"
            b"<d:sync-token>https://example.com/sync/42</d:sync-token>"
            b"</d:prop></d:propstat></d:response>"
            b"</d:multistatus>"
        )
        self.assertEqual(
            _parse_sync_token_propfind(body), "https://example.com/sync/42"
        )

    def test_returns_none_when_absent(self) -> None:
        body = (
            b'<?xml version="1.0"?>'
            b'<d:multistatus xmlns:d="DAV:"></d:multistatus>'
        )
        self.assertIsNone(_parse_sync_token_propfind(body))

    def test_empty_body_returns_none(self) -> None:
        self.assertIsNone(_parse_sync_token_propfind(b""))

    def test_garbage_returns_none(self) -> None:
        self.assertIsNone(_parse_sync_token_propfind(b"garbage"))


class BuildSyncCollectionBodyTest(unittest.TestCase):
    def test_valid_xml_with_token(self) -> None:
        body = _build_sync_collection_body("https://example.com/sync/42")
        tree = ET.fromstring(body)
        ns = {"d": "DAV:"}
        token_elem = tree.find("d:sync-token", ns)
        self.assertIsNotNone(token_elem)
        assert token_elem is not None
        self.assertEqual(token_elem.text, "https://example.com/sync/42")

    def test_returns_bytes(self) -> None:
        body = _build_sync_collection_body("token")
        self.assertIsInstance(body, bytes)


class BuildMultigetBodyTest(unittest.TestCase):
    def test_valid_xml_with_hrefs(self) -> None:
        hrefs = [
            "https://x.example.com/cal/work/a.ics",
            "https://x.example.com/cal/work/b.ics",
        ]
        body = _build_multiget_body(hrefs)
        self.assertIsInstance(body, bytes)
        # The XML should contain path-only hrefs (percent-encoded)
        self.assertIn(b"/cal/work/a.ics", body)
        self.assertIn(b"/cal/work/b.ics", body)

    def test_at_sign_is_encoded(self) -> None:
        hrefs = ["https://apidata.googleusercontent.com/caldav/v2/me@x.com/a.ics"]
        body = _build_multiget_body(hrefs)
        self.assertIn(b"me%40x.com", body)


class ParseCurrentUserPrincipalTest(unittest.TestCase):
    def test_extracts_principal_href(self) -> None:
        body = (
            b'<?xml version="1.0"?>'
            b'<d:multistatus xmlns:d="DAV:">'
            b"<d:response><d:propstat>"
            b"<d:prop><d:current-user-principal>"
            b"<d:href>/principal/</d:href>"
            b"</d:current-user-principal></d:prop>"
            b"<d:status>HTTP/1.1 200 OK</d:status>"
            b"</d:propstat></d:response>"
            b"</d:multistatus>"
        )
        result = _parse_current_user_principal(body, base_url=_BASE)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result.endswith("/principal/"))

    def test_returns_none_when_absent(self) -> None:
        body = (
            b'<?xml version="1.0"?>'
            b'<d:multistatus xmlns:d="DAV:"></d:multistatus>'
        )
        self.assertIsNone(_parse_current_user_principal(body, base_url=_BASE))

    def test_empty_body_returns_none(self) -> None:
        self.assertIsNone(_parse_current_user_principal(b"", base_url=_BASE))


class ParseCalendarHomeSetTest(unittest.TestCase):
    def test_extracts_home_set_href(self) -> None:
        body = (
            b'<?xml version="1.0"?>'
            b'<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            b"<d:response><d:propstat>"
            b"<d:prop><c:calendar-home-set>"
            b"<d:href>/calendars/</d:href>"
            b"</c:calendar-home-set></d:prop>"
            b"<d:status>HTTP/1.1 200 OK</d:status>"
            b"</d:propstat></d:response>"
            b"</d:multistatus>"
        )
        result = _parse_calendar_home_set(body, base_url=_BASE)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result.endswith("/calendars/"))

    def test_returns_none_when_absent(self) -> None:
        body = (
            b'<?xml version="1.0"?>'
            b'<d:multistatus xmlns:d="DAV:"></d:multistatus>'
        )
        self.assertIsNone(_parse_calendar_home_set(body, base_url=_BASE))

    def test_empty_body_returns_none(self) -> None:
        self.assertIsNone(_parse_calendar_home_set(b"", base_url=_BASE))
