from __future__ import annotations

from collections.abc import Callable
from typing import Any

from textual.app import ComposeResult
from textual.containers import Center, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Label

from chronos.tui.bindings import confirm_bindings


class ConfirmScreen(Screen[None]):
    """Generic yes/no modal.

    `on_confirm` is called when the user presses `y`. Cancellation
    (`n` or escape) just pops the screen with no callback. Holding the
    callback locally — instead of giving the screen a reference to the
    `ChronosApp` — keeps screens decoupled from the app per
    `CONVENTIONS.md §11`.
    """

    BINDINGS = confirm_bindings()

    def __init__(self, prompt: str, on_confirm: Callable[[], None]) -> None:
        super().__init__()
        self._prompt = prompt
        self._on_confirm = on_confirm

    def compose(self) -> ComposeResult:
        with Center(), Vertical(id="confirm-box"):
            yield Label(self._prompt, id="confirm-prompt")
            yield Label("[y] Yes   [n] No", id="confirm-hint")
        yield Footer()

    def action_confirm(self) -> None:
        self._on_confirm()
        self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]

    def action_cancel(self) -> None:
        self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]


def show_confirm(
    app: Any, prompt: str, on_confirm: Callable[[], None]
) -> ConfirmScreen:
    """Push a `ConfirmScreen` and return it (for tests / call-site clarity)."""
    screen = ConfirmScreen(prompt, on_confirm)
    app.push_screen(screen)
    return screen


__all__ = ["ConfirmScreen", "show_confirm"]
