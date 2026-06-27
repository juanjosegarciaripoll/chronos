from __future__ import annotations

import re
import tempfile
import unittest
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from chronos.authorization import Authorization
from chronos.caldav import CalDAVError
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
from tests.fake_caldav import FakeCalDAVSession


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


class RemoteCalDAVCheckTest(DoctorTestCase):
    def test_reports_remote_calendar_resource_counts(self) -> None:
        session = FakeCalDAVSession()
        session.add_calendar(url="https://cal.example.com/work/", name="work")
        session.put_resource(
            calendar_url="https://cal.example.com/work/",
            href="https://cal.example.com/work/a.ics",
            ics=corpus.simple_event(),
            etag="etag-a",
        )

        def factory(_account: AccountConfig, _auth: Authorization) -> FakeCalDAVSession:
            return session

        report = run_doctor(
            config=_config(_account()),
            mirror=self.mirror,
            index=self.index,
            creds=self.creds,
            session_factory=factory,
        )

        remote = [r for r in report.results if r.check == "remote-calendar-query"]
        self.assertEqual(len(remote), 1)
        self.assertEqual(remote[0].status, DiagnosticStatus.OK)
        self.assertIn("1 resource", remote[0].message)
        self.assertIn("0 row(s): 0 event(s), 0 task(s)", remote[0].message)

        sample = [r for r in report.results if r.check == "remote-multiget-sample"]
        self.assertEqual(len(sample), 1)
        self.assertEqual(sample[0].status, DiagnosticStatus.OK)
        self.assertIn("multiget returned 1 body", sample[0].message)
        self.assertIn("parsed 1 event component", sample[0].message)

    def test_progress_callback_reports_each_step_in_order(self) -> None:
        session = FakeCalDAVSession()
        session.add_calendar(url="https://cal.example.com/work/", name="work")
        session.put_resource(
            calendar_url="https://cal.example.com/work/",
            href="https://cal.example.com/work/a.ics",
            ics=corpus.simple_event(),
            etag="etag-a",
        )

        def factory(_account: AccountConfig, _auth: Authorization) -> FakeCalDAVSession:
            return session

        steps: list[str] = []
        run_doctor(
            config=_config(_account()),
            mirror=self.mirror,
            index=self.index,
            creds=self.creds,
            session_factory=factory,
            progress=steps.append,
        )

        joined = "\n".join(steps)
        self.assertIn("resolving credentials", joined)
        self.assertIn("discovering principal", joined)
        self.assertIn("listing calendars", joined)
        self.assertIn("calendar-query", joined)
        self.assertIn("multiget sample", joined)
        self.assertEqual(steps[-1], "personal: remote probe complete")

    def test_progress_defaults_to_noop_when_omitted(self) -> None:
        session = FakeCalDAVSession()
        session.add_calendar(url="https://cal.example.com/work/", name="work")

        def factory(_account: AccountConfig, _auth: Authorization) -> FakeCalDAVSession:
            return session

        # No progress argument: must run without raising.
        report = run_doctor(
            config=_config(_account()),
            mirror=self.mirror,
            index=self.index,
            creds=self.creds,
            session_factory=factory,
        )
        self.assertTrue(report.results)

    def test_warns_when_authorized_calendar_query_is_empty(self) -> None:
        session = FakeCalDAVSession()
        session.add_calendar(url="https://cal.example.com/work/", name="work")

        def factory(_account: AccountConfig, _auth: Authorization) -> FakeCalDAVSession:
            return session

        report = run_doctor(
            config=_config(_account()),
            mirror=self.mirror,
            index=self.index,
            creds=self.creds,
            session_factory=factory,
        )

        remote = [r for r in report.results if r.check == "remote-calendar-query"]
        self.assertEqual(remote[0].status, DiagnosticStatus.WARN)
        self.assertIn("0 resource", remote[0].message)
        self.assertFalse(
            [r for r in report.results if r.check == "remote-multiget-sample"]
        )

    def test_warns_when_multiget_sample_returns_no_bodies(self) -> None:
        class EmptyMultigetSession(FakeCalDAVSession):
            def calendar_multiget(
                self, calendar_url: str, hrefs: Sequence[str]
            ) -> Sequence[tuple[str, str, bytes]]:
                self.calls.append(("calendar_multiget", calendar_url, tuple(hrefs)))
                return ()

        session = EmptyMultigetSession()
        session.add_calendar(url="https://cal.example.com/work/", name="work")
        session.put_resource(
            calendar_url="https://cal.example.com/work/",
            href="https://cal.example.com/work/a.ics",
            ics=corpus.simple_event(),
            etag="etag-a",
        )

        def factory(
            _account: AccountConfig, _auth: Authorization
        ) -> EmptyMultigetSession:
            return session

        report = run_doctor(
            config=_config(_account()),
            mirror=self.mirror,
            index=self.index,
            creds=self.creds,
            session_factory=factory,
        )

        sample = next(r for r in report.results if r.check == "remote-multiget-sample")
        self.assertEqual(sample.status, DiagnosticStatus.WARN)
        self.assertIn("sampled 1 of 1 resource", sample.message)
        self.assertIn("multiget returned 0 body", sample.message)

    def test_warns_when_multiget_sample_has_malformed_ics(self) -> None:
        session = FakeCalDAVSession()
        session.add_calendar(url="https://cal.example.com/work/", name="work")
        session.put_resource(
            calendar_url="https://cal.example.com/work/",
            href="https://cal.example.com/work/a.ics",
            ics=b"not an ics file",
            etag="etag-a",
        )

        def factory(_account: AccountConfig, _auth: Authorization) -> FakeCalDAVSession:
            return session

        report = run_doctor(
            config=_config(_account()),
            mirror=self.mirror,
            index=self.index,
            creds=self.creds,
            session_factory=factory,
        )

        sample = next(r for r in report.results if r.check == "remote-multiget-sample")
        self.assertEqual(sample.status, DiagnosticStatus.WARN)
        self.assertIn("0 event component", sample.message)
        self.assertIn("1 parse error", sample.message)
        self.assertNotIn("not an ics file", sample.message)

    def test_reports_multiget_failure_without_leaking_url_or_token(self) -> None:
        class BrokenMultigetSession(FakeCalDAVSession):
            def calendar_multiget(
                self, calendar_url: str, _hrefs: Sequence[str]
            ) -> Sequence[tuple[str, str, bytes]]:
                raise CalDAVError(f"REPORT {calendar_url}?token=secret-token: HTTP 500")

        session = BrokenMultigetSession()
        session.add_calendar(url="https://cal.example.com/work/", name="work")
        session.put_resource(
            calendar_url="https://cal.example.com/work/",
            href="https://cal.example.com/work/a.ics",
            ics=corpus.simple_event(),
            etag="etag-a",
        )

        def factory(
            _account: AccountConfig, _auth: Authorization
        ) -> BrokenMultigetSession:
            return session

        report = run_doctor(
            config=_config(_account()),
            mirror=self.mirror,
            index=self.index,
            creds=self.creds,
            session_factory=factory,
        )

        sample = next(r for r in report.results if r.check == "remote-multiget-sample")
        self.assertEqual(sample.status, DiagnosticStatus.FAIL)
        self.assertIn("calendar-multiget failed", sample.message)
        self.assertNotIn("secret-token", sample.message)
        self.assertNotIn("https://cal.example.com", sample.message)

    def test_redacts_url_email_and_token_like_values_from_remote_errors(self) -> None:
        class BrokenSession(FakeCalDAVSession):
            def discover_principal(self) -> str:
                raise CalDAVError(
                    "Authorization: Bearer bearer-secret "
                    "token=secret-token "
                    "https://cal.example.com/users/alice@example.com/"
                )

        def factory(_account: AccountConfig, _auth: Authorization) -> BrokenSession:
            return BrokenSession()

        report = run_doctor(
            config=_config(_account()),
            mirror=self.mirror,
            index=self.index,
            creds=self.creds,
            session_factory=factory,
        )

        message = next(r for r in report.results if r.check == "remote-caldav").message
        self.assertNotIn("bearer-secret", message)
        self.assertNotIn("secret-token", message)
        self.assertNotIn("alice@example.com", message)
        self.assertNotIn("https://cal.example.com", message)
        self.assertIn("<redacted-url:", message)

    def test_fails_when_credential_cannot_be_resolved(self) -> None:
        account = _account(credential=EnvCredential(variable="NOT_SET"))
        called = False

        def factory(_account: AccountConfig, _auth: Authorization) -> FakeCalDAVSession:
            nonlocal called
            called = True
            return FakeCalDAVSession()

        report = run_doctor(
            config=_config(account),
            mirror=self.mirror,
            index=self.index,
            creds=self.creds,
            session_factory=factory,
        )

        remote = next(r for r in report.results if r.check == "remote-caldav")
        self.assertEqual(remote.status, DiagnosticStatus.FAIL)
        self.assertIn("credential unresolved", remote.message)
        self.assertFalse(called, "factory must not run when credentials are unresolved")

    def test_reports_calendar_query_failure_per_calendar(self) -> None:
        class QueryBrokenSession(FakeCalDAVSession):
            def calendar_query(self, calendar_url: str) -> Sequence[tuple[str, str]]:
                raise CalDAVError(f"REPORT {calendar_url}: HTTP 500")

        session = QueryBrokenSession()
        session.add_calendar(url="https://cal.example.com/work/", name="work")

        def factory(
            _account: AccountConfig, _auth: Authorization
        ) -> QueryBrokenSession:
            return session

        report = run_doctor(
            config=_config(_account()),
            mirror=self.mirror,
            index=self.index,
            creds=self.creds,
            session_factory=factory,
        )

        remote = next(r for r in report.results if r.check == "remote-calendar-query")
        self.assertEqual(remote.status, DiagnosticStatus.FAIL)
        self.assertIn("calendar-query failed", remote.message)
        self.assertIn("CalDAVError", remote.message)

    def test_invokes_auth_on_commit_after_successful_probe(self) -> None:
        committed = False

        def on_commit() -> None:
            nonlocal committed
            committed = True

        class CommittingCreds:
            def build_auth(self, _account: AccountConfig) -> Authorization:
                return Authorization(basic=("user", "pw"), on_commit=on_commit)

        session = FakeCalDAVSession()
        session.add_calendar(url="https://cal.example.com/work/", name="work")

        def factory(_account: AccountConfig, _auth: Authorization) -> FakeCalDAVSession:
            return session

        run_doctor(
            config=_config(_account()),
            mirror=self.mirror,
            index=self.index,
            creds=CommittingCreds(),
            session_factory=factory,
        )

        self.assertTrue(committed, "on_commit must run after a successful probe")


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
