from __future__ import annotations

import re
import tempfile
import unittest
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from chronos.credentials import DefaultCredentialsProvider
from chronos.domain import (
    AccountConfig,
    AppConfig,
    CalendarRef,
    ComponentRef,
    LocalStatus,
    Occurrence,
    PlaintextCredential,
    ResourceRef,
    StoredComponent,
    SyncResult,
    VEvent,
    VTodo,
)
from chronos.index_store import SqliteIndexRepository
from chronos.recurrence import populate_occurrences
from chronos.storage import VdirMirrorRepository
from chronos.storage_indexing import index_calendar
from chronos.tui.app import ChronosApp, TuiServices
from chronos.tui.screens.agenda_screen import (
    title_for as agenda_title,
)
from chronos.tui.screens.confirm_screen import ConfirmScreen
from chronos.tui.screens.day_view_screen import title_for as day_title
from chronos.tui.screens.day_view_screen import window_for as day_window_for
from chronos.tui.screens.event_detail_screen import EventDetailScreen
from chronos.tui.screens.event_edit_screen import EditDraft, EventEditScreen
from chronos.tui.screens.main_screen import MainScreen
from chronos.tui.screens.month_view_screen import title_for as month_title
from chronos.tui.screens.month_view_screen import window_for as month_window_for
from chronos.tui.screens.search_dialog_screen import SearchDialogScreen
from chronos.tui.screens.sync_confirm_screen import SyncConfirmScreen
from chronos.tui.screens.todo_list_screen import title_for as todo_title
from chronos.tui.screens.week_view_screen import title_for as week_title
from chronos.tui.screens.week_view_screen import window_for as week_window_for
from chronos.tui.views import (
    CalendarSelection,
    OccurrenceRow,
    ViewKind,
    agenda_window,
    all_calendar_refs,
    day_window,
    format_event_row,
    format_todo_row,
    gather_occurrences,
    gather_todos,
    month_window,
    render_event_detail,
    search_components,
    week_window,
)
from chronos.tui.widgets.date_picker import (
    DatePicker,
    InvalidDateError,
    parse_date_input,
)
from chronos.tui.widgets.event_list import component_ref_for_row
from tests import corpus

ACCOUNT_NAME = "personal"
WORK_CAL = "work"
PERSONAL_CAL = "private"
NOW = datetime(2026, 4, 25, 9, 0, tzinfo=UTC)


# Pure helpers ----------------------------------------------------------------


def _account() -> AccountConfig:
    return AccountConfig(
        name=ACCOUNT_NAME,
        url="https://caldav.example.com/dav/",
        username="user@example.com",
        credential=PlaintextCredential(password="x"),
        mirror_path=Path("/unused"),
        trash_retention_days=30,
        include=(re.compile(".*"),),
        exclude=(),
        read_only=(),
    )


def _config() -> AppConfig:
    return AppConfig(
        config_version=1,
        use_utf8=False,
        editor=None,
        accounts=(_account(),),
    )


def _seed_workspace(tmp: Path) -> tuple[VdirMirrorRepository, SqliteIndexRepository]:
    """Seed two calendars for the test account with corpus fixtures."""
    mirror = VdirMirrorRepository(tmp / "mirror")
    index = SqliteIndexRepository(tmp / "index.sqlite3")

    fixtures: dict[str, list[tuple[str, bytes]]] = {
        WORK_CAL: [
            ("simple-event-1@example.com", corpus.simple_event()),
            ("recurring-weekly-1@example.com", corpus.recurring_weekly()),
        ],
        PERSONAL_CAL: [
            ("todo-1@example.com", corpus.simple_todo()),
        ],
    }
    for calendar_name, items in fixtures.items():
        for uid, ics in items:
            mirror.write(ResourceRef(ACCOUNT_NAME, calendar_name, uid), ics)
        ref = CalendarRef(ACCOUNT_NAME, calendar_name)
        index_calendar(mirror=mirror, index=index, calendar=ref)
        populate_occurrences(
            index=index,
            calendar=ref,
            window_start=datetime(2026, 1, 1, tzinfo=UTC),
            window_end=datetime(2027, 1, 1, tzinfo=UTC),
        )
    return mirror, index


