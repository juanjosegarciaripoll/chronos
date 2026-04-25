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


def title_for(viewed: date, mode: AgendaWindow) -> str:
    start, end = window_for(viewed, mode)
    label = {
        AgendaWindow.DAY: "Day",
        AgendaWindow.WEEK: "Week",
        AgendaWindow.MONTH: "Month",
    }[mode]
    return f"Agenda · {label} · {start.date().isoformat()} – {end.date().isoformat()}"


def window_for(viewed: date, mode: AgendaWindow) -> tuple[datetime, datetime]:
    """Map an `AgendaWindow` mode to a concrete date range anchored
    at `viewed`.

    Calendar-aligned for week and month (Mon–Sun, calendar month) so
    the agenda label matches what users expect when they think "this
    week" or "this month" — not "the next 7/30 days".

    Anchored on `viewed` (not on "today") so `n` / `p` actually
    navigate: pressing `n` in Agenda Week shifts the agenda by 7
    days, the new `viewed` lands inside next week, and `week_window`
    snaps to that week's Monday.
    """
    if mode == AgendaWindow.DAY:
        return day_window(viewed)
    if mode == AgendaWindow.WEEK:
        return week_window(viewed)
    return month_window(viewed)


def rows_for(
    *,
    index: IndexRepository,
    calendars: Sequence[CalendarRef],
    selection: CalendarSelection,
    viewed: date,
    mode: AgendaWindow,
) -> tuple[OccurrenceRow, ...]:
    return gather_occurrences(
        index=index,
        calendars=calendars,
        selection=selection,
        window=window_for(viewed, mode),
    )


__all__ = ["rows_for", "title_for", "window_for"]
