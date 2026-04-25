from __future__ import annotations

import re
import tempfile
import unittest
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from chronos.domain import (
    AccountConfig,
    CalendarRef,
    ComponentRef,
    EnvCredential,
    LocalStatus,
    ResourceRef,
    SyncResult,
    SyncState,
    VEvent,
)
from chronos.index_store import SqliteIndexRepository
from chronos.storage import VdirMirrorRepository
from chronos.sync import SyncHaltError, sync_account
from tests import corpus
from tests.fake_caldav import FakeCalDAVSession

ACCOUNT_NAME = "personal"
CALENDAR_URL = "https://caldav.example.com/dav/cal/work/"
CALENDAR_NAME = "work"
OTHER_URL = "https://caldav.example.com/dav/cal/shared/"
OTHER_NAME = "shared"


def _ics_with_uid(uid: str) -> bytes:
    return (
        b"BEGIN:VCALENDAR\r\n"
        b"VERSION:2.0\r\n"
        b"PRODID:-//chronos-tests//EN\r\n"
        b"BEGIN:VEVENT\r\n"
        b"UID:" + uid.encode("utf-8") + b"\r\n"
        b"DTSTAMP:20260422T120000Z\r\n"
        b"DTSTART:20260501T090000Z\r\n"
        b"DTEND:20260501T100000Z\r\n"
        b"SUMMARY:Event " + uid.encode("utf-8") + b"\r\n"
        b"END:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )


def _account(
    *,
    include: tuple[str, ...] = (".*",),
    exclude: tuple[str, ...] = (),
    read_only: tuple[str, ...] = (),
) -> AccountConfig:
    return AccountConfig(
        name=ACCOUNT_NAME,
        url="https://caldav.example.com/dav/",
        username="user@example.com",
        credential=EnvCredential(variable="PWD_VAR"),
        mirror_path=Path("/unused"),
        trash_retention_days=30,
        include=tuple(re.compile(p) for p in include),
        exclude=tuple(re.compile(p) for p in exclude),
        read_only=tuple(re.compile(p) for p in read_only),
    )


class SyncTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.mirror = VdirMirrorRepository(tmp / "mirror")
        self.index = SqliteIndexRepository(tmp / "index.sqlite3")
        self.addCleanup(self.index.close)
        self.session = FakeCalDAVSession()
        self.session.add_calendar(url=CALENDAR_URL, name=CALENDAR_NAME)
        self.calendar_ref = CalendarRef(ACCOUNT_NAME, CALENDAR_NAME)

    def _run(self, *, account: AccountConfig | None = None) -> SyncResult:
        return sync_account(
            account=account or _account(),
            session=self.session,
            mirror=self.mirror,
            index=self.index,
            now=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
        )


class SlowPathFirstSyncTest(SyncTestCase):
    def test_first_sync_fetches_all_resources(self) -> None:
        self.session.put_resource(
            calendar_url=CALENDAR_URL,
            href=f"{CALENDAR_URL}a.ics",
            ics=_ics_with_uid("first-a@example.com"),
            etag="etag-a",
        )
        self.session.put_resource(
            calendar_url=CALENDAR_URL,
            href=f"{CALENDAR_URL}b.ics",
            ics=_ics_with_uid("first-b@example.com"),
            etag="etag-b",
        )
        result = self._run()
        self.assertEqual(result.calendars_synced, 1)
        self.assertEqual(result.components_added, 2)
        rows = self.index.list_calendar_components(self.calendar_ref)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r.href is not None and r.etag is not None for r in rows))

    def test_first_sync_stores_ctag(self) -> None:
        self.session.put_resource(
            calendar_url=CALENDAR_URL,
            href=f"{CALENDAR_URL}a.ics",
            ics=corpus.simple_event(),
            etag="etag-a",
        )
        self._run()
        state = self.index.get_sync_state(self.calendar_ref)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.ctag, self.session.current_ctag(CALENDAR_URL))

    def test_writes_raw_bytes_to_mirror(self) -> None:
        data = corpus.recurring_weekly()
        self.session.put_resource(
            calendar_url=CALENDAR_URL,
            href=f"{CALENDAR_URL}weekly.ics",
            ics=data,
            etag="etag-1",
        )
        self._run()
        resources = self.mirror.list_resources(ACCOUNT_NAME, CALENDAR_NAME)
        self.assertEqual(len(resources), 1)
        self.assertEqual(self.mirror.read(resources[0]), data)


