from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

from chronos.domain import CalendarRef
from chronos.protocols import IndexRepository
from chronos.tui.views import (
    CalendarSelection,
    OccurrenceRow,
    gather_occurrences,
    month_window,
)

_MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def title_for(viewed: date) -> str:
    return f"Month · {_MONTH_NAMES[viewed.month - 1]} {viewed.year}"


def window_for(viewed: date) -> tuple[datetime, datetime]:
    return month_window(viewed)


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
