from __future__ import annotations

from collections.abc import Callable, Sequence

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Input, Label, ListItem, ListView

from chronos.domain import StoredComponent
from chronos.tui.bindings import search_bindings
from chronos.tui.views import search_components


class SearchDialogScreen(Screen[None]):
    """Live search dialog over already-loaded components.

    The TUI keeps an in-memory snapshot of the visible calendars'
    components and runs `views.search_components` on each keystroke;
    the FTS5 index is reserved for the MCP server and CLI.

    Selecting a result calls `on_select(ref)`.
    """

    BINDINGS = search_bindings()

    def __init__(
        self,
        components: Sequence[StoredComponent],
        on_select: Callable[[StoredComponent], None],
    ) -> None:
        super().__init__()
        self._components = tuple(components)
        self._on_select = on_select
        self._matches: tuple[StoredComponent, ...] = ()

    def compose(self) -> ComposeResult:
        with Vertical(id="search-dialog"):
            yield Label("Search:", id="search-label")
            yield Input(placeholder="type to filter…", id="search-input")
            yield ListView(id="search-results")
        yield Footer()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "search-input":
            return
        self._matches = search_components(
            components=self._components, query=event.value
        )
        results: ListView = self.query_one("#search-results", ListView)
        results.clear()
        for component in self._matches:
            label = component.summary or "(no summary)"
            results.append(ListItem(Label(label)))

    def action_submit(self) -> None:
        results: ListView = self.query_one("#search-results", ListView)
        index = results.index
        if index is None or not (0 <= index < len(self._matches)):
            return
        component = self._matches[index]
        self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]
        self._on_select(component)

    def action_cancel(self) -> None:
        self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]


__all__ = ["SearchDialogScreen"]