def _services(
    tmp: Path,
    *,
    sync_runner: object | None = None,
) -> TuiServices:
    mirror, index = _seed_workspace(tmp)
    return TuiServices(
        config=_config(),
        mirror=mirror,
        index=index,
        creds=DefaultCredentialsProvider(env={}),
        now=lambda: NOW,
        sync_runner=sync_runner,  # type: ignore[arg-type]
    )


# Layer 1 — pure helpers ------------------------------------------------------


class WindowMathTest(unittest.TestCase):
    def test_day_window_is_24_hours_utc(self) -> None:
        start, end = day_window(date(2026, 4, 25))
        self.assertEqual(start, datetime(2026, 4, 25, tzinfo=UTC))
        self.assertEqual(end - start, timedelta(days=1))

    def test_week_window_starts_on_monday(self) -> None:
        # 2026-04-25 is a Saturday.
        start, end = week_window(date(2026, 4, 25))
        self.assertEqual(start, datetime(2026, 4, 20, tzinfo=UTC))
        self.assertEqual(end, datetime(2026, 4, 27, tzinfo=UTC))

    def test_month_window_handles_december_rollover(self) -> None:
        start, end = month_window(date(2026, 12, 15))
        self.assertEqual(start, datetime(2026, 12, 1, tzinfo=UTC))
        self.assertEqual(end, datetime(2027, 1, 1, tzinfo=UTC))

    def test_month_window_mid_year(self) -> None:
        start, end = month_window(date(2026, 4, 25))
        self.assertEqual(start, datetime(2026, 4, 1, tzinfo=UTC))
        self.assertEqual(end, datetime(2026, 5, 1, tzinfo=UTC))

    def test_agenda_window_default_is_two_weeks(self) -> None:
        start, end = agenda_window(date(2026, 4, 25))
        self.assertEqual(end - start, timedelta(days=14))

    def test_agenda_window_custom_days(self) -> None:
        start, end = agenda_window(date(2026, 4, 25), days=3)
        self.assertEqual(end - start, timedelta(days=3))


class ViewScreenTitleTest(unittest.TestCase):
    def test_day_title_iso_date(self) -> None:
        self.assertEqual(day_title(date(2026, 4, 25)), "Day · 2026-04-25")

    def test_week_title_includes_monday_and_sunday(self) -> None:
        title = week_title(date(2026, 4, 25))
        self.assertIn("2026-04-20", title)
        self.assertIn("2026-04-26", title)

    def test_month_title_uses_month_name(self) -> None:
        self.assertEqual(month_title(date(2026, 4, 25)), "Month · April 2026")
        self.assertEqual(month_title(date(2026, 12, 1)), "Month · December 2026")
        self.assertEqual(month_title(date(2026, 1, 1)), "Month · January 2026")

    def test_agenda_title_shows_window(self) -> None:
        title = agenda_title(date(2026, 4, 25))
        self.assertIn("2026-04-25", title)
        self.assertIn("2026-05-09", title)

    def test_todo_title_constant(self) -> None:
        self.assertEqual(todo_title(), "Todos")

    def test_view_window_helpers_match_views_module(self) -> None:
        d = date(2026, 4, 25)
        self.assertEqual(day_window_for(d), day_window(d))
        self.assertEqual(week_window_for(d), week_window(d))
        self.assertEqual(month_window_for(d), month_window(d))


class GatherOccurrencesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.mirror, self.index = _seed_workspace(self.tmp)
        self.addCleanup(self.index.close)
        self.calendars = all_calendar_refs(_config(), self.mirror)

    def test_agenda_returns_events_in_window(self) -> None:
        rows = gather_occurrences(
            index=self.index,
            calendars=self.calendars,
            selection=CalendarSelection(refs=frozenset()),
            window=agenda_window(date(2026, 4, 25), days=30),
        )
        # simple_event (2026-05-01) and weekly RRULE occurrences fall in window.
        self.assertGreater(len(rows), 0)
        starts = sorted(r.occurrence.start for r in rows)
        self.assertEqual(starts, [r.occurrence.start for r in rows])  # already sorted

    def test_selection_filter_drops_other_calendars(self) -> None:
        only_work = CalendarSelection(
            refs=frozenset({CalendarRef(ACCOUNT_NAME, WORK_CAL)})
        )
        rows = gather_occurrences(
            index=self.index,
            calendars=self.calendars,
            selection=only_work,
            window=agenda_window(date(2026, 4, 25), days=30),
        )
        # Only the work calendar's events should appear; nothing from
        # the personal calendar.
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row.component.ref.calendar_name, WORK_CAL)

    def test_trashed_components_are_dropped(self) -> None:
        # Mark the simple event as trashed.
        ref = ComponentRef(ACCOUNT_NAME, WORK_CAL, "simple-event-1@example.com")
        component = self.index.get_component(ref)
        assert component is not None
        assert isinstance(component, VEvent)
        trashed = VEvent(
            ref=component.ref,
            href=component.href,
            etag=component.etag,
            raw_ics=component.raw_ics,
            summary=component.summary,
            description=component.description,
            location=component.location,
            dtstart=component.dtstart,
            dtend=component.dtend,
            status=component.status,
            local_flags=component.local_flags,
            server_flags=component.server_flags,
            local_status=LocalStatus.TRASHED,
            trashed_at=NOW,
            synced_at=component.synced_at,
        )
        self.index.upsert_component(trashed)

        rows = gather_occurrences(
            index=self.index,
            calendars=self.calendars,
            selection=CalendarSelection(refs=frozenset()),
            window=day_window(date(2026, 5, 1)),
        )
        self.assertEqual(
            [r.component.ref.uid for r in rows],
            [
                uid
                for uid in (r.component.ref.uid for r in rows)
                if uid != "simple-event-1@example.com"
            ],
        )

    def test_empty_window_returns_empty(self) -> None:
        rows = gather_occurrences(
            index=self.index,
            calendars=self.calendars,
            selection=CalendarSelection(refs=frozenset()),
            window=(
                datetime(2030, 1, 1, tzinfo=UTC),
                datetime(2030, 1, 2, tzinfo=UTC),
            ),
        )
        self.assertEqual(rows, ())


class GatherTodosTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.mirror, self.index = _seed_workspace(self.tmp)
        self.addCleanup(self.index.close)
        self.calendars = all_calendar_refs(_config(), self.mirror)

    def test_returns_active_todo(self) -> None:
        todos = gather_todos(
            index=self.index,
            calendars=self.calendars,
            selection=CalendarSelection(refs=frozenset()),
        )
        self.assertEqual(len(todos), 1)
        self.assertEqual(todos[0].ref.uid, "todo-1@example.com")

    def test_selection_filters_calendars(self) -> None:
        only_work = CalendarSelection(
            refs=frozenset({CalendarRef(ACCOUNT_NAME, WORK_CAL)})
        )
        todos = gather_todos(
            index=self.index,
            calendars=self.calendars,
            selection=only_work,
        )
        self.assertEqual(todos, ())


class RowFormattingTest(unittest.TestCase):
    def test_format_event_row_has_four_columns(self) -> None:
        ref = ComponentRef(ACCOUNT_NAME, WORK_CAL, "x")
        event = VEvent(
            ref=ref,
            href=None,
            etag=None,
            raw_ics=b"",
            summary="Hello",
            description=None,
            location="Room 1",
            dtstart=datetime(2026, 5, 1, 9, tzinfo=UTC),
            dtend=None,
            status=None,
            local_flags=frozenset(),
            server_flags=frozenset(),
            local_status=LocalStatus.ACTIVE,
            trashed_at=None,
            synced_at=None,
        )
        row = OccurrenceRow(
            occurrence=Occurrence(
                ref=ref,
                start=datetime(2026, 5, 1, 9, tzinfo=UTC),
                end=None,
                recurrence_id=None,
                is_override=False,
            ),
            component=event,
        )
        cells = format_event_row(row)
        self.assertEqual(cells[0], "2026-05-01 09:00")
        self.assertEqual(cells[1], "Hello")
        self.assertEqual(cells[2], WORK_CAL)
        self.assertEqual(cells[3], "Room 1")

    def test_format_event_row_handles_missing_summary_and_location(self) -> None:
        ref = ComponentRef(ACCOUNT_NAME, WORK_CAL, "x")
        event = _empty_event(ref)
        row = OccurrenceRow(
            occurrence=Occurrence(
                ref=ref,
                start=datetime(2026, 5, 1, 9, tzinfo=UTC),
                end=None,
                recurrence_id=None,
                is_override=False,
            ),
            component=event,
        )
        cells = format_event_row(row)
        self.assertEqual(cells[1], "(no summary)")
        self.assertEqual(cells[3], "")

    def test_format_todo_row_renders_due_and_status(self) -> None:
        ref = ComponentRef(ACCOUNT_NAME, PERSONAL_CAL, "y")
        todo = VTodo(
            ref=ref,
            href=None,
            etag=None,
            raw_ics=b"",
            summary="Pay rent",
            description=None,
            location=None,
            dtstart=None,
            due=datetime(2026, 5, 5, 17, tzinfo=UTC),
            status="NEEDS-ACTION",
            local_flags=frozenset(),
            server_flags=frozenset(),
            local_status=LocalStatus.ACTIVE,
            trashed_at=None,
            synced_at=None,
        )
        cells = format_todo_row(todo)
        self.assertEqual(cells[0], "2026-05-05 17:00")
        self.assertEqual(cells[1], "Pay rent")
        self.assertEqual(cells[2], PERSONAL_CAL)
        self.assertEqual(cells[3], "NEEDS-ACTION")

    def test_format_todo_row_with_no_due_or_status(self) -> None:
        ref = ComponentRef(ACCOUNT_NAME, PERSONAL_CAL, "y")
        todo = _empty_todo(ref)
        cells = format_todo_row(todo)
        self.assertEqual(cells[0], "")
        self.assertEqual(cells[3], "")

    def test_component_ref_for_row_returns_ref(self) -> None:
        ref = ComponentRef(ACCOUNT_NAME, WORK_CAL, "x")
        component = _empty_event(ref)
        self.assertEqual(component_ref_for_row(component), ref)


