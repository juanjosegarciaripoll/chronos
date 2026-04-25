from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, cast

from rich.text import Text
from textual.widgets import DataTable

from chronos.domain import ComponentRef, StoredComponent, VTodo
from chronos.tui.views import OccurrenceRow, format_event_row, format_todo_row

# (header, fixed width). `width=None` would mean "auto-size to the
# wider of header / cell content", but Textual 8 sizes the column to
# the header at `add_column` time and doesn't grow to fit later rows
# — that's how we ended up with "Toda" / "Yest" truncations before.
# Explicit widths chosen for worst-case content:
#   Day      "30 Apr Wed"  → 10 chars
#   Time     "all day"     → 7 chars
#   Duration "1d23h"       → 5 chars (header "Duration" is the bound at 8)
#   Calendar account-supplied label → ~16 covers most names
# Summary and Location stay flexible so they absorb the residual
# terminal width.
_EVENT_COLUMNS_FULL: tuple[tuple[str, int | None], ...] = (
    ("Day", 12),
    ("Time", 8),
    ("Duration", 8),
    ("Summary", None),
    ("Calendar", 16),
    ("Location", None),
)
# Compact form for the Agenda view: Calendar and Location go away so
# Summary gets the screen width. The detail pane on the right still
# carries the calendar / location info for whichever row is selected,
# so nothing is lost — the agenda just stops repeating it on every
# line.
_EVENT_COLUMNS_AGENDA: tuple[tuple[str, int | None], ...] = (
    ("Day", 12),
    ("Time", 8),
    ("Duration", 8),
    ("Summary", None),
)
_TODO_COLUMNS: tuple[tuple[str, int | None], ...] = (
    ("Due", 22),
    ("Summary", None),
    ("Calendar", 16),
    ("Status", 14),
)


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
        compact: bool = False,
    ) -> None:
        """Render `rows` as agenda-style entries.

        `compact=True` drops the Calendar and Location columns —
        used by the Agenda view, where the detail pane on the right
        carries that info for the highlighted row.
        """
        columns = _EVENT_COLUMNS_AGENDA if compact else _EVENT_COLUMNS_FULL
        self._reset(columns, "events")
        # The Day cell is blanked on rows that share the previous
        # row's day, so each day is announced once and the eye reads
        # the column as a paper-agenda-style header. Rows are already
        # sorted by start time in `gather_occurrences`, so a simple
        # last-seen tracker is enough.
        prev_day_plain: str | None = None
        for row in rows:
            key = self._row_key(
                row.component.ref,
                row.occurrence.recurrence_id,
                instance=row.occurrence.start.isoformat(),
            )
            cells = format_event_row(row, today, now=now)
            day_cell = cells[0]
            day_plain = day_cell.plain if isinstance(day_cell, Text) else day_cell
            if day_plain == prev_day_plain:
                day_cell = Text("", style="dim") if isinstance(day_cell, Text) else ""
            else:
                prev_day_plain = day_plain
            full_row = (day_cell, *cells[1:])
            # Slice to whichever column set is active so compact mode
            # actually drops the trailing cells instead of feeding
            # `add_row` more values than there are columns.
            sliced = full_row[: len(columns)]
            # DataTable accepts `Text` cells at runtime, but its public
            # type stub only declares `str`. Past-event rows come back
            # as `Text(..., style="dim")` so the row renders muted; the
            # cast keeps the type-checker happy without lying about the
            # element type at the source.
            rendered = cast(tuple[Any, ...], sliced)
            self.add_row(*rendered, key=key)
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

    def _reset(self, columns: tuple[tuple[str, int | None], ...], mode: str) -> None:
        self.clear(columns=True)
        self._refs = {}
        for label, width in columns:
            self.add_column(label, width=width)
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