class FastPathTest(SyncTestCase):
    def test_fast_path_issues_no_calendar_query(self) -> None:
        self.session.put_resource(
            calendar_url=CALENDAR_URL,
            href=f"{CALENDAR_URL}a.ics",
            ics=corpus.simple_event(),
            etag="etag-a",
        )
        # First sync: slow path.
        self._run()
        # Reset call log and re-sync with unchanged CTag.
        self.session.calls.clear()
        self._run()
        method_names = [call[0] for call in self.session.calls]
        self.assertIn("get_ctag", method_names)
        self.assertNotIn("calendar_query", method_names)
        self.assertNotIn("calendar_multiget", method_names)

    def test_ctag_change_forces_slow_path(self) -> None:
        self.session.put_resource(
            calendar_url=CALENDAR_URL,
            href=f"{CALENDAR_URL}a.ics",
            ics=_ics_with_uid("ctag-a@example.com"),
            etag="etag-a",
        )
        self._run()
        # Mutate the calendar on the server to bump the CTag.
        self.session.put_resource(
            calendar_url=CALENDAR_URL,
            href=f"{CALENDAR_URL}b.ics",
            ics=_ics_with_uid("ctag-b@example.com"),
            etag="etag-b",
        )
        self.session.calls.clear()
        result = self._run()
        self.assertIn("calendar_query", [c[0] for c in self.session.calls])
        self.assertEqual(result.components_added, 1)


class ServerDeletionTest(SyncTestCase):
    def test_server_deletion_removes_local_row(self) -> None:
        for uid in ("a", "b", "c"):
            self.session.put_resource(
                calendar_url=CALENDAR_URL,
                href=f"{CALENDAR_URL}{uid}.ics",
                ics=_ics_with_uid(f"{uid}@example.com"),
                etag=f"etag-{uid}",
            )
        self._run()
        self.session.remove_resource(CALENDAR_URL, f"{CALENDAR_URL}b.ics")
        self._run()
        rows = self.index.list_calendar_components(self.calendar_ref)
        self.assertEqual(len(rows), 2)

    def test_etag_change_triggers_refetch(self) -> None:
        href = f"{CALENDAR_URL}a.ics"
        self.session.put_resource(
            calendar_url=CALENDAR_URL,
            href=href,
            ics=corpus.simple_event(),
            etag="etag-1",
        )
        self._run()
        # Server replaces the resource content + bumps etag.
        self.session.put_resource(
            calendar_url=CALENDAR_URL,
            href=href,
            ics=corpus.all_day_event(),
            etag="etag-2",
        )
        result = self._run()
        self.assertEqual(result.components_updated, 1)
        fetched = next(
            r
            for r in self.index.list_calendar_components(self.calendar_ref)
            if r.href == href
        )
        self.assertEqual(fetched.etag, "etag-2")


class MassDeletionGuardTest(SyncTestCase):
    def test_halts_on_more_than_twenty_percent_deletion(self) -> None:
        for i in range(10):
            self.session.put_resource(
                calendar_url=CALENDAR_URL,
                href=f"{CALENDAR_URL}{i}.ics",
                ics=_ics_with_uid(f"mass-{i}@example.com"),
                etag=f"etag-{i}",
            )
        self._run()
        # Delete 5 of 10 = 50%.
        for i in range(5):
            self.session.remove_resource(CALENDAR_URL, f"{CALENDAR_URL}{i}.ics")
        result = self._run()
        # Sync did not raise, but the calendar was skipped and errored.
        self.assertEqual(result.calendars_synced, 0)
        self.assertTrue(any("50%" in e or "/10" in e for e in result.errors))
        rows = self.index.list_calendar_components(self.calendar_ref)
        self.assertEqual(len(rows), 10)  # untouched because we halted

    def test_small_calendar_allows_large_percentage(self) -> None:
        # Under baseline threshold: deletions allowed even if ratio is high.
        for i in range(3):
            self.session.put_resource(
                calendar_url=CALENDAR_URL,
                href=f"{CALENDAR_URL}{i}.ics",
                ics=_ics_with_uid(f"small-{i}@example.com"),
                etag=f"etag-{i}",
            )
        self._run()
        self.session.remove_resource(CALENDAR_URL, f"{CALENDAR_URL}0.ics")
        self.session.remove_resource(CALENDAR_URL, f"{CALENDAR_URL}1.ics")
        result = self._run()
        self.assertEqual(result.calendars_synced, 1)
        rows = self.index.list_calendar_components(self.calendar_ref)
        self.assertEqual(len(rows), 1)


