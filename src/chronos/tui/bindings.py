from __future__ import annotations

from textual.binding import Binding

# Textual's `Screen.BINDINGS` is an invariant list typed as
# `list[Binding | tuple[str, str] | tuple[str, str, str]]`. We always
# return `Binding` instances, but the helpers' declared type has to
# match the wider element type so subclasses can assign their result.
BindingType = Binding | tuple[str, str] | tuple[str, str, str]

# Per-screen bindings live next to each screen. This module exposes the
# constants that more than one screen needs to keep in sync.
#
# Bindings are bare letters (no Ctrl prefix) and deliberately echo
# Pony's mail-client keymap so the two apps share muscle memory: `c`
# creates (compose / new event), `g` runs a foreground sync ("get
# mail"), `Q` quits. The agenda view is reached with `a`; the
# single-day / multi-day timeline is chosen with the number keys
# `1`–`7` (see `KEY_SPANS`).
KEY_VIEW_AGENDA = "a"

# Timeline span selectors. `1` is the single-day view; `2`–`7` are the
# multi-day grid showing that many days. These replace the old
# Ctrl-D / Ctrl-G "Day" / "Grid" view toggles.
KEY_SPANS = (1, 2, 3, 4, 5, 6, 7)

# Agenda-only window tuners. In other views these keys are no-ops.
KEY_AGENDA_DAY = "d"
KEY_AGENDA_WEEK = "w"
KEY_AGENDA_MONTH = "m"

KEY_TODAY = "t"

# Date-axis navigation. `n`/`p` shift the viewed date by one day in
# Day / Grid views; `N`/`P` shift by the grid's chunk size (1–7).
# Ineffective in Agenda (which uses calendar windows, not a viewed
# date).
KEY_NEXT_DAY = "n"
KEY_PREV_DAY = "p"
# Same shift-letter caveat as the rest: terminals split on whether
# they emit the uppercase character or the modifier form.
KEY_NEXT_CHUNK = "N"
KEY_NEXT_CHUNK_ALT = "shift+n"
KEY_PREV_CHUNK = "P"
KEY_PREV_CHUNK_ALT = "shift+p"

# `c` (create) and `g` (get/sync) mirror Pony's compose / get-mail
# keys; `Q` mirrors Pony's quit. The shift-letter aliases cope with
# terminals that emit the modifier form instead of the bare capital.
KEY_NEW = "c"
KEY_EDIT = "e"
KEY_DELETE = "D"
KEY_DELETE_ALT = "shift+d"
KEY_SYNC = "g"
KEY_SEARCH = "/"
KEY_QUIT = "Q"
KEY_QUIT_ALT = "shift+q"
KEY_OPEN = "enter"
KEY_BACK = "escape"
KEY_HELP = "f1"
KEY_TOGGLE_CALENDARS = "C"
KEY_TOGGLE_CALENDARS_ALT = "shift+c"


def _span_label(days: int) -> str:
    return "1 day" if days == 1 else f"{days} days"


def _span_binding(days: int) -> Binding:
    """A `1`–`7` key that selects the `days`-wide timeline.

    Only the endpoints (`1 day` / `7 days`) are shown in the footer —
    they advertise the available range without flooding it with seven
    near-identical entries. The in-between selectors stay hidden but
    work, and the help screen lists all of them.
    """
    return Binding(
        str(days),
        f"select_span({days})",
        _span_label(days),
        show=days in (KEY_SPANS[0], KEY_SPANS[-1]),
    )


def main_bindings() -> list[BindingType]:
    """Bindings owned by `MainScreen`. Each view registers itself here."""
    return [
        Binding(KEY_VIEW_AGENDA, "view_agenda", "Agenda"),
        *(_span_binding(days) for days in KEY_SPANS),
        Binding(KEY_AGENDA_DAY, "agenda_window_day", "Day", show=False),
        Binding(KEY_AGENDA_WEEK, "agenda_window_week", "Week", show=False),
        Binding(KEY_AGENDA_MONTH, "agenda_window_month", "Month", show=False),
        Binding(KEY_TODAY, "today", "Today"),
        Binding(KEY_NEXT_DAY, "next_day", "Next day", show=False),
        Binding(KEY_PREV_DAY, "prev_day", "Prev day", show=False),
        Binding(KEY_NEXT_CHUNK, "next_chunk", "Next chunk", show=False),
        Binding(KEY_NEXT_CHUNK_ALT, "next_chunk", "Next chunk", show=False),
        Binding(KEY_PREV_CHUNK, "prev_chunk", "Prev chunk", show=False),
        Binding(KEY_PREV_CHUNK_ALT, "prev_chunk", "Prev chunk", show=False),
        Binding(KEY_NEW, "new_event", "New"),
        Binding(KEY_EDIT, "edit_event", "Edit"),
        Binding(KEY_OPEN, "open_event", "Open", show=False),
        Binding(KEY_DELETE, "delete_event", "Delete"),
        Binding(KEY_DELETE_ALT, "delete_event", "Delete", show=False),
        Binding(KEY_TOGGLE_CALENDARS, "toggle_calendars", "Calendars"),
        Binding(KEY_TOGGLE_CALENDARS_ALT, "toggle_calendars", "Calendars", show=False),
        Binding(KEY_SYNC, "sync", "Sync"),
        Binding(KEY_SEARCH, "search", "Search"),
        Binding(KEY_HELP, "show_help", "Help"),
        Binding(KEY_QUIT, "quit", "Quit"),
        Binding(KEY_QUIT_ALT, "quit", "Quit", show=False),
    ]


def edit_bindings() -> list[BindingType]:
    return [
        Binding("ctrl+s", "save", "Save"),
        Binding(KEY_BACK, "cancel", "Cancel"),
    ]


def detail_bindings() -> list[BindingType]:
    return [
        Binding(KEY_BACK, "close", "Back"),
        Binding(KEY_EDIT, "edit", "Edit"),
    ]


def search_bindings() -> list[BindingType]:
    return [
        Binding(KEY_OPEN, "submit", "Search"),
        Binding(KEY_BACK, "cancel", "Cancel"),
    ]


__all__ = [
    "KEY_AGENDA_DAY",
    "KEY_AGENDA_MONTH",
    "KEY_AGENDA_WEEK",
    "KEY_BACK",
    "KEY_DELETE",
    "KEY_DELETE_ALT",
    "KEY_EDIT",
    "KEY_HELP",
    "KEY_NEW",
    "KEY_NEXT_CHUNK",
    "KEY_NEXT_CHUNK_ALT",
    "KEY_NEXT_DAY",
    "KEY_OPEN",
    "KEY_PREV_CHUNK",
    "KEY_PREV_CHUNK_ALT",
    "KEY_PREV_DAY",
    "KEY_QUIT",
    "KEY_QUIT_ALT",
    "KEY_SEARCH",
    "KEY_SPANS",
    "KEY_SYNC",
    "KEY_TODAY",
    "KEY_TOGGLE_CALENDARS",
    "KEY_TOGGLE_CALENDARS_ALT",
    "KEY_VIEW_AGENDA",
    "BindingType",
    "detail_bindings",
    "edit_bindings",
    "main_bindings",
    "search_bindings",
]
