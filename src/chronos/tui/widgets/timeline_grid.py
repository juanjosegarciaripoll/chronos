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
from datetime import UTC, date, time
from typing import Any, NamedTuple

from rich.text import Text
from textual.color import Color
from textual.message import Message
from textual.widgets import DataTable

from chronos.domain import ComponentRef, Occurrence
from chronos.tui.views import OccurrenceRow
from chronos.tui.views import _is_full_day as _occurrence_is_full_day

_SLOT_MINUTES = 30
_DEFAULT_START_HOUR = 6
_DEFAULT_END_HOUR = 22
_TIME_COL_WIDTH = 6
_DAY_COL_WIDTH = 20
_ALL_DAY_LABEL = "all day"
_HOUR_MARKER_CHAR = "\u2594"  # UPPER ONE EIGHTH BLOCK
_EVENT_END_CHAR = "\u2582"  # LOWER ONE QUARTER BLOCK


class _Palette(NamedTuple):
    """Concrete colours for one render, resolved from the active theme.

    `fill_a`/`fill_b` are the two event-fill shades (alternated so adjacent
    events read apart); `fg_a`/`fg_b` are their contrast-matched title
    colours; `hour_marker` and `end_fg` are the round-hour marker and the
    event end-cap foreground.
    """

    fill_a: str
    fill_b: str
    fg_a: str
    fg_b: str
    hour_marker: str
    end_fg: str


def _contrast_fg(background: str) -> str:
    """Black or white \u2014 whichever reads better on `background`.

    Mirrors what Textual's `Color.get_contrast_text` does, but returns a
    plain 6-digit hex so the result is always safe inside a Rich style.
    Rec. 601 luma: bright fills get black text, dark fills get white.
    """
    colour = Color.parse(background)
    luma = 0.299 * colour.r + 0.587 * colour.g + 0.114 * colour.b
    return "#000000" if luma > 140 else "#FFFFFF"


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
            self.show_days(self._last_days, today=self._last_today)

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

        # Banner rows: full-day events (VTodos / midnight-to-midnight)
        # for each day, one stacked line per event. Skipped silently when
        # no day has any.
        self._add_all_day_rows(days)

        palette = self._palette()
        start_hour, end_hour = _compute_hour_range(days)
        slot_count = ((end_hour - start_hour) * 60) // _SLOT_MINUTES
        for slot in range(slot_count):
            slot_minutes_in_day = (start_hour * 60) + slot * _SLOT_MINUTES
            self._add_time_row(slot_minutes_in_day, days, palette)

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

    def _add_all_day_rows(
        self, days: Sequence[tuple[date, Sequence[OccurrenceRow]]]
    ) -> None:
        # One stacked banner line per full-day event, so each event keeps
        # its own selectable cell. The section is as tall as the busiest
        # day; lighter days leave their lower cells blank. The "all day"
        # label sits on the first line only so the rows read as one group.
        per_column = [_full_day_rows(day_date, events) for day_date, events in days]
        line_count = max((len(col) for col in per_column), default=0)
        if line_count == 0:
            return
        for line in range(line_count):
            row_index = self.row_count
            label: Any = (
                Text(_ALL_DAY_LABEL, style="italic dim") if line == 0 else ""
            )
            cells: list[Any] = [label]
            for col_idx, col_rows in enumerate(per_column, start=1):
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
    ) -> None:
        # Both 30-min slots of the same hour share the same stripe so the
        # grid reads as hourly bands.  Rich Text styles only colour actual
        # characters, not trailing whitespace, so every styled cell is
        # padded to the declared column width.
        row_index = self.row_count
        # Only label the top of each hour; the :30 row is left blank so
        # the time column stays readable without clutter.
        is_hour = slot_minutes_in_day % 60 == 0
        time_text = _format_slot_time(slot_minutes_in_day) if is_hour else ""
        cells: list[Any] = [time_text]
        for col_idx, (day_date, events) in enumerate(days, start=1):
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
        return _Palette(
            fill_a=fill_a,
            fill_b=fill_b,
            fg_a=_contrast_fg(fill_a),
            fg_b=_contrast_fg(fill_b),
            # Subtle round-hour marker and low-profile event end-cap.
            hour_marker=self._theme_var("panel", "surface-lighten-2", "surface"),
            end_fg=self._theme_var("surface", "background"),
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
