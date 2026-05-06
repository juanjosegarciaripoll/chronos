from __future__ import annotations

import contextlib
import io
import re
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

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
    def test_no_subcommand_defaults_to_tui(self) -> None:
        # chronos with no args defaults to the tui command; without a
        # config it returns 2 (missing-config path), not a parser error.
        result = cli.main([])
        self.assertEqual(result, 2)

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
        from chronos.authorization import Authorization

        def factory(_account: AccountConfig, _auth: Authorization) -> FakeCalDAVSession:
            return self.session

        ctx = self._ctx(session_factory=factory)
        code = self._run(["sync"], context=ctx)
        self.assertEqual(code, 0)
        self.assertIn("personal:", self.stdout.getvalue())
        self.assertIn("+1", self.stdout.getvalue())

    def test_reset_deletes_index_and_mirror_with_yes_flag(self) -> None:
        # Seed the mirror + index so there's something to delete.
        self._seed_server()
        from chronos.authorization import Authorization

        def factory(_account: AccountConfig, _auth: Authorization) -> FakeCalDAVSession:
            return self.session

        ctx = self._ctx(session_factory=factory)
        # First sync populates everything.
        self.assertEqual(self._run(["sync"], context=ctx), 0)
        # Sanity: data is on disk.
        self.assertTrue(self.index._path.exists())  # type: ignore[attr-defined]

        index_path = self.index._path  # type: ignore[attr-defined]
        # `VdirMirrorRepository` stores its root on `_root` (the
        # constructor's `tmp / "mirror"`).
        mirror_dir = self.mirror._root  # type: ignore[attr-defined]

        code = cli.cmd_reset(
            ctx, yes=True, index_path=index_path, mirror_dir=mirror_dir
        )
        self.assertEqual(code, 0)
        self.assertFalse(index_path.exists())
        self.assertFalse(mirror_dir.exists())
        self.assertIn("Reset complete", self.stdout.getvalue())

    def test_reset_is_a_noop_when_nothing_exists(self) -> None:
        # No prior sync — index file doesn't exist on disk, mirror
        # dir doesn't exist either. Reset should print a friendly
        # "nothing to do" line and return 0.
        ctx = self._ctx()
        # Close + unlink so the file genuinely isn't there.
        ctx.index.close()
        index_path = self.index._path  # type: ignore[attr-defined]
        if index_path.exists():
            index_path.unlink()
        mirror_dir = self.mirror._root  # type: ignore[attr-defined]
        if mirror_dir.exists():
            import shutil as _sh

            _sh.rmtree(mirror_dir)

        code = cli.cmd_reset(
            ctx, yes=True, index_path=index_path, mirror_dir=mirror_dir
        )
        self.assertEqual(code, 0)
        self.assertIn("Nothing to reset", self.stdout.getvalue())

    def test_reset_refuses_when_mcp_server_reachable(self) -> None:
        import socket

        from chronos.mcp_server import McpServerState, write_state

        self._seed_server()

        def factory(_account: AccountConfig, _auth: Any) -> FakeCalDAVSession:
            return self.session

        ctx = self._ctx(session_factory=factory)
        self.assertEqual(self._run(["sync"], context=ctx), 0)

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            state_file = (
                Path(self.enterContext(tempfile.TemporaryDirectory())) / "mcp_server.json"
            )
            write_state(state_file, McpServerState(port=port, token="tok"))
            index_path = self.index._path  # type: ignore[attr-defined]
            mirror_dir = self.mirror._root  # type: ignore[attr-defined]
            code = cli.cmd_reset(
                ctx,
                yes=True,
                index_path=index_path,
                mirror_dir=mirror_dir,
                mcp_state_file=state_file,
            )
        finally:
            srv.close()

        self.assertEqual(code, 2)
        self.assertIn("TUI", self.stderr.getvalue())
        self.assertIn(str(port), self.stderr.getvalue())
        self.assertTrue(index_path.exists())

    def test_reset_refuses_when_sync_lock_held(self) -> None:
        from chronos.locking import SyncLockError

        self._seed_server()

        def factory(_account: AccountConfig, _auth: Any) -> FakeCalDAVSession:
            return self.session

        ctx = self._ctx(session_factory=factory)
        self.assertEqual(self._run(["sync"], context=ctx), 0)

        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        index_path = self.index._path  # type: ignore[attr-defined]
        mirror_dir = self.mirror._root  # type: ignore[attr-defined]

        @contextlib.contextmanager
        def _contended(_path: Path):  # type: ignore[return]
            raise SyncLockError("another chronos sync is already running (pid=1234)")
            yield  # noqa: unreachable

        with mock.patch("chronos.cli.acquire_sync_lock", new=_contended):
            code = cli.cmd_reset(
                ctx,
                yes=True,
                index_path=index_path,
                mirror_dir=mirror_dir,
                mcp_state_file=tmp / "mcp_server.json",  # doesn't exist → no TUI
            )

        self.assertEqual(code, 2)
        self.assertIn("refusing to reset", self.stderr.getvalue())
        self.assertIn("1234", self.stderr.getvalue())
        self.assertTrue(index_path.exists())

    def test_reset_force_bypasses_presence_checks(self) -> None:
        import socket

        from chronos.mcp_server import McpServerState, write_state

        self._seed_server()

        def factory(_account: AccountConfig, _auth: Any) -> FakeCalDAVSession:
            return self.session

        ctx = self._ctx(session_factory=factory)
        self.assertEqual(self._run(["sync"], context=ctx), 0)

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            state_file = (
                Path(self.enterContext(tempfile.TemporaryDirectory())) / "mcp_server.json"
            )
            write_state(state_file, McpServerState(port=port, token="tok"))
            index_path = self.index._path  # type: ignore[attr-defined]
            mirror_dir = self.mirror._root  # type: ignore[attr-defined]
            code = cli.cmd_reset(
                ctx,
                yes=True,
                force=True,
                index_path=index_path,
                mirror_dir=mirror_dir,
                mcp_state_file=state_file,
            )
        finally:
            srv.close()

        self.assertEqual(code, 0)
        self.assertFalse(index_path.exists())
        self.assertIn("Reset complete", self.stdout.getvalue())

    def test_reset_proceeds_when_state_file_stale(self) -> None:
        import socket

        from chronos.mcp_server import McpServerState, write_state

        self._seed_server()

        def factory(_account: AccountConfig, _auth: Any) -> FakeCalDAVSession:
            return self.session

        ctx = self._ctx(session_factory=factory)
        self.assertEqual(self._run(["sync"], context=ctx), 0)

        # Bind, capture port, then immediately close so nothing is listening.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        state_file = tmp / "mcp_server.json"
        write_state(state_file, McpServerState(port=port, token="tok"))

        index_path = self.index._path  # type: ignore[attr-defined]
        mirror_dir = self.mirror._root  # type: ignore[attr-defined]
        code = cli.cmd_reset(
            ctx,
            yes=True,
            index_path=index_path,
            mirror_dir=mirror_dir,
            mcp_state_file=state_file,
            lock_path=tmp / "sync.lock",
        )

        self.assertEqual(code, 0)
        self.assertFalse(index_path.exists())
        self.assertFalse(state_file.exists())

    def test_reset_refuses_without_yes_when_non_interactive(self) -> None:
        self._seed_server()
        from chronos.authorization import Authorization

        def factory(_account: AccountConfig, _auth: Authorization) -> FakeCalDAVSession:
            return self.session

        ctx = self._ctx(session_factory=factory)
        self._run(["sync"], context=ctx)
        index_path = self.index._path  # type: ignore[attr-defined]
        mirror_dir = self.mirror._root  # type: ignore[attr-defined]

        # Tests run with stdin/stdout not attached to a TTY, so the
        # confirmation path takes the "refuse non-interactively"
        # branch.
        code = cli.cmd_reset(
            ctx, yes=False, index_path=index_path, mirror_dir=mirror_dir
        )
        self.assertEqual(code, 1)
        self.assertIn("Refusing to reset non-interactively", self.stderr.getvalue())
        # Files are still there.
        self.assertTrue(index_path.exists())
        self.assertTrue(mirror_dir.exists())

    def test_sync_force_clears_sync_state_and_reruns_slow_path(self) -> None:
        # `chronos sync --force` is the user-facing escape hatch when
        # the local cache has drifted out of step with the components
        # table. It clears every per-calendar CTag so the next run
        # re-enters the slow path for every calendar (which, on its
        # tail, calls `populate_occurrences` to rebuild the cache).
        self._seed_server()
        from chronos.authorization import Authorization
        from chronos.domain import CalendarRef, SyncState

        def factory(_account: AccountConfig, _auth: Authorization) -> FakeCalDAVSession:
            return self.session

        ctx = self._ctx(session_factory=factory)
        # Pre-seed a sync-state row so we can prove --force wipes it.
        ctx.index.set_sync_state(
            SyncState(
                calendar=CalendarRef(account_name="personal", calendar_name="work"),
                ctag="ctag-stale",
                sync_token=None,
                synced_at=ctx.now,
            )
        )

        code = self._run(["sync", "--force"], context=ctx)
        self.assertEqual(code, 0)
        # The pre-seeded row was wiped, then the actual sync wrote a
        # fresh state with the server's current CTag.
        new_state = ctx.index.get_sync_state(
            CalendarRef(account_name="personal", calendar_name="work")
        )
        assert new_state is not None
        self.assertNotEqual(new_state.ctag, "ctag-stale")
        # Confirmation line on stdout so the user sees the wipe.
        self.assertIn("--force", self.stdout.getvalue())
        self.assertIn("cleared sync state", self.stdout.getvalue())

    def test_sync_reports_credential_failure(self) -> None:
        bad_config = _config(
            _account(credential=EnvCredential(variable="UNSET_FOR_TEST"))
        )
        ctx = self._ctx(config=bad_config, creds_env={})
        code = self._run(["sync"], context=ctx)
        self.assertEqual(code, 1)
        self.assertIn("UNSET_FOR_TEST", self.stderr.getvalue())

    def test_sync_without_factory_builds_caldav_http_session(self) -> None:
        from chronos.authorization import Authorization
        from chronos.caldav import CalDAVHttpSession
        from chronos.cli import _default_session_factory

        session = _default_session_factory(
            _account(), Authorization(basic=("user@example.com", "pw"))
        )
        self.assertIsInstance(session, CalDAVHttpSession)

    def test_sync_reports_caldav_error_from_session(self) -> None:
        from chronos.authorization import Authorization
        from chronos.caldav import CalDAVError

        def broken_factory(
            _account: AccountConfig, _auth: Authorization
        ) -> FakeCalDAVSession:
            session = FakeCalDAVSession()

            def _raise_principal() -> str:
                raise CalDAVError("simulated network failure")

            session.discover_principal = _raise_principal  # type: ignore[method-assign]
            return session

        ctx = self._ctx(session_factory=broken_factory)
        code = self._run(["sync"], context=ctx)
        self.assertEqual(code, 1)
        self.assertIn("CalDAV error", self.stderr.getvalue())
        self.assertIn("simulated network failure", self.stderr.getvalue())

    def test_sync_catches_caldav_error_from_session_factory(self) -> None:
        """A CalDAVError raised by the session factory itself (e.g. Google
        email-discovery failing in the default factory) must be reported
        per-account, not crash the whole sync."""
        from chronos.authorization import Authorization
        from chronos.caldav import CalDAVError

        def broken_factory(
            _account: AccountConfig, _auth: Authorization
        ) -> FakeCalDAVSession:
            raise CalDAVError("session construction blew up")

        ctx = self._ctx(session_factory=broken_factory)
        code = self._run(["sync"], context=ctx)
        self.assertEqual(code, 1)
        self.assertIn("CalDAV error", self.stderr.getvalue())
        self.assertIn("session construction blew up", self.stderr.getvalue())