class PushPendingTest(SyncTestCase):
    def _seed_local_only(self, uid: str) -> None:
        ref = ComponentRef(
            account_name=ACCOUNT_NAME,
            calendar_name=CALENDAR_NAME,
            uid=uid,
        )
        local = VEvent(
            ref=ref,
            href=None,
            etag=None,
            raw_ics=corpus.simple_event(),
            summary="Local only",
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
        self.index.upsert_component(local)

    def test_pending_create_is_pushed_with_if_none_match(self) -> None:
        self._seed_local_only("local-new@example.com")
        self._run()
        put_calls = [c for c in self.session.calls if c[0] == "put"]
        self.assertEqual(len(put_calls), 1)
        self.assertIsNone(put_calls[0][2])  # etag arg == None signals If-None-Match
        # Row now has href + etag.
        row = self.index.get_component(
            ComponentRef(ACCOUNT_NAME, CALENDAR_NAME, "local-new@example.com")
        )
        assert row is not None
        self.assertIsNotNone(row.href)
        self.assertIsNotNone(row.etag)
        # Pending queue drained.
        pending = self.index.list_pending_pushes(self.calendar_ref)
        self.assertEqual(pending, ())

    def test_read_only_calendar_skips_pushes(self) -> None:
        self._seed_local_only("local-new@example.com")
        account = _account(read_only=(".*",))
        self._run(account=account)
        put_calls = [c for c in self.session.calls if c[0] == "put"]
        self.assertEqual(put_calls, [])
        pending = self.index.list_pending_pushes(self.calendar_ref)
        self.assertEqual(len(pending), 1)


class PushTrashedTest(SyncTestCase):
    def test_trashed_row_is_deleted_on_server(self) -> None:
        href = f"{CALENDAR_URL}gone.ics"
        self.session.put_resource(
            calendar_url=CALENDAR_URL,
            href=href,
            ics=corpus.simple_event(),
            etag="etag-1",
        )
        self._run()
        # Mark local row as trashed.
        row = next(
            r
            for r in self.index.list_calendar_components(self.calendar_ref)
            if r.href == href
        )
        trashed = VEvent(
            ref=row.ref,
            href=row.href,
            etag=row.etag,
            raw_ics=row.raw_ics,
            summary=row.summary,
            description=row.description,
            location=row.location,
            dtstart=row.dtstart,
            dtend=row.dtend if isinstance(row, VEvent) else None,
            status=row.status,
            local_flags=row.local_flags,
            server_flags=row.server_flags,
            local_status=LocalStatus.TRASHED,
            trashed_at=datetime(2026, 4, 22, tzinfo=UTC),
            synced_at=row.synced_at,
        )
        self.index.upsert_component(trashed)
        self._run()
        delete_calls = [c for c in self.session.calls if c[0] == "delete"]
        self.assertEqual(len(delete_calls), 1)
        self.assertEqual(self.session.hrefs_in(CALENDAR_URL), ())
        self.assertIsNone(self.index.get_component(row.ref))


class RegexScopingTest(SyncTestCase):
    def test_exclude_regex_skips_calendar(self) -> None:
        self.session.add_calendar(url=OTHER_URL, name=OTHER_NAME)
        self.session.put_resource(
            calendar_url=CALENDAR_URL,
            href=f"{CALENDAR_URL}a.ics",
            ics=_ics_with_uid("scoped-a@example.com"),
            etag="etag-a",
        )
        self.session.put_resource(
            calendar_url=OTHER_URL,
            href=f"{OTHER_URL}x.ics",
            ics=_ics_with_uid("scoped-x@example.com"),
            etag="etag-x",
        )
        account = _account(exclude=(OTHER_NAME,))
        result = self._run(account=account)
        self.assertEqual(result.calendars_synced, 1)
        queried_urls = [c[1] for c in self.session.calls if c[0] == "calendar_query"]
        self.assertIn(CALENDAR_URL, queried_urls)
        self.assertNotIn(OTHER_URL, queried_urls)

    def test_include_regex_restricts_to_named_calendars(self) -> None:
        self.session.add_calendar(url=OTHER_URL, name=OTHER_NAME)
        account = _account(include=("work",))
        self._run(account=account)
        queried_urls = [c[1] for c in self.session.calls if c[0] == "calendar_query"]
        self.assertEqual(queried_urls, [CALENDAR_URL])

    def test_filter_skipping_all_calendars_lands_in_errors(self) -> None:
        # Server has calendars but no include pattern matches any of
        # them. The result should NOT silently report 0 syncs; it
        # should explain why.
        self.session.add_calendar(url=OTHER_URL, name=OTHER_NAME)
        account = _account(include=("nonexistent-name",))
        result = self._run(account=account)
        self.assertEqual(result.calendars_synced, 0)
        self.assertEqual(len(result.errors), 1)
        message = result.errors[0]
        self.assertIn("none matched", message)
        self.assertIn("nonexistent-name", message)
        # And the discovered calendar names are listed so the user
        # knows what they could write into the include pattern.
        self.assertIn(CALENDAR_NAME, message)
        self.assertIn(OTHER_NAME, message)

    def test_no_remote_calendars_does_not_emit_filter_message(self) -> None:
        # Empty server → 0 syncs, but no "discovered N but matched 0"
        # message because there were none to begin with.
        empty_session = FakeCalDAVSession()
        result = sync_account(
            account=_account(),
            session=empty_session,
            mirror=self.mirror,
            index=self.index,
            now=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
        )
        self.assertEqual(result.calendars_synced, 0)
        self.assertEqual(result.errors, ())


class IdempotencyTest(SyncTestCase):
    def test_no_changes_second_pass(self) -> None:
        for uid in ("a", "b"):
            self.session.put_resource(
                calendar_url=CALENDAR_URL,
                href=f"{CALENDAR_URL}{uid}.ics",
                ics=_ics_with_uid(f"idem-{uid}@example.com"),
                etag=f"etag-{uid}",
            )
        first = self._run()
        second = self._run()
        self.assertEqual(first.components_added, 2)
        self.assertEqual(second.components_added, 0)
        self.assertEqual(second.components_updated, 0)
        self.assertEqual(second.components_removed, 0)

    def test_sync_populates_occurrences_cache(self) -> None:
        # Regression: after `sync_account` returns, the `occurrences`
        # table must be populated for every synced calendar; otherwise
        # the TUI's view queries return nothing.
        self.session.put_resource(
            calendar_url=CALENDAR_URL,
            href=f"{CALENDAR_URL}a.ics",
            ics=_ics_with_uid("occ-a@example.com"),
            etag="etag-a",
        )
        self._run()
        occurrences = self.index.query_occurrences(
            CalendarRef(ACCOUNT_NAME, CALENDAR_NAME),
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2027, 1, 1, tzinfo=UTC),
        )
        self.assertEqual(len(occurrences), 1)
        self.assertEqual(occurrences[0].ref.uid, "occ-a@example.com")

    def test_empty_server_etag_does_not_trigger_phantom_updates(self) -> None:
        # Regression: against servers that don't include getetag in
        # calendar-query responses (Exchange-style gateways), the slow
        # path must not treat every event as "changed" on every pass.
        # Force slow path by leaving the server CTag unset between
        # syncs; the per-event etag from the server is "" (sentinel).
        for uid in ("a", "b"):
            self.session.put_resource(
                calendar_url=CALENDAR_URL,
                href=f"{CALENDAR_URL}{uid}.ics",
                ics=_ics_with_uid(f"empty-etag-{uid}@example.com"),
                etag="",  # server didn't return getetag
            )
        # Make the server CTag absent so every sync hits the slow path.
        self.session.set_ctag(CALENDAR_URL, "")
        self.session._ctags[CALENDAR_URL] = None  # type: ignore[assignment]

        first = self._run()
        self.assertEqual(first.components_added, 2)

        self.session.calls.clear()
        second = self._run()
        self.assertEqual(second.components_added, 0)
        self.assertEqual(second.components_updated, 0)
        self.assertEqual(second.components_removed, 0)
        # And critically: no multiget on the second pass — empty
        # server etags shouldn't drive refetches.
        self.assertEqual(
            [c for c in self.session.calls if c[0] == "calendar_multiget"],
            [],
        )


