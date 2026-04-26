"""Pure projection helpers used by every view screen.

Window arithmetic and index queries live here so tests exercise them
without spinning up a Textual app. Screens are then thin shells that
call these and render the results.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum

from rich.text import Text

from chronos.domain import (
    AppConfig,
    CalendarRef,
    LocalStatus,
    Occurrence,
    StoredComponent,
    VEvent,
    VTodo,
)
from chronos.protocols import IndexRepository, MirrorRepository

DEFAULT_AGENDA_DAYS = 14


class ViewKind(StrEnum):
    """Top-level view modes the user toggles via Ctrl-A / Ctrl-D / Ctrl-G.

    Older versions had separate Day/Week/Month/Todos views. Those
    collapsed into:

    - `AGENDA` — flat list, window controlled by `AgendaWindow`
    - `DAY`    — single-day timeline grid
    - `GRID`   — multi-day timeline grid (3 or 4 days, terminal-width
                 dependent)

    VTodos are rendered inline as full-day items in every view; there
    is no longer a dedicated Todos screen.
    """

    AGENDA = "agenda"
    DAY = "day"
    GRID = "grid"


class AgendaWindow(StrEnum):
    """Sub-mode of `ViewKind.AGENDA` controlled by `d` / `w` / `m`.

    Maps to the same windowing helpers the standalone Day/Week/Month
    views used to drive: `day_window`, `week_window`, `month_window`.
    """

    DAY = "day"
    WEEK = "week"
    MONTH = "month"


@dataclass(frozen=True, kw_only=True)
class CalendarSelection:
    """Set of (account, calendar) pairs the user is currently viewing.

    Empty selection means "all calendars across all accounts".
    """

    refs: frozenset[CalendarRef]

    def contains(self, ref: CalendarRef) -> bool:
        return not self.refs or ref in self.refs


@dataclass(frozen=True, kw_only=True)
class OccurrenceRow:
    """Joined occurrence + its parent component, ready to render."""

    occurrence: Occurrence
    component: StoredComponent


def _local_midnight(d: date) -> datetime:
    # datetime.combine produces a naive datetime; .astimezone() with no
    # argument presumes naive == system local and attaches the local
    # tzinfo. The result is a tz-aware "local midnight" for `d`. The
    # SQL layer converts back to UTC for the index query, so callers
    # don't need to do that themselves.
    return datetime.combine(d, time.min).astimezone()


def day_window(viewed: date) -> tuple[datetime, datetime]:
    start = _local_midnight(viewed)
    return start, start + timedelta(days=1)


def week_window(viewed: date) -> tuple[datetime, datetime]:
    monday = viewed - timedelta(days=viewed.weekday())
    start = _local_midnight(monday)
    return start, start + timedelta(days=7)


def month_window(viewed: date) -> tuple[datetime, datetime]:
    first = viewed.replace(day=1)
    if first.month == 12:
        nxt = first.replace(year=first.year + 1, month=1)
    else:
        nxt = first.replace(month=first.month + 1)
    return _local_midnight(first), _local_midnight(nxt)


def agenda_window(
    today: date, days: int = DEFAULT_AGENDA_DAYS
) -> tuple[datetime, datetime]:
    start = _local_midnight(today)
    return start, start + timedelta(days=days)


def all_calendar_refs(
    config: AppConfig, mirror: MirrorRepository
) -> tuple[CalendarRef, ...]:
    refs: list[CalendarRef] = []
    for account in config.accounts:
        for calendar_name in mirror.list_calendars(account.name):
            refs.append(CalendarRef(account.name, calendar_name))
    return tuple(refs)


def gather_occurrences(
    *,
    index: IndexRepository,
    calendars: Iterable[CalendarRef],
    selection: CalendarSelection,
    window: tuple[datetime, datetime],
) -> tuple[OccurrenceRow, ...]:
    """Query each calendar for occurrences in `window`, joining components.

    Trashed components are dropped. VTodos with a `due` (or `dtstart`)
    inside the window are injected as synthetic full-day rows so the
    user sees their tasks alongside events without a separate todos
    view. Result is sorted by start time, then by
    `(account, calendar, uid)` for deterministic output.
    """
    rows: list[OccurrenceRow] = []
    for calendar in calendars:
        if not selection.contains(calendar):
            continue
        components = {c.ref: c for c in index.list_calendar_components(calendar)}
        occurrences = index.query_occurrences(calendar, window[0], window[1])
        for occ in occurrences:
            component = components.get(occ.ref)
            if component is None:
                continue
            if component.local_status == LocalStatus.TRASHED:
                continue
            rows.append(OccurrenceRow(occurrence=occ, component=component))
        # Inject VTodos as full-day rows. The `occurrences` cache
        # only ever contains VEvents (sync's `populate_occurrences`
        # iterates VEvent masters), so we don't double-count by
        # adding VTodos here.
        for component in components.values():
            if not isinstance(component, VTodo):
                continue
            if component.local_status != LocalStatus.ACTIVE:
                continue
            anchor = component.due or component.dtstart
            if anchor is None:
                continue
            if not (window[0] <= anchor < window[1]):
                continue
            day_start = datetime.combine(
                anchor.astimezone(UTC).date(), time.min, tzinfo=UTC
            )
            rows.append(
                OccurrenceRow(
                    occurrence=Occurrence(
                        ref=component.ref,
                        start=day_start,
                        end=day_start + timedelta(days=1),
                        recurrence_id=None,
                        is_override=False,
                    ),
                    component=component,
                )
            )
    rows.sort(key=_row_sort_key)
    return tuple(rows)


def gather_todos(
    *,
    index: IndexRepository,
    calendars: Iterable[CalendarRef],
    selection: CalendarSelection,
) -> tuple[VTodo, ...]:
    out: list[VTodo] = []
    for calendar in calendars:
        if not selection.contains(calendar):
            continue
        for component in index.list_calendar_components(calendar):
            if (
                isinstance(component, VTodo)
                and component.local_status == LocalStatus.ACTIVE
            ):
                out.append(component)
    out.sort(key=_todo_sort_key)
    return tuple(out)


def format_friendly_start(start: datetime, today: date) -> str:
    """Render a datetime using day-of-week / Today / Tomorrow shortcuts.

    Converts to the system local timezone so displayed times match the
    clock on the user's machine.
    """
    moment = start.astimezone()
    moment_date = moment.date()
    delta = (moment_date - today).days
    time_str = moment.strftime("%H:%M")
    if delta == 0:
        return f"Today {time_str}"
    if delta == 1:
        return f"Tomorrow {time_str}"
    if delta == -1:
        return f"Yesterday {time_str}"
    if 1 < delta < 7:
        return f"{moment.strftime('%a')} {time_str}"
    if -7 < delta < -1:
        return f"Last {moment.strftime('%a')} {time_str}"
    if moment.year == today.year:
        return f"{moment.strftime('%a %d %b')} {time_str}"
    return f"{moment.strftime('%a %d %b %Y')} {time_str}"


def format_duration(start: datetime, end: datetime | None) -> str:
    """Compact human-readable duration: 30m, 1h, 1h30m, 1d, 1d6h."""
    if end is None:
        return ""
    seconds = int((end - start).total_seconds())
    if seconds <= 0:
        return ""
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    return "".join(parts) or "0m"


def format_event_row(
    row: OccurrenceRow,
    today: date,
    *,
    now: datetime | None = None,
) -> tuple[str | Text, str | Text, str | Text, str | Text, str | Text, str | Text]:
    """Six cells for the agenda DataTable: Day, Time, Duration,
    Summary, Calendar, Location.

    Day and Time are split into separate columns so `EventList` can
    blank the Day cell on rows that share the previous row's day,
    producing a paper-agenda look where each day is announced once.
    The view title carries the year, so the Day cell never repeats it.

    When `now` is supplied and the occurrence has fully ended (its
    `end`, or `start` if there's no end, is strictly before `now`),
    every cell is wrapped in a Rich `Text` with a `dim` style so the
    row renders muted. In-progress and future rows return plain
    strings — DataTable accepts a mix of `str` and `Text` cells.

    VTodo rows (and any synthesised full-day occurrence) get a
    📋 marker on the summary, "all day" in the Time column, and an
    empty Duration cell so the eye doesn't see "1d" repeated for
    every todo.
    """
    is_full_day = _is_full_day(row.occurrence)
    is_todo = isinstance(row.component, VTodo)
    day = _format_event_day(row.occurrence.start, today)
    if is_full_day:
        event_time = "all day"
        duration = ""
    else:
        event_time = row.occurrence.start.astimezone().strftime("%H:%M")
        duration = format_duration(row.occurrence.start, row.occurrence.end)
    raw_summary = row.component.summary or "(no summary)"
    summary = f"📋 {raw_summary}" if is_todo else raw_summary
    calendar = row.component.ref.calendar_name
    location = row.component.location or ""
    cells = (day, event_time, duration, summary, calendar, location)
    if now is None or not _occurrence_is_past(row.occurrence, now):
        return cells
    dimmed = tuple(Text(c, style="dim") for c in cells)
    return dimmed[0], dimmed[1], dimmed[2], dimmed[3], dimmed[4], dimmed[5]


def _format_event_day(start: datetime, today: date) -> str:
    """Day label for the agenda Day column.

    `Yesterday` / `Today` / `Tomorrow` for those three special days;
    everything else as `DD MMM ddd` ("26 May Tue", "30 Apr Wed").
    The format is fixed-width-ish (10 chars max) so the column
    width pin in `EventList` stays predictable, and never includes
    the year — the view title carries that.
    """
    moment = start.astimezone()
    delta = (moment.date() - today).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Tomorrow"
    if delta == -1:
        return "Yesterday"
    return moment.strftime("%d %b %a")


def _is_full_day(occurrence: Occurrence) -> bool:
    """A row spans midnight-to-midnight UTC."""
    if occurrence.end is None:
        return False
    start_utc = occurrence.start.astimezone(UTC)
    end_utc = occurrence.end.astimezone(UTC)
    return (
        start_utc.time() == time.min
        and end_utc.time() == time.min
        and (end_utc - start_utc) >= timedelta(hours=23)
    )


def _occurrence_is_past(occurrence: Occurrence, now: datetime) -> bool:
    boundary = occurrence.end or occurrence.start
    return boundary < now


def format_todo_row(todo: VTodo) -> tuple[str, str, str, str]:
    due = (
        todo.due.astimezone().strftime("%Y-%m-%d %H:%M") if todo.due is not None else ""
    )
    summary = todo.summary or "(no summary)"
    calendar = todo.ref.calendar_name
    status = todo.status or ""
    return due, summary, calendar, status


def search_components(
    *,
    components: Sequence[StoredComponent],
    query: str,
) -> tuple[StoredComponent, ...]:
    """Substring search over summary/description/location.

    Used by `SearchDialogScreen` for the live search box. The full
    `index.search()` FTS path is reserved for the MCP server; the TUI
    runs in-memory over already-loaded components for responsiveness
    and to avoid re-querying SQLite on every keystroke.
    """
    needle = query.strip().lower()
    if not needle:
        return ()
    out: list[StoredComponent] = []
    for component in components:
        if component.local_status == LocalStatus.TRASHED:
            continue
        haystack = " ".join(
            field
            for field in (
                component.summary,
                component.description,
                component.location,
            )
            if field
        ).lower()
        if needle in haystack:
            out.append(component)
    return tuple(out)


def _row_sort_key(row: OccurrenceRow) -> tuple[datetime, str, str, str]:
    return (
        row.occurrence.start,
        row.component.ref.account_name,
        row.component.ref.calendar_name,
        row.component.ref.uid,
    )


def _todo_sort_key(todo: VTodo) -> tuple[datetime, str]:
    due = todo.due or datetime.max.replace(tzinfo=UTC)
    return due, todo.ref.uid


_DETAIL_LABEL_WIDTH = len("Location:")  # the longest label in the grid


def render_event_detail(component: StoredComponent, today: date) -> str:
    """Multi-line component summary used by the detail pane.

    Layout (labels right-aligned so the colons line up):

         Summary: <text>
          Source: <calendar> (<account>)
        Location: <text or '(no location)'>
           Start: <friendly date + time or '(not set)'>
             End: <friendly date + time or '(not set)'>
          Status: <status>          # only when present

        Notes:
        <description or '(no notes)'>

    Times go through `format_friendly_start` so 'Today 09:00' /
    'Tomorrow 14:00' / 'Tue 09:00' / 'Wed 15 May 09:00' shorthand
    matches the row list. The iCal `UID` is not shown — it's an
    implementation detail of the storage layer, not user-facing data.
    """
    lines: list[str] = [
        _detail_field("Summary", component.summary or "(no summary)"),
        _detail_field(
            "Source",
            f"{component.ref.calendar_name} ({component.ref.account_name})",
        ),
        _detail_field("Location", component.location or "(no location)"),
    ]
    if isinstance(component, VEvent):
        lines.append(_detail_field("Start", _detail_when(component.dtstart, today)))
        lines.append(_detail_field("End", _detail_when(component.dtend, today)))
    else:
        if component.dtstart is not None:
            lines.append(_detail_field("Start", _detail_when(component.dtstart, today)))
        lines.append(_detail_field("Due", _detail_when(component.due, today)))
    if component.status:
        lines.append(_detail_field("Status", component.status))
    lines.extend(["", "Notes:", component.description or "(no notes)"])
    return "\n".join(lines)


def _detail_field(label: str, value: str) -> str:
    # Right-align so the colons line up; values then start at the
    # same column on every row.
    return f"{label + ':':>{_DETAIL_LABEL_WIDTH}} {value}"


def _detail_when(value: datetime | None, today: date) -> str:
    if value is None:
        return "(not set)"
    return format_friendly_start(value, today)


__all__ = [
    "DEFAULT_AGENDA_DAYS",
    "AgendaWindow",
    "CalendarSelection",
    "OccurrenceRow",
    "ViewKind",
    "agenda_window",
    "all_calendar_refs",
    "day_window",
    "format_duration",
    "format_event_row",
    "format_friendly_start",
    "format_todo_row",
    "gather_occurrences",
    "gather_todos",
    "month_window",
    "render_event_detail",
    "search_components",
    "week_window",
]