class SyncLockReleaseTest(CliTestCase):
    """The TUI runs sync synchronously on the UI thread. Any exception
    raised during the sync (including a Ctrl-C that bubbles up as
    KeyboardInterrupt) must release the sync lockfile so the next
    `chronos sync` invocation isn't blocked. The persistence layers
    (mirror temp-files, SQLite transactions, OAuth-token writes) are
    each tested for atomicity in their own modules; this test pins
    the additional lockfile-release guarantee at the cli boundary.
    """

    def _seed(self) -> None:
        self.session.add_calendar(url="https://cal.example.com/work/", name="work")
        self.session.put_resource(
            calendar_url="https://cal.example.com/work/",
            href="https://cal.example.com/work/a.ics",
            ics=corpus.simple_event(),
            etag="etag-a",
        )

    def test_keyboard_interrupt_during_runner_releases_lock(self) -> None:
        from chronos.authorization import Authorization

        self._seed()
        # Make discover_principal raise KeyboardInterrupt to simulate
        # a Ctrl-C landing mid-flight.
        flaky_session = FakeCalDAVSession()
        flaky_session.add_calendar(url="https://cal.example.com/work/", name="work")

        def boom() -> str:
            raise KeyboardInterrupt

        flaky_session.discover_principal = boom  # type: ignore[method-assign]

        def factory(_account: AccountConfig, _auth: Authorization) -> FakeCalDAVSession:
            return flaky_session

        ctx = self._ctx(session_factory=factory)
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        lock_path = tmp / "sync.lock"

        with mock.patch("chronos.cli.sync_lock_path", return_value=lock_path):
            runner = cli.build_sync_runner(ctx)
            with self.assertRaises(KeyboardInterrupt):
                runner()

            # Lock must be released — a fresh runner must be able to
            # acquire it without blocking or raising SyncLockError.
            ctx2 = self._ctx(session_factory=lambda *_: self.session)
            self._seed()  # idempotent
            runner2 = cli.build_sync_runner(ctx2)
            results = runner2()
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].errors, ())

    def test_concurrent_cmd_sync_invocations_rejected(self) -> None:
        # Hold the lockfile via a separate `acquire_sync_lock` and
        # verify `cmd_sync` exits non-zero with a "another chronos
        # sync is already running" message.
        from chronos.locking import acquire_sync_lock

        self._seed()
        ctx = self._ctx(session_factory=lambda *_: self.session)
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        lock_path = tmp / "sync.lock"

        with (
            mock.patch("chronos.cli.sync_lock_path", return_value=lock_path),
            acquire_sync_lock(lock_path),
        ):
            exit_code = cli.cmd_sync(ctx)

        self.assertEqual(exit_code, 2)
        self.assertIn("another chronos sync is already running", self.stderr.getvalue())