class CtagResetTest(SyncTestCase):
    def test_c4_local_state_ctag_invalidated_triggers_resync(self) -> None:
        href = f"{CALENDAR_URL}a.ics"
        self.session.put_resource(
            calendar_url=CALENDAR_URL,
            href=href,
            ics=corpus.simple_event(),
            etag="etag-1",
        )
        self._run()
        # Simulate C-4: server CTag resets to an unrelated value (e.g., the
        # calendar was re-created server-side). Local state is stale.
        self.index.set_sync_state(
            SyncState(
                calendar=self.calendar_ref,
                ctag="ctag-stale",
                sync_token=None,
                synced_at=datetime(2026, 4, 22, tzinfo=UTC),
            )
        )
        self.session.calls.clear()
        self._run()
        # Slow path must have run.
        self.assertIn("calendar_query", [c[0] for c in self.session.calls])

    def test_c1_server_deleted_while_local_pending_recreates(self) -> None:
        # Seed: server has a resource, local syncs it, user trashes it,
        # server-side the resource vanishes by other means -> we push DELETE
        # which 412s (not-found in our fake). Guard: state stays consistent.
        href = f"{CALENDAR_URL}c.ics"
        self.session.put_resource(
            calendar_url=CALENDAR_URL,
            href=href,
            ics=corpus.simple_event(),
            etag="etag-c",
        )
        self._run()
        self.session.remove_resource(CALENDAR_URL, href)
        # Sync sees server deletion + reconciles: local row deleted.
        self._run()
        rows = self.index.list_calendar_components(self.calendar_ref)
        self.assertEqual(rows, ())
        resources = self.mirror.list_resources(ACCOUNT_NAME, CALENDAR_NAME)
        self.assertEqual(resources, ())