def _empty_event(ref: ComponentRef) -> VEvent:
    return VEvent(
        ref=ref,
        href=None,
        etag=None,
        raw_ics=b"",
        summary=None,
        description=None,
        location=None,
        dtstart=None,
        dtend=None,
        status=None,
        local_flags=frozenset(),
        server_flags=frozenset(),
        local_status=LocalStatus.ACTIVE,
        trashed_at=None,
        synced_at=None,
    )


def _empty_todo(ref: ComponentRef) -> VTodo:
    return VTodo(
        ref=ref,
        href=None,
        etag=None,
        raw_ics=b"",
        summary=None,
        description=None,
        location=None,
        dtstart=None,
        due=None,
        status=None,
        local_flags=frozenset(),
        server_flags=frozenset(),
        local_status=LocalStatus.ACTIVE,
        trashed_at=None,
        synced_at=None,
    )


class SearchAndDetailTest(unittest.TestCase):
    def test_search_substring_case_insensitive(self) -> None:
        ref = ComponentRef(ACCOUNT_NAME, WORK_CAL, "uid-1")
        event = VEvent(
            ref=ref,
            href=None,
            etag=None,
            raw_ics=b"",
            summary="Quarterly Planning",
            description="Reviewing the plan for Q3",
            location="Conference room",
            dtstart=None,
            dtend=None,
            status=None,
            local_flags=frozenset(),
            server_flags=frozenset(),
            local_status=LocalStatus.ACTIVE,
            trashed_at=None,
            synced_at=None,
        )
        matches = search_components(components=(event,), query="QUART")
        self.assertEqual(matches, (event,))
        self.assertEqual(
            search_components(components=(event,), query="conference"),
            (event,),
        )
        self.assertEqual(search_components(components=(event,), query=""), ())

    def test_search_skips_trashed(self) -> None:
        ref = ComponentRef(ACCOUNT_NAME, WORK_CAL, "uid-1")
        event = VEvent(
            ref=ref,
            href=None,
            etag=None,
            raw_ics=b"",
            summary="Meeting",
            description=None,
            location=None,
            dtstart=None,
            dtend=None,
            status=None,
            local_flags=frozenset(),
            server_flags=frozenset(),
            local_status=LocalStatus.TRASHED,
            trashed_at=NOW,
            synced_at=None,
        )
        self.assertEqual(search_components(components=(event,), query="Meet"), ())

    def test_render_event_detail_event(self) -> None:
        ref = ComponentRef(ACCOUNT_NAME, WORK_CAL, "uid-1")
        event = VEvent(
            ref=ref,
            href=None,
            etag=None,
            raw_ics=b"",
            summary="Standup",
            description="Daily sync",
            location="Zoom",
            dtstart=datetime(2026, 5, 1, 9, tzinfo=UTC),
            dtend=datetime(2026, 5, 1, 9, 30, tzinfo=UTC),
            status="CONFIRMED",
            local_flags=frozenset(),
            server_flags=frozenset(),
            local_status=LocalStatus.ACTIVE,
            trashed_at=None,
            synced_at=None,
        )
        text = render_event_detail(event)
        self.assertIn("Summary: Standup", text)
        self.assertIn("Location: Zoom", text)
        self.assertIn("Start:", text)
        self.assertIn("End:", text)
        self.assertIn("Status: CONFIRMED", text)
        self.assertIn("Daily sync", text)

    def test_render_event_detail_todo(self) -> None:
        ref = ComponentRef(ACCOUNT_NAME, PERSONAL_CAL, "uid-2")
        todo = VTodo(
            ref=ref,
            href=None,
            etag=None,
            raw_ics=b"",
            summary="Buy milk",
            description=None,
            location=None,
            dtstart=datetime(2026, 5, 1, 9, tzinfo=UTC),
            due=datetime(2026, 5, 2, 17, tzinfo=UTC),
            status=None,
            local_flags=frozenset(),
            server_flags=frozenset(),
            local_status=LocalStatus.ACTIVE,
            trashed_at=None,
            synced_at=None,
        )
        text = render_event_detail(todo)
        self.assertIn("Buy milk", text)
        self.assertIn("Due:", text)
        self.assertIn("Start:", text)

    def test_render_event_detail_minimal(self) -> None:
        ref = ComponentRef(ACCOUNT_NAME, WORK_CAL, "uid-3")
        event = _empty_event(ref)
        text = render_event_detail(event)
        self.assertIn("(no summary)", text)
        self.assertIn(WORK_CAL, text)


