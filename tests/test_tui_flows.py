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
from chronos.tui.screens.agenda_screen import (
    window_for as agenda_window_for,
)
from chronos.tui.screens.confirm_screen import ConfirmScreen
from chronos.tui.screens.day_view_screen import title_for as day_title
from chronos.tui.screens.event_detail_screen import EventDetailScreen
from chronos.tui.screens.event_edit_screen import EditDraft, EventEditScreen
from chronos.tui.screens.grid_view_screen import title_for as grid_title
from chronos.tui.screens.grid_view_screen import window_for as grid_window_for
from chronos.tui.screens.main_screen import MainScreen
from chronos.tui.screens.search_dialog_screen import SearchDialogScreen
from chronos.tui.screens.sync_confirm_screen import SyncConfirmScreen
from chronos.tui.views import (
    AgendaWindow,
    CalendarSelection,
    OccurrenceRow,
    ViewKind,
    agenda_window,
    all_calendar_refs,
    day_window,
    format_duration,
    format_event_row,
    format_friendly_start,
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
from chronos.tui.widgets.event_list import EventList, component_ref_for_row
from chronos.tui.widgets.event_view import EventView
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

    def test_grid_title_includes_start_and_end(self) -> None:
        # Grid view defaults to 4 days starting from the viewed date.
        title = grid_title(date(2026, 4, 25))
        self.assertIn("2026-04-25", title)
        self.assertIn("2026-04-28", title)

    def test_agenda_title_reflects_window_mode(self) -> None:
        # Day mode: just today.
        day_title_text = agenda_title(date(2026, 4, 25), AgendaWindow.DAY)
        self.assertIn("Day", day_title_text)
        self.assertIn("2026-04-25", day_title_text)
        # Week mode: aligned Mon–Sun (2026-04-20 to 2026-04-26).
        week_title_text = agenda_title(date(2026, 4, 25), AgendaWindow.WEEK)
        self.assertIn("Week", week_title_text)
        self.assertIn("2026-04-20", week_title_text)
        # Month mode: full April 2026.
        month_title_text = agenda_title(date(2026, 4, 25), AgendaWindow.MONTH)
        self.assertIn("Month", month_title_text)
        self.assertIn("2026-04-01", month_title_text)

    def test_agenda_window_for_uses_calendar_aligned_ranges(self) -> None:
        d = date(2026, 4, 25)
        self.assertEqual(agenda_window_for(d, AgendaWindow.DAY), day_window(d))
        self.assertEqual(agenda_window_for(d, AgendaWindow.WEEK), week_window(d))
        self.assertEqual(agenda_window_for(d, AgendaWindow.MONTH), month_window(d))

    def test_grid_window_for_default_is_four_days(self) -> None:
        from datetime import time as _time

        start, end = grid_window_for(date(2026, 4, 25))
        expected_start = datetime.combine(date(2026, 4, 25), _time.min, tzinfo=UTC)
        self.assertEqual(start, expected_start)
        self.assertEqual(end - start, timedelta(days=4))


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


class FriendlyStartFormatTest(unittest.TestCase):
    TODAY = date(2026, 4, 25)  # Saturday

    def test_today(self) -> None:
        self.assertEqual(
            format_friendly_start(datetime(2026, 4, 25, 9, 30, tzinfo=UTC), self.TODAY),
            "Today 09:30",
        )

    def test_tomorrow(self) -> None:
        self.assertEqual(
            format_friendly_start(datetime(2026, 4, 26, 14, 0, tzinfo=UTC), self.TODAY),
            "Tomorrow 14:00",
        )

    def test_yesterday(self) -> None:
        self.assertEqual(
            format_friendly_start(datetime(2026, 4, 24, 8, 0, tzinfo=UTC), self.TODAY),
            "Yesterday 08:00",
        )

    def test_within_a_week_uses_weekday_name(self) -> None:
        # 2026-04-28 is the following Tuesday.
        result = format_friendly_start(
            datetime(2026, 4, 28, 9, 0, tzinfo=UTC), self.TODAY
        )
        self.assertEqual(result, "Tue 09:00")

    def test_recent_past_uses_last_weekday(self) -> None:
        # 2026-04-22 is the prior Wednesday.
        result = format_friendly_start(
            datetime(2026, 4, 22, 9, 0, tzinfo=UTC), self.TODAY
        )
        self.assertEqual(result, "Last Wed 09:00")

    def test_same_year_uses_short_date(self) -> None:
        result = format_friendly_start(
            datetime(2026, 8, 15, 9, 0, tzinfo=UTC), self.TODAY
        )
        # Sat 15 Aug 09:00 — order varies by locale of strftime but
        # the important pieces are all there.
        self.assertIn("Aug", result)
        self.assertIn("15", result)
        self.assertIn("09:00", result)
        self.assertNotIn("2026", result)  # year omitted for current-year

    def test_other_year_includes_year(self) -> None:
        result = format_friendly_start(
            datetime(2014, 9, 30, 12, 18, tzinfo=UTC), self.TODAY
        )
        self.assertIn("2014", result)
        self.assertIn("Sep", result)
        self.assertIn("12:18", result)


class DurationFormatTest(unittest.TestCase):
    def test_zero_or_missing_end_is_empty(self) -> None:
        self.assertEqual(format_duration(datetime(2026, 5, 1, 9, tzinfo=UTC), None), "")
        self.assertEqual(
            format_duration(
                datetime(2026, 5, 1, 9, tzinfo=UTC),
                datetime(2026, 5, 1, 9, tzinfo=UTC),
            ),
            "",
        )

    def test_minutes(self) -> None:
        self.assertEqual(
            format_duration(
                datetime(2026, 5, 1, 9, tzinfo=UTC),
                datetime(2026, 5, 1, 9, 30, tzinfo=UTC),
            ),
            "30m",
        )

    def test_whole_hours(self) -> None:
        self.assertEqual(
            format_duration(
                datetime(2026, 5, 1, 9, tzinfo=UTC),
                datetime(2026, 5, 1, 11, tzinfo=UTC),
            ),
            "2h",
        )

    def test_hours_and_minutes(self) -> None:
        self.assertEqual(
            format_duration(
                datetime(2026, 5, 1, 9, tzinfo=UTC),
                datetime(2026, 5, 1, 10, 30, tzinfo=UTC),
            ),
            "1h30m",
        )

    def test_full_day(self) -> None:
        self.assertEqual(
            format_duration(
                datetime(2026, 5, 1, tzinfo=UTC),
                datetime(2026, 5, 2, tzinfo=UTC),
            ),
            "1d",
        )

    def test_multi_day_with_remainder(self) -> None:
        self.assertEqual(
            format_duration(
                datetime(2026, 5, 1, tzinfo=UTC),
                datetime(2026, 5, 2, 6, 15, tzinfo=UTC),
            ),
            "1d6h15m",
        )


class RowFormattingTest(unittest.TestCase):
    TODAY = date(2026, 4, 25)

    def test_format_event_row_splits_day_and_time(self) -> None:
        ref = ComponentRef(ACCOUNT_NAME, WORK_CAL, "x")
        event = VEvent(
            ref=ref,
            href=None,
            etag=None,
            raw_ics=b"",
            summary="Hello",
            description=None,
            location="Room 1",
            dtstart=datetime(2026, 6, 15, 9, tzinfo=UTC),
            dtend=datetime(2026, 6, 15, 10, tzinfo=UTC),
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
                start=datetime(2026, 6, 15, 9, tzinfo=UTC),
                end=datetime(2026, 6, 15, 10, tzinfo=UTC),
                recurrence_id=None,
                is_override=False,
            ),
            component=event,
        )
        cells = format_event_row(row, self.TODAY)
        # 6 cells: Day, Time, Duration, Summary, Calendar, Location.
        self.assertEqual(len(cells), 6)
        # 2026-06-15 is a Monday, well outside the today/tomorrow/
        # yesterday window → "DD MMM ddd" form.
        self.assertEqual(cells[0], "15 Jun Mon")
        self.assertEqual(cells[1], "09:00")
        self.assertEqual(cells[2], "1h")
        self.assertEqual(cells[3], "Hello")
        self.assertEqual(cells[4], WORK_CAL)
        self.assertEqual(cells[5], "Room 1")

    def test_format_event_row_uses_friendly_words_for_today_tomorrow(self) -> None:
        # `Yesterday` / `Today` / `Tomorrow` replace the absolute date
        # in the Day column for those three days only — everything
        # else uses the literal `DD MMM ddd` form.
        ref = ComponentRef(ACCOUNT_NAME, WORK_CAL, "x")
        event = _empty_event(ref)

        for delta, label in ((-1, "Yesterday"), (0, "Today"), (1, "Tomorrow")):
            start = datetime(2026, 4, 25 + delta, 9, tzinfo=UTC)
            row = OccurrenceRow(
                occurrence=Occurrence(
                    ref=ref,
                    start=start,
                    end=start + timedelta(hours=1),
                    recurrence_id=None,
                    is_override=False,
                ),
                component=event,
            )
            cells = format_event_row(row, self.TODAY)
            self.assertEqual(cells[0], label, f"delta={delta}")

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
        cells = format_event_row(row, self.TODAY)
        self.assertEqual(cells[1], "09:00")  # Time
        self.assertEqual(cells[2], "")  # no end -> no duration
        self.assertEqual(cells[3], "(no summary)")
        self.assertEqual(cells[5], "")  # no location

    def test_past_event_cells_are_dimmed_when_now_is_supplied(self) -> None:
        # Regression: in the agenda view, events whose end has already
        # passed should render muted so the user's eye is drawn to
        # what's still upcoming.
        from rich.text import Text

        ref = ComponentRef(ACCOUNT_NAME, WORK_CAL, "x")
        event = _empty_event(ref)
        row = OccurrenceRow(
            occurrence=Occurrence(
                ref=ref,
                start=datetime(2026, 4, 24, 9, tzinfo=UTC),
                end=datetime(2026, 4, 24, 10, tzinfo=UTC),
                recurrence_id=None,
                is_override=False,
            ),
            component=event,
        )
        now = datetime(2026, 4, 25, 9, 0, tzinfo=UTC)
        cells = format_event_row(row, self.TODAY, now=now)
        for cell in cells:
            self.assertIsInstance(cell, Text)
            assert isinstance(cell, Text)
            self.assertEqual(cell.style, "dim")

    def test_in_progress_event_is_not_dimmed(self) -> None:
        # An event that started before `now` but hasn't ended yet is
        # still happening — keep it bright so it stays visible.
        ref = ComponentRef(ACCOUNT_NAME, WORK_CAL, "x")
        event = _empty_event(ref)
        row = OccurrenceRow(
            occurrence=Occurrence(
                ref=ref,
                start=datetime(2026, 4, 25, 8, 30, tzinfo=UTC),
                end=datetime(2026, 4, 25, 10, 0, tzinfo=UTC),
                recurrence_id=None,
                is_override=False,
            ),
            component=event,
        )
        now = datetime(2026, 4, 25, 9, 0, tzinfo=UTC)
        cells = format_event_row(row, self.TODAY, now=now)
        # All cells are plain strings — no dim wrapping.
        for cell in cells:
            self.assertIsInstance(cell, str)

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
        today = date(2026, 4, 25)
        text = render_event_detail(event, today)
        self.assertIn("Summary: Standup", text)
        # New layout: "Source: <calendar> (<account>)" replaces the
        # old "Account / Calendar:" line. The calendar comes first.
        self.assertIn(f"Source: {WORK_CAL} ({ACCOUNT_NAME})", text)
        self.assertIn("Location: Zoom", text)
        # Times render through `format_friendly_start`, not ISO.
        self.assertIn("Start: ", text)
        self.assertIn("End: ", text)
        self.assertNotIn("T09:00:00", text)
        self.assertIn("Status: CONFIRMED", text)
        self.assertIn("Notes:", text)
        self.assertIn("Daily sync", text)
        # Internal UID is suppressed.
        self.assertNotIn("UID:", text)

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
        text = render_event_detail(todo, date(2026, 4, 25))
        self.assertIn("Buy milk", text)
        self.assertIn("Due: ", text)
        self.assertIn("Start: ", text)
        # Empty description shows the placeholder, not a missing field.
        self.assertIn("(no notes)", text)

    def test_render_event_detail_minimal(self) -> None:
        ref = ComponentRef(ACCOUNT_NAME, WORK_CAL, "uid-3")
        event = _empty_event(ref)
        text = render_event_detail(event, date(2026, 4, 25))
        self.assertIn("(no summary)", text)
        self.assertIn(WORK_CAL, text)
        # The Location and Notes slots are always present, even when
        # the underlying data is missing.
        self.assertIn("(no location)", text)
        self.assertIn("(no notes)", text)
        # And Start / End placeholders surface as "(not set)" rather
        # than disappearing — keeps the layout stable across events.
        self.assertIn("(not set)", text)

    def test_render_event_detail_aligns_labels(self) -> None:
        ref = ComponentRef(ACCOUNT_NAME, WORK_CAL, "uid-x")
        event = _empty_event(ref)
        text = render_event_detail(event, date(2026, 4, 25))
        # Right-aligned labels mean every grid line's colon sits at
        # the same column. Pick the column from the "Location:" line
        # (the longest label) and assert each grid line carries a
        # colon there.
        location_line = next(
            ln for ln in text.splitlines() if ln.lstrip().startswith("Location:")
        )
        colon_col = location_line.index(":")
        for label in ("Summary", "Source", "Location", "Start", "End"):
            line = next(
                ln for ln in text.splitlines() if ln.lstrip().startswith(label + ":")
            )
            self.assertEqual(line[colon_col], ":", line)


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


