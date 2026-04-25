from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, cast

from textual.widgets import DataTable

from chronos.domain import ComponentRef, StoredComponent, VTodo
from chronos.tui.views import OccurrenceRow, format_event_row, format_todo_row

_EVENT_COLUMNS = ("When", "Duration", "Summary", "Calendar", "Location")
_TODO_COLUMNS = ("Due", "Summary", "Calendar", "Status")


class EventList(DataTable[str]):
    """A `DataTable` of events or todos, keyed by `ComponentRef`.

    Rendering is backed by `views.format_event_row` /
    `views.format_todo_row`, so the formatting logic stays unit-testable.
    """

    def on_mount(self) -> None:  # noqa: D401 — Textual lifecycle.
        self.cursor_type = "row"
        self.zebra_stripes = True
        self._mode: str | None = None
        self._refs: dict[str, ComponentRef] = {}

    def show_events(
        self,
        rows: Sequence[OccurrenceRow],
        *,
        today: date,
        now: datetime | None = None,
    ) -> None:
        self._reset(_EVENT_COLUMNS, "events")
        for row in rows:
            key = self._row_key(
                row.component.ref,
                row.occurrence.recurrence_id,
                instance=row.occurrence.start.isoformat(),
            )
            cells = format_event_row(row, today, now=now)
            # DataTable accepts `Text` cells at runtime, but its public
            # type stub only declares `str`. Past-event rows come back
            # as `Text(..., style="dim")` so the row renders muted; the
            # cast keeps the type-checker happy without lying about the
            # element type at the source.
            self.add_row(*cast(tuple[Any, ...], cells), key=key)
            self._refs[key] = row.component.ref

    def show_todos(self, todos: Sequence[VTodo]) -> None:
        self._reset(_TODO_COLUMNS, "todos")
        for todo in todos:
            key = self._row_key(todo.ref, None)
            self.add_row(*format_todo_row(todo), key=key)
            self._refs[key] = todo.ref

    def selected_ref(self) -> ComponentRef | None:
        if self.row_count == 0:
            return None
        try:
            row_key, _ = self.coordinate_to_cell_key(self.cursor_coordinate)
        except (KeyError, IndexError):
            return None
        if row_key.value is None:
            return None
        return self._refs.get(row_key.value)

    def _reset(self, columns: tuple[str, ...], mode: str) -> None:
        self.clear(columns=True)
        self._refs = {}
        for column in columns:
            self.add_column(column)
        self._mode = mode

    @staticmethod
    def _row_key(
        ref: ComponentRef, recurrence_id: str | None, *, instance: str | None = None
    ) -> str:
        suffix = f"|{recurrence_id}" if recurrence_id else ""
        instance_suffix = f"@{instance}" if instance else ""
        return (
            f"{ref.account_name}|{ref.calendar_name}|{ref.uid}{suffix}{instance_suffix}"
        )


def component_ref_for_row(component: StoredComponent) -> ComponentRef:
    """Convenience used by tests: return a stable ref for a stored component."""
    return component.ref


__all__ = ["EventList", "component_ref_for_row"]
