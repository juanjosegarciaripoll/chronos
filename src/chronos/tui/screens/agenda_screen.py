from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta

from chronos.domain import CalendarRef
from chronos.protocols import IndexRepository
from chronos.tui.views import (
    DEFAULT_AGENDA_DAYS,
    CalendarSelection,
    OccurrenceRow,
    agenda_window,
    gather_occurrences,
)


def title_for(today: date, days: int = DEFAULT_AGENDA_DAYS) -> str:
    end = today + timedelta(days=days)
    return f"Agenda · {today.isoformat()} – {end.isoformat()}"


def window_for(
    today: date, days: int = DEFAULT_AGENDA_DAYS
) -> tuple[datetime, datetime]:
    return agenda_window(today, days)


def rows_for(
    *,
    index: IndexRepository,
    calendars: Sequence[CalendarRef],
    selection: CalendarSelection,
    today: date,
    days: int = DEFAULT_AGENDA_DAYS,
) -> tuple[OccurrenceRow, ...]:
    return gather_occurrences(
        index=index,
        calendars=calendars,
        selection=selection,
        window=window_for(today, days),
    )


__all__ = ["rows_for", "title_for", "window_for"]