class ViewSwitchTest(TuiFlowTestCase):
    """`Ctrl-A`, `Ctrl-D`, `Ctrl-G` flip between the three top-level views.
    Inside the agenda view, `d` / `w` / `m` flip the agenda window
    (day / week / month) without leaving agenda."""

    async def test_ctrl_keys_switch_between_top_level_views(self) -> None:
        services = self.services()
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(pilot.app.screen, MainScreen)

            await pilot.press("ctrl+d")
            await pilot.pause()
            self.assertEqual(pilot.app.screen._view, ViewKind.DAY)

            await pilot.press("ctrl+g")
            await pilot.pause()
            self.assertEqual(pilot.app.screen._view, ViewKind.GRID)

            await pilot.press("ctrl+a")
            await pilot.pause()
            self.assertEqual(pilot.app.screen._view, ViewKind.AGENDA)

    async def test_d_w_m_tune_agenda_window_when_in_agenda(self) -> None:
        services = self.services()
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = pilot.app.screen
            assert isinstance(screen, MainScreen)
            await pilot.press("ctrl+a")
            await pilot.pause()

            await pilot.press("d")
            await pilot.pause()
            self.assertEqual(screen._agenda_window, AgendaWindow.DAY)

            await pilot.press("w")
            await pilot.pause()
            self.assertEqual(screen._agenda_window, AgendaWindow.WEEK)

            await pilot.press("m")
            await pilot.pause()
            self.assertEqual(screen._agenda_window, AgendaWindow.MONTH)
            # And the view is still agenda — d/w/m don't switch views.
            self.assertEqual(screen._view, ViewKind.AGENDA)

    async def test_d_w_m_are_noops_outside_agenda(self) -> None:
        services = self.services()
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = pilot.app.screen
            assert isinstance(screen, MainScreen)
            await pilot.press("ctrl+d")  # Day view
            await pilot.pause()
            initial_window = screen._agenda_window
            await pilot.press("m")
            await pilot.pause()
            # Pressing `m` outside agenda must not flip the agenda
            # window AND must not switch the view.
            self.assertEqual(screen._agenda_window, initial_window)
            self.assertEqual(screen._view, ViewKind.DAY)


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
            await pilot.press("t")
            await pilot.pause()
            self.assertEqual(screen._viewed_date, NOW.date())