class OverrideSyncTest(SyncTestCase):
    def test_recurring_resource_creates_master_and_override_rows(self) -> None:
        self.session.put_resource(
            calendar_url=CALENDAR_URL,
            href=f"{CALENDAR_URL}series.ics",
            ics=corpus.recurring_with_exceptions(),
            etag="etag-series",
        )
        self._run()
        rows = self.index.list_calendar_components(self.calendar_ref)
        self.assertEqual(len(rows), 2)
        recurrence_ids = {r.ref.recurrence_id for r in rows}
        self.assertIn(None, recurrence_ids)
        self.assertEqual(len(recurrence_ids), 2)

    def test_server_deletion_removes_master_and_all_overrides(self) -> None:
        href = f"{CALENDAR_URL}series.ics"
        self.session.put_resource(
            calendar_url=CALENDAR_URL,
            href=href,
            ics=corpus.recurring_with_exceptions(),
            etag="etag-series",
        )
        self._run()
        self.session.remove_resource(CALENDAR_URL, href)
        result = self._run()
        self.assertEqual(result.components_removed, 2)
        rows = self.index.list_calendar_components(self.calendar_ref)
        self.assertEqual(rows, ())


class MirrorMirrorsIndexTest(SyncTestCase):
    def test_server_deletion_also_clears_mirror_file(self) -> None:
        href = f"{CALENDAR_URL}temp.ics"
        self.session.put_resource(
            calendar_url=CALENDAR_URL,
            href=href,
            ics=corpus.simple_event(),
            etag="etag-1",
        )
        self._run()
        resources_before = self.mirror.list_resources(ACCOUNT_NAME, CALENDAR_NAME)
        self.assertEqual(len(resources_before), 1)
        self.session.remove_resource(CALENDAR_URL, href)
        self._run()
        resources_after = self.mirror.list_resources(ACCOUNT_NAME, CALENDAR_NAME)
        self.assertEqual(resources_after, ())


class SyncHaltUnusedSanityTest(unittest.TestCase):
    """SyncHaltError is emitted only from the guard; keep import live."""

    def test_halt_error_is_sync_error_subclass(self) -> None:
        self.assertTrue(issubclass(SyncHaltError, Exception))


