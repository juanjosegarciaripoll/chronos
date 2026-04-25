from __future__ import annotations

from collections.abc import Callable
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label


class ConfirmScreen(ModalScreen[None]):
    """Generic yes/no modal dialog.

    `on_confirm` is called when the user picks Yes (clicked, Enter
    on the focused button, or `y`). Cancellation (`n`, Escape, or
    clicking No) just pops the screen with no callback. Holding the
    callback locally — instead of giving the screen a reference to
    the `ChronosApp` — keeps screens decoupled from the app per
    `CONVENTIONS.md §11`.
    """

    BINDINGS = [
        Binding("y", "confirm", "Yes"),
        Binding("n", "cancel", "No"),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, prompt: str, on_confirm: Callable[[], None]) -> None:
        super().__init__()
        self._prompt = prompt
        self._on_confirm = on_confirm

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label(self._prompt, classes="dialog-title")
            with Horizontal(classes="dialog-actions"):
                yield Button("Yes", id="confirm-yes", variant="primary")
                yield Button("No", id="confirm-no", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-yes":
            self.action_confirm()
        else:
            self.action_cancel()

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
