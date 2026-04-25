from __future__ import annotations

from collections.abc import Sequence

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Static

from chronos.tui.bindings import BindingType

# Per-section grouping of action names. Anything not listed lands in
# the trailing "Other" section so a new binding doesn't disappear from
# help just because we forgot to update this map.
_SECTIONS: tuple[tuple[str, frozenset[str]], ...] = (
    (
        "Views",
        frozenset(
            {
                "view_agenda",
                "view_day",
                "view_grid",
            }
        ),
    ),
    (
        "Agenda window",
        frozenset(
            {
                "agenda_window_day",
                "agenda_window_week",
                "agenda_window_month",
            }
        ),
    ),
    (
        "Navigation",
        frozenset(
            {
                "today",
                "next_day",
                "prev_day",
                "next_chunk",
                "prev_chunk",
            }
        ),
    ),
    (
        "Events",
        frozenset({"new_event", "edit_event", "delete_event", "open_event"}),
    ),
    (
        "Tools",
        frozenset({"sync", "search", "show_help", "quit", "toggle_calendars"}),
    ),
)


class HelpScreen(Screen[None]):
    """Read-only modal listing every visible key binding of the parent screen.

    Bindings are grouped by area (Views / Navigation / Events / Tools)
    and rendered through Rich panels for a calmer visual hierarchy
    than a flat list. Hidden aliases (the `shift+t` / `shift+n` /
    `shift+p` / `shift+d` doubles that exist only to cope with
    terminal keyboard quirks) are filtered out so the help text
    doesn't duplicate every entry.
    """

    BINDINGS = [
        Binding("escape", "close", "Back"),
        Binding("f1", "close", "Back", show=False),
    ]

    def __init__(self, source_bindings: Sequence[BindingType]) -> None:
        super().__init__()
        self._source_bindings = tuple(source_bindings)

    def compose(self) -> ComposeResult:
        with Vertical(id="help-screen"):
            yield Static(id="help-body")
        yield Footer()

    def on_mount(self) -> None:
        body: Static = self.query_one("#help-body", Static)
        body.update(self._render_help())

    def action_close(self) -> None:
        self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]

    def _render_help(self) -> RenderableType:
        # Don't name this `_render` — Textual's `Widget._render` is a
        # private method that returns a `Visual`, and overriding it
        # here makes the parent screen's render pipeline crash with
        # `'str' object has no attribute 'render_strips'`.
        bucketed = self._bucket_bindings()
        renderables: list[RenderableType] = []
        for title, _ in _SECTIONS:
            entries = bucketed.get(title)
            if entries:
                renderables.append(_section_panel(title, entries))
        other = bucketed.get("Other")
        if other:
            renderables.append(_section_panel("Other", other))
        if not renderables:
            return Text("No keyboard shortcuts.", style="dim")
        return Group(*renderables)

    def _bucket_bindings(self) -> dict[str, list[tuple[str, str]]]:
        bucketed: dict[str, list[tuple[str, str]]] = {}
        for binding in self._source_bindings:
            row = _binding_row(binding)
            if row is None:
                continue
            section = _section_for(row.action)
            bucketed.setdefault(section, []).append((row.key, row.description))
        return bucketed


class _Row:
    """Internal helper carrying everything `_bucket_bindings` needs."""

    __slots__ = ("action", "description", "key")

    def __init__(self, key: str, action: str, description: str) -> None:
        self.key = key
        self.action = action
        self.description = description


def _binding_row(binding: BindingType) -> _Row | None:
    if isinstance(binding, Binding):
        # Drop the `shift+letter` aliases — they're terminal-quirk
        # doubles of the bare uppercase keys, not separate shortcuts.
        # `show=False` bindings are kept (`d`/`w`/`m` agenda tuners
        # and `n`/`p`/`N`/`P` date-axis nav are hidden from the
        # footer to keep it uncluttered, but a help screen that
        # silently dropped them would defeat its own purpose).
        if binding.key.startswith("shift+"):
            return None
        key = binding.key_display or binding.key
        return _Row(key, binding.action, binding.description)
    # Tuple form: (key, action) or (key, action, description). The
    # main screen never uses these, but the BindingType union allows
    # them — be defensive.
    if len(binding) >= 3:
        return _Row(binding[0], binding[1], binding[2])
    if len(binding) == 2:
        return _Row(binding[0], binding[1], binding[1])
    return None


def _section_for(action: str) -> str:
    for title, actions in _SECTIONS:
        if action in actions:
            return title
    return "Other"


def _section_panel(title: str, rows: Sequence[tuple[str, str]]) -> Panel:
    table = Table(
        show_header=False,
        show_edge=False,
        box=None,
        pad_edge=False,
        padding=(0, 2),
    )
    table.add_column(justify="right", style="bold yellow", no_wrap=True)
    table.add_column(style="default")
    for key, description in rows:
        table.add_row(key, description)
    return Panel(
        table,
        title=f"[bold cyan]{title}[/]",
        title_align="left",
        border_style="cyan",
        padding=(0, 1),
    )


__all__ = ["HelpScreen"]
