"""End-to-end integration tests for CalDAVHttpSession.

Gated behind the ``CHRONOS_INTEGRATION=1`` env var so the default
test run never touches the network. To run these, stand up a local
CalDAV server (Radicale is the usual choice — ``docker run -p 5232:5232
tomsquest/docker-radicale``) with a user configured and export:

    CHRONOS_INTEGRATION=1
    CHRONOS_INTEGRATION_URL=http://localhost:5232/
    CHRONOS_INTEGRATION_USERNAME=testuser
    CHRONOS_INTEGRATION_PASSWORD=testpass

Then ``uv run python -m pytest tests/test_caldav_integration.py``.

These tests are intentionally minimal — they prove the wiring works
end-to-end against a real server. The functional reconciliation logic
is covered by ``tests/test_sync.py`` via the FakeCalDAVSession.
"""

from __future__ import annotations

import os
import unittest
import uuid

from chronos.caldav import CalDAVHttpSession


def _integration_enabled() -> bool:
    return os.environ.get("CHRONOS_INTEGRATION") == "1"


def _integration_config() -> tuple[str, str, str]:
    return (
        os.environ["CHRONOS_INTEGRATION_URL"],
        os.environ["CHRONOS_INTEGRATION_USERNAME"],
        os.environ["CHRONOS_INTEGRATION_PASSWORD"],
    )


def _minimal_event(uid: str) -> bytes:
    return (
        b"BEGIN:VCALENDAR\r\n"
        b"VERSION:2.0\r\n"
        b"PRODID:-//chronos-integration//EN\r\n"
        b"BEGIN:VEVENT\r\n"
        b"UID:" + uid.encode("ascii") + b"\r\n"
        b"DTSTAMP:20260422T120000Z\r\n"
        b"DTSTART:20260501T090000Z\r\n"
        b"DTEND:20260501T100000Z\r\n"
        b"SUMMARY:chronos integration smoke\r\n"
        b"END:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )


@unittest.skipUnless(
    _integration_enabled(),
    "integration tests disabled; set CHRONOS_INTEGRATION=1 to run",
)
class CalDAVIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        url, username, password = _integration_config()
        self.session = CalDAVHttpSession(url=url, username=username, password=password)

    def test_discover_principal(self) -> None:
        principal_url = self.session.discover_principal()
        self.assertTrue(principal_url.startswith("http"))

    def test_list_calendars(self) -> None:
        principal_url = self.session.discover_principal()
        calendars = self.session.list_calendars(principal_url)
        # Most servers create at least one default calendar per user.
        self.assertGreaterEqual(len(calendars), 1)
        for calendar in calendars:
            self.assertTrue(calendar.url.startswith("http"))
            self.assertTrue(calendar.name)

    def test_put_get_delete_round_trip(self) -> None:
        principal_url = self.session.discover_principal()
        calendars = self.session.list_calendars(principal_url)
        self.assertGreaterEqual(len(calendars), 1)
        calendar = calendars[0]

        uid = f"chronos-integration-{uuid.uuid4()}@example.com"
        href = calendar.url.rstrip("/") + f"/{uid}.ics"
        ics = _minimal_event(uid)

        etag = self.session.put(href, ics, etag=None)
        # Many servers omit ETag on PUT responses; empty is acceptable.
        self.assertIsInstance(etag, str)

        try:
            pairs = self.session.calendar_query(calendar.url)
            self.assertTrue(any(h == href for h, _ in pairs))

            fetched = self.session.calendar_multiget(calendar.url, [href])
            self.assertEqual(len(fetched), 1)
            self.assertEqual(fetched[0][0], href)
            self.assertEqual(fetched[0][2], ics)
        finally:
            self.session.delete(href, etag=etag or "*")