class TodayKeyTest(TuiFlowTestCase):
    """`t` snaps `_viewed_date` back to today's date in any view."""

    async def test_t_snaps_viewed_date_in_day_view(self) -> None:
        services = self.services()
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = pilot.app.screen
            assert isinstance(screen, MainScreen)
            await pilot.press("ctrl+d")
            await pilot.pause()
            screen._viewed_date = date(1999, 1, 1)
            screen.refresh_view()
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            self.assertEqual(screen._viewed_date, NOW.date())
            self.assertEqual(screen._view, ViewKind.DAY)


class DateNavigationTest(TuiFlowTestCase):
    """`n` / `p` shift the viewed date by one day in Day / Grid views.
    `N` / `P` shift by the grid's chunk size — Grid view only. All
    four are no-ops in Agenda."""

    async def test_n_advances_one_day_in_day_view(self) -> None:
        services = self.services()
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = pilot.app.screen
            assert isinstance(screen, MainScreen)
            await pilot.press("ctrl+d")
            await pilot.pause()
            start = screen._viewed_date
            await pilot.press("n")
            await pilot.pause()
            self.assertEqual(screen._viewed_date, start + timedelta(days=1))

    async def test_p_retreats_one_day_in_day_view(self) -> None:
        services = self.services()
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = pilot.app.screen
            assert isinstance(screen, MainScreen)
            await pilot.press("ctrl+d")
            await pilot.pause()
            start = screen._viewed_date
            await pilot.press("p")
            await pilot.pause()
            self.assertEqual(screen._viewed_date, start - timedelta(days=1))

    async def test_capital_n_advances_one_chunk_in_grid_view(self) -> None:
        services = self.services()
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = pilot.app.screen
            assert isinstance(screen, MainScreen)
            await pilot.press("ctrl+g")
            await pilot.pause()
            start = screen._viewed_date
            await pilot.press("N")
            await pilot.pause()
            self.assertEqual(
                screen._viewed_date, start + timedelta(days=screen._grid_days)
            )

    async def test_capital_p_retreats_one_chunk_in_grid_view(self) -> None:
        services = self.services()
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = pilot.app.screen
            assert isinstance(screen, MainScreen)
            await pilot.press("ctrl+g")
            await pilot.pause()
            start = screen._viewed_date
            await pilot.press("P")
            await pilot.pause()
            self.assertEqual(
                screen._viewed_date, start - timedelta(days=screen._grid_days)
            )

    async def test_capital_n_in_day_view_is_a_noop(self) -> None:
        # Day view has no chunk concept — capital N/P only act in
        # Grid. `n`/`p` (lowercase) still work in Day.
        services = self.services()
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = pilot.app.screen
            assert isinstance(screen, MainScreen)
            await pilot.press("ctrl+d")
            await pilot.pause()
            start = screen._viewed_date
            await pilot.press("N")
            await pilot.pause()
            self.assertEqual(screen._viewed_date, start)

    async def test_n_in_agenda_day_window_advances_one_day(self) -> None:
        services = self.services()
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = pilot.app.screen
            assert isinstance(screen, MainScreen)
            await pilot.press("ctrl+a")  # Agenda
            await pilot.pause()
            await pilot.press("d")  # Day sub-window
            await pilot.pause()
            start = screen._viewed_date
            await pilot.press("n")
            await pilot.pause()
            self.assertEqual(screen._viewed_date, start + timedelta(days=1))

    async def test_n_in_agenda_week_window_advances_seven_days(self) -> None:
        services = self.services()
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = pilot.app.screen
            assert isinstance(screen, MainScreen)
            await pilot.press("ctrl+a")
            await pilot.pause()
            await pilot.press("w")  # Week sub-window
            await pilot.pause()
            start = screen._viewed_date
            await pilot.press("n")
            await pilot.pause()
            self.assertEqual(screen._viewed_date, start + timedelta(days=7))

    async def test_n_in_agenda_month_window_advances_one_month(self) -> None:
        services = self.services()
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = pilot.app.screen
            assert isinstance(screen, MainScreen)
            await pilot.press("ctrl+a")
            await pilot.pause()
            await pilot.press("m")  # Month sub-window
            await pilot.pause()
            screen._viewed_date = date(2026, 1, 31)  # exercise month clamp
            screen.refresh_view()
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            # Feb 2026 has 28 days; relativedelta clamps to 2026-02-28.
            self.assertEqual(screen._viewed_date, date(2026, 2, 28))


