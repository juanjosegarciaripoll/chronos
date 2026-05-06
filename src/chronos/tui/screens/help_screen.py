from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from rich.markup import escape as markup_escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Label, Static

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


@dataclass(frozen=True, slots=True)
class _Section:
    title: str
    bindings: tuple[tuple[str, str], ...]


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
    CSS = """
    HelpScreen {
        align: center middle;
    }

    #help-dialog {
        width: auto;
        max-width: 90;
        height: auto;
        border: solid $primary;
        padding: 1 2;
        background: $surface;
    }

    #help-title {
        text-style: bold;
        content-align: center middle;
        margin-bottom: 1;
    }

    #help-columns {
        layout: horizontal;
        height: auto;
    }

    .help-column {
        width: 1fr;
        height: auto;
        padding: 0 2 0 0;
    }

    #help-hint {
        color: $text-muted;
        margin-top: 1;
        content-align: center middle;
    }
    """

    def __init__(self, source_bindings: Sequence[BindingType]) -> None:
        super().__init__()
        self._source_bindings = tuple(source_bindings)

    def compose(self) -> ComposeResult:
        left_sections, right_sections = self._column_sections()
        with Vertical(id="help-dialog"):
            yield Label("Keyboard shortcuts", id="help-title")
            with Horizontal(id="help-columns"):
                yield Static(_render_column(left_sections), classes="help-column")
                yield Static(_render_column(right_sections), classes="help-column")
            yield Static("Press F1 or Esc to close.", id="help-hint")
        yield Footer()

    def action_close(self) -> None:
        self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]

    def _render_help(self) -> str:
        left_sections, right_sections = self._column_sections()
        left = _render_column(left_sections)
        right = _render_column(right_sections)
        return left if not right else f"{left}\n\n{right}"

    def _column_sections(self) -> tuple[tuple[_Section, ...], tuple[_Section, ...]]:
        bucketed = self._bucket_bindings()
        left_order = ("Views", "Agenda window", "Navigation")
        right_order = ("Events", "Tools", "Other")
        left: list[_Section] = []
        right: list[_Section] = []
        for title in left_order:
            rows = bucketed.get(title)
            if rows:
                left.append(_Section(title, tuple(rows)))
        for title in right_order:
            rows = bucketed.get(title)
            if rows:
                right.append(_Section(title, tuple(rows)))
        if not left and not right:
            left.append(_Section("Shortcuts", (("n/a", "No keyboard shortcuts."),)))
        return tuple(left), tuple(right)

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


def _render_column(sections: Sequence[_Section]) -> str:
    lines: list[str] = []
    for idx, section in enumerate(sections):
        if idx > 0:
            lines.append("")
        lines.append(f"[b $accent]{markup_escape(section.title)}[/b $accent]")
        key_width = max(len(key) for key, _ in section.bindings) + 2
        for key, desc in section.bindings:
            lines.append(
                f"[b]{markup_escape(key).ljust(key_width)}[/b] {markup_escape(desc)}"
            )
    return "\n".join(lines)


__all__ = ["HelpScreen"]
