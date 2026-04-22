from __future__ import annotations

import io
import re
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from chronos import cli
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
from chronos.storage import VdirMirrorRepository
from tests import corpus
from tests.fake_caldav import FakeCalDAVSession

NOW = datetime(2026, 4, 22, 12, 0, tzinfo=UTC)


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
        accounts=tuple(accounts) or (_account(),),
    )


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.mirror = VdirMirrorRepository(tmp / "mirror")
        self.index = SqliteIndexRepository(tmp / "index.sqlite3")
        self.addCleanup(self.index.close)
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.session = FakeCalDAVSession()

    def _ctx(
        self,
        *,
        config: AppConfig | None = None,
        session_factory: cli.SessionFactory | None = None,
        creds_env: dict[str, str] | None = None,
    ) -> cli.CliContext:
        return cli.CliContext(
            config=config or _config(),
            mirror=self.mirror,
            index=self.index,
            creds=DefaultCredentialsProvider(env=creds_env or {}),
            stdout=self.stdout,
            stderr=self.stderr,
            now=NOW,
            session_factory=session_factory,
        )

    def _run(
        self,
        argv: list[str],
        *,
        context: cli.CliContext | None = None,
    ) -> int:
        ctx = context or self._ctx()
        return cli.main(argv, context_factory=lambda _: ctx)


class ParserTest(CliTestCase):
    def test_missing_subcommand_exits_nonzero(self) -> None:
        with self.assertRaises(SystemExit):
            cli.main([])

    def test_unknown_subcommand_exits_nonzero(self) -> None:
        with self.assertRaises(SystemExit):
            cli.main(["nope"])


class SyncCommandTest(CliTestCase):
    def _seed_server(self) -> None:
        self.session.add_calendar(url="https://cal.example.com/work/", name="work")
        self.session.put_resource(
            calendar_url="https://cal.example.com/work/",
            href="https://cal.example.com/work/a.ics",
            ics=corpus.simple_event(),
            etag="etag-a",
        )

    def test_sync_happy_path(self) -> None:
        self._seed_server()

        def factory(_account: AccountConfig, _password: str) -> FakeCalDAVSession:
            return self.session

        ctx = self._ctx(session_factory=factory)
        code = self._run(["sync"], context=ctx)
        self.assertEqual(code, 0)
        self.assertIn("personal:", self.stdout.getvalue())
        self.assertIn("+1", self.stdout.getvalue())

    def test_sync_reports_credential_failure(self) -> None:
        bad_config = _config(
            _account(credential=EnvCredential(variable="UNSET_FOR_TEST"))
        )
        ctx = self._ctx(config=bad_config, creds_env={})
        code = self._run(["sync"], context=ctx)
        self.assertEqual(code, 1)
        self.assertIn("UNSET_FOR_TEST", self.stderr.getvalue())

    def test_sync_without_factory_reports_deferred_http(self) -> None:
        ctx = self._ctx(session_factory=None)
        code = self._run(["sync"], context=ctx)
        self.assertEqual(code, 1)
        self.assertIn("deferred", self.stderr.getvalue())