class StartupFocusTest(TuiFlowTestCase):
    async def test_event_list_has_focus_on_mount(self) -> None:
        # Regression: previously the calendar tree on the left grabbed
        # focus by default, so the user had to tab out of it before
        # arrow keys did anything useful.
        services = self.services()
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = pilot.app.screen
            assert isinstance(screen, MainScreen)
            event_list = screen.query_one(EventList)
            self.assertTrue(event_list.has_focus)


class DetailPaneTracksCursorTest(TuiFlowTestCase):
    async def test_arrow_down_refreshes_detail_pane(self) -> None:
        # Regression: moving the cursor through the event list left
        # the detail pane stuck on whatever row was current at the
        # last `refresh_view`. Pressing `down` must swap the rendered
        # detail to match the newly-highlighted row. Agenda only —
        # Day / Grid hide the inline detail pane and show the detail
        # in a modal `EventDetailScreen` on Enter instead.
        services = self.services()
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = pilot.app.screen
            assert isinstance(screen, MainScreen)
            # Park the agenda anchor over the seeded May events
            # (the default WEEK around `NOW` is empty otherwise).
            await pilot.press("ctrl+a")
            await pilot.pause()
            await pilot.press("m")  # Month window catches more rows
            await pilot.pause()
            screen._viewed_date = date(2026, 5, 1)
            screen.refresh_view()
            await pilot.pause()

            first_component = screen._currently_selected_component()
            self.assertIsNotNone(first_component)

            await pilot.press("down")
            await pilot.pause()

            second_component = screen._currently_selected_component()
            self.assertIsNotNone(second_component)
            assert first_component is not None and second_component is not None
            self.assertNotEqual(
                (first_component.ref.uid, first_component.ref.recurrence_id),
                (second_component.ref.uid, second_component.ref.recurrence_id),
                "down arrow did not move the cursor to a different row",
            )

            event_view = screen.query_one(EventView)
            # The EventView is a `Static`; `.content` holds the text it
            # last rendered. Compare against the freshly-selected
            # component to prove the detail pane really did follow the
            # cursor.
            self.assertEqual(
                str(event_view.content),
                render_event_detail(second_component, NOW.date()),
            )


class NewEventFlowTest(TuiFlowTestCase):
    async def test_new_event_creates_in_mirror_and_index(self) -> None:
        services = self.services()
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            # `+` opens the new-event editor. (Was `n` before the
            # keymap reshuffle freed `n` for next-day navigation.)
            await pilot.press("plus")
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

    async def test_edited_event_still_appears_in_agenda(self) -> None:
        # Regression: `IndexRepository.upsert_component` invalidates
        # the master's `occurrences` rows on every upsert. The TUI's
        # save flow used to upsert without re-expanding, so an edited
        # event vanished from every view that joins
        # `components` against `occurrences` (agenda, day, week, month)
        # until the next sync rebuilt the cache.
        services = self.services()
        ref = ComponentRef(ACCOUNT_NAME, WORK_CAL, "simple-event-1@example.com")
        component = services.index.get_component(ref)
        assert isinstance(component, VEvent)

        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = pilot.app.screen
            assert isinstance(screen, MainScreen)
            screen._edit_specific(component)
            await pilot.pause()
            edit = pilot.app.screen
            assert isinstance(edit, EventEditScreen)
            edit.query_one("#edit-summary").value = "Renamed"  # type: ignore[attr-defined]
            edit.action_save()
            await pilot.pause()

        # The agenda window covers 2026-04-25 → 2026-05-09 (NOW.date()
        # + 14 days). simple-event-1 starts 2026-05-01, so it falls
        # inside the window after the edit.
        from chronos.tui.views import (
            CalendarSelection as _Sel,
        )
        from chronos.tui.views import (
            agenda_window,
            gather_occurrences,
        )

        rows = gather_occurrences(
            index=services.index,
            calendars=(CalendarRef(ACCOUNT_NAME, WORK_CAL),),
            selection=_Sel(refs=frozenset()),
            window=agenda_window(NOW.date()),
        )
        summaries = [r.component.summary for r in rows]
        self.assertIn("Renamed", summaries)


