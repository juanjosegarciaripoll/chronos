from __future__ import annotations

from textual.binding import Binding

# Textual's `Screen.BINDINGS` is an invariant list typed as
# `list[Binding | tuple[str, str] | tuple[str, str, str]]`. We always
# return `Binding` instances, but the helpers' declared type has to
# match the wider element type so subclasses can assign their result.
BindingType = Binding | tuple[str, str] | tuple[str, str, str]

# Per-screen bindings live next to each screen. This module exposes the
# constants that more than one screen needs to keep in sync — view-switch
# keys, the universal "today" reset, the universal "quit" key.

KEY_DAY = "d"
KEY_WEEK = "w"
KEY_MONTH = "m"
KEY_AGENDA = "a"
KEY_TODAY = "t"
# Two literal key names map to "Shift+T" depending on terminal driver:
# some send the uppercase character (key="T"), others send the modifier
# form (key="shift+t"). We bind both so the action fires either way.
KEY_TODOS = "T"
KEY_TODOS_ALT = "shift+t"
KEY_NEW = "n"
KEY_EDIT = "e"
KEY_SYNC = "s"
KEY_SEARCH = "/"
KEY_QUIT = "q"
KEY_OPEN = "enter"
KEY_BACK = "escape"
KEY_HELP = "f1"
# Same shift-letter caveat as `KEY_TODOS` / `KEY_TODOS_ALT`: some
# terminals emit the uppercase character, others the modifier form.
KEY_NEXT = "N"
KEY_NEXT_ALT = "shift+n"
KEY_PREV = "P"
KEY_PREV_ALT = "shift+p"
KEY_DELETE = "D"
KEY_DELETE_ALT = "shift+d"


def main_bindings() -> list[BindingType]:
    """Bindings owned by `MainScreen`. Each view registers itself here."""
    return [
        Binding(KEY_DAY, "view_day", "Day"),
        Binding(KEY_WEEK, "view_week", "Week"),
        Binding(KEY_MONTH, "view_month", "Month"),
        Binding(KEY_AGENDA, "view_agenda", "Agenda"),
        Binding(KEY_TODAY, "today", "Today"),
        Binding(KEY_TODOS, "view_todos", "Todos"),
        # Defensive alias: terminals that emit "shift+t" instead of the
        # uppercase character "T" for the same physical keypress.
        Binding(KEY_TODOS_ALT, "view_todos", "Todos", show=False),
        Binding(KEY_NEW, "new_event", "New"),
        Binding(KEY_EDIT, "edit_event", "Edit"),
        Binding(KEY_OPEN, "open_event", "Open", show=False),
        Binding(KEY_DELETE, "delete_event", "Delete"),
        Binding(KEY_DELETE_ALT, "delete_event", "Delete", show=False),
        Binding(KEY_SYNC, "sync", "Sync"),
        Binding(KEY_SEARCH, "search", "Search"),
        Binding(KEY_NEXT, "next_period", "Next"),
        Binding(KEY_NEXT_ALT, "next_period", "Next", show=False),
        Binding(KEY_PREV, "prev_period", "Prev"),
        Binding(KEY_PREV_ALT, "prev_period", "Prev", show=False),
        Binding(KEY_HELP, "show_help", "Help"),
        Binding(KEY_QUIT, "quit", "Quit"),
    ]


def confirm_bindings() -> list[BindingType]:
    return [
        Binding("y", "confirm", "Yes"),
        Binding("n", "cancel", "No"),
        Binding(KEY_BACK, "cancel", "Cancel", show=False),
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
    "KEY_AGENDA",
    "KEY_BACK",
    "KEY_DAY",
    "KEY_DELETE",
    "KEY_DELETE_ALT",
    "KEY_EDIT",
    "KEY_HELP",
    "KEY_MONTH",
    "KEY_NEW",
    "KEY_NEXT",
    "KEY_NEXT_ALT",
    "KEY_OPEN",
    "KEY_PREV",
    "KEY_PREV_ALT",
    "KEY_QUIT",
    "KEY_SEARCH",
    "KEY_SYNC",
    "KEY_TODAY",
    "KEY_TODOS",
    "KEY_TODOS_ALT",
    "KEY_WEEK",
    "BindingType",
    "confirm_bindings",
    "detail_bindings",
    "edit_bindings",
    "main_bindings",
    "search_bindings",
]
