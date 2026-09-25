"""Time-axis grid for the Day and Grid views.

A `DataTable` with one time-of-day row per 30-min slot and one column
per day. Day view passes a single date; Grid view passes 3 or 4. Each
event lands in the cell that holds its start time. Empty timed cells
can be clicked or dragged to create an event, while timed events can
be clicked to open or dragged to reschedule.

Full-day items (VTodos and any synthesised midnight-to-midnight
occurrence) appear in a single "All day" banner row above the time
grid so they remain visible even in the timeline view.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, NamedTuple

from rich.text import Text
from textual.color import Color
from textual.coordinate import Coordinate
from textual.events import MouseDown, MouseEvent, MouseMove, MouseUp
from textual.message import Message
from textual.widgets import DataTable

from chronos.domain import ComponentRef, Occurrence
from chronos.mutations import all_day_bounds
from chronos.tui.views import IN_PROGRESS_MARK, OccurrenceRow, in_progress_keys
from chronos.tui.views import _is_full_day as _occurrence_is_full_day

_SLOT_MINUTES = 30
_DEFAULT_START_HOUR = 6
_DEFAULT_END_HOUR = 22
_TIME_COL_WIDTH = 6
_DAY_COL_WIDTH = 20
_ALL_DAY_LABEL = "all day"
_HOUR_MARKER_CHAR = "\u2594"  # UPPER ONE EIGHTH BLOCK
_EVENT_END_CHAR = "\u2582"  # LOWER ONE QUARTER BLOCK
_DRAG_PREVIEW_CHAR = "\u2591"  # LIGHT SHADE
_NOW_LINE_CHAR = "\u2500"  # BOX DRAWINGS LIGHT HORIZONTAL


class _Palette(NamedTuple):
    """Concrete colours for one render, resolved from the active theme.

    `fill_a`/`fill_b` are the two event-fill shades (alternated so adjacent
    events read apart); `fg_a`/`fg_b` are their contrast-matched title
    colours; `hour_marker` and `end_fg` are the round-hour marker and the
    event end-cap foreground. `now` marks the current slot (its time
    label and an empty today cell) and fills the event in progress,
    whose title is drawn in `now_fg`.
    """

    fill_a: str
    fill_b: str
    fg_a: str
    fg_b: str
    hour_marker: str
    end_fg: str
    now: str
    now_fg: str


class _Now(NamedTuple):
    """Where the current moment falls in the rendered grid."""

    col: int  # column of today's date
    slot_minutes: int  # start of the current 30-min slot, minutes from midnight
    active: frozenset[ComponentRef]  # events in progress in that column


def _contrast_fg(background: str) -> str:
    """Black or white \u2014 whichever reads better on `background`.

    Mirrors what Textual's `Color.get_contrast_text` does, but returns a
    plain 6-digit hex so the result is always safe inside a Rich style.
    Rec. 601 luma: bright fills get black text, dark fills get white.
    """
    colour = Color.parse(background)
    luma = 0.299 * colour.r + 0.587 * colour.g + 0.114 * colour.b
    return "#000000" if luma > 140 else "#FFFFFF"


class TimelineGrid(DataTable[str | Text]):
    """Time-axis-on-Y, days-on-X event grid.

    Cell-mode cursor; Enter or a mouse click on an event posts a
    `Selected` message. Mouse gestures on timed cells post create/move
    requests for the parent screen to persist.
    """

    class Selected(Message):
        def __init__(self, ref: ComponentRef) -> None:
            super().__init__()
            self.ref = ref

    class CreateRequested(Message):
        """A mouse selection over empty time slots or all-day cells.

        For `all_day`, `start`/`end` are the `all_day_bounds` of the
        selected days (UTC midnight, end exclusive).
        """

        def __init__(
            self, start: datetime, end: datetime, *, all_day: bool = False
        ) -> None:
            super().__init__()
            self.start = start
            self.end = end
            self.all_day = all_day

    class MoveRequested(Message):
        """A timed event dragged by ``delta`` on the grid."""

        def __init__(self, ref: ComponentRef, delta: timedelta) -> None:
            super().__init__()
            self.ref = ref
            self.delta = delta

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
        self._last_now: datetime | None = None
        # Timed cells map to their local half-hour start. All-day rows and
        # the time-label column deliberately have no entry, so their normal
        # click behaviour remains untouched.
        self._slot_starts: dict[tuple[int, int], datetime] = {}
        # Every cell of the "all day" banner maps to its column's date, so
        # empty ones can start an all-day event (click or sideways drag).
        self._all_day_dates: dict[tuple[int, int], date] = {}
        self._drag_origin: Coordinate | None = None
        self._drag_current: Coordinate | None = None
        # Original cell values replaced by the live creation-range preview.
        # Keeping the exact objects makes restoration lossless (including
        # hour markers and event styling underneath the marked range).
        self._drag_preview_originals: dict[tuple[int, int], Any] = {}

    def on_mount(self) -> None:
        self.cursor_type = "cell"
        self.zebra_stripes = False
        # Keep rendered cell width equal to declared column width.
        # Default DataTable padding inserts 1 char on each side.
        self.cell_padding = 0
        # Cells are painted as Rich Text with concrete colours resolved from
        # the active theme, so they don't restyle on their own when the user
        # switches theme (CSS-styled widgets do). Re-render on theme change so
        # the whole grid — hour markers, event bars, text — tracks the theme.
        self.app.theme_changed_signal.subscribe(self, self._on_theme_changed)

    def _on_theme_changed(self, _theme: object) -> None:
        if self._last_days is not None and self._last_today is not None:
            self.show_days(self._last_days, today=self._last_today, now=self._last_now)

    def on_resize(self) -> None:
        if self._last_days is None or self._last_today is None:
            return
        num_days = len(self._last_days)
        if num_days > 0:
            available = self.size.width - _TIME_COL_WIDTH
            new_width = max(_DAY_COL_WIDTH, available // num_days)
            if new_width == self._day_col_width:
                return
        self.show_days(self._last_days, today=self._last_today, now=self._last_now)

    def show_days(
        self,
        days: Sequence[tuple[date, Sequence[OccurrenceRow]]],
        *,
        today: date,
        now: datetime | None = None,
    ) -> None:
        """Replace the table contents with `days`'s events.

        `days` is an ordered sequence of `(date, rows)` pairs. The
        widget renders one column per pair, plus a leftmost time
        column. Pre-existing rows / columns are cleared first.

        When `now` falls on a shown day, its slot is highlighted: the
        time label gets a marker, an empty cell in that day's column
        gets a "now" line, and events in progress get the accent fill.
        """
        self._last_days = days
        self._last_today = today
        self._last_now = now
        # Defer rendering until on_resize delivers the real width; avoids
        # a flash of narrow columns before the first layout pass completes.
        if self.size.width == 0:
            return
        if self._drag_origin is not None:
            self.release_mouse()
        self._drag_origin = None
        self._drag_current = None
        self._drag_preview_originals.clear()
        self.clear(columns=True)
        self._cells.clear()
        self._slot_starts.clear()
        self._all_day_dates.clear()
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

        # Banner rows: full-day events (VTodos / midnight-to-midnight)
        # for each day, one stacked line per event, plus an empty line to
        # create new all-day events on.
        self._add_all_day_rows(days)

        palette = self._palette()
        current = _locate_now(days, now)
        start_hour, end_hour = _compute_hour_range(days)
        slot_count = ((end_hour - start_hour) * 60) // _SLOT_MINUTES
        for slot in range(slot_count):
            slot_minutes_in_day = (start_hour * 60) + slot * _SLOT_MINUTES
            self._add_time_row(slot_minutes_in_day, days, palette, current)

    def cell_ref(self, row: int, col: int) -> ComponentRef | None:
        """Lookup the event ref at `(row, col)` if any. Used by tests."""
        return self._cells.get((row, col))

    def slot_start(self, row: int, col: int) -> datetime | None:
        """Return the local start time for a timed cell, if it is one."""
        return self._slot_starts.get((row, col))

    def all_day_date(self, row: int, col: int) -> date | None:
        """Return the date of an "all day" banner cell, if it is one."""
        return self._all_day_dates.get((row, col))

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        coord = event.coordinate
        ref = self._cells.get((coord.row, coord.column))
        if ref is None:
            return
        self.post_message(self.Selected(ref))

    def on_mouse_down(self, event: MouseDown) -> None:
        """Begin creating or moving an event from a timed grid cell."""
        if event.button != 1:
            return
        coordinate = self._mouse_coordinate(event)
        if coordinate is None:
            return
        self._drag_origin = coordinate
        self._drag_current = coordinate
        self.cursor_coordinate = coordinate
        if self._cells.get((coordinate.row, coordinate.column)) is None:
            self._update_drag_preview(coordinate)
        self.capture_mouse()
        event.stop()

    def on_mouse_move(self, event: MouseMove) -> None:
        if self._drag_origin is None:
            return
        coordinate = self._mouse_coordinate(event)
        # A gesture stays within its kind: timed slots or the banner.
        if coordinate is not None and self._same_kind(coordinate, self._drag_origin):
            self._drag_current = coordinate
            self.cursor_coordinate = coordinate
            if (
                self._cells.get((self._drag_origin.row, self._drag_origin.column))
                is None
            ):
                self._update_drag_preview(coordinate)
        event.stop()

    def on_mouse_up(self, event: MouseUp) -> None:
        if event.button != 1 or self._drag_origin is None:
            return
        origin = self._drag_origin
        current = self._mouse_coordinate(event) or self._drag_current or origin
        if not self._same_kind(current, origin):
            current = self._drag_current or origin
        self._drag_origin = None
        self._drag_current = None
        self.release_mouse()
        # The gesture is complete. Prevent Textual from synthesising a second
        # Click which would also run DataTable's selection machinery.
        self.suppress_click()
        event.stop()

        origin_key = (origin.row, origin.column)
        current_key = (current.row, current.column)
        if origin_key in self._all_day_dates:
            self._finish_all_day_gesture(origin_key, current_key)
            return
        origin_start = self._slot_starts[origin_key]
        current_start = self._slot_starts[current_key]
        ref = self._cells.get(origin_key)
        self._clear_drag_preview()
        if ref is not None:
            if current_key == origin_key:
                self.post_message(self.Selected(ref))
            else:
                self.post_message(self.MoveRequested(ref, current_start - origin_start))
            return

        start = min(origin_start, current_start)
        end = max(origin_start, current_start) + timedelta(minutes=_SLOT_MINUTES)
        self.post_message(self.CreateRequested(start, end))

    def _finish_all_day_gesture(
        self, origin_key: tuple[int, int], current_key: tuple[int, int]
    ) -> None:
        """Click on a banner event opens it; a gesture over empty banner
        cells creates an all-day event spanning the covered days."""
        self._clear_drag_preview()
        ref = self._cells.get(origin_key)
        if ref is not None:
            if current_key == origin_key:
                self.post_message(self.Selected(ref))
            return
        first, last = sorted(
            (self._all_day_dates[origin_key], self._all_day_dates[current_key])
        )
        start, end = all_day_bounds(first, last)
        self.post_message(self.CreateRequested(start, end, all_day=True))

    def _same_kind(self, a: Coordinate, b: Coordinate) -> bool:
        """Both timed slots, or both "all day" banner cells."""
        return ((a.row, a.column) in self._all_day_dates) == (
            (b.row, b.column) in self._all_day_dates
        )

    def _update_drag_preview(self, current: Coordinate) -> None:
        """Paint every visible slot in the pending creation range."""
        origin = self._drag_origin
        if origin is None:
            return
        if (origin.row, origin.column) in self._all_day_dates:
            # All-day gesture: the origin's banner line, across the days.
            low, high = sorted((origin.column, current.column))
            wanted = {
                key
                for key in self._all_day_dates
                if key[0] == origin.row and low <= key[1] <= high
            }
        else:
            origin_start = self._slot_starts[(origin.row, origin.column)]
            current_start = self._slot_starts[(current.row, current.column)]
            start, end = sorted((origin_start, current_start))
            wanted = {
                key
                for key, slot_start in self._slot_starts.items()
                if start <= slot_start <= end
            }
        for key in self._drag_preview_originals.keys() - wanted:
            original = self._drag_preview_originals.pop(key)
            self.update_cell_at(Coordinate(*key), original)

        fill = self._theme_var("accent", "primary")
        foreground = _contrast_fg(fill)
        preview = Text(
            _DRAG_PREVIEW_CHAR * self._day_col_width,
            style=f"{foreground} on {fill}",
        )
        for key in wanted - self._drag_preview_originals.keys():
            coordinate = Coordinate(*key)
            self._drag_preview_originals[key] = self.get_cell_at(coordinate)
            self.update_cell_at(coordinate, preview.copy())

    def _clear_drag_preview(self) -> None:
        """Restore cells covered by the live creation-range preview."""
        for key, original in self._drag_preview_originals.items():
            self.update_cell_at(Coordinate(*key), original)
        self._drag_preview_originals.clear()

    def _mouse_coordinate(self, event: MouseEvent) -> Coordinate | None:
        """Return a timed or banner cell coordinate from render metadata."""
        meta = event.style.meta
        if meta.get("out_of_bounds", False):
            return None
        row = meta.get("row")
        column = meta.get("column")
        if not isinstance(row, int) or not isinstance(column, int):
            return None
        coordinate = Coordinate(row, column)
        key = (coordinate.row, coordinate.column)
        if key not in self._slot_starts and key not in self._all_day_dates:
            return None
        return coordinate

    # --- internal --------------------------------------------------------

    def _add_all_day_rows(
        self, days: Sequence[tuple[date, Sequence[OccurrenceRow]]]
    ) -> None:
        # One stacked banner line per full-day event, so each event keeps
        # its own selectable cell. The section is as tall as the busiest
        # day plus one line, so every day has an empty cell to click (or
        # drag across) to create an all-day event. The "all day" label
        # sits on the first line only so the rows read as one group.
        per_column = [_full_day_rows(day_date, events) for day_date, events in days]
        line_count = max((len(col) for col in per_column), default=0) + 1
        for line in range(line_count):
            row_index = self.row_count
            label: Any = Text(_ALL_DAY_LABEL, style="italic dim") if line == 0 else ""
            cells: list[Any] = [label]
            for col_idx, col_rows in enumerate(per_column, start=1):
                self._all_day_dates[(row_index, col_idx)] = days[col_idx - 1][0]
                if line < len(col_rows):
                    row = col_rows[line]
                    cells.append(row.component.summary or "(no summary)")
                    self._cells[(row_index, col_idx)] = row.component.ref
                else:
                    cells.append("")
            self.add_row(*cells)

    def _add_time_row(
        self,
        slot_minutes_in_day: int,
        days: Sequence[tuple[date, Sequence[OccurrenceRow]]],
        palette: _Palette,
        current: _Now | None = None,
    ) -> None:
        # Both 30-min slots of the same hour share the same stripe so the
        # grid reads as hourly bands.  Rich Text styles only colour actual
        # characters, not trailing whitespace, so every styled cell is
        # padded to the declared column width.
        row_index = self.row_count
        # Label every row. Mouse gestures operate at half-hour resolution,
        # so hiding the :30 labels makes the boundary after an event look
        # like part of the preceding full-hour block.
        is_hour = slot_minutes_in_day % 60 == 0
        is_now_row = current is not None and current.slot_minutes == slot_minutes_in_day
        time_text = _format_slot_time(slot_minutes_in_day)
        cells: list[Any] = [
            Text(f"{IN_PROGRESS_MARK}{time_text}", style=f"bold {palette.now}")
            if is_now_row
            else time_text
        ]
        for col_idx, (day_date, events) in enumerate(days, start=1):
            slot_hour, slot_minute = divmod(slot_minutes_in_day, 60)
            self._slot_starts[(row_index, col_idx)] = datetime.combine(
                day_date, time(slot_hour, slot_minute)
            ).astimezone()
            content, ref, is_start, is_end = _cell_for_slot(
                day_date, slot_minutes_in_day, events
            )
            if ref is not None:
                # Flip the alternation flag each time a new event starts so
                # back-to-back events always render in different shades.
                if is_start:
                    self._col_alt[col_idx] = not self._col_alt.get(col_idx, False)
                alt = self._col_alt.get(col_idx, False)
                fill = palette.fill_b if alt else palette.fill_a
                fg = palette.fg_b if alt else palette.fg_a
                if (
                    current is not None
                    and col_idx == current.col
                    and ref in current.active
                ):
                    fill, fg = palette.now, palette.now_fg
                w = self._day_col_width
                if is_start:
                    style = f"{fg} on {fill}"
                    text = content.ljust(w)
                elif is_end:
                    style = f"{palette.end_fg} on {fill}"
                    text = _EVENT_END_CHAR * w
                else:
                    style = f"on {fill}"
                    text = " " * w
                cells.append(Text(text, style=style))
                self._cells[(row_index, col_idx)] = ref
            elif is_now_row and current is not None and col_idx == current.col:
                cells.append(
                    Text(_NOW_LINE_CHAR * self._day_col_width, style=palette.now)
                )
            elif is_hour:
                cells.append(
                    Text(
                        _HOUR_MARKER_CHAR * self._day_col_width,
                        style=palette.hour_marker,
                    )
                )
            else:
                cells.append("")
        self.add_row(*cells)

    def _palette(self) -> _Palette:
        """Resolve the grid's colours from the active theme.

        Event fills come from the theme's `primary`/`secondary` accents
        (the two alternation shades); each title colour is computed for
        maximum contrast against its fill so text stays readable under any
        theme — this is what lets a high-contrast theme actually raise the
        grid's contrast, not just tint the event bars.
        """
        fill_a = self._theme_var("primary")
        fill_b = self._theme_var("secondary", "primary-darken-2", "primary")
        now = self._theme_var("accent", "warning")
        return _Palette(
            fill_a=fill_a,
            fill_b=fill_b,
            fg_a=_contrast_fg(fill_a),
            fg_b=_contrast_fg(fill_b),
            # Subtle round-hour marker and low-profile event end-cap.
            hour_marker=self._theme_var("panel", "surface-lighten-2", "surface"),
            end_fg=self._theme_var("surface", "background"),
            now=now,
            now_fg=_contrast_fg(now),
        )

    def _theme_var(self, name: str, *fallbacks: str) -> str:
        """Concrete colour for a Textual theme variable, by name.

        Reads the resolved `app.theme_variables` (always populated for the
        active theme), trying `name` then each fallback key. Skips Textual
        `auto …` values (e.g. `text`), which Rich cannot render. The chain
        ends at `surface`/`foreground`, which every built-in theme defines,
        so no hardcoded per-colour hex is needed.
        """
        variables = self.app.theme_variables
        for key in (name, *fallbacks, "surface", "foreground"):
            value = variables.get(key)
            if isinstance(value, str) and value and not value.startswith("auto"):
                return value
        return "#808080"  # unreachable: surface/foreground are always set


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


def _locate_now(
    days: Sequence[tuple[date, Sequence[OccurrenceRow]]],
    now: datetime | None,
) -> _Now | None:
    """Column, slot and in-progress events for `now`, if its day is shown."""
    if now is None:
        return None
    local = now.astimezone()
    for col_idx, (day_date, events) in enumerate(days, start=1):
        if day_date != local.date():
            continue
        minutes = local.hour * 60 + local.minute
        return _Now(
            col=col_idx,
            slot_minutes=minutes - minutes % _SLOT_MINUTES,
            active=frozenset(ref for ref, _ in in_progress_keys(events, now)),
        )
    return None


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
            if _occurrence_is_full_day(row.occurrence):
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
        if _occurrence_is_full_day(row.occurrence):
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
        24 * 60
        if first_end_dt.date() > day
        else first_end_dt.hour * 60 + first_end_dt.minute
    )
    is_start = first_start_min >= slot_start
    is_end = (not is_start) and first_end_min <= slot_end
    return summary, first.component.ref, is_start, is_end


def _full_day_rows(
    day: date,
    events: Sequence[OccurrenceRow],
) -> list[OccurrenceRow]:
    """Full-day rows covering `day`, in the order `events` arrives in.

    Each gets its own banner line, so the order is preserved rather than
    collapsed into a `+N` count. A multi-day span is included on every
    day from its start through the day before its end (`end` is
    exclusive). The view sorts `events` by (start, account, calendar,
    uid), so the banner order is stable.
    """
    out: list[OccurrenceRow] = []
    for row in events:
        if not _occurrence_is_full_day(row.occurrence):
            continue
        start_d, end_d = _full_day_dates(row.occurrence)
        if start_d <= day < end_d:
            out.append(row)
    return out


def _full_day_dates(occ: Occurrence) -> tuple[date, date]:
    """Inclusive-start, exclusive-end calendar-date span for a full-day
    occurrence, in the frame its start aligns to (UTC or local).

    A VALUE=DATE all-day event is anchored at UTC midnight, so its day
    columns come from the UTC dates; a local-midnight all-day event maps
    to local dates. Picking the frame that matches the start's midnight
    lands the banner on the right grid columns either way.
    """
    end = occ.end or occ.start
    if occ.start.astimezone(UTC).time() == time.min:
        return occ.start.astimezone(UTC).date(), end.astimezone(UTC).date()
    return occ.start.astimezone().date(), end.astimezone().date()


__all__ = ["TimelineGrid"]
