from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta

from chronos.domain import CalendarRef
from chronos.protocols import IndexRepository
from chronos.tui.views import (
    CalendarSelection,
    OccurrenceRow,
    gather_occurrences,
)

# Default multi-day grid width, used the first time the grid is opened.
# The user then picks any width from 2 to 7 days live with the `2`–`7`
# keys (the `1` key drops to the dedicated single-day view).
DEFAULT_GRID_DAYS = 4


def title_for(viewed: date, days: int = DEFAULT_GRID_DAYS) -> str:
    end = viewed + timedelta(days=days - 1)
    return f"Grid · {viewed.isoformat()} – {end.isoformat()}"


def window_for(
    viewed: date, days: int = DEFAULT_GRID_DAYS
) -> tuple[datetime, datetime]:
    # Anchored at local midnight (see views.day_window) so events at
    # local 00:00–02:00 land in the right day for users not in UTC.
    start = datetime.combine(viewed, time.min).astimezone()
    return start, start + timedelta(days=days)


def rows_for(
    *,
    index: IndexRepository,
    calendars: Sequence[CalendarRef],
    selection: CalendarSelection,
    viewed: date,
    days: int = DEFAULT_GRID_DAYS,
) -> tuple[OccurrenceRow, ...]:
    return gather_occurrences(
        index=index,
        calendars=calendars,
        selection=selection,
        window=window_for(viewed, days),
    )


__all__ = ["DEFAULT_GRID_DAYS", "rows_for", "title_for", "window_for"]
