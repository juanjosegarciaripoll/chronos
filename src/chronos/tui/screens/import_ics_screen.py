from __future__ import annotations

from collections.abc import Callable
from typing import cast

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Select

from chronos.domain import CalendarRef


class ImportIcsScreen(ModalScreen[None]):
    """Modal shown when Chronos starts with an .ics file argument."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(
        self,
        *,
        file_label: str,
        calendars: tuple[CalendarRef, ...],
        on_add_sync: Callable[[CalendarRef], None],
        on_add_only: Callable[[CalendarRef], None],
    ) -> None:
        super().__init__()
        self._file_label = file_label
        self._calendars = calendars
        self._on_add_sync = on_add_sync
        self._on_add_only = on_add_only

    def compose(self) -> ComposeResult:
        with Vertical(id="import-ics-box", classes="dialog-box"):
            yield Label("Import .ics event", classes="dialog-title")
            yield Label(f"File: {self._file_label}")
            yield Label("Target calendar:")
            yield Select(
                ((self._calendar_label(c), c) for c in self._calendars),
                value=self._calendars[0],
                allow_blank=False,
                id="import-ics-calendar",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("add+sync", id="import-ics-add-sync", variant="primary")
                yield Button("add", id="import-ics-add", variant="default")
                yield Button("cancel", id="import-ics-cancel", variant="default")

    def on_mount(self) -> None:
        self.query_one("#import-ics-add-sync", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "import-ics-add-sync":
            cal = self._selected_calendar()
            self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]
            self._on_add_sync(cal)
            return
        if event.button.id == "import-ics-add":
            cal = self._selected_calendar()
            self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]
            self._on_add_only(cal)
            return
        self.action_cancel()

    def action_cancel(self) -> None:
        self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]

    def _selected_calendar(self) -> CalendarRef:
        select = self.query_one("#import-ics-calendar", Select)
        value = cast("object", select.value)
        if not isinstance(value, CalendarRef):
            return self._calendars[0]
        return value

    @staticmethod
    def _calendar_label(ref: CalendarRef) -> str:
        return f"{ref.account_name} / {ref.calendar_name}"


__all__ = ["ImportIcsScreen"]
