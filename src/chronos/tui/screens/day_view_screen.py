from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

from chronos.domain import CalendarRef
from chronos.protocols import IndexRepository
from chronos.tui.views import (
    CalendarSelection,
    OccurrenceRow,
    day_window,
    gather_occurrences,
)


def title_for(viewed: date) -> str:
    return f"Day · {viewed.isoformat()}"


def window_for(viewed: date) -> tuple[datetime, datetime]:
    return day_window(viewed)


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
