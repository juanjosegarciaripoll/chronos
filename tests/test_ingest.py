"""Tests for src/chronos/ingest.py.

All tests use a real SqliteIndexRepository and VdirMirrorRepository
in a temp directory — no mocking of storage layers per AGENTS.md §7.1.
"""

from __future__ import annotations

import io
import re
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from chronos.domain import (
    AccountConfig,
    AppConfig,
    CalendarRef,
    ComponentRef,
    LocalStatus,
    PlaintextCredential,
    ResourceRef,
    VEvent,
)
from chronos.index_store import SqliteIndexRepository
from chronos.ingest import IngestError, IngestReport, ingest_ics_bytes
from chronos.storage import VdirMirrorRepository
from tests import corpus

_TARGET = CalendarRef(account_name="personal", calendar_name="work")


def _vcalendar(*blocks: str) -> bytes:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//test//EN"]
    for block in blocks:
        lines.extend(block.strip("\n").split("\n"))
    lines.append("END:VCALENDAR")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def _vevent(uid: str, summary: str = "Test event") -> str:
    return f"""
BEGIN:VEVENT
UID:{uid}
DTSTAMP:20260422T120000Z
DTSTART:20260501T090000Z
DTEND:20260501T100000Z
SUMMARY:{summary}
END:VEVENT
"""


def _vtodo(uid: str, summary: str = "Test todo") -> str:
    return f"""
BEGIN:VTODO
UID:{uid}
DTSTAMP:20260422T120000Z
DUE:20260505T170000Z
SUMMARY:{summary}
STATUS:NEEDS-ACTION
END:VTODO
"""


class IngestBytesTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.mirror = VdirMirrorRepository(tmp / "mirror")
        self.index = SqliteIndexRepository(tmp / "index.sqlite3")
        self.addCleanup(self.index.close)

    def _ingest(
        self,
        payload: bytes,
        *,
        on_conflict: str = "skip",
    ) -> IngestReport:
        return ingest_ics_bytes(
            payload,
            target=_TARGET,
            mirror=self.mirror,
            index=self.index,
            on_conflict=on_conflict,  # type: ignore[arg-type]
        )

    # ------------------------------------------------------------------
    # Basic ingestion
    # ------------------------------------------------------------------

    def test_single_event_ingested(self) -> None:
        report = self._ingest(corpus.simple_event())
        self.assertEqual(report.imported, 1)
        self.assertEqual(report.skipped, 0)

        stored = self.index.get_component(
            ComponentRef(
                account_name="personal",
                calendar_name="work",
                uid="simple-event-1@example.com",
            )
        )
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertIsNone(stored.href)  # href=NULL → will be pushed on sync
        self.assertEqual(stored.summary, "Simple event")

    def test_vtodo_ingested(self) -> None:
        report = self._ingest(corpus.simple_todo())
        self.assertEqual(report.imported, 1)
        stored = self.index.get_component(
            ComponentRef(
                account_name="personal",
                calendar_name="work",
                uid="todo-1@example.com",
            )
        )
        self.assertIsNotNone(stored)

    def test_mirror_file_written(self) -> None:
        self._ingest(corpus.simple_event())
        ref = ResourceRef("personal", "work", "simple-event-1@example.com")
        data = self.mirror.read(ref)
        self.assertIn(b"BEGIN:VCALENDAR", data)
        self.assertIn(b"simple-event-1@example.com", data)

    def test_timed_event_with_tz_ingested(self) -> None:
        report = self._ingest(corpus.timed_event_with_tz())
        self.assertEqual(report.imported, 1)
        self.assertEqual(len(report.details), 0)

    def test_all_day_event_ingested(self) -> None:
        report = self._ingest(corpus.all_day_event())
        self.assertEqual(report.imported, 1)

    # ------------------------------------------------------------------
    # Recurring master + override bundle
    # ------------------------------------------------------------------

    def test_recurring_with_exceptions_is_one_mirror_file(self) -> None:
        # The fixture has a master VEVENT and a RECURRENCE-ID override,
        # both sharing uid "with-exceptions-1@example.com". They should
        # land in one .ics file.
        report = self._ingest(corpus.recurring_with_exceptions())
        # Two ParsedComponents upserted (master + override), but from
        # one UID group → one file in the mirror.
        self.assertEqual(report.imported, 1)
        ref = ResourceRef("personal", "work", "with-exceptions-1@example.com")
        data = self.mirror.read(ref)
        self.assertIn(b"RECURRENCE-ID", data)

    def test_two_index_rows_for_master_and_override(self) -> None:
        self._ingest(corpus.recurring_with_exceptions())
        components = self.index.list_calendar_components(_TARGET)
        uids = [c.ref.uid for c in components]
        self.assertIn("with-exceptions-1@example.com", uids)

    # ------------------------------------------------------------------
    # Missing UID
    # ------------------------------------------------------------------

    def test_missing_uid_gets_synthesized(self) -> None:
        report = self._ingest(corpus.malformed_missing_uid())
        self.assertEqual(report.imported, 1)
        self.assertEqual(len(report.details), 0)
        # A UID ending in @chronos was assigned.
        components = self.index.list_calendar_components(_TARGET)
        self.assertEqual(len(components), 1)
        self.assertIn("@chronos", components[0].ref.uid)

    # ------------------------------------------------------------------
    # Multi-UID payload
    # ------------------------------------------------------------------

    def test_multi_uid_payload_splits_into_separate_resources(self) -> None:
        payload = _vcalendar(_vevent("uid-a@example.com"), _vevent("uid-b@example.com"))
        report = self._ingest(payload)
        self.assertEqual(report.imported, 2)
        a = self.index.get_component(
            ComponentRef("personal", "work", "uid-a@example.com")
        )
        b = self.index.get_component(
            ComponentRef("personal", "work", "uid-b@example.com")
        )
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        # Each UID gets its own mirror file.
        self.mirror.read(ResourceRef("personal", "work", "uid-a@example.com"))
        self.mirror.read(ResourceRef("personal", "work", "uid-b@example.com"))

    # ------------------------------------------------------------------
    # Conflict modes
    # ------------------------------------------------------------------

    def test_on_conflict_skip_default(self) -> None:
        self._ingest(corpus.simple_event())
        report = self._ingest(corpus.simple_event(), on_conflict="skip")
        self.assertEqual(report.skipped, 1)
        self.assertEqual(report.imported, 0)
        self.assertIn("simple-event-1@example.com", report.details[0])
        self.assertIn("skipped", report.details[0])

    def test_on_conflict_replace_overwrites_index_and_mirror(self) -> None:
        self._ingest(corpus.simple_event())

        updated = _vcalendar(
            """
BEGIN:VEVENT
UID:simple-event-1@example.com
DTSTAMP:20260422T120000Z
DTSTART:20260601T090000Z
DTEND:20260601T100000Z
SUMMARY:Updated event
END:VEVENT
"""
        )
        report = self._ingest(updated, on_conflict="replace")
        self.assertEqual(report.replaced, 1)
        self.assertEqual(report.imported, 0)

        stored = self.index.get_component(
            ComponentRef("personal", "work", "simple-event-1@example.com")
        )
        assert stored is not None
        self.assertEqual(stored.summary, "Updated event")

    def test_on_conflict_rename_assigns_new_uid(self) -> None:
        self._ingest(corpus.simple_event())
        report = self._ingest(corpus.simple_event(), on_conflict="rename")
        self.assertEqual(report.renamed, 1)
        self.assertEqual(report.imported, 0)
        self.assertIn("renamed to", report.details[0])

        # Two components: the original and the renamed copy.
        components = self.index.list_calendar_components(_TARGET)
        self.assertEqual(len(components), 2)
        uids = {c.ref.uid for c in components}
        self.assertIn("simple-event-1@example.com", uids)
        new_uid = (uids - {"simple-event-1@example.com"}).pop()
        self.assertIn("@chronos", new_uid)

    # ------------------------------------------------------------------
    # Unsupported / malformed input
    # ------------------------------------------------------------------

    def test_vjournal_raises_ingest_error(self) -> None:
        payload = _vcalendar(
            """
BEGIN:VJOURNAL
UID:journal-1@example.com
DTSTAMP:20260422T120000Z
SUMMARY:My journal
END:VJOURNAL
"""
        )
        with self.assertRaises(IngestError) as ctx:
            self._ingest(payload)
        self.assertIn("VJOURNAL", str(ctx.exception))

    def test_vfreebusy_raises_ingest_error(self) -> None:
        payload = _vcalendar(
            """
BEGIN:VFREEBUSY
UID:fb-1@example.com
DTSTAMP:20260422T120000Z
DTSTART:20260501T000000Z
DTEND:20260502T000000Z
END:VFREEBUSY
"""
        )
        with self.assertRaises(IngestError) as ctx:
            self._ingest(payload)
        self.assertIn("VFREEBUSY", str(ctx.exception))

    def test_empty_vcalendar_raises_ingest_error(self) -> None:
        payload = (
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//x//EN\r\nEND:VCALENDAR\r\n"
        )
        with self.assertRaises(IngestError) as ctx:
            self._ingest(payload)
        self.assertIn("no VEVENT or VTODO", str(ctx.exception))

    def test_malformed_bytes_raise_ingest_error(self) -> None:
        with self.assertRaises(IngestError):
            self._ingest(b"not valid ical at all")

    # ------------------------------------------------------------------
    # href=NULL signals pending push
    # ------------------------------------------------------------------

    def test_imported_component_has_null_href(self) -> None:
        self._ingest(corpus.simple_event())
        pending = self.index.list_pending_pushes(_TARGET)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].ref.uid, "simple-event-1@example.com")


