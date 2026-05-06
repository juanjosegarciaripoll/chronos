"""Time-axis grid for the Day and Grid views.

A `DataTable` with one time-of-day row per 30-min slot and one column
per day. Day view passes a single date; Grid view passes 3 or 4. Each
event lands in the cell that holds its start time; cells are
selectable, and pressing Enter posts a `Selected` message the parent
screen handles by pushing the existing `EventDetailScreen` modal.

Full-day items (VTodos and any synthesised midnight-to-midnight
occurrence) appear in a single "All day" banner row above the time
grid so they remain visible even in the timeline view.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, time, timedelta
from typing import Any

from rich.text import Text
from textual.message import Message
from textual.widgets import DataTable

from chronos.domain import ComponentRef
from chronos.tui.views import OccurrenceRow

_SLOT_MINUTES = 30
_DEFAULT_START_HOUR = 6
_DEFAULT_END_HOUR = 22
_TIME_COL_WIDTH = 6
_DAY_COL_WIDTH = 20
_ALL_DAY_LABEL = "all day"
_EVENT_END_CHAR = "\u2582"  # LOWER ONE QUARTER BLOCK


class TimelineGrid(DataTable[str]):
    """Time-axis-on-Y, days-on-X event grid.

    Cell-mode cursor; Enter on a cell that carries an event posts a
    `Selected` message with the event's `ComponentRef` so the parent
    screen can open the detail. Empty cells are no-op selections.
    """

    class Selected(Message):
        def __init__(self, ref: ComponentRef) -> None:
            super().__init__()
            self.ref = ref

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # (row_index, col_index) → ComponentRef for the event in that cell.
        self._cells: dict[tuple[int, int], ComponentRef] = {}
        # Per-column alternation flag: flipped each time a new event starts
        # in that column so adjacent back-to-back events get different shades.
        self._col_alt: dict[int, bool] = {}
        # Day column width — recomputed in show_days() from the widget's
        # actual pixel width so empty space to the right is minimised.
        self._day_col_width: int = _DAY_COL_WIDTH
        # Last args passed to show_days() so on_resize can re-render with
        # the correct width after the first layout pass (size is 0 during
        # on_mount, so the initial call uses the fallback _DAY_COL_WIDTH).
        self._last_days: Sequence[tuple[date, Sequence[OccurrenceRow]]] | None = None
        self._last_today: date | None = None

    def on_mount(self) -> None:
        self.cursor_type = "cell"
        self.zebra_stripes = False

    def on_resize(self) -> None:
        if self._last_days is None or self._last_today is None:
            return
        num_days = len(self._last_days)
        if num_days > 0:
            available = self.size.width - _TIME_COL_WIDTH
            new_width = max(_DAY_COL_WIDTH, available // num_days)
            if new_width == self._day_col_width:
                return
        self.show_days(self._last_days, today=self._last_today)

    def show_days(
        self,
        days: Sequence[tuple[date, Sequence[OccurrenceRow]]],
        *,
        today: date,
    ) -> None:
        """Replace the table contents with `days`'s events.

        `days` is an ordered sequence of `(date, rows)` pairs. The
        widget renders one column per pair, plus a leftmost time
        column. Pre-existing rows / columns are cleared first.
        """
        self._last_days = days
        self._last_today = today
        # Defer rendering until on_resize delivers the real width; avoids
        # a flash of narrow columns before the first layout pass completes.
        if self.size.width == 0:
            return
        self.clear(columns=True)
        self._cells.clear()
        self._col_alt.clear()
        if not days:
            self.add_column("(no days)")
            return

        num_days = len(days)
        available = self.size.width - _TIME_COL_WIDTH
        self._day_col_width = max(_DAY_COL_WIDTH, available // num_days)

        self.add_column("Time", width=_TIME_COL_WIDTH)
        for day_date, _ in days:
            self.add_column(_day_header(day_date, today), width=self._day_col_width)

        # Banner row: full-day events (VTodos / midnight-to-midnight)
        # for each day. Skipped silently when no day has any.
        self._add_all_day_row(days)

        start_hour, end_hour = _compute_hour_range(days)
        slot_count = ((end_hour - start_hour) * 60) // _SLOT_MINUTES
        for slot in range(slot_count):
            slot_minutes_in_day = (start_hour * 60) + slot * _SLOT_MINUTES
            self._add_time_row(slot_minutes_in_day, days)

    def cell_ref(self, row: int, col: int) -> ComponentRef | None:
        """Lookup the event ref at `(row, col)` if any. Used by tests."""
        return self._cells.get((row, col))

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        coord = event.coordinate
        ref = self._cells.get((coord.row, coord.column))
        if ref is None:
            return
        self.post_message(self.Selected(ref))

    # --- internal --------------------------------------------------------

    def _add_all_day_row(
        self, days: Sequence[tuple[date, Sequence[OccurrenceRow]]]
    ) -> None:
        all_day_per_column: list[tuple[str, ComponentRef | None]] = []
        any_present = False
        for day_date, events in days:
            cell, ref = _full_day_summary(day_date, events)
            all_day_per_column.append((cell, ref))
            if ref is not None:
                any_present = True
        if not any_present:
            return
        row_index = self.row_count
        time_label = Text(_ALL_DAY_LABEL, style="italic dim")
        cells: list[Any] = [time_label]
        for col_idx, (cell, ref) in enumerate(all_day_per_column, start=1):
            cells.append(cell)
            if ref is not None:
                self._cells[(row_index, col_idx)] = ref
        self.add_row(*cells)

    def _add_time_row(
        self,
        slot_minutes_in_day: int,
        days: Sequence[tuple[date, Sequence[OccurrenceRow]]],
    ) -> None:
        # Both 30-min slots of the same hour share the same stripe so the
        # grid reads as hourly bands.  Rich Text styles only colour actual
        # characters, not trailing whitespace, so every styled cell is
        # padded to the declared column width.
        row_index = self.row_count
        shaded = (slot_minutes_in_day // 60) % 2 == 1
        shaded_row_style = self._shaded_row_style()
        shaded_grid_bg_fg = self._shaded_grid_bg_fg()
        unshaded_grid_bg_fg = self._unshaded_grid_bg_fg()
        event_start_style = self._event_start_style()
        event_body_style = self._event_body_style()
        event_end_bg = self._event_fill_bg()
        # Only label the top of each hour; the :30 row is left blank so
        # the time column stays readable without clutter.
        is_hour = slot_minutes_in_day % 60 == 0
        time_text = _format_slot_time(slot_minutes_in_day) if is_hour else ""
        cells: list[Any] = [
            Text(time_text.ljust(_TIME_COL_WIDTH), style=shaded_row_style)
            if shaded
            else time_text
        ]
        for col_idx, (day_date, events) in enumerate(days, start=1):
            content, ref, is_start, is_end = _cell_for_slot(
                day_date, slot_minutes_in_day, events
            )
            if ref is not None:
                # Flip the alternation flag each time a new event starts so
                # back-to-back events always render in different shades.
                if is_start:
                    self._col_alt[col_idx] = not self._col_alt.get(col_idx, False)
                if is_start:
                    style = event_start_style
                elif is_end:
                    style = (
                        f"{shaded_grid_bg_fg} on {event_end_bg}"
                        if shaded
                        else f"{unshaded_grid_bg_fg} on {event_end_bg}"
                    )
                else:
                    style = event_body_style
                w = self._day_col_width
                if is_start:
                    text = content.ljust(w)
                elif is_end:
                    text = _EVENT_END_CHAR * w
                else:
                    text = " " * w
                cells.append(Text(text, style=style))
                self._cells[(row_index, col_idx)] = ref
            elif shaded:
                cells.append(Text(" " * self._day_col_width, style=shaded_row_style))
            else:
                cells.append("")
        self.add_row(*cells)

    def _theme_color(self, name: str, fallback: str) -> str:
        value = getattr(self.app.current_theme, name, None)
        if isinstance(value, str) and value:
            return value
        return fallback

    def _event_fill_bg(self) -> str:
        return self._theme_color("accent", "#0087AF")

    def _event_start_style(self) -> str:
        fg = self._theme_color("foreground", "#FFFFFF")
        return f"bold {fg} on {self._event_fill_bg()}"

    def _event_body_style(self) -> str:
        return f"on {self._event_fill_bg()}"

    def _shaded_row_style(self) -> str:
        # Hour stripe background follows theme panel tone.
        return f"on {self._theme_color('panel', '#303030')}"

    def _shaded_grid_bg_fg(self) -> str:
        # End-cap foreground must match shaded stripe background.
        return self._theme_color("panel", "#303030")

    def _unshaded_grid_bg_fg(self) -> str:
        # End-cap foreground must match unshaded grid background.
        return self._theme_color("background", "#1E1E1E")


# -- pure helpers (Layer-1 testable) --------------------------------------


def _day_header(day: date, today: date) -> str:
    """Column header for a day: 'Today Sat', 'Tomorrow Sun',
    'Yesterday Fri', or 'Mon 27 Apr' for everything else.
    Year is supplied by the view title and never repeats here."""
    delta = (day - today).days
    weekday = day.strftime("%a")
    if delta == 0:
        return f"Today {weekday}"
    if delta == 1:
        return f"Tomorrow {weekday}"
    if delta == -1:
        return f"Yesterday {weekday}"
    return day.strftime("%a %d %b")


def _format_slot_time(minutes_from_midnight: int) -> str:
    h, m = divmod(minutes_from_midnight, 60)
    return f"{h:02d}:{m:02d}"


def _compute_hour_range(
    days: Sequence[tuple[date, Sequence[OccurrenceRow]]],
) -> tuple[int, int]:
    """Default 06–22; widen if any non-full-day event in `days` falls
    outside that range so an early or late event isn't invisible."""
    start_hour = _DEFAULT_START_HOUR
    end_hour = _DEFAULT_END_HOUR
    for _, events in days:
        for row in events:
            if _is_full_day(row):
                continue
            occ_start = row.occurrence.start.astimezone()
            occ_end = (row.occurrence.end or row.occurrence.start).astimezone()
            start_hour = min(start_hour, occ_start.hour)
            # End-hour ceiling: if event ends at 22:30, we want a 22:30
            # row, so end_hour must be 23.
            tail_hour = occ_end.hour + (1 if occ_end.minute > 0 else 0)
            end_hour = max(end_hour, tail_hour)
    return start_hour, min(end_hour, 24)