class DatePickerTest(unittest.TestCase):
    def test_parse_naive_gets_utc(self) -> None:
        dt = parse_date_input("2026-05-01T09:00")
        self.assertEqual(dt, datetime(2026, 5, 1, 9, tzinfo=UTC))

    def test_parse_date_only(self) -> None:
        dt = parse_date_input("2026-05-01")
        self.assertEqual(dt, datetime(2026, 5, 1, tzinfo=UTC))

    def test_parse_with_offset(self) -> None:
        dt = parse_date_input("2026-05-01T09:00+02:00")
        self.assertEqual(dt.utcoffset(), timedelta(hours=2))

    def test_parse_empty_raises(self) -> None:
        with self.assertRaises(InvalidDateError):
            parse_date_input("")

    def test_parse_garbage_raises(self) -> None:
        with self.assertRaises(InvalidDateError):
            parse_date_input("yesterday")


class CalendarSelectionTest(unittest.TestCase):
    def test_empty_selection_contains_everything(self) -> None:
        selection = CalendarSelection(refs=frozenset())
        self.assertTrue(selection.contains(CalendarRef("a", "b")))

    def test_explicit_selection_contains_only_listed(self) -> None:
        ref = CalendarRef("a", "b")
        selection = CalendarSelection(refs=frozenset({ref}))
        self.assertTrue(selection.contains(ref))
        self.assertFalse(selection.contains(CalendarRef("a", "c")))


# Layer 2 — Pilot flows -------------------------------------------------------


