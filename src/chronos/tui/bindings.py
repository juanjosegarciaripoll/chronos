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
# View-switch keys are Ctrl-prefixed so the bare letters `d` / `w` /
# `m` / `n` / `p` / etc. stay free for in-view actions (agenda window
# tuning, date-axis navigation, ...).
KEY_VIEW_AGENDA = "ctrl+a"
KEY_VIEW_DAY = "ctrl+d"
KEY_VIEW_GRID = "ctrl+g"

# Agenda-only window tuners. In other views these keys are no-ops.
KEY_AGENDA_DAY = "d"
KEY_AGENDA_WEEK = "w"
KEY_AGENDA_MONTH = "m"

KEY_TODAY = "t"

# Date-axis navigation. `n`/`p` shift the viewed date by one day in
# Day / Grid views; `N`/`P` shift by the grid's chunk size (3 or 4).
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

KEY_NEW = "+"
KEY_EDIT = "e"
KEY_DELETE = "D"
KEY_DELETE_ALT = "shift+d"
KEY_SYNC = "s"
KEY_SEARCH = "/"
KEY_QUIT = "q"
KEY_OPEN = "enter"
KEY_BACK = "escape"
KEY_HELP = "f1"
KEY_TOGGLE_CALENDARS = "c"


def main_bindings() -> list[BindingType]:
    """Bindings owned by `MainScreen`. Each view registers itself here."""
    return [
        Binding(KEY_VIEW_AGENDA, "view_agenda", "Agenda"),
        Binding(KEY_VIEW_DAY, "view_day", "Day"),
        Binding(KEY_VIEW_GRID, "view_grid", "Grid"),
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
        Binding(KEY_SYNC, "sync", "Sync"),
        Binding(KEY_SEARCH, "search", "Search"),
        Binding(KEY_HELP, "show_help", "Help"),
        Binding(KEY_QUIT, "quit", "Quit"),
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
    "KEY_SEARCH",
    "KEY_SYNC",
    "KEY_TODAY",
    "KEY_TOGGLE_CALENDARS",
    "KEY_VIEW_AGENDA",
    "KEY_VIEW_DAY",
    "KEY_VIEW_GRID",
    "BindingType",
    "detail_bindings",
    "edit_bindings",
    "main_bindings",
    "search_bindings",
]
