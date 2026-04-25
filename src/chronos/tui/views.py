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
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    AGENDA = "agenda"
    TODOS = "todos"


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


def day_window(viewed: date) -> tuple[datetime, datetime]:
    start = datetime.combine(viewed, time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


def week_window(viewed: date) -> tuple[datetime, datetime]:
    monday = viewed - timedelta(days=viewed.weekday())
    start = datetime.combine(monday, time.min, tzinfo=UTC)
    return start, start + timedelta(days=7)


def month_window(viewed: date) -> tuple[datetime, datetime]:
    first = viewed.replace(day=1)
    if first.month == 12:
        nxt = first.replace(year=first.year + 1, month=1)
    else:
        nxt = first.replace(month=first.month + 1)
    start = datetime.combine(first, time.min, tzinfo=UTC)
    end = datetime.combine(nxt, time.min, tzinfo=UTC)
    return start, end


def agenda_window(
    today: date, days: int = DEFAULT_AGENDA_DAYS
) -> tuple[datetime, datetime]:
    start = datetime.combine(today, time.min, tzinfo=UTC)
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

    Trashed components are dropped. Result is sorted by start time, then
    by `(account, calendar, uid)` for deterministic output.
    """
    rows: list[OccurrenceRow] = []
    for calendar in calendars:
        if not selection.contains(calendar):
            continue
        occurrences = index.query_occurrences(calendar, window[0], window[1])
        if not occurrences:
            continue
        components = {c.ref: c for c in index.list_calendar_components(calendar)}
        for occ in occurrences:
            component = components.get(occ.ref)
            if component is None:
                continue
            if component.local_status == LocalStatus.TRASHED:
                continue
            rows.append(OccurrenceRow(occurrence=occ, component=component))
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

    Anchored to UTC for determinism — every fixture and live ICS in
    chronos stores instants as UTC. Switching to local time is a
    separate concern (needs a per-account or per-app TZ).
    """
    moment = start.astimezone(UTC)
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
        return f"{moment.strftime('%A')} {time_str}"
    if -7 < delta < -1:
        return f"Last {moment.strftime('%A')} {time_str}"
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


def format_event_row(row: OccurrenceRow, today: date) -> tuple[str, str, str, str, str]:
    when = format_friendly_start(row.occurrence.start, today)
    duration = format_duration(row.occurrence.start, row.occurrence.end)
    summary = row.component.summary or "(no summary)"
    calendar = row.component.ref.calendar_name
    location = row.component.location or ""
    return when, duration, summary, calendar, location


def format_todo_row(todo: VTodo) -> tuple[str, str, str, str]:
    due = (
        todo.due.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
        if todo.due is not None
        else ""
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


def render_event_detail(component: StoredComponent) -> str:
    lines = [
        f"Summary: {component.summary or '(no summary)'}",
        f"Account / Calendar: "
        f"{component.ref.account_name} / {component.ref.calendar_name}",
        f"UID: {component.ref.uid}",
    ]
    if component.location:
        lines.append(f"Location: {component.location}")
    if isinstance(component, VEvent):
        if component.dtstart is not None:
            lines.append(f"Start: {component.dtstart.astimezone(UTC).isoformat()}")
        if component.dtend is not None:
            lines.append(f"End: {component.dtend.astimezone(UTC).isoformat()}")
    else:
        if component.dtstart is not None:
            lines.append(f"Start: {component.dtstart.astimezone(UTC).isoformat()}")
        if component.due is not None:
            lines.append(f"Due: {component.due.astimezone(UTC).isoformat()}")
    if component.status:
        lines.append(f"Status: {component.status}")
    if component.description:
        lines.append("")
        lines.append(component.description)
    return "\n".join(lines)


__all__ = [
    "DEFAULT_AGENDA_DAYS",
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