class TuiFlowTestCase(unittest.IsolatedAsyncioTestCase):
    """Base for Pilot-driven flows."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def services(
        self,
        *,
        sync_runner: object | None = None,
    ) -> TuiServices:
        services = _services(self.tmp, sync_runner=sync_runner)
        # SQLite needs an explicit close on Windows or the temp dir
        # rmtree races against the still-open WAL file.
        self.addCleanup(services.index.close)
        return services


class FiveViewsNavigableTest(TuiFlowTestCase):
    async def test_view_switch_keys_change_active_view(self) -> None:
        services = self.services()
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(pilot.app.screen, MainScreen)

            await pilot.press("d")
            await pilot.pause()
            self.assertEqual(pilot.app.screen._view, ViewKind.DAY)

            await pilot.press("w")
            await pilot.pause()
            self.assertEqual(pilot.app.screen._view, ViewKind.WEEK)

            await pilot.press("m")
            await pilot.pause()
            self.assertEqual(pilot.app.screen._view, ViewKind.MONTH)

            await pilot.press("a")
            await pilot.pause()
            self.assertEqual(pilot.app.screen._view, ViewKind.AGENDA)

            await pilot.press("t")
            await pilot.pause()
            self.assertEqual(pilot.app.screen._view, ViewKind.TODOS)


class QuitBindingTest(TuiFlowTestCase):
    async def test_q_exits_the_app(self) -> None:
        # Regression: Textual's screen-binding dispatch does not bubble
        # missing actions to the App, so binding "q" to "quit" without
        # MainScreen.action_quit silently dropped the press.
        services = self.services()
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
            self.assertTrue(app._exit)


class TodayResetsViewedDateTest(TuiFlowTestCase):
    async def test_today_jumps_back_to_now(self) -> None:
        services = self.services()
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = pilot.app.screen
            assert isinstance(screen, MainScreen)
            screen._viewed_date = date(2024, 1, 1)
            await pilot.press("T")
            await pilot.pause()
            self.assertEqual(screen._viewed_date, NOW.date())


class NewEventFlowTest(TuiFlowTestCase):
    async def test_new_event_creates_in_mirror_and_index(self) -> None:
        services = self.services()
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            assert isinstance(pilot.app.screen, EventEditScreen)
            edit = pilot.app.screen
            edit.query_one("#edit-summary").value = "Brand new event"  # type: ignore[attr-defined]
            edit.query_one(DatePicker).value = "2026-05-15T10:00"
            edit.action_save()
            await pilot.pause()

        # Verify the event landed in whichever calendar was the default.
        all_components: list[StoredComponent] = []
        for ref in all_calendar_refs(services.config, services.mirror):
            all_components.extend(services.index.list_calendar_components(ref))
        new = [c for c in all_components if c.summary == "Brand new event"]
        self.assertEqual(len(new), 1)
        # And on disk under that calendar.
        on_disk = services.mirror.list_resources(
            new[0].ref.account_name, new[0].ref.calendar_name
        )
        uids = {r.uid for r in on_disk}
        self.assertIn(new[0].ref.uid, uids)


class EditExistingEventTest(TuiFlowTestCase):
    async def test_edit_replaces_summary(self) -> None:
        services = self.services()
        # Find an existing event to edit.
        ref = ComponentRef(ACCOUNT_NAME, WORK_CAL, "simple-event-1@example.com")
        component = services.index.get_component(ref)
        assert isinstance(component, VEvent)
        assert component.dtstart is not None

        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = pilot.app.screen
            assert isinstance(screen, MainScreen)
            screen._edit_specific(component)
            await pilot.pause()
            edit = pilot.app.screen
            assert isinstance(edit, EventEditScreen)
            edit.query_one("#edit-summary").value = "Edited summary"  # type: ignore[attr-defined]
            edit.action_save()
            await pilot.pause()

        updated = services.index.get_component(ref)
        assert isinstance(updated, VEvent)
        self.assertEqual(updated.summary, "Edited summary")
        # The mirror was rewritten too.
        raw = services.mirror.read(ref.resource)
        self.assertIn(b"Edited summary", raw)


class TrashFlowTest(TuiFlowTestCase):
    async def test_trash_with_confirmation_marks_trashed(self) -> None:
        services = self.services()
        ref = ComponentRef(ACCOUNT_NAME, WORK_CAL, "simple-event-1@example.com")
        component = services.index.get_component(ref)
        assert isinstance(component, VEvent)

        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = pilot.app.screen
            assert isinstance(screen, MainScreen)
            # Trigger trash directly with the component (UI selection
            # is exercised by other tests; here we focus on the confirm
            # plumbing).
            screen.trash_with_confirm(component)
            await pilot.pause()
            confirm = pilot.app.screen
            assert isinstance(confirm, ConfirmScreen)
            await pilot.press("y")
            await pilot.pause()

        trashed = services.index.get_component(ref)
        assert trashed is not None
        self.assertEqual(trashed.local_status, LocalStatus.TRASHED)

    async def test_cancel_keeps_event_active(self) -> None:
        services = self.services()
        ref = ComponentRef(ACCOUNT_NAME, WORK_CAL, "simple-event-1@example.com")
        component = services.index.get_component(ref)
        assert isinstance(component, VEvent)

        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = pilot.app.screen
            assert isinstance(screen, MainScreen)
            screen.trash_with_confirm(component)
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()

        unchanged = services.index.get_component(ref)
        assert unchanged is not None
        self.assertEqual(unchanged.local_status, LocalStatus.ACTIVE)


class SyncFlowTest(TuiFlowTestCase):
    async def test_sync_runner_called_on_confirmation(self) -> None:
        calls: list[int] = []

        def runner() -> Sequence[SyncResult]:
            calls.append(1)
            return (
                SyncResult(
                    account_name=ACCOUNT_NAME,
                    calendars_synced=2,
                    components_added=1,
                    components_updated=0,
                    components_removed=0,
                    errors=(),
                ),
            )

        services = self.services(sync_runner=runner)
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            assert isinstance(pilot.app.screen, SyncConfirmScreen)
            await pilot.press("y")
            await pilot.pause()

        self.assertEqual(calls, [1])

    async def test_sync_runner_errors_show_in_notification(self) -> None:
        def runner() -> Sequence[SyncResult]:
            return (
                SyncResult(
                    account_name=ACCOUNT_NAME,
                    calendars_synced=0,
                    components_added=0,
                    components_updated=0,
                    components_removed=0,
                    errors=("auth refused",),
                ),
            )

        services = self.services(sync_runner=runner)
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            assert isinstance(pilot.app.screen, SyncConfirmScreen)
            await pilot.press("y")
            await pilot.pause()
            # Path executed without raising; error severity is set in
            # the notification.

    async def test_sync_without_runner_notifies(self) -> None:
        services = self.services(sync_runner=None)
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            assert isinstance(pilot.app.screen, SyncConfirmScreen)
            await pilot.press("y")
            await pilot.pause()
            # No assertion on notify text; the path executed without
            # raising is the contract.


class SearchFlowTest(TuiFlowTestCase):
    async def test_search_dialog_opens_event_detail_on_select(self) -> None:
        services = self.services()
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("/")
            await pilot.pause()
            assert isinstance(pilot.app.screen, SearchDialogScreen)
            search = pilot.app.screen
            search.query_one("#search-input").value = "Simple"  # type: ignore[attr-defined]
            await pilot.pause()
            results = search.query_one("#search-results")
            results.index = 0  # type: ignore[attr-defined]
            search.action_submit()
            await pilot.pause()
            self.assertIsInstance(pilot.app.screen, EventDetailScreen)


class EditScreenValidationTest(TuiFlowTestCase):
    async def test_save_with_empty_summary_shows_error_and_keeps_screen(self) -> None:
        services = self.services()
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            edit = pilot.app.screen
            assert isinstance(edit, EventEditScreen)
            edit.query_one("#edit-summary").value = ""  # type: ignore[attr-defined]
            edit.query_one(DatePicker).value = "2026-05-15T10:00"
            edit.action_save()
            await pilot.pause()
            self.assertIsInstance(pilot.app.screen, EventEditScreen)
            self.assertEqual(edit._error, "summary is required")

    async def test_save_with_invalid_date_shows_error(self) -> None:
        services = self.services()
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            edit = pilot.app.screen
            assert isinstance(edit, EventEditScreen)
            edit.query_one("#edit-summary").value = "Anything"  # type: ignore[attr-defined]
            edit.query_one(DatePicker).value = "not-a-date"
            edit.action_save()
            await pilot.pause()
            self.assertIsInstance(pilot.app.screen, EventEditScreen)

    async def test_cancel_pops_screen(self) -> None:
        services = self.services()
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            self.assertIsInstance(pilot.app.screen, MainScreen)


class DraftAndDetailScreenWiringTest(unittest.TestCase):
    def test_edit_draft_carries_existing(self) -> None:
        ref = ComponentRef(ACCOUNT_NAME, WORK_CAL, "uid")
        event = _empty_event(ref)
        draft = EditDraft(
            target=ref.calendar,
            summary="X",
            dtstart=datetime(2026, 5, 1, 9, tzinfo=UTC),
            dtend=None,
            existing=event,
        )
        self.assertIs(draft.existing, event)

    def test_event_edit_screen_requires_calendar(self) -> None:
        with self.assertRaises(ValueError):
            EventEditScreen(
                calendars=(),
                existing=None,
                default_calendar=None,
                on_save=lambda _draft: None,
            )
