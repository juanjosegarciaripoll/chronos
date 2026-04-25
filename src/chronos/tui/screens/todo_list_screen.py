from __future__ import annotations

from collections.abc import Sequence

from chronos.domain import CalendarRef, VTodo
from chronos.protocols import IndexRepository
from chronos.tui.views import CalendarSelection, gather_todos


def title_for() -> str:
    return "Todos"


def rows_for(
    *,
    index: IndexRepository,
    calendars: Sequence[CalendarRef],
    selection: CalendarSelection,
) -> tuple[VTodo, ...]:
    return gather_todos(index=index, calendars=calendars, selection=selection)


__all__ = ["rows_for", "title_for"]
