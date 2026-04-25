from __future__ import annotations

from collections.abc import Callable
from datetime import date

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer

from chronos.domain import StoredComponent
from chronos.tui.bindings import detail_bindings
from chronos.tui.widgets.event_view import EventView


class EventDetailScreen(Screen[None]):
    """Read-only modal showing one component's details."""

    BINDINGS = detail_bindings()

    def __init__(
        self,
        component: StoredComponent,
        *,
        today: date,
        on_edit: Callable[[StoredComponent], None],
    ) -> None:
        super().__init__()
        self._component = component
        self._today = today
        self._on_edit = on_edit

    def compose(self) -> ComposeResult:
        with Vertical(id="event-detail"):
            view = EventView()
            yield view
        yield Footer()

    def on_mount(self) -> None:
        view: EventView = self.query_one(EventView)
        view.show(self._component, today=self._today)

    def action_close(self) -> None:
        self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]

    def action_edit(self) -> None:
        self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]
        self._on_edit(self._component)


__all__ = ["EventDetailScreen"]