class ListCommandTest(CliTestCase):
    def _seed_index(self, uid: str, *, summary: str = "Meeting") -> None:
        ref = ComponentRef("personal", "work", uid)
        event = VEvent(
            ref=ref,
            href="/dav/x.ics",
            etag="e",
            raw_ics=corpus.simple_event(),
            summary=summary,
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
        self.mirror.write(ref.resource, corpus.simple_event())
        self.index.upsert_component(event)

    def test_list_shows_seeded_events(self) -> None:
        self._seed_index("a@example.com", summary="Alpha")
        self._seed_index("b@example.com", summary="Beta")
        code = self._run(["list"])
        self.assertEqual(code, 0)
        output = self.stdout.getvalue()
        self.assertIn("Alpha", output)
        self.assertIn("Beta", output)
        self.assertIn("EVENT", output)

    def test_list_respects_calendar_filter(self) -> None:
        self._seed_index("a@example.com")
        code = self._run(["list", "--calendar", "nonexistent"])
        self.assertEqual(code, 0)
        self.assertEqual(self.stdout.getvalue(), "")

    def test_list_hides_trashed(self) -> None:
        self._seed_index("a@example.com", summary="Alpha")
        current = self.index.get_component(
            ComponentRef("personal", "work", "a@example.com")
        )
        assert current is not None
        assert isinstance(current, VEvent)
        trashed = VEvent(
            ref=current.ref,
            href=current.href,
            etag=current.etag,
            raw_ics=current.raw_ics,
            summary=current.summary,
            description=current.description,
            location=current.location,
            dtstart=current.dtstart,
            dtend=current.dtend,
            status=current.status,
            local_flags=current.local_flags,
            server_flags=current.server_flags,
            local_status=LocalStatus.TRASHED,
            trashed_at=NOW,
            synced_at=current.synced_at,
        )
        self.index.upsert_component(trashed)
        code = self._run(["list"])
        self.assertEqual(code, 0)
        self.assertNotIn("Alpha", self.stdout.getvalue())


class ShowCommandTest(CliTestCase):
    def test_not_found_returns_one(self) -> None:
        code = self._run(["show", "nope@example.com"])
        self.assertEqual(code, 1)
        self.assertIn("not found", self.stderr.getvalue())

    def test_renders_details(self) -> None:
        ref = ResourceRef("personal", "work", "ev@example.com")
        self.mirror.write(ref, corpus.simple_event())
        self.index.upsert_component(
            VEvent(
                ref=ComponentRef("personal", "work", "ev@example.com"),
                href="/dav/ev.ics",
                etag="etag-1",
                raw_ics=corpus.simple_event(),
                summary="Staff review",
                description="With pastries",
                location="Conference room",
                dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
                dtend=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
                status=None,
                local_flags=frozenset(),
                server_flags=frozenset(),
                local_status=LocalStatus.ACTIVE,
                trashed_at=None,
                synced_at=None,
            )
        )
        code = self._run(["show", "ev@example.com"])
        self.assertEqual(code, 0)
        output = self.stdout.getvalue()
        self.assertIn("Staff review", output)
        self.assertIn("With pastries", output)
        self.assertIn("Conference room", output)
        self.assertIn("ev@example.com", output)


class AddCommandTest(CliTestCase):
    def test_add_writes_mirror_and_index_with_href_null(self) -> None:
        code = self._run(
            [
                "add",
                "--account",
                "personal",
                "--calendar",
                "work",
                "--summary",
                "Lunch",
                "--start",
                "2026-05-01T12:00:00+00:00",
                "--end",
                "2026-05-01T13:00:00+00:00",
                "--uid",
                "lunch@example.com",
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("lunch@example.com", self.stdout.getvalue())
        # Mirror populated.
        self.assertTrue(
            self.mirror.exists(ResourceRef("personal", "work", "lunch@example.com"))
        )
        # Index row pending (href IS NULL).
        component = self.index.get_component(
            ComponentRef("personal", "work", "lunch@example.com")
        )
        assert component is not None
        self.assertIsNone(component.href)
        self.assertEqual(component.summary, "Lunch")

    def test_add_rejects_unknown_account(self) -> None:
        code = self._run(
            [
                "add",
                "--account",
                "nope",
                "--calendar",
                "work",
                "--summary",
                "X",
                "--start",
                "2026-05-01T12:00:00+00:00",
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn("unknown account", self.stderr.getvalue())

    def test_add_without_uid_generates_stable_uid(self) -> None:
        code = self._run(
            [
                "add",
                "--account",
                "personal",
                "--calendar",
                "work",
                "--summary",
                "Auto",
                "--start",
                "2026-05-01T12:00:00+00:00",
            ]
        )
        self.assertEqual(code, 0)
        uid = self.stdout.getvalue().strip()
        self.assertTrue(uid.endswith("@chronos"))
        self.assertTrue(self.mirror.exists(ResourceRef("personal", "work", uid)))


class EditCommandTest(CliTestCase):
    def test_edit_updates_summary(self) -> None:
        self._run(
            [
                "add",
                "--account",
                "personal",
                "--calendar",
                "work",
                "--summary",
                "Old",
                "--start",
                "2026-05-01T12:00:00+00:00",
                "--uid",
                "ed@example.com",
            ]
        )
        self.stdout.truncate(0)
        self.stdout.seek(0)
        code = self._run(["edit", "ed@example.com", "--summary", "New"])
        self.assertEqual(code, 0)
        component = self.index.get_component(
            ComponentRef("personal", "work", "ed@example.com")
        )
        assert component is not None
        self.assertEqual(component.summary, "New")

    def test_edit_not_found(self) -> None:
        code = self._run(["edit", "missing@example.com", "--summary", "X"])
        self.assertEqual(code, 1)
        self.assertIn("not found", self.stderr.getvalue())


class RmCommandTest(CliTestCase):
    def test_rm_marks_trashed(self) -> None:
        self._run(
            [
                "add",
                "--account",
                "personal",
                "--calendar",
                "work",
                "--summary",
                "Gone",
                "--start",
                "2026-05-01T12:00:00+00:00",
                "--uid",
                "gone@example.com",
            ]
        )
        code = self._run(["rm", "gone@example.com"])
        self.assertEqual(code, 0)
        self.assertIn("trashed 1", self.stdout.getvalue())
        component = self.index.get_component(
            ComponentRef("personal", "work", "gone@example.com")
        )
        assert component is not None
        self.assertEqual(component.local_status, LocalStatus.TRASHED)
        self.assertEqual(component.trashed_at, NOW)

    def test_rm_not_found(self) -> None:
        code = self._run(["rm", "missing@example.com"])
        self.assertEqual(code, 1)


class DoctorCommandTest(CliTestCase):
    def test_doctor_reports_ok_on_clean_state(self) -> None:
        code = self._run(["doctor"])
        self.assertEqual(code, 0)
        self.assertIn("credentials", self.stdout.getvalue())
        self.assertIn("OK", self.stdout.getvalue())

    def test_doctor_surfaces_unparseable_mirror(self) -> None:
        self.mirror.write(
            ResourceRef("personal", "work", "junk@example.com"),
            b"totally not ical",
        )
        code = self._run(["doctor"])
        self.assertEqual(code, 1)
        self.assertIn("FAIL", self.stdout.getvalue())
        self.assertIn("unparseable", self.stdout.getvalue())


class ConfigLoadErrorTest(unittest.TestCase):
    def test_missing_config_exits_two(self) -> None:
        stderr = io.StringIO()
        import sys

        original = sys.stderr
        sys.stderr = stderr
        try:
            code = cli.main(
                ["--config", "/nowhere/does/not/exist.toml", "doctor"],
            )
        finally:
            sys.stderr = original
        self.assertEqual(code, 2)
        self.assertIn("config error", stderr.getvalue())
