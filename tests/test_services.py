from __future__ import annotations

import re
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from chronos.credentials import DefaultCredentialsProvider
from chronos.domain import (
    AccountConfig,
    AppConfig,
    ComponentRef,
    EnvCredential,
    LocalStatus,
    PlaintextCredential,
    ResourceRef,
    VEvent,
)
from chronos.index_store import SqliteIndexRepository
from chronos.services import DiagnosticStatus, format_report, run_doctor
from chronos.storage import VdirMirrorRepository
from tests import corpus


def _account(
    *,
    name: str = "personal",
    credential: PlaintextCredential | EnvCredential | None = None,
) -> AccountConfig:
    return AccountConfig(
        name=name,
        url="https://caldav.example.com/dav/",
        username="user@example.com",
        credential=credential or PlaintextCredential(password="s3cret"),
        mirror_path=Path("/unused"),
        trash_retention_days=30,
        include=(re.compile(".*"),),
        exclude=(),
        read_only=(),
    )


def _config(*accounts: AccountConfig) -> AppConfig:
    return AppConfig(
        config_version=1,
        use_utf8=False,
        editor=None,
        accounts=tuple(accounts),
    )


def _event(ref: ComponentRef, raw_ics: bytes) -> VEvent:
    return VEvent(
        ref=ref,
        href="/dav/x.ics",
        etag="etag-1",
        raw_ics=raw_ics,
        summary="Fixture",
        description=None,
        location=None,
        dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
        dtend=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        status=None,
        local_flags=frozenset(),
        server_flags=frozenset(),
        local_status=LocalStatus.ACTIVE,
        trashed_at=None,
        synced_at=None,
    )


class DoctorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.mirror = VdirMirrorRepository(tmp / "mirror")
        self.index = SqliteIndexRepository(tmp / "index.sqlite3")
        self.addCleanup(self.index.close)
        self.creds = DefaultCredentialsProvider(env={})


class CredentialsCheckTest(DoctorTestCase):
    def test_ok_when_credentials_resolve(self) -> None:
        report = run_doctor(
            config=_config(_account()),
            mirror=self.mirror,
            index=self.index,
            creds=self.creds,
        )
        creds_results = [r for r in report.results if r.check == "credentials"]
        self.assertEqual(len(creds_results), 1)
        self.assertEqual(creds_results[0].status, DiagnosticStatus.OK)
        self.assertEqual(report.exit_code, 0)

    def test_fail_when_env_var_missing(self) -> None:
        account = _account(credential=EnvCredential(variable="NOT_SET"))
        report = run_doctor(
            config=_config(account),
            mirror=self.mirror,
            index=self.index,
            creds=self.creds,
        )
        self.assertEqual(len(report.failed), 1)
        self.assertEqual(report.failed[0].check, "credentials")
        self.assertEqual(report.exit_code, 1)


class MirrorIntegrityTest(DoctorTestCase):
    def _seed(self, account: str, calendar: str, uid: str, raw: bytes) -> ResourceRef:
        ref = ResourceRef(account, calendar, uid)
        self.mirror.write(ref, raw)
        return ref

    def test_ok_when_mirror_and_index_match(self) -> None:
        uid = "simple-event-1@example.com"
        ics = corpus.simple_event()
        self._seed("personal", "work", uid, ics)
        self.index.upsert_component(_event(ComponentRef("personal", "work", uid), ics))
        report = run_doctor(
            config=_config(_account()),
            mirror=self.mirror,
            index=self.index,
            creds=self.creds,
        )
        mirror_results = [r for r in report.results if r.check == "mirror-integrity"]
        self.assertTrue(mirror_results)
        self.assertTrue(all(r.status == DiagnosticStatus.OK for r in mirror_results))

    def test_warn_on_orphan_mirror_file(self) -> None:
        self._seed("personal", "work", "ghost@example.com", corpus.simple_event())
        report = run_doctor(
            config=_config(_account()),
            mirror=self.mirror,
            index=self.index,
            creds=self.creds,
        )
        self.assertTrue(report.warnings)
        self.assertIn("ghost@example.com", report.warnings[0].message)

    def test_fail_on_unparseable_mirror_file(self) -> None:
        self._seed("personal", "work", "junk@example.com", b"not an iCalendar")
        report = run_doctor(
            config=_config(_account()),
            mirror=self.mirror,
            index=self.index,
            creds=self.creds,
        )
        self.assertTrue(report.failed)
        self.assertIn(
            "unparseable",
            " ".join(r.message for r in report.failed),
        )

    def test_fail_on_missing_mirror_file_for_indexed_component(self) -> None:
        # Add an index row pointing at an href but DO NOT seed mirror.
        self.index.upsert_component(
            _event(
                ComponentRef("personal", "work", "orphan-row@example.com"),
                corpus.simple_event(),
            )
        )
        # The calendar needs to exist in the mirror for list_calendars to see
        # it; seed a different UID to make "work" visible.
        self._seed("personal", "work", "present@example.com", corpus.simple_event())
        report = run_doctor(
            config=_config(_account()),
            mirror=self.mirror,
            index=self.index,
            creds=self.creds,
        )
        self.assertTrue(report.failed)
        self.assertTrue(any("missing mirror files" in r.message for r in report.failed))


class FormatReportTest(unittest.TestCase):
    def test_empty_report_friendly_message(self) -> None:
        from chronos.services import DoctorReport

        rendered = format_report(DoctorReport(results=()))
        self.assertIn("No accounts configured", rendered)
