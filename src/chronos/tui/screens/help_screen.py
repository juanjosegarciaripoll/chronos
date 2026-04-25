from __future__ import annotations

from collections.abc import Sequence

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Static

from chronos.tui.bindings import BindingType


class HelpScreen(Screen[None]):
    """Read-only modal listing every visible key binding of the parent screen.

    Bound to `F1` from `MainScreen`. Bindings whose `show=False` (the
    `shift+t` / `shift+n` / `shift+p` aliases that exist only to cope
    with terminal keyboard quirks) are hidden so the help text doesn't
    duplicate every entry.
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

    def _render_help(self) -> str:
        # Avoid the name `_render` — Textual's `Widget._render` is a
        # private method that returns a `Visual`. Overriding it to
        # return `str` makes the parent screen's render pipeline
        # crash with `'str' object has no attribute 'render_strips'`.
        rows: list[tuple[str, str]] = []
        for binding in self._source_bindings:
            entry = _binding_row(binding)
            if entry is None:
                continue
            rows.append(entry)
        if not rows:
            return "No keyboard shortcuts."
        key_width = max(len(key) for key, _ in rows)
        lines = ["Keyboard shortcuts", ""]
        for key, description in rows:
            lines.append(f"  {key:<{key_width}}   {description}")
        return "\n".join(lines)


def _binding_row(binding: BindingType) -> tuple[str, str] | None:
    if isinstance(binding, Binding):
        if not binding.show:
            return None
        key = binding.key_display or binding.key
        return key, binding.description
    # Tuple form: (key, action) or (key, action, description). The
    # main screen never uses these, but the BindingType union allows
    # them — be defensive.
    if len(binding) >= 3:
        return binding[0], binding[2]
    if len(binding) == 2:
        return binding[0], binding[1]
    return None


__all__ = ["HelpScreen"]