class BuildSyncRunnerTest(CliTestCase):
    """`build_sync_runner` is the closure handed to the TUI's `_run_sync`.

    It mirrors `cmd_sync`'s flow but returns per-account `SyncResult`s
    instead of writing to stdout, with errors folded into the result's
    `errors` tuple so a single bad account doesn't take the whole sync
    down.
    """

    def _seed_server(self) -> None:
        self.session.add_calendar(url="https://cal.example.com/work/", name="work")
        self.session.put_resource(
            calendar_url="https://cal.example.com/work/",
            href="https://cal.example.com/work/a.ics",
            ics=corpus.simple_event(),
            etag="etag-a",
        )

    def test_happy_path_returns_one_result_per_account(self) -> None:
        self._seed_server()
        from chronos.authorization import Authorization

        def factory(_account: AccountConfig, _auth: Authorization) -> FakeCalDAVSession:
            return self.session

        ctx = self._ctx(session_factory=factory)
        runner = cli.build_sync_runner(ctx)
        results = runner()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].account_name, "personal")
        self.assertEqual(results[0].errors, ())
        self.assertEqual(results[0].components_added, 1)

    def test_credential_failure_lands_in_errors(self) -> None:
        bad_config = _config(
            _account(credential=EnvCredential(variable="UNSET_FOR_TEST"))
        )
        ctx = self._ctx(config=bad_config, creds_env={})
        runner = cli.build_sync_runner(ctx)
        results = runner()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].errors[0].count("UNSET_FOR_TEST"), 1)
        self.assertEqual(results[0].components_added, 0)

    def test_caldav_error_lands_in_errors(self) -> None:
        from chronos.authorization import Authorization
        from chronos.caldav import CalDAVError

        def broken_factory(
            _account: AccountConfig, _auth: Authorization
        ) -> FakeCalDAVSession:
            session = FakeCalDAVSession()

            def _raise_principal() -> str:
                raise CalDAVError("simulated network failure")

            session.discover_principal = _raise_principal  # type: ignore[method-assign]
            return session

        ctx = self._ctx(session_factory=broken_factory)
        runner = cli.build_sync_runner(ctx)
        results = runner()
        self.assertEqual(len(results), 1)
        self.assertIn("simulated network failure", results[0].errors[0])

    def test_session_factory_not_implemented_lands_in_errors(self) -> None:
        from chronos.authorization import Authorization

        def unimplemented_factory(
            _account: AccountConfig, _auth: Authorization
        ) -> FakeCalDAVSession:
            raise NotImplementedError("session factory not configured")

        ctx = self._ctx(session_factory=unimplemented_factory)
        runner = cli.build_sync_runner(ctx)
        results = runner()
        self.assertEqual(len(results), 1)
        self.assertIn("session factory not configured", results[0].errors[0])

    def test_session_factory_caldav_error_lands_in_errors(self) -> None:
        """The default session factory may raise CalDAVError when
        constructing the session (e.g. Google email-discovery failing).
        That must land in the per-account errors tuple, not crash."""
        from chronos.authorization import Authorization
        from chronos.caldav import CalDAVError

        def broken_factory(
            _account: AccountConfig, _auth: Authorization
        ) -> FakeCalDAVSession:
            raise CalDAVError("session construction blew up")

        ctx = self._ctx(session_factory=broken_factory)
        runner = cli.build_sync_runner(ctx)
        results = runner()
        self.assertEqual(len(results), 1)
        self.assertIn("session construction blew up", results[0].errors[0])

    def test_oauth_on_commit_called_on_success(self) -> None:
        self._seed_server()

        from chronos.authorization import Authorization
        from chronos.credentials import CredentialResolutionError

        committed: list[int] = []

        class StubCreds:
            def build_auth(self, account: AccountConfig) -> Authorization:
                _ = account
                return Authorization(
                    basic=("user@example.com", "pw"),
                    on_commit=lambda: committed.append(1),
                )

        def factory(_account: AccountConfig, _auth: Authorization) -> FakeCalDAVSession:
            return self.session

        ctx = cli.CliContext(
            config=_config(),
            mirror=self.mirror,
            index=self.index,
            creds=StubCreds(),
            stdout=self.stdout,
            stderr=self.stderr,
            now=NOW,
            session_factory=factory,
        )
        runner = cli.build_sync_runner(ctx)
        runner()
        self.assertEqual(committed, [1])
        # Silence unused-import lint for the type-only import above.
        _ = CredentialResolutionError


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
    def test_missing_config_in_non_interactive_exits_two(self) -> None:
        stderr = io.StringIO()
        stdout = io.StringIO()
        code = cli.main(
            ["--config", "/nowhere/does/not/exist.toml", "doctor"],
            is_interactive=lambda: False,
            stdout=stdout,
            stderr=stderr,
        )
        self.assertEqual(code, 2)
        self.assertIn("config not found", stderr.getvalue())