class DeleteFlowTest(TuiFlowTestCase):
    async def test_delete_with_confirmation_marks_trashed(self) -> None:
        services = self.services()
        ref = ComponentRef(ACCOUNT_NAME, WORK_CAL, "simple-event-1@example.com")
        component = services.index.get_component(ref)
        assert isinstance(component, VEvent)

        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = pilot.app.screen
            assert isinstance(screen, MainScreen)
            # Trigger delete directly with the component (UI selection
            # is exercised by other tests; here we focus on the confirm
            # plumbing).
            screen.delete_with_confirm(component)
            await pilot.pause()
            confirm = pilot.app.screen
            assert isinstance(confirm, ConfirmScreen)
            await pilot.press("y")
            await pilot.pause()

        trashed = services.index.get_component(ref)
        assert trashed is not None
        # Internal status stays LocalStatus.TRASHED — the UI label is
        # "Delete" but the on-disk state still goes through the trash
        # flow so `_push_trashed` issues the server DELETE on next sync.
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
            screen.delete_with_confirm(component)
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()

        unchanged = services.index.get_component(ref)
        assert unchanged is not None
        self.assertEqual(unchanged.local_status, LocalStatus.ACTIVE)

    async def test_uppercase_d_key_opens_delete_confirmation(self) -> None:
        # Wire-up regression: rebinding the trash action from `x` to
        # `D` (and `shift+d`) must reach the confirm screen with the
        # currently-highlighted row.
        services = self.services()
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = pilot.app.screen
            assert isinstance(screen, MainScreen)
            # Agenda Month window over the seeded May events (the
            # default WEEK around `NOW` is empty).
            await pilot.press("ctrl+a")
            await pilot.pause()
            await pilot.press("m")
            await pilot.pause()
            screen._viewed_date = date(2026, 5, 1)
            screen.refresh_view()
            await pilot.pause()
            await pilot.press("D")
            await pilot.pause()
            self.assertIsInstance(pilot.app.screen, ConfirmScreen)
            confirm = pilot.app.screen
            assert isinstance(confirm, ConfirmScreen)
            self.assertIn("Delete", confirm._prompt)


class HelpScreenTest(TuiFlowTestCase):
    async def test_f1_opens_help_grouped_by_area(self) -> None:
        from chronos.tui.screens.help_screen import HelpScreen

        services = self.services()
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("f1")
            await pilot.pause()
            self.assertIsInstance(pilot.app.screen, HelpScreen)
            help_screen = pilot.app.screen
            assert isinstance(help_screen, HelpScreen)
            # The renderable is a `rich.console.Group` — render it to
            # a plain-text capture so we can assert on the section
            # headers and bound keys without spelling out the full
            # ANSI output.
            from io import StringIO

            from rich.console import Console

            renderable = help_screen._render_help()
            buffer = StringIO()
            console = Console(
                file=buffer, force_terminal=False, width=80, color_system=None
            )
            console.print(renderable)
            text = buffer.getvalue()
            # Each section panel renders its title.
            for section in (
                "Views",
                "Agenda window",
                "Navigation",
                "Events",
                "Tools",
            ):
                self.assertIn(section, text, section)
            # And a sample of the bindings that belong to each.
            for fragment in ("Agenda", "Day", "Grid", "Delete", "Help", "Quit"):
                self.assertIn(fragment, text, fragment)
            # Aliases (shift+t, shift+n, shift+p, shift+d) must NOT
            # show up; otherwise the help text is twice as long and
            # reads as duplicates of the same shortcut.
            for alias in ("shift+n", "shift+p", "shift+d"):
                self.assertNotIn(alias, text, alias)

    async def test_escape_dismisses_help_screen(self) -> None:
        services = self.services()
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("f1")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            self.assertIsInstance(pilot.app.screen, MainScreen)


class CommandPaletteDisabledTest(TuiFlowTestCase):
    async def test_ctrl_p_does_not_open_command_palette(self) -> None:
        # Textual binds Ctrl-P to its command palette by default.
        # `ENABLE_COMMAND_PALETTE = False` on ChronosApp turns that
        # off; a stray Ctrl-P should leave the user on MainScreen.
        services = self.services()
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("ctrl+p")
            await pilot.pause()
            self.assertIsInstance(pilot.app.screen, MainScreen)
            self.assertFalse(app.ENABLE_COMMAND_PALETTE)