def _cell_for_slot(
    day: date,
    slot_minutes_in_day: int,
    events: Sequence[OccurrenceRow],
) -> tuple[str, ComponentRef | None, bool, bool]:
    """Figure out what data belongs in the (day, slot) cell.

    A slot spans `[slot_minutes_in_day, slot_minutes_in_day + 30)`.
    Any event whose `[start, end)` interval overlaps is included, so a
    multi-hour event covers every slot it touches. Events that START in
    this slot are listed before those already running from an earlier
    slot; when several are active the first wins and a `+N` suffix
    indicates hidden extras.

    Returns `(summary, ref, is_start, is_end)`:
    - `summary`: the primary event title (always set when `ref` is not None).
    - `ref`: the event to open on Enter; `None` when the slot is empty.
    - `is_start`: True when the primary event begins in this slot,
      False for a continuation slot.  The renderer uses this to decide
      whether to display the title or just the coloured bar.
    - `is_end`: True when this continuation slot is the event's final
      slot. The renderer draws a low-profile end cap for visual
      separation from whatever follows.
    """
    slot_start = slot_minutes_in_day
    slot_end = slot_minutes_in_day + _SLOT_MINUTES
    starting: list[OccurrenceRow] = []
    continuing: list[OccurrenceRow] = []
    for row in events:
        if _is_full_day(row):
            continue
        occ_start = row.occurrence.start.astimezone()
        if occ_start.date() != day:
            continue
        occ_end_dt = (row.occurrence.end or row.occurrence.start).astimezone()
        start_min = occ_start.hour * 60 + occ_start.minute
        end_min = (
            24 * 60
            if occ_end_dt.date() > day
            else occ_end_dt.hour * 60 + occ_end_dt.minute
        )
        if start_min < slot_end and end_min > slot_start:
            if start_min >= slot_start:
                starting.append(row)
            else:
                continuing.append(row)
    active = starting + continuing
    if not active:
        return "", None, False, False
    first = active[0]
    summary = first.component.summary or "(no summary)"
    if len(active) > 1:
        summary = f"{summary} +{len(active) - 1}"
    first_start = first.occurrence.start.astimezone()
    first_end_dt = (first.occurrence.end or first.occurrence.start).astimezone()
    first_start_min = first_start.hour * 60 + first_start.minute
    first_end_min = (
        24 * 60 if first_end_dt.date() > day else first_end_dt.hour * 60 + first_end_dt.minute
    )
    is_start = first_start_min >= slot_start
    is_end = (not is_start) and first_end_min <= slot_end
    return summary, first.component.ref, is_start, is_end


def _full_day_summary(
    day: date,
    events: Sequence[OccurrenceRow],
) -> tuple[str, ComponentRef | None]:
    """Banner content for a single day's column.

    Returns the first full-day summary plus a `+N` suffix when there
    are several. Most days have at most one or two todos / VEvents
    spanning the day; degrading gracefully when there are more keeps
    the banner row a fixed height.
    """
    full_day: list[OccurrenceRow] = []
    for row in events:
        if not _is_full_day(row):
            continue
        if row.occurrence.start.astimezone(UTC).date() != day:
            continue
        full_day.append(row)
    if not full_day:
        return "", None
    first = full_day[0]
    summary = first.component.summary or "(no summary)"
    if len(full_day) > 1:
        summary = f"{summary} +{len(full_day) - 1}"
    return summary, first.component.ref


def _is_full_day(row: OccurrenceRow) -> bool:
    if row.occurrence.end is None:
        return False
    start_utc = row.occurrence.start.astimezone(UTC)
    end_utc = row.occurrence.end.astimezone(UTC)
    return (
        start_utc.time() == time.min
        and end_utc.time() == time.min
        and (end_utc - start_utc) >= timedelta(hours=23)
    )


__all__ = ["TimelineGrid"]
