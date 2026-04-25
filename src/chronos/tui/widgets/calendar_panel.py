from __future__ import annotations

from collections.abc import Callable, Sequence

from rich.text import Text
from textual.binding import Binding
from textual.widgets import Tree

from chronos.domain import CalendarRef
from chronos.tui.views import CalendarSelection


class CalendarPanel(Tree[CalendarRef | None]):
    """Per-account collapsible tree of calendars.

    Each leaf is a calendar; pressing Enter toggles its membership in
    the parent screen's `CalendarSelection`. An empty selection still
    means "all calendars" (consistent with `CalendarSelection.contains`),
    so the user only ever has to flip on what they want hidden — and
    flipping everything off lands back on "show all", which is the
    least-surprising recovery from "I clicked too many things".
    """

    BINDINGS = [
        Binding("enter", "toggle_calendar", "Toggle calendar", show=False),
    ]

    def __init__(
        self,
        on_selection_change: Callable[[CalendarSelection], None] | None = None,
    ) -> None:
        super().__init__("All calendars", data=None)
        self.show_root = True
        self.guide_depth = 2
        self._selection: frozenset[CalendarRef] = frozenset()
        # Tracking known calendars lets us re-render labels with their
        # current `[x]` / `[ ]` prefix without relying on Tree node
        # identity across `populate()` calls.
        self._known: tuple[CalendarRef, ...] = ()
        self._on_selection_change = on_selection_change

    def populate(self, calendars: Sequence[CalendarRef]) -> None:
        self.clear()
        self.root.data = None
        self._known = tuple(calendars)
        by_account: dict[str, list[CalendarRef]] = {}
        for ref in calendars:
            by_account.setdefault(ref.account_name, []).append(ref)
        for account_name in sorted(by_account):
            account_node = self.root.add(account_name, data=None, expand=True)
            for cal_ref in sorted(
                by_account[account_name], key=lambda r: r.calendar_name
            ):
                account_node.add_leaf(self._render_label(cal_ref), data=cal_ref)
        self.root.expand()

    def selection(self) -> CalendarSelection:
        return CalendarSelection(refs=self._selection)

    def set_selection(self, selection: CalendarSelection) -> None:
        """Replace the panel's current selection and re-render labels."""
        self._selection = frozenset(selection.refs)
        self._refresh_labels()

    def action_toggle_calendar(self) -> None:
        cursor = self.cursor_node
        if cursor is None:
            return
        ref = cursor.data
        if ref is None:
            return
        if ref in self._selection:
            self._selection = self._selection - {ref}
        else:
            self._selection = self._selection | {ref}
        self._refresh_labels()
        if self._on_selection_change is not None:
            self._on_selection_change(self.selection())

    def _refresh_labels(self) -> None:
        # `Tree.add_leaf` returns a node we don't track; walk the
        # whole tree and update each leaf in place. Cheap — calendar
        # counts are tiny.
        for node in self.root.children:
            for leaf in node.children:
                ref = leaf.data
                if isinstance(ref, CalendarRef):
                    leaf.set_label(self._render_label(ref))

    def _render_label(self, ref: CalendarRef) -> Text:
        # Returning a `Text` object skips Textual's Rich-markup parsing
        # of label strings — without this, `[x]` is treated as a tag
        # and the brackets disappear from the rendered label.
        marker = "[x]" if ref in self._selection else "[ ]"
        return Text(f"{marker} {ref.calendar_name}")


__all__ = ["CalendarPanel"]