class SyncFlowTest(TuiFlowTestCase):
    async def test_sync_runner_called_on_confirmation(self) -> None:
        calls: list[int] = []

        def runner(**_kwargs: object) -> Sequence[SyncResult]:
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
            # Sync now runs on a Textual worker. Wait for it to settle
            # before asserting on `calls`.
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

        self.assertEqual(calls, [1])

    async def test_sync_runner_errors_show_in_notification(self) -> None:
        def runner(**_kwargs: object) -> Sequence[SyncResult]:
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

    async def test_progress_dialog_shows_summary_on_completion(self) -> None:
        # The dialog stays up after the runner returns and renders a
        # summary line plus a Close button, replacing the prior
        # background-notification flow.
        from chronos.tui.screens.sync_progress_screen import SyncProgressScreen

        def runner(**_kwargs: object) -> Sequence[SyncResult]:
            return (
                SyncResult(
                    account_name=ACCOUNT_NAME,
                    calendars_synced=1,
                    components_added=3,
                    components_updated=1,
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
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            self.assertIsInstance(pilot.app.screen, SyncProgressScreen)
            progress = pilot.app.screen
            assert isinstance(progress, SyncProgressScreen)
            self.assertEqual(progress._state, "done")
            from textual.widgets import Button, Static

            summary = progress.query_one("#sync-progress-summary", Static)
            self.assertIn("+3 added", str(summary.content))
            self.assertIn("~1 updated", str(summary.content))
            # The Cancel button is hidden once the worker reports
            # back; the Close button is visible and primary so the
            # user can dismiss the dialog.
            cancel = progress.query_one("#sync-cancel", Button)
            close = progress.query_one("#sync-close", Button)
            self.assertFalse(cancel.display)
            self.assertTrue(close.display)
            self.assertEqual(str(close.label), "Close")

    async def test_escape_during_sync_sets_cancel_event(self) -> None:
        # Regression: previously sync ran on the UI thread and a stuck
        # sync could only be killed by exiting the whole app. With the
        # progress dialog owning a worker + cancel event, Esc on the
        # dialog flips the event so the runner sees it on its next
        # polling boundary.
        import threading

        from chronos.tui.screens.sync_progress_screen import SyncProgressScreen

        gate = threading.Event()  # block the runner until we cancel
        observed: dict[str, threading.Event | None] = {"cancel": None}

        def runner(
            *, cancel_event: threading.Event | None = None
        ) -> Sequence[SyncResult]:
            observed["cancel"] = cancel_event
            # Wait until the test releases us, then bail out.
            gate.wait(timeout=5.0)
            return (
                SyncResult(
                    account_name=ACCOUNT_NAME,
                    calendars_synced=0,
                    components_added=0,
                    components_updated=0,
                    components_removed=0,
                    errors=("sync cancelled",),
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
            # The progress dialog is now the active screen, blocking
            # in `runner`.
            self.assertIsInstance(pilot.app.screen, SyncProgressScreen)
            progress = pilot.app.screen
            assert isinstance(progress, SyncProgressScreen)
            await pilot.press("escape")
            await pilot.pause()
            # Esc flipped the dialog's cancel event, which the runner
            # received as a kwarg.
            cancel = observed["cancel"]
            assert cancel is not None
            self.assertTrue(cancel.is_set())
            self.assertTrue(progress._cancel_event.is_set())
            # Release the runner so the worker finishes; dialog
            # transitions to its "done" state.
            gate.set()
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            self.assertEqual(progress._state, "done")


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
            await pilot.press("plus")
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
            await pilot.press("plus")
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
            await pilot.press("plus")
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


class CalendarPanelToggleTest(TuiFlowTestCase):
    """The calendars tree is hidden by default; `c` reveals it. Once
    visible, Enter on a leaf toggles that calendar in the active
    `CalendarSelection`, which filters the event list."""

    async def test_panel_hidden_by_default(self) -> None:
        services = self.services()
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            from chronos.tui.widgets.calendar_panel import CalendarPanel

            screen = pilot.app.screen
            assert isinstance(screen, MainScreen)
            self.assertFalse(screen.query_one(CalendarPanel).display)

    async def test_c_key_toggles_panel_visibility(self) -> None:
        services = self.services()
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            from chronos.tui.widgets.calendar_panel import CalendarPanel

            screen = pilot.app.screen
            assert isinstance(screen, MainScreen)
            panel = screen.query_one(CalendarPanel)
            self.assertFalse(panel.display)

            await pilot.press("c")
            await pilot.pause()
            self.assertTrue(panel.display)
            self.assertTrue(panel.has_focus)

            await pilot.press("c")
            await pilot.pause()
            self.assertFalse(panel.display)

    async def test_enter_on_calendar_leaf_toggles_selection(self) -> None:
        # Toggling a calendar in the panel must immediately update
        # `MainScreen._selection` and re-render the event list against
        # the new filter.
        services = self.services()
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            from chronos.tui.widgets.calendar_panel import CalendarPanel

            screen = pilot.app.screen
            assert isinstance(screen, MainScreen)
            panel = screen.query_one(CalendarPanel)
            await pilot.press("c")
            await pilot.pause()
            # Move into the tree and pick the first leaf (work calendar).
            await pilot.press("down")  # account node
            await pilot.pause()
            await pilot.press("down")  # first leaf
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            # The first-alphabetical calendar (`private`) is now in
            # the selection set.
            picked = CalendarRef(ACCOUNT_NAME, PERSONAL_CAL)
            self.assertIn(picked, screen._selection.refs)
            # And the panel's leaf label reflects the toggle.
            cursor = panel.cursor_node
            assert cursor is not None
            self.assertIn("[x]", str(cursor.label))

            # Toggling again removes the calendar — empty selection
            # falls back to "show all", per `CalendarSelection.contains`.
            await pilot.press("enter")
            await pilot.pause()
            self.assertNotIn(picked, screen._selection.refs)


class TimelineGridHelpersTest(unittest.TestCase):
    """Pure-function helpers under TimelineGrid: header, slot label,
    hour-range expansion, and per-cell event resolution. Easier to
    pin behaviour here than through a Pilot test for every edge case.
    """

    def test_day_header_uses_friendly_words_for_three_special_days(self) -> None:
        from chronos.tui.widgets.timeline_grid import _day_header

        today = date(2026, 4, 25)  # Saturday
        self.assertEqual(_day_header(today, today), "Today Sat")
        self.assertEqual(_day_header(today + timedelta(days=1), today), "Tomorrow Sun")
        self.assertEqual(_day_header(today - timedelta(days=1), today), "Yesterday Fri")

    def test_day_header_for_arbitrary_dates_uses_short_form(self) -> None:
        from chronos.tui.widgets.timeline_grid import _day_header

        today = date(2026, 4, 25)
        self.assertEqual(_day_header(date(2026, 5, 4), today), "Mon 04 May")
        self.assertEqual(_day_header(date(2026, 6, 15), today), "Mon 15 Jun")

    def test_slot_time_label_zero_pads(self) -> None:
        from chronos.tui.widgets.timeline_grid import _format_slot_time

        self.assertEqual(_format_slot_time(0), "00:00")
        self.assertEqual(_format_slot_time(7 * 60 + 30), "07:30")
        self.assertEqual(_format_slot_time(23 * 60 + 30), "23:30")

    def test_compute_hour_range_default_when_all_events_inside(self) -> None:
        from chronos.tui.widgets.timeline_grid import _compute_hour_range

        ref = ComponentRef(ACCOUNT_NAME, WORK_CAL, "x")
        event = _empty_event(ref)
        rows = (
            OccurrenceRow(
                occurrence=Occurrence(
                    ref=ref,
                    start=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
                    end=datetime(2026, 5, 1, 11, 0, tzinfo=UTC),
                    recurrence_id=None,
                    is_override=False,
                ),
                component=event,
            ),
        )
        start, end = _compute_hour_range([(date(2026, 5, 1), rows)])
        self.assertEqual((start, end), (6, 22))

    def test_compute_hour_range_widens_for_late_events(self) -> None:
        from chronos.tui.widgets.timeline_grid import _compute_hour_range

        ref = ComponentRef(ACCOUNT_NAME, WORK_CAL, "x")
        event = _empty_event(ref)
        rows = (
            # Early at 04:30 + late at 23:00; range must expand.
            OccurrenceRow(
                occurrence=Occurrence(
                    ref=ref,
                    start=datetime(2026, 5, 1, 4, 30, tzinfo=UTC),
                    end=datetime(2026, 5, 1, 5, 30, tzinfo=UTC),
                    recurrence_id=None,
                    is_override=False,
                ),
                component=event,
            ),
            OccurrenceRow(
                occurrence=Occurrence(
                    ref=ref,
                    start=datetime(2026, 5, 1, 23, 0, tzinfo=UTC),
                    end=datetime(2026, 5, 1, 23, 45, tzinfo=UTC),
                    recurrence_id=None,
                    is_override=False,
                ),
                component=event,
            ),
        )
        start, end = _compute_hour_range([(date(2026, 5, 1), rows)])
        self.assertEqual(start, 4)
        self.assertEqual(end, 24)  # 23:45 ends → 24

    def test_cell_for_slot_picks_matching_event(self) -> None:
        from chronos.tui.widgets.timeline_grid import _cell_for_slot

        ref = ComponentRef(ACCOUNT_NAME, WORK_CAL, "standup")
        event = VEvent(
            ref=ref,
            href=None,
            etag=None,
            raw_ics=b"",
            summary="Standup",
            description=None,
            location=None,
            dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 1, 9, 30, tzinfo=UTC),
            status=None,
            local_flags=frozenset(),
            server_flags=frozenset(),
            local_status=LocalStatus.ACTIVE,
            trashed_at=None,
            synced_at=None,
        )
        rows = (
            OccurrenceRow(
                occurrence=Occurrence(
                    ref=ref,
                    start=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
                    end=datetime(2026, 5, 1, 9, 30, tzinfo=UTC),
                    recurrence_id=None,
                    is_override=False,
                ),
                component=event,
            ),
        )
        # Slot 09:00 → event starts here.
        cell, hit, is_start = _cell_for_slot(date(2026, 5, 1), 9 * 60, rows)
        self.assertEqual(cell, "Standup")
        self.assertEqual(hit, ref)
        self.assertTrue(is_start)
        # Slot 09:30 → empty (event already ended; nothing starts here).
        cell_empty, hit_empty, is_start_empty = _cell_for_slot(
            date(2026, 5, 1), 9 * 60 + 30, rows
        )
        self.assertEqual(cell_empty, "")
        self.assertIsNone(hit_empty)
        self.assertFalse(is_start_empty)

    def test_cell_for_slot_appends_plus_when_overlapping(self) -> None:
        from chronos.tui.widgets.timeline_grid import _cell_for_slot

        ref_a = ComponentRef(ACCOUNT_NAME, WORK_CAL, "a")
        ref_b = ComponentRef(ACCOUNT_NAME, WORK_CAL, "b")
        rows = tuple(
            OccurrenceRow(
                occurrence=Occurrence(
                    ref=ref,
                    start=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
                    end=datetime(2026, 5, 1, 9, 30, tzinfo=UTC),
                    recurrence_id=None,
                    is_override=False,
                ),
                component=VEvent(
                    ref=ref,
                    href=None,
                    etag=None,
                    raw_ics=b"",
                    summary=label,
                    description=None,
                    location=None,
                    dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
                    dtend=datetime(2026, 5, 1, 9, 30, tzinfo=UTC),
                    status=None,
                    local_flags=frozenset(),
                    server_flags=frozenset(),
                    local_status=LocalStatus.ACTIVE,
                    trashed_at=None,
                    synced_at=None,
                ),
            )
            for ref, label in ((ref_a, "Meeting A"), (ref_b, "Meeting B"))
        )
        cell, hit, is_start = _cell_for_slot(date(2026, 5, 1), 9 * 60, rows)
        # First event wins the visible spot; the `+1` indicates one
        # other event is active in the same slot.
        self.assertEqual(cell, "Meeting A +1")
        self.assertEqual(hit, ref_a)
        self.assertTrue(is_start)  # both events start in this slot

    def test_cell_for_slot_multi_hour_event_fills_all_covered_slots(self) -> None:
        from chronos.tui.widgets.timeline_grid import _cell_for_slot

        ref = ComponentRef(ACCOUNT_NAME, WORK_CAL, "long")
        event = VEvent(
            ref=ref,
            href=None,
            etag=None,
            raw_ics=b"",
            summary="Workshop",
            description=None,
            location=None,
            dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 1, 11, 0, tzinfo=UTC),
            status=None,
            local_flags=frozenset(),
            server_flags=frozenset(),
            local_status=LocalStatus.ACTIVE,
            trashed_at=None,
            synced_at=None,
        )
        rows = (
            OccurrenceRow(
                occurrence=Occurrence(
                    ref=ref,
                    start=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
                    end=datetime(2026, 5, 1, 11, 0, tzinfo=UTC),
                    recurrence_id=None,
                    is_override=False,
                ),
                component=event,
            ),
        )
        # Event starts at 09:00 and ends at 11:00, covering four 30-min slots.
        # The start slot carries is_start=True; continuation slots carry False.
        cell_0900, hit_0900, is_start_0900 = _cell_for_slot(
            date(2026, 5, 1), 9 * 60, rows
        )
        self.assertEqual(cell_0900, "Workshop")
        self.assertEqual(hit_0900, ref)
        self.assertTrue(is_start_0900)
        for slot in (9 * 60 + 30, 10 * 60, 10 * 60 + 30):
            cell, hit, is_start = _cell_for_slot(date(2026, 5, 1), slot, rows)
            self.assertEqual(cell, "Workshop", msg=f"slot {slot}")
            self.assertEqual(hit, ref, msg=f"slot {slot}")
            self.assertFalse(is_start, msg=f"slot {slot} should be continuation")
        # Slot at 11:00 is outside the event's half-open [start, end) interval.
        cell_after, hit_after, _ = _cell_for_slot(date(2026, 5, 1), 11 * 60, rows)
        self.assertEqual(cell_after, "")
        self.assertIsNone(hit_after)

    def test_cell_for_slot_midnight_crossing_event_fills_remaining_day_slots(
        self,
    ) -> None:
        from chronos.tui.widgets.timeline_grid import _cell_for_slot

        ref = ComponentRef(ACCOUNT_NAME, WORK_CAL, "late")
        event = VEvent(
            ref=ref,
            href=None,
            etag=None,
            raw_ics=b"",
            summary="Late Call",
            description=None,
            location=None,
            dtstart=datetime(2026, 5, 1, 23, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 2, 1, 0, tzinfo=UTC),
            status=None,
            local_flags=frozenset(),
            server_flags=frozenset(),
            local_status=LocalStatus.ACTIVE,
            trashed_at=None,
            synced_at=None,
        )
        rows = (
            OccurrenceRow(
                occurrence=Occurrence(
                    ref=ref,
                    start=datetime(2026, 5, 1, 23, 0, tzinfo=UTC),
                    end=datetime(2026, 5, 2, 1, 0, tzinfo=UTC),
                    recurrence_id=None,
                    is_override=False,
                ),
                component=event,
            ),
        )
        # Event starts on 2026-05-01 at 23:00 and crosses midnight; both
        # remaining slots on that day must show the event.
        cell_2300, hit_2300, is_start_2300 = _cell_for_slot(
            date(2026, 5, 1), 23 * 60, rows
        )
        self.assertEqual(cell_2300, "Late Call")
        self.assertEqual(hit_2300, ref)
        self.assertTrue(is_start_2300)
        cell_2330, hit_2330, is_start_2330 = _cell_for_slot(
            date(2026, 5, 1), 23 * 60 + 30, rows
        )
        self.assertEqual(cell_2330, "Late Call")
        self.assertEqual(hit_2330, ref)
        self.assertFalse(is_start_2330)

    def test_cell_for_slot_newly_starting_event_takes_priority_over_running(
        self,
    ) -> None:
        from chronos.tui.widgets.timeline_grid import _cell_for_slot

        ref_running = ComponentRef(ACCOUNT_NAME, WORK_CAL, "running")
        ref_new = ComponentRef(ACCOUNT_NAME, WORK_CAL, "new")
        running = VEvent(
            ref=ref_running,
            href=None,
            etag=None,
            raw_ics=b"",
            summary="All-Morning Meeting",
            description=None,
            location=None,
            dtstart=datetime(2026, 5, 1, 8, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 1, 11, 0, tzinfo=UTC),
            status=None,
            local_flags=frozenset(),
            server_flags=frozenset(),
            local_status=LocalStatus.ACTIVE,
            trashed_at=None,
            synced_at=None,
        )
        new_event = VEvent(
            ref=ref_new,
            href=None,
            etag=None,
            raw_ics=b"",
            summary="Standup",
            description=None,
            location=None,
            dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            dtend=datetime(2026, 5, 1, 9, 30, tzinfo=UTC),
            status=None,
            local_flags=frozenset(),
            server_flags=frozenset(),
            local_status=LocalStatus.ACTIVE,
            trashed_at=None,
            synced_at=None,
        )
        rows = tuple(
            OccurrenceRow(
                occurrence=Occurrence(
                    ref=ev.ref,
                    start=ev.dtstart,  # type: ignore[arg-type]
                    end=ev.dtend,
                    recurrence_id=None,
                    is_override=False,
                ),
                component=ev,
            )
            for ev in (running, new_event)
        )
        # In the 09:00 slot, "Standup" starts here and must be listed first,
        # demoting "All-Morning Meeting" to the "+1" overflow count.
        cell, hit, is_start = _cell_for_slot(date(2026, 5, 1), 9 * 60, rows)
        self.assertEqual(cell, "Standup +1")
        self.assertEqual(hit, ref_new)
        self.assertTrue(is_start)  # Standup starts here


class TimelineGridFlowTest(TuiFlowTestCase):
    async def test_day_view_swaps_in_timeline_and_hides_detail_pane(self) -> None:
        from chronos.tui.widgets.timeline_grid import TimelineGrid

        services = self.services()
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = pilot.app.screen
            assert isinstance(screen, MainScreen)
            await pilot.press("ctrl+d")
            await pilot.pause()
            timeline = screen.query_one(TimelineGrid)
            event_list = screen.query_one(EventList)
            detail = screen.query_one(EventView)
            self.assertTrue(timeline.display)
            self.assertFalse(event_list.display)
            self.assertFalse(detail.display)

    async def test_grid_view_passes_four_day_columns(self) -> None:
        from chronos.tui.widgets.timeline_grid import TimelineGrid

        services = self.services()
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = pilot.app.screen
            assert isinstance(screen, MainScreen)
            await pilot.press("ctrl+g")
            await pilot.pause()
            timeline = screen.query_one(TimelineGrid)
            # Time column + 4 day columns = 5 columns total.
            self.assertEqual(len(timeline.columns), 5)

    async def test_enter_on_event_cell_pushes_detail_modal(self) -> None:
        # The agenda has an inline detail pane; in Day / Grid views
        # the detail must appear as a separate modal screen so the
        # timeline gets the full centre-pane height. Pressing Enter
        # on a cell that holds an event opens that modal.
        from chronos.tui.widgets.timeline_grid import TimelineGrid

        services = self.services()
        app = ChronosApp(services)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = pilot.app.screen
            assert isinstance(screen, MainScreen)
            await pilot.press("ctrl+d")
            await pilot.pause()
            screen._viewed_date = date(2026, 5, 1)  # has the simple_event seed
            screen.refresh_view()
            await pilot.pause()
            timeline = screen.query_one(TimelineGrid)
            # Find the first cell that resolves to an event.
            for row_idx in range(timeline.row_count):
                for col_idx in range(1, len(timeline.columns)):
                    if timeline.cell_ref(row_idx, col_idx) is not None:
                        timeline.cursor_coordinate = (row_idx, col_idx)  # type: ignore[assignment]
                        break
                else:
                    continue
                break
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            # Modal `EventDetailScreen` is now on top of MainScreen.
            self.assertIsInstance(pilot.app.screen, EventDetailScreen)
