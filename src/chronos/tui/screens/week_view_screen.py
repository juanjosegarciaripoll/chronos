from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta

from chronos.domain import CalendarRef
from chronos.protocols import IndexRepository
from chronos.tui.views import (
    CalendarSelection,
    OccurrenceRow,
    gather_occurrences,
    week_window,
)


def title_for(viewed: date) -> str:
    monday = viewed - timedelta(days=viewed.weekday())
    sunday = monday + timedelta(days=6)
    return f"Week · {monday.isoformat()} – {sunday.isoformat()}"


def window_for(viewed: date) -> tuple[datetime, datetime]:
    return week_window(viewed)


def rows_for(
    *,
    index: IndexRepository,
    calendars: Sequence[CalendarRef],
    selection: CalendarSelection,
    viewed: date,
) -> tuple[OccurrenceRow, ...]:
    return gather_occurrences(
        index=index,
        calendars=calendars,
        selection=selection,
        window=window_for(viewed),
    )


__all__ = ["rows_for", "title_for", "window_for"]