class ConfigEditingCliTestCase(unittest.TestCase):
    """Harness for the config-editing subcommands.

    These commands don't need a mirror/index/session — just a config path
    and captured streams.
    """

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config_path = self.tmp / "config.toml"
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def _run(
        self,
        argv: list[str],
        *,
        open_editor: cli.EditorFn | None = None,
    ) -> int:
        return cli.main(
            ["--config", str(self.config_path), *argv],
            open_editor=open_editor,
            stdout=self.stdout,
            stderr=self.stderr,
        )


class InitCommandTest(ConfigEditingCliTestCase):
    def test_init_writes_template(self) -> None:
        code = self._run(["init"])
        self.assertEqual(code, 0)
        self.assertTrue(self.config_path.exists())
        self.assertIn("Wrote template", self.stdout.getvalue())
        # The template parses to zero accounts (every example is commented).
        from chronos.config import load

        config = load(self.config_path)
        self.assertEqual(config.config_version, 1)
        self.assertEqual(config.accounts, ())
        # And it carries the inline help so a curious user sees it.
        body = self.config_path.read_text(encoding="utf-8")
        self.assertIn("# # Basic auth", body)
        self.assertIn("# # Google Calendar via OAuth", body)
        self.assertIn("# # Generic OAuth 2.0", body)

    def test_init_refuses_when_file_exists(self) -> None:
        self.config_path.write_text("config_version = 1\n", encoding="utf-8")
        code = self._run(["init"])
        self.assertEqual(code, 1)
        self.assertIn("already exists", self.stderr.getvalue())


class FirstLaunchBootstrapTest(unittest.TestCase):
    """Running a data command without a config offers to bootstrap one.

    In a non-TTY environment we exit with a helpful message instead.
    """

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config_path = self.tmp / "config.toml"
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def _scripted_prompt(self, answers: list[str]) -> Any:
        iterator = iter(answers)

        def prompt(_message: str) -> str:
            return next(iterator)

        return prompt

    def test_non_interactive_prints_help_and_exits_2(self) -> None:
        code = cli.main(
            ["--config", str(self.config_path), "sync"],
            is_interactive=lambda: False,
            stdout=self.stdout,
            stderr=self.stderr,
        )
        self.assertEqual(code, 2)
        self.assertIn("config not found", self.stderr.getvalue())
        self.assertIn("chronos init", self.stderr.getvalue())
        self.assertFalse(self.config_path.exists())

    def test_interactive_user_creates_template_and_skips_editor(self) -> None:
        edits: list[Path] = []

        code = cli.main(
            ["--config", str(self.config_path), "tui"],
            is_interactive=lambda: True,
            prompt=self._scripted_prompt(["y", "n"]),  # create yes, edit no
            open_editor=lambda p: edits.append(p),
            stdout=self.stdout,
            stderr=self.stderr,
        )
        self.assertEqual(code, 0)
        self.assertTrue(self.config_path.exists())
        self.assertEqual(edits, [])
        self.assertIn("Wrote template", self.stdout.getvalue())

    def test_interactive_user_declines(self) -> None:
        code = cli.main(
            ["--config", str(self.config_path), "list"],
            is_interactive=lambda: True,
            prompt=self._scripted_prompt(["n"]),
            stdout=self.stdout,
            stderr=self.stderr,
        )
        self.assertEqual(code, 1)
        self.assertFalse(self.config_path.exists())
        self.assertIn("Skipped", self.stdout.getvalue())

    def test_init_command_works_without_prompt_in_missing_config_path(self) -> None:
        # `chronos init` itself is a config-editing command, so the
        # missing-config bootstrap flow MUST NOT trigger for it — that
        # would prompt to create a template before letting init create
        # one, which is silly. Verify by passing no prompt at all.
        code = cli.main(
            ["--config", str(self.config_path), "init"],
            stdout=self.stdout,
            stderr=self.stderr,
        )
        self.assertEqual(code, 0)
        self.assertTrue(self.config_path.exists())