class IngestCliTest(unittest.TestCase):
    """CLI-level tests for `chronos import`."""

    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.tmp = tmp
        self.mirror = VdirMirrorRepository(tmp / "mirror")
        self.index = SqliteIndexRepository(tmp / "index.sqlite3")
        self.addCleanup(self.index.close)
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def _account(self, name: str = "personal") -> AccountConfig:
        return AccountConfig(
            name=name,
            url="https://caldav.example.com/dav/",
            username="user@example.com",
            credential=PlaintextCredential(password="s3cret"),
            mirror_path=Path("/unused"),
            trash_retention_days=30,
            include=(re.compile(".*"),),
            exclude=(),
            read_only=(),
        )

    def _config(self, *accounts: AccountConfig) -> AppConfig:
        return AppConfig(
            config_version=1,
            use_utf8=False,
            editor=None,
            accounts=tuple(accounts) or (self._account(),),
        )

    def _ctx(self, config: AppConfig | None = None) -> object:
        from chronos import cli
        from chronos.credentials import DefaultCredentialsProvider

        return cli.CliContext(
            config=config or self._config(),
            mirror=self.mirror,
            index=self.index,
            creds=DefaultCredentialsProvider(),
            stdout=self.stdout,
            stderr=self.stderr,
            now=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
        )

    def _write_ics(self, name: str, payload: bytes) -> Path:
        p = self.tmp / name
        p.write_bytes(payload)
        return p

    # ------------------------------------------------------------------
    # Both flags supplied
    # ------------------------------------------------------------------

    def test_import_with_both_flags(self) -> None:
        from chronos import cli

        ics_file = self._write_ics("event.ics", corpus.simple_event())
        ctx = self._ctx()
        # Seed the index so list_calendars returns something.
        self.index.upsert_component(
            VEvent(
                ref=ComponentRef("personal", "work", "seed@example.com"),
                href="/seed.ics",
                etag="etag-seed",
                raw_ics=b"",
                summary="seed",
                description=None,
                location=None,
                dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
                dtend=None,
                status=None,
                local_flags=frozenset(),
                server_flags=frozenset(),
                local_status=LocalStatus.ACTIVE,
                trashed_at=None,
                synced_at=None,
            )
        )
        code = cli.cmd_import(
            ctx,  # type: ignore[arg-type]
            paths=[ics_file],
            account_name="personal",
            calendar_name="work",
            on_conflict="skip",
            prompt=lambda _: "",
            is_interactive=lambda: False,
        )
        self.assertEqual(code, 0)
        self.assertIn("imported 1", self.stdout.getvalue())

    def test_import_non_interactive_missing_flags_exits_2(self) -> None:
        from chronos import cli

        ics_file = self._write_ics("event.ics", corpus.simple_event())
        ctx = self._ctx()
        code = cli.cmd_import(
            ctx,  # type: ignore[arg-type]
            paths=[ics_file],
            account_name=None,
            calendar_name=None,
            on_conflict="skip",
            prompt=lambda _: "",
            is_interactive=lambda: False,
        )
        self.assertEqual(code, 2)
        self.assertIn("non-interactive", self.stderr.getvalue())

    def test_import_interactive_prompts_calendar_selection(self) -> None:
        from chronos import cli

        # Seed two calendars so the menu has two entries.
        for cal in ("work", "home"):
            self.index.upsert_component(
                VEvent(
                    ref=ComponentRef("personal", cal, f"seed-{cal}@example.com"),
                    href=f"/{cal}/seed.ics",
                    etag="etag",
                    raw_ics=b"",
                    summary="seed",
                    description=None,
                    location=None,
                    dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
                    dtend=None,
                    status=None,
                    local_flags=frozenset(),
                    server_flags=frozenset(),
                    local_status=LocalStatus.ACTIVE,
                    trashed_at=None,
                    synced_at=None,
                )
            )

        ics_file = self._write_ics("event.ics", corpus.simple_event())
        ctx = self._ctx()
        # User picks option 1 (personal/home, sorted alphabetically).
        prompts: list[str] = []

        def capture_prompt(msg: str) -> str:
            prompts.append(msg)
            return "1"

        code = cli.cmd_import(
            ctx,  # type: ignore[arg-type]
            paths=[ics_file],
            account_name=None,
            calendar_name=None,
            on_conflict="skip",
            prompt=capture_prompt,
            is_interactive=lambda: True,
        )
        self.assertEqual(code, 0)
        self.assertTrue(prompts, "expected at least one prompt")
        self.assertIn("imported 1", self.stdout.getvalue())

    def test_import_directory_walks_ics_files(self) -> None:
        from chronos import cli

        ics_dir = self.tmp / "ics_dir"
        ics_dir.mkdir()
        (ics_dir / "a.ics").write_bytes(corpus.simple_event())
        (ics_dir / "b.ics").write_bytes(corpus.simple_todo())
        (ics_dir / "not_ics.txt").write_text("ignore me")

        ctx = self._ctx()
        code = cli.cmd_import(
            ctx,  # type: ignore[arg-type]
            paths=[ics_dir],
            account_name="personal",
            calendar_name="work",
            on_conflict="skip",
            prompt=lambda _: "",
            is_interactive=lambda: False,
        )
        self.assertEqual(code, 0)
        self.assertIn("imported 2", self.stdout.getvalue())

    def test_import_unknown_account_exits_2(self) -> None:
        from chronos import cli

        ics_file = self._write_ics("event.ics", corpus.simple_event())
        ctx = self._ctx()
        code = cli.cmd_import(
            ctx,  # type: ignore[arg-type]
            paths=[ics_file],
            account_name="no-such-account",
            calendar_name="work",
            on_conflict="skip",
            prompt=lambda _: "",
            is_interactive=lambda: False,
        )
        self.assertEqual(code, 2)
        self.assertIn("no-such-account", self.stderr.getvalue())

    def test_import_all_skipped_returns_nonzero(self) -> None:
        from chronos import cli

        ics_file = self._write_ics("event.ics", corpus.simple_event())
        ctx = self._ctx()
        # First import succeeds.
        cli.cmd_import(
            ctx,  # type: ignore[arg-type]
            paths=[ics_file],
            account_name="personal",
            calendar_name="work",
            on_conflict="skip",
            prompt=lambda _: "",
            is_interactive=lambda: False,
        )
        # Second import with skip should return non-zero.
        code = cli.cmd_import(
            ctx,  # type: ignore[arg-type]
            paths=[ics_file],
            account_name="personal",
            calendar_name="work",
            on_conflict="skip",
            prompt=lambda _: "",
            is_interactive=lambda: False,
        )
        self.assertEqual(code, 1)