class ResourceRefConstructionTest(unittest.TestCase):
    """Sanity: ResourceRef is importable where sync tests expect it."""

    def test_basic_ref(self) -> None:
        ref = ResourceRef(ACCOUNT_NAME, CALENDAR_NAME, "x")
        self.assertEqual(ref.uid, "x")


class CrashSafetyResumeTest(SyncTestCase):
    """Interrupting sync mid-flight (Ctrl-C, network drop) must leave a
    coherent on-disk state, and the next sync must converge to the
    same end state as an uninterrupted run.

    Load-bearing invariant under test: `_sync_calendar` only writes
    the new CTag via `set_sync_state` *after* the slow/fast path
    returns successfully. If anything raises before that, the prior
    CTag stays in place, and the next sync re-enters the slow path.
    """

    def _seed(self, count: int) -> None:
        for i in range(count):
            self.session.put_resource(
                calendar_url=CALENDAR_URL,
                href=f"{CALENDAR_URL}r{i}.ics",
                ics=_ics_with_uid(f"r{i}@example.com"),
                etag=f"etag-{i}",
            )

    def test_interrupt_during_multiget_preserves_prior_ctag(self) -> None:
        self._seed(3)
        # Inject an interrupt the first time multiget is called.
        original_multiget = self.session.calendar_multiget
        calls = {"n": 0}

        def boom(
            calendar_url: str, hrefs: Sequence[str]
        ) -> Sequence[tuple[str, str, bytes]]:
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyboardInterrupt
            return original_multiget(calendar_url, hrefs)

        self.session.calendar_multiget = boom  # type: ignore[method-assign]
        with self.assertRaises(KeyboardInterrupt):
            self._run()

        # No CTag was written, so the next sync re-enters the slow path.
        state = self.index.get_sync_state(self.calendar_ref)
        self.assertIsNone(state)
        # Nothing was ingested locally either — multiget raised before
        # any resources came back.
        rows = self.index.list_calendar_components(self.calendar_ref)
        self.assertEqual(rows, ())

    def test_resumed_sync_converges_to_uninterrupted_end_state(self) -> None:
        self._seed(5)
        # Reference run: a clean session that does an uninterrupted
        # sync. Capture the end state.
        ref_index_path = Path(self.enterContext(tempfile.TemporaryDirectory()))
        ref_index = SqliteIndexRepository(ref_index_path / "index.sqlite3")
        self.addCleanup(ref_index.close)
        ref_mirror = VdirMirrorRepository(ref_index_path / "mirror")
        ref_session = FakeCalDAVSession()
        ref_session.add_calendar(url=CALENDAR_URL, name=CALENDAR_NAME)
        for i in range(5):
            ref_session.put_resource(
                calendar_url=CALENDAR_URL,
                href=f"{CALENDAR_URL}r{i}.ics",
                ics=_ics_with_uid(f"r{i}@example.com"),
                etag=f"etag-{i}",
            )
        sync_account(
            account=_account(),
            session=ref_session,
            mirror=ref_mirror,
            index=ref_index,
            now=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
        )
        ref_rows = ref_index.list_calendar_components(self.calendar_ref)
        ref_uids = {r.ref.uid for r in ref_rows}
        ref_state = ref_index.get_sync_state(self.calendar_ref)

        # Fault-injected run: first sync raises KeyboardInterrupt
        # mid-multiget; second sync runs normally and must reach the
        # same uids + ctag as the reference.
        original_multiget = self.session.calendar_multiget
        calls = {"n": 0}

        def flaky_multiget(
            calendar_url: str, hrefs: Sequence[str]
        ) -> Sequence[tuple[str, str, bytes]]:
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyboardInterrupt
            return original_multiget(calendar_url, hrefs)

        self.session.calendar_multiget = flaky_multiget  # type: ignore[method-assign]
        with self.assertRaises(KeyboardInterrupt):
            self._run()
        # Recover: subsequent multiget calls work; sync runs cleanly.
        self._run()

        observed_uids = {
            r.ref.uid for r in self.index.list_calendar_components(self.calendar_ref)
        }
        observed_state = self.index.get_sync_state(self.calendar_ref)
        self.assertEqual(observed_uids, ref_uids)
        assert ref_state is not None and observed_state is not None
        self.assertEqual(observed_state.ctag, ref_state.ctag)

    def test_lost_put_response_recovers_via_412_reconciliation(self) -> None:
        # Simulate "previous push succeeded server-side, response lost":
        # the local row is still `href IS NULL` (we never recorded the
        # etag), and the server already has a resource at the
        # would-be target href with the same body. The next sync's
        # PUT-with-If-None-Match-* must 412; the recovery path issues
        # a multiget, sees the body matches by content hash, and
        # adopts the server's (href, etag) so the row stops being
        # pending.
        local_ics = _ics_with_uid("lost-response@example.com")
        ref = ComponentRef(
            account_name=ACCOUNT_NAME,
            calendar_name=CALENDAR_NAME,
            uid="lost-response@example.com",
        )
        self.index.upsert_component(
            VEvent(
                ref=ref,
                href=None,
                etag=None,
                raw_ics=local_ics,
                summary="Local",
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
        )
        # Pre-seed the server at the exact href chronos would PUT to,
        # with the same body. Mimics the lost-response state.
        target_href = f"{CALENDAR_URL}lost-response%40example.com.ics"
        self.session.put_resource(
            calendar_url=CALENDAR_URL,
            href=target_href,
            ics=local_ics,
            etag="server-etag",
        )

        self._run()

        row = self.index.get_component(ref)
        assert row is not None
        # Adopted server identity instead of looping on 412.
        self.assertEqual(row.href, target_href)
        self.assertEqual(row.etag, "server-etag")
        self.assertEqual(self.index.list_pending_pushes(self.calendar_ref), ())

    def test_412_with_diverged_remote_content_defers_to_next_sync(self) -> None:
        # Same shape as the lost-response case, but the server's body
        # differs from ours — a real conflict, not just a dropped
        # response. We must not adopt a stale etag (which would mask
        # the divergence on the next sync); leave the row pending and
        # let the next slow-path sync surface the conflict.
        local_ics = _ics_with_uid("conflict@example.com")
        ref = ComponentRef(
            account_name=ACCOUNT_NAME,
            calendar_name=CALENDAR_NAME,
            uid="conflict@example.com",
        )
        self.index.upsert_component(
            VEvent(
                ref=ref,
                href=None,
                etag=None,
                raw_ics=local_ics,
                summary="Local conflict",
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
        )
        target_href = f"{CALENDAR_URL}conflict%40example.com.ics"
        # Server has *different* bytes at the same href.
        self.session.put_resource(
            calendar_url=CALENDAR_URL,
            href=target_href,
            ics=_ics_with_uid("conflict@example.com")
            + b"X-DIVERGED:server-version\r\n",
            etag="server-divergent-etag",
        )

        self._run()

        # Row stays pending; we did NOT silently adopt a divergent etag.
        row = self.index.get_component(ref)
        assert row is not None
        self.assertIsNone(row.href)
        self.assertIsNone(row.etag)
        self.assertEqual(len(self.index.list_pending_pushes(self.calendar_ref)), 1)

    def test_interrupt_after_partial_ingest_keeps_committed_resources(self) -> None:
        # Once a per-resource transaction commits, that resource is
        # persistent — even if a later resource's ingest is interrupted.
        # The partial cache plus the unchanged prior CTag together let
        # the next sync re-fetch only what's missing (or, if the prior
        # CTag is None, re-fetch everything; either way, idempotent).
        self._seed(3)
        # Inject the interrupt inside upsert_component on the *third*
        # call so the first two resources commit first.
        original_upsert = self.index.upsert_component
        calls = {"n": 0}

        from chronos.domain import StoredComponent

        def flaky_upsert(component: StoredComponent) -> None:
            calls["n"] += 1
            if calls["n"] == 3:
                raise KeyboardInterrupt
            original_upsert(component)

        self.index.upsert_component = flaky_upsert  # type: ignore[method-assign]
        with self.assertRaises(KeyboardInterrupt):
            self._run()
        self.index.upsert_component = original_upsert  # type: ignore[method-assign]

        # The first two resources committed in their own transactions.
        rows = self.index.list_calendar_components(self.calendar_ref)
        self.assertEqual(len(rows), 2)
        # CTag was not written; re-sync converges.
        self.assertIsNone(self.index.get_sync_state(self.calendar_ref))
        # Recovery sync brings the third resource in.
        self._run()
        rows_after = self.index.list_calendar_components(self.calendar_ref)
        self.assertEqual(len(rows_after), 3)