class AccountAddCommandTest(ConfigEditingCliTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._run(["init"])
        self.stdout.truncate(0)
        self.stdout.seek(0)

    def test_add_plaintext_account(self) -> None:
        code = self._run(
            [
                "account",
                "add",
                "--name",
                "personal",
                "--url",
                "https://caldav.example.com/dav/",
                "--username",
                "user@example.com",
                "--credential-backend",
                "plaintext",
                "--credential-value",
                "s3cret",
                "--mirror-path",
                "/tmp/chronos/personal",
            ]
        )
        self.assertEqual(code, 0)
        from chronos.config import load

        config = load(self.config_path)
        self.assertEqual(len(config.accounts), 1)
        from chronos.domain import PlaintextCredential

        cred = config.accounts[0].credential
        assert isinstance(cred, PlaintextCredential)
        self.assertEqual(cred.password, "s3cret")

    def test_add_account_without_mirror_path_uses_default(self) -> None:
        from chronos.paths import default_mirror_path

        self._run(["init"])
        code = self._run(
            [
                "account",
                "add",
                "--name",
                "personal",
                "--url",
                "https://caldav.example.com/dav/",
                "--username",
                "user@example.com",
                "--credential-backend",
                "plaintext",
                "--credential-value",
                "s3cret",
            ]
        )
        self.assertEqual(code, 0)
        from chronos.config import load

        config = load(self.config_path)
        self.assertEqual(
            config.accounts[0].mirror_path, default_mirror_path("personal")
        )
        # The TOML on disk should NOT carry an explicit mirror_path line
        # (it round-trips through the default).
        body = self.config_path.read_text(encoding="utf-8")
        self.assertNotIn("mirror_path", body)

    def test_add_env_account(self) -> None:
        code = self._run(
            [
                "account",
                "add",
                "--name",
                "personal",
                "--url",
                "https://caldav.example.com/dav/",
                "--username",
                "user@example.com",
                "--credential-backend",
                "env",
                "--credential-value",
                "CHRONOS_PW",
                "--mirror-path",
                "/tmp/m",
            ]
        )
        self.assertEqual(code, 0)
        from chronos.config import load
        from chronos.domain import EnvCredential

        config = load(self.config_path)
        cred = config.accounts[0].credential
        assert isinstance(cred, EnvCredential)
        self.assertEqual(cred.variable, "CHRONOS_PW")

    def test_add_command_account_splits_value(self) -> None:
        code = self._run(
            [
                "account",
                "add",
                "--name",
                "personal",
                "--url",
                "https://x/",
                "--username",
                "u@example.com",
                "--credential-backend",
                "command",
                "--credential-value",
                "pass show chronos",
                "--mirror-path",
                "/tmp/m",
            ]
        )
        self.assertEqual(code, 0)
        from chronos.config import load
        from chronos.domain import CommandCredential

        config = load(self.config_path)
        cred = config.accounts[0].credential
        assert isinstance(cred, CommandCredential)
        self.assertEqual(cred.command, ("pass", "show", "chronos"))

    def test_add_rejects_duplicate_name(self) -> None:
        self._run(
            [
                "account",
                "add",
                "--name",
                "personal",
                "--url",
                "https://x/",
                "--username",
                "u@example.com",
                "--credential-backend",
                "plaintext",
                "--credential-value",
                "p",
                "--mirror-path",
                "/tmp/m",
            ]
        )
        self.stdout.truncate(0)
        self.stdout.seek(0)
        self.stderr.truncate(0)
        self.stderr.seek(0)
        code = self._run(
            [
                "account",
                "add",
                "--name",
                "personal",
                "--url",
                "https://x/",
                "--username",
                "u@example.com",
                "--credential-backend",
                "plaintext",
                "--credential-value",
                "p",
                "--mirror-path",
                "/tmp/m",
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("already exists", self.stderr.getvalue())


class AccountListCommandTest(ConfigEditingCliTestCase):
    def test_list_no_accounts(self) -> None:
        self._run(["init"])
        self.stdout.truncate(0)
        self.stdout.seek(0)
        code = self._run(["account", "list"])
        self.assertEqual(code, 0)
        self.assertIn("no accounts", self.stdout.getvalue())

    def test_list_shows_accounts_without_passwords(self) -> None:
        self._run(["init"])
        self._run(
            [
                "account",
                "add",
                "--name",
                "personal",
                "--url",
                "https://caldav.example.com/dav/",
                "--username",
                "user@example.com",
                "--credential-backend",
                "plaintext",
                "--credential-value",
                "SUPER_SECRET_DO_NOT_LEAK",
                "--mirror-path",
                "/tmp/m",
            ]
        )
        self.stdout.truncate(0)
        self.stdout.seek(0)
        code = self._run(["account", "list"])
        self.assertEqual(code, 0)
        output = self.stdout.getvalue()
        self.assertIn("personal", output)
        self.assertIn("user@example.com", output)
        self.assertIn("backend=plaintext", output)
        self.assertNotIn("SUPER_SECRET_DO_NOT_LEAK", output)


class AccountRmCommandTest(ConfigEditingCliTestCase):
    def test_rm_existing_account(self) -> None:
        self._run(["init"])
        self._run(
            [
                "account",
                "add",
                "--name",
                "gone",
                "--url",
                "https://x/",
                "--username",
                "u@example.com",
                "--credential-backend",
                "env",
                "--credential-value",
                "X",
                "--mirror-path",
                "/tmp/m",
            ]
        )
        self.stdout.truncate(0)
        self.stdout.seek(0)
        code = self._run(["account", "rm", "gone"])
        self.assertEqual(code, 0)
        from chronos.config import load

        config = load(self.config_path)
        self.assertEqual(config.accounts, ())

    def test_rm_missing_account(self) -> None:
        self._run(["init"])
        code = self._run(["account", "rm", "never-existed"])
        self.assertEqual(code, 1)
        self.assertIn("not found", self.stderr.getvalue())


class AccountAddOAuthTest(ConfigEditingCliTestCase):
    def test_add_oauth_account(self) -> None:
        self._run(["init"])
        self.stdout.truncate(0)
        self.stdout.seek(0)
        code = self._run(
            [
                "account",
                "add",
                "--name",
                "google",
                "--url",
                "https://apidata.googleusercontent.com/caldav/v2/me@gmail.com/events/",
                "--username",
                "me@gmail.com",
                "--credential-backend",
                "oauth",
                "--client-id",
                "1234.apps.googleusercontent.com",
                "--client-secret",
                "GOCSPX-secret",
                "--mirror-path",
                "/tmp/chronos/google",
            ]
        )
        self.assertEqual(code, 0)
        from chronos.config import load
        from chronos.domain import OAuthCredential

        config = load(self.config_path)
        cred = config.accounts[0].credential
        assert isinstance(cred, OAuthCredential)
        self.assertEqual(cred.client_id, "1234.apps.googleusercontent.com")
        self.assertEqual(cred.client_secret, "GOCSPX-secret")

    def test_add_google_account_minimal(self) -> None:
        """`--credential-backend google` accepts only client_id+secret;
        url and username are filled in from Google's defaults."""
        self._run(["init"])
        self.stdout.truncate(0)
        self.stdout.seek(0)
        code = self._run(
            [
                "account",
                "add",
                "--name",
                "google",
                "--credential-backend",
                "google",
                "--client-id",
                "1234.apps.googleusercontent.com",
                "--client-secret",
                "GOCSPX-secret",
            ]
        )
        self.assertEqual(code, 0)
        from chronos.config import load
        from chronos.domain import GOOGLE_CALDAV_URL, GoogleCredential

        config = load(self.config_path)
        account = config.accounts[0]
        cred = account.credential
        assert isinstance(cred, GoogleCredential)
        self.assertEqual(cred.client_id, "1234.apps.googleusercontent.com")
        self.assertEqual(cred.client_secret, "GOCSPX-secret")
        self.assertEqual(account.url, GOOGLE_CALDAV_URL)
        self.assertEqual(account.username, "")

    def test_google_missing_client_id_rejected(self) -> None:
        self._run(["init"])
        self.stderr.truncate(0)
        self.stderr.seek(0)
        code = self._run(
            [
                "account",
                "add",
                "--name",
                "google",
                "--credential-backend",
                "google",
                "--client-secret",
                "secret-only",
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn("--client-id", self.stderr.getvalue())

    def test_non_google_backend_still_requires_url(self) -> None:
        self._run(["init"])
        self.stderr.truncate(0)
        self.stderr.seek(0)
        code = self._run(
            [
                "account",
                "add",
                "--name",
                "personal",
                "--username",
                "u@example.com",
                "--credential-backend",
                "plaintext",
                "--credential-value",
                "pw",
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn("--url", self.stderr.getvalue())

    def test_oauth_missing_client_id_rejected(self) -> None:
        self._run(["init"])
        self.stdout.truncate(0)
        self.stdout.seek(0)
        self.stderr.truncate(0)
        self.stderr.seek(0)
        code = self._run(
            [
                "account",
                "add",
                "--name",
                "google",
                "--url",
                "https://x/",
                "--username",
                "me@example.com",
                "--credential-backend",
                "oauth",
                "--client-secret",
                "s",
                "--mirror-path",
                "/tmp/m",
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn("--client-id", self.stderr.getvalue())


class OAuthAuthorizeCommandTest(ConfigEditingCliTestCase):
    def _add_oauth_account(self, name: str = "google") -> None:
        self._run(["init"])
        self._run(
            [
                "account",
                "add",
                "--name",
                name,
                "--url",
                "https://x/",
                "--username",
                "me@example.com",
                "--credential-backend",
                "oauth",
                "--client-id",
                "cid",
                "--client-secret",
                "cs",
                "--mirror-path",
                "/tmp/m",
            ]
        )
        self.stdout.truncate(0)
        self.stdout.seek(0)

    def test_authorize_with_injected_flow_saves_tokens(self) -> None:
        from chronos.domain import OAuthCredential
        from chronos.oauth import StoredTokens

        self._add_oauth_account()
        token_path = self.tmp / "tokens.json"

        captured: dict[str, Any] = {}

        def fake_flow(credential: OAuthCredential, stdout: Any) -> StoredTokens:
            captured["credential"] = credential
            stdout.write("would have printed URL + code\n")
            return StoredTokens(
                access_token="at",
                refresh_token="rt",
                expiry_unix=12345.0,
                scope=credential.scope,
            )

        # Override the token store location by editing the saved config
        # to point at our tmp path.
        from chronos.config import load, save

        config = load(self.config_path)
        account = config.accounts[0]
        credential = account.credential
        from chronos.domain import AccountConfig, AppConfig

        assert isinstance(credential, OAuthCredential)
        updated_account = AccountConfig(
            name=account.name,
            url=account.url,
            username=account.username,
            credential=OAuthCredential(
                client_id=credential.client_id,
                client_secret=credential.client_secret,
                scope=credential.scope,
                token_path=token_path,
            ),
            mirror_path=account.mirror_path,
            trash_retention_days=account.trash_retention_days,
            include=account.include,
            exclude=account.exclude,
            read_only=account.read_only,
        )
        save(
            AppConfig(
                config_version=config.config_version,
                use_utf8=config.use_utf8,
                editor=config.editor,
                accounts=(updated_account,),
            ),
            self.config_path,
        )

        # cmd_oauth_authorize supports an injected auth_flow; call
        # it directly for the cleanest test.
        from chronos import cli

        code = cli.cmd_oauth_authorize(
            self.stdout,
            self.stderr,
            config_path=self.config_path,
            account_name="google",
            auth_flow=fake_flow,
        )
        self.assertEqual(code, 0)
        self.assertIn("would have printed", self.stdout.getvalue())
        self.assertTrue(token_path.exists())
        self.assertIn("cid", captured["credential"].client_id)

    def test_authorize_rejects_non_oauth_account(self) -> None:
        self._run(["init"])
        self._run(
            [
                "account",
                "add",
                "--name",
                "basic",
                "--url",
                "https://x/",
                "--username",
                "u",
                "--credential-backend",
                "plaintext",
                "--credential-value",
                "pw",
                "--mirror-path",
                "/tmp/m",
            ]
        )
        self.stdout.truncate(0)
        self.stdout.seek(0)
        code = self._run(["oauth", "authorize", "--account", "basic"])
        self.assertEqual(code, 2)
        self.assertIn("does not use an OAuth backend", self.stderr.getvalue())

    def test_authorize_unknown_account(self) -> None:
        self._run(["init"])
        self.stdout.truncate(0)
        self.stdout.seek(0)
        code = self._run(["oauth", "authorize", "--account", "nobody"])
        self.assertEqual(code, 1)
        self.assertIn("account not found", self.stderr.getvalue())

    def test_authorize_accepts_google_backend(self) -> None:
        """`google` is OAuth shorthand; `oauth authorize` must run the
        loopback flow with the underlying client_id/secret + Google scope."""
        from chronos.domain import GOOGLE_OAUTH_SCOPE, OAuthCredential
        from chronos.oauth import StoredTokens

        self._run(["init"])
        self._run(
            [
                "account",
                "add",
                "--name",
                "google",
                "--credential-backend",
                "google",
                "--client-id",
                "g-cid",
                "--client-secret",
                "g-cs",
            ]
        )
        self.stdout.truncate(0)
        self.stdout.seek(0)

        captured: dict[str, Any] = {}

        def fake_flow(credential: OAuthCredential, _stdout: Any) -> StoredTokens:
            captured["credential"] = credential
            return StoredTokens(
                access_token="at",
                refresh_token="rt",
                expiry_unix=12345.0,
                scope=credential.scope,
            )

        from chronos import cli

        with mock.patch(
            "chronos.cli.oauth_token_path",
            return_value=self.tmp / "g-tokens.json",
        ):
            code = cli.cmd_oauth_authorize(
                self.stdout,
                self.stderr,
                config_path=self.config_path,
                account_name="google",
                auth_flow=fake_flow,
            )
        self.assertEqual(code, 0)
        cred = captured["credential"]
        assert isinstance(cred, OAuthCredential)
        self.assertEqual(cred.client_id, "g-cid")
        self.assertEqual(cred.client_secret, "g-cs")
        self.assertEqual(cred.scope, GOOGLE_OAUTH_SCOPE)
        self.assertTrue((self.tmp / "g-tokens.json").exists())


class CliAuthorizerTest(unittest.TestCase):
    """`_default_cli_authorizer` runs the OAuth loopback flow over sys.stdout
    when interactive, and surfaces a clean error otherwise."""

    def test_raises_when_no_tty(self) -> None:
        from chronos.domain import OAuthCredential
        from chronos.oauth import OAuthError

        spec = OAuthCredential(client_id="c", client_secret="s", scope="x")
        with (
            mock.patch("chronos.cli.sys.stdin") as stdin,
            mock.patch("chronos.cli.sys.stdout") as stdout,
        ):
            stdin.isatty.return_value = False
            stdout.isatty.return_value = False
            with self.assertRaises(OAuthError) as ctx:
                cli._default_cli_authorizer(  # pyright: ignore[reportPrivateUsage]
                    "google", spec, Path("/unused")
                )
        self.assertIn("TTY", str(ctx.exception))

    def test_delegates_to_loopback_flow_when_interactive(self) -> None:
        from chronos.domain import OAuthCredential
        from chronos.oauth import StoredTokens

        spec = OAuthCredential(client_id="c", client_secret="s", scope="x")
        tokens = StoredTokens(
            access_token="at", refresh_token="rt", expiry_unix=1.0, scope="x"
        )
        flow = mock.MagicMock(return_value=tokens)
        with (
            mock.patch("chronos.cli.sys.stdin") as stdin,
            mock.patch("chronos.cli.sys.stdout") as stdout,
            mock.patch("chronos.cli._default_loopback_flow", flow),
        ):
            stdin.isatty.return_value = True
            stdout.isatty.return_value = True
            result = cli._default_cli_authorizer(  # pyright: ignore[reportPrivateUsage]
                "google", spec, Path("/unused")
            )
        self.assertIs(result, tokens)
        flow.assert_called_once()
        # And the user is told what's happening before the browser opens.
        write_calls = [c.args[0] for c in stdout.write.call_args_list]
        self.assertTrue(any("OAuth authorization required" in w for w in write_calls))


class TuiUnsupportedAuthorizerTest(unittest.TestCase):
    """The TUI swap-in authorizer always raises, with a message that tells
    the user to quit and run sync from CLI."""

    def test_raises_with_run_chronos_sync_message(self) -> None:
        from chronos.domain import OAuthCredential
        from chronos.oauth import OAuthError

        spec = OAuthCredential(client_id="c", client_secret="s", scope="x")
        with self.assertRaises(OAuthError) as ctx:
            cli._tui_unsupported_authorizer(  # pyright: ignore[reportPrivateUsage]
                "google", spec, Path("/unused")
            )
        message = str(ctx.exception)
        self.assertIn("TUI", message)
        self.assertIn("chronos sync", message)


class ConfigEditCommandTest(ConfigEditingCliTestCase):
    def test_edit_applies_user_changes(self) -> None:
        self._run(["init"])

        def editor(path: Path) -> None:
            path.write_text("config_version = 1\nuse_utf8 = true\n", encoding="utf-8")

        code = self._run(["config", "edit"], open_editor=editor)
        self.assertEqual(code, 0)
        from chronos.config import load

        self.assertTrue(load(self.config_path).use_utf8)

    def test_edit_rejects_invalid_toml_keeps_original(self) -> None:
        self._run(["init"])
        original_bytes = self.config_path.read_bytes()

        def editor(path: Path) -> None:
            path.write_text("this is = = not valid", encoding="utf-8")

        code = self._run(["config", "edit"], open_editor=editor)
        self.assertEqual(code, 1)
        self.assertIn("parse error", self.stderr.getvalue())
        # Original file untouched.
        self.assertEqual(self.config_path.read_bytes(), original_bytes)

    def test_edit_handles_missing_config(self) -> None:
        code = self._run(["config", "edit"], open_editor=lambda _: None)
        self.assertEqual(code, 1)
        self.assertIn("not found", self.stderr.getvalue())

    def test_edit_leaves_no_tempfile_behind(self) -> None:
        self._run(["init"])

        def editor(path: Path) -> None:
            path.write_text("config_version = 1\n", encoding="utf-8")

        self._run(["config", "edit"], open_editor=editor)
        leftovers = [
            p
            for p in self.config_path.parent.iterdir()
            if p.name.startswith("chronos-edit-")
        ]
        self.assertEqual(leftovers, [])


class PickEditorTest(unittest.TestCase):
    """`pick_editor` resolves an editor command without ever raising.

    Order of precedence (POSIX): VISUAL, then EDITOR, then a
    platform-appropriate default. Tests inject env / platform / which
    so they don't depend on the host OS or PATH.
    """

    def test_visual_takes_precedence_over_editor(self) -> None:
        cmd = cli.pick_editor(
            env={"VISUAL": "vim", "EDITOR": "ed"},
            platform="linux",
            which=lambda _: None,
        )
        self.assertEqual(cmd, ["vim"])

    def test_editor_used_when_visual_unset(self) -> None:
        cmd = cli.pick_editor(
            env={"EDITOR": "ed"},
            platform="linux",
            which=lambda _: None,
        )
        self.assertEqual(cmd, ["ed"])

    def test_visual_with_arguments_is_shlex_split(self) -> None:
        cmd = cli.pick_editor(
            env={"VISUAL": "code --wait"},
            platform="darwin",
            which=lambda _: None,
        )
        self.assertEqual(cmd, ["code", "--wait"])

    def test_windows_default_is_notepad(self) -> None:
        cmd = cli.pick_editor(
            env={},
            platform="win32",
            which=lambda _: None,
        )
        self.assertEqual(cmd, ["notepad"])

    def test_posix_prefers_nano_when_available(self) -> None:
        cmd = cli.pick_editor(
            env={},
            platform="linux",
            which=lambda name: "/usr/bin/nano" if name == "nano" else None,
        )
        self.assertEqual(cmd, ["nano"])

    def test_posix_falls_back_to_vi_without_nano(self) -> None:
        cmd = cli.pick_editor(
            env={},
            platform="linux",
            which=lambda _: None,
        )
        self.assertEqual(cmd, ["vi"])

    def test_macos_uses_same_posix_fallback(self) -> None:
        cmd = cli.pick_editor(
            env={},
            platform="darwin",
            which=lambda _: None,
        )
        self.assertEqual(cmd, ["vi"])

    def test_uses_real_environment_when_unspecified(self) -> None:
        # Smoke test the no-injection path: just verify it returns a
        # non-empty list without raising. The exact contents depend on
        # the test runner's environment.
        cmd = cli.pick_editor()
        self.assertTrue(cmd)
        self.assertIsInstance(cmd[0], str)
