from __future__ import annotations

from collections.abc import Callable
from datetime import date

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label

from chronos.tui.goto import GOTO_HELP, GotoError, parse_goto


class GotoScreen(ModalScreen[None]):
    """Modal asking for a date to jump to.

    Enter parses the input with `parse_goto`; a valid date pops the
    dialog and calls `on_goto(date)`, an invalid one shows the error
    under the field and keeps the dialog open. Escape cancels.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(
        self,
        *,
        viewed: date,
        today: date,
        on_goto: Callable[[date], None],
    ) -> None:
        super().__init__()
        self._viewed = viewed
        self._today = today
        self._on_goto = on_goto

    def compose(self) -> ComposeResult:
        with Vertical(id="goto-box", classes="dialog-box"):
            yield Label("Go to date", classes="dialog-title")
            yield Input(placeholder=GOTO_HELP, id="goto-input")
            yield Label("", id="goto-error")

    def on_mount(self) -> None:
        self.query_one("#goto-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        try:
            target = parse_goto(event.value, viewed=self._viewed, today=self._today)
        except GotoError as exc:
            self.query_one("#goto-error", Label).update(str(exc))
            return
        self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]
        self._on_goto(target)

    def action_cancel(self) -> None:
        self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]


__all__ = ["GotoScreen"]
