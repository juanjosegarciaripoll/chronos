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

# Default chunk size for the multi-day grid. Phase 4 swaps this out
# for a terminal-width-aware choice (3 when narrow, 4 when wide); for
# now Phase 2 ships the grid view as a flat list, so the chunk size
# only governs how many days `N`/`P` advance at once.
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
