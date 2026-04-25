from __future__ import annotations

from collections.abc import Sequence

from textual.widgets import Tree

from chronos.domain import CalendarRef


class CalendarPanel(Tree[CalendarRef | None]):
    """Per-account collapsible tree of calendars.

    Selection drives `MainScreen`'s active `CalendarSelection`. The
    root represents "all calendars".
    """

    def __init__(self) -> None:
        super().__init__("All calendars", data=None)
        self.show_root = True
        self.guide_depth = 2

    def populate(self, calendars: Sequence[CalendarRef]) -> None:
        self.clear()
        self.root.data = None
        by_account: dict[str, list[CalendarRef]] = {}
        for ref in calendars:
            by_account.setdefault(ref.account_name, []).append(ref)
        for account_name in sorted(by_account):
            account_node = self.root.add(account_name, data=None, expand=True)
            for cal_ref in sorted(
                by_account[account_name], key=lambda r: r.calendar_name
            ):
                account_node.add_leaf(cal_ref.calendar_name, data=cal_ref)
        self.root.expand()


__all__ = ["CalendarPanel"]
