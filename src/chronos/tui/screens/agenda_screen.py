from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

from chronos.domain import CalendarRef
from chronos.protocols import IndexRepository
from chronos.tui.views import (
    AgendaWindow,
    CalendarSelection,
    OccurrenceRow,
    day_window,
    gather_occurrences,
    month_window,
    week_window,
)


def title_for(today: date, mode: AgendaWindow) -> str:
    start, end = window_for(today, mode)
    label = {
        AgendaWindow.DAY: "Day",
        AgendaWindow.WEEK: "Week",
        AgendaWindow.MONTH: "Month",
    }[mode]
    return f"Agenda · {label} · {start.date().isoformat()} – {end.date().isoformat()}"


def window_for(today: date, mode: AgendaWindow) -> tuple[datetime, datetime]:
    """Map an `AgendaWindow` mode to a concrete date range.

    Calendar-aligned for week and month (Mon–Sun, calendar month) so
    the agenda label matches what users expect when they think "this
    week" or "this month" — not "the next 7/30 days".
    """
    if mode == AgendaWindow.DAY:
        return day_window(today)
    if mode == AgendaWindow.WEEK:
        return week_window(today)
    return month_window(today)


def rows_for(
    *,
    index: IndexRepository,
    calendars: Sequence[CalendarRef],
    selection: CalendarSelection,
    today: date,
    mode: AgendaWindow,
) -> tuple[OccurrenceRow, ...]:
    return gather_occurrences(
        index=index,
        calendars=calendars,
        selection=selection,
        window=window_for(today, mode),
    )


__all__ = ["rows_for", "title_for", "window_for"]
