"""Calendar-style month grid: one row per week, one column per weekday.

Each cell shows the day number and as many of that day's events as fit
the row height, with a `+N more` line for the rest. The cursor moves
over days; Enter (or a click) asks the screen to open that day.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from typing import Any

from rich.text import Text
from textual.coordinate import Coordinate
from textual.message import Message
from textual.widgets import DataTable

from chronos.tui.screens.month_view_screen import grid_dates
from chronos.tui.views import OccurrenceRow, _is_full_day, is_in_progress
from chronos.tui.widgets.timeline_grid import _full_day_dates

_WEEKDAY_HEADERS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MIN_COL_WIDTH = 10
_MIN_ROW_HEIGHT = 3


class MonthGrid(DataTable[Text]):
    """Month grid of days. Post `DayChosen` / `DayHighlighted` messages."""

    class DayChosen(Message):
        """Enter or a click on a day."""

        def __init__(self, day: date) -> None:
            super().__init__()
            self.day = day

    class DayHighlighted(Message):
        """The cursor moved to a day."""

        def __init__(self, day: date) -> None:
            super().__init__()
            self.day = day

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._days: dict[tuple[int, int], date] = {}
        self._last: tuple[date, Sequence[OccurrenceRow], date, datetime] | None = None
        self._last_size: tuple[int, int] = (0, 0)

    def on_mount(self) -> None:
        self.cursor_type = "cell"
        self.zebra_stripes = False
        self.cell_padding = 0
        self.show_row_labels = False

    def on_resize(self) -> None:
        if self._last is not None and (self.size.width, self.size.height) != (
            self._last_size
        ):
            self.show_month(*self._last)

    def day_at(self, row: int, col: int) -> date | None:
        return self._days.get((row, col))

    def show_month(
        self,
        viewed: date,
        rows: Sequence[OccurrenceRow],
        today: date,
        now: datetime,
    ) -> None:
        """Render `viewed`'s month and put the cursor on `viewed`."""
        self._last = (viewed, rows, today, now)
        # Size is 0 until the first layout pass; on_resize re-renders.
        if self.size.width == 0 or self.size.height == 0:
            return
        self._last_size = (self.size.width, self.size.height)
        start, weeks = grid_dates(viewed)
        col_width = max(_MIN_COL_WIDTH, self.size.width // 7)
        # One line goes to the weekday header.
        row_height = max(_MIN_ROW_HEIGHT, (self.size.height - 1) // weeks)
        by_day = _bucket_by_day(rows, start, weeks)
        accent = self._accent()

        # Rebuilding moves the cursor through (0, 0) before it lands on
        # `viewed`; announcing that transit would make the screen jump
        # to the grid's first day.
        with self.prevent(DataTable.CellHighlighted):
            self._rebuild(
                start, weeks, viewed, by_day, today, now, col_width, row_height, accent
            )

    def _rebuild(
        self,
        start: date,
        weeks: int,
        viewed: date,
        by_day: dict[date, list[OccurrenceRow]],
        today: date,
        now: datetime,
        col_width: int,
        row_height: int,
        accent: str,
    ) -> None:
        self.clear(columns=True)
        self._days.clear()
        for name in _WEEKDAY_HEADERS:
            self.add_column(name, width=col_width)
        for week in range(weeks):
            cells: list[Text] = []
            for weekday in range(7):
                day = start + timedelta(days=7 * week + weekday)
                self._days[(week, weekday)] = day
                cells.append(
                    _render_day(
                        day,
                        by_day.get(day, []),
                        in_month=day.month == viewed.month,
                        today=today,
                        now=now,
                        width=col_width,
                        height=row_height,
                        accent=accent,
                    )
                )
            self.add_row(*cells, height=row_height)
        offset = (viewed - start).days
        self.cursor_coordinate = Coordinate(offset // 7, offset % 7)

    def on_data_table_cell_highlighted(self, event: DataTable.CellHighlighted) -> None:
        event.stop()
        day = self._days.get((event.coordinate.row, event.coordinate.column))
        if day is not None:
            self.post_message(self.DayHighlighted(day))

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        event.stop()
        day = self._days.get((event.coordinate.row, event.coordinate.column))
        if day is not None:
            self.post_message(self.DayChosen(day))

    def _accent(self) -> str:
        value = self.app.theme_variables.get("accent")
        if isinstance(value, str) and value and not value.startswith("auto"):
            return value
        return "yellow"


def _bucket_by_day(
    rows: Sequence[OccurrenceRow], start: date, weeks: int
) -> dict[date, list[OccurrenceRow]]:
    """Every shown day an occurrence covers, all-day items first."""
    end = start + timedelta(days=7 * weeks)
    out: dict[date, list[OccurrenceRow]] = {}
    for row in rows:
        first, last = _local_days(row)
        day = max(first, start)
        while day <= last and day < end:
            out.setdefault(day, []).append(row)
            day += timedelta(days=1)
    for bucket in out.values():
        bucket.sort(key=lambda r: (not _is_full_day(r.occurrence), r.occurrence.start))
    return out


def _local_days(row: OccurrenceRow) -> tuple[date, date]:
    """First and last local day an occurrence touches (end exclusive)."""
    occ = row.occurrence
    if _is_full_day(occ):
        first, end_exclusive = _full_day_dates(occ)
        return first, max(first, end_exclusive - timedelta(days=1))
    start = occ.start.astimezone()
    end = (occ.end or occ.start).astimezone()
    if end > start and end.time() == time.min:
        end -= timedelta(microseconds=1)
    return start.date(), max(start.date(), end.date())


def _render_day(
    day: date,
    events: Sequence[OccurrenceRow],
    *,
    in_month: bool,
    today: date,
    now: datetime,
    width: int,
    height: int,
    accent: str,
) -> Text:
    text = Text(no_wrap=True, overflow="ellipsis")
    label = f"{day.day:>2}"
    if day == today:
        text.append(f" {label} ", style=f"bold reverse {accent}")
    elif in_month:
        text.append(label, style="bold")
    else:
        text.append(label, style="dim")
    capacity = height - 1
    shown = events if len(events) <= capacity else events[: max(0, capacity - 1)]
    for row in shown:
        text.append("\n")
        line = _event_line(row, day)[:width]
        if is_in_progress(row.occurrence, now):
            text.append(line, style=f"bold {accent}")
        elif not in_month:
            text.append(line, style="dim")
        else:
            text.append(line)
    hidden = len(events) - len(shown)
    if hidden:
        text.append("\n")
        text.append(f"+{hidden} more"[:width], style="italic dim")
    return text


def _event_line(row: OccurrenceRow, day: date) -> str:
    summary = row.component.summary or "(no summary)"
    start = row.occurrence.start.astimezone()
    if _is_full_day(row.occurrence) or start.date() != day:
        return summary
    return f"{start:%H:%M} {summary}"


__all__ = ["MonthGrid"]
