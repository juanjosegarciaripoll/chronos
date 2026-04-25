from __future__ import annotations

from datetime import date

from textual.widgets import Static

from chronos.domain import StoredComponent
from chronos.tui.views import render_event_detail


class EventView(Static):
    """Read-only renderer for one VEvent or VTodo.

    Implementation defers to `views.render_event_detail`, which is a
    pure function — easy to unit-test without a Textual app.
    """

    def show(self, component: StoredComponent | None, *, today: date) -> None:
        if component is None:
            self.update("(no event selected)")
            return
        self.update(render_event_detail(component, today))


__all__ = ["EventView"]
